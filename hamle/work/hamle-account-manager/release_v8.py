"""Explicit maintenance deploy of existing approved runtime; no new IAM/secrets."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from provision_metadata import run, ROOT
from import_cloud_credentials import credentials
from cloud_store import FirestoreBackend, OperationsStore, DEADLINE

OUT = ROOT / 'outputs/cloud-v11-internal-20261010'
TAG = 'europe-west1-docker.pkg.dev/gamechangers-hamle/hamle-runtime/hamle-account-manager:20261010-v8'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('build', 'build-status', 'deploy', 'advance', 'execution-status', 'pilot-status'))
    parser.add_argument('--execution')
    parser.add_argument('--attempt', type=int)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    assert now.timestamp() <= DEADLINE, 'authorization_expired'
    OUT.mkdir(parents=True, exist_ok=True)
    backend = FirestoreBackend(opt_in='yes', credentials=credentials())
    ops = OperationsStore(backend)
    try:
        control = ops.control(); budget = ops.budget_status()
        assert control['mode'] == 'review_open' and not budget['overrun_hold'] and budget['remaining_microdollars'] > 0
        def billing_ready(tx, now):
            return ops._budget(tx)['providers']['google_cloud']['billing_ready']
        assert ops._run(billing_ready) is True, 'cloud_billing_not_ready'
        if args.phase == 'pilot-status':
            from v11_internal_state import InternalPilotStore
            store = InternalPilotStore(ops); summary = store.summary(); row = store.read()
            (OUT/'pilot-private-readback.json').write_text(json.dumps(row, ensure_ascii=False, indent=2)+'\n')
            (OUT/'pilot-private-readback.json').chmod(0o600)
            print(json.dumps(summary, ensure_ascii=False)); return
        if args.phase == 'build-status':
            report = json.loads((OUT/'release-build.json').read_text())
            value = run(['builds', 'describe', report['result']['id'], '--region=europe-west1'])
            safe = {k: value.get(k) for k in ('id', 'status', 'createTime', 'startTime', 'finishTime', 'results', 'failureInfo')}
            (OUT/'build-verified.json').write_text(json.dumps(safe, indent=2)+'\n')
            print(json.dumps(safe)); return
        if args.phase == 'execution-status':
            assert args.execution and args.execution.startswith('hamle-account-manager-')
            value = run(['run', 'jobs', 'executions', 'describe', args.execution, '--region=europe-west1'])
            safe = {'name': value['metadata']['name'], 'status': value.get('status')}
            (OUT/(args.execution+'-status.json')).write_text(json.dumps(safe,indent=2)+'\n')
            print(json.dumps(safe)); return
        suffix = args.phase + ('-'+str(args.attempt) if args.phase == 'advance' else '')
        path = OUT/('release-'+suffix+'.json')
        assert not path.exists(), 'attempt_exists_reconcile_do_not_repeat'
        if args.phase == 'build':
            validation = json.loads((OUT/'local-validation.json').read_text())
            assert validation['status'] == 'passed'
            reserve = 'v11-internal-build-and-five-runs'
            ops.budget_reserve(reserve, 'google_cloud', 1_000_000)
            ops.budget_begin(reserve, control['generation'])
            command = ['builds', 'submit', str(ROOT/'outputs/app-source-20261010-v8a.tar.gz'),
                       '--config='+str(ROOT/'work/hamle-account-manager/cloudbuild.yaml'),
                       '--gcs-source-staging-dir=gs://gamechangers-hamle-runtime-source-286533609595/source',
                       '--region=europe-west1', '--async']
        elif args.phase == 'deploy':
            build = json.loads((OUT/'build-verified.json').read_text())
            assert build['status'] == 'SUCCESS'
            image = next(x for x in build['results']['images'] if x['name'] == TAG)
            digest = image['digest']; assert digest.startswith('sha256:') and len(digest) == 71
            command = ['run', 'jobs', 'update', 'hamle-account-manager', '--region=europe-west1',
                       '--image='+TAG.split(':')[0]+'@'+digest, '--task-timeout=600s']
        else:
            assert args.attempt is not None and 1 <= args.attempt <= 5, 'bounded_five_executions_only'
            assert json.loads((OUT/'release-deploy.json').read_text())['status'] == 'confirmed'
            command = ['run', 'jobs', 'execute', 'hamle-account-manager', '--region=europe-west1',
                       '--args=v11_internal_worker.py,--phase,advance', '--async']
        report = {'status': 'attempt_started', 'recorded_at': now.isoformat(), 'phase': args.phase,
                  'control_generation': control['generation'], 'budget_before': budget, 'argv': command,
                  'customer_scope_enabled': False, 'new_iam_or_secrets': False}
        path.write_text(json.dumps(report, indent=2)+'\n')
        try:
            result = run(command)
            report.update(status='confirmed', result=result)
            path.write_text(json.dumps(report, indent=2)+'\n')
            print(json.dumps({'phase': args.phase, 'status': 'confirmed',
                              'id': result.get('id'), 'name': result.get('metadata',{}).get('name')}))
        finally:
            if args.phase == 'build': ops.budget_unknown('v11-internal-build-and-five-runs')
    finally:
        backend.close()


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        # Commands contain no secrets, and generic failure output is bounded.
        print(json.dumps({'status':'held', 'reason': type(exc).__name__, 'automatic_retry':False}))
        raise SystemExit(2)
