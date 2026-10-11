"""Transactional pilot journal. Real store rules, fake serializable backend."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import unittest

from cloud_store import OperationsStore, StoreError, proposal_hash, LIMIT
from test_cloud_store import MemoryBackend, budget_snapshot
from v11_internal_state import InternalPilotStore, PILOT_ID, PILOT_DOC


class InternalPilotStateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat("2026-10-10T12:00:00+03:00")
        self.backend = MemoryBackend()
        self.ops = OperationsStore(self.backend, clock=lambda: self.now)
        self.ops.initialize_control()
        self.ops.initialize_budget(budget_snapshot())
        self.generation = self.ops.resume_review_only("verified-owner")["generation"]
        self.store = InternalPilotStore(self.ops)
        self.source = {"verified": True, "subject": PILOT_ID, "thread_id": "abcd12", "message_id": "abcd13",
                       "source_fingerprint": "a" * 64, "control_generation": self.generation,
                       "captured_at": self.now.isoformat(), "expires_at": "2027-12-31T23:59:59+03:00",
                       "evidence_ref": "actual-mail-read-test"}
        self.state = {"work_id": "internal-production-test", "mode": "internal_pilot", "control": "active",
                      "status": "awaiting_production", "revision": 0,
                      "brief": {"reply_envelope": {"thread_id": "abcd12"},
                                "binding": {"account_id": 5898865, "project_id": 49184053}}}
        self.payload = {"kind": "basecamp_todo", "target": {"project_id": 49184053}, "title": "Internal test"}
        self.receipt = {"provider_id": "12345"}

    def init(self):
        return self.store.initialize(self.state, self.source)

    def prepared(self):
        if self.store.read() is None:
            self.init()
        return self.store.prepare_action("create-main-task", self.payload)

    def begun(self):
        row = self.prepared()
        return self.store.begin_action(row["action_key"], row["payload_sha256"])

    def proof(self):
        return {"verified": True, "action_key": "create-main-task", "payload_sha256": proposal_hash(self.payload),
                "receipt_sha256": proposal_hash(self.receipt), "evidence_ref": "actual-provider-readback-test"}

    def finish(self):
        return self.store.finish_action("create-main-task", self.receipt, self.proof())

    def fails(self, code, fn):
        with self.assertRaises(StoreError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_uninitialized_read_and_summary_are_read_only(self):
        before = copy.deepcopy(self.backend.data)
        self.assertIsNone(self.store.read())
        self.assertFalse(self.store.summary()["initialized"])
        self.assertEqual(self.backend.data, before)

    def test_initialize_pins_source_state_and_restart_preserves_progress(self):
        initial = self.init()
        updated = copy.deepcopy(self.state)
        updated["status"] = "running"
        saved = self.store.save(updated, initial["revision"])
        restarted = InternalPilotStore(OperationsStore(self.backend, clock=lambda: self.now))
        self.assertEqual(restarted.initialize(self.state, self.source), saved)
        self.assertEqual(restarted.read()["state"]["status"], "running")

    def test_another_source_or_initial_state_cannot_reset_pilot(self):
        self.init()
        source = copy.deepcopy(self.source); source["message_id"] = "abcd99"
        self.fails("pilot_initialization_conflict", lambda: self.store.initialize(self.state, source))
        state = copy.deepcopy(self.state); state["status"] = "delivered"
        self.fails("pilot_initialization_conflict", lambda: self.store.initialize(state, self.source))

    def test_initial_capture_must_be_fresh_and_not_future(self):
        for seconds in (-301, 1):
            source = copy.deepcopy(self.source)
            source["captured_at"] = (self.now + timedelta(seconds=seconds)).isoformat()
            self.fails("pilot_source_capture_stale", lambda: self.store.initialize(self.state, source))

    def test_source_deadline_cannot_outlive_authorization(self):
        source = copy.deepcopy(self.source); source["expires_at"] = "2028-01-04T00:00:00+03:00"
        self.fails("pilot_expiry_invalid", lambda: self.store.initialize(self.state, source))

    def test_wrong_subject_or_unverified_source_rejected(self):
        for patch in ({"subject": "another subject"}, {"verified": False}):
            source = {**self.source, **patch}
            self.fails("pilot_source_not_verified", lambda: self.store.initialize(self.state, source))

    def test_customer_project_and_different_thread_rejected(self):
        for field, value in (("project_id", 40694338), ("account_id", 1)):
            state = copy.deepcopy(self.state); state["brief"]["binding"][field] = value
            self.fails("pilot_state_scope_mismatch", lambda: self.store.initialize(state, self.source))
        state = copy.deepcopy(self.state); state["brief"]["reply_envelope"]["thread_id"] = "ffff"
        self.fails("pilot_state_scope_mismatch", lambda: self.store.initialize(state, self.source))

    def test_state_save_uses_store_cas_not_production_revision(self):
        self.init()
        state = copy.deepcopy(self.state); state["revision"] = 50
        saved = self.store.save(state, 0)
        self.assertEqual((saved["revision"], saved["state"]["revision"]), (1, 50))
        self.fails("pilot_revision_conflict", lambda: self.store.save(state, 0))

    def test_state_cannot_change_work_identity(self):
        self.init()
        state = copy.deepcopy(self.state); state["work_id"] = "another"
        self.fails("pilot_work_identity_changed", lambda: self.store.save(state, 0))

    def test_prepared_actions_do_not_change_store_state_revision(self):
        row = self.prepared()
        self.assertEqual(self.store.read()["revision"], 0)
        self.assertFalse(row["dispatch_allowed"])
        self.assertNotIn("dispatch_allowed", self.backend.data[next(k for k in self.backend.data if "-action-" in k)])

    def test_prepare_exact_retry_is_idempotent(self):
        first = self.prepared()
        self.assertEqual(first, self.store.prepare_action("create-main-task", self.payload))
        self.assertEqual(self.store.read()["action_keys"], ["create-main-task"])

    def test_same_key_different_payload_is_refused(self):
        self.prepared()
        self.fails("pilot_action_payload_conflict", lambda: self.store.prepare_action("create-main-task", {**self.payload, "title": "changed"}))

    def test_only_first_begin_has_one_post_permission(self):
        begun = self.begun()
        self.assertTrue(begun["dispatch_allowed"])
        self.assertEqual(begun["state"], "started")
        self.fails("pilot_action_started_or_unknown_no_retry", lambda: self.store.begin_action("create-main-task", begun["payload_sha256"]))

    def test_two_concurrent_begins_only_one_dispatches(self):
        row = self.prepared()
        def begin(_):
            try:
                return self.store.begin_action("create-main-task", row["payload_sha256"])["dispatch_allowed"]
            except StoreError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(begin, range(2))), [False, True])

    def test_unknown_transaction_commit_never_replays_post(self):
        row = self.prepared(); self.backend.unknown_after_commit = True
        self.fails("store_outcome_unknown", lambda: self.store.begin_action("create-main-task", row["payload_sha256"]))
        self.fails("pilot_action_started_or_unknown_no_retry", lambda: self.store.begin_action("create-main-task", row["payload_sha256"]))

    def test_unknown_prepare_commit_can_recover_prepared_row_without_post(self):
        self.init(); self.backend.unknown_after_commit = True
        self.fails("store_outcome_unknown", lambda: self.store.prepare_action("create-main-task", self.payload))
        recovered = self.store.prepare_action("create-main-task", self.payload)
        self.assertEqual(recovered["state"], "prepared")
        self.assertFalse(recovered["dispatch_allowed"])

    def test_state_change_after_prepare_invalidates_dispatch(self):
        row = self.prepared()
        updated = copy.deepcopy(self.state); updated["status"] = "brief_changed"
        self.store.save(updated, 0)
        self.fails("pilot_prepared_state_changed", lambda: self.store.begin_action("create-main-task", row["payload_sha256"]))

    def test_receipt_is_saved_before_readback_and_never_reposted(self):
        begun = self.begun()
        saved = self.store.record_receipt("create-main-task", self.receipt)
        self.assertEqual(saved["state"], "receipt_saved")
        self.assertFalse(saved["dispatch_allowed"])
        self.fails("pilot_action_started_or_unknown_no_retry", lambda: self.store.begin_action("create-main-task", begun["payload_sha256"]))

    def test_failed_readback_preserves_only_known_provider_receipt(self):
        self.begun(); self.store.record_receipt("create-main-task", self.receipt)
        unknown = self.store.fail_action("create-main-task", "readback_failed")
        self.assertEqual(unknown["receipt"], self.receipt)
        self.assertEqual(unknown["state"], "unknown")
        self.assertEqual(self.finish()["state"], "verified")

    def test_failed_post_no_receipt_cannot_replay(self):
        row = self.begun(); self.store.fail_action("create-main-task", "provider_outcome_unknown")
        self.fails("pilot_action_started_or_unknown_no_retry", lambda: self.store.begin_action("create-main-task", row["payload_sha256"]))

    def test_verified_result_reuse_returns_proof_without_post_permission(self):
        self.begun()
        verified = self.finish()
        result = self.store.begin_action("create-main-task", verified["payload_sha256"])
        self.assertEqual(result["proof"], self.proof())
        self.assertEqual(result["receipt"], self.receipt)
        self.assertFalse(result["dispatch_allowed"])

    def test_finish_and_receipt_idempotent_but_conflicts_rejected(self):
        self.begun(); self.store.record_receipt("create-main-task", self.receipt)
        self.assertEqual(self.finish(), self.finish())
        self.fails("pilot_receipt_conflict", lambda: self.store.record_receipt("create-main-task", {"provider_id": "54321"}))
        proof = self.proof(); proof["evidence_ref"] = "another"
        self.fails("pilot_readback_proof_conflict", lambda: self.store.finish_action("create-main-task", self.receipt, proof))

    def test_unverified_wrong_action_or_payload_proof_rejected(self):
        self.begun()
        for update in ({"verified": False}, {"action_key": "another"}, {"receipt_sha256": "c" * 64}):
            self.fails("pilot_readback_proof_invalid", lambda: self.store.finish_action("create-main-task", self.receipt, {**self.proof(), **update}))
        self.fails("pilot_readback_payload_mismatch", lambda: self.store.finish_action("create-main-task", self.receipt, {**self.proof(), "payload_sha256": "c" * 64}))

    def test_finish_before_begin_cannot_fabricate_action_completion(self):
        self.prepared()
        self.fails("pilot_action_not_started", self.finish)

    def test_stop_fences_prepare_begin_and_save(self):
        row = self.prepared(); self.ops.stop("verified-owner")
        for fn in (lambda: self.store.prepare_action("another", self.payload),
                   lambda: self.store.begin_action("create-main-task", row["payload_sha256"]),
                   lambda: self.store.save(self.state, 0)):
            self.fails("control_stopped_or_stale", fn)

    def test_resume_global_control_does_not_resume_old_pilot(self):
        row = self.prepared(); self.ops.stop("verified-owner"); self.ops.resume_review_only("verified-owner")
        self.fails("control_stopped_or_stale", lambda: self.store.begin_action("create-main-task", row["payload_sha256"]))

    def test_work_takeover_permanent_for_this_fixed_pilot(self):
        self.init()
        state = {**self.state, "control": "takeover"}
        self.store.save(state, 0)
        self.fails("pilot_work_stopped_or_taken_over", lambda: self.store.prepare_action("a", self.payload))
        self.fails("pilot_stopped_work_cannot_auto_resume", lambda: self.store.save(self.state, 1))

    def test_after_stop_receipt_finish_evidence_can_still_be_recorded(self):
        self.begun(); self.ops.stop("verified-owner")
        self.store.record_receipt("create-main-task", self.receipt)
        self.assertEqual(self.finish()["state"], "verified")
        self.assertEqual(self.ops.control()["mode"], "stopped")

    def test_after_expiry_evidence_can_be_saved_but_no_new_action(self):
        row = self.begun(); self.now += timedelta(days=500)  # past AUTHORIZATION_DEADLINE
        self.store.record_receipt("create-main-task", self.receipt)
        self.assertEqual(self.finish()["state"], "verified")
        self.assertTrue(self.store.summary()["expired"])
        self.fails("authorization_expired", lambda: self.store.begin_action("create-main-task", row["payload_sha256"]))

    def test_custom_earlier_expiry_is_enforced(self):
        self.source["expires_at"] = (self.now + timedelta(seconds=1)).isoformat()
        row = self.prepared(); self.now += timedelta(seconds=1)
        self.fails("pilot_expired_or_clock_regressed", lambda: self.store.begin_action("create-main-task", row["payload_sha256"]))

    def test_budget_readiness_overrun_and_exact_limit_hold(self):
        self.init()
        original = copy.deepcopy(self.backend.data["budget"])
        variants = []
        variant = copy.deepcopy(original); variant["overrun_hold"] = True; variants.append(variant)
        variant = copy.deepcopy(original); variant["providers"]["google_cloud"]["billing_ready"] = False; variants.append(variant)
        variant = copy.deepcopy(original); variant["providers"]["google_cloud"]["paid_microdollars"] = LIMIT; variants.append(variant)
        for variant in variants:
            self.backend.data["budget"] = variant
            self.fails("budget_not_ready", lambda: self.store.prepare_action("a", self.payload))

    def test_record_size_and_corrupt_state_hash_hold(self):
        self.init()
        state = {**self.state, "oversized": "x" * 60000}
        with self.assertRaises(StoreError):
            self.store.save(state, 0)
        self.backend.data[PILOT_DOC]["state"]["status"] = "tampered"
        self.fails("pilot_integrity_mismatch", self.store.read)

    def test_summary_excludes_source_text_payload_and_receipt_contents(self):
        self.begun(); self.store.record_receipt("create-main-task", self.receipt)
        summary = self.store.summary()
        self.assertFalse(summary["dispatch_allowed"])
        self.assertTrue(summary["actions"][0]["receipt_saved"])
        self.assertNotIn("payload", summary["actions"][0])
        self.assertNotIn("source_evidence", summary)


if __name__ == "__main__":
    unittest.main()
