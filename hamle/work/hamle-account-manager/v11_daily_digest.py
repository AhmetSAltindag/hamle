"""Daily internal team digest mail (worker.py --mode daily_digest; NON-default).

Once per Istanbul weekday, at the first run at/after 09:15 (runtime_authority.
DAILY_DIGEST_WINDOW), ONE plain Turkish mail (~25 lines, every item with its
Basecamp link) from Hamle to the configured internal recipients (@gamechangers.com.tr):

  BUGÜN        todos/cards due today: iş · marka · @kişi · link
  GECİKENLER   overdue todos (account report /reports/todos/overdue.json, falling back
               to the per-project scan) + dated Card Table cards; the last 5 comments
               of up to 40 items are classified by ONE batched model call
               (🔴 bizde / 🟡 müşteri bekleniyor, tarih kaymalı / 🟢 bitmiş görünüyor).
               No budget or model error -> listed unclassified (never invented).
               Items assigned to Hamle itself are listed only.
  TREND RADARI only sourced trends from an injected trend_source (URL + date);
               none -> "Bugün doğrulanmış trend kaynağı alınamadı."
  ÖZEL GÜNLER  Mondays: today..+14 days from tr-special-days-2026.json + brand-card ideas.

Hamle never changes dates or completes items; Basecamp access is GET-only.
Durability (hamle_internal_v1): daily-digest-YYYYMMDD (team) / daily-digest-preview-
YYYYMMDD (preview, never consumes the team send): claimed (lease) -> sending (intent
before the single POST) -> sent (Sent-copy readback) | unknown (never resent).
"""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
import html
import json
from pathlib import Path
import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from autonomy_policy import HAMLE, scope_digest
from runtime_authority import DAILY_DIGEST_SEND_AT, DAILY_DIGEST_WINDOW, deadline_timestamp, start_timestamp, window_minutes

ISTANBUL = ZoneInfo("Europe/Istanbul")
CONFIG_NAME = "daily-digest-config.json"
CARDS_NAME = "brand-cards.json"
CALENDAR_NAME = "tr-special-days-2026.json"
INTERNAL = re.compile(r"[a-z0-9][a-z0-9._%+-]{0,63}@gamechangers\.com\.tr\Z")
API = "https://3.basecampapi.com"
APP = "https://3.basecamp.com"
HAMLE_PERSON = 53358100
LEASE_SECONDS = 1800  # = job timeout: research + wait-until-09:15 fit inside one claim
MAX_CLASSIFIED = 40
MODEL_INPUT_BUDGET = 15_000
NO_TRENDS = "Bugün doğrulanmış trend kaynağı alınamadı."
WEEKDAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")
SHORT_DAYS = ("Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz")
TITLE_DAYS = ("Pzt", "Salı", "Çarş", "Perş", "Cuma", "Cmt", "Paz")  # Ahmet 2026-10-11
MARKS = {"action_on_us": "🔴", "waiting_client": "🟡", "looks_done": "🟢"}
MORE_LINKS = 4
LIMITS = {"today": 3, "overdue": 6, "trends": 4, "special": 3, "new_business": 5}  # worst case ~26 non-blank lines


class DigestError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise DigestError(code)


def _load(name, path=None):
    root = Path(__file__).resolve().parents[2]
    candidates = [Path(path)] if path else [Path(__file__).with_name("chain-profile") / name,
                                            root / "outputs/gamechangers-internal" / name]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise DigestError("digest_file_missing:" + name)


def load_config(path=None):
    return _load(CONFIG_NAME, path)


def load_brand_cards(path=None):
    return _load(CARDS_NAME, path)


def load_calendar(path=None):
    return _load(CALENDAR_NAME, path)


def internal_address(value):
    return isinstance(value, str) and INTERNAL.fullmatch(value) is not None and value != HAMLE


def validate_config(cfg):
    """Internal recipients only; any other domain refuses the whole config."""
    _need(isinstance(cfg, dict) and cfg.get("schema_version") == 1 and type(cfg.get("enabled")) is bool,
          "digest_config_invalid")
    recipients = cfg.get("recipients")
    _need(isinstance(recipients, list) and len(recipients) <= 20, "digest_recipients_invalid")
    for r in recipients:
        _need(internal_address(r), "digest_recipient_not_internal_agency")
    _need(len(set(recipients)) == len(recipients), "digest_recipients_duplicate")
    note = cfg.get("day_note")
    _need(note is None or (isinstance(note, str) and len(note) <= 2000
                           and not any(ord(c) < 32 and c not in "\n\t" for c in note)), "digest_day_note_invalid")
    _need(type(cfg.get("basecamp_account_id")) is int and cfg["basecamp_account_id"] > 0, "digest_account_invalid")
    _need(type(cfg.get("trend_search_enabled", False)) is bool, "digest_trend_flag_invalid")
    excluded = cfg.get("excluded_project_ids", [])
    _need(isinstance(excluded, list) and len(excluded) <= 50
          and all(type(x) is int and x > 0 for x in excluded), "digest_excluded_projects_invalid")
    return {"enabled": cfg["enabled"], "recipients": list(recipients), "excluded": frozenset(excluded),
            "day_note": " ".join(note.split()) if isinstance(note, str) and note.strip() else None,
            "account_id": cfg["basecamp_account_id"], "trend_search": cfg.get("trend_search_enabled", False)}


def _plain(value, limit):
    text = re.sub(r"<[^>]*>", " ", value if isinstance(value, str) else "")
    return " ".join(html.unescape(text).split())[:limit]


# ------------------------------------------------------------------ Basecamp read-only listing
class BasecampDigestReader:
    """GET-only, allowlisted paths, page query only, no redirects, bounded pagination.

    get(url) -> (payload, headers). Production: cloud_providers._session() + the existing
    basecamp_cloud_auth token, same transport rules as v11_basecamp_reader.SessionGet.
    """
    PATHS = (r"projects", r"reports/todos/overdue",
             r"buckets/[1-9][0-9]*/todosets/[1-9][0-9]*/todolists",
             r"buckets/[1-9][0-9]*/todolists/[1-9][0-9]*/todos",
             r"buckets/[1-9][0-9]*/card_tables/[1-9][0-9]*",
             r"buckets/[1-9][0-9]*/card_tables/lists/[1-9][0-9]*/cards",
             r"buckets/[1-9][0-9]*/recordings/[1-9][0-9]*/comments")

    def __init__(self, account_id, *, get, max_projects=30, max_lists=120, max_pages=10, max_items=3000,
                 max_reads=600, excluded=frozenset()):
        self.account, self.get, self.excluded = account_id, get, frozenset(excluded)
        self.max_projects, self.max_lists, self.max_pages = max_projects, max_lists, max_pages
        self.max_items, self.max_reads = max_items, max_reads
        self.reads = 0

    def _check(self, url):
        p = urlsplit(url)
        _need(p.scheme == "https" and p.netloc == "3.basecampapi.com" and not p.fragment
              and re.fullmatch(rf"/{self.account}/(?:{'|'.join(self.PATHS)})\.json", p.path),
              "digest_url_out_of_scope")
        q = parse_qs(p.query, keep_blank_values=True)
        _need(not q or (set(q) == {"page"} and len(q["page"]) == 1 and re.fullmatch(r"[1-9][0-9]{0,3}", q["page"][0])),
              "digest_query_invalid")

    def _fetch(self, url):
        self._check(url)
        self.reads += 1
        _need(self.reads <= self.max_reads, "digest_read_budget_exceeded")
        return self.get(url)

    @staticmethod
    def _next(headers):
        link = {k.lower(): v for k, v in (headers or {}).items()}.get("link", "")
        _need(isinstance(link, str) and len(link) <= 4096, "digest_link_invalid")
        nexts = []
        for part in [x for x in link.split(",") if x.strip()]:
            m = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="([a-z ]+)"\s*', part)
            _need(m is not None, "digest_link_invalid")
            if "next" in m[2].split():
                nexts.append(m[1])
        _need(len(nexts) <= 1, "digest_link_ambiguous")
        return nexts[0] if nexts else None

    def _all(self, url, max_pages=None):
        out, base = [], urlsplit(url).path
        for _ in range(max_pages or self.max_pages):
            payload, headers = self._fetch(url)
            _need(isinstance(payload, list), "digest_payload_invalid")
            out.extend(payload)
            url = self._next(headers)
            if url is None:
                return out
            _need(urlsplit(url).path == base, "digest_link_out_of_scope")
        raise DigestError("digest_pagination_unbounded")

    def _one(self, url):
        payload, _ = self._fetch(url)
        _need(isinstance(payload, dict), "digest_payload_invalid")
        return payload

    def _app(self, row, pid, kind):
        url = row.get("app_url")
        if isinstance(url, str) and url.startswith(f"{APP}/{self.account}/buckets/{pid}/") and len(url) <= 300 \
                and not any(c in url for c in " <>\"'"):
            return url
        return f"{APP}/{self.account}/buckets/{pid}/{'card_tables/cards' if kind == 'card' else 'todos'}/{row['id']}"

    def _item(self, row, pid, project, kind):
        _need(isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0, "digest_item_invalid")
        people = [x for x in row.get("assignees") or [] if isinstance(x, dict)]
        comments = row.get("comments_url")
        expected = f"{API}/{self.account}/buckets/{pid}/recordings/{row['id']}/comments.json"
        return {"kind": kind, "id": row["id"], "project_id": pid, "project": str(project)[:60],
                "title": _plain(row.get("title") or row.get("content"), 110),
                "due_on": row.get("due_on") if isinstance(row.get("due_on"), str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["due_on"]) else None,
                "assignees": sorted({_plain(x.get("name") or str(x.get("id")), 40) for x in people}),
                "assignee_ids": sorted({x["id"] for x in people if type(x.get("id")) is int}),
                "app_url": self._app(row, pid, kind),
                "comments_url": expected if comments in (None, expected) else expected}

    def collect(self):
        a = self.account
        projects = [p for p in self._all(f"{API}/{a}/projects.json")
                    if isinstance(p, dict) and p.get("status") == "active" and p.get("id") not in self.excluded]
        _need(len(projects) <= self.max_projects, "digest_too_many_projects")
        items, lists = [], 0
        for project in projects:
            pid, name = project.get("id"), project.get("name") or project.get("id")
            _need(type(pid) is int and pid > 0, "digest_project_invalid")
            for dock in project.get("dock") or []:
                if not isinstance(dock, dict) or dock.get("enabled") is not True:
                    continue
                did = dock.get("id")
                if dock.get("name") == "todoset":
                    _need(type(did) is int and did > 0, "digest_todoset_invalid")
                    for tl in self._all(f"{API}/{a}/buckets/{pid}/todosets/{did}/todolists.json"):
                        lists += 1
                        _need(lists <= self.max_lists, "digest_too_many_lists")
                        lid = tl.get("id") if isinstance(tl, dict) else None
                        _need(type(lid) is int and lid > 0, "digest_list_invalid")
                        for t in self._all(f"{API}/{a}/buckets/{pid}/todolists/{lid}/todos.json"):
                            if isinstance(t, dict) and t.get("completed") is False and t.get("status") == "active":
                                items.append(self._item(t, pid, name, "todo"))
                elif dock.get("name") == "kanban_board":
                    _need(type(did) is int and did > 0, "digest_card_table_invalid")
                    table = self._one(f"{API}/{a}/buckets/{pid}/card_tables/{did}.json")
                    for col in table.get("lists") or []:
                        if not isinstance(col, dict) or str(col.get("type", "")).endswith(("DoneColumn", "NotNowColumn")):
                            continue
                        lists += 1
                        _need(lists <= self.max_lists, "digest_too_many_lists")
                        cid = col.get("id")
                        _need(type(cid) is int and cid > 0, "digest_column_invalid")
                        for c in self._all(f"{API}/{a}/buckets/{pid}/card_tables/lists/{cid}/cards.json"):
                            if isinstance(c, dict) and c.get("completed") is not True and c.get("status") == "active" \
                                    and c.get("due_on"):
                                items.append(self._item(c, pid, name, "card"))
                _need(len(items) <= self.max_items, "digest_too_many_items")
        return {"projects": len(projects), "items": items,
                "project_names": {p["id"]: str(p.get("name") or p["id"])[:60] for p in projects}}

    def overdue_report(self, project_names):
        """Account-level overdue todo report; None when unavailable (caller falls back)."""
        try:
            payload = self._one(f"{API}/{self.account}/reports/todos/overdue.json")
            rows = []
            for key, value in payload.items():
                _need(isinstance(value, list), "digest_report_invalid")
                rows.extend(value)
            out = []
            for t in rows:
                if not isinstance(t, dict) or t.get("completed") is not False:
                    continue
                pid = (t.get("bucket") or {}).get("id")
                if type(pid) is not int or pid in self.excluded:
                    continue
                out.append(self._item(t, pid, project_names.get(pid) or (t.get("bucket") or {}).get("name") or pid,
                                      "todo"))
            return out
        except DigestError:
            return None

    def last_comments(self, item, n=5):
        rows = self._all(item["comments_url"], max_pages=3)
        rows = [r for r in rows if isinstance(r, dict)]
        rows.sort(key=lambda r: str(r.get("created_at") or ""))
        return [{"at": str(r.get("created_at") or "")[:10], "by": _plain((r.get("creator") or {}).get("name"), 40),
                 "text": _plain(r.get("content"), 300)} for r in rows[-n:]]


# ------------------------------------------------------------------ classification (ONE batched model call)
def _model_input(items, comment_chars):
    return {"today": None, "items": [{"ref": it["ref"], "title": it["title"][:90], "brand": it["project"][:40],
                                      "days_overdue": it["days_overdue"],
                                      "comments": [{"at": c["at"], "by": c["by"][:30], "text": c["text"][:comment_chars]}
                                                   for c in it["comments"]]} for it in items]}


def build_model_input(items, today):
    for chars in (240, 160, 110, 70, 40, 20, 0):
        value = _model_input(items, chars)
        value["today"] = today.isoformat()
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) <= MODEL_INPUT_BUDGET:
            return value
    raise DigestError("digest_model_input_too_large")


def apply_classification(items, output):
    refs = {it["ref"]: it for it in items}
    seen = set()
    for row in (output or {}).get("items", []):
        ref = row.get("ref")
        if ref in refs and ref not in seen and row.get("status") in MARKS:
            seen.add(ref)
            refs[ref].update(status=row["status"], reason=_plain(row.get("reason"), 160),
                             action=_plain(row.get("action"), 160))
    return len(seen)


# ------------------------------------------------------------------ trends / calendar
def sourced_trends(raw, today, limit=5):
    """Hard rule: keep only items with an https source URL and a real publication date."""
    out = []
    for t in raw or []:
        try:
            url, published = t.get("url"), t.get("published_at")
            p = urlsplit(url)
            day = date.fromisoformat(str(published)[:10])
            if not (p.scheme == "https" and p.netloc and len(url) <= 300 and not any(c in url for c in " <>\"'")
                    and today - timedelta(days=30) <= day <= today and _plain(t.get("title"), 120)):
                continue
            out.append({"title": _plain(t["title"], 120), "url": url, "published_at": day.isoformat(),
                        "summary": _plain(t.get("summary"), 160), "idea": _plain(t.get("idea"), 200)})
        except Exception:
            continue
    return out[:limit]


def _brands(cards):
    return [b for b in (cards or {}).get("brands", []) if isinstance(b, dict) and b.get("include_in_suggestions")
            and b.get("sector")]


def special_days(calendar, cards, today, horizon=14):
    end = today + timedelta(days=horizon)
    rows = []
    for d in (calendar or {}).get("days", []):
        try:
            day = date.fromisoformat(d["date"])
        except Exception:
            continue
        if today <= day <= end:
            tags = set(d.get("tags") or [])
            fit = [b for b in _brands(cards) if "genel" in tags or tags & set(b.get("tags") or [])]
            rows.append({"date": day, "name": d["name"], "verified": d.get("verified") is True,
                         "note": d.get("note"), "brands": fit})
    return rows


# ------------------------------------------------------------------ text
def render(listing, *, day, day_note, overdue, trends, specials, classified, new_business=None):
    items = listing["items"]
    today_iso = day.isoformat()
    due = [t for t in items if t["due_on"] == today_iso]
    lines = [digest_title(day),
             f"Açık iş: {len(items)} · Bugün teslim: {len(due)} · Gecikmiş: {len(overdue)} "
             f"({listing['projects']} proje)"]
    if day_note:
        lines.append("Günün notu: " + day_note[:300])

    def who(t):
        return " ".join("@" + n for n in t["assignees"]) or "atanmamış"

    lines += ["", "BUGÜN"]
    due = sorted(due, key=lambda t: (t["project"], t["title"]))
    lines += [f"- {t['title']} · {t['project']} · {who(t)} · {t['app_url']}" for t in due[:LIMITS['today']]] or ["- Bugün teslim iş yok."]
    rest = due[LIMITS["today"]:]
    if rest:
        links = " ".join(t["app_url"] for t in rest[:MORE_LINKS]) + (" …" if len(rest) > MORE_LINKS else "")
        lines.append(f"- … +{len(rest)} iş daha: {links}")

    counts = " · ".join(f"{MARKS[k]} {n}" for k in MARKS
                        for n in [sum(1 for t in overdue if t.get("status") == k)] if n)
    lines += ["", "GECİKENLER" + (f" ({counts})" if classified and counts else "")
              + ("" if classified or not overdue else " (sınıflandırılmadı: model bütçesi/yanıtı yok)")]
    rank = {"action_on_us": 0, "waiting_client": 1, "looks_done": 2}
    shown = sorted(overdue, key=lambda t: (rank.get(t.get("status"), 3), t.get("hamle_assigned", False),
                                           -t["days_overdue"], t["title"]))[:LIMITS["overdue"]]
    for t in shown:
        mark = MARKS.get(t.get("status"), "•")
        reason = t.get("reason") or ("Hamle'ye atanmış; yalnız listelendi." if t.get("hamle_assigned") else "sınıflandırılmadı")
        action = f" Öneri: {t['action']}" if t.get("action") else ""
        lines.append(f"{mark} {t['title']} · {t['project']} · {who(t)} · {t['days_overdue']} gün gecikme · "
                     f"{reason}{action} · {t['app_url']}")
    if not overdue:
        lines.append("- Geciken iş yok.")
    elif len(overdue) > len(shown):
        lines.append(f"- … +{len(overdue) - len(shown)} geciken iş daha · tüm liste: "
                     f"{APP}/{listing.get('account')}/reports/todos/overdue")

    lines += ["", "TREND RADARI"]
    if trends:
        cards = [b for b in _brands(listing.get("cards")) if b.get("main_platform")]
        for i, t in enumerate(trends[:LIMITS["trends"]]):
            idea = f" Uyarlama: {t['idea']}" if t.get("idea") else ""
            if not idea and cards:
                b = cards[i % len(cards)]
                idea = f" Uyarlama: {b['brand']} ({b['main_platform']}, taslak kart) için kendi ürün diliyle denenebilir."
            lines.append(f"- {t['title']} ({t['published_at']}) · {t['url']}{idea}")
    else:
        lines.append(NO_TRENDS)

    if specials is not None:
        lines += ["", "ÖZEL GÜNLER (14 gün)"]
        for s in specials[:LIMITS["special"]]:
            if s.get("url"):  # web-researched (Opus + web search): own source URL and brand idea
                lines.append(f"- {s['date'].strftime('%d.%m')} {SHORT_DAYS[s['date'].weekday()]} · {s['name']} · "
                             f"{s['brand']}: {s['idea']} · {s['url']}")
                continue
            names = ", ".join(f"{b['brand']} ({b['main_platform'] or '?'})" for b in s["brands"][:3]) or "uygun kart yok"
            flag = "" if s["verified"] else " [tarih taslak]"
            note = f" {s['note']}" if s.get("note") else ""
            lines.append(f"- {s['date'].strftime('%d.%m')} {SHORT_DAYS[s['date'].weekday()]} · {s['name']}{flag} · "
                         f"öneri: {names}{note}")
        if not specials:
            lines.append("- Önümüzdeki 14 günde takvimde özel gün yok.")
    if new_business is not None:
        lines += ["", "NEW BUSINESS — bu haftanın hedef markaları (@Cansu)"]
        for b in new_business[:LIMITS["new_business"]]:
            lines.append(f"- {b['brand']} ({b['sector']}): {b['reason']} · {b['url']}")
        if not new_business:
            lines.append("- Bugün kaynağı doğrulanmış hedef marka bulunamadı.")
    lines += ["", FOOTER]
    return "\n".join(lines)



def digest_title(day):
    """Subject and heading, Ahmet's format: 'Hamle AI: 12 Ekim Pzt Günü Kısa Özeti'."""
    return f"Hamle AI: {day.day} {MONTHS[day.month - 1]} {TITLE_DAYS[day.weekday()]} Günü Kısa Özeti"


# ------------------------------------------------------------------ html (full width, Tahoma, titles are the links)
FONT = "Tahoma,Verdana,Segoe,sans-serif"
MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
          "Aralık")
BRAND_LABELS = {40694338: "WAT", 48639990: "Rabam", 43886681: "BWI", 48562916: "HAN Spaces", 48917059: "ya da",
                40704523: "Altınmarka", 40704530: "Bikaldi", 40704459: "Gamechangers", 44320349: "Fenerium",
                40704547: "New Biz"}
GROUPS = (("action_on_us", "🔴 Aksiyon bizde"), ("waiting_client", "🟡 Onay / müşteri dönüşü bekleniyor"),
          ("looks_done", "🟢 Bitmiş görünüyor, kapatılmamış"), (None, "• Sınıflandırılmadı"))
HTML_GROUP_LIMIT = 5
GREY, LINK, TEXT = "#6b7280", "#1a56db", "#1f2937"
FOOTER = ("Hamle AI Müşteri Temsilcisi · Gamechangers günlük iş özet · Hafta içi her gün 09:15 · "
          "Hamle'nin gelişimi ve eğitimi için Basecamp ve Google Docs kullanımlarınız çok önemlidir.")


def _e(value):
    return html.escape(str(value), quote=True)


def _a(url, text, bold=True):
    weight = "font-weight:bold;" if bold else ""
    return f'<a href="{_e(url)}" style="color:{LINK};text-decoration:none;{weight}">{_e(text)}</a>'


def _grey(text):
    return f'<span style="color:{GREY};">{_e(text)}</span>'


def _first_names(t):
    names = [n.split()[0] for n in t.get("assignees") or [] if n.split()]
    return " ".join("@" + n for n in names) or "atanmamış"


def _label(t):
    return BRAND_LABELS.get(t.get("project_id"), t.get("project") or "")


def _title(text):
    return re.sub(r"\*\*|__", "", str(text or "")).strip() or "(başlıksız)"


def _li(head, note=None):
    sub = f"<br>{note}" if note else ""
    return f'<li style="margin:0 0 8px;">{head}{sub}</li>'


def _ul(items):
    return f'<ul style="margin:4px 0 0;padding-left:20px;">{"".join(items)}</ul>'


def _h2(text):
    return (f'<div style="font-size:15px;font-weight:bold;margin:22px 0 6px;padding-bottom:4px;'
            f'border-bottom:1px solid #e5e7eb;">{_e(text)}</div>')


def _h3(text):
    return f'<div style="font-weight:bold;margin:12px 0 2px;">{_e(text)}</div>'


def _p(text_html, style=""):
    return f'<div style="margin:2px 0;{style}">{text_html}</div>'


def render_html(listing, *, day, day_note, overdue, trends, specials, classified, new_business=None):
    """HTML twin of render(): same facts, full width, Tahoma, Basecamp/source titles as links, no raw URLs."""
    items = listing["items"]
    today_iso = day.isoformat()
    due = sorted([t for t in items if t["due_on"] == today_iso], key=lambda t: (_label(t), t["title"]))
    marks = {k: sum(1 for t in overdue if t.get("status") == k) for k in MARKS}
    brands = len({t.get("project_id") for t in overdue})
    counts = [f"Açık iş {len(items)}", f"Bugün teslim {len(due)}", f"Gecikmiş {len(overdue)} ({brands} marka)"]
    if classified:
        counts += [f"{MARKS[k]} {n}" for k, n in marks.items()]
    out = [f'<div style="font-size:19px;font-weight:bold;margin:0 0 4px;">{_e(digest_title(day))}</div>',
           _p(_e(" · ".join(counts)), f"color:{GREY};")]
    if day_note:
        out.append(_p("<b>Günün notu:</b> " + _e(day_note[:300])))

    out.append(_h2(f"📌 Bugün teslim ({len(due)})"))
    out.append(_ul([_li(f"{_a(t['app_url'], _title(t['title']))} — {_grey(_label(t))} · {_e(_first_names(t))}")
                    for t in due]) if due else _p("Bugün teslim iş yok."))

    out.append(_h2(f"⏰ Gecikenler ({len(overdue)})"))
    if not overdue:
        out.append(_p("Geciken iş yok."))
    for status, name in GROUPS:
        group = [t for t in overdue if t.get("status") == status] if status else \
            [t for t in overdue if t.get("status") not in MARKS]
        if not group:
            continue
        group = sorted(group, key=lambda t: (t.get("hamle_assigned", False), -t["days_overdue"], t["title"]))
        suffix = "" if status or classified or not group else " — model yanıtı yok"
        out.append(_h3(f"{name} ({len(group)}){suffix}"))
        lis = []
        for t in group[:HTML_GROUP_LIMIT]:
            head = (f"{_a(t['app_url'], _title(t['title']))} — {_grey(_label(t))} · {_e(_first_names(t))} · "
                    f"{t['days_overdue']} gün")
            reason = t.get("reason") or ("Hamle'ye atanmış; yalnız listelendi." if t.get("hamle_assigned") else "")
            note = _e(reason) if reason else ""
            if t.get("action"):
                note += (" " if note else "") + "<b>Öneri:</b> " + _e(t["action"])
            lis.append(_li(head, note or None))
        if len(group) > HTML_GROUP_LIMIT:
            lis.append(_li(_e(f"+{len(group) - HTML_GROUP_LIMIT} iş daha · ")
                           + _a(f"{APP}/{listing.get('account')}/reports/todos/overdue", "tüm gecikenler", False)))
        out.append(_ul(lis))

    out.append(_h2("📈 Trend radarı"))
    if trends:
        cards = [b for b in _brands(listing.get("cards")) if b.get("main_platform")]
        lis = []
        for i, t in enumerate(trends[:LIMITS["trends"]]):
            idea = t.get("idea")
            if not idea and cards:
                b = cards[i % len(cards)]
                idea = f"{b['brand']} ({b['main_platform']}, taslak kart) için kendi ürün diliyle denenebilir."
            when = date.fromisoformat(t["published_at"])
            head = f"{_a(t['url'], t['title'])} {_grey(f'({when.day} {MONTHS[when.month - 1]})')}"
            note = (_e(t["summary"]) + " " if t.get("summary") else "") + \
                ("<b>Uyarlama:</b> " + _e(idea) if idea else "")
            lis.append(_li(head, note.strip() or None))
        out.append(_ul(lis))
    else:
        out.append(_p(_e(NO_TRENDS)))

    if specials is not None:
        end = day + timedelta(days=14)
        out.append(_h2(f"📅 Özel günler ({day.day}–{end.day} {MONTHS[end.month - 1]})" if day.month == end.month
                       else f"📅 Özel günler ({day.day} {MONTHS[day.month - 1]}–{end.day} {MONTHS[end.month - 1]})"))
        lis = []
        for s in specials[:LIMITS["special"]]:
            when = f"{s['date'].day} {MONTHS[s['date'].month - 1]} {SHORT_DAYS[s['date'].weekday()]}"
            name = _a(s["url"], s["name"]) if s.get("url") else f"<b>{_e(s['name'])}</b>"
            if s.get("url"):
                note = f"<b>{_e(s['brand'])}:</b> {_e(s['idea'])}"
            else:
                fit = ", ".join(f"{b['brand']} ({b['main_platform'] or '?'})" for b in s["brands"][:3]) or "uygun kart yok"
                note = "<b>Öneri:</b> " + _e(fit) + (" " + _e(s["note"]) if s.get("note") else "") \
                    + ("" if s["verified"] else " " + _grey("[tarih taslak]"))
            lis.append(_li(f"{_grey(when)} · {name}", note))
        out.append(_ul(lis) if lis else _p("Önümüzdeki 14 günde takvimde özel gün yok."))
    if new_business is not None:
        out.append(_h2("🚀 New Business — bu haftanın hedef markaları (@Cansu)"))
        lis = [_li(f"{_a(b['url'], b['brand'])} {_grey('(' + b['sector'] + ')')}", _e(b["reason"]))
               for b in new_business[:LIMITS["new_business"]]]
        out.append(_ul(lis) if lis else _p("Bugün kaynağı doğrulanmış hedef marka bulunamadı."))
    out.append(f'<div style="margin-top:26px;padding-top:8px;border-top:1px solid #e5e7eb;font-size:12px;'
               f'color:{GREY};">{_e(FOOTER)}</div>')
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:0;">'
            f'<div style="font-family:{FONT};font-size:14px;line-height:1.55;color:{TEXT};width:100%;">'
            + "".join(out) + "</div></body></html>")

# ------------------------------------------------------------------ mail (new message, one POST, readback)
class DigestMailWriter:
    """Reuses v11_chain_mail's allowlisted Gmail REST call and the verified Gmail token."""
    def __init__(self, *, token_context, session_factory):
        self.token_context, self.session_factory = token_context, session_factory

    @staticmethod
    def message_id(payload):
        return f"<hamle-digest-{scope_digest(payload)}@gamechangers.com.tr>"

    def _raw(self, payload):
        m = EmailMessage(policy=policy.SMTP)
        m["From"] = HAMLE
        m["To"] = ", ".join(payload["to"])
        m["Subject"] = payload["subject"]
        m["Message-ID"] = self.message_id(payload)
        m.set_content(payload["body"], subtype="plain", charset="utf-8")
        if payload.get("html"):
            m.add_alternative(payload["html"], subtype="html", charset="utf-8")
        return m.as_bytes()

    def write(self, payload):
        from v11_chain_mail import GMAIL, HEX, _call
        raw = self._raw(payload)
        with self.token_context() as token:
            session = self.session_factory()
            try:
                row = _call(session, "POST", GMAIL + "/messages/send", token,
                            body={"raw": base64.urlsafe_b64encode(raw).decode("ascii")})
            finally:
                session.close()
        _need(isinstance(row.get("id"), str) and HEX.fullmatch(row["id"]), "digest_receipt_unconfirmed")
        return {"remote_id": row["id"]}

    def readback(self, remote_id, payload):
        from v11_chain_mail import GMAIL, HEX, MAX_READ, _call
        _need(isinstance(remote_id, str) and HEX.fullmatch(remote_id), "digest_receipt_invalid")
        with self.token_context() as token:
            session = self.session_factory()
            try:
                row = _call(session, "GET", f"{GMAIL}/messages/{remote_id}?format=raw&fields=id%2ClabelIds%2Craw",
                            token, limit=MAX_READ)
            finally:
                session.close()
        _need(row.get("id") == remote_id and "SENT" in (row.get("labelIds") or [])
              and isinstance(row.get("raw"), str), "digest_readback_mismatch")
        raw = row["raw"]
        msg = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        to = [a.lower() for _, a in getaddresses([str(msg["To"] or "")])]
        _need(getaddresses([str(msg["From"])]) == [("", HAMLE)] and to == payload["to"] and not msg.get_all("Cc")
              and not msg.get_all("Bcc") and str(msg["Subject"]) == payload["subject"]
              and msg.get_content_type() == ("multipart/alternative" if payload.get("html") else "text/plain")
              and (not payload.get("html") or msg.get_body(preferencelist=("html",)) is not None)
              and msg.get_body(preferencelist=("plain",)) is not None
              and msg.get_body(preferencelist=("plain",)).get_content().replace("\r\n", "\n").rstrip("\n")
              == payload["body"].rstrip("\n"),
              "digest_readback_mismatch")
        return {"readback_sha256": scope_digest(payload)}


# ------------------------------------------------------------------ assembly
def build_digest(reader, *, day, day_note, classifier=None, trend_source=None, cards=None, calendar=None,
                 special_source=None, new_business_source=None):
    """Read-only listing + optional batched classification -> (body, stats)."""
    listing = reader.collect()
    listing["cards"] = cards
    listing["account"] = getattr(reader, "account", None)
    today_iso = day.isoformat()
    report = reader.overdue_report(listing["project_names"])
    todos = report if report is not None else [t for t in listing["items"] if t["kind"] == "todo"]
    cards_due = [t for t in listing["items"] if t["kind"] == "card"]
    overdue, seen = [], set()
    for t in todos + cards_due:
        if t["due_on"] and t["due_on"] < today_iso and (t["kind"], t["id"]) not in seen:
            seen.add((t["kind"], t["id"]))
            t = dict(t, days_overdue=(day - date.fromisoformat(t["due_on"])).days,
                     hamle_assigned=HAMLE_PERSON in t["assignee_ids"])
            overdue.append(t)
    overdue.sort(key=lambda t: (-t["days_overdue"], t["project"], t["title"]))
    candidates = [t for t in overdue if not t["hamle_assigned"]][:MAX_CLASSIFIED]
    classified = 0
    model_status = "not_called"
    if candidates and classifier is not None:
        try:
            for i, t in enumerate(candidates):
                t["ref"] = f"o{i + 1}"
                t["comments"] = reader.last_comments(t)
            output = classifier(build_model_input(candidates, day))
            classified = apply_classification(candidates, output)
            model_status = "classified"
        except Exception as exc:  # no budget / model error / comment read failure -> unclassified list
            for t in candidates:
                for k in ("status", "reason", "action"):
                    t.pop(k, None)
            classified = 0
            model_status = "unclassified:" + str(getattr(exc, "code", None) or type(exc).__name__)[:80]
    trends = []
    trend_status = "no_provider"
    if trend_source is not None:
        try:
            trends = sourced_trends(trend_source(day), day)
            trend_status = f"{len(trends)}_sourced"
        except Exception:
            trend_status = "provider_failed"
    specials, special_status, new_business, nb_status = None, "not_monday", None, "not_monday"
    if day.weekday() == 0:
        specials, special_status = special_days(calendar, cards, day), "calendar"
        if special_source is not None:
            try:
                researched = [dict(r, date=date.fromisoformat(r["date"])) for r in special_source(day)]
                if researched:
                    specials, special_status = researched, f"{len(researched)}_researched"
            except Exception:
                special_status = "research_failed_calendar_used"
        if new_business_source is not None:
            try:
                result = new_business_source(day)
                new_business = result["brands"] if result.get("status") == "ok" else []
                nb_status = f"{len(new_business)}_sourced"
            except Exception:
                new_business, nb_status = [], "provider_failed"
        else:
            nb_status = "no_provider"
    parts = dict(day=day, day_note=day_note, overdue=overdue, trends=trends, specials=specials,
                 classified=classified > 0, new_business=new_business)
    body = render(listing, **parts)
    stats_html = render_html(listing, **parts)
    return body, {"html": stats_html, "open_items": len(listing["items"]), "overdue": len(overdue), "classified": classified,
                  "model": model_status, "trends": trend_status, "special_days": special_status,
                  "new_business": nb_status, "overdue_source": "report" if report is not None else "scan",
                  "new_business_brands": new_business or []}


# ------------------------------------------------------------------ run
def next_weekday(day):
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def preview_target(value, today):
    """None -> today; else exactly today or the next Istanbul weekday (at most 3 days ahead)."""
    if value is None:
        return today
    try:
        _need(isinstance(value, (str, date)), "digest_preview_day_invalid")
        target = value if isinstance(value, date) else date.fromisoformat(value)
        _need(isinstance(value, date) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "digest_preview_day_invalid")
    except ValueError:
        raise DigestError("digest_preview_day_invalid") from None
    nxt = next_weekday(today)
    _need(target == today or (target == nxt and (nxt - today).days <= 3), "digest_preview_day_out_of_range")
    return target


def run_daily_digest(ops, *, config, reader_factory, writer, clock, window=DAILY_DIGEST_WINDOW,
                     preview_to=None, preview_day=None, classifier=None, trend_source=None, cards=None, calendar=None,
                     special_source=None, new_business_source=None, preview_attempt=None, sleep=time.sleep):
    """One team digest per Istanbul weekday, or one preview per target day to ONE agency address.

    preview_day (only with preview_to): today or the next Istanbul weekday at most 3 days
    ahead; BUGÜN, days-late and the Monday section are rendered as of that date.
    """
    claim = None
    try:
        now = clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        _need(start_timestamp() <= now.timestamp() <= deadline_timestamp(), "authorization_expired")
        cfg = validate_config(config)
        local = now.astimezone(ISTANBUL)
        target = local.date()
        _need(preview_day is None or preview_to is not None, "digest_preview_day_requires_preview_to")
        _need(preview_attempt is None or (preview_to is not None and type(preview_attempt) is int
                                          and 2 <= preview_attempt <= 3), "digest_preview_attempt_invalid")
        if preview_to is not None:
            _need(internal_address(preview_to), "digest_preview_not_internal_agency")
            target = preview_target(preview_day, local.date())
            recipients, key, subject_prefix = [preview_to], ("daily-digest-preview-" + target.strftime("%Y%m%d")
                                              + (f"-{preview_attempt}" if preview_attempt else "")), "[ÖNİZLEME] "
        else:
            if not cfg["enabled"] or not cfg["recipients"]:
                return {"status": "no_new_work", "reason": "digest_disabled_or_no_recipients", "business_writes": 0}
            first, last = window_minutes(window)
            minute = local.hour * 60 + local.minute
            if local.weekday() >= 5 or not first <= minute <= last:
                return {"status": "no_new_work", "reason": "outside_digest_window", "business_writes": 0}
            recipients, key, subject_prefix = cfg["recipients"], "daily-digest-" + local.strftime("%Y%m%d"), ""
        token = uuid.uuid4().hex

        def claim_op(tx, ts):
            c = ops._control(tx); b = ops._budget(tx); row = tx.get(key)
            ops._open(c, c["generation"], ts)
            _need(not b["overrun_hold"], "budget_overrun_hold")
            if row is not None and not (row["state"] == "claimed" and ts >= row["lease_until"]):
                return None  # claimed/sending/sent/unknown: never a second mail under this key
            new = {"key": key, "kind": "daily_digest_preview" if preview_to else "daily_digest", "state": "claimed",
                   "token": token, "generation": c["generation"], "claimed_at": ts,
                   "lease_until": ts + LEASE_SECONDS, "date": target.isoformat(), "run_date": local.date().isoformat()}
            tx.put(key, new)
            return new
        claim = ops._run(claim_op)
        if claim is None:
            return {"status": "already_recorded", "reason": "digest_already_claimed_or_sent", "key": key,
                    "business_writes": 0}

        body, stats = build_digest(reader_factory(), day=target, day_note=cfg["day_note"], classifier=classifier,
                                   trend_source=trend_source if cfg["trend_search"] else None,  # flag-gated
                                   cards=cards, calendar=calendar, special_source=special_source,
                                   new_business_source=new_business_source)
        if preview_to is None:  # team mail: prepared from 09:00, never sent before 09:15 Istanbul
            h, mnt = (int(v) for v in DAILY_DIGEST_SEND_AT.split(":"))
            send_at = local.replace(hour=h, minute=mnt, second=0, microsecond=0)
            wait = (send_at - clock().astimezone(ISTANBUL)).total_seconds()
            if wait > 0:
                sleep(min(wait, 20 * 60))
        payload = {"from": HAMLE, "to": list(recipients), "cc": [],
                   "subject": subject_prefix + digest_title(target), "body": body,
                   "html": stats.pop("html")}
        sha = scope_digest(payload)

        def begin_op(tx, ts):
            c = ops._control(tx); b = ops._budget(tx); row = tx.get(key)
            _need(row is not None and row["token"] == token and row["state"] == "claimed", "digest_claim_lost")
            ops._open(c, claim["generation"], ts)  # stop/generation change after claim -> no send
            _need(not b["overrun_hold"], "budget_overrun_hold")
            row.update(state="sending", payload_sha256=sha, recipients=list(payload["to"]),
                       subject=payload["subject"], stats=stats, sending_at=ts)
            tx.put(key, row)
            return row
        ops._run(begin_op)

        def mark(state, **extra):
            def op(tx, ts):
                row = tx.get(key)
                _need(row is not None and row["token"] == token, "digest_claim_lost")
                row.update(state=state, updated_at=ts, **extra)
                tx.put(key, row)
                return row
            return ops._run(op, allow_expired=True)
        try:
            remote = writer.write(payload)["remote_id"]
            back = writer.readback(remote, payload)
            _need(back.get("readback_sha256") == sha, "digest_readback_mismatch")
        except Exception as exc:
            try:
                mark("unknown", reason=str(getattr(exc, "code", None) or type(exc).__name__)[:120])
            except Exception:
                pass
            return {"status": "held", "reason": "digest_outcome_unknown_no_retry", "business_writes": None,
                    "automatic_retry_allowed": False}
        mark("sent", remote_id=remote)
        return {"status": "digest_sent", "key": key, "recipients": len(payload["to"]), "stats": stats,
                "business_writes": 1}
    except Exception as exc:
        reason = re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]
        if claim is not None:
            try:  # nothing was sent (send failures return above): release so a later run may retry
                def release(tx, ts):
                    row = tx.get(claim["key"])
                    if row is not None and row["token"] == claim["token"] and row["state"] == "claimed":
                        row.update(lease_until=ts, last_failure=reason)
                        tx.put(claim["key"], row)
                ops._run(release, allow_expired=True)
            except Exception:
                pass
        return {"status": "held", "reason": reason, "business_writes": 0, "automatic_retry_allowed": False}


def model_classifier(ops, load_key, catalog, *, transport=None, routing=None, client=None):
    """ONE batched call for role digest_classifier (model-routing.json: Claude Haiku 5.5 by default;
    OpenAI only if routed there), via v11_chain_models under the 30 USD chain cap.

    load_key: {provider: loader} or one loader (tests). Any failure leaves the overdue list
    unclassified (build_digest catches it); the digest itself still goes out.
    """
    from v11_chain_live import budget_gate
    import hashlib
    import model as base
    import model_routing
    from v11_chain_models import ModelGate, call_routed, internal_cap_microdollars, reservation_for
    cap = internal_cap_microdollars(catalog)
    route = (routing if routing is not None else model_routing.load()).route("digest_classifier")
    hold = reservation_for(route)
    gate = ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)
    budget_ok = budget_gate(ops, hold, cap)

    def classify(model_input):
        _need(budget_ok(), "model_budget_unavailable")
        # Cache key = digest of the exact input bytes sent: reuse only on a byte-identical input.
        return gate.once("digest", hashlib.sha256(b"digest\0" + base._encoded(model_input)).hexdigest(),
                         lambda: call_routed(route, "digest", model_input, load_key=load_key, transport=transport,
                                             client=client),
                         provider=route.provider, hold=hold)
    return classify


def run_worker_daily_digest(ops, secrets, *, clock=None, preview_to=None, preview_day=None, preview_attempt=None):
    """Entry for worker.py --mode daily_digest (existing credential loaders only).

    Trend provider: v11_trend_search (role trend_radar: Claude Opus 5.5 + web search by default,
    OpenAI Responses web_search only if routed there), built ONLY when daily-digest-config.json
    has trend_search_enabled=true; otherwise the section prints NO_TRENDS.
    """
    import basecamp_cloud_auth
    import cloud_providers as provider
    from v11_chain_live import load_catalog
    from v11_chain_mail import gmail_token_context
    clock = clock or (lambda: datetime.now(timezone.utc))
    config = load_config()
    sessions = []

    def reader_factory():
        token = basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh)
        session = provider._session()
        sessions.append(session)

        def get(url):
            r = None
            try:
                r = session.request("GET", url, headers={"Authorization": "Bearer " + provider._token(token),
                                                          "User-Agent": "Hamle Daily Digest (ahmet@gamechangers.com.tr)",
                                                          "Accept": "application/json"},
                                    timeout=(5, 15), allow_redirects=False, stream=True)
                _need(r.status_code == 200, "basecamp_read_rejected")
                chunks, size = [], 0
                for chunk in r.iter_content(chunk_size=16384):
                    size += len(chunk)
                    _need(size <= 1_048_576, "basecamp_page_too_large")
                    chunks.append(chunk)
                return provider._json(b"".join(chunks)), dict(r.headers)
            except DigestError:
                raise
            except Exception:
                raise DigestError("basecamp_read_failed") from None
            finally:
                if r is not None:
                    r.close()
        valid = validate_config(config)
        return BasecampDigestReader(valid["account_id"], get=get, excluded=valid["excluded"])

    writer = DigestMailWriter(token_context=gmail_token_context(secrets), session_factory=provider._session)
    keys = {"anthropic": lambda: provider.anthropic_key(secrets), "openai": lambda: provider.openai_key(secrets)}
    try:
        classifier = model_classifier(ops, keys, load_catalog())
    except Exception:
        classifier = None  # no cap/catalog/routing -> overdue listed unclassified
    cards = load_brand_cards()
    trend_source = None
    if validate_config(config)["trend_search"] is True:
        try:
            from v11_trend_search import trend_search_source
            trend_source = trend_search_source(ops, keys, load_catalog(), cards)
        except Exception:
            trend_source = None  # no cap/catalog -> NO_TRENDS
    special_source = new_business_source = None
    try:  # Monday research (Claude Opus + web search); each gated by its model-routing flag
        from v11_trend_search import special_days_research_source, new_business_research_source
        special_source = special_days_research_source(ops, keys, load_catalog(), cards)
        new_business_source = new_business_research_source(ops, keys, load_catalog(), cards)
    except Exception:
        special_source = new_business_source = None  # calendar-only / section omitted
    try:
        return run_daily_digest(ops, config=config, reader_factory=reader_factory, writer=writer, clock=clock,
                                preview_to=preview_to, preview_day=preview_day, preview_attempt=preview_attempt,
                                classifier=classifier, trend_source=trend_source, cards=cards, calendar=load_calendar(),
                                special_source=special_source, new_business_source=new_business_source)
    finally:
        for s in sessions:
            s.close()
