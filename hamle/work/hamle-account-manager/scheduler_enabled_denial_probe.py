"""One explicit enabled Scheduler denial probe; default plan and tests are offline.

The earlier PAUSED RunJob failure is preserved. Only the exact early job is
temporarily given the inert Jan-1 schedule and enabled. The new RunJob has one
permanent journal key. Finally we pause, verify, restore the real schedule only
while paused, and require all three jobs paused. A second invocation only reads.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import scheduler_pilot as pilot

PHASE = "enabled-deny-submission-v1"
PREFIX = "enabled-deny-v1-"
EARLY, SCHEDULE = pilot.SCHEDULES[0]
URL = pilot.SCHEDULER_URL + "/" + EARLY
PRIOR_PATH = pilot.OUT / "scheduler-paused-probe-rejection.json"


def _clock(clock):
    now = clock()
    pilot.require(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
    pilot.require(now < pilot.OPEN and pilot.outside_window(now), "enabled_probe_forbidden_in_or_after_window")
    return now


def prior_rejection(proof, journal):
    """Bind the new attempt to the documented pre-target rejection, not a retry."""
    old = journal.data["phases"].get("deny-submission")
    step = journal.data["steps"].get("deny-run-once")
    pilot.require(isinstance(proof, dict) and isinstance(old, dict) and isinstance(step, dict)
                  and proof.get("status") == "scheduler_paused_run_rejected_before_target"
                  and proof.get("target_cloud_run_403_proven") is False
                  and proof.get("original_submission") == old.get("submitted_at")
                  and proof.get("original_step_preserved") == step
                  and proof.get("execution_ids_unchanged") is True
                  and proof.get("execution_ids_before") == proof.get("execution_ids_after"), "prior_precondition_evidence_required")
    matches = [a for a in proof.get("audits", []) if isinstance(a, dict)
               and a.get("service") == "cloudscheduler.googleapis.com"
               and a.get("method") == "google.cloud.scheduler.v1.CloudScheduler.RunJob"
               and a.get("resource") == pilot.PARENT + "/jobs/" + EARLY
               and a.get("rpc_status", {}).get("code") == 9
               and "Job.state must be ENABLED for RunJob." in a.get("rpc_status", {}).get("message", "")]
    pilot.require(bool(matches), "prior_precondition_evidence_required")
    return {"status": "preserved_pre_target_failed_precondition", "rpc_status_code": 9,
            "source_file": PRIOR_PATH.name,
            "evidence_sha256": hashlib.sha256(json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "old_submission": old["submitted_at"], "old_step_unchanged": True,
            "target_403_proven_by_old_attempt": False}


def _pause_and_restore(cloud, journal):
    """First-invocation cleanup only. No resume/run or access-policy mutation."""
    problems = []
    try:
        row = cloud.request("GET", URL)
        if row.get("state") != "PAUSED":
            pilot.require(row.get("state") == "ENABLED", "probe_state_unconfirmed")
            try:
                journal.mutation(PREFIX + "pause", lambda: cloud.request("POST", URL + ":pause", {}))
            except Exception:
                # A pause timeout is not a reason to replay it; GET may prove it
                # was accepted. Never restore a schedule without proving PAUSED.
                problems.append("pause_response_unconfirmed")
        row = cloud.request("GET", URL)
        pilot.require(row.get("state") == "PAUSED", "probe_pause_not_verified")
        if row.get("schedule") == pilot.SENTINEL:
            pilot.verify_job(row, EARLY, pilot.SENTINEL)
            try:
                journal.mutation(PREFIX + "restore-schedule", lambda: cloud.request("PATCH", URL + "?updateMask=schedule",
                    {"name": pilot.PARENT + "/jobs/" + EARLY, "schedule": SCHEDULE}))
            except Exception:
                problems.append("restore_response_unconfirmed")
        pilot.verify_job(cloud.request("GET", URL), EARLY, SCHEDULE)
        result = pilot.readback(cloud)
        result = {"all_paused_actual_schedules_verified": True, "readback": result,
                  "uncertain_responses_reconciled_by_read": problems}
    except Exception as exc:
        result = {"all_paused_actual_schedules_verified": False,
                  "reason": exc.code if isinstance(exc, pilot.PilotError) else "probe_cleanup_unconfirmed",
                  "operator_reconciliation_required": True,
                  "automatic_resume_or_run_allowed": False}
    journal.phase(PREFIX + "cleanup", result)
    return result


def _read_result(cloud, journal, clock):
    _clock(clock)
    snapshot = journal.data["phases"][PHASE]
    # Later invocations never call cleanup mutations. An interrupted cleanup
    # remains visible and requires a separate operator reconciliation.
    pilot.readback(cloud)
    after = cloud.execution_ids()
    pilot.require(after == snapshot["execution_ids_before"], "unexpected_cloud_run_execution_requires_review")
    run = journal.data["steps"].get(PREFIX + "run-once")
    if run is None:
        return {"status": "held", "reason": "probe_not_submitted_reconcile_required", "resubmit_allowed": False,
                "all_jobs_paused": True, "execution_ids_unchanged": True}
    started = run["started_at"]
    query = (f'((protoPayload.serviceName="run.googleapis.com" AND '
             f'protoPayload.authenticationInfo.principalEmail="{pilot.SA}" AND '
             f'protoPayload.status.code=7) OR (resource.type="cloud_scheduler_job" AND '
             f'resource.labels.job_id="{EARLY}")) AND timestamp>="{started}"')
    logs = cloud.request("POST", "https://logging.googleapis.com/v2/entries:list",
        {"resourceNames": ["projects/" + pilot.PROJECT], "filter": query, "orderBy": "timestamp desc", "pageSize": 100})
    pilot.require(not logs.get("nextPageToken"), "denial_logs_incomplete")
    audit = pilot.denial_evidence(logs.get("entries", []), started)
    attempt = pilot.scheduler_denial_evidence(logs.get("entries", []), started)
    # Both log reads and execution inventory are fresh. A new accepted run
    # during evidence collection must not pass the probe.
    pilot.require(cloud.execution_ids() == snapshot["execution_ids_before"], "unexpected_cloud_run_execution_requires_review")
    _clock(clock)
    pilot.readback(cloud)
    if attempt is None:
        return {"status": "awaiting_denial_audit", "resubmit_allowed": False,
                "all_jobs_paused": True, "execution_ids_unchanged": True}
    result = {"status": "verified_denied" if audit is not None else "verified_target_denied",
              "audit": audit, "scheduler_attempt": attempt,
              "execution_ids_unchanged": True, "all_jobs_paused": True,
              "run_submission_once": True, "business_execution_started": False,
              "probe_kind": "enabled_inert_schedule", "old_paused_precondition_preserved": True,
              "target_http_denial_observed": True, "target_uri": pilot.RUN_URL + ":run",
              "configured_oauth_service_account": pilot.SA,
              "target_principal_audit_observed": audit is not None,
              "denial_exact_cause_not_observed": True,
              "allowed_window_execution_tested": False}
    if PREFIX + "verified" not in journal.data["phases"]:
        journal.phase(PREFIX + "verified", result)
    # This is the existing activation contract. Never overwrite a prior proof.
    existing = journal.data["phases"].get("deny-check")
    if existing is None:
        journal.phase("deny-check", result)
    else:
        pilot.require(existing.get("status") in ("verified_denied", "verified_target_denied")
                      and existing.get("probe_kind") == "enabled_inert_schedule", "prior_denial_phase_requires_review")
    return result


def probe(cloud, journal, proof, *, clock=None):
    clock = clock or pilot.now_utc
    now = _clock(clock)
    legacy = prior_rejection(proof, journal)
    if PHASE in journal.data["phases"]:
        return _read_result(cloud, journal, clock)
    pilot.require(now + timedelta(minutes=10) < pilot.OPEN, "enabled_probe_window_too_close")
    readback = pilot.readback(cloud)
    cloud.require_no_active_execution()
    # Fresh at actual probe start: root may have completed another approved job
    # since the rejected PAUSED attempt. Its old execution list is not reused.
    before = cloud.execution_ids()
    journal.phase(PHASE, {"status": "new_enabled_probe_started", "started_at": now.isoformat(),
                          "execution_ids_before": before, "initial_readback": readback,
                          "legacy_precondition": legacy, "only_job": EARLY,
                          "inert_schedule": pilot.SENTINEL, "restored_schedule": SCHEDULE})
    failure = None
    try:
        _clock(clock)
        pilot.verify_job(cloud.request("GET", URL), EARLY, SCHEDULE)
        journal.mutation(PREFIX + "inert-schedule", lambda: cloud.request("PATCH", URL + "?updateMask=schedule",
            {"name": pilot.PARENT + "/jobs/" + EARLY, "schedule": pilot.SENTINEL}))
        pilot.verify_job(cloud.request("GET", URL), EARLY, pilot.SENTINEL)
        _clock(clock)
        journal.mutation(PREFIX + "resume", lambda: cloud.request("POST", URL + ":resume", {}))
        pilot.verify_job(cloud.request("GET", URL), EARLY, pilot.SENTINEL, state="ENABLED")
        _clock(clock)
        # Reread exact IAM immediately before forcing the target request.
        cloud.verify_project_scope(); pilot.verify_policy(cloud.policy())
        pilot.require(cloud.execution_ids() == before, "probe_baseline_changed_before_submission")
        journal.mutation(PREFIX + "run-once", lambda: cloud.request("POST", URL + ":run", {}))
    except Exception as exc:
        failure = exc.code if isinstance(exc, pilot.PilotError) else "enabled_probe_outcome_unknown"
        journal.phase(PREFIX + "attempt-error", {"status": "held", "reason": failure,
                      "automatic_resume_or_run_allowed": False})
    finally:
        cleanup = _pause_and_restore(cloud, journal)
    pilot.require(cleanup["all_paused_actual_schedules_verified"], "enabled_probe_cleanup_requires_operator")
    result = _read_result(cloud, journal, clock)
    if failure is not None:
        result["attempt_response_unconfirmed"] = failure
    return result


def self_test():
    from tempfile import TemporaryDirectory
    from unittest.mock import patch
    fixed = datetime.fromisoformat("2026-10-10T09:00:00+00:00")
    class FakeCloud:
        def __init__(self):
            self.rows = {n: pilot.job_spec(n, s) | {"state": "PAUSED"} for n, s in pilot.SCHEDULES}
            self.policy_value = {"version": 3, "etag": "new-etag", "bindings": [
                {"role": "roles/run.invoker", "members": [pilot.MEMBER], "condition": pilot.CONDITION}]}
            self.calls = []; self.logs = []; self.executions = ["old", "new-manual-before-probe"]
            self.fail = None; self.audit = True; self.create_execution = False
            self.principal_audit = True
        def verify_project_scope(self): pass
        def policy(self): return copy.deepcopy(self.policy_value)
        def require_no_active_execution(self): pass
        def execution_ids(self): return list(self.executions)
        def request(self, method, url, body=None):
            self.calls.append((method, url, copy.deepcopy(body)))
            if url == "https://logging.googleapis.com/v2/entries:list": return {"entries": copy.deepcopy(self.logs)}
            if method == "GET":
                return copy.deepcopy(self.rows[url.rsplit("/", 1)[-1]])
            pilot.require(url.startswith(URL), "test_other_job_mutation")
            row = self.rows[EARLY]
            if method == "PATCH":
                pilot.require(url.endswith("?updateMask=schedule") and set(body) == {"name", "schedule"}
                              and row["state"] == "PAUSED", "test_unsafe_patch")
                row["schedule"] = body["schedule"]
                if self.fail == "patch": raise pilot.PilotError("unit_patch_unknown")
            elif url.endswith(":resume"):
                pilot.require(row["schedule"] == pilot.SENTINEL, "test_live_schedule_enabled")
                row["state"] = "ENABLED"
                if self.fail == "resume": raise pilot.PilotError("unit_resume_unknown")
            elif url.endswith(":pause"):
                if self.fail == "pause_before": raise pilot.PilotError("unit_pause_unknown")
                row["state"] = "PAUSED"
                if self.fail == "pause_after": raise pilot.PilotError("unit_pause_unknown")
            elif url.endswith(":run"):
                pilot.require(row["state"] == "ENABLED" and row["schedule"] == pilot.SENTINEL, "test_unsafe_run")
                self.logs = [{"timestamp": fixed.isoformat(), "protoPayload": {
                    "serviceName": "run.googleapis.com", "authenticationInfo": {"principalEmail": pilot.SA},
                    "methodName": "google.cloud.run.v2.Jobs.RunJob", "resourceName": pilot.RUN_RESOURCE,
                    "status": {"code": 7}}}, {"timestamp": fixed.isoformat(), "jsonPayload": {
                    "@type": "type.googleapis.com/google.cloud.scheduler.logging.AttemptFinished",
                    "jobName": pilot.PARENT + "/jobs/" + EARLY, "url": pilot.RUN_URL + ":run",
                    "status": "PERMISSION_DENIED", "debugInfo": "Original HTTP response code number = 403"}}] if self.audit else []
                if not self.principal_audit:
                    self.logs = [entry for entry in self.logs if "jsonPayload" in entry]
                if self.create_execution: self.executions.append("unexpected")
                if self.fail == "run": raise pilot.PilotError("unit_run_unknown")
            else: raise pilot.PilotError("test_unexpected_mutation")
            return copy.deepcopy(row)
    def readback(cloud):
        pilot.verify_policy(cloud.policy())
        for name, schedule in pilot.SCHEDULES:
            pilot.verify_job(cloud.request("GET", pilot.SCHEDULER_URL + "/" + name), name, schedule)
        return {"status": "verified", "state": "PAUSED", "iam_etag": "new-etag", "image": "unit-image"}
    with TemporaryDirectory() as directory, patch.object(pilot, "now_utc", return_value=fixed), patch.object(pilot, "readback", side_effect=readback):
        def fixture(label):
            journal = pilot.Journal(Path(directory) / (label + ".json"))
            journal.data["steps"]["deny-run-once"] = {"state": "started_do_not_retry", "started_at": "2026-10-10T08:00:00+00:00"}
            journal.data["phases"]["deny-submission"] = {"submitted_at": "2026-10-10T08:00:00+00:00", "execution_ids_before": ["old"]}
            proof = {"status": "scheduler_paused_run_rejected_before_target", "target_cloud_run_403_proven": False,
                     "original_submission": "2026-10-10T08:00:00+00:00", "original_step_preserved": copy.deepcopy(journal.data["steps"]["deny-run-once"]),
                     "execution_ids_unchanged": True, "execution_ids_before": ["old"], "execution_ids_after": ["old"],
                     "audits": [{"service": "cloudscheduler.googleapis.com", "method": "google.cloud.scheduler.v1.CloudScheduler.RunJob",
                         "resource": pilot.PARENT + "/jobs/" + EARLY, "rpc_status": {"code": 9, "message": "Job.state must be ENABLED for RunJob."}}]}
            return FakeCloud(), journal, proof
        covered = []
        for failure in (None, "run", "pause_after", "resume", "patch"):
            cloud, journal, proof = fixture(str(failure)); cloud.fail = failure
            old = copy.deepcopy(journal.data)
            result = probe(cloud, journal, proof)
            submitted = failure not in ("resume", "patch")
            pilot.require(result["status"] == ("verified_denied" if submitted else "held"), "test_failed")
            pilot.require(all(r["state"] == "PAUSED" for r in cloud.rows.values()) and cloud.rows[EARLY]["schedule"] == SCHEDULE, "test_failed")
            pilot.require(journal.data["steps"]["deny-run-once"] == old["steps"]["deny-run-once"] and
                          journal.data["phases"]["deny-submission"] == old["phases"]["deny-submission"], "test_failed")
            before_calls = len(cloud.calls)
            again = probe(cloud, journal, proof)
            pilot.require(all(method == "GET" or url.endswith("entries:list") for method, url, _ in cloud.calls[before_calls:]), "test_repeated_mutation")
            pilot.require(sum(url.endswith(":run") for _, url, _ in cloud.calls) == int(submitted), "test_run_repeated")
            pilot.require(journal.data["phases"][PHASE]["execution_ids_before"] == ["old", "new-manual-before-probe"], "test_stale_baseline")
            covered.append("success" if failure is None else failure + "_reconciled_without_resubmit")
        cloud, journal, proof = fixture("no-audit"); cloud.audit = False
        pilot.require(probe(cloud, journal, proof)["status"] == "awaiting_denial_audit", "test_failed")
        pilot.require("deny-check" not in journal.data["phases"], "test_false_proof")
        pilot.require(probe(cloud, journal, proof)["status"] == "awaiting_denial_audit", "test_failed")
        pilot.require(sum(url.endswith(":run") for _, url, _ in cloud.calls) == 1, "test_run_repeated")
        covered.append("missing_target_403_held_read_only_poll")
        cloud, journal, proof = fixture("target-only"); cloud.principal_audit = False
        result = probe(cloud, journal, proof)
        pilot.require(result["status"] == "verified_target_denied" and result["audit"] is None
                      and result["target_principal_audit_observed"] is False
                      and result["denial_exact_cause_not_observed"] is True, "test_fabricated_audit")
        pilot.verify_denial_gate(result)
        for key, value in (("configured_oauth_service_account", "other@example.test"),
                           ("target_uri", "https://wrong.example.test/"),
                           ("http_status", None), ("job_name", "wrong-job")):
            bad = copy.deepcopy(result)
            (bad["scheduler_attempt"] if key in ("http_status", "job_name") else bad)[key] = value
            try: pilot.verify_denial_gate(bad)
            except pilot.PilotError as exc: pilot.require(exc.code == "verified_denial_required", "test_failed")
            else: raise pilot.PilotError("test_false_denial_gate")
        before_calls = len(cloud.calls)
        pilot.require(probe(cloud, journal, proof)["status"] == "verified_target_denied", "test_failed")
        pilot.require(all(method == "GET" or url.endswith("entries:list") for method, url, _ in cloud.calls[before_calls:]), "test_repeated_mutation")
        cloud.rows[EARLY]["httpTarget"]["oauthToken"]["serviceAccountEmail"] = "other@example.test"
        try: probe(cloud, journal, proof)
        except pilot.PilotError: pass
        else: raise pilot.PilotError("test_fresh_oauth_identity_not_checked")
        covered.append("target_403_only_honest_proof_exact_identity_target_job_gate")
        for label in ("pause_before", "execution"):
            cloud, journal, proof = fixture(label)
            cloud.fail = label; cloud.create_execution = label == "execution"
            try: probe(cloud, journal, proof)
            except pilot.PilotError as exc:
                pilot.require(exc.code == ("enabled_probe_cleanup_requires_operator" if label == "pause_before" else
                              "unexpected_cloud_run_execution_requires_review"), "test_failed")
            else: raise pilot.PilotError("test_failed")
            count = len(cloud.calls)
            try: probe(cloud, journal, proof)
            except pilot.PilotError: pass
            pilot.require(all(method == "GET" or url.endswith("entries:list") for method, url, _ in cloud.calls[count:]), "test_repeated_mutation")
            if label == "pause_before":
                pilot.require(cloud.rows[EARLY]["schedule"] == pilot.SENTINEL, "test_live_schedule_left_enabled")
            covered.append(label + "_operator_required_no_retry")
        cloud, journal, proof = fixture("bad-policy")
        cloud.policy_value["bindings"][0].pop("condition")
        try: probe(cloud, journal, proof)
        except pilot.PilotError: pass
        else: raise pilot.PilotError("test_failed")
        pilot.require(not any(method != "GET" for method, _, _ in cloud.calls), "test_mutation_before_guard")
        covered.append("unconditional_access_blocks_probe")
    return {"status": "offline_checks_passed", "network_calls": 0, "covered": covered}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("plan", "self-test", "probe"), default="plan")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.phase == "plan":
        print(json.dumps({"status": "plan_only_no_io", "job": EARLY, "only_new_run_attempts": 1,
                          "flow": ["all paused + exact IAM + outside window", "fresh execution baseline",
                                   "paused schedule becomes inert Jan1", "resume inert job", "run once",
                                   "finally pause + verify", "restore actual cron while paused", "logs + no execution proof"],
                          "later_invocations": "read-only reconciliation; never resume/run again",
                          "old_rejection_preserved": True, "budget_or_access_changes": False}, ensure_ascii=False))
        return 0
    if args.phase == "self-test":
        print(json.dumps(self_test(), ensure_ascii=False)); return 0
    pilot.require(args.execute, "explicit_execute_required")
    os.umask(0o077)
    cloud = lock = None
    try:
        import fcntl
        lock = (pilot.OUT / "scheduler-pilot.lock").open("a")
        try: fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise pilot.PilotError("scheduler_operator_already_running") from None
        journal = pilot.Journal()
        proof = json.loads(PRIOR_PATH.read_text())
        cloud = pilot.Cloud()
        result = probe(cloud, journal, proof)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] in ("verified_denied", "verified_target_denied") else 3
    finally:
        if cloud is not None: cloud.close()
        if lock is not None: lock.close()


if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "held", "reason": exc.code if isinstance(exc, pilot.PilotError)
                          else "enabled_probe_failed_no_retry", "automatic_retry": False,
                          "operator_reconciliation_required": True}))
        raise SystemExit(2)
