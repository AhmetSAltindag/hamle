"""HTTP security boundary tests; no real store, messages or remote connections."""

import copy
import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest

from server import COOKIE_NAME, TEST_SUBJECT, create_server, public_projection


class FakeStore:
    def __init__(self):
        self.calls = []
        self.state = {
            "test_subject": TEST_SUBJECT,
            "demo_only": True,
            "external_actions_enabled": False,
            "control": {"mode": "review_open", "generation": 1, "changed_at": 1},
            "items": [{
                "review_id": "private-review", "thread_id": "private-thread",
                "message_id": "private-message", "capture_fingerprint": "private-fingerprint",
                "body": "must not escape", "status": "awaiting_human_review",
                "action_eligible": False,
            }],
        }

    def public_state(self):
        return copy.deepcopy(self.state)

    def stop(self):
        self.calls.append("stop")
        self.state["control"].update(mode="stopped", generation=2)
        self.state["items"][0]["status"] = "cancelled"
        return self.public_state()

    def takeover(self):
        self.calls.append("takeover")
        self.state["control"].update(mode="takeover", generation=2)
        self.state["items"][0]["status"] = "cancelled"
        return self.public_state()


class ReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.server = create_server(self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        code, headers, body = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', body.decode()).group(1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def post_headers(self):
        return {"Cookie": self.cookie, "Origin": self.server.origin,
                "X-CSRF-Token": self.csrf, "Content-Type": "application/json"}

    def post(self, body='{"action":"stop"}', headers=None):
        return self.request("POST", "/api/control", body, self.post_headers() if headers is None else headers)

    def test_binds_only_loopback_and_sets_defensive_headers(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        code, headers, body = self.request("GET", "/")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store, max-age=0")
        self.assertNotIn(b"__SESSION_CSRF__", body)

    def test_safe_state_projection_hides_private_fields(self):
        code, _, body = self.request("GET", "/api/state", headers={"Cookie": self.cookie})
        self.assertEqual(code, 200)
        state = json.loads(body)
        self.assertFalse(state["external_actions_enabled"])
        self.assertEqual(state["item_count"], 1)
        for private in (b"private", b"must not escape", b"changed_at", b"review_id"):
            self.assertNotIn(private, body)

    def test_state_requires_session(self):
        self.assertEqual(self.request("GET", "/api/state")[0], 403)

    def test_valid_stop_records_cancelled_state(self):
        code, _, body = self.post()
        self.assertEqual(code, 200)
        self.assertEqual(self.store.calls, ["stop"])
        self.assertEqual(json.loads(body)["item"]["status"], "cancelled")

    def test_valid_takeover_records_cancelled_state(self):
        code, _, body = self.post('{"action":"takeover"}')
        self.assertEqual(code, 200)
        self.assertEqual(self.store.calls, ["takeover"])
        self.assertEqual(json.loads(body)["control"]["mode"], "takeover")

    def test_get_and_other_methods_do_not_mutate(self):
        self.assertEqual(self.request("GET", "/api/control")[0], 405)
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            self.assertEqual(self.request(method, "/api/control")[0], 405)
        self.assertEqual(self.store.calls, [])

    def test_wrong_or_missing_origin_does_not_mutate(self):
        for origin in (None, "null", "https://example.org", "http://localhost:1234"):
            with self.subTest(origin=origin):
                headers = self.post_headers()
                if origin is None:
                    del headers["Origin"]
                else:
                    headers["Origin"] = origin
                self.assertEqual(self.post(headers=headers)[0], 403)
        self.assertEqual(self.store.calls, [])

    def test_wrong_host_blocks_both_reads_and_mutations(self):
        for host in ("example.org", "localhost", "127.0.0.1:1", self.server.authority + ".example.org"):
            with self.subTest(host=host):
                headers = self.post_headers()
                headers["Host"] = host
                self.assertEqual(self.post(headers=headers)[0], 403)
                self.assertEqual(self.request("GET", "/", headers={"Host": host})[0], 403)
        self.assertEqual(self.store.calls, [])

    def test_duplicate_host_or_origin_does_not_mutate(self):
        for duplicate in ("Host", "Origin"):
            conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=2)
            try:
                conn.putrequest("POST", "/api/control", skip_host=True)
                conn.putheader("Host", self.server.authority)
                for key, value in self.post_headers().items():
                    conn.putheader(key, value)
                conn.putheader(duplicate, self.server.authority if duplicate == "Host" else self.server.origin)
                body = b'{"action":"stop"}'
                conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                result = conn.getresponse()
                self.assertEqual(result.status, 403)
                result.read()
            finally:
                conn.close()
        self.assertEqual(self.store.calls, [])

    def test_missing_wrong_or_nonascii_csrf_does_not_mutate(self):
        for token in (None, "wrong", "\xe9"):
            headers = self.post_headers()
            if token is None:
                del headers["X-CSRF-Token"]
            else:
                headers["X-CSRF-Token"] = token
            self.assertEqual(self.post(headers=headers)[0], 403)
        self.assertEqual(self.store.calls, [])

    def test_wrong_or_missing_cookie_does_not_mutate(self):
        for cookie in (None, COOKIE_NAME + "=wrong"):
            headers = self.post_headers()
            if cookie is None:
                del headers["Cookie"]
            else:
                headers["Cookie"] = cookie
            self.assertEqual(self.post(headers=headers)[0], 403)
        self.assertEqual(self.store.calls, [])

    def test_unknown_action_extra_ids_and_duplicate_keys_rejected(self):
        invalid = ('{"action":"resume"}', '{"action":"stop","review_id":"other"}',
                   '{"action":"stop","action":"takeover"}', '[]', 'null',
                   '{"action":{"run":"stop"}}', '{"action":["stop"]}')
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.post(body)[0], 400)
        self.assertEqual(self.store.calls, [])

    def test_body_limit_media_type_and_cross_site_rejected(self):
        self.assertEqual(self.post("x" * 257)[0], 413)
        headers = self.post_headers()
        headers["Content-Type"] = "text/plain"
        self.assertEqual(self.post(headers=headers)[0], 415)
        headers = self.post_headers()
        headers["Sec-Fetch-Site"] = "cross-site"
        self.assertEqual(self.post(headers=headers)[0], 403)
        self.assertEqual(self.store.calls, [])

    def test_unknown_path_does_not_mutate(self):
        self.assertEqual(self.request("POST", "/api/resume", '{"action":"stop"}', self.post_headers())[0], 404)
        self.assertEqual(self.request("GET", "/../store.py")[0], 404)
        self.assertEqual(self.store.calls, [])

    def test_execution_or_scope_mismatch_refuses_projection(self):
        for field, value in (("external_actions_enabled", True), ("demo_only", False), ("test_subject", "customer")):
            state = self.store.public_state()
            state[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                public_projection(state)
        state = self.store.public_state()
        state["items"].append(copy.deepcopy(state["items"][0]))
        with self.assertRaises(ValueError):
            public_projection(state)

    def test_real_store_takeover_persists_cancelled_without_execution_authority(self):
        from store import Store
        from test_store import fixture

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic-review.sqlite3"
            clock = [100]
            store = Store(path, initialize_review_open=True, clock=lambda: clock[0])
            ticket = store.begin_capture(now=101)
            result = store.import_capture(fixture(), ticket=ticket, observed_at=102, now=103)
            self.assertEqual(result["outcome"], "created")
            clock[0] = 104
            self.server.store = store
            code, _, body = self.request("GET", "/api/state", headers={"Cookie": self.cookie})
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(body)["item"]["status"], "awaiting_human_review")
            code, _, body = self.post('{"action":"takeover"}')
            self.assertEqual(code, 200)
            state = json.loads(body)
            self.assertEqual(state["control"]["mode"], "takeover")
            self.assertEqual(state["item"]["status"], "cancelled")
            self.assertFalse(state["item"]["action_eligible"])
            persisted = Store(path, clock=lambda: 105).public_state()
            self.assertEqual(persisted["control"]["mode"], "takeover")
            self.assertEqual(persisted["items"][0]["status"], "cancelled")
            self.assertFalse(persisted["items"][0]["action_eligible"])
            self.assertFalse(persisted["external_actions_enabled"])


if __name__ == "__main__":
    unittest.main()
