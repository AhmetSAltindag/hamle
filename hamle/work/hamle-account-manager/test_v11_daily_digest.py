"""Daily digest: fakes only (Basecamp listing, Gmail REST, OpenAI transport, Operations store)."""
from model_routing import openai_only
import base64
from contextlib import contextmanager
from datetime import date, datetime
from email import policy as ep
from email.parser import BytesParser
import json
import os
import socket
import unittest
from unittest.mock import patch

import runtime_authority
import v11_daily_digest as dd
import worker
from cloud_store import OperationsStore
from test_cloud_providers import Response
from test_cloud_store import MemoryBackend, budget_snapshot
from test_v11_chain_models import Transport, envelope
from v11_chain_live import load_catalog

TOKEN = "synthetic-placeholder-token-never-live"
KEY = "FAKE-KEY-NEVER-PERSIST-OR-PRINT"
MON_0916 = datetime.fromisoformat("2026-10-12T09:16:00+03:00")
ACC = 5898865
BASE = f"https://3.basecampapi.com/{ACC}"
APP = f"https://3.basecamp.com/{ACC}"


def config(**kw):
    c = {"schema_version": 1, "enabled": True, "recipients": ["ahmet@gamechangers.com.tr", "ozge@gamechangers.com.tr"],
         "day_note": "Bugün 14:00'te ekip toplantısı var.", "basecamp_account_id": ACC}
    c.update(kw)
    return c


def todo(i, title, due, people=(), **kw):
    row = {"id": i, "title": title, "status": "active", "completed": False, "due_on": due,
           "assignees": [{"id": pid, "name": name} for pid, name in people]}
    row.update(kw)
    return row


def comments(n=7):
    return [{"created_at": f"2026-10-0{k}T10:00:00Z", "creator": {"name": "Müşteri"},
             "content": f"<p>Yorum {k}: onay bekliyoruz</p>"} for k in range(1, n + 1)]


class Basecamp:
    """GET-only fake; unknown URLs fail like the production non-200 path."""
    def __init__(self):
        self.calls = []
        self.pages = {
            f"{BASE}/projects.json": ([{"id": 1, "name": "Hamle İç Deneme", "status": "active",
                                        "dock": [{"name": "todoset", "id": 11, "enabled": True},
                                                 {"name": "message_board", "id": 12, "enabled": True}]},
                                       {"id": 2, "name": "WAT Mobilite", "status": "active",
                                        "dock": [{"name": "todoset", "id": 21, "enabled": True},
                                                 {"name": "kanban_board", "id": 22, "enabled": True}]}], {}),
            f"{BASE}/buckets/1/todosets/11/todolists.json": ([{"id": 101}], {}),
            f"{BASE}/buckets/2/todosets/21/todolists.json": ([{"id": 201}], {}),
            f"{BASE}/buckets/1/todolists/101/todos.json": (
                [todo(1001, "Afiş", "2026-10-12", [(1, "Ahmet")]),
                 todo(1002, "Bitti", "2026-10-12", completed=True)],
                {"Link": f'<{BASE}/buckets/1/todolists/101/todos.json?page=2>; rel="next"'}),
            f"{BASE}/buckets/1/todolists/101/todos.json?page=2": (
                [todo(1003, "Story", "2026-10-10", [(2, "Cansu"), (1, "Ahmet")])], {}),
            f"{BASE}/buckets/2/todolists/201/todos.json": (
                [todo(2001, "Post", None),
                 todo(2002, "WM // Yerli Sanayi Uluslararası Görseli", "2026-10-05", [(dd.HAMLE_PERSON, "Hamle")])], {}),
            f"{BASE}/buckets/2/card_tables/22.json": (
                {"id": 22, "lists": [{"id": 31, "type": "Kanban::Triage"}, {"id": 32, "type": "Kanban::DoneColumn"}]}, {}),
            f"{BASE}/buckets/2/card_tables/lists/31/cards.json": (
                [todo(3001, "Lansman kartı", "2026-10-08", [(3, "Özge")]), todo(3002, "Tarihsiz kart", None)], {}),
            f"{BASE}/buckets/2/card_tables/lists/32/cards.json": ([todo(3003, "Kapanmış", "2026-10-01")], {}),
            f"{BASE}/buckets/1/recordings/1003/comments.json": (comments(), {}),
            f"{BASE}/buckets/2/recordings/2002/comments.json": (comments(), {}),
            f"{BASE}/buckets/2/recordings/3001/comments.json": (comments(2), {}),
        }

    def add_report(self):
        self.pages[f"{BASE}/reports/todos/overdue.json"] = (
            {"under_a_week_late": [dict(todo(1003, "Story", "2026-10-10", [(2, "Cansu"), (1, "Ahmet")]),
                                        bucket={"id": 1, "name": "Hamle İç Deneme"})],
             "over_a_week_late": [dict(todo(2002, "WM // Yerli Sanayi Uluslararası Görseli", "2026-10-05",
                                            [(dd.HAMLE_PERSON, "Hamle")]), bucket={"id": 2, "name": "WAT Mobilite"})]},
            {})

    def get(self, url):
        self.calls.append(url)
        if url not in self.pages:
            raise dd.DigestError("basecamp_read_failed")
        payload, headers = self.pages[url]
        return json.loads(json.dumps(payload)), headers

    def comment_reads(self):
        return [u for u in self.calls if u.endswith("/comments.json")]


class Gmail:
    def __init__(self):
        self.sent, self.calls, self.fail, self.mutate = {}, [], None, None

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        assert kw["allow_redirects"] is False
        if method == "POST":
            if self.fail:
                raise self.fail
            raw = base64.urlsafe_b64decode(json.loads(kw["data"])["raw"])
            mid = "d%03d" % (len(self.sent) + 1)
            self.sent[mid] = self.mutate(raw) if self.mutate else raw
            return Response({"id": mid, "labelIds": ["SENT"]})
        mid = url.split("/messages/")[1].split("?")[0]
        return Response({"id": mid, "labelIds": ["SENT"],
                         "raw": base64.urlsafe_b64encode(self.sent[mid]).decode().rstrip("=")})

    def close(self):
        pass

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]

    def message(self, n=-1):
        return BytesParser(policy=ep.default).parsebytes(list(self.sent.values())[n])

    def text(self, n=-1):
        return self.message(n).get_content()


def classification(*rows):
    return {"items": [{"ref": ref, "status": status, "reason": reason, "action": action}
                      for ref, status, reason, action in rows]}


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.now = getattr(self, "START", MON_0916)
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: self.now)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.ops.resume_review_only("offline-test")
        self.bc, self.gmail = Basecamp(), Gmail()
        self.cards, self.calendar = dd.load_brand_cards(), dd.load_calendar()

        @contextmanager
        def token():
            yield TOKEN
        self.writer = dd.DigestMailWriter(token_context=token, session_factory=lambda: self.gmail)

    def reader(self):
        return dd.BasecampDigestReader(ACC, get=self.bc.get)

    def classifier(self, transport):
        return dd.model_classifier(self.ops, lambda: KEY, load_catalog(), transport=transport, routing=openai_only())

    def run_digest(self, cfg=None, reader_factory=None, **kw):
        kw.setdefault("cards", self.cards)
        kw.setdefault("calendar", self.calendar)
        return dd.run_daily_digest(self.ops, config=cfg or config(), reader_factory=reader_factory or self.reader,
                                   writer=self.writer, clock=lambda: self.now, **kw)

    def chain_reservations(self):
        return [r for r in self.ops.backend.data["budget"]["reservations"]
                if r["reservation_key"].startswith("chain-model-")]


class Config(Base):
    def test_only_internal_agency_recipients(self):
        for bad in (["client@example.com"], ["ahmet@gamechangers.com.tr", "x@gamechangers.com.tr.evil.com"],
                    ["hamle@gamechangers.com.tr"], ["ahmet@gamechangers.com.tr"] * 2, "ahmet@gamechangers.com.tr"):
            with self.assertRaises(dd.DigestError):
                dd.validate_config(config(recipients=bad))
        r = self.run_digest(config(recipients=["ahmet@gamechangers.com.tr", "musteri@wat.com.tr"]))
        self.assertEqual((r["status"], r["reason"]), ("held", "digest_recipient_not_internal_agency"))
        self.assertEqual((self.gmail.calls, self.bc.calls), ([], []))

    def test_disk_config_is_ahmets_team_list(self):
        cfg = dd.validate_config(dd.load_config())
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["recipients"], [n + "@gamechangers.com.tr" for n in
                         ("ahmet", "cansu", "ozge", "baris", "berkay", "ipek", "esma")])
        self.assertEqual(cfg["excluded"], frozenset({47875291}))
        self.assertIs(cfg["trend_search"], False)

    def test_excluded_projects_validated_and_skipped(self):
        for bad in ("47875291", [0], [True], ["1"]):
            with self.assertRaises(dd.DigestError):
                dd.validate_config(config(excluded_project_ids=bad))
        reader = dd.BasecampDigestReader(ACC, get=self.bc.get, excluded={40704459})
        listing = reader.collect()
        self.assertNotIn(40704459, listing["project_names"])

    def test_disk_brand_cards_and_calendar_are_versioned_drafts(self):
        for doc in (self.cards, self.calendar):
            self.assertEqual((doc["schema_version"], doc["status"]), (1, "taslak"))
            self.assertIn("taslak", doc["version"])
        self.assertIs(self.cards["verified"], False)
        brands = {b["project_id"]: b for b in self.cards["brands"]}
        self.assertEqual(set(brands), {40704523, 40704530, 43886681, 47875291, 44320349, 40704459, 49184053,
                                       48562916, 40704547, 48639990, 40694338, 48917059})
        self.assertTrue(all(b["verified"] is False and b["status"] == "taslak" for b in brands.values()))
        self.assertFalse(brands[49184053]["include_in_suggestions"])  # internal test project
        days = {d["date"]: d["name"] for d in self.calendar["days"]}
        for day, name in (("2026-10-29", "Cumhuriyet"), ("2026-11-10", "Atatürk"), ("2026-10-31", "Cadılar"),
                          ("2026-11-24", "Öğretmenler"), ("2026-11-27", "Black Friday"), ("2026-12-31", "Yılbaşı")):
            self.assertIn(name, days[day])


class Digest(Base):
    def test_first_run_after_0915_sends_once_with_links_and_readback(self):
        r = self.run_digest()
        self.assertEqual(r["status"], "digest_sent", r)
        self.assertEqual((r["stats"]["open_items"], r["stats"]["overdue"], len(self.gmail.posts())), (5, 3, 1))
        self.assertEqual(r["stats"]["overdue_source"], "scan")
        msg = self.gmail.message()
        self.assertEqual(msg["To"], "ahmet@gamechangers.com.tr, ozge@gamechangers.com.tr")
        self.assertIsNone(msg["Cc"])
        text = self.gmail.text()
        for needle in ("Bugün teslim: 1", "Gecikmiş: 3", "Günün notu:", "14:00'te ekip toplantısı", "BUGÜN",
                       f"- Afiş · Hamle İç Deneme · @Ahmet · {APP}/buckets/1/todos/1001", "GECİKENLER",
                       f"Lansman kartı · WAT Mobilite · @Özge · 4 gün gecikme · sınıflandırılmadı · "
                       f"{APP}/buckets/2/card_tables/cards/3001", "TREND RADARI", dd.NO_TRENDS):
            self.assertIn(needle, text)
        for absent in ("Kapanmış", "Tarihsiz kart", "Bitti"):
            self.assertNotIn(absent, text)
        row = self.ops.backend.data["daily-digest-20261012"]
        self.assertEqual((row["state"], row["recipients"]), ("sent", config()["recipients"]))
        self.now = datetime.fromisoformat("2026-10-12T11:30:00+03:00")
        self.assertEqual(self.run_digest()["status"], "already_recorded")
        self.assertEqual(len(self.gmail.posts()), 1)
        self.assertTrue(all(u.startswith(BASE + "/") for u in self.bc.calls))  # GET-only reader, no writes exist

    def test_window_weekday_and_deadline(self):
        for when in ("2026-10-12T09:14:00+03:00", "2026-10-12T18:31:00+03:00", "2026-10-10T10:00:00+03:00"):
            self.now = datetime.fromisoformat(when)
            self.assertEqual(self.run_digest()["reason"], "outside_digest_window")
        self.now = datetime.fromisoformat("2026-10-13T09:16:00+03:00")
        self.assertEqual(self.run_digest()["reason"], "authorization_expired")
        self.assertEqual(self.gmail.calls, [])

    def test_uncertain_send_is_never_resent(self):
        self.gmail.fail = TimeoutError("synthetic")
        r = self.run_digest()
        self.assertEqual((r["status"], r["reason"]), ("held", "digest_outcome_unknown_no_retry"))
        self.assertEqual(self.ops.backend.data["daily-digest-20261012"]["state"], "unknown")
        self.gmail.fail = None
        self.now = datetime.fromisoformat("2026-10-12T09:31:00+03:00")
        self.assertEqual(self.run_digest()["status"], "already_recorded")
        self.assertEqual(len(self.gmail.posts()), 1)

    def test_readback_mismatch_is_unknown(self):
        self.gmail.mutate = lambda raw: raw.replace(b"Subject: Hamle", b"Subject: Xamle")
        self.assertEqual(self.run_digest()["reason"], "digest_outcome_unknown_no_retry")

    def test_read_failure_before_send_releases_day_for_next_run(self):
        def broken():
            raise dd.DigestError("basecamp_read_failed")
        r = self.run_digest(reader_factory=lambda: type("R", (), {"collect": staticmethod(broken)})())
        self.assertEqual((r["status"], r["reason"], r["business_writes"]), ("held", "basecamp_read_failed", 0))
        self.now = datetime.fromisoformat("2026-10-12T09:31:00+03:00")
        self.assertEqual(self.run_digest()["status"], "digest_sent")
        self.assertEqual(len(self.gmail.posts()), 1)

    def test_global_stop_means_no_read_and_no_send(self):
        self.ops.set_mode("stopped", "offline-test")
        self.assertEqual(self.run_digest()["status"], "held")
        self.assertEqual((self.gmail.calls, self.bc.calls), ([], []))

    def test_reader_refuses_out_of_scope_link_and_is_bounded(self):
        self.bc.pages[f"{BASE}/projects.json"] = ([], {"Link": '<https://evil.example/x>; rel="next"'})
        with self.assertRaises(dd.DigestError):
            self.reader().collect()
        url = f"{BASE}/projects.json"
        self.bc.pages[url] = ([], {"Link": f'<{url}?page=2>; rel="next"'})
        self.bc.pages[url + "?page=2"] = ([], {"Link": f'<{url}?page=2>; rel="next"'})
        with self.assertRaises(dd.DigestError):
            dd.BasecampDigestReader(ACC, get=self.bc.get, max_pages=3).collect()
        with self.assertRaises(dd.DigestError):
            self.reader()._fetch(f"{BASE}/buckets/1/todos/1001/completion.json")


class Overdue(Base):
    def build(self, day=date(2026, 10, 12), **kw):
        kw.setdefault("cards", self.cards)
        kw.setdefault("calendar", self.calendar)
        return dd.build_digest(self.reader(), day=day, day_note=None, **kw)

    def test_account_report_preferred_and_cards_merged(self):
        self.bc.add_report()
        body, stats = self.build()
        self.assertEqual((stats["overdue_source"], stats["overdue"]), ("report", 3))
        self.assertIn(f"{BASE}/reports/todos/overdue.json", self.bc.calls)
        self.assertIn("Lansman kartı", body)

    def test_scan_fallback_when_report_unavailable(self):
        body, stats = self.build()
        self.assertEqual(stats["overdue_source"], "scan")
        self.assertIn("Story · Hamle İç Deneme · @Ahmet @Cansu · 2 gün gecikme", body)

    def test_one_batched_call_classifies_with_marks(self):
        out = classification(("o1", "waiting_client", "Müşteri onayı 3 gündür bekleniyor.", "Tarihi kaydırmayı öner."),
                             ("o2", "action_on_us", "Son yorumda revizyon bizden istendi.", "Revizyonu bugün paylaş."),
                             ("o9", "looks_done", "uydurma", "uydurma"))
        transport = Transport(envelope(out))
        body, stats = self.build(classifier=self.classifier(transport))
        self.assertEqual(len(transport.calls), 1)
        req = transport.calls[0]
        self.assertEqual(req["text"]["format"]["name"], "hamle_chain_digest")
        self.assertEqual((req["tools"], req["tool_choice"]), ([], "none"))
        self.assertEqual(stats["classified"], 2)
        self.assertIn("🟡 Lansman kartı · WAT Mobilite · @Özge · 4 gün gecikme · Müşteri onayı 3 gündür bekleniyor. "
                      f"Öneri: Tarihi kaydırmayı öner. · {APP}/buckets/2/card_tables/cards/3001", body)
        self.assertIn("🔴 Story", body)
        self.assertNotIn("uydurma", body)  # unknown ref ignored, never invented
        self.assertEqual(len(self.chain_reservations()), 1)  # counted against the chain ledger / 30 USD cap

    def test_last_five_comments_only_and_hamle_item_listed_only(self):
        transport = Transport(envelope(classification()))
        body, _ = self.build(classifier=self.classifier(transport))
        self.assertNotIn(f"{BASE}/buckets/2/recordings/2002/comments.json", self.bc.comment_reads())
        self.assertIn("WM // Yerli Sanayi Uluslararası Görseli · WAT Mobilite · @Hamle · 7 gün gecikme · "
                      "Hamle'ye atanmış; yalnız listelendi.", body)
        text = json.dumps(transport.calls[0], ensure_ascii=False)
        self.assertNotIn("Yerli Sanayi", text)
        self.assertIn("Yorum 7", text)
        story = dd.BasecampDigestReader(ACC, get=self.bc.get).last_comments(
            {"comments_url": f"{BASE}/buckets/1/recordings/1003/comments.json"})
        self.assertEqual([c["text"] for c in story], [f"Yorum {k}: onay bekliyoruz" for k in range(3, 8)])

    def test_model_error_or_no_budget_lists_unclassified(self):
        transport = Transport(RuntimeError("synthetic transport failure"))
        body, stats = self.build(classifier=self.classifier(transport))
        self.assertEqual(len(transport.calls), 1)
        self.assertTrue(stats["model"].startswith("unclassified:"), stats)
        self.assertIn("(sınıflandırılmadı", body)
        self.assertNotIn("🔴", body); self.assertNotIn("🟡", body); self.assertNotIn("⚪", body)
        # cap reached -> no key load, no transport call
        catalog = load_catalog()
        catalog["budget"]["internal_live_cap_microdollars"] = 1
        keys = []
        transport2 = Transport(envelope(classification()))
        clf = dd.model_classifier(self.ops, lambda: keys.append(1) or KEY, catalog, transport=transport2,
                                  routing=openai_only())
        body, stats = self.build(classifier=clf)
        self.assertEqual((transport2.calls, keys), ([], []))
        self.assertEqual(stats["model"], "unclassified:model_budget_unavailable")
        self.assertIn("sınıflandırılmadı", body)

    def test_comment_reading_capped_at_forty(self):
        many = [todo(5000 + i, f"Eski iş {i}", "2026-09-%02d" % (1 + i % 28), [(4, "Cansu")]) for i in range(45)]
        self.bc.pages[f"{BASE}/buckets/2/todolists/201/todos.json"] = (many, {})
        for t in many:
            self.bc.pages[f"{BASE}/buckets/2/recordings/{t['id']}/comments.json"] = ([], {})
        seen = []
        def fake(model_input):
            seen.append(model_input)
            return {"items": []}
        body, stats = self.build(classifier=fake)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(seen[0]["items"]), dd.MAX_CLASSIFIED)
        self.assertEqual(len(self.bc.comment_reads()), dd.MAX_CLASSIFIED)
        self.assertLessEqual(len(json.dumps(seen[0], ensure_ascii=False).encode()), dd.MODEL_INPUT_BUDGET)
        self.assertIn("geciken iş daha", body)
        self.assertLessEqual(len([l for l in body.splitlines() if l.strip()]), 27)

    def test_same_input_reuses_cached_result_preview_then_team(self):
        out = classification(("o1", "action_on_us", "Bizde.", "Bugün bitir."))
        transport = Transport(envelope(out))
        self.build(classifier=self.classifier(transport))
        self.build(classifier=self.classifier(transport))
        self.assertEqual((len(transport.calls), len(self.chain_reservations())), (1, 1))


class Sections(Base):
    def test_trends_require_https_source_and_fetched_date(self):
        raw = [{"title": "Kaynaklı trend", "url": "https://example.org/a", "published_at": "2026-10-10",
                "summary": "Kısa"},
               {"title": "Kaynaksız", "url": None, "published_at": "2026-10-10"},
               {"title": "http", "url": "http://example.org/b", "published_at": "2026-10-10"},
               {"title": "Tarihsiz", "url": "https://example.org/c"},
               {"title": "Gelecek", "url": "https://example.org/d", "published_at": "2026-10-20"},
               {"title": "Eski", "url": "https://example.org/e", "published_at": "2026-08-01"}]
        self.assertEqual([t["title"] for t in dd.sourced_trends(raw, date(2026, 10, 12))], ["Kaynaklı trend"])
        body, stats = dd.build_digest(self.reader(), day=date(2026, 10, 12), day_note=None,
                                      trend_source=lambda day: raw, cards=self.cards, calendar=self.calendar)
        self.assertIn("- Kaynaklı trend (2026-10-10) · https://example.org/a Uyarlama:", body)
        self.assertNotIn(dd.NO_TRENDS, body)
        body, stats = dd.build_digest(self.reader(), day=date(2026, 10, 12), day_note=None,
                                      trend_source=lambda day: raw[1:], cards=self.cards, calendar=self.calendar)
        self.assertIn(dd.NO_TRENDS, body)
        def boom(day):
            raise RuntimeError("x")
        body, stats = dd.build_digest(self.reader(), day=date(2026, 10, 12), day_note=None, trend_source=boom)
        self.assertEqual(stats["trends"], "provider_failed")
        self.assertIn(dd.NO_TRENDS, body)

    def test_special_days_only_on_monday_with_brand_suggestions(self):
        body, _ = dd.build_digest(self.reader(), day=date(2026, 10, 12), day_note=None,
                                  cards=self.cards, calendar=self.calendar)
        self.assertIn("ÖZEL GÜNLER (14 gün)", body)
        self.assertIn("16.10 Cum · Dünya Gıda Günü · öneri: Cacaonly (", body)
        body, _ = dd.build_digest(self.reader(), day=date(2026, 10, 13), day_note=None,
                                  cards=self.cards, calendar=self.calendar)
        self.assertNotIn("ÖZEL GÜNLER", body)
        rows = dd.special_days(self.calendar, self.cards, date(2026, 10, 26))
        names = [r["name"] for r in rows]
        self.assertTrue(any("Cumhuriyet" in n for n in names) and any("Cadılar" in n for n in names))
        self.assertFalse(any(b["brand"] in ("Hamle İç Deneme", "New Biz", "Bikaldi") for r in rows for b in r["brands"]))


class Preview(Base):
    def test_preview_goes_to_one_agency_address_under_separate_key(self):
        self.now = datetime.fromisoformat("2026-10-12T18:45:00+03:00")  # outside the team window
        r = self.run_digest(config(enabled=False, recipients=[]), preview_to="ahmet@gamechangers.com.tr")
        self.assertEqual((r["status"], r["key"], r["recipients"]), ("digest_sent", "daily-digest-preview-20261012", 1))
        msg = self.gmail.message()
        self.assertEqual(msg["To"], "ahmet@gamechangers.com.tr")
        self.assertTrue(str(msg["Subject"]).startswith("[ÖNİZLEME] Hamle günlük özet"))
        self.assertEqual(self.ops.backend.data["daily-digest-preview-20261012"]["kind"], "daily_digest_preview")
        self.assertNotIn("daily-digest-20261012", self.ops.backend.data)
        self.assertEqual(self.run_digest(preview_to="ahmet@gamechangers.com.tr")["status"], "already_recorded")
        self.assertEqual(self.run_digest()["reason"], "outside_digest_window")
        self.now = datetime.fromisoformat("2026-10-12T18:20:00+03:00")
        team = self.run_digest()
        self.assertEqual((team["status"], team["key"]), ("digest_sent", "daily-digest-20261012"))
        self.assertEqual(len(self.gmail.posts()), 2)

    def test_preview_refuses_non_agency_or_hamle_address(self):
        for bad in ("musteri@wat.com.tr", "hamle@gamechangers.com.tr", "a@gamechangers.com.tr.evil.com",
                    "ahmet@gamechangers.com.tr, ozge@gamechangers.com.tr"):
            r = self.run_digest(preview_to=bad)
            self.assertEqual((r["status"], r["reason"]), ("held", "digest_preview_not_internal_agency"))
        self.assertEqual((self.gmail.calls, self.bc.calls), ([], []))


class PreviewDay(Base):
    START = datetime.fromisoformat("2026-10-11T20:00:00+03:00")  # Sunday evening

    def test_sunday_preview_rendered_as_of_monday(self):
        r = self.run_digest(preview_to="ahmet@gamechangers.com.tr", preview_day="2026-10-12")
        self.assertEqual((r["status"], r["key"]), ("digest_sent", "daily-digest-preview-20261012"), r)
        msg = self.gmail.message()
        self.assertEqual(str(msg["Subject"]), "[ÖNİZLEME] Hamle günlük özet — 12.10.2026")
        text = msg.get_content()
        for needle in ("12.10.2026 Pazartesi", f"- Afiş · Hamle İç Deneme · @Ahmet · {APP}/buckets/1/todos/1001",
                       "Story · Hamle İç Deneme · @Ahmet @Cansu · 2 gün gecikme", "ÖZEL GÜNLER (14 gün)"):
            self.assertIn(needle, text)
        row = self.ops.backend.data["daily-digest-preview-20261012"]
        self.assertEqual((row["date"], row["run_date"]), ("2026-10-12", "2026-10-11"))
        again = self.run_digest(preview_to="ahmet@gamechangers.com.tr", preview_day="2026-10-12")
        self.assertEqual(again["status"], "already_recorded")
        today = self.run_digest(preview_to="ahmet@gamechangers.com.tr", preview_day="2026-10-11")
        self.assertEqual((today["status"], today["key"]), ("digest_sent", "daily-digest-preview-20261011"))
        self.assertNotIn("ÖZEL GÜNLER", self.gmail.text())
        self.assertIn("Story · Hamle İç Deneme · @Ahmet @Cansu · 1 gün gecikme", self.gmail.text())
        self.assertNotIn("daily-digest-20261012", self.ops.backend.data)

    def test_target_day_range_and_flag_pairing(self):
        for bad, code in (("2026-10-13", "digest_preview_day_out_of_range"),
                          ("2026-10-10", "digest_preview_day_out_of_range"),
                          ("12.10.2026", "digest_preview_day_invalid"), ("20261012", "digest_preview_day_invalid")):
            r = self.run_digest(preview_to="ahmet@gamechangers.com.tr", preview_day=bad)
            self.assertEqual((r["status"], r["reason"]), ("held", code))
        r = self.run_digest(preview_day="2026-10-12")
        self.assertEqual(r["reason"], "digest_preview_day_requires_preview_to")
        self.assertEqual((self.gmail.calls, self.bc.calls), ([], []))
        self.assertEqual(dd.preview_target("2026-10-12", date(2026, 10, 9)), date(2026, 10, 12))  # Fri -> Mon
        self.assertEqual(dd.preview_target("2026-10-09", date(2026, 10, 8)), date(2026, 10, 9))
        for today in (date(2026, 10, 8), date(2026, 10, 10)):
            if today == date(2026, 10, 10):
                self.assertEqual(dd.preview_target("2026-10-12", today), date(2026, 10, 12))  # Sat -> Mon
            else:
                with self.assertRaises(dd.DigestError):
                    dd.preview_target("2026-10-12", today)  # Thu: next weekday is Fri

    def test_team_send_reuses_classifier_only_on_byte_identical_input(self):
        out = classification(("o1", "action_on_us", "Bizde.", "Bugün bitir."))
        transport = Transport(envelope(out), envelope(out))
        r = self.run_digest(preview_to="ahmet@gamechangers.com.tr", preview_day="2026-10-12",
                            classifier=self.classifier(transport))
        self.assertEqual((r["status"], len(transport.calls)), ("digest_sent", 1))
        self.now = MON_0916
        team = self.run_digest(classifier=self.classifier(transport))
        self.assertEqual((team["status"], len(transport.calls), len(self.chain_reservations())), ("digest_sent", 1, 1))
        self.assertIn("🔴", self.gmail.text())
        # one changed comment -> different input bytes -> a new (single) call, never the old result
        self.bc.pages[f"{BASE}/buckets/2/recordings/3001/comments.json"][0][0]["content"] = "Yeni yorum"
        dd.build_digest(self.reader(), day=date(2026, 10, 12), day_note=None, classifier=self.classifier(transport))
        self.assertEqual((len(transport.calls), len(self.chain_reservations())), (2, 2))


class RealVolume(Base):
    """Mac readback shape: 7 due today, 15 overdue across 6 projects, 57 comments in last-5 windows."""
    def setUp(self):
        super().setUp()
        projects, n, left = [], 0, 57
        for p in range(1, 7):
            projects.append({"id": p, "name": f"Marka {p}", "status": "active",
                             "dock": [{"name": "todoset", "id": p * 10, "enabled": True}]})
            self.bc.pages[f"{BASE}/buckets/{p}/todosets/{p * 10}/todolists.json"] = ([{"id": p * 100}], {})
            rows = []
            for k in range(4):
                n += 1
                due = "2026-10-12" if n <= 7 else "2026-10-%02d" % (1 + n % 10) if n <= 22 else None
                rows.append(todo(p * 1000 + k, f"İş {n} " + "uzun başlık " * 6, due, [(n % 4 + 1, f"Kişi{n % 4}")]))
                if due and due < "2026-10-12":
                    take = min(5, left - max(0, (22 - n)) * 3) if n < 22 else left
                    take = max(1, min(5, take))
                    left -= take
                    self.bc.pages[f"{BASE}/buckets/{p}/recordings/{p * 1000 + k}/comments.json"] = (
                        [{"created_at": f"2026-10-0{c + 1}T10:00:00Z", "creator": {"name": "Müşteri Ekibi"},
                          "content": "Uzun yorum " * 40} for c in range(take + 2)], {})
            self.bc.pages[f"{BASE}/buckets/{p}/todolists/{p * 100}/todos.json"] = (rows, {})
        self.bc.pages[f"{BASE}/projects.json"] = (projects, {})

    def test_mail_stays_near_25_lines_with_overflow_links(self):
        seen = []
        def fake(model_input):
            seen.append(model_input)
            return classification(*[(it["ref"], "waiting_client", "Müşteri onayı bekleniyor.", "Tarihi kaydır.")
                                    for it in model_input["items"]])
        body, stats = dd.build_digest(self.reader(), day=date(2026, 10, 12), day_note="Toplantı 14:00",
                                      classifier=fake, trend_source=lambda day: [
                                          {"title": f"Trend {i}", "url": f"https://example.org/{i}",
                                           "published_at": "2026-10-10"} for i in range(5)],
                                      cards=self.cards, calendar=self.calendar)
        self.assertEqual((stats["overdue"], stats["classified"], len(seen)), (15, 15, 1))
        self.assertLessEqual(len(json.dumps(seen[0], ensure_ascii=False).encode()), dd.MODEL_INPUT_BUDGET)
        lines = [l for l in body.splitlines() if l.strip()]
        self.assertLessEqual(len(lines), 26, body)
        self.assertIn("Bugün teslim: 7 · Gecikmiş: 15 (6 proje)", body)
        more_today = next(l for l in lines if l.startswith("- … +4 iş daha: "))
        self.assertEqual(more_today.count(APP), 4)
        self.assertIn(f"- … +9 geciken iş daha · tüm liste: {APP}/reports/todos/overdue", body)
        self.assertIn("GECİKENLER (🟡 15)", body)


class Runtime(unittest.TestCase):
    def test_single_deadline_value_drives_store_and_windows(self):
        import cloud_store
        self.assertEqual(runtime_authority.AUTHORIZATION_DEADLINE, "2026-10-12T23:59:59+03:00")
        self.assertEqual(cloud_store.DEADLINE, runtime_authority.deadline_timestamp())
        self.assertEqual(runtime_authority.window_minutes(runtime_authority.CHAIN_TICK_WINDOW), (555, 1110))
        self.assertEqual(runtime_authority.window_minutes(runtime_authority.SCHEDULED_REVIEW_WINDOW), (510, 1110))
        with open(runtime_authority.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('AUTHORIZATION_DEADLINE = "2026-11-12T23:59:59+03:00"', src)  # documented one-line change

    def test_worker_daily_digest_mode_requires_cloud_opt_in(self):
        with patch.dict(os.environ, {}, clear=True), patch("builtins.print") as out:
            self.assertNotEqual(worker.main(["--mode", "daily_digest"]), 0)
        self.assertIn("cloud_opt_in_required", str(out.call_args))

    def test_worker_preview_flag_only_with_daily_digest_and_agency_address(self):
        for argv in (["--mode", "chain_tick", "--preview-to", "ahmet@gamechangers.com.tr"],
                     ["--mode", "daily_digest", "--preview-to", "musteri@wat.com.tr"],
                     ["--mode", "daily_digest", "--preview-to", "hamle@gamechangers.com.tr"]):
            with patch.dict(os.environ, {}, clear=True), patch("builtins.print") as out:
                self.assertNotEqual(worker.main(argv), 0)
            self.assertIn("preview_to_requires_daily_digest_and_agency_address", str(out.call_args))
        with patch.dict(os.environ, {}, clear=True), patch("builtins.print") as out:
            worker.main(["--mode", "daily_digest", "--preview-to", "ahmet@gamechangers.com.tr"])
        self.assertIn("cloud_opt_in_required", str(out.call_args))

    def test_packaged_with_prompt_and_draft_json(self):
        with patch.dict(os.environ, {}, clear=True), patch("builtins.print") as out:
            self.assertNotEqual(worker.main(["--mode", "daily_digest", "--preview-day", "2026-10-12"]), 0)
        self.assertIn("preview_day_requires_preview_to", str(out.call_args))
        import package_chain_source as pkg
        self.assertIn("chain_digest_prompt.txt", pkg.CHAIN_FILES)
        values = set(pkg.ALLOWLIST.values())
        for name in ("brand-cards.json", "tr-special-days-2026.json"):
            self.assertIn(pkg.PROFILE + name, values)


if __name__ == "__main__":
    unittest.main()
