"""All account, OAuth and Keychain effects are faked; no live calls."""
import copy
import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from email import policy
from email.parser import BytesParser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import hamle_gmail as app

SECRET = "PLANTED_PRIVATE_VALUE_DO_NOT_PRINT"
THREAD = "1a11a20a5785feba"
TEST_ID = "GC-HAMLE-GMAIL-20261009-01"
CLIENT = {"installed": {
    "client_id": "123-example.apps.googleusercontent.com",
    "client_secret": SECRET,
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": app.TOKEN_URI,
    "redirect_uris": ["http://localhost"],
}}


class FakeCredentials:
    def __init__(self, *, granted=None, valid=True):
        self.granted_scopes = list(app.SCOPES) if granted is None else granted
        self.refresh_token = SECRET
        self.valid = valid
        self.refreshed = False

    def to_json(self):
        return json.dumps({
            "token": SECRET, "refresh_token": self.refresh_token,
            "token_uri": app.TOKEN_URI,
            "client_id": CLIENT["installed"]["client_id"],
            "client_secret": SECRET, "scopes": list(app.SCOPES),
            "expiry": "2000-01-01T00:00:00Z",
        })

    def refresh(self, request):
        self.refreshed = True
        self.valid = True


class FakeBackend:
    def __init__(self, record=None, fail_read=False, fail_write=False):
        self.raw = json.dumps(record) if record is not None else None
        self.fail_read, self.fail_write = fail_read, fail_write
        self.writes = 0

    def get_password(self, service, account):
        assert (service, account) == (app.SERVICE, app.EXPECTED_EMAIL)
        if self.fail_read:
            raise RuntimeError(SECRET)
        return self.raw

    def set_password(self, service, account, value):
        assert (service, account) == (app.SERVICE, app.EXPECTED_EMAIL)
        if self.fail_write:
            raise RuntimeError(SECRET)
        self.writes += 1
        self.raw = value


class FakeService:
    """No other mailbox/API methods exist on this fake."""
    def __init__(self, email=app.EXPECTED_EMAIL, thread=None, send_proof=None, send_error=None):
        self.email = email
        self.thread = thread or {"id": THREAD, "messages": [
            {"id": "abc123", "snippet": SECRET, "payload": {"body": SECRET}}
        ], "snippet": SECRET}
        self.calls = []
        self.pending = None
        self.closed = False
        self.send_proof = {"id": "ab12", "threadId": "cd34"} if send_proof is None else send_proof
        self.send_error = send_error

    def users(self):
        return self

    def getProfile(self, **kwargs):
        self.calls.append(("profile", kwargs))
        self.pending = {"emailAddress": self.email, "messagesTotal": 999,
                        "unexpected_private_field": SECRET}
        return self

    def threads(self):
        return self

    def messages(self):
        return self

    def send(self, **kwargs):
        self.calls.append(("send", kwargs))
        self.pending = self.send_proof
        return self

    def get(self, **kwargs):
        self.calls.append(("thread", kwargs))
        self.pending = self.thread
        return self

    def execute(self, **kwargs):
        assert kwargs == {"num_retries": 0}
        if self.calls[-1][0] == "send" and self.send_error:
            raise self.send_error
        return self.pending

    def close(self):
        self.closed = True


def record():
    return app.make_record(FakeCredentials(), app.SCOPES,
                           {"emailAddress": app.EXPECTED_EMAIL})


class Guards(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.client_file = Path(self.directory.name) / "desktop.json"
        self.client_file.write_text(json.dumps(CLIENT))
        self.client_file.chmod(0o600)
        self.backend = FakeBackend()
        self.store = app.MacKeychainStore(self.backend)

    def tearDown(self):
        self.directory.cleanup()

    def auth(self, creds=None, service=None, token_scope=None, **kwargs):
        creds = creds or FakeCredentials()
        service = service or FakeService()
        flow = Mock()
        flow.run_local_server.return_value = creds
        flow.oauth2session.token = {"scope": token_scope}
        factory = Mock()
        factory.from_client_config.return_value = flow
        result = app.authorize(self.client_file, self.store, flow_class=factory,
                               service_factory=lambda _: service, **kwargs)
        return result, factory, flow

    def read(self, service=None, creds=None, thread_id=THREAD):
        return app.read_test(thread_id, self.store,
                             service_factory=lambda _: service or FakeService(),
                             credentials_factory=lambda _: creds or FakeCredentials(),
                             request_factory=lambda: object())

    def send(self, service=None, creds=None, journal=None, test_id=TEST_ID):
        if journal is None:
            journal = app.SendAttemptJournal(Path(self.directory.name) / "attempts")
        return app.send_test(test_id, self.store, journal=journal,
                             service_factory=lambda _: service or FakeService(),
                             credentials_factory=lambda _: creds or FakeCredentials(),
                             request_factory=lambda: object())

    def test_wrong_account_never_persists_or_replaces_prior_grant(self):
        self.store.save(record())
        before = self.backend.raw
        with self.assertRaisesRegex(app.SafeError, "wrong_account"):
            self.auth(service=FakeService("ahmet@gamechangers.com.tr"))
        self.assertEqual(self.backend.writes, 1)
        self.assertEqual(self.backend.raw, before)

    def test_missing_extra_and_unknown_actual_scopes_rejected(self):
        for scopes in [[], [app.SCOPES[0]], list(app.SCOPES) + ["https://mail.google.com/"]]:
            with self.subTest(scopes=scopes), self.assertRaises(app.SafeError):
                self.auth(creds=FakeCredentials(granted=scopes))
        creds = FakeCredentials()
        creds.granted_scopes = None
        with self.assertRaisesRegex(app.SafeError, "granted_scopes_unverified"):
            self.auth(creds=creds)
        self.assertEqual(self.backend.writes, 0)

    def test_actual_token_response_scopes_supported(self):
        creds = FakeCredentials()
        creds.granted_scopes = None
        result, _, _ = self.auth(creds=creds, token_scope=" ".join(app.SCOPES))
        self.assertEqual(result["email"], app.EXPECTED_EMAIL)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_loopback_pkce_no_browser_and_bounded_timeout(self):
        _, factory, flow = self.auth(port=8765, timeout=90)
        kwargs = factory.from_client_config.call_args.kwargs
        self.assertTrue(kwargs["autogenerate_code_verifier"])
        self.assertEqual(kwargs["scopes"], list(app.SCOPES))
        options = flow.run_local_server.call_args.kwargs
        self.assertEqual(options["host"], "127.0.0.1")
        self.assertEqual(options["bind_addr"], "127.0.0.1")
        self.assertFalse(options["open_browser"])
        self.assertEqual(options["port"], 8765)
        self.assertEqual(options["timeout_seconds"], 90)
        self.assertEqual(options["include_granted_scopes"], "false")

    def test_official_library_auth_url_has_state_s256_and_exact_scopes(self):
        # Constructing an auth URL is local only: no OAuth run or HTTP request.
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_config(
            CLIENT, scopes=list(app.SCOPES), autogenerate_code_verifier=True
        )
        flow.redirect_uri = "http://127.0.0.1:8765/"
        url, state = flow.authorization_url()
        params = parse_qs(urlparse(url).query)
        self.assertTrue(state)
        self.assertEqual(params["state"], [state])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertTrue(params["code_challenge"][0])
        self.assertEqual(set(params["scope"][0].split()), set(app.SCOPES))
        self.assertNotIn(SECRET, url)

    def test_state_mismatch_rejected_by_official_oauth_session(self):
        from google_auth_oauthlib.flow import InstalledAppFlow
        from oauthlib.oauth2 import MismatchingStateError
        flow = InstalledAppFlow.from_client_config(CLIENT, scopes=list(app.SCOPES),
                                                   autogenerate_code_verifier=True)
        flow.redirect_uri = "http://127.0.0.1:8765/"
        flow.authorization_url()
        with patch.object(flow.oauth2session, "request", side_effect=AssertionError("no network")):
            with self.assertRaises(MismatchingStateError):
                flow.fetch_token(authorization_response="https://127.0.0.1:8765/?code=fake&state=wrong")

    def test_keychain_roundtrip_keeps_granted_scope_evidence(self):
        self.store.save(record())
        loaded = self.store.load()
        self.assertEqual(loaded["granted_scopes"], sorted(app.SCOPES))
        self.assertEqual(loaded["verified_email"], app.EXPECTED_EMAIL)
        self.assertEqual(json.loads(self.backend.raw)["credentials"]["refresh_token"], SECRET)

    def test_malformed_tampered_and_missing_scope_records_rejected(self):
        variants = []
        for field, value in [("verified_email", "other@example.com"), ("granted_scopes", []),
                             ("client_id", "different"), ("verified_at", SECRET)]:
            item = record(); item[field] = value; variants.append(item)
        item = record(); del item["granted_scopes"]; variants.append(item)
        for item in variants:
            with self.subTest(item=list(item)), self.assertRaises(app.SafeError):
                app.validate_record(item)
        self.backend.raw = SECRET
        with self.assertRaisesRegex(app.SafeError, "invalid_keychain_record"):
            self.store.load()

    def test_storage_read_failure_prevents_oauth(self):
        self.backend.fail_read = True
        factory = Mock()
        with self.assertRaisesRegex(app.SafeError, "keychain_read_failed"):
            app.authorize(self.client_file, self.store, flow_class=factory)
        factory.from_client_config.assert_not_called()
        self.assertEqual(self.backend.writes, 0)

    def test_storage_write_failure_has_no_plaintext_fallback(self):
        self.backend.fail_write = True
        with self.assertRaisesRegex(app.SafeError, "keychain_write_failed"):
            self.auth()
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.client_file])
        self.assertIsNone(self.backend.raw)

    def test_non_macos_and_keychain_override_fail_closed(self):
        with patch.object(app.sys, "platform", "linux"):
            with self.assertRaisesRegex(app.SafeError, "macos_keychain_required"):
                app.MacKeychainStore()
        with patch.dict(app.os.environ, {"KEYCHAIN_PATH": "/tmp/not-allowed"}):
            with self.assertRaisesRegex(app.SafeError, "macos_keychain_required"):
                app.MacKeychainStore()

    def test_desktop_client_endpoint_and_permissions_enforced(self):
        for value in [{"web": CLIENT["installed"]}, {**CLIENT, "web": {}},
                      {"installed": {**CLIENT["installed"], "token_uri": "https://bad.example/token"}}]:
            with self.assertRaises(app.SafeError):
                app.validate_client(value)
        self.client_file.chmod(0o644)
        with self.assertRaisesRegex(app.SafeError, "client_file_not_private"):
            app.read_client(self.client_file)

    def test_status_is_local_even_for_expired_access_token(self):
        self.store.save(record())
        with patch.object(app, "build_gmail", side_effect=AssertionError("no API")), \
             patch.object(app, "authorize", side_effect=AssertionError("no auth")):
            result = app.status(self.store)
        self.assertEqual(result["status"], "stored_not_revalidated")
        self.assertFalse(result["network_used"])
        self.assertTrue(result["access_token_expired"])
        self.assertNotIn(SECRET, json.dumps(result))

    def test_read_test_only_requested_thread_and_restricted_profile(self):
        self.store.save(record()); service = FakeService()
        result = self.read(service)
        self.assertEqual(service.calls, [
            ("profile", {"userId": "me", "fields": "emailAddress"}),
            ("thread", {"userId": "me", "id": THREAD, "format": "minimal",
                        "fields": "id,messages/id"}),
        ])
        self.assertEqual(result["message_ids"], ["abc123"])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertTrue(service.closed)

    def test_read_test_wrong_identity_stops_before_mail(self):
        self.store.save(record()); service = FakeService("other@example.com")
        with self.assertRaisesRegex(app.SafeError, "wrong_account"):
            self.read(service)
        self.assertEqual(len(service.calls), 1)

    def test_refresh_persists_only_to_keychain_and_changed_scope_rejected(self):
        self.store.save(record()); creds = FakeCredentials(valid=False)
        result = self.read(creds=creds)
        self.assertTrue(creds.refreshed)
        self.assertTrue(result["token_refreshed"])
        self.assertEqual(self.backend.writes, 2)
        creds = FakeCredentials(valid=False, granted=[app.SCOPES[0]])
        with self.assertRaisesRegex(app.SafeError, "scope_mismatch"):
            self.read(creds=creds)
        self.assertEqual(self.backend.writes, 2)

    def test_refresh_storage_failure_blocks_mail_read(self):
        self.store.save(record()); self.backend.fail_write = True
        service = FakeService()
        with self.assertRaisesRegex(app.SafeError, "keychain_write_failed"):
            self.read(service=service, creds=FakeCredentials(valid=False))
        self.assertEqual([call[0] for call in service.calls], ["profile"])

    def test_destructive_keyring_replace_failure_stops_without_fallback(self):
        # keyring macOS may delete an old item before adding its replacement.
        # Failure must remain a hard stop, even when reauthorization is needed.
        self.store.save(record())
        def delete_then_fail(*args):
            self.backend.raw = None
            raise RuntimeError(SECRET)
        self.backend.set_password = delete_then_fail
        service = FakeService()
        with self.assertRaisesRegex(app.SafeError, "keychain_write_failed"):
            self.read(service=service, creds=FakeCredentials(valid=False))
        self.assertIsNone(self.backend.raw)
        self.assertEqual([call[0] for call in service.calls], ["profile"])
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.client_file])

    def test_hostile_thread_proof_and_unsafe_input_rejected(self):
        self.store.save(record())
        for thread in [{"id": SECRET, "messages": []},
                       {"id": THREAD, "messages": [{"id": SECRET}]}]:
            with self.assertRaisesRegex(app.SafeError, "thread_proof_invalid"):
                self.read(FakeService(thread=thread))
        with self.assertRaisesRegex(app.SafeError, "invalid_thread_id"):
            self.read(thread_id="../" + SECRET)

    def test_cli_sanitizes_storage_api_and_argument_failures(self):
        for args, target in [(["status"], "status"), (["read-test", "--thread-id", THREAD], "read_test"),
                             (["send-test", "--test-id", TEST_ID], "send_test")]:
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), \
                 patch.object(app, target, side_effect=RuntimeError(SECRET)):
                result = app.main(args, store_factory=lambda: self.store)
            self.assertEqual(result, 2)
            self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
            self.assertIn("operation_failed", err.getvalue())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(app.main(["send", SECRET]), 2)
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        self.assertIn("invalid_arguments", err.getvalue())

    def test_send_only_fixed_internal_fixture_and_sanitized_acceptance(self):
        self.store.save(record()); service = FakeService()
        result = self.send(service)
        self.assertEqual([call[0] for call in service.calls], ["profile", "send"])
        kwargs = service.calls[1][1]
        self.assertEqual(set(kwargs), {"userId", "body", "fields"})
        self.assertEqual(kwargs["userId"], "me")
        self.assertEqual(kwargs["fields"], "id,threadId")
        self.assertEqual(set(kwargs["body"]), {"raw"})
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(kwargs["body"]["raw"]))
        app.validate_test_message(message, TEST_ID)
        self.assertEqual(message["From"], app.EXPECTED_EMAIL)
        self.assertEqual(message["To"], app.TEST_RECIPIENT)
        self.assertEqual(message.get_content(), app.TEST_BODY)
        self.assertNotIn("Cc", message)
        self.assertNotIn("Bcc", message)
        self.assertEqual(result["status"], "send_test_accepted")
        self.assertFalse(result["delivery_confirmed"])
        self.assertFalse(result["automatic_retry"])
        self.assertNotIn(SECRET, json.dumps(result))
        paths = sorted((Path(self.directory.name) / "attempts").iterdir())
        self.assertEqual(len(paths), 2)
        for path in paths:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(SECRET, path.read_text())
        self.assertTrue(service.closed)

    def test_send_wrong_identity_and_refresh_failure_prevent_claim_and_send(self):
        self.store.save(record()); service = FakeService("other@example.com")
        with self.assertRaisesRegex(app.SafeError, "wrong_account"):
            self.send(service)
        self.assertEqual([call[0] for call in service.calls], ["profile"])
        self.assertFalse((Path(self.directory.name) / "attempts").exists())
        self.backend.fail_write = True; service = FakeService()
        with self.assertRaisesRegex(app.SafeError, "keychain_write_failed"):
            self.send(service, creds=FakeCredentials(valid=False))
        self.assertEqual([call[0] for call in service.calls], ["profile"])
        self.assertFalse((Path(self.directory.name) / "attempts").exists())

    def test_send_recipient_sender_content_and_extra_headers_rejected(self):
        for header, value, code in [("To", "customer@example.com", "test_recipient_rejected"),
                                    ("From", "other@example.com", "test_sender_rejected"),
                                    ("Subject", "Customer work", "test_message_changed")]:
            message = app.fixed_test_message(TEST_ID)
            message.replace_header(header, value)
            with self.subTest(header=header), self.assertRaisesRegex(app.SafeError, code):
                app.validate_test_message(message, TEST_ID)
        message = app.fixed_test_message(TEST_ID); message["Cc"] = "customer@example.com"
        with self.assertRaisesRegex(app.SafeError, "test_message_changed"):
            app.validate_test_message(message, TEST_ID)
        message = app.fixed_test_message(TEST_ID); message.set_content("Changed body")
        with self.assertRaisesRegex(app.SafeError, "test_message_changed"):
            app.validate_test_message(message, TEST_ID)
        for option in ("--to", "--from", "--body", "--subject", "--cc", "--bcc", "--attachment"):
            with self.subTest(option=option), self.assertRaisesRegex(app.SafeError, "invalid_arguments"):
                app.parse_args(["send-test", "--test-id", TEST_ID, option, SECRET])

    def test_send_same_test_id_never_repeats_even_after_restart(self):
        self.store.save(record()); first = FakeService(); self.send(first)
        second = FakeService()
        with self.assertRaisesRegex(app.SafeError, "test_id_already_attempted"):
            self.send(second)
        self.assertEqual([call[0] for call in first.calls].count("send"), 1)
        self.assertNotIn("send", [call[0] for call in second.calls])

    def test_send_ambiguous_result_blocks_repeat_without_logging_sensitive_error(self):
        self.store.save(record())
        for index, service in enumerate([
            FakeService(send_error=TimeoutError(SECRET)),
            FakeService(send_error=KeyboardInterrupt()),
            FakeService(send_proof={"id": SECRET, "threadId": THREAD}),
            FakeService(send_proof={}),
        ], start=1):
            test_id = f"GC-HAMLE-GMAIL-20261009-{index:02}"
            with self.subTest(test_id=test_id), self.assertRaisesRegex(app.SafeError, "send_outcome_unknown_do_not_retry"):
                self.send(service, test_id=test_id)
            self.assertEqual([call[0] for call in service.calls].count("send"), 1)
            second = FakeService()
            with self.assertRaisesRegex(app.SafeError, "test_id_already_attempted"):
                self.send(second, test_id=test_id)
            self.assertNotIn("send", [call[0] for call in second.calls])

    def test_send_storage_failure_prevents_send_and_post_send_failure_stays_claimed(self):
        self.store.save(record()); service = FakeService()
        journal = Mock(); journal.claim.side_effect = app.SafeError("attempt_storage_failed")
        with self.assertRaisesRegex(app.SafeError, "attempt_storage_failed"):
            self.send(service, journal=journal)
        self.assertNotIn("send", [call[0] for call in service.calls])
        journal = app.SendAttemptJournal(Path(self.directory.name) / "attempts")
        with patch.object(journal, "accepted", side_effect=OSError(SECRET)):
            with self.assertRaisesRegex(app.SafeError, "send_outcome_unknown_do_not_retry"):
                self.send(journal=journal)
        with self.assertRaisesRegex(app.SafeError, "test_id_already_attempted"):
            self.send()

    def test_atomic_test_id_claim_rejects_concurrent_attempt_and_unsafe_ids(self):
        journal = app.SendAttemptJournal(Path(self.directory.name) / "attempts")
        def claim():
            try:
                journal.claim(TEST_ID)
                return "claimed"
            except app.SafeError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: claim(), range(2)))
        self.assertCountEqual(results, ["claimed", "test_id_already_attempted"])
        for value in ["../escape", "GC-HAMLE-GMAIL-20261009-01\r\nTo: other@example.com", SECRET]:
            with self.assertRaisesRegex(app.SafeError, "invalid_test_id"):
                self.send(test_id=value)

    def test_send_transport_disables_http_auth_and_redirect_replay(self):
        # Official auth transport is instantiated locally, without a request.
        transport = app.SingleAttemptHttp(FakeCredentials())
        self.assertEqual(transport.session._max_refresh_attempts, 0)
        self.assertFalse(transport.session.trust_env)
        self.assertEqual(transport.session.get_adapter("https://gmail.googleapis.com").max_retries.total, 0)
        response = Mock(status_code=200, headers={}, content=b'{"id":"ab12"}')
        with patch.object(transport.session, "request", return_value=response) as request:
            resp, body = transport.request("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                                           method="POST", body=b"fixed-test", headers={})
        self.assertEqual(request.call_count, 1)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertEqual(request.call_args.kwargs["timeout"], 30)
        self.assertEqual(resp.status, 200)
        self.assertEqual(body, b'{"id":"ab12"}')
        with patch.object(transport.session, "request", side_effect=TimeoutError(SECRET)) as request:
            with self.assertRaises(TimeoutError):
                transport.request("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", method="POST")
        self.assertEqual(request.call_count, 1)
        transport.close()

    def test_official_authorized_session_does_not_replay_401_post(self):
        from google.oauth2.credentials import Credentials
        from requests import Response
        transport = app.SingleAttemptHttp(Credentials(token=SECRET))
        response = Response(); response.status_code = 401; response._content = b"{}"
        with patch.object(transport.session, "send", return_value=response) as send:
            result, _ = transport.request("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                                          method="POST", body=b"fixed-test", headers={})
        self.assertEqual(result.status, 401)
        self.assertEqual(send.call_count, 1)
        self.assertFalse(send.call_args.kwargs["allow_redirects"])
        transport.close()

    def test_official_authorized_session_blocks_implicit_token_refresh_before_send(self):
        from google.oauth2.credentials import Credentials
        creds = Credentials(token=SECRET, refresh_token=SECRET, token_uri=app.TOKEN_URI,
                            client_id=CLIENT["installed"]["client_id"], client_secret=SECRET)
        creds.expiry = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        transport = app.SingleAttemptHttp(creds)
        with patch.object(transport.session, "send") as send:
            with self.assertRaisesRegex(app.SafeError, "send_transport_refresh_blocked"):
                transport.request("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                                  method="POST", body=b"fixed-test", headers={})
        send.assert_not_called()
        transport.close()


if __name__ == "__main__":
    unittest.main()
