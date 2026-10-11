"""Atomic work authority + queue adapter in the existing Operations collection.

No credentials, authentication, configuration enablement or provider calls here.
The server supplies trusted scope and typed identity/provider evidence. A queue
claim is preparation only; only begin_authorized_write can start an action.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone

from autonomy_policy import authorize_action, scope_digest
from cloud_store import OperationsStore, StoreError, _json, _require
from v11_work_authority import (ISSUERS, HAMLE, SHA256, TrustedIntakeEvidence, WorkAuthorityError,
    authorize_work_authority, derive_work_authority_from_intake, grant_work_authority,
    rebind_work_authority_from_followup, revoke_work_authority)
from v11_work_queue import QueueStore, QueueError, WRITE_BINDING_FIELDS, text_id

QUEUE_PREFIX = 'waq-'
GRANT_PREFIX = 'work-authority-'
NAMESPACE = 'work-authority-queue-v1'


def _stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _need(ok, reason):
    _require(ok, reason)


def _key(work_id):
    text_id(work_id)
    _need('/' not in work_id, 'invalid_work_id')
    return GRANT_PREFIX + work_id


@dataclass(frozen=True)
class TrustedWorkStop:
    """Construct ONLY after IAP/session or actual Gmail sender verification.

    This Python type prevents accepting model/HTTP JSON as proof; the trusted
    adapter still owns real identity verification and visible-stop intent.
    Customer Gmail stop also needs exact work recipients + verified requester
    catalogue supplied separately by the trusted server, never email/model JSON.
    """
    work_id: str
    actor: str
    identity_subject: str
    evidence_ref: str
    observed_at: str
    channel: str = 'authenticated_control_screen'
    thread_id: str | None = None
    actual_to_cc: tuple = ()
    provider_identity: str | None = None
    model_supplied_authority: bool = False
    source_message_id: str | None = None
    source_fingerprint: str | None = None
    source_sender: str | None = None
    work_to: tuple = ()
    work_cc: tuple = ()
    stop_intent: str = 'explicit_stop'


class _QueueTx:
    def __init__(self, tx, *, allow_begin=False):
        self.tx, self.allow_begin, self.loaded = tx, allow_begin, {}

    def get(self, key):
        if key == 'control':
            control = OperationsStore._control(self.tx)
            return {'id': 'control', 'kind': 'control',
                    'mode': 'queue_open' if control['mode'] == 'review_open' else control['mode'],
                    'generation': control['generation'], 'changed_at': control['changed_at'],
                    'actor': control['actor_subject']}
        text_id(key); _need('/' not in key, 'invalid_queue_key')
        value = self.tx.get(QUEUE_PREFIX + key)
        if value is not None:
            _need(value.get('_authority_namespace') == NAMESPACE, 'queue_namespace_mismatch')
            value.pop('_authority_namespace')
        self.loaded[key] = copy.deepcopy(value)
        return value

    def put(self, key, value):
        _need(key != 'control', 'use_shared_operations_control')
        text_id(key); _need('/' not in key, 'invalid_queue_key')
        if value.get('kind') == 'action' and value.get('state') == 'started':
            _need(self.allow_begin, 'use_authority_store_begin_write')
        previous = self.loaded.get(key)
        if previous and value.get('kind') == 'work':
            _need(value['mode'] == previous['mode']
                  and value['work_generation'] == previous['work_generation'],
                  'use_atomic_authority_stop')
        stored = {**copy.deepcopy(value), '_authority_namespace': NAMESPACE}
        _json(stored)
        self.tx.put(QUEUE_PREFIX + key, stored)


class BoundAuthorityQueueBackend:
    """Same transaction/backend as Operations; never the legacy queue collection.

    Direct control changes, per-work mode changes and action starts are denied.
    Discovery is bounded; no provider operation is authorized by a query result.
    """
    def __init__(self, operations):
        _need(type(operations) is OperationsStore, 'operations_store_required')
        self.operations = operations

    def transact(self, callback):
        return self.operations.backend.transact(lambda tx: callback(_QueueTx(tx)))

    def scan(self, *, kind, filters, limit, cursor=None):
        backend = self.operations.backend
        # Serializable offline backend; never imported into production.
        if hasattr(backend, 'data') and hasattr(backend, 'lock'):
            with backend.lock:
                records = copy.deepcopy(backend.data)
            return [dict((k, v) for k, v in row.items() if k != '_authority_namespace')
                    for key, row in sorted(records.items())
                    if key.startswith(QUEUE_PREFIX) and (cursor is None or key > QUEUE_PREFIX + cursor)
                    and row.get('_authority_namespace') == NAMESPACE and row.get('kind') == kind
                    and all(row.get(k) == value for k, value in filters.items())][:limit]
        _need(hasattr(backend, 'collection'), 'atomic_queue_scan_unsupported')
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter
            query = backend.collection.where(filter=FieldFilter('_authority_namespace', '==', NAMESPACE))
            query = query.where(filter=FieldFilter('kind', '==', kind))
            for key, value in filters.items():
                query = query.where(filter=FieldFilter(key, '==', value))
            query = query.order_by('__name__')
            if cursor:
                query = query.start_after({'__name__': backend.collection.document(QUEUE_PREFIX + cursor)})
            result = []
            for row in query.limit(limit).stream(retry=None, timeout=10):
                value = row.to_dict(); value.pop('_authority_namespace')
                result.append(value)
            return result
        except Exception:
            raise StoreError('atomic_queue_query_failed') from None


class WorkAuthorityStore:
    def __init__(self, operations, *, queue_backend=None):
        _need(type(operations) is OperationsStore, 'operations_store_required')
        _need(queue_backend is None or (type(queue_backend) is BoundAuthorityQueueBackend
              and queue_backend.operations is operations), 'separate_queue_backend_unsafe')
        self.ops = operations
        self._queue_backend = queue_backend or BoundAuthorityQueueBackend(operations)

    def queue_backend(self):
        return self._queue_backend

    def queue(self):
        return QueueStore(self._queue_backend, clock=lambda: self.ops._now(allow_expired=True))

    def _read(self, tx, work_id):
        control = self.ops._control(tx)
        work = _QueueTx(tx).get(work_id)
        _need(isinstance(work, dict) and work.get('kind') == 'work', 'authority_work_missing')
        record = tx.get(_key(work_id))
        return control, work, record

    def _budget(self, tx, now):
        budget = self.ops._budget(tx)
        totals = self.ops._totals(budget)
        return {'verified': True, 'allowed': not totals['overrun_hold'] and totals['remaining_microdollars'] > 0,
                'policy_id': budget['policy_id'], 'overrun_hold': totals['overrun_hold'],
                'remaining_microdollars': totals['remaining_microdollars'],
                'expires_at': budget['expires_at'], 'observed_at': _stamp(now),
                'evidence_ref': 'firestore:hamle_internal_v1/budget:' + scope_digest(budget)}

    @staticmethod
    def _observation(record, now):
        grant = record['grant']
        if grant is None:
            return None
        return {'verified': True, 'grant_id': grant['grant_id'], 'grant_sha256': scope_digest(grant),
                'grant_revision': grant['revision'], 'observed_at': _stamp(now),
                'evidence_ref': 'firestore:hamle_internal_v1/' + record['id']}

    def _request(self, scope, request, control, work, budget, now):
        service = [s for s in scope['services'] if s['service_id'] == work['service_id']]
        # review_open is NOT customer permission. Trusted deployment scope must
        # explicitly enable this exact service after its separate acceptance.
        _need(len(service) == 1 and service[0].get('live_enabled') is True,
              'customer_service_not_live')
        self.ops._open(control, request['control']['global_generation'], now)
        _need(work['mode'] == 'active' and work['work_generation'] == request['control']['work_generation'],
              'work_stopped_or_generation_changed')
        _need(work['inflight_action_id'] is None, 'write_outcome_unresolved')
        _need(all(request['work_binding'].get(k) == work[k] for k in WRITE_BINDING_FIELDS)
              and all(request.get(k) == work[k] for k in ('work_id', 'brand_id', 'service_id'))
              and request['source']['thread_id'] == work['thread_id'], 'authority_queue_binding_mismatch')
        out = copy.deepcopy(request)
        out['control'] = {'work_id': work['work_id'], 'global_mode': 'routine_enabled', 'work_mode': work['mode'],
                          'global_generation': control['generation'], 'work_generation': work['work_generation'],
                          'observed_at': _stamp(now)}
        out['budget_authorization'] = budget
        return service[0]['work_authority_policy'], out

    def install_grant(self, scope, request, *, intake=None, issuer=None):
        """Trusted intake/human proof -> first persisted grant. Never auto-renew."""
        _need((intake is None) != (issuer is None), 'one_grant_evidence_required')
        def op(tx, now):
            control, work, old = self._read(tx, request['work_id'])
            budget = self._budget(tx, now)
            policy, current = self._request(scope, request, control, work, budget, now)
            moment = datetime.fromtimestamp(now, timezone.utc)
            grant = (derive_work_authority_from_intake(policy, scope, current, intake, budget, now=moment)
                     if intake is not None else grant_work_authority(policy, scope, current, issuer, budget, now=moment))
            if old:
                _need(old['grant'] == grant, 'work_grant_already_installed')
                return copy.deepcopy(old)
            record = {'id': _key(work['work_id']), 'kind': 'work_authority', 'work_id': work['work_id'],
                      'version': 0, 'grant': grant, 'created_at': now, 'updated_at': now, 'last_stop': None}
            _json(record); tx.put(record['id'], record)
            return record
        return self.ops._run(op)

    def observe_work(self, work_id):
        """Current durable read, also available when stopped or past budget time."""
        def op(tx, now):
            control, work, record = self._read(tx, work_id)
            return {'work': work, 'global_control': control, 'record': record,
                    'observation': self._observation(record, now) if record else None,
                    'observed_at': _stamp(now), 'customer_dispatch_enabled_by_this_read': False}
        return self.ops._run(op, allow_expired=True)

    def rebind_grant(self, scope, request, intake, *, expected_grant_id):
        def op(tx, now):
            control, work, record = self._read(tx, request['work_id'])
            budget = self._budget(tx, now)
            _need(record is not None, 'work_grant_missing')
            old = record['grant']
            policy, current = self._request(scope, request, control, work, budget, now)
            grant = rebind_work_authority_from_followup(policy, scope, current, old, intake, budget,
                        now=datetime.fromtimestamp(now, timezone.utc))
            if grant == old:
                return record
            _need(old['grant_id'] == expected_grant_id, 'stale_work_grant')
            history_key = 'authority-history-' + old['grant_id']
            history = tx.get(history_key)
            _need(history is None, 'authority_history_already_exists')
            record.update(grant=grant, version=record['version'] + 1, updated_at=now)
            # A pending action based on the old source must obtain a new lease.
            work.update(lease=None, version=work['version'] + 1,
                        queue_state='ready' if work['head_sequence'] < work['next_sequence'] else 'idle', updated_at=now)
            _json(record)
            tx.put(history_key, {'kind': 'work_authority_history', 'grant': old, 'superseded_by': grant['grant_id']})
            tx.put(record['id'], record)
            tx.put(QUEUE_PREFIX + work['id'], {**work, '_authority_namespace': NAMESPACE})
            return record
        return self.ops._run(op)

    @staticmethod
    def _mail_stop(stop, work):
        _need(stop.channel == 'authenticated_gmail_api' and stop.provider_identity == HAMLE
              and stop.thread_id == work['thread_id'] and HAMLE in stop.actual_to_cc
              and isinstance(stop.source_message_id, str) and bool(stop.source_message_id)
              and isinstance(stop.source_fingerprint, str) and SHA256.fullmatch(stop.source_fingerprint)
              and stop.source_sender == stop.actor and stop.actor != HAMLE,
              'work_stop_mail_binding_invalid')

    def stop_work(self, work_id, stop, *, expected_work_generation, mode='takeover',
                  trusted_scope=None, trusted_catalog=None):
        """One transaction: terminal revoke, generation fence, lease invalidation.

        Already started/unknown provider attempts remain reconciliation-only.
        Stop remains available after the spending deadline and global stop.
        """
        return self._stop(work_id, stop, expected_work_generation=expected_work_generation,
                          mode=mode, trusted_scope=trusted_scope, trusted_catalog=trusted_catalog,
                          verification_pending=False)

    def hold_received_stop(self, work_id, stop, *, expected_work_generation):
        """Fail closed if a real thread stop arrives but role lookup is incomplete.

        Trusted mail ingestion calls this BEFORE routine classification/actions
        whenever it cannot conclusively validate a received stop. It revokes and
        holds the work, with pending-verification provenance; never renews/grants.
        """
        return self._stop(work_id, stop, expected_work_generation=expected_work_generation,
                          mode='takeover', trusted_scope=None, trusted_catalog=None,
                          verification_pending=True)

    def _stop(self, work_id, stop, *, expected_work_generation, mode, trusted_scope,
              trusted_catalog, verification_pending):
        _need(type(stop) is TrustedWorkStop and stop.work_id == work_id
              and isinstance(stop.actor, str) and '@' in stop.actor
              and bool(stop.identity_subject) and bool(stop.evidence_ref)
              and stop.model_supplied_authority is False
              and stop.channel in ('authenticated_control_screen', 'authenticated_gmail_api')
              and stop.stop_intent in ('explicit_stop', 'human_takeover'),
              'authenticated_work_stop_required')
        if stop.channel == 'authenticated_control_screen':
            _need(stop.actor in ISSUERS and not verification_pending, 'authenticated_work_stop_required')
        _need(mode in ('takeover', 'stopped'), 'invalid_work_stop_mode')
        def op(tx, now):
            control, work, record = self._read(tx, work_id)
            moment = datetime.fromtimestamp(now, timezone.utc)
            observed = datetime.fromisoformat(stop.observed_at)
            _need(observed.tzinfo is not None and 0 <= (moment-observed).total_seconds() <= 300,
                  'work_stop_evidence_stale')
            if stop.channel == 'authenticated_gmail_api':
                self._mail_stop(stop, work)
            else:
                _need(not verification_pending, 'pending_stop_requires_real_mail')
            participant_stop = stop.actor not in ISSUERS
            if participant_stop and not verification_pending:
                _need(isinstance(trusted_scope, dict) and isinstance(trusted_catalog, dict)
                      and trusted_catalog.get('verified') is True
                      and trusted_catalog.get('brand_id') == trusted_scope.get('brand_id') == work['brand_id']
                      and stop.actor in trusted_scope.get('customer_contacts', [])
                      and record is not None and record.get('grant') is not None
                      and record['grant']['binding']['scope_digest'] == scope_digest(trusted_scope),
                      'customer_stop_trusted_scope_required')
                people = [p for p in trusted_catalog.get('contacts', []) if p.get('email') == stop.actor]
                _need(len(people) == 1 and people[0].get('verified') is True
                      and people[0].get('side') == 'customer' and people[0].get('can_request') is True
                      and work['service_id'] in people[0].get('service_ids', [])
                      and bool(people[0].get('verification_ref')),
                      'customer_stop_requester_catalog_required')
                _need(type(stop.work_to) is tuple and type(stop.work_cc) is tuple
                      and stop.actor in stop.work_to + stop.work_cc
                      and scope_digest({'to': list(stop.work_to), 'cc': list(stop.work_cc)})
                          == record['grant']['binding']['recipients_sha256'],
                      'customer_stop_not_bound_work_participant')
            stop_id = scope_digest({'work_id': work_id, 'actor': stop.actor, 'subject': stop.identity_subject,
                                    'evidence_ref': stop.evidence_ref, 'mode': mode,
                                    'verification_pending': verification_pending})
            if record and (record.get('last_stop') or {}).get('stop_id') == stop_id:
                return {'work': work, 'record': record, 'duplicate': True}
            _need(type(expected_work_generation) is int and expected_work_generation == work['work_generation'],
                  'stale_work_generation')
            if record and record.get('grant') is not None:
                grant = record['grant']
                if participant_stop or verification_pending:
                    # Reducing existing authority is not a grant by a new human.
                    # Keep the actual provider sender and honest verification state.
                    if grant['status'] == 'active':
                        grant.update(status='revoked', revision=grant['revision'] + 1)
                        grant['revocations'].append({'actor': stop.actor, 'evidence_ref': stop.evidence_ref,
                            'issued_at': moment.isoformat(), 'reason': 'received_visible_stop',
                            'actor_kind': 'provider_stop_verification_pending' if verification_pending else 'verified_customer_participant',
                            'source_message_id': stop.source_message_id, 'source_fingerprint': stop.source_fingerprint})
                    _need(grant['status'] == 'revoked', 'work_authority_grant_state_invalid')
                else:
                    issuer = {'verified': True, 'authenticated': True, 'actor': stop.actor,
                              'decision': 'revoke_visible_stop_work_authority', 'grant_id': grant['grant_id'],
                              'authorized_catalog_issuers': grant['authorized_issuers'],
                              'evidence_ref': stop.evidence_ref, 'issued_at': stop.observed_at}
                    record['grant'] = revoke_work_authority(grant, issuer, now=moment,
                            reason='human_takeover' if mode == 'takeover' else 'explicit_stop')
                record.update(version=record['version'] + 1, updated_at=now)
            elif record is None:
                # Ungranted queued work can also be stopped before any issuance.
                record = {'id': _key(work_id), 'kind': 'work_authority', 'work_id': work_id,
                          'version': 0, 'grant': None, 'created_at': now, 'updated_at': now}
            else:
                record.update(version=record['version'] + 1, updated_at=now)
            record['last_stop'] = {'stop_id': stop_id, 'actor': stop.actor, 'channel': stop.channel,
                                   'evidence_ref': stop.evidence_ref, 'at': now,
                                   'verification_pending': verification_pending,
                                   'source_message_id': stop.source_message_id,
                                   'source_fingerprint': stop.source_fingerprint}
            work.update(mode=mode, work_generation=work['work_generation'] + 1, version=work['version'] + 1,
                        lease=None, updated_at=now, actor=stop.actor,
                        queue_state='unknown' if work['inflight_action_id'] else
                            ('ready' if work['head_sequence'] < work['next_sequence'] else 'idle'))
            _json(record)
            tx.put(record['id'], record)
            tx.put(QUEUE_PREFIX + work_id, {**work, '_authority_namespace': NAMESPACE})
            return {'work': work, 'record': record, 'duplicate': False}
        return self.ops._run(op, allow_expired=True)

    def begin_authorized_write(self, claim, scope, request, *, action_key):
        """Central policy + current grant/control/budget + queue fence atomically.

        Returns a single-attempt ledger record, not a provider receipt. The caller
        still reserves any charge and handles unknown provider outcomes without
        retry. A stop after this commit cannot unsend an upstream accepted write.
        """
        def op(tx, now):
            control, work, record = self._read(tx, claim['work_id'])
            budget = self._budget(tx, now)
            _need(record is not None and record.get('grant') is not None, 'work_grant_missing')
            policy, current = self._request(scope, request, control, work, budget, now)
            current.update(work_authority_grant=record['grant'],
                           work_authority_observation=self._observation(record, now))
            decision = authorize_action(scope, current, now=datetime.fromtimestamp(now, timezone.utc))
            _need(decision['allowed'], decision['reason'])
            class OneTransaction:
                def transact(self, callback):
                    return callback(_QueueTx(tx, allow_begin=True))
            queue = QueueStore(OneTransaction(), clock=lambda: now)
            prepared = {k: current['work_binding'][k] for k in WRITE_BINDING_FIELDS}
            prepared['queue_event_fingerprint'] = current['source']['fingerprint']
            return queue.begin_write(claim, action_kind=current['action'],
                payload_sha256=scope_digest(current['action_payload']), action_key=action_key,
                prepared_binding=prepared)
        return self.ops._run(op)
