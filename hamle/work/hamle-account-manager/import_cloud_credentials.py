"""One-time, explicitly approved credential transfer; values never leave memory.

Only the original Gmail record and existing OpenAI key are supported here.
Basecamp must have a separate cloud authorization because its refresh token rotates.
"""
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from zoneinfo import ZoneInfo

from cloud_providers import (GoogleSecretManager, _crc32c, _encode, _json,
                             _request, _modules, ProviderError)

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs/new-mac-2026-10-10'
PRIVATE = ROOT / 'work/private-connections'


def credentials():
    from google.oauth2.credentials import Credentials
    proc = subprocess.run([sys.executable, str(ROOT/'work/cloud_gcloud.py'),
                           'auth','print-access-token','--account=ahmet@gamechangers.com.tr'],
                          capture_output=True, text=True, timeout=60)
    token = proc.stdout.strip()
    if proc.returncode or not token or any(c.isspace() for c in token):
        raise ProviderError('cloud_admin_credentials_unavailable')
    return Credentials(token=token)


def save_exclusive(path, data):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())


def main():
    if sys.argv[1:] not in (['--import-approved-gmail-openai'], ['--import-approved-basecamp']):
        raise ProviderError('explicit_import_required')
    os.umask(0o077)
    if sys.argv[1:] == ['--import-approved-basecamp']:
        import basecamp_cloud_auth as auth
        record = auth.validate_record(auth._read(auth.CREDENTIAL_PATH))
        if record['refresh_state'] != 'ready':
            raise ProviderError('basecamp_credential_not_verified')
        values = {'hamle-basecamp-credential':_encode(record)}
    else:
        helper = _modules()[0]
        gmail = helper.MacKeychainStore().load()
        helper.validate_record(gmail)
        keyfile = ROOT/'.env.local'
        if keyfile.is_symlink() or stat.S_IMODE(keyfile.stat().st_mode) != 0o600:
            raise ProviderError('private_key_file_required')
        keys = [line.split('=',1)[1].strip().strip('\"\'') for line in keyfile.read_text().splitlines()
                if line.startswith('OPENAI_API_KEY=')]
        if len(keys)!=1 or not keys[0].startswith('sk-'):
            raise ProviderError('openai_key_invalid')
        values = {'hamle-gmail-oauth':_encode(gmail), 'hamle-openai-api-key':keys[0].encode()}
    manager = GoogleSecretManager(credentials=credentials())
    try:
        for name, data in values.items():
            evidence = OUT/(name+'-cloud-import.json')
            attempt = PRIVATE/(name+'-cloud-import.attempt.json')
            if evidence.exists() or attempt.exists():
                raise ProviderError('import_already_attempted_reconcile_required')
            resource = manager._name(name)
            raw,_ = _request(manager.session,'GET',
                f'https://secretmanager.googleapis.com/v1/{resource}/versions',
                headers=manager._headers())
            listing = _json(raw)
            if listing.get('versions') or listing.get('nextPageToken'):
                raise ProviderError('secret_versions_already_present')
            now = datetime.now(ZoneInfo('Europe/Istanbul')).isoformat()
            save_exclusive(attempt,{'started_at':now,'secret':name,'automatic_retry':False})
            raw,_ = _request(manager.session,'POST',
                f'https://secretmanager.googleapis.com/v1/{resource}:addVersion',
                headers=manager._headers(),body=_encode({'payload':{
                    'data':base64.b64encode(data).decode(),'dataCrc32c':str(_crc32c(data))}}))
            response = _json(raw)
            version = manager._version_name(response.get('name'),name)
            if response.get('state')!='ENABLED' or response.get('clientSpecifiedPayloadChecksum') is not True:
                raise ProviderError('secret_write_unconfirmed')
            if manager.access(name) != data:
                raise ProviderError('secret_readback_mismatch')
            report={'recorded_at':now,'secret':name,'version':version,'status':'passed',
                    'private_memory_transfer':True,'exact_readback':True,'secret_printed':False,
                    'user_explicitly_approved_cloud_transfer':True}
            save_exclusive(evidence,report)
            print(json.dumps(report),flush=True)
    finally:
        manager.close()


if __name__=='__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'status':'held','reason':exc.code if isinstance(exc,ProviderError)
                          else 'private_import_failed','automatic_retry':False}))
        raise SystemExit(2)
