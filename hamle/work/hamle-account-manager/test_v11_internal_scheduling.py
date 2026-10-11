"""Real slot transactions + trusted pilot hook; no provider/network operations."""
from datetime import datetime,timedelta
import unittest
from unittest.mock import Mock,patch
from cloud_store import OperationsStore
from test_cloud_store import MemoryBackend,budget_snapshot
import worker

PILOT={'initialized':True,'pilot_id':'HAMLE-IC-URETIM-20261010-01','actions':[]}

class InternalSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.fromisoformat('2026-10-12T09:00:00+03:00')
        self.backend=MemoryBackend();self.store=OperationsStore(self.backend,clock=lambda:self.now)
        self.store.initialize_control();self.store.initialize_budget(budget_snapshot());self.store.resume_review_only('verified-owner')
        self.capture=Mock(side_effect=AssertionError('No old mail capture'))
        self.basecamp=Mock(side_effect=AssertionError('No old Basecamp'))
        self.key=Mock(side_effect=AssertionError('No model key'))
    def run_slot(self,callback):
        return worker.run_scheduled_review(self.store,capture=self.capture,check_basecamp=self.basecamp,load_key=self.key,
            advance_internal_pilot=callback,clock=lambda:self.now)
    def test_active_pilot_consumes_slot_and_skips_old_review(self):
        callback=Mock(return_value=PILOT)
        with patch.object(worker,'run_review',side_effect=AssertionError('No fallback')):
            result=self.run_slot(callback)
        self.assertEqual(result['status'],'already_recorded');self.assertEqual(result['internal_production'],PILOT)
        self.assertEqual(result['model_calls'],0);self.assertIsNone(result['business_writes']);callback.assert_called_once()
        self.assertEqual(self.backend.data[result['slot_key']]['state'],'finished');self.key.assert_not_called()
    def test_duplicate_slot_never_repeats_callback(self):
        callback=Mock(return_value=PILOT)
        self.run_slot(callback);self.assertEqual(self.run_slot(callback)['status'],'slot_already_claimed');callback.assert_called_once()
    def test_stopped_never_calls_callback(self):
        self.store.stop('owner');callback=Mock(side_effect=AssertionError('Stopped'))
        self.assertEqual(self.run_slot(callback)['status'],'stopped');callback.assert_not_called()
    def test_unknown_callback_holds_no_old_fallback_no_slot_replay(self):
        callback=Mock(side_effect=RuntimeError('Private provider result is unknown'))
        with patch.object(worker,'run_review',side_effect=AssertionError('No fallback')):
            result=self.run_slot(callback)
        self.assertEqual(result['status'],'held');self.assertFalse(result['automatic_retry_allowed']);self.assertIsNone(result['business_writes'])
        self.assertNotIn('Private',str(result));self.assertEqual(self.run_slot(callback)['status'],'slot_already_claimed');callback.assert_called_once()
    def test_none_missing_or_completed_pilot_runs_old_review(self):
        callback=Mock(return_value=None)
        with patch.object(worker,'run_review',return_value={'status':'no_new_work','business_writes':0,'model_calls':0}) as old:
            result=self.run_slot(callback)
        self.assertEqual(result['status'],'no_new_work');callback.assert_called_once();old.assert_called_once()
    def test_no_callback_keeps_old_behavior(self):
        with patch.object(worker,'run_review',return_value={'status':'no_new_work','business_writes':0,'model_calls':0}) as old:
            result=self.run_slot(None)
        self.assertEqual(result['status'],'no_new_work');old.assert_called_once()
    def test_wrong_pilot_summary_holds_and_does_not_fall_back(self):
        with patch.object(worker,'run_review',side_effect=AssertionError('No fallback')):
            result=self.run_slot(lambda:{'initialized':True,'pilot_id':'other-client'})
        self.assertEqual(result['status'],'held');self.assertEqual(result['reason'],'internal_pilot_summary_invalid')
    def test_stopped_after_callback_does_not_report_success(self):
        def advance():
            self.store.stop('owner');return PILOT
        result=self.run_slot(advance)
        self.assertEqual(result['status'],'held');self.assertEqual(result['reason'],'control_stopped_or_changed');self.assertIsNone(result['business_writes'])
    def test_weekend_expired_or_late_slots_do_not_start_pilot(self):
        for stamp in ('2026-10-10T09:00:00+03:00','2026-10-12T09:07:00+03:00','2028-01-04T09:00:00+03:00'):
            self.now=datetime.fromisoformat(stamp);callback=Mock(side_effect=AssertionError('Not in window'))
            self.assertEqual(self.run_slot(callback)['status'],'held');callback.assert_not_called()
    def test_next_valid_slot_advances_once_again(self):
        callback=Mock(return_value=PILOT)
        self.run_slot(callback);self.now+=timedelta(minutes=15);self.run_slot(callback)
        self.assertEqual(callback.call_count,2)

if __name__=='__main__':unittest.main()
