"""Narrow cloud secret and read-only provider adapters. No import-time I/O.

Caller must enforce the durable stop/generation/budget gates. Source text stays
in memory; this module never writes tokens or mail bodies to files or logs.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from functools import lru_cache
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from recipient_policy import RecipientError, validate_reply_recipients

PROJECT = "gamechangers-hamle"
PROJECT_NUMBER = "286533609595"
GMAIL_SECRET = "hamle-gmail-oauth"
OPENAI_SECRET = "hamle-openai-api-key"
BASECAMP_SECRET = "hamle-basecamp-credential"
SECRET_NAMES = frozenset((GMAIL_SECRET, OPENAI_SECRET, BASECAMP_SECRET))
# Claude API key (Secret Manager, added by Ahmet 2026-10-11). Kept on its own lines so the
# names above stay untouched; the secret allowlist is the union.
ANTHROPIC_SECRET = "hamle-anthropic-api-key"
PAGESPEED_SECRET = "hamle-pagespeed-api-key"  # Google API key limited to the PageSpeed Insights API (Dijital Karne)
SECRET_NAMES = SECRET_NAMES | frozenset((ANTHROPIC_SECRET, PAGESPEED_SECRET))
INTERNAL_SUBJECT = "HAMLE-IC-IS-20261010-01"
PRODUCTION_INTERNAL_SUBJECT = "HAMLE-IC-URETIM-20261010-01"
INTERNAL_SUBJECTS = frozenset((INTERNAL_SUBJECT, PRODUCTION_INTERNAL_SUBJECT))
CONTINUATION_THREAD = "1a122eec4ccdb53a"
HAMLE = "hamle@gamechangers.com.tr"
AHMET = "ahmet@gamechangers.com.tr"
FORBIDDEN_THREADS = frozenset(("1a12270588d30c79", "1a122c8fb08321fb"))
BASECAMP_ACCOUNT = 5898865
BASECAMP_PROJECT = 49184053
# Exact verified non-client members of 49184053 (Mac readback 2026-10-11 ~17:00Z):
# Ahmet, Cansu, Özge, Hamle. Must equal the internal catalog's project_member ids + Hamle
# (test enforces it); adding a member to the project needs both updated together.
BASECAMP_MEMBERS = frozenset((47470759, 48793256, 52096503, 53358100))
BASECAMP_PATHS = ("/my/profile.json", "/projects/49184053.json", "/projects/49184053/people.json",
                  "/buckets/49184053/todos/10391849167.json")
GMAIL_QUERY = f'from:{AHMET} {{to:{HAMLE} cc:{HAMLE}}} subject:"{INTERNAL_SUBJECT}"'
MAX_SECRET_BYTES = 65_536
MAX_BODY_BYTES = 12_000
TIMEOUT = (10, 30)


class ProviderError(Exception):
    """Fixed code only. No raw request/response/exception text escapes."""
    retry_allowed = False
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@lru_cache(maxsize=1)
def _modules():
    root = Path(__file__).resolve().parent.parent
    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    try:
        return (load("_hamle_cloud_helper", root / "hamle-gmail-connector/hamle_gmail.py"),
                load("_hamle_cloud_reader", root / "hamle-workflow/gmail_full_reader.py"),
                load("_hamle_cloud_normalizer", root / "hamle-workflow/mail_ingest.py"),
                load("_hamle_cloud_intake", root / "hamle-internal-intake/intake.py"))
    except Exception:
        raise ProviderError("local_dependencies_unavailable") from None


class _NoRedirectSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs["allow_redirects"] = False
        kwargs["timeout"] = TIMEOUT
        return super().request(method, url, **kwargs)


def _session():
    session = _NoRedirectSession()
    session.trust_env = False
    session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
    session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
    return session


def _json(raw):
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError()
            out[key] = value
        return out
    try:
        return json.loads(raw, object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except Exception:
        raise ProviderError("provider_json_invalid") from None


def _encode(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    except Exception:
        raise ProviderError("payload_invalid") from None


def _token(value):
    if (not isinstance(value, str) or not 20 <= len(value) <= 8192
            or not value.isascii() or any(c.isspace() for c in value)):
        raise ProviderError("credential_invalid")
    return value


def _request(session, method, url, *, headers, body=None, limit=262144):
    response = None
    try:
        response = session.request(method, url, headers=headers, data=body,
                                   timeout=TIMEOUT, allow_redirects=False, stream=True)
        if response.status_code != 200:
            raise ProviderError("provider_request_rejected")
        if 'rel="next"' in response.headers.get("Link", ""):
            raise ProviderError("provider_pagination_unhandled")
        data, size = [], 0
        for chunk in response.iter_content(chunk_size=16384):
            size += len(chunk)
            if size > limit:
                raise ProviderError("provider_response_too_large")
            data.append(chunk)
        return b"".join(data), dict(response.headers)
    except ProviderError:
        raise
    except Exception:
        raise ProviderError("provider_outcome_unknown") from None
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def _crc32c(data):
    # Castagnoli; fixed small secret payloads avoid an extra native dependency.
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0x82F63B78 if crc & 1 else 0)
    return crc ^ 0xFFFFFFFF


class _GoogleRequest:
    """Bounded auth transport; repeated identical token requests are refused."""
    def __init__(self, session, *, gmail=False):
        from google.auth.transport.requests import Request
        self.request = Request(session=session)
        self.gmail = gmail
        self.seen = set()
    def __call__(self, url, method="GET", body=None, headers=None, **kwargs):
        parsed = urlsplit(url)
        oauth = url == "https://oauth2.googleapis.com/token" and method == "POST"
        metadata = (not self.gmail and method == "GET" and parsed.scheme == "http"
                    and parsed.hostname in ("metadata.google.internal", "169.254.169.254")
                    and (parsed.path.startswith("/computeMetadata/v1/")
                         or (parsed.hostname == "169.254.169.254" and parsed.path in ("", "/"))))
        if not (oauth or metadata) or url in self.seen or len(self.seen) >= 8:
            raise ProviderError("auth_transport_scope_or_retry_blocked")
        self.seen.add(url)
        return self.request(url=url, method=method, body=body, headers=headers, timeout=30)


class GoogleSecretManager:
    def __init__(self, *, credentials=None, session=None):
        self.session = session or _session()
        self.owns_session = session is None
        self.credentials = credentials

    def _headers(self):
        try:
            if self.credentials is None:
                import google.auth
                self.credentials, project = google.auth.default(
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                    request=_GoogleRequest(self.session))
                if project not in (None, PROJECT, PROJECT_NUMBER):
                    raise ProviderError("adc_project_mismatch")
            if not self.credentials.valid:
                self.credentials.refresh(_GoogleRequest(self.session))
            return {"Authorization": "Bearer " + _token(self.credentials.token),
                    "Content-Type": "application/json"}
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("cloud_auth_failed") from None

    @staticmethod
    def _name(name):
        if name not in SECRET_NAMES:
            raise ProviderError("secret_not_allowed")
        return f"projects/{PROJECT}/secrets/{name}"

    @staticmethod
    def _version_name(value, name):
        pattern = rf"projects/(?:{PROJECT}|{PROJECT_NUMBER})/secrets/{re.escape(name)}/versions/[1-9][0-9]*"
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ProviderError("secret_version_mismatch")
        return value

    def access(self, name):
        resource = self._name(name)
        with _modules()[1]._quiet_call():
            raw, _ = _request(self.session, "GET",
                f"https://secretmanager.googleapis.com/v1/{resource}/versions/latest:access",
                headers=self._headers())
            result = _json(raw)
            if not isinstance(result, dict):
                raise ProviderError("secret_payload_invalid")
            self._version_name(result.get("name"), name)
            try:
                payload = result["payload"]
                data = base64.b64decode(payload["data"], validate=True)
                checksum = payload["dataCrc32c"]
                if (not 1 <= len(data) <= MAX_SECRET_BYTES or not isinstance(checksum, str)
                        or not checksum.isdigit() or int(checksum) != _crc32c(data)):
                    raise ValueError()
                return data
            except Exception:
                raise ProviderError("secret_payload_invalid") from None

    def add_gmail_version(self, record):
        helper = _modules()[0]
        try:
            helper.validate_record(record)
        except Exception:
            raise ProviderError("gmail_record_invalid") from None
        return self._add_version(GMAIL_SECRET, record)

    def add_basecamp_version(self, record):
        import basecamp_cloud_auth
        try:
            basecamp_cloud_auth.validate_record(record)
        except Exception:
            raise ProviderError("basecamp_record_invalid") from None
        return self._add_version(BASECAMP_SECRET, record)

    def _add_version(self, name, record):
        if name not in (GMAIL_SECRET, BASECAMP_SECRET):
            raise ProviderError("secret_write_not_allowed")
        encoded = _encode(record)
        if not 1 <= len(encoded) <= MAX_SECRET_BYTES:
            raise ProviderError("secret_payload_invalid")
        with _modules()[1]._quiet_call():
            body = _encode({"payload": {"data": base64.b64encode(encoded).decode(),
                                         "dataCrc32c": str(_crc32c(encoded))}})
            raw, _ = _request(self.session, "POST",
                f"https://secretmanager.googleapis.com/v1/{self._name(name)}:addVersion",
                headers=self._headers(), body=body)
            result = _json(raw)
            if (not isinstance(result, dict) or result.get("state") != "ENABLED"
                    or result.get("clientSpecifiedPayloadChecksum") is not True):
                raise ProviderError("secret_write_unconfirmed")
            return self._version_name(result.get("name"), name)

    def close(self):
        if self.owns_session:
            self.session.close()


class GmailSecretStore:
    def __init__(self, secrets):
        self.secrets = secrets
    def load(self):
        try:
            return _modules()[0].validate_record(_json(self.secrets.access(GMAIL_SECRET)))
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("gmail_record_invalid") from None
    def save(self, record):
        # Only the refreshed Gmail record may gain a secret version. A failed or
        # uncertain save stops intake before reading/acting; no retry here.
        self.secrets.add_gmail_version(record)


def openai_key(secrets):
    try:
        return _token(secrets.access(OPENAI_SECRET).decode("ascii"))
    except Exception:
        raise ProviderError("openai_credential_unavailable") from None


def anthropic_key(secrets):
    """Claude API key from hamle-anthropic-api-key: stripped, sk-ant- prefix required.

    Errors are one fixed code; the value (or any part of it) is never logged or returned in one.
    """
    try:
        value = secrets.access(ANTHROPIC_SECRET).decode("ascii").strip()
        if not value.startswith("sk-ant-"):
            raise ValueError()
        return _token(value)
    except Exception:
        raise ProviderError("anthropic_credential_unavailable") from None


def pagespeed_key(secrets):
    """PageSpeed Insights API key from hamle-pagespeed-api-key (keyless calls share an exhausted daily quota).

    Errors are one fixed code; the value is never logged or returned in one.
    """
    try:
        value = secrets.access(PAGESPEED_SECRET).decode("ascii").strip()
        if not re.fullmatch(r"AIza[0-9A-Za-z_\-]{35}", value):
            raise ValueError()
        return value
    except Exception:
        raise ProviderError("pagespeed_credential_unavailable") from None


ATTACHMENT_RESPONSE_LIMIT = 28_000_000  # base64 JSON of a 20 MB attachment


class _ReadOnlyGmailHttp:
    def __init__(self, credentials):
        self.credentials = credentials
        self.session = _session()
    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        from httplib2 import Response
        parsed = urlsplit(uri)
        attachment = re.fullmatch(r"/gmail/v1/users/me/messages/[a-fA-F0-9]{1,64}/attachments/[A-Za-z0-9_-]{1,4096}",
                                  parsed.path)  # a client mail's attachment (Ahmet 14 Oct: read every attachment)
        if (method != "GET" or body is not None or parsed.scheme != "https"
                or parsed.netloc != "gmail.googleapis.com"
                or not (attachment or re.fullmatch(r"/gmail/v1/users/me/(profile|threads(?:/[a-fA-F0-9]{1,64})?)",
                                                   parsed.path))):
            raise ProviderError("gmail_read_path_forbidden")
        if not self.credentials.valid:
            raise ProviderError("gmail_credential_expired")
        raw, response_headers = _request(self.session, "GET", uri,
            headers={"Authorization": "Bearer " + _token(self.credentials.token)},
            limit=ATTACHMENT_RESPONSE_LIMIT if attachment else 2_097_152)
        return Response({**response_headers, "status": "200"}), raw
    def close(self):
        self.session.close()


def _gmail_service(credentials):
    from googleapiclient.discovery import build
    return build("gmail", "v1", http=_ReadOnlyGmailHttp(credentials),
                 cache_discovery=False, static_discovery=True)


def verify_gmail_identity(store, *, _service_factory=None, _credentials_factory=None,
                          _request_factory=None):
    """Live profile and exact scope check only; never search or fetch mail."""
    helper, reader, _, _ = _modules()
    auth_session = None
    with reader._quiet_call():
        try:
            if _request_factory is None:
                auth_session = _session()
                _request_factory = lambda: _GoogleRequest(auth_session, gmail=True)
            with helper.verified_gmail(store, _service_factory or _gmail_service,
                    _credentials_factory, _request_factory) as (_, refreshed):
                return {"status": "identity_verified", "email": HAMLE,
                        "scopes": sorted(helper.SCOPES), "token_refreshed": bool(refreshed),
                        "mailbox_reads": 0, "mailbox_writes": 0, "action_eligible": False}
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("gmail_identity_verification_failed") from None
        finally:
            if auth_session is not None:
                auth_session.close()


def _subject_snapshot(value, thread_id, exact_subject=INTERNAL_SUBJECT):
    if not isinstance(value, dict) or value.get("id") != thread_id or "nextPageToken" in value:
        raise ProviderError("gmail_subject_snapshot_invalid")
    rows = value.get("messages")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ProviderError("gmail_subject_snapshot_invalid")
    out = {}
    for row in rows:
        try:
            mid = row["id"]
            stamp = row["internalDate"]
            if (not re.fullmatch(r"[a-f0-9]{1,64}", mid) or mid in out
                    or row["threadId"] != thread_id or not isinstance(stamp, str)
                    or not stamp.isascii() or not stamp.isdigit() or len(stamp) > 15):
                raise ValueError()
            headers = row["payload"]["headers"]
            if not isinstance(headers, list) or len(headers) > 200:
                raise ValueError()
            subjects = [h["value"] for h in headers if h["name"].casefold() == "subject"]
            if len(subjects) != 1 or not isinstance(subjects[0], str):
                raise ValueError()
            message_ids = [h["value"] for h in headers if h["name"].casefold() == "message-id"]
            references = [h["value"] for h in headers if h["name"].casefold() == "references"]
            reply_to = [h["value"] for h in headers if h["name"].casefold() == "in-reply-to"]
            out[mid] = {"time_ms": int(stamp), "matches": subjects[0] == exact_subject,
                        "subject": subjects[0], "message_ids": message_ids, "references": references,
                        "in_reply_to": reply_to}
        except Exception:
            raise ProviderError("gmail_subject_snapshot_invalid") from None
    return out


def capture_internal_request(store, *, allow_body_for_internal_request=False,
                             continuation=None, exact_subject=INTERNAL_SUBJECT,
                             clock=time.time, _service_factory=None,
                             _credentials_factory=None, _request_factory=None):
    """One exact internal thread, one latest eligible plaintext body in memory.

    The underscored dependencies exist only for network-free tests. Worker stop
    generation must be read before this function and fenced again afterward.
    """
    if allow_body_for_internal_request is not True:
        raise ProviderError("explicit_internal_body_flag_required")
    if exact_subject not in INTERNAL_SUBJECTS:
        raise ProviderError("internal_subject_not_allowed")
    if continuation is not None and exact_subject != INTERNAL_SUBJECT:
        raise ProviderError("continuation_binding_invalid")
    rfc_pattern = r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>"
    if continuation is not None and (not isinstance(continuation, dict)
            or set(continuation) != {"thread_id", "root_message_id", "root_rfc_message_id"}
            or continuation.get("thread_id") != CONTINUATION_THREAD
            or not isinstance(continuation.get("root_message_id"), str)
            or not re.fullmatch(r"[a-f0-9]{1,64}", continuation["root_message_id"])
            or not isinstance(continuation.get("root_rfc_message_id"), str)
            or len(continuation["root_rfc_message_id"]) > 998
            or not re.fullmatch(rfc_pattern, continuation["root_rfc_message_id"])):
        raise ProviderError("continuation_binding_invalid")
    helper, reader, normalizer, intake = _modules()
    auth_session = None
    with reader._quiet_call():
        try:
            service_factory = _service_factory or _gmail_service
            if _request_factory is None:
                auth_session = _session()
                _request_factory = lambda: _GoogleRequest(auth_session, gmail=True)
            with helper.verified_gmail(store, service_factory, _credentials_factory, _request_factory) as (service, refreshed):
                query = f'from:{AHMET} {{to:{HAMLE} cc:{HAMLE}}} subject:"{exact_subject}"'
                found = service.users().threads().list(userId="me", q=query,
                    maxResults=2, includeSpamTrash=False, fields="threads/id,nextPageToken").execute(num_retries=0)
                if not isinstance(found, dict) or found.get("nextPageToken"):
                    raise ProviderError("internal_search_ambiguous")
                rows = found.get("threads", [])
                if not isinstance(rows, list) or len(rows) != 1:
                    raise ProviderError("internal_request_not_found" if rows == [] else "internal_search_ambiguous")
                thread_id = intake.identifier(rows[0].get("id"))
                if thread_id in FORBIDDEN_THREADS:
                    raise ProviderError("historical_test_forbidden")
                if continuation is not None and thread_id != continuation["thread_id"]:
                    raise ProviderError("continuation_thread_mismatch")
                subjects = _subject_snapshot(service.users().threads().get(userId="me", id=thread_id,
                    format="metadata", metadataHeaders=["Subject", "Message-ID", "References", "In-Reply-To"],
                    fields="id,messages(id,threadId,internalDate,payload/headers)").execute(num_retries=0), thread_id, exact_subject)
                if not any(row["matches"] for row in subjects.values()):
                    raise ProviderError("exact_internal_subject_missing")
            def read(body_message_id):
                return reader._read_verified(thread_id, body_message_id, helper=helper,
                    normalizer=normalizer, store=store, service_factory=service_factory,
                    credentials_factory=_credentials_factory, request_factory=_request_factory)
            observed = read(None)
            observation = observed["observation"]
            metadata = intake.metadata(observation)
            latest = metadata["latest"]
            dates = {row["id"]: row["time_ms"] for row in metadata["messages"]}
            if set(dates) != set(subjects) or any(dates[m] != subjects[m]["time_ms"] for m in dates):
                raise ProviderError("thread_changed_between_reads")
            if (latest is None or latest["id"] != max(metadata["messages"], key=lambda m: m["time_ms"])["id"]
                    or latest["from"] != AHMET or latest["automated"]
                    or HAMLE not in latest["to"] + latest["cc"]):
                raise ProviderError("latest_internal_request_not_eligible")
            is_continuation = continuation is not None and latest["id"] != continuation["root_message_id"]
            if continuation is not None:
                root_id = continuation["root_message_id"]
                root = next((m for m in metadata["messages"] if m["id"] == root_id), None)
                if (root is None or root["from"] != AHMET or root["automated"]
                        or HAMLE not in root["to"] + root["cc"] or not subjects[root_id]["matches"]
                        or subjects[root_id]["message_ids"] != [continuation["root_rfc_message_id"]]):
                    raise ProviderError("continuation_root_mismatch")
            if subjects[latest["id"]]["subject"] != ("Re: " + exact_subject if is_continuation else exact_subject):
                raise ProviderError("latest_internal_request_not_eligible")
            reply_meta = subjects[latest["id"]]
            rfc_ids, refs = reply_meta["message_ids"], reply_meta["references"]
            pattern = rfc_pattern
            if (len(rfc_ids) != 1 or not isinstance(rfc_ids[0], str) or len(rfc_ids[0]) > 998
                    or not re.fullmatch(pattern, rfc_ids[0]) or len(refs) > 1):
                raise ProviderError("internal_reply_headers_missing_or_invalid")
            reference = refs[0] if refs else ""
            if (not isinstance(reference, str) or len(reference) > 1800 or "\r" in reference or "\n" in reference
                    or any(not re.fullmatch(pattern, r) for r in reference.split())):
                raise ProviderError("internal_reply_headers_missing_or_invalid")
            if is_continuation:
                parent_ids = reply_meta["in_reply_to"]
                earlier_ids = {r["message_ids"][0] for r in subjects.values()
                               if r["time_ms"] < reply_meta["time_ms"] and len(r["message_ids"]) == 1
                               and isinstance(r["message_ids"][0], str) and re.fullmatch(pattern, r["message_ids"][0])}
                if (continuation["root_rfc_message_id"] not in reference.split()
                        or len(parent_ids) != 1 or parent_ids[0] not in earlier_ids
                        or parent_ids[0] not in reference.split()):
                    raise ProviderError("continuation_reply_chain_invalid")
            reply_references = " ".join([*reference.split(), rfc_ids[0]])
            digest = intake.snapshot_fingerprint(observation)
            body_read = read(latest["id"])
            if intake.snapshot_fingerprint(body_read["observation"]) != digest:
                raise ProviderError("thread_changed_before_body")
            row = next(m for m in body_read["observation"]["messages"] if m["message_id"] == latest["id"])
            if row["body_status"] != "decoded_unrendered" or row["issues"] or not row["envelope_eligible"]:
                raise ProviderError("internal_body_unavailable")
            if any(item.get("is_attachment") for item in row["mime_inventory"]):
                raise ProviderError("internal_attachments_not_accepted")
            plain = [p["text"] for p in row["body"] if p["mime_type"] == "text/plain"]
            if len(plain) != 1 or not plain[0].strip() or len(plain[0].encode("utf-8")) > MAX_BODY_BYTES:
                raise ProviderError("internal_plaintext_body_required")
            now = intake.timestamp(clock())
            return {"source_text": plain[0], "evidence": {
                "subject": exact_subject, "thread_id": thread_id, "message_id": latest["id"],
                "source_subject": subjects[latest["id"]]["subject"], "is_continuation": is_continuation,
                "root_message_id": continuation["root_message_id"] if continuation else latest["id"],
                "root_rfc_message_id": continuation["root_rfc_message_id"] if continuation else rfc_ids[0],
                "snapshot_fingerprint": digest, "captured_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "in_reply_to": rfc_ids[0], "references": reply_references,
                "source_sha256": hashlib.sha256(plain[0].encode("utf-8")).hexdigest(),
                "sender": AHMET, "hamle_direct_recipient": True,
                "latest_to": list(latest["to"]), "latest_cc": list(latest["cc"]),
                "body_in_memory_only": True, "attachments_fetched": False,
                "token_refreshed": bool(refreshed or observed["token_refreshed"] or body_read["token_refreshed"]),
                "action_eligible": False, "mailbox_writes": 0,
                "unseen_cc_removal_resolved": False}}
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("internal_capture_failed") from None
        finally:
            if auth_session is not None:
                auth_session.close()


def verify_latest_sent_receipt(store, thread_id, message_id, *, _service_factory=None,
                               _credentials_factory=None, _request_factory=None):
    """Metadata-only proof that a durable sent receipt is still latest.

    This grants no action authority. The caller must independently own an
    accepted/verified durable receipt for these exact IDs. No body is fetched.
    """
    if (not isinstance(thread_id, str) or not re.fullmatch(r"[a-f0-9]{1,64}", thread_id)
            or thread_id in FORBIDDEN_THREADS or not isinstance(message_id, str)
            or not re.fullmatch(r"[a-f0-9]{1,64}", message_id)):
        raise ProviderError("sent_receipt_identifiers_invalid")
    helper, reader, normalizer, _ = _modules()
    not_latest = {"status": "not_latest_sent_receipt"}
    auth_session = None
    with reader._quiet_call():
        try:
            if _request_factory is None:
                auth_session = _session()
                _request_factory = lambda: _GoogleRequest(auth_session, gmail=True)
            with helper.verified_gmail(store, _service_factory or _gmail_service,
                    _credentials_factory, _request_factory) as (service, _):
                found = service.users().threads().list(userId="me", q=GMAIL_QUERY,
                    maxResults=2, includeSpamTrash=False, fields="threads/id,nextPageToken").execute(num_retries=0)
                if (not isinstance(found, dict) or found.get("nextPageToken")
                        or found.get("threads") != [{"id": thread_id}]):
                    return not_latest
                result = service.users().threads().get(userId="me", id=thread_id,
                    format="metadata", metadataHeaders=["Subject", "From", "To", "Cc", "Bcc",
                        "Auto-Submitted", "X-Autoreply", "X-Autorespond"],
                    fields="id,messages(id,threadId,internalDate,labelIds,payload/headers)").execute(num_retries=0)
                snapshot = _subject_snapshot(result, thread_id)
                if not any(row["matches"] for row in snapshot.values()):
                    return not_latest
                newest = max(row["time_ms"] for row in snapshot.values())
                latest = [mid for mid, row in snapshot.items() if row["time_ms"] == newest]
                if latest != [message_id]:
                    return not_latest
                row = next(row for row in result["messages"] if row["id"] == message_id)
                headers, errors = normalizer._headers(row.get("payload"))
                subject = normalizer._one(headers, "subject", errors, required=True)
                sender = normalizer._addresses(normalizer._one(headers, "from", errors, required=True), "From", errors)
                recipients = normalizer._addresses(normalizer._one(headers, "to", errors, required=True), "To", errors)
                cc = normalizer._addresses(normalizer._one(headers, "cc", errors), "Cc", errors)
                auto = normalizer._one(headers, "auto-submitted", errors)
                try: validate_reply_recipients(recipients, cc)
                except RecipientError: return not_latest
                if (errors or subject != "Re: " + INTERNAL_SUBJECT or sender != [HAMLE]
                        or headers.get("bcc")
                        or (auto is not None and auto.casefold() != "no")
                        or headers.get("x-autoreply") or headers.get("x-autorespond")
                        or not isinstance(row.get("labelIds"), list) or "SENT" not in row["labelIds"]):
                    return not_latest
                return {"status": "latest_sent_receipt_verified", "thread_id": thread_id,
                        "message_id": message_id, "email": HAMLE, "mailbox_writes": 0,
                        "action_eligible": False}
        except ProviderError:
            raise
        except Exception:
            raise ProviderError("sent_receipt_verification_failed") from None
        finally:
            if auth_session is not None:
                auth_session.close()


def verify_basecamp_internal(token, *, session=None):
    """Five GETs; verify the existing loose/list task parent without writes."""
    token = _token(token)
    own = session is None
    session = session or _session()
    try:
        with _modules()[1]._quiet_call():
            results = []
            for path in BASECAMP_PATHS:
                raw, _ = _request(session, "GET", f"https://3.basecampapi.com/{BASECAMP_ACCOUNT}{path}",
                    headers={"Authorization": "Bearer " + token,
                             "User-Agent": "Hamle Internal Cloud Verification (ahmet@gamechangers.com.tr)"}, limit=1_048_576)
                results.append(_json(raw))
            person, project, people, todo = results
            if (not isinstance(person, dict) or person.get("id") != 53358100
                    or person.get("email_address") != HAMLE):
                raise ProviderError("basecamp_identity_mismatch")
            if (not isinstance(project, dict) or project.get("id") != BASECAMP_PROJECT
                    or project.get("name") != "Hamle — İç Deneme" or project.get("clients_enabled") is not False
                    or project.get("status") != "active"):
                raise ProviderError("basecamp_project_not_private")
            if (not isinstance(people, list) or len(people) != len(BASECAMP_MEMBERS)
                    or any(not isinstance(p, dict) or type(p.get("id")) is not int for p in people)
                    or {p["id"] for p in people} != BASECAMP_MEMBERS
                    or any(p.get("client") is not False for p in people)):
                raise ProviderError("basecamp_membership_mismatch")
            parent = todo.get("parent") if isinstance(todo, dict) else None
            if (not isinstance(todo, dict) or todo.get("id") != 10391849167
                    or not isinstance(todo.get("bucket"), dict) or todo["bucket"].get("id") != BASECAMP_PROJECT
                    or todo.get("visible_to_clients") is not False or todo.get("status") != "active"
                    or not isinstance(parent, dict) or parent.get("type") not in ("Todolist", "Todoset")
                    or type(parent.get("id")) is not int or not 0 < parent["id"] < 10**30):
                raise ProviderError("basecamp_todo_parent_unverified")
            collection = {"Todolist": "todolists", "Todoset": "todosets"}[parent["type"]]
            parent_url = f"https://3.basecampapi.com/{BASECAMP_ACCOUNT}/buckets/{BASECAMP_PROJECT}/{collection}/{parent['id']}.json"
            if parent.get("url") != parent_url:
                raise ProviderError("basecamp_todo_parent_unverified")
            raw, _ = _request(session, "GET", parent_url,
                headers={"Authorization": "Bearer " + token,
                         "User-Agent": "Hamle Internal Cloud Verification (ahmet@gamechangers.com.tr)"}, limit=1_048_576)
            target = _json(raw)
            if (not isinstance(target, dict) or target.get("id") != parent["id"]
                    or target.get("type") != parent["type"] or target.get("status") != "active"
                    or target.get("visible_to_clients") is not False
                    or target.get("bucket", {}).get("id") != BASECAMP_PROJECT
                    or target.get("todos_url") != parent_url[:-5] + "/todos.json"):
                raise ProviderError("basecamp_todo_parent_unverified")
            return {"status": "read_verified", "account_id": BASECAMP_ACCOUNT,
                    "project_id": BASECAMP_PROJECT, "person_id": 53358100,
                    "todo_parent_id": parent["id"], "todo_parent_type": parent["type"], "assignee_id": 53358100,
                    "member_ids": sorted(BASECAMP_MEMBERS), "clients_enabled": False,
                    "basecamp_content_writes": 0, "action_eligible": False}
    except ProviderError:
        raise
    except Exception:
        raise ProviderError("basecamp_read_failed") from None
    finally:
        if own:
            session.close()
