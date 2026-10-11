"""Internal task-then-reply dispatcher with injected adapters only.

No live writer, credential access, CLI or automatic retry is provided here.
"""
from __future__ import annotations

import copy
import re
from datetime import datetime, timezone

from cloud_store import StoreError, action_id, proposal_hash
from worker import WorkerError, _capture, _fence, _stamp, _target, AHMET, HAMLE
from recipient_policy import derive_reply_recipients, validate_reply_recipients, RecipientError


class DispatchError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _require(ok, code):
    if not ok:
        raise DispatchError(code)


def _approved(store, pid, clock):
    state = store.public_state()
    p = next((p for p in state["proposals"] if p["proposal_id"] == pid), None)
    _require(p is not None and p["status"] == "approved" and isinstance(p.get("approval"), dict), "exact_approval_required")
    payload = p["payload"]
    _require(p["proposal_hash"] == proposal_hash(payload) == p["approval"]["proposal_hash"], "approved_payload_changed")
    _fence(store, p["generation"], clock)
    followup = "work" in payload
    expected = {"scope", "analysis", "reply", "work", "comment", "source"} if followup else {"scope", "analysis", "reply", "task", "source"}
    _require(set(payload) == expected and payload["scope"] == "gamechangers_internal", "internal_payload_required")
    a, reply, source = (payload[k] for k in ("analysis", "reply", "source"))
    inherited = False
    if followup:
        ref = payload["work"]
        _require(isinstance(ref, dict) and set(ref) == {"work_id", "thread_id", "task_id", "account_id", "project_id", "version", "escalation_status", "escalation_owner"}, "work_reference_invalid")
        work = store.get_work(ref["work_id"])
        _require(work is not None and all(work.get(k) == v for k, v in ref.items())
                 and ref["thread_id"] == source["thread_id"], "work_reference_changed")
        inherited = ref["escalation_status"] == "open" and ref["escalation_owner"] == "cansu_and_ahmet"
    approval = p["approval"]
    purpose = approval.get("purpose", "routine_execution")
    routine = (purpose == "routine_execution" and a.get("intent") == "routine_request"
               and a.get("escalation", {}).get("owner") == "none"
               and (not followup or payload["work"]["escalation_status"] == "not_required"))
    coordination = (purpose == "internal_coordination_only"
                    and (not followup or inherited)
                    and ((a.get("intent") == "complaint_strategy" and a.get("escalation", {}).get("owner") == "cansu_and_ahmet")
                         or (inherited and a.get("intent") in ("routine_request", "complaint_strategy", "social_planning", "unclear")))
                    and approval.get("escalation_status") == "open"
                    and approval.get("decision_authorized") is False
                    and approval.get("production_authorized") is False)
    _require(routine or coordination, "human_escalation_required")
    _require(set(reply) == {"from", "to", "cc", "subject", "body", "thread_id", "in_reply_to", "references"}
             and reply["from"] == HAMLE
             and reply["thread_id"] == source.get("thread_id"), "reply_scope_invalid")
    try:
        validate_reply_recipients(reply["to"], reply["cc"])
    except RecipientError:
        raise DispatchError("reply_recipient_scope_unverified") from None
    task = payload["comment"] if followup else payload["task"]
    if followup:
        _require(set(task) == {"account_id", "project_id", "task_id", "todo_parent_id", "todo_parent_type", "body", "task_fingerprint"}
                 and task["account_id"] == 5898865 and task["project_id"] == 49184053
                 and str(task["task_id"]) == str(work["task_id"])
                 and task["todo_parent_id"] == work["todo_parent_id"]
                 and task["todo_parent_type"] == work["todo_parent_type"]
                 and isinstance(task["body"], str) and 0 < len(task["body"]) <= 4000
                 and isinstance(task["task_fingerprint"], str) and re.fullmatch(r"[0-9a-f]{64}", task["task_fingerprint"]), "comment_scope_invalid")
    else:
        _require(set(task) == {"account_id", "project_id", "todo_parent_id", "todo_parent_type", "title", "description", "assignee_id", "due_at"}
             and task["account_id"] == 5898865 and task["project_id"] == 49184053
             and type(task["todo_parent_id"]) is int and task["todo_parent_id"] > 0
             and task["todo_parent_type"] in ("Todoset", "Todolist")
             and task["assignee_id"] == 53358100 and task["due_at"] is None, "task_scope_invalid")
    for obj, fields in ((reply, ("subject", "body", "in_reply_to", "references")), (task, ("body",) if followup else ("title", "description"))):
        _require(all(isinstance(obj[f], str) and obj[f].strip() for f in fields), "empty_action_payload")
    _require(not any(c in reply["subject"] for c in "\r\n"), "reply_subject_header_invalid")
    budget = store.budget_status()
    _require(budget["overrun_hold"] is False and budget["total_committed_microdollars"] <= budget["limit_microdollars"], "budget_not_ready")
    return p


def _fresh(store, p, capture, check_basecamp, clock, read_task_status=None):
    _fence(store, p["generation"], clock)
    followup = "work" in p["payload"]
    binding = store.get_work(p["payload"]["work"]["work_id"]) if followup else None
    _, evidence, composite = _capture(capture(), _stamp(clock), binding)
    _require(composite == p["source_fingerprint"], "source_changed_before_dispatch")
    reply = p["payload"]["reply"]
    _require(reply["in_reply_to"] == evidence["in_reply_to"] and
             reply["references"] == " ".join(evidence["references"].split()), "reply_headers_changed")
    target = _target(check_basecamp())
    if followup or "latest_to" in evidence:
        try:
            recipients = derive_reply_recipients(evidence.get("latest_to"), evidence.get("latest_cc"))
        except RecipientError:
            raise DispatchError("latest_recipient_scope_unverified") from None
        _require(reply["to"] == recipients["to"] and reply["cc"] == recipients["cc"], "latest_recipients_changed")
    task = p["payload"]["comment"] if followup else p["payload"]["task"]
    _require(target == {k: task[k] for k in ("todo_parent_id", "todo_parent_type")}, "basecamp_target_changed")
    if followup:
        _require(callable(read_task_status), "task_status_reader_required")
        observed = read_task_status(store.get_work(p["payload"]["work"]["work_id"]))
        _require(isinstance(observed, dict) and observed.get("identity_verified") is True
                 and str(observed.get("task_id")) == str(task["task_id"])
                 and observed.get("task_fingerprint") == task["task_fingerprint"]
                 and observed.get("status") == "active" and observed.get("completed") is False, "task_changed_before_dispatch")
    _fence(store, p["generation"], clock)
    return composite


def _readback(store, aid, provider_id, payload, reader):
    result = reader(provider_id, copy.deepcopy(payload))
    _require(isinstance(result, dict) and result.get("provider_id") == provider_id and
             result.get("matches_approved_payload") is True and result.get("identity_verified") is True,
             "provider_readback_unconfirmed")
    store.confirm_readback(aid, provider_id)
    return provider_id


def _effect(store, p, kind, payload, writer, reader, capture, check_basecamp, clock, read_task_status=None):
    aid = action_id(p["proposal_id"], kind)
    existing = next((a for a in store.public_state()["actions"] if a["action_id"] == aid), None)
    if existing is not None:
        _require(existing["payload_hash"] == p["proposal_hash"], "action_payload_mismatch")
        # The recorded provider ID is the read target; don't recapture the latest
        # thread here, because the expected latest message may be our own reply.
        _require(existing["state"] in ("accepted", "verified"), "existing_action_held_no_retry")
        return _readback(store, aid, existing["provider_id"], payload, reader), False
    _approved(store, p["proposal_id"], clock)
    lease = store.claim_action(aid, p["proposal_id"], kind, p["proposal_hash"], "internal-dispatch", p["generation"], lease_seconds=60)
    callback_called = False
    boundary_started = False

    def before_post():
        nonlocal callback_called, boundary_started
        _require(not callback_called, "provider_dispatch_callback_repeated")
        callback_called = True
        # The writer has already finished its slower authentication/preflight.
        # Check the source and current authority at the actual POST boundary.
        composite = _fresh(store, p, capture, check_basecamp, clock, read_task_status)
        _approved(store, p["proposal_id"], clock)
        store.begin_dispatch(lease, composite)
        boundary_started = True

    try:
        result = writer(copy.deepcopy(payload), before_post=before_post)
        _require(boundary_started, "provider_dispatch_callback_missing")
        pid = result.get("provider_id") if isinstance(result, dict) else None
        pattern = r"[0-9a-f]{1,64}" if kind == "gmail_reply" else r"[0-9]{1,30}"
        _require(isinstance(pid, str) and re.fullmatch(pattern, pid), "provider_receipt_unconfirmed")
        store.record_receipt(lease, pid)
    except Exception as exc:
        if boundary_started:
            try:
                store.mark_unknown(lease)
            except Exception:
                pass  # Dispatch_started/accepted still prohibits any auto resend.
            raise DispatchError("provider_write_outcome_unknown_no_retry") from None
        # A failed/uncertain boundary remains permanently reserved. No POST is
        # permitted before a successful callback, and no automatic retry occurs.
        if isinstance(exc, (DispatchError, WorkerError, StoreError)):
            raise
        raise DispatchError("provider_preflight_or_boundary_failed") from None
    return _readback(store, aid, pid, payload, reader), True


def dispatch_approved(store, proposal_id, *, capture, check_basecamp, create_task,
                      read_task, send_reply, read_reply, clock=None,
                      create_comment=None, read_comment=None, read_task_status=None):
    """Only trusted adapters should call this; no provider adapter is auto-loaded.

    writer(payload, before_post=callback)->{"provider_id":str}; the mandatory
    callback runs exactly once after preflight, immediately before POST.
    reader(id,payload)->same ID plus
    identity_verified=True and matches_approved_payload=True from real readback.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        p = _approved(store, proposal_id, clock)
        followup = "work" in p["payload"]
        if followup:
            _require(callable(create_comment) and callable(read_comment), "comment_adapters_required")
            comment_id, task_new = _effect(store, p, "basecamp_comment", p["payload"]["comment"], create_comment,
                                         read_comment, capture, check_basecamp, clock, read_task_status)
            task_id = p["payload"]["work"]["task_id"]
        else:
            task_id, task_new = _effect(store, p, "basecamp_todo", p["payload"]["task"], create_task,
                                      read_task, capture, check_basecamp, clock)
        # A task that failed, remained unknown or lacked readback never reaches
        # mail. A stop after task acceptance also blocks the next external write.
        p = _approved(store, proposal_id, clock)
        reply_id, reply_new = _effect(store, p, "gmail_reply", p["payload"]["reply"], send_reply,
                                     read_reply, capture, check_basecamp, clock, read_task_status)
        if followup:
            store.complete_followup(p["payload"]["work"]["work_id"], proposal_id)
        return {"status": "internal_actions_readback_verified", "task_id": task_id, "reply_id": reply_id,
                **({"comment_id": comment_id, "new_task_count": 0} if followup else {}),
                "new_write_count": int(task_new) + int(reply_new), "automatic_retry_allowed": False}
    except Exception as exc:
        reason = exc.code if isinstance(exc, (DispatchError, WorkerError, StoreError)) else "provider_read_or_preflight_failed"
        return {"status": "held", "reason": reason, "automatic_retry_allowed": False}
