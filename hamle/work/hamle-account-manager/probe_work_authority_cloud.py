"""Explicitly authorized synthetic Firestore stop proof; NO provider/model calls.

Never replay: an exclusive local attempt record is created before any cloud write.
This uses fabricated mail/brief evidence ONLY to exercise durable state fencing;
it proves no real customer intake, scope, delivery or user identity verification.
"""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import uuid

from autonomy_policy import scope_digest
from cloud_store import FirestoreBackend, OperationsStore, StoreError
from import_cloud_credentials import credentials
from v11_work_authority import (ACTIONS, ISSUERS, TrustedIntakeEvidence, build_work_authority_binding,
    default_work_authority_policy, policy_terms_digest)
from v11_work_authority_store import WorkAuthorityStore, TrustedWorkStop

OUT = Path(__file__).resolve().parents[2] / 'outputs/wat-pilot-20261010/real-output-shadow'


def save(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2); handle.write('\n')


def main():
    if sys.argv[1:] != ['--authorized-synthetic-firestore-stop-proof']:
        raise ValueError('explicit_synthetic_probe_flag_required')
    now = datetime.now(timezone.utc)
    run = 'synthetic-authority-probe-' + uuid.uuid4().hex
    save(OUT/'authority-cloud-probe.attempt.json', {'run_id':run,'started_at':now.isoformat(),
        'customer_data':False,'provider_writes':0,'model_calls':0,'automatic_replay_allowed':False})
    backend = FirestoreBackend(opt_in='yes', credentials=credentials())
    ops = OperationsStore(backend)
    store = WorkAuthorityStore(ops); queue = store.queue()
    work = None
    result = {'run_id':run,'evidence_kind':'real_firestore_synthetic_state_test',
              'synthetic_source_not_real_mail':True,'no_customer_authority_claim':True,
              'provider_writes':0,'model_calls':0,'global_control_changed':False,'wat_live_enabled':False}
    try:
        control = ops.control(); generation = control['generation']
        if control['mode'] != 'review_open': raise ValueError('global_control_not_review_open_no_changes')
        work = queue.bind_work(brand_id='wat_mobilite', thread_id=run,
            account_id=5898865, project_id=49184053, list_id=1,
            service_id='synthetic_authority_stop_probe', expected_generation=generation)
        wid = work['work_id']; issued = datetime.now(timezone.utc)
        expiry = '2026-10-12T23:59:59+03:00'
        policy = default_work_authority_policy('wat_mobilite','synthetic_authority_stop_probe')
        policy.update(enabled=True,status='accepted',actions=['acknowledge'])
        policy['acceptance']={'verified':True,'authenticated':True,'actor':'ahmet@gamechangers.com.tr',
            'decision':'accept_visible_stop_work_authority','evidence_ref':'synthetic-fixture:'+run,
            'accepted_at':(issued-timedelta(seconds=5)).isoformat(),'expires_at':expiry,
            'synthetic_fixture_only':True}
        policy['acceptance']['policy_terms_sha256']=policy_terms_digest(policy)
        scope={'schema_version':1,'brand_id':'wat_mobilite','enabled':True,
            'authority':{'verified_by':'ahmet@gamechangers.com.tr','verified_at':(issued-timedelta(seconds=5)).isoformat(),
                'expires_at':expiry,'evidence_ref':'synthetic-scope-fixture:'+run},
            'customer_contacts':['requester@example.test'],'agency_contacts':sorted(ISSUERS),
            'allowed_cc_contacts':['observer@example.test'],'agency_owner':'ahmet@gamechangers.com.tr',
            'basecamp':{'account_id':5898865,'project_id':49184053,'list_ids':[1], 'customer_visible':False,
                       'verification_ref':'synthetic-no-basecamp-target-claim:'+run},
            'services':[{'service_id':'synthetic_authority_stop_probe','live_enabled':True,
                'actions':['acknowledge'],'revision_limit':0,'list_id':1,'assignee_ids':[],
                'immediate_action_window_seconds':300,'work_authority_policy':policy}],
            'synthetic_fixture_only':True}
        source={'actual_latest':True,'own_or_automatic':False,'provider_evidence_ref':'synthetic-no-real-mail:'+run,
                'message_id':'synthetic-message-'+run,'thread_id':run,'fingerprint':scope_digest(run),
                'sender':'requester@example.test','to':['hamle@gamechangers.com.tr'],'cc':['observer@example.test'],
                'received_at':issued.isoformat(),'captured_at':issued.isoformat()}
        request={'brand_id':'wat_mobilite','service_id':'synthetic_authority_stop_probe','work_id':wid,
            'action':'acknowledge','action_state':'new','risk_area':'routine','open_escalation':False,'revision_index':0,
            'control':{'work_id':wid,'global_mode':'routine_enabled','work_mode':'active',
                'global_generation':generation,'work_generation':0,'observed_at':issued.isoformat()},
            'source':source,'work_brief_sha256':scope_digest({'synthetic':run}),
            'work_binding':{k:work[k] for k in ('work_id','brand_id','thread_id','account_id','project_id','list_id','service_id','task_id')},
            'delegation':{'actor':'ahmet@gamechangers.com.tr','evidence_ref':'synthetic-parent-derivation:'+run,
                'revoked':False,'work_id':wid,'brand_id':'wat_mobilite','service_id':'synthetic_authority_stop_probe',
                'thread_id':run,'scope_digest':scope_digest(scope),'global_generation':generation,'work_generation':0,
                'issued_at':issued.isoformat(),'expires_at':expiry,'actions':['acknowledge'],
                'basis':'accepted_brand_policy_derivation','parent_policy_digest':scope_digest(policy),
                'parent_acceptance_ref':policy['acceptance']['evidence_ref'],'source_fingerprint':source['fingerprint']}}
        intake=TrustedIntakeEvidence(adapter_id='hamle-authenticated-intake-v1',provider_identity='hamle@gamechangers.com.tr',
            evidence_ref='synthetic-no-provider-intake:'+run, classification_evidence_ref='synthetic-routine-fixture:'+run,
            binding_sha256=scope_digest(build_work_authority_binding(scope,request)), parent_policy_digest=scope_digest(policy),
            observed_at=issued.isoformat())
        record=store.install_grant(scope,request,intake=intake)
        read=store.observe_work(wid)
        if read['record']['grant']!=record['grant']:raise ValueError('grant_readback_mismatch')
        queue.enqueue_message(wid,message_id=source['message_id'],fingerprint=source['fingerprint'],source_order=1,expected_generation=generation)
        claim=queue.claim_next(wid,worker_id='synthetic-cloud-authority-probe',expected_generation=generation)
        stop=TrustedWorkStop(work_id=wid,actor='ahmet@gamechangers.com.tr',identity_subject='synthetic-admin-probe-not-user-click',
            evidence_ref='authorized-synthetic-stop-fixture:'+run,observed_at=datetime.now(timezone.utc).isoformat())
        stopped=store.stop_work(wid,stop,expected_work_generation=0)
        try:
            store.begin_authorized_write(claim,scope,request,action_key='must-never-start')
            raise ValueError('stale_action_not_refused')
        except StoreError as exc:
            if exc.code!='work_stopped_or_generation_changed':raise
            refusal=exc.code
        read=store.observe_work(wid)
        second=WorkAuthorityStore(OperationsStore(backend)).observe_work(wid)
        if read['record']!=second['record']:raise ValueError('restart_readback_mismatch')
        final_control=ops.control()
        if final_control!=control:raise ValueError('global_control_changed_during_probe')
        result.update(status='passed',work_id=wid,private_project_id=49184053,
            task_created=False,list_id_is_synthetic_placeholder=True,grant_id=record['grant']['grant_id'],
            grant_before='active',grant_after=read['record']['grant']['status'],
            work_mode=read['work']['mode'],work_generation=read['work']['work_generation'],
            pending_lease_cleared=read['work']['lease'] is None,stale_dispatch_refused=refusal,
            global_generation=generation,restarted_store_readback_matches=True,
            action_started=False,expires_at=record['grant']['expires_at'],
            stopped_at=read['record']['last_stop']['at'],finished_at=datetime.now(timezone.utc).isoformat())
        save(OUT/'authority-cloud-probe.json',result)
        print(json.dumps(result))
    except Exception as exc:
        # No blind write retries. Preserve identifiers so root can reconcile.
        result.update(status='held',work_id=work['work_id'] if work else None,
                      reason=getattr(exc,'code',type(exc).__name__),needs_read_reconciliation=True)
        save(OUT/'authority-cloud-probe-held.json',result)
        print(json.dumps(result)); raise SystemExit(2)
    finally:
        backend.close()


if __name__=='__main__':main()
