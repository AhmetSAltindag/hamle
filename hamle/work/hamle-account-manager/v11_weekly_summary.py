"""Haftalık marka özeti (Ahmet 2026-10-15: "her hafta marka için kısa haftalık özet çıkartan").

worker.py --mode weekly_summary (NON-default, no scheduler entry). Once per ISO week, at the first run on Friday
at/after 14:00 Istanbul (WINDOW 14:00-15:00; any other day or time is a no-op), ONE internal plain-Turkish mail
(+ HTML twin) from Hamle to the daily digest's recipients (daily-digest-config.json: @gamechangers.com.tr only,
never clients). Subject "Hamle AI: 16 Ekim Cuma Haftalık Marka Özeti". One section per catalog brand (brand_routes
without 'gamechangers' and 'new_biz', the brand memory's 8 brands), 1-5 short lines: what happened, what was
delivered, what is at risk / overdue, what is due next week; every line keeps a Basecamp url where one exists.

Material (read only):
- Brand memory (v11_brand_memory.brand_memory_context): the brand's events of the last 7 Istanbul days. The memory
  already leaves agency Teams chats (Heads...) out; no Teams source is added here.
- Basecamp (v11_daily_digest.BasecampDigestReader, GET only): open to-dos / dated cards of the brand project:
  overdue (account overdue report, else the scan), due today, due until the end of next ISO week. Hamle's own
  suggestion to-dos (New Business / Fikir / Karne) are left out, as in the digest.
- Hamle's client jobs (v11_brand_memory.client_jobs, the 'client-jobs' index): touched this week or due by the
  end of next week.

Model (role weekly_summary, Claude Haiku 5.5, effort low; ModelGate + budget gate exactly as brand memory): ONE
call for every brand with material when the input fits (<= MAX_INPUT_BYTES), else one call per brand: at most
one call per brand per week. A brand without material gets NOTHING_THIS_WEEK and no call. The output is shape
checked; a line survives only if it cites refs of that brand's input items; a url not among that brand's input
urls is dropped (then the first cited item's own url is used, if any); urls written inside the text are removed.
No budget / model failure / nothing valid -> deterministic lines straight from the items (never invented).

Durability: weekly-summary-YYYYWW (ISO year + week) in the operations store through the daily digest's own mail
path (v11_daily_digest.claim_mail / send_claimed / DigestMailWriter: claimed lease -> sending -> one POST ->
Sent-copy readback -> sent | unknown, never resent). preview=True (worker --summary-preview) builds the text on
any day, returns it, sends nothing and writes no summary record (model calls still go through the ledger and the
ModelGate cache, so the Friday run reuses a byte-identical answer).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import uuid
from zoneinfo import ZoneInfo

import v11_brand_memory as bm
import v11_daily_digest as dd

ISTANBUL = ZoneInfo("Europe/Istanbul")
PROMPT = Path(__file__).with_name("weekly_summary_prompt.txt")
ROLE = "weekly_summary"
WEEKDAY = 4                     # Friday
WINDOW = ("14:00", "15:00")     # first run at/after 14:00 Istanbul; later is "too late" (no-op)
KINDS = ("oldu", "teslim", "risk", "gelecek")
ICONS = {"oldu": "📌", "teslim": "✅", "risk": "⚠️", "gelecek": "📅"}
MAX_LINES = 5
MAX_INPUT_BYTES = 15_000        # model.MAX_INPUT_BYTES is 16 000
CAPS = {"events": 12, "overdue": 6, "due_today": 4, "next_week": 6, "jobs": 5}
REF = {"events": "e", "overdue": "g", "due_today": "b", "next_week": "h", "jobs": "i"}
NOTHING_THIS_WEEK = "Bu hafta kayda geçen bir gelişme yok."
FORGOT_TITLE = "Bir şey unutuldu mu?"
FORGOT_ASK = "Haftayı kapatmadan bakalım: bugün teslimi olup açık kalanlar, bu hafta verilen sözler, tarihi gelmiş müşteri işleri."
FORGOT_NONE = "Açıkta kalan bir şey görünmüyor."
FORGOT_MAX = 8
FOOTER = ("Hamle AI Müşteri Temsilcisi · Gamechangers haftalık marka özeti · Her cuma 14:00 · "
          "Hamle'nin gelişimi ve eğitimi için Basecamp ve Google Docs kullanımlarınız çok önemlidir.")
URL_IN_TEXT = re.compile(r"\s*\(?https?://[^\s()]+\)?")
OPEN_JOB_STATES = ("open", "delivered", "binding", "creating", "held")


class WeeklySummaryError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise WeeklySummaryError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _line(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def week_key(day):
    y, w, _ = day.isocalendar()
    return f"weekly-summary-{y}{w:02d}"


def week_of(day):
    """(first, last, next_week_last): the last 7 Istanbul days up to `day`, and the end of the next ISO week."""
    return day - timedelta(days=6), day, day + timedelta(days=(6 - day.weekday()) + 7)


def summary_title(day):
    return f"Hamle AI: {day.day} {dd.MONTHS[day.month - 1]} {dd.WEEKDAYS[day.weekday()]} Haftalık Marka Özeti"


def in_window(local):
    first, last = (int(v[:2]) * 60 + int(v[3:]) for v in WINDOW)
    return local.weekday() == WEEKDAY and first <= local.hour * 60 + local.minute <= last


# ------------------------------------------------------------------ material (read only)
def _who(names):
    return [n.split()[0] for n in names or [] if isinstance(n, str) and n.split()][:3]


def basecamp_material(reader, today, next_end):
    """BasecampDigestReader -> {project_id: {'overdue', 'due_today', 'next_week'}} (open items only)."""
    from v11_digital_karne import is_karne_item
    from v11_idea_tracking import is_idea_item
    from v11_nb_tracking import is_nb_item

    def tracked(t):  # Hamle's own suggestion to-dos are followed up elsewhere, not team work
        return is_nb_item(t) or is_idea_item(t) or is_karne_item(t)
    listing = reader.collect()
    items = [t for t in listing["items"] if not tracked(t)]
    report = reader.overdue_report(listing["project_names"])
    todos = [t for t in report if not tracked(t)] if report is not None else [t for t in items if t["kind"] == "todo"]
    out, seen, iso, end = {}, set(), today.isoformat(), next_end.isoformat()

    def row(t, **extra):
        return dict({"title": _line(t["title"], 110), "due_on": t["due_on"], "who": _who(t["assignees"]),
                     "url": t["app_url"]}, **extra)
    for t in todos + [t for t in items if t["kind"] == "card"]:
        if t["due_on"] and t["due_on"] < iso and (t["kind"], t["id"]) not in seen:
            seen.add((t["kind"], t["id"]))
            out.setdefault(t["project_id"], {}).setdefault("overdue", []).append(
                row(t, days_overdue=(today - date.fromisoformat(t["due_on"])).days))
    for t in sorted(items, key=lambda t: (t["due_on"] or "", t["title"])):
        if t["due_on"] == iso:
            out.setdefault(t["project_id"], {}).setdefault("due_today", []).append(row(t))
        elif t["due_on"] and iso < t["due_on"] <= end:
            out.setdefault(t["project_id"], {}).setdefault("next_week", []).append(row(t))
    for rows in out.values():
        rows.get("overdue", []).sort(key=lambda r: (-r["days_overdue"], r["title"]))
    return out


def job_rows(jobs, brand, first, next_end):
    lo = bm.day_start(first)
    out = []
    for j in jobs:
        if j.get("brand_key") != brand:
            continue
        stamps = [bm._ts(j.get(k)) for k in ("created_at", "updated_at", "opened_at", "closed_at")]
        due = j.get("due_on") if isinstance(j.get("due_on"), str) and bm.DAY.match(j["due_on"]) else None
        touched = any(t is not None and lo <= t for t in stamps)
        upcoming = due is not None and due <= next_end.isoformat() and j.get("state") in OPEN_JOB_STATES
        if touched or upcoming:
            url = j.get("app_url")
            out.append({"title": _line(j.get("title_core") or j.get("title"), 110), "state": bm._s(j.get("state"), 30),
                        "due_on": due, "who": _who([j.get("assignee_name")]),
                        "url": url if isinstance(url, str) and url.startswith("https://") and len(url) <= 300 else None,
                        "_t": max((t for t in stamps if t is not None), default=0)})
    out.sort(key=lambda r: -r["_t"])
    return [{k: v for k, v in r.items() if k != "_t"} for r in out]


def gather(ops, brands, *, basecamp, jobs, today):
    """brand key -> {'key', 'name', 'events', 'overdue', 'due_today', 'next_week', 'jobs', 'omitted'}; every item
    carries a ref unique inside its brand (e1, g1, b1, h1, i1)."""
    first, _, next_end = week_of(today)
    out = {}
    for key, info in brands.items():
        ctx = bm.brand_memory_context(ops, key, today, days=7, limit=40, detail=200)
        events = [{"date": e["date"], "kind": e.get("kind"), "title": _line(e.get("title"), 120),
                   "detail": _line(e.get("detail"), 200), "url": e.get("url") or None}
                  for e in ctx["events"] if first.isoformat() <= str(e.get("date")) <= today.isoformat()]
        events.reverse()  # newest first: the caps keep the freshest
        bc = basecamp.get(info["project_id"], {})
        b = {"key": key, "name": dd.BRAND_LABELS.get(info["project_id"], info["name"]),
             "events": events, "overdue": bc.get("overdue", []), "due_today": bc.get("due_today", []),
             "next_week": bc.get("next_week", []), "jobs": job_rows(jobs, key, first, next_end), "omitted": 0}
        for part, cap in CAPS.items():
            b["omitted"] += max(0, len(b[part]) - cap)
            b[part] = [dict(x, ref=f"{REF[part]}{n + 1}") for n, x in enumerate(b[part][:cap])]
        out[key] = b
    return out


def has_material(b):
    return any(b[p] for p in CAPS)


def _items(b):
    return [x for p in CAPS for x in b[p]]


# ------------------------------------------------------------------ model
def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCHEMA = _obj({"brands": {"type": "array", "maxItems": 8, "items": _obj({
    "key": {"type": "string", "minLength": 1, "maxLength": 40},
    "lines": {"type": "array", "minItems": 1, "maxItems": MAX_LINES, "items": _obj({
        "kind": {"type": "string", "enum": list(KINDS)},
        "text": {"type": "string", "minLength": 1, "maxLength": 200},
        "refs": {"type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "string", "minLength": 1,
                                                                             "maxLength": 8}},
        "url": {"type": ["string", "null"], "minLength": 1, "maxLength": 300}})}})}})


def read_output(value):
    """Shape check only; every line is checked against the input items in sourced()."""
    _need(isinstance(value, dict) and isinstance(value.get("brands"), list)
          and all(isinstance(b, dict) for b in value["brands"]), "weekly_summary_output_invalid")
    return value


def _brand_input(b):
    return {k: ([{f: v for f, v in x.items() if v not in (None, [], "")} for x in b[k]] if k in CAPS else b[k])
            for k in ("key", "name", *CAPS, "omitted")}


def model_input(day, brands):
    first, last, next_end = week_of(day)
    return {"today": day.isoformat(), "week": {"from": first.isoformat(), "to": last.isoformat(),
                                               "next_week_to": next_end.isoformat()},
            "brands": [_brand_input(b) for b in brands]}


def shrink(inp):
    """Bounded request (<= MAX_INPUT_BYTES): texts shrink first, then the least urgent items go (counted)."""
    for limit in (120, 60, 0):
        if bm._size(inp) <= MAX_INPUT_BYTES:
            break
        for b in inp["brands"]:
            for e in b["events"]:
                if limit:
                    e["detail"] = e.get("detail", "")[:limit]
                else:
                    e.pop("detail", None)
    order = ("events", "jobs", "next_week", "due_today", "overdue")
    while bm._size(inp) > MAX_INPUT_BYTES:
        b = max(inp["brands"], key=lambda b: sum(len(b[p]) for p in order))
        part = next((p for p in order if b[p]), None)
        _need(part is not None, "weekly_summary_input_too_large")
        b[part].pop()
        b["omitted"] += 1
    return inp


def summary_source(ops, load_key, catalog, *, routing=None, client=None):
    """summarize(request) -> model output; one ModelGate chance per exact request (internal cap, budget gate)."""
    import model as base
    import model_routing
    from v11_chain_live import budget_gate
    from v11_chain_models import ModelGate, internal_cap_microdollars, key_for, reservation_for, source_digest
    from v11_trend_search import route_fingerprint
    route = (routing if routing is not None else model_routing.load()).route(ROLE)
    _need(route.provider == "anthropic", "weekly_summary_route_not_claude")
    cap = internal_cap_microdollars(catalog)
    hold = reservation_for(route)
    budget_ok = budget_gate(ops, hold, cap)

    def summarize(request):
        _need(budget_ok(), "model_budget_unavailable")
        gate = ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)

        def call():
            try:
                system = PROMPT.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise WeeklySummaryError("weekly_summary_prompt_unavailable") from None
            key = key_for(load_key, "anthropic")
            try:
                return base.anthropic_json(route, system, request, SCHEMA, api_key=key, client=client,
                                           validator=read_output)
            finally:
                del key
        return gate.once(ROLE, source_digest(ROLE, request, route_fingerprint(route, PROMPT)), call,
                         provider="anthropic", hold=hold)
    summarize.budget_ok = budget_ok
    return summarize


def sourced(output, brand):
    """The model's lines for one brand -> (lines, dropped_lines, dropped_urls). Nothing is repaired or invented."""
    items = {x["ref"]: x for x in _items(brand)}
    urls = {x["url"] for x in items.values() if x.get("url")}
    rows = next((b.get("lines") for b in (output or {}).get("brands") or []
                 if isinstance(b, dict) and b.get("key") == brand["key"]), None)
    lines, seen, dropped, dropped_urls = [], set(), 0, 0
    for r in rows if isinstance(rows, list) else []:
        refs = [x for x in (r.get("refs") or []) if isinstance(x, str) and x in items] if isinstance(r, dict) else []
        text = _line(URL_IN_TEXT.sub("", r.get("text") or ""), 200) if refs and isinstance(r.get("text"), str) else ""
        if not text or r.get("kind") not in KINDS or text.casefold() in seen or len(lines) >= MAX_LINES:
            dropped += 1
            continue
        seen.add(text.casefold())
        url = r.get("url")
        if url is not None and url not in urls:
            dropped_urls += 1
            url = None
        if url is None:  # the cited item's own url (copied from the input, never built)
            url = next((items[x]["url"] for x in refs if items[x].get("url")), None)
        lines.append({"kind": r["kind"], "text": text, "url": url})
    return lines, dropped, dropped_urls


def _date(iso):
    d = date.fromisoformat(iso)
    return f"{d.day} {dd.MONTHS[d.month - 1]}"


def _at(names):
    return (" " + " ".join("@" + n for n in names)) if names else ""


def forgotten(brands, day):
    """Code-built 'Bir şey unutuldu mu?' rows across brands: open items due today, promises recorded this week,
    open client jobs whose date has come. Only items from the material; nothing judged or invented."""
    rows, iso = [], day.isoformat()
    for b in brands:
        tag = f" · {b['name']}"
        rows += [(f"“{t['title']}” bugün teslim, hâlâ açık{_at(t['who'])}{tag}", t["url"]) for t in b["due_today"]]
        rows += [(f"Söz: {e['title']} ({_date(e['date'])}){tag}", e.get("url")) for e in b["events"]
                 if e.get("kind") == "soz"]
        rows += [(f"Müşteri işi: “{j['title']}” tarihi {_date(j['due_on'])}, durum {j['state']}{_at(j['who'])}{tag}",
                  j["url"]) for j in b["jobs"] if j.get("due_on") and j["due_on"] <= iso and j.get("state") in OPEN_JOB_STATES]
    return rows[:FORGOT_MAX]


def fallback_lines(b):
    """Deterministic lines straight from the items (no model): one per kind, each with its item's url."""
    lines = []
    done = [e for e in b["events"] if e.get("kind") in ("teslim", "onay")]
    other = [e for e in b["events"] if e not in done]
    if other:
        lines.append({"kind": "oldu", "text": other[0]["title"] + (f" (+{len(other) - 1} kayıt daha)"
                                                                   if len(other) > 1 else ""), "url": other[0]["url"]})
    elif b["jobs"]:
        j = b["jobs"][0]
        lines.append({"kind": "oldu", "text": f"İş: {j['title']}" + (f" ({j['state']})" if j.get("state") else ""),
                      "url": j["url"]})
    if done:
        lines.append({"kind": "teslim", "text": done[0]["title"] + (f" (+{len(done) - 1} daha)" if len(done) > 1 else ""),
                      "url": done[0]["url"]})
    risky = b["overdue"] + b["due_today"]
    if risky:
        t = risky[0]
        state = f"{t['days_overdue']} gün gecikmiş" if "days_overdue" in t else "bugün teslim, hâlâ açık"
        more = f" (+{len(risky) - 1} iş daha)" if len(risky) > 1 else ""
        lines.append({"kind": "risk", "text": f"“{t['title']}” {state}{_at(t['who'])}{more}", "url": t["url"]})
    if b["next_week"]:
        t = b["next_week"][0]
        more = f" (+{len(b['next_week']) - 1} iş daha)" if len(b["next_week"]) > 1 else ""
        lines.append({"kind": "gelecek", "text": f"“{t['title']}” teslim {_date(t['due_on'])}{_at(t['who'])}{more}",
                      "url": t["url"]})
    return lines[:MAX_LINES]


def summarize_all(day, brands, model, *, max_calls=8):
    """brands: gather() values in catalog order -> (sections {key: {'lines', 'source'}}, stats)."""
    stats = {"model_calls": 0, "model": "not_called", "dropped_lines": 0, "dropped_urls": 0}
    sections = {b["key"]: {"lines": [], "source": "empty"} for b in brands}
    material = [b for b in brands if has_material(b)]
    if material and model is not None:
        whole = model_input(day, material)
        batches = [whole] if bm._size(whole) <= MAX_INPUT_BYTES else [shrink(model_input(day, [b])) for b in material]
        outcomes = []
        for request in batches:
            if stats["model_calls"] >= max_calls:
                outcomes.append("call_limit_reached")
                break
            if getattr(model, "budget_ok", None) is not None and not model.budget_ok():
                outcomes.append("model_budget_unavailable")
                break
            stats["model_calls"] += 1
            sent = {b["key"]: b for b in request["brands"]}
            try:
                output = model(request)
            except Exception as exc:
                outcomes.append("model_failed:" + _code(exc))
                continue
            outcomes.append("ok")
            for b in material:
                if b["key"] in sent:
                    lines, dropped, dropped_urls = sourced(output, dict(b, **{p: sent[b["key"]][p] for p in CAPS}))
                    stats["dropped_lines"] += dropped
                    stats["dropped_urls"] += dropped_urls
                    if lines:
                        sections[b["key"]] = {"lines": lines, "source": "model"}
        stats["model"] = ",".join(sorted(set(outcomes))) or "not_called"
    elif material:
        stats["model"] = "model_unavailable"
    for b in material:
        if not sections[b["key"]]["lines"]:
            sections[b["key"]] = {"lines": fallback_lines(b), "source": "fallback"}
    return sections, stats


# ------------------------------------------------------------------ text
def render(day, brands, sections):
    first, last, _ = week_of(day)
    overdue = sum(len(b["overdue"]) for b in brands)
    upcoming = sum(len(b["next_week"]) for b in brands)
    lines = [summary_title(day),
             f"Hafta: {_date(first.isoformat())} – {_date(last.isoformat())} · {len(brands)} marka · "
             f"Geciken: {overdue} · Gelecek hafta teslim: {upcoming}"]
    for b in brands:
        lines += ["", dd._upper_tr(b["name"])]
        rows = sections[b["key"]]["lines"]
        lines += [f"{ICONS[r['kind']]} {r['text']}" + (f" · {r['url']}" if r["url"] else "") for r in rows] \
            or ["- " + NOTHING_THIS_WEEK]
    lines += ["", dd._upper_tr(FORGOT_TITLE), FORGOT_ASK]
    lines += [f"🔎 {t}" + (f" · {u}" if u else "") for t, u in forgotten(brands, day)] or ["- " + FORGOT_NONE]
    lines += ["", FOOTER]
    return "\n".join(lines)


def render_html(day, brands, sections):
    first, last, _ = week_of(day)
    overdue = sum(len(b["overdue"]) for b in brands)
    upcoming = sum(len(b["next_week"]) for b in brands)
    out = [f'<div style="font-size:19px;font-weight:bold;margin:0 0 4px;">{dd._e(summary_title(day))}</div>',
           dd._p(dd._e(f"Hafta {_date(first.isoformat())} – {_date(last.isoformat())} · {len(brands)} marka · "
                       f"Geciken {overdue} · Gelecek hafta teslim {upcoming}"), f"color:{dd.GREY};")]
    for b in brands:
        out.append(dd._h2(b["name"]))
        rows = sections[b["key"]]["lines"]
        out.append(dd._ul([dd._li(f"{ICONS[r['kind']]} " + (dd._a(r["url"], r["text"], False) if r["url"]
                                                            else dd._e(r["text"]))) for r in rows])
                   if rows else dd._p(dd._grey(NOTHING_THIS_WEEK)))
    out.append(dd._h2(FORGOT_TITLE))
    out.append(dd._p(dd._e(FORGOT_ASK), f"color:{dd.GREY};"))
    forgot = forgotten(brands, day)
    out.append(dd._ul([dd._li("🔎 " + (dd._a(u, t, False) if u else dd._e(t))) for t, u in forgot])
               if forgot else dd._p(dd._grey(FORGOT_NONE)))
    out.append(f'<div style="margin-top:26px;padding-top:8px;border-top:1px solid #e5e7eb;font-size:12px;'
               f'color:{dd.GREY};">{dd._e(FOOTER)}</div>')
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:0;">'
            f'<div style="font-family:{dd.FONT};font-size:14px;line-height:1.55;color:{dd.TEXT};width:100%;">'
            + "".join(out) + "</div></body></html>")


def build_summary(ops, *, catalog, reader, model, day):
    """Read-only material + model (or fallback) -> (body, html, stats)."""
    found = bm.brands_of(catalog)
    brands = {k: found[k] for k in catalog["brand_routes"] if k in found}  # catalog order (WAT first)
    _, _, next_end = week_of(day)
    material = gather(ops, brands, basecamp=basecamp_material(reader, day, next_end), jobs=bm.client_jobs(ops),
                      today=day)
    ordered = [material[k] for k in brands]
    sections, stats = summarize_all(day, ordered, model, max_calls=len(ordered))
    stats["brands"] = {k: {"lines": len(s["lines"]), "source": s["source"]} for k, s in sections.items()}
    return render(day, ordered, sections), render_html(day, ordered, sections), stats


# ------------------------------------------------------------------ run
def run_weekly_summary(ops, *, config, catalog, reader_factory, writer, model, clock, preview=False):
    """One team mail per ISO week (Friday >= 14:00 Istanbul), or a preview (no send, no summary record)."""
    from runtime_authority import deadline_timestamp, start_timestamp
    claim = None
    try:
        now = clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        _need(start_timestamp() <= now.timestamp() <= deadline_timestamp(), "authorization_expired")
        cfg = dd.validate_config(config)  # the digest's recipients: agency addresses only, else refused
        local = now.astimezone(ISTANBUL)
        day, key = local.date(), week_key(local.date())
        if preview:
            c = ops.control()
            _need(c["mode"] == "review_open", "control_stopped_or_stale")
            _need(not ops.budget_status()["overrun_hold"], "budget_overrun_hold")
        else:
            if not cfg["enabled"] or not cfg["recipients"]:
                return {"status": "no_new_work", "reason": "digest_disabled_or_no_recipients", "business_writes": 0}
            if not in_window(local):
                return {"status": "no_new_work", "reason": "outside_weekly_summary_window", "business_writes": 0}
            claim = dd.claim_mail(ops, key, {"kind": "weekly_summary", "token": uuid.uuid4().hex,
                                             "date": day.isoformat(), "iso_week": key[-6:]})
            if claim is None:
                return {"status": "already_recorded", "reason": "weekly_summary_already_claimed_or_sent", "key": key,
                        "business_writes": 0}
        body, html, stats = build_summary(ops, catalog=catalog, reader=reader_factory(), model=model, day=day)
        subject = summary_title(day)
        if preview:
            return {"status": "weekly_summary_preview", "key": key, "subject": "[ÖNİZLEME] " + subject, "body": body,
                    "recipients": len(cfg["recipients"]), "stats": stats, "business_writes": 0}
        recipients = list(cfg["recipients"])
        _need(recipients and all(dd.internal_address(r) for r in recipients), "digest_recipient_not_internal_agency")
        payload = {"from": dd.HAMLE, "to": recipients, "cc": [], "subject": subject, "body": body, "html": html}
        if dd.send_claimed(ops, claim, payload, stats, writer) is None:
            return {"status": "held", "reason": "weekly_summary_outcome_unknown_no_retry", "business_writes": None,
                    "automatic_retry_allowed": False}
        return {"status": "weekly_summary_sent", "key": key, "recipients": len(recipients), "stats": stats,
                "day": day.isoformat(), "business_writes": 1}
    except Exception as exc:
        reason = _code(exc)
        dd.release_claim(ops, claim, reason)
        return {"status": "held", "reason": reason, "business_writes": 0, "automatic_retry_allowed": False}


# ------------------------------------------------------------------ live wiring
def run_worker_weekly_summary(ops, secrets, *, clock=None, preview=False):
    """Entry for worker.py --mode weekly_summary [--summary-preview] (existing credential loaders only;
    Basecamp GET only; the mail goes through the daily digest's Gmail writer)."""
    import basecamp_cloud_auth
    import cloud_providers as provider
    from v11_chain_live import _memo, load_catalog
    clock = clock or (lambda: datetime.now(timezone.utc))
    config, catalog = dd.load_config(), load_catalog()
    token = _memo(lambda: basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh))
    session = provider._session()

    def get(url):
        from v11_brand_memory import basecamp_get
        return basecamp_get(session, url, "Bearer " + provider._token(token()),
                            "Hamle Weekly Summary (ahmet@gamechangers.com.tr)", WeeklySummaryError)

    def reader_factory():
        valid = dd.validate_config(config)
        return dd.BasecampDigestReader(valid["account_id"], get=get, excluded=valid["excluded"])
    writer = None
    if not preview:
        from v11_chain_mail import gmail_token_context
        writer = dd.DigestMailWriter(token_context=gmail_token_context(secrets), session_factory=provider._session)
    try:
        model = summary_source(ops, {"anthropic": lambda: provider.anthropic_key(secrets)}, catalog)
    except Exception:
        model = None  # deterministic lines from the items
    try:
        return run_weekly_summary(ops, config=config, catalog=catalog, reader_factory=reader_factory, writer=writer,
                                  model=model, clock=clock, preview=preview)
    finally:
        session.close()
