"""'Gölge:' shadow forwards (Ahmet 2026-10-13): fakes only, no sockets, Gmail, Basecamp or models."""
import socket
import unittest
from unittest.mock import patch

import v11_chain_mail as cmail
import v11_client_watch as cw
from test_anthropic_routing import message
from test_cloud_providers import Cred, Gmail, Store
from test_cloud_providers import message as gmail_message
from test_v11_chain import Harness, LIST
from test_v11_client_reply import AHMET, CANSU, OZGE, ReplyBase, clear, reply_config
from test_v11_client_watch import contacts

FWD = """Hamle, bunu bir dene.

---------- Forwarded message ---------
From: Özge Yağmur <ozge@gamechangers.com.tr>
Date: Mon, Oct 12, 2026 at 4:02 PM
Subject: Re: Kasım içerikleri
To: Ayşe Yılmaz <ayse@watmobilite.com.tr>
Cc: Cansu Akiş Kabil <cansu@gamechangers.com.tr>


Merhaba Ayşe Hanım,
Notlarınızı aldık, revizeleri yarın iletiyoruz.
Sevgiler,
Özge

On Mon, Oct 12, 2026 at 3:14 PM Ayşe Yılmaz <ayse@watmobilite.com.tr>
wrote:

> Merhaba Özge,
> Kasım postlarında logo biraz küçük kalmış, büyütelim lütfen.
> Teşekkürler
>
> 12 Eki 2026 Pzt, 11:00 tarihinde Özge Yağmur <ozge@gamechangers.com.tr> şunu yazdı:
>
>> Merhaba, Kasım postları ekte.
"""

CLIENT_ONLY = """---------- Yönlendirilen ileti ---------
Kimden: Selin Kaya <selin@watmobilite.com.tr>
Tarih: 13 Eki 2026 Sal, 09:40
Konu: 29 Ekim postu
Kime: <ozge@gamechangers.com.tr>

Merhaba,
29 Ekim için bir post rica ediyoruz, 4:5 olsun.
"""


def shadow(n=1, *, text=FWD, subject="Gölge: Fwd: Re: Kasım içerikleri", sender=AHMET):
    return {"message_id": f"g{n}", "thread_id": f"gt{n}", "sender": sender, "to": ["hamle@gamechangers.com.tr"],
            "cc": [], "subject": subject, "text": text, "received_at_ms": 1791990000000 + n, "fingerprint": f"gfp{n}",
            "has_attachments": False, "shadow": True}


class Subject(unittest.TestCase):
    def test_only_a_leading_golge_marks_a_shadow(self):
        for subject in ("Gölge: Fwd: Kasım içerikleri", "Fwd: Gölge: X", "GÖLGE - X", "Re: Gölge: Fwd: X", "golge: X"):
            self.assertTrue(cmail.shadow_forward(subject), subject)
        for subject in ("Gölge efekti revize", "Re: Kasım içerikleri", "Logoya gölge: ekleyelim mi", None):
            self.assertFalse(cmail.shadow_forward(subject), subject)
        self.assertEqual(cw.shadow_subject("Gölge: Fwd: Re: Kasım içerikleri"), "Kasım içerikleri")


class Parse(unittest.TestCase):
    def setUp(self):
        from v11_chain_live import load_catalog
        from v11_daily_digest import load_brand_cards
        self.book = cw.validate_contacts(contacts(), load_catalog(), load_brand_cards())

    def test_gmail_forward_with_the_team_reply_on_top(self):
        p = cw.parse_forward(FWD, self.book)
        self.assertEqual((p["client"]["email"], p["client"]["name"], p["client"]["brand_key"]),
                         ("ayse@watmobilite.com.tr", "Ayşe Yılmaz", "wat_mobilite"))
        self.assertEqual(p["text"], "Merhaba Özge,\nKasım postlarında logo biraz küçük kalmış, büyütelim lütfen.\nTeşekkürler")
        self.assertEqual((p["team_reply"]["email"], p["team_reply"]["name"]), (OZGE, "Özge Yağmur"))
        self.assertTrue(p["team_reply"]["text"].startswith("Merhaba Ayşe Hanım,\nNotlarınızı aldık"))
        self.assertIn("Kasım postları ekte", p["context"])
        self.assertEqual(p["note"], "Hamle, bunu bir dene.")

    def test_turkish_client_only_forward_matches_the_company_domain(self):
        p = cw.parse_forward(CLIENT_ONLY, self.book)
        self.assertEqual((p["client"]["email"], p["client"]["direct"], p["client"]["name"]),
                         ("selin@watmobilite.com.tr", False, "Selin Kaya"))
        self.assertEqual((p["date"], p["subject"], p["team_reply"], p["note"]),
                         ("13 Eki 2026 Sal, 09:40", "29 Ekim postu", None, None))
        self.assertEqual(p["text"], "Merhaba,\n29 Ekim için bir post rica ediyoruz, 4:5 olsun.")

    def test_no_known_client_means_nothing_to_draft(self):
        self.assertIsNone(cw.parse_forward(CLIENT_ONLY.replace("watmobilite.com.tr", "example.com"), self.book))
        self.assertIsNone(cw.parse_forward("Hamle şuna bak", self.book))


class Watch(ReplyBase):
    def watch(self, cfg=None, shadows=()):
        w = super().watch(cfg or reply_config())
        w.cfg["shadow_from"] = [AHMET, CANSU]
        w.shadow_reader = lambda: [dict(s) for s in shadows]
        return w

    def test_shadow_is_drafted_and_never_sent_even_when_auto_send_would(self):
        self.client.outputs = [message(clear())]
        r = self.watch(shadows=[shadow()]).run()
        self.assertEqual((r["shadows"], r["notes"], r["sent"], r["awaiting_approval"], self.replies.sent),
                         (1, 1, 0, 0, []), r)
        self.assertIn("logo biraz küçük kalmış", str(self.client.calls[0]))
        self.assertNotIn("revizeleri yarın iletiyoruz", str(self.client.calls[0]))  # the team's answer is not input
        todo = self.todos.calls[0]
        self.assertTrue(todo["title"].startswith("[GÖLGE · WAT Mobilite] Müşteri maili · Kasım içerikleri"), todo)
        self.assertNotIn("assignee_ids", todo)
        self.assertTrue(todo["description"].startswith(cw.SHADOW_HEADER))
        self.assertIn("Ekibin gerçek cevabı (Özge Yağmur):\nMerhaba Ayşe Hanım,", todo["description"])
        self.assertIn("İleten: ahmet@gamechangers.com.tr · Notu: Hamle, bunu bir dene.", todo["description"])
        row = self.row("g1")
        self.assertEqual((row["shadow"], row["brand_key"], row.get("reply_state")), (True, "wat_mobilite", None))
        self.assertEqual(self.watch(shadows=[shadow()]).run()["notes"], 0)  # once per forward

    def test_real_client_mail_goes_before_shadows_and_summary_shows_both(self):
        from test_v11_client_watch import source
        self.client.outputs = [message(clear()), message(clear())]
        self.mails = [source(1)]
        self.watch(shadows=[shadow(2)]).run()
        self.assertEqual(len(self.replies.sent), 1)  # the real (direct) mail
        self.now = self.now.replace(hour=18, minute=30)
        self.mails = []
        self.watch().run()
        body = self.summaries.calls[0]["body"]
        self.assertIn("Bugün 1 müşteri maili geldi. Hamle 1 tanesine yanıt gönderdi", body)
        self.assertIn("iletilen 1 eski mail için gölge taslak yazdı", body)
        self.assertIn("· GÖLGE", body)
        self.assertIn("Ekibin cevabı (Özge Yağmur): Merhaba Ayşe Hanım,", body)

    def test_forward_without_a_known_client_is_reported_not_drafted(self):
        r = self.watch(shadows=[shadow(text=CLIENT_ONLY.replace("watmobilite.com.tr", "example.com"))]).run()
        self.assertEqual((r["held"], self.client.calls, self.todos.calls), (1, [], []), r)
        self.now = self.now.replace(hour=18, minute=30)
        self.watch().run()
        self.assertIn("İletilen mailde tanıdığım bir müşteri adresi bulamadım", self.summaries.calls[0]["body"])

    def test_shadow_people_must_be_verified_agency_people(self):
        with self.assertRaises(cw.ClientWatchError):
            cw.validate_config(dict(reply_config(), shadow_from=["birisi@example.com"]))
        cfg = dict(reply_config(), shadow_from=["yok@gamechangers.com.tr"])
        with self.assertRaises(cw.ClientWatchError):
            super().watch(cfg)


class Readers(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)

    def factories(self, gmail):
        return {"_service_factory": lambda _: gmail, "_credentials_factory": lambda _: Cred(),
                "_request_factory": lambda: object()}

    def test_chain_reader_skips_shadows_and_shadow_reader_takes_only_them(self):
        from test_v11_chain import live_catalog
        fwd = gmail_message("a1", 1000, subject="Gölge: Fwd: Kasım içerikleri", text=CLIENT_ONLY)
        chain = cmail.ChainMailReader(Store(), live_catalog(), clock=lambda: 100, **self.factories(Gmail([fwd])))
        self.assertEqual((chain(), chain.skipped), ([], {"shadow_forward": 1}))
        cfg = dict(reply_config(), shadow_from=[AHMET])
        reader = cw.shadow_mail_reader(Store(), cfg, clock=lambda: 100, **self.factories(Gmail([fwd])))
        src, = reader()
        self.assertEqual((src["shadow"], src["sender"], src["text"]), (True, AHMET, CLIENT_ONLY))
        plain = gmail_message("a1", 1000, subject="Kasım içerikleri")
        reader = cw.shadow_mail_reader(Store(), cfg, clock=lambda: 100, **self.factories(Gmail([plain])))
        self.assertEqual((reader(), reader.skipped), ([], {"not_a_shadow_forward": 1}))
        reply = gmail_message("a1", 1000, subject="Re: Gölge: Fwd: Kasım içerikleri")
        reader = cw.shadow_mail_reader(Store(), cfg, clock=lambda: 100, **self.factories(Gmail([reply])))
        self.assertEqual(reader(), [])


class Chain(unittest.TestCase):
    def test_shadow_forward_never_opens_work(self):
        h = Harness()
        h.mail_in("t-shadow", CLIENT_ONLY, title="Gölge: Fwd: 29 Ekim postu")
        report = h.tick()
        self.assertEqual([m["reason"] for m in report["ignored_mail"]], ["shadow_forward_not_a_request"])
        self.assertEqual((h.world.posts(f"/todolists/{LIST}/todos.json"), h.calls["interpreter"]), ([], 0))


if __name__ == "__main__":
    unittest.main()
