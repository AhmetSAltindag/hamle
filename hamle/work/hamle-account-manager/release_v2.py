import json,sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'hamle/work/hamle-account-manager'))
from provision_metadata import run,OUT,ROOT
from cloud_store import FirestoreBackend,OperationsStore
from import_cloud_credentials import credentials
phase=sys.argv[1]
p=OUT/('cloud-app-v2-'+phase+'.json')
if p.exists():raise RuntimeError('attempt_already_recorded_reconcile_required')
now=datetime.now(ZoneInfo('Europe/Istanbul'))
if now.isoformat()>'2026-10-12T23:59:59+03:00':raise RuntimeError('expired')
backend=FirestoreBackend(opt_in='yes',credentials=credentials())
store=OperationsStore(backend)
state=store.control();budget=store.budget_status();backend.close()
assert state['mode']=='stopped' and not budget['overrun_hold'] and budget['remaining_microdollars']>=0
report={'started_at':now.isoformat(),'phase':phase,'status':'attempt_started','control':state,'budget':budget,'results':[]}
p.write_text(json.dumps(report,indent=2)+'\n')
def call(args):
 result=run(args)
 report['results'].append({'argv':args,'result':result})
 p.write_text(json.dumps(report,indent=2)+'\n')
 return result
if phase=='build':
 result=call(['builds','submit',str(ROOT/'outputs/app-source-20261010-v2.tar.gz'),'--config='+str(ROOT/'work/hamle-account-manager/cloudbuild.yaml'),'--gcs-source-staging-dir=gs://gamechangers-hamle-runtime-source-286533609595/source','--region=europe-west1','--async'])
 report.update(status='submitted',build_id=result['id'])
elif phase=='deploy':
 build=json.loads((OUT/'cloud-app-v2-build-verified.json').read_text())
 assert build['status']=='SUCCESS'
 image='europe-west1-docker.pkg.dev/gamechangers-hamle/hamle-runtime/hamle-account-manager@'+build['digest']
 call(['run','jobs','update','hamle-account-manager','--region=europe-west1','--image='+image])
 call(['run','services','update','hamle-control','--region=europe-west1','--image='+image])
 report.update(status='updated_awaiting_readback',image=image)
else:raise RuntimeError('invalid_phase')
p.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k!='results'}))
