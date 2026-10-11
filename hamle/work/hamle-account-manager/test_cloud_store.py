import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading
import unittest
from runtime_authority import AUTHORIZATION_DEADLINE

from cloud_store import OperationsStore, StoreError, action_id, proposal_hash, POLICY, LIMIT


class MemoryBackend:
    """Serializable fake, rollback, and Firestore's read-before-write rule."""
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()
        self.unknown_after_commit = False

    def transact(self, callback):
        with self.lock:
            data = copy.deepcopy(self.data)
            class Tx:
                wrote = False
                def get(self, key):
                    if self.wrote:
                        raise AssertionError("Firestore read after write")
                    return copy.deepcopy(data.get(key))
                def put(self, key, value):
                    self.wrote = True
                    data[key] = copy.deepcopy(value)
            result = callback(Tx())
            self.data = data
            if self.unknown_after_commit:
                self.unknown_after_commit = False
                raise StoreError("store_outcome_unknown")
            return copy.deepcopy(result)


def budget_snapshot():
    return {"policy_id": POLICY, "limit_microdollars": LIMIT,
            "expires_at": "2027-12-31T23:59:59+03:00", "overrun_hold": False,
            "providers": {p: {"baseline_microdollars": 0, "paid_microdollars": 10_000_000 if p == "openai" else 0,
                               "billing_ready": p != "visual_generation"} for p in ("google_cloud", "openai", "visual_generation")},
            "reservations": [
                {"reservation_key": "probe", "provider": "google_cloud", "reserved_microdollars": 3_000_000, "actual_microdollars": None, "state": "unknown", "evidence_ref": None},
                {"reservation_key": "infrastructure", "provider": "google_cloud", "reserved_microdollars": 5_000_000, "actual_microdollars": None, "state": "reserved", "evidence_ref": None},
                {"reservation_key": "first-astra", "provider": "openai", "reserved_microdollars": 300_000, "actual_microdollars": 215_710, "state": "settled", "evidence_ref": "openai-first-inference.json"}],
            "evidence_ref": "budget-export.json"}


def seed_bound_work(store, now, *, strategy=True):
    """Build migration evidence through the real store state machine."""
    from test_model import draft
    from worker import _envelope, SUBJECT
    analysis = draft(); analysis["brief"]["requested_deadline"] = None
    if strategy:
        analysis["intent"] = "complaint_strategy"
        analysis["escalation"] = {"owner": "cansu_and_ahmet", "reason": "Strateji insan değerlendirmesinde."}
    result = {"status": "review_required", "action_eligible": False, "model": "gpt-6-astra",
              "reasoning_effort": "max", "automatic_retry_allowed": False, "analysis": analysis}
    evidence = {"thread_id": "ab12", "message_id": "ab13", "snapshot_fingerprint": "a" * 64,
                "captured_at": now.isoformat(), "in_reply_to": "<root@example.test>", "references": "<root@example.test>"}
    payload = _envelope(result, evidence, {"todo_parent_id": 10384126975, "todo_parent_type": "Todoset"})
    g = store.control()["generation"]
    pid = "p-" + hashlib.sha256(b"ab12:ab13").hexdigest()
    p = store.put_proposal(pid, "a" * 64, payload, g)
    store.approve(pid, p["proposal_hash"], g, "subject", purpose="internal_coordination_only" if strategy else "routine_execution")
    for kind, provider_id in (("basecamp_todo", "10392247471"), ("gmail_reply", "ab14")):
        lease = store.claim_action(action_id(pid, kind), pid, kind, p["proposal_hash"], "seed", g)
        store.begin_dispatch(lease, "a" * 64); store.record_receipt(lease, provider_id)
        store.confirm_readback(lease["action_id"], provider_id)
    return store.bind_verified_work(pid)


def task_observation(work, now, **changes):
    row = {k: work[k] for k in ("account_id", "project_id", "task_id", "todo_parent_id", "todo_parent_type")}
    row.update(title="Q4 iç plan", description="İlk onaylı görevin açıklaması.", status="active", completed=False,
               due_on=None, assignee_ids=[53358100], updated_at=now.isoformat(), task_fingerprint="c" * 64,
               observed_at=now.isoformat(), identity_verified=True)
    row.update(changes)
    return row


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat("2026-10-10T02:00:00+03:00")
        self.backend = MemoryBackend()
        self.store = OperationsStore(self.backend, clock=lambda: self.now)
        self.store.initialize_control()
        self.store.initialize_budget(budget_snapshot())
        self.payload = {"actions": {"gmail_reply": {"to": ["ahmet@gamechangers.com.tr"], "body": "İç deneme"}, "basecamp_todo": {"project_id": "49184053"}}}
        self.source = "a" * 64

    def open(self):
        return self.store.resume_review_only("google-subject-ahmet")["generation"]

    def proposed(self):
        g = self.open()
        p = self.store.put_proposal("p1", self.source, self.payload, g)
        return g, p

    def approved(self):
        g, p = self.proposed()
        self.store.approve("p1", p["proposal_hash"], g, "google-subject-ahmet")
        return g, p

    def claimed(self):
        g, p = self.approved()
        lease = self.store.claim_action(action_id("p1", "gmail_reply"), "p1", "gmail_reply", p["proposal_hash"], "worker-1", g)
        return g, p, lease

    def fails(self, code, fn):
        with self.assertRaises(StoreError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def test_default_stopped_and_restart_does_not_reset(self):
        self.fails("control_stopped_or_stale", lambda: self.store.put_proposal("p1", self.source, self.payload, 0))
        self.open(); self.store.takeover("subject")
        other = OperationsStore(self.backend, clock=lambda: self.now)
        self.assertEqual(other.initialize_control()["mode"], "takeover")
        self.assertEqual(other.control()["generation"], 2)

    def test_proposal_is_immutable_and_duplicate_idempotent(self):
        g, p = self.proposed()
        self.assertEqual(self.store.put_proposal("p1", self.source, self.payload, g), p)
        self.fails("proposal_id_conflict", lambda: self.store.put_proposal("p1", "b" * 64, self.payload, g))
        self.fails("proposal_id_conflict", lambda: self.store.put_proposal("p1", self.source, {"changed": True}, g))

    def test_approval_requires_exact_hash_and_generation(self):
        g, p = self.proposed()
        self.fails("proposal_mismatch", lambda: self.store.approve("p1", "b" * 64, g, "subject"))
        self.fails("control_stopped_or_stale", lambda: self.store.approve("p1", p["proposal_hash"], g + 1, "subject"))
        self.fails("approval_required", lambda: self.store.claim_action(action_id("p1", "gmail_reply"), "p1", "gmail_reply", p["proposal_hash"], "w", g))

    def test_new_proposal_id_cannot_replay_same_source(self):
        g, _ = self.proposed()
        self.fails("source_already_proposed_no_retry", lambda: self.store.put_proposal("p2", self.source, self.payload, g))
        self.store.stop("subject"); g = self.open()
        self.fails("source_already_proposed_no_retry", lambda: self.store.put_proposal("p3", self.source, self.payload, g))

    def test_unlisted_action_kind_is_not_approved(self):
        g, p = self.approved()
        self.fails("action_not_in_approved_proposal", lambda: self.store.claim_action(action_id("p1", "basecamp_comment"), "p1", "basecamp_comment", p["proposal_hash"], "w", g))

    def test_duplicate_approval_does_not_extend_deadline(self):
        g, p = self.approved()
        old = self.store.public_state()["proposals"][0]["approval"]["approved_at"]
        self.now += timedelta(seconds=100)
        p = self.store.approve("p1", p["proposal_hash"], g, "subject")
        self.assertEqual(p["approval"]["approved_at"], old)

    def test_takeover_cancels_waiting_and_resume_never_resurrects(self):
        g, p, lease = self.claimed()
        self.store.takeover("subject")
        self.fails("control_stopped_or_stale", lambda: self.store.begin_dispatch(lease, self.source))
        self.assertEqual(self.store.public_state()["actions"][0]["state"], "cancelled")
        newg = self.open()
        self.fails("proposal_cancelled", lambda: self.store.put_proposal("p1", self.source, self.payload, newg))
        self.assertEqual(self.store.public_state()["proposals"][0]["status"], "cancelled")

    def test_old_generation_capture_rejected_after_stop_and_resume(self):
        g = self.open(); self.store.stop("subject"); self.open()
        self.fails("control_stopped_or_stale", lambda: self.store.put_proposal("p1", self.source, self.payload, g))

    def test_claim_is_canonical_and_one_chance_under_concurrency(self):
        g, p = self.approved()
        aid = action_id("p1", "gmail_reply")
        self.fails("noncanonical_action_id", lambda: self.store.claim_action("another", "p1", "gmail_reply", p["proposal_hash"], "w", g))
        def claim(i):
            try:
                return self.store.claim_action(aid, "p1", "gmail_reply", p["proposal_hash"], str(i), g)["state"]
            except StoreError as e:
                return e.code
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(claim, range(6)))
        self.assertEqual(results.count("leased"), 1)
        self.assertEqual(results.count("action_already_reserved_no_retry"), 5)

    def test_changed_source_blocks_dispatch(self):
        _, _, lease = self.claimed()
        self.fails("source_changed", lambda: self.store.begin_dispatch(lease, "b" * 64))

    def test_expired_lease_never_requeues(self):
        g, p, lease = self.claimed()
        self.now += timedelta(seconds=61)
        self.fails("action_not_dispatchable", lambda: self.store.begin_dispatch(lease, self.source))
        self.fails("action_already_reserved_no_retry", lambda: self.store.claim_action(lease["action_id"], "p1", "gmail_reply", p["proposal_hash"], "w2", g))

    def test_stale_approval_blocks_a_new_lease(self):
        g, p = self.approved()
        self.now += timedelta(seconds=301)
        lease = self.store.claim_action(action_id("p1", "gmail_reply"), "p1", "gmail_reply", p["proposal_hash"], "w", g)
        self.fails("approval_expired_or_mismatch", lambda: self.store.begin_dispatch(lease, self.source))

    def test_ambiguous_dispatch_held_no_resend(self):
        g, p, lease = self.claimed()
        self.store.begin_dispatch(lease, self.source)
        self.store.mark_unknown(lease)
        self.fails("action_not_dispatchable", lambda: self.store.begin_dispatch(lease, self.source))
        self.fails("action_already_reserved_no_retry", lambda: self.store.claim_action(lease["action_id"], "p1", "gmail_reply", p["proposal_hash"], "w2", g))

    def test_receipt_after_takeover_preserved_but_never_reopens_control(self):
        _, _, lease = self.claimed()
        self.store.begin_dispatch(lease, self.source)
        self.store.takeover("subject")
        self.assertEqual(self.store.public_state()["actions"][0]["state"], "unknown")
        self.store.record_receipt(lease, "provider-123")
        self.store.confirm_readback(lease["action_id"], "provider-123")
        s = self.store.public_state()
        self.assertEqual(s["actions"][0]["state"], "verified")
        self.assertEqual(s["control"]["mode"], "takeover")
        self.assertNotIn("token", s["actions"][0])

    def test_readback_requires_matching_receipt(self):
        _, _, lease = self.claimed()
        self.fails("readback_receipt_mismatch", lambda: self.store.confirm_readback(lease["action_id"], "x"))
        self.store.begin_dispatch(lease, self.source)
        self.store.record_receipt(lease, "x")
        self.fails("receipt_conflict", lambda: self.store.record_receipt(lease, "y"))
        self.fails("readback_receipt_mismatch", lambda: self.store.confirm_readback(lease["action_id"], "y"))

    def test_unknown_commit_is_reconciled_with_same_action_id(self):
        g, p = self.approved()
        self.backend.unknown_after_commit = True
        aid = action_id("p1", "gmail_reply")
        self.fails("store_outcome_unknown", lambda: self.store.claim_action(aid, "p1", "gmail_reply", p["proposal_hash"], "w", g))
        self.fails("action_already_reserved_no_retry", lambda: self.store.claim_action(aid, "p1", "gmail_reply", p["proposal_hash"], "w", g))
        self.assertEqual(len(self.store.public_state()["actions"]), 1)

    def test_settle_unknown_actual_only_for_unknown_anthropic_rows(self):
        self.store.budget_reserve("chain-model-x", "anthropic", 2_000_000)
        self.fails("settle_actual_requires_unknown", lambda: self.store.settle_unknown_actual("chain-model-x", 300_000, "console-usage"))
        self.store._budget_finish("chain-model-x", None, "sent-no-receipt")
        r = self.store.settle_unknown_actual("chain-model-x", 300_000, "console-usage")
        self.assertEqual((r["state"], r["actual_microdollars"]), ("settled", 300_000))
        self.assertEqual(self.store.settle_unknown_actual("chain-model-x", 300_000, "console-usage")["state"], "settled")
        self.fails("settlement_conflict", lambda: self.store.settle_unknown_actual("chain-model-x", 1, "console-usage"))
        self.fails("reservation_missing", lambda: self.store.settle_unknown_actual("nope", 1, "console-usage"))

    def test_extend_budget_expiry_moves_only_the_date_once(self):
        b = self.backend.data["budget"]
        b["expires_at"] = "2026-10-12T23:59:59+03:00"
        before = {k: v for k, v in b.items() if k != "expires_at"}
        r = self.store.extend_budget_expiry("test-evidence")
        after = self.backend.data["budget"]
        self.assertEqual((r["status"], after["expires_at"]), ("budget_expiry_extended", AUTHORIZATION_DEADLINE))
        self.assertEqual({k: v for k, v in after.items() if k not in ("expires_at", "expiry_history")}, before)
        self.assertEqual(self.store.extend_budget_expiry("test-evidence")["status"], "already_current")
        self.backend.data["budget"]["expires_at"] = "2026-11-01T00:00:00+03:00"
        self.fails("budget_expiry_unexpected", lambda: self.store.extend_budget_expiry("test-evidence"))

    def test_deadline_blocks_new_work_but_not_stop_or_evidence(self):
        _, _, lease = self.claimed(); self.store.begin_dispatch(lease, self.source)
        self.now = datetime.fromisoformat("2028-01-04T00:00:00+03:00")
        self.fails("authorization_expired", self.open)
        self.fails("authorization_expired", lambda: self.store.budget_reserve("late", "openai", 1))
        self.store.stop("subject")
        self.store.record_receipt(lease, "receipt")
        self.store.confirm_readback(lease["action_id"], "receipt")
        self.store.budget_settle("probe", 10, "late-evidence.json")

    def test_clock_regression_does_not_block_takeover(self):
        self.open(); self.now -= timedelta(days=3)
        self.assertEqual(self.store.takeover("subject")["mode"], "takeover")

    def test_budget_import_preserves_prepaid_and_holds(self):
        b = self.store.budget_status()
        self.assertEqual(b["accounted_spend_microdollars"], 215_710)
        self.assertEqual(b["held_microdollars"], 8_000_000)
        self.assertEqual(b["total_committed_microdollars"], 18_000_000)
        self.assertEqual(b["remaining_microdollars"], 32_000_000)

    def test_budget_cannot_be_reset_and_same_import_does_not_erase_new_reservations(self):
        self.store.budget_reserve("another", "openai", 1_000_000)
        self.store.initialize_budget(budget_snapshot())
        self.assertIn("another", [r["reservation_key"] for r in self.backend.data["budget"]["reservations"]])
        changed = budget_snapshot(); changed["reservations"] = []
        self.fails("budget_already_imported_no_reset", lambda: self.store.initialize_budget(changed))

    def test_budget_overlap_prepaid_not_double_counted(self):
        self.store.budget_reserve("model-2", "openai", 2_000_000)
        self.assertEqual(self.store.budget_status()["total_committed_microdollars"], 18_000_000)
        g = self.open(); self.store.budget_begin("model-2", g)
        self.store.budget_settle("model-2", 200_000, "model-2.json")
        self.assertEqual(self.store.budget_status()["total_committed_microdollars"], 18_000_000)

    def test_credential_refresh_claim_permanent_across_instances(self):
        self.assertEqual(self.store.control()["mode"], "stopped")
        self.assertIs(self.store.claim_credential_refresh("c" * 64), True)
        other = OperationsStore(self.backend, clock=lambda: self.now)
        self.assertIs(other.claim_credential_refresh("c" * 64), False)
        self.assertIs(other.claim_credential_refresh("d" * 64), True)

    def test_budget_racing_reservations_cannot_overrun(self):
        def reserve(i):
            try:
                self.store.budget_reserve("cloud-" + str(i), "google_cloud", 20_000_000)
                return "ok"
            except StoreError as e:
                return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(reserve, range(2))), ["ok", "aggregate_budget_exhausted"])

    def test_budget_unknown_stays_held_and_begin_is_single_attempt(self):
        g = self.open(); self.store.budget_reserve("call", "openai", 100_000)
        self.store.budget_begin("call", g)
        self.fails("budget_call_not_replayable", lambda: self.store.budget_begin("call", g))
        self.store.budget_unknown("call")
        self.fails("budget_call_not_replayable", lambda: self.store.budget_begin("call", g))
        self.assertEqual(self.store.budget_status()["held_microdollars"], 8_100_000)

    def test_actual_overrun_freezes_future_reservations(self):
        self.store.budget_settle("infrastructure", 5_000_001, "cloud.json")
        self.fails("budget_not_ready", lambda: self.store.budget_reserve("later", "openai", 1))

    def test_model_receipt_survives_stop_expiry_without_settling_budget(self):
        g = self.open(); self.store.budget_reserve("model-call", "openai", 1_000_000)
        self.store.budget_begin("model-call", g)
        receipt = {"response_id": "resp_fake", "model": "gpt-6-astra", "reasoning_effort": "max",
                   "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150,
                             "output_tokens_details": {"reasoning_tokens": 30}}}
        self.store.stop("subject")
        self.now = datetime.fromisoformat("2026-10-13T00:00:00+03:00")
        before = self.store.budget_status()
        record = self.store.record_model_receipt("model-call", receipt)
        self.assertEqual(self.store.model_receipt("model-call"), record)
        self.assertEqual(self.store.record_model_receipt("model-call", receipt), record)
        self.assertEqual(self.store.budget_status(), before)
        self.assertEqual(self.store.control()["mode"], "stopped")
        receipt["response_id"] = "resp_other"
        self.fails("model_receipt_conflict", lambda: self.store.record_model_receipt("model-call", receipt))

    def test_model_receipt_rejects_extra_content_invalid_usage_or_unstarted_call(self):
        receipt = {"response_id": "resp_fake", "model": "gpt-6-astra", "reasoning_effort": "max",
                   "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}}
        self.store.budget_reserve("model-call", "openai", 1_000_000)
        self.fails("model_reservation_not_started", lambda: self.store.record_model_receipt("model-call", receipt))
        bad = copy.deepcopy(receipt); bad["raw_reasoning"] = "must-not-persist"
        self.fails("model_receipt_invalid", lambda: self.store.record_model_receipt("model-call", bad))
        bad = copy.deepcopy(receipt); bad["usage"]["input_tokens"] = True
        self.fails("model_usage_invalid", lambda: self.store.record_model_receipt("model-call", bad))
        bad = copy.deepcopy(receipt); bad["usage"]["output_tokens_details"] = {"reasoning_tokens": 51}
        self.fails("model_usage_invalid", lambda: self.store.record_model_receipt("model-call", bad))

    def test_budget_billing_false_and_invalid_money_rejected(self):
        self.fails("budget_not_ready", lambda: self.store.budget_reserve("visual", "visual_generation", 1))
        for amount in (True, 0.1, -1):
            self.fails("invalid_microdollars", lambda: self.store.budget_reserve("bad", "openai", amount))

    def test_snapshot_validation_rejects_duplicate_money_and_changed_limit(self):
        b = budget_snapshot(); b["reservations"].append(copy.deepcopy(b["reservations"][0]))
        self.fails("budget_reservation_invalid", lambda: self.store.initialize_budget(b))
        b = budget_snapshot(); b["limit_microdollars"] = LIMIT + 1
        self.fails("budget_policy_mismatch", lambda: self.store.initialize_budget(b))

    def test_stop_versus_dispatch_has_one_serialized_boundary(self):
        _, _, lease = self.claimed()
        def dispatch():
            try:
                return self.store.begin_dispatch(lease, self.source)["state"]
            except StoreError as e:
                return e.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            f = pool.submit(dispatch)
            pool.submit(self.store.stop, "subject").result()
            self.assertIn(f.result(), ("dispatch_started", "control_stopped_or_stale"))
        self.assertIn(self.store.public_state()["actions"][0]["state"], ("unknown", "cancelled"))

    def test_optional_half_hour_cadence_claims_21_istanbul_weekday_slots(self):
        g = self.open()
        start = datetime.fromisoformat("2026-10-12T08:30:00+03:00")
        keys = []
        for i in range(21):
            self.now = start + timedelta(minutes=30 * i, seconds=3)
            claim = self.store.claim_scheduled_slot(g, interval_minutes=30)
            keys.append(claim["slot_key"])
            self.assertIsNone(self.store.claim_scheduled_slot(g))
        self.assertEqual(len(set(keys)), 21)
        self.assertTrue(keys[0].endswith("0830")); self.assertTrue(keys[-1].endswith("1830"))
        self.now = start + timedelta(minutes=30 * 21)
        self.fails("outside_scheduled_window", lambda: self.store.claim_scheduled_slot(g))

    def test_scheduler_rejects_weekend_early_late_and_expired_slots(self):
        g = self.open()
        for stamp in ("2026-10-10T08:30:00+03:00", "2026-10-11T09:00:00+03:00",
                      "2026-10-12T08:29:59+03:00", "2026-10-12T08:35:00+03:00",
                      "2026-10-12T19:00:00+03:00"):
            self.now = datetime.fromisoformat(stamp)
            self.fails("outside_scheduled_window", lambda: self.store.claim_scheduled_slot(g))
        self.now = datetime.fromisoformat("2028-01-04T08:30:00+03:00")
        self.fails("authorization_expired", lambda: self.store.claim_scheduled_slot(g))
        self.assertFalse(any(key.startswith("slot-") for key in self.backend.data))

    def test_scheduler_duplicate_race_and_generation_change_never_reclaim(self):
        g = self.open(); self.now = datetime.fromisoformat("2026-10-12T08:30:01+03:00")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.claim_scheduled_slot(g), range(4)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.store.stop("subject"); g2 = self.open()
        self.assertIsNone(self.store.claim_scheduled_slot(g2))

    def test_scheduler_unknown_claim_commit_is_held_and_outcome_can_arrive_after_expiry(self):
        g = self.open(); self.now = datetime.fromisoformat("2026-10-12T18:30:01+03:00")
        self.backend.unknown_after_commit = True
        self.fails("store_outcome_unknown", lambda: self.store.claim_scheduled_slot(g))
        self.assertIsNone(self.store.claim_scheduled_slot(g))
        claim = self.backend.data["slot-20261012T1830"]
        self.now = datetime.fromisoformat("2026-10-13T00:00:00+03:00")
        outcome = self.store.finish_scheduled_slot(claim, status="held", reason="authorization_expired")
        self.assertEqual(outcome["state"], "finished")
        self.assertEqual(self.store.finish_scheduled_slot(claim, status="held", reason="authorization_expired"), outcome)
        self.fails("scheduled_outcome_conflict", lambda: self.store.finish_scheduled_slot(claim, status="no_new_work"))

    def test_scheduler_requires_open_control_and_available_shared_budget(self):
        self.now = datetime.fromisoformat("2026-10-12T08:30:00+03:00")
        self.fails("control_stopped_or_stale", lambda: self.store.claim_scheduled_slot(0))
        g = self.open(); self.backend.data["budget"]["overrun_hold"] = True
        self.fails("budget_not_ready", lambda: self.store.claim_scheduled_slot(g))

    def test_scheduler_same_instant_in_utc_uses_the_same_istanbul_slot(self):
        g = self.open(); self.now = datetime.fromisoformat("2026-10-12T08:34:59+03:00")
        claim = self.store.claim_scheduled_slot(g)
        self.now = self.now.astimezone(timezone.utc)
        self.assertIsNone(self.store.claim_scheduled_slot(g))
        self.assertEqual(claim["scheduled_at"], "2026-10-12T08:30:00+03:00")

    def strategy_proposal(self):
        payload = {"scope": "gamechangers_internal", "analysis": {
            "intent": "complaint_strategy", "escalation": {"owner": "cansu_and_ahmet"}},
            "reply": {"from": "hamle@gamechangers.com.tr", "to": ["ahmet@gamechangers.com.tr"],
                      "cc": [], "subject": "Re: HAMLE-IC-IS-20261010-01"},
            "task": {"account_id": 5898865, "project_id": 49184053, "assignee_id": 53358100, "due_at": None}}
        g = self.open()
        p = self.store.put_proposal("strategy-p1", self.source, payload, g)
        return g, p

    def test_coordination_requires_explicit_purpose_without_changing_proposal(self):
        g, p = self.strategy_proposal()
        self.fails("human_escalation_required", lambda: self.store.approve("strategy-p1", p["proposal_hash"], g, "subject"))
        approved = self.store.approve("strategy-p1", p["proposal_hash"], g, "subject", purpose="internal_coordination_only")
        self.assertEqual(approved["payload"], p["payload"])
        self.assertEqual(approved["proposal_hash"], p["proposal_hash"])
        self.assertEqual(approved["approval"]["purpose"], "internal_coordination_only")
        self.assertEqual(approved["approval"]["escalation_status"], "open")
        self.assertIs(approved["approval"]["decision_authorized"], False)
        self.assertIs(approved["approval"]["production_authorized"], False)
        self.now += timedelta(seconds=20)
        again = self.store.approve("strategy-p1", p["proposal_hash"], g, "subject", purpose="internal_coordination_only")
        self.assertEqual(again["approval"], approved["approval"])

    def test_existing_legacy_approval_cannot_be_upgraded_to_coordination(self):
        g, p = self.strategy_proposal()
        row = self.backend.data["proposal-strategy-p1"]
        row.update(status="approved", approval={"actor_subject": "subject", "approved_at": self.now.timestamp(),
                   "proposal_hash": p["proposal_hash"], "generation": g})
        self.fails("approval_purpose_conflict", lambda: self.store.approve("strategy-p1", p["proposal_hash"], g, "subject",
                                                                          purpose="internal_coordination_only"))
        self.assertNotIn("purpose", self.backend.data["proposal-strategy-p1"]["approval"])

    def test_coordination_purpose_does_not_open_commercial_or_other_destinations(self):
        g, p = self.strategy_proposal()
        row = self.backend.data["proposal-strategy-p1"]
        original = copy.deepcopy(row["payload"])
        for field, value in (("intent", "commercial"), ("recipient", "cansu@example.test"),
                             ("deadline", "2026-10-12"), ("project", 12345)):
            payload = copy.deepcopy(original)
            if field == "intent": payload["analysis"]["intent"] = value
            if field == "recipient": payload["reply"]["to"] = [value]
            if field == "deadline": payload["task"]["due_at"] = value
            if field == "project": payload["task"]["project_id"] = value
            row["payload"] = payload; row["proposal_hash"] = proposal_hash(payload)
            self.fails("coordination_scope_invalid", lambda: self.store.approve("strategy-p1", row["proposal_hash"], g, "subject",
                                                                                purpose="internal_coordination_only"))
        self.assertIsNone(self.backend.data["proposal-strategy-p1"]["approval"])


class FollowupStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat("2026-10-10T03:00:00+03:00")
        self.backend = MemoryBackend(); self.store = OperationsStore(self.backend, clock=lambda: self.now)
        self.store.initialize_control(); self.store.initialize_budget(budget_snapshot())
        self.g = self.store.resume_review_only("subject")["generation"]
        self.work = seed_bound_work(self.store, self.now)
        self.work = self.store.record_work_observation(self.work["work_id"], task_observation(self.work, self.now))

    def fails(self, code, call):
        with self.assertRaises(StoreError) as raised: call()
        self.assertEqual(raised.exception.code, code)

    def proposed(self, *, intent="routine_request", owner="none", cc=None):
        from test_model import draft
        from worker import _followup_envelope, HAMLE
        analysis = draft(); analysis["brief"]["requested_deadline"] = None
        analysis["intent"] = intent; analysis["escalation"] = {"owner": owner, "reason": None}
        result = {"status": "review_required", "action_eligible": False, "model": "gpt-6-astra",
                  "reasoning_effort": "max", "automatic_retry_allowed": False, "analysis": analysis}
        evidence = {"thread_id": "ab12", "message_id": "ab15", "snapshot_fingerprint": "d" * 64,
                    "captured_at": self.now.isoformat(), "in_reply_to": "<followup@example.test>",
                    "references": "<root@example.test> <followup@example.test>", "latest_to": [HAMLE], "latest_cc": cc or []}
        claim = self.store.claim_followup(self.work["work_id"], "ab15", "b" * 64, self.g)
        payload = _followup_envelope(result, evidence, self.work)
        return self.store.put_followup_proposal(claim, payload)

    def test_seed_binding_comes_only_from_two_verified_receipts_and_is_idempotent(self):
        self.assertEqual(self.store.work_for_thread("ab12"), self.work)
        self.assertEqual(self.store.bind_verified_work(self.work["seed_proposal_id"]), self.work)
        self.assertIsNone(self.store.work_for_thread("ffff"))
        self.backend.data["action-" + action_id(self.work["seed_proposal_id"], "gmail_reply")]["state"] = "accepted"
        self.fails("verified_seed_required", lambda: self.store.bind_verified_work(self.work["seed_proposal_id"]))

    def test_single_pending_followup_and_permanent_claim_survive_concurrency(self):
        def claim(_):
            try: return self.store.claim_followup(self.work["work_id"], "ab15", "b" * 64, self.g)["state"]
            except StoreError as exc: return exc.code
        with ThreadPoolExecutor(max_workers=4) as pool: outcomes = list(pool.map(claim, range(4)))
        self.assertEqual(outcomes.count("claimed"), 1)
        self.assertEqual(outcomes.count("followup_already_reserved_no_retry"), 3)
        self.fails("previous_followup_unresolved", lambda: self.store.claim_followup(self.work["work_id"], "ab16", "d" * 64, self.g))

    def test_unknown_claim_commit_or_model_failure_never_releases_pending_event(self):
        self.backend.unknown_after_commit = True
        self.fails("store_outcome_unknown", lambda: self.store.claim_followup(self.work["work_id"], "ab15", "b" * 64, self.g))
        self.fails("followup_already_reserved_no_retry", lambda: self.store.claim_followup(self.work["work_id"], "ab15", "b" * 64, self.g))
        event = self.backend.data[self.store.get_work(self.work["work_id"])["pending_event_id"]]
        self.store.hold_followup(event, "transport_outcome_unknown")
        self.fails("previous_followup_unresolved", lambda: self.store.claim_followup(self.work["work_id"], "ab16", "d" * 64, self.g))

    def test_inherited_strategy_stays_human_gated_when_new_model_classifies_routine(self):
        p = self.proposed(cc=["cansu@gamechangers.com.tr"])
        self.assertNotIn("task", p["payload"])
        self.assertEqual(p["payload"]["comment"]["task_id"], 10392247471)
        self.fails("human_escalation_required", lambda: self.store.approve(p["proposal_id"], p["proposal_hash"], self.g, "subject"))
        approved = self.store.approve(p["proposal_id"], p["proposal_hash"], self.g, "subject", purpose="internal_coordination_only")
        self.assertEqual(approved["payload"]["analysis"]["intent"], "routine_request")
        self.assertIs(approved["approval"]["production_authorized"], False)
        self.fails("action_not_in_approved_proposal", lambda: self.store.claim_action(action_id(p["proposal_id"], "basecamp_todo"), p["proposal_id"], "basecamp_todo", p["proposal_hash"], "w", self.g))

    def test_commercial_followup_is_not_coordination(self):
        p = self.proposed(intent="commercial", owner="ahmet")
        self.fails("coordination_scope_invalid", lambda: self.store.approve(p["proposal_id"], p["proposal_hash"], self.g, "subject", purpose="internal_coordination_only"))

    def test_completion_requires_both_readbacks_and_replay_does_not_change_binding(self):
        p = self.proposed(); pid = p["proposal_id"]
        self.store.approve(pid, p["proposal_hash"], self.g, "subject", purpose="internal_coordination_only")
        for kind, provider_id in (("basecamp_comment", "123456"), ("gmail_reply", "ab17")):
            lease = self.store.claim_action(action_id(pid, kind), pid, kind, p["proposal_hash"], "w", self.g)
            self.store.begin_dispatch(lease, p["source_fingerprint"])
            self.store.record_receipt(lease, provider_id)
            self.fails("followup_readbacks_required", lambda: self.store.complete_followup(self.work["work_id"], pid))
            self.store.confirm_readback(lease["action_id"], provider_id)
        completed = self.store.complete_followup(self.work["work_id"], pid)
        self.assertEqual(completed["revision_count"], 1); self.assertEqual(completed["version"], 1)
        self.assertIsNone(completed["pending_event_id"])
        self.assertEqual(completed["last_reply_message_id"], "ab17")
        self.assertEqual(self.store.complete_followup(self.work["work_id"], pid), completed)
        self.assertEqual(self.store.bind_verified_work(self.work["seed_proposal_id"]), completed)
        self.store.claim_followup(self.work["work_id"], "ab18", "e" * 64, self.g)

    def test_closed_or_wrong_task_observation_blocks_followup(self):
        self.fails("work_observation_scope_invalid", lambda: self.store.record_work_observation(self.work["work_id"], task_observation(self.work, self.now, task_id=999)))
        self.store.record_work_observation(self.work["work_id"], task_observation(self.work, self.now, completed=True))
        self.fails("work_not_active", lambda: self.store.claim_followup(self.work["work_id"], "ab15", "b" * 64, self.g))

    def test_every_quarter_hour_has_41_weekday_slots_no_half_hour_double_claim(self):
        self.now = datetime.fromisoformat("2026-10-12T08:30:00+03:00")
        keys = []
        for _ in range(41):
            claim = self.store.claim_scheduled_slot(self.g)
            keys.append(claim["slot_key"])
            self.assertIsNone(self.store.claim_scheduled_slot(self.g))
            if self.now.minute in (0, 30): self.assertIsNone(self.store.claim_scheduled_slot(self.g, interval_minutes=30))
            self.now += timedelta(minutes=15)
        self.assertEqual(len(set(keys)), 41)
        self.fails("outside_scheduled_window", lambda: self.store.claim_scheduled_slot(self.g))


if __name__ == "__main__":
    unittest.main()
