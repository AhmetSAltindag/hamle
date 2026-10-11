"""Offline end-to-end chain_tick with the REAL adapters over fake providers.

Fakes only: Gmail SDK + REST mailbox, Basecamp account (todos, comments, blob bytes),
OpenAI Responses transport, Firestore client, OCR text. No sockets, keys or credentials.
"""
import base64
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta
from email import policy as email_policy
from email.parser import BytesParser
import hashlib
import json
import os
import socket
import unittest
from unittest.mock import patch

import v11_artifact_inspector as inspector
import v11_chain as chain
import v11_chain_live as live
import worker
from autonomy_policy import scope_digest
from cloud_store import OperationsStore
from test_cloud_providers import Cred, Gmail, Response, Store, message
from test_cloud_store import MemoryBackend, budget_snapshot
from test_v11_artifact_inspector import png
from test_v11_chain import ACC, AHMET, LIST, PROJ, World, live_catalog
from v11_brand_task import HAMLE_PERSON, BrandTaskWriter
from v11_chain_mail import ChainMailReader, GmailThreadReplyWriter
from v11_chain_models import ChainModels, ModelGate
from v11_chain_quality import (BasecampAttachmentFetcher, ChainQualityBridge, load_rubric,
                               record_quality_approval, rubric_policy)
from v11_work_queue import InMemoryBackend, QueueError, QueueStore

TOKEN = "synthetic-placeholder-token-never-live"
KEY = "FAKE-OPENAI-KEY-NEVER-PERSIST"
HAMLE = "hamle@gamechangers.com.tr"
AHMET_MAIL = "ahmet@gamechangers.com.tr"
T0 = datetime.fromisoformat("2026-10-12T09:00:30+03:00")  # Monday, first minutes of a 15-min slot
MAIL = ("Merhaba Hamle, 1080x1080 PNG bir iç duyuru afişi lazım. Üzerinde 'YENİ OFİS', 'Kat 3' ve "
        "'Pazartesi' yazsın. Teslim 14 Ekim 17:00.")


def draft_rubric():
    """Closed copy (the on-disk rubric was accepted on 2026-10-11)."""
    r = load_rubric()
    r.update(status="draft", weights_status="draft_not_accepted", verified=False, accepted_by=None,
             accepted_at=None, evidence_ref=None)
    return r


def accepted_rubric():
    r = load_rubric()
    r.update(status="accepted", weights_status="accepted", verified=True, accepted_by=r["acceptance_required_from"],
             accepted_at=(T0 - timedelta(hours=1)).isoformat(), evidence_ref="SYNTHETIC-acceptance")
    r["rights_declaration"]["accepted"] = True
    return r


class Clock:
    def __init__(self):
        self.now = T0
    def __call__(self):
        return self.now
    def ts(self):
        return self.now.timestamp()
    def step(self, seconds=30):
        self.now += timedelta(seconds=seconds)


# ------------------------------------------------------------------ fake providers
class Mailbox(Gmail):
    """Gmail SDK fake (reader) + REST fake (writer) over ONE synthetic thread 'ab'."""

    def __init__(self, clock):
        sent_ms = int((T0 - timedelta(seconds=20)).timestamp() * 1000)
        super().__init__([message("a1", sent_ms, subject="İç duyuru afişi", text=MAIL)])
        self.clock, self.sent, self.rest_calls = clock, {}, []

    # REST (GmailThreadReplyWriter)
    def request(self, method, url, **kwargs):
        self.rest_calls.append((method, url))
        assert kwargs["allow_redirects"] is False
        if method == "POST":
            body = json.loads(kwargs["data"])
            mid = "f%03d" % (len(self.sent) + 1)
            raw = base64.urlsafe_b64decode(body["raw"])
            self.sent[mid] = raw
            row = message(mid, int(self.clock.ts() * 1000), sender=HAMLE, recipient=AHMET_MAIL,
                          subject="Re: İç duyuru afişi", text="synthetic sent copy")
            row["labelIds"] = ["SENT"]
            self.rows.append(row)
            return Response({"id": mid, "threadId": body["threadId"], "labelIds": ["SENT"]})
        path = url.split("/gmail/v1/users/me/", 1)[1]
        if path.startswith("threads/"):
            return Response({"id": "ab", "messages": [{"id": r["id"], "labelIds": r["labelIds"],
                                                       "internalDate": r["internalDate"]} for r in self.rows]})
        mid = path.split("/")[1].split("?")[0]
        if "format=raw" in url:
            return Response({"id": mid, "threadId": "ab", "labelIds": ["SENT"],
                             "raw": base64.urlsafe_b64encode(self.sent[mid]).decode().rstrip("=")})
        row = next(r for r in self.rows if r["id"] == mid)
        return Response({"id": mid, "threadId": "ab", "labelIds": row["labelIds"], "internalDate": row["internalDate"],
                         "payload": {"headers": row["payload"]["headers"]}})

    def close(self):
        pass

    def parsed(self, mid):
        return BytesParser(policy=email_policy.default).parsebytes(self.sent[mid])


class Account(World):
    """test_v11_chain.World + comment timestamps, attachments and blob downloads."""

    def __init__(self, clock):
        super().__init__()
        self.clock, self.blobs = clock, {}

    def add_comment(self, tid, author, html_text, attachment=None):
        cid = super().add_comment(tid, author, html_text)
        row = self.comments[tid][-1]
        row["created_at"] = (self.clock() - timedelta(seconds=5)).isoformat()
        if attachment is not None:
            name, data = attachment
            self.blobs[cid] = data
            row["content_attachments"] = [{
                "id": cid * 10, "filename": name, "content_type": "image/png", "byte_size": len(data),
                "download_url": f"https://storage.3.basecamp.com/{ACC}/blobs/b{cid}/download/{name}"}]
        return cid

    def session(self):
        inner = super().session()
        account = self
        original = inner.request

        def request(method, url, **kwargs):
            if method == "GET" and url.startswith(f"https://storage.3.basecamp.com/{ACC}/blobs/"):
                account.calls.append((method, url))
                cid = int(url.split("/blobs/b")[1].split("/")[0])
                return Response(account.blobs[cid])
            return original(method, url, **kwargs)
        inner.request = request
        return inner


class ModelTransport:
    """OpenAI Responses fake: one queued output per role; counts every POST."""

    def __init__(self):
        self.outputs = {"interpreter": [], "classifier": [], "answerer": []}
        self.calls = []

    def __call__(self, url, **kwargs):
        body = json.loads(kwargs["data"])
        role = body["text"]["format"]["name"].replace("hamle_chain_", "")
        self.calls.append(role)
        out = self.outputs[role].pop(0)
        envelope = {"id": "resp_" + role + str(len(self.calls)), "model": "gpt-6-astra", "status": "completed",
                    "error": None, "incomplete_details": None,
                    "output": [{"type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": json.dumps(out, ensure_ascii=False)}]}],
                    "usage": {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600}}
        raw = json.dumps(envelope).encode()

        class R:
            status_code = 200
            def iter_content(self, chunk_size):
                yield raw
            def close(self):
                pass
        return R()


def interpretation():
    return {"requirements": {"title": "İç duyuru afişi", "objective": "Yeni ofis duyurusu", "audience": "Ekip",
                             "deadline": "14 Ekim 17:00", "assets_ref": "Gamechangers logo arşivi",
                             "risk_area": "routine",
                             "deliverables": [{"id": "afis", "kind": "graphic", "quantity": 1,
                                               "formats": ["image/png"], "specification": "1080x1080 PNG afiş",
                                               "duration_seconds": None, "width": 1080, "height": 1080,
                                               "required_text": ["YENİ OFİS", "Kat 3", "Pazartesi"]}]},
            "missing_questions": [], "context_notes": ["İç duyuru; müşteri yok."], "follow_ups": []}


OCR = {}


def fake_isolated(original):
    def isolated(data, mime, *, ocr=False):
        if ocr:
            return {"text": OCR[hashlib.sha256(data).hexdigest()], "parser": "synthetic-ocr",
                    "languages": ["eng", "tur"]}
        return original(data, mime)
    return isolated


def artwork(width, height, text):
    data = png(width, height, text=f"{width}x{height}-{len(OCR)}")
    OCR[hashlib.sha256(data).hexdigest()] = text
    return data


# ------------------------------------------------------------------ harness
class Live:
    def __init__(self, *, authority=True, rubric=None, work_policy=None):
        self.clock = Clock()
        self.catalog = live_catalog()
        self.ops = OperationsStore(MemoryBackend(), clock=self.clock)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.ops_gen = self.ops.resume_review_only("offline-test")["generation"]
        self.queue = QueueStore(InMemoryBackend(), clock=self.clock.ts)
        self.clock.now = T0 - timedelta(minutes=5)  # queue opened before the synthetic mail (sent T0-20s)
        self.queue.initialize()
        self.queue_gen = self.queue.set_control("queue_open", actor="offline-test", expected_generation=0)["generation"]
        self.clock.now = T0
        self.mailbox, self.account, self.models = Mailbox(self.clock), Account(self.clock), ModelTransport()
        self.rubric = rubric or accepted_rubric()
        self.evidence = []
        self.authority = authority
        self.work_policy = work_policy
        self.deps = self.build()

    def authority_source(self, dwork, scope, source):
        if not self.authority:
            return {}
        work = self.queue.get_work(dwork["work_id"])
        return {"delegation": {
            "scope_digest": scope_digest(scope), "work_id": dwork["work_id"], "service_id": dwork["service_id"],
            "brand_id": dwork["brand_id"], "actor": scope["agency_owner"], "evidence_ref": "SYNTHETIC-delegation",
            "issued_at": (self.clock() - timedelta(minutes=5)).isoformat(),
            "expires_at": (self.clock() + timedelta(minutes=30)).isoformat(), "revoked": False,
            "global_generation": self.queue.control()["generation"], "work_generation": work["work_generation"],
            "thread_id": dwork["thread_id"], "actions": ["add_comment", "deliver_file"]}}

    def build(self):
        """Same assembly as v11_chain_live.build_live_deps, provider sessions swapped for fakes."""
        @contextmanager
        def gmail_token():
            yield TOKEN
        token = lambda: TOKEN
        fetcher = BasecampAttachmentFetcher(self.catalog, token_provider=token, session_factory=self.account.session)
        holder = {}
        self.mail = GmailThreadReplyWriter(
            self.catalog, token_context=gmail_token,
            anchor_lookup=live.anchor_lookup(self.queue, self.catalog["brand_id"]),
            attachment_source=lambda art: holder["q"].attachment_source(art),
            session_factory=lambda: self.mailbox, clock=self.clock)
        from v11_chain_authority import ChainWorkAuthority
        self.work_authority = ChainWorkAuthority(queue=self.queue, ops=self.ops, catalog=self.catalog,
                                                 policy=self.work_policy, source_reader=self.mail.source_evidence,
                                                 clock=self.clock)
        self.quality = ChainQualityBridge(self.catalog, self.rubric, queue=self.queue, fetcher=fetcher,
                                          clock=self.clock, source_reader=self.mail.source_evidence,
                                          authority_source=(self.work_authority.authority_source
                                                            if self.work_policy is not None else self.authority_source),
                                          work_authority_policy=self.work_policy)
        holder["q"] = self.quality
        self.model_keys = 0
        def key():
            self.model_keys += 1
            return KEY
        models = ChainModels(load_key=key, gate=ModelGate(ledger=self.ops, cache=self.queue, generation=self.ops_gen),
                             transport=self.models)
        def assess(view, comment):
            out = self.quality.assess(view, comment)
            self.evidence.append(out)
            return out
        self.authorizer = live.ChainAuthorizer(ops=self.ops, ops_generation=self.ops_gen, queue=self.queue,
                                               queue_generation=self.queue_gen, catalog=self.catalog,
                                               mail_writer=self.mail, clock=self.clock)
        return chain.ChainDeps(
            queue=self.queue, catalog=self.catalog, clock=self.clock,
            mail_reader=ChainMailReader(Store(), self.catalog, clock=self.clock.ts,
                                        _service_factory=lambda _: self.mailbox,
                                        _credentials_factory=lambda _: Cred(), _request_factory=lambda: object()),
            interpreter=models.interpreter, classifier=models.classifier, answerer=models.answerer,
            assessor=assess, approval_reader=self.quality.approval_reader, delivery_bridge=self.quality.bridge,
            task_writer_factory=lambda cat, st: BrandTaskWriter(cat, st, token_provider=token,
                                                                session_factory=self.account.session),
            basecamp_get=self.account.get,
            comment_writer=live.ChainCommentWriter(chain.BasecampCommentWriter(
                self.catalog, token_provider=token, session_factory=self.account.session)),
            mail_writer=self.mail, authorize=self.authorizer,
            budget_ok=live.budget_gate(self.ops, 2_000_000), worker_id="hamle-chain-offline",
            intake_authority=(lambda view, ctx: self.work_authority.on_intake(
                view, self.quality.scope(), {**ctx, "work_brief_sha256": self.quality.delivery_brief_sha256(view)}))
            if self.work_policy is not None else None)

    def tick(self, seconds=30):
        self.clock.step(seconds)
        return chain.run_chain_tick(self.deps)

    def record(self):
        work = self.queue.page(kind="work")["items"][0]
        return self.queue.backend.records[chain.Chain._key("chain-", work["work_id"])], work

    def task(self):
        return self.record()[0]["task_id"]

    def say(self, text, role=None, output=None, attachment=None):
        if role:
            self.models.outputs[role].append(output)
        return self.account.add_comment(self.task(), AHMET, "<div>" + text + "</div>", attachment)

    def hamle_comments(self):
        return [c["content"] for c in self.account.comments[self.task()] if c["creator"]["id"] == HAMLE_PERSON]

    def gmail_posts(self):
        return [c for c in self.mailbox.rest_calls if c[0] == "POST"]


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        p = patch.object(inspector, "_isolated", fake_isolated(inspector._isolated))
        p.start(); self.addCleanup(p.stop)

    def start(self, **kw):
        h = Live(**kw)
        h.models.outputs["interpreter"].append(interpretation())
        r = h.tick()
        self.assertEqual(r["status"], "ok", r)
        return h

    def submit(self, h, width, height, text):
        return h.say("Dosya ekte, teslim.", "classifier", {"class": "submission", "evidence": "dosya ekte"},
                     ("afis.png", artwork(width, height, text)))


class EndToEnd(Base):
    def test_mail_task_question_94plus_delivery_with_attachment_then_idle(self):
        h = self.start()
        rec, work = h.record()
        self.assertEqual(rec["status"], "in_production")
        self.assertEqual(len(h.account.posts(f"/todolists/{LIST}/todos.json")), 1)
        self.assertEqual(h.models.calls, ["interpreter"])
        self.assertIn("İç duyuru; müşteri yok.", h.hamle_comments()[0])

        h.say("Teslim tarihi ne zaman?", "classifier", {"class": "question", "evidence": "soru"})
        h.models.outputs["answerer"].append({"answerable": True, "answer": "Teslim 14 Ekim 17:00.",
                                             "grounded_in": ["brief.deadline"]})
        r2 = h.tick()
        self.assertEqual(r2["provider_writes"], 1, r2)
        self.assertIn("Teslim 14 Ekim 17:00.", h.hamle_comments()[-1])

        self.submit(h, 1080, 1080, "Gamechangers YENİ OFİS Kat 3 Pazartesi")
        r3 = h.tick()
        rec, _ = h.record()
        self.assertEqual((rec["status"], rec["quality"]["score"], rec["quality"]["route"]),
                         ("delivered", "100", "delivery_candidate"), (rec, h.authorizer.decisions, r3))
        mid, = h.mailbox.sent
        sent = h.mailbox.parsed(mid)
        self.assertEqual((sent["To"], sent.get("Cc"), sent["In-Reply-To"]),
                         (AHMET_MAIL, None, f"<a1@example.invalid>"))
        self.assertEqual(sent["Subject"], "Re: İç duyuru afişi")
        att = [p for p in sent.iter_attachments()]
        self.assertEqual(len(att), 1)
        self.assertEqual(hashlib.sha256(att[0].get_payload(decode=True)).hexdigest(), self.queue_sha(h))
        self.assertNotIn("Teslim tarihi ne zaman", sent.get_body(("plain",)).get_content())
        kinds = sorted(a["action_kind"] for a in h.queue.page(kind="action", limit=100)["items"])
        self.assertEqual(kinds.count("deliver_file"), 1)
        self.assertTrue(all(a["state"] == "confirmed" for a in h.queue.page(kind="action", limit=100)["items"]))
        self.assertEqual(any(k.startswith("chain-release-") for k in h.queue.backend.records), True)

        before = (list(h.models.calls), len(h.account.calls), len(h.mailbox.rest_calls), h.model_keys)
        r4, r5 = h.tick(), h.tick()
        self.assertEqual((r4["provider_writes"], r5["provider_writes"]), (0, 0))
        self.assertEqual((h.models.calls, h.model_keys), (before[0], before[3]))
        self.assertEqual(len(h.gmail_posts()), 1)

    @staticmethod
    def queue_sha(h):
        rec, _ = h.record()
        sub = h.queue.backend.records["chain-submission-" + __import__("v11_work_queue").digest(
            rec["work_id"], rec["quality"]["comment_id"])]
        return sub["attachment"]["sha256"]

    def test_86_needs_one_of_three_approvers_then_delivers(self):
        h = self.start()
        self.submit(h, 1080, 1080, "Gamechangers YENİ OFİS Kat 3")  # 'Pazartesi' missing
        h.tick()
        rec, work = h.record()
        self.assertEqual((rec["status"], rec["quality"]["score"]), ("awaiting_human_approval", "86.66668"))
        self.assertEqual(h.gmail_posts(), [])
        self.assertIn("Özge, Ahmet veya Cansu", h.hamle_comments()[-1])
        # Text in a comment is NOT an approval: no verified record -> nothing happens.
        h.say("Onaylıyorum", "classifier", {"class": "approval", "evidence": "onay"})
        h.tick()
        self.assertEqual(h.gmail_posts(), [])
        ev = h.evidence[-1]
        record_quality_approval(h.queue, work["work_id"], {
            "verified": True, "revoked": False, "evidence_ref": "SYNTHETIC-approval-ui",
            "actor": "cansu@gamechangers.com.tr", "binding": copy.deepcopy(ev["binding"]),
            "assessment_sha256": scope_digest(ev["assessment"]), "policy_digest": scope_digest(ev["policy"]),
            "score": rec["quality"]["score"], "decision": "accept_quality_for_customer_delivery",
            "issued_at": (h.clock() - timedelta(seconds=10)).isoformat(),
            "expires_at": (h.clock() + timedelta(minutes=10)).isoformat()})
        h.say("Onay kaydı girildi", "classifier", {"class": "approval", "evidence": "onay"})
        h.tick()
        rec, _ = h.record()
        self.assertEqual(rec["status"], "delivered", (rec, h.authorizer.decisions))
        self.assertEqual(len(h.gmail_posts()), 1)

    def test_wrong_size_two_internal_revisions_then_human(self):
        h = self.start()
        for round_ in (1, 2):
            self.submit(h, 1080, 1350, "Gamechangers YENİ OFİS Kat 3 Pazartesi")
            h.tick()
            rec, _ = h.record()
            self.assertEqual((rec["status"], rec["revision_count"]), ("changes_requested", round_), rec)
            self.assertIn(f"İç düzeltme {round_}/2", h.hamle_comments()[-1])
        self.submit(h, 1080, 1350, "Gamechangers YENİ OFİS Kat 3 Pazartesi")
        h.tick()
        rec, _ = h.record()
        self.assertEqual(rec["status"], "human_handoff")
        self.assertEqual(h.gmail_posts(), [])

    def test_draft_rubric_holds_every_submission_and_tags_managers(self):
        h = self.start(rubric=draft_rubric())
        self.submit(h, 1080, 1080, "Gamechangers YENİ OFİS Kat 3 Pazartesi")
        h.tick()
        rec, _ = h.record()
        self.assertEqual((rec["status"], rec["reason"]), ("quality_held", "quality_evidence_missing"))
        self.assertEqual(h.quality.last_reason, "internal_rubric_not_accepted")
        self.assertEqual(h.gmail_posts(), [])
        self.assertIn("insan incelemesi", h.hamle_comments()[-1])

    def test_missing_send_authority_holds_delivery_and_tags_managers(self):
        h = self.start(authority=False)
        self.submit(h, 1080, 1080, "Gamechangers YENİ OFİS Kat 3 Pazartesi")
        h.tick()
        rec, _ = h.record()
        self.assertEqual(rec["status"], "delivery_held", rec)
        self.assertEqual(h.gmail_posts(), [])
        self.assertIn("Teslim, gönderim öncesi kontrollerde durdu", h.hamle_comments()[-1])

    def test_newer_mail_in_thread_blocks_delivery_preflight(self):
        h = self.start()
        self.submit(h, 1080, 1080, "Gamechangers YENİ OFİS Kat 3 Pazartesi")
        late = message("a9", int(h.clock.ts() * 1000), subject="İç duyuru afişi", text="Bir şey daha")
        h.mailbox.rows.append(late)
        h.models.outputs["interpreter"].append(interpretation())
        h.tick()
        self.assertEqual(h.gmail_posts(), [])

    def test_ambiguous_gmail_send_is_held_and_never_resent(self):
        h = self.start()
        self.submit(h, 1080, 1080, "Gamechangers YENİ OFİS Kat 3 Pazartesi")
        original = h.mailbox.request
        def failing(method, url, **kw):
            if method == "POST":
                h.mailbox.rest_calls.append((method, url))
                raise TimeoutError("synthetic")
            return original(method, url, **kw)
        h.mailbox.request = failing
        h.tick()
        h.tick(); h.tick()
        self.assertEqual(len(h.gmail_posts()), 1)
        unknown = [a for a in h.queue.page(kind="action", limit=100)["items"] if a["action_kind"] == "deliver_file"]
        self.assertEqual([a["state"] for a in unknown], ["unknown"])


# ------------------------------------------------------------------ slot runner / authorizer / writer
class Slot(Base):
    def test_slot_runs_one_tick_and_second_claim_in_same_slot_is_refused(self):
        h = Live()
        h.clock.now = datetime.fromisoformat("2026-10-12T09:15:30+03:00")  # chain window starts 09:15
        h.models.outputs["interpreter"].append(interpretation())
        opened = []
        result = live.run_chain_slot(h.ops, open_queue=lambda: opened.append(1) or h.queue,
                                     build_deps=lambda q, og, qg: h.deps, clock=h.clock,
                                     check_work_stops=lambda: {"status": "completed", "grants_created": 0,
                                                               "business_writes": 0})
        self.assertEqual(result["status"], "chain_tick_completed", result)
        self.assertEqual((result["business_writes"], result["model_calls"] >= 1), (2, True))
        again = live.run_chain_slot(h.ops, open_queue=lambda: h.queue, build_deps=lambda q, og, qg: h.deps,
                                    clock=h.clock)
        self.assertEqual(again["status"], "slot_already_claimed")

    def test_global_stop_queue_stop_deadline_and_failed_stop_sweep(self):
        h = Live()
        h.clock.now = datetime.fromisoformat("2026-10-12T09:15:30+03:00")
        never = lambda *a: (_ for _ in ()).throw(AssertionError("must not build"))
        h.queue.set_control("stopped", actor="offline-test", expected_generation=h.queue_gen)
        r = live.run_chain_slot(h.ops, open_queue=lambda: h.queue, build_deps=never, clock=h.clock)
        self.assertEqual((r["status"], r["reason"]), ("stopped", "chain_queue_stopped"))
        h.clock.step(15 * 60)
        r = live.run_chain_slot(h.ops, open_queue=lambda: h.queue, build_deps=never, clock=h.clock,
                                check_work_stops=lambda: {"status": "held"})
        self.assertEqual((r["status"], r["reason"]), ("held", "work_stop_sweep_incomplete"))
        h.ops.set_mode("stopped", "offline-test")
        r = live.run_chain_slot(h.ops, open_queue=never, build_deps=never, clock=h.clock)
        self.assertEqual(r["status"], "stopped")
        late = lambda: datetime.fromisoformat("2026-10-13T09:00:30+03:00")
        r = live.run_chain_slot(h.ops, open_queue=never, build_deps=never, clock=late)
        self.assertEqual((r["status"], r["reason"]), ("held", "authorization_expired"))

    def test_worker_chain_tick_mode_requires_cloud_opt_in_and_default_modes_unchanged(self):
        with patch.dict(os.environ, {}, clear=True), patch("builtins.print") as out:
            code = worker.main(["--mode", "chain_tick"])
        self.assertNotEqual(code, 0)
        self.assertIn("cloud_opt_in_required", str(out.call_args))
        with self.assertRaises(SystemExit):
            with patch("sys.stderr"):
                worker.main(["--mode", "chain"])


class Authorizer(Base):
    def test_denies_on_changed_controls_live_flag_and_out_of_scope_target(self):
        h = Live()
        target = {"account_id": ACC, "project_id": PROJ, "list_id": LIST, "task_id": 1}
        self.assertTrue(h.authorizer("add_comment", {"target": target, "body": "x"}))
        self.assertFalse(h.authorizer("add_comment", {"target": dict(target, project_id=1), "body": "x"}))
        self.assertFalse(h.authorizer("delete_task", {}))
        h.catalog["live_enabled"] = False
        self.assertFalse(h.authorizer("add_comment", {"target": target, "body": "x"}))
        h.catalog["live_enabled"] = True
        h.queue.set_control("stopped", actor="t", expected_generation=h.queue_gen)
        self.assertFalse(h.authorizer("add_comment", {"target": target, "body": "x"}))
        self.assertEqual([d[1] for d in h.authorizer.decisions],
                         ["allowed", "comment_target_out_of_scope", "action_kind_not_allowed",
                          "profile_live_disabled", "chain_queue_changed"])

    def test_comment_writer_normalizes_coordinator_notes(self):
        class Inner:
            def __init__(self):
                self.seen = []
            def write(self, payload):
                self.seen.append(payload)
                return {"remote_id": 1}
            def readback(self, rid, payload):
                return {"readback_sha256": scope_digest(payload)}
        inner = Inner()
        w = live.ChainCommentWriter(inner)
        note = {"target": {"account_id": ACC}, "body": "kalite notu"}
        w.write(note)
        self.assertEqual(inner.seen[0], {**note, "mention_ids": [], "internal_only": True})
        self.assertEqual(w.readback(1, note), {"readback_sha256": scope_digest(note)})


# ------------------------------------------------------------------ Firestore persistence (fake client)
class FakeSnap:
    def __init__(self, key, value):
        self.id, self._value, self.exists = key, copy.deepcopy(value), value is not None
    def to_dict(self):
        return copy.deepcopy(self._value)


class FakeDoc:
    def __init__(self, col, key):
        self.col, self.id = col, key
    def get(self, transaction=None, retry=None, timeout=None):
        return FakeSnap(self.id, self.col.data.get(self.id))


class FakeQuery:
    def __init__(self, col, filters=(), after=None, n=None):
        self.col, self.filters, self.after, self.n = col, list(filters), after, n
    def where(self, filter):
        return FakeQuery(self.col, self.filters + [filter], self.after, self.n)
    def order_by(self, name):
        return self
    def start_after(self, value):
        return FakeQuery(self.col, self.filters, value["__name__"].id, self.n)
    def limit(self, n):
        return FakeQuery(self.col, self.filters, self.after, n)
    def stream(self, retry=None, timeout=None):
        assert retry is None
        rows = [FakeSnap(k, v) for k, v in sorted(self.col.data.items())
                if (self.after is None or k > self.after)
                and all(v.get(f) == val for f, op, val in self.filters)]
        return rows[:self.n]


class FakeCollection(FakeQuery):
    def __init__(self):
        self.data, self.fail_commit = {}, False
        super().__init__(self)
    def document(self, key):
        return FakeDoc(self, key)


class FakeTx:
    def __init__(self, col):
        self.col, self.writes = col, []
    def set(self, doc, value):
        self.writes.append((doc.id, copy.deepcopy(value)))


class FakeClient:
    def __init__(self):
        self.col = FakeCollection()
        self.names = []
    def collection(self, name):
        self.names.append(name)
        return self.col
    def transaction(self, max_attempts):
        assert max_attempts == 1
        return FakeTx(self.col)


class FakeSDK:
    @staticmethod
    def transactional(fn):
        def run(tx):
            result = fn(tx)
            if tx.col.fail_commit:
                raise TimeoutError("synthetic ambiguous commit")
            for key, value in tx.writes:
                tx.col.data[key] = value
            return result
        return run


class Persistence(Base):
    def backend(self):
        client = FakeClient()
        b = live.InjectedFirestoreQueueBackend(client=client, sdk=FakeSDK, field_filter=lambda f, op, v: (f, op, v),
                                               opt_in="v11-queue-explicit")
        return b, client

    def test_queue_and_chain_state_persist_through_firestore_backend(self):
        b, client = self.backend()
        self.assertEqual(client.names, ["hamle_v11_work_queue"])
        clock = Clock()
        q = live.open_cloud_queue(None, clock=clock.ts, backend=b)
        self.assertEqual(q.control()["mode"], "stopped")  # never opened implicitly
        self.assertEqual(live.queue_command(q, "status")["mode"], "stopped")
        with self.assertRaises(live.ChainLiveError):
            live.queue_command(q, "open", actor="x@example.com", expected_generation=0)
        row = live.queue_command(q, "open", actor=AHMET_MAIL, expected_generation=0)
        self.assertEqual(row["mode"], "queue_open")
        q2 = live.open_cloud_queue(None, clock=clock.ts, backend=b)  # re-open never resets control
        self.assertEqual(q2.control()["mode"], "queue_open")

    def test_full_chain_tick_runs_on_firestore_backend(self):
        b, client = self.backend()
        h = Live()
        clock = h.clock
        clock.now = T0 - timedelta(minutes=5)  # open before the synthetic mail, as in Live()
        q = live.open_cloud_queue(None, clock=clock.ts, backend=b)
        gen = q.set_control("queue_open", actor="t", expected_generation=0)["generation"]
        clock.now = T0
        h.queue, h.queue_gen = q, gen
        h.deps = h.build()
        h.models.outputs["interpreter"].append(interpretation())
        r = h.tick()
        self.assertEqual((r["status"], r["provider_writes"]), ("ok", 2), r)
        self.assertTrue(any(v.get("kind") == "chain_work" for v in client.col.data.values()))
        self.assertEqual(live.anchor_lookup(q, "gamechangers")("ab"), "a1")

    def test_ambiguous_commit_is_unknown_never_retried(self):
        b, client = self.backend()
        q = live.open_cloud_queue(None, clock=Clock().ts, backend=b)
        client.col.fail_commit = True
        with self.assertRaises(QueueError) as got:
            q.set_control("queue_open", actor="t", expected_generation=0)
        self.assertEqual(got.exception.code, "store_outcome_unknown")

    def test_backend_requires_explicit_opt_in(self):
        with self.assertRaises(live.ChainLiveError):
            live.InjectedFirestoreQueueBackend(client=FakeClient(), sdk=FakeSDK, field_filter=tuple, opt_in="yes")


class Quality(Base):
    def test_draft_rubric_is_refused_and_accepted_copy_is_100_weighted(self):
        with self.assertRaises(ValueError) as got:
            rubric_policy(draft_rubric(), live_catalog())
        self.assertEqual(got.exception.code, "internal_rubric_not_accepted")
        draft = draft_rubric()
        self.assertEqual((draft["weights_status"], draft["verified"]), ("draft_not_accepted", False))
        policy = rubric_policy(accepted_rubric(), live_catalog())
        self.assertEqual([c["id"] for c in policy["criteria"]],
                         ["file_format", "dimensions", "required_text", "brief_items"])
        self.assertEqual(policy["thresholds"], {"human_min": "80", "candidate_min": "90"})
        self.assertEqual(policy["internal_revision_limit"], 2)

    def test_disk_rubric_is_accepted_but_rights_declaration_still_false(self):
        r = load_rubric()
        self.assertEqual((r["status"], r["weights_status"], r["verified"], r["accepted_by"]),
                         ("accepted", "accepted", True, AHMET_MAIL))
        self.assertFalse(r["rights_declaration"]["accepted"])
        self.assertEqual(rubric_policy(r, live_catalog())["status"], "accepted")

    def test_internal_live_cap_blocks_model_call_before_any_ledger_write(self):
        from v11_chain_models import ChainModelError, chain_committed_microdollars
        h = Live()
        gate = ModelGate(ledger=h.ops, cache=h.queue, generation=h.ops_gen, cap_microdollars=3_000_000)
        calls = []
        gate.once("interpreter", "a" * 64, lambda: calls.append(1) or {"output": {}, "receipt": {
            "response_id": "resp_x", "model": "gpt-6-astra", "reasoning_effort": "max",
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}})
        self.assertEqual(chain_committed_microdollars(h.ops), 2_000_000)
        with self.assertRaises(ChainModelError) as got:
            gate.once("interpreter", "b" * 64, lambda: calls.append(2))
        self.assertEqual(got.exception.code, "internal_live_budget_cap_reached")
        self.assertEqual((calls, chain_committed_microdollars(h.ops)), ([1], 2_000_000))
        self.assertFalse(live.budget_gate(h.ops, 2_000_000, 3_000_000)())
        self.assertTrue(live.budget_gate(h.ops, 2_000_000, 30_000_000)())

    def test_fetcher_refuses_foreign_download_hosts(self):
        h = Live()
        fetcher = BasecampAttachmentFetcher(h.catalog, token_provider=lambda: TOKEN,
                                            session_factory=h.account.session)
        h.account.todos[1] = {"id": 1}
        h.account.comments[1] = [{"id": 5, "type": "Comment", "status": "active", "visible_to_clients": False,
                                  "bucket": {"id": PROJ}, "parent": {"id": 1, "type": "Todo"},
                                  "creator": {"id": AHMET}, "created_at": T0.isoformat(),
                                  "content_attachments": [{"id": 50, "filename": "a.png", "content_type": "image/png",
                                                           "byte_size": 3, "download_url": "https://evil.example/x"}]}]
        att = {"attachment_id": 50, "filename": "a.png", "content_type": "image/png", "byte_size": 3}
        with self.assertRaises(ValueError) as got:
            fetcher.download(1, 5, att)
        self.assertEqual(got.exception.code, "attachment_download_url_invalid")


if __name__ == "__main__":
    unittest.main()


class ManualInternalRun(Base):
    # Outside every scheduled slot (after 18:30). The harness control was opened at T0, so an
    # evening after T0 stands in for "Sunday evening" (same rule: no weekday-window claim).
    SUNDAY = datetime.fromisoformat("2026-10-12T21:07:00+03:00")

    def harness(self):
        h = Live()
        h.clock.now = self.SUNDAY
        h.models.outputs["interpreter"].append(interpretation())
        return h

    def manual(self, h, operator=AHMET_MAIL, catalog=None, **kw):
        return live.run_chain_slot(h.ops, open_queue=lambda: h.queue, build_deps=lambda q, og, qg: h.deps,
                                   clock=h.clock, manual_operator=operator,
                                   manual_catalog=catalog if catalog is not None else h.catalog, **kw)

    def test_chain_window_starts_0915_and_scheduled_review_window_unchanged(self):
        h = Live()
        h.clock.now = datetime.fromisoformat("2026-10-12T09:00:30+03:00")
        r = live.run_chain_slot(h.ops, open_queue=lambda: h.queue, build_deps=lambda q, og, qg: h.deps,
                                clock=h.clock)
        self.assertEqual((r["status"], r["reason"]), ("held", "outside_scheduled_window"))
        # legacy scheduled_review keeps 08:30-18:30
        gen = h.ops.control()["generation"]
        self.assertIsNotNone(h.ops.claim_scheduled_slot(gen))
        h.clock.now = datetime.fromisoformat("2026-10-12T18:30:30+03:00")
        r = live.run_chain_slot(h.ops, open_queue=lambda: h.queue, build_deps=lambda q, og, qg: h.deps,
                                clock=h.clock)
        self.assertNotEqual(r.get("reason"), "outside_scheduled_window")

    def test_scheduled_path_unchanged_outside_hours(self):
        h = self.harness()
        r = live.run_chain_slot(h.ops, open_queue=lambda: h.queue, build_deps=lambda q, og, qg: h.deps,
                                clock=h.clock)
        self.assertEqual((r["status"], r["reason"]), ("held", "outside_scheduled_window"))
        self.assertEqual(h.models.calls, [])

    def test_manual_run_outside_hours_processes_internal_work_with_all_gates(self):
        h = self.harness()
        r = self.manual(h)
        self.assertEqual(r["status"], "chain_tick_completed", r)
        self.assertTrue(r["slot_key"].startswith("manual-chain-20261012T180700-"))
        self.assertEqual(r["business_writes"], 2)  # task + internal note, through the normal chain gates
        row = h.ops.backend.data[r["slot_key"]] if hasattr(h.ops.backend, "data") else None
        if row is not None:
            self.assertEqual((row["state"], row["operator"]), ("finished", AHMET_MAIL))
        # lease released -> a later manual run gets a NEW unique key
        h.clock.step(60)
        r2 = self.manual(h)
        self.assertEqual(r2["status"], "chain_tick_completed", r2)
        self.assertNotEqual(r2["slot_key"], r["slot_key"])

    def test_manual_run_refuses_other_brand_profile_or_work(self):
        h = self.harness()
        wat = copy.deepcopy(h.catalog); wat["brand_id"] = "wat_mobilite"
        r = self.manual(h, catalog=wat)
        self.assertEqual(r["status"], "held")
        self.assertEqual(r["reason"], "manual_run_internal_profile_only")
        h.queue.backend.records["work-wat"] = {"id": "work-wat", "kind": "work", "work_id": "work-wat",
                                               "brand_id": "wat_mobilite", "service_id": "wat_social"}
        r = self.manual(h)
        self.assertEqual((r["status"], r["reason"]), ("held", "manual_run_non_internal_work_present"))
        self.assertEqual(h.models.calls, [])

    def test_manual_run_requires_allowed_operator(self):
        h = self.harness()
        for op in ("", "someone@example.com"):
            r = self.manual(h, operator=op)
            self.assertEqual((r["status"], r["reason"]), ("held", "manual_operator_not_allowed"))
        with patch.dict(os.environ, {}, clear=True), patch("builtins.print") as out:
            self.assertNotEqual(worker.main(["--mode", "chain_tick", "--manual-internal"]), 0)
        self.assertIn("manual_internal_requires_chain_tick_and_operator", str(out.call_args))
        with patch.dict(os.environ, {}, clear=True), patch("builtins.print") as out:
            self.assertNotEqual(worker.main(["--mode", "scheduled_review", "--manual-internal",
                                             "--operator", AHMET_MAIL]), 0)
        self.assertIn("manual_internal_requires_chain_tick_and_operator", str(out.call_args))

    def test_concurrent_manual_runs_are_blocked_by_lease(self):
        h = self.harness()
        gen = h.ops.control()["generation"]
        first = live.claim_manual_run(h.ops, gen, AHMET_MAIL)
        r = self.manual(h, operator="cansu@gamechangers.com.tr")
        self.assertEqual((r["status"], r["reason"]), ("held", "manual_run_in_progress"))
        live.finish_manual_run(h.ops, first, status="held", reason="test")
        self.assertEqual(self.manual(h)["status"], "chain_tick_completed")

    def test_manual_run_keeps_global_stop_deadline_and_cap(self):
        h = self.harness()
        h.ops.set_mode("stopped", "offline-test")
        self.assertEqual(self.manual(h)["status"], "stopped")
        h2 = self.harness()
        h2.clock.now = datetime.fromisoformat("2026-10-13T00:00:01+03:00")
        r = self.manual(h2)
        self.assertEqual((r["status"], r["reason"]), ("held", "authorization_expired"))
        h3 = self.harness()
        h3.deps.budget_ok = live.budget_gate(h3.ops, 2_000_000, 1_000_000)  # cap below one hold
        r = self.manual(h3)
        self.assertEqual(h3.models.calls, [])
