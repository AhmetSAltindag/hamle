"""Two pinned historical WAT outputs: real cloud bytes + bounded Sol vision.

Explicit maintenance entry point only. Never scheduled and never sends mail,
creates tasks or comments. Durable begun attempts are never automatically retried.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from cloud_store import FirestoreBackend, OperationsStore, DEADLINE, StoreError, _json
from cloud_providers import GoogleSecretManager
from v11_wat_vision import build_request, count_input_once, call_once, digest, VisionError
from v11_artifact_inspector import inspect_artifact

APP = Path(__file__).resolve().parent
VERSION = 'wat-real-output-shadow-20261010-v1'
MAX_COST_MICRO = 500000


def need(ok, code):
    if not ok: raise VisionError(code)


class ShadowJournal:
    def __init__(self, ops, case_id, binding_sha256):
        need(case_id in ('a', 'b'), 'case_not_allowlisted')
        self.ops = ops
        self.key = VERSION+'-'+case_id
        self.binding_sha256 = binding_sha256

    def get(self):
        return self.ops._run(lambda tx, now: tx.get(self.key), allow_expired=True)

    def initialize(self):
        def change(tx, now):
            control = self.ops._control(tx)
            self.ops._open(control, control['generation'], now)
            row = tx.get(self.key)
            if row is not None:
                need(row['binding_sha256'] == self.binding_sha256, 'existing_binding_changed')
                return row
            row = {'key': self.key, 'binding_sha256': self.binding_sha256, 'state': 'prepared',
                   'generation': control['generation'], 'created_at': now,
                   'customer_writes': 0, 'live_enabled': False}
            _json(row); tx.put(self.key, row)
            return row
        return self.ops._run(change)

    def gate(self, tx, now, row):
        self.ops._open(self.ops._control(tx), row['generation'], now)
        need(row['binding_sha256'] == self.binding_sha256 and now <= DEADLINE, 'binding_or_deadline')
        budget = self.ops._budget(tx)
        need(not budget['overrun_hold'] and budget['providers']['openai']['billing_ready'], 'budget_not_ready')
        need(self.ops._totals(budget)['total_committed_microdollars'] <= 50000000, 'budget_limit')
        reserve = next((r for r in budget['reservations'] if r['reservation_key'] == self.key), None)
        need(reserve is not None and reserve['provider'] == 'openai' and reserve['state'] == 'started'
             and reserve['reserved_microdollars'] == MAX_COST_MICRO, 'case_reservation_not_started')

    def begin(self, expected, started):
        def change(tx, now):
            row = tx.get(self.key)
            need(row is not None and row['state'] == expected, 'attempt_requires_readonly_reconciliation')
            self.gate(tx, now, row)
            row = dict(row, state=started, started_at=now)
            _json(row); tx.put(self.key, row)
            return row
        return self.ops._run(change)

    def finish(self, started, finished, field, value):
        def change(tx, now):
            row = tx.get(self.key)
            need(row is not None and row['state'] == started
                 and row['binding_sha256'] == self.binding_sha256, 'receipt_binding_invalid')
            # A stop cannot erase the already returned read-only provider receipt.
            row = dict(row, state=finished, completed_at=now)
            row[field] = value
            _json(row); tx.put(self.key, row)
            return row
        return self.ops._run(change, allow_expired=True)


def load_case(case_id):
    need(case_id in ('a', 'b'), 'case_not_allowlisted')
    cfg = json.loads((APP/'wat-shadow-cases.json').read_text())
    need(cfg['version'] == VERSION and cfg['live_enabled'] is False, 'case_config_invalid')
    case = cfg['cases'][case_id]
    need(all(case['context']['source_binding'].get(k) == v for k, v in case['artifact'].items()),
         'artifact_context_binding_mismatch')
    refs = []
    for r in cfg['reference_images']:
        path = APP/'wat-reference'/r['filename']
        need(path.parent == APP/'wat-reference' and not path.is_symlink(), 'reference_path_invalid')
        data = path.read_bytes()
        need(hashlib.sha256(data).hexdigest() == r['sha256'], 'reference_changed')
        refs.append({'role': 'reference', 'label': r['label'], 'data': data, 'sha256': r['sha256']})
    return case, refs


def run_case(case_id, ops, manager, reader):
    case, images = load_case(case_id)
    artifact = case['artifact']
    # All source identity and native attachment checks happen before trusting bytes.
    data = reader.download(task_id=artifact['task_id'], comment_id=artifact['comment_id'],
        attachment_id=artifact['attachment_id'], expected_sha256=artifact['sha256'],
        expected_size=artifact['byte_size'], producer_id=artifact['producer_id'])
    images.append({'role': 'output', 'label': 'İncelenen WAT KV', 'data': data, 'sha256': artifact['sha256']})
    _, binding = build_request(case['context'], images)
    journal = ShadowJournal(ops, case_id, digest(binding))
    row = journal.initialize()
    if row['state'] == 'completed':
        return {'case': case_id, 'state': 'already_completed', 'new_model_calls': 0, 'business_writes': 0,
                'result_sha256': digest(row['result'])}
    need(row['state'] == 'prepared', 'prior_attempt_readonly_reconcile_no_retry')
    inspection = inspect_artifact(data, declared_mime='image/png', expected_sha256=artifact['sha256'],
        brief_sha256=case['context']['brief_sha256'],
        output_spec={'id': 'wat-historical-kv', 'mime': 'image/png', 'width': 1080,
                     'height': 1350, 'text_inspection': 'tesseract'},
        source={'verified': True, 'account_id': 5898865, 'project_id': 40694338,
                'task_id': artifact['task_id'], 'attachment_id': artifact['attachment_id'],
                'evidence_ref': 'wat-native-file:'+str(artifact['attachment_id'])},
        inspected_at=datetime.now(timezone.utc).isoformat(), include_text=True)
    need(inspection['inspection_status'] == 'passed' and inspection['ocr']['status'] == 'completed',
         'real_bytes_inspection_not_completed')
    ops.budget_reserve(journal.key, 'openai', MAX_COST_MICRO)
    ops.budget_begin(journal.key, row['generation'])
    started = True
    try:
        api_key = manager.access('hamle-openai-api-key').decode('ascii')
        def before_count(actual):
            need(digest(actual) == journal.binding_sha256, 'count_binding_mismatch')
            journal.begin('prepared', 'count_started')
        counted = count_input_once(api_key, case['context'], images, before_post=before_count)
        need(counted['conservative_cost_usd'] * 1e6 <= MAX_COST_MICRO, 'count_exceeds_reservation')
        journal.finish('count_started', 'counted', 'token_count', counted)
        def before_model(actual):
            need(digest(actual) == counted['binding_sha256'], 'model_binding_mismatch')
            journal.begin('counted', 'model_started')
        result = call_once(api_key, case['context'], images, before_post=before_model)
        result['artifact_sha256'] = artifact['sha256']
        result['task_id'] = artifact['task_id']; result['comment_id'] = artifact['comment_id']
        result['producer_id'] = artifact['producer_id']
        result['deterministic_inspection'] = inspection
        checks = {r['id']: r['result'] for r in result['observation']['requirements']}
        result['historical_price_detection_check'] = checks['price'] == case['expected_price_result']
        result['customer_acceptance_proven'] = False
        result['unrecorded_historical_verbal_change_known'] = False
        usage = result['usage']
        result['token_cost_estimate_usd'] = (usage['input_tokens']*2+usage['output_tokens']*10)/1e6
        result['cost_is_invoice'] = False
        print(json.dumps({'event': 'wat_shadow_model_receipt', 'case': case_id,
                          'response_id': result['response_id'], 'usage': result['usage']}), flush=True)
        journal.finish('model_started', 'model_receipt_saved', 'model_receipt',
                       {'response_id': result['response_id'], 'usage': result['usage'],
                        'binding_sha256': result['binding_sha256'], 'model': 'gpt-6.1-sol'})
        journal.finish('model_receipt_saved', 'completed', 'result', result)
        return {'case': case_id, 'state': 'completed', 'new_model_calls': 1,
                'price_check_matches_historical_evidence': result['historical_price_detection_check'],
                'provisional_route': result['assessment']['provisional_route'], 'business_writes': 0,
                'token_cost_estimate_usd': result['token_cost_estimate_usd'], 'result_sha256': digest(result)}
    except VisionError as exc:
        failure = {'code': exc.code, 'usage': exc.usage, 'response_id': exc.response_id,
                   'automatic_retry': False, 'recorded_at': datetime.now(timezone.utc).isoformat()}
        print(json.dumps({'event': 'wat_shadow_failed_receipt', 'case': case_id, **failure}), flush=True)
        current = journal.get()
        if current['state'] in ('count_started', 'model_started', 'model_receipt_saved'):
            journal.finish(current['state'], 'failed_reconciliation_only', 'failure_receipt', failure)
        raise
    finally:
        if started: ops.budget_unknown(journal.key)


def main():
    p = argparse.ArgumentParser(); p.add_argument('--case', choices=('a', 'b'), required=True)
    args = p.parse_args()
    need(os.environ.get('HAMLE_ALLOW_FIRESTORE_OPERATIONS') == 'yes'
         and os.environ.get('HAMLE_PROJECT_ID') == 'gamechangers-hamle'
         and os.environ.get('HAMLE_FIRESTORE_DATABASE') == 'hamle-operations'
         and os.environ.get('CLOUD_RUN_JOB') == 'hamle-account-manager', 'authorized_cloud_runtime_required')
    from v11_wat_readonly import WATReadOnly
    backend = FirestoreBackend(opt_in='yes')
    manager = GoogleSecretManager()
    ops = OperationsStore(backend)
    reader = WATReadOnly(manager, claim_refresh=ops.claim_credential_refresh)
    try:
        print(json.dumps(run_case(args.case, ops, manager, reader), ensure_ascii=False))
    finally:
        reader.close(); manager.close(); backend.close()


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'state': 'held', 'reason': getattr(exc, 'code', 'shadow_runtime_failed'),
                          'automatic_retry': False, 'business_writes': 0}))
        raise SystemExit(2)
