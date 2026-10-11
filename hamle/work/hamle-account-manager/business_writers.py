"""Exact internal task/reply adapters; durable approval/dispatch ledger is external.

Every POST is a single attempt. A failure is ambiguous and must never cause an
automatic resend. These methods are only for dispatch.dispatch_approved.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import date, datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
from html import escape
from html.parser import HTMLParser
import re

import basecamp_cloud_auth
import cloud_providers as provider
from recipient_policy import RecipientError, validate_reply_recipients

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
BASECAMP_BASE = "https://3.basecampapi.com/5898865/buckets/49184053"
FOLLOWUP_TASK_ID = 10392247471
SUBJECT = "Re: " + provider.INTERNAL_SUBJECT
MESSAGE_ID = r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>"


class WriterError(Exception):
    retry_allowed = False
    def __init__(self, code):
        super().__init__(code); self.code = code


def _require(condition, code):
    if not condition: raise WriterError(code)


def _text(value, limit, *, header=False):
    _require(isinstance(value, str) and 0 < len(value) <= limit and value.strip(), "action_text_invalid")
    _require(not any(ord(c) < 32 and c not in "\n\t" for c in value), "action_text_invalid")
    if header: _require(not any(c in value for c in "\r\n\t"), "action_header_invalid")
    return value


def _reply(payload):
    _require(isinstance(payload, dict) and set(payload) ==
             {"from", "to", "cc", "subject", "body", "thread_id", "in_reply_to", "references"}, "reply_schema_invalid")
    _require(payload["from"] == provider.HAMLE and payload["subject"] == SUBJECT, "reply_scope_invalid")
    try: validate_reply_recipients(payload["to"], payload["cc"])
    except RecipientError: raise WriterError("reply_scope_invalid") from None
    tid = payload["thread_id"]
    _require(isinstance(tid, str) and re.fullmatch(r"[a-f0-9]{1,64}", tid)
             and tid not in provider.FORBIDDEN_THREADS, "reply_thread_invalid")
    _text(payload["body"], 4000); _text(payload["subject"], 300, header=True)
    _text(payload["in_reply_to"], 998, header=True); _text(payload["references"], 4096, header=True)
    _require(re.fullmatch(MESSAGE_ID, payload["in_reply_to"]) is not None
             and all(re.fullmatch(MESSAGE_ID, x) for x in payload["references"].split())
             and payload["in_reply_to"] in payload["references"].split(), "reply_rfc_headers_invalid")
    return payload


def _task(payload):
    _require(isinstance(payload, dict) and set(payload) ==
        {"account_id", "project_id", "todo_parent_id", "todo_parent_type", "title", "description", "assignee_id", "due_at"}, "task_schema_invalid")
    _require(payload["account_id"] == 5898865 and payload["project_id"] == 49184053
             and payload["assignee_id"] == 53358100 and payload["due_at"] is None
             and type(payload["todo_parent_id"]) is int and 0 < payload["todo_parent_id"] < 10**30
             and payload["todo_parent_type"] in ("Todoset", "Todolist"), "task_scope_invalid")
    _text(payload["title"], 200, header=True); _text(payload["description"], 4000)
    return payload


def _work(work):
    _require(isinstance(work, dict) and work.get("account_id") == 5898865
             and work.get("project_id") == 49184053 and work.get("task_id") == FOLLOWUP_TASK_ID
             and type(work.get("todo_parent_id")) is int and 0 < work["todo_parent_id"] < 10**30
             and work.get("todo_parent_type") in ("Todoset", "Todolist"), "work_scope_invalid")
    return work


def _comment(payload):
    _require(isinstance(payload, dict) and set(payload) == {"account_id", "project_id", "task_id",
             "todo_parent_id", "todo_parent_type", "body", "task_fingerprint"}, "comment_schema_invalid")
    _work(payload); _text(payload["body"], 4000)
    _require(isinstance(payload["task_fingerprint"], str) and
             re.fullmatch(r"[a-f0-9]{64}", payload["task_fingerprint"]), "task_fingerprint_invalid")
    return payload


def _message_id(payload):
    digest = hashlib.sha256(provider._encode(_reply(payload))).hexdigest()
    return f"<hamle-{digest}@gamechangers.com.tr>"


def _rfc_message_id_matches(actual, payload):
    # Gmail can replace the submitted RFC header on its accepted Sent copy.
    # The caller still verifies the exact recorded REST receipt ID and thread,
    # account, envelope, reply linkage and complete body independently.
    return (actual == _message_id(payload) or
            (isinstance(actual, str) and len(actual) <= 255
             and re.fullmatch(MESSAGE_ID, actual) is not None
             and actual.endswith("@mail.gmail.com>")))


def _mime(payload):
    payload = _reply(payload)
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = provider.HAMLE
    message["To"] = provider.AHMET
    if payload["cc"]:
        message["Cc"] = ", ".join(payload["cc"])
    message["Subject"] = payload["subject"]
    message["In-Reply-To"] = payload["in_reply_to"]
    message["References"] = payload["references"]
    message["Message-ID"] = _message_id(payload)
    message.set_content(payload["body"], subtype="plain", charset="utf-8")
    return message.as_bytes()


def _description(text):
    return "<div>" + escape(text, quote=True).replace("\n", "<br>") + "</div>"


class _PlainDescription(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts = []; self.invalid = False
    def handle_starttag(self, tag, attrs):
        # Basecamp adds this exact bidirectional-text wrapper on saved content.
        # All other attributes (links, events, styles, etc.) remain rejected.
        safe_attrs = not attrs or (tag == "div" and attrs == [("dir", "auto")])
        if not safe_attrs or tag not in ("div", "p", "br", "span", "strong", "b", "em", "i"):
            self.invalid = True
        if tag == "br": self.parts.append("\n")
        if tag in ("div", "p") and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")
    def handle_endtag(self, tag):
        if tag in ("div", "p") and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")
    def handle_data(self, data): self.parts.append(data)


def _description_matches(actual, expected):
    if not isinstance(actual, str) or len(actual) > 32000: return False
    parser = _PlainDescription()
    parser.feed(actual); parser.close()
    return not parser.invalid and "".join(parser.parts).rstrip("\n") == expected.rstrip("\n")


def _plain_description(actual):
    _require(isinstance(actual, str) and len(actual) <= 32000, "task_description_unverified")
    parser = _PlainDescription(); parser.feed(actual); parser.close()
    text = "".join(parser.parts).rstrip("\n")
    _require(not parser.invalid, "task_description_unverified")
    return _text(text, 4000)


def _request(session, method, url, token, *, body=None, expected=200):
    allowed = ((method == "POST" and url == GMAIL_BASE + "/messages/send")
        or (method == "GET" and re.fullmatch(re.escape(GMAIL_BASE) + r"/messages/[a-f0-9]{1,64}\?format=raw&fields=id%2CthreadId%2ClabelIds%2Craw", url))
        or (method == "POST" and re.fullmatch(re.escape(BASECAMP_BASE) + r"/(?:todolists|todosets)/[0-9]{1,30}/todos\.json", url))
        or (method == "POST" and url == BASECAMP_BASE + f"/recordings/{FOLLOWUP_TASK_ID}/comments.json")
        or (method == "GET" and re.fullmatch(re.escape(BASECAMP_BASE) + r"/(?:todos|comments)/[0-9]{1,30}\.json", url)))
    _require(allowed, "provider_endpoint_forbidden")
    response = None
    try:
        encoded = None if body is None else provider._encode(body)
        _require(encoded is None or len(encoded) <= 65536, "action_payload_too_large")
        response = session.request(method, url,
            headers={"Authorization": "Bearer " + provider._token(token), "Content-Type": "application/json",
                     "User-Agent": "Hamle Internal Approved Dispatch (ahmet@gamechangers.com.tr)"},
            data=encoded, timeout=(10, 30), allow_redirects=False, stream=True)
        _require(response.status_code == expected, "provider_response_unconfirmed")
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=16384):
            total += len(chunk)
            _require(total <= 262144, "provider_response_too_large")
            chunks.append(chunk)
        result = provider._json(b"".join(chunks))
        _require(isinstance(result, dict), "provider_response_invalid")
        return result
    except WriterError:
        raise
    except Exception:
        raise WriterError("provider_outcome_unknown_no_retry") from None
    finally:
        if response is not None:
            try: response.close()
            except Exception: pass


class BusinessWriters:
    def __init__(self, secrets, claim_refresh=None):
        self.secrets, self.claim_refresh = secrets, claim_refresh

    @contextmanager
    def _gmail(self):
        helper, reader, _, _ = provider._modules()
        auth_session = provider._session()
        captured = {}
        def service_factory(credentials):
            captured["credentials"] = credentials
            return provider._gmail_service(credentials)
        with reader._quiet_call():
            try:
                with helper.verified_gmail(provider.GmailSecretStore(self.secrets), service_factory,
                    request_factory=lambda: provider._GoogleRequest(auth_session, gmail=True)):
                    credentials = captured["credentials"]
                    _require(credentials.valid, "gmail_credential_expired")
                    yield provider._token(credentials.token)
            except WriterError:
                raise
            except Exception:
                raise WriterError("gmail_identity_or_scope_unverified") from None
            finally:
                auth_session.close()

    def _basecamp(self, payload):
        token = basecamp_cloud_auth.access_token(self.secrets, claim_refresh=self.claim_refresh)
        verified = provider.verify_basecamp_internal(token)
        _require(verified.get("todo_parent_id") == payload["todo_parent_id"]
                 and verified.get("todo_parent_type") == payload["todo_parent_type"], "basecamp_target_changed")
        return token

    def _task_status(self, work, token, session):
        work = _work(work)
        row = _request(session, "GET", f"{BASECAMP_BASE}/todos/{work['task_id']}.json", token)
        _require(row.get("id") == work["task_id"] and row.get("bucket", {}).get("id") == 49184053
                 and row.get("parent", {}).get("id") == work["todo_parent_id"]
                 and row.get("parent", {}).get("type") == work["todo_parent_type"]
                 and row.get("creator", {}).get("id") == 53358100
                 and row.get("visible_to_clients") is False and row.get("status") in ("active", "archived", "trashed")
                 and type(row.get("completed")) is bool and "due_on" in row, "task_status_scope_mismatch")
        assignees = row.get("assignees")
        _require(isinstance(assignees, list) and len(assignees) == 1 and
                 isinstance(assignees[0], dict) and assignees[0].get("id") == 53358100, "task_status_assignee_mismatch")
        due = row["due_on"]
        if due is not None:
            _require(isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due), "task_status_date_invalid")
            try: date.fromisoformat(due)
            except ValueError: raise WriterError("task_status_date_invalid") from None
        updated = row.get("updated_at")
        try:
            _require(isinstance(updated, str) and datetime.fromisoformat(updated.replace("Z", "+00:00")).tzinfo is not None,
                     "task_status_date_invalid")
        except ValueError: raise WriterError("task_status_date_invalid") from None
        result = {key: work[key] for key in ("account_id", "project_id", "task_id", "todo_parent_id", "todo_parent_type")}
        result.update(title=_text(row.get("content"), 200, header=True), description=_plain_description(row.get("description")),
                      status=row["status"], completed=row["completed"], due_on=due, assignee_ids=[53358100])
        # Exclude update time/comment count: our own comment may change those.
        fingerprint = hashlib.sha256(provider._encode(result)).hexdigest()
        return {**result, "updated_at": updated, "task_fingerprint": fingerprint,
                "observed_at": datetime.now(timezone.utc).isoformat(), "identity_verified": True}

    def read_task_status(self, work):
        _work(work)
        with provider._modules()[1]._quiet_call():
            session = provider._session()
            try:
                return self._task_status(work, self._basecamp(work), session)
            except WriterError: raise
            except Exception: raise WriterError("task_status_unconfirmed") from None
            finally: session.close()

    def create_comment(self, payload, *, before_post=None):
        _require(callable(before_post), "durable_before_post_required")
        payload = _comment(payload)
        with provider._modules()[1]._quiet_call():
            session = provider._session()
            try:
                token = self._basecamp(payload)
                status = self._task_status(payload, token, session)
                _require(status["task_fingerprint"] == payload["task_fingerprint"] and
                         status["status"] == "active" and status["completed"] is False, "task_changed_before_comment")
                before_post()
                row = _request(session, "POST", f"{BASECAMP_BASE}/recordings/{FOLLOWUP_TASK_ID}/comments.json", token,
                               body={"content": _description(payload["body"])}, expected=201)
                _require(type(row.get("id")) is int and row["id"] > 0 and
                         row.get("bucket", {}).get("id") == 49184053 and
                         row.get("parent", {}).get("id") == FOLLOWUP_TASK_ID and
                         row.get("parent", {}).get("type") == "Todo", "comment_receipt_unconfirmed")
                return {"provider_id": str(row["id"])}
            except WriterError: raise
            except Exception: raise WriterError("comment_outcome_unknown_no_retry") from None
            finally: session.close()

    def read_comment(self, provider_id, payload):
        payload = _comment(payload)
        _require(isinstance(provider_id, str) and re.fullmatch(r"[0-9]{1,30}", provider_id), "comment_receipt_invalid")
        with provider._modules()[1]._quiet_call():
            session = provider._session()
            try:
                token = self._basecamp(payload)
                self._task_status(payload, token, session)  # Current scope; old fingerprint is not a read grant.
                row = _request(session, "GET", f"{BASECAMP_BASE}/comments/{provider_id}.json", token)
                _require(row.get("id") == int(provider_id) and row.get("type") == "Comment"
                         and row.get("bucket", {}).get("id") == 49184053
                         and row.get("parent", {}).get("id") == FOLLOWUP_TASK_ID
                         and row.get("parent", {}).get("type") == "Todo"
                         and row.get("creator", {}).get("id") == 53358100
                         and row.get("status") == "active" and row.get("visible_to_clients") is False
                         and row.get("content_attachments", []) == []
                         and _description_matches(row.get("content"), payload["body"]), "comment_readback_mismatch")
                return {"provider_id": provider_id, "identity_verified": True, "matches_approved_payload": True}
            except WriterError: raise
            except Exception: raise WriterError("comment_readback_unconfirmed") from None
            finally: session.close()

    def create_task(self, payload, *, before_post=None):
        _require(callable(before_post), "durable_before_post_required")
        payload = _task(payload)
        with provider._modules()[1]._quiet_call():
            session = provider._session()
            try:
                token = self._basecamp(payload)
                collection = {"Todoset": "todosets", "Todolist": "todolists"}[payload["todo_parent_type"]]
                before_post()
                result = _request(session, "POST", f"{BASECAMP_BASE}/{collection}/{payload['todo_parent_id']}/todos.json", token,
                    body={"content": payload["title"], "description": _description(payload["description"]),
                          "assignee_ids": [53358100], "completion_subscriber_ids": [], "notify": False}, expected=201)
                _require(type(result.get("id")) is int and result["id"] > 0, "task_receipt_invalid")
                _require(result.get("bucket", {}).get("id") == 49184053, "task_receipt_project_mismatch")
                return {"provider_id": str(result["id"])}
            except WriterError:
                raise
            except Exception:
                raise WriterError("task_outcome_unknown_no_retry") from None
            finally:
                session.close()

    def read_task(self, provider_id, payload):
        payload = _task(payload)
        _require(isinstance(provider_id, str) and re.fullmatch(r"[0-9]{1,30}", provider_id), "task_receipt_invalid")
        with provider._modules()[1]._quiet_call():
            session = provider._session()
            try:
                token = self._basecamp(payload)
                result = _request(session, "GET", f"{BASECAMP_BASE}/todos/{provider_id}.json", token)
                _require(result.get("id") == int(provider_id) and result.get("bucket", {}).get("id") == 49184053
                         and result.get("parent", {}).get("id") == payload["todo_parent_id"]
                         and result.get("parent", {}).get("type") == payload["todo_parent_type"]
                         and result.get("creator", {}).get("id") == 53358100
                         and result.get("status") == "active" and result.get("visible_to_clients") is False
                         and result.get("completed") is False and "due_on" in result and result["due_on"] is None
                         and result.get("content") == payload["title"]
                         and _description_matches(result.get("description"), payload["description"]), "task_readback_mismatch")
                assignees = result.get("assignees")
                _require(isinstance(assignees, list) and len(assignees) == 1
                         and assignees[0].get("id") == 53358100, "task_readback_assignee_mismatch")
                return {"provider_id": provider_id, "identity_verified": True, "matches_approved_payload": True}
            except WriterError:
                raise
            except Exception:
                raise WriterError("task_readback_unconfirmed") from None
            finally:
                session.close()

    def send_reply(self, payload, *, before_post=None):
        _require(callable(before_post), "durable_before_post_required")
        raw = _mime(payload)
        with self._gmail() as token:
            session = provider._session()
            try:
                before_post()
                result = _request(session, "POST", GMAIL_BASE + "/messages/send", token,
                    body={"threadId": payload["thread_id"], "raw": base64.urlsafe_b64encode(raw).decode("ascii")})
                mid = result.get("id")
                _require(isinstance(mid, str) and re.fullmatch(r"[a-f0-9]{1,64}", mid)
                         and result.get("threadId") == payload["thread_id"], "mail_receipt_unconfirmed")
                return {"provider_id": mid}
            finally:
                session.close()

    def read_reply(self, provider_id, payload):
        _reply(payload)
        _require(isinstance(provider_id, str) and re.fullmatch(r"[a-f0-9]{1,64}", provider_id), "mail_receipt_invalid")
        with self._gmail() as token:
            session = provider._session()
            try:
                url = f"{GMAIL_BASE}/messages/{provider_id}?format=raw&fields=id%2CthreadId%2ClabelIds%2Craw"
                result = _request(session, "GET", url, token)
                _require(result.get("id") == provider_id and result.get("threadId") == payload["thread_id"]
                         and "SENT" in result.get("labelIds", []), "mail_readback_receipt_mismatch")
                raw = result.get("raw")
                _require(isinstance(raw, str) and re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", raw), "mail_readback_invalid")
                decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
                message = BytesParser(policy=policy.default).parsebytes(decoded)
                def one(name):
                    fields = message.get_all(name, [])
                    _require(len(fields) == 1, "mail_readback_header_mismatch")
                    _require(not getattr(fields[0], "defects", ()), "mail_readback_header_mismatch")
                    return str(fields[0])
                _require(getaddresses([one("From")]) == [("", provider.HAMLE)]
                         and getaddresses([one("To")]) == [("", provider.AHMET)]
                         and ((not message.get_all("Cc")) if not payload["cc"] else
                              [address for _, address in getaddresses([one("Cc")])] == payload["cc"])
                         and not message.get_all("Bcc")
                         and one("Subject") == payload["subject"]
                         and _rfc_message_id_matches(one("Message-ID"), payload)
                         and one("In-Reply-To") == payload["in_reply_to"]
                         and " ".join(one("References").split()) == payload["references"]
                         and not message.is_multipart() and message.get_content_type() == "text/plain"
                         and not message.defects, "mail_readback_header_mismatch")
                body = message.get_content().replace("\r\n", "\n")
                expected = payload["body"] + ("" if payload["body"].endswith("\n") else "\n")
                _require(body == expected, "mail_readback_body_mismatch")
                return {"provider_id": provider_id, "identity_verified": True, "matches_approved_payload": True}
            except WriterError:
                raise
            except Exception:
                raise WriterError("mail_readback_unconfirmed") from None
            finally:
                session.close()
