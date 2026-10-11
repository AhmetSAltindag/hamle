"""Pure, JSON-persistable production progression. No provider/model calls.

Trusted adapters supply real task/submission/byte-inspection evidence. A flag
copied from mail, model output or a comment is not provider verification. This
module tracks production and delegates score decisions to v11_quality_gate;
it never grants customer-send authority.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from autonomy_policy import SHA256, scope_digest
from v11_quality_gate import evaluate_quality

PURPOSES = {'final', 'source', 'reference'}
KINDS = {'graphic', 'animation', 'video', 'text'}


class ProductionError(ValueError):
    pass


def _need(condition, reason):
    if not condition:
        raise ProductionError(reason)


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 2000


def _positive(value):
    return type(value) is int and value > 0


def _hash(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _stamp(value):
    result = datetime.fromisoformat(value)
    _need(result.tzinfo is not None and result.utcoffset() is not None, 'timezone_required')
    return result.astimezone(timezone.utc)


def _specs(brief):
    _need(isinstance(brief, dict) and brief.get('verified') is True
          and _positive(brief.get('version')) and _text(brief.get('evidence_ref')),
          'verified_versioned_brief_required')
    _need(all(_text(brief.get(k)) for k in ('brand_id', 'service_id')), 'brief_identity_missing')
    binding = brief.get('binding', {})
    _need(all(_positive(binding.get(k)) for k in ('account_id', 'project_id', 'list_id')),
          'basecamp_binding_invalid')
    _need(_text(brief.get('reply_envelope', {}).get('thread_id')), 'thread_binding_missing')
    overseers = binding.get('subscriber_ids')
    _need(isinstance(overseers, list) and bool(overseers)
          and all(_positive(v) for v in overseers), 'oversight_people_required')
    outputs = brief.get('deliverables')
    _need(isinstance(outputs, list) and 0 < len(outputs) <= 30, 'deliverables_required')
    seen = set()
    for output in outputs:
        _need(isinstance(output, dict) and _text(output.get('id')) and output['id'] not in seen
              and output.get('kind') in KINDS and output.get('purpose') in PURPOSES,
              'deliverable_identity_kind_purpose_invalid')
        seen.add(output['id'])
    stages = binding.get('production_stages')
    _need(isinstance(stages, list) and 0 < len(stages) <= 10, 'stage_plan_required')
    seen_stages, covered = set(), set()
    specs = []
    for stage in stages:
        _need(isinstance(stage, dict) and _text(stage.get('id')) and stage['id'] not in seen_stages,
              'stage_identity_invalid')
        deps, kinds, people = stage.get('depends_on'), stage.get('kinds'), stage.get('assignee_ids')
        _need(isinstance(deps, list) and len(set(deps)) == len(deps)
              and set(deps).issubset(seen_stages), 'stage_dependencies_invalid')
        _need(isinstance(kinds, list) and bool(kinds) and set(kinds).issubset(KINDS)
              and len(set(kinds)) == len(kinds), 'stage_kinds_invalid')
        _need(isinstance(people, list) and bool(people) and all(_positive(p) for p in people)
              and len(set(people)) == len(people), 'stage_producers_invalid')
        eligible = {o['id'] for o in outputs if o['kind'] in kinds}
        ids = stage.get('output_ids', [o['id'] for o in outputs if o['kind'] in kinds])
        _need(isinstance(ids, list) and bool(ids) and all(_text(v) for v in ids)
              and len(ids) == len(set(ids)) and set(ids).issubset(eligible),
              'stage_output_ids_mismatch')
        _need(not covered.intersection(ids), 'stage_output_coverage_invalid')
        specs.append({**copy.deepcopy(stage), 'output_ids': ids})
        covered.update(ids)
        seen_stages.add(stage['id'])
    _need(covered == seen, 'stage_output_coverage_incomplete')
    return specs


def initialize_production(work_id, brief, *, mode='production'):
    """Initialize from trusted intake; persist returned JSON under the work lease."""
    _need(_text(work_id) and mode in {'production', 'internal_pilot'}, 'work_or_mode_invalid')
    specs = _specs(brief)
    return {'schema_version': 1, 'work_id': work_id, 'mode': mode, 'revision': 0,
            'control': 'active', 'status': 'awaiting_production', 'brief': copy.deepcopy(brief),
            'brief_sha256': scope_digest(brief), 'stages': {
                s['id']: {'spec': s, 'status': 'waiting', 'task': None, 'submission': None,
                          'quality': None, 'acceptance': None, 'history': []} for s in specs},
            'questions': {}, 'events': {}, 'history': [], 'customer_send_allowed': False}


def ready_stages(state):
    """Stage plans eligible for task creation/start; never provider-write intents."""
    if state['control'] != 'active' or any(q['status'] == 'open' for q in state['questions'].values()):
        return []
    return [copy.deepcopy(stage['spec']) for stage in state['stages'].values()
            if stage['status'] == 'waiting' and all(
                state['stages'][dep]['status'] == 'accepted' for dep in stage['spec']['depends_on'])]


def _dependencies(state, stage):
    return {dep: state['stages'][dep]['acceptance']['acceptance_sha256']
            for dep in stage['spec']['depends_on']
            if state['stages'][dep]['status'] == 'accepted'}


def _descendants(state, stage_id):
    ids = {stage_id}
    for key, stage in state['stages'].items():
        if ids.intersection(stage['spec']['depends_on']):
            ids.add(key)
    return ids


def _invalidate(state, ids, *, reason, event):
    for key in ids:
        stage = state['stages'][key]
        stage['history'].append({'event_id': event['id'], 'reason': reason,
                                 'brief_sha256': state['brief_sha256'],
                                 'submission': copy.deepcopy(stage['submission']),
                                 'quality': copy.deepcopy(stage['quality']),
                                 'acceptance': copy.deepcopy(stage['acceptance'])})
        stage.update(status='waiting', submission=None, quality=None, acceptance=None)
        # Task identity remains historical/current. Starting again requires a
        # fresh verified brief acknowledgement on that exact provider task.
        stage.pop('started_brief_sha256', None)
        stage.pop('dependency_acceptances', None)


def _authority(state, event, *, producer_ids=()):
    people = state['brief']['binding']['subscriber_ids'] + list(producer_ids)
    _need(event.get('actor_id') in people, 'event_actor_not_authorized')


def _event(state, event):
    _need(isinstance(event, dict) and _text(event.get('id')) and _text(event.get('type')),
          'event_identity_invalid')
    _need(event.get('verified') is True and _text(event.get('evidence_ref'))
          and _positive(event.get('actor_id')), 'trusted_event_evidence_required')
    _stamp(event.get('occurred_at'))
    _need(event.get('work_id') == state['work_id'], 'event_work_mismatch')
    _need(type(event.get('expected_revision')) is int
          and event['expected_revision'] == state['revision'], 'state_revision_conflict')


def _task_event(state, stage, event):
    target = event.get('task', {})
    expected = state['brief']['binding']
    _need(isinstance(target, dict) and all(target.get(k) == expected[k]
          for k in ('account_id', 'project_id', 'list_id')) and _positive(target.get('task_id')),
          'stage_task_target_mismatch')
    _need(target.get('provider') == 'basecamp' and target.get('verified') is True
          and _text(target.get('evidence_ref')) and target.get('customer_visible') is False,
          'stage_task_readback_required')
    _need(target.get('assignee_ids') == stage['spec']['assignee_ids'], 'stage_task_assignees_mismatch')
    _need(target.get('brief_sha256') == state['brief_sha256'], 'stage_task_brief_mismatch')
    return target


def _submit(state, stage, event):
    _need(stage['status'] in {'running', 'changes_requested'}, 'stage_not_running')
    _need(event['actor_id'] in stage['spec']['assignee_ids'], 'submission_actor_not_assigned')
    submission = event.get('submission', {})
    _need(isinstance(submission, dict) and submission.get('verified') is True
          and _text(submission.get('evidence_ref')), 'producer_submission_required')
    _need(submission.get('producer_id') == event['actor_id']
          and submission.get('task_id') == stage['task']['task_id'], 'submission_producer_or_task_mismatch')
    _need(submission.get('origin') == 'producer_submission'
          or (state['mode'] == 'internal_pilot' and submission.get('origin') == 'internal_fixture'),
          'real_production_submission_required')
    _need(submission.get('brief_sha256') == state['brief_sha256']
          and stage.get('started_brief_sha256') == state['brief_sha256'], 'submission_brief_mismatch')
    _need(submission.get('dependency_acceptances') == _dependencies(state, stage)
          and submission['dependency_acceptances'] == stage.get('dependency_acceptances'),
          'submission_dependency_mismatch')
    _need(_stamp(submission.get('submitted_at')) <= _stamp(event['occurred_at']), 'submission_time_invalid')
    artifact = submission.get('artifact', {})
    _need(_text(artifact.get('version')) and _hash(artifact.get('sha256')), 'submission_artifact_missing')
    outputs = submission.get('outputs')
    _need(isinstance(outputs, list) and len(outputs) == len(stage['spec']['output_ids']),
          'stage_outputs_incomplete')
    by_id = {o.get('id'): o for o in outputs if isinstance(o, dict)}
    _need(len(by_id) == len(outputs) and set(by_id) == set(stage['spec']['output_ids']),
          'stage_outputs_mismatch')
    requirements = {o['id']: o for o in state['brief']['deliverables']}
    for key, output in by_id.items():
        required = requirements[key]
        _need(output.get('purpose') == required['purpose'] and output.get('kind') == required['kind'],
              'output_purpose_or_kind_mismatch')
        _need(_hash(output.get('sha256')) and _positive(output.get('size_bytes'))
              and _text(output.get('mime_type')) and _text(output.get('provider_attachment_id')),
              'output_file_identity_missing')
        inspection = output.get('inspection', {})
        _need(inspection.get('verified') is True and inspection.get('bytes_read') is True
              and inspection.get('sha256') == output['sha256']
              and inspection.get('size_bytes') == output['size_bytes']
              and _text(inspection.get('evidence_ref')) and inspection.get('readable') is True,
              'real_output_byte_inspection_required')
        _need(_stamp(submission['submitted_at']) <= _stamp(inspection.get('inspected_at'))
              <= _stamp(event['occurred_at']), 'output_inspection_time_invalid')
    _need(submission.get('manifest_sha256') == scope_digest(outputs), 'output_manifest_hash_mismatch')
    if stage['submission'] is not None:
        stage['history'].append({'event_id': event['id'], 'reason': 'resubmitted',
                                 'submission': copy.deepcopy(stage['submission']),
                                 'quality': copy.deepcopy(stage['quality'])})
    stage.update(submission=copy.deepcopy(submission), quality=None, acceptance=None,
                 status='awaiting_quality')


def _assess(state, stage, event):
    _authority(state, event)
    _need(stage['status'] in {'awaiting_quality', 'awaiting_quality_approval', 'quality_held'},
          'stage_submission_required_for_quality')
    submission = stage['submission']
    binding = event.get('binding', {})
    expected = {'work_id': state['work_id'], 'brand_id': state['brief']['brand_id'],
                'service_id': state['brief']['service_id'],
                'thread_id': state['brief']['reply_envelope']['thread_id'],
                'brief_sha256': state['brief_sha256'], 'manifest_sha256': submission['manifest_sha256'],
                'version': submission['artifact']['version'], 'sha256': submission['artifact']['sha256']}
    _need(all(binding.get(k) == v for k, v in expected.items()), 'stage_quality_binding_mismatch')
    _need(_stamp(submission['submitted_at']) <= _stamp(event.get('assessment', {}).get('checked_at')),
          'quality_predates_submission')
    result = evaluate_quality(event.get('policy'), event.get('assessment'), binding,
                              event.get('safety'), now=event['occurred_at'],
                              human_approval=event.get('human_approval'))
    stage['quality'] = copy.deepcopy(result)
    route = result['route']
    stage['status'] = {'delivery_candidate': 'accepted', 'needs_human_approval': 'awaiting_quality_approval',
                       'revision': 'changes_requested', 'held': 'quality_held'}[route]
    if route == 'delivery_candidate':
        acceptance = {'brief_sha256': state['brief_sha256'],
                      'submission_sha256': scope_digest(submission),
                      'manifest_sha256': submission['manifest_sha256'],
                      'quality_result_sha256': scope_digest(result),
                      'dependency_acceptances': _dependencies(state, stage),
                      'event_id': event['id'], 'accepted_at': event['occurred_at']}
        acceptance['acceptance_sha256'] = scope_digest(acceptance)
        stage['acceptance'] = acceptance


def _apply(state, event):
    kind = event['type']
    if kind in {'stop', 'resume'}:
        _authority(state, event)
        state['control'] = 'stopped' if kind == 'stop' else 'active'
        return
    _need(state['control'] == 'active', 'production_stopped')
    if kind == 'amend_brief':
        _authority(state, event)
        brief = event.get('brief')
        specs = _specs(brief)
        _need(brief['version'] == state['brief']['version'] + 1, 'amendment_version_invalid')
        _need({k: brief.get(k) for k in ('brand_id', 'service_id', 'reply_envelope')} ==
              {k: state['brief'].get(k) for k in ('brand_id', 'service_id', 'reply_envelope')},
              'amendment_identity_change_requires_replan')
        _need(brief['binding'] == state['brief']['binding'] and specs == [
            s['spec'] for s in state['stages'].values()], 'amendment_route_change_requires_replan')
        _need([(o['id'], o['kind'], o['purpose']) for o in brief['deliverables']] == [
            (o['id'], o['kind'], o['purpose']) for o in state['brief']['deliverables']],
            'amendment_output_change_requires_replan')
        resolved = event.get('resolves_question_ids', [])
        _need(isinstance(resolved, list) and all(q in state['questions'] for q in resolved),
              'resolved_question_unknown')
        _invalidate(state, set(state['stages']), reason='brief_amended', event=event)
        for q in resolved:
            state['questions'][q].update(status='resolved', resolved_by=event['id'])
        state['history'].append({'reason': 'old_brief', 'brief': copy.deepcopy(state['brief']),
                                 'event_id': event['id']})
        state['brief'] = copy.deepcopy(brief)
        state['brief_sha256'] = scope_digest(brief)
        return
    stage_id = event.get('stage_id')
    _need(stage_id in state['stages'], 'unknown_stage')
    stage = state['stages'][stage_id]
    if kind == 'raise_question':
        _authority(state, event, producer_ids=stage['spec']['assignee_ids'])
        qid = event.get('question_id')
        _need(_text(qid) and qid not in state['questions'] and _text(event.get('question_ref')),
              'question_identity_invalid')
        _need(stage['task'] is not None and event.get('task_id') == stage['task']['task_id'],
              'question_task_mismatch')
        state['questions'][qid] = {'status': 'open', 'stage_id': stage_id,
                                  'question_ref': event['question_ref'], 'event_id': event['id']}
        _invalidate(state, _descendants(state, stage_id), reason='producer_question', event=event)
        return
    _need(not any(q['status'] == 'open' for q in state['questions'].values()), 'unresolved_production_question')
    _need(len(_dependencies(state, stage)) == len(stage['spec']['depends_on']), 'stage_dependencies_not_accepted')
    if kind == 'bind_stage_task':
        _authority(state, event)
        target = _task_event(state, stage, event)
        _need(stage['status'] == 'waiting', 'stage_task_binding_after_start')
        _need(stage['task'] is None or stage['task']['task_id'] == target['task_id'], 'stage_task_rebinding_forbidden')
        _need(not any(s is not stage and s['task'] and s['task']['task_id'] == target['task_id']
                      for s in state['stages'].values()), 'task_already_bound_to_other_stage')
        stage['task'] = copy.deepcopy(target)
    elif kind == 'begin_stage':
        _authority(state, event, producer_ids=stage['spec']['assignee_ids'])
        _need(stage['status'] == 'waiting' and stage['task'] is not None, 'stage_not_ready_to_start')
        _need(stage['task']['brief_sha256'] == state['brief_sha256'], 'task_needs_current_brief_readback')
        stage.update(status='running', started_brief_sha256=state['brief_sha256'],
                     dependency_acceptances=_dependencies(state, stage))
    elif kind == 'submit_outputs':
        _submit(state, stage, event)
    elif kind == 'assess_stage':
        _assess(state, stage, event)
    else:
        raise ProductionError('unknown_event_type')


def advance_production(state, event):
    """Return new state; caller atomically persists it under its current work lease.

    Invalid events preserve the original state. An exact repeated event is a
    no-op even after newer events; changing a recorded event ID is a conflict.
    This transaction never emits network actions or implies dispatch authority.
    """
    result = {'state': copy.deepcopy(state), 'status': 'held', 'reason': None,
              'duplicate': False, 'intents': [], 'network_writes': 0, 'customer_send_allowed': False}
    try:
        _need(state.get('schema_version') == 1, 'unsupported_state_version')
        _need(isinstance(event, dict) and _text(event.get('id')), 'event_identity_invalid')
        fingerprint = scope_digest(event)
        previous = state['events'].get(event['id'])
        if previous is not None:
            _need(previous == fingerprint, 'event_identity_conflict')
            result.update(status='duplicate', reason='exact_event_already_recorded', duplicate=True)
            return result
        _event(state, event)
        changed = copy.deepcopy(state)
        _apply(changed, event)
        changed['revision'] += 1
        changed['events'][event['id']] = fingerprint
        changed['history'].append({'event_id': event['id'], 'event_type': event['type'],
                                   'evidence_ref': event['evidence_ref'], 'occurred_at': event['occurred_at']})
        if changed['control'] != 'active':
            changed['status'] = 'stopped'
        elif any(q['status'] == 'open' for q in changed['questions'].values()):
            changed['status'] = 'held_for_question'
        elif all(s['status'] == 'accepted' for s in changed['stages'].values()):
            changed['status'] = 'production_complete'
        else:
            changed['status'] = 'awaiting_production'
        result.update(state=changed, status='applied', reason='event_recorded')
    except (ProductionError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result['reason'] = str(exc) if isinstance(exc, ProductionError) else 'malformed_production_event'
    return result


def delivery_manifest(state):
    """Final-purpose files only. Not customer-send or final package QA authority."""
    _need(state['status'] == 'production_complete' and state['control'] == 'active'
          and not any(q['status'] == 'open' for q in state['questions'].values()),
          'production_not_complete')
    outputs = [copy.deepcopy(o) for s in state['stages'].values()
               for o in s['submission']['outputs'] if o['purpose'] == 'final']
    _need(bool(outputs), 'customer_final_output_missing')
    return {'work_id': state['work_id'], 'brief_sha256': state['brief_sha256'],
            'outputs': outputs, 'manifest_sha256': scope_digest(outputs),
            'stage_acceptances': {k: s['acceptance']['acceptance_sha256'] for k, s in state['stages'].items()},
            'mode': state['mode'], 'customer_send_allowed': False, 'requires_delivery_preflight': True}
