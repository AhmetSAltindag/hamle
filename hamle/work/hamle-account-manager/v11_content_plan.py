"""Hamle Aylık İçerik Planı taslağı (Ahmet 2026-10-17, SM & MT plan step 2; pilot WAT).

Hamle prepares the INTERNAL first draft of a brand's next-month content plan; the team edits it, presents it and the
client approves it. Hamle never sends a plan to a client and never publishes anything. Ahmet's decisions
(17 Oct 11:34Z): one content idea for EVERY day of the month (the social media lead picks, drops or adds; the
contract count, e.g. WAT 18-20 posts, is marked as "önerilen"), the plan lives in Basecamp, and the strategy
reminder is derived from the brand's history until an approved strategy document is in Basecamp.

This first version builds the plan and shows it (preview); writing it to Basecamp comes in its own later change.

Material (read only): brand card (sector, audience, main platform), the brand memory of the last 90 days
(v11_brand_memory: profile + dated events from mail and Basecamp), the brand project's open to-dos due in the month
(BasecampDigestReader, GET only), the Turkish special days of the month (tr-special-days), the Platform Kural Defteri
(platform-rules, v11_algorithm_watch) and last month's plan record when there is one.

Model: own settings in content-plan-config.json (Claude Opus, no web search), through the shared budget gate and
ModelGate (one chance per exact request, never retried):
  1. skeleton: strategy reminder, last-month notes, content pillars and the calendar (one row per day: pillar,
     topic, format, platform, special day, why, recommended);
  2. details, in parts of DETAIL_DAYS days: hook, caption, hashtags, CTA, visual brief, on-screen text, size/length.
The code then checks the plan: every day present once, special days covered, no repeated topic, the recommended
count against the contract, rule book checks (no link in a LinkedIn caption, 3-5 hashtags on Instagram) and the
agency scope (no media buying / ads / sponsored content). The findings and a code score go on top of the plan.

Non-collision (Ahmet 2026-10-17): own module, mode (worker.py --mode content_plan), config (OFF), records
(content-plan-<brand>-<YYYYMM>, written only by the later Basecamp change); the preview writes nothing.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import v11_brand_memory as bm
import v11_daily_digest as dd

CONFIG_NAME = "content-plan-config.json"
PROMPT = Path(__file__).with_name("content_plan_prompt.txt")
ROLE = "content_plan"
ISTANBUL = ZoneInfo("Europe/Istanbul")
MEMORY_DAYS = 90
MAX_INPUT_BYTES = 15_000            # model.MAX_INPUT_BYTES is 16 000
DETAIL_DAYS = 8
PLATFORMS = ("instagram", "tiktok", "linkedin", "youtube", "facebook")
BOTH = "instagram+linkedin"  # the same news on both (WAT, Ahmet 17 Oct)
FORMATS = ("reels", "karusel", "tekil", "story", "video", "linkedin_belge", "linkedin_metin")
FORMAT_LABELS = {"reels": "Reels", "karusel": "Karusel", "tekil": "Tekil görsel", "story": "Story", "video": "Video",
                 "linkedin_belge": "LinkedIn belge", "linkedin_metin": "LinkedIn metin"}
DAY_NAMES = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")
MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")
STRATEGY_NOTE = "Bu strateji hatırlatması markanın son 90 günlük yazışmalarından çıkarımdır; onaylı strateji dokümanı Basecamp'e gelince ona göre güncellenir."
OUT_OF_SCOPE = re.compile(r"\b(reklam\w*|sponsorlu\w*|sponsored|ads? manager|ücretli tanıtım\w*|medya planı\w*|"
                          r"media plan\w*|boost\w*|öne çıkarılmış gönderi\w*)\b", re.I)
URL = re.compile(r"https?://|www\.", re.I)


class ContentPlanError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ContentPlanError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _line(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


# ------------------------------------------------------------------ config
def load_config(path=None):
    return dd._load(CONFIG_NAME, path)


def validate_config(cfg):
    import model_routing as mr
    _need(isinstance(cfg, dict) and cfg.get("schema_version") == 1 and type(cfg.get("enabled")) is bool,
          "content_plan_config_invalid")
    brands = cfg.get("brands")
    _need(isinstance(brands, dict) and brands, "content_plan_brands_invalid")
    out = {}
    for key, b in brands.items():
        posts = (b or {}).get("posts_per_month")
        _need(isinstance(posts, list) and len(posts) == 2 and all(type(n) is int for n in posts)
              and 1 <= posts[0] <= posts[1] <= 31, "content_plan_posts_invalid")
        platforms = b.get("platforms")
        _need(isinstance(platforms, list) and platforms and all(p in PLATFORMS for p in platforms),
              "content_plan_platforms_invalid")
        out[key] = {"posts": tuple(posts), "platforms": list(platforms)}
    m = cfg.get("model") or {}
    _need(set(m) == {"model", "effort", "max_tokens", "timeout_seconds"} and m["model"] in mr.ANTHROPIC_MODELS
          and m["effort"] in mr.EFFORTS and type(m["max_tokens"]) is int and 1024 <= m["max_tokens"] <= 16_000
          and type(m["timeout_seconds"]) is int and 30 <= m["timeout_seconds"] <= 540, "content_plan_model_invalid")
    route = mr.Route(ROLE, "anthropic", m["model"], m["effort"], m["max_tokens"], m["timeout_seconds"])
    return {"enabled": cfg["enabled"], "brands": out, "route": route}


def target_month(today, value=None):
    """'YYYY-MM' (this month up to 3 months ahead) or, by default, the next month -> its first day."""
    if value is None:
        return (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    _need(isinstance(value, str) and re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value), "content_plan_month_invalid")
    first = date(int(value[:4]), int(value[5:]), 1)
    _need(today.replace(day=1) <= first <= (today.replace(day=1) + timedelta(days=95)), "content_plan_month_out_of_range")
    return first


def month_days(first):
    return [first + timedelta(days=n) for n in range(calendar.monthrange(first.year, first.month)[1])]


def month_label(first):
    return f"{MONTHS[first.month - 1]} {first.year}"


def record_key(brand, first):
    return f"content-plan-{brand}-{first:%Y%m}"


# ------------------------------------------------------------------ material (read only)
def special_days(cal, first):
    last = month_days(first)[-1]
    return [{"date": d["date"], "name": _line(d.get("name"), 80), "tags": list(d.get("tags") or [])[:5],
             "verified": d.get("verified") is True}
            for d in (cal or {}).get("days") or []
            if isinstance(d, dict) and first.isoformat() <= str(d.get("date")) <= last.isoformat()]


def platform_rules(book, platforms):
    rules = (book or {}).get("rules") or []
    return [{"platform": r["platform"], "rule": _line(r["rule"], 200), "güven": "🟢" if r.get("source_kind") == "official"
             else "🟡"} for r in rules if isinstance(r, dict) and r.get("platform") in platforms and r.get("rule")]


def gather(ops, key, name, card, settings, first, today, *, upcoming, cal, book, previous):
    """One brand's material for the month's plan (no model, no writes)."""
    mem = bm.brand_memory_context(ops, key, today, days=MEMORY_DAYS, limit=60, detail=160)
    profile = mem.get("profile") or {}
    events = [{"date": e.get("date"), "kind": e.get("kind"), "title": _line(e.get("title"), 110),
               "detail": _line(e.get("detail"), 160)} for e in mem.get("events") or [] if isinstance(e, dict)]
    last = month_days(first)[-1]
    todos = [{"title": _line(t.get("title"), 110), "due_on": t.get("due_on")} for t in upcoming or []
             if t.get("due_on") and first.isoformat() <= t["due_on"] <= last.isoformat()][:15]
    prev = None
    if isinstance(previous, dict) and isinstance(previous.get("plan"), dict):
        p = previous["plan"]
        prev = {"pillars": [x.get("name") for x in p.get("pillars") or []][:6],
                "topics": [d.get("topic") for d in p.get("days") or []][:31],
                "revisions": list(previous.get("revisions") or [])[:10]}
    return {"brand": {"key": key, "name": name, "sector": _line((card or {}).get("sector"), 200),
                      "audience": _line((card or {}).get("audience"), 160),
                      "main_platform": (card or {}).get("main_platform"), "platforms": settings["platforms"],
                      "posts_per_month": list(settings["posts"])},
            "profile": {"preferences": list(profile.get("preferences") or [])[-15:],
                        "donts": list(profile.get("donts") or [])[-15:], "approval_flow": profile.get("approval_flow"),
                        "decisions": [_line(d.get("what"), 160) for d in profile.get("decisions") or []
                                      if isinstance(d, dict)][-10:]},
            "events": events, "upcoming_todos": todos, "special_days": special_days(cal, first),
            "rules": platform_rules(book, settings["platforms"]), "previous_plan": prev}


def _shrink(inp):
    for limit in (100, 60, 0):
        if bm._size(inp) <= MAX_INPUT_BYTES:
            return inp
        for e in inp["material"]["events"]:
            if limit:
                e["detail"] = e["detail"][:limit]
            else:
                e.pop("detail", None)
    while bm._size(inp) > MAX_INPUT_BYTES and inp["material"]["events"]:
        inp["material"]["events"].pop(0)
        inp["omitted_events"] = inp.get("omitted_events", 0) + 1
    _need(bm._size(inp) <= MAX_INPUT_BYTES, "content_plan_input_too_large")
    return inp


def skeleton_input(material, first):
    days = [{"date": d.isoformat(), "day": DAY_NAMES[d.weekday()]} for d in month_days(first)]
    return _shrink({"task": "skeleton", "month": {"name": month_label(first), "days": days},
                    "material": {k: (list(v) if isinstance(v, list) else v) for k, v in material.items()}})


def detail_input(material, skeleton, chunk):
    return _shrink({"task": "details", "brand": material["brand"], "profile": material["profile"],
                    "rules": material["rules"], "strategy": skeleton["strategy"], "pillars": skeleton["pillars"],
                    "days": chunk, "material": {"events": []}})


# ------------------------------------------------------------------ model
def _o(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _s(n):
    return {"type": "string", "minLength": 1, "maxLength": n}


def _ns(n):
    return {"type": ["string", "null"], "minLength": 1, "maxLength": n}


def _a(items, n, low=0):
    return {"type": "array", "items": items, "minItems": low, "maxItems": n}


SKELETON_SCHEMA = _o({
    "strategy": _a(_s(240), 5, 1),
    "last_month": _a(_s(240), 4),
    "pillars": _a(_o({"name": _s(60), "why": _s(200)}), 6, 2),
    "days": _a(_o({"date": _s(10), "pillar": _s(60), "topic": _s(160), "format": {"type": "string", "enum": list(FORMATS)},
                   "platform": {"type": "string", "enum": list(PLATFORMS) + [BOTH]},
                   "special_day": _ns(80), "why": _s(200), "recommended": {"type": "boolean"}}), 31, 28)})
DETAIL_SCHEMA = _o({"items": _a(_o({"date": _s(10), "hook": _s(160), "caption": _s(900), "linkedin_caption": _ns(900), "hashtags": _a(_s(40), 8),
                                    "cta": _ns(120), "visual_brief": _s(500), "on_screen_text": _ns(200),
                                    "size": _s(80)}), DETAIL_DAYS, 1)})


def model_source(ops, load_key, catalog, cfg, *, client=None):
    """call(task, request) -> output; one ModelGate chance per exact request (internal cap, budget gate)."""
    import model as base
    from v11_chain_live import budget_gate
    from v11_chain_models import ModelGate, internal_cap_microdollars, key_for, reservation_for, source_digest
    from v11_trend_search import route_fingerprint
    route = cfg["route"]
    cap = internal_cap_microdollars(catalog)
    hold = reservation_for(route)
    budget_ok = budget_gate(ops, hold, cap)

    def call(task, request):
        _need(budget_ok(), "model_budget_unavailable")
        gate = ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)
        schema = SKELETON_SCHEMA if task == "skeleton" else DETAIL_SCHEMA

        def once():
            try:
                system = PROMPT.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise ContentPlanError("content_plan_prompt_unavailable") from None
            key = key_for(load_key, "anthropic")
            try:
                return base.anthropic_json(route, system, request, schema, api_key=key, client=client)
            finally:
                del key
        return gate.once(ROLE, source_digest(ROLE, request, route_fingerprint(route, PROMPT)), once,
                         provider="anthropic", hold=hold)
    call.hold = hold
    return call


# ------------------------------------------------------------------ the plan
def build_plan(material, first, call):
    """Skeleton + details -> plan dict (days in calendar order; a day the model missed is marked, never invented)."""
    sk = call("skeleton", skeleton_input(material, first))
    days = {d.isoformat(): None for d in month_days(first)}
    for row in sk.get("days") or []:
        if row.get("date") in days and days[row["date"]] is None:
            days[row["date"]] = {k: (_line(v, 300) if isinstance(v, str) else v) for k, v in row.items()}
    rows = [dict(r, date=k) if r else {"date": k, "missing": True} for k, r in days.items()]
    present = [r for r in rows if not r.get("missing")]
    calls = 1
    for n in range(0, len(present), DETAIL_DAYS):
        chunk = [{k: r[k] for k in ("date", "pillar", "topic", "format", "platform", "special_day")}
                 for r in present[n:n + DETAIL_DAYS]]
        out = call("details", detail_input(material, sk, chunk))
        calls += 1
        by = {i.get("date"): i for i in out.get("items") or []}
        for r in present[n:n + DETAIL_DAYS]:
            i = by.get(r["date"])
            if i:
                r.update(hook=_line(i["hook"], 160), caption=str(i["caption"]).strip()[:900],
                         linkedin_caption=(str(i.get("linkedin_caption") or "").strip()[:900] or None
                                           if r.get("platform") == BOTH else None),
                         hashtags=[h if h.startswith("#") else "#" + h for h in (_line(x, 40).replace(" ", "")
                                                                                 for x in i.get("hashtags") or []) if h],
                         cta=_line(i.get("cta"), 120) or None, visual_brief=_line(i["visual_brief"], 500),
                         on_screen_text=_line(i.get("on_screen_text"), 200) or None, size=_line(i["size"], 80))
    return {"strategy": [_line(x, 240) for x in sk.get("strategy") or []],
            "last_month": [_line(x, 240) for x in sk.get("last_month") or []],
            "pillars": [{"name": _line(p["name"], 60), "why": _line(p["why"], 200)} for p in sk.get("pillars") or []],
            "days": rows, "model_calls": calls}


def check(plan, material, first):
    """Code checks -> (findings [(level, text)], score 0-100). Levels: 'kırmızı', 'sarı'."""
    out = []
    days = plan["days"]
    missing = [r["date"] for r in days if r.get("missing")]
    if missing:
        out.append(("kırmızı", f"{len(missing)} gün için öneri yok: " + ", ".join(m[8:] for m in missing[:8])))
    no_detail = [r["date"] for r in days if not r.get("missing") and not r.get("caption")]
    if no_detail:
        out.append(("sarı", f"{len(no_detail)} günün caption ve brief'i yazılamadı: "
                            + ", ".join(m[8:] for m in no_detail[:8])))
    covered = {(r.get("special_day") or "").casefold() for r in days} | {r["date"] for r in days if r.get("special_day")}
    for s in material["special_days"]:
        if s["date"] not in covered and s["name"].casefold() not in covered:
            out.append(("sarı", f"Özel gün planda yok: {s['date'][8:]}.{s['date'][5:7]} {s['name']} "
                                "(markaya uymuyorsa bilinçli olarak boş bırakılabilir)."))
    seen = {}
    for r in days:
        t = bm._fold(r.get("topic"))
        if t and t in seen:
            out.append(("sarı", f"Tekrar eden konu: {r['date'][8:]} ve {seen[t][8:]} aynı konu."))
        seen.setdefault(t, r["date"])
    lo, hi = material["brand"]["posts_per_month"]
    rec = sum(1 for r in days if r.get("recommended"))
    if not lo <= rec <= hi:
        out.append(("sarı", f"Önerilen içerik sayısı {rec}; sözleşme {lo}-{hi}. Sosyal medya sorumlusu seçerken dikkat."))
    def days_of(pred):
        return [r["date"][8:] for r in days if pred(r)]

    def listed(ds):
        return ", ".join(ds[:10]) + (" …" if len(ds) > 10 else "")
    link = days_of(lambda r: URL.search((r.get("caption") if r.get("platform") == "linkedin" else
                                         r.get("linkedin_caption") if r.get("platform") == BOTH else "") or ""))
    if link:
        out.append(("sarı", f"LinkedIn caption'ında link var ({listed(link)}); kural defterine göre erişim düşer."))
    tags = days_of(lambda r: r.get("platform") in ("instagram", BOTH) and r.get("caption")
                   and not 3 <= len(r.get("hashtags") or []) <= 5)
    if tags:
        out.append(("sarı", f"Instagram'da 3-5 niş hashtag önerilir; uymayan günler: {listed(tags)}."))
    scope = days_of(lambda r: OUT_OF_SCOPE.search(" ".join(str(r.get(k) or "") for k in ("topic", "caption",
                                                                                         "visual_brief"))))
    if scope:
        out.append(("kırmızı", f"Reklam / sponsorlu içerik geçiyor ({listed(scope)}); ajansın kapsamı dışında."))
    score = max(0, 100 - 15 * sum(1 for lv, _ in out if lv == "kırmızı") - 3 * sum(1 for lv, _ in out if lv == "sarı"))
    return out, score


# ------------------------------------------------------------------ text
def platform_label(p):
    return {"linkedin": "LinkedIn", "tiktok": "TikTok", "youtube": "YouTube", BOTH: "Instagram + LinkedIn"}.get(
        p, str(p).capitalize())


def _day(r):
    d = date.fromisoformat(r["date"])
    return f"{d.day:02d}.{d.month:02d} {DAY_NAMES[d.weekday()]}"


def render(name, first, plan, findings, score, material):
    lo, hi = material["brand"]["posts_per_month"]
    rec = sum(1 for r in plan["days"] if r.get("recommended"))
    lines = [f"Hamle AI: {name} · {month_label(first)} İçerik Planı (iç taslak)",
             f"Her gün için bir öneri ({len(plan['days'])} gün); ⭐ {rec} öneri sözleşmedeki {lo}-{hi} paylaşım için "
             "önerilen seçim. Seçim, eleme ve ekleme sosyal medya sorumlusunda. Müşteriye Hamle göndermez.",
             f"Hamle kontrolü: {score}/100" + (" · " + str(len(findings)) + " not" if findings else " · not yok")]
    if findings:
        lines += [""] + [("🔴 " if lv == "kırmızı" else "🟡 ") + text for lv, text in findings]
    number = iter(range(1, 10))

    def head(title):
        return f"{next(number)}. {title}"
    lines += ["", head("STRATEJİ HATIRLATMASI")] + ["- " + x for x in plan["strategy"]] + [STRATEGY_NOTE]
    if plan["last_month"]:
        lines += ["", head("GEÇEN AY")] + ["- " + x for x in plan["last_month"]]
    lines += ["", head("İÇERİK SÜTUNLARI")] + [f"- {p['name']}: {p['why']}" for p in plan["pillars"]]
    sd = material["special_days"]
    if sd:
        lines += ["", head("ÖZEL GÜNLER")] + [f"- {s['date'][8:]}.{s['date'][5:7]} {s['name']}" for s in sd]
    lines += ["", head("TAKVİM")]
    for r in plan["days"]:
        if r.get("missing"):
            lines.append(f"{_day(r)} · öneri yok")
            continue
        lines.append(f"{'⭐ ' if r.get('recommended') else ''}{_day(r)} · {FORMAT_LABELS.get(r['format'], r['format'])} · "
                     f"{platform_label(r['platform'])} · {r['pillar']} · {r['topic']}"
                     + (f" · Özel gün: {r['special_day']}" if r.get("special_day") else ""))
    lines += ["", head("İÇERİK DETAYLARI")]
    for r in plan["days"]:
        if r.get("missing") or not r.get("caption"):
            continue
        lines += ["", f"{'⭐ ' if r.get('recommended') else ''}{_day(r)} · {r['topic']}",
                  f"Neden: {r['why']}", f"Kanca: {r['hook']}", "Caption:", r["caption"]]
        if r.get("linkedin_caption"):
            lines += ["LinkedIn metni:", r["linkedin_caption"]]
        if r.get("hashtags"):
            lines.append("Hashtag: " + " ".join(r["hashtags"]))
        if r.get("cta"):
            lines.append("Çağrı: " + r["cta"])
        lines.append("Görsel brief: " + r["visual_brief"])
        if r.get("on_screen_text"):
            lines.append("Ekran yazısı: " + r["on_screen_text"])
        lines.append("Ölçü / süre: " + r["size"])
    lines += ["", "Hamle AI · Gamechangers iç taslak · Kaynak: marka hafızası (son 90 gün), Basecamp, özel günler "
                  "takvimi, Platform Kural Defteri."]
    return "\n".join(lines)


# ------------------------------------------------------------------ run
def run_content_plan(ops, *, config, catalog, cards, cal, book_reader, upcoming_reader, model, clock, preview=True,
                     brand=None, month=None):
    """Preview only in this version: builds one brand's plan and returns its text; writes nothing."""
    report = {"status": None, "model_calls": 0, "business_writes": 0}
    try:
        now = clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        cfg = validate_config(config)
        _need(preview, "content_plan_live_not_built")
        today = now.astimezone(ISTANBUL).date()
        first = target_month(today, month)
        key = brand or next(iter(cfg["brands"]))
        _need(key in cfg["brands"], "content_plan_brand_not_configured")
        brands = bm.brands_of(catalog)
        _need(key in brands, "content_plan_brand_unknown")
        info = brands[key]
        card = next((b for b in (cards or {}).get("brands") or [] if b.get("project_id") == info["project_id"]), None)
        report.update(brand=key, month=f"{first:%Y-%m}")
        previous = ops._run(lambda tx, _: tx.get(record_key(key, (first - timedelta(days=1)).replace(day=1))),
                            allow_expired=True)
        material = gather(ops, key, info["name"], card, cfg["brands"][key], first, today,
                          upcoming=upcoming_reader(info["project_id"]), cal=cal, book=book_reader(), previous=previous)
        plan = build_plan(material, first, model)
        report["model_calls"] = plan["model_calls"]
        findings, score = check(plan, material, first)
        text = render(info["name"], first, plan, findings, score, material)
        return dict(report, status="content_plan_preview", score=score, findings=len(findings),
                    days=len(plan["days"]), recommended=sum(1 for r in plan["days"] if r.get("recommended")),
                    text=text)
    except Exception as exc:
        return dict(report, status="held", reason=_code(exc), automatic_retry_allowed=False)


def run_worker_content_plan(ops, secrets, *, clock=None, brand=None, month=None):
    """Entry for worker.py --mode content_plan --plan-preview [--plan-brand KEY] [--plan-month YYYY-MM]."""
    import basecamp_cloud_auth
    import cloud_providers as provider
    import v11_algorithm_watch as aw
    from v11_chain_live import _memo, load_catalog
    clock = clock or (lambda: datetime.now(timezone.utc))
    config, catalog, digest = load_config(), load_catalog(), dd.load_config()
    token = _memo(lambda: basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh))
    session = provider._session()

    def get(url):
        return bm.basecamp_get(session, url, "Bearer " + provider._token(token()),
                               "Hamle Content Plan (ahmet@gamechangers.com.tr)", ContentPlanError)

    def upcoming(pid):
        valid = dd.validate_config(digest)
        listing = dd.BasecampDigestReader(valid["account_id"], get=get, excluded=valid["excluded"]).collect()
        return [t for t in listing["items"] if t["project_id"] == pid]

    def book():
        row = ops._run(lambda tx, _: tx.get(aw.RULES_KEY), allow_expired=True)
        return row if isinstance(row, dict) and row.get("kind") == "platform_rules" else aw.new_book(aw.load_seed())
    try:
        model = model_source(ops, {"anthropic": lambda: provider.anthropic_key(secrets)}, catalog,
                             validate_config(config))
        return run_content_plan(ops, config=config, catalog=catalog, cards=dd.load_brand_cards(), cal=dd.load_calendar(),
                                book_reader=book, upcoming_reader=upcoming, model=model, clock=clock, preview=True,
                                brand=brand, month=month)
    finally:
        session.close()
