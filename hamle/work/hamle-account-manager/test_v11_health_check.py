"""Hamle gece sağlık kontrolü: fakes only, no sockets, Gmail, Basecamp or models."""
import copy
from contextlib import contextmanager
from datetime import date, datetime
import socket
import unittest
from unittest.mock import patch

import v11_daily_digest as dd
import v11_health_check as hc
from cloud_store import OperationsStore
from test_cloud_store import MemoryBackend, budget_snapshot
from test_v11_daily_digest import Gmail

THU = date(2026, 10, 15)
FRI_NIGHT = datetime.fromisoformat("2026-10-16T00:05:00+03:00")  # checks Thursday 15 Oct
TOKEN = "synthetic-placeholder-token-never-live"


def config(**kw):
    c = copy.deepcopy(hc.load_config())
    c.update(kw)
    return c


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.now = datetime.fromisoformat("2026-10-12T09:00:00+03:00")
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: self.now)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.ops.resume_review_only("offline-test")
        self.ops.roll_budget_period()
        self.now = FRI_NIGHT
        self.gmail = Gmail()

        @contextmanager
        def token():
            yield TOKEN
        self.writer = dd.DigestMailWriter(token_context=token, session_factory=lambda: self.gmail)

    def healthy_day(self, day=THU):
        data = self.ops.backend.data
        for t in hc.slot_times(hc.validate_config(config())["tick"]):
            data[hc.KEYS["slot"](day, t)] = {"state": "no_new_work"}
        data[hc.KEYS["digest"](day)] = {"state": "sent"}
        data[hc.KEYS["memory"](day)] = {"status": "brand_memory_completed"}

    def check(self, preview=False, **kw):
        return hc.run_health_check(self.ops, config=config(**kw), writer=self.writer, clock=lambda: self.now,
                                   preview=preview)


class Checks(Base):
    def test_a_clean_day_is_one_record_and_no_mail(self):
        self.healthy_day()
        r = self.check()
        self.assertEqual((r["status"], r["red"], r["amber"], r["alert"], r["model_calls"]),
                         ("health_check_completed", 0, 0, "none", 0))
        row = self.ops.backend.data["health-check-20261016"]
        self.assertEqual((row["state"], row["checked"], row["findings"]), ("done", "2026-10-15", []))
        self.assertIn("temiz", row["summary"])
        self.assertEqual(self.gmail.posts(), [])

    def test_the_16_october_case_a_silent_brand_memory_is_found_and_mailed(self):
        self.healthy_day()
        del self.ops.backend.data[hc.KEYS["memory"](THU)]
        r = self.check()
        self.assertEqual((r["red"], r["alert"]), (1, "sent"))
        f = self.ops.backend.data["health-check-20261016"]["findings"][0]
        self.assertEqual((f["check"], f["severity"], f["evidence"]), ("runs.brand_memory", "red",
                                                                      ["brand-memory-run-20261015"]))
        self.assertEqual(f["confidence"], "düşük")  # the cause is a guess and says so
        text = self.gmail.text()
        self.assertIn("Marka hafızası tazelenmedi", text)
        self.assertIn("Hamle AI", text)
        self.assertEqual(self.gmail.message()["To"], "ahmet@gamechangers.com.tr")

    def test_missing_ticks_digest_and_unconfirmed_sends(self):
        self.healthy_day()
        data = self.ops.backend.data
        for t in ("1000", "1015", "1030"):
            del data[hc.KEYS["slot"](THU, t)]
        data[hc.KEYS["digest"](THU)] = {"state": "unknown"}
        data["client-day-20261015"] = {"message_keys": ["m1", "m2", "m3"]}
        data["m1"] = {"reply_state": "send_unknown"}
        data["m2"] = {"reply_state": "send_unknown", "test": True}
        data["m3"] = {"reply_state": "awaiting_approval", "updated_at": self.now.timestamp() - 2 * 86400}
        checks = {f["check"]: f for f in self.check(preview=True)["findings"]}
        self.assertEqual(checks["runs.chain_tick"]["severity"], "amber")
        self.assertIn("10:00", checks["runs.chain_tick"]["actual"])
        self.assertEqual(checks["runs.daily_digest"]["severity"], "red")
        self.assertEqual(checks["sends.unconfirmed"]["evidence"], ["m1"])
        self.assertEqual(checks["sends.waiting_approval"]["evidence"], ["m3"])

    def test_a_whole_day_without_ticks_is_red_and_weekends_are_skipped(self):
        self.healthy_day()
        for t in hc.slot_times(hc.validate_config(config())["tick"]):
            del self.ops.backend.data[hc.KEYS["slot"](THU, t)]
        f = self.check(preview=True)["findings"][0]
        self.assertEqual((f["check"], f["severity"], f["title"]), ("runs.chain_tick", "red",
                                                                   "Mail ve Basecamp turu hiç çalışmadı"))
        self.now = datetime.fromisoformat("2026-10-18T00:05:00+03:00")  # Sunday night checks Saturday
        self.assertEqual(self.check(preview=True)["findings"], [])

    def test_stuck_jobs_stop_and_budget(self):
        self.healthy_day()
        data = self.ops.backend.data
        data["client-jobs"] = {"keys": ["j1", "j2"]}
        data["j1"] = {"kind": "client_job", "key": "j1", "state": "creating", "updated_at": self.now.timestamp() - 7200}
        data["j2"] = {"kind": "client_job", "key": "j2", "state": "open", "updated_at": 0}
        self.ops.takeover("offline-test")
        checks = {f["check"]: f for f in self.check(preview=True, budget={"warn_usd": 0.000001})["findings"]}
        self.assertEqual(checks["jobs.stuck"]["evidence"], ["j1"])
        self.assertEqual(checks["control.stopped"]["severity"], "red")
        self.assertIn("budget.warn", checks)

    def test_a_broken_check_is_a_finding_and_the_others_still_run(self):
        self.healthy_day()
        with patch.object(hc, "check_jobs", side_effect=RuntimeError("boom")):
            r = self.check(preview=True)
        self.assertEqual([f["check"] for f in r["findings"]], ["check.jobs"])
        self.assertEqual(r["facts"]["broken"], ["jobs"])


class Durability(Base):
    def test_one_run_per_night_and_one_alert(self):
        self.assertEqual(self.check()["alert"], "sent")
        self.assertEqual(self.check()["status"], "already_recorded")
        self.assertEqual(len(self.gmail.posts()), 1)
        self.assertEqual(self.ops.backend.data["health-alert-20261016"]["state"], "sent")

    def test_a_running_lease_blocks_a_second_trigger_and_an_expired_one_finishes(self):
        night = hc._Night(self.ops, "health-check-20261016")
        self.assertIsNotNone(night.claim("t1", {"night": "2026-10-16"}))
        self.assertEqual(self.check()["status"], "already_recorded")
        self.now = datetime.fromisoformat("2026-10-16T00:20:00+03:00")
        r = self.check()
        self.assertEqual(r["status"], "health_check_completed")
        self.assertEqual(self.ops.backend.data["health-check-20261016"]["runs"], 2)

    def test_missed_nights_are_never_replayed_and_daytime_does_nothing(self):
        self.now = datetime.fromisoformat("2026-10-16T09:00:00+03:00")
        self.assertEqual(self.check()["reason"], "outside_health_check_window")
        self.assertNotIn("health-check-20261015", self.ops.backend.data)
        self.assertEqual(self.check(enabled=False)["reason"], "health_check_disabled")

    def test_an_unknown_alert_is_not_resent(self):
        self.gmail.fail = TimeoutError("lost")
        self.assertTrue(self.check()["alert"].startswith(("unknown", "failed")))
        self.assertEqual(self.ops.backend.data["health-check-20261016"]["state"], "done")

    def test_config_refuses_outside_addresses(self):
        with self.assertRaises(hc.HealthCheckError):
            hc.validate_config(config(alert_recipients=["someone@example.com"]))


class Digest(Base):
    def test_digest_rows_and_the_merged_nights(self):
        self.healthy_day()
        del self.ops.backend.data[hc.KEYS["memory"](THU)]
        self.check()
        row = self.ops.backend.data["health-check-20261016"]
        rows = hc.digest_rows(row, date(2026, 10, 16))
        self.assertEqual((rows["title"], rows["rows"]), ("Hamle sağlık", ["🔴 Marka hafızası tazelenmedi"]))
        self.assertIsNone(hc.digest_rows(row, date(2026, 10, 17)))
        merged = dd._health_rows([(date(2026, 10, 17), None), (date(2026, 10, 16), row)])
        self.assertEqual(merged["rows"], ["🔴 Marka hafızası tazelenmedi"])
        self.assertIsNone(dd._health_rows([(date(2026, 10, 16), None)]))
        listing = {"items": [], "projects": 3, "account": 1}
        parts = dict(day=date(2026, 10, 16), day_note=None, overdue=[], trends=[], specials=[], classified=False,
                     health=merged)
        self.assertIn("HAMLE SAĞLIK\nGece kontrolü: 1 acil, 0 dikkat.\n- 🔴 Marka hafızası tazelenmedi",
                      dd.render(listing, **parts))
        self.assertIn("Marka hafızası tazelenmedi", dd.render_html(listing, **parts))


if __name__ == "__main__":
    unittest.main()
