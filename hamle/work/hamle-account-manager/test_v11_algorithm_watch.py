"""Platform Kural Defteri + Algoritma Nöbeti: fakes only, no sockets, Gmail, Basecamp or real model calls."""
import copy
from datetime import date, datetime
import unittest
from unittest.mock import patch

import v11_algorithm_watch as aw
import v11_daily_digest as dd
import v11_health_check as hc
from test_anthropic_research import research_message
from test_anthropic_routing import FakeClient, KEY
from test_v11_daily_digest import Base, config as digest_config
from v11_chain_live import load_catalog

SUN_2130 = datetime.fromisoformat("2026-10-18T21:30:00+03:00")
MON = date(2026, 10, 19)
OFFICIAL = "https://about.instagram.com/blog/announcements/reels-length-update"
CREATORS = "https://creators.instagram.com/blog/originality"
BLOG_A = "https://www.socialpilot.co/blog/linkedin-update"
BLOG_B = "https://www.eclincher.com/articles/linkedin-update"
RUMOUR = "https://someforum.example.com/thread/1"


def config(**kw):
    c = copy.deepcopy(aw.load_config())
    c.update(enabled=True)
    c.update(kw)
    return c


def change(url, *, platform="instagram", text="Reels süresi 3 dakikaya çıktı.", day="2026-10-15", rule=None,
           critical=False, replaces=None):
    return {"platform": platform, "change": text, "date": day, "urls": url if isinstance(url, list) else [url],
            "impact": "Uzun anlatımlı Reels artık öneriye girebiliyor.", "rule": rule, "replaces": replaces,
            "critical": critical}


class Watch(Base):
    START = SUN_2130

    def setUp(self):
        super().setUp()
        self.calls = []

    def answer(self, value, results=(OFFICIAL, CREATORS, BLOG_A, BLOG_B, RUMOUR)):
        self.client = FakeClient(research_message(value, results=results, cited=()))

    def factory(self, cfg):
        self.calls.append(cfg)
        return aw.research_source(self.ops, {"anthropic": lambda: KEY}, load_catalog(), cfg, client=self.client)

    def watch(self, preview=False, **kw):
        return aw.run_algorithm_watch(self.ops, config=config(**kw), seed=aw.load_seed(), cards=self.cards,
                                      research_factory=self.factory, clock=lambda: self.now, preview=preview)

    def data(self):
        return self.ops.backend.data


class Config(Watch):
    def test_disk_config_starts_off_and_does_nothing(self):
        cfg = aw.load_config()
        self.assertIs(cfg["enabled"], False)
        r = aw.run_algorithm_watch(self.ops, config=cfg, seed=aw.load_seed(), cards=self.cards,
                                   research_factory=self.factory, clock=lambda: self.now)
        self.assertEqual((r["status"], r["reason"], self.calls), ("no_new_work", "algorithm_watch_disabled", []))
        self.assertNotIn(aw.RULES_KEY, self.data())

    def test_own_model_settings_and_the_shared_routing_file_is_untouched(self):
        import model_routing
        self.assertNotIn(aw.ROLE, model_routing.ROLES)
        route = aw.validate_config(config())["route"]
        self.assertEqual((route.model, route.web_search["max_uses"], route.timeout_seconds), ("claude-opus-5-5", 6, 600))
        for bad in ({"model": {}}, {"run_window": {"weekday": 9, "start": "21:00", "end": "23:30"}},
                    {"official_sources": {"myspace": ["myspace.com"]}}, {"reverify_days": 7}):
            with self.assertRaises(aw.AlgorithmWatchError):
                aw.validate_config(config(**bad))

    def test_seed_is_14_multi_source_rules(self):
        book = aw.new_book(aw.load_seed())
        self.assertEqual(len(book["rules"]), 14)
        self.assertTrue(all(r["source_kind"] == "multi" and len(r["source_urls"]) >= 2 for r in book["rules"]))
        self.assertEqual({r["platform"] for r in book["rules"]}, {"instagram", "tiktok", "linkedin"})

    def test_outside_sunday_window_nothing_runs(self):
        self.now = datetime.fromisoformat("2026-10-18T12:00:00+03:00")
        self.assertEqual(self.watch()["reason"], "outside_algorithm_watch_window")
        self.now = datetime.fromisoformat("2026-10-19T21:30:00+03:00")
        self.assertEqual(self.watch()["reason"], "outside_algorithm_watch_window")
        self.assertEqual(self.calls, [])


class Run(Watch):
    def test_only_sourced_recent_organic_changes_are_kept_and_graded(self):
        self.answer({"changes": [
            change(OFFICIAL, rule="Instagram: Reels 3 dakikaya kadar öneriye girer.", critical=True),
            change([BLOG_A, BLOG_B], platform="linkedin", text="Belge karuseli erişimi arttı.", critical=True),
            change(RUMOUR, text="Söylenti: hashtag tamamen kalktı."),
            change("https://about.instagram.com/never-searched", text="Aranmamış kaynak."),
            change(CREATORS, text="Eski haber.", day="2026-08-01"),
            change(CREATORS, text="Reklam ürünlerinde yeni teklif seçeneği."),
        ], "verified": [], "retired": []})
        r = self.watch()
        self.assertEqual((r["status"], r["changes"], r["dropped"], r["critical"], r["model_calls"]),
                         ("algorithm_watch_completed", 2, 4, 1, 1))
        row = self.data()["algorithm-watch-20261019"]
        kinds = [(c["platform"], c["source_kind"], c["critical"]) for c in row["changes"]]
        self.assertEqual(kinds, [("instagram", "official", True), ("linkedin", "multi", False)])  # 🟡 never critical
        book = self.data()[aw.RULES_KEY]
        self.assertEqual((book["version"], len(book["rules"]), r["added"]), (2, 15, ["in-20261018-1"]))
        call = self.client.calls[0]
        self.assertEqual(call["tools"][0]["max_uses"], 6)
        self.assertIn("KAPSAM DIŞI", call["system"])
        rows = row["card"]["rows"]
        self.assertEqual(rows[0]["brands"], ["Altınmarka", "Fenerium", "WAT Mobilite", "ya da Multicosmetics"])
        self.assertEqual(rows[1]["brands"], ["Gamechangers"])

    def test_one_run_a_week_and_a_failed_call_is_never_retried(self):
        self.answer({"changes": [], "verified": [], "retired": []})
        self.assertEqual(self.watch()["status"], "algorithm_watch_completed")
        self.assertEqual(self.watch()["status"], "already_recorded")
        self.assertEqual(len(self.client.calls), 1)
        card = self.data()["algorithm-watch-20261019"]["card"]
        self.assertIn("yeni bir değişiklik yok", card["line"])
        self.assertIn("14 kural", card["line"])

    def test_bad_answer_releases_the_lease_and_records_the_reason(self):
        self.client = FakeClient(research_message({"nope": 1}))
        r = self.watch()
        self.assertEqual(r["status"], "held")
        row = self.data()["algorithm-watch-20261019"]
        self.assertEqual(row["state"], "running")
        self.assertTrue(row["last_failure"])
        self.assertNotIn(aw.RULES_KEY, self.data())

    def test_preview_calls_but_writes_nothing(self):
        self.now = datetime.fromisoformat("2026-10-20T15:00:00+03:00")
        self.answer({"changes": [change(OFFICIAL)], "verified": [], "retired": []})
        r = self.watch(preview=True, enabled=False)
        self.assertEqual((r["status"], r["facts"]["changes"]), ("algorithm_watch_preview", 1))
        self.assertFalse([k for k in self.data() if k.startswith("algorithm-watch-") or k == aw.RULES_KEY])

    def test_reverify_and_retire_move_the_book(self):
        book = aw.new_book(aw.load_seed())
        for r in book["rules"]:
            if r["id"] in ("ig-seed-3", "li-seed-1"):
                r["last_verified"] = "2026-06-01"
        book["updated_on"] = "2026-10-11"
        self.data()[aw.RULES_KEY] = book
        self.answer({"changes": [], "verified": [{"id": "li-seed-1", "urls": [BLOG_A, BLOG_B]}],
                     "retired": [{"id": "ig-seed-3", "reason": "Instagram repost sınırını kaldırdı.", "urls": [CREATORS]},
                                 {"id": "li-seed-4", "reason": "Bir blog öyle diyor.", "urls": [BLOG_A]}]})
        r = self.watch()
        sent = self.client.calls[0]["messages"][0]["content"]
        self.assertIn('"reverify":["ig-seed-3","li-seed-1"]', sent.replace(" ", ""))
        self.assertIn('"since":"2026-10-11"', sent.replace(" ", ""))
        self.assertEqual((r["archived"], r["rules"]), (["ig-seed-3"], 13))  # only the platform itself retires a rule
        new = self.data()[aw.RULES_KEY]
        self.assertEqual({x["id"]: x["last_verified"] for x in new["rules"]}["li-seed-1"], "2026-10-18")
        self.assertEqual(new["archived"][0]["archive_reason"], "Instagram repost sınırını kaldırdı.")

    def test_a_replacing_change_archives_the_old_rule(self):
        self.answer({"changes": [change(CREATORS, text="Repost sınırı 30 günde 5'e indi.", rule="Instagram: 30 günde en "
                                        "fazla 5 repost.", replaces="ig-seed-3")], "verified": [], "retired": []})
        r = self.watch()
        self.assertEqual(r["archived"], ["ig-seed-3"])
        self.assertIn("in-20261018-1", [x["id"] for x in self.data()[aw.RULES_KEY]["rules"]])


class Digest(Watch):
    def test_mondays_digest_shows_the_card_with_the_client_warning(self):
        self.answer({"changes": [change(OFFICIAL, rule="Instagram: Reels 3 dakikaya kadar.", critical=True)],
                     "verified": [], "retired": []})
        self.watch()
        self.now = datetime.fromisoformat("2026-10-19T09:16:00+03:00")
        r = self.run_digest(digest_config(trend_search=False))
        self.assertEqual(r["status"], "digest_sent")
        text = self.gmail.text()
        self.assertIn("BU HAFTA ALGORİTMADA NE DEĞİŞTİ", text)
        self.assertIn("🟢 Instagram: Reels süresi 3 dakikaya çıktı.", text)
        self.assertIn(aw.CLIENT_WARNING, text)
        self.assertEqual(r["stats"]["algorithm"], 1)

    def test_tuesday_and_a_missing_record_show_nothing(self):
        row = {"state": "done", "week": "2026-10-19", "card": {"line": "x", "rows": []}}
        self.assertIsNotNone(dd._algorithm_rows(row, MON))
        self.assertIsNone(dd._algorithm_rows(None, MON))
        self.assertIsNone(dd._algorithm_rows(dict(row, state="running"), MON))
        listing = {"items": [], "projects": 3, "account": 1}
        parts = dict(day=MON, day_note=None, overdue=[], trends=[], specials=[], classified=False)
        self.assertNotIn("ALGORİTMA", dd.render(listing, **parts))


class Health(Watch):
    def test_a_missing_sunday_run_is_amber_only_while_the_flag_is_on(self):
        on = config()
        with patch.object(aw, "load_config", lambda path=None: on):
            fs = hc.check_algorithm(self.ops, date(2026, 10, 18))
            self.assertEqual((fs[0]["check"], fs[0]["severity"], fs[0]["evidence"]),
                             ("runs.algorithm_watch", "amber", ["algorithm-watch-20261019"]))
            self.assertEqual(hc.check_algorithm(self.ops, date(2026, 10, 17)), [])
            self.data()["algorithm-watch-20261019"] = {"state": "done"}
            self.assertEqual(hc.check_algorithm(self.ops, date(2026, 10, 18)), [])
        del self.data()["algorithm-watch-20261019"]
        self.assertEqual(hc.check_algorithm(self.ops, date(2026, 10, 18)), [])  # disk config: off


if __name__ == "__main__":
    unittest.main()
