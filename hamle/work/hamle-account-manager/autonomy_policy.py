"""Offline policy foundation; NOT imported by the current runtime.

No network, credential access, persistence, dispatch or live enable is provided.
The caller must build evidence from trusted provider/control/QA adapters, never
from model output or email instructions. Passing this pure check is not a send
capability: a future dispatcher must recheck these facts immediately before its
idempotent write. Source recency cannot detect mail Hamle never received. A
delayed action additionally needs a short-lived, explicit work authorization
from a permitted human who confirms current participation; it is not inferred
from polling, earlier CC or the absence of a stop.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

HAMLE = "hamle@gamechangers.com.tr"
BRANDS = ("wat_mobilite", "rabam", "ya_da", "multicosmetics", "altinmarka",
          "bikaldi", "han_spaces", "gamechangers")
ACTIONS = frozenset(("acknowledge", "clarify_brief", "create_task", "update_task",
                     "add_comment", "assign_task", "remind", "deliver_file"))
MAIL_ACTIONS = frozenset(("acknowledge", "clarify_brief", "remind", "deliver_file"))
TASK_ACTIONS = ACTIONS - MAIL_ACTIONS
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
EMAIL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+\Z")


class PolicyError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _require(condition, code):
    if not condition:
        raise PolicyError(code)


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 500 and not any(ord(c) < 32 for c in value)


def _stamp(value):
    try:
        d = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        _require(d.tzinfo is not None and d.utcoffset() is not None, "timestamp_timezone_required")
        return d.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError):
        raise PolicyError("timestamp_invalid") from None


def _age(now, value, maximum, code):
    _require(0 <= (now - _stamp(value)).total_seconds() <= maximum, code)


def _emails(value, *, allow_empty=False):
    _require(isinstance(value, list) and len(value) <= 100
             and (allow_empty or bool(value)) and len(set(value)) == len(value)
             and all(isinstance(v, str) and EMAIL.fullmatch(v) for v in value),
             "contact_list_invalid")
    return value


def _positive(value):
    return type(value) is int and 0 < value <= 9_007_199_254_740_991


def scope_digest(scope):
    """Bind a delegation to the exact verified scope; edits revoke old grants."""
    try:
        raw = json.dumps(scope, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError):
        raise PolicyError("scope_not_json") from None
    return hashlib.sha256(raw).hexdigest()


def validate_scope(scope, *, now):
    """Validate one enabled per-brand record; disabled templates fail closed."""
    now = _stamp(now)
    _require(isinstance(scope, dict), "scope_missing")
    _require(type(scope.get("schema_version")) is int and scope["schema_version"] == 1 and scope.get("brand_id") in BRANDS,
             "scope_identity_invalid")
    _require(scope.get("enabled") is True, "brand_scope_disabled")
    auth = scope.get("authority")
    _require(isinstance(auth, dict) and _text(auth.get("evidence_ref")), "scope_authority_missing")
    _emails([auth.get("verified_by")])
    _require(_stamp(auth.get("verified_at")) <= now < _stamp(auth.get("expires_at")), "scope_authority_expired")
    clients = _emails(scope.get("customer_contacts"))
    agency = _emails(scope.get("agency_contacts"))
    _emails(scope.get("allowed_cc_contacts"), allow_empty=True)
    _require(HAMLE not in clients + agency, "hamle_cannot_grant_customer_authority")
    _require(auth["verified_by"] in agency, "scope_verifier_not_authorized")
    _require(scope.get("agency_owner") in agency, "agency_owner_missing")
    bc = scope.get("basecamp")
    _require(isinstance(bc, dict) and _positive(bc.get("account_id"))
             and _positive(bc.get("project_id")) and type(bc.get("customer_visible")) is bool
             and isinstance(bc.get("list_ids"), list) and bool(bc["list_ids"])
             and all(_positive(v) for v in bc["list_ids"])
             and len(set(bc["list_ids"])) == len(bc["list_ids"])
             and _text(bc.get("verification_ref")), "basecamp_scope_unverified")
    services = scope.get("services")
    _require(isinstance(services, list) and 1 <= len(services) <= 30, "service_scope_missing")
    ids = []
    for s in services:
        _require(isinstance(s, dict) and _text(s.get("service_id")), "service_invalid")
        ids.append(s["service_id"])
        acts = s.get("actions")
        _require(isinstance(acts, list) and bool(acts) and len(set(acts)) == len(acts)
                 and all(v in ACTIONS for v in acts), "service_actions_invalid")
        _require(type(s.get("revision_limit")) is int and 0 <= s["revision_limit"] <= 100,
                 "revision_limit_missing")
        _require(s.get("list_id") in bc["list_ids"] and type(s.get("list_id")) is int,
                 "service_list_mismatch")
        _require(isinstance(s.get("assignee_ids"), list)
                 and all(_positive(v) for v in s["assignee_ids"]), "assignee_scope_invalid")
        _require(type(s.get("immediate_action_window_seconds")) is int
                 and 1 <= s["immediate_action_window_seconds"] <= 300,
                 "authority_freshness_policy_missing")
        work_policy = s.get("work_authority_policy")
        if work_policy is not None:
            _require(isinstance(work_policy, dict) and type(work_policy.get("enabled")) is bool,
                     "work_authority_policy_invalid")
            if work_policy["enabled"]:
                from v11_work_authority import WorkAuthorityError, validate_work_authority_policy
                try:
                    validate_work_authority_policy(work_policy, scope, s, now=now)
                except WorkAuthorityError as exc:
                    raise PolicyError(str(exc)) from None
        delivery = s.get("delivery")
        if "deliver_file" in acts:
            _require(isinstance(delivery, dict) and _text(delivery.get("accepted_qa_policy_ref"))
                     and _text(delivery.get("approved_assets_ref")), "delivery_scope_unaccepted")
            mode = delivery.get("release_mode", "human_release")
            _require(mode in ("human_release", "scored_quality_v1"), "delivery_release_mode_unknown")
            if mode == "scored_quality_v1":
                policy = delivery.get("quality_policy")
                _require(isinstance(policy, dict) and policy.get("status") == "accepted"
                         and policy.get("verified") is True
                         and policy.get("brand_id") == scope["brand_id"]
                         and policy.get("service_id") == s["service_id"]
                         and policy.get("policy_id") == delivery["accepted_qa_policy_ref"],
                         "scored_delivery_policy_unaccepted")
                _emails(policy.get("approval_actors"))
                _require(set(policy["approval_actors"]).issubset(agency), "delivery_actor_unverified")
            else:
                _emails(delivery.get("release_actors"))
                _require(set(delivery["release_actors"]).issubset(agency), "delivery_actor_unverified")
            _require(isinstance(delivery.get("allowed_mime_types"), list)
                     and bool(delivery["allowed_mime_types"])
                     and all(_text(v) for v in delivery["allowed_mime_types"]), "delivery_types_missing")
    _require(len(ids) == len(set(ids)), "duplicate_service")
    return scope_digest(scope)


def _reply_all(source):
    to = list(dict.fromkeys([source["sender"]] + source["to"]))
    to = [v for v in to if v != HAMLE]
    cc = [v for v in source["cc"] if v != HAMLE and v not in to]
    return to, cc


def _artifact(request, service, scope, now):
    artifact, qa, release = (request.get(k) for k in ("artifact", "qa", "release"))
    _require(isinstance(artifact, dict), "delivery_evidence_missing")
    _require(artifact.get("brand_id") == scope["brand_id"]
             and artifact.get("work_id") == request["work_id"], "artifact_work_or_brand_mismatch")
    _require(_text(artifact.get("version")) and isinstance(artifact.get("sha256"), str)
             and SHA256.fullmatch(artifact["sha256"]), "artifact_version_invalid")
    policy = service["delivery"]
    _require(artifact.get("mime_type") in policy["allowed_mime_types"], "artifact_type_out_of_scope")
    if policy.get("release_mode") == "scored_quality_v1":
        _scored_artifact(request, service, scope, now)
        return
    _require(isinstance(qa, dict) and isinstance(release, dict), "delivery_evidence_missing")
    fields = ("brand_id", "work_id", "version", "sha256")
    _require(all(qa.get(k) == artifact[k] and release.get(k) == artifact[k] for k in fields),
             "artifact_changed_after_qa_or_release")
    _require(qa.get("accepted") is True and _text(qa.get("evidence_ref"))
             and qa.get("policy_ref") == policy["accepted_qa_policy_ref"]
             and qa.get("assets_ref") == policy["approved_assets_ref"]
             and qa.get("internal_notes_excluded") is True
             and qa.get("rights_checked") is True, "artifact_qa_not_accepted")
    _require(_stamp(qa.get("checked_at")) <= _stamp(release.get("issued_at")) <= now,
             "artifact_release_precedes_qa")
    _require(release.get("actor") in policy["release_actors"]
             and release.get("revoked") is False and release.get("customer_delivery") is True
             and _text(release.get("evidence_ref")) and now < _stamp(release.get("expires_at")),
             "artifact_release_missing_or_expired")
    _require(release.get("reply_to") == request["reply_to"]
             and release.get("reply_cc") == request["reply_cc"]
             and release.get("source_fingerprint") == request["source"]["fingerprint"]
             and release.get("service_id") == request["service_id"]
             and release.get("scope_digest") == scope_digest(scope)
             and release.get("action_payload_sha256") == request["prepared_action"]["payload_sha256"],
             "artifact_release_destination_or_scope_mismatch")


def _scored_artifact(request, service, scope, now):
    """A scored release does not impersonate a human producer release.

    All normal source/control/delegation/dedup/fresh-participation checks run
    before this function. A trusted inspector still supplies exact byte/brief
    evidence, and the accepted rubric is part of the scope's immutable digest.
    """
    from v11_quality_gate import build_quality_binding, build_quality_note_context, evaluate_quality
    policy = service["delivery"]
    _require(request.get("quality_assets_ref") == policy["approved_assets_ref"],
             "scored_delivery_assets_changed")
    work, target, prepared = (request.get(k) for k in ("work_binding", "basecamp_target", "prepared_action"))
    _require(scope["basecamp"]["customer_visible"] is False
             and isinstance(work, dict) and isinstance(target, dict)
             and all(work.get(k) == request[k] for k in ("work_id", "brand_id", "service_id"))
             and work.get("thread_id") == request["source"]["thread_id"]
             and work.get("account_id") == scope["basecamp"]["account_id"]
             and work.get("project_id") == scope["basecamp"]["project_id"]
             and work.get("list_id") == service["list_id"]
             and _positive(work.get("task_id"))
             and all(target.get(k) == work[k] for k in ("account_id", "project_id", "list_id", "task_id"))
             and target.get("customer_visible") is False
             and prepared.get("task_id") == work["task_id"]
             and isinstance(target.get("task_fingerprint"), str)
             and SHA256.fullmatch(target["task_fingerprint"])
             and prepared.get("task_fingerprint") == target["task_fingerprint"]
             and _text(target.get("verification_ref")), "scored_quality_note_work_mismatch")
    _age(now, target.get("verified_at"), 60, "scored_quality_note_target_stale")
    binding = build_quality_binding(request,
        brief_sha256=request.get("quality_brief_sha256"),
        manifest_sha256=request.get("quality_manifest_sha256"), scope_sha256=scope_digest(scope))
    submission = request.get("quality_submission")
    fields = ("brand_id", "work_id", "version", "sha256", "brief_sha256", "manifest_sha256",
              "scope_digest", "source_fingerprint", "recipients_sha256")
    _require(isinstance(submission, dict) and submission.get("verified") is True
             and all(submission.get(k) == binding[k] for k in fields)
             and _positive(submission.get("actor_id"))
             and submission["actor_id"] in service["assignee_ids"]
             and _text(submission.get("evidence_ref")), "scored_producer_submission_unverified")
    assessment = request.get("quality_assessment")
    _require(isinstance(assessment, dict)
             and _stamp(submission.get("issued_at")) <= _stamp(assessment.get("checked_at")) <= now,
             "scored_assessment_precedes_submission")
    decision = evaluate_quality(policy["quality_policy"], assessment, binding,
        request.get("quality_safety"), now=now,
        human_approval=request.get("quality_human_approval"))
    _require(decision["route"] == "delivery_candidate", decision["reason"])
    # The common delivery gate also requires the persisted internal score note;
    # a caller cannot bypass the coordinator and send from a score alone.
    proof = request.get("quality_note_proof")
    _require(isinstance(proof, dict), "scored_quality_note_missing")
    context = build_quality_note_context(request, decision)
    _require(proof.get("quality_context") == context
             and proof.get("quality_context_sha256") == scope_digest(context),
             "scored_quality_note_context_mismatch")
    _require(_text(proof.get("action_id")) and isinstance(proof.get("payload_sha256"), str)
             and SHA256.fullmatch(proof["payload_sha256"]), "scored_quality_note_identity_invalid")
    receipt = proof.get("receipt")
    _require(isinstance(receipt, dict) and receipt.get("provider_readback_verified") is True
             and receipt.get("action_id") == proof["action_id"]
             and receipt.get("payload_sha256") == proof["payload_sha256"]
             and receipt.get("quality_context_sha256") == scope_digest(context)
             and _text(receipt.get("evidence_ref")), "scored_quality_note_readback_missing")


def authorize_action(scope, request, *, now):
    """Return a concrete allow/hold decision. Unknown/malformed evidence is held.

    Inputs represent trusted current state. This function does not authenticate
    evidence or establish provider identity; those remain runtime obligations.
    All writes (including delayed Basecamp actions) need a fresh authority basis.
    """
    try:
        now = _stamp(now)
        digest = validate_scope(scope, now=now)
        _require(isinstance(request, dict), "request_missing")
        _require(request.get("brand_id") == scope["brand_id"], "brand_scope_mismatch")
        _require(request.get("action") in ACTIONS, "action_unknown")
        _require(_text(request.get("work_id")), "work_identity_missing")
        risk = request.get("risk_area")
        _require(risk == "routine", {"commercial": "ahmet_commercial_decision_required",
                 "complaint_strategy": "cansu_and_ahmet_decision_required",
                 "social_planning": "ozge_collaboration_required"}.get(risk, "intent_requires_human_review"))
        _require(request.get("open_escalation") is False, "open_escalation_blocks_routine")
        services = [s for s in scope["services"] if s["service_id"] == request.get("service_id")]
        _require(len(services) == 1, "service_out_of_scope")
        service = services[0]
        _require(request["action"] in service["actions"], "action_out_of_service_scope")
        _require(type(request.get("revision_index")) is int
                 and 0 <= request["revision_index"] <= service["revision_limit"], "revision_limit_exceeded_or_unknown")
        control, delegation, source = (request.get(k) for k in ("control", "delegation", "source"))
        _require(all(isinstance(v, dict) for v in (control, delegation, source)), "authority_evidence_missing")
        _require(control.get("work_id") == request["work_id"], "control_work_mismatch")
        _age(now, control.get("observed_at"), 60, "control_observation_stale")
        _require(control.get("global_mode") == "routine_enabled" and control.get("work_mode") == "active",
                 "human_stop_or_takeover")
        for key in ("global_generation", "work_generation"):
            _require(type(control.get(key)) is int and control[key] >= 0
                     and delegation.get(key) == control[key] and type(delegation.get(key)) is int,
                     "delegation_generation_stale")
        _require(delegation.get("revoked") is False and delegation.get("scope_digest") == digest
                 and delegation.get("work_id") == request["work_id"]
                 and delegation.get("service_id") == service["service_id"]
                 and delegation.get("brand_id") == scope["brand_id"]
                 and delegation.get("actor") in scope["agency_contacts"]
                 and _text(delegation.get("evidence_ref")), "work_delegation_invalid")
        _require(_stamp(delegation.get("issued_at")) <= now < _stamp(delegation.get("expires_at")),
                 "work_delegation_expired")
        _require(isinstance(delegation.get("actions"), list)
                 and request["action"] in delegation["actions"]
                 and set(delegation["actions"]).issubset(service["actions"]), "action_not_delegated")
        _require(source.get("actual_latest") is True and source.get("own_or_automatic") is False
                 and _text(source.get("provider_evidence_ref")) and _text(source.get("message_id"))
                 and source.get("thread_id") == delegation.get("thread_id")
                 and _text(source.get("thread_id")) and isinstance(source.get("fingerprint"), str)
                 and SHA256.fullmatch(source["fingerprint"]), "latest_source_unverified")
        prepared = request.get("prepared_action")
        _require(isinstance(prepared, dict)
                 and all(prepared.get(k) == request[k] for k in ("work_id", "brand_id", "service_id", "action"))
                 and prepared.get("source_fingerprint") == source["fingerprint"],
                 "prepared_action_source_or_scope_changed")
        _require(isinstance(request.get("action_payload"), dict)
                 and isinstance(prepared.get("payload_sha256"), str)
                 and SHA256.fullmatch(prepared["payload_sha256"])
                 and prepared["payload_sha256"] == scope_digest(request["action_payload"]),
                 "prepared_action_payload_changed")
        _emails(source.get("to"), allow_empty=True)
        _emails(source.get("cc"), allow_empty=True)
        _require(HAMLE in source["to"] + source["cc"], "hamle_absent_from_actual_latest_to_cc")
        _require(source.get("sender") in scope["customer_contacts"] + scope["agency_contacts"],
                 "sender_not_authorized")
        expected_to, expected_cc = _reply_all(source)
        allowed = set(scope["customer_contacts"] + scope["agency_contacts"] + scope["allowed_cc_contacts"])
        _require(set(expected_to + expected_cc).issubset(allowed), "latest_recipient_out_of_scope")
        _age(now, source.get("captured_at"), 60, "source_capture_stale")
        _require(_stamp(source.get("received_at")) <= _stamp(source.get("captured_at")), "source_time_invalid")
        if request["action"] in MAIL_ACTIONS:
            _require(request.get("reply_to") == expected_to and request.get("reply_cc") == expected_cc,
                     "reply_all_recipients_mismatch")
            payload = request["action_payload"]
            mail_fields = {"from", "to", "cc", "thread_id", "body", "subject"}
            if request["action"] == "deliver_file":
                mail_fields.add("artifact")
            _require(set(payload) == mail_fields and payload.get("from") == HAMLE
                     and payload.get("to") == expected_to and payload.get("cc") == expected_cc
                     and payload.get("thread_id") == source["thread_id"]
                     and _text(payload.get("subject")) and isinstance(payload.get("body"), str)
                     and 0 < len(payload["body"]) <= 16000, "executable_mail_envelope_mismatch")
        else:
            target = request.get("basecamp_target")
            _require(isinstance(target, dict) and target.get("account_id") == scope["basecamp"]["account_id"]
                     and target.get("project_id") == scope["basecamp"]["project_id"]
                     and target.get("list_id") == service["list_id"]
                     and target.get("customer_visible") is scope["basecamp"]["customer_visible"]
                     and _text(target.get("verification_ref")), "basecamp_target_mismatch")
            _age(now, target.get("verified_at"), 60, "basecamp_target_verification_stale")
            work = request.get("work_binding")
            _require(isinstance(work, dict)
                     and all(work.get(k) == request[k] for k in ("work_id", "brand_id", "service_id"))
                     and work.get("thread_id") == source["thread_id"]
                     and all(work.get(k) == target[k] for k in ("account_id", "project_id", "list_id")),
                     "task_work_binding_mismatch")
            if request["action"] == "create_task":
                _require(all("task_id" in v and v["task_id"] is None for v in (work, prepared, target)),
                         "work_already_has_task_or_binding_missing")
            else:
                _require(_positive(work.get("task_id")) and _positive(target.get("task_id"))
                         and _positive(prepared.get("task_id"))
                         and work["task_id"] == target["task_id"] == prepared["task_id"],
                         "task_identity_mismatch")
                _require(isinstance(target.get("task_fingerprint"), str)
                         and SHA256.fullmatch(target["task_fingerprint"])
                         and prepared.get("task_fingerprint") == target["task_fingerprint"],
                         "task_changed_after_preparation")
            if request["action"] == "assign_task":
                _require(request.get("assignee_id") in service["assignee_ids"], "assignee_not_authorized")
            payload = request["action_payload"]
            task_fields = {"target", "body"}
            if request["action"] == "assign_task":
                task_fields.add("assignee_id")
            target_ids = {k: target[k] for k in ("account_id", "project_id", "list_id", "task_id")}
            _require(set(payload) == task_fields and payload.get("target") == target_ids
                     and isinstance(payload.get("body"), str) and 0 < len(payload["body"]) <= 16000,
                     "executable_task_envelope_mismatch")
            if request["action"] == "assign_task":
                _require(payload.get("assignee_id") == request.get("assignee_id"),
                         "executable_assignee_mismatch")
        delayed = (now - _stamp(source["received_at"])).total_seconds() > service["immediate_action_window_seconds"]
        has_work_grant = "work_authority_grant" in request
        if has_work_grant:
            # This explicitly accepted, bounded protocol is a DIFFERENT class
            # from fresh human confirmation. It cannot bypass the sender/actual
            # To/CC, source/control freshness or delegation checks above.
            from v11_work_authority import authorize_work_authority
            work_decision = authorize_work_authority(service.get("work_authority_policy"), scope, request,
                request.get("work_authority_grant"), request.get("work_authority_observation"),
                request.get("budget_authorization"), now=now)
            _require(work_decision["allowed"], work_decision["reason"])
        if delayed and not has_work_grant:
            fresh = request.get("fresh_work_authorization")
            _require(isinstance(fresh, dict), "delayed_action_requires_fresh_work_authorization")
            _require(fresh.get("actor") in scope["agency_contacts"]
                     and fresh.get("basis") == "explicit_current_participation_confirmation"
                     and fresh.get("hamle_participation_confirmed") is True
                     and fresh.get("revoked") is False and _text(fresh.get("evidence_ref"))
                     and fresh.get("work_id") == request["work_id"]
                     and fresh.get("scope_digest") == digest
                     and fresh.get("source_fingerprint") == source["fingerprint"]
                     and fresh.get("action") == request["action"]
                     and fresh.get("global_generation") == control["global_generation"]
                     and fresh.get("work_generation") == control["work_generation"],
                     "fresh_work_authorization_invalid")
            _require(fresh.get("prepared_action_digest") == scope_digest(prepared),
                     "fresh_authority_prepared_action_mismatch")
            _age(now, fresh.get("issued_at"), 300, "fresh_work_authorization_stale")
            _require(now < _stamp(fresh.get("expires_at")), "fresh_work_authorization_expired")
            if request["action"] == "deliver_file":
                _require(isinstance(request.get("artifact"), dict)
                         and fresh.get("artifact_sha256") == request["artifact"].get("sha256")
                         and fresh.get("artifact_version") == request["artifact"].get("version"),
                         "fresh_authority_artifact_mismatch")
        if request["action"] == "deliver_file":
            _artifact(request, service, scope, now)
            artifact = request["artifact"]
            _require(request["action_payload"].get("artifact") ==
                     {k: artifact[k] for k in ("brand_id", "work_id", "version", "sha256", "mime_type")},
                     "executable_artifact_mismatch")
        _require(request.get("action_state") == "new", "action_already_started_or_unknown")
        return {"allowed": True, "reason": "within_verified_routine_scope", "scope_digest": digest,
                "live_enabled_by_this_module": False, "unseen_cc_removal_detected": False}
    except PolicyError as exc:
        return {"allowed": False, "reason": exc.code, "live_enabled_by_this_module": False}
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"allowed": False, "reason": "malformed_authority_evidence", "live_enabled_by_this_module": False}
