"""Offline tests only; no provider calls or credentials."""
import hashlib
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from store import Store, StoreError, SUBJECT


def fixture(observed_at=102):
    thread, message = "a123", "b456"
    return {
        "status": "review_ready", "reason": "human_review_required", "test_subject": SUBJECT,
        "review_item": {
            "review_id": hashlib.sha256(f"{thread}:{message}".encode()).hexdigest(),
            "brand": "gamechangers_internal", "thread_id": thread, "message_id": message,
            "capture_fingerprint": "a" * 64, "source": "captured_normalized",
            "control_generation": 0, "human_renewed_delayed_review": False,
            "status": "awaiting_human_review", "action_eligible": False,
            "latest_scope": "latest_observed_in_captured_snapshot_only",
            "unseen_cc_removal_resolved": False, "body_or_attachments_included": False,
        },
        "action_eligible": False, "mailbox_writes": 0, "external_actions_enabled": False,
        "body_returned": False, "attachments_fetched": False, "token_refreshed": False,
        "read_scope": "one_explicit_internal_test_thread_in_hamle_mailbox",
        "unseen_cc_removal_resolved": False,
        "capture_observed_at": observed_at,
    }


def worker_import(path, ticket, barrier, queue):
    try:
        store = Store(path, clock=lambda: 100)
        barrier.wait(timeout=10)
        result = store.import_capture(fixture(), ticket=ticket, observed_at=102, now=103)
        queue.put(result["outcome"])
    except Exception as error:
        queue.put(type(error).__name__ + ":" + str(error))


def worker_stop(path, barrier, queue):
    try:
        store = Store(path, clock=lambda: 100)
        barrier.wait(timeout=10)
        store.stop(now=103)
        queue.put("stopped")
    except Exception as error:
        queue.put(type(error).__name__ + ":" + str(error))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "review.sqlite3"
        self.store = Store(self.path, initialize_review_open=True, clock=lambda: 100)

    def imported(self):
        ticket = self.store.begin_capture(now=101)
        result = self.store.import_capture(fixture(), ticket=ticket, observed_at=102, now=103)
        return ticket, result

    def test_initial_default_is_stopped_and_reinitializing_cannot_open(self):
        path = self.path.with_name("stopped.sqlite3")
        first = Store(path, clock=lambda: 100)
        self.assertEqual(first.public_state()["control"]["mode"], "stopped")
        reopened = Store(path, initialize_review_open=True, clock=lambda: 100)
        with self.assertRaisesRegex(StoreError, "human_control_stopped"):
            reopened.begin_capture()

    def test_persists_metadata_only_and_restart(self):
        _, result = self.imported()
        self.assertEqual(result["outcome"], "created")
        restarted = Store(self.path, clock=lambda: 104)
        state = restarted.public_state()
        self.assertEqual(len(state["items"]), 1)
        self.assertFalse(state["action_eligible"])
        self.assertEqual(state["items"][0]["status"], "awaiting_human_review")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(self.path) as db:
            data = "\n".join(db.iterdump()).encode()
        for forbidden in (b"@", b"Subject:", b"mail body", b"attachment"):
            self.assertNotIn(forbidden, data)

    def test_same_capture_is_idempotent(self):
        ticket, _ = self.imported()
        outcome = self.store.import_capture(fixture(), ticket=ticket, observed_at=102, now=104)
        self.assertEqual(outcome["outcome"], "duplicate")
        self.assertEqual(len(self.store.public_state(now=104)["items"]), 1)

    def test_new_fresh_read_deduplicates_item_and_refreshes_display(self):
        self.imported()
        ticket = self.store.begin_capture(now=403)
        outcome = self.store.import_capture(fixture(404), ticket=ticket, observed_at=404, now=405)
        self.assertEqual(outcome["outcome"], "duplicate")
        items = self.store.public_state(now=405)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["observed_at"], 404)

    def test_ticket_is_bound_to_capture_timestamp_and_fingerprint(self):
        ticket, _ = self.imported()
        for change in ("time", "fingerprint"):
            value = fixture()
            stamp = 102
            if change == "time":
                stamp = 103
                value["capture_observed_at"] = stamp
            else:
                value["review_item"]["capture_fingerprint"] = "b" * 64
            with self.assertRaisesRegex(StoreError, "capture_ticket_already_consumed"):
                self.store.import_capture(value, ticket=ticket, observed_at=stamp, now=104)

    def test_rejects_raw_fields_and_wrong_scope_without_persisting(self):
        mutations = [
            lambda x: x.update(body="mail body"),
            lambda x: x["review_item"].update(from_address="someone@example.com"),
            lambda x: x.update(test_subject="another subject"),
            lambda x: x.update(action_eligible=True),
            lambda x: x.update(mailbox_writes=False),
            lambda x: x["review_item"].update(brand="wat"),
            lambda x: x["review_item"].update(source="synthetic_fixture"),
            lambda x: x["review_item"].update(control_generation=1),
            lambda x: x["review_item"].update(review_id="b" * 64),
            lambda x: x["review_item"].update(message_id="not-an-id@example.com"),
        ]
        ticket = self.store.begin_capture(now=101)
        for mutate in mutations:
            value = fixture()
            mutate(value)
            with self.assertRaises(StoreError):
                self.store.import_capture(value, ticket=ticket, observed_at=102, now=103)
        self.assertEqual(self.store.public_state()["items"], [])
        self.assertNotIn(b"mail body", self.path.read_bytes())

    def test_freshness_is_display_only_never_action_authority(self):
        self.imported()
        self.assertEqual(self.store.public_state(now=402)["items"][0]["status"], "awaiting_human_review")
        stale = self.store.public_state(now=403)
        self.assertEqual(stale["items"][0]["status"], "held_stale")
        self.assertFalse(stale["items"][0]["action_eligible"])
        self.assertFalse(stale["external_actions_enabled"])

    def test_rejects_stale_future_and_pre_ticket_captures(self):
        ticket = self.store.begin_capture(now=101)
        for observed, now in ((102, 403), (105, 103), (100, 103)):
            with self.assertRaises(StoreError):
                self.store.import_capture(fixture(observed), ticket=ticket, observed_at=observed, now=now)
        self.assertEqual(self.store.public_state()["items"], [])

    def test_unknown_capture_ticket_rejected(self):
        with self.assertRaisesRegex(StoreError, "unknown_capture_ticket"):
            self.store.import_capture(fixture(101), ticket="a" * 32, observed_at=101, now=102)

    def test_stop_cancels_pending_and_survives_restart(self):
        self.imported()
        stopped = self.store.stop(now=104)
        self.assertEqual(stopped["control"]["generation"], 1)
        self.assertEqual(stopped["items"][0]["status"], "cancelled")
        restarted = Store(self.path, initialize_review_open=True, clock=lambda: 105)
        self.assertEqual(restarted.public_state()["control"]["mode"], "stopped")
        with self.assertRaisesRegex(StoreError, "human_control_stopped"):
            restarted.begin_capture()

    def test_takeover_has_own_persisted_mode(self):
        self.imported()
        taken = self.store.takeover(now=104)
        self.assertEqual(taken["control"]["mode"], "takeover")
        self.assertEqual(taken["control"]["generation"], 1)
        self.assertEqual(taken["items"][0]["status"], "cancelled")

    def test_backward_clock_cannot_prevent_stop(self):
        self.imported()
        stopped = self.store.stop(now=99)
        self.assertEqual(stopped["control"], {"mode": "stopped", "generation": 1, "changed_at": 100})
        self.assertEqual(stopped["items"][0]["status"], "cancelled")
        restarted = Store(self.path, clock=lambda: 99)
        self.assertEqual(restarted.public_state()["control"], stopped["control"])
        self.assertEqual(restarted.public_state()["items"][0]["status"], "cancelled")

    def test_backward_clock_cannot_prevent_takeover_or_inflight_fence(self):
        self.imported()
        ticket = self.store.begin_capture(now=103)
        taken = self.store.takeover(now=98)
        self.assertEqual(taken["control"], {"mode": "takeover", "generation": 1, "changed_at": 100})
        self.assertEqual(taken["items"][0]["status"], "cancelled")
        self.store.resume_review(now=104)
        blocked = self.store.import_capture(fixture(105), ticket=ticket, now=106)
        self.assertEqual(blocked["reason"], "old_control_generation")
        self.assertEqual(self.store.public_state(now=106)["items"][0]["status"], "cancelled")

    def test_backward_clock_still_blocks_resume_and_capture(self):
        with self.assertRaisesRegex(StoreError, "clock_before_control_event"):
            self.store.begin_capture(now=99)
        self.store.stop(now=110)
        with self.assertRaisesRegex(StoreError, "clock_before_control_event"):
            self.store.resume_review(now=109)
        state = self.store.public_state(now=109)
        self.assertEqual(state["control"], {"mode": "stopped", "generation": 1, "changed_at": 110})

    def test_resume_cannot_reactivate_prior_rows_or_inflight_ticket(self):
        old_ticket, _ = self.imported()
        inflight = self.store.begin_capture(now=103)
        self.store.stop(now=104)
        resumed = self.store.resume_review(now=105)
        self.assertEqual(resumed["control"]["generation"], 2)
        self.assertEqual(resumed["items"][0]["status"], "cancelled")
        for ticket in (old_ticket, inflight):
            blocked = self.store.import_capture(fixture(106), ticket=ticket, observed_at=106, now=107)
            self.assertEqual(blocked["reason"], "old_control_generation")
        new_ticket = self.store.begin_capture(now=108)
        with self.assertRaises(StoreError):
            self.store.import_capture(fixture(), ticket=new_ticket, observed_at=102, now=109)
        created = self.store.import_capture(fixture(109), ticket=new_ticket, observed_at=109, now=110)
        self.assertEqual(created["outcome"], "reopened")
        state = self.store.public_state(now=110)
        self.assertEqual(len(state["items"]), 1)
        self.assertEqual(state["items"][0]["generation"], 2)
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT generation,status FROM items ORDER BY generation").fetchall()
        self.assertEqual(rows, [(0, "cancelled"), (2, "awaiting_human_review")])

    def test_new_ticket_cannot_replay_same_old_read(self):
        self.imported()
        ticket = self.store.begin_capture(now=101)
        with self.assertRaisesRegex(StoreError, "capture_not_newer"):
            self.store.import_capture(fixture(), ticket=ticket, observed_at=102, now=104)

    def test_changed_fingerprint_does_not_replace_pending_review(self):
        self.imported()
        value = fixture()
        value["review_item"]["capture_fingerprint"] = "b" * 64
        value["capture_observed_at"] = 105
        ticket = self.store.begin_capture(now=104)
        with self.assertRaisesRegex(StoreError, "changed_snapshot"):
            self.store.import_capture(value, ticket=ticket, observed_at=105, now=106)
        self.assertEqual(self.store.public_state(now=106)["items"][0]["capture_fingerprint"], "a" * 64)

    def test_stop_blocks_capture_finishing_late(self):
        ticket = self.store.begin_capture(now=101)
        self.store.stop(now=102)
        blocked = self.store.import_capture(fixture(103), ticket=ticket, observed_at=103, now=104)
        self.assertEqual(blocked["reason"], "human_control_stopped")
        self.assertEqual(self.store.public_state()["items"], [])

    def test_capture_timestamp_cannot_be_relabelled(self):
        ticket = self.store.begin_capture(now=101)
        with self.assertRaisesRegex(StoreError, "capture_timestamp_mismatch"):
            self.store.import_capture(fixture(), ticket=ticket, observed_at=103, now=104)
        result = self.store.import_capture(fixture(), ticket=ticket, now=104)
        self.assertEqual(result["outcome"], "created")

    def test_invalid_clocks_fail_closed(self):
        for value in (True, float("nan"), float("inf"), -1, 10**1000):
            with self.assertRaises(StoreError):
                self.store.public_state(now=value)

    def _run_processes(self, specs):
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(len(specs)), context.Queue()
        processes = [context.Process(target=target, args=(*args, barrier, queue)) for target, args in specs]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            if process.is_alive():
                process.terminate()
                process.join()
                self.fail("worker_timeout")
            self.assertEqual(process.exitcode, 0)
        return [queue.get(timeout=2) for _ in specs]

    def test_multiprocess_duplicate_same_ticket_creates_one_row(self):
        ticket = self.store.begin_capture(now=101)
        results = self._run_processes([(worker_import, (str(self.path), ticket))] * 4)
        self.assertEqual(sorted(results), ["created", "duplicate", "duplicate", "duplicate"])
        self.assertEqual(len(self.store.public_state(now=104)["items"]), 1)

    def test_multiprocess_stop_vs_import_never_leaves_pending(self):
        ticket = self.store.begin_capture(now=101)
        results = self._run_processes([
            (worker_import, (str(self.path), ticket)),
            (worker_stop, (str(self.path),)),
        ])
        self.assertIn("stopped", results)
        self.assertTrue(set(results) <= {"stopped", "created", "blocked"})
        state = self.store.public_state(now=104)
        self.assertEqual(state["control"]["mode"], "stopped")
        self.assertTrue(all(item["status"] == "cancelled" for item in state["items"]))


if __name__ == "__main__":
    unittest.main()
