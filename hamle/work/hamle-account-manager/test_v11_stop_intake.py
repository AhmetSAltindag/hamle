import copy
from dataclasses import asdict, replace
import unittest
from unittest.mock import Mock

from cloud_store import StoreError
from test_autonomy_policy import NOW
import test_v11_work_authority_store as fixtures
from v11_stop_intake import ReceivedWorkMail, run_stop_intake, stop_intent


class StopIntakeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AuthorityStoreTests();self.f.setUp();self.f.install()
        self.mail=ReceivedWorkMail('thread-1','abcd','client@example.test',
            ('hamle@gamechangers.com.tr',),('cc@example.test',),'e'*64,NOW.isoformat(),'Hamle, bu işi durdur.')
        self.reader=Mock();self.reader.read_latest.return_value=self.mail

    def run_intake(self):return run_stop_intake(self.f.ops,self.reader,clock=lambda:NOW)

    def test_real_received_customer_stop_holds_before_any_grant_or_send(self):
        self.f.claim();result=self.run_intake()
        self.assertEqual(len(result['stops']),1);self.assertTrue(result['stops'][0]['verification_pending'])
        state=self.f.store.observe_work(self.f.wid)
        self.assertEqual(state['record']['grant']['status'],'revoked')
        self.assertIsNone(state['work']['lease']);self.assertEqual(result['business_writes'],0)
        self.assertEqual(result['grants_created'],0)

    def test_agency_stop_uses_actual_sender_no_pending_customer_role(self):
        self.reader.read_latest.return_value=replace(self.mail,sender=fixtures.AHMET,text='Ben devralıyorum.')
        self.assertFalse(self.run_intake()['stops'][0]['verification_pending'])

    def test_normal_mail_is_readonly_and_does_not_create_grant(self):
        self.reader.read_latest.return_value=replace(self.mail,text='Teşekkürler, devam edelim.')
        before=copy.deepcopy(self.f.backend.data)
        self.assertEqual(self.run_intake()['stops'],[])
        self.assertEqual(before,self.f.backend.data)

    def test_stopped_work_is_not_read_again(self):
        self.f.stop();self.assertEqual(self.run_intake()['checked_work_count'],0)
        self.reader.read_latest.assert_not_called()

    def test_raw_model_dict_wrong_mailbox_or_missing_tocc_is_not_receipt(self):
        for value in (asdict(self.mail),replace(self.mail,provider_identity=fixtures.AHMET),
                      replace(self.mail,direct_to=()),replace(self.mail,automatic=True),
                      replace(self.mail,sender='hamle@gamechangers.com.tr')):
            self.reader.read_latest.return_value=value
            with self.assertRaisesRegex(StoreError,'v11_stop_intake_untrusted'):self.run_intake()
        self.assertEqual(self.f.queue.get_work(self.f.wid)['mode'],'active')

    def test_provider_failure_and_unreviewed_older_incoming_block_routine_slot(self):
        self.reader.read_latest.side_effect=RuntimeError('do not expose provider text')
        with self.assertRaisesRegex(StoreError,'v11_stop_intake_read_failed'):self.run_intake()
        self.reader.read_latest.side_effect=None
        self.reader.read_latest.return_value=replace(self.mail,text='Teşekkürler.',unseen_prior_incoming=True)
        with self.assertRaisesRegex(StoreError,'v11_stop_prior_messages_unreviewed'):self.run_intake()

    def test_quoted_old_stop_does_not_cancel_new_mail(self):
        self.assertIsNone(stop_intent('Devam edelim.\n> Bu işi durdur.'))
        self.assertIsNone(stop_intent('Devam edelim.\nFrom: X\nBen devralıyorum.'))
        self.assertEqual(stop_intent('Hamle DURDUR lütfen'),'explicit_stop')
        self.assertEqual(stop_intent('Ben devralıyorum'),'human_takeover')

    def test_pagination_limit_is_explicit_hold_no_silent_partial_sweep(self):
        from unittest.mock import patch
        with patch('v11_stop_intake.WorkAuthorityStore.queue') as queue:
            queue.return_value.page.return_value={'items':[],'next_cursor':'more'}
            with self.assertRaisesRegex(StoreError,'v11_stop_sweep_incomplete'):self.run_intake()


class ScheduledStopOrderTests(unittest.TestCase):
    def test_stop_intake_precedes_pilot_and_failed_sweep_blocks_model(self):
        from worker import run_scheduled_review
        from datetime import datetime
        clock=datetime.fromisoformat('2026-10-12T08:30:00+03:00')
        store=Mock();store.control.return_value={'mode':'review_open','generation':1}
        store.claim_scheduled_slot.return_value={'generation':1,'slot_key':'slot','scheduled_at':clock.isoformat()}
        order=[]
        def stop():order.append('stop');raise StoreError('v11_stop_intake_read_failed')
        pilot=Mock(side_effect=lambda:order.append('pilot'))
        model=Mock()
        result=run_scheduled_review(store,capture=Mock(),check_basecamp=Mock(),load_key=Mock(),model_call=model,
            clock=lambda:clock,check_work_stops=stop,advance_internal_pilot=pilot)
        self.assertEqual(result['status'],'held');self.assertEqual(order,['stop'])
        pilot.assert_not_called();model.assert_not_called()
        self.assertEqual(result['reason'],'v11_stop_intake_read_failed')


if __name__=='__main__':unittest.main()

class LatestSkipSafetyTests(unittest.TestCase):
    def invoke(self, metadata):
        from types import SimpleNamespace
        from unittest.mock import patch
        from v11_stop_intake import HamleStopReader
        import cloud_providers
        first={'email':'hamle@gamechangers.com.tr','mailbox_writes':0,'observation':{}}
        reader=SimpleNamespace(_read_verified=Mock(return_value=first))
        intake=SimpleNamespace(metadata=lambda _:metadata)
        with patch.object(cloud_providers,'_modules',return_value=(None,reader,None,intake)), \
             patch.object(cloud_providers,'_session'):
            return HamleStopReader(object()).read_latest({'thread_id':'ab12'},
                {'binding':{'source_message_id':'a1'}})

    def row(self, mid, time, **kw):
        return {'id':mid,'time_ms':time,'direction':'inbound','automated':False,
                'from':'client@example.test','to':['hamle@gamechangers.com.tr'],'cc':[],**kw}

    def test_later_auto_message_cannot_hide_unreviewed_stop(self):
        rows=[self.row('a1',1),self.row('a2',2),self.row('a3',3,automated=True)]
        with self.assertRaisesRegex(StoreError,'v11_stop_prior_messages_unreviewed'):
            self.invoke({'messages':rows,'latest':rows[-1]})

    def test_later_tocc_removal_cannot_hide_unreviewed_real_mail(self):
        rows=[self.row('a1',1),self.row('a2',2,to=['other@example.test'])]
        with self.assertRaisesRegex(StoreError,'v11_stop_prior_messages_unreviewed'):
            self.invoke({'messages':rows,'latest':rows[-1]})

    def test_only_auto_after_bound_source_can_skip_without_body(self):
        rows=[self.row('a1',1),self.row('a2',2,automated=True)]
        self.assertIsNone(self.invoke({'messages':rows,'latest':rows[-1]}))
