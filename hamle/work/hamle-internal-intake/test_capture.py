"""Real existing identity/normalization helpers, fake Keychain and transports."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

import capture_internal_test as app
from test_intake import message, normalizer

READER = app._reader_module()
HELPER = READER._local_module("_hamle_internal_fake_test_helper",
    Path(__file__).resolve().parent.parent / "hamle-gmail-connector" / "hamle_gmail.py")


class Credentials:
    valid = True
    granted_scopes = HELPER.SCOPES
    refresh_token = "synthetic-refresh-token"

    def to_json(self):
        return json.dumps({"client_id": "synthetic.apps.googleusercontent.com",
            "client_secret": "synthetic-secret", "refresh_token": self.refresh_token,
            "token": "synthetic-access-token", "token_uri": HELPER.TOKEN_URI,
            "scopes": list(self.granted_scopes)})


class Store:
    def __init__(self, record): self.record = record
    def load(self): return HELPER.validate_record(deepcopy(self.record))
    def save(self, record): self.record = HELPER.validate_record(record)


class Request:
    def __init__(self, response): self.response = response
    def execute(self, *, num_retries):
        if num_retries != 0: raise AssertionError("unexpected API retry")
        if isinstance(self.response, Exception): raise self.response
        return deepcopy(self.response)


class Service:
    def __init__(self, source):
        self.source = source
        self.search = {"threads": [{"id": "ab"}]}
        self.profile = {"emailAddress": app.HAMLE}
        self.calls = []
        self.closed = False
    def users(self): return self
    def threads(self): return self
    def getProfile(self, **kwargs):
        self.calls.append(("profile", kwargs)); return Request(self.profile)
    def list(self, **kwargs):
        self.calls.append(("list", kwargs)); return Request(self.search)
    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        if kwargs["format"] == "full": return Request(self.source)
        rows = [{"id": r["id"], "threadId": r["threadId"], "internalDate": r["internalDate"],
            "payload": {"headers": [h for h in r["payload"]["headers"] if h["name"].lower() == "subject"]}}
            for r in self.source["messages"]]
        return Request({"id": self.source["id"], "messages": rows})
    def close(self): self.closed = True


def raw(subject=app.SUBJECT):
    row = message(extra=[{"name": "Subject", "value": subject}])
    return {"id": "ab", "messages": [row]}


class CaptureTests(unittest.TestCase):
    def setUp(self):
        for name in ["socket", "create_connection"]:
            blocker = patch.object(socket, name, side_effect=AssertionError("network forbidden"))
            blocker.start(); self.addCleanup(blocker.stop)
        self.credentials = Credentials()
        self.store = Store(HELPER.make_record(self.credentials, HELPER.SCOPES, {"emailAddress": app.HAMLE}))
        self.discovery = Service(raw())
        self.full = Service(raw())
        self.reader_calls = []

    def read(self, tid, *, body_message_id):
        self.reader_calls.append((tid, body_message_id))
        return READER._read_verified(tid, body_message_id, helper=HELPER, normalizer=normalizer,
            store=self.store, service_factory=lambda _: self.full,
            credentials_factory=lambda _: self.credentials, request_factory=lambda: object())

    def run_capture(self):
        return app._capture_verified(helper=HELPER, store=self.store,
            service_factory=lambda _: self.discovery, read_thread=self.read, clock=lambda: 100,
            credentials_factory=lambda _: self.credentials, request_factory=lambda: object())

    def test_one_test_read_has_bounded_search_exact_subject_and_no_body_output(self):
        result = self.run_capture()
        self.assertEqual(result["status"], "review_ready")
        self.assertEqual(result["capture_observed_at"], 100)
        self.assertFalse(result["action_eligible"])
        self.assertEqual(self.reader_calls, [("ab", None)])
        params = [p for name, p in self.discovery.calls if name == "list"][0]
        self.assertEqual(params["maxResults"], 2)
        self.assertEqual(params["q"], app.QUERY)
        self.assertEqual(params["fields"], "threads/id,nextPageToken")
        self.assertTrue(self.discovery.closed and self.full.closed)
        self.assertEqual(result["mailbox_writes"], 0)

    def test_zero_multiple_paginated_or_old_search_results_refused_before_full(self):
        cases = [({}, "test_not_found"), ({"threads": [{"id": "ab"}, {"id": "ac"}]}, "test_search_ambiguous"),
                 ({"threads": [{"id": "ab"}], "nextPageToken": "next"}, "test_search_ambiguous"),
                 ({"threads": [{"id": app.OLD_THREAD}]}, "old_test_thread_forbidden")]
        for response, reason in cases:
            self.discovery.search = response
            with self.subTest(reason=reason), self.assertRaisesRegex(app.CaptureError, reason):
                self.run_capture()
        self.assertEqual(self.reader_calls, [])

    def test_subject_search_substring_is_not_exact_subject_permission(self):
        self.discovery.source = raw("Re: " + app.SUBJECT)
        with self.assertRaisesRegex(app.CaptureError, "exact_test_subject_not_found"):
            self.run_capture()
        self.assertEqual(self.reader_calls, [])

    def test_duplicate_subject_headers_refused_before_full(self):
        self.discovery.source["messages"][0]["payload"]["headers"].append({"name": "Subject", "value": app.SUBJECT})
        with self.assertRaisesRegex(app.CaptureError, "subject_missing_or_ambiguous"):
            self.run_capture()
        self.assertEqual(self.reader_calls, [])

    def test_only_old_message_has_test_subject_is_not_latest_request(self):
        later = message("2", 2000, extra=[{"name": "Subject", "value": "other"}])
        self.discovery.source["messages"].append(later)
        self.full.source = deepcopy(self.discovery.source)
        with self.assertRaisesRegex(app.CaptureError, "latest_incoming_subject_mismatch"):
            self.run_capture()

    def test_thread_changed_between_metadata_and_full_is_held(self):
        self.full.source["messages"].append(message("2", 2000, extra=[{"name": "Subject", "value": app.SUBJECT}]))
        with self.assertRaisesRegex(app.CaptureError, "thread_changed_between_reads"):
            self.run_capture()

    def test_search_match_does_not_override_actual_direct_recipient(self):
        self.full.source = raw()
        self.full.source["messages"][0]["payload"]["headers"][1]["value"] = app.AHMET
        self.discovery.source = deepcopy(self.full.source)
        result = self.run_capture()
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["reason"], "latest_direct_to_cc_excludes_hamle")
        self.assertIsNone(result["review_item"])

    def test_wrong_profile_or_scope_stops_discovery(self):
        self.discovery.profile = {"emailAddress": app.AHMET}
        with self.assertRaises(HELPER.SafeError): self.run_capture()
        self.assertFalse(any(name == "list" for name, _ in self.discovery.calls))
        self.discovery.profile = {"emailAddress": app.HAMLE}
        self.credentials.granted_scopes = ["https://mail.google.com/"]
        with self.assertRaises(HELPER.SafeError): self.run_capture()
        self.assertFalse(any(name == "list" for name, _ in self.discovery.calls))

    def test_cli_requires_explicit_flag_and_does_not_echo_provider_errors(self):
        calls = []
        with redirect_stdout(io.StringIO()):
            self.assertEqual(app.main([], _capture=lambda: calls.append(1)), 2)
        self.assertEqual(calls, [])
        output = io.StringIO()
        def failed(): raise RuntimeError("SECRET PROVIDER SENTINEL")
        with redirect_stdout(output):
            self.assertEqual(app.main(["--read-internal-test"], _capture=failed), 2)
        self.assertNotIn("SECRET PROVIDER SENTINEL", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["reason"], "internal_capture_failed")


if __name__ == "__main__":
    unittest.main()
