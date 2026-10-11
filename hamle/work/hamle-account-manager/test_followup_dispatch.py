"""Offline integration of follow-up review, immutable binding and dispatch.

Every provider/model adapter is injected. The HTTP guard also makes an accidental
fallback to a real adapter fail this suite before any network request.
"""
import copy
import hashlib
import json
from datetime import timedelta
import unittest
from unittest.mock import patch

from cloud_store import StoreError, action_id
from dispatch import dispatch_approved
from test_cloud_store import task_observation
import test_worker as fixtures


class FollowupDispatchTests(unittest.TestCase):
    def setUp(self):
        guard = patch("requests.sessions.Session.request",
                      side_effect=AssertionError("real HTTP is forbidden in this suite"))
        guard.start(); self.addCleanup(guard.stop)
        self.w = fixtures.WorkerTests(); self.w.setUp(); self.w.open()
        self.store = self.w.store
        self.w.result["analysis"]["intent"] = "complaint_strategy"
        self.w.result["analysis"]["escalation"] = {
            "owner": "cansu_and_ahmet", "reason": "Stratejik karar insan değerlendirmesinde."}
        initial = self.w.run_worker()
        self.assertEqual(initial["status"], "review_required", initial)
        self.seed_pid = initial["proposal_id"]
        self.store.approve(self.seed_pid, initial["proposal_hash"], initial["control_generation"],
                           "verified-subject", purpose="internal_coordination_only")
        seed_writes = []

        def seed_task(payload, *, before_post):
            before_post(); seed_writes.append("task")
            return {"provider_id": "10392247471"}

        def seed_reply(payload, *, before_post):
            before_post(); seed_writes.append("reply")
            return {"provider_id": "abc123"}

        seeded = dispatch_approved(
            self.store, self.seed_pid, capture=self.w.capture, check_basecamp=self.w.check,
            create_task=seed_task, read_task=self.verified, send_reply=seed_reply,
            read_reply=self.verified, clock=lambda: self.w.now)
        self.assertEqual(seeded["status"], "internal_actions_readback_verified", seeded)
        self.assertEqual(seed_writes, ["task", "reply"])
        self.work = self.store.bind_verified_work(self.seed_pid)
        self.assertEqual(self.work["task_id"], 10392247471)
        self.assertEqual(self.work["last_reply_message_id"], "abc123")

        self.text = "Önceki stratejik konu açık kalsın; mevcut işte bu iç notu takip edelim."
        self.evidence = copy.deepcopy(self.w.evidence)
        self.evidence.update(message_id="ab14", snapshot_fingerprint="b" * 64,
            source_sha256=hashlib.sha256(self.text.encode()).hexdigest(),
            in_reply_to="<followup@example.test>",
            references="<internal@example.test> <reply@example.test> <followup@example.test>",
            source_subject="Re: " + fixtures.SUBJECT, is_continuation=True,
            root_message_id=self.work["root_message_id"],
            root_rfc_message_id=self.work["root_rfc_message_id"],
            latest_to=[fixtures.HAMLE], latest_cc=["cansu@gamechangers.com.tr"])
        self.observation = task_observation(self.work, self.w.now)
        self.followup_result = copy.deepcopy(self.w.result)
        # Even a newly classified routine note must inherit the open strategy
        # decision; the model cannot erase it by returning escalation=none.
        self.followup_result["response_id"] = "resp_followup"
        self.followup_result["analysis"]["intent"] = "routine_request"
        self.followup_result["analysis"]["escalation"] = {"owner": "none", "reason": None}
        self.writes = []
        self.reads = []
        self.captures = 0
        self.observations = 0
        self.model_calls = 0

    @staticmethod
    def verified(provider_id, payload):
        return {"provider_id": provider_id, "identity_verified": True,
                "matches_approved_payload": True}

    def capture(self):
        self.captures += 1
        return {"source_text": self.text, "evidence": copy.deepcopy(self.evidence)}

    def capture_continuation(self, work):
        self.assertEqual(work["work_id"], self.work["work_id"])
        self.assertEqual(work["task_id"], 10392247471)
        return self.capture()

    def read_status(self, work):
        self.assertEqual(work["work_id"], self.work["work_id"])
        self.observations += 1
        return copy.deepcopy(self.observation)

    def model(self, key, request):
        self.model_calls += 1
        self.assertEqual(key, "FAKE-KEY-NEVER-PERSIST-OR-PRINT")
        context = json.loads(request["request_text"])
        self.assertEqual(context["son_gelen_mesaj"], self.text)
        self.assertEqual(context["mevcut_gorev"]["task_id"], 10392247471)
        self.assertEqual(context["acik_insan_degerlendirmesi"],
                         {"owner": "cansu_and_ahmet", "status": "open"})
        return copy.deepcopy(self.followup_result)

    def review(self):
        return self.w.run_worker(capture_continuation=self.capture_continuation,
                                 read_task_status=self.read_status, model_call=self.model)

    def prepare(self):
        result = self.review()
        self.assertEqual(result["status"], "review_required", result)
        self.pid = result["proposal_id"]
        self.store.approve(self.pid, result["proposal_hash"], result["control_generation"],
                           "verified-subject", purpose="internal_coordination_only")
        self.assertEqual(self.model_calls, 1)
        return result

    def forbidden_task(self, *args, **kwargs):
        self.fail("a continuation must never create or reconcile a new task")

    def comment(self, payload, *, before_post):
        before_post()
        self.assertEqual(payload["task_id"], 10392247471)
        self.writes.append(("comment", copy.deepcopy(payload)))
        return {"provider_id": "10392249999"}

    def reply(self, payload, *, before_post):
        before_post()
        self.writes.append(("reply", copy.deepcopy(payload)))
        return {"provider_id": "abc124"}

    def read_comment(self, provider_id, payload):
        self.reads.append(("comment", provider_id))
        self.assertEqual(provider_id, "10392249999")
        return self.verified(provider_id, payload)

    def read_reply(self, provider_id, payload):
        self.reads.append(("reply", provider_id))
        self.assertEqual(provider_id, "abc124")
        return self.verified(provider_id, payload)

    def dispatch(self, **overrides):
        args = dict(capture=self.capture, check_basecamp=self.w.check,
                    create_task=self.forbidden_task, read_task=self.forbidden_task,
                    send_reply=self.reply, read_reply=self.read_reply,
                    create_comment=self.comment, read_comment=self.read_comment,
                    read_task_status=self.read_status, clock=lambda: self.w.now)
        args.update(overrides)
        return dispatch_approved(self.store, self.pid, **args)

    def actions(self):
        return {a["kind"]: a for a in self.store.public_state()["actions"]
                if a["proposal_id"] == self.pid}

    def test_accepted_comment_recovers_by_readback_then_sends_one_reply(self):
        self.prepare()
        first = self.dispatch(read_comment=lambda *_: {"identity_verified": False})
        self.assertEqual(first["reason"], "provider_readback_unconfirmed")
        self.assertEqual([kind for kind, _ in self.writes], ["comment"])
        self.assertEqual(self.actions()["basecamp_comment"]["state"], "accepted")
        self.assertNotIn("gmail_reply", self.actions())
        recovered = self.dispatch(create_comment=self.forbidden_task)
        self.assertEqual(recovered["status"], "internal_actions_readback_verified", recovered)
        self.assertEqual(recovered["new_write_count"], 1)
        self.assertEqual(recovered["new_task_count"], 0)
        self.assertEqual([kind for kind, _ in self.writes], ["comment", "reply"])
        self.assertEqual(self.reads, [("comment", "10392249999"), ("reply", "abc124")])
        self.assertEqual({a["state"] for a in self.actions().values()}, {"verified"})

    def test_unknown_comment_holds_permanently_without_mail_or_resend(self):
        self.prepare()
        def unknown(payload, *, before_post):
            self.comment(payload, before_post=before_post)
            raise TimeoutError("provider-secret-must-not-leak")
        first = self.dispatch(create_comment=unknown)
        self.assertEqual(first["reason"], "provider_write_outcome_unknown_no_retry")
        self.assertNotIn("provider-secret", json.dumps(first))
        self.assertEqual(self.actions()["basecamp_comment"]["state"], "unknown")
        for _ in range(2):
            held = self.dispatch(create_comment=self.forbidden_task)
            self.assertEqual(held["reason"], "existing_action_held_no_retry")
        self.assertEqual([kind for kind, _ in self.writes], ["comment"])
        self.assertNotIn("gmail_reply", self.actions())
        self.assertEqual(self.store.get_work(self.work["work_id"])["revision_count"], 0)

    def test_latest_cc_removed_after_comment_holds_reply_and_never_uses_old_cc(self):
        self.prepare()
        def remove_cc_after_receipt(provider_id, payload):
            result = self.read_comment(provider_id, payload)
            # Keep the other immutable capture fields equal to exercise the
            # explicit recipient fence independently of the source digest.
            self.evidence["latest_cc"] = []
            return result
        result = self.dispatch(read_comment=remove_cc_after_receipt)
        self.assertEqual(result["reason"], "latest_recipients_changed")
        self.assertEqual([kind for kind, _ in self.writes], ["comment"])
        self.assertEqual(self.actions()["basecamp_comment"]["state"], "verified")
        self.assertNotEqual(self.actions()["gmail_reply"]["state"], "accepted")
        again = self.dispatch()
        self.assertEqual(again["reason"], "existing_action_held_no_retry")
        self.assertEqual(len(self.writes), 1)

    def test_own_comment_timestamp_change_keeps_semantic_fingerprint_and_allows_mail(self):
        self.prepare()
        fingerprint = self.observation["task_fingerprint"]
        def updated_after_comment(provider_id, payload):
            result = self.read_comment(provider_id, payload)
            self.w.now += timedelta(seconds=1)
            self.observation["updated_at"] = self.w.now.isoformat()
            self.observation["observed_at"] = self.w.now.isoformat()
            self.evidence["captured_at"] = self.w.now.isoformat()
            return result
        result = self.dispatch(read_comment=updated_after_comment)
        self.assertEqual(result["status"], "internal_actions_readback_verified", result)
        self.assertEqual(self.observation["task_fingerprint"], fingerprint)
        self.assertEqual([kind for kind, _ in self.writes], ["comment", "reply"])
        self.assertEqual(self.writes[-1][1]["to"], [fixtures.AHMET])
        self.assertEqual(self.writes[-1][1]["cc"], ["cansu@gamechangers.com.tr"])
        self.assertGreaterEqual(self.observations, 3)  # Review plus both POST boundaries.

    def test_complete_repeat_only_reads_receipts_and_never_reopens_or_duplicates_work(self):
        self.prepare()
        first = self.dispatch()
        self.assertEqual(first["status"], "internal_actions_readback_verified", first)
        completed = self.store.get_work(self.work["work_id"])
        self.assertEqual(completed["revision_count"], 1)
        self.assertEqual(completed["last_message_id"], "ab14")
        self.assertEqual(completed["last_reply_message_id"], "abc124")
        self.assertIsNone(completed["pending_event_id"])
        def no_recapture():
            self.fail("receipt reconciliation must not recapture our own latest mail")
        repeated = self.dispatch(capture=no_recapture, create_comment=self.forbidden_task,
                                 send_reply=self.forbidden_task)
        self.assertEqual(repeated["status"], "internal_actions_readback_verified", repeated)
        self.assertEqual(repeated["new_write_count"], 0)
        self.assertEqual(repeated["new_task_count"], 0)
        self.assertEqual(self.store.get_work(self.work["work_id"]), completed)
        self.assertEqual([kind for kind, _ in self.writes], ["comment", "reply"])
        self.assertEqual(self.reads, [("comment", "10392249999"), ("reply", "abc124")] * 2)
        self.assertEqual(self.review()["status"], "already_recorded")
        self.assertEqual(self.model_calls, 1)
        self.assertEqual(self.store.bind_verified_work(self.seed_pid)["revision_count"], 1)

    def test_routine_followup_inherits_strategy_and_cannot_get_routine_authority(self):
        result = self.review()
        self.assertEqual(result["status"], "review_required", result)
        self.pid = result["proposal_id"]
        with self.assertRaises(StoreError) as caught:
            self.store.approve(self.pid, result["proposal_hash"], result["control_generation"],
                               "verified-subject", purpose="routine_execution")
        self.assertEqual(caught.exception.code, "human_escalation_required")
        approved = self.store.approve(self.pid, result["proposal_hash"], result["control_generation"],
                                      "verified-subject", purpose="internal_coordination_only")
        self.assertNotIn("task", approved["payload"])
        self.assertEqual(approved["payload"]["work"]["escalation_status"], "open")
        self.assertEqual(approved["payload"]["work"]["escalation_owner"], "cansu_and_ahmet")
        self.assertFalse(approved["approval"]["decision_authorized"])
        self.assertFalse(approved["approval"]["production_authorized"])
        self.assertEqual(self.dispatch()["status"], "internal_actions_readback_verified")
        saved = self.store.get_work(self.work["work_id"])
        self.assertEqual((saved["escalation_status"], saved["escalation_owner"]),
                         ("open", "cansu_and_ahmet"))

    def test_closed_task_before_review_blocks_model_and_new_proposal(self):
        self.observation["completed"] = True
        self.observation["task_fingerprint"] = "d" * 64
        before = len(self.store.public_state()["proposals"])
        result = self.review()
        self.assertEqual(result["reason"], "work_not_active")
        self.assertFalse(result["model_attempt_started"])
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(len(self.store.public_state()["proposals"]), before)
        self.assertEqual(self.writes, [])

    def test_closed_task_after_comment_blocks_mail_at_final_boundary(self):
        self.prepare()
        def closed_after_receipt(provider_id, payload):
            result = self.read_comment(provider_id, payload)
            self.observation["completed"] = True
            self.observation["task_fingerprint"] = "d" * 64
            return result
        result = self.dispatch(read_comment=closed_after_receipt)
        self.assertEqual(result["reason"], "task_changed_before_dispatch")
        self.assertEqual([kind for kind, _ in self.writes], ["comment"])
        self.assertEqual(self.actions()["basecamp_comment"]["state"], "verified")
        self.assertEqual(self.store.get_work(self.work["work_id"])["revision_count"], 0)
        self.assertEqual(self.dispatch()["reason"], "existing_action_held_no_retry")


if __name__ == "__main__":
    unittest.main()
