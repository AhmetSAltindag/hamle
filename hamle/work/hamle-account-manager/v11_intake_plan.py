"""Offline, versioned email → brief → internal task planning.

Every ``verified`` value here is a trusted adapter assertion, never text or a
model's claimed permission. This module does not fetch mail, inspect WhatsApp,
authenticate actors, persist state, send mail, or write to Basecamp. Its intents
are deliberately not dispatchable. A future dispatcher must use the durable
queue and fresh autonomy-policy checks immediately before each provider write.
In particular, seeing a recent message cannot prove that Hamle has not since
been removed from a conversation it cannot see.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from autonomy_policy import EMAIL, HAMLE, SHA256, scope_digest


class IntakeError(ValueError):
    pass


def _need(value, reason):
    if not value:
        raise IntakeError(reason)


def _text(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 16000


def _positive(value):
    return type(value) is int and value > 0


def _string_list(value):
    return isinstance(value, list) and all(_text(v) for v in value) and len(value) == len(set(value))


def _stamp(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    _need(stamp.tzinfo is not None, 'timestamp_timezone_required')
    return stamp.astimezone(timezone.utc)


def _contacts(catalog):
    _need(type(catalog.get('schema_version')) is int and catalog['schema_version'] == 1 and catalog.get('verified') is True
          and _text(catalog.get('version')) and _text(catalog.get('verification_ref'))
          and _text(catalog.get('brand_id')), 'catalog_unverified')
    rows = catalog.get('contacts')
    _need(isinstance(rows, list) and rows, 'contacts_missing')
    contacts, person_ids = {}, set()
    for row in rows:
        _need(isinstance(row, dict) and isinstance(row.get('email'), str)
              and EMAIL.fullmatch(row['email']) and row['email'] not in contacts
              and row.get('verified') is True
              and row.get('side') in ('customer', 'agency'), 'contact_unverified')
        for field in ('service_ids', 'production_kinds', 'agency_roles'):
            _need(_string_list(row.get(field, [])), 'contact_scope_list_invalid')
        for field in ('can_receive', 'can_request', 'can_relay', 'can_amend', 'basecamp_verified', 'project_member'):
            _need(field not in row or type(row[field]) is bool, 'contact_permission_invalid')
        if row.get('basecamp_verified') is True:
            _need(_positive(row.get('basecamp_id')) and row['basecamp_id'] not in person_ids,
                  'basecamp_person_identity_ambiguous')
            person_ids.add(row['basecamp_id'])
        contacts[row['email']] = row
    return contacts


def _routine(risk):
    _need(risk == 'routine', {'commercial': 'ahmet_commercial_decision_required',
                             'complaint_strategy': 'cansu_and_ahmet_decision_required',
                             'social_planning': 'ozge_collaboration_required'}.get(risk, 'risk_classification_missing'))


def _people(catalog, contacts):
    """Basecamp IDs suffice for production; no guessed producer email required."""
    raw = catalog.get('basecamp_people')
    if raw is None:
        rows = [p for p in contacts.values() if p.get('basecamp_verified') is True]
    else:
        _need(isinstance(raw, dict) and bool(raw), 'basecamp_people_missing')
        rows = []
        for key, person in raw.items():
            _need(isinstance(person, dict) and str(person.get('basecamp_id')) == key,
                  'basecamp_person_key_mismatch')
            rows.append(person)
    people = {}
    for person in rows:
        _need(person.get('basecamp_verified') is True and _positive(person.get('basecamp_id'))
              and person['basecamp_id'] not in people and person.get('side') == 'agency'
              and type(person.get('project_member')) is bool
              and _string_list(person.get('agency_roles', []))
              and _string_list(person.get('production_kinds', [])), 'basecamp_person_unverified')
        _need(_string_list(person.get('service_ids', [])), 'basecamp_person_scope_invalid')
        for field in ('can_amend', 'can_request', 'can_relay', 'can_receive'):
            _need(field not in person or type(person[field]) is bool, 'basecamp_person_permission_invalid')
        people[person['basecamp_id']] = person
    return people


def _email(source, contacts, now):
    _need(isinstance(source, dict) and source.get('channel') == 'email'
          and source.get('verified') is True and source.get('headers_origin') == 'provider'
          and source.get('latest_in_thread') is True
          and _text(source.get('evidence_ref')), 'latest_provider_email_required')
    for field in ('message_id', 'thread_id'):
        _need(_text(source.get(field)), 'email_identity_missing')
    _need(isinstance(source.get('fingerprint'), str)
          and SHA256.fullmatch(source['fingerprint']), 'email_fingerprint_missing')
    _need(0 <= (_stamp(now) - _stamp(source.get('observed_at'))).total_seconds() <= 60,
          'email_snapshot_stale')
    _need(source.get('automatic') is False and source.get('own_message') is False
          and source.get('sender') != HAMLE, 'own_or_automatic_message')
    _need(source.get('human_takeover') is False, 'human_takeover_blocks_planning')
    for field in ('to', 'cc'):
        values = source.get(field)
        _need(isinstance(values, list) and len(values) <= 100
              and all(isinstance(v, str) and EMAIL.fullmatch(v) for v in values)
              and len(values) == len(set(values)), 'recipient_headers_invalid')
    _need(HAMLE in source['to'] + source['cc'], 'hamle_not_actual_to_or_cc')
    actor = contacts.get(source.get('sender'))
    _need(actor is not None, 'sender_unverified')
    to = list(dict.fromkeys([source['sender']] + source['to']))
    to = [v for v in to if v != HAMLE]
    cc = [v for v in source['cc'] if v != HAMLE and v not in to]
    _need(bool(to) and all(v in contacts and contacts[v].get('can_receive') is True
                          for v in to + cc), 'reply_participant_unverified')
    return actor, {'thread_id': source['thread_id'], 'to': to, 'cc': cc,
                   'in_reply_to_message_id': source['message_id'],
                   'source_fingerprint': source['fingerprint']}


def _origin(source, actor, contacts, service_id):
    _need(actor.get('can_request') is True
          and service_id in actor.get('service_ids', []), 'sender_request_scope_missing')
    origin = source.get('origin')
    _need(isinstance(origin, dict), 'request_origin_missing')
    if origin.get('kind') == 'direct_email':
        return {'kind': 'direct_email', 'actor_email': source['sender'],
                'evidence_ref': source['evidence_ref']}
    _need(origin.get('kind') == 'agency_relay' and actor['side'] == 'agency'
          and actor.get('can_relay') is True, 'agency_relay_not_permitted')
    original = contacts.get(origin.get('original_actor_email'))
    _need(original is not None and original.get('side') == 'customer'
          and original.get('can_request') is True
          and service_id in original.get('service_ids', []), 'relay_original_requester_unverified')
    _need(origin.get('verified') is True and origin.get('attested_by') == source['sender']
          and origin.get('medium') in ('whatsapp', 'meeting', 'phone', 'email')
          and _text(origin.get('original_actor_name')) and _text(origin.get('evidence_ref')),
          'relay_attestation_missing')
    return copy.deepcopy(origin)


def _questions(requirements):
    """Explicit facts only: never infer animation, deadline, or all-month volume."""
    questions = []
    prompts = {'title': 'Bu işin kısa adı ne olsun?',
               'objective': 'Çalışmayla hangi mesajı veya sonucu hedefliyoruz?',
               'audience': 'Çalışma kime yönelik?',
               'deadline': 'Bu iş için beklenen teslim tarihi nedir?',
               'assets_ref': 'Kullanılacak güncel onaylı marka dosyaları hangileri?'}
    for key, prompt in prompts.items():
        if not _text(requirements.get(key)):
            questions.append({'field': key, 'question': prompt})
    items = requirements.get('deliverables')
    if not isinstance(items, list) or not items:
        questions.append({'field': 'deliverables',
                          'question': 'Hangi çıktıları istiyoruz: statik tasarım, animasyon, video veya birkaçı birlikte mi? Adetlerini de belirtir misiniz?'})
        return questions
    _need(len(items) <= 30, 'deliverable_count_unbounded')
    ids = set()
    for item in items:
        _need(isinstance(item, dict) and _text(item.get('id')) and item['id'] not in ids,
              'deliverable_identity_invalid')
        ids.add(item['id'])
        field = 'deliverables.' + item['id']
        if item.get('kind') not in ('graphic', 'animation', 'video', 'text'):
            questions.append({'field': field + '.kind', 'question': item['id'] + ': statik tasarım mı, animasyon mu, video mu bekleniyor?'})
        if not _positive(item.get('quantity')):
            questions.append({'field': field + '.quantity', 'question': item['id'] + ': kaç çıktı gerekiyor?'})
        if not isinstance(item.get('formats'), list) or not item['formats'] or not all(_text(v) for v in item['formats']):
            questions.append({'field': field + '.formats', 'question': item['id'] + ': hangi dosya formatlarında teslim edilecek?'})
        if not _text(item.get('specification')):
            questions.append({'field': field + '.specification', 'question': item['id'] + ': kullanılacağı kanal, boyut ve içerik beklentisi nedir?'})
        if item.get('kind') in ('animation', 'video') and not _positive(item.get('duration_seconds')):
            questions.append({'field': field + '.duration_seconds', 'question': item['id'] + ': hareketli çalışmanın süresi kaç saniye olacak?'})
    return questions


def _route(catalog, requirements, contacts):
    people = _people(catalog, contacts)
    bc = catalog.get('basecamp', {})
    _need(bc.get('verified') is True and _text(bc.get('verification_ref'))
          and all(_positive(bc.get(k)) for k in ('account_id', 'project_id'))
          and bc.get('customer_visible') is False, 'internal_basecamp_project_unverified')
    routes = [r for r in catalog.get('routes', [])
              if r.get('service_id') == requirements['service_id']
              and r.get('category_key') == requirements.get('category_key')]
    _need(len(routes) == 1, 'category_route_missing_or_ambiguous')
    route = routes[0]
    _need(route.get('verified') is True and _text(route.get('verification_ref'))
          and _positive(route.get('list_id')) and _text(route.get('category_name')),
          'category_route_unverified')
    kinds = {d['kind'] for d in requirements['deliverables']}
    _need(_string_list(route.get('allowed_kinds')) and kinds.issubset(set(route['allowed_kinds'])),
          'deliverable_outside_service_route')
    if 'required_any_kinds' in route:
        required = route['required_any_kinds']
        _need(_string_list(required) and bool(required)
              and set(required).issubset(set(route['allowed_kinds'])), 'route_required_kinds_invalid')
        _need(bool(kinds.intersection(required)), 'route_required_kind_missing')
    assignees = []
    by_kind = {}
    for kind in sorted(kinds):
        if 'assignee_ids_by_kind' in route:
            ids = route['assignee_ids_by_kind'].get(kind)
            _need(isinstance(ids, list) and bool(ids) and all(_positive(v) for v in ids)
                  and len(ids) == len(set(ids)), 'production_assignment_missing')
        else:
            emails = route.get('assignees_by_kind', {}).get(kind)
            _need(_string_list(emails) and emails, 'production_assignment_missing')
            ids = [contacts.get(email, {}).get('basecamp_id') for email in emails]
        for person_id in ids:
            person = people.get(person_id, {})
            _need(person.get('side') == 'agency' and person.get('basecamp_verified') is True
                  and person.get('project_member') is True and _positive(person.get('basecamp_id'))
                  and kind in person.get('production_kinds', []), 'production_assignment_unverified')
            assignees.append(person['basecamp_id'])
            by_kind.setdefault(kind, []).append(person['basecamp_id'])
    if 'production_sequences' in route:
        _need(isinstance(route['production_sequences'], dict), 'production_sequence_invalid')
        stage_specs = route['production_sequences'].get('+'.join(sorted(kinds)))
    else:
        stage_specs = route.get('production_stages')
    if len(kinds) > 1:
        _need(route.get('sequence_verified') is True and isinstance(stage_specs, list)
              and bool(stage_specs), 'production_sequence_unverified')
    else:
        stage_specs = [{'id': 'production', 'kinds': sorted(kinds), 'depends_on': []}]
    stages, stage_ids, covered = [], set(), set()
    for stage in stage_specs:
        _need(isinstance(stage, dict) and _text(stage.get('id')) and stage['id'] not in stage_ids
              and _string_list(stage.get('kinds')) and bool(stage['kinds'])
              and set(stage['kinds']).issubset(kinds) and not covered.intersection(stage['kinds'])
              and _string_list(stage.get('depends_on'))
              and set(stage['depends_on']).issubset(stage_ids), 'production_sequence_invalid')
        stages.append({**copy.deepcopy(stage), 'assignee_ids': sorted(
            {person_id for kind in stage['kinds'] for person_id in by_kind[kind]})})
        stage_ids.add(stage['id'])
        covered.update(stage['kinds'])
    _need(covered == kinds, 'production_sequence_incomplete')
    subscribers = []
    for role in ('social_manager', 'principal', 'strategy_owner'):
        matches = [p for p in people.values() if role in p.get('agency_roles', [])]
        _need(len(matches) == 1, 'internal_oversight_role_missing_or_ambiguous')
        person = matches[0]
        _need(person['side'] == 'agency' and person.get('basecamp_verified') is True
              and person.get('project_member') is True and _positive(person.get('basecamp_id')),
              'internal_subscriber_unverified')
        subscribers.append(person['basecamp_id'])
    return {'account_id': bc['account_id'], 'project_id': bc['project_id'],
            'list_id': route['list_id'], 'category_key': route['category_key'],
            'category_name': route['category_name'],
            'assignee_ids': sorted(set(assignees)), 'subscriber_ids': sorted(set(subscribers)),
            'production_stages': stages,
            'verification_ref': route['verification_ref']}


def _intent(kind, payload, brief_sha256=None):
    digest = scope_digest(payload)
    return {'kind': kind, 'intent_id': 'intake-' + digest, 'payload': payload,
            'payload_sha256': digest, 'brief_sha256': brief_sha256,
            'live_enabled': False, 'requires_dispatch_preflight': True}


def _event_digest(event):
    value = copy.deepcopy(event)
    if isinstance(value.get('source'), dict):
        value['source'].pop('observed_at', None)
    return scope_digest(value)


def intake_requirements_digest(requirements):
    """Exact facts fingerprint to bind trusted agreement to this proposed brief."""
    value = copy.deepcopy(requirements)
    value.pop('agreement', None)
    return scope_digest(value)


def amendment_proposal_digest(change):
    """Exact proposed amendment fingerprint for trusted risk classification."""
    value = copy.deepcopy(change)
    value.pop('classification', None)
    return _event_digest(value)


def plan_intake(source, requirements, catalog, *, now):
    result = {'status': 'held', 'reason': None, 'questions': [], 'brief': None,
              'intents': [], 'network_writes': 0, 'model_calls': 0}
    try:
        contacts = _contacts(catalog)
        actor, envelope = _email(source, contacts, now)
        _need(isinstance(requirements, dict) and requirements.get('brand_id') == catalog['brand_id'],
              'brief_brand_mismatch')
        _need(_text(requirements.get('service_id')), 'service_classification_missing')
        origin = _origin(source, actor, contacts, requirements['service_id'])
        _routine(requirements.get('risk_area'))
        questions = _questions(requirements)
        if questions:
            result.update(status='needs_clarification', reason='brief_incomplete', questions=questions,
                          intents=[_intent('clarify_brief', {**envelope, 'questions': questions})])
            return result
        agreement = requirements.get('agreement', {})
        _need(agreement.get('verified') is True and agreement.get('source_message_id') == source['message_id']
              and agreement.get('requirements_sha256') == intake_requirements_digest(requirements)
              and _text(agreement.get('evidence_ref')), 'brief_agreement_unverified')
        binding = _route(catalog, requirements, contacts)
        brief = {k: copy.deepcopy(requirements[k]) for k in
                 ('brand_id', 'service_id', 'title', 'objective', 'audience', 'deadline', 'assets_ref', 'deliverables')}
        # Requested outputs are final deliverables unless explicitly marked
        # source/reference; production stages need this purpose explicitly.
        for item in brief['deliverables']:
            item.setdefault('purpose', 'final')
            _need(item['purpose'] in ('final', 'source', 'reference'), 'deliverable_purpose_invalid')
        brief.update(schema_version=1, version=1, verified=True, evidence_ref=agreement['evidence_ref'],
                     origin=origin, source_message_id=source['message_id'],
                     source_fingerprint=source['fingerprint'], reply_envelope=envelope,
                     catalog_version=catalog['version'], catalog_sha256=scope_digest(catalog),
                     binding=binding, change_history=[], processed_change_ids=[],
                     processed_change_hashes={}, unresolved_questions=[])
        digest = scope_digest(brief)
        initial_ids = sorted({i for stage in binding['production_stages'] if not stage['depends_on']
                              for i in stage['assignee_ids']})
        payload = {'target': {k: binding[k] for k in ('account_id', 'project_id', 'list_id')},
                   'category': binding['category_name'], 'assignee_ids': initial_ids,
                   'production_stages': binding['production_stages'],
                   'subscriber_ids': binding['subscriber_ids'], 'brief': brief,
                   'source_thread_id': source['thread_id']}
        result.update(status='ready_internal_task', reason='verified_brief_and_route', brief=brief,
                      intents=[_intent('create_task', payload, digest)])
    except (IntakeError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result['reason'] = str(exc) if isinstance(exc, IntakeError) else 'malformed_intake_evidence'
    return result


def _work(work, catalog):
    _need(isinstance(work, dict) and _text(work.get('work_id')) and _positive(work.get('task_id')),
          'bound_work_missing')
    brief = work.get('brief', {})
    _need(brief.get('verified') is True and _positive(brief.get('version'))
          and brief.get('catalog_sha256') == scope_digest(catalog), 'work_catalog_changed_or_unverified')
    return copy.deepcopy(work), copy.deepcopy(brief), _contacts(catalog)


def _basecamp_actor(source, work, brief, contacts, catalog):
    binding = brief['binding']
    actor = _people(catalog, contacts).get(source.get('actor_id'), {})
    _need(bool(actor), 'basecamp_actor_unverified')
    mail_matches = [p for p in contacts.values() if p.get('basecamp_id') == source.get('actor_id')]
    if mail_matches:
        _need(len(mail_matches) == 1, 'basecamp_person_identity_ambiguous')
        actor = {**actor, **mail_matches[0]}
    _need(source.get('channel') == 'basecamp' and source.get('verified') is True
          and _text(source.get('evidence_ref')) and actor.get('side') == 'agency'
          and actor.get('basecamp_verified') is True and actor.get('project_member') is True
          and _positive(source.get('actor_id')) and source['actor_id'] == actor.get('basecamp_id'),
          'basecamp_actor_unverified')
    if source.get('actor_email') is not None:
        _need(source['actor_email'] == actor.get('email'), 'basecamp_actor_email_mismatch')
    _need(all(_positive(source.get(k)) and source[k] == binding[k]
              for k in ('account_id', 'project_id', 'list_id'))
          and _positive(source.get('task_id')) and source['task_id'] == work['task_id'],
          'basecamp_event_work_mismatch')
    return actor


def _invalidate_delivery(work, brief, reason):
    invalidated = {k: work.pop(k) for k in ('qa', 'release', 'pending_delivery') if k in work}
    work.setdefault('invalidated_delivery_evidence', []).append(
        {'reason': reason, 'brief_version': brief['version'], 'evidence': invalidated})
    work['delivery_ready'] = False


def amend_brief(work, change, catalog, *, now):
    """Apply one authenticated agency amendment with compare-and-swap version.

    The caller must persist this entire result atomically with queue cancellation.
    Already started/uncertain writes remain quarantined; cancellation cannot undo
    a provider operation already accepted. Removed QA stays in evidence history.
    """
    updated, brief, contacts = _work(work, catalog)
    _need(isinstance(change, dict) and _text(change.get('id')), 'amendment_identity_missing')
    if change['id'] in brief['processed_change_ids']:
        _need(brief.get('processed_change_hashes', {}).get(change['id']) == _event_digest(change),
              'amendment_identity_payload_conflict')
        return {'work': updated, 'duplicate': True, 'intents': [], 'network_writes': 0}
    _need(type(change.get('expected_brief_version')) is int
          and change['expected_brief_version'] == brief['version'], 'brief_version_conflict')
    source = change.get('source', {})
    if source.get('channel') == 'basecamp':
        actor = _basecamp_actor(source, work, brief, contacts, catalog)
        _need(source.get('human_takeover') is False, 'human_takeover_blocks_planning')
        _need(0 <= (_stamp(now) - _stamp(source.get('observed_at'))).total_seconds() <= 60,
              'basecamp_snapshot_stale')
        envelope = copy.deepcopy(brief['reply_envelope'])
        actor_email = actor.get('email')
    else:
        actor, envelope = _email(source, contacts, now)
        _need(envelope['thread_id'] == brief['reply_envelope']['thread_id'], 'amendment_thread_mismatch')
        actor_email = source['sender']
    _need(actor.get('side') == 'agency' and actor.get('can_amend') is True
          and brief['service_id'] in actor.get('service_ids', []), 'brief_amendment_actor_not_permitted')
    classification = change.get('classification', {})
    _need(classification.get('verified') is True and classification.get('change_id') == change['id']
          and classification.get('proposal_sha256') == amendment_proposal_digest(change)
          and _text(classification.get('evidence_ref')), 'amendment_risk_classification_unverified')
    _routine(classification.get('risk_area'))
    _need(_text(change.get('reason')), 'amendment_reason_missing')
    patch = change.get('patch')
    fields = {'title', 'objective', 'audience', 'deadline', 'assets_ref', 'deliverables'}
    _need(isinstance(patch, dict) and bool(patch) and set(patch).issubset(fields), 'amendment_field_not_permitted')
    before = scope_digest(brief)
    brief.update(copy.deepcopy(patch))
    questions = _questions(brief)
    _need(not questions, 'amendment_requires_clarification')
    requirements = {**brief, 'category_key': brief['binding']['category_key']}
    binding = _route(catalog, requirements, contacts)
    _need(binding == brief['binding'], 'amendment_requires_route_or_assignment_review')
    resolves = change.get('resolves_question_ids', [])
    _need(isinstance(resolves, list) and len(resolves) == len(set(resolves))
          and set(resolves).issubset({q['id'] for q in brief['unresolved_questions']}), 'question_resolution_unbound')
    brief['unresolved_questions'] = [q for q in brief['unresolved_questions'] if q['id'] not in resolves]
    brief['version'] += 1
    brief['reply_envelope'] = envelope
    if source['channel'] == 'email':
        brief['source_message_id'] = source['message_id']
        brief['source_fingerprint'] = source['fingerprint']
    brief['evidence_ref'] = source['evidence_ref']
    brief['processed_change_ids'].append(change['id'])
    brief.setdefault('processed_change_hashes', {})[change['id']] = _event_digest(change)
    brief['change_history'].append({'id': change['id'], 'actor': actor_email,
                                    'reason': change['reason'], 'previous_sha256': before,
                                    'fields': sorted(patch), 'source_ref': source['evidence_ref'],
                                    'source_channel': source['channel'],
                                    'version': brief['version'], 'resolved_question_ids': resolves})
    _invalidate_delivery(updated, brief, 'brief_amended')
    updated.update(brief=brief, status=('awaiting_internal_clarification' if brief['unresolved_questions']
                                      else 'awaiting_production'), delivery_ready=False)
    payload = {'target': {**{k: binding[k] for k in ('account_id', 'project_id', 'list_id')},
                          'task_id': work['task_id']}, 'brief': brief, 'change_reason': change['reason']}
    return {'work': updated, 'duplicate': False,
            'intents': [_intent('update_task', payload, scope_digest(brief))], 'network_writes': 0,
            'requires_atomic_queue_invalidation': True}


def record_producer_question(work, question, catalog):
    """Record a question already present in authenticated Basecamp; do not echo it."""
    updated, brief, contacts = _work(work, catalog)
    _need(isinstance(question, dict) and _text(question.get('id'))
          and _text(question.get('text')), 'producer_question_missing')
    source = question.get('source', {})
    binding = brief['binding']
    _basecamp_actor(source, work, brief, contacts, catalog)
    _need(source['actor_id'] in binding['assignee_ids'], 'producer_question_actor_unverified')
    seen = updated.setdefault('processed_question_ids', [])
    if question['id'] in seen:
        _need(updated.get('processed_question_hashes', {}).get(question['id']) == _event_digest(question),
              'question_identity_payload_conflict')
        return {'work': updated, 'duplicate': True, 'intents': [], 'network_writes': 0}
    brief['unresolved_questions'].append(copy.deepcopy(question))
    seen.append(question['id'])
    updated.setdefault('processed_question_hashes', {})[question['id']] = _event_digest(question)
    _invalidate_delivery(updated, brief, 'unresolved_producer_question')
    status = updated.get('status') if updated.get('status') in ('human_takeover', 'stopped') else 'awaiting_internal_clarification'
    updated.update(brief=brief, status=status, delivery_ready=False)
    return {'work': updated, 'duplicate': False, 'intents': [], 'network_writes': 0,
            'needs_response_from_basecamp_ids': binding['subscriber_ids'],
            'requires_atomic_queue_invalidation': True}
