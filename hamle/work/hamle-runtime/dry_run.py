"""Deterministic, local-only integration rehearsal. No provider calls."""
from datetime import date, datetime
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from budget import BudgetLedger, BudgetBlocked
from ledger import SyntheticLedger, Scope, ActionUnavailable
from schedule import day_plan, slots_for_day, summary_intent


def rehearse():
    day = date(2026, 10, 12)
    now = datetime.fromisoformat("2026-10-12T08:31:00+03:00")
    plan = day_plan(day)
    summary = summary_intent(slots_for_day(day)[0], outcome="success", completed_at=now)
    with TemporaryDirectory(prefix="hamle-dry-run-") as folder:
        folder = Path(folder)
        with BudgetLedger(folder / "budget.sqlite") as budget:
            try:
                budget.reserve("synthetic-model-call", "openai", "0.01", now=now)
                raise AssertionError("unknown_budget_must_block")
            except BudgetBlocked as blocked:
                budget_reason = blocked.code
            budget_status = budget.status()
        scope = Scope("Gamechangers", "synthetic-thread", "synthetic-job", 1)
        with SyntheticLedger(folder / "actions.sqlite") as ledger:
            action = ledger.enqueue_synthetic(scope, "synthetic-delivery", {"fixture": True}, now=100)
            duplicate = ledger.enqueue_synthetic(scope, "synthetic-delivery", {"fixture": True}, now=101)
            assert duplicate.action_id == action.action_id
            ledger.claim(action.action_id, "simulated-worker", now=102)
            ledger.stop_job(scope, now=103)
            assert ledger.get(action.action_id).state == "cancelled"

            uncertain_scope = Scope("Gamechangers", "synthetic-thread-2", "synthetic-job-2", 1)
            uncertain = ledger.enqueue_synthetic(uncertain_scope, "synthetic-uncertain", {"fixture": True}, now=104)
            lease = ledger.claim(uncertain.action_id, "simulated-worker", now=105)
            ledger.begin_simulated_dispatch(lease, now=106)
            ledger.mark_unknown(lease, now=107)
            try:
                ledger.claim(uncertain.action_id, "second-worker", now=108)
                raise AssertionError("unknown_outcome_must_not_retry")
            except ActionUnavailable:
                pass
    return {
        "mode": "synthetic_local_only", "external_calls": 0, "external_writes": 0,
        "paid_calls": 0, "scheduled_slots": len(plan["slots"]),
        "first_slot": plan["slots"][0], "last_slot": plan["slots"][-1],
        "holiday_policy": plan["holiday_policy"],
        "morning_summary_candidate_created": summary is not None,
        "morning_summary_sent": False, "duplicate_intent_deduplicated": True,
        "human_takeover_cancelled_waiting_action": True,
        "unknown_result_retry_blocked": True,
        "unverified_budget_block_reason": budget_reason,
        "budget": budget_status,
        "production_scheduler_installed": False,
        "customer_action_authority": False,
        "unseen_cc_removal_solved": False,
        "cloud_database_installed": False,
    }


if __name__ == "__main__":
    print(json.dumps(rehearse(), ensure_ascii=False, indent=2))
