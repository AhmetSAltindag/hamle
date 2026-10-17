"""Aylık İçerik Planı taslağı: fakes only, no sockets, Basecamp, Gmail or real model calls."""
import copy
from datetime import date, datetime
import io
import json
from contextlib import redirect_stdout
import unittest

import v11_algorithm_watch as aw
import v11_content_plan as cp
import v11_daily_digest as dd
import worker
from test_anthropic_routing import FakeClient, KEY, message
from test_v11_daily_digest import Base
from v11_chain_live import load_catalog

DEC = date(2026, 12, 1)
NOW = datetime.fromisoformat("2026-10-17T15:00:00+03:00")


def config(**kw):
    c = copy.deepcopy(cp.load_config())
    c.update(kw)
    return c


def skeleton(first=DEC, skip=(), dup=False, recommended=19, extra=None, platform=lambda d: "instagram"):
    days = []
    for n, d in enumerate(cp.month_days(first)):
        if d.day in skip:
            continue
        days.append({"date": d.isoformat(), "pillar": "Şarj alışkanlığı", "topic": f"Konu {d.day}" if not (dup and d.day == 2)
                     else "Konu 1", "format": "reels" if n % 2 else "karusel", "platform": platform(d),
                     "special_day": {3: "Dünya Engelliler Günü", 31: "Yılbaşı"}.get(d.day), "why": "Kış yolculukları başlıyor.",
                     "recommended": n < recommended})
    out = {"strategy": ["Çatı mesaj: şarj her yerde, kolay."], "last_month": [],
           "pillars": [{"name": "Şarj alışkanlığı", "why": "Yeni EV sahipleri öğreniyor."},
                       {"name": "Ağ büyüyor", "why": "Yeni istasyonlar."}], "days": days}
    out.update(extra or {})
    return out


def details(request, tags=4, caption="Kışın şarj süresi uzar mı? Kısa cevap: biraz. #wat", linkedin=None):
    return {"items": [{"date": d["date"], "hook": "Kışın şarj neden yavaşlar?", "caption": caption,
                       "linkedin_caption": linkedin, "hashtags": [f"#etiket{i}" for i in range(tags)], "cta": "Uygulamayı indir.",
                       "visual_brief": "Karlı otoparkta WAT istasyonu, yakın plan konnektör.",
                       "on_screen_text": "Kış şarjı 101", "size": "9:16, 20-30 sn"} for d in request["days"]]}


class Fake:
    def __init__(self, sk=None, **kw):
        self.sk, self.kw, self.calls = sk or skeleton(), kw, []

    def __call__(self, task, request):
        self.calls.append((task, request))
        return copy.deepcopy(self.sk) if task == "skeleton" else details(request, **self.kw)


class Plan(Base):
    START = NOW

    def plan(self, model=None, **kw):
        kw.setdefault("month", "2026-12")
        self.model = model or Fake()
        return cp.run_content_plan(self.ops, config=config(), catalog=load_catalog(), cards=self.cards,
                                   cal=dd.load_calendar(), book_reader=lambda: aw.new_book(aw.load_seed()),
                                   upcoming_reader=lambda pid: [{"title": "Yılbaşı kampanyası görselleri",
                                                                 "due_on": "2026-12-20", "project_id": pid}],
                                   model=self.model, clock=lambda: self.now, **kw)


class Config(Plan):
    def test_disk_config_is_off_wat_only_and_own_model(self):
        cfg = cp.validate_config(cp.load_config())
        self.assertIs(cfg["enabled"], False)
        self.assertEqual(list(cfg["brands"]), ["wat_mobilite"])
        self.assertEqual(cfg["brands"]["wat_mobilite"]["platforms"], ["instagram", "linkedin"])
        self.assertIs(cfg["route"].prompt_cache, True)  # the first and only feature with the cache on (17 Oct)
        self.assertEqual(cfg["brands"]["wat_mobilite"]["posts"], (18, 20))
        import model_routing
        self.assertNotIn(cp.ROLE, model_routing.ROLES)
        for bad in ({"brands": {}}, {"model": {"model": "gpt"}},
                    {"brands": {"wat_mobilite": {"posts_per_month": [20, 18], "platforms": ["instagram"]}}}):
            with self.assertRaises(cp.ContentPlanError):
                cp.validate_config(config(**bad))

    def test_months(self):
        self.assertEqual(cp.target_month(date(2026, 10, 17)), date(2026, 11, 1))
        self.assertEqual(cp.target_month(date(2026, 10, 17), "2026-12"), DEC)
        for bad in ("2026-13", "2027-06", "2026-09", 202612):
            with self.assertRaises(cp.ContentPlanError):
                cp.target_month(date(2026, 10, 17), bad)
        self.assertEqual(len(cp.month_days(DEC)), 31)


class Build(Plan):
    def test_a_full_plan_has_every_day_details_and_a_clean_check(self):
        r = self.plan()
        self.assertEqual((r["status"], r["days"], r["recommended"], r["business_writes"]),
                         ("content_plan_preview", 31, 19, 0))
        self.assertEqual(r["model_calls"], 1 + 4)  # skeleton + 31 days in parts of 8
        self.assertEqual([t for t, _ in self.model.calls], ["skeleton"] + ["details"] * 4)
        self.assertEqual(r["score"], 100, r["text"][:300])
        text = r["text"]
        self.assertIn("WAT Mobilite · Aralık 2026 İçerik Planı (iç taslak)", text)
        self.assertIn("⭐ 19 öneri sözleşmedeki 18-20 paylaşım", text)
        self.assertIn(cp.STRATEGY_NOTE, text)
        self.assertIn("31.12 Per · Karusel · Instagram · Şarj alışkanlığı · Konu 31 · Özel gün: Yılbaşı", text)
        self.assertIn("Görsel brief: Karlı otoparkta", text)
        sk = self.model.calls[0][1]
        self.assertEqual(sk["material"]["upcoming_todos"], [{"title": "Yılbaşı kampanyası görselleri",
                                                             "due_on": "2026-12-20"}])
        self.assertTrue(sk["material"]["rules"])
        self.assertEqual([d["date"] for d in sk["material"]["special_days"]], ["2026-12-03", "2026-12-31"])
        self.assertLessEqual(len(json.dumps(sk, ensure_ascii=False).encode()), cp.MAX_INPUT_BYTES)

    def test_missing_days_repeats_count_and_rule_checks_are_flagged(self):
        r = self.plan(model=Fake(skeleton(skip=(5, 6), dup=True, recommended=25), tags=1,
                                 caption="Sponsorlu gönderiyle duyuralım."))
        text = r["text"]
        self.assertIn("🔴 2 gün için öneri yok: 05, 06", text)
        self.assertIn("Tekrar eden konu: 02 ve 01", text)
        self.assertIn("Önerilen içerik sayısı 23; sözleşme 18-20", text)
        self.assertIn("Instagram'da 3-5 niş hashtag önerilir; uymayan günler: 01, 02, 03, 04, 07,", text)
        self.assertIn("🔴 Reklam / sponsorlu içerik geçiyor (01, 02,", text)
        self.assertEqual(r["findings"], 5)
        self.assertIn("05.12 Cmt · öneri yok", text)
        self.assertEqual(r["score"], 100 - 2 * 15 - 3 * 3)

    def test_the_same_news_on_instagram_and_linkedin(self):
        both = lambda d: cp.BOTH if d.day % 3 == 0 else "linkedin" if d.day == 1 else "instagram"
        r = self.plan(model=Fake(skeleton(platform=both), linkedin="Ağımız büyüyor: detaylar https://wat.com.tr"))
        text = r["text"]
        self.assertIn("03.12 Per · Karusel · Instagram + LinkedIn · ", text)
        self.assertIn("01.12 Sal · Karusel · LinkedIn · ", text)
        self.assertIn("LinkedIn metni:\nAğımız büyüyor", text)
        self.assertEqual(text.count("LinkedIn metni:"), 10)  # only on the shared days
        self.assertIn("LinkedIn caption'ında link var (03, 06, 09,", text)
        self.assertEqual(self.model.calls[0][1]["material"]["brand"]["platforms"], ["instagram", "linkedin"])

    def test_a_failed_model_holds_and_writes_nothing(self):
        def broken(task, request):
            raise RuntimeError("model down")
        before = set(self.ops.backend.data)
        r = self.plan(model=broken)
        self.assertEqual((r["status"], r["reason"]), ("held", "RuntimeError"))
        self.assertEqual(set(self.ops.backend.data), before)

    def test_live_runs_are_not_built_yet(self):
        self.assertEqual(self.plan(preview=False)["reason"], "content_plan_live_not_built")


class Model(Plan):
    def test_the_real_call_path_is_cached_per_request(self):
        sk = skeleton()
        outputs = [message(sk)]
        for n in range(4):
            days = cp.month_days(DEC)[n * 8:(n + 1) * 8]
            outputs.append(message(details({"days": [{"date": d.isoformat()} for d in days]})))
        client = FakeClient(*outputs)
        call = cp.model_source(self.ops, {"anthropic": lambda: KEY}, load_catalog(), cp.validate_config(config()),
                               client=client)
        r = self.plan(model=call)
        self.assertEqual((r["status"], r["days"]), ("content_plan_preview", 31), r)
        self.assertEqual(len(client.calls), 5)
        self.assertEqual(client.calls[0]["model"], "claude-opus-5-5")
        system = client.calls[0]["system"]
        self.assertEqual(system[0]["cache_control"], {"type": "ephemeral"})  # cached system prompt
        self.assertIn("KAPSAM DIŞI", system[0]["text"])
        self.assertIsInstance(client.calls[0]["messages"][0]["content"], str)  # skeleton: one plain input
        for c in client.calls[1:]:
            self.assertEqual(c["system"], system)
            shared, days = c["messages"][0]["content"]
            self.assertEqual(shared["cache_control"], {"type": "ephemeral"})
            self.assertTrue(shared["text"].startswith("Ortak girdi: "))
            self.assertIn("strategy", shared["text"])
            self.assertNotIn('"days"', shared["text"])
            self.assertEqual(sorted(json.loads(days["text"])), ["days", "material", "task"])
        self.assertEqual(len({c["messages"][0]["content"][0]["text"] for c in client.calls[1:]}), 1)  # same prefix
        self.assertEqual(r["usage"]["paid_calls"], 5)
        self.assertGreater(r["usage"]["cost_usd"], 0)
        self.assertEqual(r["usage"]["cost_without_cache_usd"], r["usage"]["cost_usd"])  # fake usage has no cache tokens
        self.assertEqual(self.plan(model=call)["text"], r["text"])  # same requests: cached, no second paid call
        self.assertEqual(len(client.calls), 5)

    def test_usage_totals_show_the_saving(self):
        use = {"input_tokens": 1000, "cache_read_input_tokens": 5000, "cache_creation_input_tokens": 0,
               "output_tokens": 2000, "web_search_requests": 0}
        import model
        rec = {"model": "claude-opus-5-5", "usage": use,
               "cost_microdollars": model.anthropic_cost_microdollars("claude-opus-5-5", use)}
        t = cp.usage_totals([rec])
        self.assertEqual((t["cost_usd"], t["cost_without_cache_usd"]), (0.045, 0.064))

    def test_cache_off_sends_todays_request(self):
        outputs = [message(skeleton())] + [message(details({"days": [{"date": d.isoformat()}
                                                                      for d in cp.month_days(DEC)[n * 8:(n + 1) * 8]]}))
                                           for n in range(4)]
        client = FakeClient(*outputs)
        cfg = config()
        cfg["model"] = dict(cfg["model"], prompt_cache=False)
        call = cp.model_source(self.ops, {"anthropic": lambda: KEY}, load_catalog(), cp.validate_config(cfg),
                               client=client)
        self.assertEqual(self.plan(model=call)["status"], "content_plan_preview")
        for c in client.calls:
            self.assertIsInstance(c["system"], str)
            self.assertIsInstance(c["messages"][0]["content"], str)
        self.assertIn('"strategy"', client.calls[1]["messages"][0]["content"])


class Wiring(unittest.TestCase):
    def test_worker_flags(self):
        for argv, reason in ((["--mode", "content_plan"], "content_plan_requires_plan_preview"),
                             (["--mode", "daily_digest", "--plan-preview"], "content_plan_requires_plan_preview"),
                             (["--mode", "daily_digest", "--plan-brand", "wat_mobilite"], "plan_flags_require_content_plan")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(worker.main(argv), 2)
            self.assertEqual(json.loads(buf.getvalue())["reason"], reason, argv)
        self.assertIn("content_plan_preview", worker.OK_STATUSES)


if __name__ == "__main__":
    unittest.main()
