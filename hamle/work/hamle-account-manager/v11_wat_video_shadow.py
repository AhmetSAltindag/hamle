"""Pinned Samet MP4 shadow read: actual decode/OCR, never customer delivery.

Sparse frames and stream metadata do not prove full video or audio correctness.
The sole runtime entry is an explicit, bounded maintenance job override.
"""
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import unicodedata

from cloud_store import FirestoreBackend, OperationsStore, DEADLINE, _json
from cloud_providers import GoogleSecretManager
from v11_wat_readonly import WATReadOnly
from v11_wat_media_inspector import inspect_wat_media
from v11_artifact_inspector import inspect_artifact
from v11_wat_vision import digest
from v11_wat_timed_frame import inspect_deadline_frame

KEY = 'wat-samet-video-shadow-20261010-v1'


def need(ok, code):
    if not ok: raise ValueError(code)


def sample_indices(facts):
    # These are frame indices, not a claim of exact presentation timestamps.
    count = int(Fraction(facts['duration_seconds']) * Fraction(facts['frame_rate']))
    need(1 <= count <= 3600, 'bounded_video_frame_count_required')
    return sorted({int((count - 1) * n / 7) for n in range(8)})


def normalized_text(text):
    value=unicodedata.normalize('NFKD',text.casefold().replace('ı','i'))
    return ''.join(c for c in value if c.isalnum() or c == '%')


def examine(data, item, brief_sha256):
    artifact = item['artifact']
    need(isinstance(item.get('timing_required_phrases'),list)
         and 1 <= len(item['timing_required_phrases']) <= 6
         and all(isinstance(p,str) and 1 <= len(p) <= 100 and normalized_text(p)
                 for p in item['timing_required_phrases']), 'timing_requirements_missing')
    source = {'verified': True, 'account_id': 5898865, 'project_id': 40694338,
              'task_id': artifact['task_id'], 'attachment_id': artifact['attachment_id'],
              'evidence_ref': 'wat-native-video:'+str(artifact['attachment_id'])}
    common = dict(declared_mime='video/mp4', expected_sha256=artifact['sha256'],
        brief_sha256=brief_sha256, source=source, checked_at=datetime.now(timezone.utc).isoformat(),
        output_spec={'id': item['id'], 'mime': 'video/mp4'})
    first = inspect_wat_media(data, **common)
    if first['technical_status'] != 'passed':
        return {'media': first, 'sampled_frame_ocr': [], 'human_review_required': True}
    indices = sample_indices(first['artifact']['facts'])
    measured = inspect_wat_media(data, **common, frame_indices=indices, include_frame_bytes=True)
    frames = measured.pop('frame_bytes', [])
    texts = []
    for frame in frames:
        read = inspect_artifact(frame['data'], declared_mime='image/png',
            expected_sha256=frame['sha256'], brief_sha256=brief_sha256, source=source,
            checked_at=datetime.now(timezone.utc).isoformat(), include_text=True,
            output_spec={'id': item['id']+'-frame-'+str(frame['frame_index']),
                         'mime': 'image/png', 'text_inspection': 'tesseract'})
        text = read.get('extracted_text', '')
        texts.append({'frame_index': frame['frame_index'], 'sha256': frame['sha256'],
                      'ocr': read.get('ocr'), 'text_sha256': read.get('text_sha256'),
                      'text': text[:800], 'text_truncated': len(text) > 800,
                      'timestamp_verified': False})
    timed = inspect_deadline_frame(data, expected_sha256=artifact['sha256'],
                                   deadline_seconds='2', include_frame_bytes=True)
    timed_png = timed.pop('frame_data', None)
    timing = {'status':'unassessed', 'evidence':timed, 'absence_is_failure':False,
              'exact_first_appearance_verified':False}
    if timed_png is not None and timed['status'] == 'verified_timestamp_frame':
        read = inspect_artifact(timed_png, declared_mime='image/png',
            expected_sha256=timed['frame']['sha256'], brief_sha256=brief_sha256, source=source,
            checked_at=datetime.now(timezone.utc).isoformat(), include_text=True,
            output_spec={'id':item['id']+'-at-deadline','mime':'image/png','text_inspection':'tesseract'})
        actual=read.get('extracted_text','')
        timing.update(ocr=read.get('ocr'), text=actual[:800], text_sha256=read.get('text_sha256'),
                      required_phrases=item['timing_required_phrases'], brief_sha256=brief_sha256)
        if (read.get('ocr',{}).get('status') == 'completed' and
                all(normalized_text(p) in normalized_text(actual) for p in item['timing_required_phrases'])):
            timing['status']='pass_positive_ocr_witness_at_or_before_deadline'
    return {'media': measured, 'sampled_frame_ocr': texts, 'message_timing':timing,
            'human_review_required': True,
            'message_visible_by_two_seconds_positive_witness':timing['status'].startswith('pass_'),
            'audio_script_or_listening_verified': False, 'full_content_review': False,
            'brand_or_creative_accepted': False, 'delivery_authorized': False}


def run(ops, reader):
    cfg = json.loads(Path(__file__).with_name('wat-video-shadow-cases.json').read_text())
    need(cfg['version'] == KEY and cfg['live_enabled'] is False and len(cfg['items']) == 2,
         'video_case_config_invalid')
    need(cfg['brief_sha256'] == digest(cfg['brief']), 'video_brief_binding_invalid')
    binding = digest(cfg)
    def begin(tx, now):
        old = tx.get(KEY)
        if old is not None:
            need(old['binding_sha256'] == binding, 'existing_video_binding_changed')
            need(old['state'] == 'completed', 'prior_video_attempt_reconcile_only')
            return old
        control = ops._control(tx); ops._open(control, control['generation'], now)
        budget = ops._budget(tx)
        need(now <= DEADLINE and not budget['overrun_hold']
             and ops._totals(budget)['total_committed_microdollars'] <= 50000000,
             'video_budget_closed')
        reserve = next((r for r in budget['reservations'] if r['reservation_key'] ==
                        'wat-v10-shadow-build-and-four-runs'), None)
        need(reserve is not None and reserve['provider'] == 'google_cloud'
             and reserve['reserved_microdollars'] >= 1000000, 'cloud_run_budget_not_reserved')
        row = {'state': 'started', 'binding_sha256': binding, 'generation': control['generation'],
               'started_at': now, 'business_writes': 0, 'model_calls': 0, 'live_enabled': False}
        _json(row); tx.put(KEY, row); return row
    state = ops._run(begin)
    if state['state'] == 'completed':
        return {'state': 'already_completed', 'new_model_calls': 0, 'business_writes': 0,
                'result_sha256': digest(state['results'])}
    results = []
    for item in cfg['items']:
        control = ops.control()
        need(control['mode'] == 'review_open' and control['generation'] == state['generation'],
             'video_control_changed')
        a = item['artifact']
        need(a['task_id'] == 10317695863 and a['producer_id'] == 50486696,
             'video_producer_binding_invalid')
        raw = reader.download(task_id=a['task_id'], comment_id=a['comment_id'],
            attachment_id=a['attachment_id'], producer_id=a['producer_id'],
            expected_sha256=a['sha256'], expected_size=a['byte_size'])
        result = examine(raw, item, cfg['brief_sha256'])
        results.append({'id': item['id'], 'source_artifact': a, 'inspection': result,
                        'historical_approval': item['historical_approval']})
    def finish(tx, now):
        row = tx.get(KEY)
        need(row['state'] == 'started' and row['binding_sha256'] == binding, 'video_receipt_binding_changed')
        row.update(state='completed', completed_at=now, results=results)
        _json(row); tx.put(KEY, row)
    ops._run(finish, allow_expired=True)
    return {'state': 'completed', 'videos': len(results), 'model_calls': 0, 'business_writes': 0,
            'technical_states': [r['inspection']['media']['technical_status'] for r in results],
            'complete_visual_or_audio_acceptance': False, 'result_sha256': digest(results)}


def main():
    need(os.environ.get('HAMLE_ALLOW_FIRESTORE_OPERATIONS') == 'yes'
         and os.environ.get('HAMLE_PROJECT_ID') == 'gamechangers-hamle'
         and os.environ.get('HAMLE_FIRESTORE_DATABASE') == 'hamle-operations'
         and os.environ.get('CLOUD_RUN_JOB') == 'hamle-account-manager', 'authorized_cloud_runtime_required')
    backend = FirestoreBackend(opt_in='yes'); manager = GoogleSecretManager()
    ops = OperationsStore(backend); reader = WATReadOnly(manager, claim_refresh=ops.claim_credential_refresh)
    try: print(json.dumps(run(ops, reader), ensure_ascii=False))
    finally: reader.close(); manager.close(); backend.close()


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'state': 'held', 'reason': getattr(exc, 'code', type(exc).__name__),
                          'automatic_retry': False, 'business_writes': 0}))
        raise SystemExit(2)
