"""Read-only deployment evidence; no provider secrets or business data."""
import json
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from provision_metadata import run, OUT

REGION = '--region=europe-west1'
queries = {
    'job': ['run','jobs','describe','hamle-account-manager',REGION],
    'service': ['run','services','describe','hamle-control',REGION],
    'job_iam': ['run','jobs','get-iam-policy','hamle-account-manager',REGION],
    'service_iam': ['run','services','get-iam-policy','hamle-control',REGION],
    'iap_iam': ['iap','web','get-iam-policy','--resource-type=cloud-run','--service=hamle-control',REGION],
}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', choices=('v1','v2','v3','v4','v5','v6','v7'), default='v1')
    release = parser.parse_args().release
    with ThreadPoolExecutor(max_workers=5) as pool:
        values = dict(zip(queries, pool.map(run, queries.values())))
    job, service = values['job'], values['service']
    spec = job['spec']['template']['spec']['template']['spec']
    template = service['spec']['template']
    build_evidence = 'cloud-app-build-verified.json' if release == 'v1' else 'cloud-app-'+release+'-build-verified.json'
    image = 'europe-west1-docker.pkg.dev/gamechangers-hamle/hamle-runtime/hamle-account-manager@' + json.loads((OUT/build_evidence).read_text())['digest']
    origin = 'https://hamle-control-286533609595.europe-west1.run.app'
    assert spec['serviceAccountName'] == 'hamle-runtime@gamechangers-hamle.iam.gserviceaccount.com'
    assert spec['maxRetries'] == 0 and str(spec['timeoutSeconds']) == ('600' if release == 'v7' else '300')
    assert spec['containers'][0]['image'] == image
    assert template['spec']['containers'][0]['image'] == image
    assert template['spec']['serviceAccountName'] == 'hamle-control@gamechangers-hamle.iam.gserviceaccount.com'
    assert service['metadata']['annotations']['run.googleapis.com/iap-enabled'] == 'true'
    assert origin in json.loads(service['metadata']['annotations']['run.googleapis.com/urls'])
    env = {v['name']:v['value'] for v in template['spec']['containers'][0]['env']}
    assert env['PUBLIC_ORIGIN'] == origin
    assert env['IAP_AUDIENCE'] == '/projects/286533609595/locations/europe-west1/services/hamle-control'
    for resource in (job, service):
        assert any(c['type'] == 'Ready' and c['status'] == 'True' for c in resource['status']['conditions'])
    for name in ('job_iam','service_iam','iap_iam'):
        assert all(member not in ('allUsers','allAuthenticatedUsers') for b in values[name].get('bindings',[]) for member in b.get('members',[]))
    access = [b for b in values['iap_iam'].get('bindings',[]) if b['role'] == 'roles/iap.httpsResourceAccessor']
    assert len(access) == 1 and access[0]['members'] == ['user:ahmet@gamechangers.com.tr']
    report = {'recorded_at':datetime.now(ZoneInfo('Europe/Istanbul')).isoformat(), 'status':'metadata_verified', 'control_url':origin, 'image':image, 'scheduler_created':False, 'business_execution_started':False, **values}
    evidence_name = 'cloud-app-deployment-verified.json' if release == 'v1' else 'cloud-app-'+release+'-deployment-verified.json'
    (OUT/evidence_name).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('status','control_url','scheduler_created','business_execution_started')}))
