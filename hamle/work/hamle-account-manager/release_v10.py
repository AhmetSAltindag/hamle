"""Maintenance release/run of bounded WAT shadow reader. No new IAM or secrets."""
import argparse
from datetime import datetime, timezone
import json
from provision_metadata import run, ROOT
from import_cloud_credentials import credentials
from cloud_store import FirestoreBackend, OperationsStore, DEADLINE
from v11_wat_shadow_worker import VERSION

OUT=ROOT/'outputs/wat-pilot-20261010/real-output-shadow/cloud-v10'
TAG='europe-west1-docker.pkg.dev/gamechangers-hamle/hamle-runtime/hamle-account-manager:20261010-v10'


def save(path,value,*,exclusive=False):
    with path.open('x' if exclusive else 'w',encoding='utf-8') as stream:
        stream.write(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    path.chmod(0o600)


def main():
    p=argparse.ArgumentParser()
    p.add_argument('phase',choices=('build','build-status','deploy','deploy-control','run','execution-status','read'))
    p.add_argument('--execution');p.add_argument('--case',choices=('a','b','video'));p.add_argument('--attempt',type=int)
    args=p.parse_args();OUT.mkdir(parents=True,exist_ok=True)
    now=datetime.now(timezone.utc);assert now.timestamp()<=DEADLINE
    b=FirestoreBackend(opt_in='yes',credentials=credentials());ops=OperationsStore(b)
    try:
        control=ops.control();budget=ops.budget_status()
        assert control['mode']=='review_open' and not budget['overrun_hold'] and budget['remaining_microdollars']>0
        if args.phase=='read':
            rows=ops._run(lambda tx,now:{c:tx.get(VERSION+'-'+c) for c in ('a','b')})
            video=ops._run(lambda tx,now:tx.get('wat-samet-video-shadow-20261010-v1'))
            report={'read_at':now.isoformat(),'cases':rows,'video':video,'budget':budget,'control_generation':control['generation']}
            save(OUT/'shadow-private-readback.json',report)
            print(json.dumps({'cases':{c:None if r is None else {'state':r['state'],
                'result_sha256':__import__('v11_wat_vision').digest(r['result']) if 'result' in r else None,
                'assessment':r.get('result',{}).get('assessment'),
                'historical_price_detection_check':r.get('result',{}).get('historical_price_detection_check'),
                'failure':r.get('failure_receipt')} for c,r in rows.items()},
                'video':None if video is None else {'state':video['state'],'technical_states':[
                    x['inspection']['media']['technical_status'] for x in video.get('results',[])]},
                'budget':budget},ensure_ascii=False));return
        if args.phase=='build-status':
            old=json.loads((OUT/'release-build.json').read_text())
            r=run(['builds','describe',old['result']['id'],'--region=europe-west1'])
            r={k:r.get(k) for k in ('id','status','createTime','startTime','finishTime','results','failureInfo')}
            save(OUT/'build-verified.json',r);print(json.dumps(r));return
        if args.phase=='execution-status':
            assert args.execution and args.execution.startswith('hamle-account-manager-')
            r=run(['run','jobs','executions','describe',args.execution,'--region=europe-west1'])
            r={'name':r['metadata']['name'],'status':r.get('status')}
            save(OUT/(args.execution+'-status.json'),r);print(json.dumps(r));return
        suffix=args.phase+('-'+str(args.case)+'-'+str(args.attempt) if args.phase=='run' else '')
        dest=OUT/('release-'+suffix+'.json');assert not dest.exists(),'existing_attempt_reconcile_only'
        if args.phase=='build':
            assert json.loads((OUT/'local-validation.json').read_text())['status']=='passed'
            ops.budget_reserve('wat-v10-shadow-build-and-four-runs','google_cloud',1000000)
            ops.budget_begin('wat-v10-shadow-build-and-four-runs',control['generation'])
            cmd=['builds','submit',str(ROOT/'outputs/app-source-20261010-v10.tar.gz'),
                 '--config='+str(ROOT/'work/hamle-account-manager/cloudbuild.yaml'),
                 '--gcs-source-staging-dir=gs://gamechangers-hamle-runtime-source-286533609595/source',
                 '--region=europe-west1','--async']
        elif args.phase in ('deploy','deploy-control'):
            build=json.loads((OUT/'build-verified.json').read_text());assert build['status']=='SUCCESS'
            image=next(x for x in build['results']['images'] if x['name']==TAG)
            assert image['digest'].startswith('sha256:') and len(image['digest'])==71
            cmd=(['run','jobs','update','hamle-account-manager','--region=europe-west1',
                  '--image='+TAG.split(':')[0]+'@'+image['digest'],'--task-timeout=600s']
                 if args.phase=='deploy' else ['run','services','update','hamle-control',
                  '--region=europe-west1','--image='+TAG.split(':')[0]+'@'+image['digest']])
        else:
            assert (args.case,args.attempt) in (('a',1),('b',1),('a',2),('video',1))
            assert json.loads((OUT/'release-deploy.json').read_text())['status']=='confirmed'
            cmd=['run','jobs','execute','hamle-account-manager','--region=europe-west1',
                 '--args='+('v11_wat_video_shadow.py' if args.case=='video' else
                            'v11_wat_shadow_worker.py,--case,'+args.case),'--async']
        record={'status':'attempt_started','recorded_at':now.isoformat(),'argv':cmd,'budget_before':budget,
                'customer_scope_enabled':False,'new_iam_or_secrets':False}
        save(dest,record,exclusive=True)
        try:
            result=run(cmd);record.update(status='confirmed',result=result);save(dest,record)
            print(json.dumps({'phase':args.phase,'status':'confirmed','id':result.get('id'),
                              'name':result.get('metadata',{}).get('name')}))
        finally:
            if args.phase=='build':ops.budget_unknown('wat-v10-shadow-build-and-four-runs')
    finally:b.close()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'status':'held','reason':getattr(exc,'code',type(exc).__name__),'automatic_retry':False}))
        raise SystemExit(2)
