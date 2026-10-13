"""Customer mail silent trial: fakes only (no sockets, no keys, no Gmail/Basecamp/Anthropic)."""
import copy
import socket
import unittest
from datetime import datetime
from unittest.mock import patch

import model_routing
import v11_client_watch as cw
from autonomy_policy import scope_digest
from cloud_store import OperationsStore
from test_anthropic_routing import FakeClient, KEY, message
from test_cloud_store import MemoryBackend, budget_snapshot
from v11_chain_live import load_catalog
from v11_daily_digest import load_brand_cards

TUE_1000 = datetime.fromisoformat("2026-10-13T10:00:00+03:00")
TUE_1830 = datetime.fromisoformat("2026-10-13T18:30:00+03:00")


def contacts(**extra):
    doc = {"schema_version": 1, "source": "test tablo",
           "brands": {"wat_mobilite": {"domains": ["watmobilite.com.tr"],
                                       "contacts": [{"email": "ayse@watmobilite.com.tr", "name": "Ayşe Yılmaz",
                                                     "title": "Pazarlama Müdürü"}]},
                      "altinmarka": {"contacts": [{"email": "mert.kaya@gmail.com", "name": "Mert Kaya"}]}},
           "test_contacts": [{"email": "ahmet.test@gmail.com", "brand_key": "gamechangers", "name": "Test Müşteri"}]}
    doc.update(extra)
    return doc


def config(**kw):
    c = {"schema_version": 1, "enabled": True, "mode": "silent_draft", "summary_to": ["ahmet@gamechangers.com.tr"],
         "summary_after": "18:25", "max_threads": 10, "max_drafts_per_run": 3, "newer_than_days": 2,
         "run_seconds": 240}
    c.update(kw)
    return c


def source(n, *, thread="t1", sender="ayse@watmobilite.com.tr", subject="Ekim post revizesi", text=None, ms=None):
    return {"message_id": f"m{n}", "thread_id": thread, "sender": sender, "to": ["ozge@gamechangers.com.tr"],
            "cc": ["hamle@gamechangers.com.tr"], "subject": subject,
            "text": text or "Merhaba Özge,\nYarınki post için görseli biraz daha aydınlık yapabilir miyiz?\nTeşekkürler",
            "received_at_ms": ms or 1791900000000 + n, "fingerprint": f"fp{n}", "has_attachments": False}


def draft_out(category="request", reply="Merhaba Ayşe,\nTalebinizi aldık, ekip görseli güncelleyecek.\nSevgiler,\nHamle"):
    return {"category": category, "summary": "Müşteri görselin daha aydınlık olmasını istiyor.", "reply": reply,
            "missing_info": [], "team_next_step": "Özge görseli Berkay'a revizeye versin.", "confidence": 82}


class Writer:
    def __init__(self, start=500):
        self.calls, self.fail, self.next = [], None, start

    def write(self, payload):
        self.calls.append(copy.deepcopy(payload))
        if self.fail:
            raise self.fail
        self.next += 1
        return {"remote_id": str(self.next)}

    def readback(self, remote_id, payload):
        return {"readback_sha256": scope_digest(payload)}


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.now = TUE_1000
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: self.now)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.ops.resume_review_only("offline-test")
        self.catalog, self.cards = load_catalog(), load_brand_cards()
        self.book = cw.validate_contacts(contacts(), self.catalog, self.cards)
        self.mails, self.reads = [], 0
        self.client = FakeClient()
        self.todos, self.comments, self.summaries = Writer(500), Writer(900), Writer(700)

    def reader(self):
        self.reads += 1
        return copy.deepcopy(self.mails)

    def watch(self, cfg=None):
        return cw.ClientWatch(self.ops, config=cfg or config(), book=self.book, reader=self.reader,
                              draft=cw.draft_source(self.ops, lambda: KEY, self.catalog, routing=model_routing.load(),
                                                    client=self.client),
                              task_writer=self.todos, comment_writer=self.comments, mail_writer=self.summaries,
                              catalog=self.catalog, clock=lambda: self.now)

    def row(self, message_id):
        return self.ops.backend.data.get(cw.mail_key(message_id))


class Contacts(Base):
    def test_table_rules(self):
        bad = [contacts(brands={"uydurma": {"contacts": []}}),
               contacts(brands={"wat_mobilite": {"domains": ["gmail.com"]}}),
               contacts(brands={"wat_mobilite": {"domains": ["gamechangers.com.tr"]}}),
               contacts(brands={"wat_mobilite": {"contacts": [{"email": "ozge@gamechangers.com.tr"}]}}),
               contacts(brands={"wat_mobilite": {"domains": ["watmobilite.com.tr"]},
                                "rabam": {"contacts": [{"email": "x@watmobilite.com.tr"}]}}),
               contacts(brands={"wat_mobilite": {"contacts": [{"email": "a@b.co", "phone": "1"}]}}),
               contacts(brands={})]
        for doc in bad:
            with self.assertRaises(cw.ClientWatchError):
                cw.validate_contacts(doc, self.catalog, self.cards)

    def test_match_exact_domain_test_and_never_agency(self):
        m = self.book.match("Ayse@WatMobilite.com.tr")
        self.assertEqual((m["brand_key"], m["name"], m["test"]), ("wat_mobilite", "Ayşe Yılmaz", False))
        self.assertEqual(self.book.match("baska.kisi@watmobilite.com.tr")["brand_key"], "wat_mobilite")
        self.assertEqual(self.book.match("mert.kaya@gmail.com")["brand_key"], "altinmarka")
        self.assertIsNone(self.book.match("baska@gmail.com"))  # public domain: exact addresses only
        self.assertIsNone(self.book.match("ozge@gamechangers.com.tr"))
        self.assertTrue(self.book.match("ahmet.test@gmail.com")["test"])
        self.assertIn("watmobilite.com.tr", self.book.query_terms())

    def test_import_is_idempotent_and_keeps_history(self):
        r = cw.import_contacts(self.ops, contacts(), actor="ahmet@gamechangers.com.tr", catalog=self.catalog)
        self.assertEqual((r["status"], r["brands"], r["addresses"], r["domains"]), ("client_contacts_imported", 3, 3, 1))
        self.assertEqual(cw.import_contacts(self.ops, contacts(), actor="ahmet@gamechangers.com.tr",
                                            catalog=self.catalog)["status"], "already_current")
        changed = contacts(source="v2")
        self.assertEqual(cw.import_contacts(self.ops, changed, actor="cansu@gamechangers.com.tr",
                                            catalog=self.catalog)["status"], "client_contacts_imported")
        self.assertEqual(len(self.ops.backend.data[cw.CONTACTS_KEY]["history"]), 1)
        with self.assertRaises(cw.ClientWatchError):
            cw.import_contacts(self.ops, contacts(), actor="x@example.com", catalog=self.catalog)
        status = cw.contacts_status(self.ops, self.catalog)
        self.assertEqual(status["brands"]["wat_mobilite"], {"addresses": 1, "domains": ["watmobilite.com.tr"]})


class Text(unittest.TestCase):
    def test_latest_part_and_sensitive_words(self):
        mail = "Merhaba,\nGörseli onaylıyoruz.\n\n12 Eki 2026 Pzt, 10:00 tarihinde Özge şunu yazdı:\n> fiyat teklifi ekte"
        self.assertEqual(cw.latest_part(mail), "Merhaba,\nGörseli onaylıyoruz.")
        self.assertEqual(cw.sensitive_hits("Re: görsel", mail), [])  # quoted 'fiyat' does not count
        self.assertEqual(cw.sensitive_hits("Ekim faturası", "Merhaba"), ["price"])
        self.assertEqual(cw.sensitive_hits("", "Bu işten hiç memnun değiliz, sözleşmeye bakacağız."),
                         ["complaint", "contract"])


class Flow(Base):
    def test_disabled_or_stopped_reads_nothing(self):
        self.mails = [source(1)]
        self.assertEqual(self.watch(config(enabled=False)).run()["status"], "no_new_work")
        self.ops.stop("ahmet-dur")
        self.assertEqual(self.watch().run()["status"], "stopped")
        self.assertEqual((self.reads, self.client.calls, self.todos.calls), (0, [], []))

    def test_thread_note_then_comment_and_never_twice(self):
        self.mails = [source(1), source(2, text="Bir de logo büyüsün lütfen.")]
        self.client.outputs = [message(draft_out()), message(draft_out())]
        r = self.watch().run()
        self.assertEqual((r["notes"], r["held"], r["business_writes"]), (2, 0, 2), r)
        todo = self.todos.calls[0]
        self.assertEqual(todo["target"], {"account_id": 5898865, "project_id": 49184053, "list_id": 10393654435})
        self.assertEqual(todo["title"], "[WAT Mobilite] Müşteri maili · Ekim post revizesi")
        self.assertIn("GÖNDERİLMEDİ", todo["description"])
        self.assertIn("Ayşe Yılmaz (Pazarlama Müdürü) <ayse@watmobilite.com.tr>", todo["description"])
        self.assertIn("Talebinizi aldık", todo["description"])
        comment = self.comments.calls[0]
        self.assertEqual((comment["target"]["task_id"], comment["internal_only"], comment["mention_ids"]),
                         (501, True, []))
        self.assertEqual((self.row("m1")["state"], self.row("m2")["state"], self.row("m2")["todo_id"]),
                         ("noted", "noted", 501))
        sent = self.client.calls[0]
        self.assertEqual(sent["model"], "claude-opus-5-5")
        self.assertNotIn("ayse@watmobilite.com.tr", sent["messages"][0]["content"])  # names only, no addresses
        again = self.watch().run()
        self.assertEqual((again["notes"], len(self.client.calls), len(self.todos.calls), len(self.comments.calls)),
                         (0, 2, 1, 1))

    def test_price_contract_complaint_get_the_fixed_sentence(self):
        self.mails = [source(1, thread="t9", subject="Kasım teklifi", text="Kasım için fiyat teklifinizi bekliyoruz.")]
        self.client.outputs = [message(draft_out(category="request"))]  # keyword backstop wins
        self.watch().run()
        body = self.todos.calls[0]["description"]
        self.assertIn(cw.FIXED_SENSITIVE_REPLY, body)
        self.assertIn("konu Ahmet ve Cansu'da", body)
        self.assertNotIn("Talebinizi aldık", body)
        self.assertEqual(self.row("m1")["sensitive"], ["price"])

    def test_unknown_write_is_never_retried_and_blocks_the_thread(self):
        self.mails = [source(1)]
        self.client.outputs = [message(draft_out()), message(draft_out())]
        self.todos.fail = RuntimeError("socket closed")
        self.assertEqual(self.watch().run()["held"], 1)
        self.assertEqual(self.row("m1")["state"], "unknown")
        self.todos.fail = None
        self.mails = [source(1), source(2)]
        r = self.watch().run()
        self.assertEqual((len(self.todos.calls), self.row("m2")["state"], r["notes"]), (1, "held", 0))
        self.assertEqual(len(self.client.calls), 1)  # no model call for a mail whose thread note is uncertain

    def test_no_budget_claims_nothing_and_model_failure_still_records(self):
        self.mails = [source(1)]
        watch = self.watch()
        watch.draft.budget_ok = lambda: False
        self.assertEqual(watch.run()["notes"], 0)
        self.assertIsNone(self.row("m1"))
        self.client.outputs = [RuntimeError("boom")]
        self.watch().run()
        self.assertEqual(self.row("m1")["state"], "noted")
        self.assertIn("taslak alınamadı", self.todos.calls[0]["description"])

    def test_unknown_sender_and_draft_limit(self):
        self.mails = [source(1, sender="biri@baska.com")] + [source(n, thread=f"t{n}") for n in range(2, 7)]
        self.client.outputs = [message(draft_out()) for _ in range(5)]
        r = self.watch(config(max_drafts_per_run=2)).run()
        self.assertEqual((r["notes"], len(self.client.calls)), (2, 2))
        self.assertIsNone(self.row("m1"))
        r = self.watch(config(max_drafts_per_run=2)).run()
        self.assertEqual(r["notes"], 2)

    def test_run_time_budget_stops_new_drafts(self):
        self.mails = [source(n, thread=f"t{n}") for n in range(1, 4)]
        self.client.outputs = [message(draft_out()) for _ in range(3)]
        ticks = iter([0, 0, 300, 300, 300])
        watch = self.watch()
        watch.monotonic = lambda: next(ticks)
        r = watch.run()
        self.assertEqual((r["notes"], r["skipped"], len(self.client.calls)), (1, 2, 1))

    def test_test_contact_title(self):
        self.mails = [source(1, sender="ahmet.test@gmail.com", subject="Deneme")]
        self.client.outputs = [message(draft_out())]
        self.watch().run()
        self.assertEqual(self.todos.calls[0]["title"], "[TEST · Gamechangers] Müşteri maili · Deneme")


class Summary(Base):
    def test_one_evening_summary_to_ahmet_only(self):
        self.mails = [source(1), source(2, thread="t2", subject="Fatura", text="Ekim faturası nerede?")]
        self.client.outputs = [message(draft_out()), message(draft_out())]
        r = self.watch().run()
        self.assertIsNone(r["summary"])  # before 18:25
        self.now, self.mails = TUE_1830, []
        r = self.watch().run()
        self.assertEqual(r["summary"], {"status": "summary_sent", "mails": 2})
        mail = self.summaries.calls[0]
        self.assertEqual((mail["to"], mail["cc"]), (["ahmet@gamechangers.com.tr"], []))
        self.assertEqual(mail["subject"], "Hamle AI: 13 Ekim Salı · Müşteri mailleri, böyle yanıtlardım")
        self.assertIn("Bugün 2 müşteri maili geldi", mail["body"])
        self.assertIn("HASSAS", mail["body"])
        self.assertIn(cw.FIXED_SENSITIVE_REPLY, mail["body"])
        self.assertIn("https://3.basecamp.com/5898865/buckets/49184053/todos/501", mail["html"])
        self.assertIn("font-family:Tahoma", mail["html"])
        self.assertEqual(self.watch().run()["summary"], {"status": "already_recorded"})
        self.assertEqual(len(self.summaries.calls), 1)

    def test_failed_summary_send_is_not_resent(self):
        self.now = TUE_1830
        self.summaries.fail = RuntimeError("timeout")
        self.assertEqual(self.watch().run()["summary"]["reason"], "client_summary_outcome_unknown_no_retry")
        self.summaries.fail = None
        self.assertEqual(self.watch().run()["summary"], {"status": "already_recorded"})
        self.assertEqual(len(self.summaries.calls), 1)

    def test_mail_read_failure_does_not_block_the_summary(self):
        self.now = TUE_1830
        watch = self.watch()
        watch.reader = lambda: (_ for _ in ()).throw(RuntimeError("gmail down"))
        r = watch.run()
        self.assertEqual((r["reason"], r["summary"]["status"]), ("mail_read_failed:RuntimeError", "summary_sent"))

    def test_stop_between_draft_and_write_releases_the_claim(self):
        self.mails = [source(1)]
        self.client.outputs = [message(draft_out())]
        watch = self.watch()
        inner = watch.draft
        def draft_then_stop(*a):
            out = inner(*a)
            self.ops.stop("ahmet-dur")
            return out
        draft_then_stop.budget_ok = inner.budget_ok
        watch.draft = draft_then_stop
        self.assertEqual(watch.run()["notes"], 0)
        self.assertEqual((self.row("m1")["state"], self.todos.calls), ("claimed", []))
        self.ops.resume_review_only("offline-test")
        self.assertEqual(self.watch().run()["notes"], 1)
        self.assertEqual(len(self.client.calls), 1)  # cached draft reused, no second model call

    def test_empty_day_still_reports(self):
        self.now = TUE_1830
        self.watch().run()
        self.assertIn("müşteri maili yok", self.summaries.calls[0]["body"])


class Reader(Base):
    def read(self, gmail):
        from test_cloud_providers import Cred, Store
        reader = cw.client_mail_reader(Store(), self.book, config(), clock=lambda: 100,
                                       _service_factory=lambda _: gmail, _credentials_factory=lambda _: Cred(),
                                       _request_factory=lambda: object())
        return reader(), reader

    def test_client_domain_mail_is_read_and_agency_or_strangers_are_not(self):
        from test_cloud_providers import Gmail, message as gmsg
        gmail = Gmail([gmsg("a1", 1000, sender="yeni.kisi@watmobilite.com.tr", subject="Ekim", text="Görseli aldık.")])
        sources, _ = self.read(gmail)
        self.assertEqual([(s["sender"], s["text"]) for s in sources], [("yeni.kisi@watmobilite.com.tr", "Görseli aldık.")])
        query = next(kw for name, kw in gmail.calls if name == "list")["q"]
        self.assertIn("from:watmobilite.com.tr", query)
        self.assertIn("from:mert.kaya@gmail.com", query)
        self.assertIn("newer_than:2d", query)
        for sender in ("ozge@gamechangers.com.tr", "stranger@example.com"):
            sources, reader = self.read(Gmail([gmsg("a1", 1000, sender=sender)]))
            self.assertEqual(sources, [])
            self.assertTrue(reader.skipped)


class AhmetsStyle(Base):
    """Ahmet's own answers to the first four test mails (2026-10-13) shape every client draft."""

    def test_prompt_carries_his_rules_and_examples_signed_as_hamle(self):
        text = cw.PROMPT.read_text(encoding="utf-8")
        for line in ("Dönüşünüze göre hızlıca iş listemize alacağız.",
                     "Genel bir 29 Ekim içeriği hazırlıyoruz, hem fikiriz değil mi?",
                     "Notlarınızı aldık ve beğenmenize çok sevindik, teşekkür ederiz!",
                     "Tam olarak nasıl beklentimiz var, satış mı? Yoksa imaj mı?",
                     "Revize notlarını tek tek geri sayma",
                     "Müşterinin kendi koyduğu kesin tarihi teyit edebilirsin",
                     "Mailde olmayan bir tarihi asla yazma."):
            self.assertIn(line, text)
        self.assertEqual(text.count("Sevgiler,\nHamle\nGamechangers"), 3)  # the three examples
        self.assertNotIn("Sevgiler,\nAhmet", text)

    def test_internal_terms(self):
        for reply, terms in (("Basecamp'te görevini açtık.", ["Basecamp", "görev"]),
                             ("Ekteki image/jpeg dosyası", ["dosya türü"]),
                             ("linkedin_post: hazırlanıyor", ["sistem kodu"]),
                             ("Hamle İşleri listesine aldık", ["Hamle İşleri"]),
                             ("Bir to-do açtık", ["to-do"]),
                             ("29 Ekim postumuz 4:5 olacak, ekibi görevlendirdik.", []),
                             ("instagram.com/wat_mobilite hesabında, deniz_kaya@watmobilite.com.tr", [])):
            self.assertEqual(cw.internal_terms(reply), terms, reply)

    def test_internal_words_are_flagged_for_the_team_and_cap_confidence(self):
        self.mails = [source(1)]
        leaked = "Merhaba Ayşe,\nTalebinizi Basecamp'te görev olarak açtık.\nSevgiler,\nHamle\nGamechangers"
        self.client.outputs = [message(draft_out(reply=leaked))]
        self.watch().run()
        body = self.todos.calls[0]["description"]
        self.assertIn("Dikkat: taslakta müşteriye gitmemesi gereken iç ifade var (Basecamp, görev).", body)
        self.assertIn("Güven: 50/100", body)
        self.assertEqual(self.row("m1")["internal_terms"], ["Basecamp", "görev"])

    def test_clean_draft_keeps_its_confidence_and_fixed_sentence_is_never_flagged(self):
        self.mails = [source(1), source(2, thread="t9", subject="Kasım teklifi", text="Fiyat teklifinizi bekliyoruz.")]
        leaked = "Merhaba,\nBasecamp'e aldık.\nSevgiler,\nHamle\nGamechangers"
        self.client.outputs = [message(draft_out()), message(draft_out(reply=leaked))]
        self.watch().run()
        self.assertIn("Güven: 82/100", self.todos.calls[0]["description"])
        self.assertNotIn("Dikkat:", self.todos.calls[0]["description"])
        self.assertEqual((self.row("m1")["internal_terms"], self.row("m2")["internal_terms"]), ([], []))
        self.assertIn(cw.FIXED_SENSITIVE_REPLY, self.todos.calls[1]["description"])


class Disk(unittest.TestCase):
    def test_shipped_config_is_on_in_silent_draft_mode_and_valid(self):
        cfg = cw.validate_config(cw.load_config())
        self.assertIs(cfg["enabled"], True)  # Ahmet 2026-10-13 07:32Z card "Evet, aç": test mails, drafts only
        self.assertEqual(cfg["mode"], "silent_draft")
        self.assertEqual(cfg["summary_to"], ["ahmet@gamechangers.com.tr"])
        self.assertTrue(cw.PROMPT.is_file())
        self.assertEqual(model_routing.load().route(cw.ROLE).model, "claude-opus-5-5")


if __name__ == "__main__":
    unittest.main()
