import copy
from datetime import date, datetime
import io
import json
from contextlib import redirect_stdout
import unittest

import v11_algorithm_watch as aw
import v11_content_plan as cp
import v11_daily_digest as dd
import v11_weekly_checks as wc
import worker
from test_v11_daily_digest import Base
from v11_chain_live import load_catalog

NOW = datetime.fromisoformat("2026-10-24T09:30:00+03:00")  # Saturday: next week 26.10 - 01.11, tomorrow 25.10
ACCT, PID, SOCIAL, MOTION = 5898865, 40694338, 8231779952, 9381497228
API = f"https://3.basecampapi.com/{ACCT}/buckets/{PID}/todolists"


def todo(i, title, due, who=("Özge Yağmur",), brief="<div>Görsel: istasyon</div>", completed=False):
    return {"id": i, "content": title, "due_on": due, "completed": completed, "status": "active",
            "assignees": [{"id": n, "name": w} for n, w in enumerate(who, 1)], "description": brief}


class Send:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, method, url, body, expected):
        self.calls.append((method, url))
        if url not in self.pages:
            raise wc.cp.ContentPlanError("fake_not_found")
        return copy.deepcopy(self.pages[url]), {}


def pages():
    return {f"{API}/{SOCIAL}/todos.json": [
        todo(1, "Kış şarjı Reels", "2026-10-26"), todo(2, "Yeni istasyon karusel", "2026-10-27"),
        todo(3, "Yeni istasyon karusel 28.10", "2026-10-28"), todo(4, "Cumhuriyet Bayramı postu", "2026-10-29"),
        todo(5, "LinkedIn: ortaklık haberi", "2026-10-25", who=(), brief=""),
        todo(6, "Bitti", "2026-10-30", completed=True), todo(7, "Tarihsiz fikir", None)],
        f"{API}/{MOTION}/todos.json": [todo(8, "WAT istasyon çekimi", "2026-10-31", who=("Samet",)),
                                       todo(9, "Ofis ÇEKİMİ", "2026-10-25", brief="")]}


class WeeklyChecks(Base):
    def run_checks(self, pages_=None, **kw):
        self.send = Send(pages() if pages_ is None else pages_)
        args = dict(config=wc.load_config(), catalog=load_catalog(), cal=dd.load_calendar(),
                    book_reader=lambda: aw.new_book(aw.load_seed()),
                    http_factory=lambda pid, lids: wc.ChecksBasecamp(ACCT, pid, lids, send=self.send),
                    clock=lambda: NOW)
        args.update(kw)
        return wc.run_weekly_checks(self.ops, **args)

    def test_config_ships_off(self):
        cfg = wc.validate_config(wc.load_config())
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["brands"]["wat_mobilite"]["lists"], ["social", "motion"])

    def test_week_and_tomorrow(self):
        r = self.run_checks()
        self.assertEqual((r["status"], r["model_calls"], r["business_writes"]), ("weekly_checks_preview", 0, 0), r)
        self.assertEqual(r["week"], "2026-10-26")
        # 30.10 (done task ignored), 31.10 (only a shoot), 01.11 are empty
        self.assertEqual((r["empty_days"], r["repeats"], r["special_days"], r["shoots"], r["tomorrow"]),
                         (3, 1, 2, 1, 2), r)
        t = r["text"]
        self.assertIn("Tekrar olabilir: \"Yeni istasyon karusel 28.10\"", t)
        self.assertIn("Özel gün: 29.10 Perşembe Cumhuriyet Bayramı\n", t)
        self.assertIn("Özel gün: 31.10 Cumartesi Cadılar Bayramı · o gün için iş yok", t)
        self.assertIn("Çekim: 31.10 Cumartesi · WAT istasyon çekimi · Samet", t)
        self.assertIn("🎬 Çekim hatırlatması: Ofis ÇEKİMİ", t)
        self.assertIn("· LinkedIn: ortaklık haberi\n    🟡 sorumlu yok\n    🟡 brief yok\n    Kural: 🟡", t)
        self.assertNotIn("Bitti", t)
        self.assertTrue(all(m == "GET" for m, _ in self.send.calls))
        self.assertEqual(len(self.send.calls), 2)

    def test_empty_days_show_plan_idea(self):
        def put(tx, _):
            tx.put(cp.record_key("wat_mobilite", date(2026, 11, 1)), {"kind": "content_plan", "state": "ready", "plan": {
                "days": [{"date": "2026-11-01", "topic": "Kasım şarj rehberi", "format": "karusel",
                          "recommended": True}]}})
        self.ops._run(put)
        t = self.run_checks()["text"]
        self.assertIn("01.11 Pazar · plandaki fikir: ⭐ Kasım şarj rehberi", t)
        self.assertIn("  · 30.10 Cuma\n", t)

    def test_no_tasks_and_day_override(self):
        r = self.run_checks(pages_={f"{API}/{SOCIAL}/todos.json": [], f"{API}/{MOTION}/todos.json": []},
                            day="2026-10-17")
        self.assertEqual((r["week"], r["empty_days"], r["tomorrow"], r["calendar_known"]),
                         ("2026-10-19", 0, 0, False), r)
        self.assertIn("Bu haftanın paylaşımları ekibin aylık plan dokümanında", r["text"])
        self.assertIn("Yarın için tarihli iş yok.", r["text"])

    def test_plan_in_team_document_is_not_reported_as_empty(self):
        # WAT 17 Oct (Y6): one dated task, the month plan lives in "WM // Ekim '26 SM Planı" (Google Doc)
        r = self.run_checks(pages_={f"{API}/{SOCIAL}/todos.json": [todo(1, "WM // Ekim '26 SM Planı", "2026-10-26")],
                                    f"{API}/{MOTION}/todos.json": []})
        self.assertEqual((r["empty_days"], r["calendar_known"]), (0, False), r)
        self.assertNotIn("Boş günler", r["text"])
        self.assertNotIn("o gün için iş yok", r["text"])

    def test_fails_closed(self):
        self.assertEqual(self.run_checks(brand="rabam")["reason"], "weekly_checks_brand_not_configured")
        bad = copy.deepcopy(load_catalog())
        bad["brand_routes"]["wat_mobilite"]["verified"] = False
        self.assertEqual(self.run_checks(catalog=bad)["reason"], "weekly_checks_route_unverified")
        r = self.run_checks(pages_={})
        self.assertEqual(r["status"], "held")

    def test_allowlist_is_get_only_and_scoped(self):
        http = wc.ChecksBasecamp(ACCT, PID, [SOCIAL], send=Send({}))
        for method, url in (("POST", f"{API}/{SOCIAL}/todos.json"), ("GET", f"{API}/{MOTION}/todos.json"),
                            ("GET", f"https://3.basecampapi.com/{ACCT}/buckets/1/todolists/{SOCIAL}/todos.json")):
            with self.assertRaises(Exception) as e:
                http.call(method, url)
            self.assertEqual(wc._code(e.exception), "nb_url_out_of_scope")

    def test_shoot_words_turkish_case(self):
        self.assertTrue(wc.is_shoot({"title": "ÇEKİM GÜNÜ"}, wc.validate_config(wc.load_config())["shoot_words"]))
        self.assertEqual(wc.platforms_of("Reels + LinkedIn"), ["instagram", "linkedin"])


class Wiring(unittest.TestCase):
    def test_worker_flags(self):
        for argv, reason in ((["--mode", "weekly_checks"], "weekly_checks_requires_checks_preview"),
                             (["--mode", "daily_digest", "--checks-preview"], "weekly_checks_requires_checks_preview"),
                             (["--mode", "daily_digest", "--checks-brand", "wat_mobilite"],
                              "checks_flags_require_weekly_checks"),
                             (["--mode", "weekly_checks", "--checks-preview", "--checks-day", "24.10"],
                              "checks_flags_require_weekly_checks")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(worker.main(argv), 2)
            self.assertEqual(json.loads(buf.getvalue())["reason"], reason, argv)
        self.assertIn("weekly_checks_preview", worker.OK_STATUSES)

    def test_packaged(self):
        import package_chain_source as pkg
        self.assertIn(pkg.APP + "v11_weekly_checks.py", pkg.ALLOWLIST)
        self.assertIn(pkg.APP + "chain-profile/weekly-checks-config.json", pkg.ALLOWLIST)


if __name__ == "__main__":
    unittest.main()
