"""Read-only WAT configuration verification, safe filtered output only.

No task/comment/member mutation. Uses existing Hamle CLI profile and keychain;
does not inspect, output or copy credentials. Customer asset bytes aren't read.
"""
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / 'work/basecamp-tools/bin/basecamp'
OUT = ROOT / 'outputs/wat-pilot-20261010/basecamp-read-evidence.json'
ENV = dict(os.environ)
ENV.update(XDG_CONFIG_HOME=str(ROOT/'work/private-connections/config'),
           XDG_CACHE_HOME=str(ROOT/'work/private-connections/cache'),
           BASECAMP_PROFILE='hamle-gamechangers')
if ENV.get('BASECAMP_TOKEN') or ENV.get('BASECAMP_NO_KEYRING'):
    raise SystemExit('Unexpected credential override; stopped')


def read(args):
    try:
        p = subprocess.run([str(CLI),*args,'--account','5898865','--json'],env=ENV,
                           capture_output=True,text=True,timeout=45)
        if p.returncode or 'fallback' in p.stderr.lower():
            code='read_rejected'
            try:
                body=json.loads(p.stdout)
                error=body.get('error',{})
                if isinstance(error,dict) and error.get('code') in ('auth_required','forbidden','not_found','unauthorized'):
                    code=error['code']
            except Exception: pass
            return {'success':False,'reason':code,'exit_code':p.returncode}
        body=json.loads(p.stdout)
        if body.get('ok') is not True:
            return {'success':False,'reason':'response_not_ok'}
        return {'success':True,'data':body['data']}
    except Exception:
        return {'success':False,'reason':'read_failed'}


def run():
    evidence={'recorded_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'identity_expected':'hamle@gamechangers.com.tr','account_id':5898865,'project_id':40694338,
              'basecamp_content_writes':0,'paid_model_calls':0,'customer_live_enabled':False,'checks':{}}
    status=read(['auth','status','--check'])
    data=status.get('data',{})
    good=(status['success'] and data.get('storage')=='keyring' and data.get('authenticated') is True
          and data.get('valid') is True and data.get('user_email')=='hamle@gamechangers.com.tr'
          and str(data.get('account_id'))=='5898865')
    evidence['checks']['identity']={'verified':good,'reason':None if good else status.get('reason','identity_mismatch')}
    if good:
        project=read(['projects','show','40694338'])
        p=project.get('data',{})
        valid=(project['success'] and str(p.get('id'))=='40694338' and p.get('status')=='active')
        evidence['checks']['project']={'verified':valid,'reason':None if valid else project.get('reason','project_mismatch')}
        if valid:
            evidence['project']={k:p.get(k) for k in ('id','name','status','clients_enabled')}
            evidence['dock']=[{k:x.get(k) for k in ('id','name','title','enabled','url')} for x in p.get('dock',[])]
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                jobs={n:pool.submit(read,args) for n,args in {
                    'people':['people','list','--in','40694338','--all'],
                    'lists':['todolists','list','--in','40694338','--all']}.items()}
                for name, future in jobs.items():
                    r=future.result();items=r.get('data')
                    evidence['checks'][name]={'verified':r['success'] and isinstance(items,list),'reason':r.get('reason')}
                    if r['success'] and isinstance(items,list):
                        fields=('id','name','email_address','client','employee') if name=='people' else ('id','name','title','status','type','app_url','todos_count','completed_ratio','bucket','parent')
                        evidence[name]=[{k:item.get(k) for k in fields if k in item} for item in items]
    OUT.parent.mkdir(parents=True,exist_ok=True)
    OUT.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n')
    OUT.chmod(0o600)
    print(json.dumps(evidence,ensure_ascii=False))


if __name__=='__main__':run()
