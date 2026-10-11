import copy
import contextlib
import hashlib
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
import types
import unittest
from unittest.mock import patch, Mock

from cloud_store import OperationsStore, action_id
from test_cloud_store import MemoryBackend, budget_snapshot, seed_bound_work, task_observation
from test_model import draft
from worker import run_review, run_scheduled_review, run_track, run_recover_followup, main, SUBJECT, AHMET, HAMLE


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat("2026-10-10T03:00:00+03:00")
        self.backend = MemoryBackend()
        self.store = OperationsStore(self.backend, clock=lambda: self.now)
        self.store.initialize_control(); self.store.initialize_budget(budget_snapshot())
        self.calls = {"capture": 0, "basecamp": 0, "key": 0, "model": 0}
        self.text = "Yalnız iç denemede bir rehber için brief hazırla."
        self.evidence = {"subject": SUBJECT, "thread_id": "ab12", "message_id": "ab13",
            "snapshot_fingerprint": "a" * 64, "captured_at": self.now.isoformat(),
            "source_sha256": hashlib.sha256(self.text.encode()).hexdigest(), "sender": AHMET,
            "hamle_direct_recipient": True, "body_in_memory_only": True,
            "attachments_fetched": False, "token_refreshed": False, "action_eligible": False,
            "mailbox_writes": 0, "unseen_cc_removal_resolved": False,
            "in_reply_to": "<internal@example.test>", "references": "<earlier@example.test> <internal@example.test>"}
        self.target = {"status": "read_verified", "account_id": 5898865, "project_id": 49184053,
            "person_id": 53358100, "member_ids": [47470759, 53358100], "clients_enabled": False,
            "basecamp_content_writes": 0, "action_eligible": False, "todo_parent_id": 10384126975,
            "todo_parent_type": "Todoset",
            "assignee_id": 53358100}
        analysis = draft(); analysis["brief"]["requested_deadline"] = None
        self.result = {"status": "review_required", "action_eligible": False, "model": "gpt-6-astra",
            "reasoning_effort": "max", "automatic_retry_allowed": False, "analysis": analysis,
            "response_id": "resp_test", "usage": {"input_tokens": 1000, "output_tokens": 500, "total_tokens": 1500}}

    def capture(self):
        self.calls["capture"] += 1
        return {"source_text": self.text, "evidence": copy.deepcopy(self.evidence)}

    def check(self):
        self.calls["basecamp"] += 1
        return copy.deepcopy(self.target)

    def key(self):
        self.calls["key"] += 1
        return "FAKE-KEY-NEVER-PERSIST-OR-PRINT"

    def model(self, key, request):
        self.calls["model"] += 1
        self.assertEqual(request["request_text"], self.text)
        self.assertIsNone(request["requested_deadline"])
        self.assertEqual(request["brand"], "Gamechangers")
        return copy.deepcopy(self.result)

    def run_worker(self, **kw):
        args = dict(capture=self.capture, check_basecamp=self.check, load_key=self.key,
                    model_call=self.model, clock=lambda: self.now)
        args.update(kw)
        return run_review(self.store, **args)

    def open(self):
        self.store.resume_review_only("verified-google-subject")

    def test_stopped_has_zero_provider_or_secret_calls(self):
        self.assertEqual(self.run_worker()["status"], "stopped")
        self.assertEqual(set(self.calls.values()), {0})

    def test_valid_source_produces_exact_review_envelope_no_send(self):
        self.open(); result = self.run_worker()
        self.assertEqual(result["status"], "review_required")
        self.assertEqual(self.calls["model"], 1)
        p = self.store.public_state()["proposals"][0]
        self.assertEqual(set(p["payload"]), {"scope", "analysis", "reply", "task", "source"})
        self.assertEqual(p["payload"]["reply"]["to"], [AHMET])
        self.assertEqual(p["payload"]["reply"]["references"], self.evidence["references"])
        self.assertEqual(p["payload"]["task"]["todo_parent_id"], 10384126975)
        self.assertEqual(p["payload"]["task"]["todo_parent_type"], "Todoset")
        self.assertIsNone(p["payload"]["task"]["due_at"])
        self.assertIsNone(p["approval"])
        self.assertEqual(self.store.public_state()["actions"], [])
        blob = json.dumps(self.backend.data)
        self.assertNotIn("FAKE-KEY", blob)
        self.assertNotIn(self.text, blob)
        self.assertNotIn("reply_draft", json.dumps(result))
        self.assertEqual(result["response_id"], "resp_test")
        record = self.store.model_receipt(result["budget_reservation_key"])
        self.assertEqual(record["receipt"]["usage"], self.result["usage"])

    def test_repeat_same_source_does_not_call_model_again(self):
        self.open(); self.run_worker()
        self.evidence["captured_at"] = (self.now - timedelta(seconds=2)).isoformat()
        result = self.run_worker()
        self.assertEqual(result["status"], "already_recorded")
        self.assertEqual(self.calls["model"], 1)
        self.assertEqual(self.calls["basecamp"], 1)

    def test_wrong_actual_recipient_blocks_all_following_calls(self):
        self.open(); self.evidence["hamle_direct_recipient"] = False
        self.assertEqual(self.run_worker()["reason"], "capture_scope_invalid")
        self.assertEqual(self.calls["model"], 0)
        self.assertEqual(self.calls["basecamp"], 0)

    def test_changed_snapshot_for_same_immutable_message_cannot_create_second_job(self):
        self.open(); self.run_worker()
        self.evidence["snapshot_fingerprint"] = "b" * 64
        self.assertEqual(self.run_worker()["reason"], "existing_request_source_changed")
        self.assertEqual(self.calls["model"], 1)
        self.assertEqual(len(self.store.public_state()["proposals"]), 1)

    def test_missing_rfc_message_id_blocks_paid_call(self):
        self.open(); del self.evidence["in_reply_to"]
        self.assertEqual(self.run_worker()["reason"], "rfc_message_id_required")
        self.assertEqual(self.calls["model"], 0)

    def test_stale_capture_and_injected_headers_blocked(self):
        self.open(); self.evidence["captured_at"] = (self.now - timedelta(seconds=301)).isoformat()
        self.assertEqual(self.run_worker()["reason"], "capture_stale")
        self.evidence["captured_at"] = self.now.isoformat()
        self.evidence["references"] = "<x@y>\r\nBcc: someone@example.test"
        self.assertEqual(self.run_worker()["reason"], "rfc_references_invalid")
        self.assertEqual(self.calls["model"], 0)

    def test_wrong_basecamp_project_blocks_model(self):
        self.open(); self.target["project_id"] = 999
        self.assertEqual(self.run_worker()["reason"], "basecamp_scope_invalid")
        self.assertEqual(self.calls["key"], 0)

    def test_stop_during_capture_blocks_model_and_secret_read(self):
        self.open()
        def capture():
            self.store.takeover("subject")
            return self.capture()
        self.assertEqual(self.run_worker(capture=capture)["reason"], "control_stopped_or_changed")
        self.assertEqual(self.calls["model"], 0)
        self.assertEqual(self.calls["key"], 0)

    def test_stop_during_model_does_not_publish_stale_proposal(self):
        self.open()
        def model(key, request):
            result = self.model(key, request)
            self.store.takeover("subject")
            return result
        result = self.run_worker(model_call=model)
        self.assertEqual(result["reason"], "control_stopped_or_changed")
        self.assertTrue(result["model_attempt_started"])
        self.assertEqual(self.store.public_state()["proposals"], [])
        self.assertEqual(self.store.budget_status()["held_microdollars"], 10_000_000)
        self.assertEqual(self.store.model_receipt(result["budget_reservation_key"])["receipt"]["response_id"], "resp_test")

    def test_only_known_adapter_error_codes_are_returned(self):
        from cloud_providers import ProviderError
        from model import ModelError
        self.open()
        def known():
            raise ProviderError("internal_request_not_found")
        self.assertEqual(self.run_worker(capture=known)["reason"], "internal_request_not_found")
        def unexpected():
            raise ProviderError("secret-mail-body-token")
        self.assertEqual(self.run_worker(capture=unexpected)["reason"], "provider_or_model_failed_no_retry")
        def quota(*args):
            raise ModelError("quota_or_rate_limit")
        self.assertEqual(self.run_worker(model_call=quota)["reason"], "quota_or_rate_limit")

    def test_model_timeout_retains_attempt_and_never_auto_repeats(self):
        self.open()
        def fail(key, request):
            self.calls["model"] += 1
            raise TimeoutError("secret-and-source-must-not-leak")
        first = self.run_worker(model_call=fail)
        self.assertNotIn("secret-and-source", json.dumps(first))
        second = self.run_worker(model_call=fail)
        self.assertEqual(second["reason"], "budget_call_not_replayable")
        self.assertEqual(self.calls["model"], 1)

    def test_concurrent_workers_get_one_model_chance(self):
        self.open(); barrier = threading.Barrier(2)
        def key():
            barrier.wait(timeout=2)
            return self.key()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.run_worker(load_key=key), range(2)))
        self.assertEqual(self.calls["model"], 1)
        self.assertEqual(sorted(r["status"] for r in results), ["held", "review_required"])

    def test_deadline_stops_before_source_read(self):
        self.open(); self.now = datetime.fromisoformat("2026-10-13T00:00:00+03:00")
        self.assertEqual(self.run_worker()["reason"], "authorization_expired")
        self.assertEqual(set(self.calls.values()), {0})

    def test_result_cannot_invent_confirmed_date_or_switch_model(self):
        self.open(); self.result["model"] = "another-model"
        self.assertEqual(self.run_worker()["reason"], "model_receipt_invalid")
        self.assertEqual(self.store.public_state()["proposals"], [])

    def test_cli_dispatch_requires_exact_id_before_any_cloud_construction(self):
        for args in (["--mode", "dispatch"], ["--mode", "dispatch", "--proposal-id", "all"],
                     ["--mode", "review", "--proposal-id", "p-" + "a" * 64]):
            with patch("cloud_store.FirestoreBackend", side_effect=AssertionError("network forbidden")), contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args), 2)
                self.assertEqual(json.loads(out.getvalue())["reason"], "explicit_proposal_required")

    def test_cli_explicit_dispatch_routes_to_injected_writers_only(self):
        backend = Mock(); secrets = Mock(); writers = Mock()
        fake_module = types.ModuleType("business_writers")
        fake_module.BusinessWriters = Mock(return_value=writers)
        env = {"HAMLE_ALLOW_FIRESTORE_OPERATIONS": "yes", "HAMLE_PROJECT_ID": "gamechangers-hamle",
               "HAMLE_FIRESTORE_DATABASE": "hamle-operations"}
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", env))
            stack.enter_context(patch.dict("sys.modules", {"business_writers": fake_module}))
            stack.enter_context(patch("cloud_store.FirestoreBackend", return_value=backend))
            stack.enter_context(patch("cloud_store.OperationsStore", return_value=self.store))
            stack.enter_context(patch("cloud_providers.GoogleSecretManager", return_value=secrets))
            stack.enter_context(patch("cloud_providers.GmailSecretStore", return_value=object()))
            route = stack.enter_context(patch("dispatch.dispatch_approved", return_value={"status": "internal_actions_readback_verified", "new_write_count": 0}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(main(["--mode", "dispatch", "--proposal-id", "p-" + "a" * 64]), 0)
        self.assertEqual(route.call_args.args, (self.store, "p-" + "a" * 64))
        self.assertIs(route.call_args.kwargs["create_task"], writers.create_task)
        self.assertIs(route.call_args.kwargs["send_reply"], writers.send_reply)
        fake_module.BusinessWriters.assert_called_once_with(secrets, claim_refresh=self.store.claim_credential_refresh)
        backend.close.assert_called_once(); secrets.close.assert_called_once()

    def test_cli_verify_preserves_static_diagnostics_and_hides_unknown_text(self):
        from cloud_providers import ProviderError
        env = {"HAMLE_ALLOW_FIRESTORE_OPERATIONS": "yes", "HAMLE_PROJECT_ID": "gamechangers-hamle",
               "HAMLE_FIRESTORE_DATABASE": "hamle-operations"}
        for error, reason in ((ProviderError("auth_transport_scope_or_retry_blocked"), "auth_transport_scope_or_retry_blocked"),
                              (ProviderError("secret-token-mail-body"), "worker_bootstrap_failed"),
                              (RuntimeError("secret-token-mail-body"), "worker_bootstrap_failed")):
            backend = Mock(); secrets = Mock()
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.dict("os.environ", env))
                stack.enter_context(patch("cloud_store.FirestoreBackend", return_value=backend))
                stack.enter_context(patch("cloud_store.OperationsStore", return_value=self.store))
                stack.enter_context(patch("cloud_providers.GoogleSecretManager", return_value=secrets))
                stack.enter_context(patch("cloud_providers.GmailSecretStore", return_value=object()))
                stack.enter_context(patch("cloud_providers.verify_gmail_identity", side_effect=error))
                stack.enter_context(patch("worker._stamp", return_value=self.now))
                out = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertEqual(main(["--mode", "verify"]), 2)
            self.assertEqual(json.loads(out.getvalue())["reason"], reason)
            self.assertNotIn("secret-token-mail-body", out.getvalue())
            backend.close.assert_called_once(); secrets.close.assert_called_once()

    def scheduled(self, **kw):
        args = dict(capture=self.capture, check_basecamp=self.check, load_key=self.key,
                    model_call=self.model, clock=lambda: self.now)
        args.update(kw)
        return run_scheduled_review(self.store, **args)

    def monday(self):
        self.now = datetime.fromisoformat("2026-10-12T08:30:02+03:00")
        self.evidence["captured_at"] = self.now.isoformat()

    def receipt(self):
        result = self.run_worker()
        pid = result["proposal_id"]
        self.store.approve(pid, result["proposal_hash"], result["control_generation"], "subject")
        p = self.store.public_state()["proposals"][0]
        lease = self.store.claim_action(action_id(pid, "gmail_reply"), pid, "gmail_reply",
                                       p["proposal_hash"], "test-worker", p["generation"])
        self.store.begin_dispatch(lease, p["source_fingerprint"])
        self.store.record_receipt(lease, "abc999")
        self.store.confirm_readback(lease["action_id"], "abc999")
        return pid

    @staticmethod
    def self_latest_capture():
        from cloud_providers import ProviderError
        raise ProviderError("latest_internal_request_not_eligible")

    @staticmethod
    def receipt_proof(thread_id, message_id):
        return {"status": "latest_sent_receipt_verified", "thread_id": thread_id,
                "message_id": message_id, "email": HAMLE, "mailbox_writes": 0, "action_eligible": False}

    def test_scheduled_duplicate_slot_and_next_slot_never_repeat_model(self):
        self.monday(); self.open()
        first = self.scheduled()
        self.assertEqual(first["status"], "review_required")
        before = copy.deepcopy(self.calls)
        self.assertEqual(self.scheduled()["status"], "slot_already_claimed")
        self.assertEqual(self.calls, before)
        self.now += timedelta(minutes=30); self.evidence["captured_at"] = self.now.isoformat()
        second = self.scheduled()
        self.assertEqual(second["status"], "already_recorded")
        self.assertNotEqual(first["slot_key"], second["slot_key"])
        self.assertEqual(self.calls["model"], 1)
        self.assertEqual(self.store.public_state()["actions"], [])

    def test_scheduled_weekend_late_expired_and_stop_call_no_providers(self):
        self.open()
        self.assertEqual(self.scheduled()["reason"], "outside_scheduled_window")
        self.monday(); self.now += timedelta(minutes=5)
        self.assertEqual(self.scheduled()["reason"], "outside_scheduled_window")
        self.now = datetime.fromisoformat("2026-10-13T08:30:00+03:00")
        self.assertEqual(self.scheduled()["reason"], "authorization_expired")
        self.monday(); self.store.stop("subject")
        self.assertEqual(self.scheduled()["status"], "stopped")
        self.assertEqual(set(self.calls.values()), {0})

    def test_scheduled_stop_resume_after_claim_does_not_use_new_generation(self):
        self.monday(); self.open()
        claim = self.store.claim_scheduled_slot
        def interrupted(g, **kwargs):
            result = claim(g, **kwargs)
            self.store.stop("subject"); self.store.resume_review_only("subject")
            return result
        with patch.object(self.store, "claim_scheduled_slot", side_effect=interrupted):
            self.assertEqual(self.scheduled()["reason"], "control_stopped_or_changed")
        self.assertEqual(set(self.calls.values()), {0})
        self.assertEqual(self.scheduled()["status"], "slot_already_claimed")

    def test_scheduled_receipted_latest_self_reply_is_ordinary_no_work(self):
        self.monday(); self.open(); pid = self.receipt()
        before = copy.deepcopy(self.calls)
        proof = Mock(side_effect=self.receipt_proof)
        result = self.scheduled(capture=self.self_latest_capture, verify_latest_sent_receipt=proof)
        self.assertEqual(result["status"], "no_new_work")
        self.assertEqual(result["proposal_id"], pid)
        proof.assert_called_once_with("ab12", "abc999")
        self.assertEqual(self.calls, before)
        self.assertEqual(self.backend.data[result["slot_key"]]["outcome"]["status"], "no_new_work")

    def test_scheduled_unreceipted_or_different_self_mail_is_not_no_work(self):
        self.monday(); self.open()
        proof = Mock(side_effect=self.receipt_proof)
        result = self.scheduled(capture=self.self_latest_capture, verify_latest_sent_receipt=proof)
        self.assertEqual(result["status"], "held"); proof.assert_not_called()
        self.now += timedelta(minutes=30); self.evidence["captured_at"] = self.now.isoformat()
        self.receipt()
        def wrong(tid, mid):
            return self.receipt_proof(tid, "def000")
        self.assertEqual(self.scheduled(capture=self.self_latest_capture,
                         verify_latest_sent_receipt=wrong)["reason"], "latest_receipt_proof_invalid")

    def test_scheduled_missing_adapter_or_latest_changed_does_not_claim_no_work(self):
        self.monday(); self.open(); self.receipt()
        self.assertEqual(self.scheduled(capture=self.self_latest_capture)["status"], "held")
        self.now += timedelta(minutes=30)
        self.assertEqual(self.scheduled(capture=self.self_latest_capture,
                         verify_latest_sent_receipt=lambda *_: {"status": "not_latest_sent_receipt"})["status"], "held")

    def test_scheduled_receipt_check_stop_rejects_even_verified_proof(self):
        self.monday(); self.open(); self.receipt()
        def interrupted(tid, mid):
            self.store.takeover("subject")
            return self.receipt_proof(tid, mid)
        result = self.scheduled(capture=self.self_latest_capture, verify_latest_sent_receipt=interrupted)
        self.assertEqual(result["reason"], "control_stopped_or_changed")
        self.assertEqual(self.calls["model"], 1)


class FollowupWorkerTests(unittest.TestCase):
    capture = WorkerTests.capture
    check = WorkerTests.check
    key = WorkerTests.key
    receipt_proof = staticmethod(WorkerTests.receipt_proof)
    self_latest_capture = staticmethod(WorkerTests.self_latest_capture)

    def setUp(self):
        WorkerTests.setUp(self)
        self.store.resume_review_only("subject")
        self.work = seed_bound_work(self.store, self.now)
        self.seed = copy.deepcopy(self.store.public_state()["proposals"][0])
        self.evidence.update(message_id="ab15", snapshot_fingerprint="d" * 64,
                             root_message_id="ab13", root_rfc_message_id="<root@example.test>",
                             source_subject="Re: " + SUBJECT, is_continuation=True,
                             in_reply_to="<followup@example.test>",
                             references="<root@example.test> <followup@example.test>",
                             latest_to=[HAMLE], latest_cc=["cansu@gamechangers.com.tr"])
        self.observation = task_observation(self.work, self.now)
        self.status_reads = 0

    def follow_capture(self, work):
        self.assertEqual(work["thread_id"], "ab12")
        return self.capture()

    def read_status(self, work):
        self.status_reads += 1
        self.assertEqual(work["task_id"], 10392247471)
        return copy.deepcopy(self.observation)

    def follow_model(self, key, request):
        self.calls["model"] += 1
        context = json.loads(request["request_text"])
        self.assertEqual(context["son_gelen_mesaj"], self.text)
        self.assertEqual(context["onceki_onayli_yanit"], self.seed["payload"]["reply"]["body"])
        self.assertEqual(context["mevcut_gorev"]["task_id"], 10392247471)
        self.assertEqual(context["acik_insan_degerlendirmesi"]["status"], "open")
        self.assertEqual(request["confirmed_facts"], ["Yalnız Gamechangers iç denemesi; müşteri gönderimi yetkisi yok."])
        return copy.deepcopy(self.result)

    def run_worker(self, **changes):
        args = dict(capture=lambda: self.fail("Bound thread must never use initial capture"),
                    capture_continuation=self.follow_capture, check_basecamp=self.check,
                    read_task_status=self.read_status, load_key=self.key, model_call=self.follow_model,
                    clock=lambda: self.now)
        args.update(changes)
        return run_review(self.store, **args)

    def test_followup_uses_existing_task_with_source_context_and_keeps_latest_cc(self):
        result = self.run_worker()
        self.assertEqual(result["status"], "review_required", result)
        state = self.store.public_state()
        self.assertEqual(state["proposals"][0], self.seed)
        payload = state["proposals"][1]["payload"]
        self.assertEqual(set(payload), {"scope", "analysis", "work", "comment", "reply", "source"})
        self.assertEqual(payload["comment"]["task_id"], 10392247471)
        self.assertEqual(payload["reply"]["cc"], ["cansu@gamechangers.com.tr"])
        self.assertEqual(payload["work"]["escalation_status"], "open")
        self.assertEqual(self.calls["model"], 1)
        self.assertEqual(self.store.get_work(self.work["work_id"])["latest_observation"], self.observation)
        self.assertNotIn(self.text, json.dumps(self.backend.data))

    def test_repeat_same_message_and_later_pending_message_do_not_buy_more_model_calls(self):
        self.assertEqual(self.run_worker()["status"], "review_required")
        self.assertEqual(self.run_worker()["status"], "already_recorded")
        self.evidence.update(message_id="ab16", snapshot_fingerprint="e" * 64)
        self.assertEqual(self.run_worker()["reason"], "previous_followup_unresolved")
        self.assertEqual(self.calls["model"], 1)

    def test_unknown_model_result_permanently_holds_event_and_budget(self):
        def failed(*_):
            self.calls["model"] += 1
            raise TimeoutError("Never log provider text or secret")
        result = self.run_worker(model_call=failed)
        self.assertEqual(result["status"], "held")
        self.assertEqual(self.run_worker()["reason"], "followup_already_reserved_no_retry")
        self.assertEqual(self.calls["model"], 1)
        event = self.backend.data[self.store.get_work(self.work["work_id"])["pending_event_id"]]
        self.assertEqual(event["state"], "held")
        self.assertEqual(self.store.budget_status()["held_microdollars"], 10_000_000)

    def test_completed_or_changed_target_blocks_model(self):
        self.observation["completed"] = True
        self.assertEqual(self.run_worker()["reason"], "work_not_active")
        self.assertEqual(self.calls["key"], 0)
        self.assertEqual(self.calls["model"], 0)

    def test_unknown_cc_wrong_root_and_missing_actual_hamle_recipient_hold_before_cost(self):
        self.evidence["latest_cc"] = ["unknown@example.test"]
        self.assertEqual(self.run_worker()["reason"], "reply_recipient_scope_unverified")
        self.evidence["latest_cc"] = []; self.evidence["root_message_id"] = "deadbeef"
        self.assertEqual(self.run_worker()["reason"], "continuation_root_invalid")
        self.evidence["root_message_id"] = "ab13"; self.evidence["hamle_direct_recipient"] = False
        self.assertEqual(self.run_worker()["reason"], "capture_scope_invalid")
        self.assertEqual(self.calls["model"], 0)

    def test_oversize_context_is_held_without_silent_truncation_or_model_call(self):
        self.observation["description"] = "kaynak " * 2000
        self.assertEqual(self.run_worker()["reason"], "continuation_context_too_large")
        self.assertEqual(self.calls["model"], 0)
        self.assertIsNone(self.store.get_work(self.work["work_id"])["pending_event_id"])

    def test_normal_review_own_verified_latest_reply_is_no_new_work_and_still_tracks_task(self):
        result = self.run_worker(capture_continuation=lambda _: self.self_latest_capture(),
                                 verify_latest_sent_receipt=self.receipt_proof)
        self.assertEqual(result["status"], "no_new_work", result)
        self.assertEqual(self.status_reads, 1)
        self.assertEqual(self.calls["model"], 0)
        self.assertEqual(self.store.get_work(self.work["work_id"])["latest_observation"], self.observation)

    def test_track_has_no_model_path_and_stop_during_read_persists_only_observation(self):
        result = run_track(self.store, read_task_status=self.read_status,
                           verify_latest_sent_receipt=self.receipt_proof, clock=lambda: self.now)
        self.assertEqual(result["status"], "work_status_read_verified", result)
        self.assertTrue(result["latest_reply_is_latest"])
        def interrupted(work):
            value = self.read_status(work)
            self.store.takeover("subject")
            return value
        result = run_track(self.store, read_task_status=interrupted, clock=lambda: self.now)
        self.assertEqual(result["reason"], "control_stopped_or_changed")
        self.assertEqual(self.calls["model"], 0)
        self.assertEqual(self.store.get_work(self.work["work_id"])["latest_observation"], self.observation)

    def test_stop_during_followup_model_records_usage_but_never_publishes(self):
        def interrupted(key, request):
            result = self.follow_model(key, request)
            self.store.takeover("subject")
            return result
        result = self.run_worker(model_call=interrupted)
        self.assertEqual(result["reason"], "control_stopped_or_changed")
        self.assertEqual(len(self.store.public_state()["proposals"]), 1)
        self.assertEqual(self.store.model_receipt(result["budget_reservation_key"])["receipt"]["response_id"], "resp_test")

    def test_scheduled_quarter_hours_only_pay_once_for_same_new_message(self):
        self.now = datetime.fromisoformat("2026-10-12T08:30:00+03:00")
        for _ in range(2):
            self.evidence["captured_at"] = self.now.isoformat(); self.observation["observed_at"] = self.now.isoformat()
            result = run_scheduled_review(self.store, capture=self.capture, capture_continuation=self.follow_capture,
                     check_basecamp=self.check, read_task_status=self.read_status, load_key=self.key,
                     model_call=self.follow_model, clock=lambda: self.now)
            self.assertIn(result["status"], ("review_required", "already_recorded"), result)
            self.now += timedelta(minutes=15)
        self.assertEqual(self.calls["model"], 1)

    def fail_incomplete(self):
        from model import ModelError
        def incomplete(*_):
            self.calls["model"] += 1
            raise ModelError("response_incomplete", response={"id": "resp_incomplete1", "model": "gpt-6-astra",
                "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {"input_tokens": 1000, "output_tokens": 8192, "total_tokens": 9192},
                "output": [{"secret_partial_text": "NEVER_PERSIST_PARTIAL"}]})
        return self.run_worker(model_call=incomplete)

    def recover(self, **changes):
        args = dict(work_id=self.work["work_id"], failure_evidence_sha256="f" * 64,
                    failed_task_fingerprint=self.observation["task_fingerprint"],
                    capture=self.capture, capture_continuation=self.follow_capture,
                    check_basecamp=self.check, read_task_status=self.read_status,
                    load_key=self.key, model_call=self.follow_model, clock=lambda: self.now)
        args.update(changes)
        return run_recover_followup(self.store, **args)

    def test_explicit_recovery_preserves_failure_and_uses_same_canonical_proposal_once(self):
        failed = self.fail_incomplete()
        failure = self.store.model_failure(failed["budget_reservation_key"])
        self.assertEqual(failure["receipt"]["response_id"], "resp_incomplete1")
        self.assertNotIn("NEVER_PERSIST_PARTIAL", json.dumps(self.backend.data))
        event = copy.deepcopy(self.backend.data[self.store.get_work(self.work["work_id"])["pending_event_id"]])
        recovered = self.recover()
        self.assertEqual(recovered["status"], "review_required", recovered)
        self.assertEqual(recovered["proposal_id"], event["proposal_id"])
        self.assertEqual(recovered["budget_reservation_key"], failed["budget_reservation_key"] + "-attempt2")
        self.assertEqual(recovered["budget_hold_microdollars"], 2_000_000)
        self.assertEqual(self.store.model_failure(failed["budget_reservation_key"]), failure)
        self.assertEqual(self.calls["model"], 2)
        self.assertEqual(self.recover()["reason"], "recovery_existing_proposal_forbidden")
        p = self.store.public_state()["proposals"][-1]
        self.assertIsNone(p["approval"])
        audit = self.backend.data["recovery-" + event["event_id"] + "-2"]["authorization"]
        self.assertFalse(audit["content_approval"])
        self.assertEqual(audit["actor"], "codex-authorized-maintenance")
        self.assertEqual(p["payload"]["comment"]["task_id"], 10392247471)

    def test_historical_failure_gap_stays_unknown_and_original_one_dollar_is_retained(self):
        with patch("worker.MODEL_RESERVATION", 1_000_000):
            failed = self.fail_incomplete()
        key = failed["budget_reservation_key"]
        # Fixture represents v6, which had no failure-receipt collection field.
        del self.backend.data["model-failure-" + hashlib.sha256(key.encode()).hexdigest()]
        eid = self.store.get_work(self.work["work_id"])["pending_event_id"]
        self.backend.data[eid].pop("task_fingerprint")
        result = self.recover()
        self.assertEqual(result["status"], "review_required", result)
        old = self.store.model_failure(key)
        self.assertTrue(old["legacy_observability_gap"])
        self.assertIsNone(old["receipt"])
        self.assertEqual(old["evidence_sha256"], "f" * 64)
        reservations = self.backend.data["budget"]["reservations"]
        self.assertEqual(next(r for r in reservations if r["reservation_key"] == key)["reserved_microdollars"], 1_000_000)
        self.assertEqual(next(r for r in reservations if r["reservation_key"] == key + "-attempt2")["reserved_microdollars"], 2_000_000)

    def test_second_incomplete_or_transport_failure_has_no_third_chance(self):
        from model import ModelError
        first = self.fail_incomplete()
        def fail(*_):
            self.calls["model"] += 1
            raise ModelError("response_incomplete", response={"id": "resp_incomplete2", "model": "gpt-6-astra", "status": "incomplete"})
        second = self.recover(model_call=fail)
        self.assertEqual(second["reason"], "response_incomplete")
        self.assertEqual(second["failure_receipt"]["response_id"], "resp_incomplete2")
        self.assertEqual(self.recover()["reason"], "recovery_already_reserved_no_retry")
        self.assertEqual(self.run_worker()["reason"], "followup_already_reserved_no_retry")
        self.assertEqual(self.calls["model"], 2)
        self.assertIsNotNone(self.store.model_failure(first["budget_reservation_key"]))
        status = self.store.public_state()["works"][0]["followup_status"]
        self.assertEqual(status, {"state": "held", "reason": "response_incomplete", "attempt": 2})
        self.assertNotIn("followup_status", self.store.get_work(self.work["work_id"]))

    def test_unknown_original_outcome_cannot_be_recovered_as_terminal_failure(self):
        def fail(*_):
            self.calls["model"] += 1
            raise TimeoutError("unknown")
        self.run_worker(model_call=fail)
        self.assertEqual(self.recover()["reason"], "recovery_not_eligible")
        self.assertEqual(self.calls["model"], 1)

    def test_recovery_requires_same_source_task_generation_and_no_actions(self):
        self.fail_incomplete()
        original = self.evidence["snapshot_fingerprint"]
        self.evidence["snapshot_fingerprint"] = "e" * 64
        self.assertEqual(self.recover()["reason"], "recovery_not_eligible")
        self.evidence["snapshot_fingerprint"] = original
        self.observation["task_fingerprint"] = "d" * 64
        self.assertEqual(self.recover(failed_task_fingerprint="c" * 64)["reason"], "recovery_task_changed")
        self.observation["task_fingerprint"] = "c" * 64
        eid = self.store.get_work(self.work["work_id"])["pending_event_id"]
        pid = self.backend.data[eid]["proposal_id"]
        self.backend.data["action-" + action_id(pid, "gmail_reply")] = {"fixture": "existing action"}
        self.assertEqual(self.recover()["reason"], "recovery_not_eligible")
        del self.backend.data["action-" + action_id(pid, "gmail_reply")]
        self.store.takeover("subject"); self.store.resume_review_only("subject")
        self.assertEqual(self.recover()["reason"], "recovery_not_eligible")
        self.assertEqual(self.calls["model"], 1)

    def test_normal_and_scheduled_review_cannot_consume_explicit_recovery(self):
        self.fail_incomplete()
        self.assertEqual(self.run_worker()["reason"], "followup_already_reserved_no_retry")
        self.now = datetime.fromisoformat("2026-10-12T08:30:00+03:00")
        self.evidence["captured_at"] = self.now.isoformat(); self.observation["observed_at"] = self.now.isoformat()
        result = run_scheduled_review(self.store, capture=self.capture, capture_continuation=self.follow_capture,
                 check_basecamp=self.check, read_task_status=self.read_status, load_key=self.key,
                 model_call=self.follow_model, clock=lambda: self.now)
        self.assertEqual(result["reason"], "followup_already_reserved_no_retry")
        self.assertFalse(any(k.startswith("recovery-") for k in self.backend.data))
        self.assertEqual(self.calls["model"], 1)

    def test_recovery_cli_rejects_missing_evidence_before_constructing_cloud_clients(self):
        with patch("cloud_store.FirestoreBackend", side_effect=AssertionError("network forbidden")), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--mode", "recover_followup", "--work-id", self.work["work_id"]]), 2)
        self.assertEqual(json.loads(output.getvalue())["reason"], "explicit_recovery_evidence_required")

    def test_followup_error_view_is_bounded_and_contains_no_failure_receipt_or_secret(self):
        self.assertIsNone(self.store.public_state()["works"][0]["followup_status"])
        self.fail_incomplete()
        self.assertEqual(self.store.public_state()["works"][0]["followup_status"],
                         {"state": "held", "reason": "response_incomplete", "attempt": 1})
        self.assertEqual(self.recover()["status"], "review_required")
        self.assertEqual(self.store.public_state()["works"][0]["followup_status"],
                         {"state": "proposed", "reason": None, "attempt": 2})
        self.store.takeover("subject")
        self.assertEqual(self.store.public_state()["works"][0]["followup_status"]["reason"], "control_stopped_or_changed")


if __name__ == "__main__":
    unittest.main()
