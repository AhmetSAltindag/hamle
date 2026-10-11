"""Real PNG structure, synthetic OCR adapter results; no live acceptance claims."""
import copy
import hashlib
import unittest
from unittest.mock import patch

from autonomy_policy import scope_digest
from test_v11_artifact_inspector import png
from test_v11_quality_gate import stamp
from v11_artifact_inspector import InspectionError, inspect_artifact
from v11_internal_quality import (ACCOUNT, AHMET, HAMLE, HAMLE_ID, PARENT, PROJECT, STRINGS, SUBJECT,
                                  InternalQualityError, build_assessment, build_internal_brief,
                                  build_stage_assessment_event, output_spec)
from v11_production_stages import initialize_production, advance_production, ready_stages


def source():
    return {'subject': SUBJECT, 'source_subject': SUBJECT, 'sender': AHMET,
            'hamle_direct_recipient': True, 'latest_to': [HAMLE], 'latest_cc': [],
            'message_id': 'new-message', 'thread_id': 'new-thread',
            'snapshot_fingerprint': 'e' * 64, 'source_sha256': 'c' * 64, 'captured_at': stamp()}


def control():
    return {'verified': True, 'generation': 7, 'mode': 'active', 'observed_at': stamp(),
            'evidence_ref': 'synthetic-current-cloud-control', 'write_state': 'new'}


def event(state, kind, **kwargs):
    return {'id': 'synthetic-' + kind + '-' + str(state['revision']), 'type': kind,
            'work_id': state['work_id'], 'expected_revision': state['revision'], 'stage_id': 'main',
            'actor_id': HAMLE_ID, 'verified': True, 'evidence_ref': 'synthetic-provider-readback',
            'occurred_at': stamp(), **kwargs}


def apply(state, kind, **kwargs):
    result = advance_production(state, event(state, kind, **kwargs))
    assert result['status'] == 'applied', result['reason']
    return result['state']


def fixture(text=None, *, ocr_error=False, width=1080):
    src = source()
    early = {**src, 'captured_at': stamp(-300)}
    brief = build_internal_brief(early, 7, stamp(-300), stamp(3600))
    state = initialize_production('internal-proof', brief, mode='internal_pilot')
    task = {'account_id': ACCOUNT, 'project_id': PROJECT, 'list_id': PARENT,
            'task_id': 99, 'provider': 'basecamp', 'verified': True,
            'evidence_ref': 'synthetic-new-task', 'customer_visible': False,
            'assignee_ids': [HAMLE_ID], 'brief_sha256': state['brief_sha256']}
    state = apply(state, 'bind_stage_task', task=task)
    state = apply(state, 'begin_stage')
    data = png(width, 1080)
    h = hashlib.sha256(data).hexdigest()
    ocr_text = ' '.join(STRINGS.values()) if text is None else text
    mock = patch('v11_artifact_inspector._isolated', side_effect=InspectionError('ocr_unavailable')) if ocr_error else patch(
        'v11_artifact_inspector._isolated', return_value={'text': ocr_text, 'parser': 'synthetic-tesseract', 'languages': ['eng', 'tur']})
    with mock:
        inspection = inspect_artifact(data, declared_mime='image/png', expected_sha256=h,
            brief_sha256=state['brief_sha256'], output_spec=output_spec('main'),
            source={'verified': True, 'account_id': ACCOUNT, 'project_id': PROJECT,
                    'task_id': 99, 'attachment_id': 100, 'evidence_ref': 'synthetic-actual-byte-read'},
            inspected_at=stamp(-60), include_text=True)
    outputs = [{'id': 'main', 'kind': 'graphic', 'purpose': 'final', 'sha256': h,
                'size_bytes': len(data), 'mime_type': 'image/png', 'provider_attachment_id': '100',
                'inspection': {'verified': True, 'bytes_read': True, 'readable': True,
                               'sha256': h, 'size_bytes': len(data), 'evidence_ref': 'synthetic-actual-byte-read',
                               'inspected_at': stamp(-60)}}]
    submission = {'verified': True, 'evidence_ref': 'synthetic-uploaded-internal-fixture',
                  'producer_id': HAMLE_ID, 'task_id': 99, 'origin': 'internal_fixture',
                  'brief_sha256': state['brief_sha256'], 'dependency_acceptances': {},
                  'submitted_at': stamp(-120), 'artifact': {'version': 'v1', 'sha256': h},
                  'outputs': outputs, 'manifest_sha256': scope_digest(outputs)}
    state = apply(state, 'submit_outputs', submission=submission)
    return state, inspection, src


def assess(state, inspection, src, **kwargs):
    return build_assessment(state, 'main', inspection, src, stamp(), 7, control=kwargs.pop('control', control()), **kwargs)


class InternalQualityTests(unittest.TestCase):
    def test_brief_is_explicit_internal_two_stage_fixture(self):
        brief = build_internal_brief(source(), 7, stamp(), stamp(3600))
        self.assertEqual(brief['binding']['project_id'], PROJECT)
        self.assertEqual(brief['binding']['production_stages'][1]['depends_on'], ['main'])
        self.assertEqual([o['spec']['height'] for o in brief['deliverables']], [1080, 1920])
        self.assertTrue(all(o['purpose'] == 'final' for o in brief['deliverables']))
        self.assertFalse(brief['internal_technical_policy']['customer_live_enabled'])

    def test_wrong_subject_rejected(self):
        src = source()
        src['subject'] = 'arbitrary customer request'
        with self.assertRaisesRegex(InternalQualityError, 'source_identity'):
            build_internal_brief(src, 7, stamp(), stamp(3600))

    def test_customer_in_cc_rejected(self):
        src = source()
        src['latest_cc'] = ['customer@example.test']
        with self.assertRaisesRegex(InternalQualityError, 'actual_recipients'):
            build_internal_brief(src, 7, stamp(), stamp(3600))

    def test_bcc_not_enough(self):
        src = source()
        src['latest_to'] = [AHMET]
        with self.assertRaisesRegex(InternalQualityError, 'actual_recipients'):
            build_internal_brief(src, 7, stamp(), stamp(3600))

    def test_stale_source_readback_rejected(self):
        src = source()
        src['captured_at'] = stamp(-61)
        with self.assertRaisesRegex(InternalQualityError, 'readback_stale'):
            build_internal_brief(src, 7, stamp(), stamp(3600))

    def test_full_technical_fixture_scores100_not_artistic_claim(self):
        state, inspection, src = fixture()
        bundle = assess(state, inspection, src)
        self.assertEqual(bundle['result']['route'], 'delivery_candidate', bundle['result'])
        self.assertEqual(bundle['result']['score'], '100')
        self.assertFalse(bundle['artistic_or_brand_quality_claim'])
        self.assertFalse(bundle['customer_live_enabled'])
        self.assertFalse(bundle['result']['send_allowed'])
        self.assertEqual(bundle['policy']['trusted_evaluators']['model_rubric'], [])

    def test_missing_date_is_critical_revision_below80(self):
        state, inspection, src = fixture(STRINGS['title'] + ' ' + STRINGS['disclosure'])
        result = assess(state, inspection, src)['result']
        self.assertEqual(result['route'], 'revision', result)
        self.assertLess(float(result['score']), 80)
        self.assertIn('date', result['critical_failures'])

    def test_unknown_ocr_is_held_never_numeric_zero(self):
        state, inspection, src = fixture(ocr_error=True)
        result = assess(state, inspection, src)['result']
        self.assertEqual(result['route'], 'held', result)
        self.assertIsNone(result['score'])

    def test_dimension_failure_is_revision_even_if_text_passes(self):
        state, inspection, src = fixture(width=1000)
        result = assess(state, inspection, src)['result']
        self.assertEqual(result['route'], 'revision', result)
        self.assertIn('technical_specs', result['critical_failures'])

    def test_visible_extra_internal_note_fails_closed(self):
        state, inspection, src = fixture(' '.join(STRINGS.values()) + ' INTERNAL DO NOT SEND')
        result = assess(state, inspection, src)['result']
        self.assertEqual(result['route'], 'revision', result)
        self.assertIn('internal_notes_excluded', result['critical_failures'])

    def test_no_control_readback_holds(self):
        state, inspection, src = fixture()
        self.assertEqual(assess(state, inspection, src, control=None)['result']['route'], 'held')

    def test_stale_control_holds(self):
        state, inspection, src = fixture()
        ctrl = control()
        ctrl['observed_at'] = stamp(-61)
        self.assertEqual(assess(state, inspection, src, control=ctrl)['result']['route'], 'held')

    def test_stop_control_holds(self):
        state, inspection, src = fixture()
        ctrl = control()
        ctrl['mode'] = 'stopped'
        self.assertEqual(assess(state, inspection, src, control=ctrl)['result']['route'], 'held')

    def test_unknown_write_state_holds(self):
        state, inspection, src = fixture()
        ctrl = control()
        ctrl['write_state'] = 'unknown'
        self.assertEqual(assess(state, inspection, src, control=ctrl)['result']['route'], 'held')

    def test_changed_global_generation_rejected(self):
        state, inspection, src = fixture()
        with self.assertRaisesRegex(InternalQualityError, 'generation_changed'):
            build_assessment(state, 'main', inspection, src, stamp(), 8, control=control())

    def test_inspection_tampering_rejected(self):
        state, inspection, src = fixture()
        inspection['checks']['required_text:date']['result'] = 'fail'
        with self.assertRaisesRegex(InternalQualityError, 'evidence_digest'):
            assess(state, inspection, src)

    def test_ocr_text_tampering_rejected(self):
        state, inspection, src = fixture()
        inspection['extracted_text'] += ' altered'
        with self.assertRaisesRegex(InternalQualityError, 'text_digest'):
            assess(state, inspection, src)

    def test_cross_task_inspection_rejected_even_with_rehashed_evidence(self):
        state, inspection, src = fixture()
        inspection['source']['task_id'] = 101
        unsigned = copy.deepcopy(inspection)
        unsigned.pop('extracted_text')
        unsigned.pop('evidence_sha256')
        inspection['evidence_sha256'] = scope_digest(unsigned)
        with self.assertRaisesRegex(InternalQualityError, 'source_target'):
            assess(state, inspection, src)

    def test_wat_scope_cannot_use_internal_rubric(self):
        state, inspection, src = fixture()
        state['brief']['brand_id'] = 'wat_mobilite'
        with self.assertRaisesRegex(InternalQualityError, 'pilot_scope'):
            assess(state, inspection, src)

    def test_operational_mode_cannot_use_internal_rubric(self):
        state, inspection, src = fixture()
        state['mode'] = 'production'
        with self.assertRaisesRegex(InternalQualityError, 'pilot_scope'):
            assess(state, inspection, src)

    def test_real_producer_not_impersonated_by_fixture_adapter(self):
        state, inspection, src = fixture()
        state['stages']['main']['submission']['producer_id'] = 47658175
        with self.assertRaisesRegex(InternalQualityError, 'fixture_submission'):
            assess(state, inspection, src)

    def test_assessment_event_integrates_with_stage_engine(self):
        state, inspection, src = fixture()
        ev = build_stage_assessment_event(state, 'main', inspection, src, stamp(), 7, control=control())
        result = advance_production(state, ev)
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual(result['state']['stages']['main']['status'], 'accepted')
        self.assertEqual([s['id'] for s in ready_stages(result['state'])], ['story'])

    def test_missing_date_event_requests_revision_without_next_stage(self):
        state, inspection, src = fixture(STRINGS['title'] + ' ' + STRINGS['disclosure'])
        ev = build_stage_assessment_event(state, 'main', inspection, src, stamp(), 7, control=control())
        result = advance_production(state, ev)
        self.assertEqual(result['state']['stages']['main']['status'], 'changes_requested')
        self.assertEqual(ready_stages(result['state']), [])

    def test_final_scope_source_change_rejected(self):
        state, inspection, src = fixture()
        src['snapshot_fingerprint'] = 'f' * 64
        with self.assertRaisesRegex(InternalQualityError, 'source_changed'):
            assess(state, inspection, src)


if __name__ == '__main__':
    unittest.main()
