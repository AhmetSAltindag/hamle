import copy
import hashlib
from datetime import timedelta
import unittest

from dispatch import dispatch_approved
import test_worker as fixtures
from cloud_store import proposal_hash


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.w = fixtures.WorkerTests(); self.w.setUp(); self.w.open()
        result = self.w.run_worker(); self.pid = result["proposal_id"]
        self.store = self.w.store
        self.store.approve(self.pid, result["proposal_hash"], result["control_generation"], "verified-subject")
        self.writes = []; self.reads = []

    def task(self, payload, *, before_post):
        before_post()
        self.writes.append(("task", payload)); return {"provider_id": "12345"}

    def reply(self, payload, *, before_post):
        before_post()
        self.writes.append(("reply", payload)); return {"provider_id": "abc123"}

    def read(self, pid, payload):
        self.reads.append(pid)
        return {"provider_id": pid, "identity_verified": True, "matches_approved_payload": True}

    def run_dispatch(self, **kw):
        args = dict(capture=self.w.capture, check_basecamp=self.w.check, create_task=self.task,
                    read_task=self.read, send_reply=self.reply, read_reply=self.read, clock=lambda: self.w.now)
        args.update(kw)
        return dispatch_approved(self.store, self.pid, **args)

    def test_task_then_mail_once_exact_approval_reused_only_for_reads(self):
        result = self.run_dispatch()
        self.assertEqual(result["status"], "internal_actions_readback_verified")
        self.assertEqual([x[0] for x in self.writes], ["task", "reply"])
        # The newly sent self-mail makes a fresh customer capture inappropriate.
        def forbidden_capture():
            self.fail("already-receipted writes must reconcile by provider ID")
        again = self.run_dispatch(capture=forbidden_capture)
        self.assertEqual(again["new_write_count"], 0)
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(self.reads, ["12345", "abc123", "12345", "abc123"])

    def test_takeover_blocks_all_writes(self):
        self.store.takeover("subject")
        self.assertEqual(self.run_dispatch()["status"], "held")
        self.assertEqual(self.writes, [])

    def test_unknown_task_stops_mail_and_never_retries(self):
        def unknown(payload, *, before_post):
            before_post()
            self.writes.append(("task", payload)); raise TimeoutError("raw-provider-secret")
        result = self.run_dispatch(create_task=unknown)
        self.assertEqual(result["reason"], "provider_write_outcome_unknown_no_retry")
        self.assertEqual(self.run_dispatch()["reason"], "existing_action_held_no_retry")
        self.assertEqual([x[0] for x in self.writes], ["task"])

    def test_failed_task_readback_blocks_mail_but_allows_receipt_only_read_recovery(self):
        result = self.run_dispatch(read_task=lambda *_: {"identity_verified": False})
        self.assertEqual(result["reason"], "provider_readback_unconfirmed")
        self.assertEqual([x[0] for x in self.writes], ["task"])
        self.assertEqual(self.run_dispatch()["status"], "internal_actions_readback_verified")
        self.assertEqual([x[0] for x in self.writes], ["task", "reply"])

    def test_stop_after_task_receipt_prevents_reply(self):
        def task(payload, *, before_post):
            result = self.task(payload, before_post=before_post); self.store.takeover("subject"); return result
        self.assertEqual(self.run_dispatch(create_task=task)["status"], "held")
        self.assertEqual([x[0] for x in self.writes], ["task"])
        self.assertEqual(self.store.public_state()["actions"][0]["state"], "verified")

    def test_new_incoming_message_between_actions_blocks_reply(self):
        count = 0
        def capture():
            nonlocal count
            count += 1
            value = self.w.capture()
            if count == 2:
                value["source_text"] = "Değişen iç talep"
                value["evidence"]["source_sha256"] = hashlib.sha256(value["source_text"].encode()).hexdigest()
            return value
        self.assertEqual(self.run_dispatch(capture=capture)["reason"], "source_changed_before_dispatch")
        self.assertEqual([x[0] for x in self.writes], ["task"])

    def test_todoset_id_cannot_be_silently_used_as_a_todolist(self):
        self.w.target["todo_parent_type"] = "Todolist"
        self.assertEqual(self.run_dispatch()["reason"], "basecamp_target_changed")
        self.assertEqual(self.writes, [])

    def test_unknown_mail_never_repeats(self):
        def unknown(payload, *, before_post):
            before_post()
            self.writes.append(("reply", payload)); raise TimeoutError()
        self.assertEqual(self.run_dispatch(send_reply=unknown)["status"], "held")
        self.assertEqual(self.run_dispatch()["reason"], "existing_action_held_no_retry")
        self.assertEqual([x[0] for x in self.writes], ["task", "reply"])

    def test_expired_approval_blocks_before_post(self):
        self.w.now += timedelta(seconds=301)
        self.w.evidence["captured_at"] = self.w.now.isoformat()
        self.assertEqual(self.run_dispatch()["reason"], "approval_expired_or_mismatch")
        self.assertEqual(self.writes, [])

    def test_takeover_during_writer_preflight_blocks_post(self):
        def preflight(payload, *, before_post):
            self.store.takeover("subject")
            before_post()
            self.writes.append(("task", payload))
            return {"provider_id": "12345"}
        result = self.run_dispatch(create_task=preflight)
        self.assertEqual(result["status"], "held")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.store.public_state()["actions"][0]["state"], "cancelled")

    def test_lease_expiring_during_writer_preflight_blocks_post_and_retry(self):
        def preflight(payload, *, before_post):
            self.w.now += timedelta(seconds=61)
            self.w.evidence["captured_at"] = self.w.now.isoformat()
            before_post()
            self.writes.append(("task", payload))
            return {"provider_id": "12345"}
        self.assertEqual(self.run_dispatch(create_task=preflight)["reason"], "action_not_dispatchable")
        self.assertEqual(self.writes, [])
        self.assertEqual(self.run_dispatch()["reason"], "existing_action_held_no_retry")

    def test_writer_missing_callback_cannot_record_a_receipt_or_retry(self):
        result = self.run_dispatch(create_task=lambda payload, **_: {"provider_id": "12345"})
        self.assertEqual(result["reason"], "provider_dispatch_callback_missing")
        self.assertEqual(self.store.public_state()["actions"][0]["state"], "leased")
        self.assertEqual(self.run_dispatch()["reason"], "existing_action_held_no_retry")
        self.assertEqual(self.writes, [])

    def test_writer_preflight_exception_remains_reserved(self):
        def preflight(payload, *, before_post):
            raise TimeoutError("must-not-leak")
        self.assertEqual(self.run_dispatch(create_task=preflight)["reason"], "provider_preflight_or_boundary_failed")
        self.assertEqual(self.store.public_state()["actions"][0]["state"], "leased")
        self.assertEqual(self.run_dispatch()["reason"], "existing_action_held_no_retry")
        self.assertEqual(self.writes, [])

    def test_unapproved_proposal_never_writes(self):
        row = self.w.backend.data["proposal-" + self.pid]
        row["approval"] = None; row["status"] = "awaiting_human_review"
        self.assertEqual(self.run_dispatch()["reason"], "exact_approval_required")
        self.assertEqual(self.writes, [])

    def test_even_human_approved_commercial_work_is_not_a_routine_dispatch(self):
        row = self.w.backend.data["proposal-" + self.pid]
        row["payload"]["analysis"]["intent"] = "commercial"
        row["payload"]["analysis"]["escalation"] = {"owner": "ahmet", "reason": "Teklif kararı"}
        row["proposal_hash"] = proposal_hash(row["payload"])
        row["approval"]["proposal_hash"] = row["proposal_hash"]
        self.assertEqual(self.run_dispatch()["reason"], "human_escalation_required")
        self.assertEqual(self.writes, [])

    def test_approved_subject_cannot_inject_another_mail_header(self):
        row = self.w.backend.data["proposal-" + self.pid]
        row["payload"]["reply"]["subject"] = "Konu\r\nBcc: other@example.test"
        row["proposal_hash"] = proposal_hash(row["payload"])
        row["approval"]["proposal_hash"] = row["proposal_hash"]
        self.assertEqual(self.run_dispatch()["reason"], "reply_subject_header_invalid")
        self.assertEqual(self.writes, [])

    def coordination(self):
        row = self.w.backend.data["proposal-" + self.pid]
        row["payload"]["analysis"]["intent"] = "complaint_strategy"
        row["payload"]["analysis"]["escalation"] = {"owner": "cansu_and_ahmet", "reason": "Stratejik değerlendirme bekliyor"}
        row["proposal_hash"] = proposal_hash(row["payload"])
        row["approval"] = None; row["status"] = "awaiting_human_review"
        before = copy.deepcopy(row["payload"])
        self.store.approve(self.pid, row["proposal_hash"], row["generation"], "subject", purpose="internal_coordination_only")
        return before

    def test_explicit_internal_coordination_sends_only_exact_internal_actions_and_keeps_escalation_open(self):
        before = self.coordination()
        self.assertEqual(self.run_dispatch()["status"], "internal_actions_readback_verified")
        self.assertEqual([x[0] for x in self.writes], ["task", "reply"])
        p = self.store.public_state()["proposals"][0]
        self.assertEqual(p["payload"], before)
        self.assertEqual(p["approval"]["escalation_status"], "open")
        self.assertIs(p["approval"]["decision_authorized"], False)
        self.assertIs(p["approval"]["production_authorized"], False)
        self.assertEqual(self.writes[1][1]["to"], ["ahmet@gamechangers.com.tr"])
        self.assertEqual(self.writes[0][1]["assignee_id"], 53358100)

    def test_coordination_without_purpose_or_open_escalation_is_held(self):
        self.coordination()
        row = self.w.backend.data["proposal-" + self.pid]
        original = copy.deepcopy(row["approval"])
        for field, value in (("purpose", "routine_execution"), ("escalation_status", "resolved"),
                             ("decision_authorized", True), ("production_authorized", True)):
            row["approval"] = {**original, field: value}
            self.assertEqual(self.run_dispatch()["reason"], "human_escalation_required")
        self.assertEqual(self.writes, [])

    def test_coordination_uses_same_short_approval_expiry(self):
        self.coordination()
        self.w.now += timedelta(seconds=301)
        self.w.evidence["captured_at"] = self.w.now.isoformat()
        self.assertEqual(self.run_dispatch()["reason"], "approval_expired_or_mismatch")
        self.assertEqual(self.writes, [])


if __name__ == "__main__":
    unittest.main()
