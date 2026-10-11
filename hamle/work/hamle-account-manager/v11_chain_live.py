"""Live adapter wiring for v11_chain; NON-default (worker.py --mode chain_tick).

run_chain_slot(ops, open_queue=..., build_deps=...) mirrors worker.run_scheduled_review:
  global control (review_open) -> one Istanbul slot claim -> generation/deadline fence
  -> received-stop sweep -> chain queue control (queue_open) -> budget-gated chain tick.
build_live_deps(...) builds ChainDeps from EXISTING credential loaders only:
  GoogleSecretManager (Gmail record, OpenAI key), basecamp_cloud_auth.access_token
  (with ops.claim_credential_refresh), ADC credentials for Firestore. No new secret
  handling, no new provider. Queue + chain state live in Firestore collection
  hamle_v11_work_queue (FirestoreQueueBackend); budget/slot/model receipts stay in
  the existing hamle_internal_v1 OperationsStore.

Mac-side operator commands (write ONLY the chain queue control document; no cost):
  python3 v11_chain_live.py queue status
  python3 v11_chain_live.py queue open  --expected-generation N --actor ahmet@gamechangers.com.tr
  python3 v11_chain_live.py queue stop  --expected-generation N --actor ahmet@gamechangers.com.tr
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import re
import sys

import cloud_providers as provider
from autonomy_policy import scope_digest
import v11_brand_routing as routing
from cloud_store import DEADLINE, START
from runtime_authority import CHAIN_TICK_WINDOW
from v11_chain import BasecampCommentWriter, ChainDeps, run_chain_tick
from v11_work_queue import COLLECTION, FirestoreQueueBackend, QueueError, QueueStore, digest

MAIL_KINDS = ("clarify_brief", "acknowledge", "deliver_file")
WRITE_KINDS = ("create_task", "add_comment") + MAIL_KINDS
QUEUE_OPT_IN = "v11-queue-explicit"


class ChainLiveError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ChainLiveError(code)


def _code(exc):
    return str(getattr(exc, "code", None) or type(exc).__name__)[:120]


def _stamp(clock):
    now = clock()
    _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
    _need(START <= now.timestamp() <= DEADLINE, "authorization_expired")
    return now


# ------------------------------------------------------------------ persistence
class InjectedFirestoreQueueBackend(FirestoreQueueBackend):
    """FirestoreQueueBackend with an injected client/SDK (tests, or an already-built client).

    transact()/scan() are the parent's unchanged code: one application attempt,
    ambiguous commit -> store_outcome_unknown, never retried here.
    """
    def __init__(self, *, client, sdk, field_filter, opt_in):
        _need(opt_in == QUEUE_OPT_IN, "cloud_opt_in_required")
        _need(client is not None and sdk is not None and field_filter is not None, "explicit_client_required")
        self.sdk, self.field_filter, self.client = sdk, field_filter, client
        self.collection = client.collection(COLLECTION)


def open_cloud_queue(credentials, *, clock=None, backend=None):
    """Chain queue on Firestore (explicit opt-in + explicit credentials), initialized stopped."""
    backend = backend or FirestoreQueueBackend(opt_in=QUEUE_OPT_IN, credentials=credentials)
    queue = QueueStore(backend, clock=clock) if clock else QueueStore(backend)
    queue.initialize()  # creates control=stopped only when absent; never reopens
    return queue


def anchor_lookup(queue, brand_id):
    """Reply anchor (Gmail id) recorded by the chain for this thread's work."""
    def lookup(thread_id):
        rows = queue.backend.scan(kind="chain_work", filters={"brand_id": brand_id, "thread_id": thread_id},
                                  limit=2)
        _need(len(rows) == 1 and isinstance(rows[0].get("reply"), dict), "reply_anchor_missing")
        return rows[0]["reply"]["in_reply_to_message_id"]
    return lookup


# ------------------------------------------------------------------ writers / gates
class ChainCommentWriter:
    """BasecampCommentWriter for both chain comments and DeliveryCoordinator notes.

    The coordinator's add_comment payload is {'target','body'}; it is written as an
    internal-only comment without mentions, and the readback hash is reported for the
    ORIGINAL payload so confirm_write binds the coordinator's exact intent.
    """
    def __init__(self, inner):
        self.inner = inner

    @staticmethod
    def _normal(payload):
        if isinstance(payload, dict) and set(payload) == {"target", "body"}:
            return {**copy.deepcopy(payload), "mention_ids": [], "internal_only": True}
        return payload

    def write(self, payload):
        return self.inner.write(self._normal(payload))

    def readback(self, remote_id, payload):
        result = self.inner.readback(remote_id, self._normal(payload))
        _need(isinstance(result, dict) and result.get("readback_sha256") == scope_digest(self._normal(payload)),
              "comment_readback_mismatch")
        return {"readback_sha256": scope_digest(payload)}


class ChainAuthorizer:
    """Fresh checks immediately before every chain write (False = denied, never retried).

    Global control + generation, chain queue control + generation, authorization
    deadline, profile live switches, budget overrun hold, internal-only comment
    target, and for mail the writer's read-only preflight (anchor still latest,
    exact To/CC/subject, attachment bytes re-downloaded and hash-checked).
    """
    def __init__(self, *, ops, ops_generation, queue, queue_generation, catalog, mail_writer, clock):
        self.ops, self.ops_generation = ops, ops_generation
        self.queue, self.queue_generation = queue, queue_generation
        self.catalog, self.mail_writer, self.clock = catalog, mail_writer, clock
        self.decisions = []

    def __call__(self, kind, payload):
        try:
            _need(kind in WRITE_KINDS, "action_kind_not_allowed")
            _stamp(self.clock)
            c = self.ops.control()
            _need(c["mode"] == "review_open" and c["generation"] == self.ops_generation, "global_control_changed")
            q = self.queue.control()
            _need(q["mode"] == "queue_open" and q["generation"] == self.queue_generation, "chain_queue_changed")
            _need(self.catalog.get("live_enabled") is True
                  and self.catalog.get("provider_mutation_allowed_by_this_file") is True, "profile_live_disabled")
            _need(self.ops.budget_status()["overrun_hold"] is False, "budget_overrun_hold")
            if kind == "add_comment":
                bc = self.catalog["basecamp"]
                t = payload.get("target", {})
                allowed = routing.allowlist(self.catalog)
                _need(t.get("account_id") == bc["account_id"] and t.get("project_id") in allowed
                      and (t.get("list_id") is None or t.get("list_id") in allowed[t["project_id"]])
                      and payload.get("internal_only", True) is True, "comment_target_out_of_scope")
            if kind == "create_task":
                task = (payload.get("payload") or {}).get("task") or {}
                _need(task.get("account_id") == self.catalog["basecamp"]["account_id"]
                      and routing.allowed_target(self.catalog, task.get("project_id"), task.get("todolist_id"))
                      and task.get("notify") is False, "task_target_out_of_scope")
            if kind in MAIL_KINDS:
                self.mail_writer.preflight(kind, copy.deepcopy(payload))
            self.decisions.append((kind, "allowed"))
            return True
        except Exception as exc:
            self.decisions.append((kind, _code(exc)))
            return False


def budget_gate(ops, reservation, cap_microdollars=None):
    def ok():
        try:
            status = ops.budget_status()
            if cap_microdollars is not None:
                from v11_chain_models import chain_committed_microdollars
                if chain_committed_microdollars(ops) + reservation > cap_microdollars:
                    return False
            return status["overrun_hold"] is False and status["remaining_microdollars"] >= reservation
        except Exception:
            return False
    return ok


def _memo(fn):
    box = {}
    def call():
        if "value" not in box:
            box["value"] = fn()
        return box["value"]
    return call


# ------------------------------------------------------------------ assembly
def load_catalog(path=None):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    name = "gamechangers-internal-catalog.json"
    candidates = [Path(path)] if path else [Path(__file__).with_name("chain-profile") / name,
                                            root / "outputs/gamechangers-internal" / name]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise ChainLiveError("catalog_missing")


def build_live_deps(*, ops, ops_generation, queue, queue_generation, secrets, catalog, rubric, clock,
                    work_policy_document=None):
    """Real adapters only; no network happens until the tick calls them."""
    import basecamp_cloud_auth
    from v11_basecamp_reader import SessionGet
    from v11_brand_task import BrandTaskWriter
    from v11_chain_mail import ChainMailReader, GmailThreadReplyWriter, gmail_token_context
    from v11_chain_models import MODEL_RESERVATION, ChainModels, ModelGate, internal_cap_microdollars
    from v11_chain_quality import BasecampAttachmentFetcher, ChainQualityBridge
    from v11_chain_authority import ChainWorkAuthority, enabled_policy

    token = _memo(lambda: basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh))
    gmail_store = provider.GmailSecretStore(secrets)
    fetcher = BasecampAttachmentFetcher(catalog, token_provider=token)
    holder = {}
    mail = GmailThreadReplyWriter(catalog, token_context=gmail_token_context(secrets),
                                  anchor_lookup=anchor_lookup(queue, catalog["brand_id"]),
                                  attachment_source=lambda art: holder["quality"].attachment_source(art), clock=clock)
    work_policy = enabled_policy(work_policy_document)  # None while pending Ahmet -> deliveries held
    authority = ChainWorkAuthority(queue=queue, ops=ops, catalog=catalog, policy=work_policy,
                                   source_reader=mail.source_evidence, clock=clock)
    quality = ChainQualityBridge(catalog, rubric, queue=queue, fetcher=fetcher, clock=clock,
                                 source_reader=mail.source_evidence, authority_source=authority.authority_source,
                                 work_authority_policy=work_policy)
    holder["quality"] = quality
    models = ChainModels(load_key=lambda: provider.openai_key(secrets),
                         gate=ModelGate(ledger=ops, cache=queue, generation=ops_generation,
                                        cap_microdollars=internal_cap_microdollars(catalog)))
    session = provider._session()
    return ChainDeps(
        queue=queue, catalog=catalog, clock=clock,
        mail_reader=ChainMailReader(gmail_store, catalog),
        interpreter=models.interpreter, classifier=models.classifier, answerer=models.answerer,
        assessor=quality.assess, approval_reader=quality.approval_reader, delivery_bridge=quality.bridge,
        task_writer_factory=lambda cat, st: BrandTaskWriter(cat, st, token_provider=token),
        basecamp_get=SessionGet(session, token),
        comment_writer=ChainCommentWriter(BasecampCommentWriter(catalog, token_provider=token)),
        mail_writer=mail,
        authorize=ChainAuthorizer(ops=ops, ops_generation=ops_generation, queue=queue,
                                  queue_generation=queue_generation, catalog=catalog, mail_writer=mail, clock=clock),
        budget_ok=budget_gate(ops, MODEL_RESERVATION, internal_cap_microdollars(catalog)), worker_id="hamle-chain-cloud",
        intake_authority=lambda view, context: authority.on_intake(
            view, quality.scope(), {**context, "work_brief_sha256": quality.delivery_brief_sha256(view)})), session


# ------------------------------------------------------------------ explicit manual internal run
MANUAL_OPERATORS = ("ahmet@gamechangers.com.tr", "cansu@gamechangers.com.tr", "ozge@gamechangers.com.tr")
MANUAL_BRAND, MANUAL_SERVICE = "gamechangers", "gc_internal"
MANUAL_LEASE_KEY = "manual-chain-lease"
MANUAL_LEASE_SECONDS = 900  # > Cloud Run task timeout (600 s); a crashed run blocks the next for 15 min


def claim_manual_run(ops, expected_generation, operator):
    """Durable unique manual run (never a slot-* key), one at a time via a lease.

    Same control/generation and budget-readiness checks as claim_scheduled_slot,
    without the weekday 08:30-18:30 window. A crash keeps the claim (no replay).
    """
    import uuid
    from cloud_store import LIMIT
    _need(operator in MANUAL_OPERATORS, "manual_operator_not_allowed")
    def op(tx, now):
        c = ops._control(tx); b = ops._budget(tx)
        ops._open(c, expected_generation, now)
        _need(not b["overrun_hold"] and b["providers"]["google_cloud"]["billing_ready"]
              and ops._totals(b)["total_committed_microdollars"] <= LIMIT, "budget_not_ready")
        lease = tx.get(MANUAL_LEASE_KEY)
        _need(lease is None or lease.get("state") != "running" or now >= lease["expires_at"],
              "manual_run_in_progress")
        token = uuid.uuid4().hex
        key = "manual-chain-" + datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + token[:12]
        _need(tx.get(key) is None, "manual_run_key_conflict")
        row = {"slot_key": key, "scheduled_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
               "claimed_at": now, "generation": c["generation"], "token": token, "state": "claimed",
               "kind": "manual_chain_run", "operator": operator, "brand_id": MANUAL_BRAND}
        tx.put(key, row)
        tx.put(MANUAL_LEASE_KEY, {"state": "running", "run_key": key, "token": token,
                                  "expires_at": now + MANUAL_LEASE_SECONDS, "operator": operator})
        return row
    return ops._run(op)


def finish_manual_run(ops, claim, *, status, reason=None):
    _need(isinstance(claim, dict) and str(claim.get("slot_key", "")).startswith("manual-chain-"), "manual_claim_invalid")
    _need(status in ("stopped", "already_recorded", "no_new_work", "held"), "manual_outcome_invalid")
    outcome = {"status": status, "reason": reason}
    def op(tx, now):
        old = tx.get(claim["slot_key"])
        lease = tx.get(MANUAL_LEASE_KEY)  # all reads before writes (Firestore transaction rule)
        _need(old is not None and old.get("token") == claim.get("token")
              and old.get("generation") == claim.get("generation"), "manual_claim_invalid")
        if old["state"] == "claimed":
            old.update(state="finished", finished_at=now, outcome=outcome)
            tx.put(claim["slot_key"], old)
        if lease is not None and lease.get("run_key") == claim["slot_key"] and lease.get("state") == "running":
            lease.update(state="released", released_at=now)
            tx.put(MANUAL_LEASE_KEY, lease)
        return old
    return ops._run(op, allow_expired=True)


def internal_only_guard(catalog, queue):
    """Manual runs touch ONLY the Gamechangers internal profile and its works."""
    _need(catalog.get("brand_id") == MANUAL_BRAND
          and catalog.get("chain", {}).get("default_service_id") == MANUAL_SERVICE
          and all(r.get("service_id") == MANUAL_SERVICE for r in catalog.get("routes", [])),
          "manual_run_internal_profile_only")
    cursor = None
    for _ in range(8):
        page = queue.page(kind="work", limit=25, cursor=cursor)
        _need(all(w.get("brand_id") == MANUAL_BRAND and w.get("service_id") == MANUAL_SERVICE
                  for w in page["items"]), "manual_run_non_internal_work_present")
        cursor = page["next_cursor"]
        if cursor is None:
            return True
    raise ChainLiveError("manual_run_work_scan_incomplete")


# ------------------------------------------------------------------ one scheduled slot
def _summary(report):
    works = {k: {"events": v.get("events"), "status": v.get("status")} for k, v in report.get("works", {}).items()}
    return {"status": report.get("status"), "reason": report.get("reason"),
            "provider_writes": report.get("provider_writes"), "model_calls": report.get("model_calls"),
            "ignored_mail": [r.get("reason") for r in report.get("ignored_mail", [])], "works": works}


def run_chain_slot(ops, *, open_queue, build_deps, clock=None, interval_minutes=15, check_work_stops=None,
                   check_chain_stops=None, manual_operator=None, manual_catalog=None):
    """Same slot/stop/generation/deadline/budget order as worker.run_scheduled_review.

    manual_operator (explicit CLI only, never scheduler args): a unique durable
    manual run claim replaces the weekday slot window; internal profile only.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    manual = manual_operator is not None
    finish = (lambda claim, **kw: finish_manual_run(ops, claim, **kw)) if manual else ops.finish_scheduled_slot
    claim = None
    attempted = False
    try:
        if manual:
            _need(manual_operator in MANUAL_OPERATORS, "manual_operator_not_allowed")
            _need(isinstance(manual_catalog, dict) and manual_catalog.get("brand_id") == MANUAL_BRAND,
                  "manual_run_internal_profile_only")
        _stamp(clock)
        c = ops.control()
        if c["mode"] != "review_open":
            return {"status": "stopped", "reason": "human_control_stopped", "model_calls": 0, "business_writes": 0}
        claim = (claim_manual_run(ops, c["generation"], manual_operator) if manual
                 else ops.claim_scheduled_slot(c["generation"], interval_minutes=interval_minutes,
                                               window=CHAIN_TICK_WINDOW))
        if claim is None:
            return {"status": "slot_already_claimed", "model_calls": 0, "business_writes": 0}
        def fence():
            _stamp(clock)
            now = ops.control()
            _need(now["mode"] == "review_open" and now["generation"] == claim["generation"], "control_stopped_or_changed")
        fence()
        if check_work_stops is not None:
            result = check_work_stops()
            _need(isinstance(result, dict) and result.get("status") == "completed"
                  and result.get("grants_created") == 0 and result.get("business_writes") == 0,
                  "work_stop_sweep_incomplete")
            fence()
        _need(ops.budget_status()["overrun_hold"] is False, "budget_overrun_hold")
        queue = open_queue()
        if manual:
            internal_only_guard(manual_catalog, queue)
        if check_chain_stops is not None:
            # Visible stop/takeover mail on chain threads: per-work generation fence first.
            result = check_chain_stops(queue)
            _need(isinstance(result, dict) and result.get("status") == "completed"
                  and result.get("grants_created") == 0 and result.get("business_writes") == 0,
                  "chain_stop_sweep_incomplete")
            fence()
        q = queue.control()
        if q["mode"] != "queue_open":
            finish(claim, status="stopped", reason="chain_queue_" + q["mode"])
            return {"status": "stopped", "reason": "chain_queue_" + q["mode"], "model_calls": 0,
                    "business_writes": 0, "slot_key": claim["slot_key"]}
        deps = build_deps(queue, claim["generation"], q["generation"])
        attempted = True
        report = run_chain_tick(deps)
        summary = _summary(report)
        finish(claim, status="already_recorded" if report["status"] == "ok" else "held",
               reason=("chain_tick_" + str(report["status"]))[:60])
        return {"status": "chain_tick_completed" if report["status"] == "ok" else "held",
                "reason": report.get("reason"), "chain": summary, "slot_key": claim["slot_key"],
                "scheduled_at": claim["scheduled_at"], "model_calls": report["model_calls"],
                "business_writes": report["provider_writes"], "automatic_retry_allowed": False}
    except Exception as exc:
        reason = re.sub(r"[^A-Za-z0-9_.:@-]", "_", _code(exc))[:120]
        if claim is not None:
            try:
                finish(claim, status="held", reason=reason)
            except Exception:
                pass
        return {"status": "held", "reason": reason, "business_writes": None if attempted else 0,
                "automatic_retry_allowed": False}


def run_worker_chain_tick(ops, secrets, *, clock=None, check_work_stops=None, chain_stop_reader=None,
                          manual_operator=None):
    """Entry used by worker.py --mode chain_tick (store/secrets built by worker.main)."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    from v11_chain_authority import chain_stop_sweep, load_policy_file
    from v11_chain_quality import load_rubric
    catalog, rubric, work_policy_document = load_catalog(), load_rubric(), load_policy_file()
    sessions = []
    def open_queue():
        secrets._headers()  # existing ADC loader of GoogleSecretManager (project-checked)
        return open_cloud_queue(secrets.credentials)
    def build(queue, ops_generation, queue_generation):
        deps, session = build_live_deps(ops=ops, ops_generation=ops_generation, queue=queue,
                                        queue_generation=queue_generation, secrets=secrets, catalog=catalog,
                                        rubric=rubric, clock=clock, work_policy_document=work_policy_document)
        sessions.append(session)
        return deps
    try:
        chain_stops = None
        if chain_stop_reader is not None:
            # Revocation does not need the policy: any received stop fences the work.
            chain_stops = lambda queue: chain_stop_sweep(
                queue, _StopRevoker(queue, clock), chain_stop_reader, clock=clock)
        return run_chain_slot(ops, open_queue=open_queue, build_deps=build, clock=clock,
                              check_work_stops=check_work_stops, check_chain_stops=chain_stops,
                              manual_operator=manual_operator, manual_catalog=catalog if manual_operator else None)
    finally:
        for session in sessions:
            session.close()


class _StopRevoker:
    """Grant revocation for the stop sweep without loading policy/catalog adapters."""
    def __init__(self, queue, clock):
        from v11_chain_authority import ChainWorkAuthority
        self.inner = ChainWorkAuthority(queue=queue, ops=None, catalog=None, policy=None,
                                        source_reader=None, clock=clock)

    def revoke(self, work_id, **kw):
        return self.inner.revoke(work_id, **kw)


# ------------------------------------------------------------------ Mac operator CLI
def queue_command(queue, action, *, actor=None, expected_generation=None):
    if action == "status":
        c = queue.control()
        works = queue.page(kind="work", limit=100)["items"]
        return {"mode": c["mode"], "generation": c["generation"], "works": len(works),
                "active": sum(w["mode"] == "active" for w in works)}
    _need(action in ("open", "stop") and isinstance(actor, str) and actor.endswith("@gamechangers.com.tr")
          and type(expected_generation) is int, "explicit_actor_and_generation_required")
    row = queue.set_control("queue_open" if action == "open" else "stopped", actor=actor,
                            expected_generation=expected_generation)
    return {"mode": row["mode"], "generation": row["generation"], "actor": row["actor"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Chain queue control (writes only the queue control document)")
    parser.add_argument("area", choices=("queue",))
    parser.add_argument("action", choices=("status", "open", "stop"))
    parser.add_argument("--actor")
    parser.add_argument("--expected-generation", type=int)
    args = parser.parse_args(argv)
    try:
        from import_cloud_credentials import credentials
        queue = open_cloud_queue(credentials())
        print(json.dumps(queue_command(queue, args.action, actor=args.actor,
                                       expected_generation=args.expected_generation), ensure_ascii=False))
        return 0
    except (ChainLiveError, QueueError) as exc:
        print(json.dumps({"status": "held", "reason": exc.code}))
        return 2
    except Exception:
        print(json.dumps({"status": "held", "reason": "queue_control_failed"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
