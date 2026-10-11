"""Crash after confirmed Gmail write must reconcile without another source/send."""
import unittest
from unittest.mock import patch

import test_v11_internal_worker as fixtures
from cloud_store import StoreError


class InternalWorkerRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InternalWorkerIntegrationTests()
        self.fixture.setUp()
        for phase in ('main-revision', 'main-corrected', 'story'):
            self.fixture.phase(phase)

    def test_verified_mail_then_failed_state_save_reconciles_without_reading_new_source(self):
        worker = self.fixture.worker()
        with patch.object(worker, 'save_state', side_effect=StoreError('store_outcome_unknown')):
            with self.assertRaisesRegex(StoreError, 'store_outcome_unknown'):
                worker.deliver()
        self.assertEqual(len(self.fixture.providers.mail), 1)
        self.assertIsNone(self.fixture.worker().load()['state']['internal_run']['delivery'])
        ledger = self.fixture.worker().pilot.summary()
        self.assertEqual(next(a['state'] for a in ledger['actions'] if a['action_key'] == 'final-delivery'), 'verified')
        count = len(self.fixture.providers.posts)
        # A real Gmail reader now observes Hamle's own sent mail and correctly
        # refuses it as new customer input. Recovery must reuse exact proof.
        with patch.object(self.fixture.providers, 'capture_source', side_effect=AssertionError('own sent mail is now latest')):
            self.fixture.worker().deliver()
        self.assertIsNotNone(self.fixture.worker().load()['state']['internal_run']['delivery'])
        self.assertEqual(len(self.fixture.providers.posts), count)
        self.assertEqual(len(self.fixture.providers.mail), 1)

    def test_receipt_without_verified_readback_still_requires_manual_reconciliation(self):
        worker = self.fixture.worker()
        with patch.object(self.fixture.providers, 'read_delivery', side_effect=RuntimeError('readback failed')):
            with self.assertRaisesRegex(RuntimeError, 'readback failed'):
                worker.deliver()
        count = len(self.fixture.providers.posts)
        with self.assertRaisesRegex(ValueError, 'prior_delivery_requires_readonly_reconciliation'):
            self.fixture.worker().deliver()
        self.assertEqual(len(self.fixture.providers.posts), count)
        self.assertEqual(len(self.fixture.providers.mail), 1)


if __name__ == '__main__':
    unittest.main()
