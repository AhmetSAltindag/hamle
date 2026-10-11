"""Send authority for the v1.1 internal chain from the EXISTING visible-stop work authority.

Uses v11_work_authority's pure functions unchanged (derive_work_authority_from_intake,
authorize_work_authority via autonomy_policy.authorize_action) and WorkAuthorityStore's
budget snapshot. Grants live in the chain queue backend (same transactions as chain
works), because WorkAuthorityStore's BoundAuthorityQueueBackend serves only the
hamle_internal_v1 queue and forbids the chain's plain begin_write.

  on_intake(view, source, context)  -> derive + persist ONE grant after the task exists
  authority_source(dwork, scope, src) -> stored delegation + grant + fresh readback
  stop_sweep(queue, reader)         -> visible stop/takeover mail fences chain works

Prepared policy file: outputs/gamechangers-internal/gamechangers-internal-work-authority-policy.json
(live_enabled=false, pending Ahmet). Without an enabled, accepted policy nothing is
derived and every delivery stays held (as before). No network here; mail evidence
comes from the injected verified Gmail adapter.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path

from autonomy_policy import HAMLE, scope_digest
from v11_stop_intake import stop_intent
from v11_work_authority import (TrustedIntakeEvidence, WorkAuthorityError, business_day_expiry,
                                derive_work_authority_from_intake, validate_work_authority_policy)
from v11_work_authority_store import WorkAuthorityStore
from v11_work_queue import digest

POLICY_NAME = "gamechangers-internal-work-authority-policy.json"
ADAPTER_ID = "hamle-authenticated-intake-v1"
SOURCE_FIELDS = ("actual_latest", "own_or_automatic", "provider_evidence_ref", "message_id", "thread_id",
                 "fingerprint", "sender", "to", "cc", "received_at", "captured_at")


class ChainAuthorityError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ChainAuthorityError(code)


def _stamp(value):
    d = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    _need(d.tzinfo is not None, "timestamp_timezone_required")
    return d.astimezone(timezone.utc)


def load_policy_file(path=None):
    root = Path(__file__).resolve().parents[2]
    candidates = [Path(path)] if path else [Path(__file__).with_name("chain-profile") / POLICY_NAME,
                                            root / "outputs/gamechangers-internal" / POLICY_NAME]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def enabled_policy(document):
    """The policy object only when the file is live AND the policy is accepted/enabled.

    Acceptance itself (actor, evidence, terms digest, expiry) is checked by
    v11_work_authority.validate_work_authority_policy on every use.
    """
    if not isinstance(document, dict) or document.get("live_enabled") is not True:
        return None
    policy = document.get("work_authority_policy")
    if not isinstance(policy, dict) or policy.get("enabled") is not True or policy.get("status") != "accepted":
        return None
    return copy.deepcopy(policy)


class ChainWorkAuthority:
    def __init__(self, *, queue, ops, catalog, policy, source_reader, clock):
        self.q, self.ops, self.catalog = queue, ops, catalog
        self.policy = copy.deepcopy(policy) if policy else None
        self.source_reader, self.clock = source_reader, clock
        self.last_reason = None

    # ----- durable rows in the chain queue backend
    @staticmethod
    def key(work_id):
        return "chain-authority-" + digest(work_id)

    def record(self, work_id):
        return self.q._run(lambda tx, _: tx.get(self.key(work_id)))

    def _put_once(self, row):
        def op(tx, now):
            old = tx.get(row["id"])
            if old is not None:
                return old
            row["created_at"] = now
            tx.put(row["id"], row)
            return row
        return self.q._run(op)

    def budget(self):
        """Existing WorkAuthorityStore budget snapshot (shared hamle_internal_v1 ledger)."""
        store = WorkAuthorityStore(self.ops)
        return self.ops._run(lambda tx, now: store._budget(tx, now), allow_expired=True)

    # ----- helpers
    def _service(self, scope, service_id):
        rows = [s for s in scope["services"] if s["service_id"] == service_id]
        _need(len(rows) == 1 and rows[0].get("work_authority_policy") == self.policy, "work_policy_not_in_scope")
        return rows[0]

    def _control(self, work, now):
        control = self.q.control()
        _need(control["mode"] == "queue_open" and work["mode"] == "active", "work_or_queue_not_active")
        return {"work_id": work["work_id"], "global_mode": "routine_enabled", "work_mode": work["mode"],
                "global_generation": control["generation"], "work_generation": work["work_generation"],
                "observed_at": now.isoformat()}

    # ----- intake: derive the grant from the accepted brand policy
    def on_intake(self, view, scope, context):
        # context: {'risk_area', 'work_brief_sha256' (= quality delivery brief digest)}
        """Called once, right after the chain's task write was read back.

        Grant = known agency requester + Hamle in actual To/CC (fresh provider
        read of the reply anchor) + accepted policy. Expiry = earliest of two
        Istanbul weekdays from mail arrival, scope, policy acceptance, budget.
        """
        try:
            _need(self.policy is not None, "work_authority_policy_not_enabled")
            now = self.clock()
            work = self.q.get_work(view["work_id"])
            service = self._service(scope, work["service_id"])
            pdigest = validate_work_authority_policy(self.policy, scope, service, now=now)
            _need(context.get("risk_area") == "routine", "work_not_routine")
            src = self.source_reader(view["thread_id"], view["reply"]["in_reply_to_message_id"])
            source = {k: copy.deepcopy(src[k]) for k in SOURCE_FIELDS}
            _need(source["actual_latest"] is True and HAMLE in source["to"] + source["cc"]
                  and source["sender"] in scope["agency_contacts"], "requester_or_to_cc_unverified")
            budget = self.budget()
            control = self._control(work, now)
            acceptance = self.policy["acceptance"]
            expires = min(_stamp(business_day_expiry(source["received_at"])), _stamp(scope["authority"]["expires_at"]),
                          _stamp(acceptance["expires_at"]), _stamp(budget["expires_at"]))
            delegation = {"scope_digest": scope_digest(scope), "work_id": work["work_id"],
                          "service_id": work["service_id"], "brand_id": work["brand_id"],
                          "thread_id": work["thread_id"], "actor": acceptance["actor"],
                          "evidence_ref": "chain-derived-delegation:" + acceptance["evidence_ref"][:200],
                          "issued_at": _stamp(source["received_at"]).isoformat(), "expires_at": expires.isoformat(),
                          "revoked": False, "global_generation": control["global_generation"],
                          "work_generation": control["work_generation"], "actions": sorted(self.policy["actions"]),
                          "basis": "accepted_brand_policy_derivation", "parent_policy_digest": pdigest,
                          "parent_acceptance_ref": acceptance["evidence_ref"],
                          "source_fingerprint": source["fingerprint"]}
            request = {"work_id": work["work_id"], "brand_id": work["brand_id"], "service_id": work["service_id"],
                       "risk_area": "routine", "open_escalation": False, "action_state": "new",
                       "control": control, "source": source, "delegation": delegation,
                       "work_brief_sha256": context["work_brief_sha256"]}
            from v11_work_authority import build_work_authority_binding
            binding = build_work_authority_binding(scope, request)
            intake = TrustedIntakeEvidence(
                adapter_id=ADAPTER_ID, provider_identity=HAMLE, evidence_ref=source["provider_evidence_ref"],
                classification_evidence_ref="chain-plan-intake:ready_internal_task:routine:" + str(view["brief_sha256"]),
                binding_sha256=scope_digest(binding), parent_policy_digest=pdigest, observed_at=now.isoformat())
            grant = derive_work_authority_from_intake(self.policy, scope, request, intake, budget, now=now)
            row = self._put_once({"id": self.key(work["work_id"]), "kind": "chain_work_authority",
                                  "work_id": work["work_id"], "grant": grant, "delegation": delegation,
                                  "stops": []})
            self.last_reason = "granted"
            return {"status": "granted", "grant_id": row["grant"]["grant_id"],
                    "expires_at": row["grant"]["expires_at"]}
        except Exception as exc:  # no grant -> delivery stays held, nothing else changes
            self.last_reason = getattr(exc, "code", None) or (str(exc) if isinstance(exc, WorkAuthorityError)
                                                              else type(exc).__name__)
            return {"status": "not_granted", "reason": str(self.last_reason)[:120]}

    # ----- delivery: stored grant + fresh durable readback
    def authority_source(self, dwork, scope, source):
        row = self.record(dwork["work_id"])
        if self.policy is None or row is None or row["grant"].get("status") != "active":
            return {}
        now = self.clock()
        grant = row["grant"]
        return {"delegation": copy.deepcopy(row["delegation"]), "work_authority_grant": copy.deepcopy(grant),
                "work_authority_observation": {
                    "verified": True, "grant_id": grant["grant_id"], "grant_sha256": scope_digest(grant),
                    "grant_revision": grant["revision"], "observed_at": now.isoformat(),
                    "evidence_ref": "chain-queue:" + row["id"]},
                "budget_authorization": self.budget()}

    # ----- visible stop / takeover
    def revoke(self, work_id, *, actor, evidence_ref, reason):
        now = self.clock()
        def op(tx, _):
            row = tx.get(self.key(work_id))
            if row is None:
                return None
            grant = row["grant"]
            if grant["status"] == "active":
                grant.update(status="revoked", revision=grant["revision"] + 1)
                grant["revocations"].append({"actor": actor, "evidence_ref": evidence_ref,
                                             "issued_at": now.isoformat(), "reason": reason})
            row["stops"] = (row.get("stops") or [])[-9:] + [{"evidence_ref": evidence_ref, "reason": reason}]
            tx.put(row["id"], row)
            return row
        return self.q._run(op)


def chain_stop_sweep(queue, authority, reader, *, clock, max_works=100):
    """Received visible stop/takeover mail on a chain work's thread fences that work.

    Reuses v11_stop_intake.HamleStopReader (verified Gmail, latest incoming, quoted
    text ignored) and stop_intent. A stop bumps the chain work_generation (existing
    queue fence: every claim, write and grant binding of that work is invalidated)
    and marks its grant revoked. Any read doubt blocks the slot; nothing is sent.
    """
    works, cursor = [], None
    for _ in range(4):
        page = queue.page(kind="work", limit=25, cursor=cursor)
        works.extend(page["items"]); cursor = page["next_cursor"]
        if cursor is None:
            break
    _need(cursor is None and len(works) <= max_works, "chain_stop_sweep_incomplete")
    stopped, checked = [], 0
    for hint in works:
        work = queue.get_work(hint["work_id"])
        if work["mode"] != "active":
            continue
        rows = queue.backend.scan(kind="chain_work", filters={"work_id": work["work_id"]}, limit=2)
        reply = rows[0].get("reply") if len(rows) == 1 else None
        if not isinstance(reply, dict):
            continue  # no reply anchor yet: the chain has not acted on this work
        try:
            mail = reader.read_latest(work, {"binding": {"source_message_id": reply["in_reply_to_message_id"]}})
        except Exception:
            raise ChainAuthorityError("chain_stop_read_failed") from None
        checked += 1
        if mail is None:
            continue
        moment = clock()
        _need(mail.thread_id == work["thread_id"] and mail.provider_identity == HAMLE and not mail.automatic
              and mail.actual_latest_incoming and mail.sender != HAMLE
              and HAMLE in mail.direct_to + mail.direct_cc
              and 0 <= (moment - _stamp(mail.captured_at)).total_seconds() <= 60, "chain_stop_mail_untrusted")
        intent = stop_intent(mail.text)
        if not intent:
            _need(not mail.unseen_prior_incoming, "chain_stop_prior_messages_unreviewed")
            continue
        evidence = f"gmail-visible-stop:{mail.thread_id}:{mail.message_id}:{mail.fingerprint}"
        queue.set_work_mode(work["work_id"], mode="takeover" if intent == "human_takeover" else "stopped",
                            actor=("mail-stop:" + mail.sender)[:200], expected_work_generation=work["work_generation"])
        if authority is not None:
            authority.revoke(work["work_id"], actor=mail.sender, evidence_ref=evidence[:500],
                             reason="received_visible_stop")
        stopped.append({"work_id": work["work_id"], "message_id": mail.message_id, "intent": intent})
    return {"status": "completed", "checked_work_count": checked, "stops": stopped,
            "model_calls": 0, "business_writes": 0, "grants_created": 0}
