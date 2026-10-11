"""Isolated v1.1 queue foundation; this module is NOT wired to the live worker.

Transactions implement get(key)/put(key, JSON record), just like cloud_store.
Queue leases serialize work; they never grant customer or provider authority.
Trusted adapters must ingest each provider oldest first and recheck authority,
budget, current mail, recipients and task binding immediately before a write.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
import uuid

from autonomy_policy import BRANDS

PROJECT = "gamechangers-hamle"
DATABASE = "hamle-operations"
COLLECTION = "hamle_v11_work_queue"
MAX_PAGE = 100
HASH = re.compile(r"[0-9a-f]{64}\Z")
WRITE_BINDING_FIELDS = ("work_id", "brand_id", "thread_id", "account_id", "project_id",
                        "list_id", "service_id", "task_id")


class QueueError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(ok, code):
    if not ok:
        raise QueueError(code)


def text_id(value):
    require(isinstance(value, str) and 0 < len(value) <= 200
            and not any(ord(c) < 32 for c in value), "invalid_identifier")
    return value


def positive(value):
    require(type(value) is int and 0 < value <= 9_007_199_254_740_991, "invalid_numeric_identifier")
    return value


def _fingerprint(value):
    require(isinstance(value, str) and HASH.fullmatch(value), "invalid_fingerprint")
    return value


def digest(*values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class _MemoryTx:
    def __init__(self, records):
        self.records = records
        self.has_written = False

    def get(self, key):
        require(not self.has_written, "read_after_write_not_supported")
        return copy.deepcopy(self.records.get(key))

    def put(self, key, value):
        # Same small JSON-only record contract as persistent storage.
        require(len(json.dumps(value, allow_nan=False).encode()) <= 60_000, "record_too_large")
        self.records[key] = copy.deepcopy(value)
        self.has_written = True


class InMemoryBackend:
    """Atomic rollback and locking; intended for offline acceptance tests only."""
    def __init__(self):
        self.records = {}
        self.lock = threading.RLock()

    def transact(self, callback):
        with self.lock:
            staged = copy.deepcopy(self.records)
            result = callback(_MemoryTx(staged))
            self.records = staged
            return copy.deepcopy(result)

    def scan(self, *, kind, filters, limit, cursor=None):
        with self.lock:
            rows = [copy.deepcopy(v) for k, v in sorted(self.records.items())
                    if (cursor is None or k > cursor) and v.get("kind") == kind
                    and all(v.get(k) == val for k, val in filters.items())]
            return rows[:limit]


class FirestoreQueueBackend:
    """Dedicated collection, lazy SDK import and explicit opt-in; never V7 data.

    No initialization, credential discovery or network operation occurs until
    this constructor is explicitly invoked by a separately authorized adapter.
    Production query indexes and IAM remain deployment acceptance obligations.
    """
    def __init__(self, *, opt_in, credentials, project=PROJECT, database=DATABASE):
        require((project, database, opt_in) == (PROJECT, DATABASE, "v11-queue-explicit"),
                "cloud_opt_in_required")
        require(credentials is not None, "explicit_credentials_required")
        from google.cloud import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter
        self.sdk, self.field_filter = firestore, FieldFilter
        self.client = firestore.Client(project=project, database=database, credentials=credentials)
        self.collection = self.client.collection(COLLECTION)

    def transact(self, callback):
        from cloud_store import _FirestoreTx
        @self.sdk.transactional
        def apply(tx):
            return callback(_FirestoreTx(self.collection, tx))
        try:
            return apply(self.client.transaction(max_attempts=1))
        except QueueError:
            raise
        except Exception:
            # Never retry an ambiguous write through this application layer.
            raise QueueError("store_outcome_unknown") from None

    def scan(self, *, kind, filters, limit, cursor=None):
        try:
            query = self.collection.where(filter=self.field_filter("kind", "==", kind))
            for key, value in filters.items():
                query = query.where(filter=self.field_filter(key, "==", value))
            query = query.order_by("__name__")
            if cursor:
                query = query.start_after({"__name__": self.collection.document(cursor)})
            return [row.to_dict() for row in query.limit(limit).stream(retry=None, timeout=10)]
        except Exception:
            raise QueueError("queue_query_failed") from None

    def close(self):
        self.client.close()


class QueueStore:
    def __init__(self, backend, *, clock=None):
        self.backend = backend
        self.clock = clock or time.time

    def _run(self, callback):
        def run(tx):
            now = self.clock()
            require(type(now) in (int, float) and 0 < now < 10_000_000_000, "invalid_clock")
            return callback(tx, now)
        return self.backend.transact(run)

    @staticmethod
    def _control(tx):
        control = tx.get("control")
        require(control is not None and control.get("kind") == "control", "control_missing")
        return control

    @staticmethod
    def _open(control, expected_generation, now):
        require(type(expected_generation) is int and expected_generation == control["generation"]
                and control["mode"] == "queue_open", "global_stop_or_stale_generation")
        require(now >= control["changed_at"], "clock_before_control")

    @staticmethod
    def _work(tx, work_id):
        text_id(work_id)
        work = tx.get(work_id)
        require(work is not None and work.get("kind") == "work", "work_missing")
        return work

    @staticmethod
    def _event_key(work_id, sequence):
        return f"event-{work_id}-{sequence:016d}"

    def initialize(self):
        def op(tx, now):
            old = tx.get("control")
            if old is not None:
                return old
            row = {"id": "control", "kind": "control", "mode": "stopped",
                   "generation": 0, "changed_at": now, "actor": "bootstrap"}
            tx.put("control", row)
            return row
        return self._run(op)

    def set_control(self, mode, *, actor, expected_generation):
        require(mode in ("queue_open", "stopped", "takeover"), "invalid_control_mode")
        text_id(actor)
        def op(tx, now):
            c = self._control(tx)
            require(type(expected_generation) is int and expected_generation == c["generation"],
                    "stale_generation")
            require(mode != "queue_open" or now >= c["changed_at"], "clock_before_control")
            c.update(mode=mode, generation=c["generation"] + 1,
                     changed_at=max(now, c["changed_at"]), actor=actor)
            tx.put("control", c)
            return c
        return self._run(op)

    def control(self):
        return self._run(lambda tx, _: self._control(tx))

    def bind_work(self, *, brand_id, thread_id, account_id, project_id, list_id,
                  service_id, expected_generation):
        require(brand_id in BRANDS, "unknown_brand")
        text_id(thread_id); text_id(service_id)
        for value in (account_id, project_id, list_id):
            positive(value)
        binding = {"brand_id": brand_id, "thread_id": thread_id, "account_id": account_id,
                   "project_id": project_id, "list_id": list_id, "service_id": service_id}
        work_id = "work-" + digest(brand_id, thread_id, account_id, project_id)
        thread_key = "thread-" + digest(thread_id)
        def op(tx, now):
            c = self._control(tx); self._open(c, expected_generation, now)
            index = tx.get(thread_key)
            work = tx.get(work_id)
            require(index is None or index["work_id"] == work_id, "thread_already_bound_to_other_scope")
            if work is not None:
                require(all(work[k] == value for k, value in binding.items()), "work_binding_conflict")
                return work
            work = {"id": work_id, "kind": "work", "work_id": work_id, **binding,
                    "task_id": None, "mode": "active", "queue_state": "idle",
                    "version": 0, "work_generation": 0, "head_sequence": 1,
                    "next_sequence": 1, "last_source_order": {}, "lease": None,
                    "inflight_action_id": None, "created_at": now, "updated_at": now}
            tx.put(thread_key, {"id": thread_key, "kind": "thread", "work_id": work_id})
            tx.put(work_id, work)
            return work
        return self._run(op)

    def get_work(self, work_id):
        return self._run(lambda tx, _: self._work(tx, work_id))

    def bind_task(self, work_id, *, task_id, account_id, project_id, list_id, expected_generation):
        """Accept only a trusted exact provider readback; never retarget a work."""
        positive(task_id)
        def op(tx, now):
            c = self._control(tx); self._open(c, expected_generation, now)
            w = self._work(tx, work_id)
            require(w["mode"] == "active", "work_taken_over")
            require((account_id, project_id, list_id) == (w["account_id"], w["project_id"], w["list_id"]),
                    "task_project_mismatch")
            require(w["task_id"] in (None, task_id), "task_binding_conflict")
            # Independent global uniqueness prevents reusing a task across jobs.
            key = "task-" + digest(account_id, project_id, task_id)
            old = tx.get(key)
            require(old is None or old["work_id"] == work_id, "task_already_bound")
            w.update(task_id=task_id, updated_at=now)
            tx.put(key, {"id": key, "kind": "task", "work_id": work_id})
            tx.put(work_id, w)
            return w
        return self._run(op)

    def thread_work(self, thread_id):
        """The work already bound to this mail thread, or None (read-only)."""
        text_id(thread_id)
        def op(tx, _):
            index = tx.get("thread-" + digest(thread_id))
            return None if index is None else tx.get(index["work_id"])
        return self._run(op)

    def retarget_list(self, claim, *, list_id, allowed_list_ids):
        """Before any task or write exists, move the claimed work to another read-back list
        of the SAME project (brand per-kind lists). Never changes account/project."""
        positive(list_id)
        require(isinstance(allowed_list_ids, (list, tuple, set, frozenset)) and list_id in allowed_list_ids,
                "list_not_allowed")
        def op(tx, now):
            w, _ = self._claimed(tx, claim, now)
            require(w["task_id"] is None and w["inflight_action_id"] is None, "work_list_frozen")
            if w["list_id"] != list_id:
                w.update(list_id=list_id, updated_at=now)
                tx.put(w["id"], w)
            return w
        return self._run(op)

    def enqueue_message(self, work_id, *, message_id, fingerprint, source_order, expected_generation):
        return self.enqueue_event(work_id, event_id=message_id, fingerprint=fingerprint,
                                  source_order=source_order, source_kind="gmail",
                                  expected_generation=expected_generation)

    def enqueue_event(self, work_id, *, event_id, fingerprint: str, source_order,
                      expected_generation, source_kind="gmail"):
        text_id(event_id)
        _fingerprint(fingerprint)
        require(source_kind in ("gmail", "basecamp"), "invalid_source_kind")
        positive(source_order)
        dedup_key = "source-" + digest(source_kind, event_id)
        def op(tx, now):
            c = self._control(tx); self._open(c, expected_generation, now)
            w = self._work(tx, work_id)
            prior = tx.get(dedup_key)
            if prior is not None:
                require(prior["work_id"] == work_id and prior["fingerprint"] == fingerprint,
                        "source_identity_conflict")
                return {"duplicate": True, "event": tx.get(prior["queue_event_id"])}
            # Ingestion order is enforced per provider. No latest-first replay.
            order = [source_order, event_id]
            require(order > w["last_source_order"].get(source_kind, [0, ""]), "out_of_order_source")
            require(w["next_sequence"] < 10**16, "sequence_exhausted")
            seq = w["next_sequence"]
            key = self._event_key(work_id, seq)
            row = {"id": key, "kind": "event", "work_id": work_id, "sequence": seq,
                   "source_kind": source_kind, "source_id": event_id, "source_order": source_order,
                   "fingerprint": fingerprint, "state": "queued", "created_at": now}
            w["next_sequence"] += 1
            w["last_source_order"][source_kind] = order
            if w["queue_state"] == "idle":
                w["queue_state"] = "ready"
            w["updated_at"] = now
            tx.put(dedup_key, {"id": dedup_key, "kind": "source", "work_id": work_id,
                               "fingerprint": fingerprint, "queue_event_id": key})
            tx.put(key, row); tx.put(work_id, w)
            return {"duplicate": False, "event": row}
        return self._run(op)

    def claim_next(self, work_id, *, worker_id, expected_generation, lease_seconds=60):
        text_id(worker_id)
        require(type(lease_seconds) is int and 1 <= lease_seconds <= 300, "invalid_lease_duration")
        def op(tx, now):
            c = self._control(tx); self._open(c, expected_generation, now)
            w = self._work(tx, work_id)
            require(w["mode"] == "active", "work_taken_over")
            require(w["inflight_action_id"] is None, "write_outcome_unresolved")
            if w["head_sequence"] >= w["next_sequence"]:
                return None
            old = w["lease"]
            require(old is None or old["expires_at"] <= now or old["global_generation"] != c["generation"],
                    "work_lease_busy")
            event = tx.get(self._event_key(work_id, w["head_sequence"]))
            require(event is not None and event["state"] in ("queued", "claimed"), "event_not_claimable")
            w["version"] += 1
            claim = {"work_id": work_id, "event_id": event["id"], "token": uuid.uuid4().hex,
                     "worker_id": worker_id, "global_generation": c["generation"],
                     "work_generation": w["work_generation"], "work_version": w["version"],
                     "issued_at": now, "expires_at": now + lease_seconds}
            w.update(lease=claim, queue_state="leased", updated_at=now)
            event.update(state="claimed", last_claim_token=claim["token"])
            tx.put(w["id"], w); tx.put(event["id"], event)
            return {**claim, "event": event}
        return self._run(op)

    def _claimed(self, tx, claim, now):
        require(isinstance(claim, dict), "invalid_claim")
        c = self._control(tx); self._open(c, claim.get("global_generation"), now)
        w = self._work(tx, claim.get("work_id"))
        require(w["mode"] == "active", "work_taken_over")
        lease = w["lease"]
        fields = ("work_id", "event_id", "token", "worker_id", "global_generation",
                  "work_generation", "work_version", "issued_at", "expires_at")
        require(isinstance(lease, dict) and all(lease[k] == claim.get(k) for k in fields)
                and w["work_generation"] == claim["work_generation"]
                and w["version"] == claim["work_version"], "stale_claim")
        require(claim["issued_at"] <= now < lease["expires_at"], "lease_expired")
        e = tx.get(lease["event_id"])
        require(e is not None and e["state"] == "claimed", "event_not_claimed")
        return w, e

    def begin_write(self, claim, *, action_kind, payload_sha256, action_key, prepared_binding):
        """Fence BEFORE exactly one provider attempt; never returns retry=True.

        Caller must perform trusted autonomy_policy + budget checks immediately
        before this method. This record itself is not permission to send.
        """
        require(action_kind in ("acknowledge", "clarify_brief", "create_task", "update_task",
                "add_comment", "assign_task", "remind", "deliver_file"), "invalid_action_kind")
        require(isinstance(claim, dict), "invalid_claim")
        _fingerprint(payload_sha256); text_id(action_key)
        action_id = "action-" + digest(claim.get("work_id"), claim.get("event_id"), action_key)
        def op(tx, now):
            w, e = self._claimed(tx, claim, now)
            existing = tx.get(action_id)
            require(isinstance(prepared_binding, dict)
                    and set(prepared_binding) == set(WRITE_BINDING_FIELDS) | {"queue_event_fingerprint"}
                    and all(prepared_binding[k] == w[k] for k in WRITE_BINDING_FIELDS)
                    and prepared_binding["queue_event_fingerprint"] == e["fingerprint"],
                    "prepared_work_or_source_mismatch")
            require(existing is None, "action_already_started")
            require(w["inflight_action_id"] is None, "write_outcome_unresolved")
            action = {"id": action_id, "kind": "action", "work_id": w["id"], "event_id": e["id"],
                      "action_kind": action_kind, "action_key": action_key, "payload_sha256": payload_sha256,
                      "prepared_binding": copy.deepcopy(prepared_binding),
                      "claim_token": claim["token"], "state": "started", "started_at": now,
                      "automatic_retry_allowed": False, "receipt": None}
            w.update(inflight_action_id=action_id, queue_state="write_pending", updated_at=now)
            tx.put(action_id, action); tx.put(w["id"], w)
            return action
        return self._run(op)

    def confirm_write(self, *, action_id, claim_token, payload_sha256, receipt):
        """Store trusted exact provider readback, including after stop/takeover.

        receipt is minimal metadata, not mail body: provider, remote_id,
        readback_sha256. Hash must match the submitted canonical action payload.
        A reconciliation adapter may record this; no retry or resume occurs.
        """
        text_id(action_id); text_id(claim_token); _fingerprint(payload_sha256)
        require(isinstance(receipt, dict) and set(receipt) == {"provider", "remote_id", "readback_sha256"},
                "invalid_receipt")
        require(receipt["provider"] in ("gmail", "basecamp"), "invalid_receipt_provider")
        text_id(receipt["remote_id"])
        require(receipt["readback_sha256"] == payload_sha256, "readback_payload_mismatch")
        def op(tx, now):
            a = tx.get(action_id)
            require(a is not None and a.get("kind") == "action" and a["claim_token"] == claim_token
                    and a["payload_sha256"] == payload_sha256, "action_receipt_binding_mismatch")
            w = self._work(tx, a["work_id"])
            expected_provider = "gmail" if a["action_kind"] in ("acknowledge", "clarify_brief", "remind", "deliver_file") else "basecamp"
            require(receipt["provider"] == expected_provider, "receipt_provider_mismatch")
            if a["state"] == "confirmed":
                require(a["receipt"] == receipt, "receipt_conflict")
                return a
            require(a["state"] in ("started", "unknown") and w["inflight_action_id"] == action_id,
                    "action_not_reconcilable")
            a.update(state="confirmed", receipt=copy.deepcopy(receipt), confirmed_at=now)
            w.update(inflight_action_id=None, queue_state="leased" if w["lease"] else "ready", updated_at=now)
            tx.put(action_id, a); tx.put(w["id"], w)
            return a
        return self._run(op)

    def hold_unknown(self, action_id):
        """Permanent no-retry hold until a trusted exact receipt is reconciled."""
        text_id(action_id)
        def op(tx, now):
            a = tx.get(action_id)
            require(a is not None and a.get("kind") == "action", "action_missing")
            w = self._work(tx, a["work_id"])
            require(a["state"] in ("started", "unknown"), "action_not_unresolved")
            a.update(state="unknown", held_at=now)
            w.update(queue_state="unknown", updated_at=now)
            tx.put(action_id, a); tx.put(w["id"], w)
            return a
        return self._run(op)

    def complete_event(self, claim, *, outcome, evidence_ref):
        require(outcome in ("processed", "superseded", "no_action", "held_for_human"), "invalid_event_outcome")
        text_id(evidence_ref)
        def op(tx, now):
            w, e = self._claimed(tx, claim, now)
            require(w["inflight_action_id"] is None, "write_outcome_unresolved")
            e.update(state="finished", outcome=outcome, evidence_ref=evidence_ref, finished_at=now)
            w["head_sequence"] += 1
            w.update(lease=None, queue_state="ready" if w["head_sequence"] < w["next_sequence"] else "idle",
                     updated_at=now)
            # A human hold blocks every later event of this work until explicit
            # resumption; it does not block unrelated work.
            if outcome == "held_for_human":
                w.update(mode="takeover", work_generation=w["work_generation"] + 1)
            tx.put(e["id"], e); tx.put(w["id"], w)
            return e
        return self._run(op)

    def set_work_mode(self, work_id, *, mode, actor, expected_work_generation):
        require(mode in ("active", "takeover", "stopped"), "invalid_work_mode")
        text_id(actor)
        def op(tx, now):
            w = self._work(tx, work_id)
            require(type(expected_work_generation) is int and expected_work_generation == w["work_generation"],
                    "stale_work_generation")
            require(mode != "active" or w["inflight_action_id"] is None, "write_outcome_unresolved")
            w.update(mode=mode, actor=actor, work_generation=w["work_generation"] + 1,
                     version=w["version"] + 1, lease=None, updated_at=now)
            if w["inflight_action_id"]:
                w["queue_state"] = "unknown"
            else:
                w["queue_state"] = "ready" if w["head_sequence"] < w["next_sequence"] else "idle"
            tx.put(w["id"], w)
            return w
        return self._run(op)

    def page(self, *, kind="work", work_id=None, queue_state=None, limit=25, cursor=None):
        """Bounded immutable-ID keyset page, no global array or 50-work ceiling.

        A query is a discovery hint, never a claim. Concurrent changes may move
        a work out of a filtered page; start a fresh sweep after the last page.
        """
        require(kind in ("work", "event", "action"), "invalid_page_kind")
        require(type(limit) is int and 1 <= limit <= MAX_PAGE, "invalid_page_limit")
        filters = {}
        if work_id is not None:
            text_id(work_id); filters["work_id"] = work_id
        if queue_state is not None:
            require(kind == "work" and queue_state in ("idle", "ready", "leased", "write_pending", "unknown"),
                    "invalid_page_state")
            filters["queue_state"] = queue_state
        if cursor is not None:
            require(isinstance(cursor, str) and re.fullmatch(r"[a-z0-9-]{1,200}", cursor), "invalid_page_cursor")
        rows = self.backend.scan(kind=kind, filters=filters, limit=limit + 1, cursor=cursor)
        return {"items": rows[:limit], "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}
