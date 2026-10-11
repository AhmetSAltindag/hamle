"""Offline aggregate budget ledger; no billing API or provider kill switch.

All money is integer microdollars. Verified existing spend is a fixed baseline
per provider, to which this ledger adds settled costs. The baseline must exclude
calls subsequently recorded here. External spending after that observation is
not detected: production needs provider reconciliation before enabling calls.
No date/device change resets the ledger or renews the user's authorization.
SQLite coordinates processes on one local durable filesystem; this is not a
Cloud Run multi-instance database. Never put credentials in evidence references.
Prepaid purchases count as committed cash. For each provider the commitment is
the greater of evidenced purchases and usage plus open call reservations, so
consuming the purchased credit does not count the same money twice. That overlap
must be verified by the caller; unrelated historical usage needs reconciliation.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import json
import sqlite3


PROVIDERS = ("google_cloud", "openai", "visual_generation")
LIMIT_MICRODOLLARS = 50_000_000
STARTS_AT = datetime.fromisoformat("2026-10-09T00:00:00+03:00")
EXPIRES_AT = datetime.fromisoformat("2026-10-12T23:59:59+03:00")
POLICY = {
    "id": "hamle-shared-20261009-20261012",
    "limit_microdollars": LIMIT_MICRODOLLARS,
    "starts_at": STARTS_AT.isoformat(),
    "expires_at": EXPIRES_AT.isoformat(),
    "providers": PROVIDERS,
}


class BudgetBlocked(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def microdollars(value) -> int:
    """Accept decimal strings/Decimal/integers, never inexact binary floats."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise BudgetBlocked("decimal_amount_required")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0:
            raise BudgetBlocked("invalid_amount")
        with localcontext() as ctx:
            ctx.prec = max(32, len(amount.as_tuple().digits) + 8)
            scaled = amount * 1_000_000
            if scaled != scaled.to_integral_value() or scaled > 9_000_000_000_000_000:
                raise BudgetBlocked("invalid_amount")
            return int(scaled)
    except (InvalidOperation, ValueError, OverflowError):
        raise BudgetBlocked("invalid_amount") from None


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BudgetBlocked("timezone_required")
    return value


def _reference(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise BudgetBlocked("evidence_reference_required")
    return value


class BudgetLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS budget_settings(
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS budget_providers(
                provider TEXT PRIMARY KEY, baseline_microdollars INTEGER,
                billing_ready INTEGER, observed_at TEXT, evidence_ref TEXT);
            CREATE TABLE IF NOT EXISTS budget_reservations(
                reservation_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL REFERENCES budget_providers(provider),
                reserved_microdollars INTEGER NOT NULL CHECK(reserved_microdollars>0),
                actual_microdollars INTEGER, state TEXT NOT NULL,
                created_at TEXT NOT NULL, settled_at TEXT, evidence_ref TEXT);
            CREATE TABLE IF NOT EXISTS budget_billing_checks(
                check_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL REFERENCES budget_providers(provider),
                billing_ready INTEGER NOT NULL CHECK(billing_ready IN (0,1)),
                observed_at TEXT NOT NULL, evidence_ref TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS budget_prepaid_purchases(
                purchase_key TEXT PRIMARY KEY,
                provider TEXT NOT NULL REFERENCES budget_providers(provider),
                paid_microdollars INTEGER NOT NULL CHECK(paid_microdollars>0),
                currency TEXT NOT NULL CHECK(currency='USD'),
                purchased_at TEXT NOT NULL, evidence_ref TEXT NOT NULL);
        """)
        encoded = json.dumps(POLICY, sort_keys=True)
        try:
            with self._transaction():
                old = self.db.execute("SELECT value FROM budget_settings WHERE key='policy'").fetchone()
                if old is not None and old[0] != encoded:
                    raise BudgetBlocked("policy_mismatch_no_automatic_reset")
                self.db.execute("INSERT OR IGNORE INTO budget_settings VALUES('policy',?)", (encoded,))
                self.db.execute("INSERT OR IGNORE INTO budget_settings VALUES('overrun_hold','0')")
                for provider in PROVIDERS:
                    self.db.execute("INSERT OR IGNORE INTO budget_providers(provider) VALUES(?)", (provider,))
        except Exception:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    @staticmethod
    def _provider(provider):
        if provider not in PROVIDERS:
            raise BudgetBlocked("provider_not_authorized")

    def verify_provider(self, provider, *, existing_spend_usd, billing_ready,
                        observed_at, evidence_ref):
        """Record one verified baseline; changing it requires reconciliation.

        Repeating the exact amount and billing state is harmless. This method
        records caller-supplied evidence; it does not contact a billing service.
        """
        self._provider(provider)
        amount = microdollars(existing_spend_usd)
        if type(billing_ready) is not bool:
            raise BudgetBlocked("billing_status_required")
        stamp = _aware(observed_at).isoformat()
        evidence = _reference(evidence_ref)
        with self._transaction():
            row = self.db.execute("SELECT * FROM budget_providers WHERE provider=?", (provider,)).fetchone()
            if row["baseline_microdollars"] is not None:
                if (row["baseline_microdollars"] != amount
                        or row["billing_ready"] != int(billing_ready)):
                    raise BudgetBlocked("baseline_locked_reconciliation_required")
                return
            self.db.execute("UPDATE budget_providers SET baseline_microdollars=?, billing_ready=?, "
                            "observed_at=?, evidence_ref=? WHERE provider=?",
                            (amount, int(billing_ready), stamp, evidence, provider))

    def _billing_state(self, provider):
        latest = self.db.execute("SELECT billing_ready,observed_at FROM budget_billing_checks "
                                 "WHERE provider=? ORDER BY rowid DESC LIMIT 1", (provider,)).fetchone()
        return latest or self.db.execute("SELECT billing_ready,observed_at FROM budget_providers "
                                         "WHERE provider=?", (provider,)).fetchone()

    def record_billing_readiness(self, check_key, provider, *, billing_ready,
                                 observed_at, evidence_ref):
        """Append a caller-verified billing observation, without changing spend.

        This is an evidence assertion, not an API check or a purchase. The caller
        must separately verify the provider's payment/credit status. Original
        baseline evidence remains immutable, and a new observation never clears
        an overrun, releases funds, or makes an old call replayable.
        """
        self._provider(provider)
        key = _reference(check_key)
        if type(billing_ready) is not bool:
            raise BudgetBlocked("billing_status_required")
        observed_at = _aware(observed_at)
        evidence = _reference(evidence_ref)
        values = (provider, int(billing_ready), observed_at.isoformat(), evidence)
        with self._transaction():
            existing = self.db.execute("SELECT provider,billing_ready,observed_at,evidence_ref "
                                       "FROM budget_billing_checks WHERE check_key=?", (key,)).fetchone()
            if existing:
                if tuple(existing) != values:
                    raise BudgetBlocked("billing_check_key_conflict")
                return {"check_key": key, "newly_recorded": False}
            previous = self._billing_state(provider)
            if previous["observed_at"] is None:
                raise BudgetBlocked("provider_baseline_required")
            if observed_at < datetime.fromisoformat(previous["observed_at"]):
                raise BudgetBlocked("billing_observation_out_of_order")
            self.db.execute("INSERT INTO budget_billing_checks VALUES(?,?,?,?,?)", (key, *values))
            return {"check_key": key, "newly_recorded": True}

    def record_prepaid_purchase(self, purchase_key, provider, amount_usd, *,
                                currency, purchased_at, evidence_ref):
        """Record an already completed USD cash purchase; never buy or refund.

        The evidence must verify the actual cash paid and that recorded provider
        usage draws from these credits. This narrow implementation accepts only
        OpenAI with a verified zero baseline; the first purchase must precede
        settled usage. Other providers/history need explicit reconciliation.
        Do not record free/promotional credit. Billing readiness is separate.
        It records actual payments even over budget or after expiry, preserving
        accounting truth. Crossing the total cap creates a persistent hold.
        Callers must obtain authorization/check the budget BEFORE purchasing;
        this recording method cannot prevent an external payment.
        """
        self._provider(provider)
        if provider != "openai":
            raise BudgetBlocked("prepaid_provider_not_supported")
        key = _reference(purchase_key)
        amount = microdollars(amount_usd)
        if amount <= 0:
            raise BudgetBlocked("positive_purchase_required")
        if currency != "USD":
            raise BudgetBlocked("usd_purchase_required")
        stamp = _aware(purchased_at).isoformat()
        evidence = _reference(evidence_ref)
        values = (provider, amount, currency, stamp, evidence)
        with self._transaction():
            existing = self.db.execute("SELECT provider,paid_microdollars,currency,purchased_at,evidence_ref "
                                       "FROM budget_prepaid_purchases WHERE purchase_key=?", (key,)).fetchone()
            if existing:
                if tuple(existing) != values:
                    raise BudgetBlocked("purchase_key_conflict")
                return {"purchase_key": key, "newly_recorded": False}
            baseline = self.db.execute("SELECT baseline_microdollars FROM budget_providers "
                                        "WHERE provider=?", (provider,)).fetchone()[0]
            if baseline != 0:
                raise BudgetBlocked("prepaid_zero_baseline_required")
            prior_paid = self.db.execute("SELECT COALESCE(SUM(paid_microdollars),0) "
                                        "FROM budget_prepaid_purchases WHERE provider=?", (provider,)).fetchone()[0]
            prior_usage = self.db.execute("SELECT COALESCE(SUM(actual_microdollars),0) "
                                         "FROM budget_reservations WHERE provider=? AND state='settled'",
                                         (provider,)).fetchone()[0]
            if prior_paid == 0 and prior_usage > 0:
                raise BudgetBlocked("prepaid_usage_reconciliation_required")
            self.db.execute("INSERT INTO budget_prepaid_purchases VALUES(?,?,?,?,?,?)", (key, *values))
            totals = self._totals()
            if totals["committed"] > LIMIT_MICRODOLLARS:
                self.db.execute("UPDATE budget_settings SET value='1' WHERE key='overrun_hold'")
            return {"purchase_key": key, "newly_recorded": True}

    def _totals(self, *, extra_provider=None, extra_reserved=0):
        providers = list(self.db.execute("SELECT * FROM budget_providers"))
        known = all(r["baseline_microdollars"] is not None and r["billing_ready"] is not None
                    for r in providers) and len(providers) == len(PROVIDERS)
        spent = held = purchases = prepaid_unallocated = 0
        for provider in providers:
            settled, reserved = self.db.execute("SELECT "
                "COALESCE(SUM(CASE WHEN state='settled' THEN actual_microdollars ELSE 0 END),0), "
                "COALESCE(SUM(CASE WHEN state IN ('reserved','started','unknown') THEN reserved_microdollars ELSE 0 END),0) "
                "FROM budget_reservations WHERE provider=?", (provider["provider"],)).fetchone()
            paid = self.db.execute("SELECT COALESCE(SUM(paid_microdollars),0) FROM budget_prepaid_purchases "
                                   "WHERE provider=?", (provider["provider"],)).fetchone()[0]
            usage = (provider["baseline_microdollars"] or 0) + settled
            if provider["provider"] == extra_provider:
                reserved += extra_reserved
            spent += usage
            held += reserved
            purchases += paid
            prepaid_unallocated += max(0, paid - usage - reserved)
        hold = self.db.execute("SELECT value FROM budget_settings WHERE key='overrun_hold'").fetchone()[0] == '1'
        return {"known": known, "spent": spent, "held": held, "hold": hold,
                "purchases": purchases, "prepaid_unallocated": prepaid_unallocated,
                "committed": spent + held + prepaid_unallocated}

    def _check_observation_times(self, now):
        for provider in PROVIDERS:
            if datetime.fromisoformat(self._billing_state(provider)["observed_at"]) > now:
                raise BudgetBlocked("billing_observation_is_in_the_future")
        if any(datetime.fromisoformat(row[0]) > now for row in self.db.execute(
                "SELECT purchased_at FROM budget_prepaid_purchases")):
            raise BudgetBlocked("prepaid_purchase_is_in_the_future")

    def status(self):
        with self._transaction():
            totals = self._totals()
            known = totals["known"]
            return {"policy_id": POLICY["id"], "limit_microdollars": LIMIT_MICRODOLLARS,
                    "all_provider_baselines_and_billing_known": known,
                    "accounted_spend_microdollars": totals["spent"] if known else None,
                    "held_microdollars": totals["held"],
                    "prepaid_purchases_microdollars": totals["purchases"],
                    "prepaid_unallocated_hold_microdollars": totals["prepaid_unallocated"],
                    "total_committed_microdollars": totals["committed"] if known else None,
                    "remaining_microdollars": max(0, LIMIT_MICRODOLLARS - totals["committed"]) if known else None,
                    "overrun_hold": totals["hold"], "provider_hard_cap_installed": False,
                    "provider_reconciliation_connected": False}

    def reserve(self, key, provider, amount_usd, *, now):
        self._provider(provider)
        key = _reference(key)
        amount = microdollars(amount_usd)
        if amount <= 0:
            raise BudgetBlocked("positive_reservation_required")
        now = _aware(now)
        if now < STARTS_AT or now > EXPIRES_AT:
            raise BudgetBlocked("budget_authorization_expired_or_not_started")
        with self._transaction():
            if self.db.execute("SELECT value FROM budget_settings WHERE key='cloud_authority'").fetchone():
                raise BudgetBlocked("budget_authority_transferred_to_cloud")
            totals = self._totals()
            if not totals["known"]:
                raise BudgetBlocked("existing_spend_or_billing_unknown")
            self._check_observation_times(now)
            if totals["hold"]:
                raise BudgetBlocked("overrun_reconciliation_required")
            ready = self._billing_state(provider)["billing_ready"]
            if ready != 1:
                raise BudgetBlocked("provider_billing_not_ready")
            existing = self.db.execute("SELECT * FROM budget_reservations WHERE reservation_key=?", (key,)).fetchone()
            if existing:
                if existing["provider"] != provider or existing["reserved_microdollars"] != amount:
                    raise BudgetBlocked("reservation_key_conflict")
                result = dict(existing)
                result["newly_reserved"] = False
                return result
            if self._totals(extra_provider=provider, extra_reserved=amount)["committed"] > LIMIT_MICRODOLLARS:
                raise BudgetBlocked("aggregate_budget_exhausted")
            self.db.execute("INSERT INTO budget_reservations "
                            "(reservation_key,provider,reserved_microdollars,state,created_at) "
                            "VALUES(?,?,?,'reserved',?)", (key, provider, amount, now.isoformat()))
            return {"reservation_key": key, "provider": provider, "reserved_microdollars": amount,
                    "state": "reserved", "newly_reserved": True}

    def mark_unknown(self, key):
        """Keep the full reservation; there is no timeout-based refund/retry."""
        with self._transaction():
            row = self.db.execute("SELECT state FROM budget_reservations WHERE reservation_key=?", (key,)).fetchone()
            if row is None:
                raise BudgetBlocked("reservation_not_found")
            if row[0] not in {"reserved", "started", "unknown"}:
                raise BudgetBlocked("reservation_already_settled")
            self.db.execute("UPDATE budget_reservations SET state='unknown' WHERE reservation_key=?", (key,))

    def begin_call(self, key, *, now):
        """Atomically recheck and consume a reserved call's one execution chance.

        Use at the real provider boundary along with the separate authority and
        action gates. A crash after this point holds funds and cannot restart the
        call automatically. This method does not invoke any provider. A budget
        overrun cannot retract already started calls; there is no such guarantee.
        """
        now = _aware(now)
        if now < STARTS_AT or now > EXPIRES_AT:
            raise BudgetBlocked("budget_authorization_expired_or_not_started")
        with self._transaction():
            if self.db.execute("SELECT value FROM budget_settings WHERE key='cloud_authority'").fetchone():
                raise BudgetBlocked("budget_authority_transferred_to_cloud")
            row = self.db.execute("SELECT * FROM budget_reservations WHERE reservation_key=?", (key,)).fetchone()
            if row is None:
                raise BudgetBlocked("reservation_not_found")
            if row["state"] != "reserved":
                raise BudgetBlocked("budget_call_not_replayable")
            totals = self._totals()
            if not totals["known"]:
                raise BudgetBlocked("existing_spend_or_billing_unknown")
            if totals["hold"]:
                raise BudgetBlocked("overrun_reconciliation_required")
            if totals["committed"] > LIMIT_MICRODOLLARS:
                raise BudgetBlocked("aggregate_budget_exhausted")
            provider = self._billing_state(row["provider"])
            if provider["billing_ready"] != 1:
                raise BudgetBlocked("provider_billing_not_ready")
            self._check_observation_times(now)
            if datetime.fromisoformat(row["created_at"]) > now:
                raise BudgetBlocked("call_before_reservation")
            self.db.execute("UPDATE budget_reservations SET state='started' WHERE reservation_key=?", (key,))
            return {"reservation_key": key, "state": "started", "external_calls": 0}

    def settle(self, key, actual_usd, *, evidence_ref, now):
        """Record evidenced actual cost, even after expiry or above reservation.

        Any overrun creates a persistent hold on new reservations. There is no
        automatic release: a future explicit reconciliation workflow is needed.
        An evidenced zero cost can close an unknown reservation; a guess cannot.
        """
        actual = microdollars(actual_usd)
        evidence = _reference(evidence_ref)
        stamp = _aware(now).isoformat()
        with self._transaction():
            row = self.db.execute("SELECT * FROM budget_reservations WHERE reservation_key=?", (key,)).fetchone()
            if row is None:
                raise BudgetBlocked("reservation_not_found")
            if _aware(now) < datetime.fromisoformat(row["created_at"]):
                raise BudgetBlocked("settlement_before_reservation")
            if row["state"] == "settled":
                if row["actual_microdollars"] != actual or row["evidence_ref"] != evidence:
                    raise BudgetBlocked("settlement_conflict")
                return
            self.db.execute("UPDATE budget_reservations SET state='settled', actual_microdollars=?, "
                            "evidence_ref=?, settled_at=? WHERE reservation_key=?", (actual, evidence, stamp, key))
            if actual > row["reserved_microdollars"]:
                self.db.execute("UPDATE budget_settings SET value='1' WHERE key='overrun_hold'")
