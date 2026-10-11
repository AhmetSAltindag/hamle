"""Synthetic end-to-end ticks for v11_chain. Fakes only: no sockets, models or credentials."""
import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import business_writers as bw
import v11_chain as chain
from autonomy_policy import HAMLE, scope_digest
from test_autonomy_policy import NOW, fixture as authority_fixture, stamp
from test_cloud_providers import FAKE_TOKEN, Response, Session
from test_v11_quality_gate import NOW as QUALITY_NOW, fixture as quality_fixture, human_approval
from v11_brand_task import HAMLE_PERSON, BrandTaskWriter
from v11_intake_plan import plan_intake
from v11_work_queue import InMemoryBackend, QueueStore

CATALOG = Path(__file__).resolve().parents[2] / "outputs/gamechangers-internal/gamechangers-internal-catalog.json"
ORIGIN = "https://3.basecampapi.com"
ACC, PROJ, LIST = 5898865, 49184053, 777000111  # LIST is synthetic: the real Todolist id is unconfirmed
AHMET, CANSU, OZGE = 47470759, 48793256, 52096503
AHMET_MAIL = "ahmet@gamechangers.com.tr"
SHIFT = QUALITY_NOW - NOW


def draft_catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def live_catalog():
    """Synthetic in-memory copy only: the file on disk stays a closed draft."""
    data = draft_catalog()
    data.update(verified=True, live_enabled=True, provider_mutation_allowed_by_this_file=True)
    data["routes"][0].update(list_id=LIST, verified=True, verification_ref="SYNTHETIC")
    for person in data["basecamp_people"].values():
        person["project_member"] = True
    for contact in data["contacts"]:
        contact["project_member"] = True
    data["chain"]["agency_direct_request_counts_as_agreement"]["verified"] = True
    return data


def shifted(value):
    """Move quality-gate fixture timestamps onto this test's clock."""
    if isinstance(value, dict):
        return {k: shifted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shifted(v) for v in value]
    if isinstance(value, str) and len(value) > 18 and value[4:5] == "-" and "T" in value:
        try:
            stamp_ = datetime.fromisoformat(value)
        except ValueError:
            return value
        if stamp_.tzinfo is not None:
            return (stamp_ - SHIFT).isoformat()
    return value


class World:
    """One fake Basecamp account: todolist, todos, comments. Shared by writers and reader."""

    def __init__(self):
        self.todos, self.comments, self.next_id, self.calls = {}, {}, 9000, []
        self.fail_comment_post = False

    def _id(self):
        self.next_id += 1
        return self.next_id

    def todo(self, tid):
        row = copy.deepcopy(self.todos[tid])
        row["comments_count"] = len(self.comments[tid])
        return row

    def add_comment(self, tid, author, html_text):
        cid = self._id()
        self.comments[tid].append({"id": cid, "type": "Comment", "status": "active", "visible_to_clients": False,
                                   "bucket": {"id": PROJ}, "parent": {"id": tid, "type": "Todo"},
                                   "creator": {"id": author}, "content": html_text})
        return cid

    def session(self):
        world = self

        class WorldSession(Session):
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                world.calls.append((method, url))
                base = f"{ORIGIN}/{ACC}/buckets/{PROJ}"
                body = json.loads(kwargs["data"]) if kwargs.get("data") else None
                if method == "GET" and url == f"{base}/todolists/{LIST}.json":
                    return Response({"id": LIST, "type": "Todolist", "bucket": {"id": PROJ},
                                     "status": "active", "visible_to_clients": False})
                if method == "POST" and url == f"{base}/todolists/{LIST}/todos.json":
                    tid = world._id()
                    world.todos[tid] = {
                        "id": tid, "type": "Todo", "status": "active", "visible_to_clients": False,
                        "bucket": {"id": PROJ}, "parent": {"id": LIST, "type": "Todolist"},
                        "creator": {"id": HAMLE_PERSON}, "completed": False, "due_on": None,
                        "content": body["content"], "description": body["description"],
                        "assignees": [{"id": i} for i in body["assignee_ids"]],
                        "completion_subscribers": [{"id": i} for i in body["completion_subscriber_ids"]],
                        "comments_url": f"{base}/recordings/{tid}/comments.json"}
                    world.comments[tid] = []
                    return Response({"id": tid, "bucket": {"id": PROJ}}, 201)
                if method == "GET" and url.startswith(f"{base}/todos/"):
                    return Response(world.todo(int(url.rsplit("/", 1)[1][:-5])))
                if method == "POST" and url.startswith(f"{base}/recordings/"):
                    if world.fail_comment_post:
                        raise TimeoutError("synthetic ambiguous comment write")
                    tid = int(url.split("/recordings/")[1].split("/")[0])
                    return Response({"id": world.add_comment(tid, HAMLE_PERSON, body["content"])}, 201)
                if method == "GET" and url.startswith(f"{base}/comments/"):
                    cid = int(url.rsplit("/", 1)[1][:-5])
                    row = next(c for rows in world.comments.values() for c in rows if c["id"] == cid)
                    return Response(row)
                raise AssertionError("unexpected fake request " + method + " " + url)
        return WorldSession()

    def get(self, url, headers):
        """BasecampReader transport (same contract as SessionGet)."""
        self.calls.append(("READ", url))
        if url.startswith(f"{ORIGIN}/{ACC}/todos/"):
            return 200, self.todo(int(url.rsplit("/", 1)[1][:-5])), {}
        tid = int(url.split("/recordings/")[1].split("/")[0])
        return 200, copy.deepcopy(self.comments[tid]), {}

    def posts(self, suffix):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]


class MailWriter:
    def __init__(self):
        self.sent = []

    def write(self, payload):
        self.sent.append(copy.deepcopy(payload))
        return {"remote_id": "synthetic-gmail-" + str(len(self.sent))}

    def readback(self, remote_id, payload):
        return {"readback_sha256": scope_digest(payload)}


def requirements(title, kinds=("graphic",), complete=True):
    req = {"brand_id": "gamechangers", "service_id": "gc_internal", "category_key": "internal",
           "risk_area": "routine", "title": title, "objective": "İç duyuru görseli.",
           "audience": "Gamechangers ekibi", "deadline": "2026-10-14 17:00 Europe/Istanbul",
           "assets_ref": "Gamechangers logo (iç arşiv)",
           "deliverables": [{"id": k, "kind": k, "quantity": 1, "formats": ["image/png"],
                             "specification": "1080x1080 sosyal post"} for k in kinds]}
    if not complete:
        req.pop("deliverables")
        req.pop("deadline")
    return req


def delivery_bridge(queue):
    """Trusted delivery evidence fake modelled on test_v11_delivery.fixture, bound to the queue work."""
    def bridge(view):
        qw = queue.get_work(view["work_id"])
        gen = queue.control()["generation"]
        scope, request = authority_fixture()
        scope.update(brand_id="gamechangers", agency_contacts=scope["agency_contacts"] + [AHMET_MAIL],
                     allowed_cc_contacts=[], basecamp={"account_id": ACC, "project_id": PROJ, "list_ids": [LIST],
                                                       "customer_visible": False, "verification_ref": "synthetic"})
        scope["services"][0].update(service_id="gc_internal", list_id=LIST, assignee_ids=[AHMET])
        wb = {k: qw[k] for k in ("work_id", "brand_id", "service_id", "thread_id", "account_id", "project_id",
                                 "list_id", "task_id")}
        request.update(brand_id="gamechangers", service_id="gc_internal", work_id=qw["work_id"],
                       reply_to=list(view["reply"]["to"]), reply_cc=list(view["reply"]["cc"]), work_binding=wb)
        request["control"].update(work_id=qw["work_id"], global_generation=gen, work_generation=qw["work_generation"])
        request["delegation"].update(work_id=qw["work_id"], service_id="gc_internal", brand_id="gamechangers",
                                     thread_id=qw["thread_id"], scope_digest=scope_digest(scope),
                                     global_generation=gen, work_generation=qw["work_generation"])
        request["source"].update(thread_id=qw["thread_id"], sender=AHMET_MAIL, to=[HAMLE], cc=[])
        request["basecamp_target"].update(account_id=ACC, project_id=PROJ, list_id=LIST, task_id=qw["task_id"])
        request["prepared_action"].update(work_id=qw["work_id"], brand_id="gamechangers", service_id="gc_internal",
                                          task_id=qw["task_id"])
        work = {**wb, "revision_index": 0,
                "brief": {"verified": True, "evidence_ref": "synthetic-brief",
                          "required_deliverables": [{"id": "main", "mime_types": ["application/pdf"],
                                                     "min_bytes": 1, "max_bytes": 100000, "facts": {"page_count": 1}}],
                          "semantic_checks": ["matches_brief", "language"],
                          "factual_checks": ["brand_identity", "no_invented_claims"]}}
        artifact = {"brand_id": "gamechangers", "work_id": qw["work_id"], "version": "v1", "sha256": "b" * 64,
                    "mime_type": "application/pdf", "evidence_ref": "synthetic-byte-readback",
                    "deliverables": [{"id": "main", "sha256": "b" * 64, "mime_type": "application/pdf",
                                      "size_bytes": 200, "facts": {"page_count": 1}, "facts_bound_sha256": "b" * 64,
                                      "evidence_ref": "synthetic-pdf-parser"}]}
        binding = {"brand_id": "gamechangers", "work_id": qw["work_id"], "version": "v1", "sha256": "b" * 64,
                   "brief_sha256": scope_digest(work["brief"]), "manifest_sha256": scope_digest(artifact["deliverables"]),
                   "scope_digest": scope_digest(scope), "source_fingerprint": request["source"]["fingerprint"],
                   "recipients_sha256": scope_digest({"to": request["reply_to"], "cc": request["reply_cc"]})}
        qa = {**binding, "verified": True, "accepted": True, "evidence_ref": "synthetic-qa", "policy_ref": "qa-v1",
              "assets_ref": "assets-v1", "internal_notes_excluded": True, "rights_checked": True,
              "checked_at": stamp(-50), "semantic_checks": {"matches_brief": True, "language": True},
              "factual_checks": {"brand_identity": True, "no_invented_claims": True}}
        body = "İstediğiniz çalışma ektedir."
        payload = {"from": HAMLE, "to": request["reply_to"], "cc": request["reply_cc"], "thread_id": qw["thread_id"],
                   "subject": "Re: İç talep", "body": body,
                   "artifact": {k: artifact[k] for k in ("brand_id", "work_id", "version", "sha256", "mime_type")}}
        release = {**binding, "verified": True, "actor": "producer@example.test", "revoked": False,
                   "customer_delivery": True, "evidence_ref": "synthetic-release", "issued_at": stamp(-40),
                   "expires_at": stamp(300), "reply_to": list(request["reply_to"]),
                   "reply_cc": list(request["reply_cc"]), "service_id": "gc_internal",
                   "action_payload_sha256": scope_digest(payload)}
        snapshot = {"binding": wb, "observed_at": stamp(-1), "evidence_ref": "synthetic-basecamp-current",
                    "previous_cursor": None, "cursor": "cursor-1", "has_more": False, "completed": True,
                    "task_fingerprint": "d" * 64, "assignee_ids": [AHMET],
                    "events": [{"id": "comment-1", "task_id": qw["task_id"], "source_order": 1,
                                "evidence_ref": "synthetic-comment-readback"}],
                    "artifact": artifact, "producer_submission": {**binding, "verified": True, "actor_id": AHMET,
                                                                 "issued_at": stamp(-60), "evidence_ref": "synthetic-submit"},
                    "qa": qa, "release": release, "customer_subject": "Re: İç talep", "customer_body": body}
        return work, Adapter(scope, request, snapshot)
    return bridge


class Adapter:
    def __init__(self, scope, request, snapshot):
        self.scope, self.request, self.snapshot = scope, request, snapshot

    def read_basecamp(self, work, *, after_cursor):
        out = copy.deepcopy(self.snapshot)
        out["previous_cursor"] = after_cursor
        if after_cursor == out["cursor"]:
            out["events"] = []
        return out

    def read_authority(self, work):
        return copy.deepcopy({"scope": self.scope, "request": self.request})


class Harness:
    def __init__(self, catalog=None):
        self.catalog = catalog or live_catalog()
        self.queue = QueueStore(InMemoryBackend(), clock=lambda: NOW.timestamp())
        self.queue.initialize()
        self.queue.set_control("queue_open", actor="offline-test", expected_generation=0)
        self.world, self.mail = World(), MailWriter()
        self.inbox, self.calls, self.authorized = [], {"interpreter": 0, "classifier": 0, "answerer": 0}, []
        self.order = 0
        self.requirements, self.evidence, self.approval_granted = {}, {}, False
        self.deps = chain.ChainDeps(
            queue=self.queue, catalog=self.catalog, clock=lambda: NOW,
            mail_reader=lambda: copy.deepcopy(self.inbox), interpreter=self.interpret,
            classifier=self.classify, answerer=self.answer, assessor=self.assess,
            task_writer_factory=lambda cat, st: BrandTaskWriter(cat, st, token_provider=lambda: FAKE_TOKEN,
                                                                session_factory=self.world.session),
            basecamp_get=self.world.get,
            comment_writer=chain.BasecampCommentWriter(self.catalog, token_provider=lambda: FAKE_TOKEN,
                                                       session_factory=self.world.session),
            mail_writer=self.mail, delivery_bridge=delivery_bridge(self.queue), authorize=self.authorize,
            approval_reader=self.approve)

    def authorize(self, kind, payload):
        self.authorized.append(kind)
        return True

    def interpret(self, text, context):
        self.calls["interpreter"] += 1
        self.last_context = context
        return {"requirements": copy.deepcopy(self.requirements[text]),
                "follow_ups": ["Aynı görselin story (1080x1920) uyarlaması"]}

    def classify(self, text, context):
        self.calls["classifier"] += 1
        return "question" if "SORU" in text else "submission" if "TESLIM" in text else \
            "approval" if "ONAY" in text else "other"

    def answer(self, text, context):
        self.calls["answerer"] += 1
        if "tarih" in text:
            return {"answer": "Teslim: " + context["brief"]["deadline"], "grounded_in": ["brief.deadline"]}
        return None

    def assess(self, view, comment):
        if comment["id"] in self.evidence:  # re-assessment of the same stored submission
            return copy.deepcopy(self.evidence[comment["id"]])
        score = comment.get("content", "").split("PUAN=")[1].split("<")[0]
        policy, assessment, binding, safety = (shifted(x) for x in quality_fixture(score))
        assessment["policy_digest"] = scope_digest(policy)  # policy timestamps moved onto this clock
        self.evidence[comment["id"]] = {"policy": policy, "assessment": assessment, "binding": binding,
                                        "safety": safety, "artifact_version": "v" + str(comment["id"])}
        return copy.deepcopy(self.evidence[comment["id"]])

    def approve(self, view, comment):
        """Stands in for a verified approval UI record; the comment text alone never counts."""
        if not self.approval_granted:
            return None
        ev = list(self.evidence.values())[-1]
        return shifted(human_approval(ev["policy"], ev["assessment"], ev["binding"], "85"))

    def mail_in(self, thread, text, title="İç duyuru görseli", complete=True):
        self.order += 1
        mid = f"synthetic-m{self.order}"
        self.requirements[text] = requirements(title, complete=complete)
        self.inbox = [m for m in self.inbox if m["thread_id"] != thread] + [{
            "channel": "email", "verified": True, "headers_origin": "provider", "latest_in_thread": True,
            "evidence_ref": "SYNTHETIC-gmail:" + mid, "message_id": mid, "thread_id": thread,
            "fingerprint": hashlib.sha256(mid.encode()).hexdigest(), "observed_at": NOW.isoformat(),
            "automatic": False, "own_message": False, "human_takeover": False, "sender": AHMET_MAIL,
            "to": [HAMLE], "cc": [], "origin": {"kind": "direct_email"}, "subject": title, "text": text,
            "source_order": self.order}]
        return mid

    def tick(self):
        return chain.run_chain_tick(self.deps)

    def record(self, thread):
        work_id = next(w["work_id"] for w in self.queue.page(kind="work")["items"] if w["thread_id"] == thread)
        return self.queue.backend.records[chain.Chain._key("chain-", work_id)], self.queue.get_work(work_id)

    def task_of(self, thread):
        return self.record(thread)[0]["task_id"]

    def say(self, thread, author, text):
        return self.world.add_comment(self.task_of(thread), author, "<div>" + text + "</div>")

    def hamle_comments(self, thread):
        return [c for c in self.world.comments[self.task_of(thread)] if c["creator"]["id"] == HAMLE_PERSON]

    def actions(self):
        return self.queue.page(kind="action", limit=100)["items"]


class DraftProfile(unittest.TestCase):
    def test_draft_file_is_closed_and_marks_unconfirmed_ids(self):
        data = draft_catalog()
        self.assertEqual((data["verified"], data["live_enabled"], data["provider_mutation_allowed_by_this_file"]),
                         (False, False, False))
        route = data["routes"][0]
        self.assertEqual((route["list_id"], route["verified"]), (None, False))
        self.assertEqual(route["assignee_ids_by_kind"]["graphic"], [AHMET])
        self.assertFalse(data["basecamp_people"]["47658175"]["project_member"])
        self.assertEqual((data["basecamp"]["account_id"], data["basecamp"]["project_id"]), (ACC, PROJ))

    def test_draft_file_makes_zero_reads_writes_or_model_calls(self):
        h = Harness(draft_catalog())
        h.mail_in("t-draft", "draft")
        report = h.tick()
        self.assertEqual((report["status"], report["reason"]), ("held", "brand_profile_live_disabled"))
        self.assertEqual((h.world.calls, h.mail.sent, h.calls["interpreter"]), ([], [], 0))

    def test_plan_intake_rejects_draft_but_accepts_confirmed_copy(self):
        h = Harness()
        h.mail_in("t-plan", "plan")
        source, req = h.inbox[0], requirements("x")
        self.assertEqual(plan_intake(source, req, draft_catalog(), now=NOW)["reason"], "catalog_unverified")
        req["agreement"] = {"verified": True, "source_message_id": source["message_id"], "evidence_ref": "S",
                            "requirements_sha256": chain.intake_requirements_digest(req)}
        plan = plan_intake(source, req, live_catalog(), now=NOW)
        self.assertEqual(plan["status"], "ready_internal_task", plan)
        b = plan["brief"]["binding"]
        self.assertEqual((b["assignee_ids"], b["subscriber_ids"]), ([AHMET], [AHMET, CANSU, OZGE]))


class EndToEnd(unittest.TestCase):
    def test_mail_task_question_answer_submission_94_delivery_same_thread(self):
        h = Harness()
        h.mail_in("t-1", "Merhaba Hamle, iç duyuru görseli lazım.")
        r1 = h.tick()
        self.assertEqual(len(h.world.posts(f"/todolists/{LIST}/todos.json")), 1)
        rec, work = h.record("t-1")
        self.assertEqual((rec["status"], work["task_id"]), ("in_production", rec["task_id"]))
        todo = h.world.todos[rec["task_id"]]
        self.assertEqual([a["id"] for a in todo["assignees"]], [AHMET])
        self.assertEqual([a["id"] for a in todo["completion_subscribers"]], [AHMET, CANSU, OZGE])
        note = h.hamle_comments("t-1")[0]["content"]
        self.assertIn("yalnız ekip içi", note)
        self.assertIn("story", note)
        self.assertEqual(r1["provider_writes"], 2)  # task + internal context note
        self.assertEqual(rec["intents"][next(k for k in rec["intents"] if k.startswith("create-stage-task"))]["status"],
                         "confirmed")

        h.say("t-1", AHMET, "SORU: teslim tarihi ne zaman?")
        r2 = h.tick()
        rec, _ = h.record("t-1")
        self.assertEqual(r2["provider_writes"], 1)
        self.assertIn("2026-10-14 17:00", h.hamle_comments("t-1")[-1]["content"])
        self.assertEqual(list(rec["questions"].values())[0]["status"], "answered")
        prod = h.queue.backend.records[chain.Chain._key("chain-prod-", rec["work_id"])]
        self.assertEqual(len(prod["intake_work"]["brief"]["unresolved_questions"]), 1)

        h.say("t-1", AHMET, "TESLIM dosya ekte PUAN=94")
        r3 = h.tick()
        rec, _ = h.record("t-1")
        self.assertEqual((rec["status"], rec["quality"]["score"]), ("delivered", "94"))
        sent, = h.mail.sent
        self.assertEqual((sent["thread_id"], sent["to"], sent["cc"]), ("t-1", [AHMET_MAIL], []))
        self.assertEqual(r3["provider_writes"], 1)
        self.assertTrue(all(a["state"] == "confirmed" for a in h.actions()))
        self.assertEqual(sorted(a["action_kind"] for a in h.actions()),
                         ["add_comment", "add_comment", "create_task", "deliver_file"])

        before = (len(h.world.calls), len(h.actions()), dict(h.calls))
        r4 = h.tick()
        r5 = h.tick()
        self.assertEqual((r4["provider_writes"], r5["provider_writes"], r5["model_calls"]), (0, 0, 0))
        self.assertEqual(len(h.actions()), before[1])
        self.assertEqual(h.calls, before[2])
        self.assertEqual(len(h.world.posts(f"/todolists/{LIST}/todos.json")), 1)
        self.assertEqual(len(h.mail.sent), 1)

    def test_unanswerable_question_tags_managers(self):
        h = Harness()
        h.mail_in("t-q", "talep")
        h.tick()
        h.say("t-q", AHMET, "SORU: hangi renk paleti?")
        h.tick()
        rec, _ = h.record("t-q")
        self.assertEqual(list(rec["questions"].values())[0]["status"], "escalated_to_managers")
        last = h.hamle_comments("t-q")[-1]["content"]
        for name in ("Özge Yağmur", "Ahmet Şükrü Altındağ", "Cansu Akiş Kabil"):
            self.assertIn("@" + name, last)

    def test_missing_info_produces_clarification_and_no_task(self):
        h = Harness()
        h.mail_in("t-c", "eksik talep", complete=False)
        report = h.tick()
        rec, work = h.record("t-c")
        self.assertEqual(rec["status"], "awaiting_clarification")
        self.assertIsNone(work["task_id"])
        self.assertEqual(h.world.posts("/todos.json"), [])
        mail, = h.mail.sent
        self.assertEqual((mail["to"], mail["cc"], mail["thread_id"]), ([AHMET_MAIL], [], "t-c"))
        self.assertIn("teslim tarihi", mail["body"])
        self.assertEqual((report["provider_writes"], h.actions()[0]["action_kind"]), (1, "clarify_brief"))
        self.assertEqual(h.tick()["provider_writes"], 0)
        # The requester answers in the same thread -> same work, task is created now.
        h.mail_in("t-c", "tamamlanmış talep")
        h.tick()
        rec, work = h.record("t-c")
        self.assertEqual((rec["status"], work["task_id"] is not None), ("in_production", True))
        self.assertEqual(len(h.world.posts("/todos.json")), 1)

    def test_score_85_waits_for_human_approval(self):
        h = Harness()
        h.mail_in("t-85", "talep")
        h.tick()
        h.say("t-85", AHMET, "TESLIM PUAN=85")
        h.tick()
        rec, _ = h.record("t-85")
        self.assertEqual((rec["status"], rec["quality"]["route"]), ("awaiting_human_approval", "needs_human_approval"))
        self.assertIn("Özge, Ahmet veya Cansu", h.hamle_comments("t-85")[-1]["content"])
        self.assertEqual(h.mail.sent, [])
        # A plain "ONAY" comment without a verified approval record is not an approval.
        h.say("t-85", OZGE, "ONAY")
        self.assertEqual(h.tick()["provider_writes"], 0)
        self.assertEqual(h.record("t-85")[0]["status"], "awaiting_human_approval")
        self.assertEqual(h.mail.sent, [])
        # With an exact verified approval record from one of Özge/Ahmet/Cansu, delivery proceeds.
        h.approval_granted = True
        h.say("t-85", OZGE, "ONAY verildi")
        h.tick()
        self.assertEqual(h.record("t-85")[0]["status"], "delivered")
        self.assertEqual([m["thread_id"] for m in h.mail.sent], ["t-85"])

    def test_score_70_two_revisions_then_human_handoff(self):
        h = Harness()
        h.mail_in("t-70", "talep")
        h.tick()
        for round_ in (1, 2):
            h.say("t-70", AHMET, f"TESLIM v{round_} PUAN=70")
            h.tick()
            rec, _ = h.record("t-70")
            self.assertEqual((rec["status"], rec["revision_count"]), ("changes_requested", round_))
            self.assertIn(f"İç düzeltme {round_}/2", h.hamle_comments("t-70")[-1]["content"])
        h.say("t-70", AHMET, "TESLIM v3 PUAN=70")
        h.tick()
        rec, work = h.record("t-70")
        self.assertEqual((rec["status"], rec["revision_count"], work["mode"]), ("human_handoff", 2, "takeover"))
        self.assertIn("2 iç düzeltme turu doldu", h.hamle_comments("t-70")[-1]["content"])
        self.assertEqual(h.mail.sent, [])
        h.say("t-70", AHMET, "TESLIM v4 PUAN=99")
        self.assertEqual(h.tick()["provider_writes"], 0)  # taken over: Hamle stays out

    def test_global_stop_means_zero_actions(self):
        h = Harness()
        h.mail_in("t-s", "talep")
        h.queue.set_control("stopped", actor="ahmet", expected_generation=1)
        report = h.tick()
        self.assertEqual((report["status"], report["provider_writes"], report["model_calls"]), ("stopped", 0, 0))
        self.assertEqual((h.world.calls, h.mail.sent, h.actions(), h.calls["interpreter"]), ([], [], [], 0))

    def test_work_takeover_stops_only_that_work(self):
        h = Harness()
        h.mail_in("t-a", "talep a")
        h.mail_in("t-b", "talep b")
        h.tick()
        _, work = h.record("t-a")
        h.queue.set_work_mode(work["work_id"], mode="takeover", actor="ahmet",
                              expected_work_generation=work["work_generation"])
        h.say("t-a", AHMET, "TESLIM PUAN=94")
        h.say("t-b", AHMET, "TESLIM PUAN=94")
        h.tick()
        self.assertEqual([m["thread_id"] for m in h.mail.sent], ["t-b"])

    def test_two_works_interleaved(self):
        h = Harness()
        h.mail_in("t-x", "talep x", title="Ofis afişi")
        h.mail_in("t-y", "talep y", title="LinkedIn postu")
        h.tick()
        self.assertEqual(len(h.world.posts("/todos.json")), 2)
        self.assertNotEqual(h.task_of("t-x"), h.task_of("t-y"))
        h.say("t-x", AHMET, "SORU: teslim tarihi?")
        h.say("t-y", AHMET, "TESLIM PUAN=94")
        h.tick()
        self.assertEqual([m["thread_id"] for m in h.mail.sent], ["t-y"])
        self.assertEqual(list(h.record("t-x")[0]["questions"].values())[0]["status"], "answered")
        h.say("t-x", AHMET, "TESLIM PUAN=70")
        h.tick()
        self.assertEqual((h.record("t-x")[0]["status"], h.record("t-y")[0]["status"]),
                         ("changes_requested", "delivered"))
        # The context note of whichever work was planned second lists the other work of the same brand.
        notes = h.hamle_comments("t-x")[0]["content"] + h.hamle_comments("t-y")[0]["content"]
        self.assertTrue("Ofis afişi · durum" in notes or "LinkedIn postu · durum" in notes, notes)

    def test_uncertain_comment_write_is_held_and_never_retried(self):
        h = Harness()
        h.mail_in("t-u", "talep")
        h.tick()
        h.say("t-u", AHMET, "SORU: teslim tarihi?")
        h.world.fail_comment_post = True
        h.tick()
        rec, work = h.record("t-u")
        self.assertEqual((rec["status"], work["queue_state"]), ("write_outcome_unknown", "unknown"))
        h.world.fail_comment_post = False
        posts = len(h.world.posts("/comments.json"))
        report = h.tick()
        self.assertEqual((report["provider_writes"], len(h.world.posts("/comments.json"))), (0, posts))

    def test_denied_fresh_authority_never_posts_task(self):
        h = Harness()
        h.deps.authorize = lambda kind, payload: False
        h.mail_in("t-d", "talep")
        h.tick()
        self.assertEqual(h.world.posts("/todos.json"), [])
        self.assertEqual(h.record("t-d")[1]["mode"], "takeover")

    def test_unknown_sender_or_hamle_not_in_to_cc_is_ignored_without_model_call(self):
        h = Harness()
        h.mail_in("t-z", "talep")
        h.inbox[0]["to"] = ["someone@gamechangers.com.tr"]
        report = h.tick()
        self.assertEqual(report["ignored_mail"][0]["reason"], "hamle_not_actual_to_or_cc")
        self.assertEqual((h.calls["interpreter"], h.queue.page(kind="work")["items"]), (0, []))

    def test_no_model_budget_means_no_model_call(self):
        h = Harness()
        h.deps.budget_ok = lambda: False
        h.mail_in("t-b", "talep")
        report = h.tick()
        self.assertEqual((report["reason"], h.calls["interpreter"], h.world.calls), ("model_budget_unavailable", 0, []))
        h.deps.budget_ok = lambda: True
        h.tick()
        self.assertEqual(len(h.world.posts("/todos.json")), 1)


class CommentWriter(unittest.TestCase):
    def payload(self):
        return {"target": {"account_id": ACC, "project_id": PROJ, "list_id": LIST, "task_id": 55},
                "body": "Merhaba\nekip", "mention_ids": [], "internal_only": True}

    def test_one_post_and_exact_readback(self):
        row = {"id": 7, "bucket": {"id": PROJ}, "parent": {"id": 55}, "creator": {"id": HAMLE_PERSON},
               "visible_to_clients": False, "content": bw._description("Merhaba\nekip")}
        sessions = [Session(Response({"id": 7}, 201)), Session(Response(row))]
        writer = chain.BasecampCommentWriter(live_catalog(), token_provider=lambda: FAKE_TOKEN,
                                             session_factory=lambda: sessions.pop(0))
        self.assertEqual(writer.write(self.payload()), {"remote_id": "7"})
        self.assertEqual(writer.readback("7", self.payload())["readback_sha256"], scope_digest(self.payload()))

    def test_foreign_project_rejected_before_any_request(self):
        writer = chain.BasecampCommentWriter(live_catalog(), token_provider=lambda: FAKE_TOKEN,
                                             session_factory=lambda: self.fail("no session"))
        payload = self.payload()
        payload["target"]["project_id"] = 40694338
        with self.assertRaises(chain.ChainError):
            writer.write(payload)


if __name__ == "__main__":
    unittest.main()
