"""Hamle Platform Kural Defteri + Algoritma Nöbeti (Ahmet 2026-10-17, SM & MT plan step 1).

Every Sunday night (scheduler, config window) ONE Claude call with web search reads the platforms' own sources
(Instagram @creators / Mosseri, Meta Newsroom, TikTok Newsroom and Creator Academy, LinkedIn Engineering and Help,
YouTube Creator Insider and blog) and answers three questions: what changed for organic content this week, which
rules in the book are still true (re-checked every reverify_days), and which rules an official source now
contradicts. The code, not the model, decides what is kept:
- every URL must be https and in the call's own web search evidence;
- 🟢 when one of its URLs is on the config's official list, 🟡 when two different sites say it, otherwise dropped
  (🔴 söylenti, never used);
- a change is dated within the last 30 days; "Bunu müşteriye de göndermelisiniz!" only on a 🟢 change;
- media planning, ad buying, boosting and sponsored content are out of scope (Ahmet 2026-10-17) and dropped.

Monday's first team digest shows the card "Bu hafta algoritmada ne değişti" (digest_rows, read-only there). The
card goes to the team only; Hamle never sends it to a client.

Non-collision (Ahmet 2026-10-17): own module, own mode (worker.py --mode algorithm_watch), own config
(algorithm-watch-config.json, OFF by default), own model settings in that config (model-routing.json untouched),
own records:
- platform-rules: the versioned rule book {version, rules: [...], archived: [...]}; seeded on first run from
  platform-rules-seed.json (multi-source 🟡 rules researched 17 Oct). An archived rule keeps its dates.
- algorithm-watch-YYYYMMDD (the Monday the card is for): one run per week through a lease (running -> done); a done
  week is never run again, missed weeks are never replayed.
The paid call goes through the shared budget gate and ModelGate (one chance per request, never retried).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from urllib.parse import urlsplit
import uuid
from zoneinfo import ZoneInfo

import v11_daily_digest as dd
from v11_chain_models import _arr, _nstr, _obj, _str, key_for, source_digest
from v11_trend_search import _iso_day, _research_gate, anthropic_research, normalize_url, route_fingerprint

CONFIG_NAME = "algorithm-watch-config.json"
SEED_NAME = "platform-rules-seed.json"
PROMPT = Path(__file__).with_name("algorithm_watch_prompt.txt")
ROLE = "algorithm_watch"
RULES_KEY = "platform-rules"
LEASE_SECONDS = 1200
PLATFORMS = {"instagram": "Instagram", "tiktok": "TikTok", "linkedin": "LinkedIn", "youtube": "YouTube",
             "facebook": "Facebook"}
CONFIDENCE = {"official": "🟢", "multi": "🟡"}
CLIENT_WARNING = "⚠️ Bunu müşteriye de göndermelisiniz!"
TITLE = "Bu hafta algoritmada ne değişti"
MAX_CHANGES, MAX_VERIFY, MAX_URLS, MAX_ACTIVE = 6, 20, 3, 80
CHANGE_DAYS = 30
# Gamechangers does not run media planning, ad buying or sponsored-content investment (Ahmet 2026-10-17).
OUT_OF_SCOPE = re.compile(r"\b(reklam\w*|sponsorlu\w*|sponsored|ads? manager|ücretli tanıtım\w*|medya planı\w*|"
                          r"media plan\w*|cpm|cpc|roas|teklif stratejisi)\b", re.I)
KEYS = {"week": lambda d: f"algorithm-watch-{d:%Y%m%d}"}


class AlgorithmWatchError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise AlgorithmWatchError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _hhmm(value):
    return isinstance(value, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) is not None


# ------------------------------------------------------------------ config
def load_config(path=None):
    return dd._load(CONFIG_NAME, path)


def load_seed(path=None):
    return dd._load(SEED_NAME, path)


def _prefixes(raw):
    """'host/path/' entries -> [(host, path)] (lower case; a bare host covers the whole site)."""
    out = []
    for entry in raw:
        _need(isinstance(entry, str) and re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}(/[a-z0-9@._/-]*)?", entry),
              "algorithm_official_source_invalid")
        host, _, path = entry.partition("/")
        out.append((host, "/" + path if path else ""))
    return out


def validate_config(cfg):
    import model_routing as mr
    _need(isinstance(cfg, dict) and cfg.get("schema_version") == 1 and type(cfg.get("enabled")) is bool,
          "algorithm_config_invalid")
    try:
        tz = ZoneInfo(cfg.get("timezone") or "")
    except Exception:
        raise AlgorithmWatchError("algorithm_timezone_invalid") from None
    win = cfg.get("run_window") or {}
    _need(win.get("weekday") in range(7) and _hhmm(win.get("start")) and _hhmm(win.get("end"))
          and win["start"] < win["end"], "algorithm_window_invalid")
    official = cfg.get("official_sources")
    _need(isinstance(official, dict) and official and set(official) <= set(PLATFORMS)
          and all(isinstance(v, list) and 0 < len(v) <= 12 for v in official.values()), "algorithm_sources_invalid")
    reverify = cfg.get("reverify_days")
    _need(type(reverify) is int and 30 <= reverify <= 180, "algorithm_reverify_invalid")
    m = cfg.get("model") or {}
    _need(set(m) == {"model", "effort", "max_tokens", "timeout_seconds", "tool_type", "web_search_max_uses"}
          and m["model"] in mr.ANTHROPIC_MODELS and m["effort"] in mr.EFFORTS
          and type(m["max_tokens"]) is int and 1024 <= m["max_tokens"] <= 16_000
          and type(m["timeout_seconds"]) is int and 30 <= m["timeout_seconds"] <= 900
          and m["tool_type"] in mr.WEB_SEARCH_TOOL_TYPES
          and type(m["web_search_max_uses"]) is int and 1 <= m["web_search_max_uses"] <= 10, "algorithm_model_invalid")
    route = mr.Route(ROLE, "anthropic", m["model"], m["effort"], m["max_tokens"], m["timeout_seconds"], None,
                     {"type": m["tool_type"], "max_uses": m["web_search_max_uses"],
                      "fee_microdollars": mr.WEB_SEARCH_FEE_MICRODOLLARS})
    return {"enabled": cfg["enabled"], "tz": tz, "weekday": win["weekday"], "window": (win["start"], win["end"]),
            "official": {p: _prefixes(v) for p, v in official.items()}, "platforms": sorted(official),
            "reverify_days": reverify, "route": route}


def in_window(local, cfg):
    lo, hi = (int(v[:2]) * 60 + int(v[3:]) for v in cfg["window"])
    return local.weekday() == cfg["weekday"] and lo <= local.hour * 60 + local.minute <= hi


def card_monday(day):
    """The Monday whose digest shows this run's card: the next Monday (a Monday run is for that same day)."""
    return day + timedelta(days=(7 - day.weekday()) % 7)


# ------------------------------------------------------------------ rule book
def validate_seed(seed):
    _need(isinstance(seed, dict) and seed.get("schema_version") == 1 and isinstance(seed.get("rules"), list),
          "algorithm_seed_invalid")
    rules, seen = [], set()
    for r in seed["rules"]:
        _need(isinstance(r, dict) and isinstance(r.get("id"), str) and r["id"] not in seen
              and r.get("platform") in PLATFORMS and isinstance(r.get("rule"), str) and 0 < len(r["rule"]) <= 240
              and r.get("source_kind") in CONFIDENCE and _iso_day(r.get("first_seen")) and _iso_day(r.get("last_verified"))
              and isinstance(r.get("source_urls"), list) and r["source_urls"]
              and all(normalize_url(u) for u in r["source_urls"]), "algorithm_seed_invalid")
        seen.add(r["id"])
        rules.append({k: r[k] for k in ("id", "platform", "rule", "source_kind", "first_seen", "last_verified")}
                     | {"source_urls": [normalize_url(u) for u in r["source_urls"]][:MAX_URLS], "status": "active"})
    return rules


def new_book(seed):
    return {"kind": "platform_rules", "version": 1, "rules": validate_seed(seed), "archived": [],
            "seeded_from": SEED_NAME}


def due_rules(book, today, reverify_days):
    """Active rules not verified for reverify_days, oldest first (at most MAX_VERIFY a week)."""
    old = [r for r in book["rules"] if _iso_day(r["last_verified"]) <= today - timedelta(days=reverify_days)]
    return sorted(old, key=lambda r: (r["last_verified"], r["id"]))[:MAX_VERIFY]


# ------------------------------------------------------------------ the call
def schema(platforms):
    url = _arr(_str(300), MAX_URLS)
    return _obj({
        "changes": _arr(_obj({"platform": {"type": "string", "enum": list(platforms)}, "change": _str(240),
                              "date": _str(10), "urls": url, "impact": _str(240), "rule": _nstr(240),
                              "replaces": _nstr(40), "critical": {"type": "boolean"}}), MAX_CHANGES),
        "verified": _arr(_obj({"id": _str(40), "urls": url}), MAX_VERIFY),
        "retired": _arr(_obj({"id": _str(40), "reason": _str(240), "urls": url}), MAX_VERIFY)})


def request_input(book, today, since, cfg):
    due = due_rules(book, today, cfg["reverify_days"])
    return {"today": today.isoformat(), "since": since.isoformat(), "country": "TR", "platforms": cfg["platforms"],
            "official_sources": {p: [h + path for h, path in v] for p, v in cfg["official"].items()},
            "rules": [{"id": r["id"], "platform": r["platform"], "rule": r["rule"],
                       "confidence": CONFIDENCE[r["source_kind"]], "last_verified": r["last_verified"]}
                      for r in book["rules"]][:MAX_ACTIVE],
            "reverify": [r["id"] for r in due]}


def research_source(ops, load_key, catalog, cfg, *, client=None):
    """research(request_input) -> raw output with evidence; one chance per request (ModelGate, budget gate)."""
    route = cfg["route"]
    _, hold, budget_ok, gate = _research_gate(ops, catalog, route)

    def research(inp):
        _need(budget_ok(), "model_budget_unavailable")
        return gate().once(ROLE, source_digest(ROLE, inp, route_fingerprint(route, PROMPT)),
                           lambda: anthropic_research(route, PROMPT, inp, schema(cfg["platforms"]),
                                                      api_key=key_for(load_key, "anthropic"), client=client,
                                                      code="invalid_algorithm_watch_schema"),
                           provider=route.provider, hold=hold)
    research.hold = hold
    return research


# ------------------------------------------------------------------ what the code keeps
def official(url, platform, cfg):
    p = urlsplit(url)
    path = p.path.lower() + "/"
    return any(p.hostname == host and path.startswith(prefix) for host, prefix in cfg["official"].get(platform, ()))


def grade(urls, platform, evidence, cfg):
    """(source_kind, kept urls) or (None, []) for a rumour: 🟢 an official URL, 🟡 two different sites."""
    kept = []
    for u in urls or []:
        n = normalize_url(u)
        if n and n in evidence and n not in kept:
            kept.append(n)
    kept = kept[:MAX_URLS]
    if any(official(u, platform, cfg) for u in kept):
        return "official", sorted(kept, key=lambda u: not official(u, platform, cfg))
    if len({urlsplit(u).hostname.removeprefix("www.") for u in kept}) >= 2:
        return "multi", kept
    return None, []


def sourced(output, book, today, cfg):
    """The model's answer -> (changes, verified ids, retired [(id, reason, urls)], dropped count)."""
    evidence = set(output.get("evidence") or [])
    active = {r["id"]: r for r in book["rules"]}
    changes, dropped, seen = [], 0, set()
    for c in output.get("changes") or []:
        day = _iso_day(c.get("date"))
        kind, urls = grade(c.get("urls"), c.get("platform"), evidence, cfg)
        text = " ".join(str(c.get("change") or "").split())
        if (kind is None or day is None or not today - timedelta(days=CHANGE_DAYS) <= day <= today or not text
                or OUT_OF_SCOPE.search(text + " " + str(c.get("rule") or "")) or (c["platform"], text) in seen):
            dropped += 1
            continue
        seen.add((c["platform"], text))
        replaces = c.get("replaces") if c.get("replaces") in active and active[c["replaces"]]["platform"] == \
            c["platform"] else None
        changes.append({"platform": c["platform"], "change": text[:240], "date": day.isoformat(), "urls": urls,
                        "source_kind": kind, "impact": " ".join(str(c.get("impact") or "").split())[:240],
                        "rule": " ".join(str(c.get("rule") or "").split())[:240], "replaces": replaces,
                        "critical": bool(c.get("critical")) and kind == "official"})
    verified = []
    for v in output.get("verified") or []:
        r = active.get(v.get("id"))
        if r and v["id"] not in verified and grade(v.get("urls"), r["platform"], evidence, cfg)[0]:
            verified.append(v["id"])
    retired = []
    for v in output.get("retired") or []:
        r = active.get(v.get("id"))
        kind, urls = grade(v.get("urls"), r["platform"], evidence, cfg) if r else (None, [])
        if kind == "official" and v["id"] not in [x[0] for x in retired]:  # only the platform itself retires a rule
            retired.append((v["id"], " ".join(str(v.get("reason") or "").split())[:240], urls))
    return changes, verified, retired, dropped


def apply(book, changes, verified, retired, today):
    """A new book version: verified rules re-dated, retired and replaced rules archived, new rules added."""
    book = {**book, "rules": [dict(r) for r in book["rules"]], "archived": list(book.get("archived") or [])}
    stamp = today.isoformat()
    by_id = {r["id"]: r for r in book["rules"]}
    for rid in verified:
        by_id[rid]["last_verified"] = stamp
    gone = {rid: (reason, urls) for rid, reason, urls in retired}
    for c in changes:
        if c["replaces"]:
            gone.setdefault(c["replaces"], ("Yerine yeni kural geldi: " + (c["rule"] or c["change"]), c["urls"]))
    for rid, (reason, urls) in gone.items():
        r = by_id.pop(rid, None)
        if r:
            book["archived"].append(dict(r, status="archived", archived_at=stamp, archive_reason=reason,
                                         archive_urls=urls))
    added = []
    n = 0
    for c in changes:
        if not c["rule"]:
            continue
        n += 1
        rid = f"{c['platform'][:2]}-{today:%Y%m%d}-{n}"
        by_id[rid] = {"id": rid, "platform": c["platform"], "rule": c["rule"], "source_kind": c["source_kind"],
                      "first_seen": stamp, "last_verified": stamp, "source_urls": c["urls"], "status": "active"}
        c["new_rule"] = rid
        added.append(rid)
    book["rules"] = sorted(by_id.values(), key=lambda r: (r["platform"], r["id"]))
    book["archived"] = book["archived"][-200:]
    book["version"] = int(book.get("version") or 1) + 1
    book["updated_on"] = stamp
    return book, added, sorted(gone)


def affected_brands(cards, platform):
    """Brand names whose card names this platform as the main one (brand-cards.json main_platform)."""
    out = []
    for b in (cards or {}).get("brands") or []:
        if b.get("include_in_suggestions") and str(b.get("main_platform") or "").lower() == platform:
            out.append(b.get("brand"))
    return [b for b in out if b]


# ------------------------------------------------------------------ the digest card
def card(changes, book, verified, retired, cards):
    rows = []
    for c in changes:
        rows.append({"icon": CONFIDENCE[c["source_kind"]], "platform": PLATFORMS[c["platform"]], "text": c["change"],
                     "impact": c["impact"], "rule": c["rule"], "url": c["urls"][0] if c["urls"] else None,
                     "brands": affected_brands(cards, c["platform"]), "critical": c["critical"]})
    counts = f"Kural defterinde {len(book['rules'])} kural"
    counts += f"; bu hafta {len(verified)} kural yeniden doğrulandı" if verified else ""
    counts += f", {len(retired)} kural arşive gitti" if retired else ""
    line = (f"{len(rows)} değişiklik. " if rows else "Bu hafta resmi kaynaklarda organik içeriği etkileyen yeni bir "
            "değişiklik yok. ") + counts + "."
    return {"title": TITLE, "line": line, "rows": rows}


def digest_rows(row, monday):
    """For the week's first digest: the card of algorithm-watch-<monday>, or None (no record, not done)."""
    if not (isinstance(row, dict) and row.get("state") == "done" and row.get("week") == monday.isoformat()
            and isinstance(row.get("card"), dict)):
        return None
    c = row["card"]
    rows = [r for r in c.get("rows") or [] if isinstance(r, dict)][:MAX_CHANGES]
    return {"title": TITLE, "line": str(c.get("line") or "")[:300], "rows": rows}


def row_text(r):
    """One card row as plain text (the HTML digest uses the same words)."""
    text = f"{r.get('icon', '')} {r.get('platform')}: {r.get('text')}"
    if r.get("rule"):
        text += f" Yeni kural: {r['rule']}"
    if r.get("brands"):
        text += " Etkilenen: " + ", ".join(r["brands"]) + "."
    return text


# ------------------------------------------------------------------ run
class _Week:
    def __init__(self, ops, key):
        self.ops, self.key = ops, key

    def claim(self, token, fields):
        def op(tx, now):
            row, book = tx.get(self.key), tx.get(RULES_KEY)
            if isinstance(row, dict) and (row.get("state") == "done" or row.get("lease_until", 0) > now):
                return None
            new = dict(row or {}, **fields, key=self.key, kind="algorithm_watch", state="running", token=token,
                       claimed_at=now, lease_until=now + LEASE_SECONDS, runs=int((row or {}).get("runs") or 0) + 1)
            tx.put(self.key, new)
            return {"row": new, "book": book}
        return self.ops._run(op, allow_expired=True)

    def finish(self, token, book, **fields):
        def op(tx, now):
            row = tx.get(self.key)
            _need(isinstance(row, dict) and row.get("token") == token and row.get("state") == "running",
                  "algorithm_claim_lost")
            if book is not None:
                tx.put(RULES_KEY, dict(book, updated_at=now))
            row.update(fields, state="done", finished_at=now, lease_until=now)
            tx.put(self.key, row)
            return row
        return self.ops._run(op, allow_expired=True)

    def release(self, token, reason):
        def op(tx, now):
            row = tx.get(self.key)
            if isinstance(row, dict) and row.get("token") == token and row.get("state") == "running":
                row.update(lease_until=now, last_failure=reason)
                tx.put(self.key, row)
        try:
            self.ops._run(op, allow_expired=True)
        except Exception:
            pass


def _last_run(book, today):
    day = _iso_day((book or {}).get("updated_on"))
    return max(day, today - timedelta(days=CHANGE_DAYS)) if day and day < today else today - timedelta(days=7)


def run_algorithm_watch(ops, *, config, seed, cards, research_factory, clock, preview=False):
    """One Sunday-night watch (config window, flag on), or a preview (any time: the call runs, nothing is written).

    research_factory(cfg) -> research(request_input); built only after the gates, so a closed flag costs nothing."""
    report = {"status": None, "week": None, "model_calls": 0, "business_writes": 0}
    week = token = None
    try:
        now = clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        cfg = validate_config(config)
        local = now.astimezone(cfg["tz"])
        today = local.date()
        monday = card_monday(today)
        report["week"] = monday.isoformat()
        if preview:
            book = ops._run(lambda tx, _: tx.get(RULES_KEY), allow_expired=True)
        else:
            if not cfg["enabled"]:
                return dict(report, status="no_new_work", reason="algorithm_watch_disabled")
            if not in_window(local, cfg):
                return dict(report, status="no_new_work", reason="outside_algorithm_watch_window")
            week, token = _Week(ops, KEYS["week"](monday)), uuid.uuid4().hex
            claimed = week.claim(token, {"week": monday.isoformat(), "run_date": today.isoformat()})
            if claimed is None:
                return dict(report, status="already_recorded", reason="algorithm_watch_done_or_running")
            book = claimed["book"]
        seeded = not (isinstance(book, dict) and book.get("kind") == "platform_rules")
        if seeded:
            book = new_book(seed)
        inp = request_input(book, today, _last_run(None if seeded else book, today), cfg)
        output = research_factory(cfg)(inp)
        report["model_calls"] = 1
        changes, verified, retired, dropped = sourced(output, book, today, cfg)
        new, added, archived = apply(book, changes, verified, retired, today)
        result = card(changes, new, verified, archived, cards)
        facts = {"changes": len(changes), "dropped": dropped, "verified": len(verified), "added": added,
                 "archived": archived, "rules": len(new["rules"]), "seeded": seeded, "version": new["version"],
                 "critical": sum(1 for c in changes if c["critical"])}
        if preview:
            return dict(report, status="algorithm_watch_preview", card=result, changes=changes, facts=facts,
                        reverify=inp["reverify"])
        week.finish(token, new, card=result, changes=changes, facts=facts)
        return dict(report, status="algorithm_watch_completed", line=result["line"], **facts)
    except Exception as exc:
        if week is not None:
            week.release(token, _code(exc))
        return dict(report, status="held", reason=_code(exc), automatic_retry_allowed=False)


def run_worker_algorithm_watch(ops, secrets, *, clock=None, preview=False):
    """Entry for worker.py --mode algorithm_watch [--algorithm-preview]."""
    import cloud_providers as provider
    from v11_chain_live import load_catalog
    clock = clock or (lambda: datetime.now(timezone.utc))
    catalog = load_catalog()
    return run_algorithm_watch(
        ops, config=load_config(), seed=load_seed(), cards=dd.load_brand_cards(), clock=clock, preview=preview,
        research_factory=lambda cfg: research_source(ops, {"anthropic": lambda: provider.anthropic_key(secrets)},
                                                     catalog, cfg))
