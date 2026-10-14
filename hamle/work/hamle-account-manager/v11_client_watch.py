"""Customer mail silent trial (Ahmet 2026-10-11, decision 6). OFF by default.

Hamle reads client mail that has hamle@ in To/CC, drafts the reply it WOULD send, and writes
it only as a client-hidden Basecamp note in the internal project (Hamle — İç Deneme ·
Hamle İşleri list): one todo per client mail thread, later mails of that thread as comments.
In mode 'silent_draft' nothing is ever sent to a client. Mode 'reply' (v11_client_reply) answers
in the same mail thread only for the brands in the config's reply block. Price / contract / complaint mails get Ahmet's
fixed sentence as the draft and are marked for Ahmet + Cansu. Once per weekday evening Ahmet
gets one summary mail ("böyle yanıtlardım").

Client addresses come only from the contact table Ahmet/Cansu provide. It lives in Firestore
(doc 'client-contacts', written from the Mac with `contacts import`), never in git. A sender
that matches no row is not read. Public mail domains (gmail.com, ...) only match exact
addresses; the agency domain never matches.

Same rules as the chain: durable record before every provider write, one attempt, exact
readback, anything uncertain is 'unknown' and never retried. Global stop ('dur') or a disabled
config stops everything before any read. Model: role customer_text (Claude Opus 5.5) through
ModelGate under the monthly 30 USD chain cap; one chance per message.

Whether Hamle may write to a client by itself ("direct"): a brand row's "direct" is Markalar.xlsx
"Hamle müşteriye cevap verebilir mi?" (default false) and covers its people and company domains;
a person row may override it ("Hamle bu kişiye doğrudan yazabilir mi?"). Test rows default to true.
Anything not direct waits for "Hamle gönder". A brand row's "note" (tone, house rules, who is who)
goes to the model with every draft for that brand.

Shadow forwards (Ahmet 2026-10-13): an old client mail that a person in config 'shadow_from' forwards
to hamle@ with "Gölge:" in front of the subject is drafted exactly like a new client mail, but the
draft never goes to anyone: it lands as a GÖLGE note next to the team's real reply (when that reply
was forwarded too) and in the evening summary. The chain skips these forwards (v11_chain_mail).

Client jobs (Ahmet 2026-10-13 17:05Z, config 'jobs', v11_client_job): a clear request of a listed brand opens
to-dos in the brand's Basecamp lists, Hamle follows them, scores each upload against the brief and mails 90+
work to the client in the same thread (80-89 waits for an approver's "uygun", below 80 is a revision request).

Basecamp mentions (Ahmet 2026-10-14, config 'mentions', v11_basecamp_mentions): when the team tags Hamle on a
Basecamp to-do, Hamle reads it and answers there, asks one question, says what it cannot do, or drafts a new mail
to the client that goes out after "Hamle gönder"; the client's answer is written back on that to-do.

Mac operator commands (ADC credentials, Firestore only, no cost):
  python3 v11_client_watch.py contacts status
  python3 v11_client_watch.py contacts import --file client-contacts.json --actor ahmet@gamechangers.com.tr
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
from pathlib import Path
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from autonomy_policy import HAMLE, scope_digest

ISTANBUL = ZoneInfo("Europe/Istanbul")
CONFIG_NAME = "client-watch-config.json"
CONTACTS_KEY = "client-contacts"
PROMPT = Path(__file__).with_name("client_draft_prompt.txt")
ROLE = "customer_text"
AGENCY_DOMAIN = "gamechangers.com.tr"
FIXED_SENSITIVE_REPLY = "Aldık, ajans başkanımız Ahmet bu konuda sizlere en kısa sürede yanıt verecek."
PUBLIC_DOMAINS = frozenset((
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.com.tr", "outlook.com", "outlook.com.tr", "live.com",
    "msn.com", "yahoo.com", "yahoo.com.tr", "icloud.com", "me.com", "mac.com", "yandex.com", "yandex.com.tr",
    "mail.ru", "protonmail.com", "proton.me", "aol.com", "gmx.com", "gmx.net", "mynet.com"))
CATEGORIES = ("request", "feedback_or_approval", "question", "information", "meeting", "price", "contract",
              "complaint", "other")
SENSITIVE = ("price", "contract", "complaint")
MODES = ("silent_draft", "reply")  # reply: v11_client_reply, only for the brands in the config's reply block
CATEGORY_TR = {"request": "iş talebi", "feedback_or_approval": "geri bildirim / onay", "question": "soru",
               "information": "bilgi", "meeting": "toplantı", "price": "fiyat", "contract": "sözleşme",
               "complaint": "şikayet", "other": "diğer"}
# Deterministic backstop on the NEW part of the mail (quoted history stripped): any hit makes
# the mail sensitive even if the model says otherwise. Over-flagging only costs a fixed sentence.
SENSITIVE_WORDS = {
    # bare 'indirim' is the client's own campaign (Rabam 14 Oct: "indirim kurgusu" story); only asking us for one counts
    "price": ("fiyat", "teklif", "ücret", "bütçe", "fatura", "ödeme", "maliyet", "indirim yap", "indirim rica",
              "indirim talep", "indirim isti", "indirim bekl", "iskonto", "tutar", "kdv", "havale", "price", "quote",
              "invoice", "payment", "budget"),
    "contract": ("sözleşme", "kontrat", "fesih", "hukuk", "avukat", "ihtar", "noter", "contract"),
    "complaint": ("şikayet", "şikâyet", "memnun değil", "hayal kırıklığı", "kabul edilemez", "rezalet",
                  "çok kötü", "berbat", "complaint", "unacceptable"),
}
QUOTE_MARKERS = (re.compile(r"^\s*on .{0,200} wrote:\s*$", re.I),
                 # live Gmail TR: "... <x@y> adresine sahip kullanıcı 13 Eki 2026 Sal, 21:57 tarihinde şunu yazdı:"
                 re.compile(r"^.{0,300}tarihinde\s+(?:.{0,200}\s)?şunu yazdı:\s*$", re.I),
                 re.compile(r"^.{0,200}adresine sahip kullanıcı(?:\s.{0,120})?$", re.I),  # the same line wrapped
                 re.compile(r"^\s*-{2,}\s*(original message|orijinal ileti|özgün ileti|forwarded message|"
                            r"iletilen ileti)\s*-{2,}\s*$", re.I),
                 re.compile(r"^\s*(from|kimden|gönderen)\s*:\s*.+$", re.I))
# Agency-internal words never belong in a client mail (Ahmet's answers, 2026-10-13). A hit does not
# block the draft; it is shown to the team and caps the draft's confidence.
INTERNAL_TERMS = (
    ("Basecamp", re.compile(r"basecamp", re.I)),
    ("to-do", re.compile(r"(?<![0-9a-zçğıöşü])to-?dos?(?![0-9a-zçğıöşü])", re.I)),
    ("görev", re.compile(r"(?<![0-9a-zçğıöşü])görev(?!l)", re.I)),
    ("Hamle İşleri", re.compile(r"hamle i̇?şleri", re.I)),
    ("dosya türü", re.compile(r"(?<![0-9a-z])(?:image|video|audio|application)/[a-z0-9.+-]+", re.I)),
    ("sistem kodu", re.compile(r"(?<![0-9a-zçğıöşü@._/-])[a-z]+(?:_[a-z]+)+(?![0-9a-zçğıöşü@/-])")),
)
INTERNAL_TERMS_CONFIDENCE = 50
RUBRIC = {"understanding": 30, "reply_type": 25, "no_promises": 20, "questions": 15, "tone": 10}
RUBRIC_TR = {"understanding": "anlama", "reply_type": "yanıt türü", "no_promises": "söz vermeme", "questions": "sorular",
             "tone": "ton"}
MAX_NEW_TEXT = 8000
MAX_CONTEXT_TEXT = 3000
LEASE_SECONDS = 900
EMAIL = re.compile(r"[a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")


class ClientWatchError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ClientWatchError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _sha(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _iso_seconds(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _line(value, limit=300):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


# ------------------------------------------------------------------ config + contact table
def load_config(path=None):
    here = Path(__file__).resolve()
    candidates = [Path(path)] if path else [here.with_name("chain-profile") / CONFIG_NAME,
                                            here.parents[2] / "outputs/gamechangers-internal" / CONFIG_NAME]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise ClientWatchError("client_watch_config_missing")


def validate_config(cfg):
    _need(isinstance(cfg, dict) and cfg.get("schema_version") == 1 and type(cfg.get("enabled")) is bool
          and cfg.get("mode") in MODES, "client_watch_config_invalid")
    _need(cfg["mode"] == "silent_draft" or isinstance(cfg.get("reply"), dict), "client_reply_config_missing")
    to = cfg.get("summary_to")
    _need(isinstance(to, list) and 1 <= len(to) <= 3 and len(set(to)) == len(to)
          and all(isinstance(a, str) and a.endswith("@" + AGENCY_DOMAIN) and a != HAMLE
                  and EMAIL.fullmatch(a) for a in to), "client_watch_summary_recipients_invalid")
    after = cfg.get("summary_after")
    _need(isinstance(after, str) and re.fullmatch(r"(1[2-9]|2[0-2]):[0-5]\d", after), "client_watch_summary_time_invalid")
    shadow = cfg.get("shadow_from", [])
    _need(isinstance(shadow, list) and len(shadow) <= 3 and len(set(shadow)) == len(shadow)
          and all(isinstance(a, str) and a.endswith("@" + AGENCY_DOMAIN) and a != HAMLE and EMAIL.fullmatch(a)
                  for a in shadow), "client_watch_shadow_invalid")
    for key, low, high in (("max_threads", 1, 20), ("max_drafts_per_run", 1, 10), ("newer_than_days", 1, 7),
                           ("run_seconds", 60, 400)):
        _need(type(cfg.get(key)) is int and low <= cfg[key] <= high, "client_watch_config_invalid")
    return cfg


class ContactBook:
    """Validated client table: exact addresses and company domains -> one brand each."""
    def __init__(self, doc, brands):
        _need(isinstance(doc, dict) and doc.get("schema_version") == 1 and isinstance(doc.get("brands"), dict)
              and 1 <= len(doc["brands"]) <= 30, "client_contacts_invalid")
        self.brands, self.emails, self.domains, self.direct, self.notes = {}, {}, {}, {}, {}
        for key, row in doc["brands"].items():
            _need(key in brands and isinstance(row, dict) and set(row) <= {"contacts", "domains", "note", "direct"},
                  "client_contacts_brand_unknown")
            _need(type(row.get("direct", False)) is bool and (row.get("note") is None or (
                isinstance(row["note"], str) and 0 < len(row["note"].strip()) <= 600)), "client_contacts_brand_invalid")
            self.brands[key], self.direct[key], self.notes[key] = brands[key], row.get("direct", False), row.get("note")
            for domain in row.get("domains") or []:
                _need(isinstance(domain, str) and DOMAIN.fullmatch(domain) and domain not in PUBLIC_DOMAINS
                      and not domain.endswith(AGENCY_DOMAIN) and domain not in self.domains,
                      "client_contacts_domain_invalid")
                self.domains[domain] = key
            contacts = row.get("contacts") or []
            _need(isinstance(contacts, list) and len(contacts) <= 40, "client_contacts_invalid")
            for c in contacts:
                self._add(key, c, test=False)
        tests = doc.get("test_contacts") or []
        _need(isinstance(tests, list) and len(tests) <= 3, "client_contacts_invalid")
        for c in tests:
            _need(isinstance(c, dict) and c.get("brand_key") in brands, "client_contacts_brand_unknown")
            self.brands.setdefault(c["brand_key"], brands[c["brand_key"]])
            self._add(c["brand_key"], {k: v for k, v in c.items() if k != "brand_key"}, test=True)
        for domain, key in self.domains.items():  # an exact address on another brand's domain is ambiguous
            _need(all(v["brand_key"] == key for e, v in self.emails.items() if e.split("@")[1] == domain),
                  "client_contacts_conflict")
        _need(self.emails or self.domains, "client_contacts_empty")

    def _add(self, key, c, *, test):
        _need(isinstance(c, dict) and set(c) <= {"email", "name", "title", "direct"}, "client_contacts_row_invalid")
        default = True if test else self.direct.get(key, False)
        _need(type(c.get("direct", default)) is bool, "client_contacts_row_invalid")
        email = c.get("email")
        _need(isinstance(email, str) and EMAIL.fullmatch(email) and not email.endswith("@" + AGENCY_DOMAIN)
              and email not in self.emails, "client_contacts_email_invalid")
        for field in ("name", "title"):
            _need(c.get(field) is None or (isinstance(c[field], str) and 0 < len(c[field].strip()) <= 120),
                  "client_contacts_row_invalid")
        self.emails[email] = {"brand_key": key, "name": c.get("name"), "title": c.get("title"), "test": test,
                              "direct": c.get("direct", default)}  # Markalar: brand "Evet" unless the person says no

    def match(self, address):
        if not isinstance(address, str):
            return None
        address = address.strip().lower()
        if address in self.emails:
            return dict(self.emails[address], email=address)
        domain = address.rsplit("@", 1)[-1]
        if "@" in address and domain in self.domains:
            key = self.domains[domain]
            return {"brand_key": key, "name": None, "title": None, "test": False, "direct": self.direct.get(key, False),
                    "email": address}
        return None

    def __contains__(self, address):  # ChainMailReader eligibility check
        return self.match(address) is not None

    def query_terms(self):
        return sorted(self.emails) + sorted(self.domains)


def brand_index(catalog, cards):
    """brand_key -> {name, project_id, sm/rep person ids, card} from the internal catalog routes."""
    by_project = {c.get("project_id"): c for c in (cards or {}).get("brands", []) if isinstance(c, dict)}
    out = {}
    for key, route in (catalog.get("brand_routes") or {}).items():
        card = by_project.get(route.get("project_id")) or {}
        out[key] = {"key": key, "name": route.get("brand_name") or key, "project_id": route.get("project_id"),
                    "sm_person_id": route.get("sm_person_id"), "rep_person_id": route.get("rep_person_id"),
                    "sector": card.get("sector"), "audience": card.get("audience"),
                    "main_platform": card.get("main_platform")}
    return out


def validate_contacts(doc, catalog, cards=None):
    return ContactBook(doc, brand_index(catalog, cards))


# ------------------------------------------------------------------ text helpers
def latest_part(text):
    """New text of a reply: stops at the first quote marker or quoted ('>') block."""
    out = []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith(">") or any(m.match(line) for m in QUOTE_MARKERS):
            break
        out.append(line)
    new = "\n".join(out).strip()
    return (new or str(text or "").strip())[:MAX_NEW_TEXT]


def sensitive_hits(subject, text):
    blob = (str(subject or "") + "\n" + latest_part(text)).casefold()
    return sorted({cat for cat, words in SENSITIVE_WORDS.items() for w in words if w.casefold() in blob})


def internal_terms(reply):
    text = str(reply or "")
    return [label for label, pattern in INTERNAL_TERMS if pattern.search(text)]


# ------------------------------------------------------------------ shadow forwards ('Gölge:')
FWD_FROM = re.compile(r"^\s*\**\s*(?:from|kimden|gönderen|sender)\s*\**\s*:\s*(.+)$", re.I)
FWD_FIELD = re.compile(r"^\s*\**\s*(date|sent|tarih|gönderildi|gönderilme tarihi|subject|konu|to|kime|alıcı|cc|"
                       r"bilgi|reply-to)\s*\**\s*:\s*(.*)$", re.I)
FWD_MARKER = re.compile(r"^\s*(?:-{2,}.*-{2,}|begin forwarded message:|başlangıç: iletilmiş ileti:?)\s*$", re.I)
ATTRIBUTION = re.compile(r"(?:wrote|yazdı)\s*:\s*$", re.I)
SHADOW_MAX_BODY_BYTES = 60_000
SHADOW_MAX_THREADS = 20


def _display_name(value):
    name = re.split(r"[<\[(]", str(value or ""), maxsplit=1)[0].strip().strip("\"'* ").strip()
    return _line(name, 120) if name and "@" not in name else None


def forward_segments(text):
    """'Gölge:' forward body -> [forwarder's note, newest forwarded mail, older quoted mails ...]."""
    lines = [re.sub(r"^(?:[ \t]*>)+[ \t]?", "", ln) for ln in str(text or "").replace("\r\n", "\n").split("\n")]
    segs = [{"author": None, "name": None, "date": None, "subject": None, "lines": []}]
    i = 0
    while i < len(lines):
        line = lines[i]
        head = FWD_FROM.match(line)
        found = EMAIL.search(head.group(1).lower()) if head else None
        if found:  # forwarded header block: From / Date / Subject / To / Cc
            seg = {"author": found.group(0), "name": _display_name(head.group(1)), "date": None, "subject": None,
                   "lines": []}
            i += 1
            while i < len(lines):
                field = FWD_FIELD.match(lines[i])
                if not field:
                    break
                name = field.group(1).lower()
                if name in ("date", "sent", "tarih", "gönderildi", "gönderilme tarihi"):
                    seg["date"] = _line(field.group(2), 80) or None
                elif name in ("subject", "konu"):
                    seg["subject"] = _line(field.group(2), 200) or None
                i += 1
            segs.append(seg)
            continue
        current, split = segs[-1]["lines"], False
        if ATTRIBUTION.search(line):  # "On ... <address> wrote:" (may be wrapped over up to 3 lines)
            for back in range(0, min(2, len(current)) + 1):
                found = EMAIL.search(" ".join(current[len(current) - back:] + [line]).lower())
                if found:
                    del current[len(current) - back:]
                    segs.append({"author": found.group(0), "name": None, "date": None, "subject": None, "lines": []})
                    split = True
                    break
        if not split:
            current.append(line)
        i += 1
    for seg in segs:
        seg["text"] = "\n".join(ln for ln in seg.pop("lines") if not FWD_MARKER.match(ln)).strip()
    return segs


def parse_forward(text, book):
    """Newest forwarded mail from a known client (+ the team's answer above it, when forwarded too)."""
    segs = forward_segments(text)
    for idx in range(1, len(segs)):
        seg = segs[idx]
        match = book.match(seg["author"])
        if match is None or not seg["text"]:
            continue
        team = next((segs[j] for j in range(idx - 1, 0, -1) if segs[j]["text"]
                     and segs[j]["author"].endswith("@" + AGENCY_DOMAIN) and segs[j]["author"] != HAMLE), None)
        older = [(s["name"] or s["author"]) + ":\n" + s["text"] for s in segs[idx + 1:] if s["text"]]
        return {"client": dict(match, name=match.get("name") or seg["name"]), "date": seg["date"],
                "subject": seg["subject"], "text": seg["text"][:MAX_NEW_TEXT],
                "context": "\n\n".join(older)[:MAX_CONTEXT_TEXT],
                "team_reply": ({"email": team["author"], "name": team["name"], "text": team["text"][:2000]}
                               if team else None),
                "note": _line(segs[0]["text"], 600) or None}
    return None


def shadow_subject(subject):
    """'Gölge: Fwd: X' -> 'X'."""
    from v11_chain_mail import REPLY_PREFIX, SHADOW
    core = REPLY_PREFIX.sub("", str(subject or "")).strip()
    found = SHADOW.match(core)
    return REPLY_PREFIX.sub("", core[found.end():] if found else core).strip()


from v11_attachments import DESCRIBED_SCHEMA  # noqa: E402  (schema shared with the mention prompt)


def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _str(maximum):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


DRAFT_SCHEMA = _obj({
    "category": {"type": "string", "enum": list(CATEGORIES)},
    "summary": _str(500),
    "reply": _str(2500),
    "missing_info": {"type": "array", "items": _str(200), "maxItems": 3},
    "team_next_step": _str(300),
    "confidence": {"type": ["integer", "null"], "minimum": 0, "maximum": 100},
    # Ahmet's 5 criteria (13 Oct): the confidence used for auto-send is their sum, not a single self-estimate.
    "rubric": {"type": ["object", "null"], "properties": {
        "understanding": {"type": "integer", "minimum": 0, "maximum": 30},
        "reply_type": {"type": "integer", "minimum": 0, "maximum": 25},
        "no_promises": {"type": "integer", "minimum": 0, "maximum": 20},
        "questions": {"type": "integer", "minimum": 0, "maximum": 15},
        "tone": {"type": "integer", "minimum": 0, "maximum": 10}},
        "required": ["understanding", "reply_type", "no_promises", "questions", "tone"], "additionalProperties": False},
    # Client jobs (v11_client_job): production can start now, and the separate jobs in the mail.
    # Ahmet 14 Oct: every attachment is read; the model names what each read file is (v11_attachments.unread)
    "attachments_read": DESCRIBED_SCHEMA,
    "job_ready": {"type": "boolean"},
    "jobs": {"type": "array", "maxItems": 3, "items": _obj({
        "title": _str(80), "list": {"type": "string", "enum": ["social", "project"]}, "brief": _str(1500),
        "due_on": {"type": ["string", "null"], "minLength": 10, "maxLength": 10}})}})


def model_input(source, sender, brand, day):
    text = str(source.get("text") or "")
    new = latest_part(text)
    rest = text.replace("\r\n", "\n")
    context = rest[len(new):].strip()[:MAX_CONTEXT_TEXT] if rest.startswith(new) else ""
    return {"today": day.isoformat(), "agency": "Gamechangers",
            "brand": {k: brand.get(k) for k in ("name", "sector", "audience", "main_platform", "note")},
            "sender": {"name": sender.get("name"), "title": sender.get("title")},
            "subject": _line(source.get("subject"), 300), "new_text": new, "quoted_context": context,
            "has_attachments": bool(source.get("has_attachments")),
            "attachments": [{k: v for k, v in row.items() if k in ("name", "type", "read", "reason", "pages", "ratio",
                                                                  "width", "height", "note")}
                            for row in source.get("attachment_status") or []]}


def draft_source(ops, load_key, catalog, *, routing=None, client=None):
    """draft(source, sender, brand, day) -> validated model output; ModelGate once per message."""
    import model as base
    import model_routing
    from v11_chain_live import budget_gate
    from v11_chain_models import ModelGate, internal_cap_microdollars, key_for, reservation_for, source_digest, validate
    from v11_trend_search import route_fingerprint
    route = (routing if routing is not None else model_routing.load()).route(ROLE)
    _need(route.provider == "anthropic", "client_draft_route_not_claude")
    cap = internal_cap_microdollars(catalog)
    hold = reservation_for(route)
    budget_ok = budget_gate(ops, hold, cap)

    def draft(source, sender, brand, day, blocks=()):
        request = model_input(source, sender, brand, day)
        read = sorted(r["name"] for r in source.get("attachment_status") or [] if r.get("read"))
        digest = source_digest("client_draft", source["fingerprint"], route_fingerprint(route, PROMPT),
                               *(["files"] + read if read else []))
        gate = ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)

        def call():
            try:
                system = PROMPT.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise ClientWatchError("client_draft_prompt_unavailable") from None
            key = key_for(load_key, "anthropic")
            try:
                if not blocks:
                    return base.anthropic_json(route, system, request, DRAFT_SCHEMA, api_key=key, client=client,
                                               validator=lambda v: validate(v, DRAFT_SCHEMA) or v)
                # the mail's attachments go to the model with it (images, PDFs, document text)
                text = json.dumps(request, ensure_ascii=False, sort_keys=True)
                _need(len(text.encode("utf-8")) <= base.MAX_INPUT_BYTES, "client_draft_input_too_large")
                kwargs = base.anthropic_request(route, system, text, schema=DRAFT_SCHEMA)
                kwargs["messages"] = [{"role": "user", "content": list(blocks) + [{"type": "text", "text": text}]}]
                data = base.anthropic_send(key, route, kwargs, client=client)
                result = base.anthropic_extract(route, data, structured=True)
                try:
                    value = base._loads(result["text"])
                    validate(value, DRAFT_SCHEMA)
                except Exception:
                    raise base._anthropic_error("invalid_analysis_schema", data, route) from None
                return {"output": value, "receipt": result["receipt"]}
            finally:
                del key
        return gate.once("client_draft", digest, call, provider="anthropic", hold=hold)
    draft.budget_ok = budget_ok
    return draft


# ------------------------------------------------------------------ note text
def _when(ms):
    return datetime.fromtimestamp(ms / 1000, ISTANBUL).strftime("%d.%m.%Y %H:%M") if isinstance(ms, int) else "-"


def note_title(brand, sender, subject, shadow=False):
    prefix = ("[GÖLGE · " if shadow else "[TEST · " if sender.get("test") else "[") + brand["name"] + "]"
    return f"{prefix} Müşteri maili · {_line(subject, 150) or '(konu yok)'}"[:200]


REPLY_HEADERS = {
    "sent": ("GÖNDERİLDİ · Hamle bu yanıtı müşteriye aynı mail zincirinde, alıcıları koruyarak gönderdi. "
             "Ekip: işi markanın Basecamp projesinde açıp müşteriye söylendiği gibi ilerletin."),
    "send_unknown": ("GÖNDERİM BELİRSİZ · Hamle yanıtı gönderdi ama teyit edemedi ve tekrar denemedi. "
                     "Gmail'de Gönderilmiş kutusuna bakın."),
    "stale": ("GÖNDERİLMEDİ · Zincirde daha yeni bir mail var (müşteri yeniden yazdı ya da ekipten biri cevap "
              "verdi); bu maili ekip cevaplasın."),
}
APPROVAL_HELP = ('Göndermek için bu nota "Hamle gönder" yaz. Düzeltip göndermek için "Hamle gönder" satırının '
                 'altına cevabın tam metnini yaz. Gönderilmesin istersen "Hamle gönderme" yaz.')


SHADOW_HEADER = ("GÖLGE · İletilen eski bir müşteri maili. Bu yanıt kimseye GÖNDERİLMEZ; ekibin gerçek cevabıyla "
                 "karşılaştırmak için yazıldı.")


JOBS_OPENED = ("GÖNDERİLDİ · Hamle bu yanıtı müşteriye aynı mail zincirinde, alıcıları koruyarak gönderdi ve işi "
               "markanın Basecamp projesinde açtı (aşağıda). Hamle işi orada takip ediyor.")


JOB_STATES = {"open": None, "creating": "açılıyordu", "awaiting_approval": "onay bekliyor",
              "delivered": "müşteriye teslim edildi", "team": "teslim ekipte", "closed": "tamamlandı",
              "send_unknown": "teslim belirsiz", "expired": "takip bitti", "held": "takip durdu",
              "unknown": "AÇILIP AÇILMADIĞI BELİRSİZ, markanın projesine bakın (tekrar denenmedi)"}


def jobs_lines(jobs):
    """Note lines for the client jobs of this mail (v11_client_job.JobDesk.from_mail)."""
    if jobs and jobs.get("kind") == "held":
        return ["İşi Basecamp'te açamadım (" + str(jobs.get("reason")) + "); ekip açsın.", ""]
    if not jobs or not jobs.get("jobs"):
        return []
    out = ["Bu zincirin Basecamp işleri:" if jobs.get("kind") == "followup" else "Basecamp'te açılan iş:"]
    for j in jobs["jobs"]:
        state = JOB_STATES.get(j.get("state"), j.get("state"))
        out.append("- " + str(j.get("title")) + (" · " + j["url"] if j.get("url") else "")
                   + (" (" + j["assignee"] + ")" if j.get("assignee") else "") + (" · " + state if state else ""))
    if jobs.get("kind") == "followup":
        out.append("Müşterinin yeni maili bu işlerin to-do'larına yorum olarak yazıldı." if jobs.get("comments")
                   else "Müşterinin yeni mailini işlerin to-do'larına yazamadım; ekip baksın.")
    return out + [""]


def note_header(plan, shadow=False, jobs_opened=False):
    if shadow:
        return SHADOW_HEADER
    if jobs_opened and plan is not None and plan["outcome"] == "sent":
        return JOBS_OPENED
    if plan is None:
        return "SESSİZ DENEME · Bu yanıt müşteriye GÖNDERİLMEDİ. Hamle'nin göndereceği taslak aşağıda."
    if plan["outcome"] == "awaiting_approval":
        return ("ONAY BEKLİYOR · Hamle bu yanıtı müşteriye göndermedi. Neden: " + "; ".join(plan["reasons"])
                + ".\n" + APPROVAL_HELP)
    if plan["outcome"] == "manual":
        return "GÖNDERİLMEDİ · " + "; ".join(plan["reasons"]) + ". Bu maili ekip cevaplasın."
    return REPLY_HEADERS[plan["outcome"]]


def note_body(draft, brand, sender, source, plan=None, shadow=None, jobs=None):
    who = " ".join(x for x in (sender.get("name"), f"({sender['title']})" if sender.get("title") else None) if x)
    opened = bool(jobs) and jobs.get("kind") == "opened" and any(j.get("state") == "open" for j in jobs["jobs"])
    lines = [note_header(plan, shadow is not None, jobs_opened=opened), "",
             "Marka: " + brand["name"],
             "Gönderen: " + ((who + " <" + sender["email"] + ">") if who else sender["email"]),
             "Tarih: " + ((shadow["date"] or "-") if shadow is not None else _when(source.get("received_at_ms"))),
             "Konu: " + (_line(source.get("subject"), 200) or "(konu yok)")]
    if shadow is not None:
        lines.append("İleten: " + str(shadow["forwarded_by"]) + (" · Notu: " + shadow["note"] if shadow["note"] else ""))
    lines.append("")
    if draft.get("summary"):
        lines += ["Mail özeti (Hamle):", draft["summary"], ""]
    if plan is not None and plan["outcome"] in ("sent", "send_unknown"):
        lines += ["Gönderilen yanıt:", plan["body"], "",
                  "Alıcılar: " + ", ".join(plan["to"]) + ("; CC: " + ", ".join(plan["cc"]) if plan["cc"] else ""), ""]
    else:
        lines += ["Hamle'nin yanıt taslağı:", plan["body"] if plan else draft["reply"], ""]
    lines += jobs_lines(jobs)
    if shadow is not None:
        team = shadow["team_reply"]
        lines += (["Ekibin gerçek cevabı (" + (team["name"] or team["email"]) + "):", team["text"][:1200], ""] if team
                  else ["Ekibin cevabı iletilen mailde yok. Karşılaştırmak için zincirdeki ekip cevabını da "
                        "\"Gölge:\" ile iletebilirsin.", ""])
    if draft.get("sensitive"):
        lines += ["Hassas konu (" + ", ".join(CATEGORY_TR.get(c, c) for c in draft["sensitive"]) +
                  "): Hamle yalnız sabit cümleyle yanıt verir; konu Ahmet ve Cansu'da.", ""]
    if draft.get("attachments") or draft.get("attachments_unread"):
        from v11_attachments import status_lines
        lines += ["Ekler:"] + (status_lines(draft.get("attachments"), draft.get("attachments_read"))
                               or ["- ekler alınamadı"]) + [""]
    if draft.get("missing_info"):
        lines += ["Eksik bilgi:"] + ["- " + m for m in draft["missing_info"]] + [""]
    if draft.get("team_next_step"):
        lines += ["Ekip için öneri: " + draft["team_next_step"]]
    if draft.get("internal_terms"):
        lines += ["Dikkat: taslakta müşteriye gitmemesi gereken iç ifade var (" + ", ".join(draft["internal_terms"])
                  + "). Bu haliyle gönderilmez."]
    meta = "Tür: " + CATEGORY_TR.get(draft.get("category"), "-")
    if draft.get("confidence") is not None:
        meta += f" · Güven: {draft['confidence']}/100"
        if draft.get("rubric"):
            meta += " (" + ", ".join(f"{RUBRIC_TR[k]} {draft['rubric'][k]}/{RUBRIC[k]}" for k in RUBRIC) + ")"
    if draft.get("model_status") != "ok":
        meta += " · Model: " + str(draft.get("model_status"))
    lines.append(meta)
    return "\n".join(lines)[:3900]


def finalize_draft(output, source, model_status):
    hits = sensitive_hits(source.get("subject"), source.get("text"))
    out = {"category": None, "summary": None, "reply": None, "missing_info": [], "team_next_step": None,
           "confidence": None, "rubric": None, "model_status": model_status, "job_ready": False, "jobs": []}
    if isinstance(output, dict):
        out.update({k: output.get(k) for k in ("category", "summary", "reply", "team_next_step", "confidence")})
        out["missing_info"] = list(output.get("missing_info") or [])
        rubric = output.get("rubric")
        if isinstance(rubric, dict) and set(rubric) == set(RUBRIC) and all(
                type(rubric[k]) is int and 0 <= rubric[k] <= RUBRIC[k] for k in RUBRIC):
            out["rubric"] = dict(rubric)
            out["confidence"] = sum(rubric.values())
        out["job_ready"] = output.get("job_ready") is True
        out["jobs"] = copy.deepcopy(list(output.get("jobs") or []))
    sensitive = sorted(set(hits) | ({out["category"]} if out["category"] in SENSITIVE else set()))
    out["sensitive"] = sensitive
    if sensitive:
        out["reply"] = FIXED_SENSITIVE_REPLY
    elif not out["reply"]:
        out["reply"] = "(taslak alınamadı: " + str(model_status) + ")"
    out["internal_terms"] = [] if sensitive else internal_terms(out["reply"])
    # Attachments (Ahmet 14 Oct, no exceptions): a file not read, or read but not described, stops auto-send
    from v11_attachments import unread
    out["attachments"] = list(source.get("attachment_status") or [])
    out["attachments_read"] = list((output or {}).get("attachments_read") or []) if isinstance(output, dict) else []
    out["attachments_unread"] = unread(out["attachments"], out["attachments_read"])
    if source.get("has_attachments") and not out["attachments"]:
        out["attachments_unread"] = ["(ekler alınamadı)"]
    if out["internal_terms"] and isinstance(out["confidence"], int):
        out["confidence"] = min(out["confidence"], INTERNAL_TERMS_CONFIDENCE)
    return out


# ------------------------------------------------------------------ Basecamp writers (internal project only)
def internal_target(catalog):
    bc = catalog["basecamp"]
    route = next(r for r in catalog["routes"] if r.get("service_id") == catalog["chain"]["default_service_id"])
    _need(bc.get("verified") is True and bc.get("customer_visible") is False and route.get("verified") is True
          and type(route.get("list_id")) is int, "client_note_target_unverified")
    return {"account_id": bc["account_id"], "project_id": bc["project_id"], "list_id": route["list_id"]}


class InternalNoteTaskWriter:
    """One todo in the internal 'Hamle İşleri' list (clients disabled), no assignee, notify off.

    write(payload) -> {'remote_id'}; readback(remote_id, payload) -> {'readback_sha256'}.
    """
    def __init__(self, catalog, *, token_provider, session_factory=None):
        import cloud_providers as provider
        from v11_chain import BasecampCommentWriter
        self.target = internal_target(catalog)
        self._http = BasecampCommentWriter(catalog, token_provider=token_provider,
                                           session_factory=session_factory or provider._session)

    def _check(self, payload):
        _need(isinstance(payload, dict) and set(payload) - {"assignee_ids"} == {"target", "title", "description"}
              and (payload.get("assignee_ids") or []) == sorted(set(payload.get("assignee_ids") or []))
              and len(payload.get("assignee_ids") or []) <= 2
              and all(type(a) is int and a > 0 for a in payload.get("assignee_ids") or [])
              and payload["target"] == self.target and isinstance(payload["title"], str)
              and 0 < len(payload["title"]) <= 200 and isinstance(payload["description"], str)
              and 0 < len(payload["description"]) <= 4000, "client_note_payload_invalid")
        t = self.target
        return f"{self._http.ORIGIN}/{t['account_id']}/buckets/{t['project_id']}"

    def write(self, payload):
        import business_writers as bw
        base = self._check(payload)
        row = self._http._request("POST", f"{base}/todolists/{self.target['list_id']}/todos.json",
                                  {"content": payload["title"], "description": bw._description(payload["description"]),
                                   "assignee_ids": list(payload.get("assignee_ids") or []),
                                   "completion_subscriber_ids": [], "notify": bool(payload.get("assignee_ids"))},
                                  expected=201)
        _need(type(row.get("id")) is int and row["id"] > 0, "client_note_receipt_unconfirmed")
        return {"remote_id": str(row["id"])}

    def readback(self, remote_id, payload):
        import business_writers as bw
        from v11_brand_task import HAMLE_PERSON
        base = self._check(payload)
        _need(isinstance(remote_id, str) and re.fullmatch(r"[0-9]{1,30}", remote_id), "client_note_receipt_invalid")
        row = self._http._request("GET", f"{base}/todos/{remote_id}.json")
        parent = row.get("parent") or {}
        _need(row.get("id") == int(remote_id) and (row.get("bucket") or {}).get("id") == self.target["project_id"]
              and parent.get("id") == self.target["list_id"] and parent.get("type") == "Todolist"
              and (row.get("creator") or {}).get("id") == HAMLE_PERSON and row.get("visible_to_clients") is False
              and row.get("content") == payload["title"]
              and sorted(a.get("id") for a in row.get("assignees") or [] if isinstance(a, dict))
              == list(payload.get("assignee_ids") or [])
              and bw._description_matches(row.get("description"), payload["description"]),
              "client_note_readback_mismatch")
        return {"readback_sha256": scope_digest(payload)}


# ------------------------------------------------------------------ durable records (ops collection)
def mail_key(message_id):
    return "client-mail-" + _sha(message_id)


def thread_key(thread_id):
    return "client-thread-" + _sha(thread_id)


def day_key(day):
    return "client-day-" + day.strftime("%Y%m%d")


def summary_key(day):
    return "client-summary-" + day.strftime("%Y%m%d")


def reply_key(thread_id):
    return "client-reply-" + _sha(thread_id)


APPROVALS_KEY = "client-approvals"
REPLY_KEEP = ("reply_state", "reply_to", "reply_cc", "reply_subject", "reply_body", "anchor", "reply_reasons",
              "reply_payload_sha256", "reply_remote_id", "reply_reason", "sending_at", "sent_at", "approved_by", "edited")


class ClientWatch:
    """One pass: read client mail, draft, write internal notes; then the evening summary."""
    def __init__(self, ops, *, config, book, reader, draft, task_writer, comment_writer, mail_writer, catalog,
                 clock, link_base="https://3.basecamp.com", monotonic=time.monotonic, started_at=None,
                 reply_writer=None, basecamp_get=None, shadow_reader=None, jobs_factory=None, mentions_factory=None,
                 brands=None, attachments=None):
        self.ops, self.cfg, self.book, self.reader = ops, validate_config(config), book, reader
        self.fetch_attachment = attachments  # (message_id, attachment_id) -> bytes; None: attachments not read
        self.draft, self.task_writer, self.comment_writer = draft, task_writer, comment_writer
        self.mail_writer, self.catalog, self.clock = mail_writer, catalog, clock
        self.target = internal_target(catalog)
        self.reply = None
        if self.cfg["mode"] == "reply":
            from v11_client_reply import validate_reply
            self.reply = validate_reply(self.cfg["reply"], catalog)
            _need(reply_writer is not None and basecamp_get is not None, "client_reply_adapters_missing")
        self.reply_writer, self.basecamp_get = reply_writer, basecamp_get
        if self.cfg.get("shadow_from"):
            from v11_client_reply import agency_people
            _need(set(self.cfg["shadow_from"]) <= set(agency_people(catalog)), "client_watch_shadow_unverified")
        self.shadow_reader = shadow_reader if self.cfg.get("shadow_from") else None
        self.link_base, self.monotonic, self.started_at = link_base, monotonic, started_at
        self.generation = None
        self.jobs = None  # client jobs (v11_client_job): Basecamp to-do, visual check, delivery
        if self.reply is not None and jobs_factory is not None:
            from v11_client_job import JobDesk, validate_jobs
            settings = validate_jobs(self.cfg.get("jobs"), catalog, self.reply)
            if settings is not None:
                self.jobs = JobDesk(self, settings, basecamp_get=basecamp_get, **jobs_factory(settings))
        self.mentions = None  # Basecamp mentions (v11_basecamp_mentions): answers, drafts, "Hamle gönder"
        if self.reply is not None and mentions_factory is not None:
            from v11_basecamp_mentions import MentionDesk, validate_mentions
            settings = validate_mentions(self.cfg.get("mentions"), catalog, self.reply)
            if settings is not None:
                self.mentions = MentionDesk(self, settings, basecamp_get=basecamp_get,
                                            brands=brands if brands is not None else brand_index(catalog, None),
                                            **mentions_factory(settings))

    # ----- control
    def _fence(self, tx, now):
        c = self.ops._control(tx)
        self.ops._open(c, self.generation, now)  # global stop ('dur') or a new generation -> no write
        b = self.ops._budget(tx)
        _need(not b["overrun_hold"], "budget_overrun_hold")

    def _get(self, key):
        return self.ops._run(lambda tx, _: tx.get(key))

    def _update(self, key, token, **fields):
        def op(tx, now):
            row = tx.get(key)
            _need(row is not None and row.get("token") == token, "client_claim_lost")
            row.update(updated_at=now, **fields)
            tx.put(key, row)
            return row
        return self.ops._run(op, allow_expired=True)

    def run(self):
        now = self.clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        started = self.started_at if self.started_at is not None else self.monotonic()  # job start: chain time counts
        report = {"status": "client_watch_completed", "mails": 0, "notes": 0, "held": 0,
                  "skipped": 0, "summary": None, "model_calls": 0, "business_writes": 0,
                  "sent": 0, "awaiting_approval": 0, "approvals": None, "shadows": 0}
        if not self.cfg["enabled"]:
            return dict(report, status="no_new_work", reason="client_watch_disabled")
        c = self.ops.control()
        if c["mode"] != "review_open":
            return dict(report, status="stopped", reason="human_control_stopped")
        self.generation = c["generation"]
        if self.reply is not None:
            try:  # people waiting on an approval go first
                report["approvals"] = self._approvals(report, started)
            except Exception as exc:
                report["approvals"] = {"status": "held", "reason": _code(exc)}
        try:
            sources = [s for s in self.reader() or [] if not s.get("shadow")]
        except Exception as exc:  # the evening summary still goes out
            sources, report["reason"] = [], "mail_read_failed:" + _code(exc)
        report["mails"] = len(sources)
        if self.shadow_reader is not None:
            try:
                shadows = [s for s in self.shadow_reader() or [] if s.get("shadow") is True]
            except Exception as exc:
                shadows, report["shadow_reason"] = [], "shadow_read_failed:" + _code(exc)
            report["shadows"] = len(shadows)
            sources += shadows
        sources.sort(key=lambda s: (bool(s.get("shadow")), s.get("received_at_ms") or 0))  # real client mail first
        for source in sources:
            # Bounded per run (the chain job has a 600 s task timeout): later mails wait for the next tick.
            if (report["model_calls"] >= self.cfg["max_drafts_per_run"]
                    or self.monotonic() - started > self.cfg["run_seconds"]):
                report["skipped"] += 1
                continue
            try:
                outcome = self._one(source, report)
            except Exception as exc:  # one mail never stops the others
                outcome = "error:" + _code(exc)
            if outcome == "noted":
                report["notes"] += 1
            elif outcome in ("held", "unknown") or outcome.startswith("error:"):
                report["held"] += 1
            else:
                report["skipped"] += 1
        if self.mentions is not None:
            try:
                report["mentions"] = self.mentions.run(report, started)
            except Exception as exc:
                report["mentions"] = {"status": "held", "reason": _code(exc)}
        if self.jobs is not None:
            try:
                report["jobs"] = self.jobs.follow(report, started)
            except Exception as exc:
                report["jobs"] = {"status": "held", "reason": _code(exc)}
        try:
            report["summary"] = self._summary()
        except Exception as exc:
            report["summary"] = {"status": "held", "reason": _code(exc)}
        if report["summary"] and report["summary"].get("status") == "summary_sent":
            report["business_writes"] += 1
        return report

    # ----- one client mail
    def _one(self, source, report):
        shadow = None
        if source.get("shadow"):  # 'Gölge:' forward: draft the forwarded client mail, never send
            parsed = parse_forward(source.get("text"), self.book)
            if parsed is None:
                return self._shadow_unreadable(source)
            shadow = dict(parsed, forwarded_by=source.get("sender"))
            sender = parsed["client"]
            context = ("\n\n-----Original Message-----\n" + parsed["context"]) if parsed["context"] else ""
            source = {"message_id": source["message_id"], "thread_id": source["thread_id"],
                      "fingerprint": source["fingerprint"], "received_at_ms": source.get("received_at_ms"),
                      "subject": parsed["subject"] or shadow_subject(source.get("subject")),
                      "text": parsed["text"] + context, "has_attachments": bool(source.get("has_attachments"))}
        else:
            sender = self.book.match(source.get("sender"))
        if sender is None:
            return "not_client"
        if self.reply is not None and source["thread_id"] in self.reply["team_threads"]:
            return "team_thread"  # Ahmet: this conversation is the team's; Hamle does nothing in it
        if shadow is None and self._team_first(source, sender):
            return "waiting_for_team"  # nothing claimed: if the teammate answers, the thread is theirs
        brand = dict(self.book.brands[sender["brand_key"]], note=self.book.notes.get(sender["brand_key"]))
        local = self.clock().astimezone(ISTANBUL)
        mkey, tkey = mail_key(source["message_id"]), thread_key(source["thread_id"])
        dkey = day_key(local.date())
        token = uuid.uuid4().hex

        def claim(tx, now):
            self._fence(tx, now)
            row, day = tx.get(mkey), tx.get(dkey)
            if row is not None and not (row.get("state") == "claimed" and now >= row["lease_until"]):
                return None
            new = {"key": mkey, "kind": "client_mail", "state": "claimed", "token": token, "claimed_at": now,
                   "lease_until": now + LEASE_SECONDS, "message_id": source["message_id"],
                   "thread_id": source["thread_id"], "brand_key": sender["brand_key"], "test": sender["test"],
                   "sender_name": sender.get("name"), "sender_title": sender.get("title"),
                   "subject": _line(source.get("subject"), 200), "received_at_ms": source.get("received_at_ms"),
                   "day": local.date().isoformat()}
            if shadow is not None:
                new.update(shadow=True, forwarded_by=shadow["forwarded_by"], client_email=sender["email"],
                           client_date=shadow["date"], team_reply=shadow["team_reply"], shadow_note=shadow["note"])
            new.update({k: v for k, v in (row or {}).items() if k in REPLY_KEEP})  # a reply already sent stays known
            keys = list((day or {}).get("message_keys") or [])
            if mkey not in keys:
                tx.put(dkey, dict(day or {}, kind="client_day", day=local.date().isoformat(),
                                  message_keys=(keys + [mkey])[-200:]))
            tx.put(mkey, new)
            return new
        if not getattr(self.draft, "budget_ok", lambda: True)():
            return "waiting_for_model_budget"  # nothing claimed: the next tick tries again
        if self.ops._run(claim) is None:
            return "already_recorded"
        thread = self._get(tkey)
        if thread is not None and thread.get("state") != "ready":  # its first note is uncertain: no model call
            self._update(mkey, token, state="held", reason="thread_note_outcome_unknown",
                         **self._fields(finalize_draft(None, source, "not_called")))
            return "held"
        blocks = ()
        if source.get("has_attachments") or source.get("attachments"):
            source, blocks = self._attachments(source)
        try:
            report["model_calls"] += 1
            output = (self.draft(source, sender, brand, local.date(), blocks) if blocks
                      else self.draft(source, sender, brand, local.date()))
            status = "ok"
        except Exception as exc:
            output, status = None, "failed:" + _code(exc)
        draft = finalize_draft(output, source, status)
        prior = self._get(mkey) or {}
        if shadow is not None:
            plan = None  # a shadow is never sent
        elif prior.get("reply_state") in ("sent", "send_unknown", "sending"):  # released after the send: never again
            plan = {"outcome": "sent" if prior["reply_state"] == "sent" else "send_unknown", "reasons": [],
                    **{k: prior[k] for k in ("reply_to", "reply_cc", "reply_subject", "reply_body", "anchor")},
                    "thread_id": source["thread_id"]}
            plan.update(to=plan.pop("reply_to"), cc=plan.pop("reply_cc"), subject=plan.pop("reply_subject"),
                        body=plan.pop("reply_body"))
        else:
            plan = self._plan(draft, source, sender)
        if plan is not None and plan["outcome"] == "send":
            sent = self._send(mkey, token, plan, report)
            if sent.startswith("released:"):  # stop / new generation / budget hold: nothing sent
                try:
                    self._update(mkey, token, lease_until=0, reason=sent.split(":", 1)[1])
                except Exception:
                    pass
                return "released"
            plan["outcome"] = sent if sent in ("sent", "send_unknown") else (
                "stale" if sent == "blocked:newer_incoming_mail_in_thread" else "awaiting_approval")
            if plan["outcome"] == "awaiting_approval":
                plan["reasons"] = ["göndermeden önceki kontrol geçmedi (" + sent.split(":", 1)[-1] + ")"]
        jobs = None
        if self.jobs is not None and shadow is None:
            try:
                jobs = self.jobs.from_mail(source, sender, draft, plan, report)
            except Exception as exc:
                jobs = {"kind": "held", "jobs": [], "comments": 0, "reason": _code(exc)}
        body = note_body(draft, brand, sender, source, plan, shadow=shadow, jobs=jobs)
        opened = bool(jobs) and jobs.get("kind") == "opened" and any(j.get("state") == "open" for j in jobs["jobs"])
        if thread is None:
            kind, writer = "todo", self.task_writer
            payload = {"target": dict(self.target),
                       "title": note_title(brand, sender, source.get("subject"), shadow=shadow is not None),
                       "description": body}
            if plan is not None and plan["outcome"] in ("awaiting_approval", "manual"):  # the brand owner is notified
                payload["assignee_ids"] = [self._owner(sender)]
            elif plan is not None and plan["outcome"] in ("sent", "send_unknown") and not opened:  # the team opens the job
                payload["assignee_ids"] = self._team(sender)
        else:
            kind, writer = "comment", self.comment_writer
            payload = {"target": dict(self.target, task_id=thread["todo_id"]), "body": body, "mention_ids": [],
                       "internal_only": True}
        sha = scope_digest(payload)

        def begin(tx, now):
            self._fence(tx, now)
            row, current = tx.get(mkey), tx.get(tkey)
            _need(row is not None and row.get("token") == token and row["state"] == "claimed", "client_claim_lost")
            _need((current is None) == (kind == "todo") and (current is None or current.get("state") == "ready"),
                  "client_thread_changed")
            row.update(state="writing", note_kind=kind, payload_sha256=sha, writing_at=now, **self._fields(draft))
            if plan is not None and row.get("reply_state") is None:
                row.update(reply_state="awaiting_approval" if plan["outcome"] == "awaiting_approval" else plan["outcome"],
                           reply_reasons=plan["reasons"], reply_to=plan["to"], reply_cc=plan["cc"],
                           reply_subject=plan["subject"], reply_body=plan["body"], anchor=plan["anchor"])
            tx.put(mkey, row)
            if kind == "todo":
                tx.put(tkey, {"key": tkey, "kind": "client_thread", "state": "creating", "thread_id": source["thread_id"],
                              "brand_key": sender["brand_key"], "todo_id": None, "message_key": mkey, "created_at": now})
            return row
        try:
            self.ops._run(begin)
        except Exception as exc:  # stop/generation change or a thread race: nothing written, release the claim
            self._update(mkey, token, lease_until=0, reason=_code(exc))  # (the draft stays cached in ModelGate)
            return "released"
        try:
            report["business_writes"] += 1
            remote = writer.write(copy.deepcopy(payload))["remote_id"]
            back = writer.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == sha, "client_note_readback_mismatch")
        except Exception as exc:
            reason = _code(exc)
            self._update(mkey, token, state="unknown", reason=reason)
            if kind == "todo":
                self.ops._run(lambda tx, now: tx.put(tkey, dict(tx.get(tkey) or {}, state="unknown", reason=reason,
                                                                updated_at=now)), allow_expired=True)
            return "unknown"
        todo_id = int(remote) if kind == "todo" else thread["todo_id"]
        row = self._update(mkey, token, state="noted", remote_id=remote, todo_id=todo_id, noted_at=self.clock().timestamp(),
                           jobs=(jobs or {}).get("jobs") or None)
        if kind == "todo":
            self.ops._run(lambda tx, now: tx.put(tkey, dict(tx.get(tkey), state="ready", todo_id=todo_id,
                                                            updated_at=now)), allow_expired=True)
        if row.get("reply_state") == "awaiting_approval":
            report["awaiting_approval"] += 1
            self.ops._run(lambda tx, now: tx.put(APPROVALS_KEY, {"kind": "client_approvals", "keys": list(dict.fromkeys(
                list((tx.get(APPROVALS_KEY) or {}).get("keys") or []) + [mkey]))[-50:]}), allow_expired=True)
        if self.mentions is not None and shadow is None:  # a thread Hamle started from a Basecamp mention
            try:
                self.mentions.client_mail(source, sender, row, todo_id, report)
            except Exception:
                pass
        return "noted"

    def _shadow_unreadable(self, source):
        """A 'Gölge:' forward with no known client mail in it: one row so the evening summary says so."""
        local = self.clock().astimezone(ISTANBUL)
        mkey, dkey = mail_key(source["message_id"]), day_key(local.date())

        def op(tx, now):
            self._fence(tx, now)
            if tx.get(mkey) is not None:
                return "already_recorded"
            day = tx.get(dkey) or {}
            keys = list(day.get("message_keys") or [])
            tx.put(dkey, dict(day, kind="client_day", day=local.date().isoformat(), message_keys=(keys + [mkey])[-200:]))
            tx.put(mkey, {"key": mkey, "kind": "client_mail", "state": "held", "reason": "shadow_client_not_found",
                          "shadow": True, "forwarded_by": source.get("sender"), "message_id": source["message_id"],
                          "thread_id": source["thread_id"], "subject": _line(source.get("subject"), 200),
                          "received_at_ms": source.get("received_at_ms"), "day": local.date().isoformat(),
                          "claimed_at": now})
            return "held"
        return self.ops._run(op)

    # ----- client replies (v11_client_reply)
    def _team_first(self, source, sender):
        """A client mail written to a teammate (hamle@ only in CC) waits team_grace_minutes for that person."""
        grace = (self.reply or {}).get("team_grace_seconds") or 0
        if not grace or sender.get("test") or sender["brand_key"] not in self.reply["brands"]:
            return False
        to = [str(a).lower() for a in source.get("to") or []]
        if HAMLE in to or not any(a in self.reply["agency"] for a in to):
            return False
        received = source.get("received_at_ms")
        return isinstance(received, int) and self.clock().timestamp() * 1000 - received < grace * 1000

    def _attachments(self, source):
        """Every attachment of the client's mail, fetched and turned into model input (v11_attachments)."""
        from v11_attachments import read_files
        files = []
        for a in source.get("attachments") or []:
            meta = {"filename": a.get("filename"), "content_type": a.get("mime_type")}
            if self.fetch_attachment is None or not a.get("attachment_id"):
                files.append((dict(meta, reason="ek mailin içinden alınamadı"), None))
                continue
            if isinstance(a.get("size_bytes"), int) and a["size_bytes"] > 20 * 1024 * 1024:
                files.append((dict(meta, reason=f"dosya çok büyük ({a['size_bytes'] / 1048576:.1f} MB)"), None))
                continue
            try:
                files.append((meta, self.fetch_attachment(source["message_id"], a["attachment_id"])))
            except Exception as exc:
                files.append((dict(meta, reason="indirilemedi (" + _code(exc) + ")"), None))
        out = read_files(files)
        return dict(source, attachment_status=out["status"]), out["blocks"]

    def _plan(self, draft, source, sender):
        """None = silent draft as before; else the reply plan and whether it may go out by itself."""
        if self.reply is None or sender["brand_key"] not in self.reply["brands"]:
            return None
        if not isinstance(source.get("received_at_ms"), int) or source["received_at_ms"] < self.reply["since_ms"][sender["brand_key"]]:
            return None
        import v11_client_reply as cr
        from v11_chain_mail import reply_subject
        to, cc, unknown = cr.recipients(source, sender, self.book, self.reply, sensitive=bool(draft.get("sensitive")))
        local = self.clock().astimezone(ISTANBUL)
        done = self._get(reply_key(source["thread_id"])) or {}
        replied = done.get("count")
        last = self._get(done["last_message_key"]) if done.get("last_message_key") else None
        uncertain = isinstance(last, dict) and last.get("reply_state") in ("send_unknown", "sending")
        sent_today = int((self._get(day_key(local.date())) or {}).get("sent_count") or 0)
        from v11_client_job import job_thread_key
        own = self._get(job_thread_key(source["thread_id"])) is not None  # Hamle answered it and opened the job
        reasons = cr.check(draft, source, sender, self.reply, thread_replied=int(replied or 0), sent_today=sent_today,
                           unknown=unknown, not_direct=cr.not_direct(to, cc, self.book, self.reply), own_thread=own,
                           last_uncertain=uncertain)
        outcome = "manual" if unknown else ("awaiting_approval" if reasons else "send")
        return {"outcome": outcome, "reasons": reasons, "to": to, "cc": cc,
                "subject": reply_subject(source.get("subject")), "body": cr.body_for(draft),
                "anchor": source["message_id"], "thread_id": source["thread_id"], "own_thread": own}

    def _owner(self, sender):
        """Brand owner on the note; a test sender's mail goes to the first approver (Ahmet), never the team."""
        return self.reply["first_approver"] if sender.get("test") else self.reply["owner_ids"][sender["brand_key"]]

    def _team(self, sender):
        """After Hamle answered: the brand's social media lead and owner open the job (a test goes to Ahmet)."""
        if sender.get("test"):
            return [self.reply["first_approver"]]
        from v11_client_reply import agency_people
        members = {p["basecamp_id"] for p in agency_people(self.catalog).values() if p["basecamp_id"]}
        sm = self.book.brands[sender["brand_key"]].get("sm_person_id")
        return sorted({self.reply["owner_ids"][sender["brand_key"]]} | ({sm} if sm in members else set()))

    def _send(self, mkey, token, plan, report, approved_by=None, edited=False):
        """One same-thread reply: 'sent' | 'send_unknown' (never retried) | 'blocked:<code>' | 'released:<code>'."""
        payload = {"from": HAMLE, "to": list(plan["to"]), "cc": list(plan["cc"]), "thread_id": plan["thread_id"],
                   "in_reply_to_message_id": plan["anchor"], "subject": plan["subject"], "body": plan["body"]}
        sha = scope_digest(payload)
        local = self.clock().astimezone(ISTANBUL)
        rkey, dkey = reply_key(plan["thread_id"]), day_key(local.date())
        try:  # read-only: the anchor is still the newest incoming mail, recipients and subject unchanged
            self.reply_writer.preflight("client_reply", copy.deepcopy(payload))
        except Exception as exc:
            return "blocked:" + _code(exc)
        from v11_client_reply import OWN_THREAD_MAX_REPLIES

        def begin(tx, now):
            self._fence(tx, now)
            row, done, day = tx.get(mkey), tx.get(rkey), tx.get(dkey) or {}
            _need(row is not None and row.get("token") == token, "client_claim_lost")
            if row.get("reply_state") not in (None, "awaiting_approval"):
                raise ClientWatchError("blocked:client_reply_state_changed")
            count = int((done or {}).get("count") or 0)
            keeps_talking = plan.get("own_thread") is True or self.reply["full_control"] is True
            if approved_by is None and count and not (keeps_talking and count < OWN_THREAD_MAX_REPLIES):
                raise ClientWatchError("blocked:thread_already_replied")
            if int(day.get("sent_count") or 0) >= self.reply["max_sends_per_day"]:
                raise ClientWatchError("blocked:daily_send_limit")
            row.update(reply_state="sending", reply_payload_sha256=sha, sending_at=now, reply_to=payload["to"],
                       reply_cc=payload["cc"], reply_subject=payload["subject"], reply_body=payload["body"],
                       anchor=plan["anchor"], reply_reasons=list(plan.get("reasons") or []),
                       approved_by=approved_by, edited=edited)
            tx.put(mkey, row)
            tx.put(rkey, {"key": rkey, "kind": "client_reply_thread", "thread_id": plan["thread_id"],
                          "count": int((done or {}).get("count") or 0) + 1, "last_message_key": mkey,
                          "updated_at": now})
            keys = list(day.get("message_keys") or [])
            tx.put(dkey, dict(day, kind="client_day", day=local.date().isoformat(),
                              sent_count=int(day.get("sent_count") or 0) + 1,
                              message_keys=list(dict.fromkeys(keys + [mkey]))[-200:]))
        try:
            self.ops._run(begin)
        except ClientWatchError as exc:
            return exc.code if exc.code.startswith("blocked:") else "released:" + exc.code
        except Exception as exc:
            return "released:" + _code(exc)
        try:  # exactly one POST; anything uncertain from here on is never retried
            report["business_writes"] += 1
            remote = self.reply_writer.write(copy.deepcopy(payload))["remote_id"]
            back = self.reply_writer.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == sha, "client_reply_readback_mismatch")
        except Exception as exc:
            self._update(mkey, token, reply_state="send_unknown", reply_reason=_code(exc))
            return "send_unknown"
        self._update(mkey, token, reply_state="sent", reply_remote_id=remote, sent_at=self.clock().timestamp())
        report["sent"] += 1
        return "sent"

    def _set_reply(self, mkey, state, **fields):
        def op(tx, now):
            row = tx.get(mkey)
            _need(row is not None and row.get("reply_state") == "awaiting_approval", "client_reply_state_changed")
            row.update(reply_state=state, updated_at=now, **fields)
            tx.put(mkey, row)
        self.ops._run(op, allow_expired=True)

    def _seen(self, mkey, comment_id):
        def op(tx, now):
            row = tx.get(mkey)
            row.update(approval_seen=comment_id, updated_at=now)
            tx.put(mkey, row)
        self.ops._run(op, allow_expired=True)

    def _ack(self, mkey, todo_id, text, report):
        """One internal comment on the note answering an approver; one attempt, never retried."""
        payload = {"target": dict(self.target, task_id=todo_id), "body": text, "mention_ids": [],
                   "internal_only": True}

        def begin(tx, now):
            self._fence(tx, now)
            row = tx.get(mkey)
            _need(row is not None and int(row.get("ack_count") or 0) < 5, "client_ack_limit")
            row["ack_count"] = int(row.get("ack_count") or 0) + 1
            tx.put(mkey, row)
        try:
            self.ops._run(begin)
        except Exception:
            return "ack_denied"
        try:
            report["business_writes"] += 1
            remote = self.comment_writer.write(copy.deepcopy(payload))["remote_id"]
            self.comment_writer.readback(remote, copy.deepcopy(payload))
        except Exception:
            return "ack_unknown"
        return "ack_written"

    def _approvals(self, report, started):
        """Drafts waiting on an approver. 'Hamle gönder' (optionally with the corrected text under it)
        sends, 'Hamle gönderme' drops. Only the newest waiting mail of a note can be approved."""
        import v11_client_reply as cr
        from v11_basecamp_reader import BasecampReader
        from v11_brand_task import HAMLE_PERSON
        keys = list((self._get(APPROVALS_KEY) or {}).get("keys") or [])
        out = {"checked": 0, "sent": 0, "rejected": 0, "expired": 0, "stale": 0}
        waiting = {}
        for key in keys:
            row = self._get(key)
            if (isinstance(row, dict) and row.get("reply_state") == "awaiting_approval"
                    and row.get("state") == "noted" and type(row.get("todo_id")) is int):
                waiting[key] = row
        newest = {}
        for key, row in waiting.items():
            other = newest.get(row["todo_id"])
            if other is None or (row.get("received_at_ms") or 0) > (waiting[other].get("received_at_ms") or 0):
                newest[row["todo_id"]] = key
        now = self.clock()
        for key, row in sorted(waiting.items(), key=lambda kv: kv[1].get("received_at_ms") or 0):
            if newest[row["todo_id"]] != key:
                self._set_reply(key, "approval_stale")
                out["stale"] += 1
                continue
            if now.timestamp() - float(row.get("noted_at") or 0) > cr.APPROVAL_WINDOW_SECONDS:
                self._set_reply(key, "approval_expired")
                out["expired"] += 1
                continue
            if self.monotonic() - started > self.cfg["run_seconds"]:
                break
            out["checked"] += 1
            work = {"work_id": key, "task_id": row["todo_id"],
                    **{k: self.target[k] for k in ("account_id", "project_id", "list_id")}}
            observation = BasecampReader(self.basecamp_get).poll(work, previous=None, observed_at=now)
            found = None
            for c in sorted(observation["comments"], key=lambda c: int(c["id"])):
                author = (c.get("creator") or {}).get("id")
                created = _iso_seconds(c.get("created_at"))
                if (author == HAMLE_PERSON or author not in self.reply["approver_ids"] or created is None
                        or created <= float(row["noted_at"]) or int(c["id"]) <= int(row.get("approval_seen") or 0)):
                    continue
                verb = cr.command(c.get("content"))
                if verb is not None:
                    found = (author, verb, int(c["id"]))
            if found is None:
                continue
            author, (verb, corrected), cid = found
            name = self.reply["approver_ids"][author]
            if verb == "reject":
                self._set_reply(key, "rejected", decided_by=name, approval_seen=cid)
                out["rejected"] += 1
                self._ack(key, row["todo_id"], "Tamam, bu maile cevap göndermiyorum; ekip cevaplayacak.", report)
                continue
            usable = row.get("model_status") == "ok" or bool(row.get("sensitive"))
            body = corrected or (row.get("reply_body") if usable else None)
            if not body:
                self._seen(key, cid)
                self._ack(key, row["todo_id"], "Bu mail için hazır bir taslağım yok. Göndermem için cevabın tam metnini "
                          "\"Hamle gönder\" satırının altına yazman gerekiyor.", report)
                continue
            plan = {"to": row["reply_to"], "cc": row["reply_cc"], "subject": row["reply_subject"], "body": body,
                    "anchor": row["anchor"], "thread_id": row["thread_id"], "reasons": row.get("reply_reasons")}
            sent = self._send(key, row["token"], plan, report, approved_by=name, edited=corrected is not None)
            if sent == "sent":
                out["sent"] += 1
                text = f"Gönderildi ({name} onayıyla" + (", düzeltilmiş metinle)." if corrected else ").")
            elif sent == "send_unknown":
                text = "Gönderdim ama teyit edemedim; tekrar denemiyorum. Gmail'de Gönderilmiş kutusuna bakın."
            elif sent == "blocked:newer_incoming_mail_in_thread":
                self._set_reply(key, "approval_stale", approval_seen=cid)
                out["stale"] += 1
                text = ("Göndermedim: zincirde daha yeni bir mail var (müşteri yeniden yazdı ya da ekipten biri "
                        "cevap verdi). Bu maili ekip cevaplasın.")
            elif sent.startswith("blocked:"):
                self._seen(key, cid)
                text = ("Göndermedim, son kontrol geçmedi (" + sent.split(":", 1)[1]
                        + "). Ekip cevaplasın ya da daha sonra yeniden \"Hamle gönder\" yaz.")
            else:
                continue  # stop / budget hold: nothing sent, the command is read again next time
            self._ack(key, row["todo_id"], text, report)

        def keep(tx, now):
            rows = {k: tx.get(k) for k in list((tx.get(APPROVALS_KEY) or {}).get("keys") or [])}
            tx.put(APPROVALS_KEY, {"kind": "client_approvals", "keys": [
                k for k, r in rows.items() if isinstance(r, dict) and r.get("reply_state") == "awaiting_approval"]})
        self.ops._run(keep, allow_expired=True)
        return out

    @staticmethod
    def _fields(draft):
        return {k: draft.get(k) for k in ("category", "summary", "reply", "missing_info", "team_next_step",
                                         "confidence", "sensitive", "internal_terms", "model_status", "job_ready",
                                         "jobs", "rubric")}

    # ----- evening summary to Ahmet
    def _summary(self):
        local = self.clock().astimezone(ISTANBUL)
        h, m = (int(v) for v in self.cfg["summary_after"].split(":"))
        if local.weekday() >= 5 or (local.hour, local.minute) < (h, m):
            return None
        skey, dkey = summary_key(local.date()), day_key(local.date())
        token = uuid.uuid4().hex

        def claim(tx, now):
            self._fence(tx, now)
            row, day = tx.get(skey), tx.get(dkey)
            if row is not None and not (row.get("state") == "claimed" and now >= row["lease_until"]):
                return None
            rows = [tx.get(k) for k in (day or {}).get("message_keys", [])]
            tx.put(skey, {"key": skey, "kind": "client_summary", "state": "claimed", "token": token,
                          "claimed_at": now, "lease_until": now + LEASE_SECONDS, "day": local.date().isoformat()})
            return [r for r in rows if isinstance(r, dict)]
        rows = self.ops._run(claim)
        if rows is None:
            return {"status": "already_recorded"}
        payload = summary_payload(rows, self.book, local.date(), self.cfg["summary_to"], self.target, self.link_base,
                                  replying=self.reply is not None)
        sha = scope_digest(payload)
        self._update(skey, token, state="sending", payload_sha256=sha, mails=len(rows))
        try:
            remote = self.mail_writer.write(payload)["remote_id"]
            back = self.mail_writer.readback(remote, payload)
            _need(back.get("readback_sha256") == sha, "client_summary_readback_mismatch")
        except Exception as exc:
            self._update(skey, token, state="unknown", reason=_code(exc))
            return {"status": "held", "reason": "client_summary_outcome_unknown_no_retry"}
        self._update(skey, token, state="sent", remote_id=remote)
        return {"status": "summary_sent", "mails": len(rows)}


MONTHS = ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık")
DAYS = ("Pzt", "Salı", "Çarş", "Perş", "Cuma", "Cmt", "Paz")
SUMMARY_FOOTER = ("Hamle AI Müşteri Temsilcisi · Sessiz deneme: müşteriye hiçbir mail gönderilmedi. "
                  "Taslaklar Basecamp'te 'Hamle İşleri' listesinde, yalnız ekip görür.")


REPLY_FOOTER = ("Hamle AI Müşteri Temsilcisi · Gönderilen yanıtlar aynı mail zincirinde. Onay bekleyenler Basecamp'te "
                "'Hamle İşleri' listesinde: notuna \"Hamle gönder\" yazılırsa gider.")


def reply_label(r):
    state = r.get("reply_state")
    if state == "sent":
        return "Müşteriye gönderildi" + (f" ({r['approved_by']} onayıyla" + (", düzeltilerek)" if r.get("edited") else ")")
                                         if r.get("approved_by") else "")
    return {"awaiting_approval": "Onay bekliyor: " + "; ".join(r.get("reply_reasons") or []),
            "send_unknown": "Gönderim belirsiz (tekrar denenmedi)", "sending": "Gönderim belirsiz (tekrar denenmedi)",
            "stale": "Gönderilmedi: zincirde daha yeni mail var", "approval_stale": "Gönderilmedi: zincirde daha yeni mail var",
            "approval_expired": "Onay gelmedi, gönderilmedi",
            "rejected": f"Gönderilmemesi istendi ({r.get('decided_by') or '-'})",
            "manual": "Gönderilmedi: " + "; ".join(r.get("reply_reasons") or [])}.get(state)


def summary_payload(rows, book, day, to, target, link_base, replying=False):
    title = (f"Hamle AI: {day.day} {MONTHS[day.month - 1]} {DAYS[day.weekday()]} · "
             + ("Müşteri mailleri günlük özeti" if replying else "Müşteri mailleri, böyle yanıtlardım"))
    rows = sorted(rows, key=lambda r: r.get("received_at_ms") or 0)
    text = [title, ""]
    blocks = []
    sent = sum(r.get("reply_state") == "sent" for r in rows)
    shadows = sum(bool(r.get("shadow")) for r in rows)
    real = len(rows) - shadows
    extra = (f" Ayrıca iletilen {shadows} eski mail için gölge taslak yazdı; bunlar kimseye gitmez, ekibin cevabıyla "
             "karşılaştırmak için." if shadows else "")
    if not rows:
        text.append("Bugün Hamle'nin gördüğü müşteri maili yok.")
    elif replying:
        text.append(f"Bugün {real} müşteri maili geldi. Hamle {sent} tanesine yanıt gönderdi; durumlar aşağıda." + extra)
    else:
        text.append(f"Bugün {real} müşteri maili geldi. Hamle hiçbirine yanıt göndermedi; göndereceği taslaklar "
                    "aşağıda." + extra)
    for i, r in enumerate(rows, 1):
        brand = (book.brands.get(r.get("brand_key")) or {}).get("name") or r.get("brand_key") or "Gölge"
        who = " ".join(x for x in (r.get("sender_name"), f"({r['sender_title']})" if r.get("sender_title") else None) if x) or "-"
        link = (f"{link_base}/{target['account_id']}/buckets/{target['project_id']}/todos/{r['todo_id']}"
                if r.get("todo_id") else None)
        state = {"noted": None, "unknown": "Basecamp notu belirsiz (tekrar denenmedi)",
                 "held": ("İletilen mailde tanıdığım bir müşteri adresi bulamadım"
                          if r.get("reason") == "shadow_client_not_found" else "Not yazılmadı: " + str(r.get("reason"))),
                 "claimed": "işleniyor",
                 "writing": "not yazılıyordu"}.get(r.get("state"), str(r.get("state")))
        state = "; ".join(x for x in (reply_label(r), state) if x) or None
        tag = ((" · GÖLGE" if r.get("shadow") else "") + (" · TEST" if r.get("test") else "")
               + (" · HASSAS" if r.get("sensitive") else "")
               + (" · İÇ DİL" if r.get("internal_terms") else ""))
        lines = [f"{i}. {brand} · {who} · {_when(r.get('received_at_ms'))}{tag}",
                 "Konu: " + (r.get("subject") or "(konu yok)")]
        if r.get("summary"):
            lines.append("Özet: " + _line(r["summary"], 500))
        lines.append(("Gönderilen yanıt: " if r.get("reply_state") == "sent" else "Hamle'nin taslağı: ")
                     + (r.get("reply_body") or r.get("reply") or "(yok)"))
        team = r.get("team_reply") if r.get("shadow") else None
        if team:
            lines.append("Ekibin cevabı (" + str(team.get("name") or team.get("email")) + "): " + str(team.get("text")))
        if state:
            lines.append("Durum: " + state)
        for j in r.get("jobs") or []:
            lines.append("İş: " + str(j.get("title")) + (" (" + j["assignee"] + ")" if j.get("assignee") else "")
                         + (" · " + j["url"] if j.get("url") else ""))
        if link:
            lines.append("Basecamp: " + link)
        text += [""] + lines
        blocks.append((brand, who, tag, r, state, link))
    text += ["", REPLY_FOOTER if replying else SUMMARY_FOOTER]
    body = "\n".join(text)

    def esc(v):
        return html.escape(str(v))
    parts = [f'<h2 style="font-size:18px;margin:0 0 12px;">{esc(title)}</h2>',
             f'<p style="margin:0 0 12px;">{esc(text[2])}</p>']
    for brand, who, tag, r, state, link in blocks:
        head = esc(brand) + " · " + esc(who) + " · " + esc(_when(r.get("received_at_ms"))) + esc(tag)
        subject = esc(r.get("subject") or "(konu yok)")
        if link:
            subject = (f'<a href="{esc(link)}" style="color:#1a56db;text-decoration:none;font-weight:bold;">'
                       f"{subject}</a>")
        item = [f'<div style="margin:0 0 4px;font-weight:bold;">{head}</div>',
                f'<div style="margin:0 0 4px;">{subject}</div>']
        if r.get("summary"):
            item.append(f'<div style="margin:0 0 4px;color:#4b5563;">{esc(_line(r["summary"], 500))}</div>')
        reply = esc(r.get("reply_body") or r.get("reply") or "(yok)").replace("\n", "<br>")
        item.append('<div style="margin:6px 0;padding:8px 10px;background:#f3f4f6;border-left:3px solid #1a56db;">'
                    f"{reply}</div>")
        team = r.get("team_reply") if r.get("shadow") else None
        if team:
            item.append('<div style="margin:6px 0;padding:8px 10px;background:#fafafa;border-left:3px solid #9ca3af;">'
                        f'<b>Ekibin cevabı ({esc(team.get("name") or team.get("email"))}):</b><br>'
                        + esc(team.get("text") or "").replace("\n", "<br>") + "</div>")
        if state:
            item.append(f'<div style="margin:0 0 4px;color:#b45309;">{esc(state)}</div>')
        for j in r.get("jobs") or []:
            name = esc(j.get("title")) + (" (" + esc(j["assignee"]) + ")" if j.get("assignee") else "")
            if j.get("url"):
                name = f'<a href="{esc(j["url"])}" style="color:#1a56db;text-decoration:none;">{name}</a>'
            item.append(f'<div style="margin:0 0 4px;">İş: {name}</div>')
        parts.append('<div style="margin:0 0 16px;padding:0 0 12px;border-bottom:1px solid #e5e7eb;">'
                     + "".join(item) + "</div>")
    parts.append(f'<p style="margin:16px 0 0;color:#6b7280;font-size:12px;">'
                 f'{esc(REPLY_FOOTER if replying else SUMMARY_FOOTER)}</p>')
    page = ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:0;">'
            '<div style="font-family:Tahoma,Verdana,Segoe,sans-serif;font-size:14px;line-height:1.55;'
            'color:#1f2937;width:100%;">' + "".join(parts) + "</div></body></html>")
    return {"from": HAMLE, "to": list(to), "cc": [], "subject": title, "body": body, "html": page}


# ------------------------------------------------------------------ live wiring
def load_contacts(ops):
    doc = ops._run(lambda tx, _: tx.get(CONTACTS_KEY), allow_expired=True)
    _need(isinstance(doc, dict) and doc.get("kind") == "client_contacts", "client_contacts_missing")
    return doc


def client_mail_reader(gmail_store, book, cfg, **factories):
    """ChainMailReader with the client table instead of agency requesters: same verified Gmail
    read path (Hamle in actual To/CC, latest incoming message, not automated, plain body)."""
    from v11_chain_mail import ChainMailReader

    class ClientMailReader(ChainMailReader):
        shadow = None  # client senders: no 'Gölge:' filter
        keep_attachments = True  # Ahmet 14 Oct: the attachments are read with the mail

        def _requesters(self):
            return book.query_terms()

        def _thread(self, tid, meta, requesters, *rest):
            return super()._thread(tid, meta, book, *rest)  # exact address or company domain
    return ClientMailReader(gmail_store, {"contacts": []}, max_threads=cfg["max_threads"],
                            newer_than_days=cfg["newer_than_days"], **factories)


def mail_attachment_fetcher(gmail_store, *, _service_factory=None, _credentials_factory=None, _request_factory=None):
    """fetch(message_id, attachment_id) -> bytes over the verified read-only hamle@ Gmail path."""
    import cloud_providers as provider
    from v11_chain_mail import HEX

    def fetch(message_id, attachment_id):
        _need(isinstance(message_id, str) and HEX.fullmatch(message_id) and isinstance(attachment_id, str)
              and re.fullmatch(r"[A-Za-z0-9_-]{1,4096}", attachment_id), "attachment_id_invalid")
        helper, reader, _, _ = provider._modules()
        service_factory, request_factory = _service_factory or provider._gmail_service, _request_factory
        auth_session = None
        with reader._quiet_call():
            try:
                if request_factory is None:
                    auth_session = provider._session()
                    request_factory = lambda: provider._GoogleRequest(auth_session, gmail=True)
                with helper.verified_gmail(gmail_store, service_factory, _credentials_factory,
                                           request_factory) as (service, _):
                    row = service.users().messages().attachments().get(
                        userId="me", messageId=message_id, id=attachment_id).execute(num_retries=0)
            finally:
                if auth_session is not None:
                    auth_session.close()
        data = row.get("data") if isinstance(row, dict) else None
        _need(isinstance(data, str) and re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", data), "attachment_data_invalid")
        import base64
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return fetch


def shadow_mail_reader(gmail_store, cfg, **factories):
    """'Gölge:' forwards from the people in cfg['shadow_from'] (same verified Gmail read path)."""
    from v11_chain_mail import ChainMailReader

    class ShadowMailReader(ChainMailReader):
        shadow = True
        max_body_bytes = SHADOW_MAX_BODY_BYTES  # forwards carry the quoted history

        def _requesters(self):
            return set(cfg["shadow_from"])

        def _thread(self, *args):
            source = super()._thread(*args)
            return None if source is None else dict(source, shadow=True)
    return ShadowMailReader(gmail_store, {"contacts": []}, max_threads=SHADOW_MAX_THREADS,
                            newer_than_days=cfg["newer_than_days"], **factories)


def run_worker_client_watch(ops, secrets, *, clock=None, config=None, started_at=None):
    """Entry used after worker.py --mode chain_tick (existing credential loaders only)."""
    import basecamp_cloud_auth
    import cloud_providers as provider
    from v11_chain import BasecampCommentWriter
    from v11_chain_live import ChainCommentWriter, _memo, load_catalog
    from v11_chain_mail import gmail_token_context
    from v11_daily_digest import DigestMailWriter, load_brand_cards
    clock = clock or (lambda: datetime.now(timezone.utc))
    try:
        cfg = validate_config(config if config is not None else load_config())
        if not cfg["enabled"]:
            return {"status": "no_new_work", "reason": "client_watch_disabled", "business_writes": 0}
        catalog = load_catalog()
        book = validate_contacts(load_contacts(ops), catalog, load_brand_cards())
        token = _memo(lambda: basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh))
        keys = {"anthropic": lambda: provider.anthropic_key(secrets)}
        reply_writer = basecamp_get = bc_session = jobs_factory = mentions_factory = None
        if cfg["mode"] == "reply":
            from v11_basecamp_reader import SessionGet
            from v11_client_reply import client_reply_writer, validate_reply
            settings = validate_reply(cfg["reply"], catalog)
            reply_writer = client_reply_writer(book, settings, token_context=gmail_token_context(secrets),
                                               session_factory=provider._session)
            bc_session = provider._session()
            basecamp_get = SessionGet(bc_session, token)
            if (cfg.get("jobs") or {}).get("enabled") is True:
                import v11_client_job as cj

                def jobs_factory(job_settings):
                    return {"todo_writer": cj.JobTodoWriter(catalog, job_settings, token_provider=token),
                            "comment_writer": BasecampCommentWriter(catalog, token_provider=token),
                            "download": cj.basecamp_downloader(job_settings["account_id"], token_provider=token),
                            "review": cj.review_source(ops, keys, catalog),
                            "delivery": cj.delivery_writer(book, settings, token_context=gmail_token_context(secrets),
                                                           session_factory=provider._session)}
            if (cfg.get("mentions") or {}).get("enabled") is True:
                import v11_basecamp_mentions as bm
                from v11_client_job import basecamp_downloader as cj_download

                def mentions_factory(mention_settings):
                    writer = BasecampCommentWriter(catalog, token_provider=token)
                    writer.allowed = {p: frozenset(ls) for p, ls in mention_settings["allowed"].items()}
                    return {"reader": bm.mention_mail_reader(provider.GmailSecretStore(secrets), mention_settings),
                            "model": bm.mention_source(ops, keys, catalog),
                            "comment_writer": writer,
                            "mail_writer": bm.NewClientMailWriter(book, settings, token_context=gmail_token_context(secrets),
                                                                  session_factory=provider._session),
                            "download": cj_download(mention_settings["account_id"], token_provider=token)}
        watch = ClientWatch(
            ops, config=cfg, book=book, catalog=catalog, clock=clock,
            reader=client_mail_reader(provider.GmailSecretStore(secrets), book, cfg),
            draft=draft_source(ops, keys, catalog),
            task_writer=InternalNoteTaskWriter(catalog, token_provider=token),
            comment_writer=ChainCommentWriter(BasecampCommentWriter(catalog, token_provider=token)),
            mail_writer=DigestMailWriter(token_context=gmail_token_context(secrets), session_factory=provider._session),
            started_at=started_at, reply_writer=reply_writer, basecamp_get=basecamp_get, jobs_factory=jobs_factory,
            mentions_factory=mentions_factory, brands=brand_index(catalog, load_brand_cards()),
            attachments=mail_attachment_fetcher(provider.GmailSecretStore(secrets)),
            shadow_reader=(shadow_mail_reader(provider.GmailSecretStore(secrets), cfg) if cfg.get("shadow_from")
                           else None))
        try:
            return watch.run()
        finally:
            if bc_session is not None:
                bc_session.close()
    except Exception as exc:
        return {"status": "held", "reason": _code(exc), "business_writes": None}


# ------------------------------------------------------------------ Mac operator CLI (contacts)
def import_contacts(ops, doc, *, actor, catalog, cards=None):
    _need(isinstance(actor, str) and actor.endswith("@" + AGENCY_DOMAIN) and EMAIL.fullmatch(actor), "actor_invalid")
    book = validate_contacts(doc, catalog, cards)
    clean = {"kind": "client_contacts", "schema_version": 1, "brands": copy.deepcopy(doc["brands"]),
             "test_contacts": copy.deepcopy(doc.get("test_contacts") or []), "source": _line(doc.get("source"), 200)}
    digest = scope_digest(clean)

    def op(tx, now):
        old = tx.get(CONTACTS_KEY)
        history = list((old or {}).get("history") or [])
        if old is not None and old.get("sha256") == digest:
            return {"status": "already_current", "sha256": digest}
        if old is not None:
            history = (history + [{"sha256": old.get("sha256"), "imported_at": old.get("imported_at"),
                                   "actor": old.get("actor")}])[-20:]
        tx.put(CONTACTS_KEY, dict(clean, sha256=digest, imported_at=now, actor=actor, history=history))
        return {"status": "client_contacts_imported", "sha256": digest}
    result = ops._run(op, allow_expired=True)
    return dict(result, brands=len(book.brands), addresses=len(book.emails), domains=len(book.domains))


def contacts_status(ops, catalog, cards=None):
    try:
        doc = load_contacts(ops)
    except ClientWatchError:
        return {"status": "client_contacts_missing"}
    book = validate_contacts(doc, catalog, cards)
    return {"status": "client_contacts_ready", "sha256": doc.get("sha256"), "actor": doc.get("actor"),
            "brands": {k: {"brand_direct": book.direct.get(k, False),
                           "addresses": sum(v["brand_key"] == k for v in book.emails.values()),
                           "direct": sum(v["brand_key"] == k and v["direct"] for v in book.emails.values()),
                           "domains": sorted(d for d, b in book.domains.items() if b == k)} for k in sorted(book.brands)}}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Client contact table for the silent trial (Firestore only)")
    parser.add_argument("area", choices=("contacts",))
    parser.add_argument("action", choices=("status", "import"))
    parser.add_argument("--file")
    parser.add_argument("--actor")
    args = parser.parse_args(argv)
    try:
        from cloud_store import FirestoreBackend, OperationsStore
        from import_cloud_credentials import credentials
        from v11_chain_live import load_catalog
        from v11_daily_digest import load_brand_cards
        ops = OperationsStore(FirestoreBackend(opt_in="yes", credentials=credentials()))
        catalog, cards = load_catalog(), load_brand_cards()
        if args.action == "status":
            result = contacts_status(ops, catalog, cards)
        else:
            _need(args.file, "file_required")
            doc = json.loads(Path(args.file).read_text(encoding="utf-8"))
            result = import_contacts(ops, doc, actor=args.actor, catalog=catalog, cards=cards)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ClientWatchError as exc:
        print(json.dumps({"status": "held", "reason": exc.code}))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "held", "reason": "contacts_command_failed:" + _code(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
