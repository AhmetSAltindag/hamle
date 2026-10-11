"""Crash/recovery acceptance with in-memory persistence; no provider or network."""
import copy
import unittest

from v11_poll_journal import PollJournal, checkpoint_digest as sha
from v11_work_queue import QueueStore, InMemoryBackend, QueueError


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_791_635_000
        self.backend = InMemoryBackend()
        self.q = QueueStore(self.backend, clock=lambda: self.now)
        self.q.initialize()
        self.g = self.q.set_control("queue_open", actor="offline-human", expected_generation=0)["generation"]
        work = self.q.bind_work(brand_id="gamechangers", thread_id="test-thread", account_id=1,
            project_id=2, list_id=3, service_id="copy", expected_generation=self.g)
        self.work_id = work["work_id"]
        self.q.bind_task(self.work_id, account_id=1, project_id=2, list_id=3, task_id=4, expected_generation=self.g)
        self.binding = {"work_id": self.work_id, "account_id": 1, "project_id": 2, "list_id": 3, "task_id": 4}
        self.journal = PollJournal(self.q)

    def observation(self, *, previous=None, task="task-v1", comments=None):
        old = previous or {"task_sha256": None, "comments": {}, "sequence": 0}
        hashes = {str(key): sha(value) for key, value in (comments or {}).items()}
        changes = []
        if old["task_sha256"] != sha(task):
            changes.append({"kind": "task_observed", "record_id": 4, "record_sha256": sha(task)})
        for key in sorted(set(old["comments"]) | set(hashes), key=int):
            if old["comments"].get(key) != hashes.get(key):
                changes.append({"kind": "comment_observed" if key in hashes else "comment_absent",
                                "record_id": int(key), "record_sha256": hashes.get(key)})
        sequence = old["sequence"]
        for change in changes:
            sequence += 1
            identity = "bc-" + sha([self.binding, sequence, change])
            change.update(source_order=sequence, event_id=identity, untrusted_content=True, grants_authority=False)
        checkpoint = {"binding": self.binding, "task_sha256": sha(task), "comments": hashes, "sequence": sequence}
        return {"binding": self.binding, "complete": True, "has_more": False, "checkpoint": checkpoint,
                "changes": changes, "fingerprint": sha(checkpoint), "task": {"content": "not persisted"},
                "comments": [{"content": "customer content not persisted"}]}

    def stage(self, observation, previous=None):
        return self.journal.stage(observation, expected_checkpoint_digest=sha(previous), expected_generation=self.g)

    def drain(self, max_events=25):
        return self.journal.drain(self.work_id, expected_generation=self.g, max_events=max_events)

    def events(self):
        return self.q.page(kind="event", work_id=self.work_id, limit=100)["items"]

    def assertCode(self, code, operation):
        with self.assertRaises(QueueError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_stage_freezes_complete_batch_before_any_enqueue(self):
        observation = self.observation(comments={5: "ready", 6: "file"})
        batch = self.stage(observation)
        self.assertEqual(len(batch["changes"]), 3)
        self.assertEqual(self.events(), [])
        self.assertIsNone(self.journal.checkpoint(self.work_id))
        self.assertEqual(self.journal.pending(self.work_id)["id"], batch["id"])
        self.assertNotIn("customer content", str(self.backend.records))
        observation["changes"].clear()
        self.assertEqual(len(self.journal.pending(self.work_id)["changes"]), 3)

    def test_incremental_drain_commits_checkpoint_only_after_all_events(self):
        observation = self.observation(comments={5: "first", 6: "second"})
        self.stage(observation)
        first = self.drain(max_events=1)
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first["remaining"], 2)
        self.assertIsNone(self.journal.checkpoint(self.work_id))
        second = self.drain(max_events=1)
        self.assertEqual(second["remaining"], 1)
        final = self.drain(max_events=1)
        self.assertEqual(final["status"], "committed")
        self.assertEqual(self.journal.checkpoint(self.work_id), observation["checkpoint"])
        self.assertIsNone(self.journal.pending(self.work_id))
        self.assertEqual(len(self.events()), 3)

    def test_crash_after_queue_write_before_ack_resumes_identical_event(self):
        observation = self.observation(comments={5: "one"})
        batch = self.stage(observation)
        original = self.q.enqueue_event
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("simulated process death after durable enqueue")
        self.q.enqueue_event = crash
        with self.assertRaises(RuntimeError):
            self.drain()
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.journal.pending(self.work_id)["next_index"], 0)
        self.assertIsNone(self.journal.checkpoint(self.work_id))
        self.q.enqueue_event = original
        # Simulate a new worker process using the same persisted backend.
        restarted_q = QueueStore(self.backend, clock=lambda: self.now)
        restarted = PollJournal(restarted_q)
        result = restarted.drain(self.work_id, expected_generation=self.g)
        self.assertEqual(result["batch_id"], batch["id"])
        self.assertEqual(result["status"], "committed")
        self.assertEqual(len(self.events()), 2)

    def test_provider_changes_before_recovery_cannot_replace_pending_batch(self):
        original = self.observation(comments={5: "version one"})
        batch = self.stage(original)
        self.drain(max_events=1)
        changed = self.observation(task="task-v2", comments={5: "version two", 6: "new"})
        self.assertCode("pending_poll_must_drain", lambda: self.stage(changed))
        self.assertEqual(self.journal.pending(self.work_id)["id"], batch["id"])
        self.drain()
        committed = self.journal.checkpoint(self.work_id)
        next_observation = self.observation(previous=committed, task="task-v2", comments={5: "version two", 6: "new"})
        self.stage(next_observation, committed)
        self.drain()
        source_orders = [row["source_order"] for row in self.events()]
        self.assertEqual(source_orders, [1, 2, 3, 4, 5])
        self.assertEqual(len({row["source_id"] for row in self.events()}), 5)

    def test_same_stage_can_reconcile_lost_reply_before_or_after_drain(self):
        observation = self.observation(comments={5: "ready"})
        first = self.stage(observation)
        self.assertEqual(self.stage(observation)["id"], first["id"])
        self.drain()
        recovered = self.stage(observation)
        self.assertEqual(recovered["state"], "committed")
        self.assertEqual(recovered["id"], first["id"])
        self.assertEqual(len(self.events()), 2)

    def test_unchanged_poll_produces_no_new_events(self):
        original = self.observation(comments={5: "same"})
        self.stage(original); self.drain()
        previous = self.journal.checkpoint(self.work_id)
        unchanged = self.observation(previous=previous, comments={5: "same"})
        staged = self.stage(unchanged, previous)
        self.assertEqual(staged["state"], "committed")
        self.assertEqual(self.drain()["status"], "idle")
        self.assertEqual(len(self.events()), 2)

    def test_comment_edit_and_removal_are_both_recorded_after_commit(self):
        self.stage(self.observation(comments={5: "old", 6: "removed"})); self.drain()
        previous = self.journal.checkpoint(self.work_id)
        changed = self.observation(previous=previous, comments={5: "new"})
        self.stage(changed, previous); self.drain()
        last = self.journal.checkpoint(self.work_id)
        self.assertEqual(last["sequence"], 5)
        self.assertNotIn("6", last["comments"])
        self.assertEqual([c["kind"] for c in changed["changes"]], ["comment_observed", "comment_absent"])

    def test_stale_checkpoint_cannot_skip_previously_committed_observations(self):
        self.stage(self.observation(comments={5: "one"})); self.drain()
        self.assertCode("poll_checkpoint_stale", lambda: self.stage(self.observation(task="different")))

    def test_partial_reader_result_cannot_be_staged(self):
        observation = self.observation()
        observation["complete"] = False
        self.assertCode("poll_incomplete", lambda: self.stage(observation))
        self.assertIsNone(self.journal.pending(self.work_id))

    def test_wrong_task_binding_is_held(self):
        observation = self.observation()
        observation["binding"] = {**observation["binding"], "task_id": 999}
        observation["checkpoint"]["binding"] = observation["binding"]
        observation["fingerprint"] = sha(observation["checkpoint"])
        self.assertCode("poll_work_binding_mismatch", lambda: self.stage(observation))

    def test_missing_change_prevents_checkpoint_advance(self):
        observation = self.observation(comments={5: "first"})
        observation["changes"].pop()
        self.assertCode("poll_changes_checkpoint_mismatch", lambda: self.stage(observation))
        self.assertIsNone(self.journal.checkpoint(self.work_id))

    def test_forged_change_identity_and_authority_flag_are_rejected(self):
        observation = self.observation()
        bad = copy.deepcopy(observation)
        bad["changes"][0]["event_id"] = "caller-selected-id"
        self.assertCode("poll_change_identity_invalid", lambda: self.stage(bad))
        bad = copy.deepcopy(observation)
        bad["changes"][0]["grants_authority"] = True
        self.assertCode("poll_change_invalid", lambda: self.stage(bad))

    def test_oversized_batch_fails_closed_without_truncation(self):
        observation = self.observation(comments={v: str(v) for v in range(100, 200)})
        self.assertEqual(len(observation["changes"]), 101)
        self.assertCode("poll_change_limit", lambda: self.stage(observation))
        self.assertIsNone(self.journal.checkpoint(self.work_id))
        self.assertEqual(self.events(), [])

    def test_global_stop_prevents_new_enqueues_and_resumes_same_pending_batch(self):
        observation = self.observation(comments={5: "one"})
        batch = self.stage(observation)
        self.drain(max_events=1)
        stopped = self.q.set_control("stopped", actor="human", expected_generation=self.g)
        self.assertCode("global_stop_or_stale_generation", lambda: self.drain())
        self.assertEqual(len(self.events()), 1)
        self.g = self.q.set_control("queue_open", actor="human", expected_generation=stopped["generation"])["generation"]
        self.assertEqual(self.drain()["batch_id"], batch["id"])
        self.assertEqual(len(self.events()), 2)

    def test_ack_without_durable_queue_event_cannot_advance_checkpoint(self):
        batch = self.stage(self.observation())
        self.assertCode("poll_event_not_durable", lambda: self.journal._ack(self.work_id, batch["id"], 0))
        self.assertEqual(self.journal.pending(self.work_id)["next_index"], 0)
        self.assertIsNone(self.journal.checkpoint(self.work_id))

    def test_real_reader_with_fake_transport_integrates_without_identity_translation(self):
        # Exercise production reader -> journal -> queue code together. Only the
        # HTTP boundary is fake; no credentials or network are involved.
        from v11_basecamp_reader import BasecampReader
        from test_v11_basecamp_reader import FakeGet, COMMENTS, comment
        work = self.q.bind_work(brand_id="gamechangers", thread_id="reader-thread", account_id=10,
            project_id=20, list_id=30, service_id="copy", expected_generation=self.g)
        work = self.q.bind_task(work["work_id"], account_id=10, project_id=20,
            list_id=30, task_id=40, expected_generation=self.g)
        work_id = work["work_id"]
        fake = FakeGet()
        reader = BasecampReader(fake, max_comments=99)
        first = reader.poll(work)
        self.journal.stage(first, expected_checkpoint_digest=sha(None), expected_generation=self.g)
        self.journal.drain(work_id, expected_generation=self.g)
        previous = self.journal.checkpoint(work_id)
        unchanged = reader.poll(work, previous=previous)
        self.assertEqual(unchanged["changes"], [])
        self.journal.stage(unchanged, expected_checkpoint_digest=sha(previous), expected_generation=self.g)
        fake.routes[COMMENTS] = (200, [comment(text="new exact revision")], {"ETag": "edited-version"})
        updated = reader.poll(work, previous=self.journal.checkpoint(work_id))
        self.assertEqual([v["kind"] for v in updated["changes"]], ["comment_observed"])
        self.journal.stage(updated, expected_checkpoint_digest=sha(previous), expected_generation=self.g)
        self.journal.drain(work_id, expected_generation=self.g)
        events = self.q.page(kind="event", work_id=work_id)["items"]
        self.assertEqual(len(events), 3)
        self.assertEqual([e["source_order"] for e in events], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
