"""Aylık detaylı marka raporu (Ahmet 2026-10-15): fakes only, no sockets, Basecamp, Gmail or models."""
import copy
from contextlib import contextmanager, redirect_stdout
from datetime import date, datetime
import io
import itertools
import json
import socket
import unittest
from unittest.mock import patch

import model as base_model
import model_routing
import v11_daily_digest as dd
import v11_monthly_report as mr
from cloud_store import OperationsStore
from test_anthropic_routing import FakeClient, KEY, message
from test_cloud_store import MemoryBackend, budget_snapshot
from test_v11_daily_digest import Gmail
from v11_chain_live import load_catalog

ACC, WAT, RABAM = 5898865, 40694338, 48639990
API = f"https://3.basecampapi.com/{ACC}"
APP = f"https://3.basecamp.com/{ACC}"
MON_1045 = datetime.fromisoformat("2026-11-02T10:45:00+03:00")  # 1 Nov 2026 is a Sunday: Monday 2 Nov is the day
TOKEN = "synthetic-placeholder-token-never-live"
SUBJECT = "Hamle AI: WAT · Ekim 2026 Aylık Marka Raporu"
BRANDS = ("wat_mobilite", "rabam", "bwi", "han_spaces", "ya_da", "altinmarka", "bikaldi", "fenerium")


def todo(i, title, due, people=(), pid=WAT, created="2026-10-01T09:00:00+03:00"):
    return {"id": i, "title": title, "status": "active", "completed": False, "due_on": due,
            "assignees": [{"id": n, "name": name} for n, name in enumerate(people, 1)],
            "app_url": f"{APP}/buckets/{pid}/todos/{i}", "created_at": created}


def rec(i, title, created, due, completed_at=None, people=(), pid=WAT):
    row = todo(i, title, due, people, pid, created)
    row.update(type="Todo", updated_at=completed_at or created, completed=completed_at is not None)
    if completed_at:
        row["completion"] = {"created_at": completed_at}
    return row


def recordings_url(pid):
    return f"{API}/projects/recordings.json?type=Todo&bucket={pid}&status=active&sort=updated_at&direction=desc"


class Basecamp:
    """GET-only fake of both readers' transport; unknown URLs fail like a non-200 (overdue report too), unknown
    brand recordings are empty."""
    def __init__(self, todos=None, recordings=None):
        self.calls = []
        todos = todos if todos is not None else [
            todo(1, "Ekim post takvimi", "2026-10-28", ["Berkay Demir"]),     # open: 4 days overdue at month end
            todo(3, "Kasım planı", "2026-11-12", ["Esma Kaya"]),              # due next month
            todo(5, "Hamle Fikir: WAT × Cumhuriyet", "2026-10-10")]            # Hamle's own idea to-do
        recordings = recordings if recordings is not None else [
            rec(13, "Eski iş", "2026-09-20T09:00:00+03:00", "2026-10-30", "2026-11-01T08:00:00+03:00"),
            rec(12, "Story serisi", "2026-10-20T09:00:00+03:00", "2026-11-03"),
            rec(11, "Banner seti", "2026-10-05T09:00:00+03:00", "2026-10-12", "2026-10-15T12:00:00+03:00",
                ["Özge Yağmur"]),
            rec(10, "Reels seti", "2026-10-02T09:00:00+03:00", "2026-10-10", "2026-10-09T17:00:00+03:00"),
            dict(rec(1, "Ekim post takvimi", "2026-10-01T09:00:00+03:00", "2026-10-28", None, ["Berkay Demir"])),
            rec(14, "Hamle Fikir: WAT × Halloween", "2026-10-03T09:00:00+03:00", "2026-10-03")]
        self.pages = {
            f"{API}/projects.json": [{"id": WAT, "name": "WAT Mobilite", "status": "active",
                                      "dock": [{"name": "todoset", "id": 11, "enabled": True}]}],
            f"{API}/buckets/{WAT}/todosets/11/todolists.json": [{"id": 101}],
            f"{API}/buckets/{WAT}/todolists/101/todos.json": todos,
            recordings_url(WAT): recordings}

    def get(self, url):
        self.calls.append(url)
        if url not in self.pages:
            if "/projects/recordings.json" in url:
                return [], {}
            raise dd.DigestError("basecamp_read_failed")
        return copy.deepcopy(self.pages[url]), {}


def config(**kw):
    c = dd.load_config()
    c.update(kw)
    return c


def event(d, kind, title, url=None, detail="Ayrıntı."):
    return {"date": d, "source": "basecamp", "kind": kind, "title": title, "detail": detail, "people": [], "url": url}


def line(text, refs, url=None, **kw):
    return dict({"text": text, "refs": list(refs), "url": url}, **kw)


def sections(**kw):
    out = {k: [] for k, _, _ in mr.SECTIONS}
    out.update(kw)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.now = datetime.fromisoformat("2026-10-12T09:00:00+03:00")  # control opened in October
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: self.now)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.ops.resume_review_only("offline-test")
        self.now = datetime.fromisoformat("2026-11-01T00:30:00+03:00")
        self.ops.roll_budget_period()  # as worker.py does before every run (November budget period)
        self.now = MON_1045
        self.catalog = load_catalog()
        self.bc, self.gmail, self.client = Basecamp(), Gmail(), FakeClient()

        @contextmanager
        def token():
            yield TOKEN
        self.writer = dd.DigestMailWriter(token_context=token, session_factory=lambda: self.gmail)
        self.model = mr.report_source(self.ops, lambda: KEY, self.catalog, routing=model_routing.load(),
                                      client=self.client)
        data = self.ops.backend.data
        data["brand-memory-wat_mobilite-202610"] = {
            "kind": "brand_memory_month", "events": [
                event("2026-10-03", "talep", "Müşteri Kasım lansmanı için görsel istedi"),
                event("2026-10-09", "teslim", "Ekim reels seti teslim edildi", f"{APP}/buckets/{WAT}/todos/10"),
                event("2026-10-20", "onay", "Müşteri lansman tarihini onayladı"),
                event("2026-10-25", "risk", "Banner için üçüncü revize istendi", f"{APP}/buckets/{WAT}/todos/11")],
            "days_done": [f"2026-10-{d:02d}" for d in range(1, 29)]}
        data["brand-memory-wat_mobilite-202609"] = {"kind": "brand_memory_month", "events": [
            event("2026-09-10", "teslim", "Eylül işi")], "days_done": []}
        data["brand-profile-wat_mobilite"] = {
            "kind": "brand_profile", "open_promises": [{"what": "Kasım takvimi 5 Kasım'a kadar gönderilecek",
                                                        "due": "2026-11-05", "by": "Özge"}],
            "decisions": [{"date": "2026-09-01", "what": "Eski karar"},
                          {"date": "2026-10-20", "what": "Lansman 10 Kasım'da"}]}
        data["client-jobs"] = {"keys": ["client-job-1", "client-job-2", "client-job-3"]}
        data["client-job-1"] = {"kind": "client_job", "brand_key": "wat_mobilite", "state": "open",
                                "title_core": "Banner revizesi", "assignee_name": "Berkay Demir",
                                "app_url": f"{APP}/buckets/{WAT}/todos/7", "created_at": "2026-10-14T09:00:00+03:00",
                                "client_notes": [{"at": "16.10.2026 10:00"}, {"at": "2026-10-20 09:00"},
                                                 {"at": "01.09.2026 10:00"}]}
        data["client-job-2"] = {"kind": "client_job", "brand_key": "wat_mobilite", "state": "closed",
                                "title_core": "Eylül işi", "created_at": "2026-09-01T09:00:00+03:00",
                                "closed_at": "2026-10-22T09:00:00+03:00"}
        data["client-job-3"] = {"kind": "client_job", "brand_key": "rabam", "state": "closed", "title_core": "Eski iş",
                                "created_at": "2026-09-01T09:00:00+03:00", "closed_at": "2026-09-05T09:00:00+03:00"}

    def answer(self, *outputs):
        self.client.outputs = [message(o) for o in outputs]

    def reader(self):
        return dd.BasecampDigestReader(ACC, get=self.bc.get, excluded=dd.validate_config(config())["excluded"])

    def recordings(self):
        return mr.bm.BasecampMemoryReader(ACC, get=self.bc.get)

    def go(self, cfg=None, **kw):
        kw.setdefault("model", self.model)
        kw.setdefault("writer", self.writer)
        return mr.run_monthly_report(self.ops, config=cfg or config(), catalog=self.catalog,
                                     reader_factory=self.reader, recordings_factory=self.recordings,
                                     clock=lambda: self.now, **kw)

    def request(self, n=0):
        return json.loads(self.client.calls[n]["messages"][0]["content"])

    def text(self, subject):
        for n in range(len(self.gmail.sent)):
            if str(self.gmail.message(n)["Subject"]) == subject:
                return self.gmail.text(n).replace("\r\n", "\n")
        raise AssertionError(subject)

    def records(self):
        return {k: v for k, v in self.ops.backend.data.items() if k.startswith("monthly-report-")}

    def good(self):
        return sections(
            summary=[line("Ekim'de reels seti teslim edildi ve lansman tarihi onaylandı.", ["e2", "e3"]),
                     line("Banner tarafında tekrar eden revizeler risk oluşturuyor.", ["e4", "i2"])],
            highlights=[line("Ekim reels seti zamanında teslim edildi.", ["e2", "c1"], f"{APP}/buckets/{WAT}/todos/10"),
                        line("Banner seti teslim tarihinden sonra tamamlandı.", ["c2"])],
            decisions=[line("Müşteri lansmanı 10 Kasım olarak onayladı.", ["e3", "k1"])],
            promises=[line("Kasım takvimi 5 Kasım'a kadar gönderilecek.", ["p1"], status="devam ediyor")],
            risks=[line("“Ekim post takvimi” ay sonunda 4 gün gecikmişti @Berkay.", ["g1"],
                        f"{APP}/buckets/{WAT}/todos/1")],
            suggestions=[line("Banner briefi revizeden önce müşteriyle netleştirilebilir.", ["e4", "i2"])])


class Window(Base):
    def test_first_istanbul_weekday_1030_to_1230_else_a_no_op(self):
        for when in ("2026-11-01T10:45:00+03:00",   # Sunday the 1st: not the first weekday
                     "2026-11-02T10:29:00+03:00", "2026-11-02T12:31:00+03:00",
                     "2026-11-03T10:45:00+03:00",   # the second weekday
                     "2026-11-02T10:45:00+00:00"):  # 13:45 Istanbul
            self.now = datetime.fromisoformat(when)
            r = self.go()
            self.assertEqual((r["status"], r["reason"], r["business_writes"]),
                             ("no_new_work", "outside_monthly_report_window", 0), when)
        self.assertEqual((self.gmail.calls, self.bc.calls, self.client.calls), ([], [], []))
        self.assertEqual(self.records(), {})
        self.assertEqual(mr.first_weekday(2026, 11), date(2026, 11, 2))
        self.assertEqual(mr.first_weekday(2026, 8), date(2026, 8, 3))    # Saturday 1 Aug -> Monday 3 Aug
        self.assertEqual(mr.first_weekday(2026, 10), date(2026, 10, 1))  # Thursday
        self.assertTrue(mr.in_window(datetime.fromisoformat("2026-11-02T12:30:00+03:00")))
        self.assertTrue(mr.in_window(datetime.fromisoformat("2026-08-03T10:30:00+03:00")))
        self.assertFalse(mr.in_window(datetime.fromisoformat("2026-08-01T11:00:00+03:00")))
        self.assertEqual(mr.report_month(date(2026, 11, 2)), (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(mr.report_month(date(2027, 1, 1)), (date(2026, 12, 1), date(2026, 12, 31)))
        self.assertEqual(mr.report_key("wat_mobilite", date(2026, 10, 1)), "monthly-report-wat_mobilite-202610")

    def test_disabled_config_is_a_no_op(self):
        r = self.go(config(enabled=False))
        self.assertEqual((r["status"], r["reason"]), ("no_new_work", "digest_disabled_or_no_recipients"))
        self.assertEqual(self.gmail.calls, [])


class Send(Base):
    def test_one_mail_per_brand_to_the_digest_recipients_and_never_twice(self):
        self.answer(self.good())
        r = self.go()
        self.assertEqual((r["status"], r["month"], r["sent"], r["business_writes"]),
                         ("monthly_report_completed", "2026-10", 8, 8))
        self.assertEqual(len(self.gmail.posts()), 8)
        digest_to = dd.validate_config(dd.load_config())["recipients"]
        self.assertTrue(all(a.endswith("@gamechangers.com.tr") for a in digest_to))
        for n in range(8):
            msg = self.gmail.message(n)
            self.assertEqual([a.strip() for a in str(msg["To"]).split(",")], digest_to)
            self.assertIsNone(msg["Cc"])
        subjects = [str(self.gmail.message(n)["Subject"]) for n in range(8)]
        self.assertEqual(subjects[0], SUBJECT)  # catalog order, digest label
        self.assertIn("Hamle AI: Rabam · Ekim 2026 Aylık Marka Raporu", subjects)
        self.assertFalse([s for s in subjects if "Gamechangers" in s or "New Biz" in s])
        text = self.text(SUBJECT)
        for heading in ("AYIN ÖZETİ", "SAYILARLA AY", "ÖNE ÇIKAN İŞLER VE TESLİMLER", "MÜŞTERİYLE KARARLAR VE ONAYLAR",
                        "VERİLEN SÖZLER VE DURUMLARI", "RİSKLER VE TEKRAR EDEN SORUNLAR",
                        "GELECEK AY İÇİN ÖNERİLER (" + mr.SUGGESTION_NOTE + ")"):
            self.assertIn("\n" + heading + "\n", text, heading)
        self.assertIn(f"- Ekim reels seti zamanında teslim edildi. · {APP}/buckets/{WAT}/todos/10", text)
        self.assertIn(f"- Banner seti teslim tarihinden sonra tamamlandı. · {APP}/buckets/{WAT}/todos/11", text)
        self.assertIn("- [devam ediyor] Kasım takvimi 5 Kasım'a kadar gönderilecek.\n", text)
        self.assertIn("- Öneri: Banner briefi revizeden önce müşteriyle netleştirilebilir.\n", text)
        self.assertTrue(text.rstrip().endswith(mr.FOOTER))
        self.assertIn("Her ayın ilk hafta içi günü 10:30", mr.FOOTER)
        self.assertIn(f'href="{APP}/buckets/{WAT}/todos/10"', self.gmail.html(0))
        rabam = self.text("Hamle AI: Rabam · Ekim 2026 Aylık Marka Raporu")
        self.assertIn("Ekim 2026 ayında Rabam için kayda geçen bir gelişme yok", rabam)
        # exactly one Opus call: only WAT has material
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.request()["brand"]["key"], "wat_mobilite")
        rows = self.records()
        self.assertEqual(sorted(rows), sorted(f"monthly-report-{b}-202610" for b in BRANDS))
        self.assertEqual({v["state"] for v in rows.values()}, {"sent"})
        self.assertEqual(rows["monthly-report-wat_mobilite-202610"]["recipients"], digest_to)
        self.assertEqual(rows["monthly-report-wat_mobilite-202610"]["stats"]["source"], "model")
        self.assertTrue(all(u.startswith(API) for u in self.bc.calls))  # GET-only readers, allowlisted paths
        # a later run in the window sends nothing again and makes no model call
        self.now = datetime.fromisoformat("2026-11-02T12:00:00+03:00")
        again = self.go()
        self.assertEqual((again["status"], again["already"], again["business_writes"]), ("already_recorded", 8, 0))
        self.assertEqual((len(self.gmail.posts()), len(self.client.calls)), (8, 1))

    def test_a_partial_run_resumes_with_the_brands_not_yet_sent(self):
        self.answer(self.good())
        clock = itertools.chain([0, 0], itertools.repeat(10_000))  # start, WAT; then the time budget is gone
        r = self.go(monotonic=lambda: next(clock))
        self.assertEqual((r["status"], r["reason"], r["sent"]), ("monthly_report_partial", "run_time_budget_reached", 1))
        self.assertEqual(r["brands"]["rabam"], {"status": "not_started"})
        self.assertEqual(list(self.records()), ["monthly-report-wat_mobilite-202610"])
        self.now = datetime.fromisoformat("2026-11-02T11:15:00+03:00")
        r = self.go()
        self.assertEqual((r["status"], r["sent"], r["already"]), ("monthly_report_completed", 7, 1))
        self.assertEqual(r["brands"]["wat_mobilite"], {"status": "already_recorded"})
        self.assertEqual((len(self.gmail.posts()), len(self.client.calls)), (8, 1))
        self.assertEqual(len(self.records()), 8)

    def test_unknown_send_outcome_is_never_retried(self):
        self.answer(self.good())
        self.gmail.fail = RuntimeError("socket reset")
        r = self.go(only=None)
        self.assertEqual((r["status"], r["reason"]), ("held", "monthly_report_outcome_unknown_no_retry"))
        self.assertEqual(self.records()["monthly-report-wat_mobilite-202610"]["state"], "unknown")
        self.gmail.fail = None
        r = self.go()
        self.assertEqual(r["brands"]["wat_mobilite"], {"status": "already_recorded"})
        self.assertEqual(len(self.client.calls), 1)

    def test_a_basecamp_failure_releases_every_claim_for_a_later_run(self):
        self.bc.pages.pop(f"{API}/projects.json")
        r = self.go()
        self.assertEqual((r["status"], r["reason"], r["sent"]), ("monthly_report_partial", "basecamp_read_failed", 0))
        self.assertEqual({v["state"] for v in self.records().values()}, {"claimed"})
        self.assertEqual(self.gmail.calls, [])
        self.bc = Basecamp()
        self.answer(self.good())
        self.assertEqual(self.go()["status"], "monthly_report_completed")
        self.assertEqual(len(self.gmail.posts()), 8)

    def test_any_client_address_refuses_every_mail(self):
        r = self.go(config(recipients=["ahmet@gamechangers.com.tr", "ayse@watmobilite.com.tr"]))
        self.assertEqual((r["status"], r["reason"]), ("held", "digest_recipient_not_internal_agency"))
        self.assertEqual((self.gmail.calls, self.client.calls, self.records()), ([], [], {}))

    def test_stop_refuses_the_run_and_the_preview(self):
        self.ops.stop("ahmet@gamechangers.com.tr")
        self.assertEqual(self.go()["status"], "held")
        self.assertEqual(self.go(preview=True)["status"], "held")
        self.assertEqual((self.gmail.calls, self.client.calls, self.records()), ([], [], {}))


class Preview(Base):
    def test_preview_builds_the_text_without_sending_or_recording_and_the_run_reuses_the_answer(self):
        self.now = datetime.fromisoformat("2026-11-02T09:00:00+03:00")  # before the window: preview still builds
        self.answer(self.good())
        r = self.go(preview=True, only="wat_mobilite", writer=None)
        self.assertEqual((r["status"], r["business_writes"], r["recipients"]), ("monthly_report_preview", 0, 7))
        self.assertEqual([x["subject"] for x in r["reports"]], ["[ÖNİZLEME] " + SUBJECT])
        self.assertIn("Ekim reels seti zamanında teslim edildi.", r["reports"][0]["body"])
        self.assertEqual((self.gmail.calls, self.records()), ([], {}))
        r2 = self.go(preview=True, only="rabam", writer=None)  # empty brand: no model call
        self.assertIn("kayda geçen bir gelişme yok", r2["reports"][0]["body"])
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(self.go(only="wat_mobilite")["status"], "held")  # --report-brand only with a preview
        self.now = MON_1045
        sent = self.go()
        self.assertEqual(sent["status"], "monthly_report_completed")
        self.assertEqual(len(self.client.calls), 1)  # byte-identical request: the ModelGate answer is reused
        self.assertEqual(self.text(SUBJECT).rstrip("\n"), r["reports"][0]["body"])


class Material(Base):
    def test_numbers_are_computed_in_code(self):
        self.answer(self.good())
        self.go()
        n = self.request()["numbers"]
        self.assertEqual(n, {"todos_opened": 4, "todos_completed": 2, "completed_late": 1, "overdue_at_month_end": 2,
                             "basecamp_complete": True, "jobs_opened": 1, "jobs_closed": 1, "client_revisions": 2,
                             "events_by_kind": {"talep": 1, "teslim": 1, "onay": 1, "risk": 1}, "events_total": 4,
                             "memory_days": "28/31", "ref": "n"})
        text = self.text(SUBJECT)
        self.assertIn("SAYILARLA AY\n"
                      "- Basecamp: 4 iş açıldı, 2 iş tamamlandı (1 tanesi teslim tarihinden sonra), ay sonunda 2 iş "
                      "gecikmişti\n"
                      "- Müşteri mail işleri: 1 açıldı, 1 kapandı · Müşteri revize / ek notu: 2\n"
                      "- Marka hafızası: 4 olay (1 talep, 1 teslim, 1 onay, 1 risk) · işlenen gün 28/31\n", text)
        req = self.request()
        self.assertEqual([x["title"] for x in req["completed"]], ["Reels seti", "Banner seti"])
        self.assertEqual([x.get("late", False) for x in req["completed"]], [False, True])
        self.assertEqual([x["title"] for x in req["opened"]], ["Ekim post takvimi", "Story serisi"])
        self.assertEqual([(x["ref"], x["title"], x["days_overdue"], x["open_now"]) for x in req["overdue"]],
                         [("g1", "Ekim post takvimi", 4, True), ("g2", "Eski iş", 2, False)])
        self.assertEqual([x["title"] for x in req["next_month_due"]], ["Kasım planı"])
        self.assertEqual([x["title"] for x in req["events"]][0], "Müşteri Kasım lansmanı için görsel istedi")
        self.assertNotIn("Eylül işi", [x["title"] for x in req["events"]])
        self.assertEqual([x["what"] for x in req["decisions"]], ["Lansman 10 Kasım'da"])
        self.assertEqual([(x["title"], x.get("client_notes", 0)) for x in req["jobs"]],
                         [("Eylül işi", 0), ("Banner revizesi", 2)])
        self.assertNotIn("Hamle Fikir", json.dumps(req, ensure_ascii=False))
        self.assertEqual((req["month"], req["next_month"]),
                         ({"from": "2026-10-01", "to": "2026-10-31", "name": "Ekim 2026"},
                          {"from": "2026-11-01", "to": "2026-11-30"}))

    def test_incomplete_basecamp_pages_mark_the_numbers_as_a_minimum(self):
        b = mr.gather(self.ops, "wat_mobilite", {"name": "WAT Mobilite", "project_id": WAT}, date(2026, 10, 1),
                      date(2026, 10, 31), opened_items={}, account=ACC, jobs=[],
                      recordings=type("R", (), {"recent": staticmethod(lambda pid, kind, since: ([], since + 3600))})())
        self.assertFalse(b["numbers"]["basecamp_complete"])
        self.assertIn("en az 0 iş açıldı", mr.number_lines(b["numbers"])[0])

    def test_a_month_before_the_client_job_record_says_so_instead_of_zero(self):
        n = {"todos_opened": 1, "todos_completed": 1, "completed_late": 0, "overdue_at_month_end": 0,
             "basecamp_complete": True, "jobs_opened": 0, "jobs_closed": 0, "client_revisions": 0,
             "events_by_kind": {}, "events_total": 0, "memory_days": "30/30", "month_first": "2026-09-01"}
        line = lambda since: mr.number_lines(dict(n, jobs_since=since))[1]
        self.assertTrue(line("2026-10-13").endswith("(Hamle bu kaydı 13 Ekim tarihinde tutmaya başladı)"))
        self.assertTrue(line("2026-09-10").endswith("(Hamle bu kaydı 10 Eylül tarihinde tutmaya başladı)"))
        self.assertTrue(line(None).endswith("(Hamle henüz müşteri mail işi kaydı tutmuyordu)"))
        self.assertTrue(line("2026-08-01").endswith("Müşteri revize / ek notu: 0"))
        at = int(datetime(2026, 10, 13, 9, tzinfo=__import__('datetime').timezone.utc).timestamp())
        self.assertEqual(mr.jobs_since([{"created_at": at}, {"opened_at": at + 86400}, {}]), "2026-10-13")
        self.assertIsNone(mr.jobs_since([]))

    def test_input_is_bounded_and_the_oldest_least_important_go_first(self):
        data = self.ops.backend.data["brand-memory-wat_mobilite-202610"]
        data["events"] = [event(f"2026-10-{d % 28 + 1:02d}", "bilgi" if d % 2 else "teslim", f"Olay {d} " + "uzun " * 15,
                                detail="x" * 240) for d in range(40)]
        b = mr.gather(self.ops, "wat_mobilite", {"name": "WAT Mobilite", "project_id": WAT}, date(2026, 10, 1),
                      date(2026, 10, 31), opened_items={}, account=ACC, jobs=[], recordings=self.recordings())
        req = mr.model_input(b, date(2026, 10, 1), date(2026, 10, 31))  # fits after the texts shrink
        self.assertLessEqual(len(base_model._encoded(req)), base_model.MAX_INPUT_BYTES)
        self.assertEqual((len(req["events"]), req["omitted"]), (40, 0))
        with patch.object(mr, "MAX_INPUT_BYTES", 6000):
            small = mr.model_input(b, date(2026, 10, 1), date(2026, 10, 31))
        self.assertLessEqual(mr.bm._size(small), 6000)
        kinds = [e["kind"] for e in small["events"]]
        bilgi = [e["title"] for e in req["events"] if e["kind"] == "bilgi"]
        kept = [e["title"] for e in small["events"] if e["kind"] == "bilgi"]
        self.assertEqual(kinds.count("teslim"), 20)        # every 'teslim' kept ...
        self.assertLess(len(kept), 20)                     # ... the least important went first,
        self.assertEqual(kept, bilgi[len(bilgi) - len(kept):])  # the oldest of them first
        self.assertEqual(small["omitted"], 20 - len(kept))
        self.assertEqual(small["overdue"], req["overdue"])


class Model(Base):
    def test_a_brand_without_material_makes_no_model_call(self):
        self.bc = Basecamp(todos=[], recordings=[])
        for k in ("brand-memory-wat_mobilite-202610", "client-jobs", "brand-profile-wat_mobilite"):
            del self.ops.backend.data[k]
        r = self.go()
        self.assertEqual((r["status"], r["sent"], r["model_calls"]), ("monthly_report_completed", 8, 0))
        self.assertEqual(self.client.calls, [])
        self.assertIn("Ekim 2026 ayında WAT Mobilite için kayda geçen bir gelişme yok", self.text(SUBJECT))
        self.assertEqual({v["stats"]["source"] for v in self.records().values()}, {"empty"})

    def test_urls_refs_and_numbers_not_in_the_input_are_dropped(self):
        evil = "https://evil.example.com/phish"
        self.answer(sections(
            summary=[line("Ay içinde 2 iş tamamlandı.", ["n"]),
                     line("Ay içinde 9 iş tamamlandı.", ["n"])],                                     # invented number
            highlights=[line("Reels teslim edildi.", ["e2"], evil),                                   # -> e2's own url
                        line(f"Uydurma iş {evil}", ["zz9"]),                                          # unknown ref
                        line(f"Banner tamamlandı ({evil}).", ["c2"], f"{APP}/buckets/{RABAM}/todos/1")],
            decisions=[line("Lansman onaylandı.", ["e3"], evil)],                                     # e3: no url
            promises=[line("Takvim sözü.", ["p1"], status="kesin")],                                  # bad status
            risks=[line("Post takvimi gecikti.", ["g1"])],
            suggestions=[line("Brief netleştirilebilir.", ["e4"], evil)]))
        r = self.go()
        stats = self.records()["monthly-report-wat_mobilite-202610"]["stats"]
        self.assertEqual((stats["dropped_lines"], stats["dropped_urls"]), (3, 4))
        text = self.text(SUBJECT)
        self.assertNotIn("evil.example.com", text)
        self.assertNotIn("Uydurma", text)
        self.assertNotIn("9 iş", text)
        self.assertIn("AYIN ÖZETİ\nAy içinde 2 iş tamamlandı.\n", text)
        self.assertIn(f"- Reels teslim edildi. · {APP}/buckets/{WAT}/todos/10", text)
        self.assertIn(f"- Banner tamamlandı. · {APP}/buckets/{WAT}/todos/11", text)
        self.assertIn("- Lansman onaylandı.\n", text)
        self.assertIn(f"- Post takvimi gecikti. · {APP}/buckets/{WAT}/todos/1", text)
        self.assertIn("- Öneri: Brief netleştirilebilir.\n", text)
        self.assertIn("VERİLEN SÖZLER VE DURUMLARI\n- " + mr.EMPTY_SECTION, text)
        self.assertEqual(r["status"], "monthly_report_completed")

    def test_route_is_opus_medium_6000_without_search_through_the_model_gate(self):
        route = model_routing.load().route("monthly_report")
        self.assertEqual((route.provider, route.model, route.effort, route.max_tokens, route.web_search),
                         ("anthropic", "claude-opus-5-5", "medium", 6000, None))
        self.assertIn("monthly_report", model_routing.ROLES)
        self.answer(self.good())
        self.go()
        call = self.client.calls[0]
        self.assertEqual((call["model"], call["max_tokens"], call["output_config"]["effort"]),
                         ("claude-opus-5-5", 6000, "medium"))
        self.assertNotIn("tools", call)
        self.assertEqual([v["role"] for k, v in self.ops.backend.data.items() if k.startswith("chain-model-")],
                         ["monthly_report"])
        holds = [x for x in self.ops.backend.data["budget"]["reservations"]
                 if x["reservation_key"].startswith("chain-model-")]
        self.assertEqual([h["state"] for h in holds], ["settled"])

    def test_no_budget_sends_the_empty_brands_and_waits_with_the_rest(self):
        catalog = copy.deepcopy(self.catalog)
        catalog["budget"]["internal_live_cap_microdollars"] = 1
        poor = mr.report_source(self.ops, lambda: KEY, catalog, routing=model_routing.load(), client=self.client)
        r = self.go(model=poor)
        self.assertEqual((r["status"], r["reason"], r["sent"], r["model_calls"]),
                         ("monthly_report_partial", "model_budget_unavailable", 7, 0))
        self.assertEqual(r["brands"]["wat_mobilite"]["status"], "waiting:model_budget_unavailable")
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.records()["monthly-report-wat_mobilite-202610"]["state"], "claimed")  # released
        self.now = datetime.fromisoformat("2026-11-02T11:30:00+03:00")
        self.answer(self.good())
        r = self.go()
        self.assertEqual((r["status"], r["sent"], r["already"]), ("monthly_report_completed", 1, 7))
        self.assertEqual(len(self.gmail.posts()), 8)

    def test_a_failed_model_call_sends_the_report_built_from_the_items(self):
        self.client.outputs = [message({"bad": 1})]
        r = self.go()
        self.assertEqual(r["status"], "monthly_report_completed")
        text = self.text(SUBJECT)
        self.assertIn(mr.FALLBACK_NOTE, text)
        self.assertIn(f"- Ekim reels seti teslim edildi · {APP}/buckets/{WAT}/todos/10", text)
        self.assertIn(f"- “Ekim post takvimi” ay sonunda 4 gün gecikmişti · {APP}/buckets/{WAT}/todos/1", text)
        self.assertIn("- [belirsiz] Kasım takvimi 5 Kasım'a kadar gönderilecek (teslim: 2026-11-05)", text)
        self.assertEqual(self.records()["monthly-report-wat_mobilite-202610"]["stats"]["source"], "fallback")


class Wiring(unittest.TestCase):
    def test_worker_flags_statuses_budget_roll_and_package(self):
        import package_chain_source as pkg
        import worker
        for argv, reason in ((["--mode", "weekly_summary", "--report-preview"], "report_preview_requires_monthly_report"),
                             (["--mode", "monthly_report", "--report-brand", "wat_mobilite"],
                              "report_brand_requires_report_preview"),
                             (["--mode", "monthly_report", "--report-preview", "--report-brand", "WAT!"],
                              "report_brand_requires_report_preview")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(worker.main(argv), 2)
            self.assertEqual(json.loads(buf.getvalue())["reason"], reason, argv)
        for status in ("monthly_report_completed", "monthly_report_partial", "monthly_report_preview", "no_new_work",
                       "already_recorded"):
            self.assertIn(status, worker.OK_STATUSES)
        with open(worker.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"weekly_summary", "monthly_report", "hamle_karne", "algorithm_watch"):\n            # Monthly budget renewal', src)
        for name in ("v11_monthly_report.py", "monthly_report_prompt.txt"):
            self.assertIn(name, pkg.CHAIN_FILES)

    def test_the_report_reads_no_teams_source(self):
        with open(mr.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("teams_context", src)
        self.assertNotIn("v11_teams", src)


if __name__ == "__main__":
    unittest.main()
