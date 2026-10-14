"""Web-search research: Trend Radarı (OFF by default), Monday special days and New Business
target brands (both OFF by default via model-routing.json flags). Data only: nothing here
posts to Basecamp or mail.

Provider per role from model-routing.json: trend_radar / monday_special_days /
new_business_research default to Claude Opus 5.5 with the web_search server tool
(model.py Anthropic transport, max_uses from config, 0.01 USD per search added to the
hold). Structured outputs cannot be combined with web-search citations, so on Claude the
model returns strict JSON in text, parsed and schema-checked here. pause_turn and
web_search error result blocks are failures (no retry). The OpenAI Responses path below
stays selectable for trend_radar only. Separate module on purpose: the chain/digest
roles never get tools.

Runs only when daily-digest-config.json has "trend_search_enabled": true. One call per
Istanbul day through v11_chain_models.ModelGate (its own chain-model-* reservation, so it
counts against the 30 USD chain cap; cached result reused, a started call never retried).

Hard rule: an item survives only with an https URL that ALSO appears in the response's
web_search evidence (url_citation annotations or web_search_call sources) and an ISO date
within the last 30 days (not in the future). Anything else is dropped, never repaired.
"""
from __future__ import annotations

import copy
import hashlib
from datetime import date, timedelta
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

import model as base
from model import ModelError
from v11_chain_models import ChainModelError, _arr, _obj, _str, key_for, reservation_for, source_digest, validate

PROMPT = Path(__file__).with_name("chain_trend_prompt.txt")
SPECIAL_PROMPT = Path(__file__).with_name("chain_special_days_prompt.txt")
NEW_BUSINESS_PROMPT = Path(__file__).with_name("chain_new_business_prompt.txt")
SPARK_PROMPT = Path(__file__).with_name("chain_daily_spark_prompt.txt")
MAX_SPARK_IDEAS, MAX_SPARK_OPPORTUNITIES, MAX_SPARK_INSPIRATIONS = 3, 2, 1
MAX_SPECIAL_DAYS = 5
SPECIAL_HORIZON_DAYS = 14
MIN_NEW_BUSINESS, MAX_NEW_BUSINESS = 3, 5
JSON_IN_TEXT = ("\n\nÇIKTI BİÇİMİ: Yanıtının tamamı, aşağıdaki JSON şemasına uyan TEK bir JSON nesnesi olsun. "
                "Kod bloğu, açıklama veya başka metin yazma. Şema:\n")
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


def strict_json_text(text):
    """The whole text must be one JSON object (an optional single ```json fence is tolerated)."""
    t = text.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", t, flags=re.S)
    if fence:
        t = fence.group(1)
    if not (t.startswith("{") and t.endswith("}")):
        raise TrendSearchError("research_output_not_json")
    return base._loads(t)


def anthropic_research(route, prompt_path, request_input, out_schema, *, api_key, client=None, code="invalid_trend_schema",
                       reader=None):
    """One Claude call with the web_search server tool; JSON returned in text (citations on).

    Returns {'output': {...validated value..., 'evidence': [normalized https URLs]}, 'receipt'}.
    reader(text) -> value replaces the strict whole-text JSON + schema check for a role that sorts out
    the model's answer itself (the monthly karne: one bad finding must not empty the whole report).
    """
    _need(route.provider == "anthropic" and route.web_search, "research_route_invalid")
    source = base._encoded(request_input)
    _need(len(source) <= base.MAX_INPUT_BYTES, "trend_input_byte_limit")
    try:
        system = prompt_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise TrendSearchError("trend_prompt_unavailable") from None
    system += JSON_IN_TEXT + base._encoded(out_schema).decode("utf-8")
    kwargs = base.anthropic_request(route, system, source.decode("utf-8"))
    data = base.anthropic_send(api_key, route, kwargs, client=client)
    result = base.anthropic_extract(route, data, structured=False)
    try:
        if reader is None:
            value = strict_json_text(result["text"])
            validate(value, out_schema)
        else:
            value = reader(result["text"])
    except Exception:
        raise base._anthropic_error(code, data, route) from None
    evidence = {normalize_url(u) for u in result["evidence"]}
    evidence.discard(None)
    return {"output": {**value, "evidence": sorted(evidence)}, "receipt": result["receipt"]}


def route_fingerprint(route, prompt_path):
    """Research cache keys bind the exact route + prompt: a changed config/prompt is a new request.

    An old 'unknown' outcome under a previous config stays held (cost pending) and is never
    retried; only a genuinely different request under the new config can run.
    """
    try:
        prompt = prompt_path.read_bytes()
    except OSError:
        prompt = b""
    return {"model": route.model, "effort": route.effort, "max_tokens": route.max_tokens,
            "timeout_seconds": route.timeout_seconds, "web_search": route.web_search,
            "prompt_sha256": hashlib.sha256(prompt).hexdigest()}


def _research_gate(ops, catalog, route):
    from v11_chain_live import budget_gate
    from v11_chain_models import ModelGate, internal_cap_microdollars
    cap = internal_cap_microdollars(catalog)
    hold = reservation_for(route)  # 2 USD token hold + max_uses x 0.01 USD search fees
    return cap, hold, budget_gate(ops, hold, cap), lambda: ModelGate(
        ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)


def _routing(routing):
    import model_routing
    return routing if routing is not None else model_routing.load()


def _iso_day(value):
    try:
        return date.fromisoformat(value) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)) else None
    except ValueError:
        return None


def _fit(text, limit):
    """Cut at a word boundary with an ellipsis (the digest caps titles at 120, ideas at 200)."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return (cut or text[:limit - 1]) + "…"


def sourced(output, today):
    """Drop every item whose URL is not https + in the web_search evidence, or whose date is bad."""
    evidence = set(output.get("evidence") or [])
    out, seen = [], set()
    for t in output.get("trends") or []:
        url = normalize_url(t.get("url"))
        day = _iso_day(t.get("date"))
        if url is None or url not in evidence or url in seen or day is None \
                or not today - timedelta(days=30) <= day <= today:
            continue
        seen.add(url)
        platform = " (" + {"instagram": "Instagram", "tiktok": "TikTok", "x": "X"}[t["platform"]] + ")"
        out.append({"title": _fit(t["trend"], 120 - len(platform)) + platform,
                    "url": url, "published_at": day.isoformat(), "summary": "",
                    "idea": _fit(f"{t['brand']}: {t['brand_idea']}", 200)})
    return out[:MAX_TRENDS]


def trend_search_source(ops, load_key, catalog, cards, *, transport=None, routing=None, client=None):
    """trend_source(day) adapter for v11_daily_digest; budget-held, once per day, cap-checked.

    load_key: {provider: loader} (live) or one loader (tests). Role trend_radar decides the provider.
    """
    route = _routing(routing).route("trend_radar")
    _, hold, budget_ok, gate = _research_gate(ops, catalog, route)
    brands = eligible_brands(cards)
    names = [b["brand"] for b in brands]

    def call(request_input):
        if route.provider == "openai":
            return call_once(key_for(load_key, "openai"), request_input, names, transport=transport,
                             profile=route.profile_id)
        return anthropic_research(route, PROMPT, request_input, schema(names),
                                  api_key=key_for(load_key, "anthropic"), client=client)

    def source(day):
        _need(names, "trend_no_brand_cards")
        _need(budget_ok(), "model_budget_unavailable")
        request_input = {"today": day.isoformat(), "country": "TR", "platforms": list(PLATFORMS), "brands": brands}
        output = gate().once("trend", source_digest("trend", request_input, route_fingerprint(route, PROMPT)), lambda: call(request_input),
                             provider=route.provider, hold=hold)
        return sourced(output, day)
    return source


# ---------------------------------------------------------------- Monday special days (data only)
def special_days_schema(brands):
    return _obj({"days": _arr(_obj({
        "date": _str(10), "name": _str(120), "url": _str(300),
        "brand": {"type": "string", "enum": list(brands)}, "idea": _str(240)}), MAX_SPECIAL_DAYS)})


def sourced_special_days(output, today, horizon=SPECIAL_HORIZON_DAYS):
    """Keep only days dated today..today+horizon whose https URL is in the web search evidence."""
    evidence = set(output.get("evidence") or [])
    out, seen = [], set()
    for d in output.get("days") or []:
        url = normalize_url(d.get("url"))
        day = _iso_day(d.get("date"))
        if url is None or url not in evidence or day is None or not today <= day <= today + timedelta(days=horizon):
            continue
        if (day, d["name"]) in seen:
            continue
        seen.add((day, d["name"]))
        out.append({"date": day.isoformat(), "name": d["name"], "brand": d["brand"], "idea": d["idea"], "url": url})
    return sorted(out, key=lambda r: r["date"])[:MAX_SPECIAL_DAYS]


def special_days_research_source(ops, load_key, catalog, cards, *, routing=None, client=None):
    """research(day) -> sourced special days for the next 14 days; None when the flag is off.

    Runs only on Mondays (Istanbul day given by the caller). Data only; nothing is posted.
    """
    routing = _routing(routing)
    if not routing.flag("monday_special_days_research_enabled"):
        return None
    route = routing.route("monday_special_days")
    _, hold, budget_ok, gate = _research_gate(ops, catalog, route)
    brands = eligible_brands(cards)
    names = [b["brand"] for b in brands]

    def research(day):
        _need(day.weekday() == 0, "special_days_monday_only")
        _need(names, "trend_no_brand_cards")
        _need(budget_ok(), "model_budget_unavailable")
        request_input = {"today": day.isoformat(), "until": (day + timedelta(days=SPECIAL_HORIZON_DAYS)).isoformat(),
                         "country": "TR", "brands": brands}
        output = gate().once("special_days", source_digest("special_days", request_input, route_fingerprint(route, SPECIAL_PROMPT)),
                             lambda: anthropic_research(route, SPECIAL_PROMPT, request_input, special_days_schema(names),
                                                        api_key=key_for(load_key, "anthropic"), client=client,
                                                        code="invalid_special_days_schema"),
                             provider=route.provider, hold=hold)
        return sourced_special_days(output, day)
    return research


# ---------------------------------------------------------------- New Business target brands (data only)
def new_business_schema():
    contact = _obj({"email": _str(120), "basis": {"type": "string", "enum": ["kaynakta", "kalıp"]}, "url": _str(300)})
    return _obj({"brands": _arr(_obj({"brand": _str(80), "sector": _str(80), "reason": _str(300),
                                      "url": _str(300), "contacts": _arr(contact, MAX_NB_CONTACTS)}),
                                MAX_NEW_BUSINESS)})


# Ahmet 2026-10-13: up to 2 likely contact addresses per NB brand, always "tahmini, doğrulanmamış".
# Only company addresses: one published on a page actually opened ("kaynakta"), or a common role
# address on the brand's own domain ("kalıp"). Never a guessed personal address; no free-mail domains.
MAX_NB_CONTACTS = 2
NB_EMAIL = re.compile(r"[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
FREE_MAIL = frozenset(("gmail.com", "googlemail.com", "hotmail.com", "hotmail.com.tr", "outlook.com", "outlook.com.tr",
                       "live.com", "msn.com", "yahoo.com", "yahoo.com.tr", "icloud.com", "me.com", "yandex.com",
                       "yandex.com.tr", "mail.ru", "protonmail.com", "proton.me", "aol.com", "gmx.com", "mynet.com"))
ROLE_LOCALS = frozenset(("info", "bilgi", "iletisim", "contact", "hello", "merhaba", "pazarlama", "marketing", "press",
                         "basin", "media", "medya", "pr", "kurumsal", "corporate", "communications", "kurumsaliletisim",
                         "partnership", "partnerships", "isbirligi", "sponsorluk", "reklam", "musteri", "destek"))


def sourced_contacts(raw, evidence):
    out = []
    for c in raw or []:
        email = str((c or {}).get("email") or "").strip().lower()
        if not NB_EMAIL.fullmatch(email) or email.rsplit("@", 1)[1] in FREE_MAIL or email in {x["email"] for x in out}:
            continue
        url = normalize_url(c.get("url"))
        if c.get("basis") == "kaynakta" and url is not None and url in evidence:
            out.append({"email": email, "basis": "kaynakta", "url": url})
        elif c.get("basis") == "kalıp" and email.split("@")[0] in ROLE_LOCALS:
            out.append({"email": email, "basis": "kalıp", "url": url})
    return out[:MAX_NB_CONTACTS]


def _brand_key(name):
    return re.sub(r"[^0-9a-zçğıöşü]+", "", str(name).casefold())


def sourced_new_business(output, current_brands):
    """Keep 3-5 non-client brands whose https source URL is in the web search evidence."""
    evidence = set(output.get("evidence") or [])
    existing = {_brand_key(b) for b in current_brands}
    out, seen = [], set()
    for b in output.get("brands") or []:
        url, key = normalize_url(b.get("url")), _brand_key(b.get("brand"))
        if url is None or url not in evidence or not key or key in existing or key in seen:
            continue
        seen.add(key)
        out.append({"brand": b["brand"], "sector": b["sector"], "reason": b["reason"], "url": url,
                    "contacts": sourced_contacts(b.get("contacts"), evidence)})
    out = out[:MAX_NEW_BUSINESS]
    return {"status": "ok" if len(out) >= MIN_NEW_BUSINESS else "insufficient_sourced_brands", "brands": out}


def new_business_research_source(ops, load_key, catalog, cards, *, routing=None, client=None, avoid=()):
    """research(day) -> {'status', 'brands': [...]}; None when the flag is off. Data only.
    avoid: brands already tracked in New Biz or passed by the team (v11_nb_tracking)."""
    routing = _routing(routing)
    if not routing.flag("new_business_research_enabled"):
        return None
    route = routing.route("new_business_research")
    _, hold, budget_ok, gate = _research_gate(ops, catalog, route)
    current = [b["brand"][:60] for b in (cards or {}).get("brands", []) if isinstance(b, dict)
               and isinstance(b.get("brand"), str)][:40]
    sectors = sorted({str(b["sector"])[:80] for b in (cards or {}).get("brands", []) if isinstance(b, dict)
                      and b.get("sector")})[:20]

    def research(day):
        _need(budget_ok(), "model_budget_unavailable")
        request_input = {"today": day.isoformat(), "country": "TR", "agency": "Gamechangers",
                         "current_clients": current, "served_sectors": sectors,
                         "wanted": str(MAX_NEW_BUSINESS)}
        if avoid:
            request_input["avoid"] = [str(a)[:60] for a in avoid][:40]
        output = gate().once("new_business", source_digest("new_business", request_input, route_fingerprint(route, NEW_BUSINESS_PROMPT)),
                             lambda: anthropic_research(route, NEW_BUSINESS_PROMPT, request_input, new_business_schema(),
                                                        api_key=key_for(load_key, "anthropic"), client=client,
                                                        code="invalid_new_business_schema"),
                             provider=route.provider, hold=hold)
        return sourced_new_business(output, current + list(request_input.get("avoid") or []))
    return research


# ---------------------------------------------------------------- daily "Günün özü" research (data only)
def spark_schema(brands):
    """Schema allows a little more than the prompt asks; sourced_spark keeps the first 1/3/1."""
    return _obj({
        "opportunities": _arr(_obj({"kind": {"type": "string", "enum": ["client", "new_business"]},
                                    "brand": _str(80), "headline": _str(200), "proposal": _str(240),
                                    "url": _str(300), "date": _str(10)}), MAX_SPARK_OPPORTUNITIES + 1),
        "ideas": _arr(_obj({"brand": {"type": "string", "enum": list(brands)}, "hook": _str(120),
                            "format": _str(60), "idea": _str(240), "url": _str(300), "date": _str(10)}),
                      MAX_SPARK_IDEAS + 2),
        "inspirations": _arr(_obj({"title": _str(160), "company": _str(80), "why": _str(240),
                                   "url": _str(300), "date": _str(10)}), MAX_SPARK_INSPIRATIONS + 1)})


def sourced_spark(output, today, brands, current_brands):
    """Every item needs an https URL from the web_search evidence (a page, never a bare homepage)
    and a date in its window:
    opportunities/inspirations today-30..today, ideas today-30..today+14 (special days ahead).
    'client' opportunities must name one of our brands, 'new_business' ones must not."""
    evidence = set(output.get("evidence") or [])
    ours = {_brand_key(b): b for b in brands}
    existing = {_brand_key(b) for b in current_brands} | set(ours)

    def match(key, keys):  # "WAT" names "WAT Mobilite"; very short keys never match loosely
        return next((k for k in keys if k == key or (len(key) >= 3 and (k.startswith(key) or key.startswith(k)))),
                    None)

    def ok(row, ahead=0):
        url, day = normalize_url(row.get("url")), _iso_day(row.get("date"))
        if url is None or url not in evidence or day is None or urlsplit(url).path in ("", "/") \
                or not today - timedelta(days=30) <= day <= today + timedelta(days=ahead):
            return None
        return url, day.isoformat()

    opportunities, ideas, inspirations, seen = [], [], [], set()
    for o in output.get("opportunities") or []:
        got, key = ok(o), _brand_key(o.get("brand"))
        mine = match(key, ours) if key else None
        if got is None or not key or (o["kind"] == "client" and mine is None) \
                or (o["kind"] == "new_business" and match(key, existing) is not None):
            continue
        opportunities.append({"kind": o["kind"], "brand": ours[mine] if mine else _fit(o["brand"], 80),
                              "headline": _fit(o["headline"], 200), "proposal": _fit(o["proposal"], 240),
                              "url": got[0], "date": got[1]})
    for i in output.get("ideas") or []:
        got = ok(i, ahead=SPECIAL_HORIZON_DAYS)
        if got is None or i["brand"] in seen:
            continue
        seen.add(i["brand"])
        ideas.append({"brand": i["brand"], "hook": _fit(i["hook"], 120), "format": _fit(i["format"], 60),
                      "idea": _fit(i["idea"], 240), "url": got[0], "date": got[1]})
    for i in output.get("inspirations") or []:
        got = ok(i)
        if got is not None:
            inspirations.append({"title": _fit(i["title"], 160), "company": _fit(i["company"], 80),
                                 "why": _fit(i["why"], 240), "url": got[0], "date": got[1]})
    return {"opportunities": opportunities[:MAX_SPARK_OPPORTUNITIES], "ideas": ideas[:MAX_SPARK_IDEAS],
            "inspirations": inspirations[:MAX_SPARK_INSPIRATIONS]}


def daily_spark_research_source(ops, load_key, catalog, cards, *, routing=None, client=None):
    """research(day, avoid=()) -> sourced fırsat / fikir / ilham for the digest's Günün özü box;
    None when the flag is off. Once per Istanbul day and input (preview + team share the call)."""
    routing = _routing(routing)
    if not routing.flag("daily_spark_research_enabled"):
        return None
    route = routing.route("daily_spark")
    _, hold, budget_ok, gate = _research_gate(ops, catalog, route)
    brands = eligible_brands(cards)
    names = [b["brand"] for b in brands]
    current = [b["brand"][:60] for b in (cards or {}).get("brands", []) if isinstance(b, dict)
               and isinstance(b.get("brand"), str)][:40]

    def research(day, avoid=()):
        _need(names, "trend_no_brand_cards")
        _need(budget_ok(), "model_budget_unavailable")
        request_input = {"today": day.isoformat(), "country": "TR", "agency": "Gamechangers", "brands": brands,
                         "current_clients": current, "avoid": [_fit(a, 120) for a in avoid][:6]}
        output = gate().once("daily_spark", source_digest("daily_spark", request_input,
                                                          route_fingerprint(route, SPARK_PROMPT)),
                             lambda: anthropic_research(route, SPARK_PROMPT, request_input, spark_schema(names),
                                                        api_key=key_for(load_key, "anthropic"), client=client,
                                                        code="invalid_daily_spark_schema"),
                             provider=route.provider, hold=hold)
        return sourced_spark(output, day, names, current)
    return research
