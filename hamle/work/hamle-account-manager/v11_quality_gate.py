"""Evidence-based quality routing, isolated from live delivery and model calls.

Only trusted adapters may construct these inputs. A ``verified`` flag in an
email, a model response, or a Basecamp comment is not verification. This module
checks evidence consistency, not truth, visual quality or provider identity.
Scores measure conformance to an accepted rubric; they are not probabilities.
Even a delivery candidate is NOT permission to send. The configured delivery
release policy, autonomy policy, queue, budget and preflight still apply.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from autonomy_policy import EMAIL, SHA256, scope_digest


BINDING_TEXT_FIELDS = ("brand_id", "work_id", "service_id", "thread_id", "version")
BINDING_HASH_FIELDS = ("sha256", "brief_sha256", "manifest_sha256", "scope_digest",
                       "source_fingerprint", "recipients_sha256", "action_payload_sha256")
BINDING_GENERATIONS = ("global_generation", "work_generation")
MANDATORY_CHECKS = ("brand_identity", "required_files", "technical_specs",
                    "rights_and_disclosure", "internal_notes_excluded")
METHODS = ("deterministic", "human_review", "model_rubric")


class QualityGateError(ValueError):
    pass


def _need(condition, reason):
    if not condition:
        raise QualityGateError(reason)


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 8000 and not any(
        ord(c) < 32 and c not in "\n\t" for c in value)


def _stamp(value):
    d = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    _need(d.tzinfo is not None and d.utcoffset() is not None, "timestamp_timezone_required")
    return d.astimezone(timezone.utc)


def _number(value, reason):
    # Strings are preferred for decimal scores. Never round a score to a band.
    _need(type(value) in (str, int, float), reason)
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise QualityGateError(reason) from None
    _need(number.is_finite() and 0 <= number <= 100 and number.as_tuple().exponent >= -6,
          reason)
    return number


def _serial(number):
    return format(number, "f").rstrip("0").rstrip(".") if "." in format(number, "f") else str(number)


def _validate_binding(binding):
    _need(isinstance(binding, dict) and set(binding) == set(
        BINDING_TEXT_FIELDS + BINDING_HASH_FIELDS + BINDING_GENERATIONS), "binding_fields_invalid")
    _need(all(_text(binding[k]) for k in BINDING_TEXT_FIELDS), "binding_identity_invalid")
    _need(all(isinstance(binding[k], str) and SHA256.fullmatch(binding[k])
              for k in BINDING_HASH_FIELDS), "binding_hash_invalid")
    _need(all(type(binding[k]) is int and binding[k] >= 0 for k in BINDING_GENERATIONS),
          "binding_generation_invalid")


def build_quality_binding(request, *, brief_sha256, manifest_sha256, scope_sha256):
    """Derive the common immutable binding from the action being authorized.

    The caller obtains brief/manifest hashes from the current trusted work and
    byte snapshot, never from model output. Both QA and delivery authorization
    use this helper so recipients/payload/control generations cannot diverge.
    """
    artifact, source, control = request["artifact"], request["source"], request["control"]
    binding = {"brand_id": request["brand_id"], "work_id": request["work_id"],
               "service_id": request["service_id"], "thread_id": source["thread_id"],
               "version": artifact["version"], "sha256": artifact["sha256"],
               "brief_sha256": brief_sha256, "manifest_sha256": manifest_sha256,
               "scope_digest": scope_sha256, "source_fingerprint": source["fingerprint"],
               "recipients_sha256": scope_digest({"to": request["reply_to"], "cc": request["reply_cc"]}),
               "action_payload_sha256": request["prepared_action"]["payload_sha256"],
               "global_generation": control["global_generation"], "work_generation": control["work_generation"]}
    _validate_binding(binding)
    return binding


def build_quality_note_context(request, quality_result):
    """Common exact context for the durable internal note before delivery."""
    _need(quality_result.get('route') == 'delivery_candidate', 'quality_note_requires_delivery_candidate')
    target = {key: request['work_binding'][key]
              for key in ('account_id', 'project_id', 'list_id', 'task_id')}
    _need(all(type(value) is int and value > 0 for value in target.values()), 'quality_note_target_invalid')
    return {'quality_binding_sha256': quality_result['binding_sha256'],
            'assessment_sha256': quality_result['assessment_sha256'],
            'policy_digest': quality_result['policy_digest'],
            'human_approval_sha256': (scope_digest(request['quality_human_approval'])
                if quality_result['reason'] == 'human_accepted_exact_assessment' else None),
            'basecamp_target': target}


def _references(value):
    return (isinstance(value, list) and 0 < len(value) <= 100
            and all(_text(v) for v in value) and len(value) == len(set(value)))


def _validate_policy(policy, binding, now):
    _need(isinstance(policy, dict) and type(policy.get("schema_version")) is int
          and policy["schema_version"] == 1 and _text(policy.get("policy_id"))
          and _text(policy.get("version")), "quality_policy_invalid")
    _need(policy.get("status") == "accepted" and policy.get("verified") is True
          and _text(policy.get("evidence_ref")), "quality_policy_not_accepted")
    _need(policy.get("brand_id") == binding["brand_id"]
          and policy.get("service_id") == binding["service_id"], "quality_policy_scope_mismatch")
    # Do not put the full scope digest inside its embedded policy (a circular
    # hash). The assessment binds the complete scope, including this policy.
    _need("scope_digest" not in policy, "quality_policy_circular_scope_digest")
    _need(_stamp(policy.get("accepted_at")) <= now < _stamp(policy.get("expires_at")),
          "quality_policy_expired")
    actors = policy.get("approval_actors")
    _need(isinstance(actors, list) and 0 < len(actors) <= 50
          and all(isinstance(v, str) and EMAIL.fullmatch(v) for v in actors)
          and len(actors) == len(set(actors)), "quality_approval_actors_invalid")
    for key, limit in (("assessment_max_age_seconds", 604800), ("approval_max_age_seconds", 3600)):
        _need(type(policy.get(key)) is int and 0 < policy[key] <= limit,
              "quality_freshness_policy_invalid")
    evaluators = policy.get("trusted_evaluators")
    _need(isinstance(evaluators, dict) and set(evaluators) == set(METHODS)
          and all(isinstance(v, list) and all(_text(x) for x in v)
                  and len(v) == len(set(v)) for v in evaluators.values()), "trusted_evaluators_missing")
    checks = policy.get("critical_checks")
    _need(isinstance(checks, list) and set(MANDATORY_CHECKS).issubset(checks)
          and all(_text(v) for v in checks) and len(checks) == len(set(checks)) <= 50,
          "critical_check_policy_incomplete")
    critical_requirements = policy.get("critical_requirements")
    _need(isinstance(critical_requirements, dict) and set(critical_requirements) == set(checks),
          "critical_requirement_mapping_missing")
    for check in critical_requirements.values():
        _need(isinstance(check, dict) and _references(check.get("requirement_refs"))
              and isinstance(check.get("methods"), list) and bool(check["methods"])
              and all(v in METHODS for v in check["methods"])
              and len(check["methods"]) == len(set(check["methods"])),
              "critical_requirement_mapping_invalid")
    criteria = policy.get("criteria")
    _need(isinstance(criteria, list) and 1 <= len(criteria) <= 50, "quality_criteria_missing")
    weights, ids = [], []
    for item in criteria:
        _need(isinstance(item, dict) and _text(item.get("id")) and _text(item.get("description"))
              and type(item.get("critical")) is bool and _references(item.get("requirement_refs")),
              "quality_criterion_invalid")
        methods = item.get("methods")
        _need(isinstance(methods, list) and bool(methods)
              and all(v in METHODS for v in methods) and len(methods) == len(set(methods)),
              "quality_criterion_methods_invalid")
        ids.append(item["id"])
        weights.append(_number(item.get("weight"), "quality_weight_invalid"))
    _need(len(ids) == len(set(ids)) and all(w > 0 for w in weights)
          and sum(weights) == Decimal(100), "quality_weights_must_total_100")
    # Versioned thresholds are explicit. Changing these needs a new accepted policy.
    thresholds = policy.get("thresholds")
    _need(isinstance(thresholds, dict), "quality_thresholds_missing")
    human = _number(thresholds.get("human_min"), "quality_threshold_invalid")
    automatic = _number(thresholds.get("candidate_min"), "quality_threshold_invalid")
    _need(0 < human < automatic <= 100, "quality_threshold_order_invalid")
    return scope_digest(policy), human, automatic


def _validate_safety(safety, binding, now):
    _need(isinstance(safety, dict) and safety.get("verified") is True
          and safety.get("binding") == binding and _text(safety.get("evidence_ref")),
          "current_safety_evidence_missing")
    _need(0 <= (now - _stamp(safety.get("observed_at"))).total_seconds() <= 60,
          "current_safety_evidence_stale")
    _need(safety.get("control") == "active", "human_stop_or_takeover")
    _need(safety.get("authority") == "allowed", "authority_requires_resolution")
    _need(safety.get("security") == "clear", "security_requires_resolution")
    _need(safety.get("write_state") == "new", "write_already_started_unknown_or_confirmed")


def _validate_record(record, policy, binding, checked_at, *, methods, requirement_refs):
    _need(isinstance(record, dict), "quality_check_unassessed")
    _need(not any(key in record for key in ("confidence", "self_confidence", "probability")),
          "model_confidence_is_not_quality")
    _need(record.get("verified") is True and record.get("binding") == binding
          and _text(record.get("evidence_ref")), "quality_check_evidence_unverified")
    method = record.get("method")
    _need(method in methods and record.get("evaluator_id") in policy["trusted_evaluators"][method],
          "quality_evaluator_not_authorized")
    _need(_stamp(record.get("checked_at")) == checked_at, "quality_check_timestamp_mismatch")
    requirements = record.get("requirement_refs")
    _need(_references(requirements) and set(requirements) == set(requirement_refs),
          "quality_requirement_mapping_mismatch")
    observations = record.get("observations")
    _need(isinstance(observations, list) and 0 < len(observations) <= 100,
          "quality_observations_missing")
    for observation in observations:
        _need(isinstance(observation, dict) and _text(observation.get("evidence_ref"))
              and _text(observation.get("location")) and _text(observation.get("finding"))
              and observation.get("artifact_sha256") == binding["sha256"]
              and observation.get("brief_sha256") == binding["brief_sha256"],
              "quality_observation_binding_invalid")


def _validate_assessment(assessment, policy, policy_digest, binding, now):
    _need(isinstance(assessment, dict) and type(assessment.get("schema_version")) is int
          and assessment["schema_version"] == 1 and assessment.get("verified") is True
          and _text(assessment.get("evidence_ref")), "assessment_unverified")
    _need(not any(key in assessment for key in ("confidence", "self_confidence", "probability", "overall_score")),
          "model_confidence_or_overall_score_not_accepted")
    _need(assessment.get("binding") == binding, "artifact_brief_source_or_destination_changed")
    _need(assessment.get("policy_digest") == policy_digest, "quality_policy_changed")
    checked_at = _stamp(assessment.get("checked_at"))
    _need(_stamp(policy["accepted_at"]) <= checked_at
          and 0 <= (now - checked_at).total_seconds() <= policy["assessment_max_age_seconds"],
          "quality_assessment_stale_or_future")
    checks, results = assessment.get("critical_checks"), assessment.get("criteria")
    _need(isinstance(checks, dict) and set(checks) == set(policy["critical_checks"])
          and isinstance(results, dict) and set(results) == {c["id"] for c in policy["criteria"]},
          "quality_check_set_incomplete_or_extra")
    failures, unassessed, rows, failure_details = [], [], [], {}
    for name in policy["critical_checks"]:
        record = checks[name]
        # A missing/unassessed check never becomes a numeric zero or auto-revision.
        if record is None or (isinstance(record, dict) and record.get("result") in (None, "unassessed")):
            unassessed.append(name)
            continue
        _validate_record(record, policy, binding, checked_at,
                         methods=policy["critical_requirements"][name]["methods"],
                         requirement_refs=policy["critical_requirements"][name]["requirement_refs"])
        _need(record.get("result") in ("passed", "failed"), "critical_check_result_invalid")
        if record["result"] == "failed":
            failures.append(name)
            failure_details[name] = [o["finding"] for o in record["observations"]]
    total = Decimal(0)
    for criterion in policy["criteria"]:
        record = results[criterion["id"]]
        if record is None or (isinstance(record, dict) and record.get("result") in (None, "unassessed")):
            unassessed.append(criterion["id"])
            continue
        _validate_record(record, policy, binding, checked_at, methods=criterion["methods"],
                         requirement_refs=criterion["requirement_refs"])
        score = _number(record.get("score"), "criterion_score_invalid")
        expected_result = "passed" if score == 100 else "failed" if score == 0 else "partial"
        _need(record.get("result") == expected_result, "criterion_score_result_inconsistent")
        contribution = _number(criterion["weight"], "quality_weight_invalid") * score / Decimal(100)
        total += contribution
        rows.append({"criterion_id": criterion["id"], "score": _serial(score),
                     "description": criterion["description"],
                     "weight": _serial(Decimal(str(criterion["weight"]))),
                     "contribution": _serial(contribution), "evidence_ref": record["evidence_ref"],
                     "findings": [o["finding"] for o in record["observations"]]})
        if criterion["critical"] and score < 100:
            failures.append(criterion["id"])
            failure_details[criterion["id"]] = [o["finding"] for o in record["observations"]]
    return total, failures, unassessed, rows, checked_at, failure_details


def _validate_human_approval(approval, policy, binding, assessment, score, checked_at, now):
    _need(isinstance(approval, dict) and approval.get("verified") is True
          and approval.get("revoked") is False and _text(approval.get("evidence_ref")),
          "quality_human_approval_unverified")
    _need(approval.get("actor") in policy["approval_actors"], "quality_human_actor_not_allowed")
    _need(approval.get("binding") == binding and approval.get("assessment_sha256") == scope_digest(assessment)
          and approval.get("policy_digest") == scope_digest(policy)
          and _number(approval.get("score"), "human_approval_score_invalid") == score,
          "quality_human_approval_binding_changed")
    _need(approval.get("decision") == "accept_quality_for_customer_delivery", "quality_human_decision_invalid")
    issued, expires = _stamp(approval.get("issued_at")), _stamp(approval.get("expires_at"))
    _need(checked_at <= issued <= now < expires
          and 0 < (expires - issued).total_seconds() <= policy["approval_max_age_seconds"],
          "quality_human_approval_expired_or_future")


def evaluate_quality(policy, assessment, binding, safety, *, now, human_approval=None):
    """Return only a conformance route; never emit a send/comment/task intent.

    Trusted current safety evidence has precedence over all scores/approvals.
    Known hard failures request revision even if other quality checks remain
    unassessed; without a known failure, an unassessed check holds the work.
    A revision route itself cannot authorize a Basecamp write or extra round.
    """
    result = {"schema_version": 1, "route": "held", "reason": "quality_evidence_missing",
              "score": None, "score_meaning": "weighted_rubric_conformance_not_probability",
              "criteria": [], "critical_failures": [], "unassessed_checks": [],
              "send_allowed": False, "live_enabled": False, "requires_producer_submission": True,
              "release_policy_enforced_by_dispatcher": True,
              "requires_action_authority": True, "network_writes": 0, "model_calls": 0}
    try:
        now = _stamp(now)
        _validate_binding(binding)
        _validate_safety(safety, binding, now)
        policy_digest, human_min, candidate_min = _validate_policy(policy, binding, now)
        score, failures, unassessed, rows, checked_at, failure_details = _validate_assessment(
            assessment, policy, policy_digest, binding, now)
        result.update(criteria=rows, critical_failures=failures, unassessed_checks=unassessed,
                      failure_details=failure_details,
                      binding_sha256=scope_digest(binding), policy_digest=policy_digest,
                      assessment_sha256=scope_digest(assessment))
        if not unassessed:
            result["score"] = _serial(score)
        if failures:
            result.update(route="revision", reason="critical_quality_failure")
        elif unassessed:
            result.update(reason="quality_checks_unassessed")
        elif score < human_min:
            result.update(route="revision", reason="score_below_human_review_band")
        elif score < candidate_min:
            result.update(route="needs_human_approval", reason="score_requires_authorized_quality_approval")
            if human_approval is not None:
                _validate_human_approval(human_approval, policy, binding, assessment, score, checked_at, now)
                result.update(route="delivery_candidate", reason="human_accepted_exact_assessment",
                              human_approval_ref=human_approval["evidence_ref"])
        else:
            result.update(route="delivery_candidate", reason="score_meets_candidate_threshold")
        return result
    except (QualityGateError, KeyError, ValueError, TypeError, AttributeError, InvalidOperation) as exc:
        result.update(route="held", reason=str(exc) if isinstance(exc, QualityGateError)
                      else "malformed_quality_evidence")
        return result
