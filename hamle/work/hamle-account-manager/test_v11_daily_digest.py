"""Daily digest: fakes only (Basecamp listing, Gmail REST, Operations store)."""
import base64
from contextlib import contextmanager
from datetime import datetime
import json
import os
import socket
import unittest
from unittest.mock import patch

import runtime_authority
import v11_daily_digest as dd
import worker
from cloud_store import OperationsStore
from test_cloud_providers import Response
from test_cloud_store import MemoryBackend, budget_snapshot

TOKEN = "synthetic-placeholder-token-never-live"
MON_0916 = datetime.fromisoformat("2026-10-12T09:16:00+03:00")
ACC = 5898865
BASE = f"https://3.basecampapi.com/{ACC}"


def config(**kw):
    c = {"schema_version": 1, "enabled": True, "recipients": ["ahmet@gamechangers.com.tr", "ozge@gamechangers.com.tr"],
         "day_note": "Bugün 14:00'te ekip toplantısı var.", "basecamp_account_id": ACC}
    c.update(kw)
    return c


class Basecamp:
    def __init__(self):
        self.calls = []
        self.pages = {
            f"{BASE}/projects.json": ([{"id": 1, "name": "Hamle — İç Deneme", "status": "active",
                                        "dock": [{"name": "todoset", "id": 11, "enabled": True},
                                                 {"name": "message_board", "id": 12, "enabled": True}]},
                                       {"id": 2, "name": "WAT", "status": "active",
                                        "dock": [{"name": "todoset", "id": 21, "enabled": True}]}], {}),
            f"{BASE}/buckets/1/todosets/11/todolists.json": ([{"id": 101}], {}),
            f"{BASE}/buckets/2/todosets/21/todolists.json": ([{"id": 201}], {}),
            f"{BASE}/buckets/1/todolists/101/todos.json": (
                [{"title": "Afiş", "status": "active", "completed": False, "due_on": "2026-10-12",
                  "assignees": [{"id": 1, "name": "Ahmet"}]},
                 {"title": "Bitti", "status": "active", "completed": True, "due_on": "2026-10-12", "assignees": []}],
                {"Link": f'<{BASE}/buckets/1/todolists/101/todos.json?page=2>; rel="next"'}),
            f"{BASE}/buckets/1/todolists/101/todos.json?page=2": (
                [{"title": "Story", "status": "active", "completed": False, "due_on": "2026-10-10",
                  "assignees": [{"id": 2, "name": "Cansu"}, {"id": 1, "name": "Ahmet"}]}], {}),
            f"{BASE}/buckets/2/todolists/201/todos.json": (
                [{"title": "Post", "status": "active", "completed": False, "due_on": None, "assignees": []}], {}),
        }

    def get(self, url):
        self.calls.append(url)
        payload, headers = self.pages[url]
        return json.loads(json.dumps(payload)), headers


class Gmail:
    def __init__(self):
        self.sent, self.calls, self.fail, self.mutate = {}, [], None, None

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        assert kw["allow_redirects"] is False
        if method == "POST":
            if self.fail:
                raise self.fail
            raw = base64.urlsafe_b64decode(json.loads(kw["data"])["raw"])
            mid = "d%03d" % (len(self.sent) + 1)
            self.sent[mid] = self.mutate(raw) if self.mutate else raw
            return Response({"id": mid, "labelIds": ["SENT"]})
        mid = url.split("/messages/")[1].split("?")[0]
        return Response({"id": mid, "labelIds": ["SENT"],
                         "raw": base64.urlsafe_b64encode(self.sent[mid]).decode().rstrip("=")})

    def close(self):
        pass

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.now = MON_0916
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: self.now)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.ops.resume_review_only("offline-test")
        self.bc, self.gmail = Basecamp(), Gmail()

        @contextmanager
        def token():
            yield TOKEN
        self.writer = dd.DigestMailWriter(token_context=token, session_factory=lambda: self.gmail)

    def run_digest(self, cfg=None, reader_factory=None):
        return dd.run_daily_digest(self.ops, config=cfg or config(),
                                   reader_factory=reader_factory or (lambda: dd.BasecampDigestReader(ACC, get=self.bc.get)),
                                   writer=self.writer, clock=lambda: self.now)


class Config(Base):
    def test_only_internal_agency_recipients(self):
        for bad in (["client@example.com"], ["ahmet@gamechangers.com.tr", "x@gamechangers.com.tr.evil.com"],
                    ["hamle@gamechangers.com.tr"], ["ahmet@gamechangers.com.tr"] * 2, "ahmet@gamechangers.com.tr"):
            with self.assertRaises(dd.DigestError):
                dd.validate_config(config(recipients=bad))
        r = self.run_digest(config(recipients=["ahmet@gamechangers.com.tr", "musteri@wat.com.tr"]))
        self.assertEqual((r["status"], r["reason"]), ("held", "digest_recipient_not_internal_agency"))
        self.assertEqual((self.gmail.calls, self.bc.calls), ([], []))

    def test_disk_config_is_disabled_placeholder(self):
        cfg = dd.load_config()
        self.assertEqual((cfg["enabled"], cfg["recipients"], cfg["day_note"]), (False, [], None))
        r = self.run_digest(cfg)
        self.assertEqual(r["status"], "no_new_work")
        self.assertEqual((self.gmail.calls, self.bc.calls), ([], []))


class Digest(Base):
    def test_first_run_after_0915_sends_once_with_readback(self):
        r = self.run_digest()
        self.assertEqual(r["status"], "digest_sent", r)
        self.assertEqual((r["open_todos"], len(self.gmail.posts())), (3, 1))
        raw = next(iter(self.gmail.sent.values())).decode()
        self.assertIn("To: ahmet@gamechangers.com.tr, ozge@gamechangers.com.tr", raw)
        self.assertNotIn("Cc:", raw)
        from email import policy as ep
        from email.parser import BytesParser
        text = BytesParser(policy=ep.default).parsebytes(next(iter(self.gmail.sent.values()))).get_content()
        for needle in ("Açık iş toplamı: 3", "Bugün teslim: 1", "Gecikmiş: 1", "- Ahmet: 2", "Günün notu:",
                       "14:00'te ekip toplantısı"):
            self.assertIn(needle, text)
        row = self.ops.backend.data["daily-digest-20261012"]
        self.assertEqual((row["state"], row["recipients"]), ("sent", config()["recipients"]))
        self.now = datetime.fromisoformat("2026-10-12T11:30:00+03:00")
        again = self.run_digest()
        self.assertEqual(again["status"], "already_recorded")
        self.assertEqual(len(self.gmail.posts()), 1)

    def test_window_weekday_and_deadline(self):
        for when in ("2026-10-12T09:14:00+03:00", "2026-10-12T18:31:00+03:00", "2026-10-10T10:00:00+03:00"):
            self.now = datetime.fromisoformat(when)
            self.assertEqual(self.run_digest()["reason"], "outside_digest_window")
        self.now = datetime.fromisoformat("2026-10-13T09:16:00+03:00")
        self.assertEqual(self.run_digest()["reason"], "authorization_expired")
        self.assertEqual(self.gmail.calls, [])

    def test_uncertain_send_is_never_resent(self):
        self.gmail.fail = TimeoutError("synthetic")
        r = self.run_digest()
        self.assertEqual((r["status"], r["reason"]), ("held", "digest_outcome_unknown_no_retry"))
        self.assertEqual(self.ops.backend.data["daily-digest-20261012"]["state"], "unknown")
        self.gmail.fail = None
        self.now = datetime.fromisoformat("2026-10-12T09:31:00+03:00")
        self.assertEqual(self.run_digest()["status"], "already_recorded")
        self.assertEqual(len(self.gmail.posts()), 1)

    def test_readback_mismatch_is_unknown(self):
        self.gmail.mutate = lambda raw: raw.replace(b"Subject: Hamle", b"Subject: Xamle")
        r = self.run_digest()
        self.assertEqual(r["reason"], "digest_outcome_unknown_no_retry")

    def test_read_failure_before_send_releases_day_for_next_run(self):
        def broken():
            raise dd.DigestError("basecamp_read_failed")
        r = self.run_digest(reader_factory=lambda: type("R", (), {"collect": staticmethod(broken)})())
        self.assertEqual((r["status"], r["reason"], r["business_writes"]), ("held", "basecamp_read_failed", 0))
        self.now = datetime.fromisoformat("2026-10-12T09:31:00+03:00")
        self.assertEqual(self.run_digest()["status"], "digest_sent")
        self.assertEqual(len(self.gmail.posts()), 1)

    def test_global_stop_means_no_read_and_no_send(self):
        self.ops.set_mode("stopped", "offline-test")
        r = self.run_digest()
        self.assertEqual(r["status"], "held")
        self.assertEqual((self.gmail.calls, self.bc.calls), ([], []))

    def test_reader_refuses_out_of_scope_link_and_is_bounded(self):
        self.bc.pages[f"{BASE}/projects.json"] = ([], {"Link": '<https://evil.example/x>; rel="next"'})
        with self.assertRaises(dd.DigestError):
            dd.BasecampDigestReader(ACC, get=self.bc.get).collect()
        url = f"{BASE}/projects.json"
        self.bc.pages[url] = ([], {"Link": f'<{url}?page=2>; rel="next"'})
        self.bc.pages[url + "?page=2"] = ([], {"Link": f'<{url}?page=2>; rel="next"'})
        with self.assertRaises(dd.DigestError):
            dd.BasecampDigestReader(ACC, get=self.bc.get, max_pages=3).collect()


class Runtime(unittest.TestCase):
    def test_single_deadline_value_drives_store_and_windows(self):
        import cloud_store
        self.assertEqual(runtime_authority.AUTHORIZATION_DEADLINE, "2026-10-12T23:59:59+03:00")
        self.assertEqual(cloud_store.DEADLINE, runtime_authority.deadline_timestamp())
        self.assertEqual(runtime_authority.window_minutes(runtime_authority.CHAIN_TICK_WINDOW), (555, 1110))
        self.assertEqual(runtime_authority.window_minutes(runtime_authority.SCHEDULED_REVIEW_WINDOW), (510, 1110))
        src = open(runtime_authority.__file__, encoding="utf-8").read()
        self.assertIn('AUTHORIZATION_DEADLINE = "2026-11-12T23:59:59+03:00"', src)  # documented one-line change

    def test_worker_daily_digest_mode_requires_cloud_opt_in(self):
        with patch.dict(os.environ, {}, clear=True), patch("builtins.print") as out:
            self.assertNotEqual(worker.main(["--mode", "daily_digest"]), 0)
        self.assertIn("cloud_opt_in_required", str(out.call_args))


if __name__ == "__main__":
    unittest.main()
