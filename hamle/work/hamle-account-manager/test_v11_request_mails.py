"""How Hamle answers team request mails: idea/planning requests, readable questions, short acks. Fakes only."""
import unittest

import v11_chain as chain
from test_v11_chain import AHMET_MAIL, LIST, Harness, live_catalog, requirements


class Mails(Harness):
    def __init__(self, catalog=None):
        super().__init__(catalog)
        self.questions = {}

    def interpret(self, text, context):
        out = super().interpret(text, context)
        key = text if text in self.requirements else text.rsplit("[Son yanıt · " + AHMET_MAIL + "]\n", 1)[-1]
        out["missing_questions"] = list(self.questions.get(key, []))
        return out

    def ask(self, thread, text, req, questions=(), title="Yılbaşı için bir fikir"):
        self.mail_in(thread, text, title=title)
        self.requirements[text] = req
        self.questions[text] = list(questions)


def idea(**extra):
    req = requirements("Yılbaşı için bir fikir", complete=False)
    req.update({"risk_area": "social_planning", **extra})
    return req


class IdeaRequests(unittest.TestCase):
    def test_idea_request_gets_smart_questions_not_silence(self):
        h = Mails()
        h.ask("t-y", "Yılbaşına ses getirecek bir şey yapalım, ne önerirsiniz?", idea(),
              ["Hangi marka için düşünüyoruz?", "Bütçe aralığı nedir?"])
        report = h.tick()
        rec, work = h.record("t-y")
        self.assertEqual((rec["status"], rec["reason"], work["mode"], work["task_id"]),
                         ("awaiting_clarification", "ozge_collaboration_required", "active", None))
        mail, = h.mail.sent
        self.assertEqual((mail["to"], mail["cc"], mail["subject"]), ([AHMET_MAIL], [], "Re: Yılbaşı için bir fikir"))
        body = mail["body"]
        self.assertIn("Talebini aldım. Bu bir sosyal medya fikir ve planlama talebi.", body)
        self.assertIn("Özge ve Cansu ile birlikte ele alacağız", body)
        self.assertNotIn("Ahmet ile", body)  # the requester is not named as a decider to himself
        self.assertEqual([l for l in body.splitlines() if l.startswith("- ")],
                         ["- Hangi marka için düşünüyoruz?", "- Bütçe aralığı nedir?"])
        self.assertEqual((report["provider_writes"], h.world.posts("/todos.json")), (1, []))
        self.assertEqual(h.tick()["provider_writes"], 0)  # asked once

    def test_default_questions_when_the_model_has_none(self):
        h = Mails()
        h.ask("t-y", "Yılbaşı için bir şey yapalım.", idea())
        h.tick()
        asked = [l for l in h.mail.sent[0]["body"].splitlines() if l.startswith("- ")]
        self.assertEqual(asked, ["- " + q for q in chain.NON_ROUTINE_QUESTIONS])

    def test_still_open_after_the_answer_closes_politely_and_goes_to_the_team(self):
        h = Mails()
        h.ask("t-y", "Yılbaşı fikri lazım.", idea())
        h.tick()
        h.ask("t-y", "Tüm markalar için düşünelim, bütçe yok.", idea())
        h.tick()
        rec, work = h.record("t-y")
        self.assertEqual((rec["status"], work["mode"]), ("held", "takeover"))
        closing = h.mail.sent[-1]["body"]
        self.assertIn("Teşekkürler, bilgileri aldım.", closing)
        self.assertIn("Özge ve Cansu ile netleştirip görev olarak açmanızı öneririm.", closing)
        self.assertEqual((len(h.mail.sent), h.world.posts("/todos.json")), (2, []))

    def test_answer_that_makes_it_a_normal_job_opens_the_task(self):
        h = Mails()
        h.ask("t-y", "Yılbaşı fikri lazım.", idea())
        h.tick()
        h.ask("t-y", "Gamechangers için 1 post, 20 Aralık.", requirements("Yılbaşı postu"))
        h.tick()
        rec, work = h.record("t-y")
        self.assertEqual((rec["status"], work["task_id"] is not None), ("in_production", True))

    def test_price_request_names_ahmet_when_someone_else_asks(self):
        h = Mails()
        h.ask("t-p", "Influencer işi için teklif hazırlayalım.", idea(risk_area="commercial"))
        h.inbox[-1]["sender"] = "cansu@gamechangers.com.tr"
        h.tick()
        mail, = h.mail.sent
        self.assertEqual(mail["to"], ["cansu@gamechangers.com.tr"])
        self.assertTrue(mail["body"].startswith("Merhaba Cansu,"), mail["body"])
        self.assertIn("fiyat, teklif veya bütçe", mail["body"])
        self.assertIn("Ahmet ile birlikte", mail["body"])


class ReadableQuestions(unittest.TestCase):
    def test_question_mail_says_what_was_understood_and_never_shows_code_ids(self):
        h = Mails()
        req = requirements("Ekim-Kasım için üç iş")
        req.pop("deadline")
        req.pop("objective")
        req["deliverables"] = [{"id": "linkedin_post", "specification": "LinkedIn'de filo müşterilerine yönelik post",
                                "quantity": 1},
                               {"id": "reels", "kind": "video", "quantity": 1, "duration_seconds": 15,
                                "formats": ["video/mp4"], "specification": "Rezervasyon özelliği için 15 sn Reels"}]
        h.ask("t-3", "Üç işimiz var.", req, title="Ekim-Kasım için üç iş")
        h.tick()
        body = h.mail.sent[0]["body"]
        self.assertIn("Anladığım kadarıyla: Ekim-Kasım için üç iş; 1 × LinkedIn'de filo müşterilerine yönelik post, "
                      "1 × Rezervasyon özelliği için 15 sn Reels.", body)
        self.assertEqual([l for l in body.splitlines() if l.startswith("- ")],
                         ["- Bu iş için beklenen teslim tarihi nedir?"])  # kind/format are team assumptions now
        self.assertNotIn("linkedin_post", body)

    def test_deliverable_questions_use_the_requesters_words(self):
        req = {"deliverables": [{"id": "linkedin_post", "specification": "LinkedIn filo postu"}, {"id": "afis_a3"}]}
        qs = [{"field": "deliverables.linkedin_post.formats",
               "question": "linkedin_post: hangi dosya formatlarında teslim edilecek?"},
              {"field": "deliverables.afis_a3.quantity", "question": "afis_a3: kaç çıktı gerekiyor?"},
              {"field": "deadline", "question": "Bu iş için beklenen teslim tarihi nedir?"}]
        self.assertEqual(chain.readable_questions(qs, req),
                         ["LinkedIn filo postu için: hangi dosya formatlarında teslim edilecek?",
                          "afis a3 için: kaç çıktı gerekiyor?", "Bu iş için beklenen teslim tarihi nedir?"])


class Revisions(unittest.TestCase):
    def test_revision_mail_echoes_the_notes_and_the_praise_before_asking(self):
        h = Mails()
        req = requirements("Hızlı şarj görseli revize", complete=False)
        req.update(objective="Logo büyüyecek; yeşil kurumsal renge çekilecek; CTA 'Uygulamayı indir' olacak.",
                   deliverables=[{"id": "gorsel", "kind": "graphic", "quantity": 1,
                                  "specification": "Hızlı şarj görseli (revize)"}])
        h.ask("t-r", "Logo küçük, yeşil açık, Uygulamayı indir yazalım. Onun dışında çok beğendik, elinize sağlık!",
              req, title="Hızlı şarj görseli revize")
        h.tick()
        body = h.mail.sent[0]["body"]
        self.assertIn("Güzel geri bildirimin için teşekkürler, ekibe ileteceğim. Anladığım kadarıyla: Hızlı şarj "
                      "görseli revize: Logo büyüyecek; yeşil kurumsal renge çekilecek; CTA 'Uygulamayı indir' "
                      "olacak; 1 × Hızlı şarj görseli (revize).", body)
        self.assertEqual([l for l in body.splitlines() if l.startswith("- ")],
                         ["- Bu iş için beklenen teslim tarihi nedir?"])

    def test_no_praise_no_thanks_line(self):
        self.assertEqual(chain.thanks_line({"text": "Logo büyüsün."}), "")
        self.assertEqual(chain.thanks_line({"text": "> çok beğendik\nLogo büyüsün."}), "")  # quoted text ignored


class ShortAck(unittest.TestCase):
    def test_ack_lists_outputs_in_words_and_keeps_model_questions_for_the_team(self):
        cat = live_catalog()
        cat["chain"]["task_ack"]["enabled"] = True
        h = Mails(cat)
        req = requirements("Ankara yeni şarj noktaları duyurusu")
        req["deliverables"][0].update(id="carousel", quantity=1, specification="Instagram carousel, 5 kart, 1080x1350")
        h.ask("t-a", "Ankara duyurusu.", req, ["Kartların içerik dağılımı nasıl olmalı?"],
              title="Ankara yeni şarj noktaları duyurusu")
        h.tick()
        ack = h.mail.sent[-1]["body"]
        self.assertIn("- Çıktılar: 1 × statik tasarım (Instagram carousel, 5 kart, 1080x1350)", ack)
        self.assertNotIn("içerik dağılımı", ack)
        rec, _ = h.record("t-a")
        note = h.world.comments[rec["task_id"]][0]["content"]
        self.assertIn("talep sahibine sorulmadı", note)
        self.assertIn("Kartların içerik dağılımı nasıl olmalı?", note)
        self.assertEqual(len(h.world.posts(f"/todolists/{LIST}/todos.json")), 1)


if __name__ == "__main__":
    unittest.main()
