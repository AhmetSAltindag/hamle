"""Single internal-pilot durable state and at-most-once provider-action journal.

Uses OperationsStore transactions, never performs provider/network operations.
Fresh authenticated mail/private-project checks remain the dispatcher's duty.
"""
from __future__ import annotations

import copy
import hashlib
from datetime import datetime, timezone

from cloud_store import (OperationsStore, StoreError, START, DEADLINE, LIMIT,
                         _hash, _json, _label, _require, proposal_hash)


PILOT_ID = "HAMLE-IC-URETIM-20261010-01"
PILOT_DOC = "v11-internal-production-20261010-01"
MAX_ACTIONS = 64
SOURCE_FIELDS = {"verified", "subject", "thread_id", "message_id", "source_fingerprint",
                 "control_generation", "captured_at", "expires_at", "evidence_ref"}


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        _require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
                 "pilot_aware_timestamp_required")
        return parsed.timestamp()
    except (TypeError, ValueError):
        raise StoreError("pilot_timestamp_invalid") from None


def _source(value):
    import re
    _require(isinstance(value, dict) and set(value) == SOURCE_FIELDS,
             "pilot_source_fields_invalid")
    _require(value["verified"] is True and value["subject"] == PILOT_ID,
             "pilot_source_not_verified")
    for key in ("thread_id", "message_id"):
        _require(isinstance(value[key], str) and re.fullmatch(r"[0-9a-f]{1,64}", value[key]),
                 "pilot_source_identity_invalid")
    _hash(value["source_fingerprint"]); _label(value["evidence_ref"])
    _require(type(value["control_generation"]) is int and value["control_generation"] >= 0,
             "pilot_generation_invalid")
    captured, expiry = _time(value["captured_at"]), _time(value["expires_at"])
    _require(START <= captured < expiry <= DEADLINE, "pilot_expiry_invalid")
    _json(value)
    return copy.deepcopy(value)


def _state(value, source):
    _require(isinstance(value, dict) and value.get("mode") == "internal_pilot",
             "pilot_internal_state_required")
    _label(value.get("work_id"))
    _require(value.get("control") in ("active", "stopped", "takeover"), "pilot_state_control_invalid")
    if "brief" in value:
        brief = value["brief"]
        _require(isinstance(brief, dict)
                 and brief.get("reply_envelope", {}).get("thread_id") == source["thread_id"]
                 and brief.get("binding", {}).get("account_id") == 5898865
                 and brief.get("binding", {}).get("project_id") == 49184053,
                 "pilot_state_scope_mismatch")
    _json(value)
    return copy.deepcopy(value)


def _action_doc(key):
    _label(key)
    return PILOT_DOC + "-action-" + hashlib.sha256(key.encode()).hexdigest()


def _view(row, *, dispatch_allowed=False):
    result = copy.deepcopy(row)
    result["dispatch_allowed"] = dispatch_allowed
    return result


class InternalPilotStore:
    def __init__(self, ops):
        _require(isinstance(ops, OperationsStore), "operations_store_required")
        self.ops = ops

    @staticmethod
    def _pilot(tx):
        row = tx.get(PILOT_DOC)
        _require(isinstance(row, dict) and row.get("pilot_id") == PILOT_ID,
                 "pilot_not_initialized")
        _require(type(row.get("revision")) is int and row["revision"] >= 0
                 and isinstance(row.get("action_keys"), list)
                 and len(row["action_keys"]) <= MAX_ACTIONS
                 and len(set(row["action_keys"])) == len(row["action_keys"]),
                 "pilot_record_invalid")
        _require(proposal_hash(row["state"]) == row["state_sha256"]
                 and proposal_hash(row["source_evidence"]) == row["source_evidence_sha256"],
                 "pilot_integrity_mismatch")
        _source(row["source_evidence"]); _state(row["state"], row["source_evidence"])
        _require(row["generation"] == row["source_evidence"]["control_generation"]
                 and row["expires_at"] == _time(row["source_evidence"]["expires_at"]),
                 "pilot_binding_mismatch")
        return row

    def _gate(self, tx, row, now, *, dispatch=False):
        control, budget = self.ops._control(tx), self.ops._budget(tx)
        self.ops._open(control, row["generation"], now)
        _require(row["created_at"] <= now < row["expires_at"], "pilot_expired_or_clock_regressed")
        _require(not budget["overrun_hold"] and budget["providers"]["google_cloud"]["billing_ready"]
                 and self.ops._totals(budget)["total_committed_microdollars"] < LIMIT,
                 "budget_not_ready")
        if dispatch:
            _require(row["state"]["control"] == "active", "pilot_work_stopped_or_taken_over")

    @staticmethod
    def _action(tx, pilot, key):
        row = tx.get(_action_doc(key))
        _require(key in pilot["action_keys"] and isinstance(row, dict)
                 and row.get("pilot_id") == PILOT_ID and row.get("action_key") == key,
                 "pilot_action_missing")
        _require(row.get("state") in ("prepared", "started", "receipt_saved", "unknown", "verified")
                 and row.get("generation") == pilot["generation"]
                 and row.get("source_evidence_sha256") == pilot["source_evidence_sha256"]
                 and row.get("payload_sha256") == proposal_hash(row["payload"]),
                 "pilot_action_integrity_mismatch")
        if row.get("receipt") is not None:
            _require(row.get("receipt_sha256") == proposal_hash(row["receipt"]),
                     "pilot_receipt_integrity_mismatch")
        if row["state"] == "verified":
            proof = row.get("proof")
            _require(isinstance(proof, dict) and proof.get("verified") is True
                     and proof.get("action_key") == key
                     and proof.get("payload_sha256") == row["payload_sha256"]
                     and proof.get("receipt_sha256") == row["receipt_sha256"]
                     and row.get("proof_sha256") == proposal_hash(proof),
                     "pilot_proof_integrity_mismatch")
        return row

    def initialize(self, state, source_evidence):
        source = _source(source_evidence)
        initial = _state(state, source)
        source_hash, initial_hash = proposal_hash(source), proposal_hash(initial)
        def op(tx, now):
            old = tx.get(PILOT_DOC)
            row = {"schema_version": 1, "pilot_id": PILOT_ID, "revision": 0,
                   "generation": source["control_generation"], "created_at": now, "updated_at": now,
                   "expires_at": _time(source["expires_at"]), "source_evidence": source,
                   "source_evidence_sha256": source_hash, "initial_state_sha256": initial_hash,
                   "state": initial, "state_sha256": initial_hash, "action_keys": []}
            if old is not None:
                old = self._pilot(tx)
                _require(old["source_evidence_sha256"] == source_hash
                         and old["initial_state_sha256"] == initial_hash, "pilot_initialization_conflict")
                self._gate(tx, old, now)
                return old
            _require(0 <= now - _time(source["captured_at"]) <= 300, "pilot_source_capture_stale")
            self._gate(tx, row, now)
            _json(row); tx.put(PILOT_DOC, row)
            return row
        return self.ops._run(op)

    def read(self):
        def op(tx, _):
            return None if tx.get(PILOT_DOC) is None else self._pilot(tx)
        return self.ops._run(op, allow_expired=True)

    def save(self, state, expected_revision):
        _require(type(expected_revision) is int and expected_revision >= 0, "pilot_revision_invalid")
        def op(tx, now):
            row = self._pilot(tx)
            updated = _state(state, row["source_evidence"])
            _require(updated["work_id"] == row["state"]["work_id"], "pilot_work_identity_changed")
            _require(row["state"]["control"] == "active" or updated["control"] == row["state"]["control"],
                     "pilot_stopped_work_cannot_auto_resume")
            _require(row["revision"] == expected_revision, "pilot_revision_conflict")
            self._gate(tx, row, now)
            row.update(state=updated, state_sha256=proposal_hash(updated), revision=row["revision"] + 1,
                       updated_at=now)
            _json(row); tx.put(PILOT_DOC, row)
            return row
        return self.ops._run(op)

    def prepare_action(self, action_key, payload):
        document, digest = _action_doc(action_key), proposal_hash(payload)
        def op(tx, now):
            pilot = self._pilot(tx)
            old = tx.get(document)
            self._gate(tx, pilot, now, dispatch=True)
            if old is not None:
                old = self._action(tx, pilot, action_key)
                _require(old["payload_sha256"] == digest, "pilot_action_payload_conflict")
                return _view(old)
            _require(action_key not in pilot["action_keys"] and len(pilot["action_keys"]) < MAX_ACTIONS,
                     "pilot_action_index_full_or_invalid")
            row = {"pilot_id": PILOT_ID, "action_key": action_key, "payload": copy.deepcopy(payload),
                   "payload_sha256": digest, "source_evidence_sha256": pilot["source_evidence_sha256"],
                   "generation": pilot["generation"], "pilot_state_sha256": pilot["state_sha256"],
                   "state": "prepared", "created_at": now, "receipt": None,
                   "receipt_sha256": None, "proof": None, "failure_reason": None}
            pilot["action_keys"].append(action_key)
            _json(row); _json(pilot)
            tx.put(document, row); tx.put(PILOT_DOC, pilot)
            return _view(row)
        return self.ops._run(op)

    def begin_action(self, action_key, payload_sha256):
        _action_doc(action_key); _hash(payload_sha256)
        def op(tx, now):
            pilot = self._pilot(tx)
            row = self._action(tx, pilot, action_key)
            self._gate(tx, pilot, now, dispatch=True)
            _require(row["payload_sha256"] == payload_sha256, "pilot_action_payload_conflict")
            if row["state"] == "verified":
                return _view(row)
            _require(row["state"] == "prepared", "pilot_action_started_or_unknown_no_retry")
            _require(row["pilot_state_sha256"] == pilot["state_sha256"], "pilot_prepared_state_changed")
            row.update(state="started", started_at=now)
            _json(row); tx.put(_action_doc(action_key), row)
            return _view(row, dispatch_allowed=True)
        return self.ops._run(op)

    def record_receipt(self, action_key, receipt):
        """Persist provider response before readback; this NEVER permits a POST."""
        _action_doc(action_key)
        _require(isinstance(receipt, dict) and bool(receipt), "pilot_provider_receipt_required")
        receipt_hash = proposal_hash(receipt)
        def op(tx, now):
            pilot = self._pilot(tx)
            row = self._action(tx, pilot, action_key)
            _require(row["state"] in ("started", "receipt_saved", "unknown", "verified"),
                     "pilot_action_not_started")
            if row["receipt"] is not None:
                _require(row["receipt_sha256"] == receipt_hash, "pilot_receipt_conflict")
                return _view(row)
            _require(row["state"] != "verified", "pilot_receipt_integrity_mismatch")
            row.update(state="receipt_saved", receipt=copy.deepcopy(receipt), receipt_sha256=receipt_hash,
                       receipt_saved_at=now)
            _json(row); tx.put(_action_doc(action_key), row)
            return _view(row)
        return self.ops._run(op, allow_expired=True)

    def finish_action(self, action_key, receipt, proof):
        _action_doc(action_key)
        _require(isinstance(receipt, dict) and bool(receipt), "pilot_provider_receipt_required")
        receipt_hash = proposal_hash(receipt)
        _require(isinstance(proof, dict) and proof.get("verified") is True
                 and proof.get("action_key") == action_key and proof.get("receipt_sha256") == receipt_hash,
                 "pilot_readback_proof_invalid")
        _hash(proof.get("payload_sha256")); _label(proof.get("evidence_ref")); _json(proof)
        proof_hash = proposal_hash(proof)
        def op(tx, now):
            pilot = self._pilot(tx)
            row = self._action(tx, pilot, action_key)
            _require(row["state"] in ("started", "receipt_saved", "unknown", "verified"),
                     "pilot_action_not_started")
            _require(row["payload_sha256"] == proof["payload_sha256"], "pilot_readback_payload_mismatch")
            if row["receipt"] is not None:
                _require(row["receipt_sha256"] == receipt_hash, "pilot_receipt_conflict")
            if row["state"] == "verified":
                _require(row["proof_sha256"] == proof_hash, "pilot_readback_proof_conflict")
                return _view(row)
            row.update(state="verified", receipt=copy.deepcopy(receipt), receipt_sha256=receipt_hash,
                       proof=copy.deepcopy(proof), proof_sha256=proof_hash, verified_at=now)
            _json(row); tx.put(_action_doc(action_key), row)
            return _view(row)
        return self.ops._run(op, allow_expired=True)

    def fail_action(self, action_key, reason):
        _action_doc(action_key); _label(reason)
        def op(tx, now):
            pilot = self._pilot(tx)
            row = self._action(tx, pilot, action_key)
            _require(row["state"] in ("started", "receipt_saved", "unknown"), "pilot_action_not_unconfirmed")
            if row["state"] == "unknown":
                return _view(row)
            # Preserve the provider's receipt even when its readback failed.
            row.update(state="unknown", failure_reason=reason, failed_at=now)
            _json(row); tx.put(_action_doc(action_key), row)
            return _view(row)
        return self.ops._run(op, allow_expired=True)

    def summary(self):
        def op(tx, now):
            if tx.get(PILOT_DOC) is None:
                return {"pilot_id": PILOT_ID, "initialized": False, "dispatch_allowed": False}
            pilot = self._pilot(tx)
            control = self.ops._control(tx)
            actions = [self._action(tx, pilot, key) for key in pilot["action_keys"]]
            return {"pilot_id": PILOT_ID, "initialized": True, "revision": pilot["revision"],
                    "state_sha256": pilot["state_sha256"], "work_status": pilot["state"].get("status"),
                    "generation": pilot["generation"], "current_generation": control["generation"],
                    "control_mode": control["mode"], "expires_at": pilot["expires_at"],
                    "expired": now >= pilot["expires_at"], "dispatch_allowed": False,
                    "actions": [{"action_key": row["action_key"], "state": row["state"],
                                 "payload_sha256": row["payload_sha256"],
                                 "receipt_saved": row["receipt"] is not None,
                                 "verified": row["state"] == "verified"} for row in actions]}
        return self.ops._run(op, allow_expired=True)
