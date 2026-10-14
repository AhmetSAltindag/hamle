"""Basecamp mentions (Ahmet 2026-10-14): Hamle reads what the team writes to it on Basecamp and acts on it.

"Hamle - Basecamp'de kendisine yazılanı anlasın ve ona göre hareket edebilsin... Basecamp bizim kutsal yerimiz...
özellikle etiketlendiğinde okusun anlasın aksiyona geçmeye hazır olsun!" and "Hamle AI ile maillerimiz ve Basecamp
%100 uyumlu ve senkronize çalışmalı".

Runs inside v11_client_watch (mode 'reply', config block 'mentions'), every tick:
1. Trigger: Basecamp's "@mentioned you" notification mails in hamle@ (From notifications@app.basecamp.com). The mail
   is only a pointer (project / to-do / comment ids). Everything Hamle acts on is read back from the Basecamp API:
   the comment (or the to-do, for a mention in its description) must be written by a verified agency person after
   config 'since', name Hamle, and sit on a client-hidden to-do of a routed brand project or the New Business
   project. The internal 'Hamle İşleri' project stays with the chain (manager questions) and the client notes.
2. One Claude Opus call per mention picks one action and Hamle writes one comment on the same to-do:
   answer (the answer), ask (one clear question), cannot (what Hamle cannot do and who should), client_mail
   (a NEW mail to the brand's client, posted as a draft with To/CC/Konu; only for the reply brands).
3. A draft goes out only after "Hamle gönder" from an approver (Ahmet, Cansu) on that to-do; text written under that
   line replaces the draft, "Hamle gönderme" drops it. The mail starts a new thread from hamle@: To = client table
   addresses of the same brand, CC = the brand's social media lead and account owner plus the person who asked.
4. The sent thread becomes Hamle's own thread for v11_client_watch: the client's reply is answered under the usual
   rules, may open the job, and is written back on the origin to-do.

Same rules as client replies: durable record before every provider write, one attempt, exact readback, anything
uncertain is 'unknown' and never retried; global stop ('dur') fences every write; the day's send limit is shared
with client replies. Hamle never closes to-dos, changes dates or assigns people here.

Mac operator command (Firestore only, no cost): a mail Hamle sent by hand for a mention becomes such a thread
  python3 v11_basecamp_mentions.py adopt --thread <gmail thread id> --project <id> --todo <id> --list <id>
      --brand <brand_key> --actor <agency address>
"""
from __future__ import annotations

import argparse
import base64
import copy
import dataclasses
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

from autonomy_policy import HAMLE, scope_digest

ISTANBUL = ZoneInfo("Europe/Istanbul")
PROMPT = Path(__file__).with_name("basecamp_mention_prompt.txt")
ROLE = "customer_text"
NOTIFY_FROM = "notifications@app.basecamp.com"
LINK = re.compile(r"https://(?:app|3)\.basecamp\.com/([1-9][0-9]{0,11})/buckets/([1-9][0-9]{0,14})/todos/"
                  r"([1-9][0-9]{0,14})(?:#__recording_([1-9][0-9]{0,14}))?")
NAMES_HAMLE = re.compile(r"(?<![0-9a-zçğıöşü])hamle(?![0-9a-zçğıöşü])", re.IGNORECASE)
# A to-do assigned to Hamle (Ahmet 14 Oct: "kendisine açılan Basecamp'i kontrol etmesi çok önemli"): Basecamp's
# to-do notifications carry "Assigned to: Hamle A."; the API check (Hamle among the assignees) decides.
ASSIGNED_TO_HAMLE = re.compile(r"Assigned to:[^\n<]{0,200}?\bHamle\b", re.IGNORECASE)
ACTIONS = ("answer", "ask", "cannot", "client_mail", "follow")
FIELDS = {"enabled", "since", "max_per_run", "newer_than_days"}
DRAFTS_KEY = "bc-mention-drafts"
DRAFT_WINDOW_SECONDS = 3 * 24 * 3600
LEASE_SECONDS = 900
MAX_PER_TODO_PER_DAY = 10
MAX_COMMENTS = 20
MAX_COMMENT_CHARS = 1500
MAX_THREADS = 20
MAX_PARTS = 50
SEND_FIELDS = {"from", "brand_key", "to", "cc", "subject", "body"}
# A failed check before the POST: nothing went out.
NOT_SENT = ("mention_mail_payload_invalid", "mention_mail_recipient_unverified", "gmail_endpoint_forbidden",
            "mail_payload_too_large")
SIGNATURE_END = "Hamle\nGamechangers"


class MentionError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise MentionError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _sha(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _line(value, limit=300):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _seconds(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _when(seconds):
    return datetime.fromtimestamp(seconds, ISTANBUL).strftime("%d.%m.%Y %H:%M") if seconds is not None else "-"


def _first(name):
    return (str(name or "").split() or ["-"])[0]


def mention_key(project_id, todo_id, comment_id):
    return "bc-mention-" + _sha(f"{project_id}|{todo_id}|{comment_id or 0}")


def todo_key(todo_id):
    return f"bc-mention-todo-{int(todo_id)}"


def draft_key(mkey):
    return "bc-mention-draft-" + mkey[len("bc-mention-"):]


def thread_key(thread_id):
    return "bc-mention-thread-" + _sha(thread_id)


# ------------------------------------------------------------------ config
def validate_mentions(block, catalog, reply):
    """Config 'mentions' block -> settings; None when absent or disabled."""
    if block is None:
        return None
    _need(isinstance(block, dict) and set(block) == FIELDS and type(block["enabled"]) is bool, "mentions_config_invalid")
    if not block["enabled"]:
        return None
    _need(reply is not None, "mentions_need_reply_mode")
    try:
        since = datetime.fromisoformat(str(block["since"]).replace("Z", "+00:00"))
    except ValueError:
        raise MentionError("mentions_since_invalid") from None
    _need(since.tzinfo is not None, "mentions_since_invalid")
    for key, low, high in (("max_per_run", 1, 5), ("newer_than_days", 1, 7)):
        _need(type(block[key]) is int and low <= block[key] <= high, "mentions_config_invalid")
    import v11_brand_routing as routing
    from v11_client_job import agency_basecamp
    from v11_nb_tracking import LIST_ID as NB_LIST, NB_PROJECT
    allowed = {p: frozenset(ls) for p, ls in routing.allowlist(catalog).items()}
    internal = catalog["basecamp"]["project_id"]
    _need(internal in allowed, "mentions_routes_unavailable")
    allowed.pop(internal)  # the chain and the client notes answer there
    allowed[NB_PROJECT] = frozenset({NB_LIST})
    projects, routes = {}, {}
    for key, route in (catalog.get("brand_routes") or {}).items():
        if isinstance(route, dict) and route.get("project_id") in allowed:
            projects[route["project_id"]] = key
            routes[key] = {"name": route.get("brand_name") or key, "sm": route.get("sm_person_id"),
                           "rep": route.get("rep_person_id")}
    people = agency_basecamp(catalog)
    return {"since_ms": int(since.timestamp() * 1000), "since": since.timestamp(), "max_per_run": block["max_per_run"],
            "newer_than_days": block["newer_than_days"], "account_id": catalog["basecamp"]["account_id"],
            "internal_project": internal, "allowed": allowed, "projects": projects, "routes": routes,
            "agency": {p["basecamp_id"]: {"email": e, "name": p["name"]} for e, p in people.items()}}


# ------------------------------------------------------------------ notification mails (pointer only)
def _part_texts(payload):
    """{'text/plain': str, 'text/html': str} of a Gmail 'full' payload (first of each)."""
    found, stack, seen = {}, [payload], 0
    while stack and seen < MAX_PARTS:
        part = stack.pop()
        seen += 1
        if not isinstance(part, dict):
            continue
        mime, data = part.get("mimeType"), (part.get("body") or {}).get("data")
        if mime in ("text/plain", "text/html") and mime not in found and isinstance(data, str):
            try:
                found[mime] = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
            except Exception:
                pass
        stack.extend(reversed(part.get("parts") or []))
    return found


def parse_notification(message, account_id):
    """One Gmail 'full' message -> pointer {'message_id','received_at_ms','project_id','todo_id','comment_id',
    'subject'} or None (not a Basecamp mention notification of this account)."""
    if not isinstance(message, dict):
        return None
    payload = message.get("payload") or {}
    heads = {}
    for h in payload.get("headers") or []:
        if isinstance(h, dict) and str(h.get("name") or "").lower() in ("from", "subject"):
            heads.setdefault(str(h["name"]).lower(), []).append(str(h.get("value") or ""))
    froms, subjects = heads.get("from", []), heads.get("subject", [])
    if len(froms) != 1 or parseaddr(froms[0])[1].lower() != NOTIFY_FROM or len(subjects) != 1:
        return None
    try:
        ms = int(message.get("internalDate"))
    except (TypeError, ValueError):
        return None
    texts = _part_texts(payload)
    if "mentioned you" in subjects[0]:
        kind = "mention"
    elif ("assigned you" in subjects[0].lower() or any(ASSIGNED_TO_HAMLE.search(t or "") for t in texts.values())) \
            and "completed a to-do" not in subjects[0]:
        kind = "assigned"
    else:
        return None
    for text in (texts.get("text/plain"), texts.get("text/html")):
        for m in LINK.finditer(text or ""):
            if int(m.group(1)) == account_id:
                return {"message_id": str(message.get("id")), "received_at_ms": ms, "project_id": int(m.group(2)),
                        "todo_id": int(m.group(3)), "kind": kind, "subject": subjects[0][:200],
                        "comment_id": int(m.group(4)) if m.group(4) and kind == "mention" else None}
    return None


def mention_mail_reader(gmail_store, settings, *, _service_factory=None, _credentials_factory=None,
                        _request_factory=None):
    """read() -> mention pointers newer than settings 'since', oldest first (verified hamle@ read path)."""
    import cloud_providers as provider

    def read():
        helper, reader, _, intake = provider._modules()
        service_factory, request_factory = _service_factory or provider._gmail_service, _request_factory
        auth_session, out, seen = None, [], set()
        with reader._quiet_call():
            try:
                if request_factory is None:
                    auth_session = provider._session()
                    request_factory = lambda: provider._GoogleRequest(auth_session, gmail=True)
                query = (f'from:{NOTIFY_FROM} (subject:mentioned OR subject:assigned OR "Assigned to Hamle") '
                         f'-subject:"completed a to-do" newer_than:{int(settings["newer_than_days"])}d')
                with helper.verified_gmail(gmail_store, service_factory, _credentials_factory,
                                           request_factory) as (service, _):
                    found = service.users().threads().list(
                        userId="me", q=query, maxResults=MAX_THREADS, includeSpamTrash=False,
                        fields="threads/id,nextPageToken").execute(num_retries=0)
                    rows = found.get("threads", []) if isinstance(found, dict) else None
                    _need(isinstance(rows, list) and len(rows) <= MAX_THREADS, "mention_search_invalid")
                    for row in rows:
                        try:
                            tid = intake.identifier(row.get("id"))
                            if tid in provider.FORBIDDEN_THREADS:
                                continue
                            thread = service.users().threads().get(
                                userId="me", id=tid, format="full",
                                fields="id,messages(id,internalDate,payload)").execute(num_retries=0)
                        except Exception:
                            continue  # one unreadable thread never stops the others
                        for message in (thread or {}).get("messages") or []:
                            p = parse_notification(message, settings["account_id"])
                            if p and p["received_at_ms"] >= settings["since_ms"] and p["message_id"] not in seen:
                                seen.add(p["message_id"])
                                out.append(p)
            finally:
                if auth_session is not None:
                    auth_session.close()
        return sorted(out, key=lambda p: p["received_at_ms"])
    return read


# ------------------------------------------------------------------ model
def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _str(maximum):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


from v11_attachments import DESCRIBED_SCHEMA  # noqa: E402

MENTION_SCHEMA = _obj({
    "action": {"type": "string", "enum": list(ACTIONS)},
    "understood": _str(300),
    "comment": _str(3000),
    "attachments_read": DESCRIBED_SCHEMA,
    # follow (Ahmet 14 Oct): a job that came by WhatsApp, written on Basecamp by the team; Hamle follows the to-do
    "job": {"type": ["object", "null"], "properties": {"brief": _str(1500)}, "required": ["brief"],
            "additionalProperties": False},
    "mail": {"type": ["object", "null"], "properties": {
        "to": {"type": "array", "items": _str(120), "maxItems": 3},
        "subject": _str(200),
        "body": _str(4000)}, "required": ["to", "subject", "body"], "additionalProperties": False}})


def mention_source(ops, load_key, catalog, *, routing=None, client=None):
    """decide(request, parts) -> validated MENTION_SCHEMA output; ModelGate once per mention."""
    import model as base
    import model_routing
    from v11_chain_live import budget_gate
    from v11_chain_models import ModelGate, internal_cap_microdollars, key_for, reservation_for, source_digest, validate
    from v11_trend_search import route_fingerprint
    route = (routing if routing is not None else model_routing.load()).route(ROLE)
    _need(route.provider == "anthropic", "mention_route_not_claude")
    route = dataclasses.replace(route, effort="medium", max_tokens=min(route.max_tokens, 8000),
                                timeout_seconds=min(route.timeout_seconds, 180))
    cap = internal_cap_microdollars(catalog)
    hold = reservation_for(route)

    def decide(request, parts, blocks=()):
        digest = source_digest("basecamp_mention", *parts, route_fingerprint(route, PROMPT))
        gate = ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)

        def call():
            try:
                system = PROMPT.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise MentionError("mention_prompt_unavailable") from None
            key = key_for(load_key, "anthropic")
            try:
                if not blocks:
                    return base.anthropic_json(route, system, request, MENTION_SCHEMA, api_key=key, client=client,
                                               validator=lambda v: validate(v, MENTION_SCHEMA) or v)
                # the to-do's files go to the model with it (images, PDFs, document text)
                text = json.dumps(request, ensure_ascii=False, sort_keys=True)
                _need(len(text.encode("utf-8")) <= base.MAX_INPUT_BYTES, "mention_input_too_large")
                kwargs = base.anthropic_request(route, system, text, schema=MENTION_SCHEMA)
                kwargs["messages"] = [{"role": "user", "content": list(blocks) + [{"type": "text", "text": text}]}]
                data = base.anthropic_send(key, route, kwargs, client=client)
                result = base.anthropic_extract(route, data, structured=True)
                try:
                    value = base._loads(result["text"])
                    validate(value, MENTION_SCHEMA)
                except Exception:
                    raise base._anthropic_error("invalid_analysis_schema", data, route) from None
                return {"output": value, "receipt": result["receipt"]}
            finally:
                del key
        return gate.once("basecamp_mention", digest, call, provider="anthropic", hold=hold)
    decide.budget_ok = budget_gate(ops, hold, cap)
    return decide


# ------------------------------------------------------------------ Gmail: a new mail to the client
class NewClientMailWriter:
    """A new thread from hamle@: To = client table addresses of one brand, CC = verified agency people.

    write(payload) -> {'remote_id', 'thread_id'} (one POST); readback(remote_id, payload) -> {'readback_sha256',
    'thread_id'} (raw Sent copy: From, To, Cc, no Bcc, Subject and the plain body exactly).
    """
    def __init__(self, book, reply, *, token_context, session_factory):
        self.book, self.reply = book, reply
        self.token_context, self.session_factory = token_context, session_factory

    def _check(self, payload):
        import business_writers as bw
        _need(isinstance(payload, dict) and set(payload) == SEND_FIELDS and payload["from"] == HAMLE,
              "mention_mail_payload_invalid")
        try:
            bw._text(payload["subject"], 200, header=True)
            bw._text(payload["body"], 16000)
        except Exception:
            raise MentionError("mention_mail_payload_invalid") from None
        to, cc, brand = payload["to"], payload["cc"], payload["brand_key"]
        _need(isinstance(to, list) and isinstance(cc, list) and 1 <= len(to) <= 3 and len(to) + len(cc) <= 10
              and len(set(to + cc)) == len(to + cc) and HAMLE not in to + cc
              and all(isinstance(a, str) and a == a.strip().lower()
                      and (self.book.match(a) or {}).get("brand_key") == brand for a in to)
              and all(isinstance(a, str) and a in self.reply["agency"] for a in cc),
              "mention_mail_recipient_unverified")

    @staticmethod
    def message_id(payload):
        return f"<hamle-mention-{scope_digest(payload)[:40]}@gamechangers.com.tr>"

    def _raw(self, payload):
        from v11_chain_mail import mail_html
        m = EmailMessage(policy=policy.SMTP)
        m["From"] = HAMLE
        m["To"] = ", ".join(payload["to"])
        if payload["cc"]:
            m["Cc"] = ", ".join(payload["cc"])
        m["Subject"] = payload["subject"]
        m["Message-ID"] = self.message_id(payload)
        m.set_content(payload["body"], subtype="plain", charset="utf-8")
        m.add_alternative(mail_html(payload["body"]), subtype="html", charset="utf-8")
        return m.as_bytes()

    def write(self, payload):
        from v11_chain_mail import GMAIL, HEX, _call
        self._check(payload)
        raw = self._raw(payload)
        with self.token_context() as token:
            session = self.session_factory()
            try:
                row = _call(session, "POST", GMAIL + "/messages/send", token,
                            body={"raw": base64.urlsafe_b64encode(raw).decode("ascii")})
            finally:
                session.close()
        _need(isinstance(row.get("id"), str) and HEX.fullmatch(row["id"]) and isinstance(row.get("threadId"), str)
              and HEX.fullmatch(row["threadId"]), "mention_mail_receipt_unconfirmed")
        return {"remote_id": row["id"], "thread_id": row["threadId"]}

    def readback(self, remote_id, payload):
        from v11_chain_mail import GMAIL, HEX, MAX_READ, _call
        self._check(payload)
        _need(isinstance(remote_id, str) and HEX.fullmatch(remote_id), "mention_mail_receipt_invalid")
        with self.token_context() as token:
            session = self.session_factory()
            try:
                row = _call(session, "GET", f"{GMAIL}/messages/{remote_id}?format=raw&fields=id%2CthreadId%2ClabelIds%2Craw",
                            token, limit=MAX_READ)
            finally:
                session.close()
        _need(row.get("id") == remote_id and "SENT" in (row.get("labelIds") or []) and isinstance(row.get("raw"), str)
              and isinstance(row.get("threadId"), str) and HEX.fullmatch(row["threadId"]), "mention_mail_readback_mismatch")
        raw = row["raw"]
        msg = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        to = [a.lower() for _, a in getaddresses([str(msg["To"] or "")])]
        cc = [a.lower() for _, a in getaddresses([str(msg["Cc"] or "")])] if msg["Cc"] else []
        plain = msg.get_body(preferencelist=("plain",))
        _need(getaddresses([str(msg["From"])]) == [("", HAMLE)] and to == payload["to"] and cc == payload["cc"]
              and not msg.get_all("Bcc") and str(msg["Subject"]) == payload["subject"]
              and msg.get_content_type() == "multipart/alternative" and plain is not None
              and plain.get_content().replace("\r\n", "\n").rstrip("\n") == payload["body"].rstrip("\n"),
              "mention_mail_readback_mismatch")
        return {"readback_sha256": scope_digest(payload), "thread_id": row["threadId"]}


# ------------------------------------------------------------------ comment texts
def draft_text(intro, mail, names, approvers, warnings):
    lines = [intro.strip(), "", "✉️ MÜŞTERİYE MAİL TASLAĞI · henüz gönderilmedi",
             "Kime: " + ", ".join(names(a) for a in mail["to"]),
             "CC: " + (", ".join(names(a) for a in mail["cc"]) or "-"),
             "Konu: " + mail["subject"], "", mail["body"], ""]
    lines += ["⚠️ " + w for w in warnings] + ([""] if warnings else [])
    lines.append(f'Göndermem için {approvers} bu to-do\'ya "Hamle gönder" yazsın. Düzeltip göndermek için '
                 '"Hamle gönder" satırının altına mailin tam metnini yazın. Gönderilmesin derseniz "Hamle gönderme" '
                 'yazın. Bir şey değişsin isterseniz beni etiketleyip ne değişeceğini yazmanız yeterli.')
    return "\n".join(lines)[:6000]


def failure_text(first):
    return (f"{first}, mesajını okudum ama şu an işleyemedim (teknik bir sorun). Biraz sonra beni yeniden "
            "etiketleyebilir ya da işi ekipten birine bırakabilirsin.")


# ------------------------------------------------------------------ the desk
class MentionDesk:
    """Mentions and their client-mail drafts for ClientWatch (same ops store, fence and day counters)."""
    def __init__(self, watch, settings, *, reader, model, comment_writer, mail_writer, basecamp_get, brands,
                 download=None):
        self.watch, self.ops, self.s = watch, watch.ops, settings
        self.reader, self.model, self.comments, self.mail = reader, model, comment_writer, mail_writer
        self.get, self.brands = basecamp_get, brands
        self.download = download  # Basecamp file download (v11_client_job.basecamp_downloader); None: files not read
        self.calls = 0

    # ----- store helpers
    def _get(self, key):
        return self.ops._run(lambda tx, _: tx.get(key))

    def _set(self, key, **fields):
        def op(tx, now):
            row = tx.get(key)
            _need(row is not None, "mention_record_missing")
            row.update(updated_at=now, **fields)
            tx.put(key, row)
            return row
        return self.ops._run(op, allow_expired=True)

    def _target(self, project_id, list_id, todo_id):
        allowed = self.s["allowed"].get(project_id) or frozenset()
        return {"account_id": self.s["account_id"], "project_id": project_id,
                "list_id": list_id if list_id in allowed else None, "task_id": todo_id}

    def _comment(self, target, text, report):
        """One internal comment; one attempt, never retried. -> remote id (str) or raises."""
        payload = {"target": dict(target), "body": text[:6000], "mention_ids": [], "internal_only": True}
        report["business_writes"] += 1
        remote = self.comments.write(copy.deepcopy(payload))["remote_id"]
        back = self.comments.readback(remote, copy.deepcopy(payload))
        _need(back.get("readback_sha256") == scope_digest(payload), "mention_comment_readback_mismatch")
        return remote

    def _note(self, key, target, text, report, limit=6):
        """A short status comment tied to a draft or thread record (bounded per record). -> bool written."""
        def begin(tx, now):
            self.watch._fence(tx, now)
            row = tx.get(key)
            _need(row is not None and int(row.get("note_count") or 0) < limit, "mention_note_limit")
            row["note_count"] = int(row.get("note_count") or 0) + 1
            tx.put(key, row)
        try:
            self.ops._run(begin)
        except Exception:
            return False
        try:
            self._comment(target, text, report)
        except Exception:
            return False
        return True

    def _name(self, address):
        for p in self.s["agency"].values():
            if p["email"] == address:
                return p["name"]
        match = self.watch.book.match(address) or {}
        return f"{match['name']} <{address}>" if match.get("name") else address

    def _approvers(self):
        return " ya da ".join(_first(n) for n in self.watch.reply["approver_ids"].values())

    # ----- run
    def run(self, report, started):
        out = {"read": 0, "answered": 0, "followed": 0, "drafts": 0, "sent": 0, "skipped": 0, "held": 0, "waiting": 0}
        self.calls = 0
        try:
            self._drafts(report, started, out)
        except Exception as exc:
            out["drafts_reason"] = _code(exc)
        try:
            pointers = list(self.reader() or [])
        except Exception as exc:
            out["reason"] = "mention_read_failed:" + _code(exc)
            return out
        out["read"] = len(pointers)
        for p in pointers:
            if self.watch.monotonic() - started > self.watch.cfg["run_seconds"]:
                out["waiting"] += 1
                continue
            try:
                outcome = self._one(p, report)
            except Exception as exc:
                outcome = "error:" + _code(exc)
            if outcome in ("answer", "ask", "cannot", "failed"):
                out["answered"] += 1
            elif outcome == "follow":
                out["followed"] += 1
            elif outcome == "client_mail":
                out["drafts"] += 1
            elif outcome == "waiting":
                out["waiting"] += 1
            elif outcome in ("unknown", "held") or outcome.startswith("error:"):
                out["held"] += 1
                out.setdefault("errors", []).append(outcome)
            elif outcome != "already_recorded":
                out["skipped"] += 1
        return out

    # ----- one mention
    def _skip(self, key, token, reason):
        self._set(key, state="skipped", reason=reason, lease_until=0)
        return "skipped:" + reason

    def _one(self, p, report):
        project, todo_id, cid = p["project_id"], p["todo_id"], p.get("comment_id")
        key = mention_key(project, todo_id, cid)
        existing = self._get(key)
        if existing is not None and existing.get("state") != "claimed":
            return "already_recorded"
        token = uuid.uuid4().hex

        def claim(tx, now):
            self.watch._fence(tx, now)
            row = tx.get(key)
            if row is not None and not (row.get("state") == "claimed" and now >= row["lease_until"]):
                return None
            tx.put(key, {"key": key, "kind": "bc_mention", "state": "claimed", "token": token, "claimed_at": now,
                         "lease_until": now + LEASE_SECONDS, "project_id": project, "todo_id": todo_id,
                         "comment_id": cid, "notification_id": p.get("message_id"),
                         "notified_at_ms": p.get("received_at_ms")})
            return True
        if self.ops._run(claim) is None:
            return "already_recorded"
        if project == self.s["internal_project"]:
            return self._skip(key, token, "internal_project_chain_handles")
        if project not in self.s["allowed"]:
            return self._skip(key, token, "project_not_allowed")
        status, todo, _ = self.get(f"https://3.basecampapi.com/{self.s['account_id']}/buckets/{project}/todos/{todo_id}.json", {})
        parent = (todo or {}).get("parent") or {}
        _need(status == 200 and isinstance(todo, dict) and todo.get("type") == "Todo" and todo.get("id") == todo_id
              and (todo.get("bucket") or {}).get("id") == project and parent.get("type") == "Todolist"
              and type(parent.get("id")) is int, "mention_todo_unreadable")
        if todo.get("visible_to_clients") is not False:
            return self._skip(key, token, "todo_visible_to_clients")
        from v11_basecamp_reader import BasecampReader
        work = {"work_id": key, "account_id": self.s["account_id"], "project_id": project, "list_id": parent["id"],
                "task_id": todo_id}
        observation = BasecampReader(self.get).poll(work, previous=None, observed_at=self.watch.clock())
        todo = observation["task"]
        comments = sorted(observation["comments"], key=lambda c: int(c["id"]))
        import v11_client_reply as cr
        if cid is not None:
            source = next((c for c in comments if c["id"] == cid), None)
            if source is None:
                return self._skip(key, token, "mention_comment_missing")
            author, html, created = (source.get("creator") or {}), source.get("content"), source.get("created_at")
        else:  # the mention is in the to-do itself, or the to-do was assigned to Hamle
            author, html, created = (todo.get("creator") or {}), todo.get("description"), todo.get("created_at")
        from v11_brand_task import HAMLE_PERSON
        assigned = cid is None and any(isinstance(a, dict) and a.get("id") == HAMLE_PERSON
                                       for a in todo.get("assignees") or [])
        person = self.s["agency"].get(author.get("id"))
        if person is None:
            return self._skip(key, token, "author_not_agency")
        at = _seconds(created)
        if assigned and p.get("kind") == "assigned":  # an old to-do assigned now: the notification's time counts
            at = (p.get("received_at_ms") or 0) / 1000
        if at is None or at < self.s["since"]:
            return self._skip(key, token, "before_since")
        if todo.get("completed") is True:
            return self._skip(key, token, "todo_completed")
        text = cr.comment_text(html)
        if not NAMES_HAMLE.search(text) and not assigned:
            return self._skip(key, token, "hamle_not_named" if p.get("kind") != "assigned" else "not_assigned_to_hamle")
        if assigned and not NAMES_HAMLE.search(text):
            text = "(Bu to-do Hamle'ye atandı.) " + text
        tkey = todo_key(todo_id)
        if cr.command(html) is not None and (self._get(tkey) or {}).get("draft_keys"):
            return self._skip(key, token, "draft_command")  # the draft check reads it
        followed = self._job_todo(todo_id)
        if followed:
            from v11_client_job import approval
            if approval(html) is not None:
                return self._skip(key, token, "job_approval")  # the client job reads it
        if self.calls >= self.s["max_per_run"] or not getattr(self.model, "budget_ok", lambda: True)():
            self._set(key, lease_until=0)
            return "waiting"
        brand_key = self.s["projects"].get(project)
        brand = self._brand(brand_key)
        draft = self._open_draft(todo_id)
        request = self._request(todo, comments, {"author": person["name"], "at": _when(at), "text": text[:4000],
                                                 "assigned_to_hamle": assigned}, brand, draft)
        # Ahmet 14 Oct, no exceptions: the to-do's files are read too (the client's WhatsApp files live here)
        files, blocks = self._files(todo, comments)
        request["attachments"] = [{k: v for k, v in row.items() if k in ("name", "type", "read", "reason", "pages",
                                                                       "ratio", "width", "height", "note")}
                                  for row in files]
        why = self._follow_problem(brand_key, parent["id"], followed)
        request["job_tracking"] = {"followed": followed, "possible": why is None, "why": why}
        self._fit(request)
        self.calls += 1
        report["model_calls"] += 1
        try:
            result = (self.model(request, [key, scope_digest(request)], blocks) if blocks
                      else self.model(request, [key, scope_digest(request)]))
            status = "ok"
        except Exception as exc:
            result, status = None, "failed:" + _code(exc)
        target = self._target(project, parent["id"], todo_id)
        first = _first(person["name"])
        mail, action, job = None, "failed", None
        described = list((result or {}).get("attachments_read") or []) if isinstance(result, dict) else []
        if isinstance(result, dict) and result["action"] == "follow":
            if why is None:
                job, why = self.watch.jobs.todo_job(
                    brand_key, todo, parent["id"], brief=(result.get("job") or {}).get("brief")
                    or cr.comment_text(todo.get("description")) or str(todo.get("content") or ""),
                    files=[{"name": _line(d.get("name"), 255), "what": _line(d.get("what"), 500)}
                           for d in described if isinstance(d, dict) and d.get("name")],
                    request_text=(cr.comment_text(todo.get("description")) + "\n\n" + text).strip(),
                    followed_by=person["email"], last_seen=max([int(c["id"]) for c in comments] or [0]))
            if job is None:
                result = dict(result, action="answer" if followed else "cannot",
                              comment=f"{first}, bu işi zaten takip ediyorum." if followed
                              else f"{first}, bu işi takibe alamıyorum: {why}.")
        if isinstance(result, dict):
            action = result["action"]
            if action == "client_mail":
                mail = self._mail(result.get("mail"), brand, author["id"])
                if mail is None:
                    action = "cannot"
                    result = dict(result, comment=f"{first}, bu markanın müşterisine benim yazabileceğim bir adres "
                                                  "henüz yok; maili ekipten biri göndersin. Markanın kişileri Hamle'ye "
                                                  "tanıtılınca bunu da ben yaparım.")
        if action == "failed":
            body = failure_text(first)
        elif action == "client_mail":
            from v11_client_watch import internal_terms, sensitive_hits
            warnings = []
            hits = sensitive_hits(mail["subject"], mail["body"])
            if hits:
                warnings.append("Mailde fiyat, sözleşme ya da şikâyet konusu olabilir (" + ", ".join(hits)
                                + "); Ahmet'in görmesi gerekiyor.")
            terms = internal_terms(mail["body"])
            if terms:
                warnings.append("Taslakta müşteriye gitmemesi gereken iç ifade var (" + ", ".join(terms)
                                + "). Düzeltilmiş metni \"Hamle gönder\" satırının altına yazın.")
            if not mail["body"].rstrip().endswith(SIGNATURE_END):
                warnings.append("İmza eksik görünüyor.")
            missed = self._unread(files, described)
            if missed:
                warnings.append("Şu dosyaları okuyamadım, maili göndermeden önce bakın: " + missed + ".")
            body = draft_text(result["comment"], mail, self._name, self._approvers(), warnings)
        elif action == "follow":
            body = result["comment"] + "\n\n" + self._follow_text(job)
        else:
            body = result["comment"]
        if action != "failed" and action != "client_mail" and self._unread(files, described):
            body += "\n\nOkuyamadığım dosyalar: " + self._unread(files, described) + "."
        payload_target = dict(target)
        dkey = draft_key(key)
        local = self.watch.clock().astimezone(ISTANBUL).date().isoformat()

        from v11_client_job import JOBS_KEY

        def begin(tx, now):
            self.watch._fence(tx, now)
            row, t = tx.get(key), tx.get(tkey) or {"key": tkey, "kind": "bc_mention_todo", "todo_id": todo_id}
            old_job, index = (tx.get(job["key"]), tx.get(JOBS_KEY)) if job is not None else (None, None)  # reads first
            _need(row is not None and row.get("token") == token and row["state"] == "claimed", "mention_claim_lost")
            count = int(t.get("count") or 0) if t.get("day") == local else 0
            _need(count < MAX_PER_TODO_PER_DAY, "mention_todo_daily_limit")
            row.update(state="writing", action=action, model_status=status, writing_at=now, author_id=author["id"],
                       understood=(result or {}).get("understood"), brand_key=brand_key, body_sha256=_sha(body))
            tx.put(key, row)
            t.update(day=local, count=count + 1)
            if mail is not None:
                t["draft_keys"] = (list(t.get("draft_keys") or []) + [dkey])[-10:]
                tx.put(dkey, {"key": dkey, "kind": "bc_mention_draft", "state": "posting", "mention_key": key,
                              "target": payload_target, "list_id": parent["id"], "todo_id": todo_id,
                              "brand_key": brand_key, "requester_id": author["id"], **mail, "created_at": now})
            tx.put(tkey, t)
            if job is not None and old_job is None:  # Hamle follows the to-do from the next run on
                tx.put(job["key"], dict(job, created_at=now, updated_at=now, opened_at=now, mention_key=key))
                keys = list((index or {}).get("keys") or [])
                tx.put(JOBS_KEY, {"kind": "client_jobs", "keys": list(dict.fromkeys(keys + [job["key"]]))[-80:]})
        try:
            self.ops._run(begin)
        except Exception as exc:
            code = _code(exc)
            if code == "mention_claim_lost":
                return "already_recorded"
            if code == "mention_todo_daily_limit":
                self._set(key, state="held", reason=code, lease_until=0)
                return "held"
            self._set(key, lease_until=0, reason=code)  # 'dur' or budget hold: nothing written, read again next run
            return "waiting"
        try:
            remote = self._comment(target, body, report)
        except Exception as exc:
            self._set(key, state="unknown", reason=_code(exc))
            if mail is not None:
                self._set(dkey, state="unknown", reason=_code(exc))
            return "unknown"
        self._set(key, state="done", remote_id=remote)
        if mail is not None:
            self._set(dkey, state="awaiting_approval", draft_comment_id=int(remote),
                      drafted_at=self.watch.clock().timestamp())

            def index(tx, now):
                keys = list((tx.get(DRAFTS_KEY) or {}).get("keys") or [])
                tx.put(DRAFTS_KEY, {"kind": "bc_mention_drafts", "keys": list(dict.fromkeys(keys + [dkey]))[-50:]})
            self.ops._run(index, allow_expired=True)
        return action

    def _files(self, todo, comments):
        """(status rows, model blocks) for the files on the to-do and its comments (newest kept)."""
        from v11_attachments import MAX_FILES, read_files
        from v11_client_job import upload_files
        metas = upload_files({"content": todo.get("description")})
        for c in comments:
            metas += upload_files(c)
        metas = metas[-MAX_FILES:]
        if not metas:
            return [], []
        files = []
        for m in metas:
            meta = {"filename": m["filename"], "content_type": m["content_type"]}
            if self.download is None:
                files.append((dict(meta, reason="dosya okuma bu çalışmada kapalı"), None))
                continue
            try:
                files.append((meta, self.download(m["download_url"], m["byte_size"])))
            except Exception as exc:
                files.append((dict(meta, reason="indirilemedi (" + _code(exc) + ")"), None))
        out = read_files(files)
        return out["status"], out["blocks"]

    @staticmethod
    def _unread(files, described):
        from v11_attachments import unread
        names = unread(files, described)
        why = {r["name"]: r.get("reason") for r in files}
        return ", ".join(n + (f" ({why[n]})" if why.get(n) else "") for n in names[:6])

    def _follow_problem(self, brand_key, list_id, followed):
        """None when this to-do can become a followed job (action 'follow'), else why not."""
        if followed:
            return "bu işi zaten takip ediyorum"
        jobs = self.watch.jobs
        if jobs is None:
            return "iş takibi kapalı"
        brand = jobs.s["brands"].get(brand_key)
        if brand is None:
            return "bu markada iş takibini henüz açmadık (şimdilik WAT Mobilite ve Rabam)"
        if list_id not in brand["lists"].values():
            return "işleri yalnız markanın Sosyal Medya ve Projeler listelerinde takip ediyorum"
        return None

    def _follow_text(self, job):
        brand = self.watch.jobs.s["brands"][job["brand_key"]]
        approvers = " ya da ".join(_first(n) for n in brand["approvers"].values())
        files = job.get("client_files") or []
        lines = ["Bu işi takip ediyorum. " + _first(job["assignee_name"]) + ", çalışmayı bu to-do'ya dosya olarak "
                 "yükleyince briefle karşılaştırıp puanlayacağım: 90 ve üstüne \"hazır\" derim, 80-89 arası "
                 + approvers + " onayına sunarım, altında gerekçeli revize isterim. İş WhatsApp'tan geldiği için "
                 "müşteriye gönderimi siz yaparsınız; müşterinin yeni isteklerini buraya yorum olarak yazın, briefe "
                 "eklerim.", "", "Anladığım brief:", job["brief"]]
        if files:
            lines += ["", "Müşterinin dosyaları:"] + ["- " + f["name"] + ": " + f["what"] for f in files]
        return "\n".join(lines)

    @staticmethod
    def _fit(request, limit=14_000):
        """Keep the model input under its byte limit: the oldest comments go first, then texts get shorter."""
        def size():
            return len(json.dumps(request, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        while size() > limit and len(request["comments"]) > 3:
            request["comments"].pop(0)
        for cap in (800, 400, 200):
            if size() <= limit:
                break
            for c in request["comments"]:
                c["text"] = c["text"][:cap]
            request["todo"]["description"] = request["todo"]["description"][:cap * 2]
            request["mention"]["text"] = request["mention"]["text"][:cap * 2]
        return request

    def _job_todo(self, todo_id):
        from v11_client_job import JOBS_KEY, todo_job_key
        if self._get(todo_job_key(todo_id)) is not None:
            return True
        for k in (self._get(JOBS_KEY) or {}).get("keys") or []:
            if (self._get(k) or {}).get("todo_id") == todo_id:
                return True
        return False

    def _open_draft(self, todo_id):
        for k in reversed((self._get(todo_key(todo_id)) or {}).get("draft_keys") or []):
            row = self._get(k) or {}
            if row.get("state") == "awaiting_approval":
                return {k: row.get(k) for k in ("to", "cc", "subject", "body")}
        return None

    def _brand(self, brand_key):
        info = dict(self.brands.get(brand_key) or {})
        book, reply = self.watch.book, self.watch.reply
        contacts = [{"email": e, "name": v.get("name"), "title": v.get("title"), "test": v.get("test")}
                    for e, v in book.emails.items() if v["brand_key"] == brand_key]
        default = next((c["email"] for c in contacts if not c["test"]), None)
        possible = brand_key in reply["brands"] and default is not None
        return {"key": brand_key, "name": info.get("name") or (self.s["routes"].get(brand_key) or {}).get("name"),
                "sector": info.get("sector"), "audience": info.get("audience"),
                "main_platform": info.get("main_platform"), "note": book.notes.get(brand_key),
                "client_mail_possible": possible, "client_contacts": contacts if possible else [],
                "default_recipient": default if possible else None}

    def _request(self, todo, comments, mention, brand, draft):
        local = self.watch.clock().astimezone(ISTANBUL)
        import v11_client_reply as cr
        from v11_brand_task import HAMLE_PERSON
        rows = []
        for c in comments[-MAX_COMMENTS:]:
            who = c.get("creator") or {}
            name = "Hamle" if who.get("id") == HAMLE_PERSON else ((self.s["agency"].get(who.get("id")) or {}).get("name")
                                                                 or who.get("name") or "-")
            rows.append({"id": c["id"], "author": name, "at": _when(_seconds(c.get("created_at"))),
                         "text": cr.comment_text(c.get("content"))[:MAX_COMMENT_CHARS]})
        parent = todo.get("parent") or {}
        return {"today": local.date().isoformat(), "now": local.strftime("%d.%m.%Y %H:%M"), "agency": "Gamechangers",
                "mention": mention,
                "todo": {"title": _line(todo.get("content") or todo.get("title"), 300),
                         "list": _line(parent.get("title"), 120) or None,
                         "description": cr.comment_text(todo.get("description"))[:3000],
                         "assignees": [_line(a.get("name"), 80) for a in todo.get("assignees") or [] if isinstance(a, dict)],
                         "due_on": todo.get("due_on"), "completed": todo.get("completed") is True},
                "comments": rows, "brand": {k: v for k, v in brand.items() if k != "key"}, "open_draft": draft}

    def _mail(self, raw, brand, requester_id):
        """The model's mail -> send payload (To: client table of this brand, CC: SM lead, owner, requester)."""
        if not brand["client_mail_possible"] or not isinstance(raw, dict):
            return None
        allowed = {c["email"] for c in brand["client_contacts"]}
        to = [a.strip().lower() for a in raw.get("to") or [] if isinstance(a, str)]
        to = [a for a in dict.fromkeys(to) if a in allowed] or [brand["default_recipient"]]
        route = self.s["routes"].get(brand["key"]) or {}
        cc = []
        for pid in (route.get("sm"), route.get("rep"), requester_id):
            email = (self.s["agency"].get(pid) or {}).get("email")
            if email and email in self.watch.reply["agency"] and email not in cc and email not in to:
                cc.append(email)
        subject, body = _line(raw.get("subject"), 200), str(raw.get("body") or "").strip()
        if not subject or not body:
            return None
        return {"from": HAMLE, "brand_key": brand["key"], "to": to, "cc": cc, "subject": subject, "body": body}

    # ----- drafts waiting for "Hamle gönder"
    def _drafts(self, report, started, out):
        import v11_client_reply as cr
        from v11_basecamp_reader import BasecampReader
        from v11_brand_task import HAMLE_PERSON
        keys = list((self._get(DRAFTS_KEY) or {}).get("keys") or [])
        rows = {k: self._get(k) for k in keys}
        waiting = {k: r for k, r in rows.items() if isinstance(r, dict) and r.get("state") == "awaiting_approval"}
        newest = {}
        for k, r in waiting.items():  # a later draft on the same to-do replaces the earlier one
            other = newest.get(r["todo_id"])
            if other is None or float(r.get("drafted_at") or 0) > float(waiting[other].get("drafted_at") or 0):
                newest[r["todo_id"]] = k
        now = self.watch.clock()
        approvers = self.watch.reply["approver_ids"]
        for k, row in sorted(waiting.items(), key=lambda kv: float(kv[1].get("drafted_at") or 0)):
            if newest[row["todo_id"]] != k:
                self._set(k, state="superseded")
                continue
            if now.timestamp() - float(row.get("drafted_at") or 0) > DRAFT_WINDOW_SECONDS:
                self._set(k, state="expired")
                continue
            if self.watch.monotonic() - started > self.watch.cfg["run_seconds"]:
                break
            work = {"work_id": k, "account_id": self.s["account_id"], "project_id": row["target"]["project_id"],
                    "list_id": row["list_id"], "task_id": row["todo_id"]}
            observation = BasecampReader(self.get).poll(work, previous=None, observed_at=now)
            found = None
            for c in sorted(observation["comments"], key=lambda c: int(c["id"])):
                who = (c.get("creator") or {}).get("id")
                if (who == HAMLE_PERSON or who not in self.s["agency"]
                        or int(c["id"]) <= max(int(row["draft_comment_id"]), int(row.get("approval_seen") or 0))):
                    continue
                verb = cr.command(c.get("content"))
                if verb is not None:
                    found = (who, verb, int(c["id"]))
            if found is None:
                continue
            who, (verb, corrected), seen = found
            if who not in approvers:
                self._set(k, approval_seen=seen)
                self._note(k, row["target"], f"{_first(self.s['agency'][who]['name'])}, bu maili {self._approvers()} "
                                             "onaylayınca gönderiyorum.", report)
                continue
            name = _first(approvers[who])
            if verb == "reject":
                self._set(k, state="rejected", decided_by=approvers[who], approval_seen=seen)
                self._note(k, row["target"], f"Tamam {name}, bu maili göndermiyorum.", report)
                continue
            from v11_client_watch import internal_terms
            body = corrected or row["body"]
            if corrected is None and internal_terms(body):
                self._set(k, approval_seen=seen)
                self._note(k, row["target"], f"{name}, taslakta müşteriye gitmemesi gereken iç ifade var; "
                                             "düzeltilmiş metni \"Hamle gönder\" satırının altına yazarsan onu gönderirim.",
                           report)
                continue
            outcome = self._send(k, row, body, approvers[who], corrected is not None, seen, report)
            if outcome == "sent":
                out["sent"] += 1

    def _send(self, k, row, body, approver, edited, seen, report):
        from v11_client_watch import day_key
        payload = {"from": HAMLE, "brand_key": row["brand_key"], "to": list(row["to"]), "cc": list(row["cc"]),
                   "subject": row["subject"], "body": body}
        sha = scope_digest(payload)
        local = self.watch.clock().astimezone(ISTANBUL)
        dkey = day_key(local.date())
        name = _first(approver)

        def begin(tx, now):
            self.watch._fence(tx, now)
            cur, day = tx.get(k), tx.get(dkey) or {}
            _need(cur is not None and cur.get("state") == "awaiting_approval", "mention_draft_state_changed")
            if int(day.get("sent_count") or 0) >= self.watch.reply["max_sends_per_day"]:
                raise MentionError("daily_send_limit")
            cur.update(state="sending", sending_at=now, payload_sha256=sha, approved_by=approver, edited=edited,
                       sent_body=body, approval_seen=seen)
            tx.put(k, cur)
            tx.put(dkey, dict(day, kind="client_day", day=local.date().isoformat(),
                              sent_count=int(day.get("sent_count") or 0) + 1))
        try:
            self.ops._run(begin)
        except MentionError as exc:
            if exc.code == "daily_send_limit":
                self._set(k, approval_seen=seen)
                self._note(k, row["target"], f"{name}, bugünkü gönderim sınırı doldu; göndermedim. Yarın yeniden "
                                             "\"Hamle gönder\" yazabilir ya da maili ekipten biri gönderebilir.", report)
            return "waiting"
        except Exception:
            return "waiting"  # stop / budget hold: nothing sent, the command is read again next run
        report["business_writes"] += 1
        try:
            receipt = self.mail.write(copy.deepcopy(payload))
        except Exception as exc:
            if _code(exc) in NOT_SENT:
                self._set(k, state="blocked", reason=_code(exc))
                self._note(k, row["target"], f"{name}, göndermedim: son kontrol geçmedi ({_code(exc)}). Maili ekip "
                                             "göndersin ya da beni etiketleyip yeniden taslak isteyin.", report)
                return "blocked"
            return self._unknown(k, row, exc, report)
        try:
            back = self.mail.readback(receipt["remote_id"], copy.deepcopy(payload))
            _need(back.get("readback_sha256") == sha and back.get("thread_id") == receipt["thread_id"],
                  "mention_mail_readback_mismatch")
        except Exception as exc:
            return self._unknown(k, row, exc, report)
        thread_id = receipt["thread_id"]
        self._set(k, state="sent", sent_at=self.watch.clock().timestamp(), remote_id=receipt["remote_id"],
                  thread_id=thread_id)
        adopt(self.ops, thread_id=thread_id, target=row["target"], list_id=row["list_id"], brand_key=row["brand_key"],
              subject=row["subject"], draft_key=k)
        report["sent"] += 1
        who = ", ".join(self._name(a) for a in payload["to"])
        cc = ", ".join(_first(self._name(a)) for a in payload["cc"])
        self._note(k, row["target"], f"✅ Gönderdim ({name} onayıyla" + (", düzeltilmiş metinle" if edited else "")
                   + f"). Kime: {who}" + (f"; CC: {cc}" if cc else "") + ". Müşteri cevap verince buraya yazacağım.",
                   report)
        return "sent"

    def _unknown(self, k, row, exc, report):
        self._set(k, state="send_unknown", reason=_code(exc))
        self._note(k, row["target"], "Gönderdim ama teyit edemedim; tekrar denemiyorum. hamle@ Gönderilmiş kutusuna "
                                     "bakın, gerekirse maili elle gönderin.", report)
        return "unknown"

    # ----- the client's answer back on the origin to-do
    def client_mail(self, source, sender, row, note_todo_id, report):
        """A client mail in a thread Hamle started from a mention: one comment on the origin to-do."""
        tkey = thread_key(source["thread_id"])
        t = self._get(tkey)
        if not isinstance(t, dict) or source["message_id"] in (t.get("synced") or []):
            return None
        from v11_client_watch import latest_part
        name = sender.get("name") or sender["email"]
        when = _when(source["received_at_ms"] / 1000) if isinstance(source.get("received_at_ms"), int) else "-"
        lines = [f"📩 {name} cevap verdi ({when}).", ""]
        if row.get("summary"):
            lines += ["Özet: " + _line(row["summary"], 400), ""]
        lines += ["Mailin yeni kısmı:", latest_part(source.get("text"))[:1200], ""]
        state = row.get("reply_state")
        note = (f"https://3.basecamp.com/{self.s['account_id']}/buckets/{self.watch.target['project_id']}/todos/"
                f"{note_todo_id}") if note_todo_id else None
        if state == "sent":
            lines += ["Hamle aynı zincirde cevap verdi:", str(row.get("reply_body") or "")[:1500]]
        elif state == "awaiting_approval":
            lines.append("Cevap taslağım onay bekliyor (" + "; ".join(row.get("reply_reasons") or []) + ")."
                         + (" Taslak: " + note if note else ""))
        elif state == "manual":
            lines.append("Bu maili ekip cevaplasın: " + "; ".join(row.get("reply_reasons") or []) + ".")
        elif note:
            lines.append("Hamle'nin notu: " + note)
        for j in row.get("jobs") or []:
            lines.append("İş: " + str(j.get("title")) + (" · " + j["url"] if j.get("url") else "")
                         + (" (" + j["assignee"] + ")" if j.get("assignee") else ""))

        def begin(tx, now):
            self.watch._fence(tx, now)
            cur = tx.get(tkey)
            _need(cur is not None and source["message_id"] not in (cur.get("synced") or []), "mention_thread_synced")
            cur["synced"] = (list(cur.get("synced") or []) + [source["message_id"]])[-30:]
            tx.put(tkey, cur)
        try:
            self.ops._run(begin)
        except Exception:
            return None
        try:
            self._comment(t["target"], "\n".join(lines).strip(), report)
        except Exception:
            return "unknown"
        return "written"


# ------------------------------------------------------------------ thread records
def adopt(ops, *, thread_id, target, list_id, brand_key, subject, draft_key=None, actor=None):
    """Make a mail thread Hamle started for a mention its own thread: v11_client_watch answers the client's reply
    under the usual rules (job_thread_key) and the reply is written back on the origin to-do (thread_key)."""
    from v11_chain_mail import HEX
    from v11_client_job import job_thread_key
    _need(isinstance(thread_id, str) and HEX.fullmatch(thread_id), "mention_thread_invalid")
    tkey, jkey = thread_key(thread_id), job_thread_key(thread_id)

    def op(tx, now):
        job = tx.get(jkey) or {"kind": "client_job_thread", "thread_id": thread_id, "job_keys": []}
        old = tx.get(tkey) or {}  # Firestore: every read before the first write
        job["mention_thread"] = tkey
        tx.put(jkey, job)
        tx.put(tkey, {"key": tkey, "kind": "bc_mention_thread", "thread_id": thread_id, "target": dict(target),
                      "list_id": list_id, "brand_key": brand_key, "subject": _line(subject, 200), "draft_key": draft_key,
                      "actor": actor, "created_at": old.get("created_at") or now, "synced": list(old.get("synced") or [])})
        return {"status": "mention_thread_adopted", "thread_key": tkey}
    return ops._run(op, allow_expired=True)


def adopt_command(ops, catalog, *, thread_id, project_id, todo_id, list_id, brand_key, actor):
    _need(isinstance(actor, str) and actor.endswith("@gamechangers.com.tr"), "actor_invalid")
    import v11_brand_routing as routing
    allowed = routing.allowlist(catalog)
    route = (catalog.get("brand_routes") or {}).get(brand_key) or {}
    _need(route.get("project_id") == project_id and project_id in allowed and list_id in allowed[project_id]
          and type(todo_id) is int and todo_id > 0, "mention_adopt_target_invalid")
    target = {"account_id": catalog["basecamp"]["account_id"], "project_id": project_id, "list_id": list_id,
              "task_id": todo_id}
    return adopt(ops, thread_id=thread_id, target=target, list_id=list_id, brand_key=brand_key,
                 subject="(elle gönderildi)", actor=actor)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Basecamp mention threads (Firestore only)")
    parser.add_argument("action", choices=("adopt",))
    parser.add_argument("--thread", required=True)
    parser.add_argument("--project", type=int, required=True)
    parser.add_argument("--todo", type=int, required=True)
    parser.add_argument("--list", type=int, required=True)
    parser.add_argument("--brand", required=True)
    parser.add_argument("--actor", required=True)
    args = parser.parse_args(argv)
    try:
        from cloud_store import FirestoreBackend, OperationsStore
        from import_cloud_credentials import credentials
        from v11_chain_live import load_catalog
        ops = OperationsStore(FirestoreBackend(opt_in="yes", credentials=credentials()))
        result = adopt_command(ops, load_catalog(), thread_id=args.thread, project_id=args.project, todo_id=args.todo,
                               list_id=args.list, brand_key=args.brand, actor=args.actor)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except MentionError as exc:
        print(json.dumps({"status": "held", "reason": exc.code}))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "held", "reason": "mention_command_failed:" + _code(exc)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
