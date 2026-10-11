"""Internal worker integration: durable memory transactions, real PNG bytes,
mocked provider writes and explicitly synthetic OCR. No cloud acceptance claim.
"""
import copy
from datetime import datetime, timedelta
import hashlib
import unittest
from unittest.mock import patch

from cloud_store import OperationsStore, StoreError
from test_cloud_store import MemoryBackend, budget_snapshot
from test_v11_artifact_inspector import png
from v11_artifact_inspector import inspect_artifact, InspectionError
from v11_internal_quality import AHMET, HAMLE, STRINGS
from v11_internal_worker import InternalWorker, PilotError
from v11_internal_providers import SUBJECT, PARENT


class FakeProviders:
    def __init__(self, clock, *, sgid_ids=False):
        self.clock = clock
        self.allowed_task_ids = frozenset()
        self.sgid_ids = sgid_ids
        self.posts = []
        self.tasks, self.uploads, self.comments, self.mail = {}, {}, {}, {}
        self.source_patch = {}
        self.fail_after_post = None
        self.before_hook = None
        self.readback_fail = False
        self.download_mutation = False
        self.source_body = ('1080x1080 ana görsel sonra 1080x1920 story PNG. '
                            'HAMLE İÇ DENEME / 12 EKİM 2026 / Bu bir teknik denemedir.')

    def capture_source(self):
        evidence = {'subject': SUBJECT, 'source_subject': SUBJECT, 'sender': AHMET,
            'hamle_direct_recipient': True, 'latest_to': [HAMLE], 'latest_cc': [],
            'message_id': 'abcd11', 'thread_id': 'abcd10',
            'snapshot_fingerprint': 'e' * 64,
            'source_sha256': hashlib.sha256(self.source_body.encode()).hexdigest(),
            'captured_at': self.clock().isoformat(), 'in_reply_to': '<internal@example.test>',
            'references': '<internal@example.test>'}
        evidence.update(self.source_patch)
        return {'source_text': self.source_body, 'evidence': evidence}

    def verify_internal(self):
        return {'todo_parent_id': PARENT, 'todo_parent_type': 'Todoset', 'identity_verified': True}

    def _post(self, kind, before_post, payload):
        if self.before_hook:
            hook, self.before_hook = self.before_hook, None
            hook()
        before_post()
        self.posts.append({'kind': kind, 'payload': copy.deepcopy(payload)})
        if self.fail_after_post == kind:
            raise RuntimeError('synthetic-lost-provider-response')

    def create_task(self, title, body, *, before_post):
        self._post('task', before_post, {'title': title, 'body': body})
        pid = 1000 + len(self.tasks)
        self.tasks[pid] = (title, body)
        return {'provider_id': str(pid)}

    def read_task(self, pid, title, body):
        return {'identity_verified': True, 'matches_approved_payload': self.tasks[int(pid)] == (title, body)}

    def upload_attachment(self, name, data, mime, *, before_post):
        self._post('upload', before_post, {'name': name, 'sha256': hashlib.sha256(data).hexdigest()})
        pid = ('basecamp-sgid-sha256:' + hashlib.sha256(name.encode()).hexdigest()) if self.sgid_ids else 3000 + len(self.uploads)
        row = {'provider_id': pid, 'filename': name, 'content_type': mime, 'byte_size': len(data),
               'sha256': hashlib.sha256(data).hexdigest(), 'attachable_sgid': 'synthetic-sgid-' + name}
        self.uploads[pid] = {'receipt': row, 'data': data}
        return copy.deepcopy(row)

    def create_comment(self, task_id, body, *, before_post, attachments=None):
        self._post('comment', before_post, {'task_id': task_id, 'body': body, 'attachments': attachments})
        pid = 2000 + len(self.comments)
        self.comments[pid] = {'task_id': task_id, 'body': body, 'attachments': copy.deepcopy(attachments or [])}
        return {'provider_id': str(pid)}

    def read_comment(self, pid, task_id, body, attachments=None):
        actual = self.comments[int(pid)]
        return {'identity_verified': True, 'matches_approved_payload': not self.readback_fail and
                actual == {'task_id': task_id, 'body': body, 'attachments': attachments or []},
                'attachment_receipts': copy.deepcopy(actual['attachments'])}

    def download_attachment(self, task_id, comment_id, receipt):
        assert self.comments[int(comment_id)]['task_id'] == task_id
        assert receipt in self.comments[int(comment_id)]['attachments']
        raw = self.uploads[receipt['provider_id']]['data']
        return raw + b'changed' if self.download_mutation else raw

    def send_delivery(self, source, body, files, *, before_post):
        self._post('mail', before_post, {'source': source, 'body': body,
                                      'files': [(f['filename'], hashlib.sha256(f['data']).hexdigest()) for f in files]})
        self.mail['abcd20'] = {'source': copy.deepcopy(source), 'body': body, 'files': copy.deepcopy(files)}
        return {'provider_id': 'abcd20'}

    def read_delivery(self, pid, source, body, files):
        return {'identity_verified': True, 'matches_approved_payload':
                self.mail[pid] == {'source': source, 'body': body, 'files': files},
                'provider_id': pid, 'recipients': {'to': [AHMET], 'cc': []}}


class InternalWorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.fromisoformat('2026-10-10T12:00:00+03:00')
        self.backend = MemoryBackend()
        self.ops = self.new_ops()
        self.ops.initialize_control()
        self.ops.initialize_budget(budget_snapshot())
        self.ops.resume_review_only('synthetic-authorized-ahmet')
        self.providers = FakeProviders(lambda: self.now)
        self.ocr_text_by_hash = {}
        self.ocr_unavailable = False

    def new_ops(self):
        return OperationsStore(self.backend, clock=lambda: self.now)

    def make_fixture(self, stage, *, missing_date=False):
        # Metadata changes the fixture hash; it is NOT used by the real inspector
        # as visible-text evidence. The mocked OCR response is explicit below.
        data = png(1080, 1080 if stage == 'main' else 1920,
                   text='synthetic-missing-date' if missing_date else 'synthetic-corrected-' + stage)
        strings = [STRINGS['title']]
        if not missing_date:
            strings.append(STRINGS['date'])
        strings.append(STRINGS['disclosure'])
        self.ocr_text_by_hash[hashlib.sha256(data).hexdigest()] = ' '.join(strings)
        return data

    def inspect(self, data, **kwargs):
        if self.ocr_unavailable:
            mock = patch('v11_artifact_inspector._isolated', side_effect=InspectionError('ocr_unavailable'))
        else:
            text = self.ocr_text_by_hash[hashlib.sha256(data).hexdigest()]
            mock = patch('v11_artifact_inspector._isolated', return_value={
                'text': text, 'parser': 'explicitly-synthetic-ocr', 'languages': ['eng', 'tur']})
        with mock:
            return inspect_artifact(data, **kwargs)

    def worker(self):
        return InternalWorker(self.new_ops(), self.providers, clock=lambda: self.now,
                              inspect=self.inspect, make_fixture=self.make_fixture)

    def phase(self, name):
        result = self.worker().run(name)
        self.now += timedelta(seconds=5)
        return result

    def all_phases(self):
        for name in ('main-revision', 'main-corrected', 'story', 'deliver'):
            self.phase(name)

    def test_realistically_advancing_clock_never_makes_current_control_future(self):
        # A different instant on every read matches real Cloud time. A fixed
        # unittest clock previously hid now-before-observed_at microseconds.
        def tick():
            self.now += timedelta(microseconds=137)
            return self.now
        self.providers.clock = tick
        for phase in ('main-revision', 'main-corrected', 'story', 'deliver'):
            worker = InternalWorker(OperationsStore(self.backend, clock=tick), self.providers,
                                    clock=tick, inspect=self.inspect, make_fixture=self.make_fixture)
            worker.run(phase)
        state = self.worker().load()['state']
        self.assertEqual(state['stages']['main']['status'], 'accepted')
        self.assertEqual(state['stages']['story']['status'], 'accepted')
        self.assertEqual([p['kind'] for p in self.providers.posts].count('mail'), 1)
        self.assertIsNotNone(state['internal_run']['delivery'])

    def test_cached_quality_hold_reassesses_same_file_without_reupload(self):
        from v11_internal_quality import build_stage_assessment_event as real_builder
        def future_safety_observation(*args, **kwargs):
            kwargs = copy.deepcopy(kwargs)
            # Recreate the exact observed cloud bug: fresh but fractionally
            # future safety evidence must hold instead of being autoaccepted.
            evaluation_time = datetime.fromisoformat(args[4])
            kwargs['control']['observed_at'] = (evaluation_time + timedelta(microseconds=1)).isoformat()
            return real_builder(*args, **kwargs)
        with patch('v11_internal_worker.build_stage_assessment_event', side_effect=future_safety_observation):
            with self.assertRaisesRegex(PilotError, 'unexpected_quality_route'):
                self.worker().run('main-revision')
        held = self.worker().load()['state']
        self.assertEqual(held['stages']['main']['status'], 'quality_held')
        self.assertEqual(held['stages']['main']['quality']['reason'], 'current_safety_evidence_stale')
        original = copy.deepcopy(held['internal_run']['artifacts']['main-v1'])
        before = len(self.providers.posts)
        self.assertEqual([p['kind'] for p in self.providers.posts], ['task', 'upload', 'comment'])
        self.now += timedelta(seconds=1)
        self.phase('main-revision')
        recovered = self.worker().load()['state']
        current = recovered['internal_run']['artifacts']['main-v1']
        self.assertEqual(recovered['stages']['main']['status'], 'changes_requested')
        self.assertEqual(current['quality']['route'], 'revision')
        self.assertEqual(current['quality']['score'], '65')
        self.assertEqual(current['task_id'], original['task_id'])
        self.assertEqual(current['submission_comment_id'], original['submission_comment_id'])
        self.assertEqual(current['attachment'], original['attachment'])
        added = self.providers.posts[before:]
        self.assertEqual([p['kind'] for p in added], ['comment'])
        self.assertIsNone(added[0]['payload']['attachments'])
        self.assertTrue(added[0]['payload']['body'].startswith('İç teknik kontrol: main-v1'))
        self.assertEqual(len(self.providers.tasks), 1)
        self.assertEqual(len(self.providers.uploads), 1)
        self.assertEqual(len(self.providers.mail), 0)
        count = len(self.providers.posts)
        self.phase('main-revision')
        self.assertEqual(len(self.providers.posts), count)

    def test_four_phases_restart_with_persistent_state_and_exact_one_mail(self):
        self.phase('main-revision')
        state = self.worker().load()['state']
        self.assertEqual(state['stages']['main']['status'], 'changes_requested')
        self.assertLess(float(state['stages']['main']['quality']['score']), 80)
        self.assertEqual(state['stages']['story']['status'], 'waiting')
        self.assertEqual(len(self.providers.tasks), 1)
        self.phase('main-corrected')
        self.assertEqual(self.worker().load()['state']['stages']['main']['status'], 'accepted')
        self.phase('story')
        state = self.worker().load()['state']
        self.assertEqual(state['status'], 'production_complete')
        self.assertEqual(len(self.providers.tasks), 2)
        self.assertNotEqual(state['stages']['main']['task']['task_id'], state['stages']['story']['task']['task_id'])
        self.phase('deliver')
        state = self.worker().load()['state']
        self.assertIsNotNone(state['internal_run']['delivery'])
        self.assertEqual([p['kind'] for p in self.providers.posts].count('mail'), 1)
        self.assertEqual([f['filename'] for f in self.providers.mail['abcd20']['files']],
                         ['hamle-main-v2.png', 'hamle-story-v1.png'])
        self.assertFalse(state['internal_run']['wat_live_enabled'])
        self.assertTrue(all(a['state'] == 'verified' for a in self.worker().pilot.summary()['actions']))

    def test_replaying_all_phases_adds_zero_provider_posts(self):
        self.all_phases()
        count = len(self.providers.posts)
        self.all_phases()
        self.assertEqual(len(self.providers.posts), count)

    def test_story_cannot_skip_main_acceptance(self):
        with self.assertRaisesRegex(PilotError, 'dependency_not_accepted'):
            self.phase('story')
        self.assertEqual(self.providers.posts, [])

    def test_corrected_main_cannot_skip_actual_revision(self):
        with self.assertRaisesRegex(PilotError, 'verified_revision_required_before_correction'):
            self.phase('main-corrected')
        self.assertEqual(self.providers.posts, [])

    def test_failed_main_prevents_early_delivery(self):
        self.phase('main-revision')
        count = len(self.providers.posts)
        with self.assertRaisesRegex(ValueError, 'production_not_complete'):
            self.phase('deliver')
        self.assertEqual(len(self.providers.posts), count)
        self.assertEqual(self.providers.mail, {})

    def test_source_change_blocks_next_provider_write(self):
        self.phase('main-revision')
        count = len(self.providers.posts)
        self.providers.source_patch['snapshot_fingerprint'] = 'f' * 64
        with self.assertRaises(Exception):
            self.phase('main-corrected')
        self.assertEqual(len(self.providers.posts), count)
        self.assertEqual(self.providers.mail, {})

    def test_human_stop_blocks_new_phase_and_no_posts(self):
        self.phase('main-revision')
        count = len(self.providers.posts)
        self.ops.stop('synthetic-owner-takeover')
        with self.assertRaises(StoreError):
            self.phase('main-corrected')
        self.assertEqual(len(self.providers.posts), count)

    def test_unknown_write_never_automatically_retried_after_restart(self):
        self.providers.fail_after_post = 'task'
        with self.assertRaises(RuntimeError):
            self.phase('main-revision')
        self.assertEqual(len(self.providers.posts), 1)
        actions = self.worker().pilot.summary()['actions']
        self.assertEqual(actions[0]['state'], 'unknown')
        self.providers.fail_after_post = None
        with self.assertRaisesRegex(PilotError, 'prior_write_requires_readonly_reconciliation'):
            self.phase('main-revision')
        self.assertEqual(len(self.providers.posts), 1)

    def test_real_downloaded_bytes_change_blocks_acceptance_and_mail(self):
        self.providers.download_mutation = True
        with self.assertRaisesRegex(PilotError, 'actual_uploaded_file_mismatch'):
            self.phase('main-revision')
        self.assertEqual(self.providers.mail, {})
        self.assertEqual(self.worker().load()['state']['stages']['main']['status'], 'running')

    def test_unknown_ocr_holds_after_real_attachment_without_next_task(self):
        self.ocr_unavailable = True
        with self.assertRaisesRegex(PilotError, 'real_cloud_ocr_not_completed'):
            self.phase('main-revision')
        self.assertEqual(len(self.providers.tasks), 1)
        self.assertEqual(self.providers.mail, {})
        self.assertEqual(self.worker().load()['state']['stages']['main']['status'], 'running')

    def test_provider_confirmed_sgid_hash_attachment_identity_integrates(self):
        self.providers.sgid_ids = True
        self.all_phases()
        self.assertEqual(len(self.providers.mail), 1)
        state = self.worker().load()['state']
        self.assertTrue(state['stages']['main']['submission']['outputs'][0]['provider_attachment_id'].startswith(
            'basecamp-sgid-sha256:'))

    def test_quality_notes_are_confirmed_before_mail_post(self):
        self.all_phases()
        posts = self.providers.posts
        self.assertEqual(posts[-1]['kind'], 'mail')
        notes = [p for p in posts if p['kind'] == 'comment' and p['payload']['body'].startswith('İç teknik kontrol:')]
        self.assertEqual(len(notes), 3)
        self.assertIn('Revizyon:', notes[0]['payload']['body'])

    def test_bad_source_bootstrap_does_no_provider_write(self):
        self.providers.source_body = 'arbitrary request without required fixed fixture content'
        with self.assertRaisesRegex(PilotError, 'internal_fixture_brief_mismatch'):
            self.phase('main-revision')
        self.assertEqual(self.providers.posts, [])


if __name__ == '__main__':
    unittest.main()
