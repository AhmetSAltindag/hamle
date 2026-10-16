"""Prospect watch (owner 2026-10-15): mail from UNKNOWN senders to the agency mailbox gets one warm first reply.

The agency's public site opens a mail to the agency mailbox ("Hamle AI demo talebi"). This module handles mail
whose sender is neither agency staff nor a known client contact (the client watch and the chain keep theirs).
Job hamle-prospect-watch, every 15 minutes, every day, 08:00-22:00 Istanbul (night mail is handled at 08:00).

Per mail, in this order:
1. Pre-filter in code, no model: auto-generated / bulk mail (Auto-Submitted other than "no", X-Autoreply,
   X-Autorespond, Precedence bulk/list/junk, List-Unsubscribe, List-Id, noreply-like senders, ignored domains)
   and mail whose Authentication-Results (our provider's own line) show an SPF/DKIM/DMARC fail or no aligned
   pass is skipped and only counted in the job log. Mail older than config 'since' is skipped the same way.
2. Durable claim per message (fence like the client watch), at most ONE automatic reply per address ever, at
   most limits.max_auto_replies_per_day automatic replies per Istanbul day (the rest: a "human should look" note).
3. One classification on the cheap route (role prospect_classifier, Haiku), schema-checked: demo_or_first_contact,
   research_request, known_client_new_person, vendor_or_job, spam, unsure. Mail text is untrusted data.
4. Company ONLY from the sender's domain: free mail = individual (no company research); a client brand domain
   (config client_domains) = a warm reply that knows the brand + a note asking the brand's lead "Bu kişi kim,
   ekleyelim mi?"; any other domain = one web-search call (role prospect_research) about the company and, when
   the person is not in the owner's known-people list, the person (professional sources only), plus the free
   digital-karne site checks after the reply. Person findings go only into the internal note.
5. The reply (role customer_text, Opus) gets an isolated input: the mail, the domain-derived company facts, the
   known-person "where we met" line. No brand memory, no client data, no internal data. The reply is validated in
   code (greeting, signature, internal words, prices, dates/promises, links, other brands, length, research words);
   a failed check sends nothing and opens the note for a human.
6. The reply goes in the same thread to the sender with the owner always in CC (owner 2026-10-16; no one else);
   a separate copy mail goes to the owner; a to-do opens
   in the New Business project's notes list (found by name), assigned to the new business lead and the owner.
7. research_request (organisations only): a to-do in the research list assigned to the owner, due that day.
   Nothing is researched until the owner's verified Basecamp person writes exactly "Onaylıyorum" on it. Then one
   web-search report (role prospect_report) is written as a comment; it reaches the prospect (same thread) only
   after the owner writes "Hamle gönder" there (owner in CC). One open request per company domain; one reminder a day after
   research.reminder_after. An agency member's "Hamle dur" on the request stops it.

Same rules as the other modules: global stop ('dur') or a new control generation fences every write and stops
the run before any read; the monthly budget (cloud_store) and the internal model cap gate every model call;
durable record before every provider write; one attempt; anything uncertain is 'unknown' and never retried.
KVKK: every prospect record is listed in a monthly index and replaced by a data-free tombstone after
retention_days (the address marker keeps only "replied", so the one-reply rule survives the purge).

Mac operator commands (ADC credentials, Firestore only, no cost):
  python3 v11_prospect_watch.py people status
  python3 v11_prospect_watch.py people import --file known-people.json --actor <owner address>
"""
from __future__ import annotations

import argparse
from html import escape
import copy
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import unicodedata
import uuid
from datetime import date, datetime, timedelta, timezone
from email.utils import getaddresses
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from autonomy_policy import HAMLE, scope_digest

ISTANBUL = ZoneInfo("Europe/Istanbul")
CONFIG_NAME = "prospect-watch-config.json"
PEOPLE_KEY = "prospect-watch-known-people"
RUN_KEY = "prospect-watch-run"
LISTS_KEY = "prospect-watch-lists"
REQUESTS_KEY = "prospect-watch-requests"
PURGE_KEY = "prospect-watch-purge"
HERE = Path(__file__).resolve().parent
PROMPTS = {"classify": HERE / "prospect_classifier_prompt.txt", "research": HERE / "prospect_research_prompt.txt",
           "reply": HERE / "prospect_reply_prompt.txt", "report": HERE / "prospect_report_prompt.txt",
           "writer": HERE / "prospect_writer_prompt.txt"}
ROLES = {"classify": "prospect_classifier", "research": "prospect_research", "reply": "customer_text",
         "report": "prospect_report", "writer": "prospect_writer"}
SCORE_PARTS = ("Kanal varlığı ve aktiflik", "Frekans ve süreklilik", "Kanallar arası denge",
               "Mesaj ve anahtar kelime tutarlılığı", "Algoritmaya uygunluk ve etkileşim", "Web ve Google görünürlüğü")
SECTIONS = (("channels", "Kanal karnesi · son 6 ay"), ("balance", "Kanallar arası denge"),
            ("message", "Ana mesaj ve anahtar kelime uyumu"), ("algorithm", "Algoritmaya uygunluk"),
            ("web", "Web sitesi"), ("google", "Google görünürlüğü"), ("competitors", "Rakiplerle kıyas"),
            ("audience", "Hedef kitle tahmini"), ("sector", "Sektör dinamikleri ve beklentiler"),
            ("company", "Kurum künyesi ve son 12 ay"), ("risks", "Fırsatlar ve riskler"))
# Owner 2026-10-16: the research runs part by part (small calls, 3 searches each), then one strategist merges them;
# one big call with 10 searches outgrew the input limit (usage_limit_exceeded). A failed part leaves a gap, not a void.
REPORT_PARTS = (("channels", ("channels", "message")), ("google", ("google",)), ("audience", ("audience",)),
                ("competitors", ("competitors",)), ("sector", ("sector",)),
                ("company", ("company", "campaigns", "regulation")))
# The strategist writes in two steps (one answer would outgrow a single call): the analysis, then the plan on top of it.
WRITER_STEPS = (("analysis", ("score", "headline", "summary", "sections", "audience_segments", "competitor_rows")),
                ("plan", ("moves", "plan_30", "plan_90", "closing_summary", "meeting_notes", "mail")))
PLATFORMS = ("instagram", "linkedin", "youtube", "tiktok", "x", "facebook")
CATEGORIES = ("demo_or_first_contact", "research_request", "known_client_new_person", "vendor_or_job", "spam",
              "unsure")
CATEGORY_TR = {"demo_or_first_contact": "demo / ilk temas", "research_request": "araştırma isteği",
               "known_client_new_person": "müşterimizden yeni kişi", "vendor_or_job": "tedarikçi / iş başvurusu",
               "spam": "spam", "unsure": "emin değilim"}
REPLY_CATEGORIES = ("demo_or_first_contact", "research_request", "known_client_new_person")
LANGUAGES = ("tr", "en")
BASE_FREE_MAIL = frozenset((
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.com.tr", "outlook.com", "outlook.com.tr", "live.com",
    "msn.com", "yahoo.com", "yahoo.com.tr", "icloud.com", "me.com", "mac.com", "yandex.com", "yandex.com.tr",
    "yandex.ru", "mail.ru", "protonmail.com", "proton.me", "aol.com", "gmx.com", "gmx.net", "gmx.de", "mynet.com",
    "windowslive.com", "zoho.com", "tutanota.com", "hey.com", "fastmail.com"))
NOREPLY_LOCAL = re.compile(r"(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_.]?daemon|postmaster)", re.I)
FILTER_HEADERS = ("Auto-Submitted", "X-Autoreply", "X-Autorespond", "Precedence", "List-Unsubscribe", "List-Id",
                  "Authentication-Results")
AUTH_BAD = ("fail", "softfail", "permerror", "temperror")
PERSONAL_SOCIAL = ("instagram.com", "facebook.com", "fb.com", "tiktok.com", "x.com", "twitter.com", "threads.net",
                   "snapchat.com", "pinterest.com", "vk.com", "youtube.com")
LEASE_SECONDS = 900
MAX_TEXT = 3000
MAX_QUOTE = 1500
MAX_NOTE = 7000
EMAIL = re.compile(r"[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")
HHMM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
WINDOW_GRACE_MINUTES = 5  # a scheduled 22:00 start that begins a few minutes late still runs
API = "https://3.basecampapi.com"
APP = "https://3.basecamp.com"


class ProspectError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ProspectError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _sha(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _line(value, limit=300):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


_FOLD = str.maketrans({"ı": "i", "ç": "c", "ş": "s", "ğ": "g", "ü": "u", "ö": "o", "â": "a", "î": "i", "û": "u"})


def fold(text):
    """Turkish-aware comparison form: İ/I/ı/i and the Turkish letters fold to plain lower-case ASCII."""
    text = unicodedata.normalize("NFKC", str(text or "")).replace("İ", "i").replace("I", "ı").lower()
    return text.replace("i̇", "i").translate(_FOLD)


# ------------------------------------------------------------------ config
def load_config(path=None):
    candidates = [Path(path)] if path else [HERE / "chain-profile" / CONFIG_NAME,
                                            HERE.parents[1] / "outputs/gamechangers-internal" / CONFIG_NAME]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                raise ProspectError("prospect_config_unreadable") from None
    raise ProspectError("prospect_config_missing")


FIELDS = {"schema_version", "kind", "enabled", "mailbox", "agency_domain", "agency_name", "auth_serv_id", "since",
          "timezone", "window", "roles", "basecamp", "limits", "signatures", "approved_sentences", "research",
          "karne_checks", "client_domains", "own_brand_keys", "ignore_domains", "ignore_locals", "free_mail_domains",
          "retention_days", "decision_ref", "rules"}
LIMITS = {"max_auto_replies_per_day": (1, 30), "max_mails_per_run": (1, 10), "max_threads": (1, 30),
          "newer_than_days": (1, 7), "run_seconds": (60, 840), "reply_max_chars": (400, 2000)}


def _domains(values, code):
    _need(isinstance(values, list) and len(values) <= 100 and all(
        isinstance(d, str) and DOMAIN.fullmatch(d) for d in values), code)
    return [d.lower() for d in values]


def validate_config(cfg):
    _need(isinstance(cfg, dict) and cfg.get("schema_version") == 1 and cfg.get("kind") == "prospect_watch_config"
          and set(cfg) <= FIELDS and type(cfg.get("enabled")) is bool, "prospect_config_invalid")
    _need(cfg.get("mailbox") == HAMLE, "prospect_mailbox_mismatch")  # the Gmail writers only send as this mailbox
    _need(isinstance(cfg.get("agency_domain"), str) and HAMLE.endswith("@" + cfg["agency_domain"]),
          "prospect_agency_domain_invalid")
    _need(isinstance(cfg.get("agency_name"), str) and 2 <= len(cfg["agency_name"]) <= 60, "prospect_config_invalid")
    _need(isinstance(cfg.get("auth_serv_id"), str) and DOMAIN.fullmatch(cfg["auth_serv_id"]), "prospect_config_invalid")
    try:
        since = datetime.fromisoformat(str(cfg.get("since")))
    except ValueError:
        raise ProspectError("prospect_since_invalid") from None
    _need(since.tzinfo is not None, "prospect_since_invalid")
    _need(cfg.get("timezone") == "Europe/Istanbul", "prospect_timezone_invalid")
    w = cfg.get("window")
    _need(isinstance(w, dict) and set(w) == {"start", "end"} and all(isinstance(w[k], str) and HHMM.fullmatch(w[k])
                                                                    for k in w) and w["start"] < w["end"],
          "prospect_window_invalid")
    roles = cfg.get("roles")
    _need(isinstance(roles, dict) and set(roles) == {"owner", "new_business_lead"}
          and all(isinstance(v, str) and EMAIL.fullmatch(v) and v.endswith("@" + cfg["agency_domain"]) and v != HAMLE
                  for v in roles.values()), "prospect_roles_invalid")
    bc = cfg.get("basecamp")
    _need(isinstance(bc, dict) and set(bc) == {"project_id", "notes_list", "research_list"}
          and type(bc["project_id"]) is int and bc["project_id"] > 0
          and all(isinstance(bc[k], str) and 2 <= len(bc[k].strip()) <= 80 for k in ("notes_list", "research_list"))
          and fold(bc["notes_list"]).strip() != fold(bc["research_list"]).strip(), "prospect_basecamp_invalid")
    lim = cfg.get("limits")
    _need(isinstance(lim, dict) and set(lim) == set(LIMITS)
          and all(type(lim[k]) is int and lo <= lim[k] <= hi for k, (lo, hi) in LIMITS.items()), "prospect_limits_invalid")
    sig = cfg.get("signatures")
    _need(isinstance(sig, dict) and set(sig) == set(LANGUAGES) and all(
        isinstance(v, str) and 5 <= len(v) <= 120 and "\n" in v for v in sig.values()), "prospect_signatures_invalid")
    sent = (cfg.get("approved_sentences") or {}).get("research_request")
    _need(isinstance(sent, dict) and set(sent) == set(LANGUAGES) and all(
        isinstance(v, str) and 10 <= len(v) <= 300 and "\n" not in v for v in sent.values()), "prospect_sentences_invalid")
    rm = (cfg.get("approved_sentences") or {}).get("report_mail")
    parts = {"greeting", "greeting_no_name", "intro", "low", "high", "close"}
    _need(isinstance(rm, dict) and set(rm) == {"high_from"} | set(LANGUAGES) and type(rm["high_from"]) is int
          and 1 <= rm["high_from"] <= 100 and all(
              isinstance(rm[lang], dict) and set(rm[lang]) == parts and all(
                  isinstance(v, str) and 5 <= len(v) <= 400 and "\n" not in v for v in rm[lang].values())
              and "{score}" in rm[lang]["low"] for lang in LANGUAGES), "prospect_report_mail_invalid")
    r = cfg.get("research")
    _need(isinstance(r, dict) and set(r) == {"enabled", "estimate", "reminder_after", "expire_days",
                                             "max_reports_per_run"}
          and type(r["enabled"]) is bool and isinstance(r["estimate"], str) and 3 <= len(r["estimate"]) <= 120
          and isinstance(r["reminder_after"], str) and HHMM.fullmatch(r["reminder_after"])
          and w["start"] <= r["reminder_after"] <= w["end"]
          and type(r["expire_days"]) is int and 1 <= r["expire_days"] <= 30
          and type(r["max_reports_per_run"]) is int and 1 <= r["max_reports_per_run"] <= 3, "prospect_research_invalid")
    _need(type(cfg.get("karne_checks")) is bool, "prospect_config_invalid")
    cd = cfg.get("client_domains")
    _need(isinstance(cd, dict) and len(cd) <= 100 and all(isinstance(k, str) and DOMAIN.fullmatch(k)
                                                          and isinstance(v, str) for k, v in cd.items()),
          "prospect_client_domains_invalid")
    _need(isinstance(cfg.get("own_brand_keys"), list) and all(isinstance(k, str) for k in cfg["own_brand_keys"]),
          "prospect_config_invalid")
    _domains(cfg.get("ignore_domains"), "prospect_ignore_domains_invalid")
    _domains(cfg.get("free_mail_domains"), "prospect_free_mail_invalid")
    _need(isinstance(cfg.get("ignore_locals"), list) and len(cfg["ignore_locals"]) <= 50 and all(
        isinstance(x, str) and re.fullmatch(r"[a-z0-9._+-]{2,64}", x) for x in cfg["ignore_locals"]),
          "prospect_ignore_locals_invalid")
    _need(type(cfg.get("retention_days")) is int and 30 <= cfg["retention_days"] <= 730, "prospect_retention_invalid")
    return cfg


def _agency_people(catalog):
    from v11_client_reply import agency_people
    return agency_people(catalog)


def first_word(name):
    token = _line(name, 120).split(" ")[0] if name else ""
    return token or None


def meeting_phrase(first, language):
    """'Deniz de görüşmede olacak.' with Turkish vowel harmony on de/da; English: '<name> will join us too.'"""
    if language == "en":
        return f"{first} will join the meeting too."
    vowels = [c for c in fold(first) if c in "aeiou"] if first else []
    raw_vowels = [c for c in str(first).lower() if c in "aeıioöuü"]
    back = raw_vowels[-1] in "aıou" if raw_vowels else (vowels[-1] in "aou" if vowels else False)
    return f"{first} {'da' if back else 'de'} görüşmede olacak."


def resolve(cfg, catalog, book=None):
    """Config + catalog -> settings: verified role people, Basecamp target, client brand domains."""
    cfg = validate_config(cfg)
    people = _agency_people(catalog)
    roles = {}
    for role, email in cfg["roles"].items():
        row = people.get(email)
        _need(row is not None and type(row.get("basecamp_id")) is int, "prospect_role_unverified:" + role)
        roles[role] = {"email": email, "basecamp_id": row["basecamp_id"], "name": row.get("name") or email,
                       "first_name": first_word(row.get("name")) or email.split("@")[0]}
    bc = catalog.get("basecamp") or {}
    _need(type(bc.get("account_id")) is int and type(bc.get("hamle_person_id")) is int, "prospect_catalog_invalid")
    routes = catalog.get("brand_routes") or {}
    members = {p["basecamp_id"] for p in people.values() if p.get("basecamp_id")}
    brands, forbidden = {}, {}
    for key, route in routes.items():
        if key in cfg["own_brand_keys"] or not isinstance(route, dict):
            continue
        names = {fold(n).strip() for n in [route.get("brand_name")] + list(route.get("aliases") or [])
                 if isinstance(n, str) and len(n.strip()) >= 3}
        forbidden[key] = sorted(names)
        lead = route.get("rep_person_id")
        brands[key] = {"key": key, "name": route.get("brand_name") or key,
                       "lead_id": lead if lead in members else roles["new_business_lead"]["basecamp_id"]}
    client_domains = {}
    for domain, key in cfg["client_domains"].items():
        _need(key in brands, "prospect_client_domain_brand_unknown")
        client_domains[domain.lower()] = key
    free = BASE_FREE_MAIL | set(cfg["free_mail_domains"])
    if book is not None:  # public client addresses are exact matches only; their domains stay free mail
        from v11_client_watch import PUBLIC_DOMAINS
        free |= PUBLIC_DOMAINS
    return {"cfg": cfg, "roles": roles, "account_id": bc["account_id"], "hamle_person": bc["hamle_person_id"],
            "project_id": cfg["basecamp"]["project_id"], "brands": brands, "forbidden": forbidden,
            "client_domains": client_domains, "free": frozenset(free), "agency": set(people),
            "agency_ids": members, "since_ms": int(datetime.fromisoformat(cfg["since"]).timestamp() * 1000)}


# ------------------------------------------------------------------ known people (Firestore, never in git)
PEOPLE_FIELDS = {"name", "company", "title", "where_met", "note", "emails", "domains"}


def validate_people(doc):
    _need(isinstance(doc, dict) and doc.get("schema_version") == 1 and isinstance(doc.get("people"), list)
          and len(doc["people"]) <= 500, "prospect_people_invalid")
    out = []
    for row in doc["people"]:
        _need(isinstance(row, dict) and set(row) <= PEOPLE_FIELDS and isinstance(row.get("name"), str)
              and 2 <= len(row["name"].strip()) <= 120, "prospect_people_row_invalid")
        for field, limit in (("company", 120), ("title", 120), ("where_met", 200), ("note", 400)):
            _need(row.get(field) is None or (isinstance(row[field], str) and len(row[field]) <= limit),
                  "prospect_people_row_invalid")
        emails = [e.strip().lower() for e in row.get("emails") or []]
        domains = [d.strip().lower() for d in row.get("domains") or []]
        _need(all(EMAIL.fullmatch(e) for e in emails) and all(DOMAIN.fullmatch(d) for d in domains)
              and (emails or domains), "prospect_people_row_unmatchable")
        out.append(dict(row, emails=emails, domains=domains))
    return out


def load_people(ops):
    doc = ops._run(lambda tx, _: tx.get(PEOPLE_KEY), allow_expired=True)
    if doc is None:
        return []
    _need(doc.get("kind") == "prospect_known_people", "prospect_people_invalid")
    return validate_people(doc)


def match_person(people, address, display_name, domain):
    """Exact address first; otherwise the same full name at one of the row's company domains."""
    for row in people:
        if address in row["emails"]:
            return row
    name = " ".join(fold(display_name).split()) if display_name else ""
    if len(name) < 3:
        return None
    for row in people:
        if domain in row["domains"] and " ".join(fold(row["name"]).split()) == name:
            return row
    return None


def import_people(ops, doc, *, actor, settings):
    _need(isinstance(actor, str) and actor in settings["agency"], "actor_invalid")
    rows = validate_people(doc)
    clean = {"kind": "prospect_known_people", "schema_version": 1, "people": copy.deepcopy(doc["people"])}
    digest = scope_digest(clean)

    def op(tx, now):
        old = tx.get(PEOPLE_KEY)
        if old is not None and old.get("sha256") == digest:
            return {"status": "already_current", "sha256": digest}
        tx.put(PEOPLE_KEY, dict(clean, sha256=digest, imported_at=now, actor=actor))
        return {"status": "prospect_people_imported", "sha256": digest}
    return dict(ops._run(op, allow_expired=True), people=len(rows))


# ------------------------------------------------------------------ pre-filter (code only, no model)
_COMMENT = re.compile(r"\([^()]*\)")


def _auth_rows(values, serv):
    """Results of our own provider's Authentication-Results lines: [(method, result, props)]."""
    out = []
    for value in values or []:
        text = _COMMENT.sub(" ", str(value or ""))
        parts = [p.strip() for p in text.split(";")]
        head = parts[0].split() if parts and parts[0] else []
        if head[:1] != [serv]:  # only the line our own provider added counts (a sender can forge others)
            continue
        for part in parts[1:]:
            m = re.match(r"(spf|dkim|dmarc)\s*=\s*([a-z]+)(.*)", part, re.I | re.S)
            if m:
                props = dict((k.lower(), v.strip().lower()) for k, v in re.findall(r"([a-z.]+)\s*=\s*(\S+)", m[3], re.I))
                out.append((m[1].lower(), m[2].lower(), props))
    return out


def _aligned(a, b):
    a, b = (a or "").lstrip("@").lower(), (b or "").lstrip("@").lower()
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def auth_verdict(values, sender, serv):
    """None when the sender's domain is verified by our provider; otherwise a skip reason."""
    rows = _auth_rows(values, serv)
    if not rows:
        return "auth_results_missing"
    if any(result in AUTH_BAD for _, result, _ in rows):
        return "auth_fail"
    domain = sender.rsplit("@", 1)[-1]
    for method, result, props in rows:
        if result != "pass":
            continue
        if method == "dmarc":
            return None
        if method == "dkim" and _aligned((props.get("header.i") or "").split("@")[-1] or props.get("header.d"), domain):
            return None
        if method == "spf" and _aligned((props.get("smtp.mailfrom") or "").split("@")[-1], domain):
            return None
    return "auth_unverified"


def prefilter(headers, sender, settings):
    """headers: {lower-case name: [values]} of the newest message -> skip reason or None."""
    cfg = settings["cfg"]
    h = {k.lower(): [str(v) for v in (vals or [])] for k, vals in (headers or {}).items()}
    local, _, domain = str(sender or "").partition("@")
    if not EMAIL.fullmatch(str(sender or "")):
        return "sender_invalid"
    auto = h.get("auto-submitted") or []
    if any(v.strip().lower() != "no" for v in auto):
        return "auto_submitted"
    if any(v.strip() for v in (h.get("x-autoreply") or []) + (h.get("x-autorespond") or [])):
        return "auto_reply"
    if any(v.strip().lower() in ("bulk", "list", "junk") for v in h.get("precedence") or []):
        return "bulk"
    if h.get("list-unsubscribe") or h.get("list-id"):
        return "list_mail"
    if NOREPLY_LOCAL.search(local) or local.lower() in cfg["ignore_locals"]:
        return "noreply_sender"
    if any(domain == d or domain.endswith("." + d) for d in cfg["ignore_domains"]):
        return "ignored_domain"
    return auth_verdict(h.get("authentication-results"), sender, cfg["auth_serv_id"])


def excluded(sender, settings, book):
    """Agency staff, the mailbox itself and known client contacts (exact or company domain) are not prospects."""
    address = str(sender or "").strip().lower()
    domain = address.rsplit("@", 1)[-1]
    return (address == HAMLE or domain == settings["cfg"]["agency_domain"]
            or domain.endswith("." + settings["cfg"]["agency_domain"])
            or address in settings["agency"] or (book is not None and book.match(address) is not None))


def domain_kind(domain, settings):
    """('individual', None) | ('client', brand_key) | ('corporate', None) -- the company comes ONLY from here."""
    domain = domain.lower()
    if domain in settings["free"]:
        return "individual", None
    for d, key in settings["client_domains"].items():
        if domain == d or domain.endswith("." + d):
            return "client", key
    return "corporate", None


_NAME = re.compile(r"[^\W\d_](?:[^\W\d_]|['’-]){1,29}")


def first_name(display, known=None):
    """The greeting name: known person's first name, else the From display name's first word (Last, First too)."""
    raw = (known or {}).get("name") or display
    if not raw:
        return None
    raw = str(raw).strip().strip("\"'")
    if "@" in raw:
        return None
    if "," in raw:
        raw = raw.split(",", 1)[1].strip()
    token = raw.split()[0] if raw.split() else ""
    if not _NAME.fullmatch(token):
        return None
    if token.islower():
        head = {"i": "İ", "ı": "I"}.get(token[0], token[0].upper())
        token = head + token[1:]
    return token


# ------------------------------------------------------------------ reply checks (code)
PRICE = re.compile(r"[₺$€£]|(?<![a-z0-9çğıöşü])(?:tl|usd|eur|euro|euros|dolar|dollars?|lira|fiyat\w*|ücret\w*|"
                   r"ucret\w*|maliyet\w*|bütçe\w*|butce\w*|indirim\w*|bedava|ücretsiz|price\w*|pricing|costs?|fees?|"
                   r"discount\w*|free of charge|kdv|vat)(?![a-z0-9çğıöşü])", re.I)
DATE = re.compile(r"\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b|\b\d{1,2}:\d{2}\b|"
                  r"\b\d{1,2}\s+(?:ocak|şubat|mart|nisan|mayıs|haziran|temmuz|ağustos|eylül|ekim|kasım|aralık|"
                  r"january|february|march|april|may|june|july|august|september|october|november|december)\b|"
                  r"(?<![a-zçğıöşü])(?:pazartesi|salı|çarşamba|perşembe|cumartesi|pazar|monday|tuesday|wednesday|"
                  r"thursday|friday|saturday|sunday|yarın|tomorrow|garanti\w*|guarantee\w*|söz veri\w*|promise\w*|"
                  r"taahhüt\w*|en geç|at the latest)(?![a-zçğıöşü])", re.I)
LINKS = re.compile(r"https?://|www\.|[a-z0-9._%+-]+@[a-z0-9-]+\.|\+?\d[\d ()-]{8,}\d", re.I)
GENDER = re.compile(r"(?<![a-zçğıöşü])(?:hanım|hanim|bey|beyefendi|hanımefendi|mr|mrs|ms|sir|madam)(?![a-zçğıöşü])",
                    re.I)
RESEARCH_WORDS = re.compile(r"araştır|incele|linkedin|google|internette|web sitenize bak|sitenize bak|profilin|"
                            r"research|looked (?:you|into|up)|we checked|your profile|online", re.I)


def brand_hits(text, settings, allowed=None):
    blob = " " + re.sub(r"[^0-9a-z]+", " ", fold(text)) + " "
    out = []
    for key, names in settings["forbidden"].items():
        if key == allowed:
            continue
        if any(" " + re.sub(r"[^0-9a-z]+", " ", n).strip() + " " in blob for n in names):
            out.append(key)
    return out


def check_reply(text, *, settings, language, first, category, kind, brand_key, include_research):
    """Reasons the reply may NOT go out (empty list = it may)."""
    from v11_client_watch import internal_terms
    cfg = settings["cfg"]
    t = str(text or "").replace("\r\n", "\n").strip()
    out = []
    sig = cfg["signatures"][language]
    if not t.endswith(sig):
        out.append("imza eksik ya da farklı")
    lines = t.split("\n")
    greet = ("Merhaba",) if language == "tr" else ("Hi", "Hello", "Dear")
    if not lines[0].startswith(greet):
        out.append("selamlama eksik")
    elif first and first not in lines[0]:
        out.append("ilk adla hitap yok")
    body = t[:-len(sig)] if t.endswith(sig) else t
    sentence = cfg["approved_sentences"]["research_request"][language]
    if include_research and sentence not in body:
        out.append("onaylı araştırma cümlesi yok")
    if not include_research and any(s in body for s in cfg["approved_sentences"]["research_request"].values()):
        out.append("araştırma cümlesi yanlış yerde")
    scan = body.replace(sentence, " ")
    if internal_terms(scan):
        out.append("iç ifade: " + ", ".join(internal_terms(scan)))
    if GENDER.search(scan):
        out.append("Hanım/Bey ya da cinsiyetli hitap")
    if PRICE.search(scan):
        out.append("fiyat ya da para ifadesi")
    if DATE.search(scan):
        out.append("tarih, gün ya da söz ifadesi")
    if LINKS.search(scan):
        out.append("bağlantı, adres ya da telefon")
    if RESEARCH_WORDS.search(scan):
        out.append("araştırma yapıldığını ima ediyor")
    hits = brand_hits(scan, settings, allowed=brand_key if kind == "client" else None)
    if hits:
        out.append("başka marka adı geçiyor")
    if kind != "individual":
        owner = settings["roles"]["owner"]["first_name"]
        if owner not in scan:
            out.append("toplantıda kimin olacağı yazmıyor")
    if len(t) > cfg["limits"]["reply_max_chars"] or len(lines) > 20:
        out.append("cevap çok uzun")
    return out


def _mail_text(source):
    from v11_client_watch import latest_part
    return latest_part(source.get("text"))[:MAX_TEXT]


def _quote(text):
    return "\n".join("> " + ln for ln in str(text or "").strip().split("\n"))[:MAX_QUOTE]


def _url_ok(url, evidence):
    from v11_trend_search import normalize_url
    u = normalize_url(url)
    return u if u is not None and u in evidence else None


def company_facts(output, domain):
    """Research output -> company facts the reply may use (sourced or from the domain's own site only)."""
    from v11_trend_search import normalize_url
    if not isinstance(output, dict):
        return None
    evidence = set(output.get("evidence") or [])
    c = output.get("company") or {}
    url = normalize_url(c.get("url"))
    host = (url or "").split("/")[2] if url else ""
    if url is None or not (url in evidence or _aligned(host, domain)):
        return None
    return {"name": _line(c.get("name"), 80) or None, "sector": _line(c.get("sector"), 120) or None,
            "summary": _line(c.get("summary"), 400) or None, "url": url}


def person_facts(output):
    """Internal-only person facts: a title only on a clear, sourced, professional (non personal-social) match."""
    if not isinstance(output, dict):
        return None
    p = output.get("person") or {}
    url = _url_ok(p.get("url"), set(output.get("evidence") or []))
    host = (url or "").split("/")[2] if url else ""
    social = any(host == s or host.endswith("." + s) for s in PERSONAL_SOCIAL)
    clear = p.get("match") == "clear" and url is not None and not social and bool(p.get("title"))
    return {"title": _line(p.get("title"), 120) if clear else None, "url": url if not social else None,
            "match": p.get("match") if p.get("match") in ("clear", "unclear", "not_found", "not_searched") else None,
            "dropped_social": social}


# ------------------------------------------------------------------ model input (isolated) + schemas
def _schemas():
    from v11_chain_models import _arr, _nstr, _obj, _str
    enum = lambda values: {"type": "string", "enum": list(values)}  # noqa: E731
    classify = _obj({"category": enum(CATEGORIES), "language": enum(LANGUAGES), "request_summary": _str(300),
                     "research_target": enum(("organisation", "person", "none")), "claimed_company": _nstr(80)})
    research = _obj({"company": _obj({"name": _nstr(80), "sector": _nstr(120), "summary": _nstr(400),
                                      "url": _nstr(300)}),
                     "person": _obj({"title": _nstr(120),
                                     "match": enum(("clear", "unclear", "not_found", "not_searched")),
                                     "url": _nstr(300)})})
    reply = _obj({"reply": _str(2500)})
    fact = _obj({"text": _str(400), "url": _str(300)})
    report = _obj({"company": _obj({"name": _nstr(120), "sector": _nstr(160), "facts": _arr(fact, 8)}),
                   "channels": _arr(_obj({"platform": enum(PLATFORMS), "url": _nstr(300),
                                          "status": enum(("active", "weak", "inactive", "none", "unknown")),
                                          "facts": _arr(fact, 5)}), 6),
                   "message": _obj({"umbrella_message": _nstr(400), "site_keywords": _arr(_str(60), 15),
                                    "facts": _arr(fact, 6)}),
                   "google": _arr(_obj({"query": _str(120),
                                        "result": enum(("first_page", "not_first_page", "unknown")),
                                        "url": _nstr(300)}), 6),
                   "competitors": _arr(_obj({"name": _str(120), "domain": _nstr(120), "facts": _arr(fact, 6)}), 3),
                   "campaigns": _arr(_obj({"text": _str(300), "url": _str(300), "date": _nstr(10)}), 6),
                   "regulation": _arr(fact, 3),
                   "audience": _obj({"segments": _arr(_obj({"name": _str(80), "description": _str(300),
                                                            "signals": _arr(fact, 3)}), 3),
                                     "facts": _arr(fact, 5)}),
                   "sector": _obj({"trends": _arr(fact, 5), "expectations": _arr(fact, 4)})})
    rag = enum(("green", "amber", "red"))
    writer = _obj({"score": _obj({"total": {"type": "integer", "minimum": 0, "maximum": 100},
                                  "parts": _arr(_obj({"name": enum(SCORE_PARTS),
                                                      "score": {"type": "integer", "minimum": 0, "maximum": 100},
                                                      "note": _str(300)}), 6)}),
                   "headline": _str(300), "summary": _arr(_str(400), 3),
                   "sections": _arr(_obj({"key": enum(k for k, _ in SECTIONS), "rag": rag,
                                          "paragraphs": _arr(_str(900), 2), "bullets": _arr(_str(400), 6)}),
                                    len(SECTIONS)),
                   "competitor_rows": _arr(_obj({"metric": _str(120), "us": _str(200),
                                                 "them": _arr(_str(200), 3)}), 6),
                   "moves": _arr(_obj({"title": _str(160), "why": _str(500), "what": _str(600),
                                       "measure": _str(300), "first_30_days": _str(400)}), 3),
                   "audience_segments": _arr(_obj({"name": _str(80), "share": enum(("high", "medium", "low")),
                                                   "why": _str(300)}), 3),
                   "plan_30": _arr(_obj({"title": _str(120), "what": _str(400), "target": _str(200)}), 4),
                   "plan_90": _arr(_obj({"title": _str(120), "what": _str(400), "target": _str(200)}), 4),
                   "closing_summary": _str(900),
                   "meeting_notes": _arr(_str(300), 6), "mail": _str(5000)})
    parts = {name: _obj({k: report["properties"][k] for k in keys}) for name, keys in REPORT_PARTS}
    steps = {name: _obj({k: writer["properties"][k] for k in keys}) for name, keys in WRITER_STEPS}
    return {"classify": classify, "research": research, "reply": reply, "report": report, "writer": writer,
            "report_parts": parts, "writer_steps": steps}


REPLY_INPUT_KEYS = ("today", "language", "category", "domain_kind", "first_name", "company", "known_brand",
                    "known_person", "include_research_sentence", "subject", "mail_text")


def reply_input(*, today, language, category, kind, first, company, brand_name, known, include_research, source):
    """The ONLY data the reply model sees: no brand memory, no client data, no internal notes."""
    return {"today": today.isoformat(), "language": language, "category": category, "domain_kind": kind,
            "first_name": first,
            "company": ({k: company.get(k) for k in ("name", "sector", "summary")} if company else None),
            "known_brand": brand_name if kind == "client" else None,
            "known_person": ({"where_met": known.get("where_met")} if known and known.get("where_met") else None),
            "include_research_sentence": bool(include_research),
            "subject": _line(source.get("subject"), 200), "mail_text": _mail_text(source)}


def render_prompt(kind, settings):
    try:
        text = PROMPTS[kind].read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ProspectError("prospect_prompt_unavailable") from None
    cfg = settings["cfg"]
    owner = settings["roles"]["owner"]["first_name"]
    values = {"{{AGENCY}}": cfg["agency_name"], "{{MAILBOX}}": cfg["mailbox"],
              "{{SIGNATURE_TR}}": cfg["signatures"]["tr"], "{{SIGNATURE_EN}}": cfg["signatures"]["en"],
              "{{OWNER_MEETING_TR}}": meeting_phrase(owner, "tr"), "{{OWNER_MEETING_EN}}": meeting_phrase(owner, "en"),
              "{{RESEARCH_SENTENCE_TR}}": cfg["approved_sentences"]["research_request"]["tr"],
              "{{RESEARCH_SENTENCE_EN}}": cfg["approved_sentences"]["research_request"]["en"]}
    for k, v in values.items():
        text = text.replace(k, v)
    return text


def model_sources(ops, load_key, catalog, settings, *, routing=None, client=None):
    """classify / research / reply / report callables; one durable chance each (ModelGate), budget-gated."""
    import model as base
    from v11_chain_live import budget_gate
    from v11_chain_models import ModelGate, internal_cap_microdollars, key_for, reservation_for, source_digest, validate
    from v11_trend_search import _routing, anthropic_research, route_fingerprint
    routing = _routing(routing)
    cap = internal_cap_microdollars(catalog)
    routes = {k: routing.route(r) for k, r in ROLES.items()}
    _need(all(r.provider == "anthropic" for r in routes.values()), "prospect_route_not_claude")
    _need(routes["research"].web_search and routes["report"].web_search and not routes["writer"].web_search
          and not routes["classify"].web_search and not routes["reply"].web_search, "prospect_route_tools_invalid")
    holds = {k: reservation_for(r) for k, r in routes.items()}
    gates = {k: budget_gate(ops, holds[k], cap) for k in routes}
    schemas = _schemas()

    def gate():
        return ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)

    def make(kind):
        route = routes[kind]

        def call(request):
            request = dict(request)
            attempt = request.pop("_attempt", 0)  # an operator re-run of a failed write is a new single chance
            schema = (schemas["report_parts"][request["part"]] if kind == "report" else
                      schemas["writer_steps"][request["step"]] if kind == "writer" else schemas[kind])
            _need(gates[kind](), "model_budget_unavailable")
            system = render_prompt(kind, settings)
            digest = source_digest("prospect_" + kind, request, route_fingerprint(route, PROMPTS[kind]), _sha(system),
                                   *([MAX_INPUT[kind], attempt] if kind in MAX_INPUT else []))
            if route.web_search:
                def once():
                    return _research_call(anthropic_research, route, system, request, schema,
                                          key_for(load_key, "anthropic"), client, kind,
                                          reader=_fit_reader(schema) if kind in TOLERANT else None)
            else:
                def once():
                    key = key_for(load_key, "anthropic")
                    try:
                        check = (lambda v: fit_checked(v, schema)) if kind in TOLERANT else \
                            (lambda v: validate(v, schema) or v)
                        return base.anthropic_json(route, system, request, schema, api_key=key, client=client,
                                                   validator=check, max_input_bytes=MAX_INPUT.get(kind,
                                                                                                  base.MAX_INPUT_BYTES))
                    finally:
                        del key
            return gate().once("prospect_" + kind, digest, once, provider="anthropic", hold=holds[kind])
        call.timeout_seconds = route.timeout_seconds
        return call
    return SimpleNamespace(classify=make("classify"), research=make("research"), reply=make("reply"),
                           report=make("report"), writer=make("writer"), budget_ok=lambda kind: gates[kind](),
                           models={k: r.model for k, r in routes.items()})


class _PromptText:
    """anthropic_research reads its system prompt from a path; this hands it the rendered text instead."""
    def __init__(self, text):
        self.text = text

    def read_text(self, encoding="utf-8"):
        return self.text


def _research_call(research, route, system, request, schema, key, client, kind, reader=None):
    try:
        return research(route, _PromptText(system), request, schema, api_key=key, client=client,
                        code="invalid_prospect_" + kind + "_schema", reader=reader)
    finally:
        del key


# The long report steps (owner 2026-10-16): one over-long sentence or one fact without a link must not throw away a
# 15-minute research. Their answers are fitted to the schema (clip, drop bad items) and then strictly validated.
TOLERANT = ("report", "writer")


class _Drop(Exception):
    pass


def fit(value, schema):
    """value fitted to a v11_chain_models schema: strings clipped, lists cut to maxItems with bad items dropped,
    unknown enum values -> null/'unknown', missing optional values -> null/[]; a required value that cannot be
    fitted drops the enclosing list item (or fails the whole answer at the top)."""
    kinds = schema.get("type")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    nullable = "null" in kinds
    if value is None or value == "":
        if nullable:
            return None
        if "array" in kinds:
            return []
        raise _Drop()
    if "object" in kinds:
        if not isinstance(value, dict):
            raise _Drop()
        return {k: fit(value.get(k), sub) for k, sub in schema["properties"].items()}
    if "array" in kinds:
        if not isinstance(value, list):
            raise _Drop()
        out = []
        for item in value:
            try:
                out.append(fit(item, schema["items"]))
            except _Drop:
                continue
        return out[:schema.get("maxItems", len(out))]
    if "integer" in kinds:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise _Drop()
        try:
            n = int(round(float(value)))
        except ValueError:
            raise _Drop() from None
        return max(schema.get("minimum", n), min(schema.get("maximum", n), n))
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise _Drop()
    text = " ".join(str(value).split()) if "enum" in schema else str(value).strip()
    if "enum" in schema:
        if text in schema["enum"]:
            return text
        if nullable:
            return None
        if "unknown" in schema["enum"]:
            return "unknown"
        raise _Drop()
    if not text:
        if nullable:
            return None
        raise _Drop()
    limit = schema.get("maxLength")
    if limit and len(text) > limit:
        cut = text[:limit - 1]
        cut = cut[:cut.rfind(" ")] if " " in cut[limit // 2:] else cut
        text = cut.rstrip(" ,;:") + "…"
    return text


def fit_checked(value, schema):
    from v11_chain_models import validate
    try:
        out = fit(value, schema)
    except _Drop:
        raise ProspectError("prospect_answer_unusable") from None
    validate(out, schema)
    return out


def _fit_reader(schema):
    """anthropic_research reader: the JSON object in the text (a sentence around it is tolerated), fitted."""
    from v11_trend_search import strict_json_text

    def read(text):
        t = str(text or "").strip()
        try:
            value = strict_json_text(t)
        except Exception:
            start, end = t.find("{"), t.rfind("}")
            _need(0 <= start < end, "prospect_answer_not_json")
            value = json.loads(t[start:end + 1])
        return fit_checked(value, schema)
    return read


# ------------------------------------------------------------------ Basecamp (the configured project only)
def basecamp_http(settings, *, send):
    from v11_nb_tracking import NbBasecamp

    class ProspectBasecamp(NbBasecamp):
        """Allow-listed: the project and its to-do lists, to-dos and comments in settings['project_id']."""
        def __init__(self):
            super().__init__(settings["account_id"], send=send)
            a, p = settings["account_id"], settings["project_id"]
            b = rf"/{a}/buckets/{p}"
            self.allowed = {"GET": (rf"/{a}/projects/{p}", rf"{b}/todosets/[1-9][0-9]*/todolists",
                                    rf"{b}/todolists/[1-9][0-9]*", rf"{b}/todos/[1-9][0-9]*",
                                    rf"{b}/recordings/[1-9][0-9]*/comments", rf"{b}/comments/[1-9][0-9]*"),
                            "POST": (rf"{b}/todolists/[1-9][0-9]*/todos", rf"{b}/recordings/[1-9][0-9]*/comments",
                                     rf"{b}/todosets/[1-9][0-9]*/todolists")}
    return ProspectBasecamp()


def _html(text):
    from v11_nb_tracking import _html as nb_html
    return nb_html(text)


def _plain(html):
    """The visible words of rich HTML (what a readback compares)."""
    from v11_nb_tracking import _Text
    p = _Text()
    p.feed(html)
    p.close()
    return "".join(p.parts)


# ------------------------------------------------------------------ Dijital Ayak İzi Raporu (owner 2026-10-16)
RAG = {"green": "🟢", "amber": "🟡", "red": "🔴"}
REF = re.compile(r"\[(\d{1,3}|Ö)\]")
ACCENT, INK = "#EBE656", "#111111"


def _compact(measured):
    """The measured data a model sees: no raw page bodies, bounded lists."""
    m = json.loads(json.dumps(measured, ensure_ascii=False, default=str))
    for ch in (m.get("channels") or {}).values():
        for k in ("recent_titles", "recent_captions", "top_tags", "top_hashtags"):
            if isinstance(ch.get(k), list):
                ch[k] = ch[k][:10]
    if isinstance(m.get("site_checks"), str):
        m["site_checks"] = m["site_checks"][:1500]
    return m


def number_sources(research, measured, domain):
    """Every https URL the research cites (only those its searches really returned, or the brand's own site) plus
    the measured channel links, numbered once; facts get 'source': n instead of 'url'."""
    from v11_trend_search import normalize_url
    evidence = set(research.get("evidence") or [])
    urls = []

    def num(url):
        u = normalize_url(url) if url else None
        host = (u or "").split("/")[2] if u else ""
        if u is None or not (u in evidence or _aligned(host, domain)):
            return None
        if u not in urls:
            urls.append(u)
        return urls.index(u) + 1
    for ch in (measured.get("channels") or {}).values():
        num(ch.get("url"))

    def facts(rows):
        out = []
        for f in rows or []:
            n = num(f.get("url"))
            if n:
                out.append({"text": f.get("text"), "source": n})
        return out
    r = research
    clean = {"company": {**{k: (r.get("company") or {}).get(k) for k in ("name", "sector")},
                         "facts": facts((r.get("company") or {}).get("facts"))},
             "channels": [{"platform": c.get("platform"), "status": c.get("status"),
                           "source": num(c.get("url")), "facts": facts(c.get("facts"))} for c in r.get("channels") or []],
             "message": {"umbrella_message": (r.get("message") or {}).get("umbrella_message"),
                         "site_keywords": (r.get("message") or {}).get("site_keywords"),
                         "facts": facts((r.get("message") or {}).get("facts"))},
             "google": [{"query": g.get("query"), "result": g.get("result"), "source": num(g.get("url"))}
                        for g in r.get("google") or []],
             "competitors": [{"name": c.get("name"), "domain": c.get("domain"), "facts": facts(c.get("facts"))}
                             for c in r.get("competitors") or []],
             "campaigns": [{"text": c.get("text"), "date": c.get("date"), "source": num(c.get("url"))}
                           for c in r.get("campaigns") or [] if num(c.get("url"))],
             "regulation": facts(r.get("regulation")),
             "audience": {"segments": [{"name": g.get("name"), "description": g.get("description"),
                                        "signals": facts(g.get("signals"))}
                                       for g in (r.get("audience") or {}).get("segments") or []],
                          "facts": facts((r.get("audience") or {}).get("facts"))},
             "sector": {"trends": facts((r.get("sector") or {}).get("trends")),
                        "expectations": facts((r.get("sector") or {}).get("expectations"))}}
    return urls, clean


def _refs(text, sources):
    """Escaped text with [n] turned into small source links; unknown numbers are dropped."""
    out, last = [], 0
    t = str(text or "")
    for m in REF.finditer(t):
        out.append(escape(t[last:m.start()], quote=True))
        n = m.group(1)
        if n == "Ö":
            out.append("<em>[Hamle ölçümü]</em>")
        elif 1 <= int(n) <= len(sources):
            out.append(f'<a href="{escape(sources[int(n) - 1], quote=True)}">[{n}]</a>')
        elif out:  # an unknown number is dropped with the space before it
            out[-1] = out[-1].rstrip()
        last = m.end()
    out.append(escape(t[last:], quote=True))
    return "".join(out)


def _sections(written):
    by = {s["key"]: s for s in written.get("sections") or []}
    return [(k, title, by[k]) for k, title in SECTIONS if k in by]


def short_report(label, written, sources):
    """When the full report cannot land on the to-do: the score, the headline and the 3 findings (the designed mail
    and its PDF carry the whole report)."""
    e = lambda t: escape(str(t), quote=True)  # noqa: E731
    sc = written.get("score") or {}
    return (f"<h1>Dijital Ayak İzi Raporu · {e(label)}</h1>"
            f"<strong>Dijital Ayak İzi Skoru: {e(sc.get('total'))}/100</strong><br>"
            f"<br><strong>{_refs(written.get('headline'), sources)}</strong><br>"
            "<br><strong>Öne çıkan 3 bulgu</strong><ul>"
            + "".join(f"<li>{_refs(x, sources)}</li>" for x in written.get("summary") or []) + "</ul>"
            "<br>Raporun tamamı burada sığmadı; tasarımlı hali mailinizde, PDF eki ile birlikte.")


def render_report(label, written, sources, measured, *, problems, test, today):
    """Basecamp rich text (h1, strong, lists, links, quote): headings, spacing, never cut."""
    e = lambda t: escape(str(t), quote=True)  # noqa: E731
    r = lambda t: _refs(t, sources)  # noqa: E731
    sc = written.get("score") or {}
    parts = [f"<h1>Dijital Ayak İzi Raporu · {e(label)}</h1>",
             f"<strong>Dijital Ayak İzi Skoru: {e(sc.get('total'))}/100</strong>",
             "<ul>" + "".join(f"<li><strong>{e(p['name'])}: {e(p['score'])}</strong> · {r(p['note'])}</li>"
                              for p in sc.get("parts") or []) + "</ul>",
             f"<br><strong>{r(written.get('headline'))}</strong><br>",
             "<br><strong>Öne çıkan 3 bulgu</strong><ul>" + "".join(f"<li>{r(x)}</li>" for x in written.get("summary") or [])
             + "</ul>"]
    for _, title, sec in _sections(written):
        body = "".join(f"{r(p)}<br><br>" for p in sec.get("paragraphs") or [])
        if sec.get("bullets"):
            body += "<ul>" + "".join(f"<li>{r(b)}</li>" for b in sec["bullets"]) + "</ul>"
        parts.append(f"<br><strong>{RAG.get(sec.get('rag'), '')} {e(title)}</strong><br>{body}")
    if written.get("audience_segments"):
        parts.append("<br><strong>Hedef kitle segmentleri (tahmin)</strong><ul>" + "".join(
            f"<li><strong>{e(x['name'])}</strong> ({SHARE.get(x['share'], '')}) · {r(x['why'])}</li>"
            for x in written["audience_segments"]) + "</ul>")
    rows = written.get("competitor_rows") or []
    if rows:
        parts.append("<br><strong>Yan yana</strong><ul>" + "".join(
            f"<li><strong>{e(x['metric'])}</strong> · Siz: {r(x['us'])} · " + " · ".join(r(t) for t in x["them"]) + "</li>"
            for x in rows) + "</ul>")
    parts.append("<br><strong>Biz olsak ilk 3 hamlemiz</strong><ol>" + "".join(
        f"<li><strong>{e(m['title'])}</strong><br>Neden: {r(m['why'])}<br>Ne yaparız: {r(m['what'])}<br>"
        f"Nasıl ölçeriz: {r(m['measure'])}<br>İlk 30 gün: {r(m['first_30_days'])}</li>"
        for m in written.get("moves") or []) + "</ol>")
    for key, title in (("plan_30", "Biz yönetseydik · ilk 30 gün (kısa vade)"),
                       ("plan_90", "Biz yönetseydik · 90 gün (orta vade)")):
        if written.get(key):
            parts.append(f"<br><strong>{title}</strong><ul>" + "".join(
                f"<li><strong>{e(x['title'])}</strong> · {r(x['what'])} <em>Hedef: {r(x['target'])}</em></li>"
                for x in written[key]) + "</ul>")
    if written.get("closing_summary"):
        parts.append(f"<br><strong>Son söz · yönetici özeti</strong><br>{r(written['closing_summary'])}<br>")
    if written.get("meeting_notes"):
        parts.append("<br><strong>Görüşme notu (yalnız ekip içi)</strong><ul>"
                     + "".join(f"<li>{r(x)}</li>" for x in written["meeting_notes"]) + "</ul>")
    mail = str(written.get("mail") or "").strip()
    parts.append("<br><strong>Kuruma gidecek mail taslağı</strong><br>"
                 + (f"<blockquote>{'<br>'.join(e(x) for x in mail.split(chr(10)))}</blockquote>" if mail else "(yok)"))
    if sources:
        parts.append("<br><strong>Kaynaklar</strong><ol>" + "".join(
            f'<li><a href="{e(u)}">{e(u)}</a></li>' for u in sources) + "</ol>")
    unmeasured = [PLATFORM_LABEL.get(p, p) for p, c in (measured.get("channels") or {}).items() if not c.get("measured")]
    note = f"Ölçüm tarihi {e(today)}; son 6 ay."
    if unmeasured:
        note += " Resmi arayüzle ölçülemeyen kanallar (kısmi): " + e(", ".join(unmeasured)) + "."
    if problems:
        todo = ("Mail taslağı kontrolden geçmedi (" + "; ".join(problems) + "). Göndermek istersen düzeltilmiş metni "
                "\"Hamle gönder\" satırının altına yaz.")
    elif test:
        todo = ("Bu bir TEST isteği: \"Hamle gönder\" yazarsan rapor kuruma değil yalnız sana gelir. "
                "Göndermeyeyim dersen \"Hamle gönderme\" yaz.")
    else:
        todo = ("Kuruma göndermem için bu to-do'ya \"Hamle gönder\" yaz (düzeltmek istersen metni bu satırın "
                "altına yaz). Göndermeyeyim dersen \"Hamle gönderme\" yaz.")
    parts.append(f"<br><strong>Sıradaki adım</strong><br>{e(todo)}<br><em>{note} Bu rapor yalnız ekip içindir; "
                 "kuruma kendiliğinden gitmez.</em>")
    return "<div>" + "".join(parts) + "</div>"


SHARE = {"high": "yüksek pay", "medium": "orta pay", "low": "düşük pay"}
PLATFORM_LABEL = {"instagram": "Instagram", "linkedin": "LinkedIn", "youtube": "YouTube", "tiktok": "TikTok",
                  "x": "X", "facebook": "Facebook"}


def render_report_mail(label, written, sources, measured, *, today, link, client=False, full=False, attached=False):
    """The same report as the designed e-mail the owner picked (2026-10-16, "Neon Kontrol Odası")."""
    import v11_report_mail
    return v11_report_mail.render(label, written, sources, measured, today=today, link=link,
                                  sections=_sections(written), refs=lambda t: _refs(t, sources), client=client,
                                  full=full, attached=attached)


def report_attachment(label, written, sources, measured, today):
    """The company's version of the report (no team notes), whole, as an A4 PDF mail attachment."""
    import v11_report_pdf
    html = render_report_mail(label, written, sources, measured, today=today, link="", client=True, full=True)
    return v11_report_pdf.attachment(label, today, v11_report_pdf.report_pdf(html))


SLIM_CHANNEL = ("measured", "url", "handle", "followers", "avg_per_month", "per_month", "last_post",
                "longest_gap_days", "formats", "engagement_rate_pct")


def slim_measured(measured):
    """Only what the report design draws (kept on the request until the report is sent)."""
    return {"channels": {p: {k: c.get(k) for k in SLIM_CHANNEL if k in c}
                         for p, c in (measured.get("channels") or {}).items() if isinstance(c, dict)}}


def _visible_matches(actual, expected):
    from v11_nb_tracking import text_matches
    return text_matches(actual, expected)


def _report_landed(actual, expected):
    """The long rich report: Basecamp may re-wrap or re-format it (and it is far past the 32 KB note check), so the
    readback proves its visible start and end landed on the comment."""
    if not isinstance(actual, str) or len(actual) > 3_000_000:
        return False
    got, want = " ".join(_plain(actual).split()), expected.split()
    if not want:
        return False
    return got == " ".join(want) or (" ".join(want[:40]) in got and " ".join(want[-20:]) in got)


# ------------------------------------------------------------------ records
def mail_key(message_id):
    return "prospect-watch-mail-" + _sha(message_id)


def addr_key(address):
    return "prospect-watch-addr-" + _sha(str(address).lower())


def day_key(day):
    return "prospect-watch-day-" + day.strftime("%Y%m%d")


def request_key(message_id):
    return "prospect-watch-request-" + _sha(message_id)


def domain_key(domain):
    return "prospect-watch-domain-" + _sha(domain.lower())


def index_key(month):
    return "prospect-watch-index-" + month


def _month(ts):
    return datetime.fromtimestamp(ts, ISTANBUL).strftime("%Y%m")


def _hm(local):
    return local.strftime("%H:%M")


def in_window(local, window):
    end = datetime.strptime(window["end"], "%H:%M") + timedelta(minutes=WINDOW_GRACE_MINUTES)
    last = "23:59" if end.day > 1 else end.strftime("%H:%M")  # the grace never wraps past midnight
    return window["start"] <= _hm(local) <= last


APPROVALS = ("onayliyorum", "onayladim", "onaylandi", "ok go", "okgo", "go")


def is_approval(content):
    """Exactly 'Onaylıyorum' / 'Onayladım' / 'Onaylandı' or 'OK GO' / 'Ok - go' / 'GO' (owner 2026-10-16; any case,
    spaces, dashes, punctuation and emoji; a question is never one; İ/ı aware). The caller accepts it only from the
    owner's verified Basecamp person."""
    from v11_client_reply import comment_text
    return "?" not in str(comment_text(content)) and _approval_form(content) in APPROVALS


def _approval_form(content):
    """Folded comment text without dashes, commas, final punctuation or emoji (owner 2026-10-16: 'OK GO 🚀')."""
    from v11_client_reply import comment_text
    text = re.sub(r"[-–—,]", " ", fold(comment_text(content)))
    text = re.sub(r"[^\w\s]+", " ", text)  # emoji, '!', '.', ':)' and the like never decide an approval
    return " ".join(text.split())


def near_approval(content):
    """Short owner comments that look like an approval but are not one ('ok go canım', 'onay'): Hamle explains."""
    words = _approval_form(content).split()
    return 0 < len(words) <= 4 and any(w in ("go", "onay", "ok", "tamam", "basla") or w.startswith("onayl")
                                       for w in words)


# ------------------------------------------------------------------ the watch
def request_todo(owner_name, estimate, source, sender, label, facts):
    """Title and body of the owner's research-request to-do (also shown by the preview)."""
    who = _line(source.get("sender_name"), 120)
    text = "\n".join([
        f"{owner_name}, bu kurum bir araştırma istedi. Hamle senin onayın olmadan hiçbir araştırma yapmaz.",
        f"Başlamam için bu to-do'ya yalnız \"Onaylıyorum\" yaz (yalnız senin yorumun geçerli). Rapor hazır olunca "
        "burada yorum olarak paylaşırım; kuruma ancak sen \"Hamle gönder\" yazarsan gider.", "",
        "İsteyen: " + (f"{who} <{sender}>" if who else sender),
        "Kurum (alan adından): " + label,
        "İstek: " + _line(facts.get("request_summary"), 300),
        "Tahmini maliyet ve süre: " + estimate, "",
        "Mailin kendisi:", _quote(_mail_text(source))])
    return _line("Araştırma isteği · " + label, 200), text


class ProspectWatch:
    def __init__(self, ops, *, settings, book, people, reader, models, http, reply_writer, copy_writer, clock,
                 web=None, psi_key=None, monotonic=time.monotonic, started_at=None, footprint=None):
        self.ops, self.s, self.cfg, self.book, self.people = ops, settings, settings["cfg"], book, people
        self.reader, self.models, self.http = reader, models, http
        self.reply_writer, self.copy_writer, self.clock = reply_writer, copy_writer, clock
        self.web, self.psi_key, self.monotonic, self.started_at = web, psi_key, monotonic, started_at
        self.footprint = footprint or {}  # {'youtube': key, 'instagram': {...}} (v11_footprint secrets; optional)
        self.generation, self.lists, self.started = None, None, None
        self.base = f"{API}/{settings['account_id']}/buckets/{settings['project_id']}"

    # ----- store helpers
    def _fence(self, tx, now):
        c = self.ops._control(tx)
        self.ops._open(c, self.generation, now)  # 'dur' / takeover / a new generation -> no write
        b = self.ops._budget(tx)
        _need(not b["overrun_hold"], "budget_overrun_hold")

    def _get(self, key):
        return self.ops._run(lambda tx, _: tx.get(key), allow_expired=True)

    def _update(self, key, token, **fields):
        def op(tx, now):
            row = tx.get(key)
            _need(row is not None and (token is None or row.get("token") == token), "prospect_claim_lost")
            row.update(updated_at=now, **fields)
            tx.put(key, row)
            return row
        return self.ops._run(op, allow_expired=True)

    def _elapsed(self):
        return self.monotonic() - self.started

    # ----- run
    def run(self):
        now = self.clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        self.started = self.started_at if self.started_at is not None else self.monotonic()
        local = now.astimezone(ISTANBUL)
        report = {"status": "prospect_watch_completed", "mails": 0, "replied": 0, "notes": 0, "logged": 0,
                  "held": 0, "skipped": 0, "filtered": {}, "model_calls": 0, "business_writes": 0, "requests": None,
                  "purge": None}
        if not self.cfg["enabled"]:
            return dict(report, status="no_new_work", reason="prospect_watch_disabled")
        c = self.ops.control()
        if c["mode"] != "review_open":
            return dict(report, status="stopped", reason="human_control_stopped")
        if not in_window(local, self.cfg["window"]):
            return dict(report, status="no_new_work", reason="outside_prospect_window")
        self.generation = c["generation"]
        token = uuid.uuid4().hex
        if not self._lease(token):
            return dict(report, status="slot_already_claimed", reason="prospect_run_in_progress")
        try:
            try:
                self.lists = self._lists(local.date())
            except Exception as exc:  # no reply ever goes out without the place its note is written
                return dict(report, status="held", reason=_code(exc))
            try:
                sources = list(self.reader() or [])
            except Exception as exc:
                sources, report["reason"] = [], "mail_read_failed:" + _code(exc)
            skipped = getattr(self.reader, "skipped", None)
            if isinstance(skipped, dict) and skipped:
                report["reader_skipped"] = dict(skipped)
            sources.sort(key=lambda s: s.get("received_at_ms") or 0)
            report["mails"] = len(sources)
            worked = 0
            for source in sources:
                if worked >= self.cfg["limits"]["max_mails_per_run"] or self._elapsed() > self.cfg["limits"]["run_seconds"]:
                    report["skipped"] += 1
                    continue
                try:
                    outcome = self._one(source, report)
                except Exception as exc:  # one mail never stops the others
                    outcome = "error:" + _code(exc)
                if outcome.startswith("filtered:"):
                    reason = outcome.split(":", 1)[1]
                    report["filtered"][reason] = report["filtered"].get(reason, 0) + 1
                    continue
                if outcome in ("not_prospect", "already_recorded"):
                    report["skipped"] += 1
                    continue
                worked += 1
                if outcome == "replied":
                    report["replied"] += 1
                elif outcome in ("noted", "followup"):
                    report["notes"] += 1
                elif outcome == "logged":
                    report["logged"] += 1
                elif outcome in ("released", "waiting_for_model_budget"):
                    report["skipped"] += 1
                else:
                    report["held"] += 1
            if self.cfg["research"]["enabled"]:
                try:
                    report["requests"] = self._requests(report, local)
                except Exception as exc:
                    report["requests"] = {"status": "held", "reason": _code(exc)}
            try:
                report["purge"] = self._purge(local)
            except Exception as exc:
                report["purge"] = {"status": "held", "reason": _code(exc)}
            return report
        finally:
            self._release(token)

    def _hold(self, seconds):
        """A deep report runs far longer than one mail run: keep this run's lease for it (no parallel run)."""
        token = getattr(self, "_token", None)

        def op(tx, now):
            row = tx.get(RUN_KEY) or {}
            if token and row.get("token") == token:
                tx.put(RUN_KEY, dict(row, until=max(row.get("until", 0), now + seconds)))
        try:
            self.ops._run(op, allow_expired=True)
        except Exception:
            pass

    def _lease(self, token):
        self._token = token

        def op(tx, now):
            self._fence(tx, now)
            row = tx.get(RUN_KEY) or {}
            if row.get("until", 0) > now and row.get("token") != token:
                return False
            tx.put(RUN_KEY, {"kind": "prospect_run", "token": token,
                             "until": now + self.cfg["limits"]["run_seconds"] + 240})
            return True
        return self.ops._run(op)

    def _release(self, token):
        def op(tx, now):
            row = tx.get(RUN_KEY) or {}
            if row.get("token") == token:
                tx.put(RUN_KEY, dict(row, until=now))
        try:
            self.ops._run(op, allow_expired=True)
        except Exception:
            pass

    def _lists(self, today, only=None):
        """The notes and research lists of the project, found by name, hidden from clients (checked daily).
        `only` (operator test runs): just those lists, not cached."""
        names = {"notes": self.cfg["basecamp"]["notes_list"], "research": self.cfg["basecamp"]["research_list"]}
        if only:
            names = {k: v for k, v in names.items() if k in only}
        st = {} if only else (self._get(LISTS_KEY) or {})
        if (st.get("checked_on") == today.isoformat() and st.get("project_id") == self.s["project_id"]
                and st.get("names") == names and set(st.get("ids") or {}) == set(names)):
            return st["ids"]
        a, p = self.s["account_id"], self.s["project_id"]
        project = self.http.one(f"{API}/{a}/projects/{p}.json")
        _need(project.get("id") == p, "prospect_project_mismatch")
        todoset = next((d for d in project.get("dock") or [] if isinstance(d, dict) and d.get("name") == "todoset"
                        and d.get("enabled") is not False), None)
        _need(todoset is not None and type(todoset.get("id")) is int, "prospect_todoset_missing")
        rows = self.http.all(f"{self.base}/todosets/{todoset['id']}/todolists.json", max_pages=3)
        ids = {}
        for role, name in names.items():
            found = [r for r in rows if isinstance(r, dict) and fold(r.get("name") or r.get("title")).strip()
                     == fold(name).strip()]
            if not found:  # owner 2026-10-16: Hamle opens its own missing list, only where clients cannot see it
                _need(project.get("clients_enabled") is False, "prospect_list_missing:" + role)
                found = [self._new_list(todoset["id"], name)]
            _need(len(found) == 1, "prospect_list_missing:" + role)
            row = found[0]
            _need(type(row.get("id")) is int and row.get("visible_to_clients") is False
                  and row.get("status") in (None, "active"), "prospect_list_unusable:" + role)
            ids[role] = row["id"]
        if only:
            return ids
        self.ops._run(lambda tx, now: tx.put(LISTS_KEY, {"kind": "prospect_lists", "project_id": p, "names": names,
                                                         "ids": ids, "checked_on": today.isoformat(),
                                                         "checked_at": now}), allow_expired=True)
        return ids

    def _new_list(self, todoset_id, name):
        row, _ = self.http.call("POST", f"{self.base}/todosets/{todoset_id}/todolists.json",
                                {"name": name, "description": "Hamle tarafından açıldı (müşteriye görünmez)."}, 201)
        lid = row.get("id") if isinstance(row, dict) else None
        _need(type(lid) is int and lid > 0, "prospect_list_receipt_invalid")
        back = self.http.one(f"{self.base}/todolists/{lid}.json")
        _need(back.get("id") == lid and fold(back.get("name") or back.get("title")).strip() == fold(name).strip()
              and (back.get("bucket") or {}).get("id") == self.s["project_id"], "prospect_list_readback_mismatch")
        return back

    # ----- Basecamp writes (one POST, exact readback; the caller records the outcome)
    def _todo(self, list_id, title, text, assignees, due=None):
        _need(list_id in self.lists.values(), "prospect_list_not_allowed")
        body = {"content": title[:200], "description": _html(text), "assignee_ids": list(assignees),
                "completion_subscriber_ids": [], "notify": True}
        if due is not None:
            body["due_on"] = due.isoformat()
        row, _ = self.http.call("POST", f"{self.base}/todolists/{list_id}/todos.json", body, 201)
        tid = row.get("id") if isinstance(row, dict) else None
        _need(type(tid) is int and tid > 0, "prospect_todo_receipt_invalid")
        back = self.http.one(f"{self.base}/todos/{tid}.json")
        parent = back.get("parent") or {}
        _need(back.get("id") == tid and back.get("content") == title[:200] and parent.get("id") == list_id
              and parent.get("type") == "Todolist" and (back.get("bucket") or {}).get("id") == self.s["project_id"]
              and (back.get("creator") or {}).get("id") == self.s["hamle_person"]
              and back.get("visible_to_clients") is False
              and sorted(x.get("id") for x in back.get("assignees") or [] if isinstance(x, dict)) == sorted(assignees)
              and (due is None or back.get("due_on") == due.isoformat())
              and _visible_matches(back.get("description"), text), "prospect_todo_readback_mismatch")
        return tid

    def _comment(self, todo_id, text, html=None):
        """Plain text as <br> lines, or prepared rich HTML (the report); readback compares the visible words."""
        if html is not None:
            text = _plain(html)
        row, _ = self.http.call("POST", f"{self.base}/recordings/{todo_id}/comments.json",
                                {"content": html if html is not None else _html(text)}, 201)
        cid = row.get("id") if isinstance(row, dict) else None
        _need(type(cid) is int and cid > 0, "prospect_comment_receipt_invalid")
        got = self.http.one(f"{self.base}/comments/{cid}.json")
        _need(got.get("id") == cid and (got.get("parent") or {}).get("id") == todo_id
              and (got.get("bucket") or {}).get("id") == self.s["project_id"]
              and (got.get("creator") or {}).get("id") == self.s["hamle_person"]
              and got.get("visible_to_clients") is False
              and (_report_landed if html is not None else _visible_matches)(got.get("content"), text),
              "prospect_comment_readback_mismatch")
        return cid

    def _comments(self, todo_id):
        return [c for c in self.http.all(f"{self.base}/recordings/{todo_id}/comments.json", max_pages=3)
                if isinstance(c, dict) and type(c.get("id")) is int]

    def _app(self, todo_id):
        return f"{APP}/{self.s['account_id']}/buckets/{self.s['project_id']}/todos/{todo_id}"

    # ----- one mail
    def _one(self, source, report):
        if source.get("filtered"):
            return "filtered:" + str(source["filtered"])
        sender = str(source.get("sender") or "").strip().lower()
        if excluded(sender, self.s, self.book):
            return "not_prospect"
        reason = prefilter(source.get("headers"), sender, self.s)
        if reason:
            return "filtered:" + reason
        if not isinstance(source.get("received_at_ms"), int) or source["received_at_ms"] < self.s["since_ms"]:
            return "filtered:before_since"
        local = self.clock().astimezone(ISTANBUL)
        mkey, akey, dkey = mail_key(source["message_id"]), addr_key(sender), day_key(local.date())
        domain = sender.rsplit("@", 1)[1]
        token = uuid.uuid4().hex

        def claim(tx, now):
            self._fence(tx, now)
            row = tx.get(mkey)
            month = _month(now)
            idx = tx.get(index_key(month))
            if row is not None and not (row.get("state") == "claimed" and now >= row.get("lease_until", 0)):
                return None
            new = dict(row or {}, key=mkey, kind="prospect_mail", state="claimed", token=token, claimed_at=now,
                       lease_until=now + LEASE_SECONDS, message_id=source["message_id"],
                       thread_id=source["thread_id"], sender=sender, sender_name=_line(source.get("sender_name"), 120),
                       domain=domain, subject=_line(source.get("subject"), 200),
                       received_at_ms=source.get("received_at_ms"), day=local.date().isoformat())
            new.setdefault("created_at", now)
            idx = idx or {"kind": "prospect_index", "month": month, "keys": [], "purged": False}
            if mkey not in idx["keys"]:
                idx["keys"].append(mkey)
            tx.put(index_key(month), idx)
            tx.put(mkey, new)
            return new
        if self.ops._run(claim) is None:
            return "already_recorded"
        prior = self._get(akey)
        if prior is not None:  # one automatic reply per address, ever: a later mail is for the team
            return self._followup(mkey, token, source, prior, report)
        if source.get("thread_agency") or HAMLE not in [a.lower() for a in source.get("to") or []]:
            return self._note_only(mkey, token, source, sender, report,
                                   "Bu mail ajans ekibinin de yazdığı bir zincirde ya da Hamle yalnız bilgide (CC); "
                                   "Hamle kendiliğinden cevap vermedi.")
        if not self.models.budget_ok("classify"):
            self._update(mkey, token, lease_until=0, reason="waiting_for_model_budget")
            return "waiting_for_model_budget"
        kind, brand_key = domain_kind(domain, self.s)
        try:
            report["model_calls"] += 1
            cls = self.models.classify({"subject": _line(source.get("subject"), 200), "new_text": _mail_text(source),
                                        "domain": domain, "domain_kind": kind,
                                        "has_attachments": bool(source.get("has_attachments"))})
            model_status = "ok"
        except Exception as exc:
            cls, model_status = None, "failed:" + _code(exc)
        category = cls["category"] if cls else "unsure"
        language = cls["language"] if cls else "tr"
        if kind == "client" and category not in ("spam", "vendor_or_job"):
            category = "known_client_new_person"  # the domain decides, never the sender's own words
        elif kind != "client" and category == "known_client_new_person":
            category = "demo_or_first_contact"  # a claimed client brand from another domain is not verified
        research_target = (cls or {}).get("research_target") or "none"
        include_research = (category == "research_request" and research_target == "organisation"
                            and kind != "individual")
        facts = {"category": category, "language": language, "kind": kind, "brand_key": brand_key,
                 "request_summary": (cls or {}).get("request_summary"),
                 "claimed_company": (cls or {}).get("claimed_company"), "research_target": research_target,
                 "classify_status": model_status}
        if category in ("spam", "vendor_or_job"):
            self._update(mkey, token, state="logged", lease_until=0, **facts,
                         digest_line=(_line(f"{CATEGORY_TR[category]}: {sender} · {facts['request_summary'] or ''}", 240)
                                      if category == "vendor_or_job" else None))
            return "logged"
        if category == "unsure":
            return self._note_only(mkey, token, source, sender, report, "Hamle bu mailin ne olduğundan emin olamadı "
                                   "(" + model_status + "); cevap gönderilmedi, bir insan baksın.", facts=facts)
        sent_today = int((self._get(dkey) or {}).get("auto_replies") or 0)
        if sent_today >= self.cfg["limits"]["max_auto_replies_per_day"]:
            return self._note_only(mkey, token, source, sender, report, "Bugünkü otomatik cevap sınırı ("
                                   f"{self.cfg['limits']['max_auto_replies_per_day']}) doldu; cevap gönderilmedi, "
                                   "bir insan baksın.", facts=facts)
        known = match_person(self.people, sender, source.get("sender_name"), domain)
        first = first_name(source.get("sender_name"), known)
        research, company, person = None, None, None
        if kind == "corporate":  # individuals: no company; client brands: the brand lead is asked instead
            if self.models.budget_ok("research"):
                try:
                    report["model_calls"] += 1
                    research = self.models.research({"domain": domain, "website": "https://" + domain,
                                                     "sender_name": None if known else (first and _line(
                                                         source.get("sender_name"), 120)),
                                                     "search_person": bool(first) and known is None})
                    facts["research_status"] = "ok"
                except Exception as exc:
                    facts["research_status"] = "failed:" + _code(exc)
            else:
                facts["research_status"] = "model_budget_unavailable"
            company = company_facts(research, domain)
            person = person_facts(research)
        brand = self.s["brands"].get(brand_key) if brand_key else None
        if not self.models.budget_ok("reply"):
            self._update(mkey, token, lease_until=0, reason="waiting_for_model_budget")
            return "waiting_for_model_budget"
        request = reply_input(today=local.date(), language=language, category=category, kind=kind, first=first,
                              company=company, brand_name=(brand or {}).get("name"), known=known,
                              include_research=include_research, source=source)
        try:
            report["model_calls"] += 1
            text = str(self.models.reply(request)["reply"]).replace("\r\n", "\n").strip()
            problems = check_reply(text, settings=self.s, language=language, first=first, category=category,
                                   kind=kind, brand_key=brand_key, include_research=include_research)
        except Exception as exc:
            text, problems = None, ["cevap alınamadı (" + _code(exc) + ")"]
        facts.update(company=company, person=person, known_person=(known or {}).get("name"),
                     known_where=(known or {}).get("where_met"), known_note=(known or {}).get("note"),
                     reply_text=text, reply_problems=problems, first_name=first)
        outcome = "not_sent"
        if not problems:
            outcome = self._send(mkey, akey, dkey, token, source, sender, text, report)
        facts["reply_state"] = outcome
        if outcome in ("sent", "send_unknown"):
            facts["copy_state"] = self._copy(source, sender, text, outcome, report)
        if kind == "corporate" and self.cfg["karne_checks"] and self.web is not None and self._elapsed() < \
                self.cfg["limits"]["run_seconds"]:
            facts["karne"] = self._karne(company, domain)
        request_row = None
        if include_research and outcome in ("sent", "send_unknown"):
            request_row = self._open_request(source, sender, domain, facts, report)
        elif category == "research_request" and research_target == "person":
            facts["person_research_refused"] = True
        return self._note(mkey, token, source, sender, facts, report, request_row)

    # ----- send (same thread, to the sender, the owner always in CC) + owner copy
    def _send(self, mkey, akey, dkey, token, source, sender, text, report):
        from v11_chain_mail import reply_subject
        payload = {"from": HAMLE, "to": [sender], "cc": [self.s["roles"]["owner"]["email"]],
                   "thread_id": source["thread_id"],
                   "in_reply_to_message_id": source["message_id"], "subject": reply_subject(source.get("subject")),
                   "body": text}
        sha = scope_digest(payload)
        try:  # read-only: the mail is still the newest incoming of its thread, recipients and subject unchanged
            self.reply_writer.preflight("client_reply", copy.deepcopy(payload))
        except Exception as exc:
            return "blocked:" + _code(exc)
        local = self.clock().astimezone(ISTANBUL)

        def begin(tx, now):
            self._fence(tx, now)
            row, addr, day = tx.get(mkey), tx.get(akey), tx.get(dkey) or {}
            month = _month(now)
            idx = tx.get(index_key(month))
            _need(row is not None and row.get("token") == token and row.get("state") == "claimed", "prospect_claim_lost")
            if addr is not None:
                raise ProspectError("blocked:address_already_answered")
            if int(day.get("auto_replies") or 0) >= self.cfg["limits"]["max_auto_replies_per_day"]:
                raise ProspectError("blocked:daily_auto_reply_limit")
            row.update(reply_state="sending", reply_payload_sha256=sha, sending_at=now, reply_to=[sender],
                       reply_subject=payload["subject"], reply_body=text)
            idx = idx or {"kind": "prospect_index", "month": month, "keys": [], "purged": False}
            if akey not in idx["keys"]:
                idx["keys"].append(akey)
            tx.put(index_key(month), idx)
            tx.put(mkey, row)
            tx.put(akey, {"key": akey, "kind": "prospect_address", "replied": True, "address": sender,
                          "first_message_key": mkey, "todo_id": None, "created_at": now})
            tx.put(dkey, dict(day, kind="prospect_day", day=local.date().isoformat(),
                              auto_replies=int(day.get("auto_replies") or 0) + 1))
        try:
            self.ops._run(begin)
        except ProspectError as exc:
            return exc.code if exc.code.startswith("blocked:") else "released:" + exc.code
        except Exception as exc:
            return "released:" + _code(exc)
        try:  # exactly one POST; anything uncertain is never retried
            report["business_writes"] += 1
            remote = self.reply_writer.write(copy.deepcopy(payload))["remote_id"]
            back = self.reply_writer.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == sha, "prospect_reply_readback_mismatch")
        except Exception as exc:
            self._update(mkey, token, reply_state="send_unknown", reply_reason=_code(exc))
            return "send_unknown"
        self._update(mkey, token, reply_state="sent", reply_remote_id=remote, sent_at=self.clock().timestamp())
        return "sent"

    def _copy(self, source, sender, text, outcome, report):
        owner = self.s["roles"]["owner"]["email"]
        state = "gönderildi" if outcome == "sent" else "gönderildi ama teyit edilemedi (tekrar denenmedi)"
        body = "\n".join([f"Hamle, {self.cfg['mailbox']} kutusuna yazan yeni bir kişiye otomatik cevap verdi ({state}).",
                          "", f"Kime: {sender}", f"Konu: {_line(source.get('subject'), 200)}", "",
                          "Gönderilen cevap:", text, "", "Gelen mail:", _quote(_mail_text(source))])
        payload = {"to": [owner], "subject": _line(f"Hamle AI: otomatik cevap kopyası · {sender}", 200), "body": body}
        try:
            report["business_writes"] += 1
            remote = self.copy_writer.write(copy.deepcopy(payload))["remote_id"]
            back = self.copy_writer.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == scope_digest(payload), "prospect_copy_readback_mismatch")
            return "sent"
        except Exception as exc:
            return "unknown:" + _code(exc)

    def _karne(self, company, domain):
        """Free digital-karne checks on the company's own site (no model); text for the internal note."""
        from v11_digital_karne import run_prospect_karne
        name = (company or {}).get("name") or domain
        try:
            r = run_prospect_karne(name, "https://" + domain, web=self.web, research=None, clock=self.clock,
                                   psi_key=self.psi_key)
        except Exception as exc:
            return {"status": "held", "reason": _code(exc)}
        if r.get("status") != "prospect_karne_ready":
            return {"status": "held", "reason": r.get("reason")}
        return {"status": "ok", "counts": r.get("counts"), "text": _line(r.get("text"), 1500)}

    # ----- internal notes
    def _assignees(self, facts):
        roles = self.s["roles"]
        lead = roles["new_business_lead"]["basecamp_id"]
        if facts and facts.get("kind") == "client" and facts.get("brand_key") in self.s["brands"]:
            lead = self.s["brands"][facts["brand_key"]]["lead_id"]
        return sorted({lead, roles["owner"]["basecamp_id"]})

    def _company_label(self, facts, domain):
        if facts and facts.get("kind") == "individual":
            return f"bireysel ({domain})"
        if facts and facts.get("kind") == "client":
            return f"{self.s['brands'][facts['brand_key']]['name']} ({domain})"
        name = ((facts or {}).get("company") or {}).get("name")
        return f"{name} ({domain})" if name else domain

    def _note_text(self, source, sender, facts, header, request_row=None):
        domain = sender.rsplit("@", 1)[1]
        facts = facts or {}
        who = _line(source.get("sender_name"), 120)
        lines = [header, ""]
        if facts.get("kind") == "client":
            lead = next((p for p in self.s["roles"].values()
                         if p["basecamp_id"] == self.s["brands"][facts["brand_key"]]["lead_id"]), None)
            lines += [f"Bu kişi kim, ekleyelim mi? ({self.s['brands'][facts['brand_key']]['name']} alan adından yazdı; "
                      "müşteri listemizde yok." + (f" {lead['name']}, bakar mısın?" if lead else "") + ")", ""]
        lines += ["Gönderen: " + (f"{who} <{sender}>" if who else sender),
                  "Şirket (yalnız alan adından): " + self._company_label(facts, domain)]
        if facts.get("category"):
            lines.append("Tür: " + CATEGORY_TR.get(facts["category"], facts["category"]))
        if facts.get("request_summary"):
            lines.append("Ne istiyor: " + _line(facts["request_summary"], 300))
        if facts.get("claimed_company") and facts.get("kind") != "corporate":
            lines.append("Kendini tanıttığı şirket: " + _line(facts["claimed_company"], 80) + " (alan adı bunu doğrulamıyor)")
        if facts.get("known_person"):
            lines.append("Tanıdığımız kişi: " + _line(facts["known_person"], 120)
                         + (f" · nerede: {_line(facts['known_where'], 200)}" if facts.get("known_where") else "")
                         + (f" · not: {_line(facts['known_note'], 300)}" if facts.get("known_note") else ""))
        person = facts.get("person") or {}
        if person.get("title"):
            lines.append(f"Kişi (yalnız iç not): unvan {person['title']} · kaynak: {person.get('url')}")
        elif person.get("match") in ("unclear", "not_found"):
            lines.append("Kişi (yalnız iç not): ad, şirket ve unvan açıkça eşleşmedi; cevap yalnız şirket düzeyinde "
                         "kişiselleştirildi.")
        if person.get("dropped_social"):
            lines.append("Kişisel sosyal medya kaynağı atıldı (kullanılmaz).")
        company = facts.get("company") or {}
        if company.get("summary") or company.get("sector"):
            lines.append("Şirket hakkında: " + " · ".join(x for x in (company.get("sector"), company.get("summary")) if x)
                         + f" (kaynak: {company.get('url')})")
        elif facts.get("research_status") and facts.get("kind") != "individual":
            lines.append("Şirket araştırması: " + ("kaynaklı bilgi bulunamadı" if facts["research_status"] == "ok"
                                                   else facts["research_status"]))
        karne = facts.get("karne") or {}
        if karne.get("status") == "ok":
            lines += ["", "Sitede ön kontrol (Hamle'nin kendi ölçümü):", karne["text"]]
        if facts.get("reply_text"):
            title = {"sent": "Gönderilen cevap:", "send_unknown": "Gönderilen (teyit edilemeyen) cevap:"}.get(
                facts.get("reply_state"), "Hamle'nin cevap taslağı (GÖNDERİLMEDİ):")
            lines += ["", title, facts["reply_text"]]
        if facts.get("reply_problems"):
            lines.append("Gönderilmedi çünkü: " + "; ".join(facts["reply_problems"]))
        if facts.get("reply_state") in ("sent", "send_unknown"):
            copy_state = facts.get("copy_state") or "-"
            lines.append(f"Alıcı {sender}, CC'de {self.s['roles']['owner']['email']}. Ayrı kopyası da "
                         + ("gitti." if copy_state == "sent" else f"gönderilemedi ({copy_state})."))
        if request_row:
            if request_row.get("existing"):
                lines.append("Bu alan adı için açık bir araştırma isteği zaten var: " + self._app(request_row["todo_id"])
                             if request_row.get("todo_id") else "Bu alan adı için açık bir araştırma isteği zaten var.")
            elif request_row.get("todo_id"):
                lines.append("Araştırma isteği (onay bekliyor): " + self._app(request_row["todo_id"]))
            else:
                lines.append("Araştırma isteği açılamadı (" + str(request_row.get("reason")) + "); bir insan baksın.")
        if facts.get("person_research_refused"):
            lines.append("Bir KİŞİ hakkında araştırma istendi; Hamle kişileri araştırmaz, yalnız kurumları.")
        lines += ["", "Gelen mail (konu: " + (_line(source.get("subject"), 200) or "(konu yok)") + "):",
                  _quote(_mail_text(source))]
        if facts.get("classify_status") and facts["classify_status"] != "ok":
            lines.append("Sınıflandırma: " + facts["classify_status"])
        return "\n".join(lines)[:MAX_NOTE]

    def _write_note(self, mkey, token, source, sender, facts, header, report, request_row=None):
        title = _line(f"Yeni temas · {self._company_label(facts, sender.rsplit('@', 1)[1])} · "
                      + CATEGORY_TR.get((facts or {}).get("category"), "insan baksın"), 200)
        text = self._note_text(source, sender, facts, header, request_row)
        assignees = self._assignees(facts)

        def begin(tx, now):
            self._fence(tx, now)
            row = tx.get(mkey)
            _need(row is not None and row.get("token") == token, "prospect_claim_lost")
            row.update(state="writing_note", note_at=now, **{("domain_kind" if k == "kind" else k): v
                                                             for k, v in (facts or {}).items()})
            tx.put(mkey, row)
        try:
            self.ops._run(begin)
        except Exception as exc:
            if (self._get(mkey) or {}).get("reply_state") in ("sent", "send_unknown", "sending"):
                self._update(mkey, token, state="note_unknown", reason=_code(exc))  # a reply went out: never again
                return "unknown"
            self._update(mkey, token, lease_until=0, reason=_code(exc))
            return "released"
        try:
            report["business_writes"] += 1
            tid = self._todo(self.lists["notes"], title, text, assignees)
        except Exception as exc:
            self._update(mkey, token, state="note_unknown", reason=_code(exc))
            return "unknown"
        self._update(mkey, token, state="noted", todo_id=tid, lease_until=0)
        akey = addr_key(sender)
        if (self._get(akey) or {}).get("todo_id") is None and self._get(akey) is not None:
            self.ops._run(lambda tx, now: tx.put(akey, dict(tx.get(akey), todo_id=tid)), allow_expired=True)
        return "noted"

    def _note(self, mkey, token, source, sender, facts, report, request_row):
        state = facts.get("reply_state")
        if state == "sent":
            header = ("GÖNDERİLDİ · Hamle bu kişiye aynı zincirde kısa bir ilk cevap verdi "
                      f"({self.s['roles']['owner']['first_name']} CC'de).")
        elif state == "send_unknown":
            header = ("GÖNDERİM BELİRSİZ · Hamle cevabı gönderdi ama teyit edemedi ve tekrar denemedi. Gmail'de "
                      "Gönderilmiş kutusuna bakın.")
        elif str(state).startswith("blocked:"):
            header = "GÖNDERİLMEDİ · Gönderim öncesi kontrol geçmedi (" + state.split(":", 1)[1] + "). Bir insan baksın."
        elif str(state).startswith("released:"):
            header = "GÖNDERİLMEDİ · Hamle durduruldu ya da bütçe beklemede; bir insan baksın."
        else:
            header = "İNSAN BAKMALI · Hamle cevabı göndermedi; aşağıdaki taslak ve nedenler."
        out = self._write_note(mkey, token, source, sender, facts, header, report, request_row)
        if out == "noted" and state in ("sent", "send_unknown"):
            return "replied"
        return out

    def _note_only(self, mkey, token, source, sender, report, why, facts=None):
        kind, brand_key = domain_kind(sender.rsplit("@", 1)[1], self.s)
        facts = dict({"kind": kind, "brand_key": brand_key}, **(facts or {}))
        return self._write_note(mkey, token, source, sender, facts, "İNSAN BAKMALI · " + why, report)

    def _followup(self, mkey, token, source, prior, report):
        """A later mail from an address Hamle already answered: one comment on its note, no model, no reply."""
        tid = prior.get("todo_id")
        if type(tid) is not int:
            return self._note_only(mkey, token, source, source["sender"].lower(), report,
                                   "Bu adrese Hamle daha önce otomatik cevap verdi; ikinci kez vermez.")
        text = "\n".join(["Aynı kişi yeniden yazdı (Hamle ikinci kez otomatik cevap vermez; ekip cevaplasın).",
                          "Konu: " + (_line(source.get("subject"), 200) or "(konu yok)"), _quote(_mail_text(source))])

        def begin(tx, now):
            self._fence(tx, now)
            row = tx.get(mkey)
            _need(row is not None and row.get("token") == token, "prospect_claim_lost")
            row.update(state="writing_followup", todo_id=tid)
            tx.put(mkey, row)
        try:
            self.ops._run(begin)
        except Exception as exc:
            self._update(mkey, token, lease_until=0, reason=_code(exc))
            return "released"
        try:
            report["business_writes"] += 1
            cid = self._comment(tid, text)
        except Exception as exc:
            self._update(mkey, token, state="followup_unknown", reason=_code(exc))
            return "unknown"
        self._update(mkey, token, state="followup_noted", comment_id=cid, lease_until=0)
        return "followup"

    # ----- research requests (owner-gated)
    def _open_request(self, source, sender, domain, facts, report):
        rkey, dkey = request_key(source["message_id"]), domain_key(domain)
        owner = self.s["roles"]["owner"]
        local = self.clock().astimezone(ISTANBUL)

        def begin(tx, now):
            self._fence(tx, now)
            ptr, old, month_idx, reqs = tx.get(dkey), tx.get(rkey), tx.get(index_key(_month(now))), tx.get(REQUESTS_KEY)
            open_row = tx.get(ptr["open_request"]) if ptr and ptr.get("open_request") else None
            if old is not None:
                return dict(old, existing=True)
            if open_row is not None and open_row.get("state") in OPEN_REQUEST_STATES:
                return dict(open_row, existing=True)
            month = _month(now)
            idx = month_idx or {"kind": "prospect_index", "month": month, "keys": [], "purged": False}
            for k in (rkey, dkey):
                if k not in idx["keys"]:
                    idx["keys"].append(k)
            row = {"key": rkey, "kind": "prospect_request", "state": "creating", "domain": domain, "sender": sender,
                   "sender_name": _line(source.get("sender_name"), 120), "first_name": facts.get("first_name"),
                   "language": facts.get("language"), "thread_id": source["thread_id"],
                   "message_id": source["message_id"], "subject": _line(source.get("subject"), 200),
                   "request_summary": facts.get("request_summary"), "request_text": _mail_text(source)[:1500],
                   "company": facts.get("company"), "day": local.date().isoformat(), "created_at": now}
            tx.put(index_key(month), idx)
            tx.put(rkey, row)
            tx.put(dkey, {"key": dkey, "kind": "prospect_domain", "domain": domain, "open_request": rkey,
                          "updated_at": now})
            tx.put(REQUESTS_KEY, {"kind": "prospect_requests", "keys": list(dict.fromkeys(
                list((reqs or {}).get("keys") or []) + [rkey]))[-60:]})
            return row
        try:
            row = self.ops._run(begin)
        except Exception as exc:
            return {"reason": _code(exc)}
        if row.get("existing"):
            return {"existing": True, "todo_id": row.get("todo_id")}
        label = self._company_label(facts, domain)
        title, text = request_todo(owner["name"], self.cfg["research"]["estimate"], source, sender, label, facts)
        try:
            report["business_writes"] += 1
            tid = self._todo(self.lists["research"], title, text, [owner["basecamp_id"]], due=local.date())
        except Exception as exc:
            reason = _code(exc)
            self.ops._run(lambda tx, now: tx.put(rkey, dict(tx.get(rkey), state="unknown", reason=reason)),
                          allow_expired=True)
            return {"reason": reason}
        self.ops._run(lambda tx, now: tx.put(rkey, dict(tx.get(rkey), state="awaiting_approval", todo_id=tid,
                                                        todo_at=now)), allow_expired=True)
        return {"todo_id": tid}

    def _set_request(self, key, expect, **fields):
        def op(tx, now):
            row = tx.get(key)
            _need(row is not None and row.get("state") in expect, "prospect_request_changed")
            row.update(updated_at=now, **fields)
            tx.put(key, row)
            return row
        row = self.ops._run(op, allow_expired=True)
        if fields.get("state") in CLOSED_REQUEST_STATES:
            self._free_domain(row)
        return row

    def _free_domain(self, row):
        dkey = domain_key(row["domain"])

        def op(tx, now):
            ptr = tx.get(dkey)
            if ptr and ptr.get("open_request") == row["key"]:
                tx.put(dkey, dict(ptr, open_request=None, updated_at=now))
        self.ops._run(op, allow_expired=True)

    def _ack(self, key, todo_id, text, report):
        def begin(tx, now):
            self._fence(tx, now)
            row = tx.get(key)
            _need(row is not None and int(row.get("ack_count") or 0) < 8, "prospect_ack_limit")
            row["ack_count"] = int(row.get("ack_count") or 0) + 1
            tx.put(key, row)
        try:
            self.ops._run(begin)
        except Exception:
            return "ack_denied"
        try:
            report["business_writes"] += 1
            self._comment(todo_id, text)
        except Exception:
            return "ack_unknown"
        return "ack_written"

    def _requests(self, report, local, only_test=False):
        from v11_client_reply import command, comment_text
        from v11_stop_intake import stop_intent
        keys = list((self._get(REQUESTS_KEY) or {}).get("keys") or [])
        out = {"checked": 0, "approved": 0, "reports": 0, "sent": 0, "rejected": 0, "reminded": 0, "expired": 0,
               "stopped": 0, "waiting": 0}
        owner_id = self.s["roles"]["owner"]["basecamp_id"]
        today = local.date().isoformat()
        reports_left = self.cfg["research"]["max_reports_per_run"]
        for key in keys:
            row = self._get(key)
            if not isinstance(row, dict) or row.get("state") not in OPEN_REQUEST_STATES or type(row.get("todo_id")) is not int:
                continue
            if only_test and not row.get("test"):
                continue
            if self._elapsed() > self.cfg["limits"]["run_seconds"]:
                break
            out["checked"] += 1
            comments = sorted(self._comments(row["todo_id"]), key=lambda c: c["id"])
            seen = int(row.get("seen_comment") or 0)
            fresh = [c for c in comments if c["id"] > seen and (c.get("creator") or {}).get("id") != self.s["hamle_person"]]
            if fresh:
                self._set_request(key, OPEN_REQUEST_STATES, seen_comment=fresh[-1]["id"])
            stop = next((c for c in fresh if (c.get("creator") or {}).get("id") in self.s["agency_ids"]
                         and stop_intent(comment_text(c.get("content")))), None)
            if stop is not None:  # 'Hamle dur' from an agency member: nothing more for this request
                self._set_request(key, OPEN_REQUEST_STATES, state="stopped", stopped_by=(stop.get("creator") or {}).get("id"))
                self._ack(key, row["todo_id"], "Tamam, bu araştırma isteğinde duruyorum; hiçbir şey yapmayacağım.", report)
                out["stopped"] += 1
                continue
            owners = [c for c in fresh if (c.get("creator") or {}).get("id") == owner_id]
            state = row["state"]
            if state == "awaiting_approval":
                if any(is_approval(c.get("content")) for c in owners):
                    row = self._set_request(key, ("awaiting_approval",), state="approved",
                                            approved_at=self.clock().timestamp())
                    out["approved"] += 1
                    state = "approved"
                else:
                    created = float(row.get("todo_at") or row.get("created_at") or 0)
                    if self.clock().timestamp() - created > self.cfg["research"]["expire_days"] * 86400:
                        self._set_request(key, ("awaiting_approval",), state="expired")
                        out["expired"] += 1
                    elif any(near_approval(c.get("content")) for c in owners):
                        self._ack(key, row["todo_id"], "Bu yorumu onay olarak okuyamadım, araştırmaya başlamadım. "
                                  "Başlamam için yeni bir yorum olarak yalnız \"OK GO\" ya da \"Onaylıyorum\" yaz.",
                                  report)
                        out["unclear"] = out.get("unclear", 0) + 1
                    elif _hm(local) >= self.cfg["research"]["reminder_after"] and row.get("reminded_on") != today:
                        self._set_request(key, ("awaiting_approval",), reminded_on=today)
                        self._ack(key, row["todo_id"], "Hatırlatma: bu araştırma isteği senin onayını bekliyor. "
                                  "Başlamam için yalnız \"OK GO\" ya da \"Onaylıyorum\" yaz; istemiyorsan bu to-do'yu kapatabilirsin.",
                                  report)
                        out["reminded"] += 1
                    continue
            if state in ("approved", "researched"):  # two runs: collect + research, then the strategist writes
                if reports_left <= 0:
                    out["waiting"] += 1
                    continue
                reports_left -= 1
                self._hold(REPORT_HOLD_SECONDS)
                done = self._research(key, row, report) if state == "approved" else "researched"
                if done == "researched":  # the strategist writes right away: one run, ~15-25 minutes in all
                    out["researched"] = out.get("researched", 0) + (state == "approved")
                    row = self._get(key) or {}
                    done = self._write(key, row, report) if row.get("state") == "researched" else "changed"
                out["reports"] += done == "report_ready"
                continue
            if state == "report_ready":
                posted = int(row.get("report_comment_id") or 0)
                found = None
                for c in owners:
                    if c["id"] > posted:
                        verb = command(c.get("content"))
                        if verb is not None:
                            found = verb
                if found is None:
                    continue
                verb, corrected = found
                if verb == "reject":
                    self._set_request(key, ("report_ready",), state="rejected")
                    self._ack(key, row["todo_id"], "Tamam, raporu kuruma göndermiyorum.", report)
                    out["rejected"] += 1
                    continue
                body = corrected or (row.get("report_mail") if row.get("report_mail_ok") else None)
                if not body:
                    self._ack(key, row["todo_id"], "Gönderilebilecek hazır bir mail metnim yok. Göndermem için mailin "
                              "tam metnini \"Hamle gönder\" satırının altına yaz.", report)
                    continue
                result = self._send_report(key, row, body, report)
                out["sent"] += result == "sent"
        return out

    def _claim(self, key, state, field):
        token = uuid.uuid4().hex

        def claim(tx, now):
            self._fence(tx, now)
            r = tx.get(key)
            if r is None or r.get("state") != state or r.get(field, 0) > now:
                return None
            r.update({field: now + REPORT_HOLD_SECONDS})
            tx.put(key, r)
            return r
        return self.ops._run(claim)

    def _research(self, key, row, report):
        """Approved by the owner (run 1 of 2): Hamle's own measurements (site, sitemap, YouTube and Instagram official
        APIs, the karne's site checks), then the web research PART BY PART (REPORT_PARTS): each part is kept on the
        request as soon as it is done, so a stopped run goes on where it left off and one failed part is only a gap."""
        from v11_footprint import collect
        if not self.models.budget_ok("report"):
            return "waiting_for_model_budget"
        if self._claim(key, "approved", "research_lease") is None:
            return "busy"
        today = self.clock().astimezone(ISTANBUL).date()
        try:
            measured = json.loads(row["measured_json"]) if row.get("measured_json") else None
        except ValueError:
            measured = None
        if measured is None:
            measured = collect(row["domain"], self.web, today, youtube_key=self.footprint.get("youtube"),
                               instagram=self.footprint.get("instagram")) if self.web is not None else {"notes": ["web_off"]}
            karne = self._karne(row.get("company"), row["domain"]) if self.web is not None else {"status": "skipped"}
            measured["site_checks"] = karne.get("text") if karne.get("status") == "ok" else None
            measured = _compact(measured)
            self._set_request(key, ("approved",), measured_json=json.dumps(measured, ensure_ascii=False)[:200_000])
        try:
            parts = json.loads(row.get("research_parts") or "{}")
        except ValueError:
            parts = {}
        base_request = {"domain": row["domain"], "website": "https://" + row["domain"],
                        "company": {k: (row.get("company") or {}).get(k) for k in ("name", "sector")},
                        "request_text": _line(row.get("request_text"), 1500), "language": row.get("language") or "tr",
                        "today": today.isoformat(), "measured": measured}
        for name, keys in REPORT_PARTS:
            if name in parts:
                continue
            if self._elapsed() > REPORT_HOLD_SECONDS - 900:  # leave room for the strategist; the next run goes on
                self._set_request(key, ("approved",), research_lease=0)
                return "busy"
            try:
                report["model_calls"] += 1
                parts[name] = self.models.report(dict(base_request, part=name, collect_only=list(keys)))
            except Exception as exc:
                parts[name] = {"failed": "failed:" + _code(exc)}
                report.setdefault("report_failures", []).append(f"{name}:" + parts[name]["failed"])
            self._set_request(key, ("approved",), research_parts=json.dumps(parts, ensure_ascii=False)[:300_000])
        failed = [n for n, v in parts.items() if "failed" in v]
        if len(failed) == len(REPORT_PARTS):
            return self._fail(key, row, ("approved",), parts[REPORT_PARTS[0][0]]["failed"], report)
        research = {"evidence": sorted({u for v in parts.values() for u in (v.get("evidence") or [])}),
                    "missing_parts": failed}
        for name, keys in REPORT_PARTS:
            for k in keys:
                research[k] = (parts.get(name) or {}).get(k)
        data = json.dumps({"measured": measured, "research": research}, ensure_ascii=False)
        if len(data) > 400_000:
            return self._fail(key, row, ("approved",), "research_too_large", report)
        try:
            self._set_request(key, ("approved",), state="researched", research_json=data,
                              research_status="ok" if not failed else "partial:" + ",".join(failed),
                              research_parts=None, measured_json=None)
        except Exception:
            return "changed"
        return "researched"

    def _fail(self, key, row, expect, status, report):
        report.setdefault("report_failures", []).append(status)  # the job log names the reason (owner's Mac)
        try:
            self._set_request(key, expect, state="writing_report", report_status=status)
        except Exception:
            return "changed"
        try:
            report["business_writes"] += 1
            cid = self._comment(row["todo_id"], "Araştırmayı bu kez tamamlayamadım (" + status + "). Tekrar "
                                "denemiyorum; istersen yeni bir istek açılabilir.")
        except Exception as exc:
            self._set_request(key, ("writing_report",), state="unknown", reason=_code(exc))
            return "unknown"
        self._set_request(key, ("writing_report",), state="failed", report_comment_id=cid)
        return "failed"

    def _write(self, key, row, report):
        """Run 2 of 2: the strategist writes the Dijital Ayak İzi Raporu from the kept data; the rich report goes on
        the to-do as one comment and, designed, to the owner's mailbox (team only; the prospect gets nothing)."""
        if not self.models.budget_ok("writer"):
            return "waiting_for_model_budget"
        if self._claim(key, "researched", "write_lease") is None:
            return "busy"
        try:
            data = json.loads(row.get("research_json") or "{}")
        except ValueError:
            data = {}
        label = self._company_label({"kind": "corporate", "company": row.get("company")}, row["domain"])
        sources, research = number_sources(data.get("research") or {}, data.get("measured") or {}, row["domain"])
        request = {"company": label, "domain": row["domain"], "language": row.get("language") or "tr",
                   "first_name": row.get("first_name"), "today": self.clock().astimezone(ISTANBUL).date().isoformat(),
                   "request_text": _line(row.get("request_text"), 1500), "measured": _compact(data.get("measured") or {}),
                   "research": research, "missing_parts": (data.get("research") or {}).get("missing_parts") or [],
                   "sources": [{"n": i, "url": u} for i, u in enumerate(sources, 1)]}
        written = {}
        for step, _ in WRITER_STEPS:  # the plan is written on top of the analysis
            try:
                report["model_calls"] += 1
                written.update(self.models.writer(dict(request, step=step, analysis=dict(written) or None,
                                                       _attempt=int(row.get("write_retries") or 0))))
            except Exception as exc:
                return self._fail(key, row, ("researched",), f"failed:{step}:" + _code(exc), report)
        # Owner 2026-10-16: the company mail is fixed approved text with the PDF attached (no model wording).
        written["mail"] = report_mail_text(self.cfg, language=row.get("language") or "tr", first=row.get("first_name"),
                                           company=label, score=(written.get("score") or {}).get("total") or 0)
        mail = written["mail"]
        problems = check_report_mail(mail, settings=self.s, language=row.get("language") or "tr",
                                     first=row.get("first_name"))
        html = render_report(label, written, sources, data.get("measured") or {}, problems=problems,
                             test=bool(row.get("test")), today=request["today"])
        try:
            self._set_request(key, ("researched",), state="writing_report", report_status="ok")
        except Exception:
            return "changed"
        try:
            report["business_writes"] += 1
            cid = self._comment(row["todo_id"], None, html=html)
        except Exception as exc:  # the full report did not land (or could not be proven): a short note instead;
            report.setdefault("report_failures", []).append("comment:" + _code(exc))  # the mail + PDF carry it all
            try:
                report["business_writes"] += 1
                cid = self._comment(row["todo_id"], None, html=short_report(label, written, sources))
            except Exception as exc2:
                report["report_failures"].append("short_comment:" + _code(exc2))
                self._set_request(key, ("writing_report",), state="unknown", reason=_code(exc2))
                return "unknown"
        view = json.dumps({"label": label, "written": written, "sources": sources, "today": request["today"],
                           "measured": slim_measured(data.get("measured") or {})}, ensure_ascii=False)
        self._set_request(key, ("writing_report",), state="report_ready", report_comment_id=cid, report_mail=mail,
                          report_mail_ok=bool(mail) and not problems, report_mail_problems=problems,
                          research_json=None, report_view_json=view if len(view) <= 400_000 else None)
        report["owner_mail"] = self._mail_owner(label, written, sources, data.get("measured") or {}, request["today"],
                                                row, report)
        return "report_ready"

    def _mail_owner(self, label, written, sources, measured, today, row, report):
        """The designed HTML report to the owner (team only). A failure here never touches the request."""
        owner = self.s["roles"]["owner"]["email"]
        try:
            att = report_attachment(label, written, sources, measured, today)
            report["owner_mail_pdf"] = "ok"
        except Exception as exc:  # the HTML mail still goes; the PDF is retried when the report is sent
            att, report["owner_mail_pdf"] = None, "failed:" + _code(exc)
        html = render_report_mail(label, written, sources, measured, today=today, link=self._app(row["todo_id"]),
                                  attached=att is not None)
        body = _plain(render_report(label, written, sources, measured, problems=[], test=bool(row.get("test")),
                                    today=today))
        payload = {"to": [owner], "subject": _line(f"Hamle AI · Dijital Ayak İzi Raporu · {label}", 200),
                   "body": body, "html": html}
        if att:
            payload["attachment"] = att
        try:
            report["business_writes"] += 1
            remote = self.copy_writer.write(copy.deepcopy(payload))["remote_id"]
            back = self.copy_writer.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == scope_digest(payload), "prospect_owner_report_readback_mismatch")
            return "sent"
        except Exception as exc:
            return "unknown:" + _code(exc)

    def _send_report(self, key, row, body, report):
        """The owner's 'Hamle gönder': the report to the prospect in the same thread, the owner in CC.
        A test request (operator preview) never reaches the prospect: the report goes only to the owner."""
        from v11_chain_mail import reply_subject
        if row.get("test"):
            return self._send_test_report(key, row, body, report)
        try:  # owner 2026-10-16: the company gets the short mail WITH the PDF, never the mail without it
            att = self._view_attachment(row)
        except Exception as exc:
            self._ack(key, row["todo_id"], f"Göndermedim: rapor PDF'ini üretemedim ({_code(exc)}).", report)
            return "blocked"
        try:
            anchor = self.reply_writer.latest_anchor(row["thread_id"])
        except Exception as exc:
            anchor, why = None, _code(exc)
        else:
            why = None
        if not anchor or str(anchor.get("sender") or "").lower() != row["sender"]:
            self._ack(key, row["todo_id"], "Göndermedim: zincirdeki son mail bu kişiden değil ya da zincir okunamadı"
                      + (f" ({why})" if why else "") + ". Ekip kendisi göndersin.", report)
            return "blocked"
        payload = {"from": HAMLE, "to": [row["sender"]], "cc": [self.s["roles"]["owner"]["email"]],
                   "thread_id": row["thread_id"],
                   "in_reply_to_message_id": anchor["message_id"], "subject": reply_subject(anchor.get("subject")),
                   "body": body, "attachment": att}
        sha = scope_digest(payload)
        try:
            self.reply_writer.preflight("client_reply", copy.deepcopy(payload))
        except Exception as exc:
            self._ack(key, row["todo_id"], f"Göndermedim, son kontrol geçmedi ({_code(exc)}).", report)
            return "blocked"

        def begin(tx, now):
            self._fence(tx, now)
            r = tx.get(key)
            _need(r is not None and r.get("state") == "report_ready", "prospect_request_changed")
            r.update(state="sending", report_payload_sha256=sha, sending_at=now)
            tx.put(key, r)
        try:
            self.ops._run(begin)
        except Exception:
            return "released"  # 'dur' or a change: nothing sent, read again next run
        try:
            report["business_writes"] += 1
            remote = self.reply_writer.write(copy.deepcopy(payload))["remote_id"]
            back = self.reply_writer.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == sha, "prospect_report_readback_mismatch")
        except Exception as exc:
            self._set_request(key, ("sending",), state="send_unknown", reason=_code(exc))
            self._ack(key, row["todo_id"], "Gönderdim ama teyit edemedim; tekrar denemiyorum. Gmail'de Gönderilmiş "
                      "kutusuna bakın.", report)
            return "send_unknown"
        self._set_request(key, ("sending",), state="sent", sent_remote_id=remote)
        self._ack(key, row["todo_id"], "Rapor kuruma aynı mail zincirinde gönderildi.", report)
        return "sent"

    @staticmethod
    def _view_attachment(row):
        view = json.loads(row.get("report_view_json") or "null")
        _need(isinstance(view, dict), "report_view_missing")
        return report_attachment(view["label"], view["written"], view["sources"], view["measured"], view["today"])

    def _send_test_report(self, key, row, body, report):
        """Test request: 'Hamle gönder' mails the report ONLY to the owner, as a new mail (never the prospect)."""
        owner = self.s["roles"]["owner"]["email"]
        label = self._company_label({"kind": "corporate", "company": row.get("company")}, row["domain"])
        text = "\n".join([f"Bu bir denemedir. Gerçek akışta bu mail {row['sender']} adresine, {owner} CC'de olarak "
                          "aynı mail zincirinde giderdi. Kuruma hiçbir şey gönderilmedi.", "", "-----", "", body])
        payload = {"to": [owner], "subject": _line(f"TEST · Hamle AI araştırma raporu · {label}", 200), "body": text}
        try:
            payload["attachment"] = self._view_attachment(row)
        except Exception as exc:
            self._ack(key, row["todo_id"], f"Göndermedim: rapor PDF'ini üretemedim ({_code(exc)}).", report)
            return "blocked"

        def begin(tx, now):
            self._fence(tx, now)
            r = tx.get(key)
            _need(r is not None and r.get("state") == "report_ready" and r.get("test") is True,
                  "prospect_request_changed")
            r.update(state="sending", sending_at=now)
            tx.put(key, r)
        try:
            self.ops._run(begin)
        except Exception:
            return "released"
        try:
            report["business_writes"] += 1
            remote = self.copy_writer.write(copy.deepcopy(payload))["remote_id"]
            back = self.copy_writer.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == scope_digest(payload), "prospect_test_report_readback_mismatch")
        except Exception as exc:
            self._set_request(key, ("sending",), state="send_unknown", reason=_code(exc))
            self._ack(key, row["todo_id"], "Sana göndermeyi denedim ama teyit edemedim; tekrar denemiyorum.", report)
            return "send_unknown"
        self._set_request(key, ("sending",), state="sent", sent_remote_id=remote)
        self._ack(key, row["todo_id"], f"Bu bir deneme olduğu için rapor kuruma değil yalnız sana ({owner}) "
                  "gönderildi.", report)
        return "sent"

    def _operator_start(self):
        """Common start of the operator runs (test request, requests-only): control open, lease, lists."""
        now = self.clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        self.started = self.monotonic()
        c = self.ops.control()
        if c["mode"] != "review_open":
            raise ProspectError("human_control_stopped")
        self.generation = c["generation"]
        token = uuid.uuid4().hex
        if not self._lease(token):
            raise ProspectError("prospect_run_in_progress")
        try:
            self.lists = self._lists(now.astimezone(ISTANBUL).date(), only=("research",))
        except Exception:
            self._release(token)
            raise
        return token, now.astimezone(ISTANBUL)

    def open_test_request(self, source, facts):
        """Operator test (owner 2026-10-16): one imagined research request becomes a REAL request record and a real
        to-do in the research list (assigned to the owner, due today, marked TEST). No mail to the sender, no
        address/domain record: 'Hamle gönder' on it mails the report only to the owner."""
        sender = str(source.get("sender") or "").strip().lower()
        _need(EMAIL.fullmatch(sender) is not None, "preview_sender_invalid")
        domain = sender.rsplit("@", 1)[1]
        owner = self.s["roles"]["owner"]
        test_id = "test-" + uuid.uuid4().hex
        rkey = request_key(test_id)
        token, local = self._operator_start()
        try:
            def begin(tx, now):
                self._fence(tx, now)
                month = _month(now)
                idx = tx.get(index_key(month)) or {"kind": "prospect_index", "month": month, "keys": [],
                                                   "purged": False}
                if rkey not in idx["keys"]:
                    idx["keys"].append(rkey)
                reqs = tx.get(REQUESTS_KEY)
                row = {"key": rkey, "kind": "prospect_request", "test": True, "state": "creating", "domain": domain,
                       "sender": sender, "sender_name": _line(source.get("sender_name"), 120),
                       "first_name": facts.get("first_name"), "language": facts.get("language"), "thread_id": None,
                       "message_id": test_id, "subject": _line(source.get("subject"), 200),
                       "request_summary": facts.get("request_summary"), "request_text": _mail_text(source)[:1500],
                       "company": facts.get("company"), "day": local.date().isoformat(), "created_at": now}
                tx.put(index_key(month), idx)
                tx.put(rkey, row)
                tx.put(REQUESTS_KEY, {"kind": "prospect_requests", "keys": list(dict.fromkeys(
                    list((reqs or {}).get("keys") or []) + [rkey]))[-60:]})
            self.ops._run(begin)
            label = self._company_label({"kind": "corporate", "company": facts.get("company")}, domain)
            title, text = request_todo(owner["name"], self.cfg["research"]["estimate"], source, sender, label, facts)
            title = _line("TEST · " + title, 200)
            text = ("TEST: Bu bir denemedir; bu mail gerçekte gelmedi ve kuruma hiçbir şey gönderilmedi. Rapor hazır "
                    f"olunca \"Hamle gönder\" yazarsan rapor kuruma değil yalnız sana ({owner['email']}) gelir.\n\n"
                    + text)
            try:
                tid = self._todo(self.lists["research"], title, text, [owner["basecamp_id"]], due=local.date())
            except Exception as exc:
                reason = _code(exc)
                self.ops._run(lambda tx, now: tx.put(rkey, dict(tx.get(rkey), state="unknown", reason=reason)),
                              allow_expired=True)
                return {"status": "held", "reason": reason, "business_writes": 1}
            self.ops._run(lambda tx, now: tx.put(rkey, dict(tx.get(rkey), state="awaiting_approval", todo_id=tid,
                                                            todo_at=now)), allow_expired=True)
            return {"status": "prospect_test_request_opened", "todo_url": self._app(tid), "title": title,
                    "business_writes": 1}
        finally:
            self._release(token)

    def run_test_requests(self):
        """Operator run (schedule stays off): only TEST requests: 'Onaylıyorum' -> report comment,
        'Hamle gönder' -> report to the owner only. Real requests are left to the scheduled run."""
        report = {"status": "prospect_test_requests_completed", "model_calls": 0, "business_writes": 0}
        try:
            token, local = self._operator_start()
        except ProspectError as exc:
            return dict(report, status="held", reason=exc.code)
        try:
            report["write_reopened"] = self._reopen_failed_writes()
            report["requests"] = self._requests(report, local, only_test=True)
            return report
        finally:
            self._release(token)

    def _reopen_failed_writes(self):
        """TEST requests whose research finished but whose writing failed go back to 'researched', so this run writes
        the report from the kept research (at most WRITE_RETRIES times; the research is never paid twice). A report
        that was written but could not be posted goes back with the SAME attempt: the writer's answers are kept by the
        model gate, so it is rebuilt at no model cost (at most WRITE_RETRIES times)."""
        n = 0
        for key in list((self._get(REQUESTS_KEY) or {}).get("keys") or []):
            row = self._get(key)
            if (isinstance(row, dict) and row.get("test") is True and row.get("state") == "unknown"
                    and row.get("research_json") and row.get("report_status") == "ok"
                    and int(row.get("post_retries") or 0) < WRITE_RETRIES):
                self._set_request(key, ("unknown",), state="researched", write_lease=0,
                                  post_retries=int(row.get("post_retries") or 0) + 1)
                n += 1
                continue
            if not (isinstance(row, dict) and row.get("test") is True and row.get("state") == "failed"
                    and row.get("research_json") and str(row.get("report_status") or "").startswith(
                        tuple(f"failed:{s}:" for s, _ in WRITER_STEPS))
                    and int(row.get("write_retries") or 0) < WRITE_RETRIES):
                continue
            self._set_request(key, ("failed",), state="researched", report_status=None, write_lease=0,
                              write_retries=int(row.get("write_retries") or 0) + 1)
            n += 1
        return n

    # ----- KVKK retention
    def _purge(self, local):
        """Once a day: every record of a month older than retention_days becomes a data-free tombstone."""
        today = local.date().isoformat()
        if (self._get(PURGE_KEY) or {}).get("day") == today:
            return {"status": "already_done"}
        now = self.clock().timestamp()
        days = self.cfg["retention_days"]
        purged = months = 0
        first = local.date().replace(day=1)
        for back in range(12, 40):  # months whose whole content is past retention (bounded look-back)
            y, m = divmod(first.year * 12 + first.month - 1 - back, 12)
            start = date(y, m + 1, 1)
            ny, nm = divmod(y * 12 + m + 1, 12)
            end = datetime(ny, nm + 1, 1, tzinfo=ISTANBUL).timestamp()
            if end + days * 86400 > now:
                continue
            ikey = index_key(start.strftime("%Y%m"))
            idx = self._get(ikey)
            if not idx or idx.get("purged"):
                continue
            months += 1
            for k in list(idx.get("keys") or []):
                def op(tx, ts, k=k):
                    row = tx.get(k)
                    if row is None or row.get("kind") in ("prospect_purged",):
                        return 0
                    if row.get("kind") == "prospect_address":  # the one-reply rule survives, the address does not
                        tx.put(k, {"kind": "prospect_address", "replied": bool(row.get("replied")), "purged_at": ts})
                    else:
                        tx.put(k, {"kind": "prospect_purged", "purged_at": ts})
                    return 1
                purged += self.ops._run(op, allow_expired=True)
            self.ops._run(lambda tx, ts, ikey=ikey: tx.put(ikey, {"kind": "prospect_index", "month": ikey[-6:],
                                                                   "keys": [], "purged": True, "purged_at": ts}),
                          allow_expired=True)
        self.ops._run(lambda tx, ts: tx.put(PURGE_KEY, {"kind": "prospect_purge", "day": today, "at": ts}),
                      allow_expired=True)
        return {"status": "purge_completed", "months": months, "records": purged}


REPORT_HOLD_SECONDS = 2700
# Owner 2026-10-16 ("başladıysa güzelce bitmeli"): the strategist reads ALL six research parts, nothing is cut.
# 200 KB is ~60k input tokens (~0.25 USD on Opus 5.5), far inside the writer's 2 USD hold.
MAX_INPUT = {"writer": 200_000}
WRITE_RETRIES = 2  # operator test runs re-run a failed write from the kept research (no new research cost)  # research (<= 15 min) + writer (<= 9 min) + measurements; job timeout 2700 s
OPEN_REQUEST_STATES = ("awaiting_approval", "approved", "researched", "writing_report", "report_ready", "sending")
CLOSED_REQUEST_STATES = ("sent", "send_unknown", "rejected", "expired", "stopped", "failed", "unknown")


def report_mail_text(cfg, *, language, first, company, score):
    """The company mail that carries the PDF (owner 2026-10-16): fixed approved text; only the middle paragraph
    depends on the score (below 'high_from' it names the score, from it on it praises and invites a deeper look)."""
    rm = cfg["approved_sentences"]["report_mail"]
    t = rm[language if language in rm else "tr"]
    greet = t["greeting"].format(first=first) if first else t["greeting_no_name"]
    middle = t["high"] if int(score) >= rm["high_from"] else t["low"].format(score=int(score))
    sig = cfg["signatures"][language if language in cfg["signatures"] else "tr"]
    return "\n\n".join([greet, t["intro"].format(company=company), middle, t["close"], sig])


def check_report_mail(text, *, settings, language, first):
    """The report mail is longer and carries its sources; the other reply rules stay."""
    from v11_client_watch import internal_terms
    cfg = settings["cfg"]
    t = str(text or "").strip()
    out = []
    if not t:
        return ["metin yok"]
    if not t.endswith(cfg["signatures"][language]):
        out.append("imza eksik ya da farklı")
    head = t.split("\n", 1)[0]
    if not head.startswith(("Merhaba",) if language == "tr" else ("Hi", "Hello", "Dear")):
        out.append("selamlama eksik")
    elif first and first not in head:
        out.append("ilk adla hitap yok")
    body = re.sub(r"https://\S+", " ", t)
    if internal_terms(body):
        out.append("iç ifade: " + ", ".join(internal_terms(body)))
    if GENDER.search(body):
        out.append("Hanım/Bey ya da cinsiyetli hitap")
    if PRICE.search(body):
        out.append("fiyat ya da para ifadesi")
    if re.search(r"(?<![a-zçğıöşü])(?:garanti\w*|guarantee\w*|söz veri\w*|promise\w*|taahhüt\w*|en geç)"
                 r"(?![a-zçğıöşü])", body, re.I):
        out.append("söz ya da garanti ifadesi")
    if len(t) > 5000:
        out.append("mail çok uzun")
    return out


# ------------------------------------------------------------------ Gmail adapters (live)
def thread_facts(meta, agency_domain):
    """Newest message's headers and sender + whether agency staff wrote in the thread (metadata only)."""
    rows = (meta or {}).get("messages") or []
    _need(isinstance(rows, list) and rows, "prospect_thread_metadata_invalid")
    newest = max(rows, key=lambda m: int(m.get("internalDate") or 0))
    agency = False
    hamle = False
    for m in rows:
        heads = ((m.get("payload") or {}).get("headers")) or []
        frm = [h.get("value") for h in heads if isinstance(h, dict) and str(h.get("name")).lower() == "from"]
        addrs = [a.lower() for _, a in getaddresses(frm)]
        if HAMLE in addrs:
            hamle = True
        elif any(a.endswith("@" + agency_domain) for a in addrs):
            agency = True
    heads = ((newest.get("payload") or {}).get("headers")) or []
    headers = {}
    for h in heads:
        if isinstance(h, dict) and isinstance(h.get("name"), str):
            headers.setdefault(h["name"].lower(), []).append(str(h.get("value") or ""))
    frm = getaddresses(headers.get("from") or [])
    _need(len(frm) == 1, "prospect_sender_ambiguous")
    name, address = frm[0]
    return {"id": newest.get("id"), "from": address.strip().lower(), "name": name.strip() or None,
            "headers": {k: v for k, v in headers.items() if k in {x.lower() for x in FILTER_HEADERS}},
            "agency_in_thread": agency, "hamle_in_thread": hamle}


def prospect_mail_reader(gmail_store, settings, book, **factories):
    """ChainMailReader on the same verified hamle@ read path, for senders that are not agency or client contacts."""
    from v11_chain_mail import ChainMailError, ChainMailReader
    cfg = settings["cfg"]

    class Everyone:
        def __contains__(self, _):
            return True

    class ProspectMailReader(ChainMailReader):
        shadow = None
        meta_headers = ("Subject", "Message-ID", "References", "From") + FILTER_HEADERS

        def _requesters(self):
            return ["*"]

        def _query(self, requesters):
            skip = sorted({cfg["agency_domain"], *cfg["ignore_domains"], *(book.domains if book else ())})
            return (f"{{to:{HAMLE} cc:{HAMLE}}} " + " ".join("-from:" + d for d in skip)
                    + f" newer_than:{int(self.days)}d")

        def _thread(self, tid, meta, requesters, *rest):
            info = thread_facts(meta, cfg["agency_domain"])
            if info["from"] == HAMLE:
                return None  # Hamle spoke last: nothing new
            if excluded(info["from"], settings, book):
                raise ChainMailError("not_a_prospect")
            reason = prefilter(info["headers"], info["from"], settings)
            if reason:
                return {"filtered": reason, "thread_id": tid, "message_id": info["id"]}
            source = super()._thread(tid, meta, Everyone(), *rest)
            if source is None:
                return None
            return dict(source, sender_name=info["name"], headers=info["headers"],
                        thread_agency=info["agency_in_thread"], thread_hamle=info["hamle_in_thread"])
    return ProspectMailReader(gmail_store, {"contacts": []}, max_threads=cfg["limits"]["max_threads"],
                              newer_than_days=cfg["limits"]["newer_than_days"], **factories)


def prospect_reply_writer(settings, book, *, token_context, session_factory):
    """Same-thread Gmail reply to the prospect (exact sender of the anchor), CC exactly the owner (owner 2026-10-16)."""
    owner_cc = [settings["roles"]["owner"]["email"]]
    import business_writers as bw
    import cloud_providers as provider
    from v11_chain_mail import CLARIFY_FIELDS, HEX, GmailThreadReplyWriter, reply_subject

    class ProspectReplyWriter(GmailThreadReplyWriter):
        def _shape(self, payload):
            _need(isinstance(payload, dict) and payload.get("from") == HAMLE
                  and set(payload) in (CLARIFY_FIELDS, CLARIFY_FIELDS | {"attachment"}), "prospect_reply_payload_invalid")
            if "attachment" in payload:
                from v11_daily_digest import DigestMailWriter
                DigestMailWriter._attachment_bytes(payload["attachment"])
            _need(isinstance(payload["thread_id"], str) and HEX.fullmatch(payload["thread_id"])
                  and payload["thread_id"] not in provider.FORBIDDEN_THREADS
                  and isinstance(payload["in_reply_to_message_id"], str)
                  and HEX.fullmatch(payload["in_reply_to_message_id"]), "prospect_reply_anchor_invalid")
            bw._text(payload["subject"], 300, header=True)
            bw._text(payload["body"], 16000)
            to = payload["to"]
            _need(isinstance(to, list) and len(to) == 1 and payload["cc"] == owner_cc and isinstance(to[0], str)
                  and to[0] != owner_cc[0]
                  and EMAIL.fullmatch(to[0]) and not excluded(to[0], settings, book), "prospect_reply_recipient_invalid")
            return "client_reply"

        def _verify(self, kind, payload, src):
            _need(payload["to"] == [src["sender"]] and payload["cc"] == owner_cc, "reply_recipients_changed")
            _need(payload["subject"] == reply_subject(src["subject"]), "reply_subject_mismatch")

        def latest_anchor(self, thread_id):
            _need(isinstance(thread_id, str) and HEX.fullmatch(thread_id), "prospect_thread_invalid")
            with self.token_context() as token:
                session = self.session_factory()
                try:
                    latest = self._latest_incoming(session, token, thread_id)
                    src = self._source(session, token, thread_id, latest)
                finally:
                    session.close()
            return {"message_id": latest, "sender": src["sender"], "subject": src["subject"]}

    return ProspectReplyWriter({"contacts": []}, token_context=token_context, anchor_lookup=lambda tid: None,
                               session_factory=session_factory)


def preview_mail(source, *, settings, people, models, clock):
    """Operator preview (owner 2026-10-16): the real decisions and model calls for ONE imagined mail, with no mail
    read or sent, no Basecamp, no copy mail and no prospect records. Prints what the live run would do."""
    sender = str(source.get("sender") or "").strip().lower()
    _need(EMAIL.fullmatch(sender) is not None, "preview_sender_invalid")
    domain = sender.rsplit("@", 1)[1]
    local = clock().astimezone(ISTANBUL)
    kind, brand_key = domain_kind(domain, settings)
    out = {"status": "prospect_watch_preview", "sender": sender, "domain": domain, "domain_kind": kind,
           "model_calls": 0, "writes": 0}
    cls = models.classify({"subject": _line(source.get("subject"), 200), "new_text": _mail_text(source),
                           "domain": domain, "domain_kind": kind, "has_attachments": False})
    out["model_calls"] += 1
    category, language = cls["category"], cls["language"]
    if kind == "client" and category not in ("spam", "vendor_or_job"):
        category = "known_client_new_person"
    elif kind != "client" and category == "known_client_new_person":
        category = "demo_or_first_contact"
    research_target = cls.get("research_target") or "none"
    include_research = category == "research_request" and research_target == "organisation" and kind != "individual"
    out.update(category=category, category_tr=CATEGORY_TR.get(category, category), language=language,
               request_summary=cls.get("request_summary"), research_target=research_target)
    if category not in REPLY_CATEGORIES:
        out["reply_text"] = None
        out["decision"] = "no_reply"
        return out
    known = match_person(people, sender, source.get("sender_name"), domain)
    first = first_name(source.get("sender_name"), known)
    company = person = None
    if kind == "corporate":
        research = models.research({"domain": domain, "website": "https://" + domain,
                                    "sender_name": None if known else (first and _line(source.get("sender_name"), 120)),
                                    "search_person": bool(first) and known is None})
        out["model_calls"] += 1
        company, person = company_facts(research, domain), person_facts(research)
    brand = settings["brands"].get(brand_key) if brand_key else None
    request = reply_input(today=local.date(), language=language, category=category, kind=kind, first=first,
                          company=company, brand_name=(brand or {}).get("name"), known=known,
                          include_research=include_research, source=source)
    text = str(models.reply(request)["reply"]).replace("\r\n", "\n").strip()
    out["model_calls"] += 1
    problems = check_reply(text, settings=settings, language=language, first=first, category=category, kind=kind,
                           brand_key=brand_key, include_research=include_research)
    out.update(first_name=first, known_person=(known or {}).get("name"), company=company,
               person_internal_only=person, reply_text=text, reply_problems=problems,
               decision="would_send" if not problems else "would_hold_for_a_human")
    if include_research:
        label = (company or {}).get("name") or domain
        title, body = request_todo(settings["roles"]["owner"]["name"], settings["cfg"]["research"]["estimate"],
                                   source, sender, f"{label} ({domain})" if (company or {}).get("name") else domain,
                                   {"request_summary": cls.get("request_summary")})
        out["research_todo"] = {"title": title, "text": body, "report": "not built in a preview"}
    elif category == "research_request" and research_target == "person":
        out["person_research_refused"] = True
    return out


def _footprint(secrets, psi_key):
    """Optional v11_footprint credentials; a missing one only marks that channel 'not measured'. The YouTube Data
    API takes any Google API key of the project, so the PageSpeed key is the fallback (owner 2026-10-16)."""
    from v11_footprint import load_instagram, load_youtube_key
    return {"youtube": load_youtube_key(secrets) or psi_key, "instagram": load_instagram(secrets)}


def _operator_watch(ops, secrets, settings, book, catalog, models, clock):
    """A watch for the operator runs: Basecamp + the owner-only mail writer; no mailbox reader, no prospect writer."""
    import basecamp_cloud_auth
    import cloud_providers as provider
    from v11_chain_live import _memo
    from v11_chain_mail import gmail_token_context
    from v11_daily_digest import DigestMailWriter
    from v11_digital_karne import public_web
    from v11_nb_tracking import session_send
    token = _memo(lambda: basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh))
    try:
        psi_key = provider.pagespeed_key(secrets)
    except Exception:
        psi_key = None
    return ProspectWatch(
        ops, settings=settings, book=book, people=[], reader=None, models=models,
        http=basecamp_http(settings, send=session_send(token, provider._session)), reply_writer=None,
        copy_writer=DigestMailWriter(token_context=gmail_token_context(secrets), session_factory=provider._session),
        web=public_web() if settings["cfg"]["karne_checks"] else None, psi_key=psi_key, clock=clock,
        footprint=_footprint(secrets, psi_key))


def run_worker_prospect_preview(ops, secrets, *, sender, sender_name, subject, text, clock=None, config=None,
                                open_request=False):
    """Entry for worker.py --mode prospect_watch --prospect-preview: model calls only, nothing written or sent.
    With open_request (--preview-open-request, owner 2026-10-16): a research request also opens a REAL test
    request + to-do for the owner; still no mail to the sender."""
    import cloud_providers as provider
    from v11_chain_live import load_catalog
    from v11_client_watch import load_contacts, validate_contacts
    from v11_daily_digest import load_brand_cards
    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        cfg = validate_config(config if config is not None else load_config())
        catalog = load_catalog()
        book = validate_contacts(load_contacts(ops), catalog, load_brand_cards())
        settings = resolve(cfg, catalog, book)
        _need(not excluded(str(sender or "").strip().lower(), settings, book), "preview_sender_is_agency_or_client")
        keys = {"anthropic": lambda: provider.anthropic_key(secrets)}
        source = {"sender": sender, "sender_name": sender_name, "subject": subject, "text": text}
        models = model_sources(ops, keys, catalog, settings)
        out = preview_mail(source, settings=settings, people=load_people(ops), models=models, clock=clock)
        if not open_request:
            return out
        if "research_todo" not in out:
            return dict(out, status="held", reason="preview_not_an_organisation_research_request")
        facts = {k: out.get(k) for k in ("first_name", "language", "request_summary", "company")}
        watch = _operator_watch(ops, secrets, settings, book, catalog, models, clock)
        opened = watch.open_test_request(source, facts)
        return dict(out, writes=opened.get("business_writes", 0), test_request=opened)
    except Exception as exc:
        return {"status": "held", "reason": _code(exc), "business_writes": 0}


def run_worker_prospect_test_requests(ops, secrets, *, clock=None, config=None):
    """Entry for worker.py --mode prospect_watch --prospect-test-requests: only TEST requests move on."""
    import cloud_providers as provider
    from v11_chain_live import load_catalog
    from v11_client_watch import load_contacts, validate_contacts
    from v11_daily_digest import load_brand_cards
    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        cfg = validate_config(config if config is not None else load_config())
        _need(cfg["research"]["enabled"], "prospect_research_disabled")
        catalog = load_catalog()
        book = validate_contacts(load_contacts(ops), catalog, load_brand_cards())
        settings = resolve(cfg, catalog, book)
        keys = {"anthropic": lambda: provider.anthropic_key(secrets)}
        watch = _operator_watch(ops, secrets, settings, book, catalog, model_sources(ops, keys, catalog, settings),
                                clock)
        return watch.run_test_requests()
    except Exception as exc:
        return {"status": "held", "reason": _code(exc), "business_writes": None}


def run_worker_prospect_watch(ops, secrets, *, clock=None, config=None, started_at=None):
    """Entry for worker.py --mode prospect_watch (existing credential loaders only)."""
    import basecamp_cloud_auth
    import cloud_providers as provider
    from v11_chain_live import _memo, load_catalog
    from v11_chain_mail import gmail_token_context
    from v11_client_watch import load_contacts, validate_contacts
    from v11_daily_digest import DigestMailWriter, load_brand_cards
    from v11_digital_karne import public_web
    from v11_nb_tracking import session_send
    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        cfg = validate_config(config if config is not None else load_config())
        if not cfg["enabled"]:
            return {"status": "no_new_work", "reason": "prospect_watch_disabled", "business_writes": 0}
        catalog = load_catalog()
        book = validate_contacts(load_contacts(ops), catalog, load_brand_cards())  # no table: hold (cannot tell)
        settings = resolve(cfg, catalog, book)
        token = _memo(lambda: basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh))
        keys = {"anthropic": lambda: provider.anthropic_key(secrets)}
        try:
            psi_key = provider.pagespeed_key(secrets)
        except Exception:
            psi_key = None
        watch = ProspectWatch(
            ops, settings=settings, book=book, people=load_people(ops),
            reader=prospect_mail_reader(provider.GmailSecretStore(secrets), settings, book),
            models=model_sources(ops, keys, catalog, settings),
            http=basecamp_http(settings, send=session_send(token, provider._session)),
            reply_writer=prospect_reply_writer(settings, book, token_context=gmail_token_context(secrets),
                                               session_factory=provider._session),
            copy_writer=DigestMailWriter(token_context=gmail_token_context(secrets), session_factory=provider._session),
            web=public_web(), psi_key=psi_key, clock=clock, started_at=started_at,
            footprint=_footprint(secrets, psi_key))
        return watch.run()
    except Exception as exc:
        return {"status": "held", "reason": _code(exc), "business_writes": None}


# ------------------------------------------------------------------ Mac operator CLI (known people)
def people_status(ops):
    try:
        rows = load_people(ops)
    except ProspectError as exc:
        return {"status": "held", "reason": exc.code}
    doc = ops._run(lambda tx, _: tx.get(PEOPLE_KEY), allow_expired=True) or {}
    return {"status": "prospect_people_ready" if doc else "prospect_people_missing", "people": len(rows),
            "sha256": doc.get("sha256"), "actor": doc.get("actor")}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Known people for the prospect watch (Firestore only)")
    parser.add_argument("area", choices=("people",))
    parser.add_argument("action", choices=("status", "import"))
    parser.add_argument("--file")
    parser.add_argument("--actor")
    args = parser.parse_args(argv)
    try:
        from cloud_store import FirestoreBackend, OperationsStore
        from import_cloud_credentials import credentials
        from v11_chain_live import load_catalog
        ops = OperationsStore(FirestoreBackend(opt_in="yes", credentials=credentials()))
        if args.action == "status":
            result = people_status(ops)
        else:
            _need(args.file, "file_required")
            doc = json.loads(Path(args.file).read_text(encoding="utf-8"))
            result = import_people(ops, doc, actor=args.actor, settings=resolve(load_config(), load_catalog()))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ProspectError as exc:
        print(json.dumps({"status": "held", "reason": exc.code}))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "held", "reason": "people_command_failed:" + _code(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
