"""Deploy the single reviewed image; schedules and business execution stay off."""
import json
from pathlib import Path
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from provision_metadata import run

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'outputs/new-mac-2026-10-10'
REGION='europe-west1'
IMAGE='europe-west1-docker.pkg.dev/gamechangers-hamle/hamle-runtime/hamle-account-manager'
ORIGIN='https://hamle-control-286533609595.europe-west1.run.app'
JOB='hamle-account-manager'
CONTROL='hamle-control'


def main(phase):
    now=datetime.now(ZoneInfo('Europe/Istanbul'))
    if now > datetime.fromisoformat('2026-10-12T23:59:59+03:00'):
        raise RuntimeError('shared_budget_authorization_expired')
    report={'recorded_at':datetime.now(ZoneInfo('Europe/Istanbul')).isoformat(),
            'phase':phase,'results':[],'business_execution_started':False,
            'scheduler_created':False}
    path=OUT/('cloud-app-'+phase+'.json')
    def call(args,missing=False):
        result=run(args,missing)
        report['results'].append({'argv':args,'result':result})
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        return result
    if phase=='build':
        if path.exists(): raise RuntimeError('build_already_attempted_reconcile_required')
        seed=json.loads((OUT/'cloud-state-seed.json').read_text())
        snapshot=json.loads((OUT/'cloud-budget-import-snapshot.json').read_text())
        holds=[r for r in snapshot['reservations'] if r['reservation_key']=='cloud-account-manager-foundation-20261010-v1']
        if (seed['control']['mode']!='stopped' or seed['budget']['overrun_hold']
                or len(holds)!=1 or holds[0]['provider']!='google_cloud'
                or holds[0]['reserved_microdollars']!=5_000_000
                or holds[0]['state'] not in ('reserved','started','unknown')):
            raise RuntimeError('cloud_state_not_ready')
        # A durable attempt precedes the single build submission.
        report['status']='submission_started'
        path.write_text(json.dumps(report,indent=2)+'\n')
        result=call(['builds','submit',str(ROOT/'outputs/app-source-20261010-v1.tar.gz'),
                     '--config='+str(Path(__file__).with_name('cloudbuild.yaml')),
                     '--gcs-source-staging-dir=gs://gamechangers-hamle-runtime-source-286533609595/source',
                     '--region='+REGION,'--async'])
        report.update(status='submitted',build_id=result['id'])
    elif phase=='deploy':
        if path.exists(): raise RuntimeError('deploy_already_attempted_reconcile_required')
        build=json.loads((OUT/'cloud-app-build-verified.json').read_text())
        if build['status']!='SUCCESS' or not re.fullmatch('sha256:[0-9a-f]{64}',build['digest']):
            raise RuntimeError('build_not_verified')
        image=IMAGE+'@'+build['digest']
        for args in [['run','jobs','describe',JOB,'--region='+REGION],
                     ['run','services','describe',CONTROL,'--region='+REGION]]:
            if run(args,True).get('absent') is not True:
                raise RuntimeError('application_resource_already_exists_reconcile_required')
        call(['run','jobs','create',JOB,'--region='+REGION,'--image='+image,
              '--service-account=hamle-runtime@gamechangers-hamle.iam.gserviceaccount.com',
              '--tasks=1','--parallelism=1','--max-retries=0','--task-timeout=300s',
              '--cpu=1','--memory=512Mi','--command=python','--args=worker.py,--mode,review',
              '--set-env-vars=HAMLE_ALLOW_FIRESTORE_OPERATIONS=yes,HAMLE_PROJECT_ID=gamechangers-hamle,HAMLE_FIRESTORE_DATABASE=hamle-operations'])
        call(['run','deploy',CONTROL,'--region='+REGION,'--image='+image,
              '--service-account=hamle-control@gamechangers-hamle.iam.gserviceaccount.com',
              '--iap','--no-allow-unauthenticated','--min=0','--max=1','--min-instances=0','--max-instances=1',
              '--cpu=1','--memory=512Mi','--concurrency=4','--timeout=60s','--cpu-throttling',
              '--no-cpu-boost','--port=8080','--command=gunicorn',
              '--args=--bind,:8080,cloud_control:create_app()',
              '--set-env-vars=HAMLE_ALLOW_FIRESTORE_OPERATIONS=yes,IAP_AUDIENCE=/projects/286533609595/locations/europe-west1/services/hamle-control,PUBLIC_ORIGIN='+ORIGIN])
        plan=json.loads(Path(__file__).with_name('cloud-resource-plan.json').read_text())
        for args in plan['phases'][5]['post_deploy_iam_commands']: call(args)
        call(['run','jobs','describe',JOB,'--region='+REGION])
        call(['run','services','describe',CONTROL,'--region='+REGION])
        call(['run','services','get-iam-policy',CONTROL,'--region='+REGION])
        call(['iap','web','get-iam-policy','--resource-type=cloud-run','--service='+CONTROL,'--region='+REGION])
        # Runtime metadata assertions are completed against the returned API
        # schema in a separate verification step; this is not live acceptance.
        report.update(status='deployed_awaiting_readback_verification',image=image,control_url=ORIGIN)
    else: raise RuntimeError('unsupported_phase')
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='results'},ensure_ascii=False))


if __name__=='__main__':
    try: main(sys.argv[1] if len(sys.argv)==2 else '')
    except Exception as exc:
        print(json.dumps({'status':'held','reason':str(exc) if isinstance(exc,RuntimeError)
                          else type(exc).__name__,'automatic_retry':False}))
        raise SystemExit(2)
