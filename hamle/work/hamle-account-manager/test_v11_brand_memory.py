"""Marka hafızası (Ahmet 2026-10-15): fakes only, no sockets, Basecamp, Gmail, Teams or models."""
import unittest
import copy
import json
from datetime import date, datetime, timedelta

import model_routing
import v11_brand_memory as bm
import v11_teams as teams
from test_anthropic_routing import KEY, message
from test_v11_client_watch import Base
from test_v11_teams import WAT_CHANNEL, AGENCY_CHAT, CoreReader, TeamsBase, out

ACC, WAT, RABAM = 5898865, 40694338, 48639990
APP = f"https://3.basecamp.com/{ACC}/buckets/{WAT}"


def todo(n, title, updated, created=None, **kw):
    row = {"id": n, "type": "Todo", "title": title, "created_at": created or updated, "updated_at": updated,
           "completed": False, "due_on": "2026-10-15", "assignees": [{"id": 1, "name": "Berkay Demir"}],
           "creator": {"id": 2, "name": "Özge Yağmur"}, "parent": {"title": "Sosyal Medya"},
           "app_url": f"{APP}/todos/{n}", "description": "<div>Ekim postları için <b>3 görsel</b></div>"}
    row.update(kw)
    return row


def comment(n, text, at, by="Özge Yağmur"):
    return {"id": n, "type": "Comment", "content": f"<div>{text}</div>", "created_at": at, "updated_at": at,
            "creator": {"name": by}, "parent": {"title": "Ekim post takvimi"}, "app_url": f"{APP}/todos/1#__recording_{n}"}


def mail(n, sender, at, subject="Ekim takvimi", text="Merhaba,\nTakvimi onaylıyoruz.\n\n> eski mesaj"):
    ms = int(datetime.fromisoformat(at).timestamp() * 1000)
    return {"message_id": f"m{n}", "thread_id": f"t{n}", "sender": sender, "subject": subject, "text": text,
            "received_at_ms": ms, "attachments": [{"filename": "takvim.pdf"}]}


def mem_out(events=(), **updates):
    u = {"contacts": [], "preferences": [], "donts": [], "approval_flow": None, "open_promises": [], "decisions": []}
    u.update(updates)
    return {"events": list(events), "profile_updates": u}


def event(d="2026-10-12", source="basecamp", kind="onay", title="Ekim post takvimi onaylandı", url=None, people=()):
    return {"date": d, "source": source, "kind": kind, "title": title, "detail": "Ayşe takvimi onayladı.",
            "people": list(people), "url": url}


class FakeBasecamp:
    account = ACC

    def __init__(self, rows=None, covered=None):
        self.rows, self.calls, self.covered, self.fail = rows or {}, [], covered, None

    def recent(self, pid, kind, since):
        self.calls.append((pid, kind, since))
        if self.fail:
            raise self.fail
        rows = [r for r in self.rows.get((pid, kind), []) if bm._ts(r["updated_at"]) >= since]
        return copy.deepcopy(rows), self.covered or since


class Memory(Base):
    def setUp(self):
        super().setUp()  # now: Tue 2026-10-13 10:00 Istanbul -> live days 10, 11, 12 Oct
        self.bc = FakeBasecamp({(WAT, "Todo"): [todo(1, "Ekim post takvimi", "2026-10-12T09:00:00Z",
                                                     "2026-10-01T09:00:00Z"),
                                                todo(2, "Eski iş", "2026-10-05T09:00:00Z")],
                                (WAT, "Comment"): [comment(11, "Takvim müşteriye gitti.", "2026-10-12T10:00:00Z")]})
        self.mails = [mail(1, "ayse@watmobilite.com.tr", "2026-10-12T12:00:00+03:00"),
                      mail(2, "biri@baskasirket.com", "2026-10-12T12:00:00+03:00"),
                      mail(3, "ahmet.test@gmail.com", "2026-10-12T12:00:00+03:00")]
        self.mail_days = []
        self.model = bm.memory_source(self.ops, lambda: KEY, self.catalog, routing=model_routing.load(),
                                      client=self.client)

    def mail_source(self, days):
        self.mail_days.append(days)
        return copy.deepcopy(self.mails)

    def go(self, **kw):
        kw.setdefault("model", self.model)
        return bm.run_brand_memory(self.ops, catalog=self.catalog, basecamp=self.bc, mail=self.mail_source,
                                   book=self.book, clock=lambda: self.now, **kw)

    def answer(self, *outputs):
        self.client.outputs = [message(o, model_id="claude-haiku-5-5") for o in outputs]

    def month(self, brand="wat_mobilite", m="202610"):
        return self.ops.backend.data.get(f"brand-memory-{brand}-{m}")

    def request(self, n=0):
        return json.loads(self.client.calls[n]["messages"][0]["content"])


class Collect(Memory):
    def test_live_run_reads_the_window_matches_mail_by_domain_and_calls_once_per_brand_day_with_material(self):
        self.answer(mem_out([event(url=f"{APP}/todos/1", people=["Ayşe", "Berkay", "Uydurma Kişi"]),
                             event("2026-10-09", title="Pencere dışı"),
                             event(source="teams", title="Teams kaydı yok"),
                             event(source="mail", kind="onay", title="Müşteri takvimi onayladı",
                                   url="https://uydurma.example.com/x")],
                            contacts=[{"name": "Ayşe Yılmaz", "role": "Pazarlama Müdürü",
                                       "email": "ayse@watmobilite.com.tr"},
                                      {"name": "Hayali Kişi", "role": None, "email": "x@y.com"}],
                            preferences=["Görseller aydınlık olsun"]))
        r = self.go()
        self.assertEqual((r["status"], r["model_calls"], r["events"], r["dropped"], r["business_writes"]),
                         ("brand_memory_completed", 1, 2, 2, 0))
        self.assertEqual(self.mail_days, [4])  # newer_than 4d covers 10 Oct from its first minute
        req = self.request()
        self.assertEqual((req["brand"]["key"], req["window"], req["mail"]),
                         ("wat_mobilite", {"from": "2026-10-12", "to": "2026-10-12"}, "okundu"))
        items = req["items"]
        self.assertEqual([i.get("title") or i.get("on") or i.get("subject") for i in items],
                         ["Ekim post takvimi", "Ekim takvimi", "Ekim post takvimi"])  # by time; not 'Eski iş' (5 Oct)
        m = [i for i in items if i["source"] == "mail"]
        self.assertEqual((m[0]["from"], m[0]["from_name"], m[0]["text"], m[0]["attachments"]),
                         ("ayse@watmobilite.com.tr", "Ayşe Yılmaz", "Merhaba,\nTakvimi onaylıyoruz.", ["takvim.pdf"]))
        self.assertEqual(items[0]["text"], "Ekim postları için 3 görsel")
        month = self.month()
        self.assertEqual([(e["source"], e["url"], e["people"]) for e in month["events"]],
                         [("basecamp", f"{APP}/todos/1", ["Ayşe", "Berkay"]), ("mail", None, [])])
        self.assertEqual(month["days_done"], ["2026-10-10", "2026-10-11", "2026-10-12"])
        for brand in bm.brands_of(self.catalog):  # every brand's empty days are done too, without a model call
            self.assertEqual(self.month(brand)["days_done"], ["2026-10-10", "2026-10-11", "2026-10-12"], brand)
        self.assertNotIn("gamechangers", bm.brands_of(self.catalog))
        self.assertNotIn("new_biz", bm.brands_of(self.catalog))
        profile = self.ops.backend.data["brand-profile-wat_mobilite"]
        self.assertEqual([c["name"] for c in profile["contacts"]], ["Ayşe Yılmaz"])
        self.assertEqual(profile["preferences"], ["Görseller aydınlık olsun"])
        run = self.ops.backend.data["brand-memory-run-20261013"]
        self.assertEqual((run["brands"]["wat_mobilite"]["model"], run["status"]), ("ok", "brand_memory_completed"))

    def test_nothing_new_means_no_model_call_and_a_second_run_does_nothing(self):
        self.bc.rows, self.mails = {}, []
        r = self.go()
        self.assertEqual((r["status"], r["model_calls"], r["empty"]), ("brand_memory_completed", 0, 3 * 8))
        self.assertEqual(self.client.calls, [])
        self.bc.rows = {(WAT, "Todo"): [todo(1, "Geç gelen", "2026-10-12T09:00:00Z")]}
        again = self.go()
        self.assertEqual((again["status"], again["reason"], again["model_calls"]),
                         ("no_new_work", "brand_memory_days_done", 0))
        self.assertEqual(self.client.calls, [])

    def test_jobs_and_teams_messages_of_the_brand_are_raw_material(self):
        self.bc.rows, self.mails = {}, []
        at = datetime.fromisoformat("2026-10-11T15:00:00+03:00").timestamp()

        def seed(tx, _):
            tx.put("client-job-1", {"kind": "client_job", "brand_key": "wat_mobilite", "state": "open", "test": False,
                                    "title_core": "Standartlar Günü postu", "created_at": at, "updated_at": at,
                                    "assignee_name": "Özge Yağmur", "client_name": "Ayşe Yılmaz",
                                    "app_url": f"{APP}/todos/9"})
            tx.put("client-job-2", {"kind": "client_job", "brand_key": "rabam", "state": "open", "test": False,
                                    "title_core": "Rabam işi", "created_at": at, "updated_at": at})
            tx.put("client-jobs", {"kind": "client_jobs", "keys": ["client-job-1", "client-job-2"]})
            tx.put(teams.brand_index_key("wat_mobilite"), {"kind": "teams_index", "keys": ["teams-conv-w"]})
            tx.put("teams-conv-w", {"name": "WAT Sosyal", "messages": [
                {"id": "a1", "received_at": at + 60, "author": "Özge Yağmur", "text": "Ayşe Hanım tarihi cumaya aldı.",
                 "files": []}]})
            tx.put(teams.AGENCY_INDEX, {"kind": "teams_index", "keys": ["teams-conv-h"]})
            tx.put("teams-conv-h", {"name": "Heads", "messages": [
                {"id": "h1", "received_at": at, "author": "Ahmet", "text": "WAT MOBİLİTE bütçesi gizli konu", "files": []}]})
        self.ops._run(seed, allow_expired=True)
        self.answer(mem_out([event("2026-10-11", "hamle", "talep", "Rabam işi açıldı")]),  # brands in key order
                    mem_out([event("2026-10-11", "teams", "karar", "Teslim cuma", people=["Özge"])]))
        r = self.go()
        self.assertEqual(r["model_calls"], 2)
        reqs = {self.request(n)["brand"]["key"]: self.request(n) for n in range(2)}
        wat = reqs["wat_mobilite"]["items"]
        self.assertEqual([(i["source"], i.get("title") or i.get("text")) for i in wat],
                         [("hamle", "Standartlar Günü postu"), ("teams", "Ayşe Hanım tarihi cumaya aldı.")])
        self.assertNotIn("bütçesi", json.dumps(reqs, ensure_ascii=False))  # agency chats stay out (Heads rule)
        self.assertEqual(self.month()["events"][0]["people"], ["Özge"])


class Backfill(Memory):
    def test_week_windows_never_cross_a_month(self):
        days = [date(2026, 9, 20) + timedelta(days=n) for n in range(23)]  # 20 Sep .. 12 Oct
        self.assertEqual(bm.windows(days, 7), [(date(2026, 9, 20), date(2026, 9, 26)), (date(2026, 9, 27), date(2026, 9, 30)),
                                               (date(2026, 10, 1), date(2026, 10, 7)), (date(2026, 10, 8), date(2026, 10, 12))])
        self.assertEqual(bm.windows([date(2026, 10, 1), date(2026, 10, 3)], 7),
                         [(date(2026, 10, 1), date(2026, 10, 1)), (date(2026, 10, 3), date(2026, 10, 3))])

    def test_backfill_is_bounded_resumable_and_reads_mail_only_where_it_reaches(self):
        self.bc.rows = {(WAT, "Todo"): [todo(1, "Eylül kampanyası", "2026-09-22T09:00:00Z"),
                                        todo(2, "Ekim takvimi", "2026-10-09T09:00:00Z")]}
        self.mails = []
        self.answer(mem_out([event("2026-09-22", title="Eylül kampanyası açıldı", kind="talep")]),
                    mem_out([event("2026-10-09", title="Ekim takvimi")]))
        first = self.go(backfill_from="2026-09-20", max_calls=1)
        self.assertEqual((first["status"], first["reason"], first["model_calls"]),
                         ("brand_memory_partial", "run_budget_reached", 1))
        self.assertEqual(self.request(0)["window"], {"from": "2026-09-20", "to": "2026-09-26"})
        self.assertEqual(self.request(0)["mail"], "bu aralık için okunmadı")
        self.assertEqual(self.mail_days, [7])
        second = self.go(backfill_from="2026-09-20", max_calls=1)
        self.assertEqual((second["status"], second["model_calls"]), ("brand_memory_completed", 1))
        self.assertEqual((self.request(1)["window"], self.request(1)["mail"]),
                         ({"from": "2026-10-08", "to": "2026-10-12"}, "okundu"))
        self.assertEqual(len(self.month(m="202609")["days_done"]), 11)
        self.assertEqual(self.go(backfill_from="2026-09-20")["status"], "no_new_work")
        for bad in ("2026-07-01", "2026-10-13", "dün"):
            self.assertEqual(self.go(backfill_from=bad)["status"], "held", bad)

    def test_a_basecamp_gap_or_failure_never_marks_days_done(self):
        self.mails = []
        self.bc.covered = bm.day_start(date(2026, 10, 12))  # the pages did not reach 10 and 11 Oct
        self.answer(mem_out([event()]))
        r = self.go()
        self.assertEqual(r["brands"]["wat_mobilite"]["status"], "basecamp_truncated")
        self.assertEqual(self.month()["days_done"], ["2026-10-12"])
        self.bc.covered, self.bc.fail = None, RuntimeError("down")
        r = self.go()
        self.assertTrue(r["brands"]["wat_mobilite"]["status"].startswith("read_failed"))
        self.assertEqual(self.month()["days_done"], ["2026-10-12"])

    def test_a_failed_model_call_leaves_the_days_open(self):
        def broken(request):
            raise bm.BrandMemoryError("model_failed_no_retry:x")
        r = self.go(model=broken)
        self.assertEqual(r["brands"]["wat_mobilite"]["status"], "model_failed:model_failed_no_retry:x")
        self.assertNotIn("2026-10-12", self.month()["days_done"])
        self.assertEqual(self.month()["days_done"], ["2026-10-10", "2026-10-11"])


    def test_a_window_whose_answer_overflows_is_halved_and_retried(self):
        self.bc.rows = {(WAT, "Todo"): [todo(1, "Eylül kampanyası", "2026-09-22T09:00:00Z")]}
        self.mails, seen = [], []
        def model(request):
            w = request["window"]
            seen.append((w["from"], w["to"]))
            if w["from"] != w["to"] and (w["from"], w["to"]) != ("2026-09-20", "2026-09-21"):
                raise bm.BrandMemoryError("model_failed_no_retry:response_max_tokens")
            return mem_out([event(w["from"], title="Eylül kampanyası açıldı", kind="talep")]
                           if w["from"] <= "2026-09-22" <= w["to"] else [])
        r = self.go(backfill_from="2026-09-20", model=model, max_calls=9)
        self.assertEqual(seen[:4], [("2026-09-20", "2026-09-26"), ("2026-09-20", "2026-09-23"),
                                    ("2026-09-22", "2026-09-23"), ("2026-09-22", "2026-09-22")])  # 20-21 Sep: no call
        self.assertNotIn("status", r["brands"]["wat_mobilite"])
        self.assertGreaterEqual(r["split"], 3)
        self.assertEqual(self.month(m="202609")["days_done"][:7], [f"2026-09-{d}" for d in range(20, 27)])
        self.assertEqual([e["title"] for e in self.month(m="202609")["events"]], ["Eylül kampanyası açıldı"])

    def test_a_single_day_that_overflows_stays_open(self):
        def model(request):
            raise bm.BrandMemoryError("model_failed_no_retry:response_max_tokens")
        r = self.go(model=model)
        self.assertEqual(r["brands"]["wat_mobilite"]["status"], "model_failed:model_failed_no_retry:response_max_tokens")
        self.assertNotIn("split", r)


class Fences(Memory):
    def test_stop_before_the_run_reads_and_writes_nothing(self):
        self.ops.stop("ahmet@gamechangers.com.tr")
        r = self.go()
        self.assertEqual((r["status"], r["reason"]), ("held", "control_stopped_or_stale"))
        self.assertEqual((self.bc.calls, self.client.calls, self.mail_days), ([], [], []))
        self.assertFalse([k for k in self.ops.backend.data if k.startswith("brand-")])

    def test_stop_during_the_run_fences_the_write(self):
        def stopping(request):
            self.ops.stop("ahmet@gamechangers.com.tr")
            return mem_out([event()])
        r = self.go(model=stopping)
        self.assertEqual((r["status"], r["reason"]), ("held", "control_stopped_or_stale"))
        self.assertNotIn("2026-10-12", (self.month() or {}).get("days_done", []))
        self.assertNotIn("brand-profile-wat_mobilite", self.ops.backend.data)

    def test_a_running_lease_is_respected(self):
        self.assertTrue(bm._Store(self.ops, date(2026, 10, 13), self.ops.control()["generation"])
                        .claim("other", 600, "live"))
        self.assertEqual(self.go()["status"], "slot_already_claimed")
        self.assertEqual(self.client.calls, [])


class Records(Memory):
    def test_month_record_stays_under_the_cap_keeping_the_newest(self):
        events = [{"date": f"2026-10-{1 + n % 28:02d}", "source": "basecamp", "kind": "bilgi", "title": f"Olay {n}",
                   "detail": "x" * 400, "people": ["Özge"], "url": None} for n in range(200)]
        m = bm.add_events(bm.empty_month("wat_mobilite", date(2026, 10, 1)), events, [date(2026, 10, 1)])
        self.assertLessEqual(bm._size(m), bm.MAX_MONTH_BYTES)
        self.assertGreater(m["truncated"], 0)
        self.assertEqual(len(m["events"]) + m["truncated"], 200)
        self.assertEqual(m["events"][-1]["date"], "2026-10-28")
        again = bm.add_events(m, events[-3:], [date(2026, 10, 2)])  # duplicates are not added twice
        self.assertEqual((len(again["events"]), again["days_done"]), (len(m["events"]), ["2026-10-01", "2026-10-02"]))

    def test_profile_merge_dedupes_turkish_safe_and_caps(self):
        p = bm.merge_profile(None, {"contacts": [{"name": "Ayşe Yılmaz", "role": None, "email": None}],
                                    "preferences": ["İstanbul çekimleri"], "donts": ["Kırmızı yok"],
                                    "approval_flow": "Ayşe onaylar.", "open_promises": [{"what": "Cuma teslim", "due": None,
                                                                                         "by": "Özge"}],
                                    "decisions": [{"date": "2026-10-02", "what": "Reels haftada 2"}]},
                             "wat_mobilite", "WAT Mobilite", 1.0)
        p = bm.merge_profile(p, {"contacts": [{"name": "AYŞE YILMAZ", "role": "Pazarlama", "email": "ayse@watmobilite.com.tr"}],
                                 "preferences": ["istanbul ÇEKİMLERİ"] + [f"Tercih {n}" for n in range(25)],
                                 "donts": ["KIRMIZI YOK"], "approval_flow": None,
                                 "open_promises": [{"what": "cuma teslim", "due": "2026-10-16", "by": "Özge"}],
                                 "decisions": [{"date": "2026-10-02", "what": "reels haftada 2"},
                                               {"date": "2026-10-01", "what": "Logo büyüdü"}]},
                             "wat_mobilite", "WAT Mobilite", 2.0)
        self.assertEqual(p["contacts"], [{"name": "AYŞE YILMAZ", "role": "Pazarlama", "email": "ayse@watmobilite.com.tr"}])
        self.assertEqual((len(p["preferences"]), p["preferences"][-1]), (20, "Tercih 24"))
        self.assertEqual((p["donts"], p["approval_flow"], len(p["open_promises"])), (["Kırmızı yok"], "Ayşe onaylar.", 1))
        self.assertEqual([d["what"] for d in p["decisions"]], ["Logo büyüdü", "Reels haftada 2"])
        self.assertEqual(p["updated_at"], 2.0)

    def test_context_gives_the_profile_and_the_last_events_of_30_days(self):
        def seed(tx, _):
            tx.put("brand-profile-wat_mobilite", {"kind": "brand_profile", "contacts": [{"name": "Ayşe"}],
                                                  "preferences": ["Aydınlık"], "donts": [], "approval_flow": None,
                                                  "open_promises": [], "decisions": [], "updated_at": self.now.timestamp()})
            tx.put("brand-memory-wat_mobilite-202609", {"events": [
                {"date": "2026-09-10", "title": "Çok eski"}, {"date": "2026-09-20", "title": "Eylül", "detail": "y" * 900}]})
            tx.put("brand-memory-wat_mobilite-202610", {"events": [
                {"date": f"2026-10-{d:02d}", "title": f"Ekim {d}"} for d in range(1, 13)]})
        self.ops._run(seed, allow_expired=True)
        ctx = bm.brand_memory_context(self.ops, "wat_mobilite", date(2026, 10, 13), limit=10)
        self.assertEqual((ctx["profile"]["preferences"], ctx["profile"]["updated"]), (["Aydınlık"], "2026-10-13 10:00"))
        self.assertEqual([e["title"] for e in ctx["events"]], ["Ekim %d" % d for d in range(3, 13)])
        wide = bm.brand_memory_context(self.ops, "wat_mobilite", date(2026, 10, 13), limit=50, detail=100)
        self.assertEqual([e["title"] for e in wide["events"]][:2], ["Eylül", "Ekim 1"])  # 10 Sep is older than 30 days
        self.assertEqual(len(wide["events"][0]["detail"]), 100)
        self.assertEqual(bm.brand_memory_context(self.ops, "rabam", date(2026, 10, 13)), {"profile": None, "events": []})

    def test_input_is_bounded_and_the_route_is_haiku_and_packaged(self):
        import package_chain_source as pkg
        items = [{"source": "basecamp", "type": "comment", "at": f"2026-10-12 {n % 24:02d}:00", "_t": [0],
                  "text": "uzun " * 400, "url": None} for n in range(200)]
        inp = bm.model_input("wat_mobilite", "WAT Mobilite", date(2026, 10, 12), date(2026, 10, 12), None, items)
        self.assertLessEqual(bm._size(inp), bm.MAX_INPUT_BYTES)
        self.assertGreater(inp["omitted_items"], 0)
        self.assertNotIn("_t", inp["items"][0])
        r = model_routing.load().route("brand_memory")
        self.assertEqual((r.provider, r.model, r.effort, r.web_search), ("anthropic", "claude-haiku-5-5", "low", None))
        for name in ("v11_brand_memory.py", "brand_memory_prompt.txt"):
            self.assertIn(name, pkg.CHAIN_FILES)

    def test_worker_memory_from_only_with_the_brand_memory_mode(self):
        import io
        from contextlib import redirect_stdout
        import worker
        for argv in (["--mode", "daily_digest", "--memory-from", "2026-09-01"],
                     ["--mode", "brand_memory", "--memory-from", "1 Eylül"]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(worker.main(argv), 2)
            self.assertEqual(json.loads(buf.getvalue())["reason"], "memory_from_requires_brand_memory", argv)
        for status in ("brand_memory_completed", "brand_memory_partial"):
            self.assertIn(status, worker.OK_STATUSES)


class Reader(Base):
    def test_recordings_reader_is_get_only_allowlisted_and_stops_at_the_window(self):
        base = f"https://3.basecampapi.com/{ACC}/projects/recordings.json?type=Todo&bucket={WAT}&status=active" \
               "&sort=updated_at&direction=desc"
        pages = {base: ([todo(1, "a", "2026-10-12T09:00:00Z"), {"bad": 1}], {"Link": f'<{base}&page=2>; rel="next"'}),
                 base + "&page=2": ([todo(2, "b", "2026-10-11T09:00:00Z"), todo(3, "c", "2026-10-01T09:00:00Z"),
                                     todo(4, "d", "2026-09-01T09:00:00Z")], {"Link": f'<{base}&page=3>; rel="next"'})}
        urls = []

        def get(url):
            urls.append(url)
            return copy.deepcopy(pages[url])
        reader = bm.BasecampMemoryReader(ACC, get=get)
        rows, covered = reader.recent(WAT, "Todo", bm.day_start(date(2026, 10, 5)))
        self.assertEqual(([r["id"] for r in rows], covered, len(urls)), ([1, 2], bm.day_start(date(2026, 10, 5)), 2))
        short = bm.BasecampMemoryReader(ACC, get=get, max_pages=1)
        rows, covered = short.recent(WAT, "Todo", bm.day_start(date(2026, 10, 5)))
        self.assertEqual(covered, bm._ts("2026-10-12T09:00:00Z"))  # not reached: older windows stay open
        for bad in (base.replace("Todo", "Upload"), base + "&bucket=1", base.replace("desc", "asc"),
                    base.replace("projects/recordings", "buckets/1/todos"), base.replace("https", "http"),
                    base + "&page=2&x=1"):
            with self.assertRaises(Exception, msg=bad):
                reader._check(bad)
        evil = {base: ([todo(1, "a", "2026-10-12T09:00:00Z")],
                       {"Link": f'<{base.replace(str(WAT), str(RABAM))}&page=2>; rel="next"'})}
        with self.assertRaises(bm.BrandMemoryError):
            bm.BasecampMemoryReader(ACC, get=lambda u: copy.deepcopy(evil[u])).recent(WAT, "Todo", 0)


class TeamsSeesMemory(TeamsBase):
    def test_a_brand_conversation_gets_the_brand_memory_and_an_agency_chat_does_not(self):
        def seed(tx, _):
            tx.put("brand-profile-wat_mobilite", {"kind": "brand_profile", "contacts": [{"name": "Ayşe Yılmaz"}],
                                                  "preferences": ["Görseller aydınlık"], "donts": ["Kırmızı yok"],
                                                  "approval_flow": "Ayşe onaylar.", "open_promises": [], "decisions": [],
                                                  "updated_at": self.now.timestamp()})
            tx.put("brand-memory-wat_mobilite-202610", {"events": [
                {"date": "2026-10-12", "source": "mail", "kind": "onay", "title": "Ekim takvimi onaylandı",
                 "detail": "Ayşe onayladı.", "people": ["Ayşe Yılmaz"], "url": None}]})
        self.ops._run(seed, allow_expired=True)
        core = teams.CoreContext(self.ops, reader_factory=CoreReader([]), clock=lambda: self.now)
        desk = self.desk(core=core)
        self.client.outputs = [message(out()), message(out())]
        self.post(desk, self.activity("WAT'ta müşteri neyi onayladı?", mention=True))
        memory = json.loads(self.client.calls[0]["messages"][0]["content"])["core"]["brand_memory"]
        self.assertEqual(([e["title"] for e in memory["events"]], memory["profile"]["donts"]),
                         (["Ekim takvimi onaylandı"], ["Kırmızı yok"]))
        self.post(desk, self.activity("Genel soru?", mention=True, conv=AGENCY_CHAT, kind="groupChat"))
        self.assertNotIn("brand_memory", json.loads(self.client.calls[1]["messages"][0]["content"])["core"])


class ReaderLimitsTest(unittest.TestCase):
    def test_a_backfill_reads_further_back_than_the_daily_run(self):
        self.assertEqual(bm.reader_limits(None), {})
        self.assertEqual(bm.reader_limits("2026-07-18"), {"max_pages": 40, "max_reads": 1000})
        r = bm.BasecampMemoryReader(1, get=None, **bm.reader_limits("2026-07-18"))
        self.assertEqual((r.max_pages, r.max_reads), (40, 1000))


class BasecampGetTest(unittest.TestCase):
    class Resp:
        def __init__(self, status, body=b"[]", headers=None):
            self.status_code, self.body, self.headers = status, body, headers or {}

        def iter_content(self, chunk_size):
            yield self.body

        def close(self):
            pass

    class Session:
        def __init__(self, *responses):
            self.responses, self.calls = list(responses), 0

        def request(self, *a, **k):
            self.calls += 1
            return self.responses.pop(0)

    def test_rate_limit_waits_and_retries(self):
        import v11_brand_memory as bm
        waits = []
        s = self.Session(self.Resp(429, headers={"Retry-After": "7"}), self.Resp(200, b"[1]"))
        data, _ = bm.basecamp_get(s, "https://3.basecampapi.com/x", "Bearer t", "UA", bm.BrandMemoryError,
                                  sleep=waits.append)
        self.assertEqual((data, waits, s.calls), ([1], [7], 2))

    def test_other_status_names_the_code(self):
        import v11_brand_memory as bm
        with self.assertRaises(bm.BrandMemoryError) as ctx:
            bm.basecamp_get(self.Session(self.Resp(403)), "u", "Bearer t", "UA", bm.BrandMemoryError, sleep=lambda s: None)
        self.assertIn("basecamp_http_403", str(ctx.exception.args) + str(getattr(ctx.exception, "code", "")))

    def test_rate_limit_gives_up_after_four_tries(self):
        import v11_brand_memory as bm
        s = self.Session(*[self.Resp(429, headers={"Retry-After": "999"}) for _ in range(4)])
        waits = []
        with self.assertRaises(bm.BrandMemoryError):
            bm.basecamp_get(s, "u", "Bearer t", "UA", bm.BrandMemoryError, sleep=waits.append)
        self.assertEqual((s.calls, waits), (4, [30, 30, 30]))


class BasecampPageSizeTest(BasecampGetTest):
    def test_two_megabyte_page_is_read(self):
        import v11_brand_memory as bm
        body = b"[" + b'"' + b"x" * (2 * 1_048_576) + b'"]'
        data, _ = bm.basecamp_get(self.Session(self.Resp(200, body)), "u", "Bearer t", "UA", bm.BrandMemoryError)
        self.assertEqual(len(data[0]), 2 * 1_048_576)

    def test_too_large_names_the_endpoint(self):
        import v11_brand_memory as bm
        body = b"x" * (bm.MAX_PAGE_BYTES + 1)
        with self.assertRaises(bm.BrandMemoryError) as ctx:
            bm.basecamp_get(self.Session(self.Resp(200, body)),
                            "https://3.basecampapi.com/1/projects/recordings.json?type=Comment", "Bearer t", "UA",
                            bm.BrandMemoryError)
        self.assertIn("basecamp_page_too_large:recordings", str(ctx.exception.args) + str(getattr(ctx.exception, "code", "")))
