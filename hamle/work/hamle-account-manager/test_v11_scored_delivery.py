"""Optional scored delivery path; no network or live customer acceptance."""
import copy
import json
import unittest

from autonomy_policy import scope_digest
from test_autonomy_policy import NOW, stamp
from test_v11_delivery import exact_receipt as provider_receipt, fixture as delivery_fixture, refresh_binding
from test_v11_quality_gate import fixture as quality_fixture
from v11_delivery import DeliveryCoordinator, DeliveryError, _make_intent, new_state, record_outcome
from v11_quality_gate import build_quality_binding


def exact_receipt(intent):
    # Synthetic trusted dispatcher joins provider readback to the immutable
    # durable intent. The context is captured here, never added by the sender
    # when wrapping an old receipt for a newer delivery attempt.
    receipt = provider_receipt(intent)
    if intent.get('quality_note') is True:
        receipt['quality_context_sha256'] = intent['quality_context_sha256']
    return receipt


def fixture(score="90"):
    work, adapter = delivery_fixture()
    policy, assessment, _, safety = quality_fixture(score)
    policy.update(policy_id='qa-v1', service_id=work['service_id'], accepted_at=stamp(-3600),
                  expires_at=stamp(3600), approval_actors=['owner@example.test', 'producer@example.test'],
                  internal_revision_limit=2)
    descriptions = ['Brief içeriğine uyum', 'Marka kurallarına uyum', 'Görsel uygulama kalitesi', 'Dil ve yazım']
    for criterion, description in zip(policy['criteria'], descriptions):
        criterion['description'] = description
    adapter.scope['services'][0]['delivery'].update(release_mode='scored_quality_v1', quality_policy=policy)
    adapter.snapshot.pop('qa')
    adapter.snapshot.pop('release')
    adapter.snapshot.update(quality_assessment=assessment, quality_safety=safety,
                            quality_assets_ref='assets-v1')
    rebuild_quality(work, adapter)
    return work, adapter


def rebuild_quality(work, adapter):
    """Trusted synthetic re-inspection; deliberately not used by stale-QA tests."""
    policy = adapter.scope['services'][0]['delivery']['quality_policy']
    digest = scope_digest(adapter.scope)
    adapter.request['delegation']['scope_digest'] = digest
    adapter.snapshot['producer_submission']['scope_digest'] = digest
    adapter.snapshot['producer_submission'].update(
        source_fingerprint=adapter.request['source']['fingerprint'],
        recipients_sha256=scope_digest({'to': adapter.request['reply_to'], 'cc': adapter.request['reply_cc']}))
    refresh_binding(work, adapter)
    intent = _make_intent(work, adapter.snapshot, adapter.scope, adapter.request,
                          action='deliver_file', body=adapter.snapshot['customer_body'],
                          artifact=adapter.snapshot['artifact'], stage='ready_delivery')
    binding = build_quality_binding(intent['authority_request'], brief_sha256=scope_digest(work['brief']),
                                     manifest_sha256=scope_digest(adapter.snapshot['artifact']['deliverables']),
                                     scope_sha256=digest)
    adapter.snapshot['quality_safety'].update(binding=copy.deepcopy(binding), observed_at=stamp(-1))
    assessment = adapter.snapshot.get('quality_assessment')
    if assessment is not None:
        assessment.update(binding=copy.deepcopy(binding), checked_at=stamp(-50), policy_digest=scope_digest(policy))
        for collection in ('criteria', 'critical_checks'):
            for record in assessment[collection].values():
                if record is None:
                    continue
                record.update(binding=copy.deepcopy(binding), checked_at=stamp(-50))
                for observation in record['observations']:
                    observation.update(artifact_sha256=binding['sha256'], brief_sha256=binding['brief_sha256'])
    return binding


def approval(adapter, score='85'):
    assessment = adapter.snapshot['quality_assessment']
    return {'verified': True, 'revoked': False, 'actor': 'owner@example.test',
            'evidence_ref': 'synthetic-human-exact-assessment', 'binding': copy.deepcopy(assessment['binding']),
            'policy_digest': assessment['policy_digest'], 'assessment_sha256': scope_digest(assessment),
            'score': score, 'decision': 'accept_quality_for_customer_delivery',
            'issued_at': stamp(-20), 'expires_at': stamp(300)}


class ScoredDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.work, self.adapter = fixture()
        self.coordinator = DeliveryCoordinator()

    def poll(self, state=None):
        return self.coordinator.poll(self.adapter, self.work, state, now=NOW)

    def confirm_note(self, result=None):
        result = self.poll() if result is None else result
        note, = result['intents']
        self.assertEqual(note['kind'], 'add_comment', result)
        self.assertTrue(note['quality_note'])
        state = record_outcome(result['state'], note, status='confirmed', receipt=exact_receipt(note))
        self.assertEqual(state['status'], 'quality_noted')
        return state

    def test_90_candidate_uses_scored_policy_without_fabricated_human_release(self):
        result = self.poll()
        self.assertEqual(result['state']['status'], 'quality_note_pending', result)
        self.assertEqual(result['intents'][0]['kind'], 'add_comment')
        self.assertTrue(result['intents'][0]['quality_note'])
        state = self.confirm_note(result)
        result = self.poll(state)
        self.assertEqual(result['state']['status'], 'ready_delivery', result)
        intent, = result['intents']
        self.assertEqual(intent['kind'], 'deliver_file')
        self.assertIsNone(intent['authority_request']['release'])
        self.assertEqual(intent['authority_request']['quality_submission']['actor_id'], 40)
        self.assertFalse(intent['live_enabled'])
        self.assertIn('90/100', result['quality_note'])
        self.assertIn('Brief içeriğine uyum', result['quality_note'])
        self.assertEqual(result['network_writes'], 0)

    def test_89_9_waits_for_authorized_human_quality_approval(self):
        self.work, self.adapter = fixture('89.9')
        result = self.poll()
        self.assertEqual(result['state']['status'], 'awaiting_quality_approval', result)
        self.assertEqual(result['intents'], [])
        self.assertIn('89.9/100', result['quality_note'])

    def test_80_exact_waits_for_authorized_human_quality_approval(self):
        self.work, self.adapter = fixture('80')
        self.assertEqual(self.poll()['state']['status'], 'awaiting_quality_approval')

    def test_85_can_be_delivered_after_exact_authorized_quality_approval(self):
        self.work, self.adapter = fixture('85')
        self.adapter.snapshot['quality_human_approval'] = approval(self.adapter)
        result = self.poll()
        self.assertEqual(result['intents'][0]['kind'], 'add_comment', result)
        result = self.poll(self.confirm_note(result))
        self.assertEqual(result['intents'][0]['kind'], 'deliver_file', result)
        self.assertIsNone(result['intents'][0]['authority_request']['release'])

    def test_79_is_internal_actionable_revision_not_customer_mail(self):
        self.work, self.adapter = fixture('79')
        result = self.poll()
        intent, = result['intents']
        self.assertEqual(intent['kind'], 'add_comment')
        self.assertTrue(intent['quality_revision'])
        self.assertEqual(set(intent['payload']), {'target', 'body'})
        self.assertIn('79/100', intent['payload']['body'])
        self.assertIn('revizyon gerekli', intent['payload']['body'])
        self.assertNotIn('score_below_human_review_band', intent['payload']['body'])

    def test_high_score_does_not_overrule_known_missing_file(self):
        self.adapter.snapshot['artifact']['deliverables'] = []
        rebuild_quality(self.work, self.adapter)
        result = self.poll()
        self.assertEqual(result['intents'][0]['kind'], 'add_comment', result)
        self.assertIn('eksik dosya: main', result['intents'][0]['payload']['body'])

    def test_high_score_does_not_overrule_actual_technical_measurement(self):
        self.adapter.snapshot['artifact']['deliverables'][0]['facts']['page_count'] = 2
        rebuild_quality(self.work, self.adapter)
        result = self.poll()
        self.assertEqual(result['intents'][0]['kind'], 'add_comment', result)
        self.assertIn('teknik özellikler', result['quality_note'])

    def test_known_spec_failure_without_qa_still_checks_security(self):
        self.adapter.snapshot['artifact']['deliverables'][0]['facts']['page_count'] = 2
        self.adapter.snapshot['quality_assessment'] = None
        rebuild_quality(self.work, self.adapter)
        self.adapter.snapshot['quality_safety']['security'] = 'blocked'
        result = self.poll()
        self.assertEqual(result['state']['reason'], 'security_requires_resolution', result)
        self.assertEqual(result['intents'], [])
        self.adapter.snapshot['quality_safety'] = None
        self.assertEqual(self.poll()['state']['reason'], 'current_safety_evidence_missing')

    def test_known_spec_failure_can_request_revision_without_qa_after_safe_context(self):
        self.adapter.snapshot['artifact']['deliverables'][0]['facts']['page_count'] = 2
        self.adapter.snapshot['quality_assessment'] = None
        rebuild_quality(self.work, self.adapter)
        result = self.poll()
        self.assertEqual(result['intents'][0]['kind'], 'add_comment', result)
        self.assertIsNone(result['state']['quality_result']['score'])

    def test_missing_assessment_never_falls_back_to_legacy_overall_accepted(self):
        self.adapter.snapshot['quality_assessment'] = None
        self.adapter.snapshot['qa'] = {'accepted': True}
        result = self.poll()
        self.assertEqual(result['state']['status'], 'ready_qa', result)
        self.assertEqual(result['intents'], [])

    def test_missing_policy_never_falls_back_to_legacy_release(self):
        self.adapter.scope['services'][0]['delivery']['quality_policy'] = None
        self.assertEqual(self.poll()['state']['status'], 'held')

    def test_producer_submission_is_still_mandatory(self):
        self.adapter.snapshot.pop('producer_submission')
        self.assertEqual(self.poll()['state']['status'], 'awaiting_production')

    def test_different_assets_reference_holds(self):
        self.adapter.snapshot['quality_assets_ref'] = 'unapproved-assets'
        self.assertEqual(self.poll()['state']['reason'], 'quality_assets_not_approved')

    def test_changed_artifact_requires_new_qa_and_approval(self):
        self.adapter.snapshot['artifact']['sha256'] = '9' * 64
        refresh_binding(self.work, self.adapter)
        result = self.poll()
        self.assertEqual(result['state']['status'], 'held')
        self.assertEqual(result['intents'], [])

    def test_new_source_after_approval_invalidates_preflight(self):
        self.work, self.adapter = fixture('85')
        self.adapter.snapshot['quality_human_approval'] = approval(self.adapter)
        first = self.poll()
        self.adapter.request['source']['fingerprint'] = '9' * 64
        result = self.coordinator.preflight_intent(self.adapter, self.work, first['state'], first['intents'][0], now=NOW)
        self.assertFalse(result['allowed'])

    def test_revoked_human_approval_is_rechecked_at_preflight(self):
        self.work, self.adapter = fixture('85')
        self.adapter.snapshot['quality_human_approval'] = approval(self.adapter)
        first = self.poll()
        self.adapter.snapshot['quality_human_approval']['revoked'] = True
        result = self.coordinator.preflight_intent(self.adapter, self.work, first['state'], first['intents'][0], now=NOW)
        self.assertFalse(result['allowed'])

    def test_control_human_takeover_overrides_100(self):
        self.work, self.adapter = fixture('100')
        self.adapter.request['control']['work_mode'] = 'human'
        self.assertEqual(self.poll()['state']['reason'], 'human_stop_or_takeover')

    def test_internal_revision_counts_only_confirmed_writes_not_customer_rounds(self):
        self.work, self.adapter = fixture('79')
        result = self.poll()
        intent = result['intents'][0]
        self.assertEqual(result['state']['quality_revision_count'], 0)
        unknown = record_outcome(result['state'], intent, status='unknown')
        self.assertEqual(unknown['quality_revision_count'], 0)
        confirmed = record_outcome(result['state'], intent, status='confirmed', receipt=exact_receipt(intent))
        self.assertEqual(confirmed['quality_revision_count'], 1)
        self.assertEqual(self.work['revision_index'], 0)
        same = record_outcome(confirmed, intent, status='confirmed', receipt=exact_receipt(intent))
        self.assertEqual(same['quality_revision_count'], 1)

    def test_revision_at_limit_recognizes_confirmed_same_comment_without_repeating(self):
        self.work, self.adapter = fixture('79')
        self.adapter.scope['services'][0]['delivery']['quality_policy']['internal_revision_limit'] = 1
        rebuild_quality(self.work, self.adapter)
        result = self.poll()
        intent = result['intents'][0]
        confirmed = record_outcome(result['state'], intent, status='confirmed', receipt=exact_receipt(intent))
        repeated = self.poll(confirmed)
        self.assertEqual(repeated['state']['status'], 'changes_requested', repeated)
        self.assertEqual(repeated['intents'], [])
        self.adapter.snapshot['artifact']['sha256'] = '9' * 64
        rebuild_quality(self.work, self.adapter)
        new_version = self.poll(confirmed)
        self.assertEqual(new_version['state']['reason'], 'internal_quality_revision_limit_missing_or_reached')
        self.assertEqual(new_version['state']['status'], 'awaiting_human_takeover')
        self.assertEqual(new_version['state']['human_owners'],
                         self.adapter.scope['services'][0]['delivery']['quality_policy']['approval_actors'])
        self.assertTrue(new_version['state']['customer_revision_count_unchanged'])
        self.assertEqual(new_version['intents'], [])

    def test_user_two_internal_rounds_then_human_without_third_comment(self):
        self.work, self.adapter = fixture('79')
        state = None
        for n in range(2):
            self.adapter.snapshot['artifact']['sha256'] = str(n + 7) * 64
            rebuild_quality(self.work, self.adapter)
            result = self.poll(state)
            intent = result['intents'][0]
            state = record_outcome(result['state'], intent, status='confirmed', receipt=exact_receipt(intent))
        self.adapter.snapshot['artifact']['sha256'] = '9' * 64
        rebuild_quality(self.work, self.adapter)
        result = self.poll(state)
        self.assertEqual(result['state']['quality_revision_count'], 2)
        self.assertEqual(result['state']['status'], 'awaiting_human_takeover')
        self.assertEqual(result['intents'], [])
        self.assertEqual(self.work['revision_index'], 0)

    def test_delivery_confirmed_is_not_resent(self):
        result = self.poll(self.confirm_note())
        intent = result['intents'][0]
        confirmed = record_outcome(result['state'], intent, status='confirmed', receipt=exact_receipt(intent))
        self.assertEqual(self.poll(confirmed)['intents'], [])

    def test_unknown_delivery_result_blocks_quality_based_retry(self):
        result = self.poll(self.confirm_note())
        intent = result['intents'][0]
        unknown = record_outcome(result['state'], intent, status='unknown')
        repeated = self.poll(unknown)
        self.assertEqual(repeated['state']['reason'], 'delivery_result_unknown_no_retry')
        self.assertEqual(repeated['intents'], [])

    def test_quality_note_confirmation_does_not_consume_revision(self):
        state = self.confirm_note()
        self.assertEqual(state['quality_revision_count'], 0)
        self.assertEqual(self.work['revision_index'], 0)
        self.assertIsNotNone(state['last_confirmed_quality_note'])
        self.assertIsNone(state['last_confirmed_delivery'])

    def test_invalid_candidate_cannot_publish_positive_note_before_delivery_rejects_it(self):
        self.adapter.snapshot['producer_submission']['issued_at'] = stamp(-1)
        result = self.poll()
        self.assertEqual(result['state']['reason'], 'scored_assessment_precedes_submission')
        self.assertEqual(result['intents'], [])

    def test_confirmed_note_survives_restart_and_is_not_emitted_twice(self):
        state = json.loads(json.dumps(self.confirm_note()))
        first = self.poll(state)
        second = self.poll(first['state'])
        self.assertEqual(first['intents'][0]['kind'], 'deliver_file')
        self.assertEqual(second['intents'][0]['kind'], 'deliver_file')
        self.assertEqual(first['intents'][0]['action_id'], second['intents'][0]['action_id'])
        self.assertEqual(len(second['state']['receipts']), 1)

    def test_started_or_unknown_note_never_repeats_comment_or_sends_mail(self):
        for status in ('started', 'unknown'):
            with self.subTest(status=status):
                first = self.poll()
                note = first['intents'][0]
                state = record_outcome(first['state'], note, status=status)
                repeated = self.poll(state)
                self.assertEqual(repeated['state']['reason'], 'delivery_result_unknown_no_retry')
                self.assertEqual(repeated['intents'], [])
                self.assertEqual(repeated['state']['quality_revision_count'], 0)

    def test_confirmed_note_cannot_override_new_human_stop(self):
        state = self.confirm_note()
        self.adapter.request['control']['work_mode'] = 'takeover'
        result = self.poll(state)
        self.assertEqual(result['state']['reason'], 'human_stop_or_takeover')
        self.assertEqual(result['intents'], [])

    def test_midband_approval_revoked_after_confirmed_note_holds_mail(self):
        self.work, self.adapter = fixture('85')
        self.adapter.snapshot['quality_human_approval'] = approval(self.adapter)
        state = self.confirm_note()
        self.adapter.snapshot['quality_human_approval']['revoked'] = True
        result = self.poll(state)
        self.assertEqual(result['state']['reason'], 'quality_human_approval_unverified')
        self.assertEqual(result['intents'], [])

    def test_confirmed_note_does_not_authorize_changed_context_even_after_new_qa(self):
        for change in ('brief', 'artifact', 'policy', 'recipients', 'handover'):
            with self.subTest(change=change):
                self.work, self.adapter = fixture()
                state = self.confirm_note()
                old_note = state['last_confirmed_quality_note']['action_id']
                if change == 'brief':
                    self.work['brief']['evidence_ref'] = 'synthetic-updated-brief'
                elif change == 'artifact':
                    self.adapter.snapshot['artifact']['sha256'] = '9' * 64
                elif change == 'policy':
                    self.adapter.scope['services'][0]['delivery']['quality_policy']['version'] = '2'
                elif change == 'recipients':
                    self.adapter.request['reply_cc'] = self.adapter.request['source']['cc'] = ['new-cc@example.test']
                    self.adapter.scope['allowed_cc_contacts'].append('new-cc@example.test')
                    self.adapter.request['source']['fingerprint'] = '9' * 64
                elif change == 'handover':
                    self.adapter.request['control']['work_generation'] += 1
                    self.adapter.request['delegation']['work_generation'] += 1
                rebuild_quality(self.work, self.adapter)
                result = self.poll(state)
                new_note, = result['intents']
                self.assertEqual(new_note['kind'], 'add_comment', result)
                self.assertTrue(new_note['quality_note'])
                self.assertNotEqual(new_note['action_id'], old_note)

    def test_changed_exact_assessment_requires_a_new_note_even_if_score_identical(self):
        state = self.confirm_note()
        self.adapter.snapshot['quality_assessment']['evidence_ref'] = 'new-exact-assessment'
        result = self.poll(state)
        self.assertEqual(result['intents'][0]['kind'], 'add_comment')
        self.assertNotEqual(result['intents'][0]['action_id'], state['last_confirmed_quality_note']['action_id'])

    def test_delivery_preflight_requires_note_receipt_still_present(self):
        state = self.confirm_note()
        ready = self.poll(state)
        mail = ready['intents'][0]
        state['receipts'].clear()
        result = self.coordinator.preflight_intent(self.adapter, self.work, state, mail, now=NOW)
        self.assertFalse(result['allowed'])

    def test_mismatched_confirmed_note_receipt_cannot_unlock_delivery(self):
        state = self.confirm_note()
        note_id = state['last_confirmed_quality_note']['action_id']
        state['receipts'][note_id]['receipt']['payload_sha256'] = '9' * 64
        result = self.poll(state)
        self.assertEqual(result['state']['reason'], 'quality_note_exact_readback_missing')
        self.assertEqual(result['intents'], [])

    def test_quality_note_recording_requires_context_already_in_trusted_receipt(self):
        result = self.poll()
        note = result['intents'][0]
        for value in (None, '9' * 64):
            with self.subTest(value=value):
                receipt = provider_receipt(note)
                if value is not None:
                    receipt['quality_context_sha256'] = value
                with self.assertRaisesRegex(DeliveryError, 'quality_note_receipt_context_mismatch'):
                    record_outcome(result['state'], note, status='confirmed', receipt=receipt)

    def test_current_proof_cannot_wrap_receipt_with_another_quality_context(self):
        state = self.confirm_note()
        note_id = state['last_confirmed_quality_note']['action_id']
        state['receipts'][note_id]['receipt']['quality_context_sha256'] = '9' * 64
        result = self.poll(state)
        self.assertEqual(result['state']['reason'], 'quality_note_exact_readback_missing')
        self.assertEqual(result['intents'], [])

    def test_started_note_context_is_immutable_until_confirmation(self):
        result = self.poll()
        note = result['intents'][0]
        state = record_outcome(result['state'], note, status='started')
        changed = copy.deepcopy(note)
        changed['quality_context']['assessment_sha256'] = '9' * 64
        changed['quality_context_sha256'] = scope_digest(changed['quality_context'])
        with self.assertRaisesRegex(DeliveryError, 'quality_note_context_changed_after_write_started'):
            record_outcome(state, changed, status='confirmed', receipt=exact_receipt(changed))

    def test_confirmed_mail_does_not_add_new_note_for_recomputed_assessment(self):
        result = self.poll(self.confirm_note())
        mail = result['intents'][0]
        state = record_outcome(result['state'], mail, status='confirmed', receipt=exact_receipt(mail))
        self.adapter.snapshot['quality_assessment']['evidence_ref'] = 'new-exact-assessment'
        result = self.poll(state)
        self.assertEqual(result['state']['status'], 'delivered', result)
        self.assertEqual(result['intents'], [])

    def test_comment_then_mail_each_exactly_once_in_durable_fake_dispatch(self):
        state, writes = None, []
        for _ in range(4):
            result = self.poll(state)
            state = result['state']
            for intent in result['intents']:
                preflight = self.coordinator.preflight_intent(self.adapter, self.work, state, intent, now=NOW)
                self.assertTrue(preflight['allowed'], preflight)
                intent = preflight['intent']
                state = record_outcome(preflight['state'], intent, status='started')
                writes.append(intent['kind'])
                state = record_outcome(state, intent, status='confirmed', receipt=exact_receipt(intent))
        self.assertEqual(writes, ['add_comment', 'deliver_file'])
        self.assertEqual(state['status'], 'delivered')
        self.assertEqual(state['quality_revision_count'], 0)

    def test_legacy_delivery_still_requires_human_release(self):
        work, adapter = delivery_fixture()
        adapter.snapshot.pop('release')
        result = self.coordinator.poll(adapter, work, None, now=NOW)
        self.assertEqual(result['state']['reason'], 'trusted_producer_release_required')
        self.assertEqual(result['intents'], [])


if __name__ == '__main__':
    unittest.main()
