"""One bounded OpenAI request; no credential loading, logging, retries or dispatch.

The caller owns a durable budget reservation and ambiguous-result ledger before
calling this module. A returned draft never confers permission to send anything.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import time
from datetime import datetime
from fractions import Fraction
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


# ============================================================ Anthropic (Claude) transport
# Added beside the OpenAI adapter above (which stays unchanged as the standby path).
# One messages.create call through the official `anthropic` SDK: client max_retries=0,
# explicit output_config.effort, structured output via output_config.format, no
# `thinking` parameter (never disabled, never budget_tokens), no assistant prefill,
# no tool_choice, no server-side fallbacks. The routing decision (model/effort/limits)
# comes from model_routing.Route; nothing here chooses a provider.
ANTHROPIC_KEY_PREFIX = "sk-ant-"
ANTHROPIC_MAX_RESPONSE_BYTES = 1_048_576  # web search results make messages larger
ANTHROPIC_MAX_INPUT_TOKENS = 400_000
HAIKU_LONG_PROMPT_TOKENS = 100_000
# USD per 1M tokens == microdollars per token: (input, output, cache read, cache write 5m).
# Cache writes are priced at 1.25x input; this adapter sends no cache_control, so they stay 0.
_P = Fraction
ANTHROPIC_PRICES = {
    "claude-opus-5-5": {"standard": (_P(4), _P(20), _P(1, 5), _P(5))},
    "claude-haiku-5-5": {"standard": (_P(1, 10), _P(1, 2), _P(1, 100), _P(1, 8)),
                         "long": (_P(1, 2), _P(5, 2), _P(1, 20), _P(5, 8))},
}
WEB_SEARCH_FEE_MICRODOLLARS = 10_000  # 0.01 USD per search
# web_search_20260209 filters results with server-side code execution ("dynamic filtering"),
# so those server blocks may appear next to web_search blocks on research roles.
WEB_SEARCH_SERVER_TOOLS = ("web_search", "code_execution", "bash_code_execution", "text_editor_code_execution")
DYNAMIC_FILTER_RESULTS = ("code_execution_tool_result", "bash_code_execution_tool_result",
                          "text_editor_code_execution_tool_result")
DOMAIN_CODES = ("escalation_mismatch", "escalation_reason_missing", "requested_deadline_changed")
ANTHROPIC_STOP_FAILURES = {"refusal": "model_refusal", "max_tokens": "response_max_tokens",
                           "pause_turn": "response_pause_turn",
                           "model_context_window_exceeded": "response_incomplete"}


def anthropic_cost_microdollars(model_id, usage):
    """Actual cost from response.usage (tokens x list price + web search fees), rounded up."""
    tiers = ANTHROPIC_PRICES.get(model_id)
    if tiers is None:
        raise ModelError("model_price_unknown")
    prompt = usage["input_tokens"] + usage["cache_read_input_tokens"] + usage["cache_creation_input_tokens"]
    tier = tiers["long"] if "long" in tiers and prompt > HAIKU_LONG_PROMPT_TOKENS else tiers["standard"]
    p_in, p_out, p_read, p_write = tier
    total = (usage["input_tokens"] * p_in + usage["output_tokens"] * p_out
             + usage["cache_read_input_tokens"] * p_read + usage["cache_creation_input_tokens"] * p_write
             + usage["web_search_requests"] * WEB_SEARCH_FEE_MICRODOLLARS)
    total = Fraction(total)
    return -(-total.numerator // total.denominator)


def anthropic_schema(schema):
    """Local strict schema -> structured-output subset (no length/number/count constraints,
    nullable types as anyOf). The full local schema is still enforced after parsing."""
    out, notes = {}, []
    kind = schema.get("type")
    if isinstance(kind, list):
        rest = {k: v for k, v in schema.items() if k != "type"}
        variants = []
        for single in kind:
            variants.append({"type": "null"} if single == "null" else anthropic_schema({**rest, "type": single}))
        return {"anyOf": variants}
    out["type"] = kind
    if "enum" in schema:
        out["enum"] = [v for v in schema["enum"] if v is not None]
    for key in ("minLength", "maxLength", "minimum", "maximum", "maxItems"):
        if key in schema:
            notes.append(f"{key}: {schema[key]}")
    if kind == "object":
        out["properties"] = {k: anthropic_schema(v) for k, v in schema["properties"].items()}
        out["required"] = list(schema["required"])
        out["additionalProperties"] = False
    elif kind == "array":
        out["items"] = anthropic_schema(schema["items"])
    if notes:
        out["description"] = "{" + ", ".join(notes) + "}"
    return out


def anthropic_request(route, system, user_text, *, schema=None):
    """messages.create keyword arguments. Pure; no SDK import."""
    if getattr(route, "provider", None) != "anthropic" or route.model not in ANTHROPIC_PRICES:
        raise ModelError("model_route_invalid")
    output_config = {"effort": route.effort}
    if schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": anthropic_schema(schema)}
    kwargs = {"model": route.model, "max_tokens": route.max_tokens, "system": system,
              "messages": [{"role": "user", "content": user_text}], "output_config": output_config}
    if route.web_search:
        kwargs["tools"] = [{"type": route.web_search["type"], "name": "web_search",
                            "max_uses": route.web_search["max_uses"]}]
    if len(_encoded(kwargs)) > MAX_REQUEST_BYTES * 2:
        raise ModelError("request_byte_limit")
    return kwargs


def _anthropic_usage(data):
    raw = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, optional in (("input_tokens", False), ("output_tokens", False),
                          ("cache_read_input_tokens", True), ("cache_creation_input_tokens", True)):
        value = raw.get(key)
        if value is None and optional:
            value = 0
        if type(value) is not int or not 0 <= value <= 2_000_000:
            return None
        out[key] = value
    server = raw.get("server_tool_use")
    if server is not None and not isinstance(server, dict):
        return None
    searches = (server or {}).get("web_search_requests")
    searches = 0 if searches is None else searches
    if type(searches) is not int or not 0 <= searches <= 100:
        return None
    out["web_search_requests"] = searches
    return out


def _anthropic_receipt(data, route, *, failure_code=None):
    data = data if isinstance(data, dict) else {}
    rid = data.get("id")
    rid = rid if isinstance(rid, str) and re.fullmatch(r"msg_[A-Za-z0-9_\-]{1,200}", rid) else None
    usage = _anthropic_usage(data)
    receipt = {"provider": "anthropic", "response_id": rid,
               "model": route.model if _anthropic_model_matches(data.get("model"), route.model) else None,
               "effort": route.effort, "usage": usage,
               "cost_microdollars": anthropic_cost_microdollars(route.model, usage) if usage is not None else None}
    if failure_code is not None:
        stop = data.get("stop_reason")
        receipt["stop_reason"] = stop if stop in ("end_turn", "max_tokens", "stop_sequence", "tool_use",
                                                  "pause_turn", "refusal", "model_context_window_exceeded") else None
    return receipt


def _anthropic_model_matches(value, expected):
    return isinstance(value, str) and (value == expected or re.fullmatch(re.escape(expected) + r"-\d{8}", value))


def _anthropic_error(code, data, route):
    exc = ModelError(code)
    receipt = _anthropic_receipt(data, route, failure_code=code) if isinstance(data, dict) else None
    exc.failure_receipt = receipt
    exc.usage = receipt["usage"] if receipt else None
    exc.response_id = receipt["response_id"] if receipt else None
    exc.status = receipt["stop_reason"] if receipt else None
    exc.incomplete_reason = None
    return exc


def anthropic_extract(route, data, *, structured=True):
    """Accept only end_turn with text output. Returns {'text','evidence','receipt'}.

    evidence: https URLs from web_search_tool_result blocks and web_search_result_location
    citations (empty for roles without web search). Thinking blocks are skipped, never returned.
    """
    def fail(code):
        raise _anthropic_error(code, data, route)
    if not isinstance(data, dict) or data.get("type") != "message" or data.get("role") != "assistant":
        fail("invalid_response")
    if not _anthropic_model_matches(data.get("model"), route.model):
        fail("model_mismatch")
    rid = data.get("id")
    if not isinstance(rid, str) or not re.fullmatch(r"msg_[A-Za-z0-9_\-]{1,200}", rid):
        fail("invalid_response_id")
    stop = data.get("stop_reason")
    if stop in ANTHROPIC_STOP_FAILURES:  # refusal / max_tokens / pause_turn: held, never retried
        fail(ANTHROPIC_STOP_FAILURES[stop])
    if stop != "end_turn":
        fail("response_not_completed")
    usage = _anthropic_usage(data)
    if usage is None:
        fail("usage_missing_or_invalid")
    if (usage["output_tokens"] > route.max_tokens or usage["input_tokens"] > ANTHROPIC_MAX_INPUT_TOKENS
            or usage["web_search_requests"] > (route.web_search["max_uses"] if route.web_search else 0)):
        fail("usage_limit_exceeded")
    content = data.get("content")
    if not isinstance(content, list) or not content or len(content) > 200:
        fail("invalid_output")
    texts, evidence = [], set()
    for block in content:
        if not isinstance(block, dict):
            fail("invalid_output_content")
        kind = block.get("type")
        if kind in ("thinking", "redacted_thinking"):
            continue
        if kind == "text":
            if not isinstance(block.get("text"), str):
                fail("invalid_output_content")
            texts.append(block["text"])
            for cite in block.get("citations") or []:
                if isinstance(cite, dict) and cite.get("type") == "web_search_result_location":
                    evidence.add(cite.get("url"))
            continue
        if route.web_search and kind == "server_tool_use" and block.get("name") in WEB_SEARCH_SERVER_TOOLS:
            continue
        if route.web_search and kind in DYNAMIC_FILTER_RESULTS:  # web_search_20260209 filtering code; not evidence
            continue
        if route.web_search and kind == "web_search_tool_result":
            result = block.get("content")
            if not isinstance(result, list):  # an error object, e.g. {"error_code": "unavailable"}
                fail("web_search_tool_error")
            for item in result:
                if isinstance(item, dict) and item.get("type") == "web_search_result":
                    evidence.add(item.get("url"))
            continue
        fail("unexpected_tool_or_output")
    if not texts or (structured and len(texts) != 1):
        fail("ambiguous_or_oversize_output")
    text = "".join(texts)
    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        fail("ambiguous_or_oversize_output")
    evidence.discard(None)
    return {"text": text, "evidence": sorted(u for u in evidence if isinstance(u, str)),
            "receipt": _anthropic_receipt(data, route)}


def _anthropic_status_code(exc):
    """Fixed code from the SDK's documented APIStatusError fields; message text never kept."""
    status = getattr(exc, "status_code", None)
    if type(status) is not int:
        return "transport_outcome_unknown"  # connection error / timeout: a charge is possible
    error_type = getattr(exc, "type", None)
    if status == 402 or error_type == "billing_error":
        return "anthropic_credit_insufficient"
    if status == 400 and "credit balance" in str(getattr(exc, "message", "") or "").lower():
        return "anthropic_credit_insufficient"
    return {400: "request_rejected", 401: "authentication_rejected", 403: "access_rejected",
            404: "anthropic_model_unavailable", 413: "request_byte_limit",
            429: "quota_or_rate_limit", 529: "provider_overloaded_no_retry"}.get(status, "http_error_no_retry")


STREAM_READ_TIMEOUT = 120


class _StreamDeadline(Exception):
    """Wall-clock limit reached mid-stream; a charge is possible (transport_outcome_unknown)."""


def _anthropic_stream(client, kwargs, wall_seconds, clock=time.monotonic):
    """messages.stream -> final message; long web-search turns never sit on one idle socket."""
    deadline = clock() + wall_seconds
    with client.messages.stream(**kwargs) as stream:
        for _event in stream:
            if clock() > deadline:
                raise _StreamDeadline()
        return stream.get_final_message()


def anthropic_send(api_key, route, kwargs, *, client=None):
    """One messages.create (messages.stream on web-search routes), SDK retries disabled.
    Returns the message as a plain dict.

    client is a test-only fake exposing messages.create(**kwargs). Errors carry only
    fixed codes; the key, exception text and request are never included.
    """
    if (not isinstance(api_key, str) or not api_key.startswith(ANTHROPIC_KEY_PREFIX)
            or not 20 <= len(api_key) <= 512 or not api_key.isascii() or any(c.isspace() for c in api_key)):
        raise ModelError("credential_missing_or_invalid")
    streamed = bool(route.web_search)
    if client is None:
        try:
            import anthropic
        except ImportError:
            raise ModelError("anthropic_sdk_unavailable") from None
        # Streaming research: the timeout is per read (events keep flowing), the wall clock
        # is bounded below by route.timeout_seconds.
        client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=float(
            min(route.timeout_seconds, STREAM_READ_TIMEOUT) if streamed else route.timeout_seconds))
    try:
        if streamed:
            message = _anthropic_stream(client, kwargs, route.timeout_seconds)
        else:
            message = client.messages.create(**kwargs)
    except Exception as exc:
        code = _anthropic_status_code(exc)
        # Job log: exception class and HTTP status only (never message text, request or key).
        print(f"anthropic_send_failed code={code} class={type(exc).__module__}.{type(exc).__name__} "
              f"status={getattr(exc, 'status_code', None)} streamed={streamed}", file=sys.stderr, flush=True)
        raise ModelError(code) from None
    try:
        data = message if isinstance(message, dict) else message.to_dict()
        size = len(_encoded(data))
    except Exception:
        raise ModelError("invalid_response") from None
    if size > ANTHROPIC_MAX_RESPONSE_BYTES:
        raise ModelError("response_byte_limit")
    return data


def anthropic_json(route, system, request_input, schema, *, api_key, client=None, validator=None):
    """One structured-output call: send -> extract -> strict local parse/validation."""
    source = _encoded(request_input)
    if len(source) > MAX_INPUT_BYTES:
        raise ModelError("input_byte_limit")
    kwargs = anthropic_request(route, system, source.decode("utf-8"), schema=schema)
    data = anthropic_send(api_key, route, kwargs, client=client)
    result = anthropic_extract(route, data, structured=True)
    try:
        value = _loads(result["text"])
        if validator is not None:
            value = validator(value)
    except Exception as exc:
        code = getattr(exc, "code", None)
        raise _anthropic_error(code if code in DOMAIN_CODES else "invalid_analysis_schema", data, route) from None
    return {"output": value, "receipt": result["receipt"]}


def call_anthropic_once(api_key, request_input, route, *, client=None):
    """Account-manager review draft (role customer_text) on Claude; same result contract as call_once."""
    build_request(request_input)  # same input validation and size bounds as the OpenAI path
    try:
        instructions = PROMPT_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ModelError("prompt_unavailable") from None
    result = anthropic_json(route, instructions, request_input, OUTPUT_SCHEMA, api_key=api_key, client=client,
                            validator=lambda value: validate_analysis(value, request_input))
    receipt = result["receipt"]
    return {"status": "review_required", "action_eligible": False, "model": route.model,
            "reasoning_effort": route.effort, "provider": "anthropic", "profile_id": None,
            "response_id": receipt["response_id"], "analysis": result["output"], "usage": receipt["usage"],
            "receipt": receipt, "automatic_retry_allowed": False}
