"""Explicit phased provisioning of approved metadata only; never handles secrets."""
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
PLAN = json.loads(Path(__file__).with_name('cloud-resource-plan.json').read_text())
OUT = ROOT / 'outputs/new-mac-2026-10-10'


def run(args, allow_missing=False):
    p = subprocess.run([sys.executable, str(ROOT / 'work/cloud_gcloud.py'), *args,
                        '--project=gamechangers-hamle', '--format=json', '--quiet'],
                       capture_output=True, text=True, timeout=180)
    if p.returncode:
        if allow_missing and ('NOT_FOUND' in p.stderr or 'does not exist' in p.stderr
                              or 'not found: 404' in p.stderr
                              or (args[:3] == ['run','jobs','describe'] and f'Cannot find job [{args[3]}].' in p.stderr)
                              or (args[:3] == ['run','services','describe'] and f'Cannot find service [{args[3]}]' in p.stderr)):
            return {'absent': True}
        # These commands only operate on metadata. Still withhold raw diagnostics.
        code = next((s for s in ['PERMISSION_DENIED','NOT_FOUND','SERVICE_DISABLED',
                               'INVALID_ARGUMENT','ALREADY_EXISTS','UNAUTHENTICATED']
                     if s in p.stderr), 'command_failed')
        raise RuntimeError(code + ':' + ' '.join(args[:3]))
    return json.loads(p.stdout) if p.stdout.strip() else {'completed': True}


def main(phase):
    os.umask(0o077)
    stamp = datetime.now(ZoneInfo('Europe/Istanbul'))
    report = {'recorded_at': stamp.isoformat(), 'phase': phase,
              'user_authorized': True, 'secret_values_handled': False, 'results': []}
    target = OUT / ('cloud-account-manager-' + phase + '-' + stamp.strftime('%H%M%S') + '.json')
    def record(args, value):
        report['results'].append({'argv': args, 'result': value})
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'command': args[:3], 'ok': True, 'absent': value.get('absent', False)
                          if isinstance(value, dict) else False}), flush=True)
    def call(args, missing=False):
        result = run(args, missing); record(args, result); return result
    try:
        if phase == 'inventory':
            enabled = call(['services','list','--enabled'])
            services = {v['config']['name'] for v in enabled}
            for args in PLAN['phases'][0]['commands'][1:]:
                if args[0] == 'secrets' and 'secretmanager.googleapis.com' not in services:
                    record(args, {'deferred': 'api_not_enabled'}); continue
                call(args, True)
        elif phase == 'identities':
            for args in PLAN['phases'][1]['commands_create_only_if_missing']:
                email = args[3] + '@gamechangers-hamle.iam.gserviceaccount.com'
                lookup = ['iam','service-accounts','describe',email]
                value = call(lookup, True)
                if value.get('absent'):
                    call(args)
                    value = call(lookup)
                if value.get('email') != email or value.get('disabled', False):
                    raise RuntimeError('identity_mismatch')
                keys = call(['iam','service-accounts','keys','list','--iam-account='+email,
                             '--managed-by=user'])
                if keys: raise RuntimeError('unexpected_user_managed_key')
        elif phase == 'resources':
            enabled = call(['services','list','--enabled'])
            services = {v['config']['name'] for v in enabled}
            missing = [v for v in PLAN['phases'][2]['required_apis'] if v not in services]
            if missing: call(['services','enable',*missing])
            lookups = PLAN['phases'][0]['commands'][5:11]
            for args, lookup in zip(PLAN['phases'][2]['commands_create_only_if_missing'], lookups):
                before = call(lookup, True)
                if before.get('absent'): call(args)
                call(lookup)
        elif phase == 'iam':
            call(['projects','get-iam-policy','gamechangers-hamle'])
            for args in PLAN['phases'][3]['commands']:
                call(args)
            call(['projects','get-iam-policy','gamechangers-hamle'])
            for secret in ['hamle-gmail-oauth','hamle-basecamp-credential','hamle-openai-api-key']:
                call(['secrets','get-iam-policy',secret])
            call(['artifacts','repositories','get-iam-policy','hamle-runtime','--location=europe-west1'])
            call(['storage','buckets','get-iam-policy','gs://gamechangers-hamle-runtime-source-286533609595'])
        else: raise RuntimeError('unsupported_phase')
        report['status'] = 'passed'
    except Exception as exc:
        report['status'] = 'held'
        report['reason'] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'phase':phase,'status':report['status'],'reason':report.get('reason'),
                      'evidence':str(target)}),flush=True)
    return 0 if report['status']=='passed' else 2


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1] if len(sys.argv)==2 else 'unsupported'))
