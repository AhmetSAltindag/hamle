"""Client reply mode (Ahmet 2026-10-13 card "Yaz"): fakes only, no sockets, Gmail, Basecamp or models."""
import copy
from contextlib import contextmanager
from datetime import timedelta
import unittest

import model_routing
import v11_chain_mail as cmail
import v11_client_reply as cr
import v11_client_watch as cw
from autonomy_policy import scope_digest
from test_anthropic_routing import KEY, message
from test_v11_chain_mail import Rest, TOKEN
from test_v11_client_watch import Base, Writer, config, contacts, draft_out, source

AYSE, OZGE, AHMET, CANSU = ("ayse@watmobilite.com.tr", "ozge@gamechangers.com.tr", "ahmet@gamechangers.com.tr",
                            "cansu@gamechangers.com.tr")
CANSU_BC, AHMET_BC, OZGE_BC = 48793256, 47470759, 52096503
CLEAR = ("Merhaba Ayşe,\n\nNotlarınızı aldık, teşekkür ederiz! Yorumlar çok net, hızlıca iş listemize alıyoruz; "
         "işimiz tamamlandığında size ileteceğiz.\n\nSevgiler,\nHamle\nGamechangers")
ASKING = ("Merhaba Ayşe,\n\nMail'inizi aldık. Görselde dış ses olsun mu? Dönüşünüze göre hızlıca iş listemize "
          "alacağız.\n\nSevgiler,\nHamle\nGamechangers")


def reply_block(**kw):
    block = {"brands": ["wat_mobilite"], "auto_min_confidence": 90,
             "auto_categories": ["request", "feedback_or_approval"], "since": "2026-10-13T00:00:00+03:00",
             "max_sends_per_day": 10, "approvers": [AHMET, CANSU], "owners": {"wat_mobilite": CANSU},
             "sensitive_cc": [AHMET, CANSU], "allow_questions": False, "new_threads_only": False}
    block.update(kw)
    return block


def reply_config(**kw):
    return config(mode="reply", reply=reply_block(**kw))


def clear(**kw):
    out = draft_out(reply=CLEAR)
    out.update({"confidence": 92, **kw})
    return out


class FakeReplyWriter:
    def __init__(self):
        self.preflights, self.sent, self.block, self.fail, self.next = [], [], None, None, 0

    def preflight(self, kind, payload):
        assert kind == "client_reply"
        self.preflights.append(copy.deepcopy(payload))
        if callable(self.block):
            self.block()
        elif self.block:
            raise cmail.ChainMailError(self.block)
        return True

    def write(self, payload):
        self.sent.append(copy.deepcopy(payload))
        if self.fail:
            raise self.fail
        self.next += 1
        return {"remote_id": "s%d" % self.next}

    def readback(self, remote_id, payload):
        return {"readback_sha256": scope_digest(payload)}


class FakeBasecamp:
    """GET-only Basecamp: note todos in the internal list and their comments."""
    ROOT = "https://3.basecampapi.com/5898865"

    def __init__(self):
        self.comments, self.reads = {}, 0

    def add(self, todo_id, author, html, when):
        rows = self.comments.setdefault(todo_id, [])
        cid = 7000 + sum(len(v) for v in self.comments.values())
        rows.append({"type": "Comment", "id": cid, "bucket": {"id": 49184053}, "parent": {"id": todo_id},
                     "status": "active", "visible_to_clients": False, "creator": {"id": author}, "content": html,
                     "created_at": when.isoformat()})
        return cid

    def __call__(self, url, conditional):
        self.reads += 1
        path = url.split("3.basecampapi.com", 1)[1]
        if path.endswith("/comments.json"):
            todo = int(path.split("/recordings/")[1].split("/")[0])
            return 200, copy.deepcopy(self.comments.get(todo, [])), {}
        todo = int(path.rsplit("/", 1)[1].split(".")[0])
        return 200, {"type": "Todo", "id": todo, "bucket": {"id": 49184053}, "parent": {"id": 10393654435},
                     "status": "active", "visible_to_clients": False, "completed": False, "assignees": [],
                     "comments_count": len(self.comments.get(todo, [])),
                     "comments_url": f"{self.ROOT}/buckets/49184053/recordings/{todo}/comments.json"}, {}


def direct_contacts(direct=True):
    doc = contacts()
    doc["brands"]["wat_mobilite"]["contacts"][0]["direct"] = direct  # Markalar: "doğrudan yazabilir mi"
    return doc


class ReplyBase(Base):
    def setUp(self):
        super().setUp()
        self.replies, self.bc = FakeReplyWriter(), FakeBasecamp()
        self.book = cw.validate_contacts(direct_contacts(), self.catalog, self.cards)

    def watch(self, cfg=None):
        return cw.ClientWatch(self.ops, config=cfg or reply_config(), book=self.book, reader=self.reader,
                              draft=cw.draft_source(self.ops, lambda: KEY, self.catalog, routing=model_routing.load(),
                                                    client=self.client),
                              task_writer=self.todos, comment_writer=self.comments, mail_writer=self.summaries,
                              catalog=self.catalog, clock=lambda: self.now, reply_writer=self.replies,
                              basecamp_get=self.bc)

    def later(self, minutes):
        self.now = self.now + timedelta(minutes=minutes)
        return self.now


class AutoSend(ReplyBase):
    def test_clear_mail_is_answered_in_the_same_thread_once(self):
        self.mails = [source(1)]
        self.client.outputs = [message(clear())]
        r = self.watch().run()
        self.assertEqual((r["sent"], r["notes"], r["awaiting_approval"]), (1, 1, 0), r)
        sent = self.replies.sent[0]
        self.assertEqual((sent["to"], sent["cc"], sent["thread_id"], sent["in_reply_to_message_id"]),
                         ([AYSE, OZGE], [], "t1", "m1"))
        self.assertEqual((sent["subject"], sent["body"], sent["from"]), ("Re: Ekim post revizesi", CLEAR, cw.HAMLE))
        todo = self.todos.calls[0]
        self.assertTrue(todo["description"].startswith("GÖNDERİLDİ"), todo["description"])
        self.assertIn("Gönderilen yanıt:", todo["description"])
        self.assertEqual(todo["assignee_ids"], sorted([CANSU_BC, OZGE_BC]))  # the team opens the job
        row = self.row("m1")
        self.assertEqual((row["reply_state"], row["state"], row["reply_remote_id"]), ("sent", "noted", "s1"))
        again = self.watch().run()
        self.assertEqual((again["sent"], len(self.replies.sent), len(self.client.calls)), (0, 1, 1))

    def test_hamles_own_job_thread_gets_answered_and_the_teams_threads_do_not(self):
        # Ahmet 13 Oct: in a thread Hamle answered and opened the job for, the client's reply is answered too.
        from v11_client_job import job_thread_key
        data = self.ops.backend.data
        for thread in ("t1", "t3"):
            data[job_thread_key(thread)] = {"key": job_thread_key(thread), "job_keys": ["client-job-x"]}
        data[cw.reply_key("t1")] = {"key": cw.reply_key("t1"), "count": 1}
        data[cw.reply_key("t2")] = {"key": cw.reply_key("t2"), "count": 1}
        data[cw.reply_key("t3")] = {"key": cw.reply_key("t3"), "count": 5}
        self.mails = [source(1, subject="Re: Ekim post revizesi"), source(2, thread="t2", subject="Re: Kasım planı"),
                      source(3, thread="t3", subject="Re: Bornova")]
        self.client.outputs = [message(clear()), message(clear()), message(clear())]
        r = self.watch(reply_config(allow_questions=True, new_threads_only=True)).run()
        self.assertEqual((r["sent"], r["awaiting_approval"]), (1, 2), r)
        self.assertEqual([s["thread_id"] for s in self.replies.sent], ["t1"])
        notes = {c["title"]: c["description"] for c in self.todos.calls}
        team = next(d for t, d in notes.items() if "Kasım planı" in t)
        self.assertIn("devam eden bir yazışma", team)
        self.assertIn("bu zincire daha önce cevap verdim", team)
        self.assertIn("bu zincire kendiliğinden 5 kez cevap verdim", next(d for t, d in notes.items() if "Bornova" in t))

    def test_question_in_the_reply_waits_for_the_brand_owner(self):
        self.mails = [source(1)]
        self.client.outputs = [message(clear(reply=ASKING))]
        r = self.watch().run()
        self.assertEqual((r["sent"], r["awaiting_approval"], self.replies.sent), (0, 1, []))
        todo = self.todos.calls[0]
        self.assertEqual(todo["assignee_ids"], [CANSU_BC])
        self.assertTrue(todo["description"].startswith("ONAY BEKLİYOR · Hamle bu yanıtı müşteriye göndermedi. "
                                                       "Neden: cevapta soru var."), todo["description"])
        self.assertIn('"Hamle gönder"', todo["description"])
        self.assertEqual(self.ops.backend.data[cw.APPROVALS_KEY]["keys"], [cw.mail_key("m1")])

    def test_test_senders_waiting_mail_goes_to_ahmet_not_the_team(self):
        self.mails = [source(1, sender="ahmet.test@gmail.com")]
        self.client.outputs = [message(clear(reply=ASKING))]
        self.watch(reply_config(brands=["gamechangers"], owners={"gamechangers": CANSU})).run()
        self.assertEqual(self.todos.calls[0]["assignee_ids"], [AHMET_BC])
        self.assertTrue(self.todos.calls[0]["title"].startswith("[TEST · "))

    def test_every_check_has_a_reason(self):
        settings = cr.validate_reply(reply_block(), self.catalog)
        sender = self.book.match(AYSE)
        bad = dict(clear(category="meeting", confidence=70, missing_info=["tarih"], reply="Selam?"),
                   model_status="ok", internal_terms=["Basecamp"], sensitive=[])
        reasons = cr.check(bad, source(1), sender, settings, thread_replied=True, sent_today=10, unknown=["x@y.com"])
        self.assertEqual(len(reasons), 9, reasons)
        good = dict(clear(), model_status="ok", internal_terms=[], sensitive=[])
        self.assertEqual(cr.check(good, source(1), sender, settings, thread_replied=False, sent_today=0, unknown=[]), [])

    def test_sensitive_mail_gets_the_fixed_sentence_with_ahmet_and_cansu(self):
        self.mails = [source(1, thread="t9", subject="Kasım teklifi", text="Kasım için fiyat teklifinizi bekliyoruz.")]
        self.client.outputs = [message(clear())]
        self.watch().run()
        sent = self.replies.sent[0]
        self.assertEqual(sent["body"], "Merhaba,\n\n" + cw.FIXED_SENSITIVE_REPLY + "\n\nSevgiler,\nHamle\nGamechangers")
        self.assertEqual((sent["to"], sent["cc"]), ([AYSE, OZGE], [AHMET, CANSU]))

    def test_unknown_recipient_is_left_to_the_team(self):
        m = source(1)
        m["cc"] = ["hamle@gamechangers.com.tr", "partner@otherfirm.com"]
        self.mails = [m]
        self.client.outputs = [message(clear())]
        self.watch().run()
        self.assertEqual(self.replies.sent, [])
        todo = self.todos.calls[0]
        self.assertTrue(todo["description"].startswith("GÖNDERİLMEDİ · tanımadığım alıcı var: partner@otherfirm.com"))
        self.assertEqual((todo["assignee_ids"], self.row("m1")["reply_state"]), ([CANSU_BC], "manual"))

    def test_newer_mail_in_the_thread_means_no_send(self):
        self.mails = [source(1)]
        self.client.outputs = [message(clear())]
        self.replies.block = "newer_incoming_mail_in_thread"
        self.watch().run()
        self.assertEqual(self.replies.sent, [])
        self.assertTrue(self.todos.calls[0]["description"].startswith("GÖNDERİLMEDİ · Zincirde daha yeni bir mail var"))
        self.assertEqual(self.row("m1")["reply_state"], "stale")

    def test_uncertain_send_is_never_retried_and_the_thread_then_needs_approval(self):
        self.mails = [source(1)]
        self.client.outputs = [message(clear()), message(clear())]
        self.replies.fail = TimeoutError("synthetic")
        self.watch().run()
        self.assertEqual((len(self.replies.sent), self.row("m1")["reply_state"]), (1, "send_unknown"))
        self.assertTrue(self.todos.calls[0]["description"].startswith("GÖNDERİM BELİRSİZ"))
        self.replies.fail = None
        self.mails = [source(1), source(2, text="Bir de logo büyüsün lütfen.")]
        self.watch().run()
        self.assertEqual((len(self.replies.sent), self.row("m2")["reply_state"]), (1, "awaiting_approval"))
        self.assertIn("bu zincire daha önce cevap verdim", self.comments.calls[0]["body"])

    def test_older_mails_and_other_brands_stay_silent_drafts(self):
        self.mails = [source(1), source(2, thread="t2", sender="mert.kaya@gmail.com")]
        self.client.outputs = [message(clear()), message(clear())]
        self.watch(reply_config(since={"wat_mobilite": "2026-10-14T00:00:00+00:00"})).run()  # the brand's own start
        self.assertEqual(self.replies.sent, [])
        self.assertTrue(all(c["description"].startswith("SESSİZ DENEME") for c in self.todos.calls))
        self.assertIsNone(self.row("m1").get("reply_state"))

    def test_daily_limit(self):
        self.mails = [source(1), source(2, thread="t2")]
        self.client.outputs = [message(clear()), message(clear())]
        self.watch(reply_config(max_sends_per_day=1)).run()
        self.assertEqual(len(self.replies.sent), 1)
        self.assertIn("günlük gönderim sınırı doldu", self.todos.calls[1]["description"])

    def test_stop_right_before_the_send_sends_nothing(self):
        self.mails = [source(1)]
        self.client.outputs = [message(clear())]
        self.replies.block = lambda: self.ops.stop("ahmet-dur")
        r = self.watch().run()
        self.assertEqual((self.replies.sent, self.todos.calls, r["notes"]), ([], [], 0))
        self.assertEqual(self.row("m1")["state"], "claimed")


    def test_stop_after_the_send_never_sends_twice(self):
        self.mails = [source(1)]
        self.client.outputs = [message(clear())]
        real_write = self.replies.write

        def write_then_stop(payload):
            out = real_write(payload)
            self.ops.stop("ahmet-dur")
            return out
        self.replies.write = write_then_stop
        self.watch().run()
        self.assertEqual((len(self.replies.sent), self.todos.calls, self.row("m1")["reply_state"]), (1, [], "sent"))
        self.ops.resume_review_only("offline-test")
        self.replies.write = real_write
        self.watch().run()
        self.assertEqual(len(self.replies.sent), 1)
        self.assertTrue(self.todos.calls[0]["description"].startswith("GÖNDERİLDİ"))
        self.assertEqual(self.row("m1")["reply_state"], "sent")


class Approval(ReplyBase):
    def waiting(self, reply=ASKING):
        self.mails = [source(1)]
        self.client.outputs = [message(clear(reply=reply))]
        self.watch().run()
        self.assertEqual(self.row("m1")["reply_state"], "awaiting_approval")
        return int(self.row("m1")["todo_id"])

    def test_hamle_gonder_from_an_approver_sends_the_draft_once(self):
        todo = self.waiting()
        self.bc.add(todo, CANSU_BC, "<div>Hamle gönder</div>", self.later(10))
        self.later(5)
        r = self.watch().run()
        self.assertEqual((r["approvals"]["sent"], len(self.replies.sent)), (1, 1), r)
        self.assertEqual(self.replies.sent[0]["body"], ASKING)
        self.assertEqual(self.comments.calls[-1]["body"], "Gönderildi (Cansu Akiş Kabil onayıyla).")
        row = self.row("m1")
        self.assertEqual((row["reply_state"], row["approved_by"], row["edited"]), ("sent", "Cansu Akiş Kabil", False))
        self.assertEqual(self.watch().run()["approvals"]["checked"], 0)
        self.assertEqual(len(self.replies.sent), 1)

    def test_corrected_text_under_the_command_is_what_goes_out(self):
        todo = self.waiting()
        self.bc.add(todo, AHMET_BC, "<div>Hamle gönder<br>Merhaba Ayşe,<br><br>Mail'inizi aldık, dış sesli "
                                    "hazırlıyoruz.<br><br>Sevgiler,<br>Hamle<br>Gamechangers</div>", self.later(10))
        self.watch().run()
        self.assertEqual(self.replies.sent[0]["body"], "Merhaba Ayşe,\n\nMail'inizi aldık, dış sesli hazırlıyoruz."
                                                       "\n\nSevgiler,\nHamle\nGamechangers")
        self.assertTrue(self.row("m1")["edited"])
        self.assertIn("düzeltilmiş metinle", self.comments.calls[-1]["body"])

    def test_outsiders_and_old_comments_do_not_count_and_gonderme_drops_it(self):
        todo = self.waiting()
        self.bc.add(todo, OZGE_BC, "Hamle gönder", self.later(5))           # not an approver
        self.bc.add(todo, CANSU_BC, "Hamle gönder", self.now - timedelta(hours=2))  # before the note
        self.watch().run()
        self.assertEqual(self.replies.sent, [])
        self.bc.add(todo, AHMET_BC, "Hamle gönderme, ben yazacağım.", self.later(5))
        r = self.watch().run()
        self.assertEqual((r["approvals"]["rejected"], self.replies.sent, self.row("m1")["reply_state"]),
                         (1, [], "rejected"))
        self.assertEqual(self.ops.backend.data[cw.APPROVALS_KEY]["keys"], [])

    def test_approval_after_the_client_wrote_again_is_not_sent(self):
        todo = self.waiting()
        self.bc.add(todo, CANSU_BC, "Hamle gönder", self.later(10))
        self.replies.block = "newer_incoming_mail_in_thread"
        self.mails = []
        self.watch().run()
        self.assertEqual((self.replies.sent, self.row("m1")["reply_state"]), ([], "approval_stale"))
        self.assertIn("daha yeni bir mail var", self.comments.calls[-1]["body"])

    def test_waiting_drafts_expire_after_three_days(self):
        self.waiting()
        self.mails = []
        self.later(3 * 24 * 60 + 1)
        self.assertEqual(self.watch().run()["approvals"]["expired"], 1)
        self.assertEqual(self.row("m1")["reply_state"], "approval_expired")

    def test_summary_says_what_happened(self):
        self.mails = [source(1), source(2, thread="t2")]
        self.client.outputs = [message(clear()), message(clear(reply=ASKING))]
        self.watch().run()
        self.now = self.now.replace(hour=18, minute=30)
        self.mails = []
        self.watch().run()
        body = self.summaries.calls[0]["body"]
        self.assertIn("Müşteri mailleri günlük özeti", body)
        self.assertIn("Hamle 1 tanesine yanıt gönderdi", body)
        self.assertIn("Durum: Müşteriye gönderildi", body)
        self.assertIn("Durum: Onay bekliyor: cevapta soru var", body)


class Direct(ReplyBase):
    """Markalar.xlsx 'Hamle bu kişiye doğrudan yazabilir mi?' = Hayır: every reply waits for an approver."""
    def test_person_marked_no_waits_for_approval_then_goes_with_hamle_gonder(self):
        self.book = cw.validate_contacts(direct_contacts(False), self.catalog, self.cards)
        self.mails = [source(1)]
        self.client.outputs = [message(clear())]
        r = self.watch().run()
        self.assertEqual((r["sent"], r["awaiting_approval"], self.replies.sent), (0, 1, []), r)
        todo = self.todos.calls[0]
        self.assertEqual(todo["assignee_ids"], [CANSU_BC])  # WAT's owner
        self.assertIn("bu kişilere onaysız yazmıyorum (Markalar: Hayır): Ayşe Yılmaz", todo["description"])
        self.bc.add(501, CANSU_BC, "<div>Hamle gönder</div>", self.later(5))
        r = self.watch().run()
        self.assertEqual((r["approvals"]["sent"], len(self.replies.sent)), (1, 1), r)
        self.assertEqual(self.replies.sent[0]["body"], CLEAR)

    def test_company_domain_match_is_never_direct(self):
        self.mails = [source(1, sender="selin@watmobilite.com.tr")]
        self.client.outputs = [message(clear())]
        r = self.watch().run()
        self.assertEqual((r["sent"], r["awaiting_approval"]), (0, 1), r)
        self.assertIn("Markalar: Hayır): selin@watmobilite.com.tr", self.todos.calls[0]["description"])

    def test_a_cc_person_marked_no_also_holds_the_reply(self):
        doc = direct_contacts()
        doc["brands"]["wat_mobilite"]["contacts"].append({"email": "berin@watmobilite.com.tr", "name": "Berin Avcı"})
        self.book = cw.validate_contacts(doc, self.catalog, self.cards)
        mail = source(1)
        mail["cc"] = ["hamle@gamechangers.com.tr", "berin@watmobilite.com.tr"]
        self.mails = [mail]
        self.client.outputs = [message(clear())]
        r = self.watch().run()
        self.assertEqual((r["sent"], r["awaiting_approval"]), (0, 1), r)
        self.assertIn("Berin Avcı", self.todos.calls[0]["description"])


class WatGo(ReplyBase):
    """Ahmet 2026-10-13 15:48Z: WAT's new mails are answered by Hamle first, questions included."""
    def test_reply_with_production_questions_goes_out_when_questions_are_allowed(self):
        self.mails = [source(1)]
        self.client.outputs = [message(clear(reply=ASKING, missing_info=["dış ses"]))]
        r = self.watch(reply_config(allow_questions=True, new_threads_only=True)).run()
        self.assertEqual((r["sent"], r["awaiting_approval"]), (1, 0), r)
        self.assertEqual(self.replies.sent[0]["body"], ASKING)
        todo = self.todos.calls[0]
        self.assertEqual(todo["assignee_ids"], sorted([CANSU_BC, OZGE_BC]))  # WAT's team opens the job
        self.assertIn("Ekip: işi markanın Basecamp projesinde açıp", todo["description"])

    def test_reply_in_an_ongoing_thread_waits_for_the_team(self):
        self.mails = [source(1, subject="Re: Ekim post revizesi")]
        self.client.outputs = [message(clear())]
        r = self.watch(reply_config(allow_questions=True, new_threads_only=True)).run()
        self.assertEqual((r["sent"], r["awaiting_approval"]), (0, 1), r)
        self.assertIn("devam eden bir yazışma", self.todos.calls[0]["description"])

    def test_low_confidence_and_other_still_wait(self):
        self.mails = [source(1), source(2, thread="t2")]
        self.client.outputs = [message(clear(confidence=85)), message(clear(category="other"))]
        r = self.watch(reply_config(allow_questions=True, auto_categories=list(cr.AUTO_CATEGORIES))).run()
        self.assertEqual((r["sent"], r["awaiting_approval"]), (0, 2), r)

    def test_confidence_is_the_sum_of_ahmets_five_criteria(self):
        # Ahmet 13 Oct: the Sarayköy-type reply is ~98, never 82; the rubric sum decides, not a self-estimate.
        full = {"understanding": 30, "reply_type": 25, "no_promises": 20, "questions": 15, "tone": 10}
        short = dict(full, no_promises=5)
        self.mails = [source(1), source(2, thread="t2")]
        self.client.outputs = [message(clear(reply=ASKING, confidence=85, rubric=full)),
                               message(clear(reply=ASKING, confidence=95, rubric=short))]
        r = self.watch(reply_config(allow_questions=True)).run()
        self.assertEqual((r["sent"], r["awaiting_approval"]), (1, 1), r)
        self.assertEqual((self.row("m1")["confidence"], self.row("m2")["confidence"]), (100, 85))
        self.assertEqual(self.row("m1")["rubric"], full)
        self.assertIn("Güven: 100/100 (anlama 30/30, yanıt türü 25/25, söz vermeme 20/20, sorular 15/15, ton 10/10)",
                      self.todos.calls[0]["description"])
        self.assertIn("güvenim 85/100", self.todos.calls[1]["description"])

    def test_brand_yes_covers_its_people_and_domain_unless_a_person_says_no(self):
        doc = contacts()
        doc["brands"]["wat_mobilite"]["direct"] = True
        doc["brands"]["wat_mobilite"]["contacts"].append({"email": "berin@watmobilite.com.tr", "direct": False})
        book = cw.validate_contacts(doc, self.catalog, self.cards)
        self.assertEqual([book.match(a)["direct"] for a in ("ayse@watmobilite.com.tr", "yeni@watmobilite.com.tr",
                                                             "berin@watmobilite.com.tr", "mert.kaya@gmail.com")],
                         [True, True, False, False])

    def test_brand_note_reaches_the_model(self):
        doc = direct_contacts()
        doc["brands"]["wat_mobilite"]["note"] = "Hitap: siz. Koç Grubu kurallarına uy."
        self.book = cw.validate_contacts(doc, self.catalog, self.cards)
        self.mails = [source(1)]
        self.client.outputs = [message(clear())]
        self.watch().run()
        self.assertIn("Koç Grubu kurallarına uy", str(self.client.calls[0]))
        doc["brands"]["wat_mobilite"]["note"] = "x" * 700
        with self.assertRaises(cw.ClientWatchError):
            cw.validate_contacts(doc, self.catalog, self.cards)


class Commands(unittest.TestCase):
    def test_commands(self):
        for html, want in (("Hamle gönder", ("send", None)), ("<div>hamle, GÖNDER</div>", ("send", None)),
                           ("Hamle gönderme", ("reject", None)), ("Hamle göndermesin lütfen", None),
                           ("Özge gönder bunu", None), ("Hamle gönder: kısa", ("send", None)),
                           ("<div>Hamle gönder<br>Merhaba,<br>Bu metin yeterince uzun bir düzeltme.</div>",
                            ("send", "Merhaba,\nBu metin yeterince uzun bir düzeltme."))):
            self.assertEqual(cr.command(html), want, html)


class Config(Base):
    def test_reply_mode_needs_a_valid_block_with_verified_people(self):
        with self.assertRaises(cw.ClientWatchError):
            cw.validate_config(config(mode="reply"))
        for bad in (reply_block(approvers=["berkay@gamechangers.com.tr"]), reply_block(owners={}),
                    reply_block(auto_min_confidence=70), reply_block(since="yarın"),
                    reply_block(auto_categories=["price"]), dict(reply_block(), extra=1)):
            with self.assertRaises(cr.ClientReplyError):
                cr.validate_reply(bad, self.catalog)
        ok = cr.validate_reply(reply_block(), self.catalog)
        self.assertEqual((ok["owner_ids"], sorted(ok["approver_ids"])), ({"wat_mobilite": CANSU_BC},
                                                                          sorted([AHMET_BC, CANSU_BC])))
        # a start time per brand: every listed brand needs one
        for bad in (reply_block(since={}), reply_block(since={"wat_mobilite": "yarın"}),
                    reply_block(since={"wat_mobilite": "2026-10-13T00:00:00+03:00", "rabam": "2026-10-14T00:00:00+03:00"})):
            with self.assertRaises(cr.ClientReplyError):
                cr.validate_reply(bad, self.catalog)
        one = cr.validate_reply(reply_block(since={"wat_mobilite": "2026-10-13T00:00:00+03:00"}), self.catalog)
        self.assertEqual(one["since_ms"], ok["since_ms"])

    def test_shipped_config_replies_for_wat_and_rabam_and_is_valid(self):
        cfg = cw.validate_config(cw.load_config())
        settings = cr.validate_reply(cfg["reply"], self.catalog)
        self.assertEqual((settings["brands"], settings["auto_min_confidence"], settings["owner_ids"]),
                         ({"wat_mobilite", "rabam"}, 90, {"wat_mobilite": CANSU_BC, "rabam": AHMET_BC}))
        # Ahmet 14 Oct: Rabam opened later than WAT; its older mails never get an answer
        self.assertLess(settings["since_ms"]["wat_mobilite"], settings["since_ms"]["rabam"])
        self.assertEqual(cfg["summary_to"], [AHMET])
        self.assertEqual(cfg["shadow_from"], [AHMET, CANSU])
        self.assertEqual((settings["allow_questions"], settings["new_threads_only"], settings["auto_categories"]),
                         (True, True, set(cr.AUTO_CATEGORIES)))
        cw.ClientWatch(self.ops, config=cfg, book=self.book, reader=list, draft=None, task_writer=None,
                       comment_writer=None, mail_writer=None, catalog=self.catalog, clock=None,
                       reply_writer=object(), basecamp_get=object(), shadow_reader=list)  # people verified


class GmailWriter(unittest.TestCase):
    def setUp(self):
        self.rest = Rest()
        self.rest.messages["a1"]["headers"].update({"From": "Ayşe <ayse@watmobilite.com.tr>",
                                                    "To": "hamle@gamechangers.com.tr, ozge@gamechangers.com.tr",
                                                    "Cc": "", "Subject": "Ekim post"})
        from test_v11_chain import live_catalog
        from v11_daily_digest import load_brand_cards
        from test_v11_client_watch import contacts
        catalog = live_catalog()
        self.book = cw.validate_contacts(contacts(), catalog, load_brand_cards())

        @contextmanager
        def token():
            yield TOKEN
        self.writer = cr.client_reply_writer(self.book, cr.validate_reply(reply_block(), catalog),
                                             token_context=token, session_factory=lambda: self.rest)

    def payload(self, **kw):
        p = {"from": cw.HAMLE, "to": [AYSE, OZGE], "cc": [], "thread_id": "ab", "in_reply_to_message_id": "a1",
             "subject": "Re: Ekim post", "body": CLEAR}
        p.update(kw)
        return p

    def test_same_thread_reply_all_to_the_client_reads_back(self):
        self.assertTrue(self.writer.preflight("client_reply", self.payload()))
        receipt = self.writer.write(self.payload())
        raw = self.rest.sent[receipt["remote_id"]]["raw"].decode()
        self.assertIn("To: ayse@watmobilite.com.tr, ozge@gamechangers.com.tr", raw)
        self.assertIn("In-Reply-To: <a1@mail.example>", raw)
        self.assertEqual(self.writer.readback(receipt["remote_id"], self.payload()),
                         {"readback_sha256": scope_digest(self.payload())})

    def test_only_ahmet_and_cansu_may_be_added_and_strangers_never(self):
        self.writer.write(self.payload(cc=[AHMET, CANSU]))
        for bad, code in ((self.payload(cc=["ipek@gamechangers.com.tr"]), "reply_recipients_changed"),
                          (self.payload(to=[AYSE]), "reply_recipients_changed")):
            with self.assertRaises((cmail.ChainMailError, cr.ClientReplyError)) as got:
                self.writer.write(bad)
            self.assertEqual(got.exception.code, code)
        with self.assertRaises(cr.ClientReplyError):
            self.writer.write(self.payload(cc=["someone@example.com"]))

    def test_same_people_in_another_order_is_not_a_changed_recipient_list(self):
        # 13 Oct 18:24Z: Ahmet wrote To "esma, cansu"; the plan listed them sorted and the send was refused.
        esma = "esma@gamechangers.com.tr"
        self.rest.messages["a1"]["headers"].update({"To": f"hamle@gamechangers.com.tr, {OZGE}, {esma}",
                                                    "Cc": f"{CANSU}, {AHMET}"})
        receipt = self.writer.write(self.payload(to=sorted([AYSE, OZGE, esma]), cc=sorted([CANSU, AHMET])))
        raw = self.rest.sent[receipt["remote_id"]]["raw"].decode()
        self.assertIn("Cc: " + ", ".join(sorted([CANSU, AHMET])), raw)
        for bad in (self.payload(to=sorted([AYSE, OZGE]), cc=sorted([CANSU, AHMET])),
                    self.payload(to=sorted([AYSE, OZGE, esma]), cc=[CANSU])):
            with self.assertRaises((cmail.ChainMailError, cr.ClientReplyError)) as got:
                self.writer.write(bad)
            self.assertEqual(got.exception.code, "reply_recipients_changed")

    def test_newer_incoming_mail_blocks(self):
        self.rest.messages["a2"] = dict(self.rest.messages["a1"], internal="2000")
        with self.assertRaises(cmail.ChainMailError) as got:
            self.writer.preflight("client_reply", self.payload())
        self.assertEqual(got.exception.code, "newer_incoming_mail_in_thread")


if __name__ == "__main__":
    unittest.main()
