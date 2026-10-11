"""Scored delivery still passes every ordinary action-authority boundary."""
import copy
from datetime import datetime, timedelta
import unittest

from autonomy_policy import authorize_action, scope_digest
from test_autonomy_policy import fixture as authority_fixture, delivery
from test_v11_quality_gate import NOW, stamp, fixture as quality_fixture, human_approval
from v11_quality_gate import build_quality_binding, build_quality_note_context, evaluate_quality


def shift(value):
    if isinstance(value, dict): return {k:shift(v) for k,v in value.items()}
    if isinstance(value, list): return [shift(v) for v in value]
    if isinstance(value,str) and value.startswith('2026-10-10T'):
        return (datetime.fromisoformat(value)+timedelta(hours=3)).isoformat()
    return value


def fixture(score='90'):
    scope, request = authority_fixture()
    delivery(request)
    scope, request = shift(scope), shift(request)
    policy, assessment, _, safety = quality_fixture(score)
    policy.update(brand_id=scope['brand_id'],service_id=request['service_id'],policy_id='qa-v1',
                  approval_actors=['producer@example.test'])
    d=scope['services'][0]['delivery']
    d.update(release_mode='scored_quality_v1',quality_policy=policy)
    d.pop('release_actors')
    request.pop('release');request.pop('qa')
    request['delegation']['scope_digest']=scope_digest(scope)
    request.update(quality_assets_ref='assets-v1',quality_brief_sha256='7'*64,quality_manifest_sha256='8'*64)
    binding=build_quality_binding(request,brief_sha256=request['quality_brief_sha256'],
        manifest_sha256=request['quality_manifest_sha256'],scope_sha256=scope_digest(scope))
    assessment.update(binding=copy.deepcopy(binding),policy_digest=scope_digest(policy))
    for group in ('critical_checks','criteria'):
        for record in assessment[group].values():
            record['binding']=copy.deepcopy(binding)
            for observation in record['observations']:
                observation.update(artifact_sha256=binding['sha256'],brief_sha256=binding['brief_sha256'])
    safety['binding']=copy.deepcopy(binding)
    request.update(quality_assessment=assessment,quality_safety=safety,
                   quality_submission={**binding,'verified':True,'actor_id':40,
                                       'evidence_ref':'synthetic-producer-submission','issued_at':stamp(-120)})
    recorded_quality_note(scope, request, binding)
    return scope,request,binding


def recorded_quality_note(scope, request, binding):
    policy=scope['services'][0]['delivery']['quality_policy']
    result=evaluate_quality(policy,request['quality_assessment'],binding,
                            request['quality_safety'],now=NOW,
                            human_approval=request.get('quality_human_approval'))
    if result['route'] != 'delivery_candidate':
        request.pop('quality_note_proof', None)
        return
    context=build_quality_note_context(request,result)
    request['quality_note_proof']={
        'action_id':'synthetic-confirmed-note','payload_sha256':'9'*64,
        'quality_context':context,'quality_context_sha256':scope_digest(context),
        'receipt':{'provider_readback_verified':True,'action_id':'synthetic-confirmed-note',
                   'payload_sha256':'9'*64,'quality_context_sha256':scope_digest(context),
                   'evidence_ref':'synthetic-exact-comment-readback'}}


def approval(scope, request, binding, score):
    policy=scope['services'][0]['delivery']['quality_policy']
    record=human_approval(policy,request['quality_assessment'],binding,score)
    record['actor']='producer@example.test'
    request['quality_human_approval']=record
    recorded_quality_note(scope, request, binding)


class ScoredAuthorityTests(unittest.TestCase):
    def evaluate(self,scope,request):return authorize_action(scope,request,now=NOW)

    def test_90_accepted_without_impersonating_human_release(self):
        s,r,_=fixture()
        self.assertNotIn('release',r)
        self.assertTrue(self.evaluate(s,r)['allowed'],self.evaluate(s,r))
        self.assertFalse(self.evaluate(s,r)['live_enabled_by_this_module'])

    def test_90_without_persisted_score_note_cannot_bypass_coordinator(self):
        s,r,_=fixture();r.pop('quality_note_proof')
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_quality_note_missing')

    def test_note_for_different_task_is_rejected_even_with_updated_context_hash(self):
        s,r,_=fixture();proof=r['quality_note_proof']
        proof['quality_context']['basecamp_target']['task_id']=999
        proof['quality_context_sha256']=scope_digest(proof['quality_context'])
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_quality_note_context_mismatch')

    def test_note_for_different_assessment_is_rejected(self):
        s,r,_=fixture();proof=r['quality_note_proof']
        proof['quality_context']['assessment_sha256']='1'*64
        proof['quality_context_sha256']=scope_digest(proof['quality_context'])
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_quality_note_context_mismatch')

    def test_note_unconfirmed_or_without_evidence_cannot_send(self):
        for change in ({'provider_readback_verified':False},{'evidence_ref':''},
                       {'payload_sha256':'0'*64},{'action_id':'different-note'}):
            with self.subTest(change=change):
                s,r,_=fixture();r['quality_note_proof']['receipt'].update(change)
                self.assertEqual(self.evaluate(s,r)['reason'],'scored_quality_note_readback_missing')

    def test_changed_human_approval_requires_note_for_that_approval(self):
        s,r,b=fixture('85');approval(s,r,b,'85')
        r['quality_human_approval']['evidence_ref']='different-current-approval-record'
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_quality_note_context_mismatch')

    def test_old_confirmed_receipt_cannot_be_rewrapped_for_new_approval(self):
        s,r,b=fixture('85');approval(s,r,b,'85')
        old_receipt=copy.deepcopy(r['quality_note_proof']['receipt'])
        r['quality_human_approval']['evidence_ref']='different-current-approval-record'
        recorded_quality_note(s,r,b)
        r['quality_note_proof']['receipt']=old_receipt
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_quality_note_readback_missing')

    def test_other_project_task_binding_cannot_pass_as_current_note_target(self):
        s,r,b=fixture()
        r['work_binding']['project_id']=999
        r['basecamp_target']['project_id']=999
        recorded_quality_note(s,r,b)
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_quality_note_work_mismatch')

    def test_note_target_must_have_fresh_project_visibility_evidence(self):
        s,r,_=fixture();r['basecamp_target']['verified_at']=stamp(-61)
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_quality_note_target_stale')

    def test_85_requires_real_approval_bound_to_assessment(self):
        s,r,b=fixture('85')
        self.assertFalse(self.evaluate(s,r)['allowed'])
        approval(s,r,b,'85')
        self.assertTrue(self.evaluate(s,r)['allowed'],self.evaluate(s,r))

    def test_80_inclusive_is_human_band(self):
        s,r,b=fixture('80')
        self.assertFalse(self.evaluate(s,r)['allowed'])
        approval(s,r,b,'80')
        self.assertTrue(self.evaluate(s,r)['allowed'],self.evaluate(s,r))

    def test_79_human_cannot_bypass_revision(self):
        s,r,b=fixture('79');approval(s,r,b,'79')
        result=self.evaluate(s,r)
        self.assertFalse(result['allowed']);self.assertEqual(result['reason'],'score_below_human_review_band')

    def test_100_does_not_hide_missing_required_output(self):
        s,r,_=fixture('100')
        r['quality_assessment']['critical_checks']['required_files']['result']='failed'
        self.assertEqual(self.evaluate(s,r)['reason'],'critical_quality_failure')

    def test_unknown_quality_cannot_send(self):
        s,r,_=fixture();r['quality_assessment']['critical_checks']['required_files']=None
        self.assertEqual(self.evaluate(s,r)['reason'],'quality_checks_unassessed')

    def test_submission_from_wrong_producer_rejected(self):
        s,r,_=fixture();r['quality_submission']['actor_id']=999
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_producer_submission_unverified')

    def test_submission_boolean_id_rejected(self):
        s,r,_=fixture();r['quality_submission']['actor_id']=True
        self.assertFalse(self.evaluate(s,r)['allowed'])

    def test_quality_cannot_precede_file_submission(self):
        s,r,_=fixture();r['quality_submission']['issued_at']=stamp(-1)
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_assessment_precedes_submission')

    def test_approved_assets_version_changed(self):
        s,r,_=fixture();r['quality_assets_ref']='different-assets'
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_delivery_assets_changed')

    def test_correct_90_does_not_bypass_latest_to_cc_rule(self):
        s,r,_=fixture();r['source']['to']=[]
        self.assertEqual(self.evaluate(s,r)['reason'],'hamle_absent_from_actual_latest_to_cc')

    def test_correct_90_does_not_bypass_invisible_cc_fresh_participation_boundary(self):
        s,r,_=fixture();r['source']['received_at']=stamp(-901)
        self.assertEqual(self.evaluate(s,r)['reason'],'delayed_action_requires_fresh_work_authorization')

    def test_correct_90_does_not_bypass_takeover(self):
        s,r,_=fixture();r['control']['work_mode']='takeover'
        self.assertEqual(self.evaluate(s,r)['reason'],'human_stop_or_takeover')

    def test_correct_90_does_not_retry_unknown_send(self):
        s,r,_=fixture();r['action_state']='unknown'
        self.assertFalse(self.evaluate(s,r)['allowed'])

    def test_body_change_even_with_new_prepared_hash_invalidates_quality_binding(self):
        s,r,_=fixture();r['action_payload']['body']='changed after review'
        r['prepared_action']['payload_sha256']=scope_digest(r['action_payload'])
        self.assertFalse(self.evaluate(s,r)['allowed'])

    def test_changed_rubric_invalidates_old_scope_delegation(self):
        s,r,_=fixture();s['services'][0]['delivery']['quality_policy']['version']='2'
        self.assertEqual(self.evaluate(s,r)['reason'],'work_delegation_invalid')

    def test_unaccepted_rubric_cannot_activate_mode(self):
        s,r,_=fixture();s['services'][0]['delivery']['quality_policy']['status']='draft'
        self.assertEqual(self.evaluate(s,r)['reason'],'scored_delivery_policy_unaccepted')

    def test_human_approval_from_unlisted_person_does_not_allow_mid_band(self):
        s,r,b=fixture('85');approval(s,r,b,'85');r['quality_human_approval']['actor']='stranger@example.test'
        self.assertEqual(self.evaluate(s,r)['reason'],'quality_human_actor_not_allowed')

    def test_legacy_still_requires_original_human_release(self):
        s,r=authority_fixture();delivery(r)
        self.assertTrue(authorize_action(s,r,now=NOW-timedelta(hours=3))['allowed'])
        r.pop('release')
        self.assertEqual(authorize_action(s,r,now=NOW-timedelta(hours=3))['reason'],'delivery_evidence_missing')

    def test_unknown_release_mode_does_not_silently_fall_back(self):
        s,r,_=fixture();s['services'][0]['delivery']['release_mode']='scored_quality_typo'
        self.assertEqual(self.evaluate(s,r)['reason'],'delivery_release_mode_unknown')


if __name__=='__main__':unittest.main()
