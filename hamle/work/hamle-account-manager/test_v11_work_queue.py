"""Offline acceptance for the isolated v1.1 multi-work queue. No provider calls."""
import concurrent.futures
import unittest

from v11_work_queue import (FirestoreQueueBackend, InMemoryBackend, QueueError,
                            QueueStore, WRITE_BINDING_FIELDS, digest)


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_791_635_000
        self.backend = InMemoryBackend()
        self.q = QueueStore(self.backend, clock=lambda: self.now)
        self.q.initialize()
        self.g = self.q.set_control("queue_open", actor="test-human", expected_generation=0)["generation"]
        self.a = self.bind("thread-a")
        self.b = self.bind("thread-b", brand="rabam", project=20)

    def bind(self, thread, brand="wat_mobilite", project=10):
        return self.q.bind_work(brand_id=brand, thread_id=thread, account_id=1,
                                project_id=project, list_id=30, service_id="copy",
                                expected_generation=self.g)["work_id"]

    def mail(self, work, msg, order):
        return self.q.enqueue_message(work, message_id=msg, fingerprint=digest(msg),
                                      source_order=order, expected_generation=self.g)

    def claim(self, work, worker="worker-a"):
        return self.q.claim_next(work, worker_id=worker, expected_generation=self.g)

    def start(self, claim, key="reply-v1"):
        work = self.q.get_work(claim["work_id"])
        prepared = {k: work[k] for k in WRITE_BINDING_FIELDS}
        prepared["queue_event_fingerprint"] = claim["event"]["fingerprint"]
        return self.q.begin_write(claim, action_kind="acknowledge", payload_sha256=digest(key),
                                  action_key=key, prepared_binding=prepared)

    def confirm(self, action, receipt_id="remote-1"):
        return self.q.confirm_write(action_id=action["id"], claim_token=action["claim_token"],
                                    payload_sha256=action["payload_sha256"],
                                    receipt={"provider": "gmail", "remote_id": receipt_id,
                                             "readback_sha256": action["payload_sha256"]})

    def assertCode(self, code, fn):
        with self.assertRaises(QueueError) as e:
            fn()
        self.assertEqual(e.exception.code, code)

    def test_two_threads_three_events_order_and_independence(self):
        a1 = self.mail(self.a, "a1", 1)
        b1 = self.mail(self.b, "b1", 1)
        a2 = self.mail(self.a, "a2", 2)
        ac = self.claim(self.a)
        bc = self.claim(self.b)
        self.assertEqual(ac["event_id"], a1["event"]["id"])
        self.assertEqual(bc["event_id"], b1["event"]["id"])
        self.assertCode("work_lease_busy", lambda: self.claim(self.a, "worker-b"))
        self.q.complete_event(bc, outcome="no_action", evidence_ref="offline-qa-b1")
        self.assertIsNone(self.claim(self.b))
        action = self.start(ac)
        self.confirm(action)
        self.q.complete_event(ac, outcome="processed", evidence_ref="readback-a1")
        self.assertEqual(self.claim(self.a)["event_id"], a2["event"]["id"])

    def test_persistent_across_store_instances(self):
        old = self.mail(self.a, "a1", 1)
        q2 = QueueStore(self.backend, clock=lambda: self.now)
        dup = q2.enqueue_message(self.a, message_id="a1", fingerprint=digest("a1"), source_order=1,
                                 expected_generation=self.g)
        self.assertTrue(dup["duplicate"])
        self.assertEqual(old["event"], dup["event"])
        self.assertEqual(q2.claim_next(self.a, worker_id="restart", expected_generation=self.g)["event_id"],
                         old["event"]["id"])

    def test_same_message_wrong_work_is_held(self):
        self.mail(self.a, "a1", 1)
        self.assertCode("source_identity_conflict", lambda: self.mail(self.b, "a1", 1))

    def test_same_message_changed_fingerprint_is_held(self):
        self.mail(self.a, "a1", 1)
        self.assertCode("source_identity_conflict", lambda: self.q.enqueue_message(
            self.a, message_id="a1", fingerprint=digest("changed"), source_order=1, expected_generation=self.g))

    def test_thread_cannot_be_rebound_to_different_brand_or_project(self):
        self.assertCode("thread_already_bound_to_other_scope", lambda: self.bind("thread-a", brand="rabam"))
        self.assertCode("thread_already_bound_to_other_scope", lambda: self.bind("thread-a", project=11))
        self.assertCode("work_binding_conflict", lambda: self.q.bind_work(brand_id="wat_mobilite",
            thread_id="thread-a", account_id=1, project_id=10, list_id=31, service_id="copy", expected_generation=self.g))

    def test_wrong_project_task_and_duplicate_task_bind_are_held(self):
        self.assertCode("task_project_mismatch", lambda: self.q.bind_task(self.a, task_id=99,
            account_id=1, project_id=20, list_id=30, expected_generation=self.g))
        self.q.bind_task(self.a, task_id=99, account_id=1, project_id=10, list_id=30, expected_generation=self.g)
        c = self.bind("thread-c")
        self.assertCode("task_already_bound", lambda: self.q.bind_task(c, task_id=99,
            account_id=1, project_id=10, list_id=30, expected_generation=self.g))
        self.assertCode("task_binding_conflict", lambda: self.q.bind_task(self.a, task_id=98,
            account_id=1, project_id=10, list_id=30, expected_generation=self.g))

    def test_claim_crash_without_write_can_be_reclaimed_but_old_owner_is_fenced(self):
        self.mail(self.a, "a1", 1)
        old = self.claim(self.a)
        self.now += 61
        new = self.claim(self.a, "restart")
        self.assertNotEqual(old["token"], new["token"])
        self.assertCode("stale_claim", lambda: self.start(old))
        self.start(new)

    def test_started_write_crash_never_becomes_retryable_after_expiry(self):
        self.mail(self.a, "a1", 1)
        claim = self.claim(self.a)
        action = self.start(claim)
        self.now += 1000
        self.assertCode("write_outcome_unresolved", lambda: self.claim(self.a, "restart"))
        self.q.hold_unknown(action["id"])
        self.assertEqual(self.q.get_work(self.a)["queue_state"], "unknown")
        self.assertCode("write_outcome_unresolved", lambda: self.claim(self.a))
        self.assertFalse(self.q.page(kind="action", work_id=self.a)["items"][0]["automatic_retry_allowed"])
        self.confirm(action)
        renewed = self.claim(self.a, "reconciler")
        self.assertCode("action_already_started", lambda: self.start(renewed))
        self.q.complete_event(renewed, outcome="processed", evidence_ref="exact-readback")

    def test_no_second_action_until_first_exact_readback(self):
        self.mail(self.a, "a1", 1)
        claim = self.claim(self.a)
        a = self.start(claim)
        self.assertCode("write_outcome_unresolved", lambda: self.start(claim, "another"))
        self.assertCode("write_outcome_unresolved", lambda: self.q.complete_event(claim,
            outcome="processed", evidence_ref="cannot-skip"))
        self.confirm(a)
        self.start(claim, "another")

    def test_work_takeover_fences_pending_action_but_other_work_runs(self):
        self.mail(self.a, "a1", 1); self.mail(self.b, "b1", 1)
        claim = self.claim(self.a)
        self.q.set_work_mode(self.a, mode="takeover", actor="ahmet", expected_work_generation=0)
        self.assertCode("work_taken_over", lambda: self.start(claim))
        self.assertCode("work_taken_over", lambda: self.claim(self.a))
        self.assertIsNotNone(self.claim(self.b))

    def test_inflight_receipt_after_takeover_is_saved_without_resuming(self):
        self.mail(self.a, "a1", 1)
        claim = self.claim(self.a); action = self.start(claim)
        self.q.set_work_mode(self.a, mode="takeover", actor="ahmet", expected_work_generation=0)
        self.assertCode("write_outcome_unresolved", lambda: self.q.set_work_mode(self.a,
            mode="active", actor="ahmet", expected_work_generation=1))
        self.confirm(action)
        self.assertEqual(self.q.get_work(self.a)["mode"], "takeover")
        self.assertCode("work_taken_over", lambda: self.claim(self.a))
        self.q.set_work_mode(self.a, mode="active", actor="ahmet", expected_work_generation=1)
        self.assertCode("stale_claim", lambda: self.start(claim, "new"))

    def test_global_stop_and_resume_fence_all_old_claims(self):
        self.mail(self.a, "a1", 1); self.mail(self.b, "b1", 1)
        ca = self.claim(self.a); cb = self.claim(self.b)
        stopped = self.q.set_control("stopped", actor="ahmet", expected_generation=self.g)
        self.assertCode("global_stop_or_stale_generation", lambda: self.start(ca))
        self.assertCode("global_stop_or_stale_generation", lambda: self.start(cb))
        self.g = self.q.set_control("queue_open", actor="ahmet", expected_generation=stopped["generation"])["generation"]
        self.assertCode("global_stop_or_stale_generation", lambda: self.start(ca))
        self.assertIsNotNone(self.claim(self.a, "new-worker"))

    def test_takeover_generation_prevents_stale_resume(self):
        self.q.set_work_mode(self.a, mode="takeover", actor="ahmet", expected_work_generation=0)
        self.assertCode("stale_work_generation", lambda: self.q.set_work_mode(self.a,
            mode="active", actor="stale-tab", expected_work_generation=0))

    def test_human_hold_blocks_later_events_of_only_this_work(self):
        self.mail(self.a, "a1", 1); self.mail(self.a, "a2", 2); self.mail(self.b, "b1", 1)
        claim = self.claim(self.a)
        self.q.complete_event(claim, outcome="held_for_human", evidence_ref="complaint-detected")
        self.assertCode("work_taken_over", lambda: self.claim(self.a))
        self.assertIsNotNone(self.claim(self.b))

    def test_order_guard_rejects_newest_first_provider_ingestion(self):
        self.mail(self.a, "a2", 2)
        self.assertCode("out_of_order_source", lambda: self.mail(self.a, "a1", 1))
        # Basecamp has its own source cursor, but joins the same ordered work.
        event = self.q.enqueue_event(self.a, event_id="bc:1:10:99:1", source_kind="basecamp",
            fingerprint=digest("comment"), source_order=1, expected_generation=self.g)
        self.assertEqual(event["event"]["sequence"], 2)

    def test_duplicate_after_completion_does_not_requeue(self):
        self.mail(self.a, "a1", 1)
        self.q.complete_event(self.claim(self.a), outcome="no_action", evidence_ref="own-mail")
        self.assertTrue(self.mail(self.a, "a1", 1)["duplicate"])
        self.assertIsNone(self.claim(self.a))

    def test_paginated_queue_exceeds_fifty_works_without_drops(self):
        expected = {self.a, self.b}
        for index in range(125):
            expected.add(self.bind("thread-" + str(index)))
        result, cursor = [], None
        while True:
            page = self.q.page(limit=17, cursor=cursor)
            self.assertLessEqual(len(page["items"]), 17)
            result.extend(r["work_id"] for r in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(set(result), expected)
        self.assertEqual(len(result), len(expected))
        self.assertCode("invalid_page_limit", lambda: self.q.page(limit=101))

    def test_pagination_is_discovery_not_authority(self):
        self.mail(self.a, "a1", 1)
        found = self.q.page(queue_state="ready")["items"][0]
        self.q.set_work_mode(self.a, mode="takeover", actor="ahmet", expected_work_generation=0)
        self.assertCode("work_taken_over", lambda: self.claim(found["work_id"]))

    def test_two_concurrent_workers_have_exactly_one_owner(self):
        self.mail(self.a, "a1", 1)
        def compete(worker):
            try:
                return self.claim(self.a, worker)
            except QueueError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, ["one", "two"]))
        self.assertEqual(sum(isinstance(v, dict) for v in results), 1)
        self.assertEqual(results.count("work_lease_busy"), 1)

    def test_exact_receipt_is_idempotent_conflicting_receipt_rejected(self):
        self.mail(self.a, "a1", 1); action = self.start(self.claim(self.a))
        self.assertEqual(self.confirm(action), self.confirm(action))
        self.assertCode("receipt_conflict", lambda: self.confirm(action, "different-id"))

    def test_receipt_must_match_submitted_payload_and_provider(self):
        self.mail(self.a, "a1", 1); action = self.start(self.claim(self.a))
        self.assertCode("readback_payload_mismatch", lambda: self.q.confirm_write(action_id=action["id"],
            claim_token=action["claim_token"], payload_sha256=action["payload_sha256"],
            receipt={"provider": "gmail", "remote_id": "x", "readback_sha256": digest("changed")}))
        self.assertCode("receipt_provider_mismatch", lambda: self.q.confirm_write(action_id=action["id"],
            claim_token=action["claim_token"], payload_sha256=action["payload_sha256"],
            receipt={"provider": "basecamp", "remote_id": "x", "readback_sha256": action["payload_sha256"]}))

    def test_transaction_failure_rolls_back_partial_writes(self):
        def fail(tx):
            tx.put("temporary", {"hello": "not-committed"})
            raise QueueError("aborted")
        self.assertCode("aborted", lambda: self.backend.transact(fail))
        self.assertNotIn("temporary", self.backend.records)

    def test_expired_and_future_claims_cannot_start(self):
        self.mail(self.a, "a1", 1); claim = self.claim(self.a)
        self.now += 60
        self.assertCode("lease_expired", lambda: self.start(claim))
        self.now -= 61
        self.assertCode("clock_before_control", lambda: self.start(claim))

    def test_backward_clock_blocks_open_queue_operations_but_stop_still_works(self):
        self.now -= 1
        self.assertCode("clock_before_control", lambda: self.mail(self.a, "a1", 1))
        self.assertCode("clock_before_control", lambda: self.bind("future"))
        self.assertEqual(self.q.set_control("stopped", actor="ahmet", expected_generation=self.g)["mode"], "stopped")

    def test_prepared_work_and_source_binding_prevents_adapter_crosswire(self):
        self.mail(self.a, "a1", 1); claim = self.claim(self.a)
        good = {k: self.q.get_work(self.a)[k] for k in WRITE_BINDING_FIELDS}
        good["queue_event_fingerprint"] = claim["event"]["fingerprint"]
        for field, wrong in (("brand_id", "rabam"), ("project_id", 999), ("work_id", self.b),
                             ("thread_id", "thread-b"), ("queue_event_fingerprint", digest("other"))):
            with self.subTest(field=field):
                prepared = {**good, field: wrong}
                self.assertCode("prepared_work_or_source_mismatch", lambda: self.q.begin_write(claim,
                    action_kind="acknowledge", action_key="reply", payload_sha256=digest("reply"),
                    prepared_binding=prepared))

    def test_cloud_constructor_needs_explicit_dedicated_opt_in(self):
        self.assertCode("cloud_opt_in_required", lambda: FirestoreQueueBackend(opt_in="yes", credentials=object()))
        self.assertCode("explicit_credentials_required", lambda: FirestoreQueueBackend(
            opt_in="v11-queue-explicit", credentials=None))
        self.assertCode("cloud_opt_in_required", lambda: FirestoreQueueBackend(
            opt_in="v11-queue-explicit", credentials=object(), project="other"))


if __name__ == "__main__":
    unittest.main()
