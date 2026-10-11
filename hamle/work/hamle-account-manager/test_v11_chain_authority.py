"""Send authority for the chain from the existing visible-stop work authority (fakes only)."""
import copy
from datetime import datetime, timedelta
import json
import unittest

import v11_chain_live as live
from autonomy_policy import HAMLE
from test_v11_chain_live import AHMET_MAIL, T0, Base, Live, interpretation
from v11_chain_authority import (POLICY_NAME, ChainWorkAuthority, chain_stop_sweep, enabled_policy,
                                 load_policy_file)
from v11_stop_intake import ReceivedWorkMail
from v11_work_authority import policy_terms_digest

ACCEPTED_AT = T0 - timedelta(hours=2)


def prepared():
    """Pending copy (the on-disk policy was accepted by Ahmet on 2026-10-11)."""
    doc = copy.deepcopy(load_policy_file())
    doc.update(live_enabled=False, status="pending_ahmet_acceptance", accepted=False)
    doc["work_authority_policy"].update(enabled=False, status="pending_ahmet_acceptance", acceptance=None)
    return doc


def accepted_policy(**changes):
    """SYNTHETIC test acceptance only; the file on disk stays pending Ahmet."""
    p = copy.deepcopy(prepared()["work_authority_policy"])
    p.update(enabled=True, status="accepted")
    p["acceptance"] = {"verified": True, "authenticated": True, "actor": AHMET_MAIL,
                       "decision": "accept_visible_stop_work_authority", "evidence_ref": "SYNTHETIC-test-acceptance",
                       "accepted_at": ACCEPTED_AT.isoformat(), "expires_at": "2027-12-31T23:59:59+03:00"}
    p["acceptance"].update(changes)
    p["acceptance"]["policy_terms_sha256"] = policy_terms_digest(p)
    return p


class StopReader:
    def __init__(self, h):
        self.h, self.mail, self.calls = h, None, []

    def read_latest(self, work, grant):
        self.calls.append((work["work_id"], grant["binding"]["source_message_id"]))
        return self.mail

    def stop(self, text, sender=AHMET_MAIL):
        self.mail = ReceivedWorkMail("ab", "a7", sender, (HAMLE,), (), "e" * 64, self.h.clock().isoformat(), text)


class PreparedFile(unittest.TestCase):
    def test_prepared_policy_is_pending_and_never_enabled(self):
        doc = prepared()
        self.assertEqual((doc["live_enabled"], doc["status"], doc["accepted"]),
                         (False, "pending_ahmet_acceptance", False))
        p = doc["work_authority_policy"]
        self.assertEqual((p["enabled"], p["acceptance"], p["max_business_days"], p["actions"]),
                         (False, None, 2, ["add_comment", "deliver_file"]))
        self.assertIsNone(enabled_policy(doc))
        accepted_form = dict(copy.deepcopy(p), enabled=True, status="accepted")
        self.assertEqual(doc["policy_terms_sha256_when_accepted"], policy_terms_digest(accepted_form))
        self.assertIn("wat_mobilite ONLY", doc["modelled_on"])

    def test_disk_policy_is_accepted_and_valid_in_the_real_scope(self):
        from datetime import datetime as dt
        from v11_chain_live import load_catalog
        from v11_chain_quality import build_scope, load_rubric, rubric_policy
        from v11_work_authority import validate_work_authority_policy
        doc = load_policy_file()
        policy = enabled_policy(doc)
        self.assertEqual((doc["live_enabled"], policy["acceptance"]["actor"], policy["acceptance"]["expires_at"]),
                         (True, AHMET_MAIL, "2027-12-31T23:59:59+03:00"))
        catalog, rubric = load_catalog(), load_rubric()
        scope = build_scope(catalog, rubric, rubric_policy(rubric, catalog), policy)
        validate_work_authority_policy(policy, scope, scope["services"][0],
                                       now=dt.fromisoformat("2026-10-12T09:00:00+03:00"))
        with self.assertRaises(Exception):
            validate_work_authority_policy(policy, scope, scope["services"][0],
                                           now=dt.fromisoformat("2028-01-04T00:00:00+03:00"))


class Authority(Base):
    def run_to_submission(self, policy, *, wait=600):
        h = Live(work_policy=policy)
        h.models.outputs["interpreter"].append(interpretation())
        h.tick()
        rec, _ = h.record()
        h.clock.step(wait)  # delivery is "delayed" (> 300 s): only a work grant can authorize it
        self.submit(h, 1080, 1080, "Gamechangers YENİ OFİS Kat 3 Pazartesi")
        return h, rec

    def test_grant_derived_at_intake_then_90plus_delivery_with_all_checks(self):
        h, rec = self.run_to_submission(accepted_policy())
        self.assertEqual(rec["authority"]["status"], "granted", rec["authority"])
        row = h.work_authority.record(rec["work_id"])
        grant = row["grant"]
        self.assertEqual((grant["issuer_kind"], grant["actor"], grant["status"]),
                         ("accepted_brand_policy_derivation", AHMET_MAIL, "active"))
        self.assertEqual(grant["binding"]["source_message_id"], "a1")
        # earliest of: 2 weekdays from mail (Wed), scope/rubric, policy acceptance, budget -> the business day
        self.assertEqual(datetime.fromisoformat(grant["expires_at"]),
                         datetime.fromisoformat(grant["business_expires_at"]))
        self.assertTrue(grant["business_expires_at"].startswith("2026-10-14T09:00:10"))
        h.tick()
        rec, _ = h.record()
        self.assertEqual(rec["status"], "delivered", (rec, h.authorizer.decisions))
        self.assertEqual(len(h.gmail_posts()), 1)

    def test_pending_policy_on_disk_means_no_grant_and_delivery_held(self):
        h = Live(authority=False)  # same as live wiring with enabled_policy(prepared()) -> None
        self.assertIsNone(enabled_policy(prepared()))
        h.models.outputs["interpreter"].append(interpretation())
        h.tick()
        h.clock.step(600)
        self.submit(h, 1080, 1080, "Gamechangers YENİ OFİS Kat 3 Pazartesi")
        h.tick()
        rec, _ = h.record()
        self.assertEqual(rec["status"], "delivery_held")
        self.assertNotIn("authority", rec)
        self.assertEqual(h.gmail_posts(), [])

    def test_mail_before_policy_acceptance_gets_no_grant(self):
        h = Live(work_policy=accepted_policy(accepted_at=T0.isoformat()))  # mail arrived 20 s earlier
        h.models.outputs["interpreter"].append(interpretation())
        h.tick()
        rec, _ = h.record()
        self.assertEqual(rec["authority"]["status"], "not_granted")
        self.assertEqual(rec["authority"]["reason"], "work_authority_historical_mail_requires_explicit_grant")
        self.assertIsNone(h.work_authority.record(rec["work_id"]))

    def test_visible_stop_mail_fences_work_and_revokes_grant(self):
        h, rec = self.run_to_submission(accepted_policy())
        reader = StopReader(h)
        reader.stop("Hamle dur, bu işi ben devralıyorum.")
        result = chain_stop_sweep(h.queue, h.work_authority, reader, clock=h.clock)
        self.assertEqual((result["status"], result["stops"][0]["intent"]), ("completed", "human_takeover"))
        self.assertEqual(reader.calls[0][1], "a1")
        work = h.queue.get_work(rec["work_id"])
        self.assertEqual(work["mode"], "takeover")
        grant = h.work_authority.record(rec["work_id"])["grant"]
        self.assertEqual((grant["status"], grant["revocations"][0]["reason"]), ("revoked", "received_visible_stop"))
        self.assertEqual(h.work_authority.authority_source({"work_id": rec["work_id"]}, {}, {}), {})
        h.tick()
        self.assertEqual(h.gmail_posts(), [])

    def test_quoted_or_absent_stop_does_nothing(self):
        h, rec = self.run_to_submission(accepted_policy())
        reader = StopReader(h)
        reader.stop("Teşekkürler\n> Hamle dur")
        self.assertEqual(chain_stop_sweep(h.queue, h.work_authority, reader, clock=h.clock)["stops"], [])
        self.assertEqual(h.queue.get_work(rec["work_id"])["mode"], "active")

    def test_queue_stop_and_reopen_invalidates_grant_by_generation(self):
        h, rec = self.run_to_submission(accepted_policy())
        g = h.queue.control()["generation"]
        live.queue_command(h.queue, "stop", actor=AHMET_MAIL, expected_generation=g)
        self.assertFalse(h.authorizer("add_comment", {"target": {}, "body": "x"}))
        h.tick()
        self.assertEqual(h.gmail_posts(), [])
        reopened = live.queue_command(h.queue, "open", actor=AHMET_MAIL, expected_generation=g + 1)
        h.queue_gen = reopened["generation"]
        h.deps = h.build()  # a new slot builds a new authorizer for the new generation
        h.tick()
        rec, _ = h.record()
        self.assertEqual(rec["status"], "delivery_held")
        self.assertEqual(h.gmail_posts(), [])

    def test_slot_runs_chain_stop_sweep_before_the_tick(self):
        h, rec = self.run_to_submission(accepted_policy())
        reader = StopReader(h)
        h.clock.now = datetime.fromisoformat("2026-10-12T09:30:30+03:00")
        reader.stop("Durdurun lütfen")
        r = live.run_chain_slot(h.ops, open_queue=lambda: h.queue, build_deps=lambda q, og, qg: h.deps,
                                clock=h.clock,
                                check_chain_stops=lambda q: chain_stop_sweep(q, h.work_authority, reader,
                                                                             clock=h.clock))
        self.assertEqual(r["status"], "chain_tick_completed", r)
        self.assertEqual(h.queue.get_work(rec["work_id"])["mode"], "stopped")
        self.assertEqual(h.gmail_posts(), [])
        reader.mail = None
        def failing(work, grant):
            raise TimeoutError("synthetic")
        reader.read_latest = failing
        h.clock.now = datetime.fromisoformat("2026-10-12T09:45:30+03:00")
        r = live.run_chain_slot(h.ops, open_queue=lambda: h.queue, build_deps=lambda q, og, qg: h.deps,
                                clock=h.clock, check_chain_stops=lambda q: {"status": "completed",
                                                                            "grants_created": 0,
                                                                            "business_writes": None})
        self.assertEqual((r["status"], r["reason"]), ("held", "chain_stop_sweep_incomplete"))


if __name__ == "__main__":
    unittest.main()
