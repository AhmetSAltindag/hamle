"""Marka hafızası (Ahmet 2026-10-15: Hamle every touchpoint of a brand in hand, structured and archived per brand).

worker.py --mode brand_memory (NON-default, no scheduler entry yet). For every catalog brand (brand_routes without
'gamechangers' and 'new_biz') and every Istanbul day not yet done:
1. Raw material of the day (read only):
   - Basecamp: the brand project's to-dos created / updated in the window and its comments written in the window,
     from the recordings endpoint (GET only, allowlisted path + query, no redirects, bounded pages and reads).
   - Mail: client mails to hamle@ (v11_client_watch.client_mail_reader: the verified ChainMailReader path) whose
     sender the contacts book matches to the brand. Subject, sender, date, the new part of the body (<= 1500
     chars), attachment names, thread id. The Gmail search reaches back at most MAIL_MAX_DAYS days.
   - Hamle's own records: the brand's client jobs and its Teams conversations (teams_context).
2. One model call per brand per window (role brand_memory, Claude Haiku; skipped when the window has nothing):
   dated events (talep / teslim / revizyon / onay / karar / söz / risk / bilgi) and profile updates. An event
   survives only with a date inside the window, a source that has raw items, people named in the items and a url
   copied from an item (else null); nothing is repaired or invented.
3. Store (operations store only): brand-memory-<brand>-<YYYYMM> (events + days_done, <= 55 KB, newest kept),
   brand-profile-<brand> (merged, deduplicated, capped), brand-memory-run-<YYYYMMDD> (lease, per-brand status).
   A day is done once (days_done); a failed model call leaves its days open for a later run.

Live run: the last LIVE_DAYS completed Istanbul days. Backfill (backfill_from, at most BACKFILL_MAX_DAYS back):
week windows that never cross a month, at most MAX_CALLS_PER_RUN model calls and run_seconds per run; the next run
continues. Same fences as every writer: global stop ('dur') or a new control generation and the budget overrun
hold stop every write; the internal model cap and the monthly budget gate every model call. Nothing is ever
written to Basecamp, Gmail or Teams. brand_memory_context() is the reader for prompts (Teams CoreContext).
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, timezone
import json
from pathlib import Path
import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from v11_daily_digest import API, APP, BasecampDigestReader, _plain

ISTANBUL = ZoneInfo("Europe/Istanbul")
PROMPT = Path(__file__).with_name("brand_memory_prompt.txt")
ROLE = "brand_memory"
EXCLUDED = frozenset(("gamechangers", "new_biz"))
SOURCES = ("mail", "basecamp", "teams", "hamle")
KINDS = ("talep", "teslim", "revizyon", "onay", "karar", "soz", "risk", "bilgi")
LIVE_DAYS = 3
BACKFILL_MAX_DAYS = 90
BACKFILL_WINDOW_DAYS = 7
MAIL_MAX_DAYS = 7          # the Gmail search reaches back this far (older backfill windows go without mail)
MAIL_MAX_THREADS = 20
MAX_CALLS_PER_RUN = 40
RUN_SECONDS = 1500
CALL_RESERVE_SECONDS = 150
LEASE_SECONDS = 1800
MAX_INPUT_BYTES = 15_000   # model.MAX_INPUT_BYTES is 16 000
MAX_MONTH_BYTES = 55_000   # the store refuses records over 60 KB
MAX_EVENTS_PER_WINDOW = 30
CONTEXT_EVENTS = 25
TEAMS_AGENCY_MENTIONS = False  # off: agency chats (Heads) must not reach brand chats through the memory (0168348)
CAPS = {"contacts": 25, "preferences": 20, "donts": 20, "open_promises": 20, "decisions": 30}
_FOLD = str.maketrans({"İ": "i", "I": "i", "ı": "i"})
DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class BrandMemoryError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise BrandMemoryError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _line(value, limit=300):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _s(value, limit):
    return _line(value, limit) if isinstance(value, str) and value.strip() else None


def _fold(text):
    """Turkish-safe case-insensitive form (İ/I/ı all fold to i)."""
    return " ".join(str(text or "").translate(_FOLD).casefold().split())


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _ts(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return d.timestamp() if d.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _local(ts):
    return datetime.fromtimestamp(ts, ISTANBUL).strftime("%Y-%m-%d %H:%M") if ts is not None else None


def day_start(d):
    return datetime.combine(d, dtime(0), ISTANBUL).timestamp()


def month_key(brand, d):
    return f"brand-memory-{brand}-{d:%Y%m}"


def profile_key(brand):
    return "brand-profile-" + brand


def run_key(d):
    return f"brand-memory-run-{d:%Y%m%d}"


def brands_of(catalog):
    """brand key -> {name, project_id}: the catalog brand_routes Hamle keeps a memory for."""
    out = {}
    for key, route in sorted((catalog.get("brand_routes") or {}).items()):
        if key in EXCLUDED or not isinstance(route, dict):
            continue
        _need(type(route.get("project_id")) is int and route["project_id"] > 0, "brand_memory_route_invalid")
        out[key] = {"name": _line(route.get("brand_name") or key, 80), "project_id": route["project_id"]}
    return out


def windows(days, size):
    """Consecutive pending days -> (first, last) windows of at most `size` days, never across a month."""
    out = []
    for d in sorted(days):
        if out and out[-1][1] + timedelta(days=1) == d and (d - out[-1][0]).days < size \
                and (d.year, d.month) == (out[-1][0].year, out[-1][0].month):
            out[-1] = (out[-1][0], d)
        else:
            out.append((d, d))
    return out


# ------------------------------------------------------------------ Basecamp (GET only)
class BasecampMemoryReader(BasecampDigestReader):
    """One project's recently touched to-dos / comments from /projects/recordings.json, newest update first.

    Same transport contract as BasecampDigestReader: get(url) -> (payload, headers); production uses the
    existing cloud OAuth token, GET only, no redirects, 1 MB page cap (run_worker_brand_memory).
    """
    TYPES = ("Todo", "Comment")
    QUERY = {"type", "bucket", "status", "sort", "direction", "page"}

    def __init__(self, account_id, *, get, max_pages=12, max_reads=300):
        super().__init__(account_id, get=get, max_pages=max_pages, max_reads=max_reads)

    def _check(self, url):
        p = urlsplit(url)
        _need(p.scheme == "https" and p.netloc == "3.basecampapi.com" and not p.fragment
              and p.path == f"/{self.account}/projects/recordings.json", "memory_url_out_of_scope")
        q = parse_qs(p.query, keep_blank_values=True)
        one = {k: v[0] for k, v in q.items() if len(v) == 1}
        _need(set(q) <= self.QUERY and len(one) == len(q) and one.get("type") in self.TYPES
              and re.fullmatch(r"[1-9][0-9]{0,11}", one.get("bucket", "")) and one.get("status") == "active"
              and one.get("sort") == "updated_at" and one.get("direction") == "desc"
              and re.fullmatch(r"[1-9][0-9]{0,3}", one.get("page", "1")), "memory_query_invalid")

    def recent(self, project_id, kind, since):
        """Rows updated at/after `since` (epoch) -> (rows, covered_from): covered_from is `since` when the pages
        reached it, else the oldest update seen (older windows are then not complete)."""
        _need(kind in self.TYPES and type(project_id) is int and project_id > 0, "memory_query_invalid")
        url = (f"{API}/{self.account}/projects/recordings.json?type={kind}&bucket={project_id}"
               "&status=active&sort=updated_at&direction=desc")
        rows, oldest = [], None
        for _ in range(self.max_pages):
            payload, headers = self._fetch(url)
            _need(isinstance(payload, list), "memory_payload_invalid")
            for r in payload:
                t = _ts(r.get("updated_at")) if isinstance(r, dict) else None
                if t is None:
                    continue
                if t < since:
                    return rows, since
                rows.append(r)
                oldest = t if oldest is None else min(oldest, t)
            nxt = self._next(headers)
            if nxt is None:
                return rows, since
            _need({k: v for k, v in parse_qs(urlsplit(nxt).query).items() if k != "page"}
                  == {k: v for k, v in parse_qs(urlsplit(url).query).items() if k != "page"}, "memory_link_out_of_scope")
            url = nxt
        return rows, (oldest if oldest is not None else since)


def _app_url(row, account, pid):
    url = row.get("app_url")
    return url if isinstance(url, str) and url.startswith(f"{APP}/{account}/buckets/{pid}/") and len(url) <= 300 \
        and not any(c in url for c in " <>\"'") else None


def _names(rows, limit=4):
    return [n for n in dict.fromkeys(_line(x.get("name"), 60) for x in rows or [] if isinstance(x, dict)) if n][:limit]


def basecamp_items(todos, comments, account, pid):
    out = []
    for r in todos:
        if not isinstance(r, dict) or r.get("type", "Todo") != "Todo":
            continue
        created, updated = _ts(r.get("created_at")), _ts(r.get("updated_at"))
        done_at = _ts(r.get("completed_at"))
        out.append({"source": "basecamp", "type": "todo", "at": _local(updated), "_t": [created, updated, done_at],
                    "title": _plain(r.get("title") or r.get("content"), 160),
                    "list": _plain((r.get("parent") or {}).get("title"), 80) or None,
                    "created_at": _local(created), "completed": r.get("completed") is True,
                    "completed_at": _local(done_at),
                    "due_on": r["due_on"] if isinstance(r.get("due_on"), str) and DAY.match(r["due_on"]) else None,
                    "assignees": _names(r.get("assignees")), "creator": _names([r.get("creator")])[:1] or None,
                    "url": _app_url(r, account, pid), "text": _plain(r.get("description"), 400) or None})
    for r in comments:
        if not isinstance(r, dict) or r.get("type", "Comment") != "Comment":
            continue
        created = _ts(r.get("created_at"))
        out.append({"source": "basecamp", "type": "comment", "at": _local(created), "_t": [created],
                    "on": _plain((r.get("parent") or {}).get("title"), 160) or None,
                    "by": (_names([r.get("creator")]) or [None])[0], "url": _app_url(r, account, pid),
                    "text": _plain(r.get("content"), 600)})
    return out


# ------------------------------------------------------------------ mail / Hamle's records
def mail_items(sources, book, brand):
    from v11_client_watch import latest_part
    out = []
    for m in sources or []:
        hit = book.match(m.get("sender")) if book is not None and isinstance(m, dict) else None
        t = _ts(m.get("received_at_ms") / 1000) if hit and type(m.get("received_at_ms")) is int else None
        if not hit or hit.get("test") or hit["brand_key"] != brand or t is None:
            continue
        out.append({"source": "mail", "type": "mail", "at": _local(t), "_t": [t], "from": hit["email"],
                    "from_name": _s(hit.get("name"), 80), "subject": _line(m.get("subject"), 200),
                    "text": latest_part(m.get("text"))[:1500],
                    "attachments": [_line(a.get("filename"), 120) for a in m.get("attachments") or []
                                    if isinstance(a, dict)][:10],
                    "thread_id": _s(m.get("thread_id"), 64), "url": None})
    return out


def client_jobs(ops):
    """Hamle's client jobs (v11_client_job index 'client-jobs'), read once per run."""
    def read(tx, _):
        rows = [tx.get(key) or {} for key in list((tx.get("client-jobs") or {}).get("keys") or [])[-200:]]
        return [j for j in rows if j.get("kind") == "client_job" and not j.get("test")]
    return ops._run(read, allow_expired=True)


def hamle_items(ops, brand, name, jobs):
    """The brand's client jobs and Teams messages (its own conversations; agency chats only if enabled)."""
    from v11_teams import teams_context
    out = []
    for j in (j for j in jobs if j.get("brand_key") == brand):
        stamps = [_ts(j.get(k)) for k in ("created_at", "updated_at", "opened_at", "closed_at")]
        out.append({"source": "hamle", "type": "job", "at": _local(stamps[1] or stamps[0]), "_t": stamps,
                    "title": _line(j.get("title_core") or j.get("title"), 150), "state": _s(j.get("state"), 30),
                    "due_on": j.get("due_on") if isinstance(j.get("due_on"), str) else None,
                    "who": _s(j.get("assignee_name"), 60), "client": _s(j.get("client_name"), 60),
                    "revisions": j.get("revisions") if type(j.get("revisions")) is int else None,
                    "url": _s(j.get("app_url"), 300)})
    msgs = teams_context(ops, brand, limit=300)
    if TEAMS_AGENCY_MENTIONS:
        pattern = re.compile(r"(?<!\w)" + re.escape(_fold(name)) + r"(?!\w)")
        msgs += [m for m in teams_context(ops, None, limit=300) if pattern.search(_fold(m.get("text")))]
    for m in msgs:
        t = _ts(m.get("received_at"))
        out.append({"source": "teams", "type": "message", "at": _local(t), "_t": [t],
                    "conversation": _s(m.get("conversation"), 120), "author": _s(m.get("author"), 60),
                    "text": _line(m.get("text"), 600), "files": [_line(f.get("name"), 120) for f in m.get("files") or []
                                                                 if isinstance(f, dict)][:5], "url": None})
    return out


def in_window(items, first, last):
    lo, hi = day_start(first), day_start(last + timedelta(days=1))
    rows = [i for i in items if any(t is not None and lo <= t < hi for t in i["_t"])]
    return sorted(rows, key=lambda i: i.get("at") or "")


# ------------------------------------------------------------------ model
def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _str(maximum):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _nstr(maximum):
    return {"type": ["string", "null"], "minLength": 1, "maxLength": maximum}


def _arr(items, maximum):
    return {"type": "array", "items": items, "maxItems": maximum}


SCHEMA = _obj({
    "events": _arr(_obj({"date": _str(10), "source": {"type": "string", "enum": list(SOURCES)},
                         "kind": {"type": "string", "enum": list(KINDS)}, "title": _str(120), "detail": _str(400),
                         "people": _arr(_str(60), 4), "url": _nstr(300)}), MAX_EVENTS_PER_WINDOW),
    "profile_updates": _obj({
        "contacts": _arr(_obj({"name": _str(80), "role": _nstr(80), "email": _nstr(120)}), 10),
        "preferences": _arr(_str(200), 10), "donts": _arr(_str(200), 10), "approval_flow": _nstr(300),
        "open_promises": _arr(_obj({"what": _str(200), "due": _nstr(40), "by": _nstr(80)}), 10),
        "decisions": _arr(_obj({"date": _str(10), "what": _str(200)}), 10)})})


def read_output(value):
    """Shape check only (the model's JSON); every field is checked against the raw items in sourced()."""
    _need(isinstance(value, dict) and isinstance(value.get("events"), list)
          and isinstance(value.get("profile_updates"), dict), "brand_memory_output_invalid")
    return value


def compact_profile(profile, *, last=10):
    p = profile or {}
    return {"contacts": list(p.get("contacts") or []), "preferences": list(p.get("preferences") or []),
            "donts": list(p.get("donts") or []), "approval_flow": p.get("approval_flow"),
            "open_promises": list(p.get("open_promises") or [])[-last:],
            "decisions": list(p.get("decisions") or [])[-last:]}


def model_input(brand, name, first, last, profile, items, mail_read=True):
    """Bounded request (<= MAX_INPUT_BYTES): texts shrink first, then the oldest items go (counted)."""
    inp = {"brand": {"key": brand, "name": name}, "window": {"from": first.isoformat(), "to": last.isoformat()},
           "mail": "okundu" if mail_read else "bu aralık için okunmadı", "profile": compact_profile(profile),
           "items": [{k: v for k, v in i.items() if k != "_t" and v not in (None, [], "")} for i in items],
           "omitted_items": 0}
    for limit in (800, 400, 200, 100):
        if _size(inp) <= MAX_INPUT_BYTES:
            break
        for i in inp["items"]:
            if "text" in i:
                i["text"] = i["text"][:limit]
    while _size(inp) > MAX_INPUT_BYTES and inp["items"]:
        inp["items"].pop(0)
        inp["omitted_items"] += 1
    _need(_size(inp) <= MAX_INPUT_BYTES, "brand_memory_input_too_large")
    return inp


def memory_source(ops, load_key, catalog, *, routing=None, client=None):
    """remember(request) -> model output; one ModelGate chance per exact request (internal cap, budget gate)."""
    import model as base
    import model_routing
    from v11_chain_live import budget_gate
    from v11_chain_models import ModelGate, internal_cap_microdollars, key_for, reservation_for, source_digest
    from v11_trend_search import route_fingerprint
    route = (routing if routing is not None else model_routing.load()).route(ROLE)
    _need(route.provider == "anthropic", "brand_memory_route_not_claude")
    cap = internal_cap_microdollars(catalog)
    hold = reservation_for(route)
    budget_ok = budget_gate(ops, hold, cap)

    def remember(request):
        _need(budget_ok(), "model_budget_unavailable")
        gate = ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)

        def call():
            try:
                system = PROMPT.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise BrandMemoryError("brand_memory_prompt_unavailable") from None
            key = key_for(load_key, "anthropic")
            try:
                return base.anthropic_json(route, system, request, SCHEMA, api_key=key, client=client,
                                           validator=read_output)
            finally:
                del key
        return gate.once(ROLE, source_digest(ROLE, request, route_fingerprint(route, PROMPT)), call,
                         provider="anthropic", hold=hold)
    remember.budget_ok = budget_ok
    remember.timeout_seconds = route.timeout_seconds
    return remember


def _people(items):
    out = set()
    for i in items:
        for n in (i.get("assignees") or []) + (i.get("creator") or []) + [i.get(k) for k in
                                                                         ("by", "author", "from_name", "who", "client")]:
            if isinstance(n, str) and n.strip():
                out.add(_fold(n))
    return out


def _known(name, people):
    f = _fold(name)
    return bool(f) and (f in people or any(p.split()[0] == f or p.startswith(f + " ") for p in people))


def sourced(output, items, first, last, profile):
    """Model output -> (events, profile updates) that the raw items carry; anything else is dropped, never repaired."""
    urls = {i["url"] for i in items if i.get("url")}
    sources = {i["source"] for i in items}
    people = _people(items)
    emails = {i["from"] for i in items if i.get("from")} | {c.get("email") for c in (profile or {}).get("contacts") or []
                                                            if c.get("email")}
    events, seen, dropped = [], set(), 0
    for e in output.get("events") or []:
        d = e.get("date") if isinstance(e, dict) and isinstance(e.get("date"), str) and DAY.match(e["date"]) else None
        ok = d is not None and first.isoformat() <= d <= last.isoformat() and e.get("source") in sources \
            and e.get("kind") in KINDS and _s(e.get("title"), 120)
        key = (d, e.get("source"), _fold(e.get("title"))) if ok else None
        if not ok or key in seen:
            dropped += 1
            continue
        seen.add(key)
        events.append({"date": d, "source": e["source"], "kind": e["kind"], "title": _s(e["title"], 120),
                       "detail": _s(e.get("detail"), 400) or "",
                       "people": [_line(p, 60) for p in e.get("people") or [] if isinstance(p, str)
                                  and _known(p, people)][:4],
                       "url": e.get("url") if e.get("url") in urls else None})
    u = output.get("profile_updates") or {}

    def strs(xs, limit):
        return [x for x in (_s(v, limit) for v in (xs if isinstance(xs, list) else [])) if x][:10]
    contacts = [{"name": _s(c["name"], 80), "role": _s(c.get("role"), 80),
                 "email": c["email"].strip().lower() if isinstance(c.get("email"), str)
                 and c["email"].strip().lower() in emails else None}
                for c in (u.get("contacts") or []) if isinstance(c, dict) and _s(c.get("name"), 80)
                and (_known(c["name"], people) or (isinstance(c.get("email"), str)
                                                   and c["email"].strip().lower() in emails))][:10]
    updates = {"contacts": contacts, "preferences": strs(u.get("preferences"), 200), "donts": strs(u.get("donts"), 200),
               "approval_flow": _s(u.get("approval_flow"), 300),
               "open_promises": [{"what": _s(p["what"], 200), "due": _s(p.get("due"), 40), "by": _s(p.get("by"), 80)}
                                 for p in u.get("open_promises") or [] if isinstance(p, dict) and _s(p.get("what"), 200)][:10],
               "decisions": [{"date": p["date"], "what": _s(p["what"], 200)} for p in u.get("decisions") or []
                             if isinstance(p, dict) and isinstance(p.get("date"), str) and DAY.match(p["date"])
                             and p["date"] <= last.isoformat() and _s(p.get("what"), 200)][:10]}
    return events[:MAX_EVENTS_PER_WINDOW], updates, dropped


# ------------------------------------------------------------------ records
def merge_profile(profile, updates, brand, name, now):
    """Deduplicated (Turkish-safe, case-insensitive), capped; newest kept."""
    p = dict(profile or {"kind": "brand_profile", "brand": brand})
    p["name"] = name

    def strings(old, new, cap):
        out = list(old or [])
        folded = {_fold(x) for x in out}
        for x in new:
            if _fold(x) not in folded:
                out.append(x)
                folded.add(_fold(x))
        return out[-cap:]
    contacts = [dict(c) for c in p.get("contacts") or []]
    for c in updates.get("contacts") or []:
        hit = next((o for o in contacts if (c["email"] and o.get("email") == c["email"])
                    or _fold(o.get("name")) == _fold(c["name"])), None)
        if hit is None:
            contacts.append(dict(c))
        else:
            hit.update({k: v for k, v in c.items() if v})
    p["contacts"] = contacts[-CAPS["contacts"]:]
    p["preferences"] = strings(p.get("preferences"), updates.get("preferences") or [], CAPS["preferences"])
    p["donts"] = strings(p.get("donts"), updates.get("donts") or [], CAPS["donts"])
    if updates.get("approval_flow"):
        p["approval_flow"] = updates["approval_flow"]
    p.setdefault("approval_flow", None)
    promises = [dict(x) for x in p.get("open_promises") or []]
    for x in updates.get("open_promises") or []:
        if all(_fold(o.get("what")) != _fold(x["what"]) for o in promises):
            promises.append(dict(x))
    p["open_promises"] = promises[-CAPS["open_promises"]:]
    decisions = [dict(x) for x in p.get("decisions") or []]
    for x in updates.get("decisions") or []:
        if all((o.get("date"), _fold(o.get("what"))) != (x["date"], _fold(x["what"])) for o in decisions):
            decisions.append(dict(x))
    p["decisions"] = sorted(decisions, key=lambda x: x["date"])[-CAPS["decisions"]:]
    p["updated_at"] = now
    while _size(p) > MAX_MONTH_BYTES and (p["decisions"] or p["open_promises"]):
        (p["decisions"] or p["open_promises"]).pop(0)
    return p


def empty_month(brand, d):
    return {"kind": "brand_memory_month", "brand": brand, "month": f"{d:%Y-%m}", "events": [], "days_done": [],
            "truncated": 0}


def add_events(month, events, days):
    """Merge events (dedupe on date + source + title) and mark the days done; over MAX_MONTH_BYTES the oldest go."""
    m = dict(month)
    keyed = {(e["date"], e["source"], _fold(e["title"])): e for e in m.get("events") or []}
    for e in events:
        keyed.setdefault((e["date"], e["source"], _fold(e["title"])), e)
    m["events"] = sorted(keyed.values(), key=lambda e: (e["date"], e["source"], e["title"]))
    m["days_done"] = sorted(set(m.get("days_done") or []) | {d.isoformat() for d in days})
    m["truncated"] = int(m.get("truncated") or 0)
    while _size(m) > MAX_MONTH_BYTES and m["events"]:
        m["events"].pop(0)
        m["truncated"] += 1
    return m


def brand_memory_context(ops, brand_key, today, days=30, limit=CONTEXT_EVENTS, detail=400):
    """{'profile', 'events'} for prompts: the merged profile and the last `limit` events of the last `days` days."""
    since = today - timedelta(days=days)
    months, d = [], date(since.year, since.month, 1)
    while d <= today:
        months.append(month_key(brand_key, d))
        d = (d + timedelta(days=32)).replace(day=1)

    def read(tx, _):
        return tx.get(profile_key(brand_key)), [tx.get(k) for k in months]
    profile, rows = ops._run(read, allow_expired=True)
    events = [dict(e, detail=str(e.get("detail") or "")[:detail]) for r in rows if isinstance(r, dict)
              for e in r.get("events") or [] if since.isoformat() <= str(e.get("date")) <= today.isoformat()]
    events.sort(key=lambda e: e["date"])
    p = None
    if isinstance(profile, dict):
        p = dict(compact_profile(profile), updated=_local(_ts(profile.get("updated_at"))))
    return {"profile": p, "events": events[-limit:]}


# ------------------------------------------------------------------ the run
class _Store:
    def __init__(self, ops, today, generation):
        self.ops, self.key, self.generation = ops, run_key(today), generation
        self.today = today

    def _fence(self, tx, now):
        c = self.ops._control(tx)
        self.ops._open(c, self.generation, now)  # 'dur' or a new generation -> no write
        _need(not self.ops._budget(tx)["overrun_hold"], "budget_overrun_hold")

    def _empty(self):
        return {"key": self.key, "kind": "brand_memory_run", "day": self.today.isoformat(), "lease": None, "brands": {},
                "runs": 0}

    def claim(self, token, seconds, mode):
        def op(tx, now):
            c = self.ops._control(tx)
            self.ops._open(c, self.generation, now)
            _need(not self.ops._budget(tx)["overrun_hold"], "budget_overrun_hold")
            row = tx.get(self.key) or self._empty()
            lease = row.get("lease") or {}
            if lease.get("until", 0) > now and lease.get("token") != token:
                return False
            row.update(lease={"token": token, "until": now + seconds}, mode=mode, runs=int(row.get("runs") or 0) + 1,
                       updated_at=now)
            tx.put(self.key, row)
            return True
        return self.ops._run(op)

    def release(self, token, report):
        def op(tx, now):
            row = tx.get(self.key)
            if row is not None and (row.get("lease") or {}).get("token") == token:
                row["lease"]["until"] = now
                row.update(status=report["status"], reason=report.get("reason"), model_calls=report["model_calls"],
                           updated_at=now)
                tx.put(self.key, row)
        self.ops._run(op, allow_expired=True)

    def done_days(self, brands, days):
        keys = sorted({month_key(b, d) for b in brands for d in days})

        def read(tx, _):
            return {k: tx.get(k) for k in keys}
        rows = self.ops._run(read, allow_expired=True)
        return {b: {x for d in days for x in ((rows.get(month_key(b, d)) or {}).get("days_done") or [])}
                for b in brands}

    def profile(self, brand):
        return self.ops._run(lambda tx, _: tx.get(profile_key(brand)), allow_expired=True)

    def note(self, brand, **fields):
        def op(tx, now):
            self._fence(tx, now)
            row = tx.get(self.key) or self._empty()
            row["brands"].setdefault(brand, {}).update(fields, at=now)
            tx.put(self.key, row)
        self.ops._run(op)

    def save(self, brand, name, first, last, events, updates, status):
        days = [first + timedelta(days=n) for n in range((last - first).days + 1)]
        mkey, pkey = month_key(brand, first), profile_key(brand)

        def op(tx, now):
            self._fence(tx, now)
            month, profile, run = tx.get(mkey), tx.get(pkey), tx.get(self.key) or self._empty()
            tx.put(mkey, dict(add_events(month or empty_month(brand, first), events, days), key=mkey, updated_at=now))
            if updates is not None and any(updates.values()):
                tx.put(pkey, dict(merge_profile(profile, updates, brand, name, now), key=pkey))
            b = run["brands"].setdefault(brand, {})
            b.update(days=int(b.get("days") or 0) + len(days), events=int(b.get("events") or 0) + len(events),
                     last=last.isoformat(), model=status, at=now)
            tx.put(self.key, run)
        self.ops._run(op)


def run_brand_memory(ops, *, catalog, basecamp, model, clock, mail=None, book=None, backfill_from=None,
                     monotonic=time.monotonic, run_seconds=RUN_SECONDS, max_calls=MAX_CALLS_PER_RUN):
    """basecamp: BasecampMemoryReader-like (.recent, .account). mail(days) -> client mail sources of the last `days`
    days (None: no mail). model: memory_source(...) or None (windows with raw items then stay open)."""
    report = {"status": "brand_memory_completed", "mode": "backfill" if backfill_from else "live", "brands": {},
              "windows": 0, "empty": 0, "events": 0, "dropped": 0, "model_calls": 0, "already": 0,
              "business_writes": 0}
    store = token = None
    try:
        now = clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        today = now.astimezone(ISTANBUL).date()
        c = ops.control()
        _need(c["mode"] == "review_open", "control_stopped_or_stale")
        _need(not ops.budget_status()["overrun_hold"], "budget_overrun_hold")
        brands = brands_of(catalog)
        if backfill_from is None:
            days = [today - timedelta(days=n) for n in range(LIVE_DAYS, 0, -1)]
        else:
            start = backfill_from if isinstance(backfill_from, date) else date.fromisoformat(str(backfill_from))
            _need(today - timedelta(days=BACKFILL_MAX_DAYS) <= start < today, "brand_memory_backfill_out_of_range")
            days = [start + timedelta(days=n) for n in range((today - start).days)]
        store, token, started = _Store(ops, today, c["generation"]), uuid.uuid4().hex, monotonic()
        done = store.done_days(brands, days)
        pending = {b: windows([d for d in days if d.isoformat() not in done[b]],
                              BACKFILL_WINDOW_DAYS if backfill_from else 1) for b in brands}
        report["already"] = sum(1 for b in brands if not pending[b])
        if not any(pending.values()):
            return dict(report, status="no_new_work", reason="brand_memory_days_done")
        if not store.claim(token, LEASE_SECONDS, report["mode"]):
            store = None
            return dict(report, status="slot_already_claimed", reason="brand_memory_run_in_progress")
        mail_rows, mail_from, jobs = None, None, client_jobs(ops)
        if mail is not None:
            first_day = min(w[0][0] for w in pending.values() if w)
            span = min(MAIL_MAX_DAYS, (today - first_day).days + 1)
            try:
                mail_rows, mail_from = mail(span), today - timedelta(days=span - 1)
            except Exception as exc:
                report["mail"] = "unavailable:" + _code(exc)
        stop = None
        for brand, wins in pending.items():
            if not wins:
                continue
            info, row = brands[brand], report["brands"].setdefault(brand, {"windows": 0, "events": 0})
            if stop:
                row["status"] = stop
                continue
            try:
                since = day_start(wins[0][0])
                todos, t_from = basecamp.recent(info["project_id"], "Todo", since)
                comments, c_from = basecamp.recent(info["project_id"], "Comment", since)
                covered = max(t_from, c_from)
                items = basecamp_items(todos, comments, basecamp.account, info["project_id"]) \
                    + mail_items(mail_rows, book, brand) + hamle_items(ops, brand, info["name"], jobs)
            except Exception as exc:
                row["status"] = "read_failed:" + _code(exc)
                store.note(brand, status=row["status"])
                continue
            for first, last in wins:
                if day_start(first) < covered:
                    row["status"] = "basecamp_truncated"  # older pages not reached: never marked done on a gap
                    continue
                raw = in_window(items, first, last)
                if not raw:
                    store.save(brand, info["name"], first, last, [], None, "skipped_empty")
                    report["empty"] += 1
                    row["windows"] += 1
                    continue
                if model is None or report["model_calls"] >= max_calls \
                        or monotonic() - started + CALL_RESERVE_SECONDS > run_seconds:
                    stop = "model_unavailable" if model is None else "run_budget_reached"
                    row["status"] = stop
                    break
                if getattr(model, "budget_ok", None) is not None and not model.budget_ok():
                    stop = row["status"] = "model_budget_unavailable"
                    break
                profile = store.profile(brand)
                request = model_input(brand, info["name"], first, last, profile, raw,
                                      mail_rows is not None and mail_from is not None and first >= mail_from)
                report["model_calls"] += 1
                try:
                    output = model(request)
                except Exception as exc:
                    row["status"] = "model_failed:" + _code(exc)  # the days stay open; nothing invented
                    store.note(brand, status=row["status"])
                    continue
                events, updates, dropped = sourced(output, raw, first, last, profile)
                store.save(brand, info["name"], first, last, events, updates, "ok")
                report["events"] += len(events)
                report["dropped"] += dropped
                row["windows"] += 1
                row["events"] += len(events)
            report["windows"] += row["windows"]
        if stop:
            report["status"], report["reason"] = "brand_memory_partial", stop
        else:
            failed = sorted(b for b, r in report["brands"].items()
                            if str(r.get("status") or "").startswith(("read_failed", "model_failed")))
            if failed:  # their days stay open; say so instead of "completed"
                report["status"], report["reason"] = "brand_memory_partial", "brands_failed:" + ",".join(failed)
        return report
    except Exception as exc:
        return dict(report, status="held", reason=_code(exc))
    finally:
        if store is not None and token is not None:
            try:
                store.release(token, report)
            except Exception:
                pass


# ------------------------------------------------------------------ live wiring
# ------------------------------------------------------------------ one Basecamp GET (shared by memory / weekly / monthly)
RATE_LIMIT_TRIES = 4
RATE_LIMIT_WAIT_MAX = 30
MAX_PAGE_BYTES = 8 * 1_048_576  # WAT has pages over 1 MB (long HTML comments); paging still follows the Link header


def url_kind(url):
    """'recordings', 'todos', ... : which Basecamp endpoint a page came from (for the reason, never the full url)."""
    path = urlsplit(url).path.rstrip("/")
    return re.sub(r"[^a-z_]", "", path.rsplit("/", 1)[-1].split(".")[0].lower())[:30] or "page"


def basecamp_get(session, url, authorization, agent, error, *, sleep=time.sleep):
    """GET-only, no redirects, 8 MB page cap. 429: wait Retry-After (max 30 s) and try again, up to 4 tries.
    Any other non-200 raises error('basecamp_http_<status>') so the reason shows which status it was."""
    for attempt in range(RATE_LIMIT_TRIES):
        r = None
        try:
            r = session.request("GET", url, headers={"Authorization": authorization, "User-Agent": agent,
                                                      "Accept": "application/json"},
                                timeout=(5, 15), allow_redirects=False, stream=True)
            if r.status_code == 429 and attempt + 1 < RATE_LIMIT_TRIES:
                try:
                    wait = int(r.headers.get("Retry-After") or 10)
                except (TypeError, ValueError):
                    wait = 10
                wait = min(max(wait, 1), RATE_LIMIT_WAIT_MAX)
                r.close(); r = None
                sleep(wait)
                continue
            if r.status_code != 200:
                raise error("basecamp_rate_limited" if r.status_code == 429 else "basecamp_http_%d" % r.status_code)
            chunks, size = [], 0
            for chunk in r.iter_content(chunk_size=16384):
                size += len(chunk)
                if size > MAX_PAGE_BYTES:
                    raise error("basecamp_page_too_large:" + url_kind(url))
                chunks.append(chunk)
            import cloud_providers as provider
            return provider._json(b"".join(chunks)), dict(r.headers)
        except error:
            raise
        except Exception:
            raise error("basecamp_read_failed") from None
        finally:
            if r is not None:
                r.close()


def run_worker_brand_memory(ops, secrets, *, clock=None, backfill_from=None):
    """Entry for worker.py --mode brand_memory (existing credential loaders only; Basecamp and Gmail read only)."""
    import basecamp_cloud_auth
    import cloud_providers as provider
    from v11_chain_live import _memo, load_catalog
    clock = clock or (lambda: datetime.now(timezone.utc))
    catalog = load_catalog()
    token = _memo(lambda: basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh))
    session = provider._session()

    def get(url):
        return basecamp_get(session, url, "Bearer " + provider._token(token()),
                            "Hamle Brand Memory (ahmet@gamechangers.com.tr)", BrandMemoryError)
    book = mail = None
    try:
        from v11_client_watch import client_mail_reader, load_contacts, validate_contacts
        from v11_daily_digest import load_brand_cards
        book = validate_contacts(load_contacts(ops), catalog, load_brand_cards())
        gmail = provider.GmailSecretStore(secrets)

        def mail(days):
            return client_mail_reader(gmail, book, {"max_threads": MAIL_MAX_THREADS, "newer_than_days": days})()
    except Exception:
        book = mail = None  # no contacts book: the memory is built without mail
    try:
        model = memory_source(ops, {"anthropic": lambda: provider.anthropic_key(secrets)}, catalog)
    except Exception:
        model = None  # windows with raw material stay open for a later run
    try:
        return run_brand_memory(ops, catalog=catalog, basecamp=BasecampMemoryReader(catalog["basecamp"]["account_id"],
                                                                                   get=get),
                                model=model, clock=clock, mail=mail, book=book, backfill_from=backfill_from)
    finally:
        session.close()
