import multiprocessing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from budget import BudgetBlocked, BudgetLedger, EXPIRES_AT, PROVIDERS, microdollars


NOW = datetime.fromisoformat("2026-10-10T12:00:00+03:00")


def race_reservation(path, key, ready, results, start):
    try:
        with BudgetLedger(path) as ledger:
            ready.put(key)
            if not start.wait(5):
                results.put("start_timeout")
                return
            try:
                ledger.reserve(key, "openai", "30", now=NOW)
                results.put("reserved")
            except BudgetBlocked as error:
                results.put(error.code)
    except Exception as error:
        results.put(type(error).__name__)


def race_purchase(path, ready, results, start):
    try:
        with BudgetLedger(path) as ledger:
            ready.put(True)
            if not start.wait(5):
                results.put("start_timeout")
                return
            result = ledger.record_prepaid_purchase("same-paid-receipt", "openai", "5", currency="USD",
                                                    purchased_at=NOW, evidence_ref="synthetic-receipt")
            results.put(result["newly_recorded"])
    except Exception as error:
        results.put(type(error).__name__)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "budget.sqlite"
        self.ledger = BudgetLedger(self.path)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def verify(self, spends=None, billing=None):
        spends, billing = spends or {}, billing or {}
        for provider in PROVIDERS:
            self.ledger.verify_provider(provider, existing_spend_usd=spends.get(provider, "0"),
                billing_ready=billing.get(provider, True), observed_at=NOW-timedelta(minutes=1),
                evidence_ref="synthetic-billing-evidence:"+provider)

    def assertBlocked(self, code, function, *args, **kwargs):
        with self.assertRaises(BudgetBlocked) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def purchase(self, key="receipt-1", amount="5", *, purchased_at=NOW):
        return self.ledger.record_prepaid_purchase(key, "openai", amount, currency="USD",
            purchased_at=purchased_at, evidence_ref="synthetic-receipt:"+key)

    def test_cloud_handoff_stops_new_local_reservations_and_existing_calls(self):
        self.verify()
        self.ledger.reserve("before-handoff", "openai", "1", now=NOW)
        self.ledger.db.execute("INSERT INTO budget_settings VALUES('cloud_authority','hamle-operations')")
        self.assertBlocked("budget_authority_transferred_to_cloud", self.ledger.reserve,
                           "after-handoff", "openai", "1", now=NOW)
        self.assertBlocked("budget_authority_transferred_to_cloud", self.ledger.begin_call,
                           "before-handoff", now=NOW)
        self.assertEqual(self.ledger.status()["held_microdollars"], 1_000_000)

    def test_billing_readiness_appends_evidence_preserving_baseline_and_holds(self):
        self.verify(billing={"openai": False})
        original = tuple(self.ledger.db.execute("SELECT * FROM budget_providers WHERE provider='openai'").fetchone())
        self.ledger.reserve("cloud", "google_cloud", "3", now=NOW)
        self.ledger.mark_unknown("cloud")
        kwargs = dict(billing_ready=True, observed_at=NOW, evidence_ref="synthetic-credit-and-payment-screen")
        self.assertTrue(self.ledger.record_billing_readiness("payment-check", "openai", **kwargs)["newly_recorded"])
        self.assertFalse(self.ledger.record_billing_readiness("payment-check", "openai", **kwargs)["newly_recorded"])
        self.ledger.reserve("model", "openai", "1", now=NOW)
        self.assertEqual(tuple(self.ledger.db.execute("SELECT * FROM budget_providers WHERE provider='openai'").fetchone()), original)
        self.ledger.record_billing_readiness("payment-disabled", "openai", billing_ready=False,
            observed_at=NOW+timedelta(seconds=1), evidence_ref="synthetic-billing-disabled")
        self.assertBlocked("provider_billing_not_ready", self.ledger.begin_call,
            "model", now=NOW+timedelta(seconds=2))
        self.assertFalse(self.ledger.record_billing_readiness("payment-check", "openai", **kwargs)["newly_recorded"])
        self.assertEqual(self.ledger.status()["held_microdollars"], 4_000_000)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM budget_billing_checks").fetchone()[0], 2)

    def test_billing_observation_strictness_conflict_and_time(self):
        self.assertBlocked("provider_baseline_required", self.ledger.record_billing_readiness,
            "check", "openai", billing_ready=True, observed_at=NOW, evidence_ref="synthetic")
        self.verify()
        for invalid in (1, 0, "true", None):
            self.assertBlocked("billing_status_required", self.ledger.record_billing_readiness,
                "check", "openai", billing_ready=invalid, observed_at=NOW, evidence_ref="synthetic")
        self.ledger.record_billing_readiness("check", "openai", billing_ready=True,
            observed_at=NOW+timedelta(seconds=1), evidence_ref="synthetic")
        self.assertBlocked("billing_check_key_conflict", self.ledger.record_billing_readiness,
            "check", "openai", billing_ready=False, observed_at=NOW+timedelta(seconds=1), evidence_ref="synthetic")
        self.assertBlocked("billing_observation_out_of_order", self.ledger.record_billing_readiness,
            "older", "openai", billing_ready=False, observed_at=NOW, evidence_ref="synthetic")
        self.assertBlocked("billing_observation_is_in_the_future", self.ledger.reserve,
            "r", "openai", "1", now=NOW)

    def test_prepaid_cash_and_usage_share_one_commitment_without_refunding_cash(self):
        self.verify()
        self.ledger.reserve("cloud", "google_cloud", "3", now=NOW)
        self.ledger.mark_unknown("cloud")
        self.purchase()
        self.assertEqual(self.ledger.status()["remaining_microdollars"], 42_000_000)
        self.ledger.reserve("model", "openai", "1", now=NOW)
        self.ledger.begin_call("model", now=NOW)
        self.assertEqual(self.ledger.status()["remaining_microdollars"], 42_000_000)
        self.ledger.settle("model", ".2", evidence_ref="synthetic-usage", now=NOW)
        status = self.ledger.status()
        self.assertEqual(status["accounted_spend_microdollars"], 200_000)
        self.assertEqual(status["held_microdollars"], 3_000_000)
        self.assertEqual(status["prepaid_unallocated_hold_microdollars"], 4_800_000)
        self.assertEqual(status["remaining_microdollars"], 42_000_000)
        self.purchase("receipt-2")
        with BudgetLedger(self.path) as reopened:
            self.assertEqual(reopened.status()["prepaid_purchases_microdollars"], 10_000_000)
            self.assertEqual(reopened.status()["remaining_microdollars"], 37_000_000)

    def test_purchase_idempotency_does_not_enable_billing_or_cover_other_provider(self):
        self.verify(billing={"openai": False})
        self.assertTrue(self.purchase()["newly_recorded"])
        self.assertFalse(self.purchase()["newly_recorded"])
        self.assertBlocked("purchase_key_conflict", self.purchase, amount="6")
        self.assertBlocked("provider_billing_not_ready", self.ledger.reserve, "model", "openai", "1", now=NOW)
        self.ledger.reserve("cloud", "google_cloud", "45", now=NOW)
        self.assertBlocked("aggregate_budget_exhausted", self.ledger.reserve,
            "cloud-extra", "google_cloud", ".01", now=NOW)
        self.ledger.record_billing_readiness("ready", "openai", billing_ready=True,
            observed_at=NOW, evidence_ref="synthetic-billing")
        self.ledger.reserve("covered-model", "openai", "5", now=NOW)
        self.assertEqual(self.ledger.status()["remaining_microdollars"], 0)
        self.assertBlocked("aggregate_budget_exhausted", self.ledger.reserve,
            "uncovered-model", "openai", ".01", now=NOW)

    def test_already_paid_purchase_over_cap_is_recorded_and_hold_persists(self):
        self.verify()
        self.ledger.reserve("waiting", "openai", "1", now=NOW)
        self.purchase(amount="51", purchased_at=EXPIRES_AT+timedelta(days=1))
        status = self.ledger.status()
        self.assertEqual(status["prepaid_purchases_microdollars"], 51_000_000)
        self.assertEqual(status["total_committed_microdollars"], 51_000_000)
        self.assertEqual(status["remaining_microdollars"], 0)
        self.assertTrue(status["overrun_hold"])
        self.ledger.settle("waiting", "0", evidence_ref="synthetic-confirmed-no-call", now=NOW)
        with BudgetLedger(self.path) as reopened:
            self.assertTrue(reopened.status()["overrun_hold"])
            self.assertEqual(reopened.status()["prepaid_purchases_microdollars"], 51_000_000)

    def test_prepaid_accepts_only_usd_cash_for_verified_zero_baseline_openai(self):
        self.assertBlocked("prepaid_zero_baseline_required", self.purchase)
        self.verify({"openai": "1"})
        self.assertBlocked("prepaid_zero_baseline_required", self.purchase)
        for invalid in (True, 5.0, "-1", "NaN"):
            with self.subTest(invalid=invalid), self.assertRaises(BudgetBlocked):
                self.purchase(amount=invalid)
        self.assertBlocked("positive_purchase_required", self.purchase, amount="0")
        for invalid in ("TRY", "usd", True, None):
            self.assertBlocked("usd_purchase_required", self.ledger.record_prepaid_purchase,
                "r", "openai", "5", currency=invalid, purchased_at=NOW, evidence_ref="synthetic")
        self.assertBlocked("prepaid_provider_not_supported", self.ledger.record_prepaid_purchase,
            "r", "google_cloud", "5", currency="USD", purchased_at=NOW, evidence_ref="synthetic")

    def test_prepaid_cannot_reclassify_prior_unfunded_usage_or_use_future_credit(self):
        self.verify()
        self.ledger.reserve("prior", "openai", "1", now=NOW)
        self.ledger.settle("prior", ".2", evidence_ref="synthetic-unfunded-usage", now=NOW)
        self.assertBlocked("prepaid_usage_reconciliation_required", self.purchase)
        with BudgetLedger(Path(self.temp.name) / "second.sqlite") as fresh:
            for provider in PROVIDERS:
                fresh.verify_provider(provider, existing_spend_usd="0", billing_ready=True,
                    observed_at=NOW, evidence_ref="synthetic")
            fresh.record_prepaid_purchase("future", "openai", "5", currency="USD",
                purchased_at=NOW+timedelta(seconds=1), evidence_ref="synthetic")
            self.assertBlocked("prepaid_purchase_is_in_the_future", fresh.reserve,
                "new", "openai", "1", now=NOW)

    def test_two_processes_recording_same_purchase_do_not_double_count(self):
        self.verify()
        context = multiprocessing.get_context("spawn")
        ready, results, start = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=race_purchase, args=(str(self.path), ready, results, start))
                     for _ in range(2)]
        try:
            for process in processes:
                process.start()
            ready.get(timeout=10)
            ready.get(timeout=10)
            start.set()
            self.assertCountEqual([results.get(timeout=10), results.get(timeout=10)], [True, False])
            self.assertEqual(self.ledger.status()["prepaid_purchases_microdollars"], 5_000_000)
        finally:
            start.set()
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join()
            ready.close()
            results.close()

    def test_new_database_has_no_assumed_credit_or_zero_spend(self):
        self.assertIsNone(self.ledger.status()["remaining_microdollars"])
        self.assertBlocked("existing_spend_or_billing_unknown", self.ledger.reserve, "first", "openai", "1", now=NOW)

    def test_all_provider_spend_and_billing_required(self):
        for provider in PROVIDERS[:-1]:
            self.ledger.verify_provider(provider, existing_spend_usd="0", billing_ready=True,
                observed_at=NOW, evidence_ref="synthetic")
        self.assertBlocked("existing_spend_or_billing_unknown", self.ledger.reserve, "test", "openai", "1", now=NOW)
        self.assertBlocked("billing_status_required", self.ledger.verify_provider, PROVIDERS[-1],
            existing_spend_usd="0", billing_ready=None, observed_at=NOW, evidence_ref="synthetic")

    def test_verified_unavailable_billing_blocks_target_provider(self):
        self.verify(billing={"google_cloud": False})
        self.assertBlocked("provider_billing_not_ready", self.ledger.reserve, "g", "google_cloud", "1", now=NOW)
        self.ledger.reserve("o", "openai", "1", now=NOW)

    def test_existing_spend_and_all_providers_share_one_cap(self):
        self.verify({"google_cloud": "10", "openai": "5", "visual_generation": "2"})
        self.ledger.reserve("g", "google_cloud", "20", now=NOW)
        self.ledger.reserve("o", "openai", "13", now=NOW)
        self.assertEqual(self.ledger.status()["remaining_microdollars"], 0)
        self.assertBlocked("aggregate_budget_exhausted", self.ledger.reserve, "v", "visual_generation", ".000001", now=NOW)

    def test_money_exact_and_float_negative_nonfinite_rejected(self):
        self.assertEqual(microdollars(Decimal("0.000001")), 1)
        for bad in (0.1, True, "-1", "NaN", "Infinity", ".0000001", "0.00000100000000000000000000000001"):
            with self.subTest(bad=bad), self.assertRaises(BudgetBlocked):
                microdollars(bad)

    def test_reservation_key_prevents_double_charging_and_payload_change(self):
        self.verify()
        self.assertTrue(self.ledger.reserve("same", "openai", "2", now=NOW)["newly_reserved"])
        self.assertFalse(self.ledger.reserve("same", "openai", "2", now=NOW)["newly_reserved"])
        self.assertEqual(self.ledger.status()["held_microdollars"], 2_000_000)
        self.assertBlocked("reservation_key_conflict", self.ledger.reserve, "same", "google_cloud", "2", now=NOW)

    def test_unknown_survives_reopen_and_never_refunds_automatically(self):
        self.verify()
        self.ledger.reserve("maybe-sent", "openai", "50", now=NOW)
        self.ledger.mark_unknown("maybe-sent")
        with BudgetLedger(self.path) as reopened:
            self.assertEqual(reopened.status()["held_microdollars"], 50_000_000)
            self.assertBlocked("aggregate_budget_exhausted", reopened.reserve, "new", "openai", "1", now=NOW+timedelta(days=1))
            existing = reopened.reserve("maybe-sent", "openai", "50", now=NOW)
            self.assertFalse(existing["newly_reserved"])
            self.assertEqual(existing["state"], "unknown")

    def test_settlement_releases_only_unused_reserve_and_is_idempotent(self):
        self.verify({"openai": "10"})
        self.ledger.reserve("r", "openai", "5", now=NOW)
        self.ledger.settle("r", "3", evidence_ref="synthetic-usage", now=NOW)
        self.ledger.settle("r", "3", evidence_ref="synthetic-usage", now=NOW)
        status = self.ledger.status()
        self.assertEqual(status["accounted_spend_microdollars"], 13_000_000)
        self.assertEqual(status["held_microdollars"], 0)
        self.assertEqual(status["remaining_microdollars"], 37_000_000)
        self.assertBlocked("settlement_conflict", self.ledger.settle, "r", "2", evidence_ref="synthetic-usage", now=NOW)

    def test_overrun_persists_hold_even_below_overall_limit(self):
        self.verify()
        self.ledger.reserve("r", "openai", "1", now=NOW)
        self.ledger.settle("r", "1.01", evidence_ref="synthetic-usage", now=NOW)
        with BudgetLedger(self.path) as reopened:
            self.assertTrue(reopened.status()["overrun_hold"])
            self.assertBlocked("overrun_reconciliation_required", reopened.reserve, "r2", "google_cloud", "1", now=NOW)

    def test_spend_above_cap_is_recorded_without_hiding_it(self):
        self.verify()
        self.ledger.reserve("r", "openai", "49", now=NOW)
        self.ledger.settle("r", "51", evidence_ref="synthetic-usage", now=NOW)
        self.assertEqual(self.ledger.status()["accounted_spend_microdollars"], 51_000_000)
        self.assertEqual(self.ledger.status()["remaining_microdollars"], 0)

    def test_deadline_inclusive_and_no_period_or_device_reset(self):
        self.verify()
        self.ledger.reserve("at-end", "openai", "1", now=EXPIRES_AT)
        with BudgetLedger(self.path) as reopened:
            self.assertEqual(reopened.status()["held_microdollars"], 1_000_000)
            self.assertBlocked("budget_authorization_expired_or_not_started", reopened.reserve,
                "after-end", "openai", "1", now=EXPIRES_AT+timedelta(microseconds=1))
            reopened.settle("at-end", ".5", evidence_ref="synthetic-late-cost", now=EXPIRES_AT+timedelta(days=1))
            self.assertEqual(reopened.status()["accounted_spend_microdollars"], 500_000)

    def test_baseline_cannot_be_replaced_with_zero_to_reset_spending(self):
        self.verify({"openai": "20"})
        self.assertBlocked("baseline_locked_reconciliation_required", self.ledger.verify_provider, "openai",
            existing_spend_usd="0", billing_ready=True, observed_at=NOW, evidence_ref="synthetic-reset")
        self.assertEqual(self.ledger.status()["remaining_microdollars"], 30_000_000)

    def test_timezone_and_future_evidence_enforced(self):
        self.verify()
        self.assertBlocked("timezone_required", self.ledger.reserve, "r", "openai", "1", now=NOW.replace(tzinfo=None))
        self.assertBlocked("billing_observation_is_in_the_future", self.ledger.reserve, "r", "openai", "1", now=NOW-timedelta(hours=1))

    def test_unknown_needs_evidenced_settlement(self):
        self.verify()
        self.ledger.reserve("r", "openai", "5", now=NOW)
        self.ledger.mark_unknown("r")
        self.assertBlocked("evidence_reference_required", self.ledger.settle, "r", "0", evidence_ref="", now=NOW)
        self.assertEqual(self.ledger.status()["held_microdollars"], 5_000_000)
        self.ledger.settle("r", "0", evidence_ref="synthetic-confirmed-no-charge", now=NOW)
        self.assertEqual(self.ledger.status()["held_microdollars"], 0)

    def test_call_boundary_rechecks_new_overrun_against_existing_reservation(self):
        self.verify()
        self.ledger.reserve("first", "openai", "1", now=NOW)
        self.ledger.reserve("waiting", "openai", "1", now=NOW)
        self.ledger.begin_call("first", now=NOW)
        self.ledger.settle("first", "1.1", evidence_ref="synthetic-overrun", now=NOW)
        self.assertBlocked("overrun_reconciliation_required", self.ledger.begin_call, "waiting", now=NOW)

    def test_started_call_holds_funds_after_restart_and_cannot_replay(self):
        self.verify()
        self.ledger.reserve("r", "openai", "5", now=NOW)
        self.assertEqual(self.ledger.begin_call("r", now=NOW)["external_calls"], 0)
        with BudgetLedger(self.path) as reopened:
            self.assertEqual(reopened.status()["held_microdollars"], 5_000_000)
            self.assertBlocked("budget_call_not_replayable", reopened.begin_call, "r", now=NOW)
            reopened.mark_unknown("r")
            self.assertEqual(reopened.status()["held_microdollars"], 5_000_000)

    def test_reservation_made_before_deadline_cannot_start_after_deadline(self):
        self.verify()
        self.ledger.reserve("r", "openai", "1", now=NOW)
        self.assertBlocked("budget_authorization_expired_or_not_started", self.ledger.begin_call,
                           "r", now=EXPIRES_AT+timedelta(seconds=1))

    def test_two_processes_cannot_each_reserve_30_from_one_50_budget(self):
        self.verify()
        context = multiprocessing.get_context("spawn")
        ready, results, start = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=race_reservation, args=(str(self.path), key, ready, results, start))
                     for key in ("worker-a", "worker-b")]
        try:
            for process in processes:
                process.start()
            ready.get(timeout=10)
            ready.get(timeout=10)
            start.set()
            outcomes = [results.get(timeout=10), results.get(timeout=10)]
            self.assertCountEqual(outcomes, ["reserved", "aggregate_budget_exhausted"])
            self.assertEqual(self.ledger.status()["held_microdollars"], 30_000_000)
        finally:
            start.set()
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join()
            ready.close()
            results.close()


if __name__ == "__main__":
    unittest.main()
