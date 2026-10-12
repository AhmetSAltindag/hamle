"""Same-thread task confirmation (acknowledge) after the Basecamp task is read back. Fakes only."""
import unittest

from autonomy_policy import HAMLE
from test_v11_brand_routing import WAT, BrandHarness
from test_v11_chain import ACC, PROJ, live_catalog, requirements
from test_v11_chain_live import Base, Live, interpretation, AHMET_MAIL
from v11_chain_mail import GmailThreadReplyWriter
from recipient_policy import RecipientError


def ack_on(catalog):
    catalog["chain"]["task_ack"]["enabled"] = True
    return catalog


class DiskFlag(unittest.TestCase):
    def test_disk_catalog_enables_task_ack(self):
        import json
        from test_v11_chain import CATALOG
        data = json.loads(CATALOG.read_text(encoding="utf-8"))
        self.assertIs(data["chain"]["task_ack"]["enabled"], True)


class LiveGmailAck(Base):
    def start_ack(self):
        h = Live()
        ack_on(h.catalog)
        h.models.outputs["interpreter"].append(interpretation())
        r = h.tick()
        self.assertEqual(r["status"], "ok", r)
        return h, r

    def test_one_ack_reply_after_task_readback_then_delivery_still_works(self):
        h, r = self.start_ack()
        rec, work = h.record()
        self.assertEqual(rec["status"], "in_production")
        self.assertEqual(r["provider_writes"], 3, r)  # task + context note + ack
        mid, = h.mailbox.sent
        sent = h.mailbox.parsed(mid)
        self.assertEqual((sent["To"], sent.get("Cc"), sent["In-Reply-To"]), (AHMET_MAIL, None, "<a1@example.invalid>"))
        self.assertEqual(sent["Subject"], "Re: İç duyuru afişi")
        body = sent.get_body(("plain",)).get_content()
        link = f"https://3.basecamp.com/{ACC}/buckets/{PROJ}/todos/{rec['task_id']}"
        for part in ("Talebinizi aldım", "- İş: ", "- Amaç: ", "Açıldığı yer: ", "Atanan: Ahmet", "Görev: " + link):
            self.assertIn(part, body)
        acks = [a for a in h.queue.page(kind="action", limit=100)["items"] if a["action_kind"] == "acknowledge"]
        self.assertEqual([a["state"] for a in acks], ["confirmed"])
        h.tick(); h.tick()
        self.assertEqual(len(h.mailbox.sent), 1)  # once per work
        self.submit(h, 1080, 1080, "Gamechangers YENİ OFİS Kat 3 Pazartesi")
        h.tick()
        rec, _ = h.record()
        self.assertEqual(rec["status"], "delivered", rec)
        self.assertEqual(len(h.mailbox.sent), 2)

    def test_ambiguous_ack_send_is_held_and_never_resent(self):
        h = Live()
        ack_on(h.catalog)
        h.models.outputs["interpreter"].append(interpretation())
        original = h.mailbox.request
        def failing(method, url, **kw):
            if method == "POST":
                h.mailbox.rest_calls.append((method, url))
                raise TimeoutError("synthetic")
            return original(method, url, **kw)
        h.mailbox.request = failing
        h.tick(); h.tick(); h.tick()
        self.assertEqual(len(h.gmail_posts()), 1)
        acks = [a for a in h.queue.page(kind="action", limit=100)["items"] if a["action_kind"] == "acknowledge"]
        self.assertEqual([a["state"] for a in acks], ["unknown"])
        rec, _ = h.record()
        self.assertEqual(rec["intents"]["task-ack"]["status"], "unknown")
        self.assertIsNotNone(rec["task_id"])

    def test_ack_payload_with_non_agency_recipient_is_refused(self):
        h, _ = self.start_ack()
        writer = GmailThreadReplyWriter(h.catalog, token_context=None, anchor_lookup=lambda t: None)
        payload = {"from": HAMLE, "to": [AHMET_MAIL, "client@example.com"], "cc": [], "thread_id": "abc123",
                   "in_reply_to_message_id": "abc124", "subject": "Re: x", "body": "x", "ack_task_id": 1}
        with self.assertRaises(RecipientError):
            writer._shape(payload)
        payload["to"] = [AHMET_MAIL]
        self.assertEqual(writer._shape(payload), "acknowledge")
        payload["ack_task_id"] = 0
        with self.assertRaises(Exception):
            writer._shape(payload)


class BrandAck(unittest.TestCase):
    def test_brand_ack_names_brand_list_assignee_and_keeps_cc(self):
        h = BrandHarness(ack_on(live_catalog()))
        h.brand_mail("t-w", "WAT Mobilite ekim postu", "WAT için post lazım.")
        h.inbox[-1]["cc"] = ["cansu@gamechangers.com.tr"]
        h.tick()
        rec, work, todo = h.task("t-w")
        ack, = h.mail.sent
        self.assertEqual((ack["to"], ack["cc"], ack["thread_id"]), (["ahmet@gamechangers.com.tr"],
                                                                    ["cansu@gamechangers.com.tr"], "t-w"))
        self.assertEqual(ack["ack_task_id"], rec["task_id"])
        self.assertIn("WAT Mobilite · Sosyal Medya listesi", ack["body"])
        self.assertIn("Atanan: Özge", ack["body"])
        self.assertIn("Teslim: 2026-10-14 17:00 Europe/Istanbul", ack["body"])
        self.assertIn(f"/buckets/{WAT}/todos/{rec['task_id']}", ack["body"])
        self.assertEqual(h.authorized.count("acknowledge"), 1)

    def test_clarification_path_unchanged_no_ack(self):
        h = BrandHarness(ack_on(live_catalog()))
        h.brand_mail("t-c", "WAT post", "WAT için bir şey.", req=requirements("WAT post", complete=False))
        h.tick()
        rec, work = h.record("t-c")
        self.assertEqual(rec["status"], "awaiting_clarification")
        mail, = h.mail.sent
        self.assertNotIn("ack_task_id", mail)
        self.assertEqual(mail["to"], ["ahmet@gamechangers.com.tr"])

    def test_one_question_round_then_task_opens_with_assumptions(self):
        h = BrandHarness(ack_on(live_catalog()))
        req = requirements("WAT story")
        for key in ("deadline", "audience", "assets_ref", "objective"):
            req.pop(key)
        req["deliverables"][0].pop("formats")
        h.brand_mail("t-r", "Re: Re: WAT story", "WAT için story, Instagram.", req=req)
        h.tick()
        rec, _ = h.record("t-r")
        self.assertEqual(rec["status"], "awaiting_clarification")
        question, = h.mail.sent
        asked = [l for l in question["body"].splitlines() if l.startswith("- ")]
        self.assertEqual(asked, ["- Bu iş için beklenen teslim tarihi nedir?"])  # only the blocking gap
        self.assertEqual(question["subject"], "Re: WAT story")
        # The answer still has no date: no second question round, the task opens anyway.
        h.brand_mail("t-r", "Re: WAT story", "Ekip planlasın.", req=requirements("WAT story", complete=False))
        h.tick()
        rec, work, todo = h.task("t-r")
        self.assertEqual(rec["status"], "in_production")
        self.assertIn("Teslim tarihi: Belirtilmedi (ekip talep sahibiyle netleştirecek)", todo["description"])
        ack = h.mail.sent[-1]
        self.assertIn("ack_task_id", ack)
        self.assertIn("Netleşecekler", ack["body"])
        self.assertIn("Teslim tarihi yazılmadı", ack["body"])
        self.assertEqual(len([m for m in h.mail.sent if "ack_task_id" not in m]), 1)

    def test_denied_ack_does_not_block_task(self):
        h = BrandHarness(ack_on(live_catalog()))
        h.authorize = lambda kind, payload: kind != "acknowledge"
        h.deps.authorize = h.authorize
        h.brand_mail("t-d", "WAT post", "WAT için post.")
        h.tick()
        rec, work, todo = h.task("t-d")
        self.assertEqual((rec["status"], rec["intents"]["task-ack"]["status"], h.mail.sent), ("in_production", "denied", []))


class AckHtml(unittest.TestCase):
    def test_task_link_is_the_job_title_in_tahoma(self):
        from v11_chain_mail import mail_html
        url = "https://3.basecamp.com/5898865/buckets/40694338/todos/77"
        page = mail_html("Merhaba,\n\n- İş: Yerli <Sanayi> Görseli\n- Amaç: tanıtım\n\nGörev: " + url + "\n\nHamle")
        self.assertIn("font-family:Tahoma", page)
        self.assertIn(f'<a href="{url}"', page)
        self.assertIn(">Yerli &lt;Sanayi&gt; Görseli</a>", page)
        self.assertEqual(page.count("https://"), 1)  # only inside href, never as visible text


if __name__ == "__main__":
    unittest.main()
