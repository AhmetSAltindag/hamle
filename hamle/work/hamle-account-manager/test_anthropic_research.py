"""Claude web-search research (Trend Radarı, Monday special days, New Business): fakes only."""
import copy
import json
import unittest
from datetime import date
from pathlib import Path

import model_routing
import v11_trend_search as ts
from test_anthropic_routing import FakeClient, KEY, StatusError, message, usage
from test_v11_daily_digest import Base
from v11_chain_live import load_catalog

DAY = date(2026, 10, 12)  # Monday
ROOT = Path(__file__).resolve().parents[2]
GOOD = "https://www.example.org/trend/mini-vlog"
CITED = "https://news.example.com/2026/10/09/kampanya"


def routing(**changes):
    doc = json.loads((ROOT / "outputs/gamechangers-internal/model-routing.json").read_text(encoding="utf-8"))
    for key, value in changes.items():
        if key in doc["flags"]:
            doc["flags"][key] = value
        else:
            doc[key] = value
    return model_routing.validate(doc)


def research_message(value, *, results=(GOOD,), cited=(CITED,), searches=2, stop="end_turn", extra=(),
                     result_content=None):
    text = json.dumps(value, ensure_ascii=False)
    half = len(text) // 2
    content = [{"type": "thinking", "thinking": "", "signature": "sig"},
               {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "trend"}},
               {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1",
                "content": result_content if result_content is not None else
                [{"type": "web_search_result", "url": u, "title": "t", "encrypted_content": "e", "page_age": None}
                 for u in results]},
               *extra,
               {"type": "text", "text": text[:half], "citations": None},
               {"type": "text", "text": text[half:],
                "citations": [{"type": "web_search_result_location", "url": u, "title": "t", "cited_text": "c",
                               "encrypted_index": "i"} for u in cited]}]
    return message(content=content, stop=stop, use=usage(5000, 1000, server_tool_use={"web_search_requests": searches,
                                                                                       "web_fetch_requests": 0}))


def trend(url, day="2026-10-10", brand="Altınmarka"):
    return {"trend": "Mini vlog akımı", "platform": "tiktok", "url": url, "date": day,
            "brand_idea": "Ürün hazırlığını 10 saniyelik vlog ile göster.", "brand": brand}


class Research(Base):
    def setUp(self):
        super().setUp()
        self.keys = []

    def key(self):
        self.keys.append(1)
        return KEY

    def chain(self):
        return [r for r in self.ops.backend.data["budget"]["reservations"] if r["reservation_key"].startswith("chain-model-")]


class TrendOnClaude(Research):
    def source(self, client, r=None):
        return ts.trend_search_source(self.ops, {"anthropic": self.key}, load_catalog(), self.cards,
                                      routing=r or routing(), client=client)

    def test_web_search_request_and_only_sourced_recent_items_survive(self):
        value = {"trends": [trend(GOOD + "?utm_source=x"), trend(CITED, brand="Fenerium"),
                            trend("https://never.searched.example/x"), trend(GOOD, day="2026-08-01"),
                            trend("http://www.example.org/trend/mini-vlog")]}
        client = FakeClient(research_message(value))
        items = self.source(client)(DAY)
        self.assertEqual([i["url"] for i in items], [GOOD, CITED])
        call = client.calls[0]
        self.assertEqual(call["tools"], [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}])
        self.assertEqual(call["output_config"], {"effort": "medium"})  # no format: citations + JSON in text
        self.assertEqual(call["model"], "claude-opus-5-5")
        self.assertIn("TEK bir JSON nesnesi", call["system"])
        for forbidden in ("tool_choice", "thinking", "fallbacks"):
            self.assertNotIn(forbidden, call)
        [r] = self.chain()
        # hold = 2 USD + 5 x 0.01 USD; actual = 5000x4 + 1000x20 + 2 searches x 10_000
        self.assertEqual((r["provider"], r["reserved_microdollars"], r["state"], r["actual_microdollars"]),
                         ("anthropic", 2_040_000, "settled", 20_000 + 20_000 + 20_000))
        self.assertEqual(self.source(client)(DAY), items)  # same day: cached, no second call
        self.assertEqual(len(client.calls), 1)

    def test_tool_type_comes_from_config(self):
        r = routing(web_search={"tool_type": "web_search_20250305", "max_uses": 3, "fee_microdollars_per_search": 10_000})
        client = FakeClient(research_message({"trends": []}))
        self.assertEqual(self.source(client, r)(DAY), [])
        self.assertEqual(client.calls[0]["tools"], [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}])

    def test_dynamic_filtering_blocks_are_tolerated(self):
        extra = [{"type": "server_tool_use", "id": "srvtoolu_2", "name": "code_execution", "input": {"code": "x"}},
                 {"type": "code_execution_tool_result", "tool_use_id": "srvtoolu_2",
                  "content": {"type": "code_execution_result", "stdout": "", "stderr": "", "return_code": 0,
                              "content": []}}]
        client = FakeClient(research_message({"trends": [trend(GOOD)]}, extra=extra))
        self.assertEqual(len(self.source(client)(DAY)), 1)

    def test_server_tool_error_pause_turn_and_non_json_fail_without_retry(self):
        cases = [(research_message({"trends": []}, result_content={"type": "web_search_tool_result_error",
                                                                    "error_code": "unavailable"}),
                  "web_search_tool_error"),
                 (research_message({"trends": []}, stop="pause_turn"), "response_pause_turn"),
                 (message(content=[{"type": "text", "text": "Bugün şu trendler var: ..."}]), "invalid_trend_schema"),
                 (research_message({"trends": [], "extra": 1}), "invalid_trend_schema"),
                 (research_message({"trends": []}, searches=6), "usage_limit_exceeded")]
        for n, (data, code) in enumerate(cases):
            day = date(2026, 10, 12 + n)  # distinct day -> distinct request digest
            ops_before = len(self.chain())
            client = FakeClient(data)
            src = self.source(client)
            with self.assertRaises(Exception) as ctx:
                src(day)
            self.assertEqual(getattr(ctx.exception, "code", None), "model_failed_no_retry:" + code, code)
            with self.assertRaises(Exception) as ctx:  # same day: never a second paid chance
                src(day)
            self.assertEqual(ctx.exception.code, "model_outcome_unknown_no_retry")
            self.assertEqual(len(client.calls), 1)
            self.assertEqual(len(self.chain()), ops_before + 1)

    def test_credit_error_and_cap(self):
        client = FakeClient(StatusError(402, "billing_error"))
        with self.assertRaises(Exception) as ctx:
            self.source(client)(DAY)
        self.assertEqual(ctx.exception.code, "model_failed_no_retry:anthropic_credit_insufficient")
        catalog = load_catalog(); catalog["budget"]["internal_live_cap_microdollars"] = 1
        client = FakeClient()
        src = ts.trend_search_source(self.ops, {"anthropic": self.key}, catalog, self.cards, routing=routing(),
                                     client=client)
        with self.assertRaises(ts.TrendSearchError):
            src(DAY)
        self.assertEqual((client.calls, len(self.keys)), ([], 1))


class SpecialDays(Research):
    def build(self, client, **flags):
        return ts.special_days_research_source(self.ops, {"anthropic": self.key}, load_catalog(), self.cards,
                                               routing=routing(**flags), client=client)

    def test_flag_off_by_default_means_no_provider(self):
        self.assertTrue(model_routing.load().flag("monday_special_days_research_enabled"))  # on per Ahmet 2026-10-11
        self.assertIsNone(self.build(FakeClient(), monday_special_days_research_enabled=False))
        self.assertEqual((self.keys, self.chain()), ([], []))

    def test_monday_only_sourced_days_in_window(self):
        days = {"days": [{"date": "2026-10-24", "name": "Birleşmiş Milletler Günü", "url": CITED, "brand": "Fenerium",
                          "idea": "Dünya kupası temalı taraftar serisi."},
                         {"date": "2026-10-13", "name": "Uydurma gün", "url": "https://unsearched.example/x",
                          "brand": "Altınmarka", "idea": "x"},
                         {"date": "2026-11-20", "name": "Çok ileri", "url": GOOD, "brand": "Altınmarka", "idea": "x"},
                         {"date": "2026-10-16", "name": "Dünya Gıda Günü", "url": GOOD, "brand": "Altınmarka",
                          "idea": "Kakao üreticisi hikâyesi."}]}
        client = FakeClient(research_message(days))
        research = self.build(client, monday_special_days_research_enabled=True)
        with self.assertRaises(ts.TrendSearchError):
            research(date(2026, 10, 13))  # Tuesday
        self.assertEqual(client.calls, [])
        out = research(DAY)
        self.assertEqual([(d["date"], d["name"]) for d in out],
                         [("2026-10-16", "Dünya Gıda Günü"), ("2026-10-24", "Birleşmiş Milletler Günü")])
        self.assertEqual(client.calls[0]["tools"][0]["type"], "web_search_20260209")
        self.assertIn("Pazartesi", client.calls[0]["system"])


class NewBusiness(Research):
    def build(self, client, **flags):
        return ts.new_business_research_source(self.ops, {"anthropic": self.key}, load_catalog(), self.cards,
                                               routing=routing(**flags), client=client)

    def test_flag_off_by_default_means_no_provider(self):
        self.assertTrue(model_routing.load().flag("new_business_research_enabled"))  # on per Ahmet 2026-10-11
        self.assertIsNone(self.build(FakeClient(), new_business_research_enabled=False))

    def test_three_to_five_sourced_non_client_brands(self):
        urls = [f"https://news.example.com/{i}" for i in range(5)]
        brands = {"brands": [{"brand": "Fenerium", "sector": "perakende", "reason": "Mevcut müşteri.", "url": urls[0]},
                             {"brand": "Yeni Kahve", "sector": "gıda", "reason": "Yeni yatırım aldı.", "url": urls[1]},
                             {"brand": "Mavi Mobil", "sector": "mobilite", "reason": "Lansman yapıyor.", "url": urls[2]},
                             {"brand": "Kuzey Kozmetik", "sector": "kozmetik", "reason": "Ajans arıyor.", "url": urls[3]},
                             {"brand": "Kaynaksız", "sector": "x", "reason": "x", "url": "https://nowhere.example/x"}]}
        client = FakeClient(research_message(brands, results=urls, cited=()))
        out = self.build(client, new_business_research_enabled=True)(DAY)
        self.assertEqual(out["status"], "ok")
        self.assertEqual([b["brand"] for b in out["brands"]], ["Yeni Kahve", "Mavi Mobil", "Kuzey Kozmetik"])
        self.assertTrue(all(b["reason"] and b["url"].startswith("https://") for b in out["brands"]))
        sent = json.loads(client.calls[0]["messages"][0]["content"])
        self.assertIn("Fenerium", sent["current_clients"])

    def test_fewer_than_three_is_flagged(self):
        brands = {"brands": [{"brand": "Yeni Kahve", "sector": "gıda", "reason": "Yatırım.", "url": GOOD}]}
        out = self.build(FakeClient(research_message(brands)), new_business_research_enabled=True)(DAY)
        self.assertEqual((out["status"], len(out["brands"])), ("insufficient_sourced_brands", 1))


class Packaging(unittest.TestCase):
    def test_prompts_packaged(self):
        import package_chain_source as pkg
        for name in ("chain_special_days_prompt.txt", "chain_new_business_prompt.txt"):
            self.assertIn(pkg.APP + name, pkg.ALLOWLIST)
            self.assertTrue((Path(__file__).with_name(name)).is_file())

    def test_strict_json_text(self):
        self.assertEqual(ts.strict_json_text('```json\n{"a": 1}\n```'), {"a": 1})
        for bad in ('Sonuç: {"a": 1}', '{"a": 1} ek metin', "[]"):
            with self.assertRaises(Exception):
                ts.strict_json_text(bad)


class Streaming(unittest.TestCase):
    def test_research_route_streams_and_wall_clock_is_bounded(self):
        import model
        route = model_routing.load().route("trend_radar")
        self.assertLessEqual(route.timeout_seconds, 900)

        class Client:
            def __init__(self):
                self.messages, self.used = self, []

            def create(self, **kw):
                self.used.append("create")
                raise AssertionError("research must stream")

            def stream(self, **kw):
                self.used.append("stream")
                return self

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __iter__(self):
                return iter(range(3))

            def get_final_message(self):
                return {"ok": True}

        c = Client()
        self.assertEqual(model._anthropic_stream(c, {}, 60), {"ok": True})
        ticks = iter([0, 10, 70, 200])
        with self.assertRaises(model._StreamDeadline):
            model._anthropic_stream(Client(), {}, 60, clock=lambda: next(ticks))
        with self.assertRaises(model.ModelError) as err:
            model.anthropic_send(KEY, route, {}, client=type("Slow", (Client,), {
                "__iter__": lambda self: (_ for _ in ()).throw(TimeoutError())})())
        self.assertEqual(err.exception.code, "transport_outcome_unknown")


class CacheKey(unittest.TestCase):
    def test_route_or_prompt_change_gives_a_new_research_key(self):
        import dataclasses
        route = model_routing.load().route("trend_radar")
        a = ts.route_fingerprint(route, ts.PROMPT)
        self.assertEqual(a, ts.route_fingerprint(route, ts.PROMPT))
        self.assertNotEqual(a, ts.route_fingerprint(dataclasses.replace(route, effort="high"), ts.PROMPT))
        self.assertNotEqual(a, ts.route_fingerprint(route, ts.SPECIAL_PROMPT))


if __name__ == "__main__":
    unittest.main()
