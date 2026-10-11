import copy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from cloud_store import OperationsStore, StoreError
from test_cloud_store import MemoryBackend, budget_snapshot
import v11_wat_video_shadow as video


class VideoShadowTests(unittest.TestCase):
    def setUp(self):
        self.backend=MemoryBackend()
        self.ops=OperationsStore(self.backend,clock=lambda:datetime.fromisoformat('2026-10-10T18:00:00+03:00'))
        self.ops.initialize_control();self.ops.initialize_budget(budget_snapshot())
        generation=self.ops.resume_review_only('test-owner')['generation']
        self.ops.budget_reserve('wat-v10-shadow-build-and-four-runs','google_cloud',1000000)
        self.ops.budget_begin('wat-v10-shadow-build-and-four-runs',generation)
        self.cfg={'version':video.KEY,'live_enabled':False,'brief':{'text':'data only'},'items':[
            {'id':str(n),'artifact':{'task_id':10317695863,'comment_id':100+n,'attachment_id':200+n,
                'producer_id':50486696,'sha256':'a'*64,'byte_size':20},
             'historical_approval':{'exact_hash_approval':False}} for n in range(2)]}
        self.cfg['brief_sha256']=video.digest(self.cfg['brief'])
    def call(self, reader=None):
        reader=reader or Mock(download=Mock(return_value=b'fake-test-bytes'))
        with patch.object(Path,'read_text',return_value=json.dumps(self.cfg)), \
             patch.object(video,'examine',return_value={'media':{'technical_status':'passed'},'human_review_required':True}):
            return video.run(self.ops,reader)
    def test_samples_bounded_unique_and_not_timestamps(self):
        self.assertEqual(video.sample_indices({'duration_seconds':'1','frame_rate':'2'}),[0,1])
        values=video.sample_indices({'duration_seconds':'60','frame_rate':'60'})
        self.assertEqual(len(values),8);self.assertEqual((values[0],values[-1]),(0,3599))
        with self.assertRaises(ValueError):video.sample_indices({'duration_seconds':'61','frame_rate':'60'})
    def test_completed_replay_has_no_further_reader_or_model_calls(self):
        reader=Mock(download=Mock(return_value=b'x'))
        self.assertEqual(self.call(reader)['state'],'completed')
        self.assertEqual(reader.download.call_count,2)
        self.assertEqual(self.call(reader)['state'],'already_completed')
        self.assertEqual(reader.download.call_count,2)
    def test_source_failure_never_auto_retries_started_attempt(self):
        reader=Mock(download=Mock(side_effect=ValueError('read failed')))
        with self.assertRaises(ValueError):self.call(reader)
        with self.assertRaisesRegex(ValueError,'prior_video_attempt'):self.call(reader)
        self.assertEqual(reader.download.call_count,1)
    def test_changed_context_cannot_reuse_receipt(self):
        self.call();self.cfg['items'][0]['artifact']['sha256']='b'*64
        with self.assertRaisesRegex(ValueError,'existing_video_binding_changed'):self.call()
    def test_global_stop_before_start_blocks_reader(self):
        self.ops.stop('test-owner');reader=Mock()
        with self.assertRaises(StoreError):self.call(reader)
        reader.download.assert_not_called()
    def test_other_producer_never_downloaded(self):
        self.cfg['items'][0]['artifact']['producer_id']=1;reader=Mock()
        with self.assertRaisesRegex(ValueError,'video_producer_binding_invalid'):self.call(reader)
        reader.download.assert_not_called()


if __name__=='__main__':unittest.main()
