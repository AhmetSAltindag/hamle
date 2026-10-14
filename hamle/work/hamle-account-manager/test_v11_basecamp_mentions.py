"""Basecamp mentions (Ahmet 2026-10-14): fakes only, no sockets, Gmail, Basecamp or models."""
import base64
import copy
from contextlib import contextmanager
from datetime import timedelta
from email import policy
from email.parser import BytesParser
import json
import unittest

import model_routing
import v11_basecamp_mentions as bm
import v11_client_watch as cw
from autonomy_policy import scope_digest
from test_anthropic_routing import FakeClient, KEY, StatusError, message
from test_v11_chain_mail import Resp, TOKEN
from test_v11_client_reply import (AHMET, AHMET_BC, AYSE, CANSU, CANSU_BC, OZGE, OZGE_BC, ReplyBase, clear,
                                   reply_config)
from test_v11_client_watch import source
from v11_brand_task import HAMLE_PERSON
from v11_client_job import job_thread_key

WAT, SOCIAL, INTERNAL, NB, NB_LIST = 40694338, 8231779952, 49184053, 40704547, 10398528523
ROOT = "https://3.basecampapi.com/5898865"
TODO = 10402860490
ESMA_BC = 53167312
MENTION = ('<div>Bence iyi fikir, bi story yaptırabiliriz hızlıca? <bc-attachment sgid="BAh7" '
           'content-type="application/vnd.basecamp.mention"><figure><figcaption>Hamle</figcaption></figure>'
           '</bc-attachment> bunu müşteriye mail atarak sorabilir misin lütfen?</div>')
BODY = ("Merhaba Ayşe Hanım,\n\nBugün Dünya Standartlar Günü. WAT için hızlıca bir story hazırlamayı öneriyoruz. "
        "Uygun görürseniz ekibimiz hemen başlasın.\n\nSevgiler,\nHamle\nGamechangers")
SUBJECT = "WAT Mobilite × Gamechangers | Dünya Standartlar Günü önerisi"


def mentions_block(**kw):
    block = {"enabled": True, "since": "2026-10-13T09:00:00+03:00", "max_per_run": 2, "newer_than_days": 2}
    block.update(kw)
    return block


def mention_config(**kw):
    cfg = reply_config()
    cfg["mentions"] = mentions_block(**kw)
    return cfg


def mail_out(**kw):
    out = {"action": "client_mail", "understood": "Ahmet Standartlar Günü story önerisini müşteriye sormamı istedi.",
           "comment": "Ahmet, taslağı hazırladım, aşağıda.",
           "mail": {"to": [AYSE], "subject": SUBJECT, "body": BODY}}
    out.update(kw)
    return out


def answer_out(text="Ahmet, bu to-do'da henüz yüklenmiş bir görsel yok; Özge'de görünüyor.", action="answer"):
    return {"action": action, "understood": "Ahmet işin durumunu sordu.", "comment": text, "mail": None}


class MentionBasecamp:
    """GET-only Basecamp for brand to-dos (plus the internal note to-dos of ClientWatch)."""
    def __init__(self, test):
        self.test, self.todos, self.comments, self.next, self.reads = test, {}, {}, 80000, 0

    def todo(self, todo_id, *, project=WAT, list_id=SOCIAL, visible=False, creator=AHMET_BC, description="",
             title="Hamle Fikir: WAT Mobilite × Dünya Standartlar Günü"):
        self.todos[todo_id] = {"type": "Todo", "id": todo_id, "bucket": {"id": project, "name": "WAT Mobilite"},
                               "parent": {"id": list_id, "title": "Sosyal Medya", "type": "Todolist"},
                               "status": "active", "visible_to_clients": visible, "completed": False,
                               "assignees": [{"id": OZGE_BC, "name": "Özge Yağmur"}], "content": title,
                               "description": description, "creator": {"id": creator, "name": "Ahmet Ş."},
                               "created_at": self.test.now.isoformat(), "due_on": None}

    def add(self, todo_id, author, html, when=None):
        self.next += 1
        project = self.todos[todo_id]["bucket"]["id"] if todo_id in self.todos else INTERNAL
        self.comments.setdefault(todo_id, []).append(
            {"type": "Comment", "id": self.next, "bucket": {"id": project}, "parent": {"id": todo_id},
             "status": "active", "visible_to_clients": False, "creator": {"id": author, "name": "kişi"},
             "content": html, "created_at": (when or self.test.now).isoformat()})
        return self.next

    def __call__(self, url, conditional):
        self.reads += 1
        path = url.split("3.basecampapi.com", 1)[1]
        if path.endswith("/comments.json"):
            todo = int(path.split("/recordings/")[1].split("/")[0])
            return 200, copy.deepcopy(self.comments.get(todo, [])), {}
        todo = int(path.rsplit("/", 1)[1].split(".")[0])
        if todo not in self.todos:  # an internal note to-do written by ClientWatch
            row = {"type": "Todo", "id": todo, "bucket": {"id": INTERNAL}, "parent": {"id": 10393654435},
                   "status": "active", "visible_to_clients": False, "completed": False, "assignees": []}
        else:
            row = copy.deepcopy(self.todos[todo])
        row["comments_count"] = len(self.comments.get(todo, []))
        row["comments_url"] = f"{ROOT}/buckets/{row['bucket']['id']}/recordings/{todo}/comments.json"
        return 200, row, {}


class BcComments:
    """Comment writer: Hamle's comment shows up on the to-do like a real one."""
    def __init__(self, bc):
        self.bc, self.calls, self.fail = bc, [], None

    def write(self, payload):
        self.calls.append(copy.deepcopy(payload))
        if self.fail:
            raise self.fail
        return {"remote_id": str(self.bc.add(payload["target"]["task_id"], HAMLE_PERSON, payload["body"]))}

    def readback(self, remote_id, payload):
        return {"readback_sha256": scope_digest(payload)}


class FakeMail:
    def __init__(self):
        self.sent, self.fail = [], None

    def write(self, payload):
        self.sent.append(copy.deepcopy(payload))
        if self.fail:
            raise self.fail
        return {"remote_id": "a%d" % len(self.sent), "thread_id": "c0ffee%d" % len(self.sent)}

    def readback(self, remote_id, payload):
        return {"readback_sha256": scope_digest(payload), "thread_id": "c0ffee%d" % len(self.sent)}


class MentionBase(ReplyBase):
    def setUp(self):
        super().setUp()
        self.bc = MentionBasecamp(self)
        self.bc.todo(TODO)
        self.mention_comments, self.mail, self.pointers = BcComments(self.bc), FakeMail(), []
        self.mclient = FakeClient()

    def read_pointers(self):
        return copy.deepcopy(self.pointers)

    def watch(self, cfg=None):
        def factory(settings):
            return {"reader": self.read_pointers, "comment_writer": self.mention_comments, "mail_writer": self.mail,
                    "model": bm.mention_source(self.ops, lambda: KEY, self.catalog, routing=model_routing.load(),
                                               client=self.mclient)}
        return cw.ClientWatch(self.ops, config=cfg or mention_config(), book=self.book, reader=self.reader,
                              draft=cw.draft_source(self.ops, lambda: KEY, self.catalog, routing=model_routing.load(),
                                                    client=self.client),
                              task_writer=self.todos, comment_writer=self.comments, mail_writer=self.summaries,
                              catalog=self.catalog, clock=lambda: self.now, reply_writer=self.replies,
                              basecamp_get=self.bc, mentions_factory=factory,
                              brands=cw.brand_index(self.catalog, self.cards))

    def mention(self, html=MENTION, author=AHMET_BC, todo=TODO, project=WAT, comment=True):
        cid = self.bc.add(todo, author, html) if comment else None
        self.pointers.append({"message_id": "n%d" % (len(self.pointers) + 1), "received_at_ms": int(self.now.timestamp() * 1000),
                              "project_id": project, "todo_id": todo, "comment_id": cid, "subject": "(WAT) mention"})
        return cid

    def hamle_said(self, todo=TODO):
        return [c["content"] for c in self.bc.comments.get(todo, []) if c["creator"]["id"] == HAMLE_PERSON]

    def drafted(self):
        self.mention()
        self.mclient.outputs = [message(mail_out())]
        return self.watch().run()


class Notification(unittest.TestCase):
    @staticmethod
    def mail(text=None, sender="Basecamp <notifications@app.basecamp.com>",
             subject="(WAT Mobilite) Ahmet Ş. @mentioned you in Re: Hamle Fikir: WAT Mobilite × Dünya Standartlar Günü",
             html=None):
        parts = []
        if text is not None:
            parts.append({"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")}})
        if html is not None:
            parts.append({"mimeType": "text/html", "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()}})
        return {"id": "1a139c78988f5351", "internalDate": "1791970740000",
                "payload": {"mimeType": "multipart/alternative", "headers": [{"name": "From", "value": sender},
                                                                            {"name": "Subject", "value": subject}],
                            "parts": parts}}

    TEXT = ("Ahmet Ş. @mentioned you in ‘Re: Hamle Fikir’.\n\nHamle bunu müşteriye mail atarak sorabilir misin?\n\n"
            "• Reply to Ahmet on Basecamp:\nhttps://app.basecamp.com/5898865/buckets/40694338/todos/10402860490"
            "#__recording_10403350702\n\n• Unsubscribe:\nhttps://app.basecamp.com/5898865/email/form_submission?x=1")

    def test_live_notification_is_a_pointer_to_the_comment(self):
        p = bm.parse_notification(self.mail(self.TEXT), 5898865)
        self.assertEqual((p["project_id"], p["todo_id"], p["comment_id"], p["received_at_ms"], p["message_id"]),
                         (40694338, 10402860490, 10403350702, 1791970740000, "1a139c78988f5351"))

    def test_mention_in_the_todo_itself_and_html_only(self):
        p = bm.parse_notification(self.mail(html='<a href="https://app.basecamp.com/5898865/buckets/40694338/todos/'
                                                 '10095930436">WM // Yerli Sanayi</a>'), 5898865)
        self.assertEqual((p["todo_id"], p["comment_id"]), (10095930436, None))

    def test_anything_else_is_not_a_pointer(self):
        for msg in (self.mail(self.TEXT, sender="notifications@app.basecamp.com.evil.com"),
                    self.mail(self.TEXT, sender="ahmet@gamechangers.com.tr"),
                    self.mail(self.TEXT, subject="(WAT) Ahmet assigned you a to-do"),
                    self.mail("https://app.basecamp.com/5898865/buckets/40694338/messages/123"),
                    self.mail("https://app.basecamp.com/999/buckets/40694338/todos/1#__recording_2"),
                    dict(self.mail(self.TEXT), internalDate="x"), None):
            self.assertIsNone(bm.parse_notification(msg, 5898865))


class Config(MentionBase):
    def test_shipped_config_listens_after_13_00_on_brand_projects_not_the_internal_one(self):
        cfg = cw.validate_config(cw.load_config())
        from v11_client_reply import validate_reply
        s = bm.validate_mentions(cfg["mentions"], self.catalog, validate_reply(cfg["reply"], self.catalog))
        self.assertEqual((s["max_per_run"], s["newer_than_days"]), (2, 2))
        self.assertEqual(s["since_ms"], int(cw.datetime.fromisoformat("2026-10-14T13:00:00+03:00").timestamp() * 1000))
        self.assertNotIn(INTERNAL, s["allowed"])
        self.assertIn(WAT, s["allowed"])
        self.assertEqual(s["allowed"][NB], frozenset({NB_LIST}))
        self.assertEqual((s["projects"][WAT], s["projects"][NB]), ("wat_mobilite", "new_biz"))
        self.assertNotIn(44320349, s["allowed"])  # Fenerium has no verified route

    def test_bad_blocks(self):
        from v11_client_reply import validate_reply
        reply = validate_reply(mention_config()["reply"], self.catalog)
        self.assertIsNone(bm.validate_mentions(None, self.catalog, reply))
        self.assertIsNone(bm.validate_mentions(mentions_block(enabled=False), self.catalog, reply))
        for bad in (mentions_block(since="2026-10-14T13:00:00"), mentions_block(max_per_run=9),
                    dict(mentions_block(), extra=1), mentions_block(enabled="true")):
            with self.assertRaises(bm.MentionError):
                bm.validate_mentions(bad, self.catalog, reply)
        with self.assertRaises(bm.MentionError):
            bm.validate_mentions(mentions_block(), self.catalog, None)

    def test_packaged(self):
        import package_chain_source as pkg
        self.assertIn("v11_basecamp_mentions.py", pkg.CHAIN_FILES)
        self.assertIn("basecamp_mention_prompt.txt", pkg.CHAIN_FILES)


class Draft(MentionBase):
    def test_ask_for_a_client_mail_drafts_it_on_the_todo_and_sends_nothing(self):
        r = self.drafted()
        self.assertEqual((r["mentions"]["drafts"], r["sent"], self.mail.sent), (1, 0, []), r)
        (body,) = self.hamle_said()
        self.assertTrue(body.startswith("Ahmet, taslağı hazırladım"), body)
        self.assertIn("MÜŞTERİYE MAİL TASLAĞI · henüz gönderilmedi", body)
        self.assertIn("Kime: Ayşe Yılmaz <" + AYSE + ">", body)
        self.assertIn("CC: Özge Yağmur, Cansu Akiş Kabil, Ahmet Şükrü Altındağ", body)
        self.assertIn("Konu: " + SUBJECT, body)
        self.assertIn(BODY, body)
        self.assertIn('Ahmet ya da Cansu bu to-do\'ya "Hamle gönder" yazsın', body)
        target = self.mention_comments.calls[0]["target"]
        self.assertEqual(target, {"account_id": 5898865, "project_id": WAT, "list_id": SOCIAL, "task_id": TODO})
        request = json.loads(self.mclient.calls[0]["messages"][0]["content"])
        self.assertEqual(request["mention"]["author"], "Ahmet Şükrü Altındağ")
        self.assertIn("bunu müşteriye mail atarak sorabilir misin", request["mention"]["text"])
        self.assertEqual((request["brand"]["default_recipient"], request["brand"]["client_mail_possible"],
                          request["todo"]["title"], request["open_draft"]),
                         (AYSE, True, "Hamle Fikir: WAT Mobilite × Dünya Standartlar Günü", None))
        again = self.watch().run()  # the same notification is read again: nothing new
        self.assertEqual((len(self.mclient.calls), len(self.hamle_said()), again["mentions"]["read"]), (1, 1, 1))

    def test_hamle_gonder_from_ahmet_sends_a_new_mail_and_the_thread_becomes_hamles(self):
        self.drafted()
        self.now += timedelta(minutes=10)
        self.mention("<div>Hamle gönder</div>")  # it names Hamle too: the draft check reads it, not the model
        r = self.watch().run()
        (sent,) = self.mail.sent
        self.assertEqual((sent["to"], sent["cc"], sent["subject"], sent["body"], sent["brand_key"]),
                         ([AYSE], [OZGE, CANSU, AHMET], SUBJECT, BODY, "wat_mobilite"))
        self.assertEqual((r["sent"], r["mentions"]["sent"], len(self.mclient.calls)), (1, 1, 1), r)
        self.assertTrue(self.hamle_said()[-1].startswith("✅ Gönderdim (Ahmet onayıyla). Kime: Ayşe Yılmaz"),
                        self.hamle_said())
        self.assertIsNotNone(self.ops.backend.data.get(job_thread_key("c0ffee1")))
        self.assertEqual(self.ops.backend.data[cw.day_key(self.now.date())]["sent_count"], 1)
        self.watch().run()
        self.assertEqual(len(self.mail.sent), 1)  # never twice

    def test_corrected_text_under_the_command_is_what_goes(self):
        self.drafted()
        self.now += timedelta(minutes=5)
        fixed = "Merhaba Ayşe Hanım,\n\nKısa bir story önerimiz var.\n\nSevgiler,\nHamle\nGamechangers"
        self.bc.add(TODO, CANSU_BC, "<div>Hamle gönder</div><div>" + fixed.replace("\n", "<br>") + "</div>")
        self.watch().run()
        self.assertEqual(self.mail.sent[0]["body"], fixed)
        self.assertIn("Cansu onayıyla, düzeltilmiş metinle", self.hamle_said()[-1])

    def test_hamle_gonderme_drops_it_and_others_cannot_approve(self):
        self.drafted()
        self.now += timedelta(minutes=5)
        self.bc.add(TODO, OZGE_BC, "<div>Hamle gönder</div>")
        self.watch().run()
        self.assertEqual(self.mail.sent, [])
        self.assertIn("Ahmet ya da Cansu onaylayınca gönderiyorum", self.hamle_said()[-1])
        self.bc.add(TODO, AHMET_BC, "<div>Hamle gönderme</div>")
        self.watch().run()
        self.bc.add(TODO, AHMET_BC, "<div>Hamle gönder</div>")
        self.watch().run()
        self.assertEqual(self.mail.sent, [])
        self.assertEqual(self.hamle_said()[-1], "Tamam Ahmet, bu maili göndermiyorum.")

    def test_daily_send_limit_is_shared_with_client_replies(self):
        self.drafted()
        day = cw.day_key(self.now.astimezone(cw.ISTANBUL).date())
        self.ops.backend.data[day] = {"kind": "client_day", "sent_count": 10}
        self.bc.add(TODO, AHMET_BC, "<div>Hamle gönder</div>")
        self.watch().run()
        self.assertEqual(self.mail.sent, [])
        self.assertIn("bugünkü gönderim sınırı doldu", self.hamle_said()[-1])

    def test_uncertain_send_is_never_retried(self):
        self.drafted()
        self.mail.fail = RuntimeError("socket closed")
        self.bc.add(TODO, AHMET_BC, "<div>Hamle gönder</div>")
        self.watch().run()
        self.mail.fail = None
        self.bc.add(TODO, AHMET_BC, "<div>Hamle gönder</div>")
        self.watch().run()
        self.assertEqual(len(self.mail.sent), 1)
        self.assertIn("teyit edemedim", self.hamle_said()[-1])

    def test_a_new_draft_on_the_same_todo_replaces_the_old_one(self):
        self.drafted()
        self.now += timedelta(minutes=3)
        self.mention('<div><bc-attachment content-type="application/vnd.basecamp.mention"><figcaption>Hamle</figcaption>'
                     '</bc-attachment> konuya "story" kelimesini de ekle</div>')
        self.mclient.outputs = [message(mail_out(mail={"to": [AYSE], "subject": SUBJECT + " (story)", "body": BODY}))]
        self.watch().run()
        request = json.loads(self.mclient.calls[1]["messages"][0]["content"])
        self.assertEqual(request["open_draft"]["subject"], SUBJECT)
        self.bc.add(TODO, AHMET_BC, "<div>Hamle gönder</div>")
        self.watch().run()
        self.assertEqual([m["subject"] for m in self.mail.sent], [SUBJECT + " (story)"])

    def test_model_picks_an_unknown_address_falls_back_to_the_first_contact(self):
        self.mention()
        self.mclient.outputs = [message(mail_out(mail={"to": ["birisi@baska.com"], "subject": SUBJECT, "body": BODY}))]
        self.watch().run()
        self.assertIn("Kime: Ayşe Yılmaz <" + AYSE + ">", self.hamle_said()[0])


class Answer(MentionBase):
    def test_question_gets_an_answer_comment_and_no_draft(self):
        self.mention("<div>Hamle bu iş ne durumda?</div>")
        self.mclient.outputs = [message(answer_out())]
        r = self.watch().run()
        self.assertEqual(r["mentions"]["answered"], 1, r)
        self.assertEqual(self.hamle_said(), ["Ahmet, bu to-do'da henüz yüklenmiş bir görsel yok; Özge'de görünüyor."])
        self.assertIsNone(self.ops.backend.data.get(bm.DRAFTS_KEY))

    def test_mention_in_the_todo_description(self):
        self.bc.todo(777, creator=ESMA_BC, description="<div>Hamle merhaba, bu post için metin önerir misin?</div>",
                     title="WM // Yerli Sanayi Linkedin Görseli")
        self.mention(todo=777, comment=False)
        self.mclient.outputs = [message(answer_out("Esma, önerim şu: ..."))]
        self.watch().run()
        request = json.loads(self.mclient.calls[0]["messages"][0]["content"])
        self.assertEqual((request["mention"]["author"], request["todo"]["title"]), ("Esma", "WM // Yerli Sanayi Linkedin Görseli"))
        self.assertEqual(self.hamle_said(777), ["Esma, önerim şu: ..."])

    def test_client_mail_for_a_brand_hamle_cannot_write_to_becomes_an_honest_no(self):
        alt = 40704523
        self.bc.todo(555, project=alt, list_id=8238555593, title="Altınmarka kapkek")
        self.mention(todo=555, project=alt)
        self.mclient.outputs = [message(mail_out())]
        self.watch().run()
        request = json.loads(self.mclient.calls[0]["messages"][0]["content"])
        self.assertIs(request["brand"]["client_mail_possible"], False)
        (said,) = self.hamle_said(555)
        self.assertIn("benim yazabileceğim bir adres henüz yok", said)
        self.assertIsNone(self.ops.backend.data.get(bm.DRAFTS_KEY))

    def test_model_failure_says_so_once(self):
        self.mention()
        self.mclient.outputs = [StatusError(500, "api_error")]
        self.watch().run()
        self.assertIn("şu an işleyemedim", self.hamle_said()[0])
        self.watch().run()
        self.assertEqual(len(self.hamle_said()), 1)

    def test_per_run_limit_leaves_the_rest_for_the_next_tick(self):
        for _ in range(3):
            self.mention("<div>Hamle bu iş ne durumda?</div>")
        self.mclient.outputs = [message(answer_out()) for _ in range(3)]
        r = self.watch().run()
        self.assertEqual((r["mentions"]["answered"], r["mentions"]["waiting"]), (2, 1), r)
        self.watch().run()
        self.assertEqual(len(self.hamle_said()), 3)


class Skip(MentionBase):
    def run_once(self):
        self.mclient.outputs = [message(answer_out())]
        return self.watch().run()["mentions"]

    def test_not_agency_hamle_itself_or_not_named(self):
        self.mention("<div>Hamle bu iş ne durumda?</div>", author=999)
        self.mention("<div>Hamle not</div>", author=HAMLE_PERSON)
        self.mention("<div>Özge bu iş ne durumda?</div>")
        r = self.run_once()
        self.assertEqual((r["skipped"], len(self.mclient.calls), self.mention_comments.calls), (3, 0, []), r)

    def test_internal_project_and_unknown_projects_are_not_read(self):
        self.pointers = [{"message_id": "x1", "received_at_ms": 1, "project_id": INTERNAL, "todo_id": 5, "comment_id": 6},
                         {"message_id": "x2", "received_at_ms": 1, "project_id": 44320349, "todo_id": 5, "comment_id": 7}]
        r = self.run_once()
        self.assertEqual((r["skipped"], self.bc.reads, len(self.mclient.calls)), (2, 0, 0), r)

    def test_client_visible_todo_and_old_comments(self):
        self.bc.todo(901, visible=True)
        self.mention(todo=901)
        self.bc.todo(902)
        self.bc.add(902, AHMET_BC, MENTION, when=self.now - timedelta(days=1))
        self.pointers.append({"message_id": "old", "received_at_ms": 1, "project_id": WAT, "todo_id": 902,
                              "comment_id": self.bc.next})
        r = self.run_once()
        self.assertEqual((r["skipped"], len(self.mclient.calls)), (2, 0), r)
        reasons = sorted(v.get("reason") for k, v in self.ops.backend.data.items() if k.startswith("bc-mention-") and
                         isinstance(v, dict) and v.get("kind") == "bc_mention")
        self.assertEqual(reasons, ["before_since", "todo_visible_to_clients"])

    def test_global_stop_writes_nothing(self):
        self.mention()
        self.ops.stop("ahmet-dur")
        r = self.watch().run()
        self.assertEqual((r["status"], self.hamle_said(), len(self.mclient.calls)), ("stopped", [], 0))

    def test_stop_between_model_and_write_releases_the_claim(self):
        self.mention()
        self.mclient.outputs = [message(answer_out())]
        watch = self.watch()
        inner = watch.mentions.model
        def answer_then_stop(*a):
            out = inner(*a)
            self.ops.stop("ahmet-dur")
            return out
        answer_then_stop.budget_ok = inner.budget_ok
        watch.mentions.model = answer_then_stop
        self.assertEqual(watch.run()["mentions"]["waiting"], 1)
        self.assertEqual(self.mention_comments.calls, [])
        self.ops.resume_review_only("offline-test")
        self.assertEqual(self.watch().run()["mentions"]["answered"], 1)
        self.assertEqual((len(self.mclient.calls), len(self.hamle_said())), (1, 1))  # cached answer, no second call


class Sync(MentionBase):
    def test_client_answer_in_hamles_thread_is_answered_and_written_on_the_origin_todo(self):
        self.drafted()
        self.bc.add(TODO, AHMET_BC, "<div>Hamle gönder</div>")
        self.watch().run()
        self.now += timedelta(minutes=30)
        self.mails = [source(5, thread="c0ffee1", subject="Re: " + SUBJECT,
                             text="Merhaba,\nÇok güzel fikir, story'yi hazırlayalım.\nTeşekkürler")]
        self.client.outputs = [message(clear())]
        r = self.watch().run()
        self.assertEqual(r["sent"], 1, r)  # Hamle's own thread: answered by the usual rules
        said = self.hamle_said()[-1]
        self.assertTrue(said.startswith("📩 Ayşe Yılmaz cevap verdi"), said)
        self.assertIn("Çok güzel fikir, story'yi hazırlayalım.", said)
        self.assertIn("Hamle aynı zincirde cevap verdi:", said)
        self.watch().run()
        self.assertEqual(sum(c.startswith("📩") for c in self.hamle_said()), 1)

    def test_adopt_makes_a_hand_sent_thread_hamles(self):
        out = bm.adopt_command(self.ops, self.catalog, thread_id="1a139ccc07e2cff5", project_id=WAT, todo_id=TODO,
                               list_id=SOCIAL, brand_key="wat_mobilite", actor=AHMET)
        self.assertEqual(out["status"], "mention_thread_adopted")
        self.assertIsNotNone(self.ops.backend.data.get(job_thread_key("1a139ccc07e2cff5")))
        with self.assertRaises(bm.MentionError):
            bm.adopt_command(self.ops, self.catalog, thread_id="1a139ccc07e2cff5", project_id=WAT, todo_id=TODO,
                             list_id=SOCIAL, brand_key="rabam", actor=AHMET)


class Writer(unittest.TestCase):
    class Gmail:
        def __init__(self):
            self.sent, self.calls = {}, []

        def close(self):
            pass

        def request(self, method, url, **kwargs):
            self.calls.append((method, url))
            if method == "POST":
                body = json.loads(kwargs["data"])
                assert "threadId" not in body  # a new thread
                self.sent["f001"] = base64.urlsafe_b64decode(body["raw"])
                return Resp({"id": "f001", "threadId": "beef01", "labelIds": ["SENT"]})
            return Resp({"id": "f001", "threadId": "beef01", "labelIds": ["SENT"],
                         "raw": base64.urlsafe_b64encode(self.sent["f001"]).decode().rstrip("=")})

    def setUp(self):
        from test_v11_client_reply import direct_contacts
        from v11_chain_live import load_catalog
        from v11_client_reply import validate_reply
        from v11_daily_digest import load_brand_cards
        catalog = load_catalog()
        book = cw.validate_contacts(direct_contacts(), catalog, load_brand_cards())
        self.gmail = self.Gmail()

        @contextmanager
        def token():
            yield TOKEN
        self.writer = bm.NewClientMailWriter(book, validate_reply(reply_config()["reply"], catalog), token_context=token,
                                             session_factory=lambda: self.gmail)

    def payload(self, **kw):
        out = {"from": cw.HAMLE, "brand_key": "wat_mobilite", "to": [AYSE], "cc": [OZGE, CANSU], "subject": SUBJECT,
               "body": BODY}
        out.update(kw)
        return out

    def test_new_thread_with_exact_headers_and_readback(self):
        p = self.payload()
        receipt = self.writer.write(copy.deepcopy(p))
        self.assertEqual(receipt, {"remote_id": "f001", "thread_id": "beef01"})
        msg = BytesParser(policy=policy.default).parsebytes(self.gmail.sent["f001"])
        self.assertEqual((str(msg["To"]), str(msg["Cc"]), str(msg["Subject"]), msg["In-Reply-To"]),
                         (AYSE, OZGE + ", " + CANSU, SUBJECT, None))
        self.assertIn("Tahoma", msg.get_body(preferencelist=("html",)).get_content())
        back = self.writer.readback("f001", copy.deepcopy(p))
        self.assertEqual(back, {"readback_sha256": scope_digest(p), "thread_id": "beef01"})
        with self.assertRaises(bm.MentionError):
            self.writer.readback("f001", self.payload(body=BODY + "x"))

    def test_only_this_brands_clients_in_to_and_agency_in_cc(self):
        for bad in (self.payload(to=[OZGE]), self.payload(to=["biri@gmail.com"]), self.payload(cc=[AYSE]),
                    self.payload(to=[]), self.payload(cc=["x@gamechangers.com.tr"]), self.payload(to=[cw.HAMLE]),
                    self.payload(subject="a\nb"), dict(self.payload(), extra=1)):
            with self.assertRaises(bm.MentionError):
                self.writer.write(bad)
        self.assertEqual(self.gmail.calls, [])


if __name__ == "__main__":
    unittest.main()
