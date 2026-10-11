"""Existing approved Cloud SDK login; only safe status is emitted."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parent
URL=ROOT/'private-connections/cloud-reauth-request.url'
if len(sys.argv)==3 and sys.argv[1]=='--capture':
    u=sys.argv[2]
    if urlparse(u).scheme!='https' or urlparse(u).hostname!='accounts.google.com':
        raise SystemExit(2)
    URL.write_text(u);URL.chmod(0o600)
    raise SystemExit(0)
if len(sys.argv)!=1:raise SystemExit(2)
os.umask(0o077)
URL.unlink(missing_ok=True)
env=dict(os.environ)
env['BROWSER']=shlex.quote(sys.executable)+' '+shlex.quote(str(Path(__file__).resolve()))+' --capture %s'
result=subprocess.run([sys.executable,str(ROOT/'cloud_gcloud.py'),'auth','login',
    'ahmet@gamechangers.com.tr','--force','--launch-browser','--brief'],env=env,
    capture_output=True,text=True)
print(json.dumps({'reauth_completed':result.returncode==0,'exit_code':result.returncode}))
if result.returncode==0:URL.unlink(missing_ok=True)
raise SystemExit(result.returncode)
