"""Aylık detaylı marka raporu (Ahmet 2026-10-15: "her ay ise detaylı kompleks rapor hazırlayan").

worker.py --mode monthly_report (NON-default, no scheduler entry). On the first Istanbul weekday (Mon-Fri) of each
month, between 10:30 and 12:30 (WINDOW; any other day or time is a no-op), ONE internal plain-Turkish mail (+ HTML
twin) per catalog brand (brand_routes without 'gamechangers' and 'new_biz', the brand memory's 8 brands) about the
previous calendar month, from Hamle to the daily digest's recipients (daily-digest-config.json: @gamechangers.com.tr
only, never clients). Subject "Hamle AI: WAT · Ekim 2026 Aylık Marka Raporu". Sections:
  1. Ayın özeti (model, 2-3 sentences)          5. Verilen sözler ve durumları (model)
  2. Sayılarla ay (CODE, never the model)       6. Riskler ve tekrar eden sorunlar (model)
  3. Öne çıkan işler ve teslimler (model)       7. Gelecek ay için öneriler (model, <= 3, marked as suggestions)
  4. Müşteriyle kararlar ve onaylar (model)

Material (read only; no Teams source is added here, the brand memory already leaves agency chats out):
- Brand memory: the month record brand-memory-<brand>-<YYYYMM> (every event of the month) and brand-profile-<brand>
  (open promises; decisions dated in the month).
- Basecamp, GET only, two existing readers:
  * v11_daily_digest.BasecampDigestReader: open to-dos / dated cards (collect + account overdue report). It lists
    OPEN items only (todos.json without completed=true; its allowlist accepts no other query), so it gives
    "overdue at month end and still open" and "due next month", never completions.
  * v11_brand_memory.BasecampMemoryReader (a BasecampDigestReader subclass, recordings endpoint): the brand
    project's to-dos updated since the first day of the month, completed ones included -> opened in the month,
    completed in the month, completed late (completion day after due_on), and overdue at month end but completed
    after it. When its pages do not reach the first of the month the numbers are marked incomplete (basecamp_complete
    false, shown as "en az").
  Hamle's own suggestion to-dos (New Business / Fikir / Karne) are left out, as in the digest.
- Hamle's client jobs (v11_brand_memory.client_jobs, the 'client-jobs' index): opened / closed in the month and the
  client's follow-up notes in the month (client_notes, counted as revisions).

Model (role monthly_report, Claude Opus 5.5, effort medium, max_tokens 6000, no web search; ModelGate + budget gate
exactly as v11_weekly_summary): ONE call per brand with material, input <= MAX_INPUT_BYTES (texts shrink first, then
the oldest and least important items go, counted). A brand without material gets a short "nothing this month" mail
built without a model call. Every content line must cite refs of that brand's input items; a number in the text must
appear in the cited items; a url not among the cited items' urls is dropped (the first cited item's own url is used);
urls written inside the text are removed. A failed model call (ModelGate never retries the same request) -> the
sections are built straight from the items, marked as such. No budget (budget gate) or no model -> that brand is
NOT sent; its claim is released and a later run in the window continues.

Durability: monthly-report-<brand>-<YYYYMM> (the REPORTED month) per brand in the operations store through the
daily digest's one-mail path (claim_mail / send_claimed / release_claim: claimed lease -> sending -> one POST ->
Sent-copy readback -> sent | unknown, never resent). A run walks the brands in catalog order, skips every brand
already claimed / sent, and stops cleanly before RUN_SECONDS (25 min) minus one model call's reserve; the next run
in the window continues. preview=True (worker --report-preview [--report-brand KEY]) builds the text on any day for
the previous month, sends nothing and writes no report record (model calls still go through the ledger and the
ModelGate cache, so the real run reuses a byte-identical answer).
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import time
import uuid
from zoneinfo import ZoneInfo

import v11_brand_memory as bm
import v11_daily_digest as dd

ISTANBUL = ZoneInfo("Europe/Istanbul")
PROMPT = Path(__file__).with_name("monthly_report_prompt.txt")
ROLE = "monthly_report"
WINDOW = ("10:30", "12:30")      # first Istanbul weekday of the month
RUN_SECONDS = 1500               # a run stops cleanly before 25 minutes
CALL_RESERVE_SECONDS = 300       # one Opus call (= the route timeout) must still fit
MAX_INPUT_BYTES = 15_000         # model.MAX_INPUT_BYTES is 16 000
CAPS = {"events": 40, "completed": 15, "opened": 10, "overdue": 10, "next_month_due": 8, "jobs": 10, "promises": 10,
        "decisions": 10}
REF = {"events": "e", "completed": "c", "opened": "o", "overdue": "g", "next_month_due": "h", "jobs": "i",
       "promises": "p", "decisions": "k"}
NUMBERS_REF = "n"
SECTIONS = (("summary", "Ayın özeti", 3), ("highlights", "Öne çıkan işler ve teslimler", 6),
            ("decisions", "Müşteriyle kararlar ve onaylar", 5), ("promises", "Verilen sözler ve durumları", 5),
            ("risks", "Riskler ve tekrar eden sorunlar", 5), ("suggestions", "Gelecek ay için öneriler", 3))
LIMITS = {k: n for k, _, n in SECTIONS}
PROMISE_STATES = ("tutuldu", "devam ediyor", "gecikti", "belirsiz")
NUMBERS_TITLE = "Sayılarla ay"
SUGGESTION_NOTE = "Hamle'nin önerileridir; karar ekibindir."
EMPTY_SECTION = "Bu ay kayda geçen bir şey yok."
FALLBACK_NOTE = "Not: Model yanıtı alınamadı; bölümler kayıtlardan otomatik derlendi, öneri yazılmadı."
FOOTER = ("Hamle AI Müşteri Temsilcisi · Gamechangers aylık marka raporu · Her ayın ilk hafta içi günü 10:30 · "
          "Hamle'nin gelişimi ve eğitimi için Basecamp ve Google Docs kullanımlarınız çok önemlidir.")
KIND_LABELS = {"talep": "talep", "teslim": "teslim", "revizyon": "revizyon", "onay": "onay", "karar": "karar",
               "soz": "söz", "risk": "risk", "bilgi": "bilgi"}
URL_IN_TEXT = re.compile(r"\s*\(?https?://[^\s()]+\)?")
DIGITS = re.compile(r"\d+")
OPEN_JOB_STATES = ("open", "delivered", "binding", "creating", "held", "awaiting_approval")


class MonthlyReportError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise MonthlyReportError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _line(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


# ------------------------------------------------------------------ calendar
def report_month(day):
    """(first, last) of the calendar month before `day`."""
    last = day.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


def next_month(first):
    nxt = (first + timedelta(days=32)).replace(day=1)
    return nxt, (nxt + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def first_weekday(year, month):
    d = date(year, month, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def in_window(local):
    lo, hi = (int(v[:2]) * 60 + int(v[3:]) for v in WINDOW)
    return local.date() == first_weekday(local.year, local.month) and lo <= local.hour * 60 + local.minute <= hi


def report_key(brand, first):
    return f"monthly-report-{brand}-{first:%Y%m}"


def month_name(first):
    return f"{dd.MONTHS[first.month - 1]} {first.year}"


def report_title(label, first):
    return f"Hamle AI: {label} · {month_name(first)} Aylık Marka Raporu"


def _local_day(ts):
    return datetime.fromtimestamp(ts, ISTANBUL).date() if ts is not None else None


# ------------------------------------------------------------------ material (read only)
def _tracked(t):
    """Hamle's own suggestion to-dos (New Business / Fikir / Karne) are followed up elsewhere, not team work."""
    from v11_digital_karne import is_karne_item
    from v11_idea_tracking import is_idea_item
    from v11_nb_tracking import is_nb_item
    return is_nb_item(t) or is_idea_item(t) or is_karne_item(t)


def _who(names):
    return [n.split()[0] for n in names or [] if isinstance(n, str) and n.split()][:3]


def open_items(reader):
    """BasecampDigestReader (open items only) -> {project_id: [item]} without Hamle's own suggestion to-dos."""
    listing = reader.collect()
    report = reader.overdue_report(listing["project_names"])
    out, seen = {}, set()
    for t in listing["items"] + (report or []):
        if (t["kind"], t["id"]) in seen or _tracked(t):
            continue
        seen.add((t["kind"], t["id"]))
        out.setdefault(t["project_id"], []).append(t)
    return out


def _completed_ts(row):
    t = bm._ts(row.get("completed_at"))
    if t is None and isinstance(row.get("completion"), dict):
        t = bm._ts(row["completion"].get("created_at"))
    return t


def recorded_todos(rows, account, pid):
    """Recordings rows (to-dos updated since the month start, completed ones included) -> plain rows."""
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get("type", "Todo") != "Todo" or type(r.get("id")) is not int:
            continue
        row = {"id": r["id"], "kind": "todo", "project_id": pid,
               "title": dd._plain(r.get("title") or r.get("content"), 110),
               "created": bm._ts(r.get("created_at")), "done": _completed_ts(r) if r.get("completed") is True else None,
               "due_on": r["due_on"] if isinstance(r.get("due_on"), str) and bm.DAY.match(r["due_on"]) else None,
               "who": _who(bm._names(r.get("assignees"))), "url": bm._app_url(r, account, pid)}
        if not _tracked(row):
            out.append(row)
    return out


def _notes_in(job, first, last):
    """The client's follow-up notes on a job dated inside the month ('%d.%m.%Y %H:%M' or 'YYYY-MM-DD HH:MM')."""
    n = 0
    for note in job.get("client_notes") or []:
        at = str((note or {}).get("at") or "") if isinstance(note, dict) else ""
        d = None
        for fmt in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
            try:
                d = datetime.strptime(at[:16], fmt).date()
                break
            except ValueError:
                continue
        n += d is not None and first <= d <= last
    return n


def job_rows(jobs, brand, first, last):
    lo, hi = bm.day_start(first), bm.day_start(last + timedelta(days=1))
    out = []
    for j in jobs:
        if j.get("brand_key") != brand:
            continue
        opened = min((t for t in (bm._ts(j.get("created_at")), bm._ts(j.get("opened_at"))) if t is not None),
                     default=None)
        closed = bm._ts(j.get("closed_at"))
        updated = bm._ts(j.get("updated_at"))
        notes = _notes_in(j, first, last)
        touched = any(t is not None and lo <= t < hi for t in (opened, closed, updated)) or notes
        if not touched:
            continue
        url = j.get("app_url")
        out.append({"title": _line(j.get("title_core") or j.get("title"), 110), "state": bm._s(j.get("state"), 30),
                    "opened_on": _local_day(opened).isoformat() if opened is not None else None,
                    "closed_on": _local_day(closed).isoformat() if closed is not None else None,
                    "client_notes": notes, "who": _who([j.get("assignee_name")]),
                    "url": url if isinstance(url, str) and url.startswith("https://") and len(url) <= 300 else None,
                    "_opened": opened is not None and lo <= opened < hi, "_closed": closed is not None and lo <= closed < hi,
                    "_t": opened or 0})
    out.sort(key=lambda r: r["_t"])
    return out


def _read_memory(ops, brand, first):
    def read(tx, _):
        return tx.get(bm.month_key(brand, first)), tx.get(bm.profile_key(brand))
    return ops._run(read, allow_expired=True)


def gather(ops, key, info, first, last, *, opened_items, recordings, account, jobs):
    """One brand's material for the month -> {'key', 'name', 'label', 'numbers', <parts with refs>, 'omitted'}."""
    pid = info["project_id"]
    lo, hi = bm.day_start(first), bm.day_start(last + timedelta(days=1))
    nxt_first, nxt_last = next_month(first)
    month_row, profile = _read_memory(ops, key, first)
    month_row = month_row if isinstance(month_row, dict) else {}
    events = [{"date": e["date"], "kind": e.get("kind"), "source": e.get("source"), "title": _line(e.get("title"), 120),
               "detail": _line(e.get("detail"), 240), "people": list(e.get("people") or [])[:4],
               "url": e.get("url") or None}
              for e in month_row.get("events") or []
              if isinstance(e, dict) and first.isoformat() <= str(e.get("date")) <= last.isoformat()]
    events.sort(key=lambda e: e["date"])
    days_done = {d for d in month_row.get("days_done") or [] if first.isoformat() <= str(d) <= last.isoformat()}
    profile = profile if isinstance(profile, dict) else {}
    promises = [{"what": _line(p.get("what"), 200), "due": bm._s(p.get("due"), 40), "by": bm._s(p.get("by"), 80)}
                for p in profile.get("open_promises") or [] if isinstance(p, dict) and bm._s(p.get("what"), 200)]
    decisions = [{"date": p["date"], "what": _line(p.get("what"), 200)} for p in profile.get("decisions") or []
                 if isinstance(p, dict) and isinstance(p.get("date"), str)
                 and first.isoformat() <= p["date"] <= last.isoformat() and bm._s(p.get("what"), 200)]
    # Basecamp: recordings (completions) + open listing (still open now)
    rows, covered = recordings.recent(pid, "Todo", lo)
    todos = recorded_todos(rows, account, pid)
    complete = covered <= lo
    in_month = [t for t in todos if t["created"] is not None and lo <= t["created"] < hi]
    done = [t for t in todos if t["done"] is not None and lo <= t["done"] < hi]
    completed = [{"title": t["title"], "due_on": t["due_on"], "completed_on": _local_day(t["done"]).isoformat(),
                  "late": bool(t["due_on"] and _local_day(t["done"]).isoformat() > t["due_on"]), "who": t["who"],
                  "url": t["url"], "_t": t["done"]} for t in done]
    completed.sort(key=lambda r: r["_t"])
    opened = [{"title": t["title"], "created_on": _local_day(t["created"]).isoformat(), "due_on": t["due_on"],
               "who": t["who"], "url": t["url"], "_t": t["created"]} for t in in_month if t not in done]
    opened.sort(key=lambda r: r["_t"])
    overdue, seen = [], set()
    for t in opened_items.get(pid, []):  # still open now, due by the month end
        if t["kind"] == "todo" and t["due_on"] and t["due_on"] <= last.isoformat() \
                and (t.get("created_on") or "") <= last.isoformat():
            seen.add(t["id"])
            overdue.append({"title": t["title"], "due_on": t["due_on"], "open_now": True, "who": _who(t["assignees"]),
                            "url": t["app_url"]})
    for t in todos:  # completed after the month end, due by it, created before it
        if t["id"] not in seen and t["due_on"] and t["due_on"] <= last.isoformat() and t["done"] is not None \
                and t["done"] >= hi and t["created"] is not None and t["created"] < hi:
            seen.add(t["id"])
            overdue.append({"title": t["title"], "due_on": t["due_on"], "open_now": False, "who": t["who"],
                            "url": t["url"]})
    month_end = last + timedelta(days=1)
    for r in overdue:
        r["days_overdue"] = (month_end - date.fromisoformat(r["due_on"])).days
    overdue.sort(key=lambda r: (-r["days_overdue"], r["title"]))
    upcoming = sorted(({"title": t["title"], "due_on": t["due_on"], "who": _who(t["assignees"]), "url": t["app_url"]}
                       for t in opened_items.get(pid, [])
                       if t["due_on"] and nxt_first.isoformat() <= t["due_on"] <= nxt_last.isoformat()),
                      key=lambda r: (r["due_on"], r["title"]))
    jrows = job_rows(jobs, key, first, last)
    kinds = Counter(e["kind"] for e in events if e["kind"] in bm.KINDS)
    numbers = {"todos_opened": len(in_month), "todos_completed": len(done),
               "completed_late": sum(1 for c in completed if c["late"]), "overdue_at_month_end": len(overdue),
               "basecamp_complete": complete,
               "jobs_opened": sum(1 for j in jrows if j["_opened"]), "jobs_closed": sum(1 for j in jrows if j["_closed"]),
               "client_revisions": sum(j["client_notes"] for j in jrows),
               "events_by_kind": {k: kinds[k] for k in bm.KINDS if kinds[k]}, "events_total": len(events),
               "memory_days": f"{len(days_done)}/{(last - first).days + 1}"}
    b = {"key": key, "name": info["name"], "label": dd.BRAND_LABELS.get(pid, info["name"]), "numbers": numbers,
         "events": events, "completed": completed, "opened": opened, "overdue": overdue, "next_month_due": upcoming,
         "jobs": jrows, "promises": promises, "decisions": decisions, "omitted": 0}
    for part, cap in CAPS.items():  # the newest are kept (oldest go first)
        keep = b[part] if part in ("overdue", "next_month_due") else b[part][-cap:]
        keep = keep[:cap]
        b["omitted"] += len(b[part]) - len(keep)
        b[part] = [dict({k: v for k, v in x.items() if not k.startswith("_")}, ref=f"{REF[part]}{n + 1}")
                   for n, x in enumerate(keep)]
    return b


def has_material(b):
    """Anything that happened in the month, or work overdue at its end (next-month items or old promises alone are
    not a month's material)."""
    n = b["numbers"]
    return bool(b["events"] or b["completed"] or b["opened"] or b["overdue"] or b["jobs"] or b["decisions"]
                or n["todos_opened"] or n["todos_completed"] or n["jobs_opened"] or n["jobs_closed"])


def _items(b):
    return [x for p in CAPS for x in b[p]]


# ------------------------------------------------------------------ model
def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_REFS = {"type": "array", "minItems": 1, "maxItems": 6, "items": {"type": "string", "minLength": 1, "maxLength": 8}}
_TEXT = {"type": "string", "minLength": 1, "maxLength": 260}
_URL = {"type": ["string", "null"], "minLength": 1, "maxLength": 300}
_LINE = _obj({"text": _TEXT, "refs": _REFS, "url": _URL})
SCHEMA = _obj({
    "summary": {"type": "array", "maxItems": 3, "items": _obj({"text": _TEXT, "refs": _REFS})},
    "highlights": {"type": "array", "maxItems": 6, "items": _LINE},
    "decisions": {"type": "array", "maxItems": 5, "items": _LINE},
    "promises": {"type": "array", "maxItems": 5, "items": _obj({"text": _TEXT, "refs": _REFS, "url": _URL,
                                                                "status": {"type": "string",
                                                                           "enum": list(PROMISE_STATES)}})},
    "risks": {"type": "array", "maxItems": 5, "items": _LINE},
    "suggestions": {"type": "array", "maxItems": 3, "items": _obj({"text": _TEXT, "refs": _REFS})}})


def read_output(value):
    """Shape check only; every line is checked against the brand's input items in sourced()."""
    _need(isinstance(value, dict) and all(isinstance(value.get(k), list) for k, _, _ in SECTIONS),
          "monthly_report_output_invalid")
    return value


def model_input(b, first, last):
    nxt_first, nxt_last = next_month(first)
    inp = {"month": {"from": first.isoformat(), "to": last.isoformat(), "name": month_name(first)},
           "next_month": {"from": nxt_first.isoformat(), "to": nxt_last.isoformat()},
           "brand": {"key": b["key"], "name": b["name"]}, "numbers": dict(b["numbers"], ref=NUMBERS_REF)}
    for part in CAPS:
        inp[part] = [{k: v for k, v in x.items() if v not in (None, [], "")} for x in b[part]]
    inp["omitted"] = b["omitted"]
    return shrink(inp)


# least important first; inside a part the oldest (or the least urgent) goes first
DROP_ORDER = (("events", lambda x: x.get("kind") == "bilgi", 0), ("opened", None, 0),
              ("events", lambda x: x.get("kind") in ("talep", "revizyon"), 0), ("completed", lambda x: not x.get("late"), 0),
              ("jobs", None, 0), ("next_month_due", None, -1), ("events", None, 0), ("completed", None, 0),
              ("overdue", None, -1), ("promises", None, 0), ("decisions", None, 0))


def shrink(inp):
    """Bounded request (<= MAX_INPUT_BYTES): texts shrink first, then the oldest / least important items go."""
    for limit in (160, 80, 0):
        if bm._size(inp) <= MAX_INPUT_BYTES:
            break
        for e in inp["events"]:
            if limit:
                e["detail"] = e.get("detail", "")[:limit]
            else:
                e.pop("detail", None)
    for part, pick, end in DROP_ORDER:
        while bm._size(inp) > MAX_INPUT_BYTES:
            idx = [n for n, x in enumerate(inp[part]) if pick is None or pick(x)]
            if not idx:
                break
            inp[part].pop(idx[end])
            inp["omitted"] += 1
    _need(bm._size(inp) <= MAX_INPUT_BYTES, "monthly_report_input_too_large")
    return inp


def report_source(ops, load_key, catalog, *, routing=None, client=None):
    """compose(request) -> model output; one ModelGate chance per exact request (internal cap, budget gate)."""
    import model as base
    import model_routing
    from v11_chain_live import budget_gate
    from v11_chain_models import ModelGate, internal_cap_microdollars, key_for, reservation_for, source_digest
    from v11_trend_search import route_fingerprint
    route = (routing if routing is not None else model_routing.load()).route(ROLE)
    _need(route.provider == "anthropic", "monthly_report_route_not_claude")
    cap = internal_cap_microdollars(catalog)
    hold = reservation_for(route)
    budget_ok = budget_gate(ops, hold, cap)

    def compose(request):
        _need(budget_ok(), "model_budget_unavailable")
        gate = ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)

        def call():
            try:
                system = PROMPT.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise MonthlyReportError("monthly_report_prompt_unavailable") from None
            key = key_for(load_key, "anthropic")
            try:
                return base.anthropic_json(route, system, request, SCHEMA, api_key=key, client=client,
                                           validator=read_output)
            finally:
                del key
        return gate.once(ROLE, source_digest(ROLE, request, route_fingerprint(route, PROMPT)), call,
                         provider="anthropic", hold=hold)
    compose.budget_ok = budget_ok
    compose.timeout_seconds = route.timeout_seconds
    return compose


def _numbers_ok(text, cited):
    """Every digit run in the text must appear in the cited sources (no invented numbers)."""
    blob = json.dumps(cited, ensure_ascii=False)
    return all(d in blob for d in DIGITS.findall(text))


def sourced(output, request):
    """The model's sections for one brand -> ({section: [line]}, dropped_lines, dropped_urls). Nothing is repaired
    or invented: a line needs known refs, numbers found in those refs and (if any) a url of those refs."""
    items = {x["ref"]: x for p in CAPS for x in request[p]}
    items[NUMBERS_REF] = request["numbers"]
    out, dropped, dropped_urls, seen = {}, 0, 0, set()
    for section, _, limit in SECTIONS:
        rows = (output or {}).get(section)
        out[section] = []
        for r in rows if isinstance(rows, list) else []:
            refs = [x for x in (r.get("refs") or []) if isinstance(x, str) and x in items] if isinstance(r, dict) else []
            text = _line(URL_IN_TEXT.sub("", r.get("text") or ""), 260) if refs and isinstance(r.get("text"), str) \
                else ""
            cited = [items[x] for x in refs]
            ok = text and text.casefold() not in seen and len(out[section]) < limit and _numbers_ok(text, cited)
            if section == "promises":
                ok = ok and r.get("status") in PROMISE_STATES
            if not ok:
                dropped += 1
                continue
            seen.add(text.casefold())
            line = {"text": text, "url": None}
            if section not in ("summary", "suggestions"):
                own = [c.get("url") for c in cited if isinstance(c, dict) and c.get("url")]
                url = r.get("url")
                if url is not None and url not in own:
                    dropped_urls += 1
                    url = None
                line["url"] = url if url is not None else (own[0] if own else None)
            elif r.get("url") is not None:
                dropped_urls += 1  # summary / suggestion lines carry no link
            if section == "promises":
                line["status"] = r["status"]
            out[section].append(line)
    return out, dropped, dropped_urls


def _more(n, noun="kayıt"):
    return f" (+{n} {noun} daha)" if n > 0 else ""


def fallback_sections(b, first):
    """Deterministic sections straight from the items (no model): never invented, each with its item's url."""
    n = b["numbers"]
    ev = b["events"]
    summary = [{"text": f"{month_name(first)} boyunca marka hafızasına {n['events_total']} olay kaydedildi; "
                        f"Basecamp'te {n['todos_completed']} iş tamamlandı, ay sonunda {n['overdue_at_month_end']} iş "
                        "gecikmiş durumdaydı.", "url": None}]
    done = [e for e in ev if e["kind"] == "teslim"][::-1]
    highlights = [{"text": e["title"], "url": e.get("url")} for e in done[:3]]
    highlights += [{"text": f"Tamamlandı: {c['title']}" + (" (gecikmeli)" if c.get("late") else ""), "url": c.get("url")}
                   for c in b["completed"][::-1][:6 - len(highlights)]]
    decisions = [{"text": e["title"], "url": e.get("url")} for e in ev if e["kind"] in ("onay", "karar")][::-1][:3]
    decisions += [{"text": f"{d['what']} ({d['date']})", "url": None} for d in b["decisions"][::-1][:5 - len(decisions)]]
    promises = [{"text": e["title"], "url": e.get("url"), "status": "belirsiz"} for e in ev if e["kind"] == "soz"][::-1][:3]
    promises += [{"text": p["what"] + (f" (teslim: {p['due']})" if p.get("due") else ""), "url": None,
                  "status": "belirsiz"} for p in b["promises"][::-1][:5 - len(promises)]]
    risks = [{"text": e["title"], "url": e.get("url")} for e in ev if e["kind"] == "risk"][::-1][:3]
    for t in b["overdue"][:5 - len(risks)]:
        risks.append({"text": f"“{t['title']}” ay sonunda {t['days_overdue']} gün gecikmişti"
                              + ("" if t.get("open_now") else ", sonra tamamlandı"), "url": t.get("url")})
    return {"summary": summary, "highlights": highlights, "decisions": decisions, "promises": promises,
            "risks": risks, "suggestions": []}


def compose_brand(b, first, last, model):
    """-> (sections, stats). One model call; a failure falls back to the items (marked)."""
    stats = {"model": "not_called", "model_calls": 0, "dropped_lines": 0, "dropped_urls": 0, "input_bytes": 0}
    request = model_input(b, first, last)
    stats["input_bytes"] = bm._size(request)
    stats["model_calls"] = 1
    try:
        output = model(request)
    except Exception as exc:
        stats["model"] = "model_failed:" + _code(exc)
        return fallback_sections(b, first), dict(stats, source="fallback")
    sections, dropped, dropped_urls = sourced(output, request)
    stats.update(model="ok", dropped_lines=dropped, dropped_urls=dropped_urls)
    if not any(sections.values()):
        return fallback_sections(b, first), dict(stats, source="fallback")
    return sections, dict(stats, source="model")


# ------------------------------------------------------------------ text
def _date(iso):
    d = date.fromisoformat(iso)
    return f"{d.day} {dd.MONTHS[d.month - 1]}"


def number_lines(n):
    """'Sayılarla ay', written by code from the computed numbers."""
    at_least = "" if n["basecamp_complete"] else "en az "
    kinds = ", ".join(f"{v} {KIND_LABELS[k]}" for k, v in n["events_by_kind"].items()) or "olay yok"
    out = [f"Basecamp: {at_least}{n['todos_opened']} iş açıldı, {at_least}{n['todos_completed']} iş tamamlandı "
           f"({n['completed_late']} tanesi teslim tarihinden sonra), ay sonunda {n['overdue_at_month_end']} iş gecikmişti",
           f"Müşteri mail işleri: {n['jobs_opened']} açıldı, {n['jobs_closed']} kapandı · Müşteri revize / ek notu: "
           f"{n['client_revisions']}",
           f"Marka hafızası: {n['events_total']} olay ({kinds}) · işlenen gün {n['memory_days']}"]
    if not n["basecamp_complete"]:
        out.append("Basecamp sayfaları ayın başına kadar okunamadı; açılan / tamamlanan sayıları eksik olabilir.")
    return out


def _period(first, last):
    return f"{first.day} – {last.day} {month_name(first)}"


def _row_text(section, r):
    head = f"[{r['status']}] " if section == "promises" else ("Öneri: " if section == "suggestions" else "")
    return head + r["text"]


def render(b, first, last, sections, *, fallback=False):
    lines = [report_title(b["label"], first), f"Dönem: {_period(first, last)} · {b['name']}"]
    if sections is None:
        lines += ["", f"{month_name(first)} ayında {b['name']} için kayda geçen bir gelişme yok: Basecamp'te ay içinde "
                      "açılan ya da tamamlanan iş, müşteri mail işi ve marka hafızasında olay bulunmuyor.",
                  f"Marka hafızasında işlenen gün: {b['numbers']['memory_days']}", "", FOOTER]
        return "\n".join(lines)
    if fallback:
        lines += ["", FALLBACK_NOTE]
    for n, (section, title, _) in enumerate(SECTIONS):
        if section == "highlights":
            lines += ["", dd._upper_tr(NUMBERS_TITLE)] + ["- " + x for x in number_lines(b["numbers"])]
        heading = dd._upper_tr(title) + (f" ({SUGGESTION_NOTE})" if section == "suggestions" else "")
        lines += ["", heading]
        rows = sections.get(section) or []
        if section == "summary":
            lines += [" ".join(r["text"] for r in rows)] if rows else [EMPTY_SECTION]
            continue
        lines += [f"- {_row_text(section, r)}" + (f" · {r['url']}" if r.get("url") else "") for r in rows] \
            or ["- " + EMPTY_SECTION]
    lines += ["", FOOTER]
    return "\n".join(lines)


def render_html(b, first, last, sections, *, fallback=False):
    out = [f'<div style="font-size:19px;font-weight:bold;margin:0 0 4px;">{dd._e(report_title(b["label"], first))}</div>',
           dd._p(dd._e(f"Dönem {_period(first, last)} · {b['name']}"), f"color:{dd.GREY};")]
    if sections is None:
        out.append(dd._p(dd._e(f"{month_name(first)} ayında {b['name']} için kayda geçen bir gelişme yok: Basecamp'te "
                               "ay içinde açılan ya da tamamlanan iş, müşteri mail işi ve marka hafızasında olay "
                               "bulunmuyor."), "margin-top:14px;"))
    else:
        if fallback:
            out.append(dd._p(dd._grey(FALLBACK_NOTE), "margin-top:10px;"))
        for section, title, _ in SECTIONS:
            if section == "highlights":
                out.append(dd._h2(NUMBERS_TITLE))
                out.append(dd._ul([dd._li(dd._e(x)) for x in number_lines(b["numbers"])]))
            out.append(dd._h2(title))
            if section == "suggestions":
                out.append(dd._p(dd._grey(SUGGESTION_NOTE)))
            rows = sections.get(section) or []
            if section == "summary":
                out.append(dd._p(dd._e(" ".join(r["text"] for r in rows)) if rows else dd._grey(EMPTY_SECTION)))
                continue
            out.append(dd._ul([dd._li(dd._a(r["url"], _row_text(section, r), False) if r.get("url")
                                      else dd._e(_row_text(section, r))) for r in rows])
                       if rows else dd._p(dd._grey(EMPTY_SECTION)))
    out.append(f'<div style="margin-top:26px;padding-top:8px;border-top:1px solid #e5e7eb;font-size:12px;'
               f'color:{dd.GREY};">{dd._e(FOOTER)}</div>')
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:0;">'
            f'<div style="font-family:{dd.FONT};font-size:14px;line-height:1.55;color:{dd.TEXT};width:100%;">'
            + "".join(out) + "</div></body></html>")


# ------------------------------------------------------------------ run
def _brands(catalog):
    found = bm.brands_of(catalog)
    return {k: found[k] for k in catalog["brand_routes"] if k in found}  # catalog order (WAT first)


def run_monthly_report(ops, *, config, catalog, reader_factory, recordings_factory, writer, model, clock,
                       preview=False, only=None, monotonic=time.monotonic, run_seconds=RUN_SECONDS):
    """One team mail per brand per month (first Istanbul weekday, 10:30-12:30), resumable brand by brand; or a
    preview (no send, no report record)."""
    from runtime_authority import deadline_timestamp, start_timestamp
    started = monotonic()
    report = {"status": None, "month": None, "brands": {}, "sent": 0, "already": 0, "model_calls": 0,
              "business_writes": 0}
    try:
        now = clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        _need(start_timestamp() <= now.timestamp() <= deadline_timestamp(), "authorization_expired")
        cfg = dd.validate_config(config)  # the digest's recipients: agency addresses only, else refused
        local = now.astimezone(ISTANBUL)
        first, last = report_month(local.date())
        report["month"] = f"{first:%Y-%m}"
        brands = _brands(catalog)
        if only is not None:
            _need(preview and only in brands, "monthly_report_brand_unknown")
            brands = {only: brands[only]}
        if preview:
            c = ops.control()
            _need(c["mode"] == "review_open", "control_stopped_or_stale")
            _need(not ops.budget_status()["overrun_hold"], "budget_overrun_hold")
        else:
            if not cfg["enabled"] or not cfg["recipients"]:
                return dict(report, status="no_new_work", reason="digest_disabled_or_no_recipients")
            if not in_window(local):
                return dict(report, status="no_new_work", reason="outside_monthly_report_window")
        recipients = list(cfg["recipients"])
        _need(preview or (recipients and all(dd.internal_address(r) for r in recipients)),
              "digest_recipient_not_internal_agency")
        reserve = max(CALL_RESERVE_SECONDS, getattr(model, "timeout_seconds", None) or 0)
        shared, jobs, previews, stop, unknown, failures = {}, None, [], None, [], []

        def open_listing():
            if "error" in shared:
                raise shared["error"]
            if "open" not in shared:
                try:
                    shared["open"] = open_items(reader_factory())
                except Exception as exc:
                    shared["error"] = exc
                    raise
            return shared["open"]
        for key, info in brands.items():
            rk = report_key(key, first)
            row = report["brands"].setdefault(key, {})
            if monotonic() - started + reserve > run_seconds:
                stop = stop or "run_time_budget_reached"
                row["status"] = "not_started"
                continue
            claim = None
            if not preview:
                claim = dd.claim_mail(ops, rk, {"kind": "monthly_report", "token": uuid.uuid4().hex, "brand": key,
                                                "month": report["month"], "date": local.date().isoformat()})
                if claim is None:
                    report["already"] += 1
                    row["status"] = "already_recorded"
                    continue
            try:
                if jobs is None:
                    jobs = bm.client_jobs(ops)
                if "recordings" not in shared:
                    shared["recordings"] = recordings_factory()
                b = gather(ops, key, info, first, last, opened_items=open_listing(),
                           recordings=shared["recordings"], account=shared["recordings"].account, jobs=jobs)
                sections, fallback, stats = None, False, {"source": "empty", "model_calls": 0}
                if has_material(b):
                    if stop in ("model_unavailable", "model_budget_unavailable") or model is None \
                            or (getattr(model, "budget_ok", None) is not None and not model.budget_ok()):
                        stop = stop or ("model_unavailable" if model is None else "model_budget_unavailable")
                        dd.release_claim(ops, claim, stop)
                        row["status"] = "waiting:" + stop
                        continue
                    sections, stats = compose_brand(b, first, last, model)
                    report["model_calls"] += stats["model_calls"]
                    fallback = stats["source"] == "fallback"
                stats["numbers"] = b["numbers"]
                stats["sections"] = {k: len(v) for k, v in (sections or {}).items()}
                body = render(b, first, last, sections, fallback=fallback)
                subject = report_title(b["label"], first)
                row.update(source=stats["source"])
                if preview:
                    previews.append({"brand": key, "key": rk, "subject": "[ÖNİZLEME] " + subject, "body": body,
                                     "stats": stats})
                    row["status"] = "preview"
                    continue
                payload = {"from": dd.HAMLE, "to": recipients, "cc": [], "subject": subject, "body": body,
                           "html": render_html(b, first, last, sections, fallback=fallback)}
                if dd.send_claimed(ops, claim, payload, stats, writer) is None:
                    unknown.append(key)
                    row["status"] = "unknown_no_retry"
                    continue
                report["sent"] += 1
                report["business_writes"] += 1
                row["status"] = "sent"
            except Exception as exc:
                reason = _code(exc)
                dd.release_claim(ops, claim, reason)
                failures.append(reason)
                row["status"] = "failed:" + reason
        if preview:
            return dict(report, status="monthly_report_preview", reports=previews, recipients=len(recipients),
                        reason=stop)
        if unknown:
            return dict(report, status="held", reason="monthly_report_outcome_unknown_no_retry", unknown=unknown,
                        automatic_retry_allowed=False)
        if report["sent"] + report["already"] == len(brands):
            if report["sent"] == 0:
                return dict(report, status="already_recorded", reason="monthly_report_already_sent")
            return dict(report, status="monthly_report_completed")
        return dict(report, status="monthly_report_partial", reason=stop or (failures[0] if failures else None))
    except Exception as exc:
        return dict(report, status="held", reason=_code(exc), automatic_retry_allowed=False)


# ------------------------------------------------------------------ live wiring
def run_worker_monthly_report(ops, secrets, *, clock=None, preview=False, only=None):
    """Entry for worker.py --mode monthly_report [--report-preview [--report-brand KEY]] (existing credential loaders
    only; Basecamp GET only; the mail goes through the daily digest's Gmail writer)."""
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
                            "Hamle Monthly Report (ahmet@gamechangers.com.tr)", MonthlyReportError)

    def reader_factory():
        valid = dd.validate_config(config)
        return dd.BasecampDigestReader(valid["account_id"], get=get, excluded=valid["excluded"])

    def recordings_factory():
        return bm.BasecampMemoryReader(dd.validate_config(config)["account_id"], get=get)
    writer = None
    if not preview:
        from v11_chain_mail import gmail_token_context
        writer = dd.DigestMailWriter(token_context=gmail_token_context(secrets), session_factory=provider._session)
    try:
        model = report_source(ops, {"anthropic": lambda: provider.anthropic_key(secrets)}, catalog)
    except Exception:
        model = None  # brands with material wait for a later run; empty brands still get their short mail
    try:
        return run_monthly_report(ops, config=config, catalog=catalog, reader_factory=reader_factory,
                                  recordings_factory=recordings_factory, writer=writer, model=model, clock=clock,
                                  preview=preview, only=only)
    finally:
        session.close()
