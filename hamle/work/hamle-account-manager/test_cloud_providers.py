import base64
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
import socket
import unittest
from unittest.mock import patch

import cloud_providers as app

HELPER, READER, NORMALIZER, INTAKE = app._modules()
FAKE_TOKEN = "synthetic-placeholder-token-never-live"


class Cred:
    valid = True
    token = FAKE_TOKEN
    refresh_token = "synthetic-refresh-token"
    granted_scopes = HELPER.SCOPES
    def to_json(self):
        return json.dumps({"client_id": "synthetic.apps.googleusercontent.com", "client_secret": "synthetic",
            "refresh_token": self.refresh_token, "token": self.token, "token_uri": HELPER.TOKEN_URI,
            "scopes": list(self.granted_scopes)})


class Store:
    def __init__(self):
        self.record = HELPER.make_record(Cred(), HELPER.SCOPES, {"emailAddress": app.HAMLE})
    def load(self): return HELPER.validate_record(deepcopy(self.record))
    def save(self, value): self.record = HELPER.validate_record(deepcopy(value))


class Response:
    def __init__(self, payload, status=200, headers=None):
        self.status_code = status
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.headers = headers or {}
        self.closed = False
    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw), chunk_size): yield self.raw[start:start+chunk_size]
    def close(self): self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responses.pop(0)
        if isinstance(result, Exception): raise result
        return result
    def close(self): self.closed = True


def secret_payload(data=b"opaque-example", name=app.GMAIL_SECRET):
    return {"name": f"projects/{app.PROJECT_NUMBER}/secrets/{name}/versions/1",
            "payload": {"data": base64.b64encode(data).decode(), "dataCrc32c": str(app._crc32c(data))}}


def message(mid="a1", stamp=1000, *, sender=app.AHMET, recipient=app.HAMLE,
            subject=app.INTERNAL_SUBJECT, extra=None, text="Tek sayfalık iç eğitim rehberi hazırlayalım."):
    data = text.encode()
    return {"id": mid, "threadId": "ab", "internalDate": str(stamp), "labelIds": ["INBOX"],
            "payload": {"mimeType": "text/plain", "headers": [
                {"name": "From", "value": sender}, {"name": "To", "value": recipient},
                {"name": "Subject", "value": subject}, {"name": "Message-ID", "value": f"<{mid}@example.invalid>"},
                {"name": "Content-Type", "value": "text/plain; charset=utf-8"},
                *(extra or [])], "body": {"size": len(data), "data": base64.urlsafe_b64encode(data).decode()}}}


class ApiResult:
    def __init__(self, result): self.result = result
    def execute(self, *, num_retries):
        assert num_retries == 0
        return deepcopy(self.result)


class Gmail:
    def __init__(self, rows=None):
        self.rows = rows or [message()]
        self.profile = {"emailAddress": app.HAMLE}
        self.search = {"threads": [{"id": "ab"}]}
        self.calls = []
        self.full_count = 0
        self.mutate_second_full = False
    def users(self): return self
    def threads(self): return self
    def getProfile(self, **kwargs):
        self.calls.append(("profile", kwargs)); return ApiResult(self.profile)
    def list(self, **kwargs):
        self.calls.append(("list", kwargs)); return ApiResult(self.search)
    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        if kwargs["format"] == "metadata":
            rows = [{"id": r["id"], "threadId": r["threadId"], "internalDate": r["internalDate"],
                "labelIds": r.get("labelIds", []),
                "payload": {"headers": [h for h in r["payload"]["headers"]
                    if h["name"].lower() in [name.lower() for name in kwargs["metadataHeaders"]]]}}
                for r in self.rows]
        else:
            self.full_count += 1
            rows = deepcopy(self.rows)
            if self.full_count == 2 and self.mutate_second_full:
                rows.append(message("a2", 2000))
        return ApiResult({"id": self.rows[0]["threadId"], "messages": rows})
    def close(self): pass


class ProviderTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
    def error(self, code, function):
        with self.assertRaises(app.ProviderError) as got: function()
        self.assertEqual(got.exception.code, code)
        self.assertFalse(got.exception.retry_allowed)
        self.assertNotIn(FAKE_TOKEN, str(got.exception))
    def capture(self, gmail):
        return app.capture_internal_request(Store(), allow_body_for_internal_request=True,
            _service_factory=lambda _: gmail, _credentials_factory=lambda _: Cred(),
            _request_factory=lambda: object(), clock=lambda: 100)

    def test_secret_read_is_fixed_project_checked_crc_and_one_request(self):
        session = Session(Response(secret_payload()))
        secrets = app.GoogleSecretManager(credentials=Cred(), session=session)
        self.assertEqual(secrets.access(app.GMAIL_SECRET), b"opaque-example")
        method, url, args = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, f"https://secretmanager.googleapis.com/v1/projects/{app.PROJECT}/secrets/{app.GMAIL_SECRET}/versions/latest:access")
        self.assertFalse(args["allow_redirects"])
        self.assertEqual(args["timeout"], (10, 30))
        self.assertEqual(len(session.calls), 1)

    def test_secret_allowlist_blocks_arbitrary_paths(self):
        session = Session()
        for name in ("other", "../hamle-gmail-oauth", "projects/other/secrets/hamle-gmail-oauth"):
            self.error("secret_not_allowed", lambda: app.GoogleSecretManager(credentials=Cred(), session=session).access(name))
        self.assertEqual(session.calls, [])

    def test_wrong_secret_project_or_crc_refused(self):
        cases = []
        value = secret_payload(); value["name"] = value["name"].replace(app.PROJECT_NUMBER, "other"); cases.append((value, "secret_version_mismatch"))
        value = secret_payload(); value["payload"]["dataCrc32c"] = "1"; cases.append((value, "secret_payload_invalid"))
        value = secret_payload(); value["payload"].pop("dataCrc32c"); cases.append((value, "secret_payload_invalid"))
        for value, code in cases:
            self.error(code, lambda: app.GoogleSecretManager(credentials=Cred(), session=Session(Response(value))).access(app.GMAIL_SECRET))

    def test_crc32c_known_vector(self):
        self.assertEqual(app._crc32c(b"123456789"), 0xe3069283)

    def test_only_gmail_record_can_be_versioned_with_checksum_confirmation(self):
        result = {"name": f"projects/{app.PROJECT}/secrets/{app.GMAIL_SECRET}/versions/2",
                  "state": "ENABLED", "clientSpecifiedPayloadChecksum": True}
        session = Session(Response(result))
        manager = app.GoogleSecretManager(credentials=Cred(), session=session)
        self.assertTrue(manager.add_gmail_version(Store().record).endswith("/versions/2"))
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/hamle-gmail-oauth:addVersion"))
        payload = json.loads(kwargs["data"])["payload"]
        self.assertEqual(int(payload["dataCrc32c"]), app._crc32c(base64.b64decode(payload["data"])))
        self.error("gmail_record_invalid", lambda: manager.add_gmail_version({"token": FAKE_TOKEN}))
        self.assertEqual(len(session.calls), 1)

    def test_uncertain_secret_write_not_repeated(self):
        session = Session(RuntimeError(FAKE_TOKEN))
        manager = app.GoogleSecretManager(credentials=Cred(), session=session)
        self.error("provider_outcome_unknown", lambda: manager.add_gmail_version(Store().record))
        self.assertEqual(len(session.calls), 1)

    def test_http_error_has_no_raw_body_or_redirect_retry(self):
        for status in (301, 401, 429, 503):
            session = Session(Response(FAKE_TOKEN.encode(), status=status))
            self.error("provider_request_rejected", lambda: app.GoogleSecretManager(credentials=Cred(), session=session).access(app.GMAIL_SECRET))
            self.assertEqual(len(session.calls), 1)

    def test_gmail_store_uses_original_record_scope_validation(self):
        class Secrets:
            def access(self, name): return json.dumps(Store().record).encode()
        self.assertEqual(app.GmailSecretStore(Secrets()).load()["verified_email"], app.HAMLE)
        class WrongSecrets:
            def access(self, name):
                record = Store().record; record["granted_scopes"] = ["https://mail.google.com/"]
                return json.dumps(record).encode()
        self.error("gmail_record_invalid", lambda: app.GmailSecretStore(WrongSecrets()).load())

    def test_openai_accessor_only_returns_memory_and_fixed_name(self):
        class Secrets:
            def access(self, name):
                assert name == app.OPENAI_SECRET
                return FAKE_TOKEN.encode()
        captured = io.StringIO()
        with redirect_stdout(captured), redirect_stderr(captured):
            self.assertEqual(app.openai_key(Secrets()), FAKE_TOKEN)
        self.assertEqual(captured.getvalue(), "")

    def test_capture_requires_explicit_body_flag_before_any_io(self):
        self.error("explicit_internal_body_flag_required", lambda: app.capture_internal_request(None))

    def test_identity_only_verification_never_reads_mail(self):
        gmail = Gmail()
        result = app.verify_gmail_identity(Store(), _service_factory=lambda _: gmail,
            _credentials_factory=lambda _: Cred(), _request_factory=lambda: object())
        self.assertEqual(result["email"], app.HAMLE)
        self.assertEqual(result["mailbox_reads"], 0)
        self.assertEqual([name for name, _ in gmail.calls], ["profile"])

    def test_refreshed_record_saved_before_mail_reads(self):
        class Expired(Cred):
            valid = False
            def refresh(self, request):
                self.valid = True
                self.token = "synthetic-refreshed-token-not-real"
        credential, gmail, store = Expired(), Gmail(), Store()
        saved = []
        original_save = store.save
        def save(record):
            self.assertFalse(any(name == "list" for name, _ in gmail.calls))
            saved.append(record); original_save(record)
        store.save = save
        app.capture_internal_request(store, allow_body_for_internal_request=True,
            _service_factory=lambda _: gmail, _credentials_factory=lambda _: credential,
            _request_factory=lambda: object())
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["credentials"]["token"], credential.token)

    def test_failed_refresh_persistence_stops_before_mail_reads(self):
        class Expired(Cred):
            valid = False
            def refresh(self, request): self.valid = True
        credential, gmail, store = Expired(), Gmail(), Store()
        def fail(record): raise app.ProviderError("secret_write_unconfirmed")
        store.save = fail
        self.error("secret_write_unconfirmed", lambda: app.capture_internal_request(store,
            allow_body_for_internal_request=True, _service_factory=lambda _: gmail,
            _credentials_factory=lambda _: credential, _request_factory=lambda: object()))
        self.assertFalse(any(name == "list" for name, _ in gmail.calls))

    def test_auth_transport_rejects_other_host_and_token_replay(self):
        with patch("google.auth.transport.requests.Request") as ctor:
            request = app._GoogleRequest(Session(), gmail=True)
            request("https://oauth2.googleapis.com/token", method="POST")
            self.error("auth_transport_scope_or_retry_blocked", lambda: request("https://oauth2.googleapis.com/token", method="POST"))
            self.error("auth_transport_scope_or_retry_blocked", lambda: request("https://evil.example/token", method="POST"))
            self.assertEqual(ctor.return_value.call_count, 1)

    def test_real_adc_discovery_project_and_refresh_with_slashless_metadata_ping(self):
        # Exercise installed google-auth discovery, not a mocked default() result.
        import google.auth._default as defaults
        from urllib.parse import urlsplit
        class MetadataSession:
            def __init__(self): self.calls = []
            def close(self): pass
            def request(inner, method, url, **kwargs):
                inner.calls.append((method, url))
                path = urlsplit(url).path
                if url == "http://169.254.169.254":
                    data, content_type = b"", "text/plain"
                elif path.endswith("/project/project-id"):
                    data, content_type = app.PROJECT.encode(), "text/plain"
                elif path.endswith("/service-accounts/default/"):
                    data = json.dumps({"email": "hamle-agent@gamechangers-hamle.iam.gserviceaccount.com",
                                       "scopes": ["https://www.googleapis.com/auth/cloud-platform"], "aliases": ["default"]}).encode()
                    content_type = "application/json"
                elif path.endswith("/service-accounts/default/token"):
                    data = json.dumps({"access_token": FAKE_TOKEN, "expires_in": 3600, "token_type": "Bearer"}).encode()
                    content_type = "application/json"
                else: raise AssertionError("Unexpected metadata path")
                response = app.requests.Response()
                response.status_code = 200; response._content = data; response._content_consumed = True
                response.headers = {"metadata-flavor": "Google", "content-type": content_type}
                return response
        session = MetadataSession()
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(defaults, "_get_explicit_environ_credentials", return_value=(None, None)), \
             patch.object(defaults, "_get_gcloud_sdk_credentials", return_value=(None, None)), \
             patch.object(defaults, "_get_gae_credentials", return_value=(None, None)):
            manager = app.GoogleSecretManager(session=session)
            headers = manager._headers()
        self.assertEqual(headers["Authorization"], "Bearer " + FAKE_TOKEN)
        self.assertEqual(session.calls[0], ("GET", "http://169.254.169.254"))
        self.assertEqual(len(session.calls), 4)
        self.assertTrue(all(method == "GET" for method, _ in session.calls))

    def test_metadata_probe_preserves_exact_host_and_method_limits(self):
        with patch("google.auth.transport.requests.Request") as ctor:
            request = app._GoogleRequest(Session())
            request("http://169.254.169.254")
            request("http://169.254.169.254/")
            for url, method in (("http://169.254.169.254", "POST"), ("http://metadata.google.internal", "GET"),
                                ("http://169.254.169.253", "GET"), ("https://169.254.169.254", "GET")):
                self.error("auth_transport_scope_or_retry_blocked", lambda: request(url, method=method))
            self.assertEqual(ctor.return_value.call_count, 2)

    def test_secret_error_fixed_code_survives_gmail_wrappers(self):
        class RejectedSecrets:
            def access(self, name): raise app.ProviderError("provider_request_rejected")
        store = app.GmailSecretStore(RejectedSecrets())
        self.error("provider_request_rejected", lambda: app.verify_gmail_identity(store))

    def test_session_forces_redirects_off_even_for_google_auth_request(self):
        with patch.object(app.requests.Session, "request", return_value=object()) as dispatch:
            session = app._session()
            try:
                session.request("POST", "https://oauth2.googleapis.com/token", allow_redirects=True, timeout=120)
                self.assertFalse(session.trust_env)
                self.assertFalse(dispatch.call_args.kwargs["allow_redirects"])
                self.assertEqual(dispatch.call_args.kwargs["timeout"], (10, 30))
                self.assertEqual(session.get_adapter("https://").max_retries.total, 0)
            finally:
                session.close()

    def test_capture_new_exact_internal_message_no_keychain_or_writes(self):
        gmail = Gmail()
        with patch.object(HELPER, "MacKeychainStore", side_effect=AssertionError("Keychain forbidden")):
            result = self.capture(gmail)
        self.assertIn("rehberi", result["source_text"])
        self.assertNotIn("source_text", result["evidence"])
        self.assertFalse(result["evidence"]["action_eligible"])

        self.assertFalse(result["evidence"]["unseen_cc_removal_resolved"])
        self.assertEqual(result["evidence"]["captured_at"], "1970-01-01T00:01:40+00:00")
        self.assertEqual(result["evidence"]["in_reply_to"], "<a1@example.invalid>")
        self.assertEqual(result["evidence"]["references"], "<a1@example.invalid>")
        self.assertEqual(gmail.full_count, 2)
        search = next(k for n, k in gmail.calls if n == "list")
        self.assertEqual(search["maxResults"], 2)
        self.assertEqual(search["q"], app.GMAIL_QUERY)

    def sent_receipt(self, gmail, mid="a2"):
        return app.verify_latest_sent_receipt(Store(), "ab", mid,
            _service_factory=lambda _: gmail, _credentials_factory=lambda _: Cred(),
            _request_factory=lambda: object())

    def test_latest_sent_receipt_metadata_only_with_exact_internal_envelope(self):
        sent = message("a2", 2000, sender=app.HAMLE, recipient=app.AHMET, subject="Re: " + app.INTERNAL_SUBJECT)
        sent["labelIds"] = ["SENT"]
        gmail = Gmail([message(), sent])
        result = self.sent_receipt(gmail)
        self.assertEqual(result, {"status": "latest_sent_receipt_verified", "thread_id": "ab", "message_id": "a2",
                                  "email": app.HAMLE, "mailbox_writes": 0, "action_eligible": False})
        self.assertEqual(gmail.full_count, 0)
        self.assertEqual([name for name, _ in gmail.calls], ["profile", "list", "get"])
        self.assertEqual(gmail.calls[-1][1]["format"], "metadata")
        self.assertNotIn("body", gmail.calls[-1][1]["fields"])
        sent["payload"]["headers"].append({"name": "Cc", "value": "Cansu <cansu@gamechangers.com.tr>"})
        result = self.sent_receipt(Gmail([message(), sent]))
        self.assertEqual(result["status"], "latest_sent_receipt_verified")

    def test_new_incoming_or_different_sent_receipt_does_not_count_as_done(self):
        sent = message("a2", 2000, sender=app.HAMLE, recipient=app.AHMET, subject="Re: " + app.INTERNAL_SUBJECT)
        sent["labelIds"] = ["SENT"]
        for gmail, mid in ((Gmail([message(), sent, message("a3", 3000)]), "a2"),
                           (Gmail([message(), sent]), "a3"),
                           (Gmail([message(stamp=2000), sent]), "a2")):
            self.assertEqual(self.sent_receipt(gmail, mid), {"status": "not_latest_sent_receipt"})

    def test_wrong_sent_envelope_label_or_account_never_silently_skips(self):
        for field, value in (("From", app.AHMET), ("To", "other@example.invalid"), ("Subject", "Other"),
                             ("Cc", app.AHMET), ("Auto-Submitted", "auto-replied")):
            sent = message("a2", 2000, sender=app.HAMLE, recipient=app.AHMET, subject="Re: " + app.INTERNAL_SUBJECT)
            sent["labelIds"] = ["SENT"]
            sent["payload"]["headers"] = [h for h in sent["payload"]["headers"] if h["name"] != field]
            sent["payload"]["headers"].append({"name": field, "value": value})
            self.assertEqual(self.sent_receipt(Gmail([message(), sent])), {"status": "not_latest_sent_receipt"})
        sent["labelIds"] = ["INBOX"]
        self.assertEqual(self.sent_receipt(Gmail([message(), sent])), {"status": "not_latest_sent_receipt"})
        gmail = Gmail(); gmail.profile = {"emailAddress": app.AHMET}
        self.error("sent_receipt_verification_failed", lambda: self.sent_receipt(gmail))

    def test_capture_refuses_zero_ambiguous_and_historical_threads(self):
        for value, code in (({}, "internal_request_not_found"),
                            ({"threads": [{"id": "ab"}, {"id": "ac"}]}, "internal_search_ambiguous"),
                            ({"threads": [{"id": "ab"}], "nextPageToken": "next"}, "internal_search_ambiguous")):
            gmail = Gmail(); gmail.search = value
            self.error(code, lambda: self.capture(gmail)); self.assertEqual(gmail.full_count, 0)
        for tid in app.FORBIDDEN_THREADS:
            gmail = Gmail(); gmail.search = {"threads": [{"id": tid}]}
            self.error("historical_test_forbidden", lambda: self.capture(gmail))

    def test_search_subject_is_not_permission_for_substring(self):
        gmail = Gmail([message(subject="Re: " + app.INTERNAL_SUBJECT)])
        self.error("exact_internal_subject_missing", lambda: self.capture(gmail))
        self.assertEqual(gmail.full_count, 0)

    def test_real_latest_to_cc_sender_auto_and_self_are_gates(self):
        cases = [message(recipient=app.AHMET), message(sender="other@gamechangers.com.tr"),
                 message(extra=[{"name": "Auto-Submitted", "value": "auto-replied"}])]
        for row in cases:
            gmail = Gmail([row])
            self.error("latest_internal_request_not_eligible", lambda: self.capture(gmail))
            self.assertEqual(gmail.full_count, 1)
        gmail = Gmail([message(), message("a2", 2000, sender=app.HAMLE, recipient=app.AHMET)])
        self.error("latest_internal_request_not_eligible", lambda: self.capture(gmail))

    def test_body_recheck_rejects_newer_incoming_message(self):
        gmail = Gmail(); gmail.mutate_second_full = True
        self.error("thread_changed_before_body", lambda: self.capture(gmail))

    def test_reply_rfc_headers_must_be_real_single_headers_and_valid(self):
        row = message()
        row["payload"]["headers"] = [h for h in row["payload"]["headers"] if h["name"] != "Message-ID"]
        self.error("internal_reply_headers_missing_or_invalid", lambda: self.capture(Gmail([row])))
        for header, code in (({"name": "Message-ID", "value": "<other@example.invalid>"}, "internal_reply_headers_missing_or_invalid"),
                             ({"name": "References", "value": "<a@example.invalid>\r\nBcc: bad@example.invalid"}, "internal_capture_failed")):
            self.error(code, lambda: self.capture(Gmail([message(extra=[header])])))

    def test_body_html_only_oversize_and_attachment_rejected(self):
        row = message(); row["payload"]["mimeType"] = "text/html"; row["payload"]["headers"][-1]["value"] = "text/html"
        self.error("internal_plaintext_body_required", lambda: self.capture(Gmail([row])))
        self.error("internal_plaintext_body_required", lambda: self.capture(Gmail([message(text="x" * 12001)])))
        row = message(); row["payload"]["filename"] = "attachment.txt"
        self.error("internal_attachments_not_accepted", lambda: self.capture(Gmail([row])))

    def test_gmail_transport_refuses_send_and_arbitrary_url(self):
        with patch.object(app, "_session", return_value=Session()):
            http = app._ReadOnlyGmailHttp(Cred())
            for uri, method in (("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", "POST"),
                                ("https://evil.example/gmail/v1/users/me/profile", "GET"),
                                ("https://gmail.googleapis.com/gmail/v1/users/other/profile", "GET")):
                self.error("gmail_read_path_forbidden", lambda: http.request(uri, method=method))

    def test_basecamp_reads_fixed_project_identity_and_private_members(self):
        session = self.basecamp_session()
        result = app.verify_basecamp_internal(FAKE_TOKEN, session=session)
        self.assertFalse(result["action_eligible"])
        self.assertEqual(result["basecamp_content_writes"], 0)
        self.assertEqual([m for m, _, _ in session.calls], ["GET"] * 5)
        self.assertEqual(result["todo_parent_id"], 123)
        self.assertTrue(all(u.startswith("https://3.basecampapi.com/5898865/") for _, u, _ in session.calls))

    def basecamp_session(self, *, wrong_person=False, clients=False, extra=False):
        return Session(Response({"id": 1 if wrong_person else 53358100, "email_address": app.HAMLE}),
            Response({"id": 49184053, "name": "Hamle — İç Deneme", "clients_enabled": clients, "status": "active"}),
            Response([{"id": x, "client": False} for x in ([47470759, 53358100, 1] if extra else [47470759, 53358100])]),
            Response({"id": 10391849167, "bucket": {"id": 49184053}, "visible_to_clients": False,
                      "status": "active", "parent": {"id": 123, "type": "Todolist",
                         "url": "https://3.basecampapi.com/5898865/buckets/49184053/todolists/123.json"}}),
            Response({"id": 123, "type": "Todolist", "status": "active", "visible_to_clients": False, "bucket": {"id": 49184053},
                      "todos_url": "https://3.basecampapi.com/5898865/buckets/49184053/todolists/123/todos.json"}))

    def test_basecamp_wrong_identity_clients_or_members_fail_closed(self):
        for kwargs, code in (({"wrong_person": True}, "basecamp_identity_mismatch"),
                             ({"clients": True}, "basecamp_project_not_private"),
                             ({"extra": True}, "basecamp_membership_mismatch")):
            self.error(code, lambda: app.verify_basecamp_internal(FAKE_TOKEN, session=self.basecamp_session(**kwargs)))

    def test_existing_loose_todo_verifies_private_todoset_without_creating_list(self):
        session = self.basecamp_session()
        for index in (3, 4):
            session.responses[index].raw = session.responses[index].raw.replace(b"Todolist", b"Todoset").replace(b"todolists", b"todosets")
        result = app.verify_basecamp_internal(FAKE_TOKEN, session=session)
        self.assertEqual(result["todo_parent_type"], "Todoset")
        self.assertEqual(session.calls[-1][1], "https://3.basecampapi.com/5898865/buckets/49184053/todosets/123.json")
        self.assertEqual([method for method, _, _ in session.calls], ["GET"] * 5)

    def continuation_gmail(self, *, newer=None, extra=None):
        root = message()
        sent = message("b1", 2000, sender=app.HAMLE, recipient=app.AHMET, subject="Re: " + app.INTERNAL_SUBJECT)
        sent["labelIds"] = ["SENT"]
        follow = message("b2", 3000, subject="Re: " + app.INTERNAL_SUBJECT, extra=extra or [
            {"name": "References", "value": "<a1@example.invalid> <b1@example.invalid>"},
            {"name": "In-Reply-To", "value": "<b1@example.invalid>"}])
        rows = [root, sent, follow] + ([newer] if newer else [])
        for row in rows: row["threadId"] = app.CONTINUATION_THREAD
        gmail = Gmail(rows); gmail.search = {"threads": [{"id": app.CONTINUATION_THREAD}]}
        return gmail

    def capture_continuation(self, gmail, binding=None):
        return app.capture_internal_request(Store(), allow_body_for_internal_request=True,
            continuation=binding or {"thread_id": app.CONTINUATION_THREAD, "root_message_id": "a1",
                                     "root_rfc_message_id": "<a1@example.invalid>"},
            _service_factory=lambda _: gmail, _credentials_factory=lambda _: Cred(),
            _request_factory=lambda: object(), clock=lambda: 100)

    def test_continuation_uses_real_root_and_reply_headers_and_latest_to_cc(self):
        result = self.capture_continuation(self.continuation_gmail())
        e = result["evidence"]
        self.assertTrue(e["is_continuation"])
        self.assertEqual(e["root_message_id"], "a1")
        self.assertEqual(e["root_rfc_message_id"], "<a1@example.invalid>")
        self.assertEqual(e["source_subject"], "Re: " + app.INTERNAL_SUBJECT)
        self.assertEqual(e["in_reply_to"], "<b2@example.invalid>")
        self.assertEqual(e["references"], "<a1@example.invalid> <b1@example.invalid> <b2@example.invalid>")
        self.assertEqual(e["latest_to"], [app.HAMLE])
        self.assertEqual(e["latest_cc"], [])
        self.assertFalse(e["action_eligible"])

    def test_continuation_recipient_evidence_uses_only_latest_real_headers(self):
        from recipient_policy import derive_reply_recipients
        gmail = self.continuation_gmail()
        gmail.rows[0]["payload"]["headers"].append({"name": "Cc", "value": "old@example.invalid"})
        gmail.rows[-1]["payload"]["headers"].append({"name": "Cc", "value": "Cansu <cansu@gamechangers.com.tr>"})
        result = self.capture_continuation(gmail)
        self.assertEqual(result["evidence"]["latest_cc"], ["cansu@gamechangers.com.tr"])
        self.assertEqual(derive_reply_recipients(result["evidence"]["latest_to"], result["evidence"]["latest_cc"]),
                         {"to": [app.AHMET], "cc": ["cansu@gamechangers.com.tr"]})
        gmail.rows[0]["payload"]["headers"][-1] = {"name": "Cc", "value": "cansu@gamechangers.com.tr"}
        gmail.rows[-1]["payload"]["headers"] = [h for h in gmail.rows[-1]["payload"]["headers"] if h["name"] != "Cc"]
        result = self.capture_continuation(gmail)
        self.assertEqual(derive_reply_recipients(result["evidence"]["latest_to"], result["evidence"]["latest_cc"]),
                         {"to": [app.AHMET], "cc": []})

    def test_continuation_missing_root_unknown_parent_or_quoted_refs_are_rejected(self):
        for headers in ([{"name": "References", "value": "<b1@example.invalid>"},
                         {"name": "In-Reply-To", "value": "<b1@example.invalid>"}],
                        [{"name": "References", "value": "<a1@example.invalid> <other@example.invalid>"},
                         {"name": "In-Reply-To", "value": "<other@example.invalid>"}],
                        [{"name": "References", "value": "<a1@example.invalid>"}]):
            self.error("continuation_reply_chain_invalid", lambda: self.capture_continuation(self.continuation_gmail(extra=headers)))
        binding = {"thread_id": app.CONTINUATION_THREAD, "root_message_id": "a1", "root_rfc_message_id": "<wrong@example.invalid>"}
        self.error("continuation_root_mismatch", lambda: self.capture_continuation(self.continuation_gmail(), binding))

    def test_continuation_rejects_newest_self_new_sender_and_cc_removal(self):
        for sender, recipient, extra in ((app.HAMLE, app.AHMET, None), ("other@example.invalid", app.HAMLE, None),
                                         (app.AHMET, "other@example.invalid", None),
                                         (app.AHMET, app.HAMLE, [{"name": "Auto-Submitted", "value": "auto-replied"}])):
            latest = message("c1", 4000, sender=sender, recipient=recipient, subject="Re: " + app.INTERNAL_SUBJECT, extra=extra)
            self.error("latest_internal_request_not_eligible", lambda: self.capture_continuation(self.continuation_gmail(newer=latest)))

    def test_continuation_foreign_thread_binding_cannot_widen_mail_access(self):
        gmail = self.continuation_gmail()
        binding = {"thread_id": "abc", "root_message_id": "a1", "root_rfc_message_id": "<a1@example.invalid>"}
        self.error("continuation_binding_invalid", lambda: self.capture_continuation(gmail, binding))
        self.assertEqual(gmail.calls, [])

    def test_parent_url_redirect_or_private_state_mismatch_fails_closed(self):
        session = self.basecamp_session()
        session.responses[3].raw = session.responses[3].raw.replace(b"https://3.basecampapi.com", b"https://evil.example")
        self.error("basecamp_todo_parent_unverified", lambda: app.verify_basecamp_internal(FAKE_TOKEN, session=session))
        self.assertEqual(len(session.calls), 4)
        session = self.basecamp_session()
        target = json.loads(session.responses[-1].raw); target["visible_to_clients"] = True
        session.responses[-1] = Response(target)
        self.error("basecamp_todo_parent_unverified", lambda: app.verify_basecamp_internal(FAKE_TOKEN, session=session))

    def test_basecamp_pagination_is_not_silently_truncated(self):
        session = self.basecamp_session()
        session.responses[2].headers = {"Link": '<https://other.example/page2>; rel="next"'}
        self.error("provider_pagination_unhandled", lambda: app.verify_basecamp_internal(FAKE_TOKEN, session=session))
        self.assertEqual(len(session.calls), 3)


if __name__ == "__main__": unittest.main()
