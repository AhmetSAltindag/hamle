"""Synthetic adapter evidence only; no provider calls or customer acceptance."""
import copy
import json
import unittest

from autonomy_policy import scope_digest
from test_v11_quality_gate import fixture as quality_fixture, human_approval, stamp
from v11_production_stages import (ProductionError, advance_production, delivery_manifest,
                                   initialize_production, ready_stages)


def fixture(mode='internal_pilot'):
    brief = {'verified': True, 'version': 1, 'evidence_ref': 'synthetic-brief',
             'brand_id': 'wat_mobilite', 'service_id': 'synthetic-design',
             'reply_envelope': {'thread_id': 'synthetic-thread-1'},
             'deliverables': [{'id': 'design', 'kind': 'graphic', 'purpose': 'source', 'width': 1080},
                              {'id': 'video', 'kind': 'video', 'purpose': 'final', 'duration': 5}],
             'binding': {'account_id': 1, 'project_id': 2, 'list_id': 3, 'subscriber_ids': [7, 8, 9],
                         'production_stages': [
                             {'id': 'design', 'kinds': ['graphic'], 'depends_on': [], 'assignee_ids': [4]},
                             {'id': 'motion', 'kinds': ['video'], 'depends_on': ['design'], 'assignee_ids': [5]}]}}
    return initialize_production('synthetic-work-1', brief, mode=mode)


def event(state, kind, stage='design', **kwargs):
    return {'id': 'event-' + str(state['revision']) + '-' + kind, 'type': kind,
            'work_id': state['work_id'], 'expected_revision': state['revision'],
            'stage_id': stage, 'actor_id': 7, 'verified': True,
            'evidence_ref': 'synthetic-event-readback', 'occurred_at': stamp(), **kwargs}


def apply(state, kind, stage='design', **kwargs):
    result = advance_production(state, event(state, kind, stage, **kwargs))
    if result['status'] != 'applied':
        raise AssertionError(result['reason'])
    return result['state']


def task(state, stage='design'):
    return {'account_id': 1, 'project_id': 2, 'list_id': 3,
            'task_id': 10 if stage == 'design' else 11,
            'provider': 'basecamp', 'verified': True, 'evidence_ref': 'synthetic-task-readback',
            'customer_visible': False, 'assignee_ids': [4] if stage == 'design' else [5],
            'brief_sha256': state['brief_sha256']}


def start(state, stage='design'):
    state = apply(state, 'bind_stage_task', stage, task=task(state, stage))
    return apply(state, 'begin_stage', stage)


def submission(state, stage='design'):
    record = state['stages'][stage]
    reqs = {o['id']: o for o in state['brief']['deliverables']}
    outputs = []
    for oid in record['spec']['output_ids']:
        req = reqs[oid]
        outputs.append({'id': oid, 'kind': req['kind'], 'purpose': req['purpose'],
                        'sha256': ('a' if stage == 'design' else 'b') * 64,
                        'size_bytes': 256, 'mime_type': 'image/png' if stage == 'design' else 'video/mp4',
                        'provider_attachment_id': 'synthetic-upload-' + stage,
                        'inspection': {'verified': True, 'bytes_read': True, 'readable': True,
                                       'sha256': ('a' if stage == 'design' else 'b') * 64,
                                       'size_bytes': 256, 'evidence_ref': 'synthetic-byte-read',
                                       'inspected_at': stamp(-90)}})
    return {'verified': True, 'evidence_ref': 'synthetic-producer-upload',
            'producer_id': record['spec']['assignee_ids'][0], 'task_id': record['task']['task_id'],
            'origin': 'internal_fixture', 'brief_sha256': state['brief_sha256'],
            'dependency_acceptances': copy.deepcopy(record['dependency_acceptances']),
            'submitted_at': stamp(-120), 'artifact': {'version': 'v1', 'sha256': outputs[0]['sha256']},
            'outputs': outputs, 'manifest_sha256': scope_digest(outputs)}


def submit(state, stage='design'):
    return apply(state, 'submit_outputs', stage, actor_id=state['stages'][stage]['spec']['assignee_ids'][0],
                 submission=submission(state, stage))


def quality_event(state, stage='design', score='90', approve=False):
    policy, assessment, binding, safety = quality_fixture(score)
    record = state['stages'][stage]['submission']
    binding.update(brief_sha256=state['brief_sha256'], manifest_sha256=record['manifest_sha256'],
                   sha256=record['artifact']['sha256'], version=record['artifact']['version'])
    assessment['binding'] = copy.deepcopy(binding)
    safety['binding'] = copy.deepcopy(binding)
    for row in list(assessment['critical_checks'].values()) + list(assessment['criteria'].values()):
        row['binding'] = copy.deepcopy(binding)
        for observation in row['observations']:
            observation.update(artifact_sha256=binding['sha256'], brief_sha256=binding['brief_sha256'])
    result = event(state, 'assess_stage', stage, policy=policy, assessment=assessment,
                   binding=binding, safety=safety)
    if approve:
        result['human_approval'] = human_approval(policy, assessment, binding, score)
    return result


def accept(state, stage='design'):
    result = advance_production(state, quality_event(state, stage))
    if result['status'] != 'applied' or result['state']['stages'][stage]['status'] != 'accepted':
        raise AssertionError(result)
    return result['state']


class ProductionStageTests(unittest.TestCase):
    def setUp(self):
        self.state = fixture()

    def test_two_graphic_stages_can_split_explicit_output_ids(self):
        brief = copy.deepcopy(self.state['brief'])
        brief['deliverables'][0].update(id='main', purpose='final')
        brief['deliverables'][1].update(id='story', kind='graphic')
        brief['binding']['production_stages'] = [
            {'id': 'main', 'kinds': ['graphic'], 'output_ids': ['main'],
             'depends_on': [], 'assignee_ids': [4]},
            {'id': 'story', 'kinds': ['graphic'], 'output_ids': ['story'],
             'depends_on': ['main'], 'assignee_ids': [4]}]
        state = initialize_production('synthetic-work-1', brief, mode='internal_pilot')
        self.assertEqual([s['id'] for s in ready_stages(state)], ['main'])
        self.assertEqual(state['stages']['story']['spec']['output_ids'], ['story'])

    def test_two_stages_cannot_claim_same_output(self):
        brief = copy.deepcopy(self.state['brief'])
        brief['binding']['production_stages'][1].update(kinds=['graphic'], output_ids=['design'])
        with self.assertRaisesRegex(ProductionError, 'coverage_invalid'):
            initialize_production('synthetic-work-1', brief)

    def test_only_initial_stage_is_ready(self):
        self.assertEqual([s['id'] for s in ready_stages(self.state)], ['design'])
        self.assertFalse(self.state['customer_send_allowed'])

    def test_state_roundtrips_json(self):
        self.assertEqual(json.loads(json.dumps(self.state)), self.state)

    def test_purpose_required_for_every_output(self):
        brief = copy.deepcopy(self.state['brief'])
        del brief['deliverables'][0]['purpose']
        with self.assertRaisesRegex(ProductionError, 'purpose_invalid'):
            initialize_production('work', brief)

    def test_cycle_or_future_dependency_rejected(self):
        brief = copy.deepcopy(self.state['brief'])
        brief['binding']['production_stages'][0]['depends_on'] = ['motion']
        with self.assertRaisesRegex(ProductionError, 'dependencies_invalid'):
            initialize_production('work', brief)

    def test_missing_output_stage_rejected(self):
        brief = copy.deepcopy(self.state['brief'])
        brief['binding']['production_stages'].pop()
        with self.assertRaisesRegex(ProductionError, 'coverage_incomplete'):
            initialize_production('work', brief)

    def test_downstream_task_cannot_be_bound_before_accepted_design(self):
        result = advance_production(self.state, event(self.state, 'bind_stage_task', 'motion', task=task(self.state, 'motion')))
        self.assertEqual(result['reason'], 'stage_dependencies_not_accepted')
        self.assertEqual(result['state'], self.state)

    def test_unverified_provider_binding_rejected(self):
        target = task(self.state)
        target['verified'] = False
        result = advance_production(self.state, event(self.state, 'bind_stage_task', task=target))
        self.assertEqual(result['reason'], 'stage_task_readback_required')

    def test_customer_visible_task_rejected(self):
        target = task(self.state)
        target['customer_visible'] = True
        result = advance_production(self.state, event(self.state, 'bind_stage_task', task=target))
        self.assertEqual(result['reason'], 'stage_task_readback_required')

    def test_cross_project_task_rejected(self):
        target = task(self.state)
        target['project_id'] = 200
        result = advance_production(self.state, event(self.state, 'bind_stage_task', task=target))
        self.assertEqual(result['reason'], 'stage_task_target_mismatch')

    def test_cannot_begin_without_real_task_readback(self):
        result = advance_production(self.state, event(self.state, 'begin_stage'))
        self.assertEqual(result['reason'], 'stage_not_ready_to_start')

    def test_stale_state_event_rejected(self):
        ev = event(self.state, 'stop', expected_revision=100)
        self.assertEqual(advance_production(self.state, ev)['reason'], 'state_revision_conflict')

    def test_exact_repeated_event_noop_after_newer_events(self):
        ev = event(self.state, 'stop')
        state = advance_production(self.state, ev)['state']
        state = apply(state, 'resume')
        result = advance_production(state, ev)
        self.assertTrue(result['duplicate'])
        self.assertEqual(result['state'], state)

    def test_reused_event_identity_with_changed_payload_conflicts(self):
        ev = event(self.state, 'stop')
        state = advance_production(self.state, ev)['state']
        ev['evidence_ref'] = 'changed'
        self.assertEqual(advance_production(state, ev)['reason'], 'event_identity_conflict')

    def test_human_stop_blocks_progress_until_explicit_resume(self):
        state = apply(self.state, 'stop')
        self.assertEqual(ready_stages(state), [])
        result = advance_production(state, event(state, 'bind_stage_task', task=task(state)))
        self.assertEqual(result['reason'], 'production_stopped')
        self.assertEqual(apply(state, 'resume')['control'], 'active')

    def test_unassigned_producer_submission_rejected(self):
        state = start(self.state)
        result = advance_production(state, event(state, 'submit_outputs', actor_id=8, submission=submission(state)))
        self.assertEqual(result['reason'], 'submission_actor_not_assigned')

    def test_read_only_assessment_is_not_production(self):
        state = start(self.state)
        sub = submission(state)
        sub['origin'] = 'historical_reference_review'
        result = advance_production(state, event(state, 'submit_outputs', actor_id=4, submission=sub))
        self.assertEqual(result['reason'], 'real_production_submission_required')

    def test_internal_generated_fixture_never_accepted_in_production_mode(self):
        state = start(fixture('production'))
        result = advance_production(state, event(state, 'submit_outputs', actor_id=4, submission=submission(state)))
        self.assertEqual(result['reason'], 'real_production_submission_required')

    def test_byte_reading_required_not_only_url_metadata(self):
        state = start(self.state)
        sub = submission(state)
        sub['outputs'][0]['inspection']['bytes_read'] = False
        sub['manifest_sha256'] = scope_digest(sub['outputs'])
        result = advance_production(state, event(state, 'submit_outputs', actor_id=4, submission=sub))
        self.assertEqual(result['reason'], 'real_output_byte_inspection_required')

    def test_source_output_cannot_be_relabelled_final(self):
        state = start(self.state)
        sub = submission(state)
        sub['outputs'][0]['purpose'] = 'final'
        sub['manifest_sha256'] = scope_digest(sub['outputs'])
        result = advance_production(state, event(state, 'submit_outputs', actor_id=4, submission=sub))
        self.assertEqual(result['reason'], 'output_purpose_or_kind_mismatch')

    def test_changed_file_breaks_inspection_binding(self):
        state = start(self.state)
        sub = submission(state)
        sub['outputs'][0]['sha256'] = 'f' * 64
        result = advance_production(state, event(state, 'submit_outputs', actor_id=4, submission=sub))
        self.assertEqual(result['reason'], 'real_output_byte_inspection_required')

    def test_future_inspection_rejected(self):
        state = start(self.state)
        sub = submission(state)
        sub['outputs'][0]['inspection']['inspected_at'] = stamp(100)
        result = advance_production(state, event(state, 'submit_outputs', actor_id=4, submission=sub))
        self.assertEqual(result['reason'], 'output_inspection_time_invalid')

    def test_changed_manifest_hash_rejected(self):
        state = start(self.state)
        sub = submission(state)
        sub['manifest_sha256'] = 'f' * 64
        result = advance_production(state, event(state, 'submit_outputs', actor_id=4, submission=sub))
        self.assertEqual(result['reason'], 'output_manifest_hash_mismatch')

    def test_90_accepts_design_then_exposes_motion(self):
        state = accept(submit(start(self.state)))
        self.assertEqual([s['id'] for s in ready_stages(state)], ['motion'])
        self.assertEqual(state['status'], 'awaiting_production')

    def test_89_needs_human_approval(self):
        state = submit(start(self.state))
        result = advance_production(state, quality_event(state, score='89'))
        self.assertEqual(result['state']['stages']['design']['status'], 'awaiting_quality_approval')
        self.assertEqual(ready_stages(result['state']), [])

    def test_80_with_authorized_exact_approval_accepts(self):
        state = submit(start(self.state))
        result = advance_production(state, quality_event(state, score='80', approve=True))
        self.assertEqual(result['state']['stages']['design']['status'], 'accepted')

    def test_79_requests_revision_without_downstream_start(self):
        state = submit(start(self.state))
        result = advance_production(state, quality_event(state, score='79'))
        self.assertEqual(result['state']['stages']['design']['status'], 'changes_requested')
        self.assertEqual(ready_stages(result['state']), [])

    def test_revision_accepts_fresh_producer_resubmission(self):
        state = submit(start(self.state))
        state = advance_production(state, quality_event(state, score='79'))['state']
        state = submit(state)
        self.assertEqual(state['stages']['design']['status'], 'awaiting_quality')
        self.assertEqual(len(state['stages']['design']['history']), 1)

    def test_changed_quality_brief_binding_rejected(self):
        state = submit(start(self.state))
        ev = quality_event(state)
        ev['binding']['brief_sha256'] = 'f' * 64
        self.assertEqual(advance_production(state, ev)['reason'], 'stage_quality_binding_mismatch')

    def test_critical_failure_overrides_high_score(self):
        state = submit(start(self.state))
        ev = quality_event(state, score='100')
        ev['assessment']['critical_checks']['required_files']['result'] = 'failed'
        result = advance_production(state, ev)
        self.assertEqual(result['state']['stages']['design']['status'], 'changes_requested')

    def test_motion_submission_binds_accepted_design_exactly(self):
        state = start(accept(submit(start(self.state))), 'motion')
        sub = submission(state, 'motion')
        sub['dependency_acceptances'] = {'design': 'f' * 64}
        result = advance_production(state, event(state, 'submit_outputs', 'motion', actor_id=5, submission=sub))
        self.assertEqual(result['reason'], 'submission_dependency_mismatch')

    def test_complete_chain_manifest_excludes_source(self):
        state = accept(submit(start(self.state)))
        state = accept(submit(start(state, 'motion'), 'motion'), 'motion')
        manifest = delivery_manifest(state)
        self.assertEqual(state['status'], 'production_complete')
        self.assertEqual([o['id'] for o in manifest['outputs']], ['video'])
        self.assertFalse(manifest['customer_send_allowed'])
        self.assertEqual(manifest['mode'], 'internal_pilot')

    def test_incomplete_production_cannot_form_delivery_manifest(self):
        with self.assertRaisesRegex(ProductionError, 'production_not_complete'):
            delivery_manifest(self.state)

    def test_question_invalidates_accepted_design_and_downstream_submission(self):
        state = accept(submit(start(self.state)))
        state = submit(start(state, 'motion'), 'motion')
        state = apply(state, 'raise_question', actor_id=4, task_id=10,
                      question_id='question-1', question_ref='synthetic-question')
        self.assertEqual(state['status'], 'held_for_question')
        self.assertIsNone(state['stages']['design']['acceptance'])
        self.assertIsNone(state['stages']['motion']['submission'])
        self.assertIsNotNone(state['stages']['design']['history'][0]['acceptance'])
        self.assertIsNotNone(state['stages']['motion']['history'][0]['submission'])
        self.assertEqual(ready_stages(state), [])

    def test_question_from_unassigned_actor_rejected(self):
        state = start(self.state)
        result = advance_production(state, event(state, 'raise_question', actor_id=5, task_id=10,
                                    question_id='q', question_ref='synthetic-question'))
        self.assertEqual(result['reason'], 'event_actor_not_authorized')

    def test_brief_amendment_resolves_question_but_requires_new_readback_and_outputs(self):
        state = start(self.state)
        state = apply(state, 'raise_question', actor_id=4, task_id=10, question_id='q', question_ref='question')
        brief = copy.deepcopy(state['brief'])
        brief['version'] = 2
        brief['deliverables'][0]['width'] = 1200
        state = apply(state, 'amend_brief', brief=brief, resolves_question_ids=['q'])
        self.assertEqual(state['questions']['q']['status'], 'resolved')
        self.assertEqual([s['id'] for s in ready_stages(state)], ['design'])
        result = advance_production(state, event(state, 'begin_stage'))
        self.assertEqual(result['reason'], 'task_needs_current_brief_readback')
        state = start(state)
        self.assertEqual(state['stages']['design']['status'], 'running')
        self.assertTrue(any(h['reason'] == 'old_brief' for h in state['history'] if 'reason' in h))

    def test_brief_amendment_cannot_change_route_or_producer(self):
        brief = copy.deepcopy(self.state['brief'])
        brief['version'] = 2
        brief['binding']['project_id'] = 999
        result = advance_production(self.state, event(self.state, 'amend_brief', brief=brief))
        self.assertEqual(result['reason'], 'amendment_route_change_requires_replan')

    def test_cross_work_event_rejected(self):
        result = advance_production(self.state, event(self.state, 'stop', work_id='different-work'))
        self.assertEqual(result['reason'], 'event_work_mismatch')

    def test_quality_cannot_skip_real_production(self):
        policy, assessment, binding, safety = quality_fixture()
        ev = event(self.state, 'assess_stage', policy=policy, assessment=assessment, binding=binding, safety=safety)
        self.assertEqual(advance_production(self.state, ev)['reason'], 'stage_submission_required_for_quality')


if __name__ == '__main__':
    unittest.main()
