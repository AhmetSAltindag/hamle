from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from cloud_store import OperationsStore, StoreError
from test_cloud_store import MemoryBackend, budget_snapshot
import v11_wat_shadow_worker as shadow_worker
from v11_wat_shadow_worker import ShadowJournal, load_case
from v11_wat_vision import VisionError, build_request


class ShadowJournalTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime.fromisoformat('2026-10-10T18:00:00+03:00')
        self.backend=MemoryBackend();self.ops=OperationsStore(self.backend,clock=lambda:self.now)
        self.ops.initialize_control();self.ops.initialize_budget(budget_snapshot())
        self.gen=self.ops.resume_review_only('test-owner')['generation']
        self.j=ShadowJournal(self.ops,'a','a'*64)
        self.j.initialize();self.ops.budget_reserve(self.j.key,'openai',500000)
        self.ops.budget_begin(self.j.key,self.gen)
    def test_only_one_winner(self):
        def start(_):
            try:self.j.begin('prepared','count_started');return True
            except VisionError:return False
        with ThreadPoolExecutor(max_workers=4) as pool:r=list(pool.map(start,range(4)))
        self.assertEqual(r.count(True),1)
    def test_started_never_replayed(self):
        self.j.begin('prepared','count_started')
        again=ShadowJournal(self.ops,'a','a'*64)
        self.assertEqual(again.initialize()['state'],'count_started')
        with self.assertRaises(VisionError):again.begin('prepared','count_started')
    def test_changed_input_cannot_resume(self):
        with self.assertRaises(VisionError):ShadowJournal(self.ops,'a','b'*64).initialize()
    def test_count_receipt_before_model(self):
        with self.assertRaises(VisionError):self.j.begin('counted','model_started')
        self.j.begin('prepared','count_started');self.j.finish('count_started','counted','token_count',{'input_tokens':1})
        self.assertEqual(self.j.begin('counted','model_started')['state'],'model_started')
    def test_completed_immutable_attempt(self):
        self.j.begin('prepared','model_started')
        self.j.finish('model_started','completed','result',{'status':'test'})
        with self.assertRaises(VisionError):self.j.begin('prepared','model_started')
    def test_stop_blocks_model_after_count(self):
        self.j.begin('prepared','count_started');self.j.finish('count_started','counted','token_count',{'input_tokens':1})
        self.ops.stop('test-owner')
        with self.assertRaises(StoreError):self.j.begin('counted','model_started')
    def test_receipt_persisted_after_stop(self):
        self.j.begin('prepared','model_started');self.ops.stop('test-owner')
        self.assertEqual(self.j.finish('model_started','completed','result',{'status':'test'})['state'],'completed')
    def test_no_case_budget_no_call(self):
        b=ShadowJournal(self.ops,'b','b'*64);b.initialize()
        with self.assertRaises(VisionError):b.begin('prepared','count_started')
    def test_cases_do_not_contain_model_expected_answer(self):
        for case_id in ('a','b'):
            c,refs=load_case(case_id)
            self.assertEqual(len(refs),3)
            self.assertNotIn('expected_price_result',c['context'])
            self.assertEqual(c['context']['source_binding']['producer_id'],47658175)
    def test_saved_model_receipt_survives_oversized_result_and_blocks_replay(self):
        self.j.begin('prepared','count_started')
        self.j.finish('count_started','counted','token_count',{'input_tokens':1000})
        self.j.begin('counted','model_started')
        receipt={'response_id':'resp_known_usage','model':'gpt-6.1-sol',
                 'binding_sha256':'a'*64,
                 'usage':{'input_tokens':1000,'output_tokens':100,'total_tokens':1100}}
        self.j.finish('model_started','model_receipt_saved','model_receipt',receipt)
        with self.assertRaises(StoreError):
            self.j.finish('model_receipt_saved','completed','result',{'large_text':'x'*60000})
        saved=self.j.get()
        self.assertEqual(saved['state'],'model_receipt_saved')
        self.assertEqual(saved['model_receipt'],receipt)
        self.assertNotIn('result',saved)
        with self.assertRaises(VisionError):
            self.j.begin('prepared','model_started')
    def test_pinned_context_must_match_each_provider_artifact_identity_field(self):
        original=json.loads((shadow_worker.APP/'wat-shadow-cases.json').read_text())
        for field in ('task_id','comment_id','attachment_id','producer_id','byte_size','sha256'):
            with self.subTest(field=field), tempfile.TemporaryDirectory(prefix='hamle-shadow-test-') as directory:
                cfg=copy.deepcopy(original)
                binding=cfg['cases']['a']['context']['source_binding']
                binding[field]='f'*64 if field=='sha256' else binding[field]+1
                path=Path(directory)
                (path/'wat-shadow-cases.json').write_text(json.dumps(cfg))
                with patch.object(shadow_worker,'APP',path):
                    with self.assertRaisesRegex(VisionError,'artifact_context_binding_mismatch'):
                        load_case('a')


if __name__=='__main__':unittest.main()
