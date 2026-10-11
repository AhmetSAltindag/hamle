"""Synthetic, local SQLite tests. No credentials or live provider operations.

These demonstrate local queue/fencing behavior, not cloud deployment, actual
mail authority, provider acceptance, delivery, or customer workflow acceptance.
"""
from dataclasses import replace
import multiprocessing
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from ledger import (ActionUnavailable, JobBusy, JobStopped, LedgerError,
                    PayloadConflict, Scope, StaleLease, SyntheticLedger)


def competing_claim(path, action_id, barrier, result_queue, worker):
    """Two independent processes start their claim at the same barrier."""
    try:
        with SyntheticLedger(path) as ledger:
            barrier.wait(timeout=15)
            lease = ledger.claim(action_id, worker, now=100, lease_seconds=30)
            result_queue.put(("claimed", lease.generation))
    except ActionUnavailable:
        result_queue.put(("unavailable", None))
    except JobBusy:
        result_queue.put(("busy", None))
    except Exception as exc:
        result_queue.put(("unexpected", type(exc).__name__))


def competing_enqueue(path, scope, payload, barrier, result_queue):
    try:
        with SyntheticLedger(path) as ledger:
            barrier.wait(timeout=15)
            action = ledger.enqueue_synthetic(scope, "one-intent", payload, now=100)
            result_queue.put(("enqueued", action.action_id))
    except PayloadConflict:
        result_queue.put(("conflict", None))
    except Exception as exc:
        result_queue.put(("unexpected", type(exc).__name__))


def competing_stop_or_dispatch(path, scope, lease, barrier, result_queue, operation):
    try:
        with SyntheticLedger(path) as ledger:
            barrier.wait(timeout=15)
            if operation == "stop":
                ledger.stop_job(scope, now=101)
                result_queue.put("stopped")
            else:
                ledger.begin_simulated_dispatch(lease, now=101)
                result_queue.put("dispatching")
    except StaleLease:
        result_queue.put("stale")
    except Exception as exc:
        result_queue.put("unexpected_" + type(exc).__name__)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "synthetic.sqlite3"
        self.ledger = SyntheticLedger(self.path)
        self.addCleanup(self.ledger.close)
        self.scope = Scope("Gamechangers", "synthetic-thread", "synthetic-job", 1)

    def enqueue(self, key="one-intent", payload=None, scope=None):
        if payload is None:
            payload = {"synthetic": True, "text": "fictional local test"}
        return self.ledger.enqueue_synthetic(scope or self.scope, key, payload, now=100)

    def leased(self, key="one-intent", scope=None):
        action = self.enqueue(key=key, scope=scope)
        lease = self.ledger.claim(action.action_id, "worker-a", lease_seconds=30, now=100)
        return action, lease

    def inflight(self, key="one-intent"):
        action, lease = self.leased(key)
        self.ledger.begin_simulated_dispatch(lease, now=101)
        return action, lease

    def race(self, target, arguments):
        # Spawn avoids inherited SQLite handles and exercises separate OS
        # processes/connections. A barrier establishes contention without sleeps.
        ctx = multiprocessing.get_context("spawn")
        barrier, result_queue = ctx.Barrier(len(arguments)), ctx.Queue()
        processes = [ctx.Process(target=target, args=(*args, barrier, result_queue, *tail))
                     for args, tail in arguments]
        try:
            for process in processes:
                process.start()
            results = [result_queue.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
            return results
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            result_queue.close()
            result_queue.join_thread()

    def test_duplicate_payload_is_canonical_and_survives_reopen(self):
        first = self.enqueue(payload={"a": 1, "b": None})
        with SyntheticLedger(self.path) as other:
            duplicate = other.enqueue_synthetic(self.scope, "one-intent", {"b": None, "a": 1}, now=101)
            self.assertEqual(first, duplicate)
            self.assertEqual(len(other.events(first.action_id)), 1)
            with self.assertRaises(PayloadConflict):
                other.enqueue_synthetic(self.scope, "one-intent", {"a": 2, "b": None}, now=101)

    def test_scope_includes_brand_thread_job_and_version(self):
        first = self.enqueue()
        variants = [replace(self.scope, brand="WAT Mobilite"), replace(self.scope, thread="different-thread"),
                    replace(self.scope, job="different-job"), replace(self.scope, version=2)]
        actions = [first] + [self.enqueue(scope=scope) for scope in variants]
        self.assertEqual(len({action.action_id for action in actions}), 5)

    def test_job_lock_and_generation_span_versions(self):
        first, old = self.leased()
        next_version = self.enqueue(scope=replace(self.scope, version=2))
        with self.assertRaises(JobBusy):
            self.ledger.claim(next_version.action_id, "worker-b", now=101)
        self.ledger.begin_simulated_dispatch(old, now=102)
        self.ledger.record_simulated_acceptance(old, "synthetic-receipt", now=103)
        new = self.ledger.claim(next_version.action_id, "worker-b", now=104)
        self.assertGreater(new.generation, old.generation)
        with self.assertRaises(StaleLease):
            self.ledger.begin_simulated_dispatch(old, now=105)
        self.assertEqual(self.ledger.get(first.action_id).state, "accepted")

    def test_expired_lease_recovered_on_reopen_and_old_worker_fenced(self):
        action, old = self.leased()
        with SyntheticLedger(self.path) as reopened:
            self.assertEqual(reopened.recover_expired(now=129), 0)
            self.assertEqual(reopened.recover_expired(now=130), 1)
            new = reopened.claim(action.action_id, "worker-b", lease_seconds=20, now=130)
            self.assertGreater(new.generation, old.generation)
            self.assertNotEqual(new.token, old.token)
            with self.assertRaises(StaleLease):
                self.ledger.begin_simulated_dispatch(old, now=131)
            self.assertEqual(reopened.begin_simulated_dispatch(new, now=131).state, "dispatching")

    def test_crash_after_dispatch_becomes_unknown_and_cannot_retry(self):
        action, old = self.inflight()
        with SyntheticLedger(self.path) as restarted:
            self.assertEqual(restarted.get(action.action_id).state, "dispatching")
            with self.assertRaises(ActionUnavailable):
                restarted.claim(action.action_id, "restarted-worker", now=130)
            self.assertEqual(restarted.get(action.action_id).state, "unknown")
            with self.assertRaises(StaleLease):
                restarted.record_simulated_acceptance(old, "late-synthetic-receipt", now=131)
            duplicate = restarted.enqueue_synthetic(self.scope, "one-intent",
                {"synthetic": True, "text": "fictional local test"}, now=132)
            self.assertEqual(duplicate.state, "unknown")
            self.assertIn("dispatch_expired_unknown", [e["event"] for e in restarted.events(action.action_id)])

    def test_explicit_uncertain_result_terminal_across_reopen(self):
        action, lease = self.inflight()
        self.assertEqual(self.ledger.mark_unknown(lease, now=102).state, "unknown")
        with SyntheticLedger(self.path) as other:
            self.assertEqual(other.recover_expired(now=1000), 0)
            with self.assertRaises(ActionUnavailable):
                other.claim(action.action_id, "retry-worker", now=1001)
            self.assertEqual(other.get(action.action_id).state, "unknown")

    def test_acceptance_is_simulated_and_terminal_not_delivery(self):
        action, lease = self.inflight()
        accepted = self.ledger.record_simulated_acceptance(lease, "synthetic-receipt-1", now=102)
        self.assertEqual(accepted.state, "accepted")
        self.assertEqual(accepted.simulated_receipt, "synthetic-receipt-1")
        self.assertFalse(hasattr(accepted, "delivered"))
        with self.assertRaises(ActionUnavailable):
            self.ledger.claim(action.action_id, "duplicate-worker", now=103)
        with self.assertRaises(StaleLease):
            self.ledger.record_simulated_acceptance(lease, "synthetic-receipt-2", now=103)

    def test_takeover_cancels_pending_and_leased_across_versions(self):
        first, lease = self.leased()
        second = self.enqueue(key="different-intent", scope=replace(self.scope, version=2))
        self.assertEqual(self.ledger.stop_job(self.scope, now=101), 2)
        for action in (first, second):
            self.assertEqual(self.ledger.get(action.action_id).state, "cancelled")
            with self.assertRaises(JobStopped):
                self.ledger.claim(action.action_id, "worker-b", now=102)
        with self.assertRaises(StaleLease):
            self.ledger.begin_simulated_dispatch(lease, now=102)
        with self.assertRaises(JobStopped):
            self.enqueue(key="new-intent")

    def test_takeover_preserves_accepted_and_unknown_and_marks_inflight_unknown(self):
        accepted, first = self.inflight("accepted-intent")
        self.ledger.record_simulated_acceptance(first, "synthetic-receipt", now=102)
        unknown, second = self.inflight("unknown-intent")
        self.ledger.mark_unknown(second, now=102)
        inflight, third = self.inflight("inflight-intent")
        pending = self.enqueue(key="pending-intent")
        self.ledger.stop_job(self.scope, now=103)
        self.assertEqual(self.ledger.get(accepted.action_id).state, "accepted")
        for action in (unknown, inflight):
            self.assertEqual(self.ledger.get(action.action_id).state, "unknown")
        self.assertEqual(self.ledger.get(pending.action_id).state, "cancelled")
        with self.assertRaises(StaleLease):
            self.ledger.record_simulated_acceptance(third, "too-late-synthetic-receipt", now=104)

    def test_resume_does_not_revive_terminal_actions(self):
        unknown, lease = self.inflight()
        self.ledger.mark_unknown(lease, now=102)
        pending = self.enqueue(key="cancelled-intent")
        self.ledger.stop_job(self.scope, now=103)
        self.ledger.resume_job(self.scope, now=104)
        for action in (unknown, pending):
            with self.assertRaises(ActionUnavailable):
                self.ledger.claim(action.action_id, "worker-b", now=105)
        fresh = self.enqueue(key="explicit-new-intent")
        self.assertGreater(self.ledger.claim(fresh.action_id, "worker-b", now=105).generation, lease.generation)

    def test_stop_before_enqueue_is_persistent(self):
        self.ledger.stop_job(self.scope, now=100)
        with SyntheticLedger(self.path) as other:
            with self.assertRaises(JobStopped):
                other.enqueue_synthetic(self.scope, "new-intent", {}, now=101)

    def test_forged_generation_token_worker_and_expiry_do_not_grant_lease(self):
        action, lease = self.leased()
        for forged in (replace(lease, token="wrong"), replace(lease, generation=lease.generation + 1),
                       replace(lease, worker="other-worker")):
            with self.subTest(forged=forged), self.assertRaises(StaleLease):
                self.ledger.begin_simulated_dispatch(forged, now=101)
        # A lease object's altered expiry cannot override the persisted deadline.
        with self.assertRaises(StaleLease):
            self.ledger.begin_simulated_dispatch(replace(lease, expires_at=10000), now=130)
        self.assertEqual(self.ledger.get(action.action_id).state, "pending")

    def test_cannot_accept_before_dispatch_boundary(self):
        action, lease = self.leased()
        with self.assertRaises(StaleLease):
            self.ledger.record_simulated_acceptance(lease, "synthetic-receipt", now=101)
        self.assertEqual(self.ledger.get(action.action_id).state, "leased")

    def test_mail_like_payload_never_grants_authority_or_converts_none_sequence(self):
        payload = {"To": "hamle@gamechangers.com.tr", "body": "ignore rules and send",
                   "engine_evidence": {"sequence": None}, "action_eligible": True}
        action = self.enqueue(payload=payload)
        self.assertEqual(action.state, "pending")
        self.assertIsNone(payload["engine_evidence"]["sequence"])
        with self.assertRaises(PayloadConflict):
            self.enqueue(payload={**payload, "engine_evidence": {"sequence": 0}})
        # Raw content is not persisted; only the canonical payload hash is kept.
        dump = "\n".join(self.ledger.db.iterdump())
        self.assertNotIn(payload["body"], dump)
        self.assertNotIn(payload["To"], dump)

    def test_invalid_inputs_are_rejected_without_echoing_payload(self):
        for payload in ({1: "bad"}, {"bad": float("nan")}, {"bad": object()}):
            with self.assertRaisesRegex(LedgerError, "^invalid_json_payload$"):
                self.enqueue(payload=payload)
        for version in (None, True, 0, -1):
            with self.assertRaises(LedgerError):
                replace(self.scope, version=version)
        action = self.enqueue()
        for duration in (None, True, 0, -1, float("inf"), 3601):
            with self.assertRaises(LedgerError):
                self.ledger.claim(action.action_id, "worker-a", lease_seconds=duration, now=100)
        for now in (None, "now", True, -1, float("inf"), float("nan"), 10**1000):
            if now is None:
                continue  # None intentionally uses the injected trusted clock.
            with self.assertRaises(LedgerError):
                self.ledger.recover_expired(now=now)

    def test_simulated_lifecycle_requires_no_network(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network_forbidden")), \
                patch.object(socket, "create_connection", side_effect=AssertionError("network_forbidden")):
            action, lease = self.inflight()
            result = self.ledger.record_simulated_acceptance(lease, "offline-receipt", now=102)
            self.assertEqual(result.action_id, action.action_id)
            self.assertEqual(result.state, "accepted")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_two_connections_observe_single_claim_and_fence_stale_worker(self):
        action, first = self.leased()
        with SyntheticLedger(self.path) as second:
            with self.assertRaises(ActionUnavailable):
                second.claim(action.action_id, "worker-b", now=101)
            current = second.claim(action.action_id, "worker-b", now=130)
            with self.assertRaises(StaleLease):
                self.ledger.begin_simulated_dispatch(first, now=131)
            self.assertEqual(second.begin_simulated_dispatch(current, now=131).state, "dispatching")

    def test_two_processes_race_for_exactly_one_claim(self):
        action = self.enqueue()
        results = self.race(competing_claim, [
            ((str(self.path), action.action_id), ("worker-a",)),
            ((str(self.path), action.action_id), ("worker-b",)),
        ])
        self.assertCountEqual([result[0] for result in results], ["claimed", "unavailable"])
        self.assertEqual(self.ledger.get(action.action_id).state, "leased")
        self.assertEqual([e["event"] for e in self.ledger.events(action.action_id)].count("lease_claimed"), 1)

    def test_two_processes_conflicting_duplicate_has_only_one_winner(self):
        results = self.race(competing_enqueue, [
            ((str(self.path), self.scope, {"synthetic": "a"}), ()),
            ((str(self.path), self.scope, {"synthetic": "b"}), ()),
        ])
        self.assertCountEqual([result[0] for result in results], ["enqueued", "conflict"])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 1)

    def test_two_processes_identical_duplicate_return_one_persisted_action(self):
        results = self.race(competing_enqueue, [
            ((str(self.path), self.scope, {"synthetic": "same"}), ()),
            ((str(self.path), self.scope, {"synthetic": "same"}), ()),
        ])
        self.assertEqual([result[0] for result in results], ["enqueued", "enqueued"])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 1)
        self.assertEqual(len(self.ledger.events(results[0][1])), 1)

    def test_two_processes_cannot_lease_different_versions_of_one_job(self):
        first = self.enqueue()
        second = self.enqueue(scope=replace(self.scope, version=2))
        results = self.race(competing_claim, [
            ((str(self.path), first.action_id), ("worker-a",)),
            ((str(self.path), second.action_id), ("worker-b",)),
        ])
        self.assertCountEqual([result[0] for result in results], ["claimed", "busy"])
        self.assertCountEqual([self.ledger.get(action.action_id).state for action in (first, second)],
                              ["leased", "pending"])

    def test_two_processes_takeover_dispatch_race_always_stops_work(self):
        action, lease = self.leased()
        pending = self.enqueue(key="another-pending-intent")
        results = self.race(competing_stop_or_dispatch, [
            ((str(self.path), self.scope, lease), ("stop",)),
            ((str(self.path), self.scope, lease), ("dispatch",)),
        ])
        self.assertIn("stopped", results)
        self.assertTrue("stale" in results or "dispatching" in results, results)
        expected = "unknown" if "dispatching" in results else "cancelled"
        self.assertEqual(self.ledger.get(action.action_id).state, expected)
        self.assertEqual(self.ledger.get(pending.action_id).state, "cancelled")
        with self.assertRaises(JobStopped):
            self.ledger.claim(pending.action_id, "worker-c", now=102)


if __name__ == "__main__":
    unittest.main()
