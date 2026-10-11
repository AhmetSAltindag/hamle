"""Synthetic unit tests; no real customer messages, credentials, or API calls."""
import copy
import unittest
from datetime import datetime, timezone

from autonomy_policy import HAMLE
from v11_intake_plan import (IntakeError, amend_brief, plan_intake, record_producer_question,
                             intake_requirements_digest, amendment_proposal_digest)

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def fixture():
    customer = {'email': 'client@example.test', 'verified': True, 'side': 'customer',
                'can_request': True, 'can_receive': True, 'service_ids': ['social']}
    agency = {'email': 'social@example.test', 'verified': True, 'side': 'agency',
              'can_request': True, 'can_receive': True, 'can_relay': True, 'can_amend': True,
              'service_ids': ['social'], 'agency_roles': ['social_manager'],
              'basecamp_verified': True, 'project_member': True, 'basecamp_id': 101}
    principal = {**agency, 'email': 'principal@example.test', 'agency_roles': ['principal'], 'basecamp_id': 102}
    strategy = {**agency, 'email': 'strategy@example.test', 'agency_roles': ['strategy_owner'], 'basecamp_id': 103}
    designer = {**agency, 'email': 'designer@example.test', 'agency_roles': [], 'basecamp_id': 104,
                'production_kinds': ['graphic']}
    animator = {**designer, 'email': 'animator@example.test', 'basecamp_id': 105,
                'production_kinds': ['animation', 'video']}
    catalog = {'schema_version': 1, 'verified': True, 'version': 'synthetic-1',
               'verification_ref': 'synthetic-catalog', 'brand_id': 'synthetic_brand',
               'contacts': [customer, agency, principal, strategy, designer, animator],
               'basecamp': {'verified': True, 'verification_ref': 'synthetic-project',
                            'account_id': 11, 'project_id': 22, 'customer_visible': False},
               'routes': [{'service_id': 'social', 'category_key': 'social_static',
                           'verified': True, 'verification_ref': 'synthetic-list', 'list_id': 33,
                           'category_name': 'Synthetic category',
                           'allowed_kinds': ['graphic', 'animation', 'video'],
                           'assignees_by_kind': {'graphic': ['designer@example.test'],
                                                'animation': ['animator@example.test'],
                                                'video': ['animator@example.test']}}]}
    source = {'channel': 'email', 'verified': True, 'headers_origin': 'provider',
              'latest_in_thread': True, 'evidence_ref': 'synthetic-email', 'message_id': 'm1',
              'thread_id': 't1', 'fingerprint': 'a' * 64, 'observed_at': NOW.isoformat(),
              'automatic': False, 'own_message': False, 'human_takeover': False,
              'sender': customer['email'], 'to': [HAMLE], 'cc': [strategy['email']],
              'origin': {'kind': 'direct_email'}}
    requirements = {'brand_id': 'synthetic_brand', 'service_id': 'social',
                    'category_key': 'social_static', 'risk_area': 'routine',
                    'title': 'Synthetic post', 'objective': 'Share an approved service update.',
                    'audience': 'Existing members', 'deadline': '2026-10-15 12:00 Europe/Istanbul',
                    'assets_ref': 'synthetic-assets-v2',
                    'deliverables': [{'id': 'feed', 'kind': 'graphic', 'quantity': 1,
                                      'formats': ['image/png'], 'specification': '1080x1080 feed, approved copy'}],
                    'agreement': {'verified': True, 'source_message_id': 'm1', 'evidence_ref': 'synthetic-facts'}}
    requirements['agreement']['requirements_sha256'] = intake_requirements_digest(requirements)
    return source, requirements, catalog


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.source, self.requirements, self.catalog = fixture()

    def plan(self):
        return plan_intake(self.source, self.requirements, self.catalog, now=NOW)

    def held(self, reason):
        result = self.plan()
        self.assertEqual((result['status'], result['reason']), ('held', reason), result)
        self.assertEqual(result['intents'], [])

    def test_complete_brief_goes_to_verified_internal_route(self):
        result = self.plan()
        self.assertEqual(result['status'], 'ready_internal_task', result)
        intent, = result['intents']
        self.assertEqual(intent['payload']['target'], {'account_id': 11, 'project_id': 22, 'list_id': 33})
        self.assertEqual(intent['payload']['assignee_ids'], [104])
        self.assertEqual(intent['payload']['subscriber_ids'], [101, 102, 103])
        self.assertFalse(intent['live_enabled'])
        self.assertEqual((result['network_writes'], result['model_calls']), (0, 0))

    def test_reply_all_preserves_customer_thread_and_actual_cc(self):
        envelope = self.plan()['brief']['reply_envelope']
        self.assertEqual(envelope['to'], ['client@example.test'])
        self.assertEqual(envelope['cc'], ['strategy@example.test'])
        self.assertEqual(envelope['thread_id'], 't1')
        self.assertNotIn('principal@example.test', envelope['to'] + envelope['cc'])

    def test_ambiguous_output_asks_instead_of_inventing_animation(self):
        self.requirements['deliverables'][0]['kind'] = 'design_or_video'
        result = self.plan()
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['intents'][0]['kind'], 'clarify_brief')
        self.assertIn('deliverables.feed.kind', [q['field'] for q in result['questions']])

    def test_missing_brief_outputs_does_not_expand_monthly_volume(self):
        self.requirements['deliverables'] = []
        self.catalog['monthly_volume'] = 30
        result = self.plan()
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertIsNone(result['brief'])
        self.assertNotIn('30', result['questions'][0]['question'])

    def test_video_duration_is_required(self):
        self.requirements['deliverables'][0]['kind'] = 'video'
        result = self.plan()
        self.assertIn('deliverables.feed.duration_seconds', [q['field'] for q in result['questions']])

    def test_quoted_header_does_not_grant_authority(self):
        self.source['headers_origin'] = 'quoted'
        self.held('latest_provider_email_required')

    def test_bcc_or_old_hamle_header_is_not_entry(self):
        self.source.update(to=['social@example.test'], cc=[], bcc=[HAMLE], quoted_to=[HAMLE])
        self.held('hamle_not_actual_to_or_cc')

    def test_older_thread_message_is_not_entry(self):
        self.source['latest_in_thread'] = False
        self.held('latest_provider_email_required')

    def test_own_message_is_not_new_work(self):
        self.source['sender'] = HAMLE
        self.held('own_or_automatic_message')

    def test_auto_reply_is_not_new_work(self):
        self.source['automatic'] = True
        self.held('own_or_automatic_message')

    def test_human_takeover_blocks_planning(self):
        self.source['human_takeover'] = True
        self.held('human_takeover_blocks_planning')

    def test_unknown_cc_is_held_and_never_silently_dropped(self):
        self.source['cc'].append('unknown@example.test')
        self.held('reply_participant_unverified')

    def test_unknown_category_does_not_guess_social_list(self):
        self.requirements['category_key'] = 'outdoor'
        self.requirements['agreement']['requirements_sha256'] = intake_requirements_digest(self.requirements)
        self.held('category_route_missing_or_ambiguous')

    def test_unverified_producer_id_is_not_guessed_from_name(self):
        self.catalog['contacts'][4].pop('basecamp_id')
        self.held('basecamp_person_identity_ambiguous')

    def test_customer_cannot_be_subscribed_to_internal_task(self):
        self.catalog['contacts'][1]['side'] = 'customer'
        self.held('basecamp_person_unverified')

    def test_customer_visible_project_is_held(self):
        self.catalog['basecamp']['customer_visible'] = True
        self.held('internal_basecamp_project_unverified')

    def test_unagreed_model_classification_is_not_verified_brief(self):
        self.requirements['agreement']['verified'] = False
        self.held('brief_agreement_unverified')

    def test_commercial_and_strategy_keep_named_escalation(self):
        self.requirements['risk_area'] = 'commercial'
        self.held('ahmet_commercial_decision_required')
        self.requirements['risk_area'] = 'complaint_strategy'
        self.held('cansu_and_ahmet_decision_required')

    def test_social_plan_does_not_pretend_ozge_consulted(self):
        self.requirements['risk_area'] = 'social_planning'
        self.held('ozge_collaboration_required')

    def relay(self):
        self.source['sender'] = 'social@example.test'
        self.source['origin'] = {'kind': 'agency_relay', 'medium': 'whatsapp',
                                 'original_actor_email': 'client@example.test',
                                 'original_actor_name': 'Synthetic Client',
                                 'attested_by': 'social@example.test',
                                 'verified': True, 'evidence_ref': 'synthetic-relay-attestation'}

    def test_attested_agency_relay_can_be_brief_entry(self):
        self.relay()
        result = self.plan()
        self.assertEqual(result['status'], 'ready_internal_task', result)
        self.assertEqual(result['brief']['origin']['medium'], 'whatsapp')

    def test_relay_does_not_add_quoted_customer_to_actual_loop(self):
        self.relay()
        envelope = self.plan()['brief']['reply_envelope']
        self.assertEqual(envelope['to'], ['social@example.test'])
        self.assertNotIn('client@example.test', envelope['to'] + envelope['cc'])

    def test_relay_requires_authorized_original_requester(self):
        self.relay()
        self.source['origin']['original_actor_email'] = 'unknown@example.test'
        self.held('relay_original_requester_unverified')

    def test_relay_requires_actual_agency_attestation(self):
        self.relay()
        self.source['origin']['attested_by'] = 'principal@example.test'
        self.held('relay_attestation_missing')

    def test_second_output_requires_agreed_production_sequence(self):
        self.requirements['deliverables'].append({'id': 'motion', 'kind': 'animation', 'quantity': 1,
            'formats': ['video/mp4'], 'duration_seconds': 10, 'specification': '1080x1920 motion'})
        self.requirements['agreement']['requirements_sha256'] = intake_requirements_digest(self.requirements)
        self.held('production_sequence_unverified')
        self.catalog['routes'][0].update(sequence_verified=True, production_stages=[
            {'id': 'design', 'kinds': ['graphic'], 'depends_on': []},
            {'id': 'motion', 'kinds': ['animation'], 'depends_on': ['design']}])
        result = self.plan()
        self.assertEqual(result['status'], 'ready_internal_task', result)
        payload = result['intents'][0]['payload']
        self.assertEqual(payload['assignee_ids'], [104])
        self.assertEqual(payload['production_stages'][1]['assignee_ids'], [105])
        self.assertEqual(payload['production_stages'][1]['depends_on'], ['design'])

    def test_identical_source_and_plan_has_same_intent_identity(self):
        self.assertEqual(self.plan()['intents'][0]['intent_id'], self.plan()['intents'][0]['intent_id'])

    def test_producer_email_not_required_for_verified_basecamp_id_assignment(self):
        self.catalog['basecamp_people'] = {str(p['basecamp_id']): copy.deepcopy(p)
            for p in self.catalog['contacts'] if p.get('basecamp_verified')}
        self.catalog['contacts'] = self.catalog['contacts'][:4]
        for person in self.catalog['basecamp_people'].values():
            person.pop('email')
        route = self.catalog['routes'][0]
        route.pop('assignees_by_kind')
        route['assignee_ids_by_kind'] = {'graphic': [104], 'animation': [105], 'video': [105]}
        result = self.plan()
        self.assertEqual(result['status'], 'ready_internal_task', result)
        self.assertEqual(result['intents'][0]['payload']['assignee_ids'], [104])

    def test_string_service_permission_cannot_match_by_substring(self):
        self.catalog['contacts'][0]['service_ids'] = 'social_annual'
        self.held('contact_scope_list_invalid')

    def test_production_kind_requires_exact_array(self):
        self.catalog['contacts'][4]['production_kinds'] = 'not_graphic'
        self.held('contact_scope_list_invalid')

    def test_duplicate_basecamp_person_identity_is_held(self):
        self.catalog['contacts'][5]['basecamp_id'] = 104
        self.held('basecamp_person_identity_ambiguous')

    def test_modified_requirements_cannot_reuse_old_agreement(self):
        self.requirements['objective'] = 'A materially different objective.'
        self.held('brief_agreement_unverified')

    def test_exact_multi_output_sequence_matches_without_inventing_video_output(self):
        self.requirements['deliverables'].append({'id': 'motion', 'kind': 'animation', 'quantity': 1,
            'formats': ['video/mp4'], 'duration_seconds': 10, 'specification': '1080x1920 motion'})
        self.requirements['agreement']['requirements_sha256'] = intake_requirements_digest(self.requirements)
        self.catalog['routes'][0].update(sequence_verified=True, production_sequences={
            'animation+graphic': [{'id': 'design', 'kinds': ['graphic'], 'depends_on': []},
                                  {'id': 'motion', 'kinds': ['animation'], 'depends_on': ['design']}],
            'graphic+video': [{'id': 'design', 'kinds': ['graphic'], 'depends_on': []},
                              {'id': 'video', 'kinds': ['video'], 'depends_on': ['design']}]})
        result = self.plan()
        self.assertEqual(result['status'], 'ready_internal_task', result)
        self.assertEqual(result['brief']['binding']['production_stages'][1]['kinds'], ['animation'])

    def test_motion_route_cannot_take_standalone_graphic(self):
        self.catalog['routes'][0]['required_any_kinds'] = ['animation', 'video']
        self.held('route_required_kind_missing')

    def test_motion_route_can_take_graphic_then_motion_in_one_list(self):
        self.requirements['deliverables'].append({'id': 'motion', 'kind': 'animation', 'quantity': 1,
            'formats': ['video/mp4'], 'duration_seconds': 10, 'specification': '1080x1920 motion'})
        self.requirements['agreement']['requirements_sha256'] = intake_requirements_digest(self.requirements)
        self.catalog['routes'][0].update(required_any_kinds=['animation', 'video'],
            sequence_verified=True, production_sequences={
                'animation+graphic': [{'id': 'design', 'kinds': ['graphic'], 'depends_on': []},
                                      {'id': 'motion', 'kinds': ['animation'], 'depends_on': ['design']}]})
        result = self.plan()
        self.assertEqual(result['status'], 'ready_internal_task', result)
        self.assertEqual(result['intents'][0]['payload']['target']['list_id'], 33)
        self.assertEqual(result['intents'][0]['payload']['assignee_ids'], [104])
        self.assertEqual(result['brief']['binding']['production_stages'][1]['assignee_ids'], [105])

    def test_required_any_kinds_cannot_use_string_membership(self):
        self.catalog['routes'][0]['required_any_kinds'] = 'graphic'
        self.held('route_required_kinds_invalid')

    def test_required_any_kinds_must_belong_to_service(self):
        self.catalog['routes'][0]['required_any_kinds'] = ['text']
        self.held('route_required_kinds_invalid')


class AmendmentTests(unittest.TestCase):
    def setUp(self):
        source, requirements, self.catalog = fixture()
        self.work = {'work_id': 'work-1', 'task_id': 44,
                     'brief': plan_intake(source, requirements, self.catalog, now=NOW)['brief'],
                     'qa': {'old': True}, 'release': {'old': True}, 'pending_delivery': {'old': True}}
        source.update(sender='social@example.test', message_id='m2', fingerprint='b' * 64)
        self.change = {'id': 'change-1', 'expected_brief_version': 1, 'source': source,
                       'reason': 'Agency owner confirmed the new wording.',
                       'classification': {'verified': True, 'change_id': 'change-1',
                                          'risk_area': 'routine', 'evidence_ref': 'synthetic-risk'},
                       'patch': {'objective': 'Communicate the revised approved service update.'}}
        self.question = {'id': 'question-1', 'text': 'Which approved logo version should be used?',
                         'source': {'channel': 'basecamp', 'verified': True,
                                    'evidence_ref': 'synthetic-question', 'actor_email': 'designer@example.test',
                                    'actor_id': 104, 'account_id': 11, 'project_id': 22, 'list_id': 33, 'task_id': 44}}
        self.change['classification']['proposal_sha256'] = amendment_proposal_digest(self.change)

    def amend(self, work=None):
        return amend_brief(work or self.work, self.change, self.catalog, now=NOW)

    def test_amendment_versions_brief_and_invalidates_exact_previous_delivery(self):
        result = self.amend()
        work = result['work']
        self.assertEqual(work['brief']['version'], 2)
        self.assertFalse(work['delivery_ready'])
        self.assertNotIn('qa', work)
        self.assertNotIn('release', work)
        self.assertNotIn('pending_delivery', work)
        self.assertEqual(work['invalidated_delivery_evidence'][0]['evidence']['qa'], {'old': True})
        self.assertEqual(work['brief']['change_history'][0]['actor'], 'social@example.test')
        self.assertTrue(result['requires_atomic_queue_invalidation'])
        self.assertEqual(result['intents'][0]['payload']['target']['task_id'], 44)
        self.assertEqual(self.work['brief']['version'], 1)

    def test_duplicate_change_does_not_repeat_write_or_increment_version(self):
        first = self.amend()
        again = self.amend(first['work'])
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['intents'], [])
        self.assertEqual(again['work']['brief']['version'], 2)

    def test_same_change_id_with_different_content_is_conflict(self):
        first = self.amend()
        self.change['patch']['objective'] = 'A different objective with reused event ID.'
        with self.assertRaisesRegex(IntakeError, 'amendment_identity_payload_conflict'):
            self.amend(first['work'])

    def test_amendment_rechecks_commercial_strategy_classification(self):
        self.change['classification']['risk_area'] = 'commercial'
        with self.assertRaisesRegex(IntakeError, 'ahmet_commercial_decision_required'):
            self.amend()

    def test_unclassified_amendment_is_held(self):
        self.change.pop('classification')
        with self.assertRaisesRegex(IntakeError, 'amendment_risk_classification_unverified'):
            self.amend()

    def test_authorized_basecamp_amendment_preserves_actual_email_loop(self):
        self.change['source'] = {**self.question['source'], 'actor_id': 101,
                                 'actor_email': 'social@example.test', 'human_takeover': False,
                                 'observed_at': NOW.isoformat()}
        self.change['classification']['proposal_sha256'] = amendment_proposal_digest(self.change)
        original_envelope = copy.deepcopy(self.work['brief']['reply_envelope'])
        result = self.amend()
        self.assertEqual(result['work']['brief']['reply_envelope'], original_envelope)
        self.assertEqual(result['work']['brief']['source_message_id'], 'm1')
        self.assertEqual(result['work']['brief']['change_history'][0]['source_channel'], 'basecamp')

    def test_concurrent_old_version_is_held(self):
        self.change['expected_brief_version'] = 2
        with self.assertRaisesRegex(IntakeError, 'brief_version_conflict'):
            self.amend()

    def test_customer_cannot_make_agency_amendment(self):
        self.change['source']['sender'] = 'client@example.test'
        with self.assertRaisesRegex(IntakeError, 'brief_amendment_actor_not_permitted'):
            self.amend()

    def test_amendment_cannot_reroute_task_by_patch(self):
        self.change['patch'] = {'binding': {'project_id': 999}}
        self.change['classification']['proposal_sha256'] = amendment_proposal_digest(self.change)
        with self.assertRaisesRegex(IntakeError, 'amendment_field_not_permitted'):
            self.amend()

    def test_amendment_cannot_target_another_thread(self):
        self.change['source']['thread_id'] = 'another-thread'
        with self.assertRaisesRegex(IntakeError, 'amendment_thread_mismatch'):
            self.amend()

    def test_producer_question_holds_delivery_without_external_echo(self):
        result = record_producer_question(self.work, self.question, self.catalog)
        self.assertEqual(result['work']['status'], 'awaiting_internal_clarification')
        self.assertFalse(result['work']['delivery_ready'])
        self.assertEqual(result['needs_response_from_basecamp_ids'], [101, 102, 103])
        self.assertEqual(result['intents'], [])
        self.assertEqual(result['work']['brief']['version'], 1)
        self.assertNotIn('qa', result['work'])
        self.assertNotIn('pending_delivery', result['work'])

    def test_unassigned_producer_cannot_insert_question_as_assigned_designer(self):
        self.question['source'].update(actor_email='animator@example.test', actor_id=105)
        with self.assertRaisesRegex(IntakeError, 'producer_question_actor_unverified'):
            record_producer_question(self.work, self.question, self.catalog)

    def test_question_cannot_cross_work_binding(self):
        self.question['source']['task_id'] = 45
        with self.assertRaisesRegex(IntakeError, 'basecamp_event_work_mismatch'):
            record_producer_question(self.work, self.question, self.catalog)

    def test_question_is_deduplicated_and_can_be_resolved_by_agency_amendment(self):
        first = record_producer_question(self.work, self.question, self.catalog)
        again = record_producer_question(first['work'], self.question, self.catalog)
        self.assertTrue(again['duplicate'])
        self.change['resolves_question_ids'] = ['question-1']
        self.change['classification']['proposal_sha256'] = amendment_proposal_digest(self.change)
        revised = self.amend(again['work'])
        self.assertEqual(revised['work']['brief']['unresolved_questions'], [])

    def test_unknown_question_resolution_is_rejected(self):
        self.change['resolves_question_ids'] = ['question-never-seen']
        self.change['classification']['proposal_sha256'] = amendment_proposal_digest(self.change)
        with self.assertRaisesRegex(IntakeError, 'question_resolution_unbound'):
            self.amend()

    def test_edited_comment_reusing_event_id_is_conflict(self):
        first = record_producer_question(self.work, self.question, self.catalog)
        self.question['text'] = 'Edited provider comment needs its own revision event ID.'
        with self.assertRaisesRegex(IntakeError, 'question_identity_payload_conflict'):
            record_producer_question(first['work'], self.question, self.catalog)

    def test_id_only_producer_question_needs_no_guessed_email(self):
        source, requirements, catalog = fixture()
        catalog['basecamp_people'] = {str(p['basecamp_id']): copy.deepcopy(p)
            for p in catalog['contacts'] if p.get('basecamp_verified')}
        catalog['contacts'] = catalog['contacts'][:4]
        for person in catalog['basecamp_people'].values():
            person.pop('email')
        catalog['routes'][0]['assignee_ids_by_kind'] = {'graphic': [104]}
        brief = plan_intake(source, requirements, catalog, now=NOW)['brief']
        work = {'work_id': 'work-1', 'task_id': 44, 'brief': brief}
        self.question['source'].pop('actor_email')
        result = record_producer_question(work, self.question, catalog)
        self.assertEqual(result['work']['status'], 'awaiting_internal_clarification')

    def test_recording_question_does_not_undo_human_takeover_status(self):
        self.work['status'] = 'human_takeover'
        result = record_producer_question(self.work, self.question, self.catalog)
        self.assertEqual(result['work']['status'], 'human_takeover')

    def test_changed_patch_cannot_reuse_prior_routine_classification(self):
        self.change['patch']['objective'] = 'Issue a new commercial price promise.'
        with self.assertRaisesRegex(IntakeError, 'amendment_risk_classification_unverified'):
            self.amend()


if __name__ == '__main__':
    unittest.main()
