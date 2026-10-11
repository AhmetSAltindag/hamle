"""Offline adapter flows: no real messages, Basecamp writes or model calls."""
import copy
import unittest

from autonomy_policy import scope_digest
from test_autonomy_policy import NOW, fixture as authority_fixture, stamp
from v11_delivery import DeliveryCoordinator, DeliveryError, new_state, record_outcome


def fixture(work_id='work-1', task_id=41, thread_id='thread-1'):
    scope, request = authority_fixture()
    request['work_id'] = request['control']['work_id'] = request['delegation']['work_id'] = work_id
    request['source']['thread_id'] = request['delegation']['thread_id'] = thread_id
    work = {'work_id': work_id, 'brand_id': scope['brand_id'], 'service_id': 'coordination',
            'thread_id': thread_id, 'account_id': 10, 'project_id': 20, 'list_id': 30,
            'task_id': task_id, 'revision_index': 0,
            'brief': {'verified': True, 'evidence_ref': 'synthetic-brief',
                      'required_deliverables': [{'id': 'main', 'mime_types': ['application/pdf'],
                                                 'min_bytes': 1, 'max_bytes': 100000,
                                                 'facts': {'page_count': 1}}],
                      'semantic_checks': ['matches_brief', 'language'],
                      'factual_checks': ['brand_identity', 'no_invented_claims']}}
    artifact = {'brand_id': scope['brand_id'], 'work_id': work_id, 'version': 'v1',
                'sha256': 'b' * 64, 'mime_type': 'application/pdf', 'evidence_ref': 'synthetic-byte-readback',
                'deliverables': [{'id': 'main', 'sha256': 'b' * 64, 'mime_type': 'application/pdf',
                                  'size_bytes': 200, 'facts': {'page_count': 1},
                                  'facts_bound_sha256': 'b' * 64, 'evidence_ref': 'synthetic-pdf-parser'}]}
    binding = {'brand_id': work['brand_id'], 'work_id': work_id, 'version': 'v1', 'sha256': 'b' * 64,
               'brief_sha256': scope_digest(work['brief']), 'manifest_sha256': scope_digest(artifact['deliverables']),
               'scope_digest': scope_digest(scope), 'source_fingerprint': request['source']['fingerprint'],
               'recipients_sha256': scope_digest({'to': request['reply_to'], 'cc': request['reply_cc']})}
    qa = {**binding, 'verified': True, 'accepted': True, 'evidence_ref': 'synthetic-qa',
          'policy_ref': 'qa-v1', 'assets_ref': 'assets-v1', 'internal_notes_excluded': True,
          'rights_checked': True, 'checked_at': stamp(-50),
          'semantic_checks': {'matches_brief': True, 'language': True},
          'factual_checks': {'brand_identity': True, 'no_invented_claims': True}}
    body = 'İstediğiniz çalışma ektedir.'
    payload = {'from': 'hamle@gamechangers.com.tr', 'to': request['reply_to'], 'cc': request['reply_cc'],
               'thread_id': thread_id, 'subject': 'Re: Synthetic brief', 'body': body,
               'artifact': {k: artifact[k] for k in ('brand_id', 'work_id', 'version', 'sha256', 'mime_type')}}
    release = {**binding, 'verified': True, 'actor': 'producer@example.test', 'revoked': False,
               'customer_delivery': True, 'evidence_ref': 'synthetic-release',
               'issued_at': stamp(-40), 'expires_at': stamp(300),
               'reply_to': request['reply_to'].copy(), 'reply_cc': request['reply_cc'].copy(),
               'service_id': 'coordination', 'action_payload_sha256': scope_digest(payload)}
    snapshot = {'binding': {k: work[k] for k in ('work_id', 'brand_id', 'service_id', 'thread_id', 'account_id', 'project_id', 'list_id', 'task_id')},
                'observed_at': stamp(-1), 'evidence_ref': 'synthetic-basecamp-current',
                'previous_cursor': None, 'cursor': 'cursor-1', 'has_more': False, 'completed': True,
                'task_fingerprint': 'd' * 64, 'assignee_ids': [40],
                'events': [{'id': 'comment-1', 'task_id': task_id, 'source_order': 1,
                            'evidence_ref': 'synthetic-comment-readback'}],
                'artifact': artifact, 'producer_submission': {**binding, 'verified': True, 'actor_id': 40,
                                                             'issued_at': stamp(-60), 'evidence_ref': 'synthetic-submit'},
                'qa': qa, 'release': release, 'customer_subject': 'Re: Synthetic brief', 'customer_body': body}
    return work, FakeAdapter(scope, request, snapshot)


class FakeAdapter:
    def __init__(self, scope, request, snapshot):
        self.scope, self.request, self.snapshot = scope, request, snapshot
        self.reads = []

    def read_basecamp(self, work, *, after_cursor):
        self.reads.append(('basecamp', work['work_id'], after_cursor))
        out = copy.deepcopy(self.snapshot)
        out['previous_cursor'] = after_cursor
        if after_cursor == out['cursor']:
            out['events'] = []
        return out

    def read_authority(self, work):
        self.reads.append(('authority', work['work_id']))
        return copy.deepcopy({'scope': self.scope, 'request': self.request})


def exact_receipt(intent):
    return {'provider_readback_verified': True, 'payload_sha256': intent['payload_sha256'],
            'action_id': intent['action_id'], 'evidence_ref': 'synthetic-provider-readback'}


def refresh_binding(work, adapter):
    """New version is explicitly re-submitted; do not silently refresh QA/release."""
    a = adapter.snapshot['artifact']
    adapter.snapshot['producer_submission'].update(
        version=a['version'], sha256=a['sha256'], brief_sha256=scope_digest(work['brief']),
        manifest_sha256=scope_digest(a['deliverables']))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.work, self.adapter = fixture()
        self.coordinator = DeliveryCoordinator()

    def poll(self, state=None):
        return self.coordinator.poll(self.adapter, self.work, state, now=NOW)

    def held(self, reason):
        result = self.poll()
        self.assertEqual(result['state']['status'], 'held', result)
        self.assertEqual(result['state']['reason'], reason, result)
        self.assertEqual(result['intents'], [])

    def test_trusted_versioned_delivery_intent_preserves_thread_and_cc(self):
        result = self.poll()
        self.assertEqual(result['state']['status'], 'ready_delivery', result)
        intent, = result['intents']
        self.assertEqual(intent['payload']['thread_id'], 'thread-1')
        self.assertEqual(intent['payload']['cc'], ['cc@example.test'])
        self.assertEqual(intent['kind'], 'deliver_file')
        self.assertFalse(intent['live_enabled'])
        self.assertEqual((result['network_writes'], result['model_calls']), (0, 0))

    def test_done_todo_alone_never_means_ready(self):
        self.adapter.snapshot.pop('producer_submission')
        result = self.poll()
        self.assertEqual(result['state']['status'], 'awaiting_production')
        self.assertEqual(result['intents'], [])

    def test_no_attached_artifact_awaits_production(self):
        self.adapter.snapshot.pop('artifact')
        self.assertEqual(self.poll()['state']['status'], 'awaiting_production')

    def test_explicit_submission_without_qa_awaits_qa(self):
        self.adapter.snapshot['qa'] = None
        result = self.poll()
        self.assertEqual(result['state']['status'], 'ready_qa')
        self.assertEqual(result['intents'], [])

    def test_qa_without_trusted_final_release_cannot_send(self):
        self.adapter.snapshot['release'] = None
        result = self.poll()
        self.assertEqual(result['state']['reason'], 'trusted_producer_release_required')
        self.assertEqual(result['intents'], [])

    def test_wrong_format_goes_to_internal_feedback_only(self):
        self.adapter.snapshot['artifact']['deliverables'][0]['mime_type'] = 'application/octet-stream'
        refresh_binding(self.work, self.adapter)
        result = self.poll()
        self.assertEqual(result['state']['status'], 'changes_requested', result)
        intent, = result['intents']
        self.assertEqual(intent['kind'], 'add_comment')
        self.assertEqual(set(intent['payload']), {'target', 'body'})
        self.assertIn('main:format', intent['payload']['body'])
        self.assertNotIn('customer', intent['payload']['body'])

    def test_changed_bytes_with_same_version_and_problem_get_new_feedback_identity(self):
        self.adapter.snapshot['artifact']['deliverables'][0]['mime_type'] = 'application/octet-stream'
        refresh_binding(self.work, self.adapter)
        first = self.poll()
        original = first['intents'][0]
        state = record_outcome(first['state'], original, status='confirmed', receipt=exact_receipt(original))
        self.adapter.snapshot['artifact']['sha256'] = 'e' * 64
        refresh_binding(self.work, self.adapter)
        second = self.poll(state)
        self.assertEqual(len(second['intents']), 1, second)
        self.assertNotEqual(second['intents'][0]['action_id'], original['action_id'])

    def test_boolean_producer_id_is_not_a_real_verified_person_id(self):
        self.adapter.scope['services'][0]['assignee_ids'] = [1]
        self.adapter.snapshot['assignee_ids'] = [1]
        digest = scope_digest(self.adapter.scope)
        self.adapter.request['delegation']['scope_digest'] = digest
        for key in ('producer_submission', 'qa', 'release'):
            self.adapter.snapshot[key]['scope_digest'] = digest
        self.adapter.snapshot['producer_submission']['actor_id'] = True
        self.held('producer_submission_unverified')

    def test_failed_factual_constraint_produces_internal_revision(self):
        self.adapter.snapshot['artifact']['deliverables'][0]['facts']['page_count'] = 2
        refresh_binding(self.work, self.adapter)
        result = self.poll()
        self.assertEqual(result['intents'][0]['kind'], 'add_comment', result)
        self.assertIn('main:facts', result['intents'][0]['payload']['body'])

    def test_failed_semantic_qa_is_not_delivery(self):
        self.adapter.snapshot['qa']['semantic_checks']['matches_brief'] = False
        result = self.poll()
        self.assertEqual(result['state']['status'], 'changes_requested')
        self.assertEqual(result['intents'][0]['kind'], 'add_comment')

    def test_model_overall_pass_cannot_override_failed_facts(self):
        self.adapter.snapshot['qa']['factual_checks']['brand_identity'] = False
        self.assertEqual(self.poll()['intents'][0]['kind'], 'add_comment')

    def test_internal_feedback_not_written_in_customer_visible_project(self):
        self.adapter.scope['basecamp']['customer_visible'] = True
        digest = scope_digest(self.adapter.scope)
        self.adapter.request['delegation']['scope_digest'] = digest
        for name in ('producer_submission', 'qa', 'release'):
            self.adapter.snapshot[name]['scope_digest'] = digest
        self.adapter.snapshot['qa']['semantic_checks']['language'] = False
        self.held('internal_feedback_requires_private_project')

    def test_changed_bytes_invalidates_previous_submission(self):
        self.adapter.snapshot['artifact']['sha256'] = 'c' * 64
        self.held('producer_submission_version_or_brief_changed')

    def test_resubmitted_new_bytes_need_new_qa(self):
        self.adapter.snapshot['artifact']['sha256'] = 'c' * 64
        self.adapter.snapshot['artifact']['version'] = 'v2'
        refresh_binding(self.work, self.adapter)
        self.held('artifact_or_brief_changed_after_qa')

    def test_changed_brief_cannot_reuse_old_qa(self):
        self.work['brief']['required_deliverables'][0]['max_bytes'] += 1
        refresh_binding(self.work, self.adapter)
        self.held('artifact_or_brief_changed_after_qa')

    def test_file_facts_bound_to_wrong_bytes_hold(self):
        self.adapter.snapshot['artifact']['deliverables'][0]['facts_bound_sha256'] = 'f' * 64
        self.held('artifact_manifest_unverified')

    def test_missing_required_deliverable_holds(self):
        self.adapter.snapshot['artifact']['deliverables'] = []
        self.held('required_deliverables_mismatch')

    def test_source_reply_after_qa_requires_updated_evidence(self):
        self.adapter.request['source']['fingerprint'] = 'f' * 64
        self.held('producer_submission_version_or_brief_changed')

    def test_omitted_cc_cannot_reuse_release(self):
        self.adapter.request['reply_cc'] = []
        self.held('producer_submission_version_or_brief_changed')

    def test_changed_customer_body_invalidates_final_release(self):
        self.adapter.snapshot['customer_body'] = 'Different proposed message'
        self.held('artifact_release_destination_or_scope_mismatch')

    def test_producer_identity_must_be_verified_assignee(self):
        self.adapter.snapshot['producer_submission']['actor_id'] = 999
        self.held('producer_submission_unverified')

    def test_current_task_wrong_assignee_holds(self):
        self.adapter.snapshot['assignee_ids'] = [999]
        self.held('basecamp_assignees_not_verified')

    def test_expired_release_holds(self):
        self.adapter.snapshot['release']['expires_at'] = stamp(-1)
        self.held('artifact_release_missing_or_expired')

    def test_recent_poll_does_not_solve_unseen_cc_removal(self):
        self.adapter.request['source']['received_at'] = stamp(-3600)
        self.held('delayed_action_requires_fresh_work_authorization')

    def test_fresh_authority_is_bound_to_prepared_exact_delivery(self):
        initial = self.poll()['intents'][0]
        self.adapter.request['source']['received_at'] = stamp(-3600)
        req = initial['authority_request']
        self.adapter.request['fresh_work_authorization'] = {
            'actor': 'owner@example.test', 'basis': 'explicit_current_participation_confirmation',
            'hamle_participation_confirmed': True, 'revoked': False, 'evidence_ref': 'synthetic-fresh',
            'work_id': self.work['work_id'], 'scope_digest': scope_digest(self.adapter.scope),
            'source_fingerprint': req['source']['fingerprint'], 'action': 'deliver_file',
            'global_generation': 2, 'work_generation': 3,
            'prepared_action_digest': scope_digest(req['prepared_action']),
            'issued_at': stamp(-10), 'expires_at': stamp(290),
            'artifact_sha256': 'b' * 64, 'artifact_version': 'v1'}
        self.assertEqual(len(self.poll()['intents']), 1)

    def test_human_stop_after_preparation_blocks_preflight(self):
        result = self.poll()
        self.adapter.request['control']['work_mode'] = 'takeover'
        checked = self.coordinator.preflight_intent(self.adapter, self.work, result['state'], result['intents'][0], now=NOW)
        self.assertFalse(checked['allowed'])
        self.assertEqual(checked['reason'], 'human_stop_or_takeover')

    def test_file_change_after_preparation_blocks_preflight(self):
        result = self.poll()
        self.adapter.snapshot['artifact']['sha256'] = 'c' * 64
        checked = self.coordinator.preflight_intent(self.adapter, self.work, result['state'], result['intents'][0], now=NOW)
        self.assertFalse(checked['allowed'])

    def test_preflight_always_rereads_unchanged_cursor(self):
        result = self.poll()
        checked = self.coordinator.preflight_intent(self.adapter, self.work, result['state'], result['intents'][0], now=NOW)
        self.assertTrue(checked['allowed'], checked)
        self.assertEqual([r[2] for r in self.adapter.reads if r[0] == 'basecamp'], [None, 'cursor-1'])

    def test_confirmed_exact_readback_is_not_resent(self):
        result = self.poll()
        intent = result['intents'][0]
        state = record_outcome(result['state'], intent, status='confirmed', receipt=exact_receipt(intent))
        polled = self.poll(state)
        self.assertEqual(polled['state']['status'], 'delivered')
        self.assertEqual(polled['intents'], [])

    def test_confirmed_delivery_history_survives_missing_current_artifact_or_qa(self):
        result = self.poll()
        intent = result['intents'][0]
        state = record_outcome(result['state'], intent, status='confirmed', receipt=exact_receipt(intent))
        original = copy.deepcopy(state['last_confirmed_delivery'])
        self.adapter.snapshot.pop('artifact')
        polled = self.poll(state)
        self.assertEqual(polled['state']['status'], 'awaiting_production')
        self.assertEqual(polled['state']['last_confirmed_delivery'], original)
        self.assertEqual(original['artifact_sha256'], 'b' * 64)
        self.assertEqual(original['receipt']['provider_readback_verified'], True)

    def test_unknown_delivery_never_retries_even_after_new_snapshot(self):
        result = self.poll()
        state = record_outcome(result['state'], result['intents'][0], status='unknown')
        self.adapter.snapshot['cursor'] = 'cursor-2'
        polled = self.poll(state)
        self.assertEqual(polled['state']['reason'], 'delivery_result_unknown_no_retry')
        self.assertEqual(polled['intents'], [])

    def test_readback_must_match_actual_intended_envelope(self):
        result = self.poll()
        receipt = exact_receipt(result['intents'][0])
        receipt['payload_sha256'] = 'f' * 64
        with self.assertRaisesRegex(DeliveryError, 'exact_provider_readback_required'):
            record_outcome(result['state'], result['intents'][0], status='confirmed', receipt=receipt)

    def test_two_independent_workflows_do_not_cross_tasks_or_mail_threads(self):
        work2, adapter2 = fixture('work-2', 42, 'thread-2')
        self.adapter.snapshot['qa']['semantic_checks']['language'] = False
        first = self.poll()
        second = self.coordinator.poll(adapter2, work2, None, now=NOW)
        self.assertEqual(first['intents'][0]['payload']['target']['task_id'], 41)
        self.assertEqual(second['intents'][0]['payload']['thread_id'], 'thread-2')
        self.assertNotEqual(first['intents'][0]['action_id'], second['intents'][0]['action_id'])
        self.adapter.request['control']['work_mode'] = 'takeover'
        self.assertEqual(self.poll(first['state'])['state']['status'], 'held')
        self.assertTrue(self.coordinator.preflight_intent(adapter2, work2, second['state'], second['intents'][0], now=NOW)['allowed'])

    def test_incremental_pages_do_not_dispatch_until_page_complete(self):
        self.adapter.snapshot['has_more'] = True
        first = self.poll()
        self.assertEqual(first['state']['cursor'], 'cursor-1')
        self.assertEqual(first['intents'], [])
        self.adapter.snapshot.update(has_more=False, cursor='cursor-2', events=[
            {'id': 'comment-2', 'task_id': 41, 'source_order': 2, 'evidence_ref': 'synthetic-comment-2'}])
        second = self.poll(first['state'])
        self.assertEqual(len(second['intents']), 1, second)
        self.assertEqual(second['state']['source_order'], 2)

    def test_replayed_or_out_of_order_incremental_event_holds(self):
        first = self.poll()
        self.adapter.snapshot['cursor'] = 'cursor-2'
        second = self.poll(first['state'])
        self.assertEqual(second['state']['reason'], 'basecamp_event_invalid')

    def test_cross_task_comment_page_holds(self):
        self.adapter.snapshot['events'][0]['task_id'] = 42
        self.held('basecamp_event_invalid')

    def test_scope_disabled_holds_even_with_accepted_qa(self):
        self.adapter.scope['enabled'] = False
        self.held('brand_scope_disabled')

    def test_missing_contract_revision_limit_holds(self):
        self.adapter.scope['services'][0]['revision_limit'] = None
        self.held('revision_limit_missing')


if __name__ == '__main__':
    unittest.main()
