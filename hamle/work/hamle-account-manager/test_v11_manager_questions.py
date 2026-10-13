"""Managers (Ahmet, Cansu, Özge) tagging Hamle on its Basecamp task. Fakes only: no sockets, models or credentials."""
import unittest
from datetime import timedelta

import v11_chain as chain
import v11_chain_models as models
from test_v11_chain import AHMET, CANSU, LIST, NOW, OZGE, Harness

LATER = NOW + timedelta(hours=2)


class Answering(Harness):
    def __init__(self):
        super().__init__()
        self.contexts = []

    def answer(self, text, context):
        self.contexts.append(context)
        return super().answer(text, context)


def started(h, thread="t-m"):
    h.mail_in(thread, "talep")
    h.tick()
    return h.record(thread)[0]


def taken_over(h, thread="t-h"):
    """Three 70-point submissions: the work goes to the team (human_handoff, takeover)."""
    h.mail_in(thread, "talep")
    h.tick()
    for round_ in (1, 2, 3):
        h.say(thread, AHMET, f"TESLIM v{round_} PUAN=70")
        h.tick()
    rec, work = h.record(thread)
    assert (rec["status"], work["mode"]) == ("human_handoff", "takeover")
    return rec


def say_at(h, thread, author, text, when):
    cid = h.say(thread, author, text)
    row = next(c for c in h.world.comments[h.task_of(thread)] if c["id"] == cid)
    row["created_at"] = when.isoformat()
    return cid


class ActiveWork(unittest.TestCase):
    def test_manager_status_question_gets_status_from_hamles_record(self):
        h = Answering()
        started(h)
        cid = h.say("t-m", CANSU, "Hamle SORU: bu iş ne durumda, sıradaki adım ne?")
        report = h.tick()
        rec, work = h.record("t-m")
        self.assertEqual(report["provider_writes"], 1)
        last = h.hamle_comments("t-m")[-1]["content"]
        self.assertTrue(last.startswith("<div>@Cansu Akiş Kabil Bu soruya"), last)
        self.assertIn("İşin durumu: üretimde.", last)
        self.assertIn("Sıradaki adım: üretici dosyayı", last)
        self.assertIn("Üretici: Ahmet Şükrü Altındağ.", last)
        self.assertEqual(rec["questions"]["manager-" + str(cid)]["status"], "status_given")
        self.assertEqual((rec["status"], work["mode"]), ("in_production", "active"))
        work_ctx = h.contexts[-1]["work"]
        self.assertEqual((work_ctx["status"], work_ctx["assignees"]), ("üretimde", "Ahmet Şükrü Altındağ"))
        # Not a producer question: the brief gets no unresolved question and delivery is untouched.
        prod = h.queue.backend.records[chain.Chain._key("chain-prod-", rec["work_id"])]
        self.assertEqual(prod["intake_work"]["brief"].get("unresolved_questions") or [], [])
        self.assertEqual(h.tick()["provider_writes"], 0)  # answered once

    def test_grounded_manager_answer_tags_only_the_asker(self):
        h = Answering()
        started(h)
        h.say("t-m", OZGE, "Hamle SORU: teslim tarihi ne?")
        h.tick()
        last = h.hamle_comments("t-m")[-1]["content"]
        self.assertIn("@Özge Yağmur Teslim: 2026-10-14 17:00", last)
        self.assertIn("(Dayanak: brief.deadline)", last)
        rec, _ = h.record("t-m")
        self.assertEqual(list(rec["questions"].values())[-1]["status"], "answered")

    def test_manager_talking_to_the_team_is_left_alone(self):
        h = Answering()
        started(h)
        h.say("t-m", CANSU, "SORU: Özge bunu yarına yetiştirebilir miyiz?")
        report = h.tick()
        self.assertEqual((report["provider_writes"], h.contexts), (0, []))

    def test_approval_outside_the_approval_step_is_explained(self):
        h = Answering()
        started(h)
        h.say("t-m", CANSU, "Hamle ONAY veriyorum, bu haliyle paylaşabiliriz.")
        report = h.tick()
        self.assertEqual(report["provider_writes"], 1)
        last = h.hamle_comments("t-m")[-1]["content"]
        self.assertIn("onay adımında değil", last)
        self.assertIn("İşin durumu: üretimde.", last)
        self.assertEqual((h.record("t-m")[0]["status"], h.mail.sent), ("in_production", []))

    def test_producer_question_flow_is_unchanged(self):
        h = Answering()
        started(h)
        h.say("t-m", AHMET, "SORU: hangi renk paleti?")
        h.tick()
        rec, _ = h.record("t-m")
        self.assertEqual(list(rec["questions"].values())[0]["status"], "escalated_to_managers")
        self.assertNotIn("work", h.contexts[-1])

    def test_answerer_refs_cover_hamles_work_record(self):
        for ref in ("work.status", "work.next_step", "work.assignees", "work.due_on"):
            self.assertIn(ref, models.ANSWER_REFS)


class HeldWork(unittest.TestCase):
    def test_tagging_hamle_on_a_held_work_gets_one_status_note(self):
        h = Harness()
        taken_over(h)
        h.deps.clock = lambda: LATER
        say_at(h, "t-h", CANSU, "Hamle ben onay veriyorum, bunu bu haliyle paylaşabiliriz.", LATER - timedelta(minutes=30))
        say_at(h, "t-h", CANSU, "Hamle bundan sonraki adımların neler?", LATER - timedelta(minutes=10))
        report = h.tick()
        self.assertEqual(report["provider_writes"], 1)
        note = h.hamle_comments("t-h")[-1]["content"]
        self.assertTrue(note.startswith("<div>@Cansu Akiş Kabil<br>Notunu gördüm. Onayını not ettim"), note)
        self.assertIn("İşin durumu: iç düzeltme turları doldu, iş ekibe devredildi.<br>", note)
        self.assertIn("benim otomatik adımım yok", note)
        rec, work = h.record("t-h")
        self.assertEqual((rec["status"], work["mode"], h.mail.sent), ("human_handoff", "takeover", []))
        self.assertEqual(h.tick()["provider_writes"], 0)  # the same burst is never answered twice
        self.assertEqual(report["works"][work["work_id"]]["held_note"], "held_note_written")

    def test_29_ekim_case_quality_held_then_cansu_approves_and_asks(self):
        """12 Oct: the file could not be assessed (quality_held, takeover); 13 Oct Cansu tagged Hamle twice."""
        h = Harness()
        h.assess = lambda view, comment: None
        h.deps.assessor = h.assess
        h.mail_in("t-29", "talep")
        h.tick()
        h.say("t-29", AHMET, "TESLIM dosya ekte")
        h.tick()
        rec, work = h.record("t-29")
        self.assertEqual((rec["status"], rec["reason"], work["mode"]), ("quality_held", "quality_evidence_missing", "takeover"))
        h.deps.clock = lambda: LATER
        say_at(h, "t-29", CANSU, "Hamle ben onay veriyorum, bunu bu haliyle paylaşabiliriz.", LATER - timedelta(minutes=20))
        say_at(h, "t-29", CANSU, "Hamle bundan sonraki ilerleteceğin adımlar neler?", LATER - timedelta(minutes=2))
        self.assertEqual(h.tick()["provider_writes"], 1)
        note = h.hamle_comments("t-29")[-1]["content"]
        self.assertIn("kalite kontrolünü tamamlayamadım, iş ekipte (teslim edilen dosyayı göremediğim için puan veremedim)", note)
        self.assertIn("gönderimi ben yapmayacağım", note)
        self.assertEqual(h.mail.sent, [])

    def test_held_work_ignores_untagged_old_and_outside_comments(self):
        h = Harness()
        taken_over(h)
        h.deps.clock = lambda: LATER
        say_at(h, "t-h", CANSU, "Özge bunu sen gönderir misin?", LATER - timedelta(minutes=5))
        say_at(h, "t-h", 999, "Hamle merhaba", LATER - timedelta(minutes=5))
        say_at(h, "t-h", OZGE, "Hamle eski not", NOW - timedelta(hours=1))  # before the hold
        self.assertEqual(h.tick()["provider_writes"], 0)
        h.deps.clock = lambda: NOW + timedelta(days=5)
        say_at(h, "t-h", OZGE, "Hamle durum ne?", NOW + timedelta(days=1))  # older than 3 days by now
        self.assertEqual(h.tick()["provider_writes"], 0)

    def test_unknown_note_write_is_never_retried(self):
        h = Harness()
        taken_over(h)
        h.deps.clock = lambda: LATER
        say_at(h, "t-h", CANSU, "Hamle durum ne?", LATER - timedelta(minutes=5))
        h.world.fail_comment_post = True
        report = h.tick()
        rec, work = h.record("t-h")
        self.assertEqual(report["works"][work["work_id"]]["held_note"], "held_note_unknown")
        h.world.fail_comment_post = False
        say_at(h, "t-h", CANSU, "Hamle orada mısın?", LATER - timedelta(minutes=1))
        report = h.tick()
        self.assertEqual((report["provider_writes"], report["works"][work["work_id"]]["held_note"]),
                         (0, "held_note_unresolved"))

    def test_denied_note_writes_nothing_and_may_ask_again(self):
        h = Harness()
        taken_over(h)
        h.deps.clock = lambda: LATER
        say_at(h, "t-h", CANSU, "Hamle durum ne?", LATER - timedelta(minutes=5))
        h.deps.authorize = lambda kind, payload: False
        before = len(h.hamle_comments("t-h"))
        self.assertEqual(h.tick()["provider_writes"], 0)
        self.assertEqual(len(h.hamle_comments("t-h")), before)
        h.deps.authorize = h.authorize
        self.assertEqual(h.tick()["provider_writes"], 1)

    def test_global_stop_reads_and_writes_nothing_on_held_work(self):
        h = Harness()
        taken_over(h)
        h.deps.clock = lambda: LATER
        say_at(h, "t-h", CANSU, "Hamle durum ne?", LATER - timedelta(minutes=5))
        h.queue.set_control("stopped", actor="ahmet", expected_generation=h.queue.control()["generation"])
        reads = len(h.world.calls)
        report = h.tick()
        self.assertEqual((report["status"], report["provider_writes"], len(h.world.calls)), ("stopped", 0, reads))

    def test_held_target_stays_in_the_internal_list(self):
        h = Harness()
        taken_over(h)
        h.deps.clock = lambda: LATER
        say_at(h, "t-h", CANSU, "Hamle durum ne?", LATER - timedelta(minutes=5))
        h.tick()
        posts = [c for c in h.world.calls if c[0] == "POST"]
        self.assertTrue(posts[-1][1].endswith(f"/recordings/{h.task_of('t-h')}/comments.json"))
        self.assertIn(LIST, {w["list_id"] for w in h.queue.page(kind="work")["items"]})


if __name__ == "__main__":
    unittest.main()
