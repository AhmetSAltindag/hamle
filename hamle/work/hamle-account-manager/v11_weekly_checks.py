"""Haftalık ve günlük kontroller (SM & MT step 3, items 5, 10, 11; Ahmet 2026-10-17).

Code only: no model call, nothing written, nothing sent. One brand at a time, read from its Basecamp lists
(weekly-checks-config.json names them, e.g. social + motion) with GET only, plus Hamle's monthly content plan
record when one exists (content-plan-<brand>-<YYYYMM>), the special days calendar and the Platform Kural Defteri.

- Hafta başı: next Monday..Sunday. Empty days (with the plan's idea for that day when there is one), repeats
  (two open tasks with the same title), special days, shoot days.
- Yarın: tomorrow's tasks; a task with no assignee or no brief is flagged, and the rule book's lines for the
  platforms the title names are listed as reminders. A shoot tomorrow is a reminder of its own.

Off by default (enabled=false); the worker runs it only as --checks-preview, which returns the text.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import v11_content_plan as cp
from v11_nb_tracking import NbBasecamp

API = "https://3.basecampapi.com"
ISTANBUL = ZoneInfo("Europe/Istanbul")
CONFIG = Path(__file__).with_name("chain-profile") / "weekly-checks-config.json"
FALLBACK_CONFIG = Path(__file__).resolve().parents[2] / "outputs/gamechangers-internal/weekly-checks-config.json"
DAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
MIN_DATED_DAYS = 3
PLATFORM_WORDS = {"instagram": ("instagram", "ig", "reels", "reel", "story", "karusel", "carousel", "post"),
                  "linkedin": ("linkedin",), "tiktok": ("tiktok",)}
_need, _code = cp._need, cp._code


# ------------------------------------------------------------------ config
def load_config(path=None):
    p = Path(path) if path else (CONFIG if CONFIG.exists() else FALLBACK_CONFIG)
    return json.loads(p.read_text(encoding="utf-8"))


def validate_config(cfg):
    _need(isinstance(cfg, dict) and cfg.get("schema_version") == 1, "weekly_checks_config_invalid")
    _need(type(cfg.get("enabled")) is bool, "weekly_checks_enabled_invalid")
    words = cfg.get("shoot_words")
    _need(isinstance(words, list) and words and all(isinstance(w, str) and 2 <= len(w) <= 30 for w in words),
          "weekly_checks_shoot_words_invalid")
    brands = cfg.get("brands")
    _need(isinstance(brands, dict) and brands, "weekly_checks_brands_invalid")
    out = {}
    for key, b in brands.items():
        _need(re.fullmatch(r"[a-z_]{2,40}", key) and isinstance(b, dict), "weekly_checks_brand_invalid")
        lists = b.get("lists")
        _need(isinstance(lists, list) and lists and all(x in ("social", "project", "motion") for x in lists)
              and len(set(lists)) == len(lists), "weekly_checks_lists_invalid")
        out[key] = {"lists": list(lists)}
    return {"enabled": cfg["enabled"], "shoot_words": [_fold(w) for w in words], "brands": out}


# ------------------------------------------------------------------ dates and text
def _fold(text):
    return str(text or "").replace("İ", "i").lower()  # "ÇEKİM" -> "çekim"; "LinkedIn" stays "linkedin"


def next_week(today):
    monday = today + timedelta(days=7 - today.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def _d(day):
    return f"{day.day:02d}.{day.month:02d} {DAYS[day.weekday()]}"


def _norm_title(title):
    t = re.sub(r"\d{1,2}[./]\d{1,2}([./]\d{2,4})?", " ", _fold(title))
    return " ".join(re.findall(r"[^\W\d_]+", t))


def platforms_of(title):
    words = set(re.findall(r"[^\W_]+", _fold(title)))
    return [p for p, keys in PLATFORM_WORDS.items() if words & set(keys)]


# ------------------------------------------------------------------ Basecamp (GET only, the brand's lists)
class ChecksBasecamp(NbBasecamp):
    """Allow-listed GETs only: the to-dos of the named lists in one brand project."""
    def __init__(self, account_id, project_id, list_ids, *, send):
        super().__init__(account_id, send=send)
        b = rf"/{account_id}/buckets/{project_id}"
        self.allowed = {"GET": tuple(rf"{b}/todolists/{lid}/todos" for lid in list_ids)}


def route_lists(catalog, cfg, key):
    route = (catalog.get("brand_routes") or {}).get(key) or {}
    _need(route.get("verified") is True and route.get("hamle_member") is True, "weekly_checks_route_unverified")
    pid, lists = route.get("project_id"), route.get("lists") or {}
    ids = [lists.get(name) for name in cfg["brands"][key]["lists"]]
    _need(type(pid) is int and pid > 0 and all(type(x) is int and x > 0 for x in ids), "weekly_checks_route_incomplete")
    return pid, ids


def read_tasks(http, project_id, list_ids):
    out = []
    for lid in list_ids:
        for t in http.all(f"{API}/{http.account}/buckets/{project_id}/todolists/{lid}/todos.json"):
            if not isinstance(t, dict) or t.get("completed") is not False or t.get("status") not in (None, "active"):
                continue
            due = t.get("due_on")
            out.append({"id": t.get("id"), "list_id": lid, "title": cp._line(t.get("content") or t.get("title"), 110),
                        "due_on": due if isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) else None,
                        "assignees": sorted(cp._line(a.get("name"), 40) for a in t.get("assignees") or []
                                            if isinstance(a, dict) and a.get("name")),
                        "has_brief": bool(re.sub(r"<[^>]*>|\s", "", str(t.get("description") or "")))})
    return out


# ------------------------------------------------------------------ checks (pure)
def plan_ideas(records, days):
    """date -> idea from Hamle's monthly plan records (dict month 'YYYY-MM' -> record or None)."""
    want, out = {d.isoformat() for d in days}, {}
    for row in records.values():
        plan = row.get("plan") if isinstance(row, dict) else None
        for r in (plan or {}).get("days") or []:
            if isinstance(r, dict) and r.get("date") in want and r.get("topic"):
                out[r["date"]] = {"topic": cp._line(r["topic"], 110), "format": r.get("format"),
                                  "recommended": bool(r.get("recommended"))}
    return out


def is_shoot(task, words):
    t = _fold(task["title"])
    return any(w in t for w in words)


def week_check(tasks, days, ideas, cal, words):
    by_day = {d.isoformat(): [t for t in tasks if t["due_on"] == d.isoformat()] for d in days}
    posts = {k: [t for t in v if not is_shoot(t, words)] for k, v in by_day.items()}
    # Teams that keep the month plan in their own document (WAT Oct '26: "WM // Ekim '26 SM Planı", a Google Doc)
    # have few dated tasks; empty days are only reported once the calendar is known: Hamle's monthly plan covers
    # the week, or dated post tasks fill at least MIN_DATED_DAYS of it.
    known = bool(ideas) or sum(1 for v in posts.values() if v) >= MIN_DATED_DAYS
    empty = [{"date": k, "idea": ideas.get(k)} for k, v in posts.items() if not v] if known else []
    seen, repeats = {}, []
    for k in sorted(posts):
        for t in posts[k]:
            n = _norm_title(t["title"])
            if n and n in seen:
                repeats.append({"title": t["title"], "dates": [seen[n], k]})
            seen.setdefault(n, k)
    specials = [{"date": s["date"], "name": s["name"], "has_task": bool(posts.get(s["date"])) or not known}
                for s in cp.special_days(cal, days[0]) + (cp.special_days(cal, days[-1]) if days[-1].month != days[0].month
                                                           else []) if s["date"] in by_day]
    shoots = [{"date": k, "title": t["title"], "assignees": t["assignees"]}
              for k, v in by_day.items() for t in v if is_shoot(t, words)]
    return {"known": known, "empty": empty, "repeats": repeats, "specials": specials, "shoots": shoots,
            "posts": sum(len(v) for v in posts.values())}


def tomorrow_check(tasks, day, book, words):
    rules = (book or {}).get("rules") or []
    out = []
    for t in (x for x in tasks if x["due_on"] == day.isoformat()):
        shoot = is_shoot(t, words)
        flags = ([] if t["assignees"] else ["sorumlu yok"]) + ([] if t["has_brief"] or shoot else ["brief yok"])
        reminders = [] if shoot else [
            ("🟢 " if r.get("source_kind") == "official" else "🟡 ") + cp._line(r["rule"], 160)
            for p in platforms_of(t["title"]) for r in [x for x in rules if isinstance(x, dict)
                                                         and x.get("platform") == p and x.get("rule")][:2]]
        out.append({"title": t["title"], "shoot": shoot, "assignees": t["assignees"], "flags": flags,
                    "reminders": reminders})
    return out


def render(name, today, days, week, tomorrow):
    lines = [f"Hamle AI: {name} · haftalık ve günlük kontrol ({_d(today)})", "",
             f"GELECEK HAFTA ({_d(days[0])} - {_d(days[-1])}): {week['posts']} paylaşım işi"]
    if not week["known"]:
        lines.append("Bu haftanın paylaşımları ekibin aylık plan dokümanında; boş gün kontrolü Hamle'nin aylık planı "
                     "Basecamp'e geçince başlar.")
    elif week["empty"]:
        lines.append("Boş günler:")
        for e in week["empty"]:
            i = e["idea"]
            lines.append(f"  · {_d(date.fromisoformat(e['date']))}" + (
                f" · plandaki fikir: {'⭐ ' if i['recommended'] else ''}{i['topic']}" if i else ""))
    else:
        lines.append("Boş gün yok.")
    for r in week["repeats"]:
        lines.append(f"Tekrar olabilir: \"{r['title']}\" ({', '.join(_d(date.fromisoformat(x)) for x in r['dates'])})")
    for s in week["specials"]:
        lines.append(f"Özel gün: {_d(date.fromisoformat(s['date']))} {s['name']}"
                     + ("" if s["has_task"] else " · o gün için iş yok"))
    for s in week["shoots"]:
        lines.append(f"Çekim: {_d(date.fromisoformat(s['date']))} · {s['title']}"
                     + (f" · {', '.join(s['assignees'])}" if s["assignees"] else " · sorumlu yok"))
    tday = today + timedelta(days=1)
    lines += ["", f"YARIN ({_d(tday)})"]
    if not tomorrow:
        lines.append("Yarın için tarihli iş yok.")
    for t in tomorrow:
        head = ("🎬 Çekim hatırlatması: " if t["shoot"] else "· ") + t["title"]
        lines.append(head + (f" · {', '.join(t['assignees'])}" if t["assignees"] else ""))
        lines += [f"    🟡 {f}" for f in t["flags"]] + [f"    Kural: {r}" for r in t["reminders"]]
    lines += ["", "Kaynak: Basecamp görevleri (yalnız okuma), Hamle'nin aylık planı, özel günler takvimi, "
              "Platform Kural Defteri. Hamle bu kontrolde hiçbir şey yazmaz, göndermez."]
    return "\n".join(lines)


# ------------------------------------------------------------------ run
def run_weekly_checks(ops, *, config, catalog, cal, book_reader, http_factory, clock, brand=None, day=None):
    """Preview only: one brand's next-week and tomorrow checks as text. Never writes, never sends."""
    report = {"status": None, "model_calls": 0, "business_writes": 0}
    try:
        now = clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        cfg = validate_config(config)
        today = date.fromisoformat(day) if day else now.astimezone(ISTANBUL).date()
        key = brand or next(iter(cfg["brands"]))
        _need(key in cfg["brands"], "weekly_checks_brand_not_configured")
        name = ((catalog.get("brand_routes") or {}).get(key) or {}).get("brand_name") or key
        pid, lids = route_lists(catalog, cfg, key)
        tasks = read_tasks(http_factory(pid, lids), pid, lids)
        days = next_week(today)
        months = {d.replace(day=1) for d in days}
        records = {f"{m:%Y-%m}": ops._run(lambda tx, _, m=m: tx.get(cp.record_key(key, m)), allow_expired=True)
                   for m in months}
        week = week_check(tasks, days, plan_ideas(records, days), cal, cfg["shoot_words"])
        tomorrow = tomorrow_check(tasks, today + timedelta(days=1), book_reader(), cfg["shoot_words"])
        return dict(report, status="weekly_checks_preview", brand=key, week=f"{days[0]}", tasks=len(tasks),
                    calendar_known=week["known"],
                    empty_days=len(week["empty"]), repeats=len(week["repeats"]), special_days=len(week["specials"]),
                    shoots=len(week["shoots"]), tomorrow=len(tomorrow),
                    text=render(name, today, days, week, tomorrow))
    except Exception as exc:
        return dict(report, status="held", reason=_code(exc))


def run_worker_weekly_checks(ops, secrets, *, clock=None, brand=None, day=None):
    """Entry for worker.py --mode weekly_checks --checks-preview [--checks-brand KEY] [--checks-day YYYY-MM-DD]."""
    import basecamp_cloud_auth
    import cloud_providers as provider
    import v11_algorithm_watch as aw
    import v11_daily_digest as dd
    from v11_chain_live import _memo, load_catalog
    from v11_nb_tracking import session_send
    clock = clock or (lambda: datetime.now(timezone.utc))
    catalog = load_catalog()
    token = _memo(lambda: basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh))
    send = session_send(token, provider._session)

    def http(pid, lids):
        return ChecksBasecamp((catalog.get("basecamp") or {}).get("account_id"), pid, lids, send=send)

    def book():
        row = ops._run(lambda tx, _: tx.get(aw.RULES_KEY), allow_expired=True)
        return row if isinstance(row, dict) and row.get("kind") == "platform_rules" else aw.new_book(aw.load_seed())
    return run_weekly_checks(ops, config=load_config(), catalog=catalog, cal=dd.load_calendar(), book_reader=book,
                             http_factory=http, clock=clock, brand=brand, day=day)
