"""Offline profile and transport contract tests. Never contact a provider."""
import copy
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

import model
from model_profiles import (ASTRA_MAX, SOL_MEDIUM_CANDIDATE, SOL_MEDIUM_EVALUATION, PROFILES,
                            ProfileError, resolve_profile)
from test_model import INPUT, FAKE_KEY, FakeResponse, draft, envelope


class ProfileTests(unittest.TestCase):
    def code(self, expected, action):
        with self.assertRaises((model.ModelError, ProfileError)) as got:
            action()
        self.assertEqual(got.exception.code, expected)
        return got.exception

    def test_profiles_and_registry_are_immutable(self):
        with self.assertRaises(FrozenInstanceError):
            ASTRA_MAX.model = "other"
        with self.assertRaises(TypeError):
            PROFILES["other"] = ASTRA_MAX
        self.assertIs(resolve_profile(), ASTRA_MAX)
        self.assertIs(resolve_profile(ASTRA_MAX.profile_id, for_live=True), ASTRA_MAX)

    def test_unknown_or_modified_profile_never_reaches_transport(self):
        for profile, code in (("other", "model_profile_unknown"),
                              ({"model": "gpt-6-astra"}, "model_profile_invalid"),
                              (replace(SOL_MEDIUM_CANDIDATE, live_enabled=True), "model_profile_mismatch"),
                              (replace(ASTRA_MAX, model="gpt-6.1-sol"), "model_profile_mismatch")):
            with patch.object(model.requests, "Session") as session:
                self.code(code, lambda: model.call_once(FAKE_KEY, INPUT, profile=profile))
                session.assert_not_called()

    def test_candidate_live_call_disabled_before_credential_or_session(self):
        for profile in (SOL_MEDIUM_CANDIDATE, SOL_MEDIUM_CANDIDATE.profile_id):
            with patch.object(model.requests, "Session") as session:
                self.code("model_profile_live_disabled", lambda: model.call_once(None, INPUT, profile=profile))
                session.assert_not_called()

    def test_explicit_evaluation_profile_never_changes_production_default(self):
        self.assertIs(resolve_profile(for_live=True), ASTRA_MAX)
        self.assertIs(resolve_profile(SOL_MEDIUM_EVALUATION.profile_id, for_live=True), SOL_MEDIUM_EVALUATION)
        self.assertTrue(SOL_MEDIUM_EVALUATION.evaluation_only)
        self.assertFalse(SOL_MEDIUM_CANDIDATE.live_enabled)
        raw = envelope(); raw.update(model="gpt-6.1-sol", reasoning={"effort": "medium"})
        with patch.object(model.requests, "Session") as ctor:
            ctor.return_value.post.return_value = FakeResponse(raw)
            result = model.call_once(FAKE_KEY, INPUT, profile=SOL_MEDIUM_EVALUATION)
            self.assertEqual(ctor.return_value.post.call_count, 1)
            self.assertEqual(result["profile_id"], "sol-medium-evaluation-v1")
            self.assertFalse(result["action_eligible"])

    def test_offline_candidate_request_is_bounded_review_only(self):
        request = model.build_request(INPUT, profile=SOL_MEDIUM_CANDIDATE)
        self.assertEqual(request["model"], "gpt-6.1-sol")
        self.assertEqual(request["reasoning"], {"effort": "medium"})
        self.assertEqual(request["max_output_tokens"], 8192)
        self.assertEqual(request["tools"], [])
        self.assertEqual(request["tool_choice"], "none")
        self.assertFalse(request["store"])

    def test_candidate_fake_transport_has_exact_selected_profile_and_one_call(self):
        calls = []
        raw = envelope()
        raw.update(model="gpt-6.1-sol", reasoning={"effort": "medium"})
        response = FakeResponse(raw)
        def fake(*args, **kwargs):
            calls.append((args, kwargs))
            return response
        result = model.call_once(FAKE_KEY, INPUT, profile=SOL_MEDIUM_CANDIDATE, transport=fake)
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0][1]["data"])["model"], "gpt-6.1-sol")
        self.assertEqual(result["profile_id"], SOL_MEDIUM_CANDIDATE.profile_id)
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["reasoning_effort"], "medium")
        self.assertEqual(result["status"], "review_required")
        self.assertFalse(result["action_eligible"])
        self.assertFalse(result["automatic_retry_allowed"])
        self.assertTrue(response.closed)

    def test_wrong_model_or_effort_rejected_without_fallback(self):
        for selected, returned_model, effort, code in (
            (SOL_MEDIUM_CANDIDATE, "gpt-6-astra", "max", "model_mismatch"),
            (ASTRA_MAX, "gpt-6.1-sol", "medium", "model_mismatch"),
            (SOL_MEDIUM_CANDIDATE, "gpt-6.1-sol", "max", "reasoning_mismatch"),
            (ASTRA_MAX, "gpt-6-astra", "medium", "reasoning_mismatch")):
            calls = []
            raw = envelope(); raw.update(model=returned_model, reasoning={"effort": effort})
            def fake(*args, **kwargs):
                calls.append(1)
                return FakeResponse(raw)
            error = self.code(code, lambda: model.call_once(FAKE_KEY, INPUT, profile=selected, transport=fake))
            self.assertEqual(calls, [1])
            self.assertFalse(error.retry_allowed)
            self.assertIsNone(error.failure_receipt["reasoning_effort"])

    def test_candidate_usage_cap_and_failure_receipt_use_selected_profile(self):
        raw = envelope(); raw["model"] = "gpt-6.1-sol"
        raw["usage"].update(output_tokens=8193, total_tokens=9393)
        error = self.code("usage_limit_exceeded", lambda: model.extract_response(raw, INPUT, profile=SOL_MEDIUM_CANDIDATE))
        self.assertEqual(error.usage["output_tokens"], 8193)
        self.assertEqual(error.failure_receipt["model"], "gpt-6.1-sol")
        self.assertEqual(error.failure_receipt["reasoning_effort"], "medium")
        raw["usage"].update(output_tokens=8192, total_tokens=9392)
        self.assertEqual(model.extract_response(raw, INPUT, profile=SOL_MEDIUM_CANDIDATE)["usage"]["output_tokens"], 8192)

    def test_incomplete_candidate_no_fallback_and_preserves_usage(self):
        raw = envelope()
        raw.update(model="gpt-6.1-sol", status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        calls = []
        def fake(*args, **kwargs):
            calls.append(1)
            return FakeResponse(raw)
        error = self.code("response_incomplete", lambda: model.call_once(FAKE_KEY, INPUT, profile=SOL_MEDIUM_CANDIDATE, transport=fake))
        self.assertEqual(calls, [1])
        self.assertEqual(error.failure_receipt["model"], "gpt-6.1-sol")
        self.assertEqual(error.failure_receipt["usage"]["total_tokens"], 1700)
        self.assertFalse(error.retry_allowed)

    def test_candidate_unknown_transport_outcome_never_falls_back(self):
        calls = []
        def fake(*args, **kwargs):
            calls.append(1)
            raise RuntimeError(FAKE_KEY)
        error = self.code("transport_outcome_unknown", lambda: model.call_once(FAKE_KEY, INPUT, profile=SOL_MEDIUM_CANDIDATE, transport=fake))
        self.assertEqual(calls, [1])
        self.assertIsNone(error.failure_receipt)
        self.assertNotIn(FAKE_KEY, str(error))

    def test_provider_neutral_domain_validation_preserves_business_guards(self):
        value = draft()
        validated = model.validate_analysis(value, INPUT)
        value["known_facts"].append("later mutation")
        self.assertNotIn("later mutation", validated["known_facts"])
        bad = copy.deepcopy(validated); bad["intent"] = "commercial"
        self.code("escalation_mismatch", lambda: model.validate_analysis(bad, INPUT))
        bad = copy.deepcopy(validated); bad["task_draft"]["assignee"] = "invented"
        self.code("invalid_analysis_schema", lambda: model.validate_analysis(bad, INPUT))


if __name__ == "__main__":
    unittest.main()
