"""Pure work-scoped visible-stop authority; disabled without explicit acceptance.

No I/O or live grants. Trusted authenticated UI/provider/control adapters must
supply every evidence object; JSON copied from email/model output is not proof.
This handles the accepted revocation protocol, NOT detection of unseen CC loss.
It never replaces autonomy_policy, QA, the write ledger, or budget reservation.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from autonomy_policy import ACTIONS, HAMLE, SHA256, scope_digest

POLICY_KIND = 'visible_stop_work_policy_v1'
GRANT_KIND = 'visible_stop_work_grant_v1'
ISSUERS = frozenset(('ahmet@gamechangers.com.tr', 'cansu@gamechangers.com.tr', 'ozge@gamechangers.com.tr'))
BINDING_FIELDS = ('work_id', 'brand_id', 'service_id', 'thread_id', 'source_message_id',
                  'source_fingerprint', 'source_received_at', 'scope_digest', 'brief_sha256',
                  'recipients_sha256', 'global_generation', 'work_generation')
IMMUTABLE_GRANT_FIELDS = ('kind', 'binding', 'policy_digest', 'actions', 'authorized_issuers',
    'actor', 'issuer_kind', 'derivation', 'issuer_evidence_ref', 'issued_at', 'expires_at',
    'business_expires_at', 'delegation_digest', 'budget_policy_id', 'budget_deadline', 'rebindings')
FOLLOWUP_CHANGE_FIELDS = frozenset(('source_message_id', 'source_fingerprint', 'source_received_at'))


class WorkAuthorityError(ValueError):
    pass


@dataclass(frozen=True)
class TrustedIntakeEvidence:
    """Capability-shaped input constructed by the trusted provider adapter ONLY.

    A JSON/model dictionary cannot stand in for this type. Construction is not
    cryptographic authentication: the adapter still owns Gmail identity, actual
    header readback and independently accepted classification verification.
    """
    adapter_id: str
    provider_identity: str
    evidence_ref: str
    classification_evidence_ref: str
    binding_sha256: str
    parent_policy_digest: str
    observed_at: str
    intake_kind: str = 'new_work'
    source_kind: str = 'authenticated_gmail_api'
    classification: str = 'routine'
    model_supplied_authority: bool = False


def policy_terms_digest(policy):
    value = copy.deepcopy(policy)
    value.pop('acceptance', None)
    return scope_digest(value)


def _need(condition, reason):
    if not condition:
        raise WorkAuthorityError(reason)


def _text(value):
    return isinstance(value, str) and 0 < len(value) <= 2000


def _hash(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _stamp(value):
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    _need(moment.tzinfo is not None and moment.utcoffset() is not None, 'work_authority_timezone_required')
    return moment.astimezone(timezone.utc)


def _fresh(now, value, maximum=300):
    return 0 <= (_stamp(now) - _stamp(value)).total_seconds() <= maximum


def business_day_expiry(issued_at, days=2):
    """Add weekday dates, preserving Istanbul local clock time; no holiday guess.

    Friday +2 = Tuesday; weekend issuance +2 = Tuesday. This is a calendar-date
    duration, not 48 staffed hours. The dispatcher separately enforces work hours.
    """
    _need(type(days) is int and days == 2, 'work_authority_business_day_limit_invalid')
    moment = _stamp(issued_at).astimezone(ZoneInfo('Europe/Istanbul'))
    count = 0
    while count < days:
        moment += timedelta(days=1)
        if moment.weekday() < 5:
            count += 1
    return moment.isoformat()


def default_work_authority_policy(brand_id, service_id):
    """A template is never accepted or enabled automatically."""
    return {'schema_version': 1, 'kind': POLICY_KIND, 'enabled': False, 'status': 'draft',
            'brand_id': brand_id, 'service_id': service_id, 'max_business_days': 2,
            'timezone': 'Europe/Istanbul', 'calendar': 'weekends_only',
            'observation_max_age_seconds': 300, 'visible_stop_required': True,
            'unseen_cc_removal_detectable': False, 'unseen_cc_removal_is_revocation': False,
            'allowed_issuers': sorted(ISSUERS), 'actions': [], 'acceptance': None}


def validate_work_authority_policy(policy, scope, service, *, now):
    _need(isinstance(policy, dict) and policy.get('kind') == POLICY_KIND
          and policy.get('schema_version') == 1, 'work_authority_policy_invalid')
    _need(policy.get('enabled') is True and policy.get('status') == 'accepted',
          'work_authority_policy_not_enabled')
    _need(policy.get('brand_id') == scope.get('brand_id') and policy.get('service_id') == service.get('service_id'),
          'work_authority_policy_scope_mismatch')
    _need(policy.get('max_business_days') == 2 and type(policy['max_business_days']) is int
          and policy.get('timezone') == 'Europe/Istanbul' and policy.get('calendar') == 'weekends_only'
          and type(policy.get('observation_max_age_seconds')) is int
          and 1 <= policy['observation_max_age_seconds'] <= 300,
          'work_authority_policy_limits_invalid')
    _need(policy.get('visible_stop_required') is True
          and policy.get('unseen_cc_removal_detectable') is False
          and policy.get('unseen_cc_removal_is_revocation') is False,
          'work_authority_visible_stop_acceptance_missing')
    people = policy.get('allowed_issuers')
    _need(isinstance(people, list) and bool(people) and len(people) == len(set(people))
          and set(people).issubset(ISSUERS) and set(people).issubset(scope['agency_contacts']),
          'work_authority_issuer_catalog_invalid')
    actions = policy.get('actions')
    _need(isinstance(actions, list) and bool(actions) and len(actions) == len(set(actions))
          and set(actions).issubset(ACTIONS) and set(actions).issubset(service['actions']),
          'work_authority_actions_invalid')
    accepted = policy.get('acceptance')
    _need(isinstance(accepted, dict) and accepted.get('verified') is True
          and accepted.get('authenticated') is True and accepted.get('actor') in people
          and accepted.get('decision') == 'accept_visible_stop_work_authority'
          and accepted.get('policy_terms_sha256') == policy_terms_digest(policy)
          and _text(accepted.get('evidence_ref'))
          and _stamp(accepted.get('accepted_at')) <= _stamp(now) < _stamp(accepted.get('expires_at')),
          'work_authority_explicit_acceptance_missing')
    return scope_digest(policy)


def build_work_authority_binding(scope, request):
    source, control = request['source'], request['control']
    to = list(dict.fromkeys([source['sender']] + source['to']))
    to = [v for v in to if v != HAMLE]
    cc = [v for v in source['cc'] if v != HAMLE and v not in to]
    brief = request.get('work_brief_sha256')
    _need(_hash(brief), 'work_authority_current_brief_missing')
    if request.get('quality_brief_sha256') is not None:
        _need(request['quality_brief_sha256'] == brief, 'work_authority_quality_brief_mismatch')
    binding = {'work_id': request['work_id'], 'brand_id': request['brand_id'],
               'service_id': request['service_id'], 'thread_id': source['thread_id'],
               'source_message_id': source['message_id'], 'source_fingerprint': source['fingerprint'],
               'source_received_at': _stamp(source['received_at']).isoformat(),
               'scope_digest': scope_digest(scope), 'brief_sha256': brief,
               'recipients_sha256': scope_digest({'to': to, 'cc': cc}),
               'global_generation': control['global_generation'], 'work_generation': control['work_generation']}
    _need(all(_text(binding[k]) for k in ('work_id', 'brand_id', 'service_id', 'thread_id', 'source_message_id'))
          and all(_hash(binding[k]) for k in ('source_fingerprint', 'scope_digest', 'brief_sha256', 'recipients_sha256'))
          and all(type(binding[k]) is int and binding[k] >= 0 for k in ('global_generation', 'work_generation')),
          'work_authority_binding_invalid')
    return binding


def _context(policy, scope, request, budget, now):
    services = [s for s in scope['services'] if s['service_id'] == request['service_id']]
    _need(len(services) == 1 and request['brand_id'] == scope['brand_id'] and scope.get('enabled') is True,
          'work_authority_scope_invalid')
    service = services[0]
    _need(service.get('work_authority_policy') == policy, 'work_authority_policy_not_in_scope')
    pdigest = validate_work_authority_policy(policy, scope, service, now=now)
    _need(scope['authority'].get('verified_by') in scope['agency_contacts']
          and _text(scope['authority'].get('evidence_ref'))
          and _stamp(scope['authority']['verified_at']) <= _stamp(now) < _stamp(scope['authority']['expires_at']),
          'work_authority_scope_expired_or_unverified')
    source, control, delegation = request['source'], request['control'], request['delegation']
    limit = policy['observation_max_age_seconds']
    _need(request.get('risk_area') == 'routine' and request.get('open_escalation') is False,
          'work_authority_routine_scope_required')
    _need(request.get('action_state') == 'new', 'work_authority_write_not_new')
    _need(control.get('work_id') == request['work_id'] and control.get('global_mode') == 'routine_enabled'
          and control.get('work_mode') == 'active' and _fresh(now, control.get('observed_at'), limit),
          'work_authority_stopped_or_control_stale')
    _need(source.get('actual_latest') is True and source.get('own_or_automatic') is False
          and HAMLE in source.get('to', []) + source.get('cc', [])
          and source.get('sender') in scope['customer_contacts'] + scope['agency_contacts']
          and _text(source.get('provider_evidence_ref')) and _fresh(now, source.get('captured_at'), limit)
          and _stamp(source['received_at']) <= _stamp(source['captured_at']),
          'work_authority_source_unverified_or_stale')
    known = set(scope['customer_contacts'] + scope['agency_contacts'] + scope['allowed_cc_contacts'] + [HAMLE])
    _need(set(source['to'] + source['cc']).issubset(known), 'work_authority_recipient_out_of_scope')
    binding = build_work_authority_binding(scope, request)
    _need(delegation.get('revoked') is False and delegation.get('actor') in scope['agency_contacts']
          and _text(delegation.get('evidence_ref'))
          and all(delegation.get(k) == binding[k] for k in
                  ('work_id', 'brand_id', 'service_id', 'thread_id', 'scope_digest', 'global_generation', 'work_generation'))
          and _stamp(delegation['issued_at']) <= _stamp(now) < _stamp(delegation['expires_at'])
          and set(policy['actions']).issubset(set(delegation.get('actions', []))),
          'work_authority_delegation_invalid')
    _need(isinstance(budget, dict) and budget.get('verified') is True and budget.get('allowed') is True
          and budget.get('overrun_hold') is False and _text(budget.get('policy_id'))
          and _text(budget.get('evidence_ref')) and _fresh(now, budget.get('observed_at'), limit)
          and type(budget.get('remaining_microdollars')) is int and budget['remaining_microdollars'] > 0
          and _stamp(now) < _stamp(budget.get('expires_at')), 'work_authority_budget_unavailable')
    return binding, pdigest


def _issuer(issuer, policy, binding, pdigest, now, decision):
    _need(isinstance(issuer, dict) and issuer.get('verified') is True and issuer.get('authenticated') is True
          and issuer.get('actor') in policy['allowed_issuers'] and issuer.get('actor') in ISSUERS
          and issuer.get('decision') == decision and _text(issuer.get('evidence_ref'))
          and issuer.get('binding_sha256') == scope_digest(binding) and issuer.get('policy_digest') == pdigest
          and _fresh(now, issuer.get('issued_at')), 'work_authority_authenticated_issuer_required')


def grant_work_authority(policy, scope, request, issuer, budget, *, now):
    """Issue only from a current authenticated explicit grant decision.

    A provider read cannot mint/renew this grant. Callers must persist issuance
    under the work transaction and authenticate issuer evidence outside this pure
    module. No automatically accepted real policy is supplied by this module.
    """
    binding, pdigest = _context(policy, scope, request, budget, now)
    _issuer(issuer, policy, binding, pdigest, now, 'grant_visible_stop_work_authority')
    issued = _stamp(issuer['issued_at'])
    deadline = business_day_expiry(issued)
    expiry = min(_stamp(deadline), _stamp(budget['expires_at']), _stamp(scope['authority']['expires_at']),
                 _stamp(policy['acceptance']['expires_at']), _stamp(request['delegation']['expires_at']))
    _need(_stamp(now) < expiry, 'work_authority_expired_at_issue')
    return _make_grant(policy, binding, pdigest, request, budget, issued, expiry, deadline,
                       actor=issuer['actor'], evidence_ref=issuer['evidence_ref'],
                       issuer_kind='authenticated_human_decision', derivation=None)


def _make_grant(policy, binding, pdigest, request, budget, issued, expiry, deadline, *,
                actor, evidence_ref, issuer_kind, derivation):
    immutable = {'kind': GRANT_KIND, 'binding': binding, 'policy_digest': pdigest,
                 'actions': sorted(policy['actions']), 'authorized_issuers': sorted(policy['allowed_issuers']),
                 'actor': actor, 'issuer_kind': issuer_kind, 'derivation': copy.deepcopy(derivation),
                 'issuer_evidence_ref': evidence_ref, 'issued_at': issued.isoformat(),
                 'expires_at': expiry.isoformat(), 'business_expires_at': deadline,
                 'delegation_digest': scope_digest(request['delegation']),
                 'rebindings': [],
                 'budget_policy_id': budget['policy_id'], 'budget_deadline': _stamp(budget['expires_at']).isoformat()}
    return {'schema_version': 1, 'grant_id': 'work-grant-' + scope_digest(immutable),
            **immutable, 'status': 'active', 'revision': 0, 'revocations': [],
            'live_enabled_by_this_module': False, 'unseen_cc_removal_detected': False}


def derive_work_authority_from_intake(policy, scope, request, intake, budget, *, now):
    """Derive a new-work grant from a previously human-accepted brand policy.

    This does NOT claim a new human decision for each job. Only a trusted typed
    intake receipt is accepted; raw model/mail/comment dictionaries are refused.
    The duration starts at original mail arrival, never latest read or execution.
    Historical mail predating policy acceptance cannot be activated this way.
    """
    binding, pdigest = _context(policy, scope, request, budget, now)
    _need(type(intake) is TrustedIntakeEvidence
          and intake.adapter_id == 'hamle-authenticated-intake-v1'
          and intake.provider_identity == HAMLE
          and intake.source_kind == 'authenticated_gmail_api' and intake.intake_kind == 'new_work'
          and intake.classification == 'routine' and intake.model_supplied_authority is False
          and _text(intake.evidence_ref) and _text(intake.classification_evidence_ref)
          and intake.binding_sha256 == scope_digest(binding) and intake.parent_policy_digest == pdigest
          and _fresh(now, intake.observed_at, policy['observation_max_age_seconds']),
          'work_authority_trusted_new_intake_required')
    issued = _stamp(binding['source_received_at'])
    _need(_stamp(policy['acceptance']['accepted_at']) <= issued <= _stamp(now),
          'work_authority_historical_mail_requires_explicit_grant')
    delegation = request['delegation']
    _need(delegation.get('basis') == 'accepted_brand_policy_derivation'
          and delegation.get('parent_policy_digest') == pdigest
          and delegation.get('parent_acceptance_ref') == policy['acceptance']['evidence_ref']
          and delegation.get('actor') == policy['acceptance']['actor']
          and delegation.get('source_fingerprint') == binding['source_fingerprint'],
          'work_authority_derived_delegation_unverified')
    deadline = business_day_expiry(issued)
    expiry = min(_stamp(deadline), _stamp(budget['expires_at']), _stamp(scope['authority']['expires_at']),
                 _stamp(policy['acceptance']['expires_at']), _stamp(delegation['expires_at']))
    _need(_stamp(now) < expiry, 'work_authority_intake_grant_already_expired')
    derivation = {'intake_evidence_ref': intake.evidence_ref,
                  'classification_evidence_ref': intake.classification_evidence_ref,
                  'binding_sha256': intake.binding_sha256, 'parent_policy_digest': pdigest,
                  'adapter_id': intake.adapter_id, 'provider_identity': intake.provider_identity,
                  'source_kind': intake.source_kind, 'origin_source_received_at': issued.isoformat()}
    return _make_grant(policy, binding, pdigest, request, budget, issued, expiry, deadline,
                       actor=policy['acceptance']['actor'], evidence_ref=policy['acceptance']['evidence_ref'],
                       issuer_kind='accepted_brand_policy_derivation', derivation=derivation)


def _grant_integrity(grant):
    _need(isinstance(grant, dict) and type(grant.get('schema_version')) is int
          and grant['schema_version'] == 1 and grant.get('kind') == GRANT_KIND,
          'work_authority_grant_invalid')
    _need(grant.get('grant_id') == 'work-grant-' + scope_digest({k: grant[k] for k in IMMUTABLE_GRANT_FIELDS}),
          'work_authority_grant_integrity_mismatch')
    _need(grant.get('status') == 'active' and grant.get('revision') == 0 and grant.get('revocations') == [],
          'work_authority_revoked')
    history = grant.get('rebindings')
    _need(isinstance(history, list) and len(history) <= 32, 'work_authority_rebind_history_invalid')
    expected = grant['binding']
    for row in reversed(history):
        previous = row['previous_binding']
        _need(row.get('current_binding') == expected
              and all(previous[k] == expected[k] for k in BINDING_FIELDS if k not in FOLLOWUP_CHANGE_FIELDS)
              and previous['source_message_id'] != expected['source_message_id']
              and previous['source_fingerprint'] != expected['source_fingerprint']
              and _stamp(previous['source_received_at']) <= _stamp(expected['source_received_at'])
              and row.get('adapter_id') == 'hamle-authenticated-intake-v1'
              and row.get('provider_identity') == HAMLE
              and row.get('source_kind') == 'authenticated_gmail_api'
              and row.get('parent_policy_digest') == grant['policy_digest']
              and _text(row.get('previous_grant_id')) and _text(row.get('intake_evidence_ref'))
              and _text(row.get('classification_evidence_ref')), 'work_authority_rebind_history_invalid')
        expected = previous
    return expected


def rebind_work_authority_from_followup(policy, scope, request, grant, intake, budget, *, now):
    """Pin a same-scope continuation to its actual new incoming mail, never renew.

    The caller MUST read the previous grant from its current durable work record
    and replace that record with this result using a compare-and-set transaction.
    A new source is not a new grant period. Brief/recipients/generations stay exact.
    """
    binding, pdigest = _context(policy, scope, request, budget, now)
    _grant_integrity(grant)
    _need(grant['policy_digest'] == pdigest
          and grant['actions'] == sorted(policy['actions'])
          and grant['authorized_issuers'] == sorted(policy['allowed_issuers'])
          and grant['actor'] in policy['allowed_issuers']
          and grant['budget_policy_id'] == budget['policy_id']
          and grant['budget_deadline'] == _stamp(budget['expires_at']).isoformat()
          and _stamp(grant['issued_at']) <= _stamp(now) < _stamp(grant['expires_at'])
          and _stamp(grant['expires_at']) <= min(_stamp(grant['business_expires_at']),
              _stamp(budget['expires_at']), _stamp(scope['authority']['expires_at']),
              _stamp(policy['acceptance']['expires_at']), _stamp(request['delegation']['expires_at'])),
          'work_authority_followup_prior_authority_invalid')
    _need(type(intake) is TrustedIntakeEvidence and intake.intake_kind == 'same_scope_followup'
          and intake.adapter_id == 'hamle-authenticated-intake-v1' and intake.provider_identity == HAMLE
          and intake.source_kind == 'authenticated_gmail_api' and intake.classification == 'routine'
          and intake.model_supplied_authority is False and _text(intake.evidence_ref)
          and _text(intake.classification_evidence_ref) and intake.binding_sha256 == scope_digest(binding)
          and intake.parent_policy_digest == pdigest
          and _fresh(now, intake.observed_at, policy['observation_max_age_seconds']),
          'work_authority_trusted_followup_required')
    previous = grant['binding']
    _need(all(binding[k] == previous[k] for k in BINDING_FIELDS if k not in FOLLOWUP_CHANGE_FIELDS),
          'work_authority_followup_scope_changed')
    if binding == previous:
        _need(grant['delegation_digest'] == scope_digest(request['delegation']),
              'work_authority_delegation_changed')
        return copy.deepcopy(grant)
    _need(binding['source_message_id'] != previous['source_message_id']
          and binding['source_fingerprint'] != previous['source_fingerprint']
          and _stamp(previous['source_received_at']) <= _stamp(binding['source_received_at']) <= _stamp(now)
          and len(grant['rebindings']) < 32, 'work_authority_followup_not_new_or_history_full')
    if grant['issuer_kind'] == 'accepted_brand_policy_derivation':
        delegation = request['delegation']
        _need(delegation.get('basis') == 'accepted_brand_policy_derivation'
              and delegation.get('parent_policy_digest') == pdigest
              and delegation.get('parent_acceptance_ref') == policy['acceptance']['evidence_ref']
              and delegation.get('actor') == policy['acceptance']['actor']
              and delegation.get('source_fingerprint') == binding['source_fingerprint'],
              'work_authority_derived_delegation_unverified')
    out = copy.deepcopy(grant)
    out['rebindings'].append({'previous_grant_id': grant['grant_id'], 'previous_binding': previous,
        'current_binding': binding, 'intake_evidence_ref': intake.evidence_ref,
        'classification_evidence_ref': intake.classification_evidence_ref,
        'adapter_id': intake.adapter_id, 'provider_identity': intake.provider_identity,
        'source_kind': intake.source_kind, 'parent_policy_digest': pdigest})
    out.update(binding=binding, delegation_digest=scope_digest(request['delegation']))
    out['grant_id'] = 'work-grant-' + scope_digest({k: out[k] for k in IMMUTABLE_GRANT_FIELDS})
    return out


def revoke_work_authority(grant, issuer, *, now, reason='human_takeover'):
    """Terminal revoke; never resume or mutate the original grant/history."""
    _need(isinstance(grant, dict) and grant.get('kind') == GRANT_KIND,
          'work_authority_grant_invalid')
    _need(isinstance(issuer, dict) and issuer.get('verified') is True and issuer.get('authenticated') is True
          and issuer.get('actor') in ISSUERS and issuer.get('actor') in grant.get('authorized_issuers', [])
          and issuer.get('actor') in issuer.get('authorized_catalog_issuers', [])
          and issuer.get('decision') == 'revoke_visible_stop_work_authority'
          and issuer.get('grant_id') == grant.get('grant_id') and _text(issuer.get('evidence_ref'))
          and _fresh(now, issuer.get('issued_at')), 'work_authority_authenticated_revoker_required')
    _need(reason in {'human_takeover', 'explicit_stop', 'work_closed', 'scope_changed', 'authority_expired'},
          'work_authority_revocation_reason_invalid')
    out = copy.deepcopy(grant)
    if out['status'] == 'revoked':
        return out
    _need(out['status'] == 'active', 'work_authority_grant_state_invalid')
    out.update(status='revoked', revision=out['revision'] + 1)
    out['revocations'].append({'actor': issuer['actor'], 'evidence_ref': issuer['evidence_ref'],
                               'issued_at': _stamp(now).isoformat(), 'reason': reason})
    return out


def authorize_work_authority(policy, scope, request, grant, observation, budget, *, now):
    """Consistency gate only; still require central authorize_action and dispatch.

    A fresh database observation binds the current grant digest and revision.
    Refreshing it never changes issued_at, original mail time or the deadline.
    """
    result = {'allowed': False, 'reason': None, 'authority_kind': GRANT_KIND,
              'live_enabled_by_this_module': False, 'unseen_cc_removal_detected': False}
    try:
        binding, pdigest = _context(policy, scope, request, budget, now)
        origin_binding = _grant_integrity(grant)
        _need(grant.get('binding') == binding and grant.get('policy_digest') == pdigest,
              'work_authority_binding_changed')
        _need(grant['actor'] in policy['allowed_issuers']
              and grant['authorized_issuers'] == sorted(policy['allowed_issuers']) and _text(grant['issuer_evidence_ref'])
              and grant.get('actions') == sorted(policy['actions']) and request.get('action') in grant['actions'],
              'work_authority_action_not_granted')
        _need(grant.get('delegation_digest') == scope_digest(request['delegation']), 'work_authority_delegation_changed')
        _need(grant.get('issuer_kind') in ('authenticated_human_decision', 'accepted_brand_policy_derivation'),
              'work_authority_issuer_kind_invalid')
        if grant['issuer_kind'] == 'accepted_brand_policy_derivation':
            proof = grant.get('derivation', {})
            _need(isinstance(proof, dict) and proof.get('binding_sha256') == scope_digest(origin_binding)
                  and proof.get('parent_policy_digest') == pdigest
                  and proof.get('adapter_id') == 'hamle-authenticated-intake-v1'
                  and proof.get('provider_identity') == HAMLE
                  and proof.get('source_kind') == 'authenticated_gmail_api'
                  and _text(proof.get('intake_evidence_ref')) and _text(proof.get('classification_evidence_ref'))
                  and grant['actor'] == policy['acceptance']['actor']
                  and grant['issuer_evidence_ref'] == policy['acceptance']['evidence_ref']
                  and proof.get('origin_source_received_at') == origin_binding['source_received_at'] == grant['issued_at']
                  and _stamp(policy['acceptance']['accepted_at']) <= _stamp(grant['issued_at']),
                  'work_authority_derivation_proof_invalid')
        else:
            _need(grant.get('derivation') is None, 'work_authority_explicit_grant_has_derivation')
        _need(grant['business_expires_at'] == business_day_expiry(grant['issued_at'])
              and _stamp(grant['issued_at']) <= _stamp(now) < _stamp(grant['expires_at'])
              <= _stamp(grant['business_expires_at']), 'work_authority_expired')
        _need(grant['budget_policy_id'] == budget['policy_id']
              and grant['budget_deadline'] == _stamp(budget['expires_at']).isoformat()
              and _stamp(grant['expires_at']) <= _stamp(grant['budget_deadline']),
              'work_authority_budget_policy_changed')
        _need(isinstance(observation, dict) and observation.get('verified') is True
              and _text(observation.get('evidence_ref')) and observation.get('grant_id') == grant['grant_id']
              and observation.get('grant_sha256') == scope_digest(grant)
              and observation.get('grant_revision') == grant['revision']
              and _fresh(now, observation.get('observed_at'), policy['observation_max_age_seconds']),
              'work_authority_current_grant_readback_required')
        result.update(allowed=True, reason='accepted_visible_stop_work_authority', grant_id=grant['grant_id'])
    except (WorkAuthorityError, ValueError, TypeError, KeyError, AttributeError) as exc:
        result['reason'] = str(exc) if isinstance(exc, WorkAuthorityError) else 'malformed_work_authority_evidence'
    return result
