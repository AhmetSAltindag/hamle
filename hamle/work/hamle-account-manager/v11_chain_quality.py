"""Internal technical quality rubric + delivery bridge for v11_chain (offline-testable).

ChainQualityBridge supplies three ChainDeps hooks and the mail attachment source:
  assess(view, comment)          -> {'policy','assessment','binding','safety','artifact_version'} | None
  approval_reader(view, comment) -> stored human approval bound to the CURRENT assessment | None
  bridge(view)                   -> (delivery_work, DeliveryAdapter) for DeliveryCoordinator
  attachment_source(artifact)    -> freshly re-downloaded, hash-checked bytes for the mail

Path: producer comment -> fresh Basecamp read (task, comment, attachment) -> real bytes
via the existing v11_internal_providers download transport -> v11_artifact_inspector ->
accepted rubric -> v11_quality_gate.evaluate_quality. The SAME assessment is bound to
the exact future delivery intent (scope, recipients, payload, generations), so the
coordinator's scored release, the 80-89 human approval and the final authorize_action
all check one immutable record. A software release record is stored for audit.

The rubric file is a DRAFT (weights_status=draft_not_accepted): until Ahmet accepts it
assess() returns None and the chain holds every submission for the managers.
Send authority (delegation / work-authority grant) is injected by authority_source;
without it autonomy_policy.authorize_action holds delivery. Nothing here sends.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import cloud_providers as provider
from autonomy_policy import HAMLE, scope_digest
from v11_artifact_inspector import INSPECTOR_ID, InspectionError, inspect_artifact
from v11_brand_task import HAMLE_PERSON
from v11_chain_mail import reply_subject
from v11_quality_gate import MANDATORY_CHECKS, build_quality_binding
from v11_work_queue import digest

ROOT = Path(__file__).resolve().parents[2]
RUBRIC_NAME = "gamechangers-internal-quality-rubric.json"
CUSTOMER_BODY = ("Merhaba,\n\nİstediğiniz çalışma ektedir. Hamle iç teknik kalite kontrolünden geçmiştir.\n\n"
                 "Teşekkürler,\nHamle AI")
IMAGE = ("image/png", "image/jpeg")
KIND_MIMES = {"graphic": IMAGE, "text": ("application/pdf", "text/plain")}
CRITERIA = ("file_format", "dimensions", "required_text", "brief_items")


class ChainQualityError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ChainQualityError(code)


def _stamp(value):
    d = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    _need(d.tzinfo is not None, "timestamp_timezone_required")
    return d.astimezone(timezone.utc)


def load_rubric(path=None):
    candidates = [Path(path)] if path else [Path(__file__).with_name("chain-profile") / RUBRIC_NAME,
                                            ROOT / "outputs/gamechangers-internal" / RUBRIC_NAME]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise ChainQualityError("rubric_missing")


def rubric_policy(rubric, catalog):
    """Accepted rubric -> v11_quality_gate policy. A draft never becomes a policy."""
    _need(isinstance(rubric, dict) and rubric.get("schema_version") == 1, "rubric_invalid")
    _need(rubric.get("status") == "accepted" and rubric.get("weights_status") == "accepted"
          and rubric.get("verified") is True and isinstance(rubric.get("evidence_ref"), str)
          and rubric.get("accepted_by") == rubric.get("acceptance_required_from")
          and isinstance(rubric.get("accepted_at"), str), "internal_rubric_not_accepted")
    _need(rubric.get("brand_id") == catalog.get("brand_id")
          and rubric.get("service_id") == catalog.get("chain", {}).get("default_service_id"), "rubric_scope_mismatch")
    ids = [c["id"] for c in rubric["criteria"]]
    _need(ids == list(CRITERIA) and sum(Decimal(c["weight"]) for c in rubric["criteria"]) == 100,
          "rubric_criteria_invalid")
    pid = rubric["rubric_id"]
    return {"schema_version": 1, "policy_id": pid, "version": rubric["version"],
            "brand_id": rubric["brand_id"], "service_id": rubric["service_id"], "status": "accepted",
            "verified": True, "evidence_ref": rubric["evidence_ref"], "accepted_at": rubric["accepted_at"],
            "expires_at": rubric["expires_at"], "approval_actors": list(rubric["approval_actors"]),
            "assessment_max_age_seconds": rubric["assessment_max_age_seconds"],
            "approval_max_age_seconds": rubric["approval_max_age_seconds"],
            "trusted_evaluators": {"deterministic": [INSPECTOR_ID], "human_review": [], "model_rubric": []},
            "critical_checks": list(MANDATORY_CHECKS),
            "critical_requirements": {n: {"requirement_refs": [pid + "/" + n], "methods": ["deterministic"]}
                                      for n in MANDATORY_CHECKS},
            "criteria": [{"id": c["id"], "description": c["description"], "weight": c["weight"], "critical": False,
                          "methods": ["deterministic"], "requirement_refs": [pid + "/" + c["id"]]}
                         for c in rubric["criteria"]],
            "thresholds": dict(rubric["thresholds"]), "internal_revision_limit": rubric["internal_revision_limit"],
            "scope_label": "internal_technical_conformance_only", "artistic_or_brand_quality_claim": False}


def _route(catalog):
    chain = catalog.get("chain", {})
    routes = [r for r in catalog.get("routes", []) if r.get("service_id") == chain.get("default_service_id")
              and r.get("category_key") == chain.get("default_category_key")]
    _need(len(routes) == 1 and type(routes[0].get("list_id")) is int, "default_route_unconfirmed")
    return routes[0]


def build_scope(catalog, rubric, policy, work_authority_policy=None):
    """Verified per-brand autonomy scope derived from the profile + accepted rubric."""
    contacts = [c for c in catalog["contacts"] if c.get("verified") is True and c.get("side") == "agency"]
    route = _route(catalog)
    owner = [c["email"] for c in contacts if "principal" in c.get("agency_roles", [])]
    _need(len(owner) == 1, "agency_owner_missing")
    bc = catalog["basecamp"]
    return {"schema_version": 1, "brand_id": catalog["brand_id"], "enabled": True,
            "authority": {"verified_by": rubric["accepted_by"], "verified_at": rubric["accepted_at"],
                          "expires_at": rubric["expires_at"], "evidence_ref": rubric["evidence_ref"]},
            "customer_contacts": sorted(c["email"] for c in contacts if c.get("can_request") is True),
            "agency_contacts": sorted(c["email"] for c in contacts),
            "allowed_cc_contacts": sorted(c["email"] for c in contacts if c.get("can_receive") is True),
            "agency_owner": owner[0],
            "basecamp": {"account_id": bc["account_id"], "project_id": bc["project_id"],
                         "list_ids": [route["list_id"]], "customer_visible": False,
                         "verification_ref": bc["verification_ref"]},
            "services": [{"service_id": route["service_id"], "actions": ["add_comment", "deliver_file"],
                          "revision_limit": 2, "list_id": route["list_id"],
                          "assignee_ids": sorted({i for ids in route["assignee_ids_by_kind"].values() for i in ids}),
                          "immediate_action_window_seconds": 300,
                          "delivery": {"accepted_qa_policy_ref": policy["policy_id"],
                                       "approved_assets_ref": rubric["approved_assets_ref"],
                                       "release_mode": "scored_quality_v1", "quality_policy": policy,
                                       "allowed_mime_types": list(rubric["allowed_mime_types"])},
                          **({"work_authority_policy": copy.deepcopy(work_authority_policy)}
                             if work_authority_policy else {})}]}


def record_quality_approval(queue, work_id, approval):
    """Durable store for an AUTHENTICATED human approval (future UI). Not wired to any UI;
    evaluate_quality still checks actor, binding, assessment hash, score and expiry."""
    key = "chain-approval-" + digest(work_id, approval.get("assessment_sha256"))
    def op(tx, now):
        tx.put(key, {"id": key, "kind": "chain_quality_approval", "work_id": work_id,
                     "assessment_sha256": approval.get("assessment_sha256"), "approval": copy.deepcopy(approval),
                     "recorded_at": now})
        return key
    return queue._run(op)


# ------------------------------------------------------------------ Basecamp reads
class BasecampAttachmentFetcher:
    """Fresh, read-only Basecamp reads for one verified private project.

    Bytes come through v11_internal_providers._download_bytes (the existing,
    read-only-verified transport: one GET, at most one storage.basecamp.com
    redirect without OAuth, 2 MiB cap). No signed URL is persisted.
    """
    API = "https://3.basecampapi.com"
    HOSTS = ("3.basecampapi.com", "storage.3.basecamp.com", "storage.app.basecamp.com")

    def __init__(self, catalog, *, token_provider, session_factory=provider._session):
        bc = catalog["basecamp"]
        _need(bc.get("verified") is True and bc.get("customer_visible") is False, "basecamp_project_unverified")
        self.account, self.project = bc["account_id"], bc["project_id"]
        self.token_provider, self.session_factory = token_provider, session_factory

    def _get(self, session, path):
        from v11_internal_providers import _transport
        return _transport(session, "GET", f"{self.API}/{self.account}/buckets/{self.project}/{path}",
                          self.token_provider())

    def read_task(self, task_id, list_id):
        session = self.session_factory()
        try:
            row = self._get(session, f"todos/{int(task_id)}.json")
        finally:
            session.close()
        assignees = row.get("assignees")
        _need(row.get("id") == task_id and row.get("bucket", {}).get("id") == self.project
              and row.get("parent", {}).get("id") == list_id and row.get("parent", {}).get("type") == "Todolist"
              and row.get("visible_to_clients") is False and row.get("status") == "active"
              and row.get("creator", {}).get("id") == HAMLE_PERSON and type(row.get("completed")) is bool
              and isinstance(assignees, list) and all(type(a.get("id")) is int for a in assignees),
              "task_scope_mismatch")
        stable = {k: row.get(k) for k in ("id", "content", "description", "completed", "due_on")}
        stable["assignee_ids"] = sorted(a["id"] for a in assignees)
        stable["parent"] = row["parent"]["id"]
        return {"assignee_ids": stable["assignee_ids"], "completed": row["completed"],
                "task_fingerprint": scope_digest(stable)}

    def _comment(self, session, task_id, comment_id):
        row = self._get(session, f"comments/{int(comment_id)}.json")
        _need(row.get("id") == int(comment_id) and row.get("type") == "Comment"
              and row.get("bucket", {}).get("id") == self.project
              and row.get("parent", {}).get("id") == task_id and row.get("parent", {}).get("type") == "Todo"
              and row.get("status") == "active" and row.get("visible_to_clients") is False
              and type(row.get("creator", {}).get("id")) is int and isinstance(row.get("created_at"), str),
              "comment_scope_mismatch")
        return row

    @staticmethod
    def _attachments(row):
        out = []
        for a in row.get("content_attachments") or []:
            _need(isinstance(a, dict) and type(a.get("id")) is int and isinstance(a.get("filename"), str)
                  and isinstance(a.get("content_type"), str) and type(a.get("byte_size")) is int,
                  "attachment_metadata_invalid")
            out.append({"attachment_id": a["id"], "filename": a["filename"],
                        "content_type": a["content_type"], "byte_size": a["byte_size"]})
        return out

    def read_comment(self, task_id, comment_id):
        session = self.session_factory()
        try:
            row = self._comment(session, task_id, comment_id)
        finally:
            session.close()
        return {"comment_id": int(comment_id), "creator_id": row["creator"]["id"],
                "created_at": _stamp(row["created_at"]).isoformat(), "attachments": self._attachments(row)}

    def download(self, task_id, comment_id, attachment):
        from v11_internal_providers import _download_bytes
        session = self.session_factory()
        try:
            row = self._comment(session, task_id, comment_id)
            match = [a for a in row.get("content_attachments") or [] if a.get("id") == attachment["attachment_id"]
                     and a.get("filename") == attachment["filename"]
                     and a.get("content_type") == attachment["content_type"]
                     and a.get("byte_size") == attachment["byte_size"]]
            _need(len(match) == 1 and isinstance(match[0].get("download_url"), str), "attachment_binding_mismatch")
            url = urlsplit(match[0]["download_url"])
            _need(url.scheme == "https" and url.netloc in self.HOSTS and url.path.startswith(f"/{self.account}/blobs/")
                  and "/download/" in url.path and not url.fragment, "attachment_download_url_invalid")
            data = _download_bytes(session, match[0]["download_url"], self.token_provider())
        finally:
            session.close()
        _need(type(data) is bytes and len(data) == attachment["byte_size"], "attachment_bytes_mismatch")
        return data


# ------------------------------------------------------------------ bridge
def _contained(fn):
    """Provider/transport exceptions become a static ValueError the coordinator/chain hold on."""
    try:
        return fn()
    except ChainQualityError:
        raise
    except Exception as exc:
        raise ChainQualityError("bridge_read_failed:" + str(getattr(exc, "code", None) or type(exc).__name__)[:80]) from None


class _Adapter:
    """DeliveryAdapter protocol implementation over one bridge context."""
    def __init__(self, bridge, view, sub):
        self.bridge, self.view, self.sub = bridge, copy.deepcopy(view), copy.deepcopy(sub)

    def read_basecamp(self, work, *, after_cursor):
        return _contained(lambda: self.bridge._snapshot(self.view, self.sub, after_cursor))

    def read_authority(self, work):
        ctx = _contained(lambda: self.bridge._context(self.view, self.sub))
        return {"scope": ctx["scope"], "request": ctx["request"]}


class ChainQualityBridge:
    def __init__(self, catalog, rubric, *, queue, fetcher, source_reader, clock,
                 authority_source=None, inspect=inspect_artifact, work_authority_policy=None):
        self.catalog, self.rubric = copy.deepcopy(catalog), copy.deepcopy(rubric)
        self.queue, self.fetcher, self.source_reader, self.clock = queue, fetcher, source_reader, clock
        self.authority_source = authority_source or (lambda work, scope, source: {})
        self.inspect = inspect
        self.work_authority_policy = copy.deepcopy(work_authority_policy)
        self.route = _route(self.catalog)

    # ----- durable rows (same backend as the chain)
    def _get(self, key):
        return self.queue._run(lambda tx, _: tx.get(key))

    def _put(self, key, row):
        def op(tx, now):
            row["updated_at"] = now
            tx.put(key, row)
            return row
        return self.queue._run(op)

    # ----- brief / spec
    def _deliverable(self, view):
        brief = view.get("brief") or {}
        finals = [d for d in brief.get("deliverables", []) if d.get("purpose", "final") == "final"]
        _need(len(finals) == 1 and finals[0].get("quantity") == 1, "single_file_scope_only")
        item = finals[0]
        kinds = KIND_MIMES.get(item.get("kind"))
        _need(kinds is not None, "deliverable_kind_not_inspectable")
        mimes = [m for m in item.get("formats", []) if m in self.rubric["allowed_mime_types"]]
        _need(mimes, "deliverable_format_not_inspectable")
        return item, mimes

    def _spec(self, item, mimes, content_type):
        spec = {"id": item["id"], "mime_types": mimes, "min_bytes": 1, "max_bytes": self.rubric["max_file_bytes"]}
        if type(item.get("width")) is int and type(item.get("height")) is int:
            spec["facts"] = {"width": item["width"], "height": item["height"]}
        spec["required_text"] = ([{"id": f"brief-{i}", "text": t, "critical": False}
                                  for i, t in enumerate(item.get("required_text", []))]
                                 + [{"id": f"brand-{i}", "text": t, "critical": False}
                                    for i, t in enumerate(self.rubric["brand_literals"])])
        spec["forbidden_text"] = [{"id": f"forbidden-{i}", "text": t, "critical": True}
                                  for i, t in enumerate(self.rubric["forbidden_literals"])]
        if content_type in IMAGE:
            spec["text_inspection"] = "tesseract"
        return spec

    def _brief_sha(self, view):
        return scope_digest(view["brief"])

    # ----- submission (download + inspect once per comment/attachment; cached)
    def _submission(self, view, comment_id):
        key = "chain-submission-" + digest(view["work_id"], int(comment_id))
        cached = self._get(key)
        if cached is not None:
            _need(cached.get("brief_sha256") == self._brief_sha(view), "brief_changed_after_inspection")
            return cached
        item, mimes = self._deliverable(view)
        comment = self.fetcher.read_comment(view["task_id"], comment_id)
        task = self.fetcher.read_task(view["task_id"], self.route["list_id"])
        producers = set(self._service_assignees())
        _need(comment["creator_id"] in task["assignee_ids"] and comment["creator_id"] in producers,
              "submission_not_by_assigned_producer")
        files = [a for a in comment["attachments"] if a["content_type"] in self.rubric["allowed_mime_types"]]
        _need(len(files) == 1 and len(comment["attachments"]) == 1, "single_supported_attachment_required")
        attachment = files[0]
        _need(0 < attachment["byte_size"] <= self.rubric["max_file_bytes"], "attachment_size_out_of_scope")
        data = self.fetcher.download(view["task_id"], comment_id, attachment)
        sha = hashlib.sha256(data).hexdigest()
        bc = self.catalog["basecamp"]
        source = {"verified": True, "account_id": bc["account_id"], "project_id": bc["project_id"],
                  "task_id": view["task_id"], "attachment_id": attachment["attachment_id"],
                  "evidence_ref": f"basecamp-attachment:{view['task_id']}/{comment_id}/{attachment['attachment_id']}"}
        now = self.clock()
        _need(_stamp(comment["created_at"]) <= now, "submission_in_future")
        try:
            inspection = self.inspect(data, declared_mime=attachment["content_type"], expected_sha256=sha,
                                      brief_sha256=self._brief_sha(view), source=source,
                                      output_spec=self._spec(item, mimes, attachment["content_type"]),
                                      checked_at=now, include_text=False)
        except InspectionError as exc:
            raise ChainQualityError("inspection_" + exc.code) from None
        row = {"id": key, "kind": "chain_submission", "work_id": view["work_id"], "comment_id": int(comment_id),
               "producer_id": comment["creator_id"], "submitted_at": comment["created_at"],
               "attachment": dict(attachment, sha256=sha), "deliverable_id": item["id"],
               "brief_sha256": self._brief_sha(view), "inspection": inspection}
        return self._put(key, row)

    def _service_assignees(self):
        return sorted({i for ids in self.route["assignee_ids_by_kind"].values() for i in ids})

    def delivery_brief(self, view):
        """Deterministic delivery brief (view brief + rubric); also bound by the work grant."""
        item, mimes = self._deliverable(view)
        return {"verified": True, "evidence_ref": view["brief"]["evidence_ref"],
                "required_deliverables": [{"id": item["id"], "mime_types": mimes,
                                           "facts": ({"width": item["width"], "height": item["height"]}
                                                     if type(item.get("width")) is int
                                                     and type(item.get("height")) is int else {}),
                                           "min_bytes": 1, "max_bytes": self.rubric["max_file_bytes"]}],
                "semantic_checks": ["rubric_technical_conformance_only"],
                "factual_checks": ["inspector_measured_bytes"]}

    def delivery_brief_sha256(self, view):
        return scope_digest(self.delivery_brief(view))

    def scope(self):
        """Verified autonomy scope (accepted rubric + optional accepted work-authority policy)."""
        policy = rubric_policy(self.rubric, self.catalog)
        return build_scope(self.catalog, self.rubric, policy, self.work_authority_policy)

    # ----- one immutable context shared by assessment, coordinator and authority
    def _context(self, view, sub):
        now = self.clock()
        policy = rubric_policy(self.rubric, self.catalog)
        scope = self.scope()
        work = self.queue.get_work(view["work_id"])
        control = self.queue.control()
        reply = view["reply"]
        source = self.source_reader(view["thread_id"], reply["in_reply_to_message_id"])
        _need(isinstance(source, dict) and source.get("thread_id") == view["thread_id"], "source_unverified")
        item, mimes = self._deliverable(view)
        inspection = sub["inspection"]
        deliverable = copy.deepcopy(inspection["artifact"])
        artifact = {"brand_id": work["brand_id"], "work_id": work["work_id"],
                    "version": f"bc-comment-{sub['comment_id']}", "sha256": deliverable["sha256"],
                    "mime_type": deliverable["mime_type"], "evidence_ref": deliverable["evidence_ref"],
                    "deliverables": [deliverable]}
        dwork = {k: work[k] for k in ("work_id", "brand_id", "service_id", "thread_id", "account_id", "project_id",
                                      "list_id", "task_id")}
        dwork["revision_index"] = 0
        dwork["brief"] = self.delivery_brief(view)
        request = {"brand_id": work["brand_id"], "service_id": work["service_id"], "work_id": work["work_id"],
                   "revision_index": 0, "risk_area": "routine", "open_escalation": False,
                   # The work-authority grant binds this same delivery brief (quality_brief_sha256).
                   "work_brief_sha256": scope_digest(dwork["brief"]),
                   "reply_to": list(reply["to"]), "reply_cc": list(reply["cc"]),
                   "control": {"work_id": work["work_id"], "observed_at": now.isoformat(),
                               "global_mode": "routine_enabled" if control["mode"] == "queue_open" else control["mode"],
                               "work_mode": work["mode"], "global_generation": control["generation"],
                               "work_generation": work["work_generation"]},
                   "source": {k: source[k] for k in ("actual_latest", "own_or_automatic", "provider_evidence_ref",
                                                     "message_id", "thread_id", "fingerprint", "sender", "to",
                                                     "cc", "received_at", "captured_at")}}
        extra = self.authority_source(copy.deepcopy(dwork), copy.deepcopy(scope), copy.deepcopy(request["source"]))
        _need(isinstance(extra, dict) and set(extra) <= {"delegation", "fresh_work_authorization",
                                                        "work_authority_grant", "work_authority_observation",
                                                        "budget_authorization"}, "authority_source_invalid")
        request.update(copy.deepcopy(extra))
        public = {k: artifact[k] for k in ("brand_id", "work_id", "version", "sha256", "mime_type")}
        subject = reply_subject(source.get("subject"))
        payload = {"from": HAMLE, "to": request["reply_to"], "cc": request["reply_cc"],
                   "thread_id": work["thread_id"], "subject": subject, "body": CUSTOMER_BODY, "artifact": public}
        binding = build_quality_binding({**request, "artifact": public,
                                         "prepared_action": {"payload_sha256": scope_digest(payload)}},
                                        brief_sha256=scope_digest(dwork["brief"]),
                                        manifest_sha256=scope_digest(artifact["deliverables"]),
                                        scope_sha256=scope_digest(scope))
        evidence = {k: binding[k] for k in ("brand_id", "work_id", "version", "sha256", "brief_sha256",
                                            "manifest_sha256", "scope_digest", "source_fingerprint",
                                            "recipients_sha256")}
        return {"now": now, "policy": policy, "scope": scope, "work": work, "control": control, "dwork": dwork,
                "request": request, "artifact": artifact, "payload": payload, "binding": binding,
                "evidence_binding": evidence, "subject": subject}

    # ----- assessment from the cached inspection
    def _assessment(self, ctx, sub):
        policy, binding, ins = ctx["policy"], ctx["binding"], sub["inspection"]
        checks = ins["checks"]
        pid = policy["policy_id"]
        ref = "inspection:" + ins["evidence_sha256"]
        item = next(d for d in ctx["dwork"]["brief"]["required_deliverables"])
        brief_item = next(d for d in ctx["_view_brief"]["deliverables"] if d["id"] == item["id"])

        def obs(names, extra=()):
            rows = [(checks[n]["location"], checks[n]["finding"]) for n in names if n in checks]
            return [{"evidence_ref": ref, "location": loc, "finding": text, "artifact_sha256": binding["sha256"],
                     "brief_sha256": binding["brief_sha256"]} for loc, text in list(rows) + list(extra)] or \
                [{"evidence_ref": ref, "location": "rubric", "finding": "Rubric rule applied to inspector evidence.",
                  "artifact_sha256": binding["sha256"], "brief_sha256": binding["brief_sha256"]}]

        def record(name, result, observations, score=None):
            if result == "unassessed":
                return {"result": "unassessed"}
            row = {"verified": True, "binding": copy.deepcopy(binding), "method": "deterministic",
                   "evaluator_id": INSPECTOR_ID, "evidence_ref": ref, "checked_at": ins["checked_at"],
                   "requirement_refs": [pid + "/" + name], "observations": observations, "result": result}
            if score is not None:
                row["score"] = score
            return row

        def merged(names):
            values = [checks[n]["result"] for n in names]
            return "fail" if "fail" in values else "unassessed" if "unassessed" in values else "pass"

        def scored(name, passed, total, observations):
            if total == 0:
                return record(name, "unassessed", observations)
            # 4 decimals: integer-percent weights keep the weighted total within the gate's
            # 6-decimal limit, so a human approval can state the exact same score.
            value = (Decimal(100) * passed / total).quantize(Decimal("0.0001"), rounding=ROUND_HALF_EVEN).normalize()
            text = format(value, "f")
            result = "passed" if value == 100 else "failed" if value == 0 else "partial"
            return record(name, result, observations, text)

        fmt = merged(["mime_type", "size_bytes"])
        dims = [n for n in ("fact:width", "fact:height") if n in checks]
        brief_text = [n for n in checks if n.startswith("required_text:brief-")]
        brand = [n for n in checks if n.startswith("required_text:brand-")]
        forbidden = [n for n in checks if n.startswith("forbidden_text:")]
        kind_ok = ins["artifact"]["mime_type"] in KIND_MIMES.get(brief_item.get("kind"), ())
        criteria = {
            "file_format": (record("file_format", "unassessed", []) if fmt == "unassessed" else
                            scored("file_format", 1 if fmt == "pass" else 0, 1, obs(["mime_type", "size_bytes"]))),
            "dimensions": (record("dimensions", "unassessed", []) if not dims or merged(dims) == "unassessed" else
                           scored("dimensions", sum(checks[n]["result"] == "pass" for n in dims), len(dims), obs(dims))),
            "required_text": (record("required_text", "unassessed", []) if not brief_text
                              or merged(brief_text) == "unassessed" else
                              scored("required_text", sum(checks[n]["result"] == "pass" for n in brief_text),
                                     len(brief_text), obs(brief_text))),
            "brief_items": scored("brief_items", 1 if kind_ok else 0, 1, obs([], [(
                "submission attachments", "Exactly one supported file for the single final deliverable; kind "
                + ("matches." if kind_ok else "does not match the brief."))]))}
        tech_names = ["mime_type", "size_bytes"] + dims
        tech = merged(tech_names)
        brand_found = any(checks[n]["result"] == "pass" for n in brand)
        forbidden_hit = any(checks[n]["result"] == "fail" for n in forbidden)
        declared = self.rubric.get("rights_declaration", {}).get("accepted") is True
        critical = {
            "brand_identity": (record("brand_identity", "passed", obs([n for n in brand if checks[n]["result"] == "pass"]))
                               if brand_found else record("brand_identity", "unassessed", [])),
            "required_files": record("required_files", "passed" if kind_ok else "failed",
                                     criteria["brief_items"]["observations"]),
            "technical_specs": (record("technical_specs", "unassessed", []) if tech == "unassessed" else
                                record("technical_specs", "passed" if tech == "pass" else "failed", obs(tech_names))),
            "rights_and_disclosure": (record("rights_and_disclosure", "passed", obs([], [(
                "accepted rubric", self.rubric["rights_declaration"]["text"])])) if declared
                else record("rights_and_disclosure", "unassessed", [])),
            "internal_notes_excluded": record(
                "internal_notes_excluded", "failed" if forbidden_hit else "passed",
                obs([n for n in forbidden if checks[n]["result"] == "fail"], [] if forbidden_hit else [(
                    "delivery construction", "Mail carries only the producer's downloaded file bytes and a fixed "
                    "template body; no comment text. No forbidden literal was detected.")]))}
        return {"schema_version": 1, "verified": True, "evidence_ref": ref, "binding": copy.deepcopy(binding),
                "policy_digest": scope_digest(policy), "checked_at": ins["checked_at"],
                "critical_checks": critical, "criteria": criteria}

    def _safety(self, ctx):
        work, control = ctx["work"], ctx["control"]
        active = control["mode"] == "queue_open" and work["mode"] == "active"
        clear = active and work.get("inflight_action_id") is None
        return {"verified": True, "binding": copy.deepcopy(ctx["binding"]), "observed_at": ctx["now"].isoformat(),
                "evidence_ref": f"queue-control:{control['generation']}/{work['work_generation']}",
                "control": "active" if active else "held", "authority": "allowed" if active else "held",
                "security": "clear", "write_state": "new" if clear else "held"}

    def _evidence(self, view, sub):
        ctx = self._context(view, sub)
        ctx["_view_brief"] = view["brief"]
        assessment = self._assessment(ctx, sub)
        return ctx, {"policy": ctx["policy"], "assessment": assessment, "binding": ctx["binding"],
                     "safety": self._safety(ctx), "artifact_version": ctx["artifact"]["version"]}

    # ----- ChainDeps hooks
    def assess(self, view, comment):
        try:
            rubric_policy(self.rubric, self.catalog)
            sub = self._submission(view, comment["id"])
            self._put("chain-current-" + digest(view["work_id"]),
                      {"id": "chain-current-" + digest(view["work_id"]), "kind": "chain_current_submission",
                       "work_id": view["work_id"], "comment_id": sub["comment_id"]})
            _, evidence = self._evidence(view, sub)
            return evidence
        except Exception as exc:  # any read/inspection doubt -> no evidence -> chain holds for managers
            self.last_reason = getattr(exc, "code", None) or type(exc).__name__
            return None

    def _current(self, view):
        row = self._get("chain-current-" + digest(view["work_id"]))
        _need(row is not None, "no_current_submission")
        sub = self._get("chain-submission-" + digest(view["work_id"], row["comment_id"]))
        _need(sub is not None and sub["brief_sha256"] == self._brief_sha(view), "current_submission_changed")
        return sub

    def approval_reader(self, view, comment):
        try:
            sub = self._current(view)
            _, evidence = self._evidence(view, sub)
        except Exception:
            return None
        row = self._get("chain-approval-" + digest(view["work_id"], scope_digest(evidence["assessment"])))
        return copy.deepcopy(row["approval"]) if row else None

    def bridge(self, view):
        sub = _contained(lambda: self._current(view))
        ctx = _contained(lambda: self._context(view, sub))
        return copy.deepcopy(ctx["dwork"]), _Adapter(self, view, sub)

    def _snapshot(self, view, sub, after_cursor):
        ctx, evidence = self._evidence(view, sub)
        task = self.fetcher.read_task(view["task_id"], self.route["list_id"])
        cursor = f"chain-submission-{sub['comment_id']}"
        events = [] if after_cursor == cursor else [{
            "id": f"submission-{sub['comment_id']}", "task_id": view["task_id"], "source_order": sub["comment_id"],
            "evidence_ref": f"basecamp-comment:{sub['comment_id']}"}]
        approval = None
        row = self._get("chain-approval-" + digest(view["work_id"], scope_digest(evidence["assessment"])))
        if row is not None:
            approval = copy.deepcopy(row["approval"])
        release_key = "chain-release-" + digest(view["work_id"], ctx["payload"]["artifact"]["sha256"],
                                                scope_digest(ctx["payload"]))
        release = {"id": release_key, "kind": "chain_release", "work_id": view["work_id"], **ctx["evidence_binding"],
                   "verified": True, "actor": "hamle-software-release", "release_mode": "scored_quality_v1",
                   "assessment_sha256": scope_digest(evidence["assessment"]),
                   "action_payload_sha256": scope_digest(ctx["payload"]),
                   "human_approval_sha256": scope_digest(approval) if approval else None,
                   "customer_delivery": False, "internal_delivery": True, "issued_at": ctx["now"].isoformat()}
        if self._get(release_key) is None:
            self._put(release_key, copy.deepcopy(release))
        wb = {k: ctx["dwork"][k] for k in ("work_id", "brand_id", "service_id", "thread_id", "account_id",
                                           "project_id", "list_id", "task_id")}
        return {"binding": wb, "observed_at": ctx["now"].isoformat(),
                "evidence_ref": f"basecamp-task:{view['task_id']}", "previous_cursor": after_cursor,
                "cursor": cursor, "has_more": False, "completed": task["completed"],
                "task_fingerprint": task["task_fingerprint"], "assignee_ids": task["assignee_ids"],
                "events": events, "artifact": ctx["artifact"],
                "producer_submission": {**ctx["evidence_binding"], "verified": True, "actor_id": sub["producer_id"],
                                        "issued_at": sub["submitted_at"],
                                        "evidence_ref": f"basecamp-comment:{sub['comment_id']}"},
                "qa": None, "release": release,
                "customer_subject": ctx["subject"], "customer_body": CUSTOMER_BODY,
                "quality_assessment": evidence["assessment"], "quality_safety": evidence["safety"],
                "quality_human_approval": approval, "quality_assets_ref": self.rubric["approved_assets_ref"]}

    def attachment_source(self, artifact):
        """Final re-download + hash check right before preflight/send (never cached bytes)."""
        work_id = artifact["work_id"]
        row = self._get("chain-current-" + digest(work_id))
        _need(row is not None, "no_current_submission")
        sub = self._get("chain-submission-" + digest(work_id, row["comment_id"]))
        _need(sub is not None and sub["attachment"]["sha256"] == artifact["sha256"], "artifact_not_current")
        work = self.queue.get_work(work_id)
        att = {k: sub["attachment"][k] for k in ("attachment_id", "filename", "content_type", "byte_size")}
        data = _contained(lambda: self.fetcher.download(work["task_id"], sub["comment_id"], att))
        _need(hashlib.sha256(data).hexdigest() == artifact["sha256"], "artifact_bytes_changed")
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", att["filename"]).strip("-.")[:100] or "teslim"
        return {"filename": name, "data": data, "content_type": att["content_type"]}
