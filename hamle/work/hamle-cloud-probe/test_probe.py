"""In-memory transaction fakes; these do not validate Firestore concurrency."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import io
import json
import threading
import types
import unittest
from unittest.mock import patch

from core import (Config, DEADLINE, NAMESPACE, PROJECT, Refused, authorize_network,
                  claim, complete, initialize, run_once, set_enabled)
from main import main
from firestore_store import FirestoreStore

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
CONFIG = Config(PROJECT, NAMESPACE, "(default)", "synthetic-20261010-01")
OWNER = "a" * 32


class FakeTx:
    def __init__(self, documents):
        self.documents = documents
        self.wrote = False

    def get(self, key):
        if self.wrote:
            raise AssertionError("Firestore reads must precede writes")
        return deepcopy(self.documents.get(key))

    def create(self, key, data):
        if key in self.documents:
            raise AssertionError("already exists")
        self.put(key, data)

    def put(self, key, data):
        self.wrote = True
        self.documents[key] = deepcopy(data)


class FakeStore:
    def __init__(self):
        self.documents = {}
        self.lock = threading.Lock()
        self.calls = 0
        self.fail_after_commit = None

    def transact(self, callback):
        with self.lock:
            self.calls += 1
            working = deepcopy(self.documents)
            result = callback(FakeTx(working))
            self.documents = working
            if self.calls == self.fail_after_commit:
                raise RuntimeError("SECRET SENTINEL: never log SDK error")
            return result


class ProbeTests(unittest.TestCase):
    def armed(self):
        store = FakeStore()
        initialize(store)
        set_enabled(store, True)
        return store

    def env(self):
        return {"HAMLE_PROJECT_ID": PROJECT, "HAMLE_PROBE_NAMESPACE": NAMESPACE,
                "HAMLE_FIRESTORE_DATABASE": "(default)",
                "HAMLE_PROBE_RUN_ID": CONFIG.run_id, "HAMLE_ALLOW_FIRESTORE_PROBE": "yes"}

    def test_missing_config_never_constructs_network_client(self):
        def forbidden(_):
            self.fail("network client constructed")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["run"], {}, forbidden, NOW), 2)

    def test_wrong_project_namespace_database_and_run_id_refused(self):
        values = [Config("other", NAMESPACE, "(default)", CONFIG.run_id),
                  Config(PROJECT, "customer", "(default)", CONFIG.run_id),
                  Config(PROJECT, NAMESPACE, "production", CONFIG.run_id),
                  Config(PROJECT, NAMESPACE, "(default)", "customer-request"),
                  Config(PROJECT, NAMESPACE, "(default)", "synthetic-a/b")]
        for config in values:
            with self.subTest(config=config), self.assertRaises(Refused):
                config.validate()

    def test_opt_in_and_expiry_guard_before_network(self):
        for opt_in, now in [("", NOW), ("yes", datetime(2026, 10, 13, tzinfo=timezone.utc)),
                            ("yes", datetime(2026, 10, 10))]:
            with self.subTest(opt_in=opt_in, now=now), self.assertRaises(Refused):
                authorize_network(CONFIG, opt_in, now)
        authorize_network(CONFIG, "yes", DEADLINE)

    def test_adapter_checks_live_clock_before_each_transaction(self):
        events = []
        clock_time = [NOW]

        class Client:
            def __init__(self, **kwargs):
                events.append("client")

            def collection(self, name):
                return object()

            def transaction(self, **kwargs):
                events.append("transaction")
                return object()

        sdk = types.SimpleNamespace(Client=Client, transactional=lambda callback: callback)
        cloud = types.ModuleType("google.cloud")
        cloud.firestore = sdk
        with patch.dict("sys.modules", {"google.cloud": cloud}):
            store = FirestoreStore(CONFIG, opt_in="yes", clock=lambda: clock_time[0])
            self.assertEqual(store.transact(lambda _: "claimed"), "claimed")
            clock_time[0] = DEADLINE + timedelta(microseconds=1)
            with self.assertRaisesRegex(Refused, "spending_authorization_expired"):
                store.transact(lambda _: self.fail("expired completion ran"))
        self.assertEqual(events, ["client", "transaction"])

    def test_expired_adapter_refuses_before_sdk_client_creation(self):
        with self.assertRaisesRegex(Refused, "spending_authorization_expired"):
            FirestoreStore(CONFIG, opt_in="yes", clock=lambda: DEADLINE + timedelta(seconds=1))

    def test_initialize_stopped_and_reinitialize_does_not_rearm(self):
        store = FakeStore()
        self.assertEqual(initialize(store), "initialized_stopped")
        self.assertEqual(run_once(store, CONFIG, OWNER), "stopped")
        self.assertEqual(initialize(store), "already_initialized")
        self.assertNotIn(CONFIG.run_key, store.documents)

    def test_missing_and_malformed_control_fail_closed(self):
        for control in [None, {"schema": 1, "enabled": "true", "generation": 0},
                        {"schema": 1, "enabled": True, "generation": True},
                        {"schema": True, "enabled": True, "generation": 0}]:
            store = FakeStore()
            if control is not None:
                store.documents["control"] = control
            with self.subTest(control=control), self.assertRaises(Refused):
                run_once(store, CONFIG, OWNER)
            self.assertNotIn(CONFIG.run_key, store.documents)

    def test_malformed_completed_records_never_report_acceptance_or_reclaim(self):
        store = self.armed()
        run_once(store, CONFIG, OWNER)
        valid = deepcopy(store.documents[CONFIG.run_key])
        mutations = [{"schema": True}, {"generation": True}, {"attempts": True},
                     {"attempts": 2}, {"owner": "short"}, {"state": "unknown"},
                     {"synthetic_result": 5050.0}, {"synthetic_result": 0}]
        candidates = [{**valid, **mutation} for mutation in mutations]
        candidates += [{k: v for k, v in valid.items() if k != field} for field in valid]
        for candidate in candidates:
            store.documents[CONFIG.run_key] = deepcopy(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(Refused):
                run_once(store, CONFIG, "b" * 32)
            self.assertEqual(store.documents[CONFIG.run_key], candidate)

    def test_boolean_generation_or_schema_cannot_complete_started_record(self):
        for mutation in [{"schema": True}, {"generation": True}, {"attempts": True},
                         {"synthetic_result": 5050}]:
            store = self.armed()
            claim(store, CONFIG, OWNER)
            store.documents[CONFIG.run_key].update(mutation)
            before = deepcopy(store.documents[CONFIG.run_key])
            with self.subTest(mutation=mutation), self.assertRaises(Refused):
                complete(store, CONFIG, OWNER)
            self.assertEqual(store.documents[CONFIG.run_key], before)

    def test_completion_persists_and_same_id_is_deduplicated(self):
        store = self.armed()
        self.assertEqual(run_once(store, CONFIG, OWNER), "completed")
        before = deepcopy(store.documents[CONFIG.run_key])
        self.assertEqual(run_once(store, CONFIG, "b" * 32), "duplicate_completed")
        self.assertEqual(store.documents[CONFIG.run_key], before)
        self.assertEqual(before["synthetic_result"], 5050)
        self.assertEqual(before["attempts"], 1)

    def test_crash_after_claim_is_held_without_reclaim(self):
        store = self.armed()
        self.assertEqual(claim(store, CONFIG, OWNER), "claimed")
        self.assertEqual(run_once(store, CONFIG, "b" * 32), "existing_run_held")
        self.assertEqual(store.documents[CONFIG.run_key]["state"], "started")

    def test_stop_between_claim_and_completion_prevents_acceptance(self):
        store = self.armed()
        claim(store, CONFIG, OWNER)
        set_enabled(store, False)
        self.assertEqual(complete(store, CONFIG, OWNER), "stopped_or_fenced")
        self.assertNotIn("synthetic_result", store.documents[CONFIG.run_key])

    def test_stop_then_rearm_fences_old_worker_and_retains_run(self):
        store = self.armed()
        claim(store, CONFIG, OWNER)
        set_enabled(store, False)
        set_enabled(store, True)
        self.assertEqual(complete(store, CONFIG, OWNER), "stopped_or_fenced")
        self.assertEqual(run_once(store, CONFIG, "b" * 32), "existing_run_held")

    def test_other_owner_cannot_finalize(self):
        store = self.armed()
        claim(store, CONFIG, OWNER)
        with self.assertRaises(Refused):
            complete(store, CONFIG, "b" * 32)
        self.assertEqual(store.documents[CONFIG.run_key]["state"], "started")

    def test_fake_atomic_race_accepts_one_claim(self):
        # Serializability comes from fake lock, not a real Firestore proof.
        store = self.armed()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda n: claim(store, CONFIG, f"{n:032x}"), range(8)))
        self.assertEqual(results.count("claimed"), 1)
        self.assertEqual(results.count("existing_run_held"), 7)

    def test_ambiguous_claim_commit_is_redacted_and_never_reclaimed(self):
        store = self.armed()
        store.fail_after_commit = store.calls + 1
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["run"], self.env(), lambda _: store, NOW)
        self.assertEqual(result, 4)
        self.assertEqual(json.loads(output.getvalue())["outcome"], "unknown_hold")
        self.assertNotIn("SECRET SENTINEL", output.getvalue())
        self.assertEqual(run_once(store, CONFIG, "b" * 32), "existing_run_held")

    def test_ambiguous_completion_commit_is_deduplicated_on_inspection(self):
        store = self.armed()
        store.fail_after_commit = store.calls + 2
        with redirect_stdout(io.StringIO()):
            result = main(["run"], self.env(), lambda _: store, NOW)
        self.assertEqual(result, 4)
        self.assertEqual(run_once(store, CONFIG, "b" * 32), "duplicate_completed")
        self.assertEqual(store.documents[CONFIG.run_key]["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
