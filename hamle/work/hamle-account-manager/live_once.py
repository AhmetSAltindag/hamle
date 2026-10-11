"""Explicit, one-shot, customer-free first Astra acceptance with a durable hold."""
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "work/hamle-runtime"))
from budget import BudgetLedger, BudgetBlocked
from model import build_request, call_once, ModelError

KEY = "astra-max-first-internal-20261010-v1"
EVIDENCE = ROOT / "outputs/new-mac-2026-10-10/openai-first-inference.json"


def now():
    return datetime.now(ZoneInfo("Europe/Istanbul"))


def save(path, value):
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def main():
    if sys.argv[1:] != ["--run-first-model"]:
        print(json.dumps({"status": "held", "reason": "explicit_first_model_flag_required"}))
        return 2
    os.umask(0o077)
    source = json.loads(Path(__file__).with_name("synthetic_request.json").read_text())
    payload = build_request(source)
    secret = ROOT / ".env.local"
    if secret.is_symlink() or stat.S_IMODE(secret.stat().st_mode) != 0o600:
        raise ModelError("private_key_file_required")
    values = [line.split("=", 1)[1].strip().strip("\"'")
              for line in secret.read_text().splitlines() if line.startswith("OPENAI_API_KEY=")]
    if len(values) != 1 or not values[0]:
        raise ModelError("credential_missing_or_invalid")
    attempt = ROOT / "work/private-connections" / (KEY + ".attempt.json")
    if attempt.exists() or EVIDENCE.exists():
        raise ModelError("first_model_attempt_already_recorded_no_retry")
    with BudgetLedger(ROOT / "work/private-connections/shared-budget.sqlite3") as ledger:
        reservation = ledger.reserve(KEY, "openai", "1", now=now())
        if reservation["state"] != "reserved":
            raise ModelError("first_model_attempt_not_replayable")
        save(attempt, {"started_at": now().isoformat(), "reservation_key": KEY,
                       "state": "attempt_recorded_no_automatic_retry"})
        ledger.begin_call(KEY, now=now())
        result, error, usage = None, None, None
        try:
            result = call_once(values[0], source)
            usage = result["usage"]
        except ModelError as exc:
            error, usage = exc.code, exc.usage
        except Exception:
            error = "outcome_unknown"
        # Keep funds held until the provider usage/cost evidence is reconciled.
        # A local estimate is useful but is not substituted for a provider invoice.
        ledger.mark_unknown(KEY)
        cost = None
        if usage is not None:
            cost = str((Decimal(usage["input_tokens"]) * 10
                        + Decimal(usage["output_tokens"]) * 50) / 1_000_000)
        report = {"recorded_at": now().isoformat(), "scope": "customer_free_internal_request",
                  "model_requested": "gpt-6-astra", "reasoning_requested": "max",
                  "model_call_count": 1, "customer_data_sent": False,
                  "status": "passed" if result is not None else "held", "error_code": error,
                  "result": result, "usage": usage,
                  "standard_uncached_token_cost_estimate_usd": cost,
                  "estimate_is_provider_invoice": False, "budget_reservation_key": KEY,
                  "budget_hold_retained_usd": "1", "budget": ledger.status(),
                  "external_business_writes": 0, "automatic_retry": False,
                  "cloud_execution_verified": False,
                  "request_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                  "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                    for p in [Path(__file__), Path(__file__).with_name("model.py"),
                                              Path(__file__).with_name("account_manager_prompt.txt")]}}
        save(EVIDENCE, report)
        print(json.dumps({"status": report["status"], "error_code": error,
                          "usage": usage, "estimated_token_cost_usd": cost,
                          "evidence": str(EVIDENCE), "external_business_writes": 0}, ensure_ascii=False))
        return 0 if result is not None else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ModelError, BudgetBlocked) as exc:
        print(json.dumps({"status": "held", "reason": exc.code, "automatic_retry": False}))
        raise SystemExit(2)
    except Exception:
        print(json.dumps({"status": "held", "reason": "first_model_local_failure", "automatic_retry": False}))
        raise SystemExit(2)
