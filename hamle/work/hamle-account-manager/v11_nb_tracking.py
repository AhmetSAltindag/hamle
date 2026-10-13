"""New Business takibi (Ahmet 2026-10-13 07:04Z: "Basecamp'te bu işi Hamle'nin takip etmesi için ne yapabiliriz?").

Each New Business suggestion of a SENT team digest (the Monday research brands and a
new-business fırsat) becomes one todo "NB // <Marka>" in the New Biz project's "Hamle Önerileri"
list, assigned to Cansu, with why-now, source and a first step. The team writes progress as a
one-line comment; the next weekday digest reads them (nb_follow_ups): thanks for a new comment,
"pas" -> the brand is not suggested again for 90 days, one reminder after 7 quiet days, thanks
once when the todo is no longer open. Hamle never closes a task or sets a date.

Writes: Basecamp project 40704547 only (allow-listed URLs): the list once (found by its exact
name, else created), then todos. Same rules as the chain: durable record before every write,
one attempt, readback, anything uncertain is 'unknown' and never retried. Global stop ('dur')
blocks every write.
"""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
from html import escape
from html.parser import HTMLParser
import re
from urllib.parse import urlsplit

NB_PROJECT = 40704547
LIST_NAME = "Hamle Önerileri"
LIST_DESCRIPTION = ("Hamle'nin New Business önerileri. Gelişmeyi görevin altına tek satır yorum olarak yazın; "
                    "'pas' yazılan markayı Hamle 3 ay önermez. Görevleri kapatmak ekipte.")
PREFIX = "NB // "
CANSU = 48793256
HAMLE_PERSON = 53358100
STATE_KEY = "nb-tracking"
PASS_DAYS, QUIET_DAYS, MAX_NEW, MAX_READS = 90, 7, 6, 15
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


def description(s, day):
    return (f"Neden şimdi: {_line(s['reason'], 400)}\nKaynak: {s['url']}\nİlk adım önerisi: {_line(s['first_step'], 240)}\n"
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
    """New Business suggestions of one sent team digest -> todo inputs (deduplicated by brand)."""
    out, seen = [], set()
    for b in (stats or {}).get("new_business_brands") or []:
        if isinstance(b, dict) and b.get("brand") and str(b.get("url") or "").startswith("https://"):
            out.append({"brand": _line(b["brand"], 80), "reason": b.get("reason") or "", "url": b["url"],
                        "first_step": FIRST_STEP, "source": "Pazartesi New Business listesi"})
    f = ((stats or {}).get("ozu") or {}).get("firsat") or {}
    if f.get("kind") == "new_business" and f.get("brand") and str(f.get("url") or "").startswith("https://"):
        out.append({"brand": _line(f["brand"], 80), "reason": f.get("headline") or "", "url": f["url"],
                    "first_step": f.get("proposal") or FIRST_STEP, "source": "Günün özü fırsatı"})
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


def nb_follow_ups(state, *, reader, items, day, limit=8):
    """Tracked NB todos -> follow-up rows (icon, prefix, url, text, suffix) + effects to keep once
    the team digest is sent: {'seen': {key: comment id}, 'passes': [key], 'reminded': [key], 'done': [key]}."""
    rows, eff = [], {"seen": {}, "passes": [], "reminded": [], "done": []}
    open_by_id = {t["id"]: t for t in items if t.get("kind") == "todo" and t.get("project_id") == NB_PROJECT}
    reads = 0
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
            else:
                rows.append(("👍", "", t["app_url"], title,
                             f": “{_line(latest.get('text'), 90)}” ({_line(latest.get('by'), 40)}). Teşekkürler, takipteyim."))
            continue
        seen = [c.get("at") for c in comments if c.get("at")] + [t.get("created_on") or r.get("suggested_on") or ""]
        try:
            quiet = (day - date.fromisoformat(max(seen)[:10])).days
        except ValueError:
            continue
        if quiet >= QUIET_DAYS and not r.get("reminded_on"):
            rows.append(("", "@Cansu, ", t["app_url"], title, f" {quiet} gündür sessiz; bir adım atalım mı?"))
            eff["reminded"].append(key)
    return rows, eff


# ------------------------------------------------------------------ Basecamp (project 40704547 only)
class NbBasecamp:
    """Allow-listed calls only. send(method, url, body, expected) -> (payload, headers)."""
    def __init__(self, account_id, *, send):
        _need(type(account_id) is int and account_id > 0, "nb_account_invalid")
        self.account, self.send = account_id, send
        b = rf"/{account_id}/buckets/{NB_PROJECT}"
        self.allowed = {"GET": (rf"/{account_id}/projects/{NB_PROJECT}", rf"{b}/todosets/[1-9][0-9]*/todolists",
                                rf"{b}/todolists/[1-9][0-9]*", rf"{b}/todos/[1-9][0-9]*"),
                        "POST": (rf"{b}/todosets/[1-9][0-9]*/todolists", rf"{b}/todolists/[1-9][0-9]*/todos")}

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
            for key, cid in (effects.get("seen") or {}).items():
                if key in st["brands"]:
                    st["brands"][key]["seen_comment"] = cid; n += 1
            for field, value in (("passes", ("passed_until", until)), ("reminded", ("reminded_on", day.isoformat())),
                                 ("done", ("done_on", day.isoformat()))):
                for key in effects.get(field) or []:
                    if key in st["brands"]:
                        st["brands"][key][value[0]] = value[1]; n += 1
            return n
        return self._write(fn, fenced=False)

    def ensure_list(self):
        lst = self.state().get("list") or {}
        if lst.get("state") == "ready":
            return lst["id"]
        _need(not lst, "nb_list_outcome_unknown")  # a create that may have landed: a human checks, never a second list
        project = self.http.one(f"{API}/{self.http.account}/projects/{NB_PROJECT}.json")
        sets = [d.get("id") for d in project.get("dock") or [] if isinstance(d, dict) and d.get("name") == "todoset"
                and d.get("enabled") is True]
        _need(len(sets) == 1 and type(sets[0]) is int and sets[0] > 0, "nb_todoset_missing")
        url = f"{self.base}/todosets/{sets[0]}/todolists.json"
        same = [x for x in self.http.all(url) if isinstance(x, dict)
                and _line(x.get("name") or x.get("title"), 200) == LIST_NAME]
        _need(len(same) <= 1, "nb_list_ambiguous")
        if same:
            lid = same[0].get("id")
            _need(type(lid) is int and lid > 0 and same[0].get("visible_to_clients") is not True, "nb_list_unusable")
            self._write(lambda st, now: st.update(list={"state": "ready", "id": lid, "found_at": now}))
            return lid

        def begin(st, now):
            _need(not st.get("list"), "nb_list_race")
            st["list"] = {"state": "creating", "at": now}
        self._write(begin)
        try:
            row, _ = self.http.call("POST", url, {"name": LIST_NAME, "description": _html(LIST_DESCRIPTION)}, 201)
            lid = row.get("id") if isinstance(row, dict) else None
            _need(type(lid) is int and lid > 0, "nb_list_receipt_invalid")
            back = self.http.one(f"{self.base}/todolists/{lid}.json")
            _need(back.get("id") == lid and _line(back.get("name") or back.get("title"), 200) == LIST_NAME
                  and (back.get("bucket") or {}).get("id") == NB_PROJECT and back.get("visible_to_clients") is False
                  and (back.get("creator") or {}).get("id") == HAMLE_PERSON, "nb_list_readback_mismatch")
        except Exception as exc:
            reason = _code(exc)
            self._write(lambda st, now: st.update(list={"state": "unknown", "reason": reason, "at": now}), fenced=False)
            raise NbError("nb_list_outcome_unknown") from None
        self._write(lambda st, now: st.update(list={"state": "ready", "id": lid, "created_at": now}), fenced=False)
        return lid

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
