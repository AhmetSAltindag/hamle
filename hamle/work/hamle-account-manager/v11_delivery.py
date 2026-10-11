"""Versioned Basecamp → QA → delivery coordinator, isolated from the V7 runtime.

No network, model invocation or provider write lives here. Adapters must obtain
current, authenticated evidence, hash downloaded bytes, and verify producer/QA
records outside mail or comment text. Models cannot issue release/authority.
The returned intent is a proposal. A dispatcher must acquire a work-wide queue
lease, call preflight_intent immediately before each write, and use the durable
queue's no-retry/unknown-write rules. Local state must be persisted atomically.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Protocol

from autonomy_policy import HAMLE, SHA256, authorize_action, scope_digest, validate_scope
from v11_quality_gate import build_quality_binding, build_quality_note_context, evaluate_quality


class DeliveryAdapter(Protocol):
    def read_basecamp(self, work: dict, *, after_cursor: str | None) -> dict:
        """Current complete task/artifact snapshot + one bounded page of events.

        Snapshot binding, assignees, artifact bytes/facts, producer submission,
        final release and QA provenance are verified by this trusted adapter.
        Even an unchanged comment cursor requires a fresh current-state read.
        """
        ...

    def read_authority(self, work: dict) -> dict:
        """Return current {'scope': verified scope, 'request': evidence fields}.

        Never use a model's interpretation of consent. Include current control,
        latest-source, delegation and (when needed) fresh_work_authorization.
        """
        ...


class DeliveryError(ValueError):
    pass


def _need(value, reason):
    if not value:
        raise DeliveryError(reason)


def _stamp(value):
    d = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    _need(d.tzinfo is not None, 'timestamp_timezone_required')
    return d.astimezone(timezone.utc)


def _fresh(value, now, seconds=60):
    return 0 <= (now - _stamp(value)).total_seconds() <= seconds


def _hash(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 16000


def new_state(work_id):
    return {'schema_version': 1, 'work_id': work_id, 'status': 'awaiting_production',
            'reason': 'no_production_observed', 'cursor': None, 'source_order': -1,
            'receipts': {}, 'pending_intent_id': None, 'last_confirmed_delivery': None,
            'quality_revision_count': 0, 'last_confirmed_quality_note': None}


def _binding(work):
    return {k: work[k] for k in ('work_id', 'brand_id', 'service_id', 'thread_id',
                                 'account_id', 'project_id', 'list_id', 'task_id')}


def _check_work(work):
    _need(isinstance(work, dict), 'work_missing')
    _binding(work)
    for k in ('account_id', 'project_id', 'list_id', 'task_id'):
        _need(type(work[k]) is int and work[k] > 0, 'work_binding_invalid')
    for k in ('work_id', 'brand_id', 'service_id', 'thread_id'):
        _need(_text(work[k]), 'work_binding_invalid')
    _need(type(work.get('revision_index')) is int and work['revision_index'] >= 0,
          'revision_index_missing')
    brief = work.get('brief')
    _need(isinstance(brief, dict) and brief.get('verified') is True
          and _text(brief.get('evidence_ref')), 'verified_brief_missing')
    required = brief.get('required_deliverables')
    _need(isinstance(required, list) and 0 < len(required) <= 30, 'deliverable_requirements_missing')
    ids = []
    for item in required:
        _need(isinstance(item, dict) and _text(item.get('id')), 'deliverable_requirement_invalid')
        ids.append(item['id'])
        _need(isinstance(item.get('mime_types'), list) and bool(item['mime_types'])
              and all(_text(v) for v in item['mime_types']), 'required_format_missing')
        _need(isinstance(item.get('facts'), dict), 'factual_constraints_missing')
        for key in ('min_bytes', 'max_bytes'):
            _need(type(item.get(key)) is int and item[key] > 0, 'size_constraints_missing')
        _need(item['min_bytes'] <= item['max_bytes'], 'size_constraints_invalid')
    _need(len(ids) == len(set(ids)), 'duplicate_deliverable_requirement')
    for key in ('semantic_checks', 'factual_checks'):
        _need(isinstance(brief.get(key), list) and bool(brief[key])
              and all(_text(v) for v in brief[key]) and len(set(brief[key])) == len(brief[key]),
              'qa_requirements_missing')


def _read_snapshot(adapter, work, state, now):
    snap = adapter.read_basecamp(copy.deepcopy(work), after_cursor=state['cursor'])
    _need(isinstance(snap, dict) and snap.get('binding') == _binding(work), 'basecamp_binding_mismatch')
    _need(_fresh(snap.get('observed_at'), now) and _text(snap.get('evidence_ref')),
          'basecamp_snapshot_stale_or_unverified')
    _need(snap.get('previous_cursor') == state['cursor'], 'basecamp_cursor_mismatch')
    _need(_text(snap.get('cursor')) and type(snap.get('has_more')) is bool, 'basecamp_cursor_invalid')
    _need(type(snap.get('completed')) is bool and _hash(snap.get('task_fingerprint')),
          'basecamp_task_snapshot_invalid')
    events = snap.get('events')
    _need(isinstance(events, list) and len(events) <= 100, 'basecamp_page_unbounded')
    seen = set()
    order = state['source_order']
    for e in events:
        _need(isinstance(e, dict) and _text(e.get('id')) and e['id'] not in seen
              and e.get('task_id') == work['task_id'] and type(e.get('source_order')) is int
              and e['source_order'] > order and _text(e.get('evidence_ref')), 'basecamp_event_invalid')
        seen.add(e['id'])
        order = e['source_order']
    _need(not events or snap['cursor'] != state['cursor'], 'basecamp_events_without_cursor_progress')
    state.update(cursor=snap['cursor'], source_order=order,
                 last_snapshot_ref=snap['evidence_ref'], last_observed_at=snap['observed_at'])
    return snap


def _context(adapter, work, snap, now):
    context = adapter.read_authority(copy.deepcopy(work))
    _need(isinstance(context, dict) and isinstance(context.get('request'), dict), 'authority_missing')
    scope = context.get('scope')
    digest = validate_scope(scope, now=now)
    _need(scope['brand_id'] == work['brand_id'], 'brand_scope_mismatch')
    services = [s for s in scope['services'] if s['service_id'] == work['service_id']]
    _need(len(services) == 1, 'service_out_of_scope')
    service = services[0]
    _need(work['account_id'] == scope['basecamp']['account_id']
          and work['project_id'] == scope['basecamp']['project_id']
          and work['list_id'] == service['list_id'], 'basecamp_scope_mismatch')
    _need(isinstance(snap.get('assignee_ids'), list) and bool(snap['assignee_ids'])
          and all(type(v) is int and v in service['assignee_ids'] for v in snap['assignee_ids']),
          'basecamp_assignees_not_verified')
    base = copy.deepcopy(context['request'])
    control = base.get('control', {})
    _need(control.get('work_id') == work['work_id'] and _fresh(control.get('observed_at'), now),
          'control_evidence_stale_or_mismatched')
    _need(control.get('global_mode') == 'routine_enabled' and control.get('work_mode') == 'active',
          'human_stop_or_takeover')
    source = base.get('source', {})
    _need(source.get('thread_id') == work['thread_id'] and _hash(source.get('fingerprint')),
          'source_binding_mismatch')
    base.update({k: work[k] for k in ('work_id', 'brand_id', 'service_id', 'revision_index')})
    return scope, service, digest, base


def _artifact(work, snap, *, allow_requirement_problems=False):
    """One immutable transport artifact may contain multiple inspected deliverables.

    Multi-file delivery needs a trusted adapter-created approved bundle. This
    avoids claiming atomic multi-attachment support from the singular V1 policy.
    """
    artifact = snap.get('artifact')
    _need(isinstance(artifact, dict), 'production_artifact_missing')
    _need(artifact.get('work_id') == work['work_id'] and artifact.get('brand_id') == work['brand_id']
          and _hash(artifact.get('sha256')) and _text(artifact.get('version'))
          and _text(artifact.get('mime_type')) and _text(artifact.get('evidence_ref')),
          'artifact_identity_unverified')
    items = artifact.get('deliverables')
    _need(isinstance(items, list) and len(items) <= 30, 'artifact_manifest_missing')
    by_id = {}
    for item in items:
        _need(isinstance(item, dict) and _text(item.get('id')) and item['id'] not in by_id
              and _hash(item.get('sha256')) and _text(item.get('evidence_ref'))
              and item.get('facts_bound_sha256') == item['sha256'], 'artifact_manifest_unverified')
        by_id[item['id']] = item
    requirements = work['brief']['required_deliverables']
    problems = []
    expected = {r['id'] for r in requirements}
    if allow_requirement_problems:
        problems += ['missing:' + name for name in sorted(expected - set(by_id))]
        problems += ['unexpected:' + name for name in sorted(set(by_id) - expected)]
    else:
        _need(set(by_id) == expected, 'required_deliverables_mismatch')
    for required in requirements:
        if required['id'] not in by_id:
            continue
        actual = by_id[required['id']]
        if actual.get('mime_type') not in required['mime_types']:
            problems.append(required['id'] + ':format')
        if type(actual.get('size_bytes')) is not int or not required['min_bytes'] <= actual['size_bytes'] <= required['max_bytes']:
            problems.append(required['id'] + ':size')
        if not isinstance(actual.get('facts'), dict) or any(actual['facts'].get(k) != v for k, v in required['facts'].items()):
            problems.append(required['id'] + ':facts')
    return artifact, problems


def _evidence_binding(work, artifact, digest, base):
    return {'brand_id': work['brand_id'], 'work_id': work['work_id'], 'version': artifact['version'],
            'sha256': artifact['sha256'], 'brief_sha256': scope_digest(work['brief']),
            'manifest_sha256': scope_digest(artifact['deliverables']), 'scope_digest': digest,
            'source_fingerprint': base['source']['fingerprint'],
            'recipients_sha256': scope_digest({'to': base.get('reply_to'), 'cc': base.get('reply_cc')})}


def _same_binding(record, binding, reason):
    _need(isinstance(record, dict) and all(record.get(k) == v for k, v in binding.items()), reason)


def _make_intent(work, snap, scope, base, *, action, body, artifact=None, qa=None, release=None,
                 stage, quality_context_sha256=None):
    request = copy.deepcopy(base)
    request.update(action=action, action_state='new')
    target = {k: work[k] for k in ('account_id', 'project_id', 'list_id', 'task_id')}
    if action == 'add_comment':
        _need(scope['basecamp']['customer_visible'] is False, 'internal_feedback_requires_private_project')
        payload = {'target': target, 'body': body}
    else:
        request.pop('quality_note_proof', None)
        _need(action == 'deliver_file' and _text(snap.get('customer_subject'))
              and _text(body), 'customer_envelope_missing')
        public_artifact = {k: artifact[k] for k in ('brand_id', 'work_id', 'version', 'sha256', 'mime_type')}
        payload = {'from': HAMLE, 'to': base.get('reply_to'), 'cc': base.get('reply_cc'),
                   'thread_id': work['thread_id'], 'subject': snap['customer_subject'],
                   'body': body, 'artifact': public_artifact}
        request.update(artifact=public_artifact, qa=qa, release=release)
    prepared = {'action': action, 'work_id': work['work_id'], 'brand_id': work['brand_id'],
                'service_id': work['service_id'], 'source_fingerprint': base['source']['fingerprint'],
                'payload_sha256': scope_digest(payload), 'task_id': work['task_id'],
                'task_fingerprint': snap['task_fingerprint']}
    request.update(action_payload=payload, prepared_action=prepared,
                   work_binding=_binding(work), basecamp_target={**target,
                       'customer_visible': scope['basecamp']['customer_visible'],
                       'verification_ref': snap['evidence_ref'], 'verified_at': snap['observed_at'],
                       'task_fingerprint': snap['task_fingerprint']})
    if action == 'deliver_file' and scope['services']:
        service = next(s for s in scope['services'] if s['service_id'] == work['service_id'])
        if service.get('delivery', {}).get('release_mode') == 'scored_quality_v1':
            request.update(quality_assessment=copy.deepcopy(snap.get('quality_assessment')),
                           quality_safety=copy.deepcopy(snap.get('quality_safety')),
                           quality_human_approval=copy.deepcopy(snap.get('quality_human_approval')),
                           quality_submission=copy.deepcopy(snap.get('producer_submission')),
                           quality_assets_ref=snap.get('quality_assets_ref'),
                           quality_brief_sha256=scope_digest(work['brief']),
                           quality_manifest_sha256=scope_digest(artifact['deliverables']))
    identity = {'work_id': work['work_id'], 'action': action, 'payload_sha256': prepared['payload_sha256'],
                'brief_sha256': scope_digest(work['brief']), 'scope_digest': scope_digest(scope),
                'source_fingerprint': base['source']['fingerprint'],
                'revision_index': work['revision_index'],
                'artifact_sha256': snap['artifact']['sha256'],
                'artifact_version': snap['artifact']['version'],
                'manifest_sha256': scope_digest(snap['artifact']['deliverables'])}
    if quality_context_sha256 is not None:
        _need(action == 'add_comment' and _hash(quality_context_sha256), 'quality_note_context_invalid')
        identity['quality_context_sha256'] = quality_context_sha256
    intent = {'schema_version': 1, **identity, 'action_id': 'delivery-' + scope_digest(identity),
              'kind': action, 'stage': stage, 'payload': payload, 'prepared_action': prepared,
              'authority_request': request, 'queue_source_kind': 'basecamp',
              'queue_event_id': 'delivery-' + scope_digest(identity),
              'requires_durable_queue_lease': True, 'live_enabled': False}
    return intent


def quality_feedback(result, *, artifact_version):
    """A reviewable internal note; its durable write is a separate intent."""
    reasons = {'critical_quality_failure': 'Zorunlu kalite koşulu sağlanmadı; düzeltme gerekli.',
               'artifact_specification_failure': 'Dosya veya teknik teslim koşulu sağlanmadı; düzeltme gerekli.',
               'score_below_human_review_band': 'Çalışma brief ile yeterince uyumlu değil; revizyon gerekli.',
               'score_requires_authorized_quality_approval': 'Müşteriye teslim için yetkili insan incelemesi bekleniyor.',
               'human_accepted_exact_assessment': 'Bu çalışma sürümü için yetkili insan kalite onayı alındı.',
               'score_meets_candidate_threshold': 'Kalite eşiği sağlandı; teslim öncesi diğer yetki kontrolleri uygulanacak.',
               'quality_checks_unassessed': 'Bazı kalite kontrolleri tamamlanamadı; inceleme bekleniyor.'}
    check_labels = {'brand_identity': 'marka kimliği', 'required_files': 'zorunlu dosyalar',
                    'technical_specs': 'teknik özellikler', 'rights_and_disclosure': 'haklar ve açıklamalar',
                    'internal_notes_excluded': 'iç yazışmaların teslimden çıkarılması'}
    def label(value):
        if value.startswith('missing:'):
            return 'eksik dosya: ' + value[8:]
        if value.startswith('unexpected:'):
            return 'brief dışında dosya: ' + value[11:]
        if ':' in value:
            name, condition = value.rsplit(':', 1)
            return name + ' — ' + {'format': 'dosya biçimi', 'size': 'dosya boyutu',
                                   'facts': 'teknik özellikler'}.get(condition, condition)
        return check_labels.get(value, next((row.get('description', value) for row in result.get('criteria', [])
                                            if row['criterion_id'] == value), 'zorunlu kalite kontrolü'))
    lines = ['Hamle iç kalite kontrolü — ' + artifact_version,
             'Brief uyum puanı: ' + (result.get('score') or 'hesaplanamadı') + '/100',
             'Sonuç: ' + reasons.get(result.get('reason'), 'İşlem durduruldu; kanıt veya yetki incelemesi gerekli.')]
    if result.get('critical_failures'):
        lines.append('Zorunlu düzeltmeler: ' + ', '.join(label(v) for v in result['critical_failures']))
        for findings in result.get('failure_details', {}).values():
            lines += ['Gözlem: ' + finding[:1000] for finding in findings[:3]]
    if result.get('unassessed_checks'):
        lines.append('Henüz değerlendirilemeyenler: ' + ', '.join(label(v) for v in result['unassessed_checks']))
    for row in result.get('criteria', []):
        lines.append(row.get('description', row['criterion_id']) + ': ' + row['score'] + '/100; ağırlık ' + row['weight']
                     + '; toplam puana katkı ' + row['contribution'])
        if row['score'] != '100':
            lines += ['Gözlem: ' + finding[:1000] for finding in row.get('findings', [])[:2]]
    lines.append('Bu puan, sürümlü ölçütlere uyumdur; doğruluk olasılığı değildir.')
    text = '\n'.join(lines)
    return text[:12000] + ('\nAyrıntı kalite değerlendirme kaydında.' if len(text) > 12000 else '')


def _scored_poll(state, result, work, snap, artifact, problems, scope, service, base, now):
    policy = service['delivery'].get('quality_policy')
    _need(isinstance(policy, dict), 'scored_quality_policy_missing')
    assessment = snap.get('quality_assessment')
    _need(snap.get('quality_assets_ref') == service['delivery'].get('approved_assets_ref'),
          'quality_assets_not_approved')
    intent = _make_intent(work, snap, scope, base, action='deliver_file',
                          body=snap.get('customer_body'), artifact=artifact, stage='ready_delivery')
    request = intent['authority_request']
    binding = build_quality_binding(request, brief_sha256=scope_digest(work['brief']),
                                     manifest_sha256=scope_digest(artifact['deliverables']),
                                     scope_sha256=scope_digest(scope))
    quality = evaluate_quality(policy, assessment, binding, snap.get('quality_safety'),
                               now=now, human_approval=snap.get('quality_human_approval'))
    # This reason is reached only after current safety and accepted policy pass.
    if assessment is None and not problems and quality['reason'] == 'assessment_unverified':
        state.update(status='ready_qa', reason='version_requires_scored_quality_evidence', pending_intent_id=None)
        return result
    # A trusted byte/format check overrides a generous semantic grade, or an
    # absent QA assessment after safety/policy validation. It never overrides
    # authority/security/unknown-write holds or stale/changed QA evidence.
    if problems and (quality['route'] != 'held'
                     or (assessment is None and quality['reason'] == 'assessment_unverified')):
        quality.update(route='revision', reason='artifact_specification_failure',
                       critical_failures=quality['critical_failures'] + problems)
    state['quality_result'] = copy.deepcopy(quality)
    result['quality_note'] = quality_feedback(quality, artifact_version=artifact['version'])
    route = quality['route']
    if route == 'held':
        state.update(status='held', reason=quality['reason'], pending_intent_id=None)
    elif route == 'needs_human_approval':
        state.update(status='awaiting_quality_approval', reason=quality['reason'], pending_intent_id=None)
    elif route == 'revision':
        # Internal QA attempts are distinct from customer revision rounds. A
        # per-work count advances only after exact provider readback confirms.
        limit = policy.get('internal_revision_limit')
        count = state.get('quality_revision_count', 0)
        intent = _make_intent(work, snap, scope, base, action='add_comment',
                              body=result['quality_note'], stage='changes_requested')
        intent['quality_revision'] = True
        prior = state['receipts'].get(intent['action_id'])
        _need(type(count) is int and count >= 0, 'quality_revision_counter_invalid')
        if not prior or prior['status'] != 'confirmed':
            _need(type(limit) is int and 0 < limit <= 10,
                  'internal_quality_revision_limit_missing_or_reached')
            if count >= limit:
                state.update(status='awaiting_human_takeover',
                    reason='internal_quality_revision_limit_missing_or_reached',
                    pending_intent_id=None, human_owners=list(policy['approval_actors']),
                    customer_revision_count_unchanged=True)
                return result
        result['intents'] = _allow_intent(state, intent, scope, now)
    elif route == 'delivery_candidate':
        delivery_intent = intent
        delivered = state['receipts'].get(delivery_intent['action_id'])
        if delivered:
            # An already delivered exact envelope never produces a new note or
            # second mail merely because somebody reran its quality assessment.
            result['intents'] = _allow_intent(state, delivery_intent, scope, now)
            return result
        # Producer identity/binding already passed in poll. QA must also follow
        # that submission before its positive note is published. The note gets
        # ordinary add_comment authority; final delivery additionally requires
        # this note's confirmed receipt at the shared authority gate.
        _need(_stamp(snap['producer_submission']['issued_at']) <= _stamp(assessment['checked_at']) <= now,
              'scored_assessment_precedes_submission')
        context = build_quality_note_context(delivery_intent['authority_request'], quality)
        context_digest = scope_digest(context)
        note_intent = _make_intent(work, snap, scope, base, action='add_comment',
                                   body=result['quality_note'], stage='quality_note_pending',
                                   quality_context_sha256=context_digest)
        note_intent['quality_note'] = True
        note_intent['quality_context'] = context
        note = state['receipts'].get(note_intent['action_id'])
        if not note:
            result['intents'] = _allow_intent(state, note_intent, scope, now)
            return result
        _need(note['status'] == 'confirmed' and note['payload_sha256'] == note_intent['payload_sha256']
              and note.get('quality_context_sha256') == context_digest,
              'quality_note_not_confirmed_for_current_delivery')
        _need(isinstance(note.get('receipt'), dict)
              and note['receipt'].get('provider_readback_verified') is True
              and note['receipt'].get('action_id') == note_intent['action_id']
              and note['receipt'].get('payload_sha256') == note_intent['payload_sha256']
              and note['receipt'].get('quality_context_sha256') == context_digest,
              'quality_note_exact_readback_missing')
        delivery_intent.update(required_quality_note_action_id=note_intent['action_id'],
                               required_quality_note_payload_sha256=note_intent['payload_sha256'],
                               quality_context_sha256=context_digest)
        delivery_intent['authority_request']['quality_note_proof'] = {
            'action_id': note_intent['action_id'], 'payload_sha256': note_intent['payload_sha256'],
            'quality_context_sha256': context_digest, 'quality_context': copy.deepcopy(context),
            'receipt': copy.deepcopy(note['receipt'])}
        result['intents'] = _allow_intent(state, delivery_intent, scope, now)
    else:
        raise DeliveryError('quality_route_unknown')
    return result


def _confirmed_status(intent):
    return ('delivered' if intent['kind'] == 'deliver_file' else
            'quality_noted' if intent.get('quality_note') is True else 'changes_requested')


def _allow_intent(state, intent, scope, now):
    prior = state['receipts'].get(intent['action_id'])
    if prior:
        _need(prior['payload_sha256'] == intent['payload_sha256'], 'action_identity_payload_mismatch')
        if prior['status'] == 'confirmed':
            state.update(status=_confirmed_status(intent),
                         reason='exact_provider_readback_already_confirmed', pending_intent_id=None)
            return []
        raise DeliveryError('delivery_result_unknown_no_retry')
    result = authorize_action(scope, intent['authority_request'], now=now)
    _need(result['allowed'], result['reason'])
    state.update(status=intent['stage'], reason='authorized_intent_requires_dispatch_preflight',
                 pending_intent_id=intent['action_id'])
    return [intent]


class DeliveryCoordinator:
    def poll(self, adapter: DeliveryAdapter, work: dict, state: dict | None, *, now):
        """Return a persistable state and zero or one intent, never perform a write."""
        now = _stamp(now)
        state = copy.deepcopy(state) if state is not None else new_state(work.get('work_id'))
        result = {'state': state, 'intents': [], 'network_writes': 0, 'model_calls': 0, 'live_enabled': False}
        try:
            _check_work(work)
            _need(state.get('work_id') == work['work_id'] and state.get('schema_version') == 1
                  and isinstance(state.get('receipts'), dict), 'state_work_mismatch')
            _need(not any(v.get('status') in ('unknown', 'started') for v in state['receipts'].values()),
                  'delivery_result_unknown_no_retry')
            snap = _read_snapshot(adapter, work, state, now)
            scope, service, digest, base = _context(adapter, work, snap, now)
            if snap['has_more']:
                raise DeliveryError('basecamp_poll_incomplete_continue_cursor')
            artifact = snap.get('artifact')
            submission = snap.get('producer_submission')
            if not artifact or not submission:
                state.update(status='awaiting_production', reason='explicit_producer_submission_required', pending_intent_id=None)
                return result
            scored = service.get('delivery', {}).get('release_mode') == 'scored_quality_v1'
            artifact, problems = _artifact(work, snap, allow_requirement_problems=scored)
            binding = _evidence_binding(work, artifact, digest, base)
            _same_binding(submission, binding, 'producer_submission_version_or_brief_changed')
            _need(submission.get('verified') is True and type(submission.get('actor_id')) is int
                  and submission.get('actor_id') in snap['assignee_ids']
                  and _text(submission.get('evidence_ref')) and _stamp(submission['issued_at']) <= now,
                  'producer_submission_unverified')
            state.update(artifact_sha256=artifact['sha256'], artifact_version=artifact['version'],
                         brief_sha256=binding['brief_sha256'], manifest_sha256=binding['manifest_sha256'])
            if scored:
                return _scored_poll(state, result, work, snap, artifact, problems, scope, service, base, now)
            qa = snap.get('qa')
            if not problems and qa is None:
                state.update(status='ready_qa', reason='version_requires_accepted_quality_evidence', pending_intent_id=None)
                return result
            if not problems:
                _same_binding(qa, binding, 'artifact_or_brief_changed_after_qa')
                policy = service.get('delivery', {})
                _need(qa.get('verified') is True and _text(qa.get('evidence_ref'))
                      and qa.get('policy_ref') == policy.get('accepted_qa_policy_ref')
                      and qa.get('assets_ref') == policy.get('approved_assets_ref')
                      and _stamp(submission['issued_at']) <= _stamp(qa.get('checked_at')) <= now,
                      'qa_provenance_or_policy_unverified')
                _need(isinstance(qa.get('semantic_checks'), dict) and isinstance(qa.get('factual_checks'), dict),
                      'qa_check_results_missing')
                for key in ('semantic_checks', 'factual_checks'):
                    for check in work['brief'][key]:
                        if qa[key].get(check) is not True:
                            problems.append(key + ':' + check)
                if qa.get('accepted') is not True or qa.get('internal_notes_excluded') is not True or qa.get('rights_checked') is not True:
                    problems.append('qa:not_accepted')
            if problems:
                body = 'Hamle iç kalite kontrolü — ' + artifact['version'] + '\nDüzeltilmesi gereken kontroller: ' + ', '.join(problems)
                intent = _make_intent(work, snap, scope, base, action='add_comment', body=body,
                                      stage='changes_requested')
                result['intents'] = _allow_intent(state, intent, scope, now)
                return result
            release = snap.get('release')
            if release is None:
                state.update(status='ready_delivery', reason='trusted_producer_release_required', pending_intent_id=None)
                return result
            _same_binding(release, binding, 'release_version_brief_or_destination_changed')
            _need(release.get('verified') is True, 'release_provenance_unverified')
            intent = _make_intent(work, snap, scope, base, action='deliver_file',
                                  body=snap.get('customer_body'), artifact=artifact, qa=qa, release=release,
                                  stage='ready_delivery')
            result['intents'] = _allow_intent(state, intent, scope, now)
            return result
        except (DeliveryError, KeyError, ValueError, TypeError, AttributeError) as exc:
            state.update(status='held', reason=str(exc) if isinstance(exc, (DeliveryError, ValueError)) else 'malformed_delivery_evidence',
                         pending_intent_id=None)
            return result

    def preflight_intent(self, adapter, work, state, intent, *, now):
        """Freshly rebuild the exact intent. No cached authority can authorize a write."""
        result = self.poll(adapter, work, state, now=now)
        candidates = result['intents']
        if len(candidates) == 1 and all(candidates[0].get(k) == intent.get(k)
                                        for k in ('action_id', 'payload_sha256', 'payload', 'prepared_action')):
            return {'allowed': True, 'intent': candidates[0], 'state': result['state'], 'live_enabled': False}
        return {'allowed': False, 'reason': result['state']['reason'] if not candidates else 'intent_changed_before_write',
                'state': result['state'], 'live_enabled': False}


def record_outcome(state, intent, *, status, receipt=None):
    """Persist only trusted queue/dispatcher results, never infer success from HTTP timeout.

    confirmed requires the canonical provider-readback payload hash to match the
    exact intended envelope (including artifact bytes, recipients and thread).
    started/unknown are terminal holds here; external reconciliation is explicit.
    """
    out = copy.deepcopy(state)
    _need(status in ('started', 'unknown', 'confirmed'), 'outcome_status_invalid')
    _need(out['work_id'] == intent['work_id'] and intent['payload_sha256'] == scope_digest(intent['payload']),
          'outcome_identity_mismatch')
    old = out['receipts'].get(intent['action_id'])
    if old and old.get('quality_context_sha256') is not None:
        _need(intent.get('quality_note') is True
              and old['quality_context_sha256'] == intent.get('quality_context_sha256'),
              'quality_note_context_changed_after_write_started')
    if old and old['status'] == 'confirmed':
        _need(status == 'confirmed' and receipt == old.get('receipt'), 'confirmed_outcome_immutable')
        return out
    if intent.get('required_quality_note_action_id') is not None:
        note = out['receipts'].get(intent['required_quality_note_action_id'], {})
        _need(note.get('status') == 'confirmed'
              and note.get('payload_sha256') == intent.get('required_quality_note_payload_sha256')
              and note.get('quality_context_sha256') == intent.get('quality_context_sha256')
              and isinstance(note.get('receipt'), dict)
              and note['receipt'].get('quality_context_sha256') == intent.get('quality_context_sha256'),
              'delivery_requires_confirmed_quality_note')
    if status == 'confirmed':
        _need(isinstance(receipt, dict) and receipt.get('provider_readback_verified') is True
              and receipt.get('payload_sha256') == intent['payload_sha256']
              and receipt.get('action_id') == intent['action_id'] and _text(receipt.get('evidence_ref')),
              'exact_provider_readback_required')
        if intent.get('quality_note') is True:
            _need(receipt.get('quality_context_sha256') == intent.get('quality_context_sha256'),
                  'quality_note_receipt_context_mismatch')
    out['receipts'][intent['action_id']] = {'status': status, 'payload_sha256': intent['payload_sha256'],
                                           'receipt': copy.deepcopy(receipt)}
    if intent.get('quality_note') is True:
        _need(intent['kind'] == 'add_comment' and _hash(intent.get('quality_context_sha256'))
              and scope_digest(intent.get('quality_context')) == intent['quality_context_sha256']
              and intent.get('quality_revision') is not True, 'quality_note_context_invalid')
        out['receipts'][intent['action_id']]['quality_context_sha256'] = intent['quality_context_sha256']
        if status == 'confirmed':
            out['last_confirmed_quality_note'] = {k: copy.deepcopy(intent[k]) for k in (
                'action_id', 'payload_sha256', 'quality_context_sha256', 'quality_context')}
            out['last_confirmed_quality_note']['receipt'] = copy.deepcopy(receipt)
    if status == 'confirmed' and intent.get('quality_revision') is True:
        count = out.get('quality_revision_count', 0)
        _need(type(count) is int and count >= 0, 'quality_revision_counter_invalid')
        out['quality_revision_count'] = count + 1
    if status == 'confirmed' and intent['kind'] == 'deliver_file':
        out['last_confirmed_delivery'] = {k: copy.deepcopy(intent[k]) for k in (
            'action_id', 'payload_sha256', 'artifact_sha256', 'artifact_version',
            'manifest_sha256', 'brief_sha256', 'scope_digest', 'source_fingerprint', 'revision_index')}
        out['last_confirmed_delivery']['receipt'] = copy.deepcopy(receipt)
    out.update(status=_confirmed_status(intent) if status == 'confirmed' else 'held',
               reason='exact_provider_readback_confirmed' if status == 'confirmed' else 'delivery_result_unknown_no_retry',
               pending_intent_id=None)
    return out
