"""Trend Radarı provider: OpenAI Responses API + web_search tool (OFF by default).

Separate module on purpose: model.py / v11_chain_models keep tools=[] and tool_choice=none.
This module reuses model.py's profile (gpt-6-astra), endpoint, encoder, JSON loader,
usage parser and no-retry transport pattern, and the caller's existing OpenAI key loader.

Runs only when daily-digest-config.json has "trend_search_enabled": true. One call per
Istanbul day through v11_chain_models.ModelGate (its own chain-model-* reservation, so it
counts against the 30 USD chain cap; cached result reused, a started call never retried).

Hard rule: an item survives only with an https URL that ALSO appears in the response's
web_search evidence (url_citation annotations or web_search_call sources) and an ISO date
within the last 30 days (not in the future). Anything else is dropped, never repaired.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

import model as base
from model import ModelError
from v11_chain_models import ChainModelError, _arr, _obj, _str, source_digest, validate

PROMPT = Path(__file__).with_name("chain_trend_prompt.txt")
PLATFORMS = ("instagram", "tiktok", "x")
MAX_TRENDS = 5
MAX_RESPONSE_BYTES = 1_048_576  # web_search sources make responses larger than review outputs
MAX_INPUT_TOKENS = 400_000      # fetched page content is billed as input tokens


class TrendSearchError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise TrendSearchError(code)


def schema(brands):
    return _obj({"trends": _arr(_obj({
        "trend": _str(160), "platform": {"type": "string", "enum": list(PLATFORMS)},
        "url": _str(300), "date": _str(10), "brand_idea": _str(240),
        "brand": {"type": "string", "enum": list(brands)}}), MAX_TRENDS)})


def eligible_brands(cards):
    rows = [b for b in (cards or {}).get("brands", []) if isinstance(b, dict) and b.get("include_in_suggestions")
            and b.get("sector") and isinstance(b.get("brand"), str)]
    return [{"brand": b["brand"][:60], "sector": str(b["sector"])[:80],
             "audience": (str(b["audience"])[:120] if b.get("audience") else None),
             "main_platform": (str(b["main_platform"])[:30] if b.get("main_platform") else None)} for b in rows][:12]


def build_request(request_input, brands, *, profile=None):
    selected = base._selected_profile(profile)
    _need(brands, "trend_no_brand_cards")
    source = base._encoded(request_input)
    _need(len(source) <= base.MAX_INPUT_BYTES, "trend_input_byte_limit")
    try:
        instructions = PROMPT.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise TrendSearchError("trend_prompt_unavailable") from None
    payload = {"model": selected.model, "reasoning": {"effort": selected.reasoning_effort},
               "instructions": instructions, "input": [{"role": "user", "content": source.decode("utf-8")}],
               "store": False, "background": False, "service_tier": "default",
               "tools": [{"type": "web_search"}], "tool_choice": "auto",
               "include": ["web_search_call.action.sources"],
               "max_output_tokens": selected.max_output_tokens, "truncation": "disabled",
               "text": {"format": {"type": "json_schema", "name": "hamle_trend_radar", "strict": True,
                                   "schema": copy.deepcopy(schema(brands))}}}
    _need(len(base._encoded(payload)) <= base.MAX_REQUEST_BYTES, "trend_request_byte_limit")
    return payload


def normalize_url(url):
    """https only; drop fragment and utm_* tracking params; lower-case host; no trailing slash."""
    if not isinstance(url, str) or len(url) > 600 or any(c in url for c in " <>\"'\\") or any(ord(c) < 33 for c in url):
        return None
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme.lower() != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_")])
    return urlunsplit(("https", p.hostname.lower(), p.path.rstrip("/") or "", query, ""))


def _evidence(output):
    urls = set()
    for item in output:
        if item.get("type") == "web_search_call":
            for src in ((item.get("action") or {}).get("sources") or []):
                if isinstance(src, dict):
                    urls.add(normalize_url(src.get("url")))
        elif item.get("type") == "message":
            for part in item.get("content") or []:
                for ann in (part.get("annotations") or []) if isinstance(part, dict) else []:
                    if isinstance(ann, dict) and ann.get("type") == "url_citation":
                        urls.add(normalize_url(ann.get("url")))
    urls.discard(None)
    return urls


def extract(response, brands, *, profile=None):
    """model.py response rules, plus: web_search_call items allowed, evidence URLs collected."""
    selected = base._selected_profile(profile)
    def fail(code):
        raise ModelError(code, response=response, profile=selected)
    if not isinstance(response, dict):
        fail("invalid_response")
    if response.get("model") != selected.model:
        fail("model_mismatch")
    effort = response.get("reasoning")
    if effort is not None and (not isinstance(effort, dict) or effort.get("effort") != selected.reasoning_effort):
        fail("reasoning_mismatch")
    if response.get("status") != "completed":
        fail("response_incomplete" if response.get("status") == "incomplete" else "response_not_completed")
    if response.get("error") is not None or response.get("incomplete_details") is not None:
        fail("response_error")
    usage = base._usage(response)
    if usage is None:
        fail("usage_missing_or_invalid")
    if usage["output_tokens"] > selected.max_output_tokens or usage["input_tokens"] > MAX_INPUT_TOKENS:
        fail("usage_limit_exceeded")
    rid = response.get("id")
    if not isinstance(rid, str) or not re.fullmatch(r"resp_[A-Za-z0-9_\-]{1,200}", rid):
        fail("invalid_response_id")
    output = response.get("output")
    if not isinstance(output, list) or not output or len(output) > 64:
        fail("invalid_output")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            fail("invalid_output")
        kind = item.get("type")
        if kind in ("reasoning", "web_search_call"):
            continue
        if kind != "message":
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
    if len(texts) != 1 or len(texts[0].encode("utf-8")) > base.MAX_RESPONSE_BYTES:
        fail("ambiguous_or_oversize_output")
    try:
        value = base._loads(texts[0])
        validate(value, schema(brands))
    except (ModelError, ChainModelError, KeyError, TypeError):
        fail("invalid_trend_schema")
    return {"output": {"trends": value["trends"], "evidence": sorted(_evidence(output))},
            "receipt": {"response_id": rid, "model": selected.model,
                        "reasoning_effort": selected.reasoning_effort, "usage": usage}}


def call_once(api_key, request_input, brands, *, transport=None, profile=None):
    """One POST, no retries, no redirects, proxies off (same contract as model.call_once)."""
    selected = base._selected_profile(profile, for_live=transport is None)
    if (not isinstance(api_key, str) or not 20 <= len(api_key) <= 512
            or not api_key.isascii() or any(c.isspace() for c in api_key)):
        raise ModelError("credential_missing_or_invalid")
    body = base._encoded(build_request(request_input, brands, profile=selected))
    session = response = None
    try:
        if transport is None:
            session = requests.Session()
            session.trust_env = False
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            transport = session.post
        response = transport(base.ENDPOINT, headers={"Authorization": "Bearer " + api_key,
                                                     "Content-Type": "application/json"},
                             data=body, timeout=base.TIMEOUT, allow_redirects=False, stream=True)
        if response.status_code != 200:
            raise ModelError({401: "authentication_rejected", 403: "access_rejected", 429: "quota_or_rate_limit",
                              400: "request_rejected"}.get(response.status_code, "http_error_no_retry"))
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=16_384):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ModelError("response_byte_limit")
            chunks.append(chunk)
        return extract(base._loads(b"".join(chunks)), brands, profile=selected)
    except ModelError:
        raise
    except Exception:
        raise ModelError("transport_outcome_unknown") from None
    finally:
        for closable in (response, session):
            if closable is not None:
                try:
                    closable.close()
                except Exception:
                    pass


def sourced(output, today):
    """Drop every item whose URL is not https + in the web_search evidence, or whose date is bad."""
    evidence = set(output.get("evidence") or [])
    out, seen = [], set()
    for t in output.get("trends") or []:
        url = normalize_url(t.get("url"))
        try:
            day = date.fromisoformat(t.get("date")) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(t.get("date"))) else None
        except ValueError:
            day = None
        if url is None or url not in evidence or url in seen or day is None \
                or not today - timedelta(days=30) <= day <= today:
            continue
        seen.add(url)
        out.append({"title": f"{t['trend']} ({ {'instagram': 'Instagram', 'tiktok': 'TikTok', 'x': 'X'}[t['platform']] })",
                    "url": url, "published_at": day.isoformat(), "summary": "",
                    "idea": f"{t['brand']}: {t['brand_idea']}"})
    return out[:MAX_TRENDS]


def trend_search_source(ops, load_key, catalog, cards, *, transport=None):
    """trend_source(day) adapter for v11_daily_digest; budget-held, once per day, cap-checked."""
    from v11_chain_live import budget_gate
    from v11_chain_models import MODEL_RESERVATION, ModelGate, internal_cap_microdollars
    cap = internal_cap_microdollars(catalog)
    brands = eligible_brands(cards)
    names = [b["brand"] for b in brands]
    budget_ok = budget_gate(ops, MODEL_RESERVATION, cap)

    def source(day):
        _need(names, "trend_no_brand_cards")
        _need(budget_ok(), "model_budget_unavailable")
        request_input = {"today": day.isoformat(), "country": "TR", "platforms": list(PLATFORMS), "brands": brands}
        gate = ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)
        output = gate.once("trend", source_digest("trend", request_input),
                           lambda: call_once(load_key(), request_input, names, transport=transport))
        return sourced(output, day)
    return source
