"""Aylık İçerik Planı taslağı: fakes only, no sockets, Basecamp, Gmail or real model calls."""
import copy
from datetime import date, datetime
import io
import json
from contextlib import redirect_stdout
import unittest
from unittest import mock

from urllib.parse import urlsplit

import v11_algorithm_watch as aw
import v11_content_plan as cp
import v11_content_plan_basecamp as pb
import v11_daily_digest as dd
import worker
from test_anthropic_routing import FakeClient, KEY, message
from test_v11_daily_digest import Base
from v11_chain_live import load_catalog

DEC = date(2026, 12, 1)
NOW = datetime.fromisoformat("2026-10-17T15:00:00+03:00")
AHMET, CANSU, OZGE, BERKAY = 47470759, 48793256, 52096503, 47658175


def config(**kw):
    c = copy.deepcopy(cp.load_config())
    c.update(enabled=False, basecamp_write=False)  # each test turns the switches on itself
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

    def plan(self, model=None, cfg=None, **kw):
        kw.setdefault("month", "2026-12")
        self.model = model or Fake()
        return cp.run_content_plan(self.ops, config=config(**(cfg or {})), catalog=load_catalog(), cards=self.cards,
                                   cal=dd.load_calendar(), book_reader=lambda: aw.new_book(aw.load_seed()),
                                   upcoming_reader=lambda pid: [{"title": "Yılbaşı kampanyası görselleri",
                                                                 "due_on": "2026-12-20", "project_id": pid}],
                                   model=self.model, clock=lambda: self.now, **kw)


class Config(Plan):
    def test_disk_config_is_wat_only_and_own_model(self):
        cfg = cp.validate_config(cp.load_config())
        # Ahmet 17 Oct 15:25Z "Basecamp'e yaz": on for WAT only; a write still needs --plan-write with brand + month.
        self.assertEqual((cfg["enabled"], cfg["basecamp_write"]), (True, True))
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

    def test_new_standard_sections(self):
        extra = {"brand_asks": [{"what": "Aralık kampanya detayları", "who": "WAT pazarlama ekibi", "by": "1 Aralık"}],
                 "algorithm_moves": ["Reels'te ilk 3 saniyeye kanca koyuyoruz."],
                 "shoots": [{"title": "Kış şarjı çekimi", "dates": ["2026-12-04", "2026-12-40"], "place": "Maslak istasyonu",
                             "cast": "[ekip doldurur]", "scenes": ["İstasyona geliş", "Soketi takma"]}]}
        sk = skeleton(extra=extra)
        sk["days"][3]["series"] = "EV 101"
        r = self.plan(model=Fake(sk))
        t = r["text"]
        self.assertEqual(r["score"], 100, t[:400])
        for line in ("KAMPANYA TAKVİMİ", "- Her Salı WAT'la: 13.10 - 31.12 · Her salı %10", "MARKADAN BEKLEDİKLERİMİZ",
                     "- Aralık kampanya detayları · kimden: WAT pazarlama ekibi · ne zamana: 1 Aralık",
                     "BU AY ALGORİTMADA NE DEĞİŞTİ, BİZ NE YAPIYORUZ", "→ Reels'te ilk 3 saniyeye kanca koyuyoruz.",
                     "ÇEKİM PLANI", "- Kış şarjı çekimi · 04.12 · yer: Maslak istasyonu", "Seri: EV 101",
                     "Durum: Taslak"):
            self.assertIn(line, t)
        self.assertNotIn("YKB World", t)  # ended in November: not in December's campaign calendar
        sk_in = self.model.calls[0][1]["material"]["brand"]
        self.assertEqual(sk_in["ended_campaigns"], ["YKB World"])
        self.assertIn("provizyon", sk_in["banned_terms"])
        self.assertTrue(self.model.calls[1][1]["brand"]["facts"])  # facts reach the detail calls too

    def test_stale_facts_and_campaign_dates_turn_red(self):
        r = self.plan(model=Fake(caption="Provizyon nedir? YKB World ile şarj avantajı. #wat"))
        self.assertIn("🔴 Güncel olmayan bilgi: \"provizyon\" geçiyor (01, 02,", r["text"])
        self.assertIn("🔴 YKB World kampanyası bu aydan önce bitiyor ama planda geçiyor (01,", r["text"])
        cfg = cp.load_config()
        cfg["brands"]["wat_mobilite"]["campaigns"] = [{"name": "Kış Kampanyası", "start": "2026-12-10",
                                                        "end": "2026-12-20"}]
        r = self.plan(model=Fake(caption="Kış Kampanyası başladı! #wat"), cfg={"brands": cfg["brands"]})
        self.assertIn("🔴 Kış Kampanyası kampanya tarihleri dışında (01, 02,", r["text"])
        self.assertNotIn(" 10, ", r["text"].split("kampanya tarihleri dışında")[1].split(")")[0])

    def test_denied_ad_word_is_a_style_note(self):
        r = self.plan(model=Fake(caption="Doğal ışık, cilalı reklam estetiği yok. #wat"))
        self.assertNotIn("Reklam / sponsorlu", r["text"])
        self.assertEqual(r["score"], 100)
        self.assertTrue(cp.out_of_scope("Reklam yok ama sponsorlu gönderi var"))
        self.assertTrue(cp.out_of_scope("Boost edelim"))
        # Y7 17 Oct, 25.12 visual brief
        self.assertFalse(cp.out_of_scope("İstasyon ve araç kadrajın içinde, cilalı reklam estetiği yerine samimi an."))

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

    def test_live_runs_need_both_switches_and_preview_shows_the_target(self):
        self.assertEqual(self.plan(preview=False)["reason"], "content_plan_disabled")
        r = self.plan(preview=False, cfg=dict(enabled=True))
        self.assertEqual(r["reason"], "content_plan_basecamp_write_off")
        self.assertEqual(self.model.calls, [])
        bc = self.plan()["basecamp"]
        self.assertEqual((bc["project_id"], bc["list_id"], bc["assignee_ids"], bc["subscriber_ids"]),
                         (40694338, 8231779952, [OZGE], [AHMET, BERKAY, CANSU]))
        self.assertEqual(bc["document"], "WAT Mobilite · Aralık 2026 İçerik Planı (Hamle taslağı)")


class FakeBasecamp:
    """Basecamp for project 40694338 only; records every call."""
    def __init__(self, doc_visible=False):
        self.calls, self.doc_visible = [], doc_visible

    def send(self, method, url, body, expected):
        path = urlsplit(url).path
        self.calls.append((method, path, body))
        b = "/5898865/buckets/40694338"
        if path == "/5898865/projects/40694338.json":
            return {"id": 40694338, "dock": [{"name": "message_board", "id": 1, "enabled": True},
                                             {"name": "vault", "id": 777, "enabled": True}]}, {}
        if path == f"{b}/vaults/777.json":
            return {"id": 777, "bucket": {"id": 40694338}, "status": "active"}, {}
        if path == f"{b}/todolists/8231779952.json":
            return {"id": 8231779952, "bucket": {"id": 40694338}, "visible_to_clients": False, "status": "active"}, {}
        if method == "POST" and path == f"{b}/vaults/777/documents.json":
            self.doc = body
            return {"id": 9001}, {}
        if path == f"{b}/documents/9001.json":
            return {"id": 9001, "title": self.doc["title"], "bucket": {"id": 40694338}, "parent": {"id": 777},
                    "creator": {"id": 53358100}, "visible_to_clients": self.doc_visible,
                    "app_url": "https://3.basecamp.com/5898865/buckets/40694338/documents/9001"}, {}
        if method == "POST" and path == f"{b}/todolists/8231779952/todos.json":
            self.todo = body
            return {"id": 9002}, {}
        if path == f"{b}/todos/9002.json":
            t = self.todo
            return {"id": 9002, "content": t["content"], "parent": {"id": 8231779952}, "bucket": {"id": 40694338},
                    "creator": {"id": 53358100}, "visible_to_clients": False,
                    "assignees": [{"id": i} for i in t["assignee_ids"]],
                    "completion_subscribers": [{"id": i} for i in t["completion_subscriber_ids"]],
                    "app_url": "https://3.basecamp.com/5898865/buckets/40694338/todos/9002"}, {}
        raise AssertionError(f"unexpected {method} {path}")


try:
    import weasyprint  # noqa: F401  (in the cloud image; optional on a laptop)
    HAVE_PDF = True
except Exception:
    HAVE_PDF = False


class Write(Plan):
    def setUp(self):
        super().setUp()
        # The real PDF is checked in Files and by the image's content_plan_files_smoke; here only the upload path.
        import v11_content_plan_files as pf
        patcher = mock.patch.object(pf, "plan_pdf", lambda items: b"%PDF-1.7 fake plan")
        patcher.start()
        self.addCleanup(patcher.stop)

    def raw(self, method, url, data, mime, expected):
        self.uploads.append((method, url, mime, data[:4]))
        return {"attachable_sgid": f"BAh7CEkiCGdpZAY6BkVU{len(self.uploads)}"}

    def live(self, bc, model=None, notify=True):
        def factory(dest):
            return pb.PlanBasecamp(5898865, dest["project_id"], dest["list_id"], send=bc.send,
                                   send_raw=self.raw)
        self.uploads = []
        return self.plan(model=model, preview=False, cfg=dict(enabled=True, basecamp_write=True),
                         http_factory=factory, notify=notify)

    def test_a_quiet_write_notifies_only_ahmet(self):
        bc = FakeBasecamp()
        r = self.live(bc, notify=False)
        self.assertEqual(r["status"], "content_plan_written", r.get("reason"))
        self.assertEqual((bc.doc["subscriptions"], bc.todo["notify"]), ([AHMET], False))
        self.assertEqual(bc.todo["assignee_ids"], [OZGE])
        loud = FakeBasecamp()
        self.ops.backend.data.pop("content-plan-wat_mobilite-202612")
        self.live(loud)
        self.assertNotIn("subscriptions", loud.doc)
        self.assertIs(loud.todo["notify"], True)

    def test_one_document_and_one_todo_then_never_again(self):
        bc = FakeBasecamp()
        r = self.live(bc)
        self.assertEqual((r["status"], r["business_writes"]), ("content_plan_written", 2), r.get("reason"))
        posts = [(m, p) for m, p, _ in bc.calls if m == "POST"]
        self.assertEqual([p.rsplit("/", 1)[-1] for _, p in posts], ["documents.json", "todos.json"])
        self.assertIn("<h1>Takvim</h1>", bc.doc["content"])
        self.assertEqual([(m, u.split("?")[0].rsplit("/", 1)[-1], d) for m, u, _, d in self.uploads],
                         [("POST", "attachments.json", b"%PDF"), ("POST", "attachments.json", b"PK\x03\x04")])
        self.assertIn("Icerik-Plani-Hamle-taslagi.pdf", self.uploads[0][1])
        self.assertIn('<bc-attachment sgid="BAh7CEkiCGdpZAY6BkVU1"></bc-attachment>', bc.doc["content"])
        self.assertIn('<bc-attachment sgid="BAh7CEkiCGdpZAY6BkVU2"></bc-attachment>', bc.doc["content"])
        self.assertIn("<strong>31.12 Per · Karusel · Instagram</strong>", bc.doc["content"])
        self.assertEqual((bc.todo["assignee_ids"], bc.todo["completion_subscriber_ids"], bc.todo["notify"]),
                         ([OZGE], [AHMET, BERKAY, CANSU], True))
        self.assertIn("documents/9001", bc.todo["description"])
        self.assertIn("linki bu göreve yorum olarak yazsın", bc.todo["description"])
        row = self.ops.backend.data["content-plan-wat_mobilite-202612"]
        self.assertEqual((row["state"], row["doc_id"], row["todo_id"]), ("ready", 9001, 9002))
        self.assertEqual(len(row["plan"]["days"]), 31)
        again = self.live(FakeBasecamp(), model=Fake())
        self.assertEqual((again["status"], again["reason"], self.model.calls), ("no_new_work",
                                                                                 "content_plan_already_recorded", []))

    def test_a_document_clients_could_see_stops_before_the_todo(self):
        bc = FakeBasecamp(doc_visible=True)
        r = self.live(bc)
        self.assertEqual((r["status"], r["reason"]), ("held", "document:content_plan_doc_readback_mismatch"))
        self.assertFalse([p for m, p, _ in bc.calls if p.endswith("todos.json")])
        self.assertEqual(self.ops.backend.data["content-plan-wat_mobilite-202612"]["state"], "unknown")

    def test_a_failed_model_writes_nothing_and_blocks_the_month(self):
        def broken(task, request):
            raise RuntimeError("model down")
        bc = FakeBasecamp()
        r = self.live(bc, model=broken)
        self.assertEqual(r["status"], "held")
        self.assertEqual(bc.calls, [])
        self.assertEqual(self.ops.backend.data["content-plan-wat_mobilite-202612"]["state"], "failed")

    def test_calls_outside_the_brand_project_are_refused(self):
        http = pb.PlanBasecamp(5898865, 40694338, 8231779952, send=FakeBasecamp().send)
        for method, url in (("POST", "https://3.basecampapi.com/5898865/buckets/48639990/todolists/1/todos.json"),
                            ("PUT", "https://3.basecampapi.com/5898865/buckets/40694338/todos/5.json"),
                            ("POST", "https://3.basecampapi.com/5898865/buckets/40694338/todolists/1/todos.json")):
            with self.assertRaises(Exception):
                http.call(method, url, {}, 201)


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
    def test_worker_entry_builds_the_basecamp_writer_only_for_a_write(self):
        import cloud_providers
        seen = []
        fake_session = type("S", (), {"close": lambda self: None})
        with mock.patch.object(cp, "run_content_plan", lambda ops, **kw: seen.append(kw) or {"status": "x"}), \
                mock.patch.object(cp, "model_source", lambda *a, **k: None), \
                mock.patch.object(cloud_providers, "_session", lambda: fake_session()):
            cp.run_worker_content_plan(None, {}, brand="wat_mobilite", month="2026-12")
            cp.run_worker_content_plan(None, {}, brand="wat_mobilite", month="2026-12", preview=False, notify=False)
        self.assertEqual((seen[0]["preview"], seen[0]["http_factory"], seen[0]["notify"]), (True, None, True))
        self.assertEqual((seen[1]["preview"], seen[1]["notify"]), (False, False))
        dest = pb.target(load_catalog(), cp.validate_config(cp.load_config()), "wat_mobilite")
        http = seen[1]["http_factory"](dest)
        self.assertIsInstance(http, pb.PlanBasecamp)
        self.assertIsNotNone(http.send_raw)
        self.assertEqual(dest["principal_id"], AHMET)

    def test_worker_flags(self):
        need = "content_plan_requires_plan_preview_or_write"
        for argv, reason in ((["--mode", "content_plan"], need),
                             (["--mode", "daily_digest", "--plan-preview"], need),
                             (["--mode", "content_plan", "--plan-preview", "--plan-write"], need),
                             (["--mode", "daily_digest", "--plan-brand", "wat_mobilite"], "plan_flags_require_content_plan"),
                             (["--mode", "content_plan", "--plan-write", "--plan-brand", "wat_mobilite"],
                              "plan_write_requires_brand_and_month"),
                             (["--mode", "content_plan", "--plan-preview", "--plan-quiet"], "plan_quiet_requires_plan_write")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(worker.main(argv), 2)
            self.assertEqual(json.loads(buf.getvalue())["reason"], reason, argv)
        self.assertIn("content_plan_preview", worker.OK_STATUSES)
        self.assertIn("content_plan_written", worker.OK_STATUSES)


class Files(Plan):
    @unittest.skipUnless(HAVE_PDF, "WeasyPrint is installed in the cloud image")
    def test_preview_carries_the_plan_and_makes_pdf_and_docx(self):
        import tempfile
        import zipfile
        from pathlib import Path
        import v11_content_plan_files as pf
        r = self.plan()
        p = r["plan_data"]
        self.assertEqual((p["name"], len(p["plan"]["days"])), ("WAT Mobilite", 31))
        self.assertNotIn("model_calls", p["plan"])
        json.dumps(r)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "r.json").write_text(json.dumps(r), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                pf.main([str(Path(tmp, "r.json")), tmp])
            pdf = Path(tmp, "WAT-Mobilite-2026-12-Icerik-Plani-Hamle-taslagi.pdf").read_bytes()
            docx = Path(tmp, "WAT-Mobilite-2026-12-Icerik-Plani-Hamle-taslagi.docx")
            self.assertTrue(pdf.startswith(b"%PDF-"))
            with zipfile.ZipFile(docx) as z:
                xml = z.read("word/document.xml").decode("utf-8")
            self.assertIn("Takvim", xml)
            self.assertIn("Aralık 2026", xml)

    def test_smoke_is_packaged_and_run_by_the_image(self):
        import package_chain_source as pkg
        self.assertIn(pkg.APP + "content_plan_files_smoke.py", pkg.ALLOWLIST)
        self.assertIn("python content_plan_files_smoke.py", open("Dockerfile", encoding="utf-8").read())

    def test_worker_splits_a_big_preview_into_log_sized_lines(self):
        result = {"status": "content_plan_preview", "score": 85, "usage": {"cost_usd": 0.9},
                  "plan_data": {"plan": {"days": ["x" * 50000]}}, "text": "ş" * 130000}
        buf = io.StringIO()
        with redirect_stdout(buf):
            worker._emit(result)
        lines = buf.getvalue().splitlines()
        self.assertTrue(all(len(line.encode("utf-8")) < 100000 for line in lines))
        head = json.loads(lines[0])
        self.assertEqual((head["usage"], head["text_parts"], "text" in head), ({"cost_usd": 0.9}, 7, False))
        self.assertEqual(json.loads(lines[1])["plan_data"], result["plan_data"])
        self.assertEqual("".join(json.loads(x)["text"] for x in lines[2:]), result["text"])

    def test_file_names_are_ascii(self):
        import v11_content_plan_files as pf
        self.assertEqual(pf.file_name("Yâ Da Kozmetik", DEC, "docx"),
                         "Ya-Da-Kozmetik-2026-12-Icerik-Plani-Hamle-taslagi.docx")


if __name__ == "__main__":
    unittest.main()
