"""Internal technical fixture QA only. No model/provider calls or WAT release.

The accepted policy below is for the user's explicitly requested internal
production proof: measured PNG dimensions and literal OCR text. A score of 100
means those technical assertions passed, never professional artistic quality.
"""
from __future__ import annotations

from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import re
import unicodedata

from autonomy_policy import SHA256, scope_digest
from v11_artifact_inspector import INSPECTOR_ID, _spec as normalize_output_spec
from v11_quality_gate import MANDATORY_CHECKS, evaluate_quality

SUBJECT = 'HAMLE-IC-URETIM-20261010-01'
ACCOUNT, PROJECT, PARENT, HAMLE_ID = 5898865, 49184053, 10384126975, 53358100
AHMET, HAMLE = 'ahmet@gamechangers.com.tr', 'hamle@gamechangers.com.tr'
STRINGS = {'title': 'HAMLE İÇ DENEME', 'date': '12 EKİM 2026',
           'disclosure': 'Bu bir teknik denemedir.'}
DIMENSIONS = {'main': (1080, 1080), 'story': (1080, 1920)}
POLICY_ID = 'hamle-internal-technical-png-20261010-v1'


class InternalQualityError(ValueError):
    pass


def _need(ok, reason):
    if not ok:
        raise InternalQualityError(reason)


def _stamp(value):
    d = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    _need(d.tzinfo is not None and d.utcoffset() is not None, 'timezone_required')
    return d.astimezone(timezone.utc)


def _iso(value):
    return _stamp(value).isoformat()


def _hash(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _norm(value):
    value = unicodedata.normalize('NFKD', value).casefold()
    return ' '.join(''.join(c for c in value if not unicodedata.combining(c)).split())


def _source(source, now):
    _need(isinstance(source, dict), 'internal_source_required')
    value = source.get('evidence', source)
    _need(value.get('subject') == SUBJECT and value.get('source_subject') == SUBJECT
          and value.get('sender') == AHMET and value.get('hamle_direct_recipient') is True,
          'internal_source_identity_mismatch')
    _need(isinstance(value.get('latest_to'), list) and isinstance(value.get('latest_cc'), list)
          and HAMLE in value['latest_to'] + value['latest_cc']
          and set(value['latest_to'] + value['latest_cc']).issubset({AHMET, HAMLE}),
          'internal_source_actual_recipients_invalid')
    _need(all(isinstance(value.get(k), str) and value[k] for k in ('message_id', 'thread_id'))
          and _hash(value.get('snapshot_fingerprint')) and _hash(value.get('source_sha256')),
          'internal_source_binding_missing')
    _need(0 <= (_stamp(now) - _stamp(value.get('captured_at'))).total_seconds() <= 60,
          'internal_source_readback_stale')
    if 'source_text' in source:
        _need(hashlib.sha256(source['source_text'].encode()).hexdigest() == value['source_sha256'],
              'internal_source_body_hash_mismatch')
    return value


def output_spec(stage_id):
    _need(stage_id in DIMENSIONS, 'internal_stage_unknown')
    width, height = DIMENSIONS[stage_id]
    return {'id': stage_id, 'mime': 'image/png', 'min_bytes': 9, 'max_bytes': 2 * 1024 * 1024,
            'width': width, 'height': height, 'text_inspection': 'tesseract',
            'required_text': [{'id': key, 'text': value, 'critical': True}
                              for key, value in STRINGS.items()]}


def build_internal_brief(source, control_generation, now, expires):
    source = _source(source, now)
    _need(type(control_generation) is int and control_generation >= 0, 'control_generation_invalid')
    _need(_stamp(now) < _stamp(expires), 'internal_policy_expiry_invalid')
    return {'verified': True, 'version': 1, 'evidence_ref': 'gmail:' + source['message_id'],
            'brand_id': 'gamechangers', 'service_id': 'internal-technical-png-proof',
            'title': SUBJECT, 'objective': 'İç teknik üretim ve dosya kontrolü provası',
            'audience': 'Yalnız Ahmet ve Hamle', 'deadline': _iso(expires),
            'assets_ref': 'internal-generated-fixture-no-external-assets',
            'source_message_id': source['message_id'], 'source_fingerprint': source['snapshot_fingerprint'],
            'reply_envelope': {'thread_id': source['thread_id'], 'to': [AHMET], 'cc': [],
                               'in_reply_to_message_id': source['message_id']},
            'deliverables': [{'id': stage, 'kind': 'graphic', 'purpose': 'final',
                              'spec': output_spec(stage)} for stage in ('main', 'story')],
            'internal_technical_policy': {'id': POLICY_ID, 'accepted_at': _iso(now),
                                          'expires_at': _iso(expires),
                                          'control_generation': control_generation,
                                          'customer_live_enabled': False,
                                          'artistic_or_brand_quality_claim': False},
            'binding': {'account_id': ACCOUNT, 'project_id': PROJECT, 'list_id': PARENT,
                        'category_name': 'Hamle — İç Deneme', 'subscriber_ids': [HAMLE_ID],
                        'production_stages': [
                            {'id': 'main', 'kinds': ['graphic'], 'output_ids': ['main'],
                             'assignee_ids': [HAMLE_ID], 'depends_on': []},
                            {'id': 'story', 'kinds': ['graphic'], 'output_ids': ['story'],
                             'assignee_ids': [HAMLE_ID], 'depends_on': ['main']}]}}


def _policy(state):
    brief = state['brief']
    declaration = brief['internal_technical_policy']
    criteria = [('format', 10), ('dimensions', 25), ('title', 15), ('date', 25),
                ('disclosure', 15), ('exact_fixture_text', 10)]
    return {'schema_version': 1, 'policy_id': POLICY_ID, 'version': '1',
            'brand_id': brief['brand_id'], 'service_id': brief['service_id'], 'status': 'accepted',
            'verified': True, 'evidence_ref': brief['evidence_ref'] + ':internal-technical-proof-only',
            'accepted_at': declaration['accepted_at'], 'expires_at': declaration['expires_at'],
            'approval_actors': [AHMET], 'assessment_max_age_seconds': 1800,
            'approval_max_age_seconds': 600,
            'trusted_evaluators': {'deterministic': [INSPECTOR_ID], 'human_review': [], 'model_rubric': []},
            'critical_checks': list(MANDATORY_CHECKS),
            'critical_requirements': {name: {'requirement_refs': [POLICY_ID + '/' + name],
                                           'methods': ['deterministic']} for name in MANDATORY_CHECKS},
            'criteria': [{'id': key, 'description': 'İç teknik kontrol: ' + key, 'weight': weight,
                          'critical': key in ('date', 'disclosure'), 'methods': ['deterministic'],
                          'requirement_refs': [POLICY_ID + '/' + key]} for key, weight in criteria],
            'thresholds': {'human_min': '80', 'candidate_min': '90'}, 'internal_revision_limit': 2,
            'scope_label': 'internal_generated_technical_fixture_only',
            'artistic_or_brand_quality_claim': False, 'customer_live_enabled': False}


def _inspection(state, stage_id, inspection, now):
    stage = state['stages'][stage_id]
    submission = stage['submission']
    _need(isinstance(submission, dict) and submission.get('origin') == 'internal_fixture'
          and submission.get('producer_id') == HAMLE_ID, 'internal_fixture_submission_required')
    _need(isinstance(inspection, dict) and inspection.get('inspector_id') == INSPECTOR_ID,
          'trusted_byte_inspection_required')
    unsigned = copy.deepcopy(inspection)
    declared = unsigned.pop('evidence_sha256', None)
    unsigned.pop('extracted_text', None)
    _need(declared == scope_digest(unsigned), 'inspection_evidence_digest_mismatch')
    _need(inspection.get('output_spec_sha256') == scope_digest(normalize_output_spec(output_spec(stage_id)))
          and inspection.get('brief_sha256') == state['brief_sha256'], 'inspection_spec_or_brief_changed')
    source = inspection.get('source', {})
    _need(source.get('verified') is True and source.get('account_id') == ACCOUNT
          and source.get('project_id') == PROJECT and source.get('task_id') == stage['task']['task_id'],
          'inspection_source_target_mismatch')
    _need(inspection.get('source_sha256') == scope_digest(source), 'inspection_source_digest_mismatch')
    artifact = inspection.get('artifact', {})
    _need(len(submission['outputs']) == 1 and artifact.get('id') == stage_id
          and artifact.get('sha256') == submission['artifact']['sha256']
          and artifact.get('sha256') == submission['outputs'][0]['sha256']
          and str(source.get('attachment_id')) == submission['outputs'][0]['provider_attachment_id']
          and artifact.get('size_bytes') == submission['outputs'][0]['size_bytes']
          and artifact.get('mime_type') == submission['outputs'][0]['mime_type'],
          'inspection_submission_bytes_mismatch')
    _need(_stamp(submission['submitted_at']) <= _stamp(inspection['checked_at']) <= _stamp(now),
          'inspection_submission_time_mismatch')
    required = {'mime_type', 'size_bytes', 'fact:width', 'fact:height', 'text_inspection',
                'required_text:title', 'required_text:date', 'required_text:disclosure'}
    _need(set(inspection.get('checks', {})) == required, 'inspection_required_checks_missing')
    for row in inspection['checks'].values():
        _need(row.get('artifact_sha256') == artifact['sha256']
              and row.get('brief_sha256') == state['brief_sha256']
              and row.get('evaluator_id') == INSPECTOR_ID
              and row.get('result') in {'pass', 'fail', 'unassessed'}, 'inspection_check_binding_invalid')
    text = inspection.get('extracted_text')
    if text is not None:
        _need(isinstance(text, str) and hashlib.sha256(text.encode()).hexdigest() == inspection['text_sha256'],
              'inspection_text_digest_mismatch')
    return submission, text


def build_assessment(state, stage_id, inspection, source, now, generation, *, control=None):
    """Build exact existing-quality inputs. Missing current control yields a hold.

    Caller must pass the actual provider inspector result with include_text=True
    and trusted current global-control readback. This function does no I/O.
    """
    _need(state.get('mode') == 'internal_pilot' and stage_id in DIMENSIONS,
          'internal_pilot_scope_required')
    brief = state['brief']
    _need(brief.get('brand_id') == 'gamechangers' and brief.get('service_id') == 'internal-technical-png-proof'
          and brief.get('binding', {}).get('project_id') == PROJECT
          and brief.get('internal_technical_policy', {}).get('id') == POLICY_ID,
          'internal_pilot_scope_required')
    current = _source(source, now)
    _need(current['thread_id'] == brief['reply_envelope']['thread_id']
          and current['message_id'] == brief['source_message_id']
          and current['snapshot_fingerprint'] == brief['source_fingerprint'], 'internal_source_changed')
    _need(type(generation) is int and generation == brief['internal_technical_policy']['control_generation'],
          'internal_control_generation_changed')
    submission, text = _inspection(state, stage_id, inspection, now)
    policy = _policy(state)
    binding = {'brand_id': brief['brand_id'], 'work_id': state['work_id'], 'service_id': brief['service_id'],
               'thread_id': current['thread_id'], 'version': submission['artifact']['version'],
               'sha256': submission['artifact']['sha256'], 'brief_sha256': state['brief_sha256'],
               'manifest_sha256': submission['manifest_sha256'], 'scope_digest': scope_digest(policy),
               'source_fingerprint': current['snapshot_fingerprint'],
               'recipients_sha256': scope_digest({'to': [AHMET], 'cc': []}),
               'action_payload_sha256': scope_digest({'stage_id': stage_id,
                    'submission_sha256': scope_digest(submission), 'purpose': 'internal_stage_acceptance'}),
               'global_generation': generation, 'work_generation': state['revision']}
    checked = inspection['checked_at']
    checks = inspection['checks']
    ref = 'inspection:' + inspection['evidence_sha256']
    def merged(names):
        results = [checks[name]['result'] for name in names]
        return 'fail' if 'fail' in results else 'unassessed' if 'unassessed' in results else 'pass'
    ocr_known = inspection.get('ocr', {}).get('status') == 'completed' and text is not None
    expected_text = ' '.join(STRINGS.values())
    text_exact = 'unassessed' if not ocr_known else 'pass' if _norm(text) == _norm(expected_text) else 'fail'
    extra = 'unassessed'
    if ocr_known:
        tokens = lambda value: Counter(re.findall(r'\w+', _norm(value)))
        extra = 'pass' if not (tokens(text) - tokens(expected_text)) else 'fail'
    def record(name, result, observations, *, criterion=False):
        if result == 'unassessed':
            return {'result': 'unassessed'}
        row = {'verified': True, 'binding': copy.deepcopy(binding), 'method': 'deterministic',
               'evaluator_id': INSPECTOR_ID, 'evidence_ref': ref, 'checked_at': checked,
               'requirement_refs': [POLICY_ID + '/' + name],
               'observations': [{'evidence_ref': ref, 'location': location, 'finding': finding,
                                 'artifact_sha256': binding['sha256'],
                                 'brief_sha256': binding['brief_sha256']} for location, finding in observations],
               'result': 'passed' if result == 'pass' else 'failed'}
        if criterion:
            row['score'] = '100' if result == 'pass' else '0'
        return row
    def obs(names):
        return [(checks[name]['location'], checks[name]['finding']) for name in names]
    groups = {'format': ['mime_type', 'size_bytes'], 'dimensions': ['fact:width', 'fact:height'],
              'title': ['required_text:title'], 'date': ['required_text:date'],
              'disclosure': ['required_text:disclosure']}
    rows = {name: record(name, merged(names), obs(names), criterion=True) for name, names in groups.items()}
    rows['exact_fixture_text'] = record('exact_fixture_text', text_exact,
        [('actual pixel OCR', 'OCR text compared literally with the three internal-fixture lines; no artistic assessment.')],
        criterion=True)
    critical = {
        'brand_identity': record('brand_identity', merged(['required_text:title']), obs(['required_text:title'])),
        'required_files': record('required_files', 'pass', [('actual provider attachment bytes',
                              'Exactly this stage PNG was downloaded, hashed and bound to the producer submission.')]),
        'technical_specs': record('technical_specs', merged(['mime_type', 'size_bytes', 'fact:width', 'fact:height']),
                                  obs(['mime_type', 'size_bytes', 'fact:width', 'fact:height'])),
        'rights_and_disclosure': record('rights_and_disclosure', merged(['required_text:disclosure']),
            [('internal generated fixture', 'Internal technical fixture only; no external brand asset approval inferred.')]
            + obs(['required_text:disclosure'])),
        'internal_notes_excluded': record('internal_notes_excluded', extra,
            [('actual pixel OCR allowlist', 'Detected text contains only the fixed internal-fixture wording; OCR remains fallible.')])}
    assessment = {'schema_version': 1, 'verified': True, 'evidence_ref': ref,
                  'binding': copy.deepcopy(binding), 'policy_digest': scope_digest(policy),
                  'checked_at': checked, 'critical_checks': critical, 'criteria': rows}
    valid_control = (isinstance(control, dict) and control.get('verified') is True
                     and control.get('generation') == generation and control.get('mode') == 'active'
                     and state.get('control') == 'active' and isinstance(control.get('evidence_ref'), str)
                     and control.get('write_state') == 'new')
    safety = {'verified': bool(valid_control), 'binding': copy.deepcopy(binding),
              'observed_at': control.get('observed_at') if isinstance(control, dict) else _iso(now),
              'evidence_ref': control.get('evidence_ref') if isinstance(control, dict) else 'missing-control',
              'control': 'active' if valid_control else 'held', 'authority': 'allowed' if valid_control else 'held',
              'security': 'clear', 'write_state': 'new' if valid_control else 'held'}
    result = evaluate_quality(policy, assessment, binding, safety, now=now)
    return {'policy': policy, 'assessment': assessment, 'binding': binding, 'safety': safety,
            'result': result, 'scope_label': 'internal_technical_fixture_only',
            'artistic_or_brand_quality_claim': False, 'customer_live_enabled': False}


def build_stage_assessment_event(state, stage_id, inspection, source, now, generation, *, control=None):
    bundle = build_assessment(state, stage_id, inspection, source, now, generation, control=control)
    identity = scope_digest({'stage_id': stage_id, 'inspection': inspection['evidence_sha256'],
                             'revision': state['revision'], 'generation': generation})
    return {'id': 'internal-quality-' + identity, 'type': 'assess_stage', 'work_id': state['work_id'],
            'expected_revision': state['revision'], 'stage_id': stage_id, 'actor_id': HAMLE_ID,
            'verified': True, 'evidence_ref': bundle['assessment']['evidence_ref'],
            'occurred_at': _iso(now), **{k: bundle[k] for k in ('policy', 'assessment', 'binding', 'safety')}}
