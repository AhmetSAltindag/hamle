"""Hamle v1.1 internal work chain: one 15-minute tick, offline-testable.

Connects existing modules without changing them:
  verified mail -> interpreter -> v11_intake_plan.plan_intake
    -> clarification reply (same thread, original sender only)  or
    -> v11_work_queue bind/enqueue/claim -> v11_production_stages
    -> v11_brand_task.create_stage_task (profile route/producer, overseers subscribed)
  each tick: BasecampReader -> PollJournal -> queue events -> classifier
    question   -> record_producer_question -> grounded answer comment or manager tag
    submission -> v11_quality_gate.evaluate_quality
                  90+    -> DeliveryCoordinator (same mail thread, recipient/CC/stop checks)
                  80-89  -> manager approval tag, wait
                  <80    -> internal revision comment, max N rounds, then human handoff

Every provider write is a durable chain intent, then queue.begin_write, ONE provider
attempt, exact readback and queue.confirm_write. Anything uncertain is
queue.hold_unknown and never retried. All providers, models and the clock are
injected; nothing here imports a network client or reads credentials. Not wired
into worker.py; run_chain_tick() is a separate, non-default entry point.
"""
from __future__ import annotations

import copy
import html
import re
from datetime import datetime

from autonomy_policy import HAMLE, scope_digest
import business_writers as bw
import cloud_providers as provider
from v11_basecamp_reader import BasecampReader, ReadError, digest as record_digest
from v11_brand_task import HAMLE_PERSON, create_stage_task, prepare_stage_task
from v11_chain_mail import digest_reply, reply_subject, shadow_forward
from v11_delivery import DeliveryCoordinator, quality_feedback, record_outcome
from v11_intake_plan import IntakeError, _contacts, _email, intake_requirements_digest, plan_intake, \
    record_producer_question
from v11_poll_journal import PollJournal, checkpoint_digest
from v11_production_stages import initialize_production, ready_stages
from v11_quality_gate import evaluate_quality
from v11_work_queue import QueueError, WRITE_BINDING_FIELDS, digest
import v11_brand_routing as routing

MANAGER_ROLES = ("social_manager", "principal", "strategy_owner")
CLASSES = ("question", "submission", "approval", "other")
MAX_EVENTS_PER_WORK = 25
# A manager's Basecamp comment is for Hamle only when it names Hamle (plain text or @mention).
ADDRESSED = re.compile(r"(?<![0-9a-zçğıöşü])hamle(?![0-9a-zçğıöşü])", re.IGNORECASE)
# Work status / next step in the team's words (manager answers and held-work notes).
STATUS_TR = {
    "new": "talep yeni geldi, görev henüz açılmadı",
    "awaiting_clarification": "talep sahibinden eksik bilgileri bekliyorum",
    "in_production": "üretimde",
    "awaiting_internal_clarification": "üreticinin sorusu cevap bekliyor",
    "changes_requested": "iç düzeltme istendi, yeni dosya bekleniyor",
    "awaiting_human_approval": "kalite puanı 80-89, yönetici onayı bekleniyor",
    "quality_held": "kalite kontrolünü tamamlayamadım, iş ekipte",
    "delivery_held": "teslim, gönderim öncesi kontrollerde durdu, iş ekipte",
    "delivered": "teslim edildi",
    "human_handoff": "iç düzeltme turları doldu, iş ekibe devredildi",
    "held": "bir kontrolde durdum, iş ekipte",
    "write_outcome_unknown": "bir yazma işleminin sonucunu doğrulayamadım, iş ekipte",
}
NEXT_TR = {
    "new": "talep maili okununca görevi açacağım",
    "awaiting_clarification": "talep sahibi eksikleri yanıtlayınca görevi açacağım",
    "in_production": "üretici dosyayı bu görevin altına yükleyip teslim ettiğini yazınca puanlayacağım",
    "awaiting_internal_clarification": "üreticinin sorusu yanıtlanınca üretim devam edecek",
    "changes_requested": "üretici düzeltilmiş dosyayı yükleyince yeniden puanlayacağım",
    "awaiting_human_approval": "Özge, Ahmet veya Cansu bu görevin altına onay yazınca teslim adımına geçeceğim",
    "delivered": "bu işte benim yapacağım bir adım kalmadı",
}
HELD_NEXT = ("bu işte benim otomatik adımım yok; kontrol, onay ve talep sahibine teslim ekipte. "
             "İşi yeniden bana devretmek için Ahmet'in onayı gerekiyor.")
REASON_TR = {"quality_evidence_missing": "teslim edilen dosyayı göremediğim için puan veremedim"}
# plan_intake hold reasons for non-routine requests -> (why, who decides with Hamle).
NON_ROUTINE = {
    "ozge_collaboration_required": ("Bu bir sosyal medya fikir ve planlama talebi.", ("Özge", "Cansu")),
    "cansu_and_ahmet_decision_required": ("Bu talep strateji kararı içeriyor.", ("Cansu", "Ahmet")),
    "ahmet_commercial_decision_required": ("Bu talep fiyat, teklif veya bütçe içeriyor.", ("Ahmet",)),
    "risk_classification_missing": ("Talebin türünü net anlayamadım.", ("Cansu", "Özge")),
}
NON_ROUTINE_QUESTIONS = ("Hangi marka veya proje için düşünüyoruz?",
                         "Yayın dönemi veya teslim tarihi ne olmalı?",
                         "Bütçe, kanal veya format açısından bir sınır var mı?")
HELD_LOOKBACK_SECONDS = 3 * 24 * 3600  # older comments on a held work are not answered
HELD_LISTEN_SECONDS = 30 * 24 * 3600  # a work held longer than this is no longer read


class ChainError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ChainError(code)


def _plain(value, limit=4000):
    """Untrusted Basecamp HTML -> bounded plain text (never rendered or executed)."""
    text = re.sub(r"<[^>]*>", " ", value if isinstance(value, str) else "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()[:limit]


def _timestamp(value):
    """Basecamp ISO time -> epoch seconds, or None."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


CONVERSATION_MAX, CONVERSATION_CHARS = 6, 2000  # remembered request/question turns per work


def _unquoted(text, limit=CONVERSATION_CHARS):
    return "\n".join(l for l in str(text or "").splitlines() if not l.lstrip().startswith(">")).strip()[:limit]


def interpreter_text(source, rec):
    """Mail text for the interpreter. The Gmail reader hands over only the latest mail body, so an
    answer to Hamle's questions is read together with the request and questions remembered in the
    chain record (rec['conversation']); details given once are not lost and asked again."""
    text = source.get("text", "")
    turns = rec.get("conversation") or []
    if rec.get("status") != "awaiting_clarification" or not turns:
        return text
    parts = []
    for i, m in enumerate(turns):
        label = "Hamle'nin soruları" if m.get("from") == HAMLE else ("İlk talep" if i == 0 else "Önceki yanıt")
        parts.append(f"[{label} · {m.get('from')}]\n{m.get('text', '')}")
    parts.append(f"[Son yanıt · {source.get('sender')}]\n{text}")
    return "\n\n".join(parts)


DEFAULT_FORMATS = {"graphic": ["image/jpeg"], "animation": ["video/mp4"], "video": ["video/mp4"], "text": ["text/plain"]}
FORMAT_LABELS = {"image/jpeg": "JPG", "image/png": "PNG", "application/pdf": "PDF", "video/mp4": "MP4",
                 "text/plain": "metin"}
KIND_LABELS = {"graphic": "statik tasarım", "animation": "animasyon", "video": "video", "text": "metin"}


def deliverable_label(item, limit=80):
    """A deliverable in the requester's words: its specification, else its id without code marks."""
    spec = re.sub(r"\s+", " ", str(item.get("specification") or "")).strip()
    if spec:
        return spec if len(spec) <= limit else spec[:limit].rsplit(" ", 1)[0] + "…"
    return re.sub(r"[_-]+", " ", str(item.get("id") or "çıktı")).strip()


def readable_questions(questions, requirements):
    """plan_intake questions with deliverable ids ('linkedin_post: ...') replaced by readable labels."""
    items = {str(i.get("id")): i for i in requirements.get("deliverables") or [] if isinstance(i, dict)}
    out = []
    for q in questions:
        text, field = str(q["question"]), str(q.get("field") or "")
        ident = field.split(".")[1] if field.startswith("deliverables.") and field.count(".") >= 2 else None
        if ident in items and text.startswith(ident + ": "):
            text = deliverable_label(items[ident]) + " için: " + text[len(ident) + 2:]
        out.append(text)
    return out


def understanding(requirements):
    """One plain sentence of what Hamle understood (no bullets: questions stay the only list)."""
    title, objective = (str(requirements.get(k) or "").strip() for k in ("title", "objective"))
    objective = objective.rstrip(".")
    parts = [title + (": " + objective if objective and objective != title else "")]
    outs = []
    for item in requirements.get("deliverables") or []:
        if not isinstance(item, dict):
            continue
        qty = item.get("quantity") if isinstance(item.get("quantity"), int) else None
        outs.append((f"{qty} × " if qty else "") + deliverable_label(item, 120))
    if outs:
        parts.append(", ".join(outs))
    if isinstance(requirements.get("deadline"), str) and requirements["deadline"].strip():
        parts.append("teslim: " + requirements["deadline"].strip())
    text = "; ".join(p for p in parts if p).rstrip(".")
    return ("Anladığım kadarıyla: " + text[:700] + ".") if text else ""


PRAISE = re.compile(r"beğendik|elinize sağlık|eline sağlık|harika olmuş|çok güzel olmuş|bayıldık|süper olmuş",
                    re.IGNORECASE)


def thanks_line(source, plural=False):
    """A requester who praised the work hears it back (the team gets told too)."""
    if not PRAISE.search(_unquoted(source.get("text"), 4000)):
        return ""
    return "Güzel geri bildiriminiz için teşekkürler, ekibe ileteceğim." if plural else \
        "Güzel geri bildirimin için teşekkürler, ekibe ileteceğim."


def fill_internal_defaults(requirements, source, *, final):
    """A verified agency request is never stalled on details the team can settle itself.

    Non-blocking gaps (title, objective, audience, brand files, count, format, spec) are filled
    with plainly labelled assumptions. Blocking gaps (deadline, what to produce) are asked once;
    with final=True (the requester already answered one round) they are filled too, so the task
    opens and the team settles the rest. Returns the assumptions, in Turkish, for the
    confirmation mail. Never touches agreement or routing fields."""
    notes = []
    def put(key, value, note):
        if not (isinstance(requirements.get(key), str) and requirements[key].strip()):
            requirements[key] = value
            notes.append(note)
    subject = reply_subject(source.get("subject"))[4:].strip()
    put("title", (subject or "İç talep")[:120], "İş adı mail konusundan alındı.")
    title = requirements["title"]
    put("objective", title, "Amaç ayrıca yazılmadığı için iş adından alındı.")
    put("audience", "Markanın mevcut hedef kitlesi (talepte belirtilmedi)",
        "Hedef kitle belirtilmedi; markanın mevcut kitlesi varsayıldı.")
    put("assets_ref", "Ekipteki güncel onaylı marka dosyaları" + (" ve talep mailindeki ekler"
                                                                 if source.get("has_attachments") else ""),
        "Marka dosyası olarak ekipteki güncel onaylı dosyalar kullanılacak.")
    items = requirements.get("deliverables")
    if final and not (isinstance(items, list) and items):
        requirements["deliverables"] = items = [{"id": "tasarim", "kind": "graphic"}]
        notes.append("Çıktı türü yazılmadığı için statik tasarım varsayıldı.")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if item.get("kind") not in DEFAULT_FORMATS:  # what to produce is known; its kind the team settles
            item["kind"] = "graphic"
            notes.append(f"{deliverable_label(item)}: tür belirtilmedi, statik tasarım varsayıldı.")
        if not (isinstance(item.get("quantity"), int) and item["quantity"] > 0):
            item["quantity"] = 1
            notes.append(f"{deliverable_label(item)}: adet yazılmadı, 1 adet varsayıldı.")
        if not (isinstance(item.get("formats"), list) and item["formats"]):
            item["formats"] = list(DEFAULT_FORMATS[item["kind"]])
            notes.append(f"{deliverable_label(item)}: format yazılmadı, "
                         f"{FORMAT_LABELS.get(item['formats'][0], item['formats'][0])} varsayıldı.")
        if not (isinstance(item.get("specification"), str) and item["specification"].strip()):
            item["specification"] = title
        if item["kind"] in ("animation", "video") and not item.get("duration_seconds") and final:
            item["duration_seconds"] = 15
            notes.append(f"{deliverable_label(item)}: süre yazılmadı, 15 saniye varsayıldı.")
    if final:
        put("deadline", "Belirtilmedi (ekip talep sahibiyle netleştirecek)",
            "Teslim tarihi yazılmadı; ekip talep sahibiyle netleştirecek.")
    return notes


def remember_turns(rec, *turns):
    rows = (rec.get("conversation") or []) + [{"from": f, "text": _unquoted(t)} for f, t in turns if _unquoted(t)]
    rec["conversation"] = rows[-CONVERSATION_MAX:]


class ChainDeps:
    """All side effects are injected.

    mail_reader() -> list of verified latest-message sources (plan_intake source
      fields + subject, text, source_order).
    interpreter(text, context) -> {'requirements': {...}, 'follow_ups': [str]}
    classifier(text, context) -> one of CLASSES
    answerer(text, context) -> {'answer': str, 'grounded_in': ['brief.x'|'profile.y']} or None
    assessor(view, comment) -> {'policy','assessment','binding','safety'} or None
    approval_reader(view, comment) -> exact verified human_approval record or None
    task_writer_factory(catalog, state) -> BrandTaskWriter
    basecamp_get(url, conditional_headers) -> (status, payload, headers)  (SessionGet)
    comment_writer / mail_writer: write(payload) -> {'remote_id'};
      readback(remote_id, payload) -> {'readback_sha256'}
    delivery_bridge(view) -> (delivery_work, DeliveryAdapter)
    authorize(action_kind, payload) -> True only after fresh stop/To-CC/budget checks
    budget_ok() -> False skips every model call for this tick
    intake_authority(view, context) -> optional; derives send authority right after the task exists
    clock() -> timezone-aware datetime
    """
    REQUIRED = ("queue", "catalog", "clock", "mail_reader", "interpreter", "classifier", "answerer",
                "assessor", "task_writer_factory", "basecamp_get", "comment_writer", "mail_writer",
                "delivery_bridge", "authorize")

    def __init__(self, **kw):
        missing = [k for k in self.REQUIRED if kw.get(k) is None]
        _need(not missing, "chain_dependency_missing")
        kw.setdefault("budget_ok", lambda: True)
        kw.setdefault("approval_reader", lambda view, comment: None)
        kw.setdefault("worker_id", "hamle-chain")
        kw.setdefault("stage_event_actor_id", None)
        kw.setdefault("intake_authority", None)
        self.__dict__.update(kw)


class Chain:
    def __init__(self, deps):
        self.d = deps
        self.q = deps.queue
        self.journal = PollJournal(self.q)
        self.catalog = copy.deepcopy(deps.catalog)
        people = self.catalog.get("basecamp_people", {})
        self.names = {int(k): v.get("name", k) for k, v in people.items()}
        self.managers = sorted(int(k) for k, v in people.items()
                               if set(v.get("agency_roles", [])) & set(MANAGER_ROLES))
        chain = self.catalog.get("chain", {})
        self.approvers = list(chain.get("quality_approver_basecamp_ids", self.managers))
        self.base_managers, self.base_approvers = list(self.managers), list(self.approvers)
        self.route_key = None
        self.cat = routing.internal_catalog(self.catalog)

    def _use(self, work):
        """Per-work derived catalog (internal or one usable brand route) + its managers."""
        key, cat = routing.work_catalog(self.catalog, work)
        self.cat, self.route_key = cat, key
        self.managers = routing.managers(cat) if key else list(self.base_managers)
        self.approvers = (list(cat["chain"]["quality_approver_basecamp_ids"]) if key
                          else list(self.base_approvers))
        return cat

    def _detect(self, source):
        if not routing.enabled(self.catalog):
            return {"status": "disabled", "brand_key": None, "brand_name": None, "excluded": False, "matches": []}
        return routing.detect_brand(self.catalog, source.get("subject"), source.get("text"))

    def _binding_for(self, source):
        """(project_id, list_id): an already bound thread keeps its binding; otherwise exactly one
        usable brand -> that brand project's social list, anything else -> internal route."""
        bound = self.q.thread_work(source["thread_id"])
        if bound is not None:
            return bound["project_id"], bound["list_id"]
        det = self._detect(source)
        key = det["brand_key"] if det["status"] == "single" and not det["excluded"] else None
        if key and routing.usable(self.catalog, key):
            route = self.catalog["brand_routes"][key]
            return route["project_id"], route["list_id"]
        return self.catalog["basecamp"]["project_id"], self._default_route()["list_id"]

    # ---------- durable chain records (same backend, own record kinds) ----------
    @staticmethod
    def _key(prefix, work_id):
        return prefix + digest(work_id)

    def _load(self, prefix, work_id):
        return self.q._run(lambda tx, _: tx.get(self._key(prefix, work_id)))

    def _save(self, prefix, row):
        key = self._key(prefix, row["work_id"])
        def op(tx, now):
            row["updated_at"] = now
            tx.put(key, row)
            return row
        return self.q._run(op)

    def _new_record(self, work):
        return {"id": self._key("chain-", work["work_id"]), "kind": "chain_work", "work_id": work["work_id"],
                "brand_id": work["brand_id"], "thread_id": work["thread_id"], "status": "new", "reason": None,
                "title": None, "reply": None, "task_id": None, "stage_id": None, "assignee_ids": [],
                "questions": {}, "revision_count": 0, "quality": None, "delivery_state": None,
                "intents": {}, "pending_changes": {}, "history": []}

    def _note(self, rec, text):
        rec["history"] = (rec["history"] + [text])[-20:]

    # ---------- tick ----------
    def tick(self):
        now = self.d.clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "clock_timezone_required")
        report = {"status": "ok", "reason": None, "provider_writes": 0, "model_calls": 0,
                  "ignored_mail": [], "works": {}}
        self._report, self._now, self._tasks = report, now, {}
        control = self.q.control()
        if control["mode"] != "queue_open":
            report.update(status="stopped", reason="global_" + control["mode"])
            return report
        if not (self.catalog.get("live_enabled") is True
                and self.catalog.get("provider_mutation_allowed_by_this_file") is True):
            report.update(status="held", reason="brand_profile_live_disabled")
            return report
        self.gen = control["generation"]
        self._opened_ms = int(control["changed_at"] * 1000)
        sources = {}
        # Read mail first, then take the tick time: the reader stamps observed_at with its own
        # clock, and intake freshness requires observed_at <= now.
        mail = list(self.d.mail_reader() or [])
        now = self.d.clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "clock_timezone_required")
        self._now = now
        for source in mail:
            self._ingest(source, sources)
        model_ok = self.d.budget_ok() is True
        if not model_ok:
            report["reason"] = "model_budget_unavailable"
        cursor = None
        while True:
            page = self.q.page(kind="work", limit=100, cursor=cursor)
            for work in page["items"]:
                if work["brand_id"] == self.catalog["brand_id"]:
                    try:
                        report["works"][work["work_id"]] = self._run_work(work, sources, model_ok)
                    except (ValueError, KeyError, TypeError, AttributeError) as exc:  # isolate one work
                        report["works"][work["work_id"]] = {"events": 0, "status": "error:" + str(
                            getattr(exc, "code", None) or type(exc).__name__)[:150]}
            cursor = page["next_cursor"]
            if cursor is None:
                break
        return report

    def _ingest(self, source, sources):
        try:
            contacts = _contacts(self.catalog)
            _email(source, contacts, self._now)  # Hamle in real To/CC, allowed sender, fresh, not own/auto.
            sender = contacts[source["sender"]]
            _need(sender.get("side") == "agency" and sender.get("can_request") is True, "sender_not_agency_requester")
            _need(type(source.get("source_order")) is int and source["source_order"] > 0, "source_order_missing")
            received = source.get("received_at_ms")
            _need(received is None or (type(received) is int and received >= self._opened_ms),
                  "mail_before_queue_open")  # never pick up mail older than the current queue opening
            _need(not digest_reply(source.get("subject")), "digest_reply_not_a_request")
            _need(not shadow_forward(source.get("subject")), "shadow_forward_not_a_request")
            route = self._default_route()
            project_id, list_id = self._binding_for(source)
            _need(routing.allowed_target(self.catalog, project_id, list_id), "route_target_not_allowed")
            work = self.q.bind_work(brand_id=self.catalog["brand_id"], thread_id=source["thread_id"],
                                    account_id=self.catalog["basecamp"]["account_id"],
                                    project_id=project_id, list_id=list_id,
                                    service_id=route["service_id"], expected_generation=self.gen)
            self.q.enqueue_message(work["work_id"], message_id=source["message_id"],
                                   fingerprint=source["fingerprint"], source_order=source["source_order"],
                                   expected_generation=self.gen)
            sources[source["message_id"]] = source
        except (IntakeError, ChainError, QueueError, KeyError, TypeError, ValueError) as exc:
            self._report["ignored_mail"].append({"message_id": str(source.get("message_id"))[:200]
                                                 if isinstance(source, dict) else None,
                                                 "reason": getattr(exc, "code", None) or str(exc)})

    def _default_route(self):
        chain = self.catalog.get("chain", {})
        routes = [r for r in self.catalog.get("routes", [])
                  if r.get("service_id") == chain.get("default_service_id")
                  and r.get("category_key") == chain.get("default_category_key")]
        _need(len(routes) == 1 and type(routes[0].get("list_id")) is int, "default_route_unconfirmed")
        return routes[0]

    def _run_work(self, work, sources, model_ok):
        out = {"events": 0, "status": None}
        if work["mode"] != "active":
            out["status"] = "work_" + work["mode"]
            if work["mode"] == "takeover" and work.get("task_id"):
                try:
                    note = self._held_listen(work)
                except (ReadError, QueueError, ChainError) as exc:
                    note = "held_note_error:" + str(getattr(exc, "code", None) or exc)[:100]
                if note:
                    out["held_note"] = note
            return out
        comments = {}
        if work.get("task_id"):
            try:
                comments = self._poll(work)
            except (ReadError, QueueError, ChainError) as exc:
                out["status"] = "poll_held:" + (getattr(exc, "code", None) or str(exc))
                return out
        for _ in range(MAX_EVENTS_PER_WORK):
            head = self._head(work["work_id"])
            if head is None:
                break
            if head["source_kind"] == "gmail" and head["source_id"] not in sources:
                out["status"] = "waiting_for_mail_reread"
                break
            if not model_ok:
                out["status"] = "waiting_for_model_budget"
                break
            try:
                claim = self.q.claim_next(work["work_id"], worker_id=self.d.worker_id, expected_generation=self.gen)
            except QueueError as exc:
                out["status"] = "queue_held:" + exc.code
                break
            if claim is None:
                break
            done = self._handle(claim, sources, comments)
            out["events"] += 1
            if done is None:  # unresolved write: work stays blocked, no retry
                out["status"] = "write_outcome_unknown"
                break
            outcome, ref = done
            try:
                self.q.complete_event(claim, outcome=outcome, evidence_ref=ref)
            except QueueError as exc:  # e.g. takeover/stop landed mid-event: leave it for a human
                out["status"] = "queue_held:" + exc.code
                break
            if outcome == "held_for_human":
                break
        rec = self._load("chain-", work["work_id"])
        out["status"] = out["status"] or (rec or {}).get("status")
        return out

    def _head(self, work_id):
        def op(tx, _):
            w = self.q._work(tx, work_id)
            if w["head_sequence"] >= w["next_sequence"]:
                return None
            return tx.get(self.q._event_key(work_id, w["head_sequence"]))
        return self.q._run(op)

    # ---------- Basecamp observation -> journal -> queue ----------
    def _poll(self, work):
        rec = self._load("chain-", work["work_id"]) or self._new_record(work)
        def remember(batch):
            for change in (batch or {}).get("changes", []):
                rec["pending_changes"][change["event_id"]] = {k: change[k] for k in ("kind", "record_id", "record_sha256")}
        pending = self.journal.pending(work["work_id"])
        if pending is not None:  # crash recovery: drain the frozen batch first, never re-poll over it
            remember(pending)
            self._save("chain-", rec)
            self.journal.drain(work["work_id"], expected_generation=self.gen, max_events=100)
        previous = self.journal.checkpoint(work["work_id"])
        reader = BasecampReader(self.d.basecamp_get)
        observation = reader.poll({k: work[k] for k in ("work_id", "account_id", "project_id", "list_id", "task_id")},
                                  previous=previous, observed_at=self._now)
        batch = self.journal.stage(observation, expected_checkpoint_digest=checkpoint_digest(previous),
                                   expected_generation=self.gen)
        remember(batch)
        self._save("chain-", rec)
        self.journal.drain(work["work_id"], expected_generation=self.gen, max_events=100)
        self._tasks[work["work_id"]] = observation["task"]
        return {str(c["id"]): c for c in observation["comments"]}

    # ---------- event dispatch ----------
    def _handle(self, claim, sources, comments):
        work = self.q.get_work(claim["work_id"])
        rec = self._load("chain-", work["work_id"]) or self._new_record(work)
        event = claim["event"]
        try:
            self._use(work)
            if event["source_kind"] == "gmail":
                return self._on_mail(claim, work, rec, sources[event["source_id"]])
            return self._on_basecamp(claim, work, rec, comments)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:  # incl. BrandTask/Production errors
            rec = self._load("chain-", work["work_id"]) or rec  # keep durable intents written so far
            code = getattr(exc, "code", None) or (str(exc) if isinstance(exc, (IntakeError, ValueError))
                                                   and not isinstance(exc, KeyError) else type(exc).__name__)
            rec.update(status="held", reason=str(code)[:150])
            self._save("chain-", rec)
            if self.q.get_work(work["work_id"])["inflight_action_id"]:
                return None
            return "held_for_human", "chain-held:" + rec["reason"][:150]

    def _model(self, fn, *args):
        self._report["model_calls"] += 1
        return fn(*args)

    # ---------- mail ----------
    def _past_works(self, work):
        rows = self.q.backend.scan(kind="chain_work", filters={"brand_id": work["brand_id"]}, limit=50)
        return [{"title": r.get("title"), "status": r.get("status"), "task_id": r.get("task_id"),
                 "same_thread": r.get("thread_id") == work["thread_id"]}
                for r in rows if r["work_id"] != work["work_id"] and r.get("title")][:20]

    def _on_mail(self, claim, work, rec, source):
        if rec["status"] not in ("new", "awaiting_clarification"):
            # Follow-up mail on a running work: brief changes need amend_brief + human review.
            if rec.get("task_id"):
                body = self._tag(self.managers, "Bu iş zincirine yeni bir mail geldi. Brief değişikliği olabilir; "
                                 "lütfen inceleyin (Hamle brief'i kendiliğinden değiştirmez).")
                if self._comment(claim, work, rec, "followup-mail", body, self.managers) is None:
                    return None
            return "no_action", "followup-mail-flagged"
        past = self._past_works(work)
        result = self._model(self.d.interpreter, interpreter_text(source, rec),
                             {"subject": source.get("subject"), "brand_id": work["brand_id"], "past_works": past,
                              "status": rec["status"]}) or {}
        requirements = copy.deepcopy(result.get("requirements") or {})
        chain = self.catalog.get("chain", {})
        requirements.setdefault("brand_id", self.catalog["brand_id"])
        requirements.setdefault("service_id", chain.get("default_service_id"))
        requirements.setdefault("category_key", chain.get("default_category_key"))
        requirements.pop("agreement", None)  # never accept an agreement claimed by model output
        work, routing_note, mention = self._route_requirements(claim, work, source, requirements)
        # One question round at most: after an answer, remaining gaps become labelled assumptions.
        rec["assumptions"] = fill_internal_defaults(requirements, source,
                                                    final=rec["status"] == "awaiting_clarification")
        rec["open_points"] = [str(q).strip()[:300] for q in (result.get("missing_questions") or [])
                              if isinstance(q, str) and q.strip()][:3]
        rule = chain.get("agency_direct_request_counts_as_agreement", {})
        if rule.get("value") is True and rule.get("verified") is True:
            requirements["agreement"] = {"verified": True, "source_message_id": source["message_id"],
                                         "evidence_ref": source["evidence_ref"],
                                         "requirements_sha256": intake_requirements_digest(requirements)}
        plan = plan_intake(source, requirements, self.cat, now=self._now)
        rec["title"] = requirements.get("title") if isinstance(requirements.get("title"), str) else rec["title"]
        if plan["status"] == "needs_clarification":
            intent = plan["intents"][0]["payload"]
            first = next((str(c.get("name") or "").split(" ")[0] for c in self.catalog.get("contacts") or []
                          if isinstance(c, dict) and c.get("email") == source["sender"]), "")
            lines = [f"Merhaba {first}," if first else "Merhaba,", ""]
            seen = " ".join(x for x in (thanks_line(source), understanding(requirements)) if x)
            if seen:
                lines += [seen, ""]
            lines += ["İşe başlayabilmem için şu bilgilere ihtiyacım var:"]
            lines += ["- " + q for q in readable_questions(plan["questions"], requirements)]
            # Model-suggested questions are not asked: they go to the team as open points.
            lines += ["", "Diğer detayları ben tamamlıyorum; görevi açınca onay mailinde hepsini göreceksin.",
                      "", "Teşekkürler,", "Hamle"]
            payload = {"from": HAMLE, "to": [source["sender"]], "cc": [], "thread_id": intent["thread_id"],
                       "in_reply_to_message_id": intent["in_reply_to_message_id"],
                       "subject": reply_subject(source.get("subject")), "body": "\n".join(lines)}
            remember_turns(rec, (source["sender"], source.get("text")), (HAMLE, "\n".join(lines[2:-5])))
            rec.update(status="awaiting_clarification", reason=plan["reason"])
            state = self._write(claim, work, rec, "clarify-" + source["message_id"][:150], "clarify_brief",
                                "gmail", payload, self.d.mail_writer)
            return None if state == "unknown" else ("processed", "clarification:" + state)
        if plan["status"] == "held" and plan["reason"] in NON_ROUTINE:
            return self._non_routine(claim, work, rec, source, requirements, plan["reason"])
        _need(plan["status"] == "ready_internal_task", "intake_" + str(plan["reason"]))
        brief = plan["brief"]
        _need(brief["binding"]["list_id"] == work["list_id"] and brief["service_id"] == work["service_id"],
              "intake_route_differs_from_bound_work")
        state = initialize_production(work["work_id"], brief)
        stage_id = ready_stages(state)[0]["id"]
        rec.update(status="creating_task", reason=None, title=brief["title"], stage_id=stage_id,
                   reply={k: brief["reply_envelope"][k] for k in ("thread_id", "to", "cc", "in_reply_to_message_id")},
                   assignee_ids=state["stages"][stage_id]["spec"]["assignee_ids"],
                   brief_sha256=state["brief_sha256"])
        intake_work = {"work_id": work["work_id"], "task_id": None, "brief": brief, "status": "awaiting_production"}
        prod = {"id": self._key("chain-prod-", work["work_id"]), "kind": "chain_production",
                "work_id": work["work_id"], "state": state, "intake_work": intake_work}
        self._save("chain-prod-", prod)
        _need(routing.allowed_target(self.catalog, work["project_id"], work["list_id"]), "route_target_not_allowed")
        prepared = prepare_stage_task(state, self.cat, stage_id, work, claim)
        key = prepared["action_key"]
        rec["intents"][key] = {"kind": "create_task", "provider": "basecamp", "status": "prepared",
                               "payload_sha256": prepared["payload_sha256"], "event_id": claim["event_id"]}
        self._save("chain-", rec)
        writer = self.d.task_writer_factory(copy.deepcopy(self.cat), copy.deepcopy(state))
        created = create_stage_task(self.q, claim, state, self.cat, stage_id, writer,
                                    authorize=lambda p: self.d.authorize("create_task", p) is True)
        self._report["provider_writes"] += created["network_writes"]
        if created["status"] != "task_created_readback_verified":
            status = "unknown" if created["network_writes"] else "denied"
            rec["intents"][key]["status"] = status
            rec.update(status="held", reason=created["reason"])
            self._save("chain-", rec)
            return None if status == "unknown" else ("held_for_human", "create-task:" + str(created["reason"])[:150])
        task = created["stage_task"]
        rec["intents"][key].update(status="confirmed", remote_id=str(task["task_id"]))
        rec.update(status="in_production", task_id=task["task_id"])
        intake_work["task_id"] = task["task_id"]
        prod["intake_work"] = intake_work
        prod["stage_task"] = task  # ready for a bind_stage_task event once its overseer actor is decided
        self._save("chain-prod-", prod)
        self._save("chain-", rec)
        if self.d.intake_authority is not None:
            # Send authority is derived once, at intake, from the accepted brand policy
            # (no grant -> later delivery is held). Never blocks the task itself.
            try:
                rec["authority"] = self.d.intake_authority(self._view(rec), {"risk_area": requirements.get("risk_area")})
            except Exception:
                rec["authority"] = {"status": "not_granted", "reason": "intake_authority_failed"}
            self._save("chain-", rec)
        work = self.q.get_work(work["work_id"])
        note = self._context_note(rec, past, result.get("follow_ups"), result.get("context_notes"),
                                  rec.get("open_points"))
        if routing_note:
            note = (self._tag([mention], routing_note) if mention else routing_note) + "\n\n" + note
        if self._comment(claim, work, rec, "context-note", note, [mention] if mention else []) is None:
            return None
        if self._ack_enabled():
            payload = self._ack_payload(source, rec, brief, task)
            if self._write(claim, work, rec, "task-ack", "acknowledge", "gmail", payload, self.d.mail_writer) == "unknown":
                return None  # ambiguous send: work blocked, never resent
        return "processed", "basecamp-readback:" + str(task["task_id"])

    def _non_routine(self, claim, work, rec, source, requirements, reason):
        """Idea, planning, strategy or price requests never go silent: the requester gets smart
        questions once (round 1), then one closing line (round 2) and the work goes to the team.
        No task is opened by Hamle for these; plan_intake keeps them out of the automatic flow."""
        why, who = NON_ROUTINE[reason]
        first = next((str(c.get("name") or "").split(" ")[0] for c in self.catalog.get("contacts") or []
                      if isinstance(c, dict) and c.get("email") == source["sender"]), "")
        people = [n for n in who if n != first]
        team = (" ve ".join([", ".join(people[:-1]), people[-1]]) if len(people) > 1 else people[0]) if people else "ekip"
        final = rec["status"] == "awaiting_clarification"
        lines = [f"Merhaba {first}," if first else "Merhaba,", ""]
        if not final:
            asks = list(rec.get("open_points") or [])[:3] or list(NON_ROUTINE_QUESTIONS)
            lines += ["Talebini aldım. " + why + " Bu yüzden görevi tek başıma açmıyorum; " + team
                      + " ile birlikte ele alacağız.", "", "İyi bir öneriyle dönebilmemiz için birkaç şeyi netleştirelim:"]
            lines += ["- " + str(q).strip()[:300] for q in asks]
        else:
            lines += ["Teşekkürler, bilgileri aldım. " + why + " Bu yüzden görevi ben açmıyorum; "
                      + team + " ile netleştirip görev olarak açmanızı öneririm."]
        lines += ["", "Teşekkürler,", "Hamle"]
        payload = {"from": HAMLE, "to": [source["sender"]], "cc": [], "thread_id": source["thread_id"],
                   "in_reply_to_message_id": source["message_id"],
                   "subject": reply_subject(source.get("subject")), "body": "\n".join(lines)}
        remember_turns(rec, (source["sender"], source.get("text")), (HAMLE, "\n".join(lines[2:-3])))
        rec.update(status="held" if final else "awaiting_clarification", reason=reason)
        self._note(rec, "non-routine " + reason + (" (closed)" if final else " (asked)"))
        state = self._write(claim, work, rec, "clarify-" + source["message_id"][:150], "clarify_brief",
                            "gmail", payload, self.d.mail_writer)
        if state == "unknown":
            return None
        if final:
            return "held_for_human", "non-routine:" + reason
        return "processed", "non-routine-questions:" + state

    LIST_LABELS = {"social": "Sosyal Medya", "project": "Projeler", "motion": "Motion"}

    def _ack_enabled(self):
        return (self.catalog.get("chain", {}).get("task_ack") or {}).get("enabled") is True

    def _ack_payload(self, source, rec, brief, task):
        """Same-thread confirmation (reply-all within the brief's verified agency envelope)."""
        def line(value, limit=240):
            return re.sub(r"\s+", " ", str(value)).strip()[:limit]
        outputs = ", ".join(f"{d.get('quantity')} × {KIND_LABELS.get(d.get('kind'), d.get('kind'))} "
                            f"({line(deliverable_label(d, 120), 120)})" for d in brief.get("deliverables") or [])[:600]
        routed = self.cat.get("routed_brand")
        if routed:
            place = f"{routed['brand_name']} · {self.LIST_LABELS.get(self.cat['routes'][0].get('list_name'), 'liste')} listesi"
        else:
            bc = self.cat["basecamp"]
            place = f"{bc.get('project_name') or 'Hamle'} · {self.cat['routes'][0].get('list_name') or 'Hamle İşleri'} listesi"
        assignees = ", ".join(self.names.get(i, str(i)) for i in task["assignee_ids"]) or "-"
        link = f"https://3.basecamp.com/{task['account_id']}/buckets/{task['project_id']}/todos/{task['task_id']}"
        lines = ["Merhaba,", "", " ".join(x for x in (thanks_line(source, plural=True), "Talebinizi aldım ve Basecamp'te görevi "
                                                     "açtım. Anladığım kadarıyla:") if x),
                 "- İş: " + line(brief["title"]), "- Amaç: " + line(brief["objective"])]
        if brief.get("audience"):
            lines.append("- Hedef kitle: " + line(brief["audience"]))
        if outputs:
            lines.append("- Çıktılar: " + outputs)
        lines += ["", "Açıldığı yer: " + place, "Atanan: " + assignees]
        if brief.get("deadline"):
            lines.append("Teslim: " + line(brief["deadline"], 120))
        lines += ["Görev: " + link]
        notes = list(rec.get("assumptions") or [])  # model questions go to the team note, not the requester
        if notes:
            lines += ["", "Netleşecekler (ekip tamamlayacak, farklıysa bu maile yazman yeterli):"]
            lines += ["- " + line(n, 300) for n in notes[:8]]
        lines += ["", "Yanlış anladığım bir şey varsa bu maile yanıt verin.", "", "Hamle"]
        return {"from": HAMLE, "to": list(rec["reply"]["to"]), "cc": list(rec["reply"]["cc"]),
                "thread_id": rec["reply"]["thread_id"], "in_reply_to_message_id": rec["reply"]["in_reply_to_message_id"],
                "subject": reply_subject(source.get("subject")), "body": "\n".join(lines),
                "ack_task_id": int(task["task_id"])}

    def _route_requirements(self, claim, work, source, requirements):
        """Brand routing for one intake mail (deterministic, never guessed).

        Brand-bound work: move to the brand's per-kind read-back list before planning.
        Internal work: when exactly one brand was named, its name goes into the title and
        the context note says why the internal route was used (and tags the brand owner).
        """
        det = self._detect(source)
        if self.route_key:
            route = self.catalog["brand_routes"][self.route_key]
            deliverables = requirements.get("deliverables")
            if isinstance(deliverables, list) and deliverables:
                target = routing.select_list(route, deliverables)
                if target != work["list_id"]:
                    work = self.q.retarget_list(claim, list_id=target,
                                                allowed_list_ids=sorted(routing.allowlist(self.catalog)[work["project_id"]]))
                    self._use(work)
            return work, None, None
        if det["status"] == "disabled":
            return work, None, None
        if det["status"] == "ambiguous":
            return work, ("Hamle marka yönlendirmesi: talepte birden fazla marka geçti (" + ", ".join(det["matches"])
                          + "); tahmin edilmedi, iş iç 'Hamle İşleri' listesinde açıldı."), None
        if det["status"] != "single":
            return work, None, None
        name = det["brand_name"]
        title = requirements.get("title")
        if isinstance(title, str) and title.strip() and not title.startswith(f"[{name}]"):
            requirements["title"] = f"[{name}] {title.strip()}"[:200]
        if det["excluded"]:
            reason = "marka Hamle yönlendirmesinin dışında"
        else:
            reason = "markanın Basecamp rotası henüz doğrulanmadı (" + str(
                routing.route_problem(self.catalog, det["brand_key"])) + ")"
        mention = None if det["excluded"] else routing.owner_person(self.catalog, det["brand_key"])
        if mention is not None and mention not in self.names:
            mention = None
        return work, (f"Hamle marka yönlendirmesi: talep {name} markası için; {reason}. "
                      "İş iç 'Hamle İşleri' listesinde açıldı."), mention

    def _context_note(self, rec, past, follow_ups, context_notes=None, open_points=None):
        lines = ["Hamle iç bağlam notu — yalnız ekip içi, müşteriye/requester'a gönderilmez."]
        notes = [str(n)[:300] for n in (context_notes or []) if isinstance(n, str) and n.strip()][:5]
        if notes:
            lines.append("Talepten çıkan iç notlar (model önerisi):")
            lines += ["- " + n for n in notes]
        points = [str(n)[:300] for n in (open_points or []) if isinstance(n, str) and n.strip()][:3]
        if points:
            lines.append("Ekibin netleştirmesi gerekebilecek sorular (talep sahibine sorulmadı):")
            lines += ["- " + n for n in points]
        lines.append("Aynı marka/zincirdeki geçmiş işler:")
        lines += [f"- {p['title']} · durum: {p['status']}" + (f" · görev {p['task_id']}" if p["task_id"] else "")
                  + (" · aynı mail zinciri" if p["same_thread"] else "") for p in past] or ["- kayıt yok"]
        items = [str(f)[:300] for f in (follow_ups or []) if isinstance(f, str) and f.strip()][:5]
        lines.append("Olası takip işleri (model önerisi; onay veya söz değildir):")
        lines += ["- " + f for f in items] or ["- öneri yok"]
        return "\n".join(lines)

    # ---------- Basecamp comments ----------
    def _on_basecamp(self, claim, work, rec, comments):
        change = rec["pending_changes"].pop(claim["event"]["source_id"], None)
        self._save("chain-", rec)
        if change is None or change["kind"] != "comment_observed":
            return "no_action", "basecamp-" + (change or {}).get("kind", "change_unknown")
        comment = comments.get(str(change["record_id"]))
        if comment is None or record_digest(comment) != change["record_sha256"]:
            return "superseded", "comment-changed-or-absent"
        author = (comment.get("creator") or {}).get("id")
        if author == HAMLE_PERSON:
            return "no_action", "own-comment"
        producer, manager = author in rec["assignee_ids"], author in self.managers
        if not (producer or manager) or rec["status"] in ("delivered", "human_handoff"):
            return "no_action", "comment-not-actionable"
        text = _plain(comment.get("content"))
        kind = self._model(self.d.classifier, text, {"status": rec["status"], "producer": producer,
                                                    "manager": manager})
        if kind == "question" and producer:
            return self._question(claim, work, rec, comment, text)
        if kind == "submission" and producer:
            return self._submission(claim, work, rec, comment)
        if kind == "approval" and author in self.approvers and rec["status"] == "awaiting_human_approval":
            return self._approval(claim, work, rec, comment)
        if manager and ADDRESSED.search(text):
            if kind == "question":
                return self._manager_question(claim, work, rec, comment, text)
            if kind == "approval":  # approval outside the approval step: say why it is not used
                return self._manager_status(claim, work, rec, comment, approval=True)
        return "no_action", "comment-class-" + str(kind)[:40]

    def _question(self, claim, work, rec, comment, text):
        prod = self._load("chain-prod-", work["work_id"])
        b = prod["intake_work"]["brief"]["binding"]
        qid = "bc-comment-" + str(comment["id"])
        question = {"id": qid, "text": text or "(boş)", "source": {
            "channel": "basecamp", "verified": True, "evidence_ref": "basecamp-comment:" + str(comment["id"]),
            "actor_id": comment["creator"]["id"], "account_id": b["account_id"], "project_id": b["project_id"],
            "list_id": b["list_id"], "task_id": work["task_id"]}}
        recorded = record_producer_question(prod["intake_work"], question, self.cat)
        prod["intake_work"] = recorded["work"]
        self._save("chain-prod-", prod)
        brief = prod["intake_work"]["brief"]
        answer = self._model(self.d.answerer, text, {"brief": {k: brief.get(k) for k in (
            "title", "objective", "audience", "deadline", "assets_ref", "deliverables")},
            "profile": {k: self.cat.get(k) for k in ("brand_id", "terms", "verification_scope")}})
        allowed = {"brief." + k for k in ("title", "objective", "audience", "deadline", "assets_ref", "deliverables")}
        allowed |= {"profile." + k for k in ("terms", "brand_id", "verification_scope")}
        refs = answer.get("grounded_in") if isinstance(answer, dict) else None
        grounded = (isinstance(answer, dict) and isinstance(answer.get("answer"), str) and answer["answer"].strip()
                    and isinstance(refs, list) and refs and all(isinstance(r, str) and r.split("[")[0] in allowed
                                                                for r in refs))
        name = self.names.get(comment["creator"]["id"], "")
        if grounded:
            body = f"@{name} {answer['answer'].strip()[:3000]}\n(Dayanak: {', '.join(refs)})"
            mentions, status = [comment["creator"]["id"]], "answered"
        else:
            body = self._tag(self.managers, "Üreticinin sorusu brief/profilden güvenle yanıtlanamadı; "
                             "lütfen bu görevin altında yanıtlayın.")
            mentions, status = list(self.managers), "escalated_to_managers"
        rec["questions"][qid] = {"status": status}
        result = self._comment(claim, work, rec, "answer-" + qid, body, mentions)
        if result is None:
            return None
        rec["questions"][qid]["intent"] = result
        self._save("chain-", rec)
        return "processed", "producer-question:" + status

    # ---------- manager questions (Ahmet, Cansu, Özge tag Hamle on its task) ----------
    def _work_facts(self, rec, task=None):
        """Hamle's own record of the work in the team's words (no model, no provider read)."""
        status = rec["status"]
        reason = REASON_TR.get(rec.get("reason") or "", "")
        task = task if task is not None else self._tasks.get(rec["work_id"]) or {}
        due = task.get("due_on") if isinstance(task.get("due_on"), str) else None
        return {"status": STATUS_TR.get(status, status) + (f" ({reason})" if reason else ""),
                "next_step": NEXT_TR.get(status, HELD_NEXT),
                "assignees": ", ".join(self.names.get(i, str(i)) for i in rec["assignee_ids"]),
                "due_on": due}

    def _status_text(self, facts):
        lines = ["İşin durumu: " + facts["status"] + ".", "Sıradaki adım: " + facts["next_step"]
                 + ("" if facts["next_step"].endswith(".") else ".")]
        if facts["assignees"]:
            lines.append("Üretici: " + facts["assignees"] + ".")
        if facts["due_on"]:
            lines.append("Görevdeki teslim tarihi: " + facts["due_on"] + ".")
        return "\n".join(lines)

    def _manager_question(self, claim, work, rec, comment, text):
        """A manager asks Hamle on the task: answer from the brief and Hamle's own record of the
        work; never records a producer question, never changes the work or the brief."""
        prod = self._load("chain-prod-", work["work_id"]) or {}
        brief = (prod.get("intake_work") or {}).get("brief") or {}
        facts = self._work_facts(rec)
        answer = self._model(self.d.answerer, text, {"brief": {k: brief.get(k) for k in (
            "title", "objective", "audience", "deadline", "assets_ref", "deliverables")},
            "profile": {k: self.cat.get(k) for k in ("brand_id", "terms", "verification_scope")},
            "work": facts})
        allowed = {"brief." + k for k in ("title", "objective", "audience", "deadline", "assets_ref", "deliverables")}
        allowed |= {"profile." + k for k in ("terms", "brand_id", "verification_scope")}
        allowed |= {"work." + k for k in ("status", "next_step", "assignees", "due_on")}
        refs = answer.get("grounded_in") if isinstance(answer, dict) else None
        grounded = (isinstance(answer, dict) and isinstance(answer.get("answer"), str) and answer["answer"].strip()
                    and isinstance(refs, list) and refs and all(isinstance(r, str) and r.split("[")[0] in allowed
                                                                for r in refs))
        author = comment["creator"]["id"]
        name = self.names.get(author, "")
        if grounded:
            body = f"@{name} {answer['answer'].strip()[:3000]}\n(Dayanak: {', '.join(refs)})"
            status = "answered"
        else:
            body = (f"@{name} Bu soruya brief'ten ve kayıtlarımdan net bir cevap çıkaramadım; "
                    "ekipten biri burada yanıtlayabilir.\n" + self._status_text(facts))
            status = "status_given"
        key = "manager-" + str(comment["id"])
        rec["questions"][key] = {"status": status}
        result = self._comment(claim, work, rec, "answer-" + key, body, [author])
        if result is None:
            return None
        rec["questions"][key]["intent"] = result
        self._save("chain-", rec)
        return "processed", "manager-question:" + status

    def _manager_status(self, claim, work, rec, comment, approval=False):
        author = comment["creator"]["id"]
        lead = ("Onayın için teşekkürler; ama bu iş şu an onay adımında değil, bu yüzden onay olarak "
                "işleyemiyorum. Onayı, kalite puanı 80-89 çıktığında bu görevin altında isterim."
                if approval else "Notunu gördüm.")
        body = f"@{self.names.get(author, '')} {lead}\n" + self._status_text(self._work_facts(rec))
        if self._comment(claim, work, rec, "status-" + str(comment["id"]), body, [author]) is None:
            return None
        return "processed", "manager-status" + (":approval-out-of-step" if approval else "")

    # ---------- held works: managers tagging Hamle get one status note ----------
    def _held_listen(self, work):
        """Work in human takeover: no event is claimed and nothing about the work changes. When a
        manager or producer names Hamle in a new comment, Hamle writes ONE status note for that
        burst of comments (durable intent, fresh authorization, never retried)."""
        rec = self._load("chain-", work["work_id"])
        if not rec or not rec.get("task_id"):
            return None
        self._use(work)
        if "held_since" not in rec:  # last save before the hold ~= when the work was held
            rec["held_since"] = rec.get("updated_at") or self._now.timestamp()
            rec["held_seen"] = []
            self._save("chain-", rec)
        if self._now.timestamp() - float(rec["held_since"]) > HELD_LISTEN_SECONDS:
            return None
        if any(i.get("status") in ("prepared", "started", "unknown") for k, i in rec["intents"].items()
               if k.startswith("held-note-")):
            return "held_note_unresolved"
        reader = BasecampReader(self.d.basecamp_get)  # read-only: no journal, no queue event
        observation = reader.poll({k: work[k] for k in ("work_id", "account_id", "project_id", "list_id", "task_id")},
                                  previous=None, observed_at=self._now)
        floor = max(float(rec["held_since"]), self._now.timestamp() - HELD_LOOKBACK_SECONDS)
        team = set(self.managers) | set(rec["assignee_ids"])
        fresh = []
        for c in sorted(observation["comments"], key=lambda c: int(c["id"])):
            author = (c.get("creator") or {}).get("id")
            if str(c["id"]) in rec["held_seen"] or author == HAMLE_PERSON or author not in team:
                continue
            created = _timestamp(c.get("created_at"))
            if created is None or created <= floor or not ADDRESSED.search(_plain(c.get("content"))):
                continue
            fresh.append(c)
        if not fresh:
            return None
        last = fresh[-1]
        authors = list(dict.fromkeys(c["creator"]["id"] for c in fresh))
        approval = any(re.search(r"onay(lıyorum|ladım| veriyorum)|gönderilebilir|paylaşabiliriz",
                                 _plain(c.get("content")).lower()) for c in fresh)
        facts = self._work_facts(rec, observation["task"])
        lead = "Notunu gördüm." + (" Onayını not ettim; ama iş ekipte olduğu için gönderimi ben yapmayacağım."
                                   if approval else "")
        body = self._tag(authors, lead + "\n" + self._status_text(facts))
        seen = (rec["held_seen"] + [str(c["id"]) for c in fresh])[-50:]
        key = "held-note-" + str(last["id"])
        payload = {"target": {k: work[k] for k in ("account_id", "project_id", "list_id", "task_id")},
                   "body": body[:12000], "mention_ids": sorted(set(authors)), "internal_only": True}
        sha = scope_digest(payload)
        rec["intents"][key] = {"kind": "add_comment", "provider": "basecamp", "status": "prepared",
                               "payload_sha256": sha, "event_id": None}
        self._save("chain-", rec)  # durable intent BEFORE any authority/provider step
        if self.d.authorize("add_comment", copy.deepcopy(payload)) is not True:
            rec["intents"][key]["status"] = "denied"  # nothing written: the next tick may ask again
            self._save("chain-", rec)
            return "held_note_denied"
        rec["intents"][key]["status"] = "started"
        rec["held_seen"] = seen  # from here on this burst is never answered twice
        self._save("chain-", rec)
        try:
            self._report["provider_writes"] += 1
            remote = self._one_write(self.d.comment_writer, payload, sha)
        except Exception:
            rec["intents"][key]["status"] = "unknown"  # never retried; the next note waits for a human
            self._save("chain-", rec)
            return "held_note_unknown"
        rec["intents"][key].update(status="confirmed", remote_id=remote)
        self._note(rec, "held note -> " + ",".join(str(a) for a in authors))
        self._save("chain-", rec)
        return "held_note_written"

    def _evidence(self, rec, comment):
        view = self._view(rec)
        self._report["model_calls"] += 1
        return self.d.assessor(view, copy.deepcopy(comment)), view

    def _submission(self, claim, work, rec, comment, human_approval=None):
        evidence, view = self._evidence(rec, comment)
        if not isinstance(evidence, dict):
            result = {"route": "held", "reason": "quality_evidence_missing", "score": None}
        else:
            result = evaluate_quality(evidence.get("policy"), evidence.get("assessment"), evidence.get("binding"),
                                      evidence.get("safety"), now=self._now, human_approval=human_approval)
        rec["quality"] = {"route": result["route"], "reason": result["reason"], "score": result.get("score"),
                          "comment_id": comment["id"], "assessment_sha256": result.get("assessment_sha256")}
        self._note(rec, f"quality {result.get('score')} -> {result['route']}")
        version = str((evidence or {}).get("artifact_version") or f"comment-{comment['id']}")
        route = result["route"]
        if route == "delivery_candidate":
            return self._deliver(claim, work, rec)
        if route == "needs_human_approval":
            rec.update(status="awaiting_human_approval", reason=result["reason"])
            body = self._tag(self.approvers, "Puan 80–89 bandında: müşteriye/requester'a gitmeden önce Özge, Ahmet "
                             "veya Cansu'dan birinin onayı gerekiyor.\n\n" + quality_feedback(result, artifact_version=version))
            if self._comment(claim, work, rec, f"approval-{comment['id']}", body, self.approvers) is None:
                return None
            return "processed", "quality:needs_human_approval"
        if route == "revision":
            limit = self.cat.get("terms", {}).get("internal_quality_revision_limit_per_work")
            _need(type(limit) is int and 1 <= limit <= 10, "internal_revision_limit_missing")
            if rec["revision_count"] < limit:
                rec.update(status="changes_requested", reason=result["reason"])
                body = self._tag(rec["assignee_ids"], f"İç düzeltme {rec['revision_count'] + 1}/{limit}.\n\n"
                                 + quality_feedback(result, artifact_version=version))
                state = self._comment(claim, work, rec, f"revision-{comment['id']}", body, rec["assignee_ids"])
                if state is None:
                    return None
                if state == "confirmed":  # only a verified revision write counts toward the limit
                    rec["revision_count"] += 1
                    self._save("chain-", rec)
                return "processed", "quality:revision"
            rec.update(status="human_handoff", reason="internal_revision_limit_reached")
            body = self._tag(self.managers, f"{limit} iç düzeltme turu doldu; iş insana devredildi. "
                             "Hamle bu işte yeni düzeltme istemeyecek.\n\n" + quality_feedback(result, artifact_version=version))
            if self._comment(claim, work, rec, f"handoff-{comment['id']}", body, self.managers) is None:
                return None
            return "held_for_human", "internal-revision-limit"
        rec.update(status="quality_held", reason=result["reason"])
        body = self._tag(self.managers, "Kalite değerlendirmesi tamamlanamadı (" + result["reason"] +
                         "); insan incelemesi gerekiyor.")
        if self._comment(claim, work, rec, f"quality-held-{comment['id']}", body, self.managers) is None:
            return None
        return "held_for_human", "quality-held"

    def _approval(self, claim, work, rec, comment):
        approval = self.d.approval_reader(self._view(rec), copy.deepcopy(comment))
        if not isinstance(approval, dict):
            return "no_action", "approval-comment-without-verified-record"
        source = {"id": rec["quality"]["comment_id"]}
        return self._submission(claim, work, rec, source, human_approval=approval)

    # ---------- delivery (same mail thread) ----------
    def _deliver(self, claim, work, rec):
        dwork, adapter = self.d.delivery_bridge(self._view(rec))
        _need(all(dwork.get(k) == work[k] for k in WRITE_BINDING_FIELDS), "delivery_bridge_binding_mismatch")
        coordinator = DeliveryCoordinator()
        for _ in range(2):  # scored path may need a durable quality note before the mail
            polled = coordinator.poll(adapter, dwork, rec["delivery_state"], now=self._now)
            rec["delivery_state"] = polled["state"]
            if not polled["intents"]:
                break
            intent = polled["intents"][0]
            if intent["kind"] == "deliver_file":
                p = intent["payload"]
                _need(p["thread_id"] == rec["reply"]["thread_id"] == work["thread_id"], "delivery_thread_changed")
                _need(p["to"] == rec["reply"]["to"] and p["cc"] == rec["reply"]["cc"], "delivery_recipients_changed")
            pre = coordinator.preflight_intent(adapter, dwork, rec["delivery_state"], intent, now=self._now)
            if not pre["allowed"]:
                rec["delivery_state"] = pre["state"]
                break
            if self._delivery_write(claim, work, rec, intent, pre["state"]) != "confirmed":
                if self.q.get_work(work["work_id"])["inflight_action_id"]:
                    return None  # started/unknown write: no tag, no retry
                rec["delivery_state"] = dict(rec["delivery_state"] or {}, status="held",
                                             reason="delivery_write_not_authorized")
                break
        state = rec["delivery_state"] or {}
        if state.get("status") == "delivered":
            rec.update(status="delivered", reason=None)
            self._save("chain-", rec)
            return "processed", "delivered:" + state["last_confirmed_delivery"]["action_id"][:100]
        rec.update(status="delivery_held", reason=state.get("reason"))
        self._save("chain-", rec)
        body = self._tag(self.managers, "Teslim, gönderim öncesi kontrollerde durdu (" + str(state.get("reason"))[:150]
                         + "). Hamle bu işi göndermeyecek; lütfen inceleyin ve gerekirse elle gönderin.")
        key = "delivery-held-" + str(rec["quality"]["comment_id"] if rec.get("quality") else "x")
        if self._comment(claim, work, rec, key, body, self.managers) is None:
            return None
        return "held_for_human", "delivery-held:" + str(state.get("reason"))[:120]

    def _delivery_write(self, claim, work, rec, intent, state):
        key, kind = intent["action_id"], intent["kind"]
        provider_name = "gmail" if kind == "deliver_file" else "basecamp"
        rec["intents"][key] = {"kind": kind, "provider": provider_name, "status": "prepared",
                               "payload_sha256": intent["payload_sha256"], "event_id": claim["event_id"]}
        self._save("chain-", rec)
        if self.d.authorize(kind, copy.deepcopy(intent["payload"])) is not True:
            rec["intents"][key]["status"] = "denied"
            self._save("chain-", rec)
            return "denied"
        binding = {k: intent["authority_request"]["work_binding"][k] for k in WRITE_BINDING_FIELDS}
        binding["queue_event_fingerprint"] = claim["event"]["fingerprint"]
        action = self.q.begin_write(claim, action_kind=kind, payload_sha256=intent["payload_sha256"],
                                    action_key=key, prepared_binding=binding)
        rec["delivery_state"] = record_outcome(state, intent, status="started")
        rec["intents"][key].update(status="started", action_id=action["id"])
        self._save("chain-", rec)
        writer = self.d.mail_writer if kind == "deliver_file" else self.d.comment_writer
        try:
            self._report["provider_writes"] += 1
            remote = self._one_write(writer, intent["payload"], intent["payload_sha256"])
            self.q.confirm_write(action_id=action["id"], claim_token=claim["token"],
                                 payload_sha256=intent["payload_sha256"],
                                 receipt={"provider": provider_name, "remote_id": remote,
                                          "readback_sha256": intent["payload_sha256"]})
        except Exception:
            return self._unknown(rec, key, action, lambda: record_outcome(rec["delivery_state"], intent, status="unknown"))
        receipt = {"provider_readback_verified": True, "payload_sha256": intent["payload_sha256"],
                   "action_id": key, "evidence_ref": f"{provider_name}-readback:{remote}"}
        if intent.get("quality_note") is True:
            receipt["quality_context_sha256"] = intent["quality_context_sha256"]
        rec["delivery_state"] = record_outcome(rec["delivery_state"], intent, status="confirmed", receipt=receipt)
        rec["intents"][key].update(status="confirmed", remote_id=remote)
        self._save("chain-", rec)
        return "confirmed"

    # ---------- generic single write ----------
    def _tag(self, ids, text):
        return " ".join("@" + self.names.get(i, str(i)) for i in ids) + "\n" + text

    def _comment(self, claim, work, rec, key, body, mention_ids):
        _need(work.get("task_id"), "comment_requires_bound_task")
        payload = {"target": {k: work[k] for k in ("account_id", "project_id", "list_id", "task_id")},
                   "body": body[:12000], "mention_ids": sorted(set(mention_ids)), "internal_only": True}
        state = self._write(claim, work, rec, key, "add_comment", "basecamp", payload, self.d.comment_writer)
        return None if state == "unknown" else state

    def _write(self, claim, work, rec, key, action_kind, provider_name, payload, writer):
        key = key[:200]
        sha = scope_digest(payload)
        prior = rec["intents"].get(key)
        if prior:
            _need(prior["payload_sha256"] == sha, "intent_payload_conflict")
            if prior["status"] in ("confirmed", "denied"):
                return prior["status"]
            _need(prior["status"] == "prepared", "intent_outcome_unknown_no_retry")
        rec["intents"][key] = {"kind": action_kind, "provider": provider_name, "status": "prepared",
                               "payload_sha256": sha, "event_id": claim["event_id"]}
        self._save("chain-", rec)  # durable intent BEFORE any authority/provider step
        if self.d.authorize(action_kind, copy.deepcopy(payload)) is not True:
            rec["intents"][key]["status"] = "denied"
            self._save("chain-", rec)
            return "denied"
        work = self.q.get_work(work["work_id"])
        binding = {**{k: work[k] for k in WRITE_BINDING_FIELDS}, "queue_event_fingerprint": claim["event"]["fingerprint"]}
        action = self.q.begin_write(claim, action_kind=action_kind, payload_sha256=sha, action_key=key,
                                    prepared_binding=binding)
        rec["intents"][key].update(status="started", action_id=action["id"])
        self._save("chain-", rec)
        try:
            self._report["provider_writes"] += 1
            remote = self._one_write(writer, payload, sha)
            self.q.confirm_write(action_id=action["id"], claim_token=claim["token"], payload_sha256=sha,
                                 receipt={"provider": provider_name, "remote_id": remote, "readback_sha256": sha})
        except Exception:
            return self._unknown(rec, key, action)
        rec["intents"][key].update(status="confirmed", remote_id=remote)
        self._save("chain-", rec)
        return "confirmed"

    @staticmethod
    def _one_write(writer, payload, sha):
        receipt = writer.write(copy.deepcopy(payload))
        remote = receipt.get("remote_id") if isinstance(receipt, dict) else None
        _need(isinstance(remote, str) and 0 < len(remote) <= 200, "provider_receipt_unconfirmed")
        readback = writer.readback(remote, copy.deepcopy(payload))
        _need(isinstance(readback, dict) and readback.get("readback_sha256") == sha, "provider_readback_mismatch")
        return remote

    def _unknown(self, rec, key, action, delivery_update=None):
        try:
            self.q.hold_unknown(action["id"])
        except Exception:
            pass  # started action already forbids any resend
        if delivery_update is not None:
            try:
                rec["delivery_state"] = delivery_update()
            except Exception:
                pass
        rec["intents"][key]["status"] = "unknown"
        rec.update(status="write_outcome_unknown", reason="provider_write_outcome_unknown_no_retry")
        self._save("chain-", rec)
        return "unknown"

    def _view(self, rec):
        prod = self._load("chain-prod-", rec["work_id"]) or {}
        return {"work_id": rec["work_id"], "thread_id": rec["thread_id"], "task_id": rec["task_id"],
                "reply": copy.deepcopy(rec["reply"]), "stage_id": rec["stage_id"],
                "brief": copy.deepcopy((prod.get("intake_work") or {}).get("brief")),
                "brief_sha256": rec.get("brief_sha256"), "revision_count": rec["revision_count"]}


def run_chain_tick(deps):
    """Separate, non-default entry point. worker.py does not call this."""
    return Chain(deps).tick()


class BasecampCommentWriter:
    """Narrow comment writer for the verified internal project plus, when brand routing is
    enabled, each usable brand route's project (allow-list = catalog); one POST, exact readback.

    write(payload) -> {'remote_id'}; readback(remote_id, payload) -> {'readback_sha256'}.
    Mentions are listed in payload['mention_ids'] and named in text; rendering real
    Basecamp @mentions needs each person's attachable sgid (not wired yet).
    """
    ORIGIN = "https://3.basecampapi.com"

    def __init__(self, catalog, *, token_provider, session_factory=provider._session):
        bc = catalog["basecamp"]
        _need(bc.get("verified") is True and bc.get("customer_visible") is False, "comment_project_unverified")
        self.account, self.project = bc["account_id"], bc["project_id"]
        self.allowed = {p: frozenset(ls) for p, ls in routing.allowlist(catalog).items()}
        self.token_provider, self.session_factory = token_provider, session_factory

    def _check(self, payload):
        t = payload.get("target", {}) if isinstance(payload, dict) else {}
        _need(set(payload) == {"target", "body", "mention_ids", "internal_only"} and payload["internal_only"] is True
              and t.get("account_id") == self.account and t.get("project_id") in self.allowed
              and (t.get("list_id") is None or t.get("list_id") in self.allowed[t["project_id"]])
              and type(t.get("task_id")) is int and t["task_id"] > 0
              and isinstance(payload["body"], str) and 0 < len(payload["body"]) <= 12000, "comment_payload_invalid")
        return f"{self.ORIGIN}/{self.account}/buckets/{t['project_id']}"

    def _request(self, method, url, body=None, expected=200):
        session, response = self.session_factory(), None
        try:
            response = session.request(method, url, headers={
                "Authorization": "Bearer " + provider._token(self.token_provider()),
                "Content-Type": "application/json", "User-Agent": "Hamle Chain (ahmet@gamechangers.com.tr)"},
                data=None if body is None else provider._encode(body), timeout=(10, 30),
                allow_redirects=False, stream=True)
            _need(response.status_code == expected, "provider_response_unconfirmed")
            raw = b"".join(response.iter_content(chunk_size=16384))
            _need(len(raw) <= 262144, "provider_response_too_large")
            result = provider._json(raw)
            _need(isinstance(result, dict), "provider_response_invalid")
            return result
        finally:
            if response is not None:
                response.close()
            session.close()

    def write(self, payload):
        base = self._check(payload)
        row = self._request("POST", f"{base}/recordings/{payload['target']['task_id']}/comments.json",
                            {"content": bw._description(payload["body"])}, expected=201)
        _need(type(row.get("id")) is int and row["id"] > 0, "comment_receipt_unconfirmed")
        return {"remote_id": str(row["id"])}

    def readback(self, remote_id, payload):
        base = self._check(payload)
        _need(isinstance(remote_id, str) and re.fullmatch(r"[0-9]{1,30}", remote_id), "comment_receipt_invalid")
        row = self._request("GET", f"{base}/comments/{remote_id}.json")
        _need(row.get("id") == int(remote_id) and row.get("bucket", {}).get("id") == payload["target"]["project_id"]
              and row.get("parent", {}).get("id") == payload["target"]["task_id"]
              and row.get("creator", {}).get("id") == HAMLE_PERSON and row.get("visible_to_clients") is False
              and bw._description_matches(row.get("content"), payload["body"]), "comment_readback_mismatch")
        return {"readback_sha256": scope_digest(payload)}
