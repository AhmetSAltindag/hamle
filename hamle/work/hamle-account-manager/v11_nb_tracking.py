"""New Business takibi (Ahmet 2026-10-13 07:04Z: "Basecamp'te bu işi Hamle'nin takip etmesi için ne yapabiliriz?").

Ahmet's rules (2026-10-13 ~07:25Z): New Business is a Monday thing. Each brand of the sent
Monday digest (5 a week, with why-now, source and up to 2 "tahmini, doğrulanmamış" company
addresses) becomes one todo "NB // <Marka>" in the list Ahmet opened in New Biz ("Hamle
Öneriler", 10398528523), assigned to Cansu; his weekly umbrella todo stays as it is. The team
writes progress as a one-line comment and the next weekday digests read them (nb_follow_ups):
- the day after a todo opens with no activity: one grouped question to Cansu;
- a new comment: thanks (recorded as progress); "pas": not suggested again for 90 days (recorded
  as a no);
- activity, then 5 quiet days: a reminder asking to speed up; no activity at all for 7 days: a
  kind reminder; each once per quiet stretch;
- the todo no longer open: thanks once.
Hamle never closes a task or sets a date, and never writes to a brand (Ahmet: only after "GO").

Writes: Basecamp list 10398528523 in project 40704547 only (allow-listed URLs). Same rules as
the chain: durable record before every write, one attempt, readback, anything uncertain is
'unknown' and never retried. Global stop ('dur') blocks every write.
"""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
from html import escape
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

NB_PROJECT = 40704547
LIST_ID = 10398528523  # "Hamle Öneriler", opened by Ahmet 2026-10-13 (visible_to_clients false)
PREFIX = "NB // "
CANSU = 48793256
HAMLE_PERSON = 53358100
STATE_KEY = "nb-tracking"
PASS_DAYS, QUIET_DAYS, STALL_DAYS, MAX_NEW, MAX_READS, MAX_HISTORY = 90, 7, 5, 6, 15, 12
FIRST_STEP = "Pazarlama yöneticisine kısa bir tanışma maili ve 2-3 fikirlik bir ön sunum önerisi."
API = "https://3.basecampapi.com"
APP = "https://3.basecamp.com"
MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
          "Aralık")
PASS = re.compile(r"(?<![0-9a-zçğıöşü])pas(?![0-9a-zçğıöşü])")


class NbError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise NbError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _line(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def brand_key(name):
    return re.sub(r"[^0-9a-zçğıöşü]+", "", str(name or "").casefold())


def empty_state():
    return {"kind": "nb_tracking", "schema_version": 1, "list": None, "brands": {}}


def avoid_brands(state, day):
    """Brand names research must not suggest again: tracked (any state) or passed less than 90 days ago."""
    out = []
    for r in ((state or {}).get("brands") or {}).values():
        until = r.get("passed_until")
        if r.get("brand") and (until is None or until >= day.isoformat()):
            out.append(r["brand"])
    return out[:40]


def contact_text(contacts):
    """'info@x.com (kaynak: https://...), pazarlama@x.com (kalıp)' or ''."""
    parts = [f"{c['email']} ({'kaynak: ' + c['url'] if c.get('basis') == 'kaynakta' and c.get('url') else 'kalıp'})"
             for c in contacts or [] if isinstance(c, dict) and c.get("email")]
    return ", ".join(parts[:2])


def description(s, day):
    who = contact_text(s.get("contacts"))
    return (f"Neden şimdi: {_line(s['reason'], 400)}\nKaynak: {s['url']}\n"
            + (f"İletişim (tahmini, doğrulanmamış): {who}\n" if who else "")
            + f"İlk adım önerisi: {_line(s['first_step'], 240)}\n"
            f"Öneren: Hamle, {day.day} {MONTHS[day.month - 1]} {day.year} ({s['source']})\n\n"
            "Gelişmeyi bu görevin altına tek satır yorum olarak yazın (ör. \"aradık\", \"toplantı 20 Ekim\"). "
            "\"pas\" yazarsanız Hamle bu markayı 3 ay önermez. Görevi kapatmak sizde.")


def _html(text):
    return "<div>" + escape(text, quote=True).replace("\n", "<br>") + "</div>"


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "div", "p"):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def text_matches(actual_html, expected):
    """Basecamp may wrap or auto-link saved rich text: compare the visible words only."""
    if not isinstance(actual_html, str) or len(actual_html) > 32000:
        return False
    p = _Text()
    p.feed(actual_html)
    p.close()
    return " ".join("".join(p.parts).split()) == " ".join(expected.split())


def suggestions(stats):
    """The Monday New Business brands of one sent team digest -> todo inputs (deduplicated by brand)."""
    out, seen = [], set()
    for b in (stats or {}).get("new_business_brands") or []:
        if isinstance(b, dict) and b.get("brand") and str(b.get("url") or "").startswith("https://"):
            out.append({"brand": _line(b["brand"], 80), "reason": b.get("reason") or "", "url": b["url"],
                        "contacts": [c for c in b.get("contacts") or [] if isinstance(c, dict)][:2],
                        "first_step": FIRST_STEP, "source": "Pazartesi New Business listesi"})
    keep = []
    for s in out:
        key = brand_key(s["brand"])
        if len(key) >= 2 and key not in seen:
            seen.add(key)
            keep.append(s)
    return keep


# ------------------------------------------------------------------ morning read (digest follow-ups)
def _comment_id(c):
    return hashlib.sha256(f"{c.get('at')}|{c.get('by')}|{c.get('text')}".encode("utf-8")).hexdigest()[:16]


def _and(names):
    names = [n for n in names if n]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " ve " + names[-1] if names else ""


def nb_follow_ups(state, *, reader, items, day, limit=8, prev_day=None, when="dün"):
    """Tracked NB todos -> follow-up rows (icon, prefix, url, text, suffix) + effects to keep once
    the team digest is sent: {'seen': {key: comment id}, 'passes': [key], 'reminded': {key: last
    activity day}, 'done': [key], 'history': {key: [entry]}}. prev_day: the previous weekday
    (a todo opened then with no activity gets the grouped day-after question)."""
    rows, eff = [], {"seen": {}, "passes": [], "reminded": {}, "done": [], "history": {}}
    open_by_id = {t["id"]: t for t in items if t.get("kind") == "todo" and t.get("project_id") == NB_PROJECT}
    fresh_quiet, reads = [], 0

    def note(key, kind, text=""):
        eff["history"].setdefault(key, []).append({"day": day.isoformat(), "kind": kind, "text": _line(text, 160)})
    for key, r in sorted(((state or {}).get("brands") or {}).items(), key=lambda kv: kv[1].get("suggested_on") or ""):
        if len(rows) >= limit:
            break
        if r.get("state") != "ready" or r.get("done_on") or r.get("passed_until"):
            continue
        title = f"“{PREFIX}{r['brand']}”"
        t = open_by_id.get(r.get("todo_id"))
        if t is None:
            rows.append(("✅", "", r.get("app_url"), title, " artık açık değil, tamamlanmış olmalı. Teşekkürler @Cansu!"))
            eff["done"].append(key)
            note(key, "kapandı")
            continue
        if reads >= MAX_READS:
            continue
        reads += 1
        try:
            comments = [c for c in reader.last_comments(t, n=3) if not str(c.get("by") or "").startswith("Hamle")]
        except Exception:
            continue
        latest = comments[-1] if comments else None
        if latest is not None and _comment_id(latest) != r.get("seen_comment"):
            eff["seen"][key] = _comment_id(latest)
            if PASS.search(str(latest.get("text") or "").casefold()):
                rows.append(("👌", "", t["app_url"], title, " için pas dendi; bu markayı 3 ay önermeyeceğim."))
                eff["passes"].append(key)
                note(key, "olumsuz", latest.get("text"))
            else:
                rows.append(("👍", "", t["app_url"], title,
                             f": “{_line(latest.get('text'), 90)}” ({_line(latest.get('by'), 40)}). Teşekkürler, takipteyim."))
                note(key, "ilerleme", latest.get("text"))
            continue
        opened = str(t.get("created_on") or r.get("suggested_on") or "")[:10]
        if not comments and prev_day is not None and opened == prev_day.isoformat():
            fresh_quiet.append(r["brand"])
            continue
        last = max([str(c.get("at") or "")[:10] for c in comments if c.get("at")] + [opened])
        try:
            quiet = (day - date.fromisoformat(last)).days
        except ValueError:
            continue
        if r.get("reminded_for") == last:
            continue  # one reminder per quiet stretch
        if comments and quiet >= STALL_DAYS:
            rows.append(("", "@Cansu, ", t["app_url"], title,
                         f" son yorumdan bu yana {quiet} gündür sessiz; biraz hızlandıralım mı?"))
        elif not comments and quiet >= QUIET_DAYS:
            rows.append(("", "@Cansu, ", t["app_url"], title, f" {quiet} gündür sessiz; bir adım atalım mı?"))
        else:
            continue
        eff["reminded"][key] = last
        note(key, "hatırlatma")
    if fresh_quiet:
        rows.insert(0, ("", f"@Cansu, {when} açtığım New Business görevlerinde (", None, _and(fresh_quiet),
                        ") henüz hareket yok; bir adım attık mı?"))
    return rows[:limit], eff


# ------------------------------------------------------------------ Basecamp (project 40704547 only)
class NbBasecamp:
    """Allow-listed calls only. send(method, url, body, expected) -> (payload, headers)."""
    def __init__(self, account_id, *, send):
        _need(type(account_id) is int and account_id > 0, "nb_account_invalid")
        self.account, self.send = account_id, send
        b = rf"/{account_id}/buckets/{NB_PROJECT}"
        self.allowed = {"GET": (rf"{b}/todolists/{LIST_ID}", rf"{b}/todos/[1-9][0-9]*"),
                        "POST": (rf"{b}/todolists/{LIST_ID}/todos",)}

    def call(self, method, url, body=None, expected=200):
        p = urlsplit(url)
        _need(p.scheme == "https" and p.netloc == "3.basecampapi.com" and not p.fragment
              and any(re.fullmatch(x + r"\.json", p.path) for x in self.allowed.get(method, ())), "nb_url_out_of_scope")
        _need(not p.query or (method == "GET" and re.fullmatch(r"page=[1-9][0-9]{0,2}", p.query)), "nb_query_invalid")
        return self.send(method, url, body, expected)

    def one(self, url):
        payload, _ = self.call("GET", url)
        _need(isinstance(payload, dict), "nb_payload_invalid")
        return payload

    def all(self, url, max_pages=5):
        out, base = [], urlsplit(url).path
        for _ in range(max_pages):
            payload, headers = self.call("GET", url)
            _need(isinstance(payload, list), "nb_payload_invalid")
            out.extend(payload)
            link = {k.lower(): v for k, v in (headers or {}).items()}.get("link", "")
            m = re.search(r'<([^<>]+)>\s*;\s*rel="next"', link or "")
            if m is None:
                return out
            url = m[1]
            _need(urlsplit(url).path == base, "nb_link_out_of_scope")
        raise NbError("nb_pagination_unbounded")


def session_send(token_provider, session_factory):
    """Production transport: existing Basecamp OAuth token, no redirects, bounded body."""
    import cloud_providers as provider

    def send(method, url, body, expected):
        session, r = session_factory(), None
        try:
            r = session.request(method, url, headers={
                "Authorization": "Bearer " + provider._token(token_provider()), "Accept": "application/json",
                "Content-Type": "application/json", "User-Agent": "Hamle NB Tracking (ahmet@gamechangers.com.tr)"},
                data=None if body is None else provider._encode(body), timeout=(10, 30), allow_redirects=False,
                stream=True)
            _need(r.status_code == expected, "nb_response_unconfirmed")
            chunks, size = [], 0
            for chunk in r.iter_content(chunk_size=16384):
                size += len(chunk)
                _need(size <= 1_048_576, "nb_response_too_large")
                chunks.append(chunk)
            return provider._json(b"".join(chunks)), dict(r.headers)
        finally:
            if r is not None:
                r.close()
            session.close()
    return send


# ------------------------------------------------------------------ writes after the team digest is sent
class NbTracker:
    def __init__(self, ops, http):
        self.ops, self.http = ops, http
        self.base = f"{API}/{http.account}/buckets/{NB_PROJECT}"

    def state(self):
        return self.ops._run(lambda tx, _: tx.get(STATE_KEY), allow_expired=True) or empty_state()

    def _write(self, fn, *, fenced=True):
        def op(tx, now):
            if fenced:  # 'dur' / takeover: no new write starts
                c = self.ops._control(tx)
                self.ops._open(c, c["generation"], now)
            st = tx.get(STATE_KEY) or empty_state()
            out = fn(st, now)
            tx.put(STATE_KEY, st)
            return out
        return self.ops._run(op, allow_expired=not fenced)

    def apply(self, effects, day):
        """Yesterday's reading, kept only after that digest was really sent."""
        effects = effects if isinstance(effects, dict) else {}
        until = (day + timedelta(days=PASS_DAYS)).isoformat()

        def fn(st, now):
            n = 0
            brands = st["brands"]
            for field, name in (("seen", "seen_comment"), ("reminded", "reminded_for")):
                for key, value in (effects.get(field) or {}).items():
                    if key in brands:
                        brands[key][name] = value; n += 1
            for field, name, value in (("passes", "passed_until", until), ("done", "done_on", day.isoformat())):
                for key in effects.get(field) or []:
                    if key in brands:
                        brands[key][name] = value; n += 1
            for key, entries in (effects.get("history") or {}).items():
                if key in brands and isinstance(entries, list):
                    brands[key]["history"] = (list(brands[key].get("history") or []) + entries)[-MAX_HISTORY:]
            return n
        return self._write(fn, fenced=False)

    def ensure_list(self):
        """Ahmet's list, checked once: in New Biz, hidden from clients, active."""
        lst = self.state().get("list") or {}
        if lst.get("state") == "ready" and lst.get("id") == LIST_ID:
            return LIST_ID
        back = self.http.one(f"{self.base}/todolists/{LIST_ID}.json")
        _need(back.get("id") == LIST_ID and (back.get("bucket") or {}).get("id") == NB_PROJECT
              and back.get("visible_to_clients") is False and back.get("status") in (None, "active")
              and back.get("type") in (None, "Todolist"), "nb_list_unusable")
        self._write(lambda st, now: st.update(list={"state": "ready", "id": LIST_ID, "checked_at": now}))
        return LIST_ID

    def add(self, s, list_id, day):
        key, title, text = brand_key(s["brand"]), PREFIX + s["brand"], description(s, day)

        def begin(st, now):
            if key in st["brands"]:
                return False
            st["brands"][key] = {"brand": s["brand"], "state": "writing", "todo_id": None, "app_url": None,
                                 "suggested_on": day.isoformat(), "source": s["source"], "url": s["url"], "at": now}
            return True
        if not self._write(begin):
            return "already_tracked"
        try:
            row, _ = self.http.call("POST", f"{self.base}/todolists/{list_id}/todos.json",
                                    {"content": title, "description": _html(text), "assignee_ids": [CANSU],
                                     "completion_subscriber_ids": [], "notify": True}, 201)
            tid = row.get("id") if isinstance(row, dict) else None
            _need(type(tid) is int and tid > 0, "nb_todo_receipt_invalid")
            back = self.http.one(f"{self.base}/todos/{tid}.json")
            parent = back.get("parent") or {}
            _need(back.get("id") == tid and back.get("content") == title and parent.get("id") == list_id
                  and parent.get("type") == "Todolist" and (back.get("bucket") or {}).get("id") == NB_PROJECT
                  and (back.get("creator") or {}).get("id") == HAMLE_PERSON and back.get("visible_to_clients") is False
                  and sorted(x.get("id") for x in back.get("assignees") or [] if isinstance(x, dict)) == [CANSU]
                  and text_matches(back.get("description"), text), "nb_todo_readback_mismatch")
        except Exception as exc:
            reason = _code(exc)
            self._write(lambda st, now: st["brands"][key].update(state="unknown", reason=reason), fenced=False)
            return "unknown"
        app = back.get("app_url")
        if not (isinstance(app, str) and app.startswith(f"{APP}/{self.http.account}/buckets/{NB_PROJECT}/todos/")
                and len(app) <= 300 and not any(c in app for c in " <>\"'")):
            app = f"{APP}/{self.http.account}/buckets/{NB_PROJECT}/todos/{tid}"
        self._write(lambda st, now: st["brands"][key].update(state="ready", todo_id=tid, app_url=app), fenced=False)
        return "created"


def run_nb_tracking(ops, *, http, stats, day, earlier=()):
    """After the team digest is sent: keep this morning's reading effects, then open new NB todos.
    earlier: [(day, stats)] of this week's sent Monday digest, so its list is caught up if Monday's
    step did not run (first day live, a held write)."""
    report = {"status": "nb_tracking_completed", "created": 0, "already_tracked": 0, "unknown": 0, "effects": 0,
              "business_writes": 0}
    try:
        tracker = NbTracker(ops, http)
        report["effects"] = tracker.apply((stats or {}).get("nb"), day)
        tracked = tracker.state()["brands"]
        fresh, seen = [], set(tracked)
        for d, st in [(day, stats)] + [x for x in earlier or () if x and x[1]]:
            for s in suggestions(st):
                if brand_key(s["brand"]) not in seen:
                    seen.add(brand_key(s["brand"]))
                    fresh.append((d, s))
        if not fresh:
            return dict(report, status="no_new_work")
        list_id = tracker.ensure_list()
        for d, s in fresh[:MAX_NEW]:
            outcome = tracker.add(s, list_id, d)
            report["business_writes"] += outcome in ("created", "unknown")
            report[outcome] += 1
        return report
    except Exception as exc:
        return dict(report, status="held", reason=_code(exc))
