"""Haftalık marka özeti (Ahmet 2026-10-15): fakes only, no sockets, Basecamp, Gmail or models."""
import copy
from contextlib import contextmanager
from datetime import date, datetime
import io
import json
import socket
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import model as base_model
import model_routing
import v11_daily_digest as dd
import v11_weekly_summary as ws
from cloud_store import OperationsStore
from test_anthropic_routing import FakeClient, KEY, message
from test_cloud_store import MemoryBackend, budget_snapshot
from test_v11_daily_digest import Gmail
from v11_chain_live import load_catalog

ACC, WAT, RABAM = 5898865, 40694338, 48639990
API = f"https://3.basecampapi.com/{ACC}"
APP = f"https://3.basecamp.com/{ACC}"
FRI_1735 = datetime.fromisoformat("2026-10-16T17:35:00+03:00")
TOKEN = "synthetic-placeholder-token-never-live"


def todo(i, title, due, people=(), pid=WAT):
    return {"id": i, "title": title, "status": "active", "completed": False, "due_on": due,
            "assignees": [{"id": n, "name": name} for n, name in enumerate(people, 1)],
            "app_url": f"{APP}/buckets/{pid}/todos/{i}", "created_at": "2026-10-01T09:00:00Z"}


class Basecamp:
    """GET-only fake of the digest reader's transport; unknown URLs fail like a non-200 (overdue report too)."""
    def __init__(self, todos=None):
        self.calls = []
        todos = todos if todos is not None else [
            todo(1, "Ekim post takvimi", "2026-10-13", ["Berkay Demir"]),     # 3 days overdue
            todo(2, "Lansman görseli", "2026-10-16", ["Özge Yağmur"]),        # due today, open
            todo(3, "Kasım planı", "2026-10-21", ["Esma Kaya"]),              # next week
            todo(4, "Yılbaşı kampanyası", "2026-11-20"),                      # too far
            todo(5, "Hamle Fikir: WAT × Cumhuriyet", "2026-10-10")]            # Hamle's own idea to-do
        self.pages = {
            f"{API}/projects.json": [{"id": WAT, "name": "WAT Mobilite", "status": "active",
                                      "dock": [{"name": "todoset", "id": 11, "enabled": True}]}],
            f"{API}/buckets/{WAT}/todosets/11/todolists.json": [{"id": 101}],
            f"{API}/buckets/{WAT}/todolists/101/todos.json": todos}

    def get(self, url):
        self.calls.append(url)
        if url not in self.pages:
            raise dd.DigestError("basecamp_read_failed")
        return copy.deepcopy(self.pages[url]), {}


def config(**kw):
    c = dd.load_config()
    c.update(kw)
    return c


def event(d, kind, title, url=None):
    return {"date": d, "source": "basecamp", "kind": kind, "title": title, "detail": "Ayrıntı.", "people": [],
            "url": url}


def line(kind, text, refs, url=None):
    return {"kind": kind, "text": text, "refs": list(refs), "url": url}


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.now = datetime.fromisoformat("2026-10-12T09:00:00+03:00")  # control opened earlier in the week
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: self.now)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.ops.resume_review_only("offline-test")
        self.now = FRI_1735
        self.catalog = load_catalog()
        self.bc, self.gmail, self.client = Basecamp(), Gmail(), FakeClient()

        @contextmanager
        def token():
            yield TOKEN
        self.writer = dd.DigestMailWriter(token_context=token, session_factory=lambda: self.gmail)
        self.model = ws.summary_source(self.ops, lambda: KEY, self.catalog, routing=model_routing.load(),
                                       client=self.client)
        data = self.ops.backend.data
        data["brand-memory-wat_mobilite-202610"] = {"kind": "brand_memory_month", "events": [
            event("2026-10-08", "bilgi", "Geçen haftanın olayı"),
            event("2026-10-12", "teslim", "Ekim reels seti teslim edildi", f"{APP}/buckets/{WAT}/todos/9"),
            event("2026-10-14", "talep", "Müşteri yeni banner istedi")], "days_done": []}
        data["client-jobs"] = {"keys": ["client-job-1", "client-job-2"]}
        data["client-job-1"] = {"kind": "client_job", "brand_key": "wat_mobilite", "state": "open",
                                "title_core": "Banner revizesi", "due_on": "2026-10-20", "assignee_name": "Berkay Demir",
                                "app_url": f"{APP}/buckets/{WAT}/todos/7", "created_at": "2026-10-14T09:00:00+03:00"}
        data["client-job-2"] = {"kind": "client_job", "brand_key": "rabam", "state": "closed", "title_core": "Eski iş",
                                "created_at": "2026-09-01T09:00:00+03:00", "closed_at": "2026-09-05T09:00:00+03:00"}

    def answer(self, *outputs):
        self.client.outputs = [message(o, model_id="claude-haiku-5-5") for o in outputs]

    def reader(self):
        return dd.BasecampDigestReader(ACC, get=self.bc.get, excluded=dd.validate_config(config())["excluded"])

    def go(self, cfg=None, **kw):
        kw.setdefault("model", self.model)
        kw.setdefault("writer", self.writer)
        return ws.run_weekly_summary(self.ops, config=cfg or config(), catalog=self.catalog,
                                     reader_factory=self.reader, clock=lambda: self.now, **kw)

    def request(self, n=0):
        return json.loads(self.client.calls[n]["messages"][0]["content"])

    def good(self):
        return {"brands": [{"key": "wat_mobilite", "lines": [
            line("oldu", "Müşteri yeni banner istedi; @Berkay revizeyi aldı.", ["e1", "i1"]),
            line("teslim", "Ekim reels seti teslim edildi.", ["e2"], f"{APP}/buckets/{WAT}/todos/9"),
            line("risk", "“Ekim post takvimi” 3 gündür gecikmiş @Berkay.", ["g1"], f"{APP}/buckets/{WAT}/todos/1"),
            line("gelecek", "“Kasım planı” 21 Ekim'de teslim @Esma.", ["h1"], f"{APP}/buckets/{WAT}/todos/3")]}]}


class Window(Base):
    def test_only_friday_from_1730_to_1830_istanbul_else_a_no_op(self):
        for when in ("2026-10-15T17:35:00+03:00", "2026-10-16T17:29:00+03:00", "2026-10-16T18:31:00+03:00",
                     "2026-10-17T17:35:00+03:00", "2026-10-16T15:35:00+00:00"):
            self.now = datetime.fromisoformat(when)
            r = self.go()
            self.assertEqual((r["status"], r["reason"], r["business_writes"]),
                             ("no_new_work", "outside_weekly_summary_window", 0), when)
        self.assertEqual((self.gmail.calls, self.bc.calls, self.client.calls), ([], [], []))
        self.assertFalse([k for k in self.ops.backend.data if k.startswith("weekly-summary-")])
        self.assertTrue(ws.in_window(datetime.fromisoformat("2026-10-16T18:30:00+03:00")))
        self.assertEqual(ws.week_key(date(2026, 10, 16)), "weekly-summary-202642")
        self.assertEqual(ws.week_key(date(2027, 1, 1)), "weekly-summary-202653")  # ISO year of the week

    def test_disabled_config_is_a_no_op(self):
        r = self.go(config(enabled=False))
        self.assertEqual((r["status"], r["reason"]), ("no_new_work", "digest_disabled_or_no_recipients"))
        self.assertEqual(self.gmail.calls, [])


class Send(Base):
    def test_friday_run_sends_one_mail_to_the_digest_recipients_and_never_twice_in_the_week(self):
        self.answer(self.good())
        r = self.go()
        self.assertEqual((r["status"], r["key"], r["business_writes"]), ("weekly_summary_sent", "weekly-summary-202642", 1))
        self.assertEqual(len(self.gmail.posts()), 1)
        msg = self.gmail.message()
        digest_to = dd.validate_config(dd.load_config())["recipients"]
        self.assertEqual([a.strip() for a in str(msg["To"]).split(",")], digest_to)
        self.assertTrue(all(a.endswith("@gamechangers.com.tr") for a in digest_to))
        self.assertEqual(str(msg["Subject"]), "Hamle AI: 16 Ekim Cuma Haftalık Marka Özeti")
        self.assertIsNone(msg["Cc"])
        text = self.gmail.text().replace("\r\n", "\n")
        self.assertTrue(text.split("\n")[3].startswith("WAT"))  # catalog order, digest label
        self.assertIn(f"⚠️ “Ekim post takvimi” 3 gündür gecikmiş @Berkay. · {APP}/buckets/{WAT}/todos/1", text)
        self.assertTrue(text.rstrip().endswith(ws.FOOTER))
        self.assertIn("Her cuma 17:30", ws.FOOTER)
        for name in ("Rabam", "BWI", "HAN Spaces", "ya da", "Altınmarka", "Bikaldi", "Fenerium"):
            self.assertIn(dd._upper_tr(name) + "\n- " + ws.NOTHING_THIS_WEEK, text, name)
        self.assertNotIn("GAMECHANGERS", text)
        self.assertNotIn("NEW BIZ", text)
        self.assertIn(f'href="{APP}/buckets/{WAT}/todos/9"', self.gmail.html())
        # one model call for the only brand with material; Hamle's suggestion to-do and old events left out
        self.assertEqual(len(self.client.calls), 1)
        req = self.request()
        self.assertEqual([b["key"] for b in req["brands"]], ["wat_mobilite"])
        b = req["brands"][0]
        self.assertEqual([e["title"] for e in b["events"]], ["Müşteri yeni banner istedi", "Ekim reels seti teslim edildi"])
        self.assertEqual([(x["ref"], x["title"]) for x in b["overdue"]], [("g1", "Ekim post takvimi")])
        self.assertEqual([x["title"] for x in b["due_today"]], ["Lansman görseli"])
        self.assertEqual([x["title"] for x in b["next_week"]], ["Kasım planı"])
        self.assertEqual([x["title"] for x in b["jobs"]], ["Banner revizesi"])
        self.assertEqual(req["week"], {"from": "2026-10-10", "to": "2026-10-16", "next_week_to": "2026-10-25"})
        self.assertTrue(all(u.startswith(API) for u in self.bc.calls))  # GET-only reader, allowlisted paths
        row = self.ops.backend.data["weekly-summary-202642"]
        self.assertEqual((row["state"], row["kind"], row["recipients"]), ("sent", "weekly_summary", digest_to))
        self.assertEqual(row["stats"]["brands"]["wat_mobilite"], {"lines": 4, "source": "model"})
        # a second run in the same ISO week never sends again (and makes no model call)
        self.now = datetime.fromisoformat("2026-10-16T18:10:00+03:00")
        again = self.go()
        self.assertEqual((again["status"], again["key"]), ("already_recorded", "weekly-summary-202642"))
        self.assertEqual((len(self.gmail.posts()), len(self.client.calls)), (1, 1))

    def test_unknown_send_outcome_is_never_retried(self):
        self.answer(self.good())
        self.gmail.fail = RuntimeError("socket reset")
        r = self.go()
        self.assertEqual((r["status"], r["reason"]), ("held", "weekly_summary_outcome_unknown_no_retry"))
        self.assertEqual(self.ops.backend.data["weekly-summary-202642"]["state"], "unknown")
        self.gmail.fail = None
        self.assertEqual(self.go()["status"], "already_recorded")
        self.assertEqual(len(self.gmail.posts()), 1)

    def test_a_failure_before_the_send_releases_the_claim_for_a_later_run(self):
        self.bc.pages.clear()
        r = self.go()
        self.assertEqual((r["status"], r["reason"]), ("held", "basecamp_read_failed"))
        self.assertEqual(self.ops.backend.data["weekly-summary-202642"]["state"], "claimed")
        self.bc = Basecamp()
        self.answer(self.good())
        self.assertEqual(self.go()["status"], "weekly_summary_sent")

    def test_recipients_are_the_digest_ones_and_any_client_address_refuses_the_mail(self):
        r = self.go(config(recipients=["ahmet@gamechangers.com.tr", "ayse@watmobilite.com.tr"]))
        self.assertEqual((r["status"], r["reason"]), ("held", "digest_recipient_not_internal_agency"))
        self.assertEqual((self.gmail.calls, self.client.calls), ([], []))

    def test_stop_refuses_the_claim(self):
        self.ops.stop("ahmet@gamechangers.com.tr")
        self.assertEqual(self.go()["status"], "held")
        self.assertEqual(self.go(preview=True)["status"], "held")
        self.assertEqual((self.gmail.calls, self.client.calls), ([], []))
        self.assertNotIn("weekly-summary-202642", self.ops.backend.data)


class Preview(Base):
    def test_preview_builds_the_text_without_sending_or_recording_and_the_friday_run_reuses_the_answer(self):
        self.now = datetime.fromisoformat("2026-10-16T17:00:00+03:00")  # before the window: preview still builds
        self.answer(self.good())
        r = self.go(preview=True, writer=None)
        self.assertEqual((r["status"], r["business_writes"]), ("weekly_summary_preview", 0))
        self.assertEqual(r["subject"], "[ÖNİZLEME] Hamle AI: 16 Ekim Cuma Haftalık Marka Özeti")
        self.assertIn("Ekim reels seti teslim edildi.", r["body"])
        self.assertEqual(r["recipients"], 7)
        self.now = datetime.fromisoformat("2026-10-14T11:00:00+03:00")  # any weekday: still a preview, never a send
        self.answer(self.good())
        self.assertEqual(self.go(preview=True, writer=None)["status"], "weekly_summary_preview")
        self.client.calls.pop()
        self.assertEqual(self.gmail.calls, [])
        self.assertNotIn("weekly-summary-202642", self.ops.backend.data)
        self.now = FRI_1735
        sent = self.go()
        self.assertEqual(sent["status"], "weekly_summary_sent")
        self.assertEqual(len(self.client.calls), 1)  # byte-identical request: the ModelGate answer is reused
        self.assertEqual(self.gmail.text().replace("\r\n", "\n").rstrip("\n"), r["body"])


class Model(Base):
    def test_brands_without_material_make_no_model_call(self):
        self.bc = Basecamp(todos=[])
        for k in ("brand-memory-wat_mobilite-202610", "client-jobs"):
            del self.ops.backend.data[k]
        r = self.go()
        self.assertEqual(r["status"], "weekly_summary_sent")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(r["stats"]["model_calls"], 0)
        self.assertEqual(self.gmail.text().count(ws.NOTHING_THIS_WEEK), 8)
        self.assertEqual({v["source"] for v in r["stats"]["brands"].values()}, {"empty"})

    def test_urls_not_in_the_input_are_dropped_and_lines_without_known_refs_are_dropped(self):
        evil = "https://evil.example.com/phish"
        self.answer({"brands": [{"key": "wat_mobilite", "lines": [
            line("risk", "Geciken iş var.", ["g1"], evil),                                   # -> g1's own url
            line("oldu", "Banner istendi.", ["e1"], evil),                                   # e1 has no url -> None
            line("oldu", f"Uydurma satır {evil}", ["zz9"]),                                  # unknown ref -> dropped
            line("teslim", f"Reels teslim edildi ({evil}).", ["e2"], f"{APP}/buckets/{RABAM}/todos/1"),
        ]}, {"key": "rabam", "lines": [line("oldu", "Başka marka", ["e1"])]}]})
        r = self.go()
        self.assertEqual((r["stats"]["dropped_urls"], r["stats"]["dropped_lines"]), (3, 1))
        text = self.gmail.text().replace("\r\n", "\n")
        self.assertNotIn("evil.example.com", text)
        self.assertNotIn("Uydurma", text)
        self.assertIn(f"⚠️ Geciken iş var. · {APP}/buckets/{WAT}/todos/1", text)
        self.assertIn("📌 Banner istendi.\n", text)
        self.assertIn(f"✅ Reels teslim edildi. · {APP}/buckets/{WAT}/todos/9", text)
        self.assertIn("RABAM\n- " + ws.NOTHING_THIS_WEEK, text)  # a key that was not asked is ignored

    def test_route_is_haiku_low_and_calls_go_through_the_model_gate_and_budget(self):
        r = model_routing.load().route("weekly_summary")
        self.assertEqual((r.provider, r.model, r.effort, r.web_search), ("anthropic", "claude-haiku-5-5", "low", None))
        self.assertIn("weekly_summary", model_routing.ROLES)
        self.answer(self.good())
        self.go()
        self.assertEqual(self.client.calls[0]["model"], "claude-haiku-5-5")
        cached = [v for k, v in self.ops.backend.data.items() if k.startswith("chain-model-")]
        self.assertEqual([v["role"] for v in cached], ["weekly_summary"])
        holds = [x for x in self.ops.backend.data["budget"]["reservations"]
                 if x["reservation_key"].startswith("chain-model-")]
        self.assertEqual(len(holds), 1)
        self.assertEqual(holds[0]["state"], "settled")

    def test_no_budget_means_no_call_and_lines_straight_from_the_items(self):
        catalog = copy.deepcopy(self.catalog)
        catalog["budget"]["internal_live_cap_microdollars"] = 1
        model = ws.summary_source(self.ops, lambda: KEY, catalog, routing=model_routing.load(), client=self.client)
        r = self.go(model=model)
        self.assertEqual((r["status"], r["stats"]["model"], r["stats"]["model_calls"]),
                         ("weekly_summary_sent", "model_budget_unavailable", 0))
        self.assertEqual(self.client.calls, [])
        text = self.gmail.text().replace("\r\n", "\n")
        self.assertIn(f"✅ Ekim reels seti teslim edildi · {APP}/buckets/{WAT}/todos/9", text)
        self.assertIn(f"⚠️ “Ekim post takvimi” 3 gün gecikmiş @Berkay (+1 iş daha) · {APP}/buckets/{WAT}/todos/1", text)
        self.assertIn(f"📅 “Kasım planı” teslim 21 Ekim @Esma · {APP}/buckets/{WAT}/todos/3", text)
        self.assertEqual(r["stats"]["brands"]["wat_mobilite"]["source"], "fallback")

    def test_one_call_for_all_brands_when_it_fits_else_one_per_brand_and_never_over_the_input_limit(self):
        brands = {}
        for n, key in enumerate(ws.bm.brands_of(self.catalog)):
            ev = [dict(event("2026-10-14", "bilgi", f"Olay {k} " + "uzun " * 30, None), detail="x" * 200,
                       ref=f"e{k + 1}") for k in range(12)]
            brands[key] = {"key": key, "name": key, "events": ev, "overdue": [], "due_today": [], "next_week": [],
                           "jobs": [], "omitted": 0}
        calls = []

        def model(request):
            calls.append(request)
            self.assertLessEqual(len(base_model._encoded(request)), base_model.MAX_INPUT_BYTES)
            return {"brands": [{"key": b["key"], "lines": [line("oldu", "Bir olay.", ["e1"])]} for b in request["brands"]]}
        sections, stats = ws.summarize_all(date(2026, 10, 16), list(brands.values()), model)
        self.assertEqual(len(calls), 8)  # too big together: one call per brand, never more
        self.assertEqual({s["source"] for s in sections.values()}, {"model"})
        small = {k: dict(v, events=v["events"][:1]) for k, v in brands.items()}
        calls.clear()
        ws.summarize_all(date(2026, 10, 16), list(small.values()), model)
        self.assertEqual(len(calls), 1)


class Wiring(unittest.TestCase):
    def test_worker_flag_statuses_and_package(self):
        import package_chain_source as pkg
        import worker
        for argv in (["--mode", "daily_digest", "--summary-preview"], ["--mode", "brand_memory", "--summary-preview"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(worker.main(argv), 2)
            self.assertEqual(json.loads(buf.getvalue())["reason"], "summary_preview_requires_weekly_summary", argv)
        for status in ("weekly_summary_sent", "weekly_summary_preview", "no_new_work", "already_recorded"):
            self.assertIn(status, worker.OK_STATUSES)
        for name in ("v11_weekly_summary.py", "weekly_summary_prompt.txt"):
            self.assertIn(name, pkg.CHAIN_FILES)

    def test_the_summary_reads_no_teams_source(self):
        with open(ws.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("teams_context", src)
        self.assertNotIn("v11_teams", src)


if __name__ == "__main__":
    unittest.main()
