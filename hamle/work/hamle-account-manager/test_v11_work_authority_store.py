"""Offline atomicity tests against the real store, queue and central policy."""
import copy
from dataclasses import asdict, replace
from datetime import timedelta
import unittest

from autonomy_policy import scope_digest
from cloud_store import OperationsStore, StoreError
from test_cloud_store import MemoryBackend, budget_snapshot
from test_autonomy_policy import NOW, stamp
from test_v11_work_authority import fixture, AHMET
from v11_work_authority import (TrustedIntakeEvidence, WorkAuthorityError, build_work_authority_binding,
    rebind_work_authority_from_followup, grant_work_authority, authorize_work_authority)
from v11_work_authority_store import WorkAuthorityStore, TrustedWorkStop, QUEUE_PREFIX
from v11_work_queue import InMemoryBackend, QueueError, WRITE_BINDING_FIELDS


class AuthorityStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.backend = MemoryBackend()
        self.ops = OperationsStore(self.backend, clock=lambda: self.now)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.generation = self.ops.resume_review_only('synthetic-ahmet-sub')['generation']
        self.store = WorkAuthorityStore(self.ops); self.queue = self.store.queue()
        self.policy, self.scope, self.request, self.issuer = fixture()
        self.scope['services'][0]['live_enabled'] = True  # Offline fixture only.
        self.work = self.queue.bind_work(brand_id='wat_mobilite', thread_id='thread-1',
            account_id=10, project_id=20, list_id=30, service_id='coordination', expected_generation=self.generation)
        self.work = self.queue.bind_task(self.work['work_id'], task_id=41, account_id=10,
            project_id=20, list_id=30, expected_generation=self.generation)
        self.wid = self.work['work_id']
        r = self.request
        r.update(work_id=self.wid)
        for key in ('control', 'delegation', 'work_binding', 'prepared_action'):
            r[key]['work_id'] = self.wid
        for key in ('control', 'delegation'):
            r[key].update(global_generation=self.generation, work_generation=0)
        r['delegation']['scope_digest'] = scope_digest(self.scope)
        self.issuer.update(binding_sha256=scope_digest(build_work_authority_binding(self.scope, r)))
        for key in ('work_authority_grant', 'work_authority_observation', 'budget_authorization'):
            r.pop(key)

    def install(self):
        return self.store.install_grant(self.scope, self.request, issuer=self.issuer)

    def claim(self):
        self.queue.enqueue_message(self.wid, message_id=self.request['source']['message_id'],
            fingerprint=self.request['source']['fingerprint'], source_order=1, expected_generation=self.generation)
        return self.queue.claim_next(self.wid, worker_id='offline-worker', expected_generation=self.generation)

    def stop(self, *, evidence='synthetic-stop-1', **kwargs):
        return self.store.stop_work(self.wid, TrustedWorkStop(work_id=self.wid, actor=AHMET,
            identity_subject='synthetic-ahmet-sub', evidence_ref=evidence, observed_at=self.now.isoformat()),
            expected_work_generation=kwargs.pop('expected_work_generation', 0), **kwargs)

    def begin(self, claim):
        return self.store.begin_authorized_write(claim, self.scope, self.request, action_key='reply-1')

    def followup(self):
        self.request['source'].update(message_id='message-2', fingerprint='c'*64, received_at=stamp(-5))
        self.request['prepared_action']['source_fingerprint'] = 'c'*64
        return TrustedIntakeEvidence(adapter_id='hamle-authenticated-intake-v1', provider_identity='hamle@gamechangers.com.tr',
            evidence_ref='synthetic-followup-2', classification_evidence_ref='synthetic-same-brief-verified',
            binding_sha256=scope_digest(build_work_authority_binding(self.scope, self.request)),
            parent_policy_digest=scope_digest(self.policy), observed_at=self.now.isoformat(), intake_kind='same_scope_followup')

    def test_disabled_service_cannot_install_even_with_accepted_policy(self):
        self.scope['services'][0]['live_enabled'] = False
        with self.assertRaisesRegex(StoreError, 'customer_service_not_live'):
            self.install()
        self.assertIsNone(self.store.observe_work(self.wid)['record'])

    def test_separate_queue_backend_is_rejected(self):
        with self.assertRaisesRegex(StoreError, 'separate_queue_backend_unsafe'):
            WorkAuthorityStore(self.ops, queue_backend=InMemoryBackend())

    def test_install_current_observation_and_central_authorized_write(self):
        record = self.install()
        self.assertEqual(self.install(), record)
        read = self.store.observe_work(self.wid)
        self.assertEqual(read['observation']['grant_sha256'], scope_digest(record['grant']))
        action = self.begin(self.claim())
        self.assertEqual(action['state'], 'started')
        self.assertFalse(action['automatic_retry_allowed'])
        with self.assertRaisesRegex(StoreError, 'write_outcome_unresolved'):
            self.begin(self.work.get('lease') or {'work_id': self.wid})

    def test_actual_mail_tocc_and_risk_still_checked_at_write(self):
        self.install(); claim = self.claim()
        self.request['source']['to'] = ['client@example.test']
        with self.assertRaises(StoreError):
            self.begin(claim)
        self.request['source']['to'] = ['hamle@gamechangers.com.tr']
        self.request['risk_area'] = 'commercial'
        with self.assertRaisesRegex(StoreError, 'ahmet_commercial_decision_required'):
            self.begin(claim)
        self.assertIsNone(self.queue.get_work(self.wid)['inflight_action_id'])

    def test_stale_source_read_cannot_be_refreshed_by_store_observation(self):
        self.install(); claim = self.claim(); self.now += timedelta(seconds=61)
        with self.assertRaisesRegex(StoreError, 'source_capture_stale'):
            self.begin(claim)

    def test_stop_revocation_work_generation_and_lease_are_atomic(self):
        self.install(); claim = self.claim()
        stopped = self.stop()
        self.assertEqual(stopped['record']['grant']['status'], 'revoked')
        self.assertEqual(stopped['work']['work_generation'], 1)
        self.assertIsNone(stopped['work']['lease'])
        self.assertEqual(stopped['work']['mode'], 'takeover')
        with self.assertRaisesRegex(StoreError, 'work_stopped_or_generation_changed'):
            self.begin(claim)
        self.assertTrue(self.stop()['duplicate'])
        self.assertEqual(self.queue.get_work(self.wid)['work_generation'], 1)
        self.assertEqual(self.ops.control()['generation'], self.generation)

    def test_restart_and_unknown_commit_retry_never_resurrect(self):
        self.install(); self.claim(); self.backend.unknown_after_commit = True
        with self.assertRaisesRegex(StoreError, 'store_outcome_unknown'):
            self.stop()
        self.store = WorkAuthorityStore(OperationsStore(self.backend, clock=lambda: self.now))
        read = self.store.observe_work(self.wid)
        self.assertEqual(read['record']['grant']['status'], 'revoked')
        self.assertTrue(self.stop()['duplicate'])

    def test_direct_queue_bypasses_rejected(self):
        self.install(); claim = self.claim()
        binding = {k: self.work[k] for k in WRITE_BINDING_FIELDS}
        binding['queue_event_fingerprint'] = 'a'*64
        with self.assertRaisesRegex(StoreError, 'use_authority_store_begin_write'):
            self.queue.begin_write(claim, action_kind='acknowledge', payload_sha256='b'*64,
                action_key='bypass', prepared_binding=binding)
        with self.assertRaisesRegex(StoreError, 'use_atomic_authority_stop'):
            self.queue.set_work_mode(self.wid, mode='takeover', actor=AHMET, expected_work_generation=0)
        with self.assertRaisesRegex(StoreError, 'use_shared_operations_control'):
            self.queue.set_control('queue_open', actor=AHMET, expected_generation=self.generation)

    def test_global_stop_and_resume_invalidates_old_generation(self):
        self.install(); claim = self.claim(); self.ops.stop('synthetic-ahmet-sub')
        self.ops.resume_review_only('synthetic-ahmet-sub')
        with self.assertRaisesRegex(StoreError, 'control_stopped_or_stale'):
            self.begin(claim)

    def test_stop_after_deadline_and_before_any_grant(self):
        self.now += timedelta(days=4)
        stopped = self.stop()
        self.assertIsNone(stopped['record']['grant'])
        self.assertEqual(stopped['work']['mode'], 'takeover')
        self.assertIsNone(self.store.observe_work(self.wid)['observation'])
        self.assertTrue(self.stop()['duplicate'])

    def test_no_model_json_or_unknown_actor_can_stop(self):
        receipt = TrustedWorkStop(self.wid, AHMET, 'sub', 'ref', stamp())
        for candidate in (asdict(receipt), replace(receipt, actor='model'), replace(receipt, model_supplied_authority=True)):
            with self.assertRaisesRegex(StoreError, 'authenticated_work_stop_required'):
                self.store.stop_work(self.wid, candidate, expected_work_generation=0)
        self.assertEqual(self.queue.get_work(self.wid)['mode'], 'active')

    def test_gmail_stop_requires_actual_thread_tocc_and_identity(self):
        self.install()
        receipt = TrustedWorkStop(self.wid, AHMET, 'gmail:ahmet', 'provider-message-stop', stamp(),
            channel='authenticated_gmail_api', thread_id='thread-1', actual_to_cc=('hamle@gamechangers.com.tr',),
            provider_identity='hamle@gamechangers.com.tr', source_message_id='stop-message-1',
            source_fingerprint='e'*64, source_sender=AHMET)
        for candidate in (replace(receipt, thread_id='other'), replace(receipt, actual_to_cc=()), replace(receipt, provider_identity=AHMET)):
            with self.assertRaisesRegex(StoreError, 'work_stop_mail_binding_invalid'):
                self.store.stop_work(self.wid, candidate, expected_work_generation=0)
        self.assertEqual(self.store.stop_work(self.wid, receipt, expected_work_generation=0)['record']['grant']['status'], 'revoked')

    def test_started_write_kept_unknown_and_receipt_reconciles_after_stop(self):
        self.install(); claim = self.claim(); action = self.begin(claim)
        stopped = self.stop()
        self.assertEqual(stopped['work']['inflight_action_id'], action['id'])
        self.assertEqual(stopped['work']['queue_state'], 'unknown')
        confirmed = self.queue.confirm_write(action_id=action['id'], claim_token=claim['token'],
            payload_sha256=action['payload_sha256'], receipt={'provider':'gmail', 'remote_id':'fake-receipt',
                'readback_sha256':action['payload_sha256']})
        self.assertEqual(confirmed['state'], 'confirmed')
        self.assertEqual(self.queue.get_work(self.wid)['mode'], 'takeover')

    def test_expired_budget_blocks_new_action_but_stop_remains_available(self):
        self.install(); claim = self.claim(); self.now += timedelta(days=4)
        with self.assertRaisesRegex(StoreError, 'authorization_expired'):
            self.begin(claim)
        self.assertEqual(self.stop()['work']['mode'], 'takeover')

    def test_budget_read_is_current_not_caller_supplied(self):
        self.install(); claim = self.claim()
        self.backend.data['budget']['overrun_hold'] = True
        with self.assertRaisesRegex(StoreError, 'work_authority_budget_unavailable'):
            self.begin(claim)

    def test_followup_rebind_preserves_expiry_and_invalidates_old_claim(self):
        initial = self.install(); claim = self.claim(); evidence = self.followup()
        record = self.store.rebind_grant(self.scope, self.request, evidence, expected_grant_id=initial['grant']['grant_id'])
        for key in ('issued_at','expires_at','business_expires_at'):
            self.assertEqual(record['grant'][key], initial['grant'][key])
        self.assertNotEqual(record['grant']['grant_id'], initial['grant']['grant_id'])
        self.assertIsNone(self.queue.get_work(self.wid)['lease'])
        with self.assertRaisesRegex(QueueError, 'stale_claim'):
            self.begin(claim)
        self.assertEqual(self.store.rebind_grant(self.scope, self.request, evidence,
            expected_grant_id=initial['grant']['grant_id']), record)
        history = self.backend.data['authority-history-' + initial['grant']['grant_id']]
        self.assertEqual(history['grant'], initial['grant'])

    def test_scope_or_recipient_or_brief_changes_do_not_rebind(self):
        initial = self.install()
        for edit in ('brief','recipients','generation'):
            r = copy.deepcopy(self.request); evidence = self.followup()
            if edit == 'brief': self.request['work_brief_sha256'] = 'f'*64
            if edit == 'recipients': self.request['source']['cc'] = []
            if edit == 'generation': self.request['control']['work_generation'] += 1
            evidence = replace(evidence, binding_sha256=scope_digest(build_work_authority_binding(self.scope, self.request)))
            with self.assertRaises((StoreError, WorkAuthorityError)):
                self.store.rebind_grant(self.scope,self.request,evidence,expected_grant_id=initial['grant']['grant_id'])
            self.request = r
        self.assertEqual(self.store.observe_work(self.wid)['record'], initial)

    def test_queue_page_has_no_old_operations_records(self):
        self.install()
        self.backend.data['some-v7-record'] = {'kind':'work', 'id':'some-v7-record'}
        rows = self.queue.page()['items']
        self.assertEqual([r['work_id'] for r in rows], [self.wid])

    def test_derived_followup_keeps_original_intake_clock_and_true_policy_provenance(self):
        r = self.request
        r['delegation'].update(basis='accepted_brand_policy_derivation', parent_policy_digest=scope_digest(self.policy),
            parent_acceptance_ref=self.policy['acceptance']['evidence_ref'], source_fingerprint=r['source']['fingerprint'])
        intake = TrustedIntakeEvidence(adapter_id='hamle-authenticated-intake-v1', provider_identity='hamle@gamechangers.com.tr',
            evidence_ref='synthetic-initial-provider-read', classification_evidence_ref='synthetic-routine-check',
            binding_sha256=scope_digest(build_work_authority_binding(self.scope,r)), parent_policy_digest=scope_digest(self.policy),
            observed_at=stamp())
        initial = self.store.install_grant(self.scope, r, intake=intake)
        followup = self.followup(); r['delegation']['source_fingerprint'] = r['source']['fingerprint']
        current = self.store.rebind_grant(self.scope,r,followup,expected_grant_id=initial['grant']['grant_id'])
        self.assertEqual(initial['grant']['issued_at'], current['grant']['issued_at'])
        self.assertEqual(current['grant']['issuer_evidence_ref'], self.policy['acceptance']['evidence_ref'])
        self.assertEqual(self.begin(self.claim())['state'], 'started')

    def test_model_or_general_new_intake_cannot_rebind_followup(self):
        initial = self.install(); receipt = self.followup()
        for evidence in (asdict(receipt), replace(receipt, intake_kind='new_work'), replace(receipt, model_supplied_authority=True)):
            with self.assertRaisesRegex(WorkAuthorityError, 'trusted_followup_required'):
                self.store.rebind_grant(self.scope,self.request,evidence,expected_grant_id=initial['grant']['grant_id'])
        self.assertEqual(self.store.observe_work(self.wid)['record'], initial)

    def test_rebind_cas_conflict_rolls_back_all_state(self):
        initial = self.install(); receipt = self.followup()
        before = copy.deepcopy(self.backend.data)
        with self.assertRaisesRegex(StoreError, 'stale_work_grant'):
            self.store.rebind_grant(self.scope,self.request,receipt,expected_grant_id='wrong')
        self.assertEqual(before, self.backend.data)

    def test_missing_live_flag_is_closed_and_legacy_collection_work_does_not_migrate(self):
        self.scope['services'][0].pop('live_enabled')
        with self.assertRaisesRegex(StoreError, 'customer_service_not_live'):
            self.install()
        self.backend.data['legacy-id'] = {'kind':'work','work_id':'legacy-id'}
        with self.assertRaisesRegex(StoreError, 'authority_work_missing'):
            self.store.observe_work('legacy-id')

    def customer_stop(self):
        receipt = TrustedWorkStop(self.wid, 'client@example.test', 'gmail-read:stop-1',
            'synthetic-gmail-provider-read-stop-1', stamp(), channel='authenticated_gmail_api',
            thread_id='thread-1', actual_to_cc=('hamle@gamechangers.com.tr',),
            provider_identity='hamle@gamechangers.com.tr', source_message_id='stop-message-1',
            source_fingerprint='e'*64, source_sender='client@example.test',
            work_to=('client@example.test',), work_cc=('cc@example.test',))
        catalog = {'verified':True,'brand_id':'wat_mobilite','contacts':[{'email':'client@example.test',
            'verified':True,'side':'customer','can_request':True,'service_ids':['coordination'],
            'verification_ref':'synthetic-verified-requester-catalog'}]}
        return receipt, catalog

    def test_verified_same_work_customer_can_stop_without_becoming_grant_issuer(self):
        self.install(); claim = self.claim(); receipt, catalog = self.customer_stop()
        result = self.store.stop_work(self.wid, receipt, expected_work_generation=0,
            trusted_scope=self.scope, trusted_catalog=catalog)
        revoked = result['record']['grant']
        self.assertEqual(revoked['status'],'revoked')
        self.assertEqual(revoked['revocations'][0]['actor'],'client@example.test')
        self.assertEqual(revoked['revocations'][0]['actor_kind'],'verified_customer_participant')
        self.assertNotIn('client@example.test',revoked['authorized_issuers'])
        self.assertEqual(result['work']['work_generation'],1)
        self.assertIsNone(result['work']['lease'])
        with self.assertRaisesRegex(StoreError,'work_stopped_or_generation_changed'):
            self.begin(claim)

    def test_customer_stop_needs_verified_catalog_scope_and_same_service(self):
        self.install(); receipt, catalog = self.customer_stop()
        for mutation in ('unverified','wrong_brand','wrong_service','cannot_request'):
            c = copy.deepcopy(catalog)
            if mutation=='unverified':c['contacts'][0]['verified']=False
            if mutation=='wrong_brand':c['brand_id']='rabam'
            if mutation=='wrong_service':c['contacts'][0]['service_ids']=['different_service']
            if mutation=='cannot_request':c['contacts'][0]['can_request']=False
            with self.assertRaises(StoreError):
                self.store.stop_work(self.wid,receipt,expected_work_generation=0,
                    trusted_scope=self.scope,trusted_catalog=c)
        self.assertEqual(self.store.observe_work(self.wid)['record']['grant']['status'],'active')

    def test_customer_stop_cannot_use_other_work_recipients_or_fake_provider_flags(self):
        self.install(); receipt, catalog = self.customer_stop()
        for candidate in (replace(receipt,work_cc=()), replace(receipt,thread_id='other'),
                          replace(receipt,actual_to_cc=()), replace(receipt,source_sender=AHMET),
                          replace(receipt,source_message_id=None), replace(receipt,model_supplied_authority=True),
                          asdict(receipt)):
            with self.assertRaises(StoreError):
                self.store.stop_work(self.wid,candidate,expected_work_generation=0,
                    trusted_scope=self.scope,trusted_catalog=catalog)
        self.assertEqual(self.queue.get_work(self.wid)['mode'],'active')

    def test_catalog_unavailable_real_received_stop_holds_with_honest_provenance(self):
        self.install(); claim=self.claim(); receipt,_=self.customer_stop()
        with self.assertRaisesRegex(StoreError,'customer_stop_trusted_scope_required'):
            self.store.stop_work(self.wid,receipt,expected_work_generation=0)
        result=self.store.hold_received_stop(self.wid,receipt,expected_work_generation=0)
        self.assertEqual(result['record']['grant']['status'],'revoked')
        self.assertTrue(result['record']['last_stop']['verification_pending'])
        self.assertEqual(result['record']['grant']['revocations'][0]['actor_kind'],
            'provider_stop_verification_pending')
        with self.assertRaisesRegex(StoreError,'work_stopped_or_generation_changed'):
            self.begin(claim)

    def test_pending_stop_is_not_a_raw_json_or_ui_bypass(self):
        self.install(); receipt,_=self.customer_stop()
        for candidate in (asdict(receipt),replace(receipt,channel='authenticated_control_screen'),
                          replace(receipt,provider_identity=AHMET),replace(receipt,stop_intent='model_uncertain')):
            with self.assertRaises(StoreError):
                self.store.hold_received_stop(self.wid,candidate,expected_work_generation=0)
        self.assertEqual(self.queue.get_work(self.wid)['mode'],'active')

    def test_received_customer_stop_after_budget_deadline_still_fences(self):
        self.install(); receipt,catalog=self.customer_stop();self.now+=timedelta(days=4)
        receipt=replace(receipt,observed_at=self.now.isoformat())
        result=self.store.stop_work(self.wid,receipt,expected_work_generation=0,
            trusted_scope=self.scope,trusted_catalog=catalog)
        self.assertEqual(result['record']['grant']['status'],'revoked')


if __name__ == '__main__':
    unittest.main()
