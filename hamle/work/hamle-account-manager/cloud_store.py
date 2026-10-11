"""Bounded internal workflow ledger; no provider call or identity authentication.

The caller must verify IAP identity, current Gmail/To/CC authority and Basecamp
scope. This store fences pending work, not a request already accepted upstream.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

PROJECT = "gamechangers-hamle"
DATABASE = "hamle-operations"
POLICY = "hamle-shared-20261009-20261012"
from runtime_authority import (AUTHORIZATION_DEADLINE, SCHEDULED_REVIEW_WINDOW, deadline_timestamp,
                               start_timestamp, window_minutes)

START = start_timestamp()
DEADLINE = deadline_timestamp()  # single source: runtime_authority.AUTHORIZATION_DEADLINE
PROVIDERS = ("google_cloud", "openai", "visual_generation", "anthropic")
LEGACY_PROVIDERS = ("google_cloud", "openai", "visual_generation")  # budgets imported before Claude
# Claude API prepaid credit loaded by Ahmet on 2026-10-11 (20 USD). Added to an already
# imported budget on the first anthropic reservation; prepaid counts as committed money.
ANTHROPIC_PROVIDER_SEED = {"baseline_microdollars": 0, "paid_microdollars": 20_000_000, "billing_ready": True}
ANTHROPIC_SEED_EVIDENCE = "owner-thread-2026-10-11-anthropic-prepaid-20usd"
ANTHROPIC_MODELS = ("claude-opus-5-5", "claude-haiku-5-5")
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")
ANTHROPIC_USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                          "cache_creation_input_tokens", "web_search_requests")
KINDS = ("gmail_reply", "basecamp_todo", "basecamp_comment")
LIMIT = 50_000_000  # per Istanbul calendar month (budget["period"]); renews by itself
ISTANBUL = ZoneInfo("Europe/Istanbul")
# Ledger compaction: the budget doc must stay under _json's 60 KB record limit (~170 rows).
BUDGET_COMPACT_AT = 100        # current-month settled rows are archived from this many rows on
BUDGET_COMPACT_MIN_AGE = 3600  # settled rows younger than this stay (settle replays find them)
BUDGET_ARCHIVE_CHUNK = 80
SETTLE_PROVIDERS = ("anthropic", "openai", "google_cloud")
WORK_REF_FIELDS = ("work_id", "thread_id", "task_id", "account_id", "project_id", "version",
                   "escalation_status", "escalation_owner")


def budget_period(now):
    """Istanbul calendar month 'YYYY-MM' (Ahmet 2026-10-11: monthly cap that renews by itself)."""
    return datetime.fromtimestamp(now, ISTANBUL).strftime("%Y-%m")


def _in_period(b, r):
    # Rows written before periods existed belong to the budget's current period until a roll stamps them.
    return r.get("period", b.get("period")) == b.get("period")


def _anthropic_receipt(r, *, failure):
    """Claude receipt: usage from response.usage and the cost computed from it (not an invoice)."""
    keys = {"provider", "response_id", "model", "effort", "usage", "cost_microdollars"} | ({"stop_reason"} if failure else set())
    _require(isinstance(r, dict) and set(r) == keys, "model_receipt_invalid")
    rid_ok = isinstance(r["response_id"], str) and re.fullmatch(r"msg_[A-Za-z0-9_-]{1,200}", r["response_id"])
    _require(rid_ok or (failure and r["response_id"] is None), "model_receipt_invalid")
    _require(r["model"] in ANTHROPIC_MODELS or (failure and r["model"] is None), "model_receipt_invalid")
    _require(r["effort"] in ANTHROPIC_EFFORTS, "model_receipt_invalid")
    _require(not failure or r["stop_reason"] in (None, "end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn",
                                                 "refusal", "model_context_window_exceeded"), "model_receipt_invalid")
    u = r["usage"]
    if u is None:
        _require(failure and r["cost_microdollars"] is None, "model_usage_invalid")
    else:
        _require(isinstance(u, dict) and set(u) == set(ANTHROPIC_USAGE_FIELDS)
                 and all(type(u[k]) is int and 0 <= u[k] <= 2_000_000 for k in ANTHROPIC_USAGE_FIELDS), "model_usage_invalid")
        _money(r["cost_microdollars"])
    return r


class StoreError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(condition, code):
    if not condition:
        raise StoreError(code)


def _label(value):
    _require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:@-]{1,180}", value), "invalid_identifier")
    return value


def _hash(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "invalid_hash")
    return value


def _money(value):
    _require(type(value) is int and 0 <= value <= 9_000_000_000_000_000, "invalid_microdollars")
    return value


def _json(value):
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        _require(len(raw.encode()) <= 60_000, "record_too_large")
        return raw
    except (TypeError, ValueError, RecursionError):
        raise StoreError("invalid_json_record") from None


def proposal_hash(payload):
    _require(isinstance(payload, dict), "proposal_object_required")
    return hashlib.sha256(_json(payload).encode()).hexdigest()


def action_id(proposal_id, kind):
    _label(proposal_id)
    _require(kind in KINDS, "invalid_action_kind")
    return hashlib.sha256((proposal_id + ":" + kind).encode()).hexdigest()


class _FirestoreTx:
    def __init__(self, collection, transaction):
        self.collection, self.transaction = collection, transaction

    def get(self, key):
        row = self.collection.document(key).get(transaction=self.transaction, retry=None, timeout=10)
        return row.to_dict() if row.exists else None

    def put(self, key, value):
        self.transaction.set(self.collection.document(key), value)


class FirestoreBackend:
    """Explicit fixed project/database, single application transaction attempt."""
    def __init__(self, *, project=PROJECT, database=DATABASE, opt_in, credentials=None):
        _require((project, database, opt_in) == (PROJECT, DATABASE, "yes"), "cloud_opt_in_required")
        from google.cloud import firestore
        self.sdk = firestore
        self.client = firestore.Client(project=project, database=database, credentials=credentials)
        self.collection = self.client.collection("hamle_internal_v1")

    def transact(self, callback):
        @self.sdk.transactional
        def apply(tx):
            return callback(_FirestoreTx(self.collection, tx))
        try:
            return apply(self.client.transaction(max_attempts=1))
        except StoreError:
            raise
        except Exception:
            # Caller must reconcile stable identifiers, never blindly retry a write.
            raise StoreError("store_outcome_unknown") from None

    def close(self):
        self.client.close()


class OperationsStore:
    MAX_PROPOSALS = 50
    MAX_RESERVATIONS = 200

    def __init__(self, backend, *, clock=None):
        self.backend = backend
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self, *, allow_expired=False):
        stamp = self.clock()
        _require(isinstance(stamp, datetime) and stamp.tzinfo is not None and stamp.utcoffset() is not None, "aware_clock_required")
        now = stamp.timestamp()
        if not allow_expired:
            _require(START <= now <= DEADLINE, "authorization_expired")
        return now

    def _run(self, callback, *, allow_expired=False):
        # Fresh clock at transaction execution, not at request construction time.
        return self.backend.transact(lambda tx: callback(tx, self._now(allow_expired=allow_expired)))

    @staticmethod
    def _control(tx):
        c = tx.get("control")
        _require(isinstance(c, dict) and c.get("mode") in ("stopped", "takeover", "review_open") and type(c.get("generation")) is int, "control_missing_or_invalid")
        return c

    @staticmethod
    def _open(c, generation, now):
        _require(type(generation) is int and generation == c["generation"] and c["mode"] == "review_open", "control_stopped_or_stale")
        _require(now >= c["changed_at"], "clock_before_control")

    def initialize_control(self):
        def op(tx, now):
            old = tx.get("control")
            if old is not None:
                return self._control(tx)
            c = {"mode": "stopped", "generation": 0, "changed_at": now, "actor_subject": "bootstrap", "proposal_ids": []}
            tx.put("control", c)
            return c
        return self._run(op)

    def control(self):
        return self._run(lambda tx, _: self._control(tx), allow_expired=True)

    def set_mode(self, mode, actor_subject, expected_generation=None):
        _require(mode in ("review_open", "stopped", "takeover"), "invalid_mode")
        _label(actor_subject)
        def op(tx, now):
            c = self._control(tx)
            if expected_generation is not None:
                _require(type(expected_generation) is int and expected_generation == c["generation"], "stale_generation")
            # Stop always wins, including clock regression/authorization expiry.
            if mode == "review_open":
                _require(now >= c["changed_at"], "clock_before_control")
            c.update(mode=mode, generation=c["generation"] + 1,
                     changed_at=max(now, c["changed_at"]), actor_subject=actor_subject)
            tx.put("control", c)
            return c
        return self._run(op, allow_expired=mode != "review_open")

    def stop(self, actor_subject):
        return self.set_mode("stopped", actor_subject)

    def takeover(self, actor_subject):
        return self.set_mode("takeover", actor_subject)

    def resume_review_only(self, actor_subject):
        return self.set_mode("review_open", actor_subject)

    def claim_scheduled_slot(self, expected_generation, *, interval_minutes=15, window=SCHEDULED_REVIEW_WINDOW):
        """One review attempt per current Istanbul slot, with five-minute grace.

        Uses trusted server time, never caller-supplied dates. A lost result or
        crash retains the claim; no catch-up, requeue or new-generation replay.
        """
        _require(type(interval_minutes) is int and interval_minutes in (15, 30), "scheduled_interval_invalid")
        try:
            first, last = window_minutes(window)
        except Exception:
            raise StoreError("scheduled_window_invalid") from None
        _require(first % interval_minutes == 0 and last % interval_minutes == 0, "scheduled_window_invalid")
        def op(tx, now):
            c = self._control(tx); b = self._budget(tx)
            self._open(c, expected_generation, now)
            local = datetime.fromtimestamp(now, ISTANBUL)
            slot = local.replace(minute=(local.minute // interval_minutes) * interval_minutes, second=0, microsecond=0)
            minute = slot.hour * 60 + slot.minute
            _require(slot.weekday() < 5 and first <= minute <= last
                     and 0 <= now - slot.timestamp() < 300, "outside_scheduled_window")
            _require(not b["overrun_hold"] and b["providers"]["google_cloud"]["billing_ready"]
                     and self._totals(b)["total_committed_microdollars"] <= LIMIT, "budget_not_ready")
            key = "slot-" + slot.strftime("%Y%m%dT%H%M")
            if tx.get(key) is not None:
                return None
            row = {"slot_key": key, "scheduled_at": slot.isoformat(), "claimed_at": now,
                   "generation": c["generation"], "token": uuid.uuid4().hex, "state": "claimed",
                   "interval_minutes": interval_minutes}
            tx.put(key, row)
            return row
        return self._run(op)

    def finish_scheduled_slot(self, claim, *, status, reason=None, proposal_id=None):
        """Persist a small outcome even after stop/expiry; never release a slot."""
        _require(isinstance(claim, dict), "scheduled_claim_invalid")
        key = _label(claim.get("slot_key"))
        _require(re.fullmatch(r"slot-[0-9]{8}T[0-9]{4}", key), "scheduled_claim_invalid")
        _require(status in ("stopped", "review_required", "already_recorded", "no_new_work", "held"), "scheduled_outcome_invalid")
        if reason is not None: _label(reason)
        if proposal_id is not None: _label(proposal_id)
        outcome = {"status": status, "reason": reason, "proposal_id": proposal_id}
        def op(tx, now):
            old = tx.get(key)
            _require(old is not None and all(old.get(k) == claim.get(k) for k in
                     ("slot_key", "token", "generation")), "scheduled_claim_invalid")
            if old["state"] == "finished":
                _require(old["outcome"] == outcome, "scheduled_outcome_conflict")
                return old
            _require(old["state"] == "claimed", "scheduled_claim_invalid")
            old.update(state="finished", finished_at=now, outcome=outcome)
            tx.put(key, old)
            return old
        return self._run(op, allow_expired=True)

    def put_proposal(self, proposal_id, source_fingerprint, payload, expected_generation):
        _label(proposal_id); _hash(source_fingerprint)
        digest = proposal_hash(payload)
        def op(tx, now):
            c = self._control(tx)
            old = tx.get("proposal-" + proposal_id)
            source = tx.get("source-" + source_fingerprint)
            self._open(c, expected_generation, now)
            if old is not None:
                _require(old["proposal_hash"] == digest and old["source_fingerprint"] == source_fingerprint, "proposal_id_conflict")
                _require(old["generation"] == c["generation"], "proposal_cancelled")
                return old
            _require(source is None, "source_already_proposed_no_retry")
            _require(len(c["proposal_ids"]) < self.MAX_PROPOSALS, "internal_queue_full")
            row = {"proposal_id": proposal_id, "proposal_hash": digest, "source_fingerprint": source_fingerprint,
                   "payload": copy.deepcopy(payload), "generation": c["generation"], "created_at": now,
                   "status": "awaiting_human_review", "approval": None}
            c["proposal_ids"].append(proposal_id)
            tx.put("control", c); tx.put("proposal-" + proposal_id, row)
            tx.put("source-" + source_fingerprint, {"proposal_id": proposal_id, "created_at": now})
            return row
        return self._run(op)

    def bind_verified_work(self, seed_proposal_id):
        """Migrate a verified initial task/reply into one immutable thread binding.

        No provider ID is accepted from the caller. Both IDs come from durable
        readback receipts. Repeating this operation never resets later progress.
        """
        _label(seed_proposal_id)
        def op(tx, now):
            c = self._control(tx)
            p = tx.get("proposal-" + seed_proposal_id)
            todo = tx.get("action-" + action_id(seed_proposal_id, "basecamp_todo"))
            mail = tx.get("action-" + action_id(seed_proposal_id, "gmail_reply"))
            _require(p is not None and p.get("approval") is not None, "verified_seed_required")
            payload = p["payload"]; task = payload.get("task", {}); reply = payload.get("reply", {})
            source = payload.get("source", {})
            _require(payload.get("scope") == "gamechangers_internal"
                     and task.get("account_id") == 5898865 and task.get("project_id") == 49184053
                     and task.get("assignee_id") == 53358100 and task.get("due_at") is None
                     and type(task.get("todo_parent_id")) is int and task["todo_parent_id"] > 0
                     and task.get("todo_parent_type") in ("Todoset", "Todolist")
                     and reply.get("from") == "hamle@gamechangers.com.tr"
                     and reply.get("to") == ["ahmet@gamechangers.com.tr"] and reply.get("cc") == []
                     and reply.get("subject") == "Re: HAMLE-IC-IS-20261010-01"
                     and reply.get("thread_id") == source.get("thread_id"), "verified_seed_scope_invalid")
            for field in ("thread_id", "message_id"):
                _require(isinstance(source.get(field), str) and re.fullmatch(r"[0-9a-f]{1,64}", source[field]), "verified_seed_scope_invalid")
            _require(isinstance(reply.get("in_reply_to"), str)
                     and re.fullmatch(r"<[^<>\s]{1,240}@[^<>\s]{1,240}>", reply["in_reply_to"]), "verified_seed_scope_invalid")
            for row, kind in ((todo, "basecamp_todo"), (mail, "gmail_reply")):
                _require(isinstance(row, dict) and row.get("state") == "verified"
                         and row.get("proposal_id") == seed_proposal_id
                         and row.get("action_id") == action_id(seed_proposal_id, kind)
                         and row.get("payload_hash") == p["proposal_hash"] == proposal_hash(payload), "verified_seed_required")
            _require(isinstance(todo.get("provider_id"), str) and re.fullmatch(r"[1-9][0-9]{0,19}", todo["provider_id"])
                     and isinstance(mail.get("provider_id"), str) and re.fullmatch(r"[0-9a-f]{1,64}", mail["provider_id"]), "verified_seed_receipt_invalid")
            thread = source["thread_id"]
            wid = "w-" + hashlib.sha256(("5898865:49184053:" + thread).encode()).hexdigest()
            index_key = "thread-" + hashlib.sha256(thread.encode()).hexdigest()
            index = tx.get(index_key); old = tx.get("work-" + wid)
            immutable = {"work_id": wid, "version": 1, "thread_id": thread,
                         "root_message_id": source["message_id"], "root_rfc_message_id": reply["in_reply_to"],
                         "seed_proposal_id": seed_proposal_id, "seed_proposal_hash": p["proposal_hash"],
                         "account_id": 5898865, "project_id": 49184053, "task_id": int(todo["provider_id"]),
                         "todo_parent_id": task["todo_parent_id"], "todo_parent_type": task["todo_parent_type"],
                         "seed_reply_message_id": mail["provider_id"]}
            if index is not None or old is not None:
                _require(index is not None and index.get("work_id") == wid and old is not None
                         and all(old.get(k) == v for k, v in immutable.items()), "thread_work_binding_conflict")
                return old
            owner = payload.get("analysis", {}).get("escalation", {}).get("owner")
            _require(owner in ("none", "cansu_and_ahmet"), "work_escalation_scope_invalid")
            w = {**immutable, "escalation_owner": owner,
                 "escalation_status": "open" if owner != "none" else "not_required",
                 "revision_count": 0, "pending_event_id": None, "event_ids": [],
                 "last_reply_message_id": mail["provider_id"], "last_message_id": source["message_id"],
                 "latest_observation": None, "created_at": now}
            ids = c.setdefault("work_ids", [])
            _require(len(ids) < self.MAX_PROPOSALS, "internal_queue_full")
            ids.append(wid)
            tx.put("control", c); tx.put("work-" + wid, w)
            tx.put(index_key, {"work_id": wid, "thread_id": thread})
            return w
        return self._run(op)

    def get_work(self, work_id):
        _label(work_id)
        def op(tx, _):
            w = tx.get("work-" + work_id)
            _require(w is not None and w.get("work_id") == work_id, "work_not_found")
            return w
        return self._run(op, allow_expired=True)

    def work_for_thread(self, thread_id):
        _label(thread_id)
        def op(tx, _):
            index = tx.get("thread-" + hashlib.sha256(thread_id.encode()).hexdigest())
            if index is None:
                return None
            w = tx.get("work-" + index["work_id"])
            _require(w is not None and w.get("thread_id") == thread_id, "thread_work_binding_conflict")
            return w
        return self._run(op, allow_expired=True)

    def record_work_observation(self, work_id, snapshot):
        _label(work_id)
        s = copy.deepcopy(snapshot)
        fields = {"account_id", "project_id", "task_id", "todo_parent_id", "todo_parent_type", "title",
                  "description", "status", "completed", "due_on", "assignee_ids", "updated_at",
                  "task_fingerprint", "observed_at", "identity_verified"}
        _require(isinstance(s, dict) and set(s) == fields and s["identity_verified"] is True
                 and s["status"] in ("active", "archived", "trashed") and type(s["completed"]) is bool
                 and s["assignee_ids"] == [53358100] and isinstance(s["title"], str)
                 and isinstance(s["description"], str), "work_observation_invalid")
        _hash(s["task_fingerprint"]); _json(s)
        try:
            observed = datetime.fromisoformat(s["observed_at"])
            _require(observed.tzinfo is not None and observed.utcoffset() is not None, "work_observation_invalid")
        except (ValueError, TypeError):
            raise StoreError("work_observation_invalid") from None
        def op(tx, now):
            w = tx.get("work-" + work_id)
            _require(w is not None and all(w.get(k) == s.get(k) for k in
                     ("account_id", "project_id", "task_id", "todo_parent_id", "todo_parent_type")), "work_observation_scope_invalid")
            _require(0 <= now - observed.timestamp() <= 300, "work_observation_stale")
            old = w.get("latest_observation")
            if old is not None:
                _require(datetime.fromisoformat(old["observed_at"]).timestamp() <= observed.timestamp(), "work_observation_clock_regression")
            w["latest_observation"] = s
            tx.put("work-" + work_id, w)
            return w
        return self._run(op, allow_expired=True)

    def claim_followup(self, work_id, message_id, source_fingerprint, expected_generation):
        _label(work_id); _label(message_id); _hash(source_fingerprint)
        def op(tx, now):
            c = self._control(tx); w = tx.get("work-" + work_id)
            self._open(c, expected_generation, now)
            _require(w is not None and message_id != w["root_message_id"], "followup_root_already_processed")
            request_key = hashlib.sha256((w["thread_id"] + ":" + message_id).encode()).hexdigest()
            event_id = "evt-" + request_key; pid = "p-" + request_key
            event = tx.get(event_id); p = tx.get("proposal-" + pid)
            _require(event is None and p is None, "followup_already_reserved_no_retry")
            _require(w["pending_event_id"] is None, "previous_followup_unresolved")
            _require(len(w["event_ids"]) < self.MAX_PROPOSALS, "internal_queue_full")
            obs = w.get("latest_observation") or {}
            _require(obs.get("status") == "active" and obs.get("completed") is False, "work_not_active")
            _require(0 <= now - datetime.fromisoformat(obs["observed_at"]).timestamp() <= 300, "work_observation_stale")
            row = {"event_id": event_id, "work_id": work_id, "proposal_id": pid,
                   "message_id": message_id, "source_fingerprint": source_fingerprint,
                   "task_fingerprint": obs["task_fingerprint"], "attempt_number": 1,
                   "generation": c["generation"], "token": uuid.uuid4().hex,
                   "claimed_at": now, "state": "claimed", "revision_number": w["revision_count"] + 1}
            w["pending_event_id"] = event_id; w["event_ids"].append(event_id)
            tx.put(event_id, row); tx.put("work-" + work_id, w)
            return row
        return self._run(op)

    def claim_followup_recovery(self, work_id, source_fingerprint, task_fingerprint,
                                expected_generation, failure_evidence_hash):
        """One explicit maintenance reanalysis, never a content approval.

        Only terminal response_incomplete is eligible; first cost/evidence and
        pending-event identity remain intact. No third attempt or transport
        uncertainty can enter this path. The caller authenticates maintenance.
        """
        _label(work_id); _hash(source_fingerprint); _hash(task_fingerprint); _hash(failure_evidence_hash)
        def op(tx, now):
            c = self._control(tx); w = tx.get("work-" + work_id); b = self._budget(tx)
            self._open(c, expected_generation, now)
            _require(w is not None and w.get("pending_event_id"), "recovery_pending_event_required")
            e = tx.get(w["pending_event_id"])
            _require(e is not None, "recovery_pending_event_required")
            rid = "recovery-" + e["event_id"] + "-2"
            old = tx.get(rid); p = tx.get("proposal-" + e["proposal_id"])
            actions = [tx.get("action-" + action_id(e["proposal_id"], k)) for k in KINDS]
            key = "model-" + e["proposal_id"][2:]
            failure_doc = "model-failure-" + hashlib.sha256(key.encode()).hexdigest()
            failure = tx.get(failure_doc)
            success = tx.get("model-receipt-" + hashlib.sha256(key.encode()).hexdigest())
            _require(old is None, "recovery_already_reserved_no_retry")
            _require(e["state"] == "held" and e.get("reason") == "response_incomplete"
                     and e["generation"] == c["generation"] and e["source_fingerprint"] == source_fingerprint
                     and p is None and not any(actions) and success is None, "recovery_not_eligible")
            first = next((r for r in b["reservations"] if r["reservation_key"] == key), None)
            _require(first is not None and first["provider"] == "openai" and first["state"] in ("started", "unknown"), "recovery_original_hold_required")
            obs = w.get("latest_observation") or {}
            _require(obs.get("status") == "active" and obs.get("completed") is False
                     and obs.get("task_fingerprint") == task_fingerprint
                     and e.get("task_fingerprint", task_fingerprint) == task_fingerprint
                     and 0 <= now - datetime.fromisoformat(obs["observed_at"]).timestamp() <= 300, "recovery_task_changed")
            if failure is not None:
                _require(failure["reason"] == "response_incomplete" and failure.get("receipt") is not None
                         and failure["receipt"].get("status") == "incomplete", "recovery_terminal_failure_required")
            else:
                # Historical v6 captured only the safe error code. The operator
                # must bind the original execution evidence; no missing provider
                # response ID, token usage or incomplete cause is invented.
                failure = {"reservation_key": key, "reason": "response_incomplete", "receipt": None,
                           "recorded_at": now, "legacy_observability_gap": True,
                           "evidence_sha256": failure_evidence_hash, "usage_is_invoice": False}
                tx.put(failure_doc, failure)
            authorization = {"actor": "codex-authorized-maintenance", "content_approval": False,
                             "failure_evidence_sha256": failure_evidence_hash, "prior_reservation_key": key,
                             "source_fingerprint": source_fingerprint, "task_fingerprint": task_fingerprint,
                             "generation": c["generation"], "authorized_at": now,
                             "attempt_number": 2, "max_attempts": 2, "work_id": work_id,
                             "event_id": e["event_id"], "proposal_id": e["proposal_id"]}
            row = {"recovery_id": rid, "token": uuid.uuid4().hex, "state": "claimed",
                   "authorization": authorization, "reservation_key": key + "-attempt2"}
            tx.put(rid, row)
            return {**e, "recovery_id": rid, "recovery_token": row["token"],
                    "reservation_key": row["reservation_key"], "attempt_number": 2}
        return self._run(op)

    @staticmethod
    def _bound_proposal(tx, p, *, require_pending=True):
        payload = p["payload"]
        if "work" not in payload:
            return None
        ref = payload["work"]
        _require(isinstance(ref, dict) and set(ref) == set(WORK_REF_FIELDS), "followup_work_reference_invalid")
        w = tx.get("work-" + _label(ref.get("work_id")))
        _require(w is not None and all(ref[k] == w.get(k) for k in WORK_REF_FIELDS), "followup_work_changed")
        eid = "evt-" + hashlib.sha256((w["thread_id"] + ":" + payload.get("source", {}).get("message_id", "")).encode()).hexdigest()
        e = tx.get(eid)
        _require(e is not None and e.get("proposal_id") == p["proposal_id"] and e.get("work_id") == w["work_id"], "followup_event_invalid")
        if require_pending:
            _require(w["pending_event_id"] == eid and e["state"] == "proposed", "followup_not_pending")
        return w

    def put_followup_proposal(self, claim, payload):
        _require(isinstance(claim, dict), "followup_claim_invalid")
        eid = _label(claim.get("event_id")); wid = _label(claim.get("work_id"))
        digest = proposal_hash(payload)
        def op(tx, now):
            c = self._control(tx); w = tx.get("work-" + wid); e = tx.get(eid)
            _require(e is not None and all(e.get(k) == claim.get(k) for k in
                     ("token", "work_id", "proposal_id", "generation", "source_fingerprint")), "followup_claim_invalid")
            pid = e["proposal_id"]; old = tx.get("proposal-" + pid); source = tx.get("source-" + e["source_fingerprint"])
            recovery = tx.get(_label(claim["recovery_id"])) if claim.get("recovery_id") else None
            self._open(c, e["generation"], now)
            if recovery is not None:
                _require(recovery.get("token") == claim.get("recovery_token") and recovery["state"] in ("claimed", "proposed")
                         and recovery["authorization"]["event_id"] == eid
                         and recovery["authorization"]["generation"] == c["generation"], "recovery_claim_invalid")
            _require(w is not None and w["pending_event_id"] == eid and
                     (e["state"] in ("claimed", "proposed") or (recovery is not None and e["state"] == "held" and e.get("reason") == "response_incomplete")), "followup_not_pending")
            _require(set(payload) == {"scope", "analysis", "work", "comment", "reply", "source"}
                     and payload["scope"] == "gamechangers_internal"
                     and payload["work"] == {k: w[k] for k in WORK_REF_FIELDS}, "followup_work_reference_invalid")
            comment = payload["comment"]
            _require(isinstance(comment, dict) and set(comment) == {"account_id", "project_id", "task_id", "todo_parent_id", "todo_parent_type", "body", "task_fingerprint"}
                     and all(comment.get(k) == w.get(k) for k in ("account_id", "project_id", "task_id", "todo_parent_id", "todo_parent_type"))
                     and isinstance(comment.get("body"), str) and bool(comment["body"].strip())
                     and comment.get("task_fingerprint") == (w.get("latest_observation") or {}).get("task_fingerprint"), "followup_comment_invalid")
            _require(payload["source"].get("message_id") == e["message_id"]
                     and payload["source"].get("thread_id") == w["thread_id"]
                     and payload["reply"].get("thread_id") == w["thread_id"], "followup_source_invalid")
            if old is not None:
                _require(old["proposal_hash"] == digest and old["generation"] == c["generation"], "proposal_id_conflict")
                return old
            _require(source is None and (e["state"] == "claimed" or recovery is not None), "source_already_proposed_no_retry")
            _require(len(c["proposal_ids"]) < self.MAX_PROPOSALS, "internal_queue_full")
            row = {"proposal_id": pid, "proposal_hash": digest, "source_fingerprint": e["source_fingerprint"],
                   "payload": copy.deepcopy(payload), "generation": c["generation"], "created_at": now,
                   "status": "awaiting_human_review", "approval": None}
            c["proposal_ids"].append(pid); e.update(state="proposed", proposal_hash=digest)
            tx.put("control", c); tx.put("proposal-" + pid, row); tx.put(eid, e)
            if recovery is not None:
                recovery.update(state="proposed", proposal_hash=digest)
                tx.put(claim["recovery_id"], recovery)
            tx.put("source-" + e["source_fingerprint"], {"proposal_id": pid, "created_at": now})
            return row
        return self._run(op)

    def hold_followup(self, claim, reason):
        _require(isinstance(claim, dict), "followup_claim_invalid"); _label(reason)
        eid = _label(claim.get("event_id"))
        def op(tx, now):
            e = tx.get(eid)
            recovery = tx.get(_label(claim["recovery_id"])) if claim.get("recovery_id") else None
            _require(e is not None and e.get("token") == claim.get("token"), "followup_claim_invalid")
            if recovery is not None:
                _require(recovery.get("token") == claim.get("recovery_token"), "recovery_claim_invalid")
                if recovery["state"] == "claimed":
                    recovery.update(state="held", reason=reason, held_at=now)
                    tx.put(claim["recovery_id"], recovery)
                return e
            if e["state"] == "claimed":
                e.update(state="held", reason=reason, held_at=now); tx.put(eid, e)
            return e
        return self._run(op, allow_expired=True)

    def complete_followup(self, work_id, proposal_id):
        _label(work_id); _label(proposal_id)
        def op(tx, now):
            w = tx.get("work-" + work_id); p = tx.get("proposal-" + proposal_id)
            _require(w is not None and p is not None and p["payload"].get("work", {}).get("work_id") == work_id, "followup_event_invalid")
            eid = "evt-" + hashlib.sha256((w["thread_id"] + ":" + p["payload"]["source"]["message_id"]).encode()).hexdigest()
            e = tx.get(eid)
            comment = tx.get("action-" + action_id(proposal_id, "basecamp_comment"))
            mail = tx.get("action-" + action_id(proposal_id, "gmail_reply"))
            _require(e is not None and e.get("proposal_id") == proposal_id, "followup_event_invalid")
            for a in (comment, mail):
                _require(a is not None and a["state"] == "verified" and a["payload_hash"] == p["proposal_hash"], "followup_readbacks_required")
            if e["state"] == "completed":
                return w
            _require(e["state"] == "proposed" and w["pending_event_id"] == eid, "followup_not_pending")
            e.update(state="completed", completed_at=now)
            w.update(pending_event_id=None, revision_count=w["revision_count"] + 1,
                     last_reply_message_id=mail["provider_id"], last_message_id=e["message_id"], last_completed_at=now)
            tx.put(eid, e); tx.put("work-" + work_id, w)
            return w
        return self._run(op, allow_expired=True)

    def approve(self, proposal_id, proposal_hash, generation, actor_subject, *, purpose="routine_execution"):
        _label(proposal_id); _hash(proposal_hash); _label(actor_subject)
        _require(purpose in ("routine_execution", "internal_coordination_only"), "approval_purpose_invalid")
        def op(tx, now):
            c = self._control(tx); p = tx.get("proposal-" + proposal_id)
            self._open(c, generation, now)
            _require(p is not None and p["generation"] == generation and p["proposal_hash"] == proposal_hash, "proposal_mismatch")
            _require(p["created_at"] <= now, "clock_before_proposal")
            if p["approval"] is not None:
                _require(p["approval"].get("purpose", "routine_execution") == purpose, "approval_purpose_conflict")
                return p  # Replay does not refresh the approval timestamp.
            payload = p["payload"]
            analysis = payload.get("analysis")
            work = self._bound_proposal(tx, p)
            inherited = work is not None and work["escalation_status"] == "open" and work["escalation_owner"] == "cansu_and_ahmet"
            if purpose == "internal_coordination_only":
                reply, task = payload.get("reply", {}), payload.get("task", {})
                _require(payload.get("scope") == "gamechangers_internal" and isinstance(analysis, dict)
                         and (work is None or inherited)
                         and ((analysis.get("intent") == "complaint_strategy"
                               and analysis.get("escalation", {}).get("owner") == "cansu_and_ahmet")
                              or (inherited and analysis.get("intent") in ("routine_request", "complaint_strategy", "social_planning", "unclear"))),
                         "coordination_scope_invalid")
                from recipient_policy import validate_reply_recipients, RecipientError
                try:
                    validate_reply_recipients(reply.get("to"), reply.get("cc"))
                except RecipientError:
                    raise StoreError("coordination_scope_invalid") from None
                _require(reply.get("from") == "hamle@gamechangers.com.tr"
                         and reply.get("subject") == "Re: HAMLE-IC-IS-20261010-01"
                         and (work is not None or (task.get("account_id") == 5898865 and task.get("project_id") == 49184053
                              and task.get("assignee_id") == 53358100 and task.get("due_at") is None)),
                         "coordination_scope_invalid")
            elif isinstance(analysis, dict):
                _require(analysis.get("intent") == "routine_request"
                         and analysis.get("escalation", {}).get("owner") == "none"
                         and (work is None or work["escalation_status"] == "not_required"), "human_escalation_required")
            p.update(status="approved", approval={"actor_subject": actor_subject, "approved_at": now,
                     "proposal_hash": proposal_hash, "generation": generation, "purpose": purpose,
                     "escalation_status": "open" if purpose == "internal_coordination_only" else "not_required",
                     **({"decision_authorized": False, "production_authorized": False}
                        if purpose == "internal_coordination_only" else {})})
            tx.put("proposal-" + proposal_id, p)
            return p
        return self._run(op)

    def claim_action(self, action_id, proposal_id, kind, payload_hash, worker, expected_generation, *, lease_seconds=60):
        _label(proposal_id); _label(worker); _hash(payload_hash)
        _require(action_id == globals()["action_id"](proposal_id, kind), "noncanonical_action_id")
        _require(type(lease_seconds) is int and 1 <= lease_seconds <= 300, "invalid_lease")
        def op(tx, now):
            c = self._control(tx); p = tx.get("proposal-" + proposal_id); a = tx.get("action-" + action_id)
            self._open(c, expected_generation, now)
            _require(p is not None and p["generation"] == c["generation"] and p["proposal_hash"] == payload_hash and p["approval"] is not None, "approval_required")
            self._bound_proposal(tx, p)
            declared = p["payload"].get("actions")
            if not isinstance(declared, dict):
                declared = {k: p["payload"].get(v) for k, v in
                            (("gmail_reply", "reply"), ("basecamp_todo", "task"), ("basecamp_comment", "comment"))}
            _require(isinstance(declared.get(kind), dict), "action_not_in_approved_proposal")
            _require(a is None, "action_already_reserved_no_retry")
            row = {"action_id": action_id, "proposal_id": proposal_id, "kind": kind,
                   "payload_hash": payload_hash, "generation": c["generation"], "worker": worker,
                   "token": uuid.uuid4().hex, "lease_until": min(now + lease_seconds, DEADLINE),
                   "created_at": now, "state": "leased", "provider_id": None}
            tx.put("action-" + action_id, row)
            return row
        return self._run(op)

    @staticmethod
    def _lease(a, lease):
        _require(isinstance(lease, dict) and a is not None and all(a.get(k) == lease.get(k) for k in ("action_id", "token", "worker", "generation")), "invalid_lease")

    def begin_dispatch(self, lease, source_fingerprint, *, approval_max_age_seconds=300):
        _hash(source_fingerprint)
        _require(type(approval_max_age_seconds) is int and 1 <= approval_max_age_seconds <= 300, "invalid_approval_ttl")
        _require(isinstance(lease, dict), "invalid_lease"); aid = _label(lease.get("action_id"))
        def op(tx, now):
            c = self._control(tx); a = tx.get("action-" + aid)
            self._lease(a, lease)
            p = tx.get("proposal-" + a["proposal_id"])
            self._open(c, a["generation"], now)
            _require(a["state"] == "leased" and a["created_at"] <= now < a["lease_until"], "action_not_dispatchable")
            _require(p is not None and p["source_fingerprint"] == source_fingerprint and p["generation"] == c["generation"], "source_changed")
            self._bound_proposal(tx, p)
            approval = p["approval"]
            _require(approval is not None and approval["proposal_hash"] == a["payload_hash"] and 0 <= now - approval["approved_at"] <= approval_max_age_seconds, "approval_expired_or_mismatch")
            a.update(state="dispatch_started", dispatched_at=now)
            tx.put("action-" + aid, a)
            return a
        return self._run(op)

    def record_receipt(self, lease, provider_id):
        _label(provider_id)
        return self._finish(lease, provider_id)

    def mark_unknown(self, lease):
        return self._finish(lease, None)

    def _finish(self, lease, provider_id):
        _require(isinstance(lease, dict), "invalid_lease"); aid = _label(lease.get("action_id"))
        def op(tx, now):
            a = tx.get("action-" + aid)
            self._lease(a, lease)
            _require(a["state"] in ("dispatch_started", "unknown", "accepted"), "action_not_started")
            if a["state"] == "accepted":
                _require(a["provider_id"] == provider_id, "receipt_conflict")
                return a
            a.update(state="accepted" if provider_id else "unknown", provider_id=provider_id, finished_at=now)
            # Evidence can arrive after stop/expiry; never reopen an action.
            tx.put("action-" + aid, a)
            return a
        return self._run(op, allow_expired=True)

    def confirm_readback(self, action_id, provider_id):
        _label(action_id); _label(provider_id)
        def op(tx, now):
            a = tx.get("action-" + action_id)
            _require(a is not None and a["state"] in ("accepted", "verified") and a["provider_id"] == provider_id, "readback_receipt_mismatch")
            a.update(state="verified", readback_at=now)
            tx.put("action-" + action_id, a)
            return a
        return self._run(op, allow_expired=True)

    @staticmethod
    def _budget(tx):
        b = tx.get("budget")
        _require(isinstance(b, dict) and b.get("policy_id") == POLICY and b.get("limit_microdollars") == LIMIT, "budget_not_imported")
        return b

    PREVIOUS_DEADLINE = "2026-10-12T23:59:59+03:00"

    def extend_budget_expiry(self, evidence):
        """Operator one-shot (Ahmet 2026-10-11, decision 3: no end date; stop with 'dur').

        Moves the imported budget's expires_at from the previous deadline to the current
        AUTHORIZATION_DEADLINE. Nothing else changes (limit, reservations, holds, stop state).
        Idempotent: a budget already on the current deadline returns unchanged.
        """
        evidence = _label(evidence)
        def op(tx, now):
            b = self._budget(tx)
            if b.get("expires_at") == AUTHORIZATION_DEADLINE:
                return {"status": "already_current", "expires_at": AUTHORIZATION_DEADLINE, "business_writes": 0}
            _require(b.get("expires_at") == self.PREVIOUS_DEADLINE, "budget_expiry_unexpected")
            b["expires_at"] = AUTHORIZATION_DEADLINE
            b.setdefault("expiry_history", []).append({"from": self.PREVIOUS_DEADLINE, "to": AUTHORIZATION_DEADLINE,
                                                      "at": now, "evidence_ref": evidence})
            tx.put("budget", b)
            return {"status": "budget_expiry_extended", "expires_at": AUTHORIZATION_DEADLINE, "business_writes": 0}
        return self._run(op)

    @staticmethod
    def _totals(b):
        spent = held = paid = committed = 0
        for name in PROVIDERS:
            p = b["providers"].get(name)
            if p is None:  # legacy budget without a Claude entry yet
                continue
            rows = [r for r in b["reservations"] if r["provider"] == name and _in_period(b, r)]
            usage = (p["baseline_microdollars"] + p.get("archived_microdollars", 0)
                     + sum(r["actual_microdollars"] for r in rows if r["state"] == "settled"))
            reserved = sum(r["reserved_microdollars"] for r in rows if r["state"] != "settled")
            spent += usage; held += reserved; paid += p["paid_microdollars"]
            committed += max(p["paid_microdollars"], usage + reserved)
        return {"limit_microdollars": LIMIT, "accounted_spend_microdollars": spent, "held_microdollars": held,
                "prepaid_purchases_microdollars": paid, "total_committed_microdollars": committed,
                "remaining_microdollars": max(0, LIMIT - committed), "overrun_hold": b["overrun_hold"],
                "provider_hard_cap_installed": False, "period": b.get("period")}

    @staticmethod
    def _chain_committed(b):
        """Chain model money of the budget's current month: archived actuals + holds + settled actuals."""
        return b.get("archived_chain_microdollars", 0) + sum(
            (r.get("actual_microdollars") or 0) if r.get("state") == "settled" else r["reserved_microdollars"]
            for r in b["reservations"]
            if str(r.get("reservation_key", "")).startswith("chain-model-") and _in_period(b, r))

    def _roll(self, b, now):
        """Monthly renewal and ledger compaction, in memory. Returns archive docs for the caller to write.

        New Istanbul month: the old month's totals go to period_history, its rows are stamped with
        their month (they stop counting), baselines/prepaid/archived sums restart at 0. Unsettled old
        rows stay in the list so they can still be settled from a bill. Settled rows older than
        BUDGET_COMPACT_MIN_AGE are moved to budget-archive-* docs: always for past months, and for
        the current month once the list reaches BUDGET_COMPACT_AT (their sums stay in the totals).
        """
        period = budget_period(now)
        if b.get("period") is None:  # first sight: stamp, no renewal (existing rows are this month's)
            b["period"] = period
        rolled = period > b["period"]
        if rolled:
            b.setdefault("period_history", []).append(
                {"period": b["period"], "totals": self._totals(b), "chain_microdollars": self._chain_committed(b),
                 "providers": copy.deepcopy(b["providers"]), "rolled_at": now})
            for r in b["reservations"]:
                r.setdefault("period", b["period"])
            for p in b["providers"].values():
                p.update(baseline_microdollars=0, paid_microdollars=0, archived_microdollars=0)
            b["archived_chain_microdollars"] = 0
            b["period"] = period
        compact = len(b["reservations"]) >= BUDGET_COMPACT_AT
        keep, moved = [], []
        for r in b["reservations"]:
            old_enough = now - (r.get("finished_at") or 0) >= BUDGET_COMPACT_MIN_AGE
            if r["state"] == "settled" and old_enough and (not _in_period(b, r) or compact):
                moved.append(r)
            else:
                keep.append(r)
        for r in moved:
            if _in_period(b, r):
                p = b["providers"][r["provider"]]
                p["archived_microdollars"] = p.get("archived_microdollars", 0) + r["actual_microdollars"]
                if str(r["reservation_key"]).startswith("chain-model-"):
                    b["archived_chain_microdollars"] = b.get("archived_chain_microdollars", 0) + r["actual_microdollars"]
                r.setdefault("period", b["period"])
        archives = []
        for i in range(0, len(moved), BUDGET_ARCHIVE_CHUNK):
            seq = b.get("archive_seq", 0) + 1
            b["archive_seq"] = seq
            archives.append(("budget-archive-%05d" % seq, {"kind": "budget_archive", "policy_id": POLICY,
                             "period": b["period"], "archived_at": now, "rows": moved[i:i + BUDGET_ARCHIVE_CHUNK]}))
        b["reservations"] = keep
        return {"rolled": rolled, "archived_rows": len(moved), "archives": archives}

    def roll_budget_period(self):
        """Idempotent start-of-run step: monthly renewal + compaction (writes only when something changed)."""
        def op(tx, now):
            b = self._budget(tx)
            before = b.get("period")
            r = self._roll(b, now)
            if before == b["period"] and not r["archived_rows"]:
                return {"status": "budget_period_current", "period": b["period"], "archived_rows": 0}
            for key, doc in r["archives"]:
                _json(doc); tx.put(key, doc)
            _json(b); tx.put("budget", b)
            return {"status": "budget_period_rolled" if r["rolled"] else "budget_period_updated",
                    "period": b["period"], "archived_rows": r["archived_rows"]}
        return self._run(op)

    def initialize_budget(self, snapshot):
        b = copy.deepcopy(snapshot)
        _require(isinstance(b, dict) and b.get("policy_id") == POLICY and b.get("limit_microdollars") == LIMIT and b.get("expires_at") == AUTHORIZATION_DEADLINE, "budget_policy_mismatch")
        _require(type(b.get("overrun_hold")) is bool and isinstance(b.get("providers"), dict) and set(b["providers"]) in (set(PROVIDERS), set(LEGACY_PROVIDERS)), "budget_snapshot_invalid")
        _label(b.get("evidence_ref"))
        for p in b["providers"].values():
            _require(isinstance(p, dict) and set(p) == {"baseline_microdollars", "paid_microdollars", "billing_ready"} and type(p["billing_ready"]) is bool, "budget_provider_invalid")
            _money(p["baseline_microdollars"]); _money(p["paid_microdollars"])
        _require(isinstance(b.get("reservations"), list) and len(b["reservations"]) <= self.MAX_RESERVATIONS, "budget_reservations_invalid")
        keys = set()
        for r in b["reservations"]:
            _require(isinstance(r, dict), "budget_reservation_invalid")
            key = _label(r.get("reservation_key"))
            _require(key not in keys and r.get("provider") in b["providers"] and r.get("state") in ("reserved", "started", "unknown", "settled"), "budget_reservation_invalid")
            keys.add(key)
            _require(_money(r.get("reserved_microdollars")) > 0, "budget_reservation_invalid")
            if r["state"] == "settled":
                _money(r.get("actual_microdollars")); _label(r.get("evidence_ref"))
            else:
                _require(r.get("actual_microdollars") is None, "budget_reservation_invalid")
        if any(r["state"] == "settled" and r["actual_microdollars"] > r["reserved_microdollars"] for r in b["reservations"]) or self._totals(b)["total_committed_microdollars"] > LIMIT:
            b["overrun_hold"] = True
        digest = hashlib.sha256(_json(b).encode()).hexdigest()
        b["import_hash"] = digest
        def op(tx, now):
            old = tx.get("budget")
            if old is not None:
                _require(old.get("import_hash") == digest, "budget_already_imported_no_reset")
                return self._totals(old)
            tx.put("budget", b)
            return self._totals(b)
        return self._run(op)

    def budget_status(self):
        return self._run(lambda tx, _: self._totals(self._budget(tx)), allow_expired=True)

    def claim_credential_refresh(self, record_hash):
        """One irreversible refresh chance per credential version digest.

        Explicit read-only identity probes may refresh while workflow is stopped.
        A timeout/crash requires manual reconciliation, never a replayed refresh.
        """
        _hash(record_hash)
        def op(tx, now):
            key = "refresh-" + record_hash
            if tx.get(key) is not None:
                return False
            tx.put(key, {"state": "claimed_do_not_retry", "claimed_at": now})
            return True
        return self._run(op)

    def budget_reserve(self, key, provider, microdollars):
        _label(key); _require(provider in PROVIDERS and _money(microdollars) > 0, "invalid_reservation")
        def op(tx, now):
            b = self._budget(tx)
            if provider == "anthropic" and "anthropic" not in b["providers"]:
                b["providers"]["anthropic"] = dict(ANTHROPIC_PROVIDER_SEED)
                b.setdefault("provider_additions", []).append(
                    {"provider": "anthropic", "evidence_ref": ANTHROPIC_SEED_EVIDENCE, "added_at": now})
            _require(provider in b["providers"], "budget_not_ready")
            _require(not b["overrun_hold"] and b["providers"][provider]["billing_ready"], "budget_not_ready")
            old = next((r for r in b["reservations"] if r["reservation_key"] == key), None)
            if old:
                _require(old["provider"] == provider and old["reserved_microdollars"] == microdollars, "reservation_key_conflict")
                return old
            rolled = self._roll(b, now)
            _require(len(b["reservations"]) < self.MAX_RESERVATIONS, "budget_ledger_full")
            row = {"reservation_key": key, "provider": provider, "reserved_microdollars": microdollars,
                   "actual_microdollars": None, "state": "reserved", "evidence_ref": None, "created_at": now,
                   "period": b["period"]}
            b["reservations"].append(row)
            _require(self._totals(b)["total_committed_microdollars"] <= LIMIT, "aggregate_budget_exhausted")
            _json(b)
            for archive_key, doc in rolled["archives"]:
                _json(doc); tx.put(archive_key, doc)
            tx.put("budget", b)
            return row
        return self._run(op)

    def budget_begin(self, key, expected_generation):
        _label(key)
        def op(tx, now):
            c = self._control(tx); b = self._budget(tx)
            self._open(c, expected_generation, now)
            r = next((r for r in b["reservations"] if r["reservation_key"] == key), None)
            _require(r is not None and r["state"] == "reserved", "budget_call_not_replayable")
            _require(not b["overrun_hold"] and b["providers"][r["provider"]]["billing_ready"] and self._totals(b)["total_committed_microdollars"] <= LIMIT, "budget_not_ready")
            _require(r.get("created_at", now) <= now, "clock_before_reservation")
            r.update(state="started", started_at=now)
            tx.put("budget", b)
            return r
        return self._run(op)

    def budget_unknown(self, key):
        return self._budget_finish(key, None, None)

    def record_model_receipt(self, reservation_key, receipt):
        """Immutable usage evidence, never a bill or release of reserved funds."""
        _label(reservation_key)
        r = copy.deepcopy(receipt)
        if isinstance(r, dict) and r.get("provider") == "anthropic":
            return self._record_anthropic(reservation_key, "model-receipt-", _anthropic_receipt(r, failure=False),
                                          {"receipt": r})
        _require(isinstance(r, dict) and set(r) == {"response_id", "model", "reasoning_effort", "usage"}, "model_receipt_invalid")
        _require(isinstance(r["response_id"], str) and re.fullmatch(r"resp_[A-Za-z0-9_-]{1,200}", r["response_id"])
                 and r["model"] == "gpt-6-astra" and r["reasoning_effort"] == "max", "model_receipt_invalid")
        u = r["usage"]
        required = {"input_tokens", "output_tokens", "total_tokens"}
        optional = {"input_tokens_details", "output_tokens_details"}
        _require(isinstance(u, dict) and required <= set(u) <= required | optional, "model_usage_invalid")
        _require(all(type(u[k]) is int and 0 <= u[k] <= 2_000_000 for k in required)
                 and u["total_tokens"] == u["input_tokens"] + u["output_tokens"], "model_usage_invalid")
        for details, field, total in (("input_tokens_details", "cached_tokens", "input_tokens"),
                                      ("output_tokens_details", "reasoning_tokens", "output_tokens")):
            if details in u:
                v = u[details]
                _require(isinstance(v, dict) and set(v) == {field} and type(v[field]) is int
                         and 0 <= v[field] <= u[total], "model_usage_invalid")
        doc = "model-receipt-" + hashlib.sha256(reservation_key.encode()).hexdigest()
        def op(tx, now):
            b = self._budget(tx)
            reservation = next((x for x in b["reservations"] if x["reservation_key"] == reservation_key), None)
            old = tx.get(doc)
            _require(reservation is not None and reservation["provider"] == "openai"
                     and reservation["state"] in ("started", "unknown", "settled"), "model_reservation_not_started")
            if old is not None:
                _require(old["receipt"] == r and old["reservation_key"] == reservation_key, "model_receipt_conflict")
                return old
            row = {"reservation_key": reservation_key, "receipt": r, "recorded_at": now,
                   "usage_is_invoice": False}
            tx.put(doc, row)
            return row
        return self._run(op, allow_expired=True)

    def record_model_failure(self, reservation_key, reason, receipt=None):
        """Immutable failed-attempt metadata; never a draft, invoice or retry grant."""
        _label(reservation_key); _label(reason)
        r = copy.deepcopy(receipt)
        if isinstance(r, dict) and r.get("provider") == "anthropic":
            return self._record_anthropic(reservation_key, "model-failure-", _anthropic_receipt(r, failure=True),
                                          {"reason": reason, "receipt": r, "legacy_observability_gap": False})
        if r is not None:
            _require(isinstance(r, dict) and set(r) == {"response_id", "model", "reasoning_effort", "status", "incomplete_reason", "usage"}, "model_failure_receipt_invalid")
            _require(r["response_id"] is None or (isinstance(r["response_id"], str) and re.fullmatch(r"resp_[A-Za-z0-9_-]{1,200}", r["response_id"])), "model_failure_receipt_invalid")
            _require(r["model"] in (None, "gpt-6-astra") and r["reasoning_effort"] in (None, "max")
                     and r["status"] in (None, "completed", "incomplete", "failed", "cancelled", "queued", "in_progress")
                     and r["incomplete_reason"] in (None, "max_output_tokens", "content_filter", "steered"), "model_failure_receipt_invalid")
            u = r["usage"]
            if u is not None:
                required = {"input_tokens", "output_tokens", "total_tokens"}
                _require(isinstance(u, dict) and required <= set(u) <= required | {"input_tokens_details", "output_tokens_details"}
                         and all(type(u[k]) is int and 0 <= u[k] <= 2_000_000 for k in required)
                         and u["total_tokens"] == u["input_tokens"] + u["output_tokens"], "model_usage_invalid")
                for details, field, total in (("input_tokens_details", "cached_tokens", "input_tokens"),
                                              ("output_tokens_details", "reasoning_tokens", "output_tokens")):
                    if details in u:
                        v = u[details]
                        _require(isinstance(v, dict) and set(v) == {field} and type(v[field]) is int
                                 and 0 <= v[field] <= u[total], "model_usage_invalid")
        doc = "model-failure-" + hashlib.sha256(reservation_key.encode()).hexdigest()
        def op(tx, now):
            b = self._budget(tx); old = tx.get(doc)
            reservation = next((x for x in b["reservations"] if x["reservation_key"] == reservation_key), None)
            _require(reservation is not None and (reservation["provider"] == "openai" or (r is None and reservation["provider"] == "anthropic"))
                     and reservation["state"] in ("started", "unknown", "settled"), "model_reservation_not_started")
            if old is not None:
                _require(old["reason"] == reason and old.get("receipt") == r
                         and old.get("legacy_observability_gap") is False, "model_failure_conflict")
                return old
            row = {"reservation_key": reservation_key, "reason": reason, "receipt": r,
                   "recorded_at": now, "legacy_observability_gap": False, "usage_is_invoice": False}
            tx.put(doc, row)
            return row
        return self._run(op, allow_expired=True)

    def _record_anthropic(self, reservation_key, prefix, r, fields):
        doc = prefix + hashlib.sha256(reservation_key.encode()).hexdigest()
        def op(tx, now):
            b = self._budget(tx)
            reservation = next((x for x in b["reservations"] if x["reservation_key"] == reservation_key), None)
            _require(reservation is not None and reservation["provider"] == "anthropic"
                     and reservation["state"] in ("started", "unknown", "settled"), "model_reservation_not_started")
            old = tx.get(doc)
            if old is not None:
                _require(all(old.get(k) == v for k, v in fields.items()) and old["reservation_key"] == reservation_key,
                         "model_receipt_conflict" if prefix == "model-receipt-" else "model_failure_conflict")
                return old
            row = {"reservation_key": reservation_key, **fields, "recorded_at": now, "usage_is_invoice": False}
            tx.put(doc, row)
            return row
        return self._run(op, allow_expired=True)

    def model_failure(self, reservation_key):
        _label(reservation_key)
        doc = "model-failure-" + hashlib.sha256(reservation_key.encode()).hexdigest()
        return self._run(lambda tx, _: tx.get(doc), allow_expired=True)

    def model_receipt(self, reservation_key):
        _label(reservation_key)
        doc = "model-receipt-" + hashlib.sha256(reservation_key.encode()).hexdigest()
        return self._run(lambda tx, _: tx.get(doc), allow_expired=True)

    def budget_settle(self, key, actual_microdollars, evidence_ref):
        _money(actual_microdollars); _label(evidence_ref)
        return self._budget_finish(key, actual_microdollars, evidence_ref)

    def settle_unknown_actual(self, key, actual_microdollars, evidence):
        """Operator one-shot: a call or hold whose outcome was 'unknown' (sent, no receipt) is
        settled at the real cost read from the provider's bill (Anthropic Console, OpenAI usage,
        Google Cloud billing). Only state 'unknown' rows of SETTLE_PROVIDERS; never lowers a
        settled amount; overrun rules unchanged."""
        _label(key); evidence = _label(evidence)
        _require(type(actual_microdollars) is int and 0 <= actual_microdollars <= 20_000_000, "settle_actual_invalid")
        b = self._run(lambda tx, now: self._budget(tx), allow_expired=True)
        r = next((r for r in b["reservations"] if r["reservation_key"] == key), None)
        _require(r is not None and r["provider"] in SETTLE_PROVIDERS, "reservation_missing")
        if r["state"] == "settled":
            _require(r["actual_microdollars"] == actual_microdollars and r["evidence_ref"] == evidence, "settlement_conflict")
            return r
        _require(r["state"] == "unknown", "settle_actual_requires_unknown")
        return self._budget_finish(key, actual_microdollars, evidence)

    def settle_unknown_total(self, provider, keys, total_microdollars, evidence):
        """Operator one-shot: one bill covers several 'unknown' holds of one provider (e.g. the
        OpenAI usage of 9-10 Oct, or the Google Cloud cost of the builds). The billed total is
        split evenly over those rows (remainder on the first) in one transaction. Idempotent for
        the same evidence and total; overrun rules unchanged."""
        _require(provider in SETTLE_PROVIDERS, "settle_provider_invalid")
        _require(isinstance(keys, (list, tuple)) and 1 <= len(keys) <= 50 and len(set(keys)) == len(keys), "settle_keys_invalid")
        for k in keys:
            _label(k)
        evidence = _label(evidence)
        _require(type(total_microdollars) is int and 0 <= total_microdollars <= LIMIT, "settle_actual_invalid")
        def op(tx, now):
            b = self._budget(tx)
            rows = [next((r for r in b["reservations"] if r["reservation_key"] == k), None) for k in keys]
            _require(all(r is not None and r["provider"] == provider for r in rows), "reservation_missing")
            if all(r["state"] == "settled" and r["evidence_ref"] == evidence for r in rows):
                _require(sum(r["actual_microdollars"] for r in rows) == total_microdollars, "settlement_conflict")
                return {"status": "billed_total_settled", "rows": len(rows), "total_microdollars": total_microdollars}
            _require(all(r["state"] == "unknown" for r in rows), "settle_actual_requires_unknown")
            share, rest = divmod(total_microdollars, len(rows))
            for i, r in enumerate(rows):
                actual = share + (rest if i == 0 else 0)
                r.update(state="settled", actual_microdollars=actual, evidence_ref=evidence, finished_at=now)
                if actual > r["reserved_microdollars"]:
                    b["overrun_hold"] = True
            if self._totals(b)["total_committed_microdollars"] > LIMIT:
                b["overrun_hold"] = True
            tx.put("budget", b)
            return {"status": "billed_total_settled", "rows": len(rows), "total_microdollars": total_microdollars}
        return self._run(op, allow_expired=True)

    def _budget_finish(self, key, actual, evidence):
        _label(key)
        def op(tx, now):
            b = self._budget(tx)
            r = next((r for r in b["reservations"] if r["reservation_key"] == key), None)
            _require(r is not None, "reservation_missing")
            if r["state"] == "settled":
                _require(r["actual_microdollars"] == actual and r["evidence_ref"] == evidence, "settlement_conflict")
                return r
            r.update(state="unknown" if actual is None else "settled", actual_microdollars=actual, evidence_ref=evidence, finished_at=now)
            if actual is not None and (actual > r["reserved_microdollars"] or self._totals(b)["total_committed_microdollars"] > LIMIT):
                b["overrun_hold"] = True
            tx.put("budget", b)
            return r
        return self._run(op, allow_expired=True)

    def public_state(self):
        def op(tx, now):
            c = self._control(tx); b = tx.get("budget")
            proposals, actions = [], []
            works = []
            for wid in c.get("work_ids", []):
                w = tx.get("work-" + wid)
                _require(w is not None, "work_record_missing")
                followup_status = None
                if w.get("pending_event_id"):
                    e = tx.get(w["pending_event_id"])
                    _require(e is not None, "followup_event_invalid")
                    recovery = tx.get("recovery-" + e["event_id"] + "-2")
                    attempt = 2 if recovery is not None else 1
                    active = recovery if recovery is not None else e
                    state = "proposed" if e["state"] == "proposed" else (
                        "claimed" if active["state"] == "claimed" else "held")
                    reason = active.get("reason") if state == "held" else None
                    if e["generation"] != c["generation"] or c["mode"] != "review_open":
                        state, reason = "held", "control_stopped_or_changed"
                    elif now > DEADLINE:
                        state, reason = "held", "authorization_expired"
                    allowed = {"response_incomplete", "control_stopped_or_changed", "control_stopped_or_stale",
                               "authorization_expired", "provider_or_model_failed_no_retry", "transport_outcome_unknown",
                               "aggregate_budget_exhausted", "budget_not_ready"}
                    if state == "held" and reason not in allowed:
                        reason = "requires_manual_review"
                    followup_status = {"state": state, "reason": reason, "attempt": attempt}
                w["followup_status"] = followup_status  # computed view only; never persisted in binding
                works.append(w)
            for pid in c["proposal_ids"]:
                p = tx.get("proposal-" + pid)
                _require(p is not None, "proposal_record_missing")
                if p["generation"] != c["generation"] or c["mode"] != "review_open":
                    p["status"] = "cancelled"
                proposals.append(p)
                for kind in KINDS:
                    a = tx.get("action-" + action_id(pid, kind))
                    if a is None:
                        continue
                    if a["state"] in ("leased", "dispatch_started") and (a["generation"] != c["generation"] or c["mode"] != "review_open" or now >= a["lease_until"]):
                        a["state"] = "unknown" if a["state"] == "dispatch_started" else "cancelled"
                    a.pop("token", None)
                    actions.append(a)
            return {"control": c, "proposals": proposals, "actions": actions, "works": works,
                    "budget": self._totals(b) if b else None, "external_actions_enabled": False,
                    "unseen_cc_removal_resolved": False}
        return self._run(op, allow_expired=True)

    snapshot = public_state
