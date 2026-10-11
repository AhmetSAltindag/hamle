"""Trend Radarı web_search provider: fake transport only, no sockets, no keys."""
from datetime import date, datetime
from model_routing import openai_only
import json
import os
import unittest

import model as base
import v11_chain_models as cm
import v11_daily_digest as dd
import v11_trend_search as ts
from test_v11_chain_models import Transport, envelope
from test_v11_daily_digest import Base, KEY, config
from v11_chain_live import load_catalog

DAY = date(2026, 10, 12)


def response(trends, *, cited=(), sources=(), extra=()):
    row = envelope({"trends": trends})
    msg = row["output"][-1]
    msg["content"][0]["annotations"] = [{"type": "url_citation", "url": u, "title": "t", "start_index": 0,
                                         "end_index": 1} for u in cited]
    row["output"] = [row["output"][0],
                     {"type": "web_search_call", "id": "ws_1", "status": "completed",
                      "action": {"type": "search", "query": "trend", "sources": [{"type": "url", "url": u}
                                                                                 for u in sources]}},
                     *extra, msg]
    return row


def trend(url, day="2026-10-10", brand="Altınmarka", platform="tiktok", name="Mini vlog akımı"):
    return {"trend": name, "platform": platform, "url": url, "date": day,
            "brand_idea": "Ürün hazırlığını 10 saniyelik vlog ile göster.", "brand": brand}


class Provider(Base):
    def source(self, transport, catalog=None):
        return ts.trend_search_source(self.ops, lambda: KEY, catalog or load_catalog(), self.cards, transport=transport,
                                     routing=openai_only())

    def test_request_is_separate_web_search_path_and_model_py_stays_tool_free(self):
        brands = [b["brand"] for b in ts.eligible_brands(self.cards)]
        req = ts.build_request({"today": DAY.isoformat()}, brands)
        self.assertEqual((req["model"], req["tools"], req["tool_choice"]), ("gpt-6-astra", [{"type": "web_search"}], "auto"))
        fmt = req["text"]["format"]
        self.assertEqual((fmt["name"], fmt["strict"]), ("hamle_trend_radar", True))
        item = fmt["schema"]["properties"]["trends"]["items"]
        self.assertEqual(item["properties"]["platform"]["enum"], ["instagram", "tiktok", "x"])
        self.assertIn("Altınmarka", item["properties"]["brand"]["enum"])
        self.assertNotIn("Hamle İç Deneme", item["properties"]["brand"]["enum"])
        self.assertIn("Bikaldi", brands)  # sector filled from its website (2026-10-11)
        chain = cm.build_request("digest", {"items": []})
        self.assertEqual((chain["tools"], chain["tool_choice"]), ([], "none"))
        with open(base.__file__, encoding="utf-8") as f:
            self.assertIn('"tools": []', f.read())

    def test_only_cited_https_recent_items_survive(self):
        good = "https://www.example.org/trend/mini-vlog"
        trends = [trend(good + "?utm_source=openai"),
                  trend("https://uncited.example.org/a", name="Uydurma"),
                  trend("https://old.example.org/a", day="2026-08-01", name="Eski"),
                  trend("https://future.example.org/a", day="2026-10-20", name="Gelecek"),
                  trend("https://nodate.example.org/a", day="bilinmiyor", name="Tarihsiz")]
        transport = Transport(response(trends, cited=[good + "/?utm_source=openai"],
                                       sources=["https://old.example.org/a", "https://future.example.org/a",
                                                "https://nodate.example.org/a"]))
        out = self.source(transport)(DAY)
        self.assertEqual(out, [{"title": "Mini vlog akımı (TikTok)", "url": good, "published_at": "2026-10-10",
                                "summary": "", "idea": "Altınmarka: Ürün hazırlığını 10 saniyelik vlog ile göster."}])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(self.chain_reservations()), 1)  # own hold, counted against the 30 USD chain cap
        sent = transport.calls[0]
        self.assertEqual(sent["tools"], [{"type": "web_search"}])
        self.assertEqual(json.loads(sent["input"][0]["content"])["today"], "2026-10-12")
        # same day again: cached, no second call or hold
        self.assertEqual(self.source(transport)(DAY), out)
        self.assertEqual((len(transport.calls), len(self.chain_reservations())), (1, 1))

    def test_http_and_unlisted_brand_or_tool_outputs_are_rejected(self):
        self.assertIsNone(ts.normalize_url("http://example.org/a"))
        self.assertIsNone(ts.normalize_url("https://user:pw@example.org/a"))
        self.assertEqual(ts.sourced({"trends": [trend("http://example.org/a")], "evidence": ["http://example.org/a"]},
                                    DAY), [])
        cases = ((DAY, response([trend("https://a.example.org", brand="Uydurma Marka")], cited=["https://a.example.org"])),
                 (date(2026, 10, 11), response([], extra=[{"type": "function_call", "name": "x", "arguments": "{}"}])))
        for day, bad in cases:
            with self.assertRaises(cm.ChainModelError):
                self.source(Transport(bad))(day)
        transport = Transport()
        with self.assertRaises(cm.ChainModelError):  # failed chance is never retried the same day
            self.source(transport)(DAY)
        self.assertEqual(transport.calls, [])

    def test_cap_reached_means_no_key_no_call(self):
        catalog = load_catalog()
        catalog["budget"]["internal_live_cap_microdollars"] = 1
        transport, keys = Transport(), []
        src = ts.trend_search_source(self.ops, lambda: keys.append(1) or KEY, catalog, self.cards, transport=transport,
                                     routing=openai_only())
        with self.assertRaises(ts.TrendSearchError):
            src(DAY)
        self.assertEqual((transport.calls, keys), ([], []))


class Flag(Base):
    def test_disk_config_flag_is_on_per_decision(self):
        self.assertIs(dd.load_config()["trend_search_enabled"], True)
        self.assertIs(dd.validate_config(dd.load_config())["trend_search"], True)
        with self.assertRaises(dd.DigestError):
            dd.validate_config(config(trend_search_enabled="yes"))

    def test_provider_never_runs_without_flag(self):
        good = "https://www.example.org/trend/mini-vlog"
        transport = Transport(response([trend(good)], cited=[good]))
        src = ts.trend_search_source(self.ops, lambda: KEY, load_catalog(), self.cards, transport=transport,
                                     routing=openai_only())
        r = self.run_digest(trend_source=src)
        self.assertEqual(r["status"], "digest_sent", r)
        self.assertEqual(transport.calls, [])
        self.assertIn(dd.NO_TRENDS, self.gmail.text())
        self.now = datetime.fromisoformat("2026-10-12T18:00:00+03:00")
        p = self.run_digest(config(trend_search_enabled=True), trend_source=src, preview_to="ahmet@gamechangers.com.tr")
        self.assertEqual(p["status"], "digest_sent", p)
        self.assertEqual(len(transport.calls), 1)
        text = self.gmail.text()
        self.assertIn(f"- Mini vlog akımı (TikTok) (2026-10-10) · {good} Uyarlama: Altınmarka: ", text)
        self.assertNotIn(dd.NO_TRENDS, text)

    def test_worker_builds_provider_only_when_flag_true(self):
        with open(dd.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('if validate_config(config)["trend_search"] is True:', src)

    def test_packaged(self):
        import package_chain_source as pkg
        for name in ("v11_trend_search.py", "chain_trend_prompt.txt"):
            self.assertIn(name, pkg.CHAIN_FILES)


if __name__ == "__main__":
    unittest.main()
