"""Offline queue + delivery + fake provider acceptance, with two distinct jobs.

This proves module cooperation and fencing, not live Basecamp/Gmail acceptance.
"""
import unittest

from autonomy_policy import scope_digest
from test_autonomy_policy import NOW
from test_v11_delivery import exact_receipt, fixture, refresh_binding
from v11_delivery import DeliveryCoordinator, record_outcome
from v11_work_queue import InMemoryBackend, QueueError, QueueStore, WRITE_BINDING_FIELDS


class FakeProvider:
    def __init__(self):
        self.attempts = []

    def send_once(self, intent, *, unknown=False):
        self.attempts.append((intent['work_id'], intent['kind'], intent['payload_sha256']))
        if unknown:
            raise TimeoutError('Synthetic ambiguous provider outcome')
        return {'provider': 'gmail' if intent['kind'] == 'deliver_file' else 'basecamp',
                'remote_id': 'synthetic-remote-' + str(len(self.attempts)),
                'readback_sha256': intent['payload_sha256']}


class QueueDeliveryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.queue = QueueStore(InMemoryBackend(), clock=lambda: NOW.timestamp())
        self.queue.initialize()
        self.queue.set_control('queue_open', actor='synthetic-owner', expected_generation=0)
        self.coordinator, self.provider = DeliveryCoordinator(), FakeProvider()

    def bind(self, number):
        stored = self.queue.bind_work(brand_id='wat_mobilite', thread_id='thread-' + str(number),
                                     account_id=10, project_id=20, list_id=30, service_id='coordination',
                                     expected_generation=1)
        stored = self.queue.bind_task(stored['work_id'], task_id=40 + number, account_id=10,
                                     project_id=20, list_id=30, expected_generation=1)
        work, adapter = fixture(stored['work_id'], 40 + number, 'thread-' + str(number))
        for key in ('control', 'delegation'):
            adapter.request[key]['global_generation'] = 1
            adapter.request[key]['work_generation'] = 0
        return work, adapter

    def enqueue(self, work, adapter):
        # Trusted collector's immutable observation, separate from model content.
        fingerprint = scope_digest({'binding': adapter.snapshot['binding'],
                                    'task_fingerprint': adapter.snapshot['task_fingerprint'],
                                    'artifact': adapter.snapshot['artifact'],
                                    'source': adapter.request['source']['fingerprint']})
        return self.queue.enqueue_event(work['work_id'], event_id='task-' + str(work['task_id']) + '-comment-1',
                                        fingerprint=fingerprint, source_order=1, source_kind='basecamp',
                                        expected_generation=1)

    def process(self, work, adapter, state=None, *, unknown=False):
        claim = self.queue.claim_next(work['work_id'], worker_id='synthetic-worker', expected_generation=1)
        self.assertIsNotNone(claim)
        result = self.coordinator.poll(adapter, work, state, now=NOW)
        self.assertEqual(len(result['intents']), 1, result)
        intent = result['intents'][0]
        preflight = self.coordinator.preflight_intent(adapter, work, result['state'], intent, now=NOW)
        self.assertTrue(preflight['allowed'], preflight)
        # Budget checker is separately required by production dispatcher; no
        # paid model/provider costs exist in this deterministic fake flow.
        prepared_binding = {k: intent['authority_request']['work_binding'][k] for k in WRITE_BINDING_FIELDS}
        prepared_binding['queue_event_fingerprint'] = claim['event']['fingerprint']
        action = self.queue.begin_write(claim, action_kind=intent['kind'],
                                       payload_sha256=intent['payload_sha256'], action_key=intent['action_id'],
                                       prepared_binding=prepared_binding)
        state = record_outcome(preflight['state'], intent, status='started')
        try:
            receipt = self.provider.send_once(intent, unknown=unknown)
        except TimeoutError:
            self.queue.hold_unknown(action['id'])
            return record_outcome(state, intent, status='unknown')
        self.queue.confirm_write(action_id=action['id'], claim_token=claim['token'],
                                 payload_sha256=intent['payload_sha256'], receipt=receipt)
        state = record_outcome(state, intent, status='confirmed', receipt=exact_receipt(intent))
        self.queue.complete_event(claim, outcome='processed', evidence_ref='synthetic-queue-provider-readback')
        return state

    def test_two_jobs_revision_and_delivery_then_second_sweep_zero_writes(self):
        work1, adapter1 = self.bind(1)
        work2, adapter2 = self.bind(2)
        adapter1.snapshot['artifact']['deliverables'][0]['facts']['page_count'] = 2
        refresh_binding(work1, adapter1)
        self.enqueue(work1, adapter1)
        self.enqueue(work2, adapter2)
        state1 = self.process(work1, adapter1)
        state2 = self.process(work2, adapter2)
        self.assertEqual([a[1] for a in self.provider.attempts], ['add_comment', 'deliver_file'])
        self.assertEqual(state1['status'], 'changes_requested')
        self.assertEqual(state2['status'], 'delivered')
        for work, adapter, state in ((work1, adapter1, state1), (work2, adapter2, state2)):
            self.assertTrue(self.enqueue(work, adapter)['duplicate'])
            self.assertIsNone(self.queue.claim_next(work['work_id'], worker_id='another', expected_generation=1))
            self.assertEqual(self.coordinator.poll(adapter, work, state, now=NOW)['intents'], [])
        self.assertEqual(len(self.provider.attempts), 2)

    def test_unknown_one_job_holds_without_blocking_another_job(self):
        work1, adapter1 = self.bind(1)
        work2, adapter2 = self.bind(2)
        self.enqueue(work1, adapter1)
        self.enqueue(work2, adapter2)
        unknown_state = self.process(work1, adapter1, unknown=True)
        self.assertEqual(self.queue.get_work(work1['work_id'])['queue_state'], 'unknown')
        with self.assertRaisesRegex(QueueError, 'write_outcome_unresolved'):
            self.queue.claim_next(work1['work_id'], worker_id='retry', expected_generation=1)
        self.assertEqual(self.coordinator.poll(adapter1, work1, unknown_state, now=NOW)['intents'], [])
        delivered_state = self.process(work2, adapter2)
        self.assertEqual(delivered_state['status'], 'delivered')
        self.assertEqual(len(self.provider.attempts), 2)

    def test_takeover_between_preflight_and_write_is_fenced(self):
        work, adapter = self.bind(1)
        self.enqueue(work, adapter)
        claim = self.queue.claim_next(work['work_id'], worker_id='worker', expected_generation=1)
        result = self.coordinator.poll(adapter, work, None, now=NOW)
        intent = result['intents'][0]
        self.assertTrue(self.coordinator.preflight_intent(adapter, work, result['state'], intent, now=NOW)['allowed'])
        prepared_binding = {k: intent['authority_request']['work_binding'][k] for k in WRITE_BINDING_FIELDS}
        prepared_binding['queue_event_fingerprint'] = claim['event']['fingerprint']
        self.queue.set_work_mode(work['work_id'], mode='takeover', actor='synthetic-owner', expected_work_generation=0)
        with self.assertRaisesRegex(QueueError, 'work_taken_over'):
            self.queue.begin_write(claim, action_kind=intent['kind'], payload_sha256=intent['payload_sha256'],
                                   action_key=intent['action_id'], prepared_binding=prepared_binding)
        self.assertEqual(self.provider.attempts, [])

    def test_cross_work_intent_cannot_enter_other_work_write_lease(self):
        work1, adapter1 = self.bind(1)
        work2, adapter2 = self.bind(2)
        self.enqueue(work1, adapter1)
        self.enqueue(work2, adapter2)
        claim1 = self.queue.claim_next(work1['work_id'], worker_id='worker', expected_generation=1)
        intent2 = self.coordinator.poll(adapter2, work2, None, now=NOW)['intents'][0]
        prepared_binding = {k: intent2['authority_request']['work_binding'][k] for k in WRITE_BINDING_FIELDS}
        prepared_binding['queue_event_fingerprint'] = claim1['event']['fingerprint']
        with self.assertRaisesRegex(QueueError, 'prepared_work_or_source_mismatch'):
            self.queue.begin_write(claim1, action_kind=intent2['kind'], payload_sha256=intent2['payload_sha256'],
                                   action_key=intent2['action_id'], prepared_binding=prepared_binding)
        self.assertEqual(self.provider.attempts, [])


if __name__ == '__main__':
    unittest.main()
