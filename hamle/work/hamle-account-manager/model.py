"""One bounded OpenAI request; no credential loading, logging, retries or dispatch.

The caller owns a durable budget reservation and ambiguous-result ledger before
calling this module. A returned draft never confers permission to send anything.
"""
from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from pathlib import Path

import requests
from model_profiles import DEFAULT_PROFILE, ProfileError, resolve_profile

MODEL = DEFAULT_PROFILE.model
ENDPOINT = "https://api.openai.com/v1/responses"
MAX_INPUT_BYTES = 16_000
MAX_REQUEST_BYTES = 32_768
MAX_OUTPUT_TOKENS = DEFAULT_PROFILE.max_output_tokens
MAX_RESPONSE_BYTES = 262_144
TIMEOUT = (10, 360)
BRANDS = ("WAT Mobilite", "Rabam", "ya da", "Multicosmetics", "Altınmarka",
          "Bikaldi", "HAN Spaces", "Gamechangers")
PROMPT_PATH = Path(__file__).with_name("account_manager_prompt.txt")


def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _string(maximum=1200):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _strings(maximum=16):
    return {"type": "array", "items": _string(), "maxItems": maximum}


def _nullable_string(maximum=1200):
    return {"type": ["string", "null"], "minLength": 1, "maxLength": maximum}


OUTPUT_SCHEMA = _object({
    "request_summary": _string(1000),
    "intent": {"type": "string", "enum": ["routine_request", "commercial",
                 "complaint_strategy", "social_planning", "unclear"]},
    "known_facts": _strings(),
    "missing_questions": _strings(3),
    "brief": _object({"deliverables": _strings(12),
                      "audience": _nullable_string(),
                      "requested_deadline": _nullable_string(80),
                      "confirmed_deadline": {"type": "null"}}),
    "reply_draft": _object({"subject": _string(200), "body": _string(4000)}),
    "task_draft": _object({"title": _string(200), "description": _string(4000),
                           "assignee": {"type": "null"}, "due_at": {"type": "null"}}),
    "escalation": _object({
        "owner": {"type": "string", "enum": ["none", "ahmet", "cansu_and_ahmet",
                    "ozge_and_hamle", "human_review"]},
        "reason": _nullable_string()})
})
INPUT_SCHEMA = _object({
    "brand": {"type": "string", "enum": list(BRANDS)},
    "subject": _string(300), "request_text": _string(12000),
    "requested_deadline": _nullable_string(80),
    "audience": _nullable_string(), "confirmed_facts": _strings()
})


class ModelError(Exception):
    """Fixed error code plus safe provider metadata, never a usable draft.

    A failure receipt documents the parsed API response, not an approved result
    or final bill. Transport failures have no response receipt. The caller must
    persist the receipt against its existing attempt, without retrying it.
    """
    def __init__(self, code, *, usage=None, response=None, profile=None):
        super().__init__(code)
        self.code = code
        self.failure_receipt = _failure_receipt(response, profile=profile)
        self.usage = (self.failure_receipt["usage"] if self.failure_receipt is not None
                      else _usage({"usage": usage}))
        self.response_id = self.failure_receipt["response_id"] if self.failure_receipt is not None else None
        self.status = self.failure_receipt["status"] if self.failure_receipt is not None else None
        self.incomplete_reason = self.failure_receipt["incomplete_reason"] if self.failure_receipt is not None else None
        self.retry_allowed = False


def _selected_profile(profile=None, *, for_live=False):
    try:
        return resolve_profile(profile, for_live=for_live)
    except ProfileError as exc:
        raise ModelError(exc.code) from None


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ModelError("invalid_json_value") from None


def _validate(value, schema, code):
    kind = schema["type"]
    if isinstance(kind, list):
        if value is None and "null" in kind:
            return
        kind = "string"
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["required"]):
            raise ModelError(code)
        for key, subschema in schema["properties"].items():
            _validate(value[key], subschema, code)
    elif kind == "array":
        if not isinstance(value, list) or len(value) > schema["maxItems"]:
            raise ModelError(code)
        for item in value:
            _validate(item, schema["items"], code)
    elif kind == "null":
        if value is not None:
            raise ModelError(code)
    elif kind == "string":
        if not isinstance(value, str) or not value.strip():
            raise ModelError(code)
        if len(value) > schema.get("maxLength", 1200):
            raise ModelError(code)
        if "enum" in schema and value not in schema["enum"]:
            raise ModelError(code)
    else:
        raise ModelError("internal_schema_error")


def build_request(request_input, *, profile=None):
    selected = _selected_profile(profile)
    _validate(request_input, INPUT_SCHEMA, "invalid_input")
    source = _encoded(request_input)
    if len(source) > MAX_INPUT_BYTES:
        raise ModelError("input_byte_limit")
    deadline = request_input["requested_deadline"]
    if deadline is not None:
        try:
            if datetime.fromisoformat(deadline).tzinfo is None:
                raise ValueError()
        except ValueError:
            raise ModelError("invalid_input_deadline") from None
    try:
        instructions = PROMPT_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ModelError("prompt_unavailable") from None
    payload = {
        "model": selected.model, "reasoning": {"effort": selected.reasoning_effort},
        "instructions": instructions,
        "input": [{"role": "user", "content": source.decode("utf-8")}],
        "store": False, "background": False, "service_tier": "default",
        "tools": [], "tool_choice": "none", "max_output_tokens": selected.max_output_tokens,
        "truncation": "disabled",
        "text": {"format": {"type": "json_schema", "name": "hamle_account_manager",
                             "strict": True, "schema": copy.deepcopy(OUTPUT_SCHEMA)}}
    }
    if len(_encoded(payload)) > MAX_REQUEST_BYTES:
        raise ModelError("request_byte_limit")
    return payload


def _usage(response):
    raw = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(raw, dict):
        return None
    fields = ("input_tokens", "output_tokens", "total_tokens")
    if any(type(raw.get(k)) is not int or not 0 <= raw[k] <= 2_000_000 for k in fields):
        return None
    if raw["total_tokens"] != raw["input_tokens"] + raw["output_tokens"]:
        return None
    clean = {k: raw[k] for k in fields}
    for details, field in (("input_tokens_details", "cached_tokens"),
                           ("output_tokens_details", "reasoning_tokens")):
        value = raw.get(details)
        if value is not None:
            parent = "input_tokens" if details.startswith("input") else "output_tokens"
            if (not isinstance(value, dict) or type(value.get(field)) is not int
                    or not 0 <= value[field] <= raw[parent]):
                return None
            clean[details] = {field: value[field]}
    return clean


def _failure_receipt(response, *, profile=None):
    """Allowlist non-content API metadata; do not retain the response object."""
    if not isinstance(response, dict):
        return None
    selected = resolve_profile(profile)
    response_id = response.get("id")
    if not isinstance(response_id, str) or not re.fullmatch(r"resp_[A-Za-z0-9_\-]{1,200}", response_id):
        response_id = None
    status = response.get("status")
    if status not in ("completed", "incomplete", "failed", "cancelled", "queued", "in_progress"):
        status = None
    details = response.get("incomplete_details")
    reason = details.get("reason") if status == "incomplete" and isinstance(details, dict) else None
    if reason not in ("max_output_tokens", "content_filter", "steered"):
        reason = None
    matched_model = selected.model if response.get("model") == selected.model else None
    raw_reasoning = response.get("reasoning")
    matched_effort = (selected.reasoning_effort if matched_model is not None and
                      (raw_reasoning is None or (isinstance(raw_reasoning, dict) and
                       raw_reasoning.get("effort") == selected.reasoning_effort)) else None)
    return {"response_id": response_id, "model": matched_model,
            "reasoning_effort": matched_effort,
            "status": status, "incomplete_reason": reason, "usage": _usage(response)}


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _loads(raw):
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ModelError("invalid_response_json") from None


def validate_analysis(analysis, request_input=None):
    """Provider-neutral domain boundary. A valid draft still grants no action.

    Provider adapters must call this after their own strict response parser and
    must normalize accounting separately. No provider response object is needed.
    """
    _validate(analysis, OUTPUT_SCHEMA, "invalid_analysis_schema")
    required_owner = {"commercial": "ahmet", "complaint_strategy": "cansu_and_ahmet",
                      "social_planning": "ozge_and_hamle", "unclear": "human_review",
                      "routine_request": "none"}[analysis["intent"]]
    if analysis["escalation"]["owner"] != required_owner:
        raise ModelError("escalation_mismatch")
    if required_owner != "none" and not analysis["escalation"]["reason"]:
        raise ModelError("escalation_reason_missing")
    if request_input is not None:
        _validate(request_input, INPUT_SCHEMA, "invalid_input")
        if analysis["brief"]["requested_deadline"] != request_input["requested_deadline"]:
            raise ModelError("requested_deadline_changed")
    return copy.deepcopy(analysis)


def extract_response(response, request_input=None, *, profile=None):
    """Accept only a completed, constrained draft. Never return hidden reasoning."""
    selected = _selected_profile(profile)
    usage = _usage(response)
    def fail(code):
        raise ModelError(code, response=response, profile=selected)
    if not isinstance(response, dict):
        fail("invalid_response")
    if response.get("model") != selected.model:
        fail("model_mismatch")
    raw_reasoning = response.get("reasoning")
    if raw_reasoning is not None and (not isinstance(raw_reasoning, dict) or
                                      raw_reasoning.get("effort") != selected.reasoning_effort):
        fail("reasoning_mismatch")
    if response.get("status") != "completed":
        fail("response_incomplete" if response.get("status") == "incomplete" else "response_not_completed")
    if response.get("error") is not None or response.get("incomplete_details") is not None:
        fail("response_error")
    if usage is None:
        fail("usage_missing_or_invalid")
    if usage["output_tokens"] > selected.max_output_tokens or usage["input_tokens"] > MAX_REQUEST_BYTES + 1024:
        fail("usage_limit_exceeded")
    response_id = response.get("id")
    if not isinstance(response_id, str) or not re.fullmatch(r"resp_[A-Za-z0-9_\-]{1,200}", response_id):
        fail("invalid_response_id")
    output = response.get("output")
    if not isinstance(output, list) or not output:
        fail("invalid_output")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            fail("invalid_output")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message":
            fail("unexpected_tool_or_output")
        if item.get("role") != "assistant" or item.get("status") != "completed":
            fail("invalid_output_message")
        content = item.get("content")
        if not isinstance(content, list) or not content:
            fail("invalid_output_content")
        for part in content:
            if not isinstance(part, dict):
                fail("invalid_output_content")
            if part.get("type") == "refusal":
                fail("model_refusal")
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                fail("invalid_output_content")
            texts.append(part["text"])
    if len(texts) != 1 or len(texts[0].encode("utf-8")) > MAX_RESPONSE_BYTES:
        fail("ambiguous_or_oversize_output")
    try:
        analysis = validate_analysis(_loads(texts[0]), request_input)
    except ModelError as exc:
        fail(exc.code)
    return {"status": "review_required", "action_eligible": False,
            "model": selected.model, "reasoning_effort": selected.reasoning_effort,
            "provider": selected.provider, "profile_id": selected.profile_id, "response_id": response_id,
            "analysis": analysis, "usage": usage, "automatic_retry_allowed": False}


def call_once(api_key, request_input, *, transport=None, profile=None):
    """One POST. Caller must durably mark the attempt before entry.

    transport is a test-only callable following requests.Session.post's interface.
    Errors deliberately omit exception messages, raw response and request headers.
    """
    selected = _selected_profile(profile, for_live=transport is None)
    if (not isinstance(api_key, str) or not 20 <= len(api_key) <= 512
            or not api_key.isascii() or any(c.isspace() for c in api_key)):
        raise ModelError("credential_missing_or_invalid")
    body = _encoded(build_request(request_input, profile=selected))
    session = None
    response = None
    try:
        if transport is None:
            session = requests.Session()
            session.trust_env = False
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            transport = session.post
        response = transport(ENDPOINT,
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            data=body, timeout=TIMEOUT, allow_redirects=False, stream=True)
        status = response.status_code
        if status != 200:
            codes = {401: "authentication_rejected", 403: "access_rejected",
                     429: "quota_or_rate_limit", 400: "request_rejected"}
            raise ModelError(codes.get(status, "http_error_no_retry"))
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=16_384):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ModelError("response_byte_limit")
            chunks.append(chunk)
        return extract_response(_loads(b"".join(chunks)), request_input, profile=selected)
    except ModelError:
        raise
    except Exception:
        raise ModelError("transport_outcome_unknown") from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
