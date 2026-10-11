"""Read-only, filtered cloud and internal Basecamp metadata evidence.

Never accesses Secret Manager payloads, modifies cloud/business state, or makes
model calls. OAuth access token stays in memory in the existing credentials helper.
"""
import concurrent.futures
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'work/hamle-account-manager'))
from provision_metadata import run
from import_cloud_credentials import credentials
from cloud_store import FirestoreBackend, OperationsStore
from verify_wat_read_only import read as basecamp_read

OUT = ROOT / 'outputs/cloud-v11-internal-20261010'
REGION = '--region=europe-west1'
SECRETS = ('hamle-gmail-oauth', 'hamle-basecamp-credential', 'hamle-openai-api-key')


def checked(args):
    try:
        return {'ok': True, 'value': run(args)}
    except Exception as exc:
        return {'ok': False, 'reason': str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}


def container_summary(container):
    # Whitelist operational values. Other env values are never persisted.
    safe = {'HAMLE_CLOUD_OPT_IN', 'HAMLE_COMMAND', 'PORT', 'PUBLIC_ORIGIN', 'IAP_AUDIENCE'}
    return {k: container[k] for k in ('name', 'image', 'command', 'args', 'resources') if k in container} | {
        'environment_names': [r['name'] for r in container.get('env', [])],
        'operational_environment': {r['name']: r.get('value') for r in container.get('env', []) if r['name'] in safe},
        'secret_references': [r.get('valueFrom', {}) for r in container.get('env', []) if 'valueFrom' in r]}


def main():
    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True)
    report = {'recorded_at': datetime.now(ZoneInfo('Europe/Istanbul')).isoformat(),
              'project_id': 'gamechangers-hamle', 'mode': 'read_only',
              'cloud_mutations': 0, 'basecamp_content_writes': 0, 'paid_model_calls': 0,
              'secret_payloads_read': False, 'checks': {}}
    queries = {
        'auth_accounts': ['auth', 'list'],
        'project': ['projects', 'describe', 'gamechangers-hamle'],
        'billing_link': ['billing', 'projects', 'describe', 'gamechangers-hamle'],
        'job': ['run', 'jobs', 'describe', 'hamle-account-manager', REGION],
        'control_service': ['run', 'services', 'describe', 'hamle-control', REGION],
        'project_iam': ['projects', 'get-iam-policy', 'gamechangers-hamle'],
        'job_iam': ['run', 'jobs', 'get-iam-policy', 'hamle-account-manager', REGION],
        'control_iam': ['run', 'services', 'get-iam-policy', 'hamle-control', REGION],
        'iap_iam': ['iap', 'web', 'get-iam-policy', '--resource-type=cloud-run', '--service=hamle-control', REGION],
        'database': ['firestore', 'databases', 'describe', '--database=hamle-operations'],
        'schedulers': ['scheduler', 'jobs', 'list', '--location=europe-west1'],
        'repository': ['artifacts', 'repositories', 'describe', 'hamle-runtime', '--location=europe-west1'],
        'source_bucket': ['storage', 'buckets', 'describe', 'gs://gamechangers-hamle-runtime-source-286533609595'],
    }
    for secret in SECRETS:
        queries['secret_versions_' + secret] = ['secrets', 'versions', 'list', secret]
        queries['secret_iam_' + secret] = ['secrets', 'get-iam-policy', secret]
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        results = dict(zip(queries, pool.map(checked, queries.values())))
    for name, result in results.items():
        report['checks'][name] = {'ok': result['ok'], **({'reason': result['reason']} if not result['ok'] else {})}
        if not result['ok']:
            continue
        value = result['value']
        if name == 'job':
            spec = value['spec']['template']['spec']['template']['spec']
            report[name] = {'name': value['metadata']['name'], 'generation': value['metadata'].get('generation'),
                            'service_account': spec.get('serviceAccountName'), 'max_retries': spec.get('maxRetries'),
                            'timeout_seconds': spec.get('timeoutSeconds'), 'containers': [container_summary(x) for x in spec.get('containers', [])],
                            'conditions': value.get('status', {}).get('conditions'),
                            'latest_execution': value.get('status', {}).get('latestCreatedExecution')}
        elif name == 'control_service':
            spec = value['spec']['template']['spec']
            report[name] = {'name': value['metadata']['name'], 'generation': value['metadata'].get('generation'),
                            'service_account': spec.get('serviceAccountName'),
                            'containers': [container_summary(x) for x in spec.get('containers', [])],
                            'iap_enabled': value['metadata'].get('annotations', {}).get('run.googleapis.com/iap-enabled'),
                            'latest_ready_revision': value.get('status', {}).get('latestReadyRevisionName'),
                            'conditions': value.get('status', {}).get('conditions')}
        elif name == 'schedulers':
            report[name] = [{k: x.get(k) for k in ('name', 'state', 'schedule', 'timeZone', 'scheduleTime', 'lastAttemptTime')} |
                            {'target_uri': x.get('httpTarget', {}).get('uri'),
                             'target_service_account': x.get('httpTarget', {}).get('oauthToken', {}).get('serviceAccountEmail')}
                           for x in value if x.get('name', '').split('/')[-1].startswith('hamle-')]
        elif name.startswith('secret_versions_'):
            report[name] = [{k: x.get(k) for k in ('name', 'state', 'createTime', 'destroyTime')} for x in value]
        elif name == 'source_bucket':
            report[name] = {k: value.get(k) for k in ('name', 'location', 'public_access_prevention', 'uniform_bucket_level_access')}
        else:
            report[name] = value
    try:
        backend = FirestoreBackend(opt_in='yes', credentials=credentials())
        try:
            state = OperationsStore(backend).public_state()
            report['operations'] = {
                'control': state['control'], 'budget': state['budget'],
                'works': [{k: w.get(k) for k in ('work_id', 'status', 'project_id', 'task_id', 'pending_event_id')} for w in state.get('works', [])],
                'proposals': [{k: p.get(k) for k in ('proposal_id', 'status', 'kind', 'generation')} for p in state['proposals']],
                'actions': [{k: a.get(k) for k in ('action_id', 'state', 'kind')} for a in state['actions']],
                'external_actions_enabled': state['external_actions_enabled'],
                'unseen_cc_removal_resolved': state['unseen_cc_removal_resolved']}
            report['checks']['operations'] = {'ok': True}
        finally:
            backend.close()
    except Exception as exc:
        report['checks']['operations'] = {'ok': False, 'reason': type(exc).__name__}
    internal = {'account_id': 5898865, 'project_id': 49184053, 'writes': 0}
    status = basecamp_read(['auth', 'status', '--check'])
    identity = status.get('data', {})
    internal['identity_verified'] = (status['success'] and identity.get('storage') == 'keyring' and
                                     identity.get('authenticated') is True and identity.get('valid') is True and
                                     identity.get('user_email') == 'hamle@gamechangers.com.tr' and
                                     str(identity.get('account_id')) == '5898865')
    if internal['identity_verified']:
        for name, args in [('project', ['projects', 'show', '49184053']),
                           ('people', ['people', 'list', '--in', '49184053', '--all']),
                           ('todolists', ['todolists', 'list', '--in', '49184053', '--all'])]:
            result = basecamp_read(args)
            internal[name + '_read_ok'] = result['success']
            data = result.get('data')
            if result['success']:
                if name == 'project':
                    internal[name] = {k: data.get(k) for k in ('id', 'name', 'status', 'clients_enabled', 'dock')}
                elif name == 'people':
                    internal[name] = [{k: r.get(k) for k in ('id', 'name', 'email_address', 'client', 'employee')} for r in data]
                else:
                    internal[name] = [{k: r.get(k) for k in ('id', 'name', 'status', 'parent', 'bucket')} for r in data]
    report['basecamp_internal'] = internal
    report['spend_reconciliation'] = {'provider_invoice_currently_verified': False,
        'budget_values_are_reservations_and_prepayments_not_invoices': True,
        'last_provider_ui_read': 'hamle/outputs/new-mac-2026-10-10/hybrid-provider-billing-check.json',
        'cloud_billing_link_is_not_cost_report': True,
        'authorization_expires_at': '2026-10-12T23:59:59+03:00',
        'new_spend_reserved_by_this_preflight': 0}
    path = OUT / 'preflight-cloud-and-internal-readback.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    print(json.dumps({'evidence': str(path), 'checks': report['checks'],
                      'operations_budget': report.get('operations', {}).get('budget'),
                      'control_mode': report.get('operations', {}).get('control', {}).get('mode'),
                      'internal_identity_verified': internal['identity_verified']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
