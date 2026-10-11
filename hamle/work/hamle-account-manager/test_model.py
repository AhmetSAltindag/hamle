import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import model


INPUT = json.loads(Path(__file__).with_name("synthetic_request.json").read_text())
FAKE_KEY = "unit-test-placeholder-not-a-real-key"


def draft():
    return {
        "request_summary": "Yeni ekip arkadaşları için tek sayfalık Türkçe rehber isteniyor.",
        "intent": "routine_request",
        "known_facts": ["Talep iç eğitim içindir."],
        "missing_questions": ["Rehberin onayını kim verecek?"],
        "brief": {"deliverables": ["Tek sayfalık başlangıç rehberi"],
                  "audience": INPUT["audience"], "requested_deadline": INPUT["requested_deadline"],
                  "confirmed_deadline": None},
        "reply_draft": {"subject": "İç eğitim rehberi", "body": "Talebi aldım. İstenen tarihi kapasite kontrolü sonrası netleştirebiliriz."},
        "task_draft": {"title": "Başlangıç rehberi hazırla", "description": "Talep, brief ve revizyon adımlarını anlat.",
                       "assignee": None, "due_at": None},
        "escalation": {"owner": "none", "reason": None}}


def envelope(analysis=None):
    return {"id": "resp_example", "model": model.MODEL, "status": "completed",
            "error": None, "incomplete_details": None,
            "output": [{"type": "reasoning", "summary": []},
                       {"type": "message", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": json.dumps(analysis or draft())}]}],
            "usage": {"input_tokens": 1200, "output_tokens": 500, "total_tokens": 1700,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 200}}}


class FakeResponse:
    def __init__(self, data=None, *, status=200, raw=None):
        self.status_code = status
        self.raw = json.dumps(data if data is not None else envelope()).encode() if raw is None else raw
        self.closed = False
    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw), chunk_size):
            yield self.raw[start:start + chunk_size]
    def close(self):
        self.closed = True


class ModelTests(unittest.TestCase):
    def assert_code(self, code, action):
        with self.assertRaises(model.ModelError) as got:
            action()
        self.assertEqual(got.exception.code, code)
        self.assertEqual(str(got.exception), code)
        self.assertFalse(got.exception.retry_allowed)
        return got.exception

    def test_success_returns_review_only_and_omits_reasoning(self):
        result = model.extract_response(envelope(), INPUT)
        self.assertEqual(result["status"], "review_required")
        self.assertFalse(result["action_eligible"])
        self.assertFalse(result["automatic_retry_allowed"])
        self.assertNotIn("output", result)
        self.assertNotIn("summary", result)

    def test_request_fixes_model_effort_output_bound_and_disables_tools_storage(self):
        req = model.build_request(INPUT)
        self.assertEqual(req["model"], "gpt-6-astra")
        self.assertEqual(req["reasoning"], {"effort": "max"})
        self.assertEqual(req["max_output_tokens"], 16384)
        self.assertEqual(req["service_tier"], "default")
        self.assertEqual(req["tools"], [])
        self.assertEqual(req["tool_choice"], "none")
        self.assertFalse(req["store"])
        self.assertFalse(req["background"])
        self.assertTrue(req["text"]["format"]["strict"])
        self.assertEqual(json.loads(req["input"][0]["content"]), INPUT)
        self.assertEqual(req["input"][0]["role"], "user")

    def test_input_bytes_include_multibyte_characters(self):
        source = copy.deepcopy(INPUT)
        source["request_text"] = "ş" * 8000
        self.assert_code("input_byte_limit", lambda: model.build_request(source))

    def test_request_size_counts_prompt_and_schema(self):
        with patch.object(model, "MAX_REQUEST_BYTES", 100):
            self.assert_code("request_byte_limit", lambda: model.build_request(INPUT))

    def test_unknown_brand_and_extra_input_authority_rejected(self):
        for key, val in (("brand", "ya da/Multicosmetics"), ("send_authorized", True)):
            source = {**INPUT, key: val}
            self.assert_code("invalid_input", lambda: model.build_request(source))

    def test_deadline_must_have_timezone(self):
        for value in ("yarın", "2026-10-12T15:00:00"):
            source = {**INPUT, "requested_deadline": value}
            self.assert_code("invalid_input_deadline", lambda: model.build_request(source))

    def test_incomplete_never_accepted_even_if_valid_draft_and_usage_retained(self):
        raw = envelope()
        raw["status"] = "incomplete"
        raw["incomplete_details"] = {"reason": "max_output_tokens"}
        error = self.assert_code("response_incomplete", lambda: model.extract_response(raw))
        self.assertEqual(error.usage["output_tokens"], 500)
        self.assertEqual(error.response_id, "resp_example")
        self.assertEqual(error.status, "incomplete")
        self.assertEqual(error.incomplete_reason, "max_output_tokens")
        self.assertEqual(error.failure_receipt, {
            "response_id": "resp_example", "model": model.MODEL, "reasoning_effort": "max",
            "status": "incomplete", "incomplete_reason": "max_output_tokens", "usage": raw["usage"]})

    def test_failure_receipt_omits_partial_output_reasoning_and_freeform_errors(self):
        raw = envelope()
        raw.update(status="incomplete", incomplete_details={"reason": "max_output_tokens", "message": FAKE_KEY},
                   error={"code": "untrusted-code-" + FAKE_KEY, "message": FAKE_KEY},
                   output=[{"type": "reasoning", "summary": [FAKE_KEY]},
                           {"type": "message", "content": [{"type": "output_text", "text": FAKE_KEY}]}])
        raw["usage"]["output_tokens_details"]["raw_reasoning"] = FAKE_KEY
        error = self.assert_code("response_incomplete", lambda: model.extract_response(raw))
        self.assertNotIn(FAKE_KEY, json.dumps(vars(error)))
        self.assertEqual(error.failure_receipt["usage"]["output_tokens_details"], {"reasoning_tokens": 200})
        # Later mutations of the provider response cannot alter saved metadata.
        raw["usage"]["output_tokens_details"]["reasoning_tokens"] = 0
        self.assertEqual(error.failure_receipt["usage"]["output_tokens_details"]["reasoning_tokens"], 200)

    def test_unrecognized_failure_metadata_is_null_and_never_echoed(self):
        for field, value in (("id", FAKE_KEY), ("status", FAKE_KEY), ("model", FAKE_KEY),
                             ("usage", {"secret": FAKE_KEY}),
                             ("incomplete_details", {"reason": FAKE_KEY})):
            raw = envelope()
            raw.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
            raw[field] = value
            with self.assertRaises(model.ModelError) as got:
                model.extract_response(raw)
            receipt = got.exception.failure_receipt
            self.assertNotIn(FAKE_KEY, json.dumps(vars(got.exception)))
            key = {"id": "response_id", "incomplete_details": "incomplete_reason"}.get(field, field)
            self.assertIsNone(receipt[key])

    def test_incomplete_http_response_has_one_attempt_and_safe_failure_receipt(self):
        raw = envelope()
        raw.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        raw["usage"].update(output_tokens=model.MAX_OUTPUT_TOKENS,
                            total_tokens=1200 + model.MAX_OUTPUT_TOKENS)
        raw["usage"]["output_tokens_details"]["reasoning_tokens"] = model.MAX_OUTPUT_TOKENS
        response = FakeResponse(raw)
        calls = []
        def transport(*args, **kwargs):
            calls.append(1)
            return response
        error = self.assert_code("response_incomplete", lambda: model.call_once(FAKE_KEY, INPUT, transport=transport))
        self.assertEqual(calls, [1])
        self.assertTrue(response.closed)
        self.assertEqual(error.failure_receipt["usage"]["output_tokens"], model.MAX_OUTPUT_TOKENS)
        self.assertEqual(error.response_id, raw["id"])
        self.assertNotIn("analysis", error.failure_receipt)

    def test_refusal_or_completed_schema_failure_still_preserves_accounting_metadata(self):
        for part, code in (({"type": "refusal", "refusal": FAKE_KEY}, "model_refusal"),
                           ({"type": "output_text", "text": "{}"}, "invalid_analysis_schema")):
            raw = envelope(); raw["output"][1]["content"] = [part]
            error = self.assert_code(code, lambda: model.extract_response(raw))
            self.assertEqual(error.failure_receipt["response_id"], "resp_example")
            self.assertEqual(error.failure_receipt["status"], "completed")
            self.assertIsNone(error.failure_receipt["incomplete_reason"])
            self.assertEqual(error.failure_receipt["usage"]["total_tokens"], 1700)

    def test_other_terminal_and_nonterminal_statuses_rejected(self):
        for status in (None, "failed", "cancelled", "queued", "in_progress"):
            raw = {**envelope(), "status": status}
            self.assert_code("response_not_completed", lambda: model.extract_response(raw))

    def test_wrong_model_rejected(self):
        for name in ("gpt-6-sol", "gpt-6-astra-other", None):
            raw = {**envelope(), "model": name}
            self.assert_code("model_mismatch", lambda: model.extract_response(raw))

    def test_refusal_has_no_body_or_unsafe_text_in_error(self):
        raw = envelope()
        raw["output"][1]["content"] = [{"type": "refusal", "refusal": FAKE_KEY}]
        error = self.assert_code("model_refusal", lambda: model.extract_response(raw))
        self.assertNotIn(FAKE_KEY, str(error))

    def test_any_tool_call_or_unknown_output_rejected(self):
        for kind in ("function_call", "web_search_call", "computer_call", "future_type"):
            raw = envelope()
            raw["output"].append({"type": kind, "arguments": FAKE_KEY})
            self.assert_code("unexpected_tool_or_output", lambda: model.extract_response(raw))

    def test_multiple_messages_or_text_parts_rejected(self):
        raw = envelope()
        raw["output"].append(copy.deepcopy(raw["output"][1]))
        self.assert_code("ambiguous_or_oversize_output", lambda: model.extract_response(raw))

    def test_invalid_json_or_duplicate_json_keys_rejected(self):
        for text in ("{", '{"intent":"routine_request","intent":"commercial"}', '{"a":NaN}'):
            raw = envelope()
            raw["output"][1]["content"][0]["text"] = text
            self.assert_code("invalid_response_json", lambda: model.extract_response(raw))

    def test_output_extra_fields_and_missing_fields_rejected(self):
        for value in ({**draft(), "send": True}, {k: v for k, v in draft().items() if k != "intent"}):
            self.assert_code("invalid_analysis_schema", lambda: model.extract_response(envelope(value)))

    def test_unapproved_deadline_or_assignee_cannot_pass(self):
        for section, field in (("brief", "confirmed_deadline"), ("task_draft", "assignee"), ("task_draft", "due_at")):
            value = draft()
            value[section][field] = "invented"
            self.assert_code("invalid_analysis_schema", lambda: model.extract_response(envelope(value)))

    def test_requested_deadline_preserved_exactly(self):
        value = draft()
        value["brief"]["requested_deadline"] = "2026-10-13T15:00:00+03:00"
        self.assert_code("requested_deadline_changed", lambda: model.extract_response(envelope(value), INPUT))

    def test_routes_need_required_escalation_and_reason(self):
        for intent, owner in (("commercial", "ahmet"), ("complaint_strategy", "cansu_and_ahmet"),
                              ("social_planning", "ozge_and_hamle"), ("unclear", "human_review")):
            value = draft()
            value["intent"] = intent
            self.assert_code("escalation_mismatch", lambda: model.extract_response(envelope(value)))
            value["escalation"]["owner"] = owner
            self.assert_code("escalation_reason_missing", lambda: model.extract_response(envelope(value)))
            value["escalation"]["reason"] = "İnsan kararı gerekiyor."
            self.assertEqual(model.extract_response(envelope(value))["analysis"]["escalation"]["owner"], owner)

    def test_missing_invalid_or_boolean_usage_rejected(self):
        for usage in (None, {}, {"input_tokens": True, "output_tokens": 1, "total_tokens": 2},
                      {"input_tokens": 1, "output_tokens": 1, "total_tokens": 3}):
            raw = {**envelope(), "usage": usage}
            self.assert_code("usage_missing_or_invalid", lambda: model.extract_response(raw))

    def test_token_limit_violation_retains_usage_and_stops(self):
        raw = envelope()
        raw["usage"].update(output_tokens=16385, total_tokens=17585)
        error = self.assert_code("usage_limit_exceeded", lambda: model.extract_response(raw))
        self.assertEqual(error.usage["output_tokens"], 16385)

    def test_completed_response_at_revised_token_limit_remains_review_only(self):
        raw = envelope()
        raw["usage"].update(output_tokens=16384, total_tokens=17584)
        result = model.extract_response(raw, INPUT)
        self.assertEqual(result["usage"]["output_tokens"], 16384)
        self.assertEqual(result["status"], "review_required")
        self.assertFalse(result["action_eligible"])
        self.assertFalse(result["automatic_retry_allowed"])

    def test_http_rejections_are_single_calls_and_do_not_read_error_body(self):
        for status in (301, 400, 401, 403, 429, 500):
            calls = []
            response = FakeResponse(status=status, raw=FAKE_KEY.encode())
            def transport(*args, **kwargs):
                calls.append((args, kwargs))
                return response
            with self.assertRaises(model.ModelError) as got:
                model.call_once(FAKE_KEY, INPUT, transport=transport)
            self.assertNotIn(FAKE_KEY, str(got.exception))
            self.assertEqual(len(calls), 1)
            self.assertFalse(calls[0][1]["allow_redirects"])
            self.assertTrue(response.closed)

    def test_transport_failure_is_unknown_and_never_retried(self):
        calls = []
        def transport(*args, **kwargs):
            calls.append(1)
            raise requests_exception()
        def requests_exception():
            return RuntimeError("raw request including " + FAKE_KEY)
        error = self.assert_code("transport_outcome_unknown", lambda: model.call_once(FAKE_KEY, INPUT, transport=transport))
        self.assertEqual(calls, [1])
        self.assertNotIn(FAKE_KEY, str(error))
        self.assertIsNone(error.failure_receipt)
        self.assertIsNone(error.response_id)
        self.assertIsNone(error.usage)

    def test_response_byte_cap_applied_while_streaming(self):
        response = FakeResponse(raw=b"x" * (model.MAX_RESPONSE_BYTES + 1))
        self.assert_code("response_byte_limit", lambda: model.call_once(FAKE_KEY, INPUT, transport=lambda *a, **k: response))
        self.assertTrue(response.closed)

    def test_production_session_disables_env_proxy_and_retries(self):
        with patch.object(model.requests, "Session") as ctor:
            session = ctor.return_value
            session.post.return_value = FakeResponse()
            result = model.call_once(FAKE_KEY, INPUT)
            self.assertFalse(session.trust_env)
            self.assertEqual(session.mount.call_args.args[1].max_retries.total, 0)
            self.assertEqual(session.post.call_count, 1)
            args, kwargs = session.post.call_args
            self.assertEqual(args[0], "https://api.openai.com/v1/responses")
            self.assertEqual(kwargs["timeout"], (10, 360))
            self.assertEqual(kwargs["headers"]["Authorization"], "Bearer " + FAKE_KEY)
            self.assertLessEqual(len(kwargs["data"]), model.MAX_REQUEST_BYTES)
            session.close.assert_called_once()
            self.assertFalse(result["action_eligible"])

    def test_invalid_credential_never_reaches_transport(self):
        for key in (None, "", "short", FAKE_KEY + "\nInjected: bad"):
            with patch.object(model.requests, "Session") as ctor:
                self.assert_code("credential_missing_or_invalid", lambda: model.call_once(key, INPUT))
                ctor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
