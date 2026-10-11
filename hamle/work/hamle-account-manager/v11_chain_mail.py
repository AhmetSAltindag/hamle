"""Gmail adapters for v11_chain: verified multi-thread reader and same-thread reply writer.

ChainMailReader() -> verified latest-message sources for v11_chain (plan_intake fields).
GmailThreadReplyWriter: write(payload)/readback(remote_id, payload)/preflight(kind, payload)
  for the chain's clarify_brief payload and DeliveryCoordinator's deliver_file payload.

Reuse: cloud_providers (verified Gmail credential/identity, no-redirect zero-retry
sessions, gmail connector/normalizer/intake helpers), business_writers (MIME text
rules, RFC Message-ID pattern, Gmail-replaced Message-ID tolerance).
Rules: exact To/CC re-derived from the CURRENT provider headers of the reply anchor
(actual sender + actual To minus Hamle; actual CC minus Hamle/To), every address a
verified can_receive agency contact of the profile, In-Reply-To/References from the
anchor's real RFC headers, one POST, Sent-copy raw readback of every header, the body
and attachment bytes. Any exception after the POST is ambiguous: the chain records
hold_unknown and never resends. No retry, no draft, no label change happens here.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
import re
import time

import business_writers as bw
import cloud_providers as provider
from autonomy_policy import HAMLE, scope_digest
from recipient_policy import RecipientError

GMAIL = bw.GMAIL_BASE
HEX = re.compile(r"[a-f0-9]{1,64}\Z")
MAX_RAW = 4 * 1024 * 1024
MAX_READ = 8 * 1024 * 1024
META_HEADERS = ("From", "To", "Cc", "Subject", "Message-ID", "References")
INTERNAL_DOMAIN = "@gamechangers.com.tr"
CLARIFY_FIELDS = {"from", "to", "cc", "thread_id", "in_reply_to_message_id", "subject", "body"}
DELIVER_FIELDS = {"from", "to", "cc", "thread_id", "subject", "body", "artifact"}
ARTIFACT_FIELDS = {"brand_id", "work_id", "version", "sha256", "mime_type"}
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\Z")


class ChainMailError(ValueError):
    retry_allowed = False

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ChainMailError(code)


def _addresses(values):
    out = [a.strip().lower() for _, a in getaddresses(values) if a.strip()]
    return list(dict.fromkeys(out))


def reply_all(sender, to, cc):
    """Same derivation as v11_intake_plan._email / autonomy_policy._reply_all."""
    to_out = [v for v in dict.fromkeys([sender] + list(to)) if v != HAMLE]
    cc_out = [v for v in cc if v != HAMLE and v not in to_out]
    return to_out, list(dict.fromkeys(cc_out))


def message_fingerprint(thread_id, message_id, sender, to, cc, internal_ms, extra=""):
    return hashlib.sha256(provider._encode([thread_id, message_id, sender, sorted(to), sorted(cc),
                                            internal_ms, extra])).hexdigest()


def _allowed_recipients(catalog):
    rows = catalog.get("contacts") or []
    return {r["email"] for r in rows if isinstance(r, dict) and r.get("verified") is True
            and r.get("side") == "agency" and r.get("can_receive") is True
            and isinstance(r.get("email"), str) and r["email"].endswith(INTERNAL_DOMAIN)}


def check_recipients(catalog, to, cc):
    allowed = _allowed_recipients(catalog)
    if (not isinstance(to, list) or not isinstance(cc, list) or not to or len(to) + len(cc) > 20
            or len(set(to + cc)) != len(to + cc) or HAMLE in to + cc
            or any(v not in allowed for v in to + cc)):
        raise RecipientError("reply_recipient_scope_unverified")
    return {"to": list(to), "cc": list(cc)}


def _call(session, method, url, token, *, body=None, limit=262144):
    """Allowlisted Gmail REST call; static error codes only, never response text."""
    allowed = ((method == "POST" and url == GMAIL + "/messages/send")
               or (method == "GET" and re.fullmatch(re.escape(GMAIL) + r"/messages/[a-f0-9]{1,64}\?format=(?:metadata|raw)&.*", url))
               or (method == "GET" and re.fullmatch(re.escape(GMAIL) + r"/threads/[a-f0-9]{1,64}\?format=minimal&.*", url)))
    _need(allowed, "gmail_endpoint_forbidden")
    response = None
    try:
        encoded = None if body is None else provider._encode(body)
        _need(encoded is None or len(encoded) <= MAX_RAW * 2, "mail_payload_too_large")
        response = session.request(method, url, headers={
            "Authorization": "Bearer " + provider._token(token), "Content-Type": "application/json",
            "User-Agent": "Hamle Chain Mail (ahmet@gamechangers.com.tr)"},
            data=encoded, timeout=(10, 30), allow_redirects=False, stream=True)
        _need(response.status_code == 200, "provider_response_unconfirmed")
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=16384):
            total += len(chunk)
            _need(total <= limit, "provider_response_too_large")
            chunks.append(chunk)
        result = provider._json(b"".join(chunks))
        _need(isinstance(result, dict), "provider_response_invalid")
        return result
    except ChainMailError:
        raise
    except Exception:
        raise ChainMailError("provider_outcome_unknown_no_retry") from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def gmail_token_context(secrets):
    """Existing verified Gmail credential path (identity + exact scopes + refresh persistence)."""
    return bw.BusinessWriters(secrets)._gmail


class GmailThreadReplyWriter:
    """Same-thread Gmail reply for the chain. Constructed per tick; no state of its own.

    token_context() -> context manager yielding a verified Hamle Gmail access token.
    anchor_lookup(thread_id) -> Gmail id of the message this work replies to (chain record).
    attachment_source(artifact) -> {'filename','data','content_type'} freshly re-downloaded
      and hash-checked bytes for the deliver_file artifact (quality bridge).
    """

    def __init__(self, catalog, *, token_context, anchor_lookup, attachment_source=None,
                 session_factory=provider._session, clock=None):
        self.catalog = catalog
        self.token_context, self.anchor_lookup = token_context, anchor_lookup
        self.attachment_source, self.session_factory = attachment_source, session_factory
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ----- shape
    def _shape(self, payload):
        _need(isinstance(payload, dict) and payload.get("from") == HAMLE, "mail_payload_invalid")
        keys = set(payload)
        _need(keys in (CLARIFY_FIELDS, DELIVER_FIELDS), "mail_payload_invalid")
        kind = "clarify_brief" if keys == CLARIFY_FIELDS else "deliver_file"
        _need(isinstance(payload["thread_id"], str) and HEX.fullmatch(payload["thread_id"])
              and payload["thread_id"] not in provider.FORBIDDEN_THREADS, "mail_thread_invalid")
        bw._text(payload["subject"], 300, header=True)
        bw._text(payload["body"], 16000)
        if kind == "clarify_brief":
            _need(isinstance(payload["in_reply_to_message_id"], str)
                  and HEX.fullmatch(payload["in_reply_to_message_id"]), "mail_anchor_invalid")
        else:
            art = payload["artifact"]
            _need(isinstance(art, dict) and set(art) == ARTIFACT_FIELDS
                  and isinstance(art["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", art["sha256"]),
                  "mail_artifact_invalid")
        check_recipients(self.catalog, payload["to"], payload["cc"])
        return kind

    def _anchor(self, kind, payload):
        if kind == "clarify_brief":
            return payload["in_reply_to_message_id"]
        anchor = self.anchor_lookup(payload["thread_id"])
        _need(isinstance(anchor, str) and HEX.fullmatch(anchor), "mail_anchor_missing")
        return anchor

    # ----- provider reads
    @staticmethod
    def _headers(row, names):
        headers = (row.get("payload") or {}).get("headers")
        _need(isinstance(headers, list) and len(headers) <= 200, "mail_headers_invalid")
        out = {}
        for name in names:
            values = [h.get("value") for h in headers if isinstance(h, dict)
                      and isinstance(h.get("name"), str) and h["name"].casefold() == name.casefold()]
            _need(len(values) <= 1 and all(isinstance(v, str) for v in values), "mail_headers_ambiguous")
            out[name] = values[0] if values else None
        return out

    def _source(self, session, token, thread_id, anchor):
        fields = "id%2CthreadId%2ClabelIds%2CinternalDate%2Cpayload%2Fheaders"
        query = "&".join("metadataHeaders=" + h for h in META_HEADERS)
        row = _call(session, "GET", f"{GMAIL}/messages/{anchor}?format=metadata&{query}&fields={fields}", token)
        _need(row.get("id") == anchor and row.get("threadId") == thread_id, "mail_anchor_thread_mismatch")
        labels = row.get("labelIds") or []
        _need(isinstance(labels, list) and "SENT" not in labels and "DRAFT" not in labels, "mail_anchor_not_incoming")
        h = self._headers(row, META_HEADERS)
        senders = _addresses([h["From"] or ""])
        _need(len(senders) == 1 and senders[0] != HAMLE, "mail_anchor_sender_invalid")
        rfc = h["Message-ID"]
        _need(isinstance(rfc, str) and len(rfc) <= 998 and re.fullmatch(bw.MESSAGE_ID, rfc.strip()),
              "mail_anchor_rfc_id_invalid")
        refs = " ".join((h["References"] or "").split())
        _need(len(refs) <= 3000 and all(re.fullmatch(bw.MESSAGE_ID, r) for r in refs.split()),
              "mail_anchor_references_invalid")
        internal = row.get("internalDate")
        _need(isinstance(internal, str) and internal.isdigit() and len(internal) <= 15, "mail_anchor_time_invalid")
        to, cc = _addresses([h["To"] or ""]), _addresses([h["Cc"] or ""])
        _need(HAMLE in to + cc, "hamle_not_actual_to_or_cc")
        return {"message_id": anchor, "thread_id": thread_id, "sender": senders[0], "to": to, "cc": cc,
                "subject": h["Subject"] or "", "rfc_message_id": rfc.strip(),
                "references": " ".join(dict.fromkeys(refs.split() + [rfc.strip()])),
                "internal_ms": int(internal)}

    def _latest_incoming(self, session, token, thread_id):
        row = _call(session, "GET", f"{GMAIL}/threads/{thread_id}?format=minimal"
                    "&fields=id%2Cmessages(id%2ClabelIds%2CinternalDate)", token)
        _need(row.get("id") == thread_id and isinstance(row.get("messages"), list)
              and 0 < len(row["messages"]) <= 200, "mail_thread_snapshot_invalid")
        incoming = [m for m in row["messages"] if isinstance(m, dict) and not
                    {"SENT", "DRAFT"} & set(m.get("labelIds") or [])]
        _need(incoming and all(isinstance(m.get("internalDate"), str) and m["internalDate"].isdigit()
                               for m in incoming), "mail_thread_snapshot_invalid")
        return max(incoming, key=lambda m: int(m["internalDate"]))["id"]

    def _expected(self, kind, src):
        if kind == "clarify_brief":
            return [src["sender"]], []  # clarification goes ONLY to the requester
        return reply_all(src["sender"], src["to"], src["cc"])

    def _verify(self, kind, payload, src):
        to, cc = self._expected(kind, src)
        _need(payload["to"] == to and payload["cc"] == cc, "reply_recipients_changed")
        _need(payload["subject"] == "Re: " + src["subject"][:180], "reply_subject_mismatch")

    def source_evidence(self, thread_id, anchor):
        """Fresh provider evidence for DeliveryAdapter.read_authority (reads only)."""
        with self.token_context() as token:
            session = self.session_factory()
            try:
                src = self._source(session, token, thread_id, anchor)
                latest = self._latest_incoming(session, token, thread_id)
            finally:
                session.close()
        now = self.clock()
        return {"actual_latest": latest == anchor, "own_or_automatic": False,
                "provider_evidence_ref": f"gmail-metadata:{thread_id}/{anchor}",
                "message_id": anchor, "thread_id": thread_id,
                "fingerprint": message_fingerprint(thread_id, anchor, src["sender"], src["to"], src["cc"],
                                                   src["internal_ms"]),
                "sender": src["sender"], "to": src["to"], "cc": src["cc"], "subject": src["subject"],
                "received_at": datetime.fromtimestamp(src["internal_ms"] / 1000, timezone.utc).isoformat(),
                "captured_at": now.isoformat()}

    def preflight(self, kind, payload):
        """Read-only check right before authority: anchor is still the latest incoming
        message, exact To/CC and subject match it, attachment bytes match the artifact."""
        actual = self._shape(payload)
        _need(actual == kind, "mail_kind_mismatch")
        anchor = self._anchor(kind, payload)
        with self.token_context() as token:
            session = self.session_factory()
            try:
                src = self._source(session, token, payload["thread_id"], anchor)
                _need(self._latest_incoming(session, token, payload["thread_id"]) == anchor,
                      "newer_incoming_mail_in_thread")
            finally:
                session.close()
        self._verify(kind, payload, src)
        if kind == "deliver_file":
            self._attachment(payload)
        return True

    # ----- MIME
    def _attachment(self, payload):
        _need(callable(self.attachment_source), "attachment_source_missing")
        art = payload["artifact"]
        file = self.attachment_source(dict(art))
        _need(isinstance(file, dict) and type(file.get("data")) is bytes and 0 < len(file["data"]) <= 3 * 1024 * 1024
              and hashlib.sha256(file["data"]).hexdigest() == art["sha256"]
              and file.get("content_type") == art["mime_type"]
              and isinstance(file.get("filename"), str) and FILENAME.fullmatch(file["filename"])
              and ".." not in file["filename"], "attachment_bytes_mismatch")
        return file

    @staticmethod
    def _message_id(payload, src):
        digest = hashlib.sha256(provider._encode([scope_digest(payload), src["rfc_message_id"]])).hexdigest()
        return f"<hamle-chain-{digest}@gamechangers.com.tr>"

    def _mime(self, kind, payload, src, file):
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = HAMLE
        message["To"] = ", ".join(payload["to"])
        if payload["cc"]:
            message["Cc"] = ", ".join(payload["cc"])
        message["Subject"] = payload["subject"]
        message["In-Reply-To"] = src["rfc_message_id"]
        message["References"] = src["references"]
        message["Message-ID"] = self._message_id(payload, src)
        message.set_content(payload["body"], subtype="plain", charset="utf-8")
        if file is not None:
            main, sub = file["content_type"].split("/", 1)
            message.add_attachment(file["data"], maintype=main, subtype=sub, filename=file["filename"])
        raw = message.as_bytes()
        _need(len(raw) <= MAX_RAW, "mail_payload_too_large")
        return raw

    # ----- chain writer contract
    def write(self, payload):
        kind = self._shape(payload)
        anchor = self._anchor(kind, payload)
        file = self._attachment(payload) if kind == "deliver_file" else None
        with self.token_context() as token:
            session = self.session_factory()
            try:
                src = self._source(session, token, payload["thread_id"], anchor)
                self._verify(kind, payload, src)
                raw = self._mime(kind, payload, src, file)
                # Exactly one POST; any failure from here on is ambiguous for the caller.
                row = _call(session, "POST", GMAIL + "/messages/send", token,
                            body={"threadId": payload["thread_id"],
                                  "raw": base64.urlsafe_b64encode(raw).decode("ascii")})
            finally:
                session.close()
        mid = row.get("id")
        _need(isinstance(mid, str) and HEX.fullmatch(mid) and row.get("threadId") == payload["thread_id"],
              "mail_receipt_unconfirmed")
        return {"remote_id": mid}

    def readback(self, remote_id, payload):
        kind = self._shape(payload)
        _need(isinstance(remote_id, str) and HEX.fullmatch(remote_id), "mail_receipt_invalid")
        anchor = self._anchor(kind, payload)
        with self.token_context() as token:
            session = self.session_factory()
            try:
                src = self._source(session, token, payload["thread_id"], anchor)
                row = _call(session, "GET", f"{GMAIL}/messages/{remote_id}?format=raw"
                            "&fields=id%2CthreadId%2ClabelIds%2Craw", token, limit=MAX_READ)
            finally:
                session.close()
        _need(row.get("id") == remote_id and row.get("threadId") == payload["thread_id"]
              and "SENT" in (row.get("labelIds") or []), "mail_readback_receipt_mismatch")
        raw = row.get("raw")
        _need(isinstance(raw, str) and re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", raw), "mail_readback_invalid")
        try:
            message = BytesParser(policy=policy.default).parsebytes(
                base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
        except Exception:
            raise ChainMailError("mail_readback_invalid") from None
        def one(name):
            fields = message.get_all(name, [])
            _need(len(fields) == 1 and not getattr(fields[0], "defects", ()), "mail_readback_header_mismatch")
            return str(fields[0])
        returned = one("Message-ID")
        cc_ok = (not message.get_all("Cc")) if not payload["cc"] else \
            [a.lower() for _, a in getaddresses([one("Cc")])] == payload["cc"]
        _need(getaddresses([one("From")]) == [("", HAMLE)]
              and [a.lower() for _, a in getaddresses([one("To")])] == payload["to"] and cc_ok
              and not message.get_all("Bcc") and one("Subject") == payload["subject"]
              and one("In-Reply-To") == src["rfc_message_id"]
              and " ".join(one("References").split()) == src["references"]
              and (returned == self._message_id(payload, src)
                   or (re.fullmatch(bw.MESSAGE_ID, returned) and returned.endswith("@mail.gmail.com>")))
              and not message.defects, "mail_readback_header_mismatch")
        expected_body = payload["body"] + ("" if payload["body"].endswith("\n") else "\n")
        if kind == "clarify_brief":
            _need(not message.is_multipart() and message.get_content_type() == "text/plain"
                  and message.get_content().replace("\r\n", "\n") == expected_body, "mail_readback_body_mismatch")
        else:
            parts = list(message.iter_parts()) if message.is_multipart() else []
            _need(message.get_content_type() == "multipart/mixed" and len(parts) == 2
                  and parts[0].get_content_type() == "text/plain"
                  and parts[0].get_content().replace("\r\n", "\n") == expected_body, "mail_readback_body_mismatch")
            data = parts[1].get_payload(decode=True)
            art = payload["artifact"]
            _need(parts[1].get_content_disposition() == "attachment" and not parts[1].defects
                  and parts[1].get_content_type() == art["mime_type"] and type(data) is bytes
                  and hashlib.sha256(data).hexdigest() == art["sha256"], "mail_readback_attachment_mismatch")
        return {"readback_sha256": scope_digest(payload)}


class ChainMailReader:
    """Verified latest incoming mail per thread for the chain (bodies stay in memory).

    Only threads whose newest message is an incoming, non-automated mail from a
    verified agency requester with Hamle in actual To/CC are returned. A thread
    whose newest message is Hamle's own reply yields nothing (no new work).
    Per-thread failures are skipped and counted, never guessed.
    """

    def __init__(self, store, catalog, *, clock=time.time, max_threads=10, newer_than_days=3,
                 _service_factory=None, _credentials_factory=None, _request_factory=None):
        self.store, self.catalog, self.clock = store, catalog, clock
        self.max_threads, self.days = max_threads, newer_than_days
        self.factories = (_service_factory, _credentials_factory, _request_factory)
        self.skipped = {}
        self.order = 0

    def _requesters(self):
        return {r["email"] for r in self.catalog.get("contacts") or [] if isinstance(r, dict)
                and r.get("verified") is True and r.get("side") == "agency" and r.get("can_request") is True
                and isinstance(r.get("email"), str) and r["email"].endswith(INTERNAL_DOMAIN)}

    def _skip(self, reason):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def __call__(self):
        helper, reader, normalizer, intake = provider._modules()
        service_factory, credentials_factory, request_factory = self.factories
        requesters = sorted(self._requesters())
        if not requesters:
            return []
        auth_session = None
        out = []
        with reader._quiet_call():
            try:
                service_factory = service_factory or provider._gmail_service
                if request_factory is None:
                    auth_session = provider._session()
                    request_factory = lambda: provider._GoogleRequest(auth_session, gmail=True)
                senders = " ".join("from:" + s for s in requesters)
                query = f"{{to:{HAMLE} cc:{HAMLE}}} {{{senders}}} newer_than:{int(self.days)}d"
                with helper.verified_gmail(self.store, service_factory, credentials_factory,
                                           request_factory) as (service, _):
                    found = service.users().threads().list(
                        userId="me", q=query, maxResults=self.max_threads, includeSpamTrash=False,
                        fields="threads/id,nextPageToken").execute(num_retries=0)
                    rows = found.get("threads", []) if isinstance(found, dict) else None
                    _need(isinstance(rows, list) and len(rows) <= self.max_threads, "mail_search_invalid")
                    if found.get("nextPageToken"):
                        self._skip("search_page_truncated")
                    threads = []
                    for row in rows:
                        try:
                            tid = intake.identifier(row.get("id"))
                            if tid in provider.FORBIDDEN_THREADS:
                                raise ChainMailError("historical_thread_forbidden")
                            meta = service.users().threads().get(
                                userId="me", id=tid, format="metadata",
                                metadataHeaders=["Subject", "Message-ID", "References"],
                                fields="id,messages(id,threadId,internalDate,payload/headers)").execute(num_retries=0)
                            threads.append((tid, meta))
                        except Exception as exc:
                            self._skip(getattr(exc, "code", None) or "thread_metadata_failed")
                for tid, meta in threads:
                    try:
                        source = self._thread(tid, meta, set(requesters), helper, reader, normalizer, intake,
                                              service_factory, credentials_factory, request_factory)
                        if source is not None:
                            out.append(source)
                    except Exception as exc:
                        self._skip(getattr(exc, "code", None) or "thread_read_failed")
            finally:
                if auth_session is not None:
                    auth_session.close()
        return out

    def _thread(self, tid, meta, requesters, helper, reader, normalizer, intake,
                service_factory, credentials_factory, request_factory):
        subjects = provider._subject_snapshot(meta, tid, exact_subject=None)
        def read(body_id):
            return reader._read_verified(tid, body_id, helper=helper, normalizer=normalizer, store=self.store,
                                         service_factory=service_factory, credentials_factory=credentials_factory,
                                         request_factory=request_factory)
        observed = read(None)
        md = intake.metadata(observed["observation"])
        newest = max(md["messages"], key=lambda m: m["time_ms"])
        latest = md["latest"]
        if latest is None or newest["id"] != latest["id"]:
            return None  # Hamle (or nobody) spoke last: no new work in this thread
        if latest["from"] not in requesters or latest["automated"] or HAMLE not in latest["to"] + latest["cc"]:
            raise ChainMailError("latest_mail_not_eligible")
        dates = {m["id"]: m["time_ms"] for m in md["messages"]}
        _need(set(dates) == set(subjects) and all(dates[k] == subjects[k]["time_ms"] for k in dates),
              "thread_changed_between_reads")
        before = intake.snapshot_fingerprint(observed["observation"])
        body = read(latest["id"])
        _need(intake.snapshot_fingerprint(body["observation"]) == before, "thread_changed_before_body")
        row = next(m for m in body["observation"]["messages"] if m["message_id"] == latest["id"])
        _need(row["body_status"] == "decoded_unrendered" and not row["issues"] and row["envelope_eligible"],
              "mail_body_unavailable")
        plain = [p["text"] for p in row["body"] if p["mime_type"] == "text/plain"]
        _need(len(plain) == 1 and plain[0].strip() and len(plain[0].encode("utf-8")) <= provider.MAX_BODY_BYTES,
              "plaintext_body_required")
        rfc = subjects[latest["id"]]["message_ids"]
        _need(len(rfc) == 1 and isinstance(rfc[0], str) and re.fullmatch(bw.MESSAGE_ID, rfc[0].strip()),
              "mail_rfc_headers_invalid")
        body_sha = hashlib.sha256(plain[0].encode("utf-8")).hexdigest()
        to, cc = sorted(latest["to"]), sorted(latest["cc"])
        observed_at = datetime.fromtimestamp(intake.timestamp(self.clock()), timezone.utc).isoformat()
        return {"channel": "email", "verified": True, "headers_origin": "provider", "latest_in_thread": True,
                "evidence_ref": f"gmail:{tid}/{latest['id']}", "message_id": latest["id"], "thread_id": tid,
                "fingerprint": message_fingerprint(tid, latest["id"], latest["from"], to, cc, latest["time_ms"],
                                                   body_sha),
                "observed_at": observed_at, "automatic": False, "own_message": False, "human_takeover": False,
                "sender": latest["from"], "to": to, "cc": cc, "origin": {"kind": "direct_email"},
                "subject": subjects[latest["id"]]["subject"][:300], "text": plain[0],
                "source_order": latest["time_ms"], "received_at_ms": latest["time_ms"], "rfc_message_id": rfc[0].strip(),
                "attachments_fetched": False,
                "has_attachments": any(i.get("is_attachment") for i in row["mime_inventory"])}
