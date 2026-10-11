"""Offline accepted-policy fixtures only. No live policy or grant is installed."""
import copy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import unittest

from autonomy_policy import ACTIONS, authorize_action, scope_digest
from test_autonomy_policy import fixture as old_fixture, select_action, delivery, fresh_authority, stamp, NOW
from v11_work_authority import (ISSUERS, GRANT_KIND, WorkAuthorityError, authorize_work_authority,
    build_work_authority_binding, business_day_expiry, default_work_authority_policy,
    grant_work_authority, revoke_work_authority, validate_work_authority_policy, policy_terms_digest,
    derive_work_authority_from_intake, TrustedIntakeEvidence)

AHMET = 'ahmet@gamechangers.com.tr'


def fixture():
    scope, request = old_fixture()
    scope['agency_contacts'] = sorted(ISSUERS) + ['producer@example.test']
    scope['authority'].update(verified_by=AHMET, expires_at=stamp(10 * 86400))
    scope['agency_owner'] = AHMET
    service = scope['services'][0]
    policy = default_work_authority_policy(scope['brand_id'], service['service_id'])
    policy.update(enabled=True, status='accepted', actions=sorted(ACTIONS),
                  acceptance={'verified': True, 'authenticated': True, 'actor': AHMET,
                    'decision': 'accept_visible_stop_work_authority', 'evidence_ref': 'synthetic-explicit-policy-choice',
                    'accepted_at': stamp(-3600), 'expires_at': stamp(10 * 86400)})
    policy['acceptance']['policy_terms_sha256'] = policy_terms_digest(policy)
    service['work_authority_policy'] = policy
    request['delegation'].update(actor=AHMET, scope_digest=scope_digest(scope), expires_at=stamp(10 * 86400))
    request['source']['received_at'] = stamp(-3600)
    request['work_brief_sha256'] = 'b' * 64
    budget = {'verified': True, 'allowed': True, 'overrun_hold': False,
              'policy_id': 'synthetic-shared-budget-50', 'remaining_microdollars': 31_000_000,
              'observed_at': stamp(-1), 'expires_at': '2027-12-31T23:59:59+03:00',
              'evidence_ref': 'synthetic-fresh-budget-ledger'}
    issuer = {'verified': True, 'authenticated': True, 'actor': AHMET,
              'decision': 'grant_visible_stop_work_authority', 'evidence_ref': 'synthetic-human-grant-1',
              'issued_at': stamp(-1), 'binding_sha256': scope_digest(build_work_authority_binding(scope, request)),
              'policy_digest': scope_digest(policy)}
    grant = grant_work_authority(policy, scope, request, issuer, budget, now=NOW)
    observation = {'verified': True, 'grant_id': grant['grant_id'], 'grant_sha256': scope_digest(grant),
                   'grant_revision': grant['revision'], 'observed_at': stamp(-1),
                   'evidence_ref': 'synthetic-current-grant-document'}
    request.update(work_authority_grant=grant, work_authority_observation=observation, budget_authorization=budget)
    return policy, scope, request, issuer


class WorkAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.policy, self.scope, self.request, self.issuer = fixture()

    def decision(self, now=NOW):
        r = self.request
        return authorize_work_authority(self.policy, self.scope, r, r['work_authority_grant'],
                                         r['work_authority_observation'], r['budget_authorization'], now=now)

    def central(self):
        return authorize_action(self.scope, self.request, now=NOW)

    def intake(self):
        self.request['delegation'].update(basis='accepted_brand_policy_derivation',
            parent_policy_digest=scope_digest(self.policy),
            parent_acceptance_ref=self.policy['acceptance']['evidence_ref'],
            source_fingerprint=self.request['source']['fingerprint'])
        return TrustedIntakeEvidence(adapter_id='hamle-authenticated-intake-v1',
            provider_identity='hamle@gamechangers.com.tr', evidence_ref='synthetic-provider-intake-1',
            classification_evidence_ref='synthetic-independent-routine-classification',
            binding_sha256=scope_digest(build_work_authority_binding(self.scope, self.request)),
            parent_policy_digest=scope_digest(self.policy), observed_at=stamp())

    def derive(self, intake=None, now=NOW):
        return derive_work_authority_from_intake(self.policy, self.scope, self.request,
            self.intake() if intake is None else intake, self.request['budget_authorization'], now=now)

    def install(self, grant):
        self.request['work_authority_grant'] = grant
        self.request['work_authority_observation'].update(grant_id=grant['grant_id'],
            grant_sha256=scope_digest(grant), grant_revision=grant['revision'])

    def test_accepted_brand_policy_derives_new_work_without_fake_fresh_human_decision(self):
        grant = self.derive()
        self.assertEqual(grant['issuer_kind'], 'accepted_brand_policy_derivation')
        self.assertEqual(grant['actor'], self.policy['acceptance']['actor'])
        self.assertEqual(grant['issuer_evidence_ref'], self.policy['acceptance']['evidence_ref'])
        self.assertEqual(grant['issued_at'], self.request['source']['received_at'])
        self.install(grant)
        self.assertTrue(self.central()['allowed'], self.central())

    def test_model_json_with_bogus_verified_flags_cannot_derive_authority(self):
        raw = asdict(self.intake())
        raw.update(verified=True, authenticated=True)
        with self.assertRaisesRegex(WorkAuthorityError, 'trusted_new_intake_required'):
            self.derive(raw)

    def test_model_comment_or_followup_cannot_masquerade_as_new_provider_intake(self):
        for fields in ({'source_kind': 'basecamp_comment'}, {'model_supplied_authority': True},
                       {'intake_kind': 'followup'}, {'provider_identity': AHMET}, {'classification': 'commercial'},
                       {'classification_evidence_ref': ''}, {'parent_policy_digest': 'f' * 64}):
            with self.assertRaisesRegex(WorkAuthorityError, 'trusted_new_intake_required'):
                self.derive(replace(self.intake(), **fields))

    def test_intake_cannot_activate_historical_mail_predating_policy_acceptance(self):
        self.request['source']['received_at'] = stamp(-3601)
        with self.assertRaisesRegex(WorkAuthorityError, 'historical_mail_requires_explicit_grant'):
            self.derive()

    def test_new_read_of_same_intake_never_restarts_two_day_clock(self):
        intake = self.intake()
        first = self.derive(intake)
        later = NOW + timedelta(seconds=120)
        self.request['source']['captured_at'] = later.isoformat()
        self.request['control']['observed_at'] = later.isoformat()
        self.request['budget_authorization']['observed_at'] = later.isoformat()
        again = self.derive(replace(intake, observed_at=later.isoformat()), now=later)
        self.assertEqual(first, again)

    def test_parent_policy_choice_does_not_fake_legacy_delegation(self):
        intake = self.intake()
        self.request['delegation'].pop('basis')
        with self.assertRaisesRegex(WorkAuthorityError, 'derived_delegation_unverified'):
            self.derive(intake)

    def test_parent_acceptance_is_bound_to_exact_policy_terms(self):
        self.policy['actions'] = ['acknowledge']
        with self.assertRaisesRegex(WorkAuthorityError, 'explicit_acceptance_missing'):
            self.derive()

    def test_default_template_is_disabled_and_cannot_grant(self):
        policy = default_work_authority_policy('wat_mobilite', 'coordination')
        self.assertFalse(policy['enabled'])
        self.scope['services'][0]['work_authority_policy'] = policy
        with self.assertRaisesRegex(WorkAuthorityError, 'policy_not_enabled'):
            grant_work_authority(policy, self.scope, self.request, self.issuer,
                                 self.request['budget_authorization'], now=NOW)

    def test_explicit_policy_acceptance_is_required_not_just_enabled_flag(self):
        self.policy['acceptance'] = None
        self.assertEqual(self.decision()['reason'], 'work_authority_explicit_acceptance_missing')

    def test_business_days_skip_weekend_preserve_istanbul_hour(self):
        self.assertEqual(business_day_expiry('2026-10-09T18:30:00+03:00'), '2026-10-13T18:30:00+03:00')
        self.assertEqual(business_day_expiry('2026-10-10T12:00:00+03:00'), '2026-10-13T12:00:00+03:00')
        self.assertEqual(business_day_expiry('2026-10-12T08:30:00+03:00'), '2026-10-14T08:30:00+03:00')

    def test_no_naive_timestamp_or_more_than_two_business_days(self):
        for value, days in (('2026-10-09T18:30:00', 2), ('2026-10-09T18:30:00+03:00', 3)):
            with self.assertRaises(WorkAuthorityError):
                business_day_expiry(value, days)

    def test_budget_deadline_caps_two_business_day_grant(self):
        grant = self.request['work_authority_grant']
        # no end date on authority (Ahmet 2026-10-11): the grant ends with its 2-business-day window
        self.assertLessEqual(datetime.fromisoformat(grant['expires_at']), datetime.fromisoformat(grant['business_expires_at']))
        self.assertLessEqual(datetime.fromisoformat(grant['expires_at']), datetime.fromisoformat('2027-12-31T23:59:59+03:00'))

    def test_same_human_decision_replay_cannot_extend_authority(self):
        first = self.request['work_authority_grant']
        req = copy.deepcopy(self.request)
        later = NOW + timedelta(seconds=120)
        req['source']['captured_at'] = later.isoformat()
        req['control']['observed_at'] = later.isoformat()
        req['budget_authorization']['observed_at'] = later.isoformat()
        again = grant_work_authority(self.policy, self.scope, req, self.issuer, req['budget_authorization'], now=later)
        self.assertEqual(first, again)
        self.assertEqual(again['binding']['source_received_at'], self.request['source']['received_at'])

    def test_valid_delayed_work_grant_still_is_not_provider_permission(self):
        self.assertTrue(self.decision()['allowed'], self.decision())
        self.assertTrue(self.central()['allowed'], self.central())
        self.assertFalse(self.decision()['live_enabled_by_this_module'])
        self.assertFalse(self.decision()['unseen_cc_removal_detected'])

    def test_policy_cannot_claim_unseen_cc_removal_detection(self):
        self.policy['unseen_cc_removal_detectable'] = True
        self.assertEqual(self.decision()['reason'], 'work_authority_visible_stop_acceptance_missing')

    def test_model_and_hamle_are_not_human_grant_issuers(self):
        for actor in ('hamle@gamechangers.com.tr', 'model', 'client@example.test'):
            issuer = {**self.issuer, 'actor': actor}
            with self.assertRaisesRegex(WorkAuthorityError, 'authenticated_issuer_required'):
                grant_work_authority(self.policy, self.scope, self.request, issuer,
                                     self.request['budget_authorization'], now=NOW)

    def test_any_catalogued_three_human_issuer_can_explicitly_grant(self):
        for actor in ISSUERS:
            issuer = {**self.issuer, 'actor': actor}
            grant = grant_work_authority(self.policy, self.scope, self.request, issuer,
                                         self.request['budget_authorization'], now=NOW)
            self.assertEqual(grant['actor'], actor)

    def test_authenticated_true_without_exact_binding_is_not_enough(self):
        issuer = {**self.issuer, 'binding_sha256': 'f' * 64}
        with self.assertRaisesRegex(WorkAuthorityError, 'authenticated_issuer_required'):
            grant_work_authority(self.policy, self.scope, self.request, issuer,
                                 self.request['budget_authorization'], now=NOW)

    def test_stale_human_decision_cannot_be_refreshed_by_provider_read(self):
        issuer = {**self.issuer, 'issued_at': stamp(-301)}
        with self.assertRaisesRegex(WorkAuthorityError, 'authenticated_issuer_required'):
            grant_work_authority(self.policy, self.scope, self.request, issuer,
                                 self.request['budget_authorization'], now=NOW)

    def test_changed_source_identity_or_received_at_revokes_reuse(self):
        for field, value in (('message_id', 'new-message'), ('fingerprint', 'f' * 64), ('received_at', stamp(-5))):
            request = copy.deepcopy(self.request)
            self.request['source'][field] = value
            self.assertEqual(self.decision()['reason'], 'work_authority_binding_changed')
            self.request = request

    def test_changed_brief_recipients_or_control_generation_revokes_reuse(self):
        for patch in ('brief', 'recipients', 'generation'):
            request = copy.deepcopy(self.request)
            if patch == 'brief': self.request['work_brief_sha256'] = 'f' * 64
            if patch == 'recipients': self.request['source']['cc'] = []
            if patch == 'generation':
                self.request['control']['work_generation'] += 1
                self.request['delegation']['work_generation'] += 1
            self.assertFalse(self.decision()['allowed'], patch)
            self.request = request

    def test_cannot_replace_original_source_age_with_read_time(self):
        self.request['source']['received_at'] = self.request['source']['captured_at']
        self.assertEqual(self.decision()['reason'], 'work_authority_binding_changed')

    def test_fresh_provider_read_never_extends_grant_deadline(self):
        r = self.request
        r['source']['captured_at'] = stamp()
        r['work_authority_observation']['observed_at'] = stamp()
        before = copy.deepcopy(r['work_authority_grant'])
        self.assertTrue(self.decision()['allowed'])
        self.assertEqual(r['work_authority_grant'], before)

    def test_missing_or_stale_grant_database_readback_is_held(self):
        for patch in ({'verified': False}, {'observed_at': stamp(-301)}, {'grant_revision': 9}, {'grant_sha256': 'f' * 64}):
            old = copy.deepcopy(self.request['work_authority_observation'])
            self.request['work_authority_observation'].update(patch)
            self.assertEqual(self.decision()['reason'], 'work_authority_current_grant_readback_required')
            self.request['work_authority_observation'] = old

    def test_future_observation_is_not_fresh(self):
        self.request['work_authority_observation']['observed_at'] = stamp(1)
        self.assertEqual(self.decision()['reason'], 'work_authority_current_grant_readback_required')

    def test_observation300_seconds_does_not_weaken_central60_second_source_gate(self):
        self.request['source']['captured_at'] = stamp(-61)
        self.assertTrue(self.decision()['allowed'])
        self.assertEqual(self.central()['reason'], 'source_capture_stale')

    def test_control_human_takeover_is_still_denied(self):
        self.request['control']['work_mode'] = 'takeover'
        self.assertEqual(self.central()['reason'], 'human_stop_or_takeover')

    def test_unknown_action_result_is_still_denied(self):
        self.request['action_state'] = 'unknown'
        self.assertEqual(self.central()['reason'], 'work_authority_write_not_new')

    def test_real_latest_hamle_to_cc_removal_is_still_denied(self):
        self.request['source']['to'] = ['client@example.test']
        self.assertEqual(self.central()['reason'], 'hamle_absent_from_actual_latest_to_cc')

    def test_unknown_sender_or_new_cc_is_still_denied(self):
        self.request['source']['sender'] = 'stranger@example.test'
        self.assertEqual(self.central()['reason'], 'sender_not_authorized')
        self.request['source']['sender'] = 'client@example.test'
        self.request['source']['cc'].append('stranger@example.test')
        self.assertEqual(self.central()['reason'], 'latest_recipient_out_of_scope')

    def test_bcc_old_quoted_header_and_own_message_cannot_be_work_basis(self):
        for patch in ({'actual_latest': False}, {'own_or_automatic': True}):
            previous = copy.deepcopy(self.request['source'])
            self.request['source'].update(patch)
            self.assertEqual(self.central()['reason'], 'latest_source_unverified')
            self.request['source'] = previous

    def test_grant_cannot_bypass_commercial_strategy_or_social_human_gates(self):
        for risk in ('commercial', 'complaint_strategy', 'social_planning'):
            self.request['risk_area'] = risk
            self.assertFalse(self.central()['allowed'])

    def test_grant_cannot_bypass_changed_executable_payload(self):
        self.request['action_payload']['body'] = 'changed'
        self.assertEqual(self.central()['reason'], 'prepared_action_payload_changed')

    def test_grant_cannot_bypass_qa_or_exact_final_artifact_release(self):
        delivery(self.request)
        self.assertTrue(self.central()['allowed'], self.central())
        self.request['qa']['accepted'] = False
        self.assertEqual(self.central()['reason'], 'artifact_qa_not_accepted')

    def test_all_routine_action_types_still_pass_the_central_exact_gate(self):
        for action in ACTIONS:
            self.policy, self.scope, self.request, self.issuer = fixture()
            if action == 'deliver_file': delivery(self.request)
            else: select_action(self.request, action)
            self.assertTrue(self.central()['allowed'], (action, self.central()))

    def test_revoked_grant_is_terminal_and_history_preserved(self):
        grant = self.request['work_authority_grant']
        revoker = {'verified': True, 'authenticated': True, 'actor': 'ozge@gamechangers.com.tr',
                   'authorized_catalog_issuers': sorted(ISSUERS), 'decision': 'revoke_visible_stop_work_authority',
                   'grant_id': grant['grant_id'], 'evidence_ref': 'synthetic-visible-stop', 'issued_at': stamp()}
        revoked = revoke_work_authority(grant, revoker, now=NOW)
        self.assertEqual(grant['status'], 'active')
        self.assertEqual(revoked['revision'], 1)
        self.assertEqual(len(revoked['revocations']), 1)
        self.assertEqual(revoke_work_authority(revoked, revoker, now=NOW), revoked)
        self.request['work_authority_grant'] = revoked
        self.assertEqual(self.decision()['reason'], 'work_authority_revoked')

    def test_revoked_or_invalid_grant_cannot_fallback_to_fresh_authority(self):
        self.request['work_authority_grant']['status'] = 'revoked'
        fresh_authority(self.scope, self.request)
        self.request['fresh_work_authorization']['actor'] = AHMET
        self.assertEqual(self.central()['reason'], 'work_authority_revoked')

    def test_existing_fresh_authority_path_remains_unchanged_without_work_grant(self):
        self.request.pop('work_authority_grant')
        self.assertEqual(self.central()['reason'], 'delayed_action_requires_fresh_work_authorization')
        fresh_authority(self.scope, self.request)
        self.request['fresh_work_authorization']['actor'] = AHMET
        self.assertTrue(self.central()['allowed'])

    def test_even_immediate_action_cannot_ignore_present_revoked_grant(self):
        self.request['source']['received_at'] = stamp(-10)
        self.request['work_authority_grant']['status'] = 'revoked'
        self.assertFalse(self.central()['allowed'])

    def test_budget_exhausted_unknown_or_expired_always_holds(self):
        for patch in ({'remaining_microdollars': 0}, {'overrun_hold': True}, {'verified': False},
                      {'expires_at': stamp(-1)}, {'observed_at': stamp(-301)}):
            prior = copy.deepcopy(self.request['budget_authorization'])
            self.request['budget_authorization'].update(patch)
            self.assertEqual(self.decision()['reason'], 'work_authority_budget_unavailable')
            self.request['budget_authorization'] = prior

    def test_new_budget_policy_never_renews_old_grant(self):
        self.request['budget_authorization']['policy_id'] = 'new-period'
        self.assertEqual(self.decision()['reason'], 'work_authority_budget_policy_changed')

    def test_delegation_revocation_or_refresh_requires_explicit_new_grant(self):
        self.request['delegation']['revoked'] = True
        self.assertEqual(self.decision()['reason'], 'work_authority_delegation_invalid')
        self.request['delegation']['revoked'] = False
        self.request['delegation']['evidence_ref'] = 'new-delegation'
        self.assertEqual(self.decision()['reason'], 'work_authority_delegation_changed')

    def test_fresh_authorization_cannot_be_disguised_as_work_grant(self):
        fresh_authority(self.scope, self.request)
        self.request['work_authority_grant'] = self.request['fresh_work_authorization']
        self.assertEqual(self.decision()['reason'], 'work_authority_grant_invalid')

    def test_unaccepted_enabled_policy_is_rejected_in_central_scope(self):
        self.policy['acceptance']['authenticated'] = False
        self.assertEqual(self.central()['reason'], 'work_authority_explicit_acceptance_missing')

    def test_quality_brief_and_work_brief_must_agree(self):
        self.request['quality_brief_sha256'] = 'f' * 64
        self.assertEqual(self.decision()['reason'], 'work_authority_quality_brief_mismatch')


if __name__ == '__main__':
    unittest.main()
