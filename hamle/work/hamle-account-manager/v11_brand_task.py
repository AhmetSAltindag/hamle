"""Brand-profile-bound production task: verified intake brief -> queue -> Basecamp.

Connects existing modules instead of widening the internal-only writer:
  v11_intake_plan (verified brief + profile route) -> v11_production_stages
  (ready stage) -> v11_work_queue (begin/confirm write) -> BrandTaskWriter.

The task body is re-derived from the CURRENT versioned brand profile and the
brief at prepare time and again inside the writer. A changed profile, route,
producer or brief fails closed and needs a fresh plan. The writer accepts only
the profile's verified account/project/list, the stage's profile producers and
the profile's oversight roles. A profile with live_enabled=false (e.g. WAT
today) can be prepared and tested but never POSTs. Every POST is one attempt;
an uncertain outcome is held in the queue and never retried automatically.
"""
from __future__ import annotations

import copy
import re

import basecamp_cloud_auth
import business_writers as bw
import cloud_providers as provider
from autonomy_policy import BRANDS, scope_digest
from v11_intake_plan import IntakeError, _contacts, _route
from v11_production_stages import ready_stages
from v11_work_queue import WRITE_BINDING_FIELDS, QueueError

ORIGIN = "https://3.basecampapi.com"
HAMLE_PERSON = basecamp_cloud_auth.PERSON
ACCOUNT = basecamp_cloud_auth.ACCOUNT
KIND_NAMES = {"graphic": "statik tasarım", "animation": "animasyon", "video": "video", "text": "metin"}
TASK_FIELDS = {"account_id", "project_id", "todolist_id", "title", "description",
               "assignee_ids", "completion_subscriber_ids", "notify", "due_on"}
PAYLOAD_FIELDS = {"brand_id", "work_id", "stage_id", "catalog_version", "catalog_sha256",
                  "brief_sha256", "task"}


class BrandTaskError(ValueError):
    retry_allowed = False
    def __init__(self, code):
        super().__init__(code); self.code = code


def _require(ok, code):
    if not ok: raise BrandTaskError(code)


def _ids(value):
    return isinstance(value, list) and value == sorted(set(value)) and all(type(v) is int and v > 0 for v in value)


def _requester(brief, catalog):
    origin = brief["origin"]
    names = {c["email"]: c.get("name") for c in catalog["contacts"]}
    if origin["kind"] == "direct_email":
        return (names.get(origin["actor_email"]) or "doğrulanmış kişi") + ", doğrudan e-posta"
    relay = names.get(origin["attested_by"]) or "ajans"
    return f"{origin['original_actor_name']} ({relay} aracılığıyla, {origin['medium']})"


def _describe(brief, catalog, stage, stages):
    people = catalog.get("basecamp_people", {})
    name = lambda pid: people.get(str(pid), {}).get("name") or str(pid)
    outputs = {o["id"]: o for o in brief["deliverables"]}
    lines = [f"Marka: {brief['brand_id']} · Profil: {catalog['version']}",
             f"Hizmet: {brief['binding']['category_name']} ({brief['service_id']})",
             f"Bu görevin aşaması: {stage['id']} · Üretici: " + ", ".join(name(p) for p in stage["assignee_ids"])]
    later = [s for s in stages if s["id"] != stage["id"]]
    if later:
        lines.append("Sonraki aşamalar: " + "; ".join(
            f"{s['id']} ({', '.join(name(p) for p in s['assignee_ids'])}, {'+'.join(s['depends_on'])} kabulünden sonra)"
            if s["depends_on"] else s["id"] for s in later))
    lines += ["", "Talep eden: " + _requester(brief, catalog),
              "Amaç: " + brief["objective"], "Hedef kitle: " + brief["audience"],
              "Teslim tarihi: " + brief["deadline"], "Onaylı marka dosyaları: " + brief["assets_ref"],
              "", "Bu aşamadaki çıktılar:"]
    for oid in stage["output_ids"]:
        o = outputs[oid]
        extra = f", {o['duration_seconds']} sn" if o.get("duration_seconds") else ""
        lines.append(f"- {oid}: {KIND_NAMES[o['kind']]}, {o['quantity']} adet, "
                     f"{', '.join(o['formats'])}{extra}. {o['specification']}")
    lines += ["", f"Brief sürümü: {brief['version']} · Kanıt: {brief['evidence_ref']}",
              "Sorunuz olursa bu görevin altına yazın; brief değişirse Hamle görevi güncel sürümle yeniden bağlar."]
    return "\n".join(lines)


def build_stage_task(state, catalog, stage_id):
    """Deterministic Basecamp body for one stage, from brief + current profile."""
    brief = state["brief"]
    try:
        contacts = _contacts(catalog)
        _require(catalog["brand_id"] in BRANDS and brief.get("brand_id") == catalog["brand_id"], "brand_profile_mismatch")
        _require(brief.get("catalog_version") == catalog["version"]
                 and brief.get("catalog_sha256") == scope_digest(catalog), "brand_profile_changed_replan_required")
        binding = brief["binding"]
        current = _route(catalog, {"service_id": brief["service_id"], "category_key": binding["category_key"],
                                   "deliverables": brief["deliverables"]}, contacts)
    except IntakeError as exc:
        raise BrandTaskError("brand_profile_" + str(exc)) from None
    _require(current == binding, "brand_route_changed_replan_required")
    _require(state["brief_sha256"] == scope_digest(brief), "brief_digest_mismatch")
    stages = [s["spec"] for s in state["stages"].values()]
    stage = state["stages"].get(stage_id)
    _require(stage is not None, "unknown_stage")
    spec = stage["spec"]
    planned = next((s for s in binding["production_stages"] if s["id"] == stage_id), None)
    _require(planned is not None and planned["assignee_ids"] == spec["assignee_ids"], "stage_producers_changed")
    title = brief["title"] if len(stages) == 1 else f"{brief['title']} · {stage_id}"
    _require(isinstance(title, str) and 0 < len(title) <= 200 and not any(c in title for c in "\r\n\t"),
             "task_title_invalid")
    description = _describe(brief, catalog, spec, stages)
    _require(len(description) <= 4000, "task_description_too_long")
    return {"account_id": binding["account_id"], "project_id": binding["project_id"],
            "todolist_id": binding["list_id"], "title": title, "description": description,
            "assignee_ids": list(spec["assignee_ids"]), "completion_subscriber_ids": list(binding["subscriber_ids"]),
            "notify": False, "due_on": None}


def prepare_stage_task(state, catalog, stage_id, work, claim):
    """Immutable create_task intent for a ready stage of this exact queue work."""
    _require(state.get("schema_version") == 1 and state.get("mode") == "production", "production_state_required")
    _require(stage_id in [s["id"] for s in ready_stages(state)]
             and state["stages"][stage_id]["task"] is None, "stage_not_ready_for_task")
    brief = state["brief"]
    binding = brief["binding"]
    _require(isinstance(work, dict) and work.get("work_id") == state["work_id"]
             and work.get("brand_id") == brief["brand_id"] and work.get("service_id") == brief["service_id"]
             and work.get("thread_id") == brief["reply_envelope"]["thread_id"]
             and (work.get("account_id"), work.get("project_id"), work.get("list_id")) ==
                 (binding["account_id"], binding["project_id"], binding["list_id"]), "queue_work_binding_mismatch")
    first = not state["stages"][stage_id]["spec"]["depends_on"]
    _require(work.get("task_id") is None if first else work.get("task_id") is not None, "queue_task_binding_state_invalid")
    event = (claim or {}).get("event") or {}
    _require(claim.get("work_id") == work["work_id"] and event.get("fingerprint") == brief["source_fingerprint"],
             "queue_event_not_brief_source")
    payload = {"brand_id": brief["brand_id"], "work_id": work["work_id"], "stage_id": stage_id,
               "catalog_version": catalog["version"], "catalog_sha256": scope_digest(catalog),
               "brief_sha256": state["brief_sha256"], "task": build_stage_task(state, catalog, stage_id)}
    digest = scope_digest(payload)
    return {"payload": payload, "payload_sha256": digest,
            "action_key": f"create-stage-task-{stage_id}-{state['brief_sha256'][:16]}",
            "prepared_binding": {**{k: work[k] for k in WRITE_BINDING_FIELDS},
                                 "queue_event_fingerprint": event["fingerprint"]},
            "live_enabled": catalog.get("live_enabled") is True}


class BrandTaskWriter:
    """Narrow Basecamp to-do writer for one verified, live-enabled brand profile.

    token_provider() returns a current OAuth access token (e.g. from
    basecamp_cloud_auth.access_token); session_factory() a no-redirect session.
    Both are injected so tests never reach the network.
    """

    def __init__(self, catalog, state, *, token_provider, session_factory=provider._session):
        self.catalog, self.state = copy.deepcopy(catalog), copy.deepcopy(state)
        self.token_provider, self.session_factory = token_provider, session_factory

    def _payload(self, payload):
        _require(isinstance(payload, dict) and set(payload) == PAYLOAD_FIELDS
                 and isinstance(payload["task"], dict) and set(payload["task"]) == TASK_FIELDS, "brand_task_schema_invalid")
        _require(payload["catalog_sha256"] == scope_digest(self.catalog)
                 and payload["catalog_version"] == self.catalog["version"]
                 and payload["brand_id"] == self.catalog["brand_id"]
                 and payload["work_id"] == self.state["work_id"]
                 and payload["brief_sha256"] == self.state["brief_sha256"], "brand_profile_changed_replan_required")
        expected = build_stage_task(self.state, self.catalog, payload["stage_id"])
        _require(payload["task"] == expected, "brand_task_not_profile_derived")
        task = payload["task"]
        _require(task["account_id"] == ACCOUNT and _ids(task["assignee_ids"])
                 and _ids(task["completion_subscriber_ids"]) and task["notify"] is False
                 and task["due_on"] is None, "brand_task_scope_invalid")
        return payload

    def _live(self):
        _require(self.catalog.get("live_enabled") is True
                 and self.catalog.get("provider_mutation_allowed_by_this_file") is True, "brand_profile_live_disabled")

    def _base(self, task):
        return f"{ORIGIN}/{task['account_id']}/buckets/{task['project_id']}"

    def _request(self, session, method, url, token, task, *, body=None, expected=200):
        base = re.escape(self._base(task))
        allowed = ((method == "GET" and url == f"{self._base(task)}/todolists/{task['todolist_id']}.json")
                   or (method == "POST" and url == f"{self._base(task)}/todolists/{task['todolist_id']}/todos.json")
                   or (method == "GET" and re.fullmatch(base + r"/todos/[0-9]{1,30}\.json", url)))
        _require(allowed, "provider_endpoint_forbidden")
        response = None
        try:
            encoded = None if body is None else provider._encode(body)
            response = session.request(method, url, headers={
                "Authorization": "Bearer " + provider._token(token), "Content-Type": "application/json",
                "User-Agent": "Hamle Brand Task Writer (ahmet@gamechangers.com.tr)"},
                data=encoded, timeout=(10, 30), allow_redirects=False, stream=True)
            _require(response.status_code == expected, "provider_response_unconfirmed")
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=16384):
                total += len(chunk)
                _require(total <= 262144, "provider_response_too_large")
                chunks.append(chunk)
            result = provider._json(b"".join(chunks))
            _require(isinstance(result, dict), "provider_response_invalid")
            return result
        except BrandTaskError: raise
        except Exception: raise BrandTaskError("provider_outcome_unknown_no_retry") from None
        finally:
            if response is not None:
                try: response.close()
                except Exception: pass

    def create_task(self, payload, *, before_post=None):
        _require(callable(before_post), "durable_before_post_required")
        payload = self._payload(payload); self._live()
        task = payload["task"]
        session = self.session_factory()
        try:
            token = self.token_provider()
            target = self._request(session, "GET", f"{self._base(task)}/todolists/{task['todolist_id']}.json", token, task)
            _require(target.get("id") == task["todolist_id"] and target.get("type") == "Todolist"
                     and target.get("bucket", {}).get("id") == task["project_id"]
                     and target.get("status") == "active" and target.get("visible_to_clients") is False,
                     "basecamp_target_changed")
            before_post()
            row = self._request(session, "POST", f"{self._base(task)}/todolists/{task['todolist_id']}/todos.json",
                token, task, body={"content": task["title"], "description": bw._description(task["description"]),
                                   "assignee_ids": task["assignee_ids"],
                                   "completion_subscriber_ids": task["completion_subscriber_ids"],
                                   "notify": False}, expected=201)
            _require(type(row.get("id")) is int and row["id"] > 0
                     and row.get("bucket", {}).get("id") == task["project_id"], "task_receipt_unconfirmed")
            return {"provider_id": str(row["id"])}
        except BrandTaskError: raise
        except Exception: raise BrandTaskError("task_outcome_unknown_no_retry") from None
        finally: session.close()

    def read_task(self, provider_id, payload):
        payload = self._payload(payload)
        task = payload["task"]
        _require(isinstance(provider_id, str) and re.fullmatch(r"[0-9]{1,30}", provider_id), "task_receipt_invalid")
        session = self.session_factory()
        try:
            row = self._request(session, "GET", f"{self._base(task)}/todos/{provider_id}.json", self.token_provider(), task)
            _require(row.get("id") == int(provider_id) and row.get("bucket", {}).get("id") == task["project_id"]
                     and row.get("parent", {}).get("id") == task["todolist_id"]
                     and row.get("parent", {}).get("type") == "Todolist"
                     and row.get("creator", {}).get("id") == HAMLE_PERSON
                     and row.get("status") == "active" and row.get("visible_to_clients") is False
                     and row.get("completed") is False and row.get("due_on") is None
                     and row.get("content") == task["title"]
                     and bw._description_matches(row.get("description"), task["description"]), "task_readback_mismatch")
            people = lambda key: sorted(p.get("id") for p in row.get(key) or [] if isinstance(p, dict))
            _require(people("assignees") == task["assignee_ids"], "task_readback_assignee_mismatch")
            _require(people("completion_subscribers") == task["completion_subscriber_ids"], "task_readback_subscriber_mismatch")
            return {"provider_id": provider_id, "identity_verified": True, "matches_approved_payload": True,
                    "readback_sha256": scope_digest(payload)}
        except BrandTaskError: raise
        except Exception: raise BrandTaskError("task_readback_unconfirmed") from None
        finally: session.close()


def create_stage_task(queue, claim, state, catalog, stage_id, writer, *, authorize):
    """One queue-fenced, read-back-verified stage task. Never retries.

    authorize(prepared) is the caller's fresh autonomy/budget/stop/To-CC check;
    it runs at the POST boundary, immediately before queue.begin_write.
    Returns task evidence shaped for a production bind_stage_task event.
    """
    result = {"status": "held", "reason": None, "automatic_retry_allowed": False, "network_writes": 0}
    action = None
    try:
        work = queue.get_work(claim["work_id"])
        prepared = prepare_stage_task(state, catalog, stage_id, work, claim)
        def before_post():
            nonlocal action
            _require(action is None, "provider_dispatch_callback_repeated")
            _require(authorize(copy.deepcopy(prepared)) is True, "fresh_authorization_denied")
            action = queue.begin_write(claim, action_kind="create_task", payload_sha256=prepared["payload_sha256"],
                                       action_key=prepared["action_key"], prepared_binding=prepared["prepared_binding"])
        try:
            receipt = writer.create_task(copy.deepcopy(prepared["payload"]), before_post=before_post)
            _require(action is not None, "provider_dispatch_callback_missing")
            pid = receipt.get("provider_id") if isinstance(receipt, dict) else None
            _require(isinstance(pid, str) and re.fullmatch(r"[0-9]{1,30}", pid), "provider_receipt_unconfirmed")
            readback = writer.read_task(pid, copy.deepcopy(prepared["payload"]))
            _require(readback.get("provider_id") == pid and readback.get("identity_verified") is True
                     and readback.get("matches_approved_payload") is True, "provider_readback_unconfirmed")
            queue.confirm_write(action_id=action["id"], claim_token=claim["token"],
                                payload_sha256=prepared["payload_sha256"],
                                receipt={"provider": "basecamp", "remote_id": pid,
                                         "readback_sha256": readback["readback_sha256"]})
        except Exception:
            if action is not None:
                try: queue.hold_unknown(action["id"])
                except Exception: pass  # The started action still forbids any resend.
                raise BrandTaskError("provider_write_outcome_unknown_no_retry") from None
            raise
        task = prepared["payload"]["task"]
        if not state["stages"][stage_id]["spec"]["depends_on"]:
            queue.bind_task(work["work_id"], task_id=int(pid), account_id=task["account_id"],
                            project_id=task["project_id"], list_id=task["todolist_id"],
                            expected_generation=claim["global_generation"])
        result.update(status="task_created_readback_verified", reason="profile_bound_task", network_writes=1,
                      stage_id=stage_id, action_id=action["id"], stage_task={
                          "provider": "basecamp", "verified": True, "evidence_ref": f"basecamp-readback:{pid}",
                          "account_id": task["account_id"], "project_id": task["project_id"],
                          "list_id": task["todolist_id"], "task_id": int(pid), "customer_visible": False,
                          "assignee_ids": task["assignee_ids"], "brief_sha256": state["brief_sha256"]})
    except (BrandTaskError, QueueError) as exc:
        result["reason"] = exc.code
        result["network_writes"] = int(action is not None)
    except (ValueError, TypeError, KeyError, AttributeError):
        result["reason"] = "malformed_brand_task_evidence"
        result["network_writes"] = int(action is not None)
    return result
