"""Explicit one-day internal Scheduler pilot; no I/O on import or default plan.

Production phases are operator-invoked. This file does not provision identities,
grant new roles, access provider secrets, run a model, or approve business work.
Unknown mutation outcomes remain journaled for reconciliation, never replayed.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys

PROJECT = "gamechangers-hamle"
REGION = "europe-west1"
JOB = "hamle-account-manager"
SA = "hamle-scheduler@gamechangers-hamle.iam.gserviceaccount.com"
MEMBER = "serviceAccount:" + SA
PARENT = f"projects/{PROJECT}/locations/{REGION}"
RUN_RESOURCE = PARENT + "/jobs/" + JOB
RUN_URL = "https://run.googleapis.com/v2/" + RUN_RESOURCE
SCHEDULER_URL = "https://cloudscheduler.googleapis.com/v1/" + PARENT + "/jobs"
OPEN = datetime.fromisoformat("2026-10-12T05:30:00+00:00")
CLOSE = datetime.fromisoformat("2026-10-12T15:35:00+00:00")
CONDITION = {
    "title": "hamle_scheduler_20261012_only",
    "description": "Only 12 October 2026 internal review slots; no customer dispatch.",
    "expression": 'request.time >= timestamp("2026-10-12T05:30:00Z") && request.time < timestamp("2026-10-12T15:35:00Z")',
}
SCHEDULES = (
    ("hamle-pilot-20261012-early", "30,45 8 12 10 *"),
    ("hamle-pilot-20261012-day", "*/15 9-17 12 10 *"),
    ("hamle-pilot-20261012-late", "0,15,30 18 12 10 *"),
)
SENTINEL = "0 0 1 1 *"  # Jan 1 is always outside the exact IAM date window.
HOLD_KEY = "cloud-scheduler-pilot-20261012-v1"
HOLD_AMOUNT = 1_000_000
TASK_TIMEOUT_SECONDS = 600
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs/new-mac-2026-10-10"
JOURNAL_PATH = OUT / "scheduler-pilot-journal.json"
JSON_BODY = base64.b64encode(b"{}").decode("ascii")


class PilotError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(ok, code):
    if not ok:
        raise PilotError(code)


def now_utc():
    return datetime.now(timezone.utc)


def outside_window(now):
    require(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
    return not OPEN <= now < CLOSE


def job_spec(name, schedule):
    return {"name": PARENT + "/jobs/" + name,
            "description": "Hamle internal review only, 12 October 2026; exact-date IAM guard.",
            "schedule": schedule, "timeZone": "Europe/Istanbul", "attemptDeadline": "30s",
            "retryConfig": {"retryCount": 0, "maxRetryDuration": "0s"},
            "httpTarget": {"uri": RUN_URL + ":run", "httpMethod": "POST",
                           "headers": {"Content-Type": "application/json"}, "body": JSON_BODY,
                           "oauthToken": {"serviceAccountEmail": SA,
                                          "scope": "https://www.googleapis.com/auth/cloud-platform"}}}


def verify_job(row, name, schedule, *, state="PAUSED"):
    expected = job_spec(name, schedule)
    require(isinstance(row, dict) and row.get("name") == expected["name"]
            and row.get("schedule") == schedule and row.get("timeZone") == "Europe/Istanbul"
            and row.get("state") == state and row.get("attemptDeadline") == "30s", "scheduler_job_mismatch")
    retry = row.get("retryConfig", {})
    require(retry.get("retryCount", 0) == 0 and retry.get("maxRetryDuration", "0s") == "0s", "scheduler_retry_enabled")
    target = row.get("httpTarget", {})
    require(set(target) <= {"uri", "httpMethod", "headers", "body", "oauthToken"}
            and all(target.get(k) == expected["httpTarget"][k] for k in
                    ("uri", "httpMethod", "body", "oauthToken")), "scheduler_target_mismatch")
    headers = {k.lower(): v for k, v in target.get("headers", {}).items()}
    require(headers.get("content-type") == "application/json"
            and set(headers) <= {"content-type", "user-agent"}, "scheduler_headers_mismatch")
    return True


def conditional_policy(policy):
    """Narrow exactly the existing scheduler grant, retaining etag/other members."""
    p = copy.deepcopy(policy)
    require(isinstance(p, dict) and isinstance(p.get("etag"), str) and p["etag"], "iam_etag_required")
    require(p.get("version", 1) in (1, 3) and isinstance(p.get("bindings"), list), "iam_policy_invalid")
    found = 0; bindings = []
    for binding in p["bindings"]:
        require(isinstance(binding, dict) and isinstance(binding.get("members"), list), "iam_policy_invalid")
        require(not {"allUsers", "allAuthenticatedUsers"}.intersection(binding["members"]), "public_job_access_blocked")
        if MEMBER not in binding["members"]:
            bindings.append(binding); continue
        found += 1
        require(binding.get("role") == "roles/run.invoker" and
                ("condition" not in binding or binding["condition"] == CONDITION), "unexpected_scheduler_grant")
        others = [x for x in binding["members"] if x != MEMBER]
        if others:
            bindings.append({**binding, "members": others})
    require(found == 1, "existing_scheduler_invoker_required")
    bindings.append({"role": "roles/run.invoker", "members": [MEMBER], "condition": CONDITION})
    p.update(version=3, bindings=bindings)
    return p


def verify_policy(policy):
    narrowed = conditional_policy(policy)
    require(policy.get("version") == 3 and policy.get("bindings") == narrowed["bindings"], "conditional_invoker_not_verified")
    return True


def verify_run_job(row, *, scheduled=True):
    # gcloud Cloud Run currently returns v1 metadata; intentionally reject a
    # different/unreviewed shape instead of guessing at the execution bounds.
    try:
        outer = row["spec"]["template"]["spec"]
        spec = outer["template"]["spec"]
        container = spec["containers"][0]
    except (KeyError, IndexError, TypeError):
        raise PilotError("run_job_shape_invalid") from None
    require(row.get("metadata", {}).get("name") == JOB and outer.get("taskCount") == 1
            and outer.get("parallelism") == 1 and len(spec["containers"]) == 1
            and spec.get("maxRetries") == 0 and str(spec.get("timeoutSeconds")) == str(TASK_TIMEOUT_SECONDS)
            and spec.get("serviceAccountName") == "hamle-runtime@gamechangers-hamle.iam.gserviceaccount.com"
            and container.get("command") == ["python"]
            and container.get("resources", {}).get("limits") == {"cpu": "1", "memory": "512Mi"}, "run_job_bounds_changed")
    args = container.get("args")
    require(args == ["worker.py", "--mode", "scheduled_review"] if scheduled else
            args in (["worker.py", "--mode", "review"], ["worker.py", "--mode", "scheduled_review"]), "run_job_mode_invalid")
    env = {v["name"]: v.get("value") for v in container.get("env", [])}
    require(env == {"HAMLE_ALLOW_FIRESTORE_OPERATIONS": "yes", "HAMLE_PROJECT_ID": PROJECT,
                    "HAMLE_FIRESTORE_DATABASE": "hamle-operations"}, "run_job_environment_changed")
    require(isinstance(container.get("image"), str) and re.fullmatch(
            r"europe-west1-docker\.pkg\.dev/gamechangers-hamle/hamle-runtime/hamle-account-manager@sha256:[0-9a-f]{64}",
            container["image"]), "immutable_image_required")
    return container["image"]


def plan():
    return {"status": "plan_only_no_io", "project": PROJECT, "region": REGION,
            "run_job": RUN_RESOURCE, "schedules": [job_spec(*p) for p in SCHEDULES],
            "invoker_condition": CONDITION, "slots": 41, "cadence_minutes": 15,
            "task_timeout_seconds": TASK_TIMEOUT_SECONDS,
            "planned_41_executions_vcpu_seconds_upper_bound": 41 * TASK_TIMEOUT_SECONDS,
            "planned_41_executions_gib_seconds_upper_bound": 41 * TASK_TIMEOUT_SECONDS * 0.5,
            "duplicate_delivery_or_manual_runs_not_in_that_bound": True,
            "provider_hard_cap_installed": False,
            "maximum_model_calls_per_unchanged_message": 1,
            "google_reservation_microdollars": HOLD_AMOUNT,
            "existing_holds_released": False, "automatic_business_dispatch": False,
            "cron_has_year_field": False, "year_enforced_by_iam_and_worker": True,
            "initial_creation": "Jan1 sentinel schedule, pause, then update desired schedule while paused"}


class Journal:
    def __init__(self, path=JOURNAL_PATH):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else {"schema": 1, "steps": {}, "phases": {}}
        require(self.data.get("schema") == 1, "journal_schema_mismatch")

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(self.data, stream, ensure_ascii=False, indent=2)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def mutation(self, key, callback):
        # A journaled unknown or completed operation is never executed twice.
        require(key not in self.data["steps"], "mutation_already_attempted_reconcile_required")
        self.data["steps"][key] = {"state": "started_do_not_retry", "started_at": now_utc().isoformat()}
        self.save()
        result = callback()
        self.data["steps"][key].update(state="returned", returned_at=now_utc().isoformat())
        self.save()
        return result

    def phase(self, name, value):
        self.data["phases"][name] = {"recorded_at": now_utc().isoformat(), **value}
        self.save()


class Cloud:
    """Admin OAuth remains in memory, never appears in journal/output/argv."""
    def __init__(self):
        from import_cloud_credentials import credentials
        import requests
        self.credentials = credentials()
        self.session = requests.Session(); self.session.trust_env = False
        self.session.headers.update(Authorization="Bearer " + self.credentials.token)

    def request(self, method, url, body=None, *, allow_missing=False):
        require(url.startswith((RUN_URL, SCHEDULER_URL, "https://logging.googleapis.com/v2/entries:list")), "rest_endpoint_forbidden")
        try:
            response = self.session.request(method, url, json=body, timeout=30, allow_redirects=False)
            if allow_missing and response.status_code == 404:
                return {"absent": True}
            require(200 <= response.status_code < 300, "cloud_metadata_request_rejected")
            require(len(response.content) <= 2_000_000, "cloud_metadata_response_too_large")
            result = response.json()
            require(isinstance(result, dict), "cloud_metadata_response_invalid")
            return result
        except PilotError:
            raise
        except Exception:
            raise PilotError("cloud_metadata_outcome_unknown_no_retry") from None

    def gcloud(self, args):
        from provision_metadata import run
        try:
            return run(args)
        except Exception:
            raise PilotError("gcloud_metadata_failed_no_retry") from None

    def policy(self):
        return self.request("GET", RUN_URL + ":getIamPolicy?options.requestedPolicyVersion=3")

    def run_job(self):
        return self.gcloud(["run", "jobs", "describe", JOB, "--region=" + REGION])

    def verify_project_scope(self):
        policy = self.gcloud(["projects", "get-iam-policy", PROJECT])
        require(not any(MEMBER in b.get("members", []) for b in policy.get("bindings", [])),
                "scheduler_project_grant_requires_review")

    def execution_rows(self):
        value = self.request("GET", RUN_URL + "/executions?pageSize=1000")
        require(not value.get("nextPageToken"), "execution_list_incomplete")
        rows = value.get("executions", [])
        require(isinstance(rows, list) and all(isinstance(v, dict) and isinstance(v.get("name"), str) for v in rows), "execution_list_invalid")
        return rows

    def execution_ids(self):
        return sorted(row["name"] for row in self.execution_rows())

    def require_no_active_execution(self):
        rows = self.execution_rows()
        require(all(isinstance(row.get("completionTime"), str) and row["completionTime"]
                    and row.get("runningCount", 0) == 0 for row in rows),
                "active_or_unconfirmed_execution_requires_review")

    def store(self):
        from cloud_store import OperationsStore, FirestoreBackend
        backend = FirestoreBackend(opt_in="yes", credentials=self.credentials)
        return OperationsStore(backend), backend

    def close(self):
        self.session.close()


def readback(cloud, *, state="PAUSED"):
    cloud.verify_project_scope()
    policy = cloud.policy(); verify_policy(policy)
    image = verify_run_job(cloud.run_job())
    names = []
    for name, schedule in SCHEDULES:
        row = cloud.request("GET", SCHEDULER_URL + "/" + name)
        verify_job(row, name, schedule, state=state); names.append(row["name"])
    return {"status": "verified", "state": state, "job_names": names,
            "iam_policy_version": policy["version"], "iam_etag": policy["etag"], "image": image}


def prepare(cloud, journal):
    require(now_utc() < OPEN, "preparation_must_precede_pilot_window")
    cloud.verify_project_scope()
    cloud.require_no_active_execution()
    verify_run_job(cloud.run_job(), scheduled=False)
    old = cloud.policy(); new = conditional_policy(old)
    if old != new:
        journal.mutation("narrow-invoker", lambda: cloud.request("POST", RUN_URL + ":setIamPolicy", {"policy": new}))
    verify_policy(cloud.policy())
    for name, schedule in SCHEDULES:
        url = SCHEDULER_URL + "/" + name
        row = cloud.request("GET", url, allow_missing=True)
        if row.get("absent"):
            row = journal.mutation("create-" + name, lambda: cloud.request("POST", SCHEDULER_URL, job_spec(name, SENTINEL)))
        if row.get("state") == "ENABLED":
            verify_job(row, name, SENTINEL, state="ENABLED")
            journal.mutation("pause-" + name, lambda: cloud.request("POST", url + ":pause", {}))
            row = cloud.request("GET", url)
        if row.get("schedule") == SENTINEL:
            verify_job(row, name, SENTINEL)
            journal.mutation("schedule-" + name, lambda: cloud.request("PATCH", url + "?updateMask=schedule", {"name": job_spec(name, schedule)["name"], "schedule": schedule}))
        verify_job(cloud.request("GET", url), name, schedule)
    row = cloud.run_job()
    if row["spec"]["template"]["spec"]["template"]["spec"]["containers"][0]["args"] != ["worker.py", "--mode", "scheduled_review"]:
        journal.mutation("job-mode-scheduled-review", lambda: cloud.gcloud([
            "run", "jobs", "update", JOB, "--region=" + REGION,
            "--args=worker.py,--mode,scheduled_review", "--task-timeout=" + str(TASK_TIMEOUT_SECONDS) + "s"]))
    result = readback(cloud); journal.phase("prepare-paused", result)
    return result


def denial_evidence(entries, submitted_at):
    threshold = datetime.fromisoformat(submitted_at).timestamp()
    for entry in entries:
        p = entry.get("protoPayload", {})
        try:
            observed = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        if (observed >= threshold and p.get("serviceName") == "run.googleapis.com"
                and p.get("authenticationInfo", {}).get("principalEmail") == SA
                and p.get("status", {}).get("code") == 7
                and isinstance(p.get("methodName"), str) and p["methodName"].endswith(".RunJob")
                and isinstance(p.get("resourceName"), str) and p["resourceName"].endswith("/jobs/" + JOB)):
            return {"timestamp": entry["timestamp"], "insert_id": entry.get("insertId"),
                    "principal": SA, "status_code": 7,
                    "method": p["methodName"], "resource": p["resourceName"]}
    return None


def scheduler_denial_evidence(entries, submitted_at):
    threshold = datetime.fromisoformat(submitted_at).timestamp()
    name = PARENT + "/jobs/" + SCHEDULES[0][0]
    for entry in entries:
        p = entry.get("jsonPayload", {})
        try:
            observed = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError):
            continue
        status = entry.get("httpRequest", {}).get("status")
        debug = p.get("debugInfo", "")
        explicit_403 = status == 403 or (isinstance(debug, str) and
                        re.search(r"Original HTTP response code number\s*=\s*403(?:\D|$)", debug) is not None)
        if (observed >= threshold and p.get("@type") == "type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished"
                and p.get("jobName") == name and p.get("url") == RUN_URL + ":run"
                and p.get("status") == "PERMISSION_DENIED" and explicit_403):
            return {"timestamp": entry["timestamp"], "insert_id": entry.get("insertId"),
                    "job_name": name, "http_status": 403, "status": "PERMISSION_DENIED"}
    return None


def deny_check(cloud, journal):
    require(outside_window(now_utc()), "denial_probe_forbidden_inside_allowed_window")
    readback(cloud)
    old = journal.data["phases"].get("deny-submission")
    if old is None:
        before = cloud.execution_ids()
        # Persist baseline BEFORE the run attempt. Reinvoking this phase only
        # reads logs; it never resubmits even if the API response was uncertain.
        journal.phase("deny-submission", {"status": "attempt_pending", "submitted_at": now_utc().isoformat(),
                                        "execution_ids_before": before, "job_name": SCHEDULES[0][0]})
        old = journal.data["phases"]["deny-submission"]
        journal.mutation("deny-run-once", lambda: cloud.request("POST", SCHEDULER_URL + "/" + SCHEDULES[0][0] + ":run", {}))
    query = (f'((protoPayload.serviceName="run.googleapis.com" AND '
             f'protoPayload.authenticationInfo.principalEmail="{SA}" AND '
             f'protoPayload.status.code=7) OR (resource.type="cloud_scheduler_job" AND '
             f'resource.labels.job_id="{SCHEDULES[0][0]}")) AND timestamp>="{old["submitted_at"]}"')
    logs = cloud.request("POST", "https://logging.googleapis.com/v2/entries:list",
                        {"resourceNames": ["projects/" + PROJECT], "filter": query,
                         "orderBy": "timestamp desc", "pageSize": 100})
    require(not logs.get("nextPageToken"), "denial_logs_incomplete")
    audit = denial_evidence(logs.get("entries", []), old["submitted_at"])
    scheduler_audit = scheduler_denial_evidence(logs.get("entries", []), old["submitted_at"])
    after = cloud.execution_ids()
    require(after == old["execution_ids_before"], "unexpected_cloud_run_execution_requires_review")
    require(outside_window(now_utc()), "denial_probe_window_changed")
    readback(cloud)  # A probe must not silently leave any plan job enabled.
    if audit is None or scheduler_audit is None:
        return {"status": "awaiting_denial_audit", "resubmit_allowed": False,
                "execution_ids_unchanged": True}
    result = {"status": "verified_denied", "audit": audit, "scheduler_attempt": scheduler_audit,
              "execution_ids_unchanged": True,
              "run_submission_once": True, "business_execution_started": False}
    journal.phase("deny-check", result)
    return result


def pause_after_activation_failure(cloud, journal):
    """Only reduce activity after a possibly partial resume; never replay it."""
    outcomes = []
    for name, schedule in SCHEDULES:
        url = SCHEDULER_URL + "/" + name
        try:
            row = cloud.request("GET", url)
            require(row.get("state") in ("PAUSED", "ENABLED"), "scheduler_state_unconfirmed")
            verify_job(row, name, schedule, state=row["state"])
            if row["state"] == "ENABLED":
                journal.mutation("failure-pause-" + name,
                                 lambda: cloud.request("POST", url + ":pause", {}))
            verify_job(cloud.request("GET", url), name, schedule)
            outcomes.append({"job": name, "state": "PAUSED", "verified": True})
        except Exception as exc:
            outcomes.append({"job": name, "state": "unconfirmed", "verified": False,
                             "reason": exc.code if isinstance(exc, PilotError) else "safety_pause_failed"})
    result = {"all_paused_verified": all(row["verified"] for row in outcomes), "jobs": outcomes}
    journal.phase("activation-failure-safety-pause", result)
    return result


def verify_denial_gate(denied):
    attempt = denied.get("scheduler_attempt", {})
    audited = (denied.get("status") == "verified_denied"
               and isinstance(denied.get("audit"), dict) and denied["audit"].get("principal") == SA)
    target_only = (denied.get("status") == "verified_target_denied"
                   and denied.get("probe_kind") == "enabled_inert_schedule"
                   and denied.get("target_http_denial_observed") is True
                   and denied.get("configured_oauth_service_account") == SA
                   and denied.get("target_uri") == RUN_URL + ":run"
                   and attempt.get("job_name") == PARENT + "/jobs/" + SCHEDULES[0][0]
                   and attempt.get("status") == "PERMISSION_DENIED"
                   and denied.get("all_jobs_paused") is True
                   and denied.get("run_submission_once") is True
                   and denied.get("business_execution_started") is False
                   and denied.get("target_principal_audit_observed") is False
                   and denied.get("audit") is None
                   and denied.get("denial_exact_cause_not_observed") is True
                   and denied.get("allowed_window_execution_tested") is False)
    require((audited or target_only) and denied.get("execution_ids_unchanged") is True
            and attempt.get("http_status") == 403, "verified_denial_required")


def activate(cloud, journal):
    require(now_utc() < OPEN, "activation_must_precede_pilot_window")
    denied = journal.data["phases"].get("deny-check", {})
    verify_denial_gate(denied)
    readback(cloud)
    store, backend = cloud.store()
    try:
        control = store.control(); status = store.budget_status()
        require(control["mode"] == "review_open" and status["overrun_hold"] is False
                and status["total_committed_microdollars"] <= status["limit_microdollars"], "current_budget_or_control_not_ready")
        # Additional conservative hold; never settle/release prior 3+5 USD.
        hold = journal.mutation("reserve-google-pilot", lambda: store.budget_reserve(HOLD_KEY, "google_cloud", HOLD_AMOUNT))
        require(hold["reserved_microdollars"] == HOLD_AMOUNT and hold["state"] == "reserved", "pilot_hold_not_fresh")
        journal.mutation("begin-google-pilot-hold", lambda: store.budget_begin(HOLD_KEY, control["generation"]))
        journal.phase("budget-before-activation", {"status": "reserved", "reservation_key": HOLD_KEY,
                      "reservation_microdollars": HOLD_AMOUNT, "budget": store.budget_status(),
                      "retained_prior_holds": True, "provider_hard_cap_installed": False})
        # Recheck controls/date between resumes. On partial failure, only safety
        # pauses are attempted; the failed resume and model work are not retried.
        for name, schedule in SCHEDULES:
            require(now_utc() < OPEN, "activation_window_changed")
            require(store.control() == control and store.budget_status()["overrun_hold"] is False, "control_changed_during_activation")
            verify_job(cloud.request("GET", SCHEDULER_URL + "/" + name), name, schedule)
            journal.mutation("resume-" + name, lambda: cloud.request("POST", SCHEDULER_URL + "/" + name + ":resume", {}))
        result = readback(cloud, state="ENABLED")
        # This is a reserve, not a provider invoice or claim of hard billing cap.
        store.budget_unknown(HOLD_KEY)
        result.update(status="activated_exact_date_internal_review", slots=41,
                      reservation_key=HOLD_KEY, automatic_business_dispatch=False)
        journal.phase("activate", result)
        return result
    except Exception:
        paused = pause_after_activation_failure(cloud, journal)
        if not paused["all_paused_verified"]:
            raise PilotError("activation_failed_pause_not_verified_operator_required") from None
        raise PilotError("activation_failed_all_jobs_paused_reconcile_required") from None
    finally:
        backend.close()


def self_test():
    from tempfile import TemporaryDirectory
    from unittest.mock import patch

    old = {"etag": "unit-etag", "version": 1,
           "bindings": [{"role": "roles/run.invoker", "members": [MEMBER, "user:ahmet@gamechangers.com.tr"]}]}
    result = conditional_policy(old)
    require(result["etag"] == old["etag"] and result["version"] == 3, "test_failed")
    verify_policy(result)
    require(result["bindings"][0]["members"] == ["user:ahmet@gamechangers.com.tr"] and old["version"] == 1, "test_failed")
    rejected = 0
    for p in ({"bindings": old["bindings"]}, {**old, "bindings": []},
              {**old, "bindings": [{"role": "roles/owner", "members": [MEMBER]}]},
              {**old, "bindings": old["bindings"] + [{"role": "roles/run.invoker", "members": ["allUsers"]}]}):
        try: conditional_policy(p)
        except PilotError: rejected += 1
    require(rejected == 4, "test_failed")
    require(outside_window(datetime.fromisoformat("2026-10-12T05:29:59+00:00")) and not outside_window(OPEN)
            and outside_window(CLOSE) and outside_window(datetime.fromisoformat("2027-10-12T08:00:00+00:00")), "test_failed")
    for name, schedule in SCHEDULES:
        row = job_spec(name, schedule) | {"state": "PAUSED"}
        verify_job(row, name, schedule)
        row["retryConfig"]["retryCount"] = 1
        try: verify_job(row, name, schedule)
        except PilotError: pass
        else: raise PilotError("test_failed")
    bad = {"timestamp": OPEN.isoformat(), "protoPayload": {"serviceName": "run.googleapis.com",
           "authenticationInfo": {"principalEmail": "someone@example.test"}, "status": {"code": 7},
           "methodName": "google.cloud.run.v2.Jobs.RunJob", "resourceName": RUN_RESOURCE}}
    require(denial_evidence([bad], OPEN.isoformat()) is None, "test_failed")
    bad["protoPayload"]["authenticationInfo"]["principalEmail"] = SA
    require(denial_evidence([bad], OPEN.isoformat()) is not None, "test_failed")
    attempt = {"timestamp": OPEN.isoformat(), "jsonPayload": {
        "@type": "type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished",
        "jobName": PARENT + "/jobs/" + SCHEDULES[0][0], "url": RUN_URL + ":run",
        "status": "PERMISSION_DENIED", "debugInfo": "Original HTTP response code number = 401"}}
    require(scheduler_denial_evidence([attempt], OPEN.isoformat()) is None, "test_failed")
    attempt["jsonPayload"]["debugInfo"] = "Original HTTP response code number = 403"
    require(scheduler_denial_evidence([attempt], OPEN.isoformat()) is not None, "test_failed")
    fixture = {"metadata": {"name": JOB}, "spec": {"template": {"spec": {
        "taskCount": 1, "parallelism": 1, "template": {"spec": {
            "maxRetries": 0, "timeoutSeconds": TASK_TIMEOUT_SECONDS,
            "serviceAccountName": "hamle-runtime@gamechangers-hamle.iam.gserviceaccount.com",
            "containers": [{"command": ["python"], "args": ["worker.py", "--mode", "scheduled_review"],
                "image": "europe-west1-docker.pkg.dev/gamechangers-hamle/hamle-runtime/hamle-account-manager@sha256:" + "a" * 64,
                "resources": {"limits": {"cpu": "1", "memory": "512Mi"}},
                "env": [{"name": "HAMLE_ALLOW_FIRESTORE_OPERATIONS", "value": "yes"},
                        {"name": "HAMLE_PROJECT_ID", "value": PROJECT},
                        {"name": "HAMLE_FIRESTORE_DATABASE", "value": "hamle-operations"}]}]}}}}}}
    verify_run_job(fixture)
    legacy = copy.deepcopy(fixture)
    legacy["spec"]["template"]["spec"]["template"]["spec"]["timeoutSeconds"] = 300
    try: verify_run_job(legacy)
    except PilotError: pass
    else: raise PilotError("test_failed")
    require(plan()["planned_41_executions_vcpu_seconds_upper_bound"] == 24600
            and plan()["planned_41_executions_gib_seconds_upper_bound"] == 12300, "test_failed")

    class FakeCloud:
        def __init__(self):
            self.rows = {name: job_spec(name, schedule) | {"state": "PAUSED"} for name, schedule in SCHEDULES}
            self.p = conditional_policy(old)
            self.calls = []; self.logs = []; self.closed = False
            self.reject_iam = False; self.fail_resume = None; self.fail_pause = False
            self.executions = [{"name": RUN_RESOURCE + "/executions/unit-old", "completionTime": "2026-10-10T00:00:00Z"}]

        def verify_project_scope(self): pass
        def run_job(self): return copy.deepcopy(fixture)
        def policy(self): return copy.deepcopy(self.p)
        def execution_rows(self): return copy.deepcopy(self.executions)
        def execution_ids(self): return sorted(row["name"] for row in self.executions)
        def require_no_active_execution(self): return Cloud.require_no_active_execution(self)
        def request(self, method, url, body=None, **kwargs):
            self.calls.append((method, url))
            if url == RUN_URL + ":setIamPolicy":
                if self.reject_iam: raise PilotError("unit_iam_rejected")
                self.p = copy.deepcopy(body["policy"]); return self.policy()
            if url == "https://logging.googleapis.com/v2/entries:list":
                return {"entries": copy.deepcopy(self.logs)}
            for name, row in self.rows.items():
                base = SCHEDULER_URL + "/" + name
                if method == "GET" and url == base: return copy.deepcopy(row)
                if method == "POST" and url == base + ":run": return copy.deepcopy(row)
                if method == "POST" and url == base + ":resume":
                    row["state"] = "ENABLED"
                    if name == self.fail_resume: raise PilotError("unit_resume_outcome_unknown")
                    return copy.deepcopy(row)
                if method == "POST" and url == base + ":pause":
                    if self.fail_pause: raise PilotError("unit_pause_outcome_unknown")
                    row["state"] = "PAUSED"; return copy.deepcopy(row)
            raise PilotError("unit_unexpected_request")
        def store(self): return self, self
        def control(self): return {"mode": "review_open", "generation": 1}
        def budget_status(self): return {"overrun_hold": False, "total_committed_microdollars": 18_000_000, "limit_microdollars": 50_000_000}
        def budget_reserve(self, key, provider, amount):
            require((key, provider, amount) == (HOLD_KEY, "google_cloud", HOLD_AMOUNT), "test_failed")
            return {"reserved_microdollars": amount, "state": "reserved"}
        def budget_begin(self, *args): pass
        def budget_unknown(self, *args): pass
        def close(self): self.closed = True

    fixed = datetime.fromisoformat("2026-10-10T08:00:00+00:00")
    with TemporaryDirectory() as directory, patch(__name__ + ".now_utc", return_value=fixed):
        def journal(name): return Journal(Path(directory) / (name + ".json"))
        denied_proof = {"status": "verified_denied", "execution_ids_unchanged": True,
                        "audit": {"principal": SA}, "scheduler_attempt": {"http_status": 403}}
        cloud = FakeCloud(); cloud.p = copy.deepcopy(old); cloud.reject_iam = True
        try: prepare(cloud, journal("iam-reject"))
        except PilotError as exc: require(exc.code == "unit_iam_rejected", "test_failed")
        else: raise PilotError("test_failed")
        require(not any(url.startswith(SCHEDULER_URL) for _, url in cloud.calls), "test_failed")

        cloud = FakeCloud(); cloud.executions[0].pop("completionTime")
        try: prepare(cloud, journal("active-execution"))
        except PilotError as exc: require(exc.code == "active_or_unconfirmed_execution_requires_review", "test_failed")
        else: raise PilotError("test_failed")
        require(cloud.calls == [], "test_failed")

        cloud = FakeCloud(); j = journal("missing-audit")
        # Scheduler reports 403, but Run Data Access audit may be disabled.
        # A missing authenticated-principal record is never treated as proof.
        cloud.logs = [copy.deepcopy(attempt)]
        cloud.logs[0]["timestamp"] = fixed.isoformat()
        require(deny_check(cloud, j)["status"] == "awaiting_denial_audit", "test_failed")
        require(deny_check(cloud, j)["status"] == "awaiting_denial_audit", "test_failed")
        require(sum(url.endswith(":run") for _, url in cloud.calls) == 1, "test_failed")
        try: activate(cloud, j)
        except PilotError as exc: require(exc.code == "verified_denial_required", "test_failed")
        else: raise PilotError("test_failed")
        require(all(row["state"] == "PAUSED" for row in cloud.rows.values()), "test_failed")

        cloud = FakeCloud(); j = journal("activation-success"); j.phase("deny-check", denied_proof)
        require(activate(cloud, j)["status"] == "activated_exact_date_internal_review", "test_failed")
        require(cloud.closed and all(row["state"] == "ENABLED" for row in cloud.rows.values())
                and sum(url.endswith(":resume") for _, url in cloud.calls) == 3, "test_failed")

        for pause_fails in (False, True):
            cloud = FakeCloud(); cloud.fail_resume = SCHEDULES[1][0]; cloud.fail_pause = pause_fails
            j = journal("partial-resume-" + str(pause_fails)); j.phase("deny-check", denied_proof)
            try: activate(cloud, j)
            except PilotError as exc:
                require(exc.code == ("activation_failed_pause_not_verified_operator_required" if pause_fails
                                     else "activation_failed_all_jobs_paused_reconcile_required"), "test_failed")
            else: raise PilotError("test_failed")
            require(cloud.closed and sum(url.endswith(":resume") for _, url in cloud.calls) == 2, "test_failed")
            require(j.data["phases"]["activation-failure-safety-pause"]["all_paused_verified"] is not pause_fails, "test_failed")
            if not pause_fails:
                require(all(row["state"] == "PAUSED" for row in cloud.rows.values()), "test_failed")
    return {"status": "offline_checks_passed", "network_calls": 0,
            "covered": ["etag_and_members_preserved", "existing_grant_only", "public_or_broader_grant_held",
                        "exact_year_window", "three_targets_no_retry", "actual_denied_principal_required",
                        "scheduler_actual_403_required", "600_second_bounds_old_300_rejected",
                        "iam_rejection_no_scheduler_mutation", "active_execution_blocks_prepare",
                        "missing_audit_no_activation_no_probe_retry", "partial_resume_safety_pause",
                        "unconfirmed_pause_requires_operator", "exact_activation_success"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("plan", "self-test", "prepare-paused", "readback", "deny-check", "activate"), default="plan")
    parser.add_argument("--execute", action="store_true", help="Explicit operator gate for cloud phases")
    args = parser.parse_args(argv)
    if args.phase == "plan":
        print(json.dumps(plan(), ensure_ascii=False, indent=2)); return 0
    if args.phase == "self-test":
        print(json.dumps(self_test(), ensure_ascii=False)); return 0
    require(args.execute, "explicit_execute_required")
    require(now_utc() < CLOSE, "pilot_authorization_expired")
    os.umask(0o077)
    cloud = None
    lock = None
    try:
        import fcntl
        OUT.mkdir(parents=True, exist_ok=True)
        lock = (OUT / "scheduler-pilot.lock").open("a")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PilotError("scheduler_operator_already_running") from None
        journal = Journal(); cloud = Cloud()
        if args.phase == "prepare-paused": result = prepare(cloud, journal)
        elif args.phase == "readback":
            result = readback(cloud); journal.phase("readback", result)
        elif args.phase == "deny-check": result = deny_check(cloud, journal)
        else: result = activate(cloud, journal)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] != "awaiting_denial_audit" else 3
    finally:
        if cloud is not None: cloud.close()
        if lock is not None: lock.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "held", "reason": exc.code if isinstance(exc, PilotError)
                          else "scheduler_pilot_failed_no_retry", "automatic_retry": False,
                          "reconcile_journal_before_any_mutation_replay": True}))
        raise SystemExit(2)
