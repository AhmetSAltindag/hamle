"""Crash-boundary tests for budget handoff; real temporary SQLite, no cloud."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import seed_cloud_state as seed
from budget import BudgetBlocked, BudgetLedger, PROVIDERS
from cloud_store import StoreError

NOW = datetime.fromisoformat("2026-10-10T02:00:00+03:00")


class CloudBudgetHandoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.root / "work/private-connections/shared-budget.sqlite3"
        self.db.parent.mkdir(parents=True)
        self.snapshot = self.root / "outputs/cloud-budget-import-snapshot.json"
        self.snapshot.parent.mkdir(parents=True)
        for target, value in (("ROOT", self.root), ("SNAPSHOT", self.snapshot)):
            setting = patch.object(seed, target, value)
            setting.start(); self.addCleanup(setting.stop)
        with BudgetLedger(self.db) as ledger:
            for provider in PROVIDERS:
                ledger.verify_provider(provider, existing_spend_usd="0", billing_ready=True,
                                       observed_at=NOW - timedelta(minutes=1), evidence_ref="synthetic-" + provider)
            ledger.reserve("cloud-account-manager-foundation-20261010-v1", "google_cloud", "5", now=NOW)

    def test_snapshot_write_crash_keeps_local_spending_frozen_and_retry_held(self):
        with patch.object(seed, "save_exclusive", side_effect=OSError("simulated disk failure")):
            with self.assertRaises(OSError):
                seed.export()
        self.assertFalse(self.snapshot.exists())
        with BudgetLedger(self.db) as ledger:
            authority = ledger.db.execute("SELECT value FROM budget_settings WHERE key='cloud_authority'").fetchone()
            self.assertEqual(authority[0], "hamle-operations")
            with self.assertRaises(BudgetBlocked) as error:
                ledger.reserve("after-crash", "openai", "1", now=NOW)
            self.assertEqual(error.exception.code, "budget_authority_transferred_to_cloud")
        with self.assertRaises(StoreError) as error:
            seed.export()
        self.assertEqual(error.exception.code, "local_handoff_without_snapshot_reconcile_required")

    def test_existing_snapshot_without_committed_local_freeze_is_rejected(self):
        self.snapshot.write_text(json.dumps({"synthetic": "orphaned snapshot"}))
        with self.assertRaises(StoreError) as error:
            seed.export()
        self.assertEqual(error.exception.code, "snapshot_without_local_freeze_reconcile_required")


if __name__ == "__main__":
    unittest.main()
