"""Offline adversarial tests. Evidence is synthetic, never customer acceptance."""
import copy
import unittest
from datetime import datetime, timedelta, timezone

from autonomy_policy import scope_digest
from v11_quality_gate import MANDATORY_CHECKS, QualityGateError, build_quality_binding, build_quality_note_context, evaluate_quality

NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)


def stamp(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def fixture(score="90"):
    binding = {"brand_id": "wat_mobilite", "work_id": "synthetic-work-1",
               "service_id": "synthetic-design", "thread_id": "synthetic-thread-1", "version": "v1",
               "sha256": "a" * 64, "brief_sha256": "b" * 64, "manifest_sha256": "c" * 64,
               "scope_digest": "d" * 64, "source_fingerprint": "e" * 64,
               "recipients_sha256": "f" * 64, "action_payload_sha256": "1" * 64,
               "global_generation": 1, "work_generation": 3}
    policy = {"schema_version": 1, "policy_id": "synthetic-wat-quality", "version": "1",
              "brand_id": "wat_mobilite", "service_id": "synthetic-design",
              "status": "accepted", "verified": True, "evidence_ref": "synthetic-policy-acceptance",
              "accepted_at": stamp(-3600), "expires_at": stamp(3600),
              "assessment_max_age_seconds": 1800, "approval_max_age_seconds": 600,
              "approval_actors": ["authorized@example.test"],
              "trusted_evaluators": {"deterministic": ["synthetic-parser"],
                                     "human_review": ["synthetic-reviewer"],
                                     "model_rubric": ["synthetic-vision-adapter"]},
              "critical_checks": list(MANDATORY_CHECKS),
              "critical_requirements": {name: {"requirement_refs": ["synthetic-brief/" + name],
                                               "methods": ["deterministic", "human_review"]}
                                        for name in MANDATORY_CHECKS},
              "criteria": [{"id": name, "description": "Synthetic " + name, "weight": weight,
                            "requirement_refs": ["synthetic-brief/" + name],
                            "critical": False, "methods": ["model_rubric", "human_review"]}
                           for name, weight in (("brief_match", 30), ("brand_match", 25),
                                                ("execution", 25), ("language", 20))],
              "thresholds": {"human_min": "80", "candidate_min": "90"}}
    record = {"verified": True, "binding": copy.deepcopy(binding), "method": "deterministic",
              "evaluator_id": "synthetic-parser", "evidence_ref": "synthetic-inspection-record",
              "checked_at": stamp(-60), "requirement_refs": ["synthetic-brief/requirement-1"],
              "observations": [{"evidence_ref": "synthetic-byte-inspection", "location": "page 1",
                                "finding": "Synthetic recorded comparison to the exact brief",
                                "artifact_sha256": binding["sha256"],
                                "brief_sha256": binding["brief_sha256"]}], "result": "passed"}
    checks = {name: copy.deepcopy(record) for name in MANDATORY_CHECKS}
    for name, check in checks.items():
        check["requirement_refs"] = ["synthetic-brief/" + name]
    criteria = {}
    for criterion in policy["criteria"]:
        criteria[criterion["id"]] = {**copy.deepcopy(record), "method": "model_rubric",
            "requirement_refs": criterion["requirement_refs"],
            "evaluator_id": "synthetic-vision-adapter", "score": score,
            "result": "passed" if str(score) == "100" else "failed" if str(score) == "0" else "partial"}
    assessment = {"schema_version": 1, "verified": True, "evidence_ref": "synthetic-assessment",
                  "binding": copy.deepcopy(binding), "policy_digest": scope_digest(policy),
                  "checked_at": stamp(-60), "critical_checks": checks, "criteria": criteria}
    safety = {"verified": True, "binding": copy.deepcopy(binding), "observed_at": stamp(-1),
              "evidence_ref": "synthetic-current-control-authority", "control": "active",
              "authority": "allowed", "security": "clear", "write_state": "new"}
    return policy, assessment, binding, safety


def human_approval(policy, assessment, binding, score="85"):
    return {"verified": True, "revoked": False, "evidence_ref": "synthetic-human-ui-acceptance",
            "actor": "authorized@example.test", "binding": copy.deepcopy(binding),
            "assessment_sha256": scope_digest(assessment), "policy_digest": scope_digest(policy),
            "score": score, "decision": "accept_quality_for_customer_delivery",
            "issued_at": stamp(-30), "expires_at": stamp(300)}


class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.policy, self.assessment, self.binding, self.safety = fixture()

    def evaluate(self, approval=None):
        return evaluate_quality(self.policy, self.assessment, self.binding, self.safety,
                                now=NOW, human_approval=approval)

    def update_policy_digest(self):
        self.assessment["policy_digest"] = scope_digest(self.policy)

    def band(self, score, expected):
        p, a, b, s = fixture(score)
        result = evaluate_quality(p, a, b, s, now=NOW)
        self.assertEqual(result["route"], expected, result)
        self.assertEqual(result["score"], score)
        self.assertFalse(result["send_allowed"])
        self.assertFalse(result["live_enabled"])

    def test_exact_90_is_only_delivery_candidate(self):
        self.band("90", "delivery_candidate")
        self.assertTrue(self.evaluate()["requires_producer_submission"])
        self.assertTrue(self.evaluate()["release_policy_enforced_by_dispatcher"])
        self.assertTrue(self.evaluate()["requires_action_authority"])

    def test_exact_80_needs_authorized_human(self):
        self.band("80", "needs_human_approval")

    def test_89_999999_never_rounds_to_90(self):
        self.band("89.999999", "needs_human_approval")

    def test_79_999999_never_rounds_to_80(self):
        self.band("79.999999", "revision")

    def test_all_100_means_rubric_conformance_not_probability(self):
        self.band("100", "delivery_candidate")
        self.assertEqual(self.evaluate()["score_meaning"], "weighted_rubric_conformance_not_probability")

    def test_zero_is_revision_when_all_checks_were_assessed(self):
        self.band("0", "revision")

    def test_score_is_computed_from_weights_not_model_overall(self):
        values = {"brief_match": 100, "brand_match": 100, "execution": 80, "language": 50}
        for name, score in values.items():
            self.assessment["criteria"][name].update(score=score, result="passed" if score == 100 else "partial")
        result = self.evaluate()
        self.assertEqual(result["score"], "85")
        self.assertEqual(result["route"], "needs_human_approval")

    def test_critical_failures_override_100(self):
        for name in ("brand_identity", "required_files", "technical_specs"):
            with self.subTest(check=name):
                p, a, b, s = fixture("100")
                a["critical_checks"][name]["result"] = "failed"
                result = evaluate_quality(p, a, b, s, now=NOW)
                self.assertEqual(result["route"], "revision", result)
                self.assertIn(name, result["critical_failures"])

    def test_unknown_check_does_not_become_zero_or_revision(self):
        self.assessment["critical_checks"]["required_files"] = None
        result = self.evaluate()
        self.assertEqual(result["route"], "held")
        self.assertEqual(result["reason"], "quality_checks_unassessed")
        self.assertIsNone(result["score"])

    def test_unassessed_criterion_holds(self):
        self.assessment["criteria"]["language"] = {"result": "unassessed"}
        result = self.evaluate()
        self.assertEqual(result["route"], "held")
        self.assertIsNone(result["score"])

    def test_missing_check_key_holds(self):
        del self.assessment["critical_checks"]["required_files"]
        self.assertEqual(self.evaluate()["reason"], "quality_check_set_incomplete_or_extra")

    def test_unknown_extra_criterion_not_silently_ignored(self):
        self.assessment["criteria"]["extra"] = copy.deepcopy(self.assessment["criteria"]["language"])
        self.assertEqual(self.evaluate()["route"], "held")

    def test_known_hard_failure_can_request_revision_while_other_check_unknown(self):
        self.assessment["critical_checks"]["required_files"]["result"] = "failed"
        self.assessment["criteria"]["language"] = None
        result = self.evaluate()
        self.assertEqual(result["route"], "revision")
        self.assertIsNone(result["score"])

    def test_critical_weighted_criterion_cannot_average_away_failure(self):
        self.policy["criteria"][0]["critical"] = True
        self.update_policy_digest()
        self.assertEqual(self.evaluate()["route"], "revision")

    def test_no_observations_means_no_score(self):
        self.assessment["criteria"]["language"]["observations"] = []
        self.assertEqual(self.evaluate()["reason"], "quality_observations_missing")

    def test_unknown_evaluator_holds(self):
        self.assessment["criteria"]["language"]["evaluator_id"] = "untrusted-generated-id"
        self.assertEqual(self.evaluate()["reason"], "quality_evaluator_not_authorized")

    def test_self_confidence_is_not_an_allowed_evaluation_method(self):
        self.assessment["criteria"]["language"]["method"] = "model_confidence"
        self.assertEqual(self.evaluate()["reason"], "quality_evaluator_not_authorized")

    def test_confidence_or_overall_score_is_not_accepted(self):
        for field in ("confidence", "self_confidence", "probability", "overall_score"):
            with self.subTest(field=field):
                p, a, b, s = fixture()
                a[field] = 100
                self.assertEqual(evaluate_quality(p, a, b, s, now=NOW)["route"], "held")
        self.assessment["criteria"]["language"]["confidence"] = 1
        self.assertEqual(self.evaluate()["reason"], "model_confidence_is_not_quality")

    def test_score_rejects_boolean_nonfinite_and_out_of_range(self):
        for value in (True, False, "NaN", "Infinity", -1, "100.1", "90.0000001"):
            with self.subTest(value=value):
                p, a, b, s = fixture()
                a["criteria"]["language"]["score"] = value
                self.assertEqual(evaluate_quality(p, a, b, s, now=NOW)["route"], "held")

    def test_full_score_cannot_be_labelled_failed(self):
        self.assessment["criteria"]["language"].update(score=100, result="failed")
        self.assertEqual(self.evaluate()["reason"], "criterion_score_result_inconsistent")

    def test_any_immutable_binding_change_invalidates_qa(self):
        for field in self.binding:
            with self.subTest(field=field):
                p, a, b, s = fixture()
                b[field] = b[field] + 1 if type(b[field]) is int else "9" * 64 if len(b[field]) == 64 else b[field] + "-changed"
                s["binding"] = copy.deepcopy(b)
                if field in ("brand_id", "service_id"):
                    p[field] = b[field]
                    a["policy_digest"] = scope_digest(p)
                result = evaluate_quality(p, a, b, s, now=NOW)
                self.assertEqual(result["reason"], "artifact_brief_source_or_destination_changed", result)

    def test_unrelated_or_omitted_requirement_reference_cannot_pass(self):
        self.assessment["criteria"]["language"]["requirement_refs"] = ["unrelated-brief-item"]
        self.assertEqual(self.evaluate()["reason"], "quality_requirement_mapping_mismatch")

    def test_critical_check_methods_follow_accepted_policy(self):
        self.assessment["critical_checks"]["rights_and_disclosure"].update(
            method="model_rubric", evaluator_id="synthetic-vision-adapter")
        self.assertEqual(self.evaluate()["reason"], "quality_evaluator_not_authorized")

    def test_build_binding_uses_current_envelope_and_control(self):
        request = {"artifact": {"version": "v2", "sha256": "0" * 64},
                   "source": {"thread_id": "t2", "fingerprint": "1" * 64},
                   "control": {"global_generation": 2, "work_generation": 7},
                   "brand_id": "wat_mobilite", "work_id": "w2", "service_id": "design",
                   "reply_to": ["customer@example.test"], "reply_cc": ["cc@example.test"],
                   "prepared_action": {"payload_sha256": "2" * 64}}
        binding = build_quality_binding(request, brief_sha256="3" * 64,
                                        manifest_sha256="4" * 64, scope_sha256="5" * 64)
        self.assertEqual(binding["sha256"], "0" * 64)
        self.assertEqual(binding["recipients_sha256"], scope_digest({"to": request["reply_to"], "cc": request["reply_cc"]}))
        self.assertEqual(binding["work_generation"], 7)

    def test_quality_note_context_binds_exact_assessment_and_task(self):
        result = self.evaluate()
        request = {'work_binding': {'account_id': 1, 'project_id': 2, 'list_id': 3, 'task_id': 4}}
        context = build_quality_note_context(request, result)
        self.assertEqual(context['assessment_sha256'], scope_digest(self.assessment))
        self.assertEqual(context['quality_binding_sha256'], scope_digest(self.binding))
        self.assertEqual(context['basecamp_target']['task_id'], 4)
        self.assertIsNone(context['human_approval_sha256'])
        request['work_binding']['task_id'] = 5
        self.assertNotEqual(scope_digest(context), scope_digest(build_quality_note_context(request, result)))

    def test_quality_note_context_binds_human_record_and_rejects_non_candidate(self):
        self.policy, self.assessment, self.binding, self.safety = fixture('85')
        accepted = human_approval(self.policy, self.assessment, self.binding)
        result = self.evaluate(accepted)
        request = {'work_binding': {'account_id': 1, 'project_id': 2, 'list_id': 3, 'task_id': 4},
                   'quality_human_approval': accepted}
        context = build_quality_note_context(request, result)
        self.assertEqual(context['human_approval_sha256'], scope_digest(accepted))
        with self.assertRaises(QualityGateError):
            build_quality_note_context(request, self.evaluate())

    def test_changed_nested_evidence_binding_is_held(self):
        self.assessment["criteria"]["language"]["binding"]["sha256"] = "9" * 64
        self.assertEqual(self.evaluate()["reason"], "quality_check_evidence_unverified")

    def test_observation_must_refer_to_actual_bytes_and_brief(self):
        self.assessment["criteria"]["language"]["observations"][0]["artifact_sha256"] = "9" * 64
        self.assertEqual(self.evaluate()["reason"], "quality_observation_binding_invalid")

    def test_policy_draft_never_accepts_customer_delivery(self):
        self.policy["status"] = "draft"
        self.assertEqual(self.evaluate()["reason"], "quality_policy_not_accepted")

    def test_policy_expiry_and_new_policy_version_invalidate(self):
        self.policy["version"] = "2"
        self.assertEqual(self.evaluate()["reason"], "quality_policy_changed")
        self.policy["expires_at"] = stamp()
        self.assertEqual(self.evaluate()["reason"], "quality_policy_expired")

    def test_policy_must_include_all_critical_checks(self):
        self.policy["critical_checks"].remove("required_files")
        self.assertEqual(self.evaluate()["reason"], "critical_check_policy_incomplete")

    def test_policy_must_have_positive_weights_totalling_100(self):
        self.policy["criteria"][0]["weight"] = 10
        self.assertEqual(self.evaluate()["reason"], "quality_weights_must_total_100")

    def test_policy_thresholds_are_explicit_and_ordered(self):
        self.policy["thresholds"]["human_min"] = 95
        self.assertEqual(self.evaluate()["reason"], "quality_threshold_order_invalid")

    def test_safety_conditions_override_any_quality_score(self):
        for field, value, reason in (
            ("control", "stopped", "human_stop_or_takeover"),
            ("authority", "unknown", "authority_requires_resolution"),
            ("authority", "denied", "authority_requires_resolution"),
            ("security", "unknown", "security_requires_resolution"),
            ("security", "blocked", "security_requires_resolution"),
            ("write_state", "started", "write_already_started_unknown_or_confirmed"),
            ("write_state", "unknown", "write_already_started_unknown_or_confirmed"),
            ("write_state", "confirmed", "write_already_started_unknown_or_confirmed")):
            with self.subTest(field=field, value=value):
                p, a, b, s = fixture("100")
                s[field] = value
                result = evaluate_quality(p, a, b, s, now=NOW)
                self.assertEqual(result["route"], "held")
                self.assertEqual(result["reason"], reason)

    def test_safety_observation_must_be_fresh_and_exact(self):
        self.safety["observed_at"] = stamp(-61)
        self.assertEqual(self.evaluate()["reason"], "current_safety_evidence_stale")
        self.safety["observed_at"] = stamp(-1)
        self.safety["binding"]["global_generation"] += 1
        self.assertEqual(self.evaluate()["reason"], "current_safety_evidence_missing")

    def test_assessment_age_and_future_are_rejected(self):
        for age in (-1801, 1):
            self.assessment["checked_at"] = stamp(age)
            self.assertEqual(self.evaluate()["reason"], "quality_assessment_stale_or_future")

    def test_midband_approval_accepts_only_exact_quality_assessment(self):
        self.policy, self.assessment, self.binding, self.safety = fixture("85")
        approval = human_approval(self.policy, self.assessment, self.binding)
        result = self.evaluate(approval)
        self.assertEqual(result["route"], "delivery_candidate", result)
        self.assertEqual(result["reason"], "human_accepted_exact_assessment")
        self.assertFalse(result["send_allowed"])
        self.assertTrue(result["requires_producer_submission"])

    def test_unallowed_expired_future_revoked_human_approval_holds(self):
        self.policy, self.assessment, self.binding, self.safety = fixture("85")
        valid = human_approval(self.policy, self.assessment, self.binding)
        for field, value in (("actor", "intruder@example.test"), ("expires_at", stamp()),
                             ("issued_at", stamp(1)), ("revoked", True),
                             ("decision", "looks_good"), ("score", "84.9"),
                             ("expires_at", stamp(3600))):
            with self.subTest(field=field):
                approval = copy.deepcopy(valid)
                approval[field] = value
                self.assertEqual(self.evaluate(approval)["route"], "held")

    def test_human_approval_before_assessment_is_rejected(self):
        self.policy, self.assessment, self.binding, self.safety = fixture("85")
        approval = human_approval(self.policy, self.assessment, self.binding)
        approval["issued_at"] = stamp(-61)
        self.assertEqual(self.evaluate(approval)["reason"], "quality_human_approval_expired_or_future")

    def test_human_approval_cannot_override_critical_failure_or_low_score(self):
        for score in ("85", "79"):
            p, a, b, s = fixture(score)
            approval = human_approval(p, a, b, score)
            a["critical_checks"]["required_files"]["result"] = "failed"
            approval["assessment_sha256"] = scope_digest(a)
            self.assertEqual(evaluate_quality(p, a, b, s, now=NOW, human_approval=approval)["route"], "revision")
        p, a, b, s = fixture("79")
        approval = human_approval(p, a, b, "79")
        self.assertEqual(evaluate_quality(p, a, b, s, now=NOW, human_approval=approval)["route"], "revision")

    def test_changed_assessment_evidence_invalidates_human_approval_even_same_score(self):
        self.policy, self.assessment, self.binding, self.safety = fixture("85")
        approval = human_approval(self.policy, self.assessment, self.binding)
        self.assessment["criteria"]["language"]["observations"][0]["finding"] = "Changed evidence"
        self.assertEqual(self.evaluate(approval)["reason"], "quality_human_approval_binding_changed")

    def test_function_does_not_mutate_inputs(self):
        original = copy.deepcopy((self.policy, self.assessment, self.binding, self.safety))
        self.evaluate()
        self.assertEqual((self.policy, self.assessment, self.binding, self.safety), original)

    def test_malformed_evidence_returns_hold_instead_of_crashing(self):
        for p, a, b, s in ((None, None, None, None),
                           (self.policy, [], self.binding, self.safety),
                           (self.policy, self.assessment, self.binding, {})):
            self.assertEqual(evaluate_quality(p, a, b, s, now=NOW)["route"], "held")


if __name__ == "__main__":
    unittest.main()
