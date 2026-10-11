"""Five synthetic Sol calls within one approved $1 hold; never dispatch/retry.

No import-time network/credential access. The shared cloud reservation is the
cross-process replay fence; local exclusive markers preserve each call outcome.
Production worker/model selection remains unchanged.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys

from model import build_request, call_once, ModelError, MAX_REQUEST_BYTES
from model_profiles import SOL_MEDIUM_EVALUATION

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs/new-mac-2026-10-10"
KEY = "sol-medium-five-synthetic-20261010-v1"
FIXTURE = Path(__file__).with_name("model-evaluation-cases.json")


def stamp():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())


def inputs():
    fixture = json.loads(FIXTURE.read_text())
    assert fixture["max_calls"] == 5 and len(fixture["cases"]) == 5
    assert fixture["profile_id"] == SOL_MEDIUM_EVALUATION.profile_id
    assert fixture["customer_data_present"] is False
    assert len({c["id"] for c in fixture["cases"]}) == 5
    for case in fixture["cases"]:
        assert case["id"].replace("_", "").isalnum()
        build_request(case["input"], profile=SOL_MEDIUM_EVALUATION)
    # Use cache-write price for every input token, never assume cache savings.
    worst_microdollars = 5 * ((MAX_REQUEST_BYTES + 1024) * 2.5
                              + SOL_MEDIUM_EVALUATION.max_output_tokens * 10)
    assert worst_microdollars <= 1_000_000
    return fixture, int(worst_microdollars)


def load_key():
    path = ROOT / ".env.local"
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ModelError("private_key_file_required")
    values = [line.split("=", 1)[1].strip().strip("\"'")
              for line in path.read_text().splitlines()
              if line.startswith("OPENAI_API_KEY=")]
    if len(values) != 1:
        raise ModelError("credential_missing_or_invalid")
    return values[0]


def main():
    fixture, worst = inputs()
    if sys.argv[1:] == ["--validate-only"]:
        print(json.dumps({"status": "offline_validated", "cases": 5,
                          "conservative_max_usd": worst / 1e6, "api_calls": 0}))
        return 0
    if sys.argv[1:] != ["--run-approved-sol-evaluation"]:
        raise ModelError("explicit_evaluation_flag_required")
    decision = json.loads((OUT / "user-decisions-hybrid-preparation.json").read_text())
    assert decision["model_choice"]["selected"] is True
    assert decision["model_choice"]["routine"] == "gpt-6.1-sol"
    assert decision["model_choice"]["complex"] == "gpt-6-astra"
    assert decision["evaluation_authorization"]["max_usd"] == 1
    assert decision["evaluation_authorization"]["within_existing_shared_50_usd_limit"] is True
    directory = OUT / KEY
    if directory.exists():
        raise ModelError("evaluation_already_attempted_reconcile_only")
    # Existing approved Hamle key only; never create or display a credential.
    api_key = load_key()
    from import_cloud_credentials import credentials
    from cloud_store import FirestoreBackend, OperationsStore, START, DEADLINE
    backend = FirestoreBackend(opt_in="yes", credentials=credentials())
    started = False
    try:
        store = OperationsStore(backend)
        generation = store.control()["generation"]
        directory.mkdir(mode=0o700)
        save(directory / "run-start.json", {
            "created_at": stamp(), "reservation_key": KEY,
            "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
            "profile_id": SOL_MEDIUM_EVALUATION.profile_id,
            "max_calls": 5, "approved_max_usd": 1,
            "conservative_max_usd": worst / 1e6, "automatic_retry": False})
        store.budget_reserve(KEY, "openai", 1_000_000)
        store.budget_begin(KEY, generation)
        started = True
        results = []
        for index, case in enumerate(fixture["cases"], 1):
            current = store.control()
            budget = store.budget_status()
            if not START <= datetime.now(timezone.utc).timestamp() <= DEADLINE:
                raise ModelError("authorization_expired")
            if current["generation"] != generation or current["mode"] != "review_open":
                raise ModelError("control_changed_evaluation_stopped")
            if budget["overrun_hold"] or budget["total_committed_microdollars"] > 50_000_000:
                raise ModelError("evaluation_budget_unavailable")
            prefix = directory / (str(index) + "-" + case["id"])
            save(Path(str(prefix) + ".attempt.json"), {
                "started_at": stamp(), "case_id": case["id"],
                "profile_id": SOL_MEDIUM_EVALUATION.profile_id,
                "outcome": "started_do_not_replay"})
            try:
                current = store.control()
                if current["generation"] != generation or current["mode"] != "review_open":
                    raise ModelError("control_changed_evaluation_stopped")
                if not START <= datetime.now(timezone.utc).timestamp() <= DEADLINE:
                    raise ModelError("authorization_expired")
                result = call_once(api_key, case["input"], profile=SOL_MEDIUM_EVALUATION)
                check = (result["analysis"]["intent"] == case["expected_intent"]
                         and result["analysis"]["escalation"]["owner"] == case["expected_owner"])
                usage = result["usage"]
                cached = usage.get("input_tokens_details", {}).get("cached_tokens", 0)
                estimated = ((usage["input_tokens"] - cached) * 2 + cached * .1
                             + usage["output_tokens"] * 10) / 1e6
                row = {"case_id": case["id"], "completed_at": stamp(),
                       "result": result, "routing_check_passed": check,
                       "token_cost_estimate_usd": estimated, "is_invoice": False,
                       "human_semantic_review_required": True, "business_writes": 0}
                save(Path(str(prefix) + ".result.json"), row)
                results.append(row)
                print(json.dumps({"case": case["id"], "routing_check_passed": check,
                                  "token_cost_estimate_usd": estimated}), flush=True)
                if not check:
                    raise ModelError("evaluation_routing_check_failed")
            except ModelError as exc:
                save(Path(str(prefix) + ".failure.json"), {
                    "recorded_at": stamp(), "code": exc.code,
                    "failure_receipt": exc.failure_receipt, "usage": exc.usage,
                    "automatic_retry": False, "business_writes": 0})
                raise
        save(directory / "summary.json", {
            "status": "five_synthetic_responses_completed_pending_semantic_review",
            "completed_at": stamp(), "model": SOL_MEDIUM_EVALUATION.model,
            "reasoning_effort": SOL_MEDIUM_EVALUATION.reasoning_effort,
            "call_count": len(results), "routing_checks_passed": True,
            "estimated_total_token_cost_usd": sum(r["token_cost_estimate_usd"] for r in results),
            "is_invoice": False, "reservation_retained_usd": 1,
            "new_astra_calls": 0, "customer_data_sent": False, "business_writes": 0,
            "production_model_changed": False, "customer_autonomy_accepted": False})
        return 0
    finally:
        try:
            if started:
                store.budget_unknown(KEY)  # Retain full hold until billing reconciliation.
        finally:
            backend.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        code = getattr(exc, "code", "evaluation_local_failure_reconcile_only")
        print(json.dumps({"status": "held", "reason": code, "automatic_retry": False}))
        raise SystemExit(2)
