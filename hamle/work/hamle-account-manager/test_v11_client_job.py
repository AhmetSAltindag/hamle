"""Client jobs end to end (Ahmet 2026-10-13 17:05Z): fakes only, no sockets, Gmail, Basecamp or models."""
import base64
import copy
from contextlib import contextmanager
from datetime import timedelta
from email import policy
from email.parser import BytesParser
import hashlib
import io
import json
import unittest

import model_routing
import v11_client_job as cj
import v11_client_watch as cw
from autonomy_policy import scope_digest
from test_anthropic_routing import FakeClient, KEY, message
from test_v11_chain_mail import Resp, Rest, TOKEN
from test_v11_client_reply import (AHMET, AYSE, CANSU, CANSU_BC, OZGE, OZGE_BC, ReplyBase, clear, direct_contacts,
                                   reply_config)
from test_v11_client_watch import Writer, source

BERKAY_BC, AHMET_BC = 47658175, 47470759
WAT, SOCIAL, PROJECT = 40694338, 8231779952, 8234557425
ROOT = "https://3.basecampapi.com/5898865"


def png(width=1080, height=1350, color=(20, 120, 90)):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


def jobs_block(**kw):
    block = {"enabled": True,
             "brands": {"wat_mobilite": {"title_prefix": "WM // ",
                                         "assignees": {"social": "berkay@gamechangers.com.tr", "project": CANSU},
                                         "followers": [OZGE], "approvers": [OZGE, CANSU, AHMET],
                                         "when_done": [AHMET, CANSU, OZGE]}},
             "send_min_score": 90, "approve_min_score": 80, "max_revisions": 2, "max_scorings_per_run": 1,
             "max_mail_mb": 18}
    block.update(kw)
    return block


def job_config(**kw):
    cfg = reply_config(allow_questions=True, auto_categories=["request", "feedback_or_approval", "question"])
    cfg["jobs"] = jobs_block(**kw)
    return cfg


ITEMS = [{"title": "Bornova şarj noktası post ve story", "list": "social",
          "brief": "4:5 post ve 9:16 story. Ana mesaj: \"İzmir'de şarj artık daha yakın\". Yayın 25 Ekim sabahı.",
          "due_on": "2026-10-25"},
         {"title": "Otoyol roll-up", "list": "project", "brief": "6 roll-up, \"Yolda kesintisiz şarj\".",
          "due_on": None}]


def ready(**kw):
    out = clear(job_ready=True, jobs=copy.deepcopy(ITEMS))
    out.update(kw)
    return out


def review(score, **kw):
    out = {"score": score, "summary": "4:5 post, Bornova mesajı ve logo var.",
           "missing": [] if score >= 90 else ["Story yok"],
           "designer_note": "Teşekkürler!" if score >= 90 else "Story eksik; briefte post ve story isteniyor.",
           "client_mail": "Merhaba Ayşe,\n\nBornova postumuz ekte. Görüşlerinizi bekliyoruz.\n\nSevgiler,\nHamle\nGamechangers"}
    out.update(kw)
    return out


class FakeTodos:
    def __init__(self):
        self.calls, self.subs, self.next, self.fail = [], [], 900000, None

    def write(self, payload):
        self.calls.append(copy.deepcopy(payload))
        if self.fail:
            raise self.fail
        self.next += 1
        return {"remote_id": str(self.next)}

    def readback(self, remote_id, payload):
        return {"readback_sha256": scope_digest(payload),
                "app_url": f"https://3.basecamp.com/5898865/buckets/{payload['target']['project_id']}/todos/{remote_id}"}

    def subscribe(self, payload, todo_id, ids):
        self.subs.append((todo_id, list(ids)))
        return sorted(ids)


class BrandBasecamp:
    """GET-only Basecamp for job to-dos in brand lists (and the internal notes' comments)."""
    def __init__(self, test):
        self.test, self.comments, self.completed, self.lists, self.next = test, {}, set(), {}, 50000
        self.meta = {}  # extra to-do fields (a to-do the team opened by hand)

    def add(self, todo_id, author, html="", files=(), name="Berkay Kayaköylü"):
        self.next += 1
        rows = self.comments.setdefault(todo_id, [])
        rows.append({"type": "Comment", "id": self.next, "bucket": {"id": WAT}, "parent": {"id": todo_id},
                     "status": "active", "visible_to_clients": False, "creator": {"id": author, "name": name},
                     "content": html, "created_at": self.test.now.isoformat(),
                     "content_attachments": [dict(f) for f in files]})
        return self.next

    def __call__(self, url, conditional):
        path = url.split("3.basecampapi.com", 1)[1]
        if path.endswith("/comments.json"):
            todo = int(path.split("/recordings/")[1].split("/")[0])
            return 200, copy.deepcopy(self.comments.get(todo, [])), {}
        todo = int(path.rsplit("/", 1)[1].split(".")[0])
        row = {"type": "Todo", "id": todo, "bucket": {"id": WAT}, "parent": {"id": self.lists.get(todo, SOCIAL)},
               "status": "active", "visible_to_clients": False, "completed": todo in self.completed,
               "assignees": [], "comments_count": len(self.comments.get(todo, [])),
               "comments_url": f"{ROOT}/buckets/{WAT}/recordings/{todo}/comments.json"}
        row.update(copy.deepcopy(self.meta.get(todo, {})))
        return 200, row, {}


class FakeDelivery:
    def __init__(self):
        self.sent, self.plan, self.fail, self.anchors = [], None, None, 0

    def anchor(self, thread_id, brand_key):
        self.anchors += 1
        return copy.deepcopy(self.plan or {"anchor": "m1", "to": [AYSE, OZGE], "cc": [], "subject": "Re: Ekim post revizesi",
                                           "unknown": [], "clients": [AYSE], "not_direct": []})

    def write(self, payload, data):
        self.sent.append((copy.deepcopy(payload), list(data)))
        if self.fail:
            raise self.fail
        return {"remote_id": "d%d" % len(self.sent)}

    def readback(self, remote_id, payload):
        return {"readback_sha256": scope_digest(payload)}


class JobBase(ReplyBase):
    def setUp(self):
        super().setUp()
        self.todo_writer, self.job_comments = FakeTodos(), Writer(300)
        self.brand_bc, self.delivery, self.blobs = BrandBasecamp(self), FakeDelivery(), {}
        self.vision = FakeClient()
        self.bc.__class__ = type("Both", (type(self.bc),), {"__call__": self._get})

    def _get(self, url, conditional):  # internal notes and brand jobs share one GET transport
        self.brand_bc.lists = {int(self.todo_writer.next) - len(self.todo_writer.calls) + 1 + i: c["target"]["list_id"]
                               for i, c in enumerate(self.todo_writer.calls)}
        if f"/buckets/{WAT}/" in url or any(f"/todos/{t}.json" in url for t in self.todo_ids() + list(self.brand_bc.meta)):
            return self.brand_bc(url, conditional)
        return type(self.bc).__mro__[1].__call__(self.bc, url, conditional)

    def todo_ids(self):
        return [int(self.todo_writer.next - i) for i in range(len(self.todo_writer.calls))]

    def download(self, url, size):
        data = self.blobs[url]
        assert len(data) == size
        return data

    def watch(self, cfg=None):
        def factory(settings):
            return {"todo_writer": self.todo_writer, "comment_writer": self.job_comments, "download": self.download,
                    "review": cj.review_source(self.ops, lambda: KEY, self.catalog, routing=model_routing.load(),
                                               client=self.vision),
                    "delivery": self.delivery}
        return cw.ClientWatch(self.ops, config=cfg or job_config(), book=self.book, reader=self.reader,
                              draft=cw.draft_source(self.ops, lambda: KEY, self.catalog, routing=model_routing.load(),
                                                    client=self.client),
                              task_writer=self.todos, comment_writer=self.comments, mail_writer=self.summaries,
                              catalog=self.catalog, clock=lambda: self.now, reply_writer=self.replies,
                              basecamp_get=self.bc, jobs_factory=factory)

    def job(self, i=0, message_id="m1"):
        return self.ops.backend.data.get(cj.job_key(message_id, i))

    def upload(self, todo_id, data=None, name="bornova.png", kind="image/png", author=BERKAY_BC):
        data = png() if data is None else data
        url = f"{ROOT}/blobs/{len(self.blobs) + 1}abc/download/{name}"
        self.blobs[url] = data
        return self.brand_bc.add(todo_id, author, "<div>Görsel ekte</div>",
                                 [{"filename": name, "content_type": kind, "byte_size": len(data), "download_url": url}])

    def opened(self, items=None):
        self.mails = [source(1)]
        self.client.outputs = [message(ready(jobs=items if items is not None else copy.deepcopy(ITEMS[:1])))]
        r = self.watch().run()
        return r, self.job()["todo_id"]


class Open(JobBase):
    def test_clear_request_opens_one_todo_per_job_in_the_right_list(self):
        self.mails = [source(1)]
        self.client.outputs = [message(ready())]
        r = self.watch().run()
        self.assertEqual((r["sent"], r["notes"], r["jobs_opened"]), (1, 1, 2), r)
        social, project = self.todo_writer.calls
        self.assertEqual((social["target"]["project_id"], social["target"]["list_id"], social["assignee_ids"],
                          social["due_on"]), (WAT, SOCIAL, [BERKAY_BC], "2026-10-25"))
        self.assertEqual((project["target"]["list_id"], project["assignee_ids"], project["due_on"]),
                         (PROJECT, [CANSU_BC], None))
        self.assertEqual(social["title"], "WM // Bornova şarj noktası post ve story")
        self.assertEqual(social["completion_subscriber_ids"], sorted([AHMET_BC, CANSU_BC, OZGE_BC]))  # "When done"
        d = social["description"]
        self.assertTrue(d.startswith("4:5 post ve 9:16 story."), d)
        for text in ("Teslim: 25 Ekim 2026", "Talep: Ayşe Yılmaz (WAT Mobilite) · ",
                     "Çalışmayı bu to-do'ya dosya olarak yükle; Hamle kontrol edip müşteriye iletir."):
            self.assertIn(text, d)
        for text in ("Merhaba Özge", "Müşterinin maili", "uygun", "tur", "@watmobilite"):
            self.assertNotIn(text, d)
        self.assertLess(len(d.splitlines()), 10, d)
        self.assertIn("Teslim: müşteri tarih vermedi", project["description"])
        self.assertEqual(self.todo_writer.subs, [(900001, [OZGE_BC]), (900002, [OZGE_BC])])
        note = self.todos.calls[0]
        self.assertNotIn("assignee_ids", note)  # the job is open: nobody has to open it by hand
        self.assertTrue(note["description"].startswith(cw.JOBS_OPENED), note["description"])
        self.assertIn("Basecamp'te açılan iş:\n- WM // Bornova şarj noktası post ve story · "
                      "https://3.basecamp.com/5898865/buckets/40694338/todos/900001 (Berkay Kayaköylü)",
                      note["description"])
        self.assertEqual((self.job()["state"], self.job(1)["state"]), ("open", "open"))
        self.assertEqual(self.ops.backend.data[cj.JOBS_KEY]["keys"], [cj.job_key("m1", 0), cj.job_key("m1", 1)])
        self.assertEqual(self.row("m1")["jobs"][0]["url"], "https://3.basecamp.com/5898865/buckets/40694338/todos/900001")
        self.watch().run()
        self.assertEqual(len(self.todo_writer.calls), 2)  # once per mail

    def test_no_todo_when_the_mail_is_not_ready_sensitive_or_unsure(self):
        cases = [ready(job_ready=False), ready(category="price"), ready(confidence=70), ready(jobs=[]),
                 ready(category="meeting")]
        for n, out in enumerate(cases, 1):
            self.mails = [source(n, thread=f"t{n}")]
            self.client.outputs = [message(out)]
            self.watch().run()
        self.assertEqual(self.todo_writer.calls, [])
        self.assertEqual(self.todos.calls[0]["assignee_ids"], sorted([CANSU_BC, OZGE_BC]))  # as before: team opens it

    def test_test_sender_gets_a_test_title_and_unknown_write_is_never_retried(self):
        doc = direct_contacts()
        doc["test_contacts"] = [{"email": "ahmetsukrua@gmail.com", "brand_key": "wat_mobilite", "name": "Ahmet (test)"}]
        self.book = cw.validate_contacts(doc, self.catalog, self.cards)
        self.mails = [source(1, sender="ahmetsukrua@gmail.com")]
        self.client.outputs = [message(ready(jobs=ITEMS[:1]))]
        self.todo_writer.fail = RuntimeError("timeout")
        self.watch().run()
        call = self.todo_writer.calls[0]
        self.assertEqual(call["title"], "WM // TEST – Bornova şarj noktası post ve story")
        self.assertTrue(call["description"].startswith("TEST · müşteriye gitmez"))
        self.assertEqual(self.job()["state"], "unknown")
        note = self.todos.calls[0]["description"]
        self.assertIn("AÇILIP AÇILMADIĞI BELİRSİZ", note)
        self.assertEqual(self.todos.calls[0]["assignee_ids"], [AHMET_BC])  # not opened: the test goes to Ahmet
        self.watch().run()
        self.assertEqual(len(self.todo_writer.calls), 1)

    def test_draft_items_are_cleaned(self):
        items = cj.job_items({"jobs": [
            {"title": "WM // 29 Ekim postu", "list": "social", "brief": "x", "due_on": "2026-13-40"},
            {"title": "Video", "list": "motion", "brief": "x", "due_on": None},
            {"title": "Rapor", "list": "project", "brief": "y", "due_on": "2026-11-02"}]})
        self.assertEqual(items, [{"title": "29 Ekim postu", "list": "social", "brief": "x", "due_on": None},
                                 {"title": "Rapor", "list": "project", "brief": "y", "due_on": "2026-11-02"}])


class Follow(JobBase):
    def test_upload_scored_90_plus_goes_to_the_client_with_the_file(self):
        _, todo = self.opened()
        self.later(20)
        data = png()
        self.upload(todo, data)
        self.vision.outputs = [message(review(94))]
        r = self.watch().run()
        self.assertEqual((r["jobs"]["scored"], r["jobs"]["sent"]), (1, 1), r)
        call = self.vision.calls[0]
        blocks = call["messages"][0]["content"]
        self.assertEqual((blocks[0]["type"], blocks[0]["source"]["media_type"], blocks[-1]["type"]),
                         ("image", "image/jpeg", "text"))
        request = json.loads(blocks[-1]["text"])
        self.assertEqual((request["files"][0]["width"], request["files"][0]["height"], request["files"][0]["ratio"]),
                         (1080, 1350, "4:5"))
        self.assertIn("İzmir'de şarj artık daha yakın", request["brief"])
        self.assertEqual(call["output_config"]["effort"], "medium")
        payload, files = self.delivery.sent[0]
        self.assertEqual((payload["to"], payload["cc"], payload["in_reply_to_message_id"], payload["thread_id"]),
                         ([AYSE, OZGE], [], "m1", "t1"))
        self.assertTrue(payload["body"].startswith("Merhaba Ayşe,\n\nBornova postumuz ekte."))
        self.assertEqual((payload["files"][0]["filename"], payload["files"][0]["sha256"], files),
                         ("bornova.png", hashlib.sha256(data).hexdigest(), [data]))
        comment = self.job_comments.calls[-1]
        self.assertEqual(comment["target"], {"account_id": 5898865, "project_id": WAT, "list_id": SOCIAL, "task_id": todo})
        self.assertTrue(comment["body"].startswith("✅ Puan: 94/100. Briefle uyumlu, müşteriye gönderdim"))
        self.assertEqual(self.job()["state"], "delivered")
        self.later(15)
        self.watch().run()
        self.assertEqual((len(self.vision.calls), len(self.delivery.sent)), (1, 1))

    def test_80_to_89_waits_for_uygun_from_ozge_or_cansu(self):
        _, todo = self.opened()
        self.upload(todo)
        self.vision.outputs = [message(review(85))]
        self.watch().run()
        ask = self.job_comments.calls[-1]["body"]
        self.assertTrue(ask.startswith("🟡 Puan: 85/100."), ask)
        self.assertIn("Özge Yağmur, Cansu Akiş Kabil, Ahmet Şükrü Altındağ: uygunsa bu to-do'ya \"uygun\" yazın", ask)
        self.assertEqual((self.job()["state"], self.delivery.sent), ("awaiting_approval", []))
        self.brand_bc.add(todo, BERKAY_BC, "<div>uygun</div>")  # not an approver
        self.brand_bc.add(todo, OZGE_BC, "<div>Renkler uygun değil, logo büyüsün</div>", name="Özge Yağmur")
        self.watch().run()
        self.assertEqual(self.delivery.sent, [])
        self.brand_bc.add(todo, CANSU_BC, "<div>Uygun 👍</div>", name="Cansu Akiş Kabil")
        r = self.watch().run()
        self.assertEqual((r["jobs"]["sent"], len(self.delivery.sent), self.job()["approved_by"]),
                         (1, 1, "Cansu Akiş Kabil"))
        self.assertTrue(self.job_comments.calls[-1]["body"].startswith("✅ Cansu Akiş Kabil onayıyla müşteriye gönderdim"))
        self.assertEqual(len(self.vision.calls), 1)

    def test_what_a_manager_approved_is_never_docked_again(self):
        # Ahmet 13 Oct 19:24Z: once a manager said send, the next round only checks the client's new notes.
        _, todo = self.opened()
        self.upload(todo)
        self.vision.outputs = [message(review(85))]
        self.watch().run()
        self.brand_bc.add(todo, CANSU_BC, "<div>uygun</div>", name="Cansu Akiş Kabil")
        self.watch().run()
        self.assertEqual((self.job()["state"], self.job()["accepted"], self.job()["accepted_by"]),
                         ("delivered", ["Story yok"], "Cansu Akiş Kabil"))
        self.later(60)
        self.mails = [source(2, subject="Re: Ekim post revizesi", text="Merhaba,\nLogo biraz küçük, büyütelim.\nAyşe")]
        self.client.outputs = [message(ready(summary="Logo büyütülsün."))]
        self.ops.backend.data[cj.job_key("m1", 0)] = dict(self.job(), revisions=2)  # the earlier round used them up
        self.watch().run()
        self.assertEqual((self.job()["state"], self.job()["revisions"]), ("open", 0))
        self.upload(todo, png(color=(1, 2, 3)))
        self.vision.outputs = [message(review(93))]
        self.watch().run()
        request = json.loads(self.vision.calls[1]["messages"][0]["content"][-1]["text"])
        self.assertEqual(request["approved_before"], {"by": "Cansu Akiş Kabil", "accepted": ["Story yok"]})
        self.assertNotIn("approved_before", json.loads(self.vision.calls[0]["messages"][0]["content"][-1]["text"]))
        self.assertEqual(len(self.delivery.sent), 2)
        prompt = cj.REVIEW_PROMPT.read_text(encoding="utf-8")
        self.assertIn("o iş artık 99/100 sayılır", prompt)

    def test_below_80_asks_the_designer_twice_then_the_approvers(self):
        _, todo = self.opened()
        for n, score in enumerate((70, 72), 1):
            self.upload(todo, png(color=(n, 0, 0)))
            self.vision.outputs = [message(review(score))]
            self.watch().run()
            body = self.job_comments.calls[-1]["body"]
            self.assertTrue(body.startswith(f"🔴 Puan: {score}/100 (revize {n}/2)."), body)
            self.assertIn("Story eksik; briefte post ve story isteniyor.", body)
            self.assertIn("Berkay Kayaköylü, revize edip yeni dosyayı bu to-do'ya yükler misin?", body)
            self.assertEqual((self.job()["state"], self.job()["revisions"]), ("open", n))
        self.upload(todo, png(color=(9, 9, 9)))
        self.vision.outputs = [message(review(75))]
        self.watch().run()
        self.assertTrue(self.job_comments.calls[-1]["body"].startswith("🟡 2 revizeden sonra puan hâlâ 75/100."))
        self.assertEqual((self.job()["state"], len(self.vision.calls), self.delivery.sent), ("awaiting_approval", 3, []))
        request = json.loads(self.vision.calls[1]["messages"][0]["content"][-1]["text"])
        self.assertEqual(request["revision_round"], 1)

    def test_hamle_gonderme_leaves_delivery_to_the_team_and_completed_todo_closes(self):
        _, todo = self.opened()
        self.upload(todo)
        self.vision.outputs = [message(review(85))]
        self.watch().run()
        self.brand_bc.add(todo, OZGE_BC, "<div>Hamle gönderme, ben ileteceğim</div>", name="Özge Yağmur")
        self.watch().run()
        self.assertEqual((self.job()["state"], self.delivery.sent), ("team", []))
        self.assertNotIn(cj.job_key("m1", 0), self.ops.backend.data[cj.JOBS_KEY]["keys"])
        self.mails = [source(2, thread="t2")]
        self.client.outputs = [message(ready(jobs=ITEMS[:1]))]
        self.watch().run()
        other = self.job(0, "m2")["todo_id"]
        self.brand_bc.completed.add(other)
        r = self.watch().run()
        self.assertEqual((r["jobs"]["closed"], self.job(0, "m2")["state"]), (1, "closed"))

    def test_unscoreable_file_and_unknown_recipient_go_to_people(self):
        _, todo = self.opened()
        self.upload(todo, b"\x00\x00\x00\x18ftypmp42" + b"0" * 100, name="reels.mp4", kind="video/mp4")
        self.watch().run()
        self.assertEqual(self.vision.calls, [])
        self.assertIn("puanlayamıyorum", self.job_comments.calls[-1]["body"])
        self.assertEqual(self.job()["state"], "awaiting_approval")
        self.delivery.plan = {"anchor": "m1", "to": [AYSE, "yabanci@example.com"], "cc": [],
                              "subject": "Re: Ekim post revizesi", "unknown": ["yabanci@example.com"],
                              "clients": [AYSE], "not_direct": []}
        self.brand_bc.add(todo, OZGE_BC, "<div>uygun</div>", name="Özge Yağmur")
        self.watch().run()
        self.assertEqual((self.delivery.sent, self.job()["state"]), ([], "team"))
        self.assertIn("tanımadığım alıcı var: yabanci@example.com", self.job_comments.calls[-1]["body"])

    def test_client_mail_after_delivery_reopens_the_job_with_the_notes(self):
        _, todo = self.opened()
        self.upload(todo)
        self.vision.outputs = [message(review(95))]
        self.watch().run()
        self.assertEqual(self.job()["state"], "delivered")
        self.later(60)
        self.mails = [source(2, subject="Re: Ekim post revizesi", text="Merhaba,\nLogo biraz küçük, büyütelim.\nAyşe")]
        self.client.outputs = [message(ready(summary="Logo büyütülsün."))]
        self.watch().run()
        self.assertEqual(len(self.todo_writer.calls), 1)  # same thread: no new to-do
        note = self.job_comments.calls[-1]["body"]
        self.assertTrue(note.startswith("Müşteriden bu işle ilgili yeni mail geldi (Ayşe Yılmaz"), note)
        self.assertIn("Logo biraz küçük", note)
        self.assertEqual((self.job()["state"], self.job()["client_notes"][0]["summary"]), ("open", "Logo büyütülsün."))
        self.assertIn("Bu zincirin Basecamp işleri:", self.comments.calls[-1]["body"])
        self.upload(todo, png(color=(1, 2, 3)))
        self.vision.outputs = [message(review(92))]
        self.watch().run()
        request = json.loads(self.vision.calls[1]["messages"][0]["content"][-1]["text"])
        self.assertIn("Logo biraz küçük", request["client_notes"][0]["text"])
        self.assertEqual((len(self.delivery.sent), self.job()["deliveries"]), (2, 2))

    def test_too_big_files_ask_for_a_smaller_one_and_only_one_scoring_per_run(self):
        _, todo = self.opened()
        big = png() + b"0" * (19 * 1024 * 1024)
        self.upload(todo, big, name="dev.png")
        self.watch().run()
        self.assertIn("mail ekine sığmıyor (sınır 18 MB)", self.job_comments.calls[-1]["body"])
        self.assertEqual(self.vision.calls, [])
        self.mails = [source(2, thread="t2")]
        self.client.outputs = [message(ready(jobs=ITEMS[:1]))]
        self.watch().run()
        other = self.job(0, "m2")["todo_id"]
        self.upload(todo)
        self.upload(other)
        self.vision.outputs = [message(review(85)), message(review(85))]
        r = self.watch().run()
        self.assertEqual((r["jobs"]["scored"], len(self.vision.calls)), (1, 1))
        self.watch().run()
        self.assertEqual(len(self.vision.calls), 2)

    def test_basecamp_hiccup_is_retried_not_dropped(self):
        _, todo = self.opened()
        real = self.brand_bc.__class__.__call__
        calls = {"n": 0}

        def flaky(bc, url, conditional):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("429")
            return real(bc, url, conditional)
        self.brand_bc.__class__ = type("Flaky", (BrandBasecamp,), {"__call__": flaky})
        r = self.watch().run()
        self.assertEqual((r["jobs"]["held"], self.job()["state"], self.job()["failures"]), (0, "open", 1))
        self.assertEqual(self.job()["reason"], "basecamp_transport_failed")  # the real reason, not "ReadError"
        self.upload(todo)
        self.vision.outputs = [message(review(85))]
        self.watch().run()
        self.assertEqual((self.job()["state"], self.job()["failures"]), ("awaiting_approval", 0))

    def test_daily_limit_holds_the_delivery_until_it_can_go(self):
        _, todo = self.opened()  # the reply to the client used today's first send
        self.upload(todo)
        self.vision.outputs = [message(review(95))]
        tight = job_config()
        tight["reply"]["max_sends_per_day"] = 1
        self.watch(tight).run()
        self.assertEqual((self.delivery.sent, self.job()["deliver_pending"], self.job()["state"]), ([], "auto", "open"))
        self.watch().run()
        self.assertEqual((len(self.delivery.sent), len(self.vision.calls), self.job()["state"]), (1, 1, "delivered"))
        self.assertIsNone(self.job()["deliver_pending"])

    def test_global_stop_sends_nothing(self):
        _, todo = self.opened()
        self.upload(todo)
        self.vision.outputs = [message(review(95))]
        self.ops.stop_all("dur") if hasattr(self.ops, "stop_all") else self.ops.stop("dur")
        r = self.watch().run()
        self.assertEqual((r["status"], self.delivery.sent, self.vision.calls), ("stopped", [], []))


class Bind(JobBase):
    """A to-do the team opened by hand becomes Hamle's job for a client thread (Ahmet 14 Oct, Rabam story)."""
    THREAD, MESSAGE, TODO = "1a139e3fccf3f390", "1a139e3fccf3f391", 10404000001

    def setUp(self):
        super().setUp()
        self.brand_bc.meta[self.TODO] = {
            "content": "WM // Instagram kampanya story", "due_on": "2026-10-16",
            "description": "<div>Cuma öğle Instagram'a özel indirim kampanyası için 9:16 story. Paket görseli ekte.</div>",
            "assignees": [{"id": OZGE_BC, "name": "Özge Yağmur"}],
            "app_url": f"https://app.basecamp.com/5898865/buckets/{WAT}/todos/{self.TODO}"}

    def bind(self, **kw):
        args = dict(thread_id=self.THREAD, message_id=self.MESSAGE, todo_id=self.TODO, list_name="social",
                    brand_key="wat_mobilite", client_email=AYSE, actor=AHMET)
        args.update(kw)
        return cj.bind_job(self.ops, self.catalog, job_config(), **args)

    def bound(self):
        self.assertEqual(self.bind()["status"], "client_job_bound")
        r = self.watch().run()
        return r, self.ops.backend.data[cj.job_key(self.MESSAGE, 0)]

    def test_first_run_reads_the_todo_and_says_hamle_follows(self):
        r, job = self.bound()
        self.assertEqual(r.get("jobs_bound"), 1, r)
        self.assertEqual((job["state"], job["title_core"], job["assignee_name"], job["due_on"], job["client_name"]),
                         ("open", "Instagram kampanya story", "Özge Yağmur", "2026-10-16", "Ayşe Yılmaz"))
        self.assertIn("indirim kampanyası için 9:16 story", job["brief"])
        said = self.job_comments.calls[-1]
        self.assertEqual(said["target"]["task_id"], self.TODO)
        self.assertTrue(said["body"].startswith("Bu işi ben takip ediyorum: Ayşe Yılmaz ile olan mail zincirine"),
                        said["body"])
        self.watch().run()
        self.assertEqual(len(self.job_comments.calls), 1)  # said once

    def test_upload_on_the_bound_todo_is_scored_and_delivered_in_the_client_thread(self):
        self.bound()
        self.later(20)
        self.upload(self.TODO, author=OZGE_BC)
        self.vision.outputs = [message(review(94))]
        r = self.watch().run()
        self.assertEqual((r["jobs"]["scored"], r["jobs"]["sent"]), (1, 1), r)
        request = json.loads(self.vision.calls[0]["messages"][0]["content"][-1]["text"])
        self.assertEqual((request["job"], request["due_on"]), ("Instagram kampanya story", "2026-10-16"))
        payload, _ = self.delivery.sent[0]
        self.assertEqual(payload["thread_id"], self.THREAD)
        self.assertEqual(self.ops.backend.data[cj.job_key(self.MESSAGE, 0)]["state"], "delivered")

    def test_the_clients_next_mail_in_the_thread_is_hamles_and_lands_on_the_todo(self):
        self.bound()
        self.later(30)
        self.mails = [source(5, thread=self.THREAD, subject="Re: Ynt: Sosyal Medyaya Özel Kampanya Tasarımı",
                             text="Merhaba,\nİndirim kodu RABAM25, kampanya Cuma 12:00'de başlıyor.\nMert")]
        self.client.outputs = [message(clear())]
        r = self.watch().run()
        self.assertEqual(r["sent"], 1, r)  # own thread: answered by the usual rules
        note = self.job_comments.calls[-1]
        self.assertEqual(note["target"]["task_id"], self.TODO)
        self.assertIn("İndirim kodu RABAM25", note["body"])

    def test_a_client_visible_or_finished_todo_is_left_to_the_team(self):
        self.brand_bc.meta[self.TODO]["visible_to_clients"] = True
        r, job = self.bound()
        self.assertEqual((job["state"], self.job_comments.calls), ("team", []))
        self.assertNotIn(cj.job_key(self.MESSAGE, 0), self.ops.backend.data[cj.JOBS_KEY]["keys"])

    def test_bad_input_and_twice_are_refused(self):
        self.bind()
        for kw in ({}, {"thread_id": "t1"}, {"list_name": "motion"}, {"brand_key": "altinmarka"},
                   {"client_email": OZGE}, {"actor": "biri@gmail.com"}):
            with self.assertRaises(cj.ClientJobError):
                self.bind(**kw)


class Parts(unittest.TestCase):
    def test_approval_words(self):
        for text in ("uygun", "Uygun 👍", "<div>Uygundur, gönderebilirsin</div>", "onaylı", "Hamle gönder"):
            self.assertEqual(cj.approval(text), "send", text)
        for text in ("uygun değil", "Renkler uygun ama logo küçük", "bence uygun", "revize lazım", "Hamle gönderme"):
            self.assertNotEqual(cj.approval(text), "send", text)
        self.assertEqual(cj.approval("Hamle gönderme"), "reject")

    def test_upload_files_read_api_rows_or_rich_text_tags(self):
        row = {"content_attachments": [{"filename": "a.png", "content_type": "image/png", "byte_size": 12,
                                        "download_url": f"{ROOT}/blobs/1/download/a.png", "sgid": "x"}]}
        self.assertEqual(cj.upload_files(row)[0]["byte_size"], 12)
        tag = ('<div>Görsel<bc-attachment sgid="BAh" content-type="image/jpeg" filename="Bornova Story.jpg" '
               'filesize="2048" href="https://storage.3.basecamp.com/5898865/blobs/u1/download/Bornova%20Story.jpg" '
               'url="https://preview.3.basecamp.com/5898865/blobs/u1/previews/full"></bc-attachment></div>')
        self.assertEqual(cj.upload_files({"content": tag}), [{
            "filename": "Bornova Story.jpg", "content_type": "image/jpeg", "byte_size": 2048,
            "download_url": "https://storage.3.basecamp.com/5898865/blobs/u1/download/Bornova%20Story.jpg"}])
        self.assertEqual(cj.upload_files({"content": "<div>uygun</div>"}), [])

    def test_client_mail_falls_back_when_the_model_text_is_not_clean(self):
        job = {"client_name": "Ayşe Yılmaz", "title_core": "Bornova postu"}
        self.assertEqual(cj.client_mail(review(95), job), review(95)["client_mail"])
        for bad in ("Selam,\nekte.", "Merhaba,\nBasecamp'e yükledik.\n\nSevgiler,\nHamle\nGamechangers",
                    "Merhaba,\n94/100 puan aldı.\n\nSevgiler,\nHamle\nGamechangers"):
            self.assertEqual(cj.client_mail({"client_mail": bad}, job),
                             "Merhaba Ayşe,\n\nBornova postu için hazırladığımız çalışmayı ekte paylaşıyoruz. "
                             "Görüşlerinizi bekliyoruz.\n\nSevgiler,\nHamle\nGamechangers")

    def test_files_for_the_model(self):
        from PIL import Image
        out = io.BytesIO()
        Image.new("RGBA", (3000, 1500), (0, 0, 0, 0)).save(out, "PNG")
        blocks, info, skipped = cj.model_files([({"filename": "a.png", "content_type": "image/png"}, out.getvalue()),
                                                ({"filename": "b.mp4", "content_type": "video/mp4"}, b"x"),
                                                ({"filename": "c.png", "content_type": "image/png"}, b"bozuk")])
        self.assertEqual((len(blocks), info[0]["width"], info[0]["ratio"], skipped), (1, 3000, "2:1", ["b.mp4", "c.png"]))
        with Image.open(io.BytesIO(base64.b64decode(blocks[0]["source"]["data"]))) as im:
            self.assertEqual(im.size, (1568, 784))
        self.assertEqual(cj.safe_filename("Bornova Şarj Görseli (son).PNG"), "Bornova_Sarj_Gorseli_son_.PNG")

    def test_config_rules(self):
        from test_v11_chain import live_catalog
        import v11_client_reply as cr
        catalog = live_catalog()
        reply = cr.validate_reply(job_config()["reply"], catalog)
        self.assertIsNone(cj.validate_jobs(None, catalog, reply))
        self.assertIsNone(cj.validate_jobs(jobs_block(enabled=False), catalog, reply))
        s = cj.validate_jobs(jobs_block(), catalog, reply)
        self.assertEqual(s["brands"]["wat_mobilite"]["lists"], {"social": SOCIAL, "project": PROJECT})
        self.assertEqual(s["brands"]["wat_mobilite"]["when_done"], sorted([AHMET_BC, CANSU_BC, OZGE_BC]))
        bad = [jobs_block(send_min_score=70), jobs_block(extra=1),
               jobs_block(brands={"rabam": jobs_block()["brands"]["wat_mobilite"]})]
        b = jobs_block()
        b["brands"]["wat_mobilite"]["assignees"]["social"] = "samet@gamechangers.com.tr"
        bad.append(b)
        for when_done in ([OZGE, OZGE], ["someone@example.com"], OZGE):
            b = jobs_block()
            b["brands"]["wat_mobilite"]["when_done"] = when_done
            bad.append(b)
        for block in bad:
            with self.assertRaises(cj.ClientJobError):
                cj.validate_jobs(block, catalog, reply)
        with self.assertRaises(cj.ClientJobError):
            cj.validate_jobs(jobs_block(), catalog, None)

    def test_shipped_config_runs_wat_and_rabam_jobs(self):
        from v11_chain_live import load_catalog
        import v11_client_reply as cr
        cfg = cw.validate_config(cw.load_config())
        catalog = load_catalog()
        s = cj.validate_jobs(cfg["jobs"], catalog, cr.validate_reply(cfg["reply"], catalog))
        wat = s["brands"]["wat_mobilite"]
        self.assertEqual((wat["title_prefix"], wat["assignees"]["social"]["id"], wat["assignees"]["project"]["id"],
                          sorted(wat["followers"]), sorted(wat["approvers"])),
                         ("WM // ", BERKAY_BC, CANSU_BC, [OZGE_BC], sorted([OZGE_BC, CANSU_BC, AHMET_BC])))
        # Ahmet 13 Oct: "When done" on every WAT to-do = Ahmet, Cansu, Özge, Barış (demo week)
        self.assertEqual(wat["when_done"], sorted([AHMET_BC, CANSU_BC, OZGE_BC, 52168172]))
        self.assertEqual((s["send_min_score"], s["approve_min_score"], s["max_revisions"]), (90, 80, 2))
        # Ahmet 14 Oct "Rabam'ı da yayına alalım": the team's own "Rabam // " titles, Ahmet is the rep; his Markalar.xlsx
        # (12:59) opens Rabam work to Özge, Barış (Rabam designs) follows
        rabam = s["brands"]["rabam"]
        self.assertEqual((rabam["title_prefix"], rabam["project_id"], rabam["lists"],
                          rabam["assignees"]["social"]["id"], rabam["assignees"]["project"]["id"],
                          sorted(rabam["followers"]), sorted(rabam["approvers"]), rabam["when_done"]),
                         ("Rabam // ", 48639990, {"social": 10239606739, "project": 10239607231}, OZGE_BC, AHMET_BC,
                          [52168172], sorted([OZGE_BC, CANSU_BC, AHMET_BC]),
                          sorted([AHMET_BC, CANSU_BC, OZGE_BC, 52168172])))


class Readback(unittest.TestCase):
    def test_basecamp_autolinks_are_accepted_only_when_they_point_at_their_own_text(self):
        import business_writers as bw
        text = "Gönderen: Ahmet (test) <ahmetsukrua@gmail.com>\nİş: https://3.basecamp.com/5898865/x"
        saved = ('<div>Gönderen: Ahmet (test) &lt;<a href="mailto:ahmetsukrua@gmail.com">ahmetsukrua@gmail.com</a>&gt;'
                 '<br>İş: <a href="https://3.basecamp.com/5898865/x">https://3.basecamp.com/5898865/x</a></div>')
        self.assertTrue(bw._description_matches(saved, text))
        live = ('<div>Gönderen: Ahmet (test) &lt;<a href="mailto:ahmetsukrua@gmail.com" class="autolinked" '
                'data-behavior="truncate">ahmetsukrua@gmail.com</a>&gt;<br>İş: <a href="https://3.basecamp.com/5898865/x" '
                'class="autolinked" data-behavior="truncate">https://3.basecamp.com/5898865/x</a></div>')
        self.assertTrue(bw._description_matches(live, text))  # Basecamp's own autolink markup (2026-10-13 live)
        for bad in (live.replace('class="autolinked"', 'class="x"', 1),
                    live.replace('data-behavior="truncate"', 'data-behavior="hover"', 1),
                    live.replace('class="autolinked"', 'style="color:red"', 1)):
            self.assertFalse(bw._description_matches(bad, text), bad)
        for bad in (saved.replace("mailto:ahmetsukrua", "mailto:baska"),
                    '<div>a <a href="https://evil.example">https://good.example</a></div>',
                    '<div>a <a href="x" onclick="y">x</a></div>', '<div>a <a href="x"><b>x</b></a></div>',
                    '<div>a <a href="x">x</div>'):
            self.assertFalse(bw._description_matches(bad, text if bad.startswith("<div>Gönderen") else "a x"
                                                     if "good" not in bad else "a https://good.example"), bad)


    def test_job_todo_readback_takes_the_live_app_host_and_nothing_else(self):
        from test_v11_chain import live_catalog
        from v11_brand_task import HAMLE_PERSON
        import v11_client_reply as cr
        catalog = live_catalog()
        settings = cj.validate_jobs(jobs_block(), catalog, cr.validate_reply(job_config()["reply"], catalog))
        payload = {"target": {"account_id": 5898865, "project_id": WAT, "list_id": SOCIAL},
                   "title": "WM // TEST – Sarayköy post", "description": "Müşteri: WAT Mobilite",
                   "assignee_ids": [BERKAY_BC], "due_on": None,
                   "completion_subscriber_ids": sorted([AHMET_BC, CANSU_BC, OZGE_BC])}
        writer = cj.JobTodoWriter.__new__(cj.JobTodoWriter)
        writer.settings = settings

        class Http:
            ORIGIN = ROOT.rsplit("/", 1)[0]

            def __init__(self, url, done=(AHMET_BC, CANSU_BC, OZGE_BC)):
                self.url, self.done = url, done

            def _request(self, method, url, body=None, expected=200):
                return {"completion_subscribers": [{"id": i} for i in self.done], "id": 10401115561, "bucket": {"id": WAT}, "parent": {"id": SOCIAL, "type": "Todolist"},
                        "creator": {"id": HAMLE_PERSON}, "visible_to_clients": False, "content": payload["title"],
                        "completed": False, "assignees": [{"id": BERKAY_BC}], "due_on": None,
                        "description": "<div>Müşteri: WAT Mobilite</div>", "app_url": self.url}

        for host in ("app.basecamp.com", "3.basecamp.com"):
            writer._http = Http(f"https://{host}/5898865/buckets/{WAT}/todos/10401115561")
            self.assertEqual(writer.readback("10401115561", payload)["app_url"], writer._http.url)
        for url in ("https://evil.example/5898865/buckets/1/todos/2", "https://app.basecamp.com/1234567/x",
                    "http://app.basecamp.com/5898865/x", None):
            writer._http = Http(url)
            with self.assertRaises(cj.ClientJobError):
                writer.readback("10401115561", payload)
        writer._http = Http(f"https://app.basecamp.com/5898865/buckets/{WAT}/todos/10401115561", (AHMET_BC, OZGE_BC))
        with self.assertRaises(cj.ClientJobError):  # a "When done" person missing on Basecamp
            writer.readback("10401115561", payload)


class Transport(unittest.TestCase):
    def setUp(self):
        import socket
        from unittest.mock import patch
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)

    def test_downloader_follows_one_storage_redirect_without_the_token(self):
        calls = []

        class R:
            def __init__(self, status, body=b"", location=None):
                self.status_code, self.body, self.headers = status, body, {"Location": location} if location else {}
            def iter_content(self, chunk_size):
                yield self.body
            def close(self):
                pass

        class S:
            def __init__(self, rows):
                self.rows = rows
            def request(self, method, url, **kw):
                calls.append((url, "Authorization" in kw["headers"]))
                return self.rows.pop(0)
            def close(self):
                pass
        url = f"{ROOT}/blobs/x1/download/a.png"
        rows = [R(302, location="https://storage.basecamp.com/signed/a.png"), R(200, b"12345")]
        get = cj.basecamp_downloader(5898865, token_provider=lambda: TOKEN, session_factory=lambda: S(rows))
        self.assertEqual(get(url, 5), b"12345")
        self.assertEqual(calls, [(url, True), ("https://storage.basecamp.com/signed/a.png", False)])
        for bad_rows, size in (([R(302, location="https://evil.example/a.png")], 5), ([R(200, b"123")], 5)):
            get = cj.basecamp_downloader(5898865, token_provider=lambda: TOKEN, session_factory=lambda: S(bad_rows))
            with self.assertRaises(cj.ClientJobError):
                get(url, size)
        with self.assertRaises(cj.ClientJobError):
            get("https://example.com/5898865/blobs/x/download/a.png", 5)


class MailRest(Rest):
    """Rest + format=full readback of sent mail + the multipart upload endpoint."""
    def request(self, method, url, **kwargs):
        if method == "POST" and url == cj.UPLOAD_SEND:
            self.calls.append((method, url))
            body = kwargs["data"]
            start = body.index(b"Content-Type: message/rfc822\r\n\r\n") + len(b"Content-Type: message/rfc822\r\n\r\n")
            raw = body[start:body.rindex(b"\r\n--")]
            thread = json.loads(body.split(b"\r\n\r\n", 1)[1].split(b"\r\n", 1)[0])["threadId"]
            mid = "f%03d" % (len(self.sent) + 1)
            self.sent[mid] = {"thread": thread, "raw": raw}
            return Resp({"id": mid, "threadId": thread, "labelIds": ["SENT"]})
        if method == "GET" and "format=full" in url:
            self.calls.append((method, url))
            mid = url.split("/messages/")[1].split("?")[0]
            msg = BytesParser(policy=policy.default).parsebytes(self.sent[mid]["raw"])

            def part(p):
                row = {"mimeType": p.get_content_type(), "filename": p.get_filename() or "",
                       "headers": [{"name": k, "value": str(v)} for k, v in p.items()]}
                if p.is_multipart():
                    row["parts"] = [part(x) for x in p.iter_parts()]
                    row["body"] = {"size": 0}
                else:
                    row["body"] = {"size": len(p.get_payload(decode=True) or b"")}
                return row
            return Resp({"id": mid, "threadId": self.sent[mid]["thread"], "labelIds": ["SENT"], "payload": part(msg)})
        return super().request(method, url, **kwargs)


class Delivery(unittest.TestCase):
    def setUp(self):
        from test_v11_chain import live_catalog
        import v11_client_reply as cr
        import socket
        from unittest.mock import patch
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.rest = MailRest()
        book = cw.validate_contacts(direct_contacts(), live_catalog(), None)
        reply = cr.validate_reply(job_config()["reply"], live_catalog())

        @contextmanager
        def token():
            yield TOKEN
        self.writer = cj.delivery_writer(book, reply, token_context=token, session_factory=lambda: self.rest)

    def payload(self, data):
        plan = self.writer.anchor("ab", "wat_mobilite")
        return plan, {"from": cj.HAMLE, "to": plan["to"], "cc": plan["cc"], "thread_id": "ab",
                      "in_reply_to_message_id": plan["anchor"], "subject": plan["subject"], "body": "Merhaba,\nEkte.",
                      "files": [{"filename": "bornova.png", "content_type": "image/png", "byte_size": len(data),
                                 "sha256": hashlib.sha256(data).hexdigest()}]}

    def test_small_file_goes_as_json_send_and_reads_back(self):
        data = png()
        plan, payload = self.payload(data)
        self.assertEqual((plan["anchor"], plan["to"], plan["cc"], plan["unknown"], plan["clients"]),
                         ("a1", [AHMET, CANSU], [OZGE], [], []))  # an agency-only thread has no client
        remote = self.writer.write(payload, [data])["remote_id"]
        self.assertEqual([c[1] for c in self.rest.calls if c[0] == "POST"], [cj.GMAIL_SEND if hasattr(cj, "GMAIL_SEND")
                                                                                else "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"])
        raw = self.rest.sent[remote]["raw"]
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        self.assertEqual((msg.get_content_type(), msg["In-Reply-To"], msg["Cc"]), ("multipart/mixed", "<a1@mail.example>", OZGE))
        att = [p for p in msg.iter_attachments()][0]
        self.assertEqual((att.get_filename(), att.get_payload(decode=True)), ("bornova.png", data))
        self.assertEqual(self.writer.readback(remote, payload), {"readback_sha256": scope_digest(payload)})

    def test_big_file_uses_the_upload_endpoint(self):
        data = png() + b"0" * (4 * 1024 * 1024)
        _, payload = self.payload(data)
        remote = self.writer.write(payload, [data])["remote_id"]
        self.assertEqual([c[1] for c in self.rest.calls if c[0] == "POST"], [cj.UPLOAD_SEND])
        self.assertEqual(self.writer.readback(remote, payload), {"readback_sha256": scope_digest(payload)})

    def test_changed_recipients_or_bytes_stop_before_the_post(self):
        data = png()
        _, payload = self.payload(data)
        for bad, args in ((dict(payload, cc=[]), [data]), (payload, [data + b"x"])):
            with self.assertRaises(Exception) as got:
                self.writer.write(copy.deepcopy(bad), args)
            self.assertIn(getattr(got.exception, "code", ""), cj.NOT_SENT)
        self.assertEqual([c for c in self.rest.calls if c[0] == "POST"], [])


if __name__ == "__main__":
    unittest.main()
