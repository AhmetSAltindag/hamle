"""Synthetic briefs on the verified WAT profile; fake HTTP only, sockets never used."""
import copy
import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import v11_brand_task as app
from autonomy_policy import HAMLE, scope_digest
from test_cloud_providers import Response, Session, FAKE_TOKEN
from v11_intake_plan import intake_requirements_digest, plan_intake
from v11_production_stages import advance_production, initialize_production
from v11_work_queue import InMemoryBackend, QueueStore

CATALOG = Path(__file__).resolve().parents[2] / "outputs/wat-pilot-20261010/wat-planning-catalog.json"
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
SOCIAL, PROJECT = 8231779952, 40694338
BERKAY, SAMET = 47658175, 50486696
OVERSIGHT = [47470759, 48793256, 52096503]


def catalog(live=False):
    data = json.loads(CATALOG.read_text())
    if live:  # Synthetic copy only: the real WAT profile stays live_enabled=false.
        data.update(live_enabled=True, provider_mutation_allowed_by_this_file=True)
    return data


def plan(profile, kinds=("graphic",), service="wat_social", category="social"):
    source = {"channel": "email", "verified": True, "headers_origin": "provider", "latest_in_thread": True,
              "evidence_ref": "SYNTHETIC", "message_id": "synthetic-m1", "thread_id": "synthetic-t1",
              "fingerprint": hashlib.sha256(b"synthetic-wat").hexdigest(), "observed_at": NOW.isoformat(),
              "automatic": False, "own_message": False, "human_takeover": False,
              "sender": "selina.mircavi@watmobilite.com", "to": [HAMLE], "cc": ["cansu@gamechangers.com.tr"],
              "origin": {"kind": "direct_email"}}
    req = {"brand_id": "wat_mobilite", "service_id": service, "category_key": category, "risk_area": "routine",
           "title": "Ekim kampanya postu", "objective": "Yeni rota duyurusu.", "audience": "Mevcut kullanıcılar",
           "deadline": "2026-10-15 12:00 Europe/Istanbul", "assets_ref": "WATMobilite-Brand Guideline.pdf",
           "deliverables": [dict({"id": k, "kind": k, "quantity": 1,
                                  "formats": ["image/png" if k == "graphic" else "video/mp4"],
                                  "specification": "1080x1920 story"},
                                 **({} if k == "graphic" else {"duration_seconds": 10})) for k in kinds]}
    req["agreement"] = {"verified": True, "source_message_id": "synthetic-m1", "evidence_ref": "SYNTHETIC",
                        "requirements_sha256": intake_requirements_digest(req)}
    result = plan_intake(source, req, profile, now=NOW)
    assert result["status"] == "ready_internal_task", result
    return source, result["brief"]


class Fixture:
    def __init__(self, profile, kinds=("graphic",), **kw):
        self.profile = profile
        self.source, brief = plan(profile, kinds, **kw)
        b = brief["binding"]
        self.queue = QueueStore(InMemoryBackend())
        self.queue.initialize()
        self.gen = self.queue.set_control("queue_open", actor="offline-test", expected_generation=0)["generation"]
        self.work = self.queue.bind_work(brand_id="wat_mobilite", thread_id="synthetic-t1", account_id=b["account_id"],
                                         project_id=b["project_id"], list_id=b["list_id"],
                                         service_id=brief["service_id"], expected_generation=self.gen)
        self.queue.enqueue_message(self.work["work_id"], message_id="synthetic-m1",
                                   fingerprint=self.source["fingerprint"], source_order=1, expected_generation=self.gen)
        self.claim = self.queue.claim_next(self.work["work_id"], worker_id="offline", expected_generation=self.gen)
        self.state = initialize_production(self.work["work_id"], brief)

    def prepared(self, stage="production"):
        return app.prepare_stage_task(self.state, self.profile, stage, self.queue.get_work(self.work["work_id"]), self.claim)


def todolist(list_id=SOCIAL):
    return {"id": list_id, "type": "Todolist", "bucket": {"id": PROJECT}, "status": "active", "visible_to_clients": False}


def readback(task, tid=900):
    return {"id": tid, "bucket": {"id": PROJECT}, "parent": {"id": task["todolist_id"], "type": "Todolist"},
            "creator": {"id": app.HAMLE_PERSON}, "status": "active", "visible_to_clients": False, "completed": False,
            "due_on": None, "content": task["title"], "description": app.bw._description(task["description"]),
            "assignees": [{"id": i} for i in task["assignee_ids"]],
            "completion_subscribers": [{"id": i} for i in task["completion_subscriber_ids"]]}


class ProfileRouting(unittest.TestCase):
    def test_social_static_goes_to_berkay_on_social_list(self):
        f = Fixture(catalog())
        task = f.prepared()["payload"]["task"]
        self.assertEqual((task["account_id"], task["project_id"], task["todolist_id"]), (5898865, PROJECT, SOCIAL))
        self.assertEqual(task["assignee_ids"], [BERKAY])
        self.assertEqual(task["completion_subscriber_ids"], OVERSIGHT)
        self.assertEqual((task["notify"], task["due_on"]), (False, None))
        self.assertIn("Berkay Kayaköylü", task["description"])
        self.assertIn("Selina Mirçavi, doğrudan e-posta", task["description"])
        self.assertNotIn("@", task["description"])

    def test_social_video_goes_to_samet(self):
        task = Fixture(catalog(), ("video",)).prepared()["payload"]["task"]
        self.assertEqual((task["todolist_id"], task["assignee_ids"]), (SOCIAL, [SAMET]))

    def test_design_then_animation_opens_berkay_first_and_holds_samet(self):
        f = Fixture(catalog(), ("graphic", "animation"))
        task = f.prepared("design")["payload"]["task"]
        self.assertEqual(task["assignee_ids"], [BERKAY])
        self.assertIn("motion (Samet Göksu, design kabulünden sonra)", task["description"])
        with self.assertRaises(app.BrandTaskError) as e:
            f.prepared("motion")
        self.assertEqual(e.exception.code, "stage_not_ready_for_task")

    def test_non_social_motion_uses_motion_list(self):
        task = Fixture(catalog(), ("animation",), service="wat_motion",
                       category="non_social_motion").prepared()["payload"]["task"]
        self.assertEqual((task["todolist_id"], task["assignee_ids"]), (9381497228, [SAMET]))

    def test_changed_profile_requires_replan(self):
        f = Fixture(catalog())
        f.profile["routes"][0]["assignee_ids_by_kind"]["graphic"] = [SAMET]
        with self.assertRaises(app.BrandTaskError) as e:
            f.prepared()
        self.assertEqual(e.exception.code, "brand_profile_changed_replan_required")

    def test_queue_event_must_be_the_brief_source_mail(self):
        f = Fixture(catalog())
        f.claim["event"]["fingerprint"] = "b" * 64
        with self.assertRaises(app.BrandTaskError) as e:
            f.prepared()
        self.assertEqual(e.exception.code, "queue_event_not_brief_source")


class Dispatch(unittest.TestCase):
    def run_flow(self, f, session, authorize=lambda p: True):
        writer = app.BrandTaskWriter(f.profile, f.state, token_provider=lambda: FAKE_TOKEN, session_factory=lambda: session)
        return app.create_stage_task(f.queue, f.claim, f.state, f.profile, "production", writer, authorize=authorize)

    def test_live_profile_creates_one_task_reads_back_and_binds_work(self):
        f = Fixture(catalog(live=True))
        task = f.prepared()["payload"]["task"]
        session = Session(Response(todolist()), Response({"id": 900, "bucket": {"id": PROJECT}}, 201), Response(readback(task)))
        result = self.run_flow(f, session)
        self.assertEqual(result["status"], "task_created_readback_verified", result)
        posts = [c for c in session.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][1].endswith(f"/5898865/buckets/{PROJECT}/todolists/{SOCIAL}/todos.json"))
        body = json.loads(posts[0][2]["data"])
        self.assertEqual((body["assignee_ids"], body["completion_subscriber_ids"], body["notify"]), ([BERKAY], OVERSIGHT, False))
        self.assertEqual(f.queue.get_work(f.work["work_id"])["task_id"], 900)
        action = f.queue.page(kind="action")["items"][0]
        self.assertEqual((action["state"], action["receipt"]["remote_id"]), ("confirmed", "900"))
        bound = advance_production(f.state, {"id": "bind-1", "type": "bind_stage_task", "work_id": f.state["work_id"],
            "expected_revision": 0, "stage_id": "production", "actor_id": 52096503, "verified": True,
            "evidence_ref": "synthetic", "occurred_at": NOW.isoformat(), "task": result["stage_task"]})
        self.assertEqual(bound["status"], "applied", bound)

    def test_real_wat_profile_stays_closed_without_any_request(self):
        f = Fixture(catalog())
        session = Session()
        result = self.run_flow(f, session)
        self.assertEqual((result["status"], result["reason"], result["network_writes"]),
                         ("held", "brand_profile_live_disabled", 0))
        self.assertEqual(session.calls, [])
        self.assertEqual(f.queue.page(kind="action")["items"], [])

    def test_denied_fresh_authorization_never_posts(self):
        f = Fixture(catalog(live=True))
        session = Session(Response(todolist()))
        result = self.run_flow(f, session, authorize=lambda p: False)
        self.assertEqual(result["reason"], "fresh_authorization_denied")
        self.assertEqual([c[0] for c in session.calls], ["GET"])
        self.assertEqual(f.queue.page(kind="action")["items"], [])

    def test_uncertain_post_is_held_and_never_resent(self):
        f = Fixture(catalog(live=True))
        session = Session(Response(todolist()), TimeoutError())
        result = self.run_flow(f, session)
        self.assertEqual((result["reason"], result["network_writes"]), ("provider_write_outcome_unknown_no_retry", 1))
        self.assertEqual(f.queue.get_work(f.work["work_id"])["queue_state"], "unknown")
        again = Session(Response(todolist()), Response({"id": 901, "bucket": {"id": PROJECT}}, 201))
        self.assertEqual(self.run_flow(f, again)["status"], "held")
        self.assertFalse(any(c[0] == "POST" for c in again.calls))

    def test_readback_with_wrong_producer_is_not_confirmed(self):
        f = Fixture(catalog(live=True))
        row = readback(f.prepared()["payload"]["task"]); row["assignees"] = [{"id": SAMET}]
        result = self.run_flow(f, Session(Response(todolist()), Response({"id": 900, "bucket": {"id": PROJECT}}, 201), Response(row)))
        self.assertEqual(result["reason"], "provider_write_outcome_unknown_no_retry")
        self.assertIsNone(f.queue.get_work(f.work["work_id"])["task_id"])

    def test_writer_rejects_payload_not_derived_from_profile(self):
        f = Fixture(catalog(live=True))
        payload = f.prepared()["payload"]; payload["task"]["assignee_ids"] = [SAMET]
        session = Session()
        writer = app.BrandTaskWriter(f.profile, f.state, token_provider=lambda: FAKE_TOKEN, session_factory=lambda: session)
        with self.assertRaises(app.BrandTaskError) as e:
            writer.create_task(payload, before_post=lambda: None)
        self.assertEqual(e.exception.code, "brand_task_not_profile_derived")
        self.assertEqual(session.calls, [])

    def test_moved_or_client_visible_list_is_not_written(self):
        f = Fixture(catalog(live=True))
        row = todolist(); row["visible_to_clients"] = True
        session = Session(Response(row))
        self.assertEqual(self.run_flow(f, session)["reason"], "basecamp_target_changed")
        self.assertFalse(any(c[0] == "POST" for c in session.calls))


if __name__ == "__main__":
    unittest.main()
