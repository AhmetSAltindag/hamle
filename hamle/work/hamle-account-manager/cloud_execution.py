"""Explicit one-shot launch and read-only evidence for internal acceptance."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import re
from zoneinfo import ZoneInfo
from provision_metadata import run,OUT

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('start','read'))
    parser.add_argument('label')
    parser.add_argument('--mode',choices=('verify','review','dispatch','track','recover_followup'))
    parser.add_argument('--proposal-id')
    parser.add_argument('--work-id')
    parser.add_argument('--failure-evidence-sha256')
    parser.add_argument('--failed-task-fingerprint')
    args=parser.parse_args()
    if not re.fullmatch(r'[a-z0-9-]{1,80}',args.label):raise ValueError('invalid_label')
    path=OUT/('cloud-execution-'+args.label+'.json')
    if args.action=='start':
        if args.mode is None or path.exists():raise ValueError('launch_exists_or_mode_missing')
        now=datetime.now(ZoneInfo('Europe/Istanbul'))
        if now>datetime.fromisoformat('2026-10-12T23:59:59+03:00'):raise ValueError('authorization_expired')
        argv=['worker.py','--mode',args.mode]
        if args.mode=='dispatch':
            if not args.proposal_id or not re.fullmatch(r'p-[0-9a-f]{64}',args.proposal_id):raise ValueError('exact_proposal_required')
            argv+=['--proposal-id',args.proposal_id]
        elif args.proposal_id is not None:raise ValueError('unexpected_proposal')
        recovery_fields = (args.work_id, args.failure_evidence_sha256, args.failed_task_fingerprint)
        if args.mode=='recover_followup':
            if not args.work_id or not re.fullmatch(r'w-[0-9a-f]{64}', args.work_id):raise ValueError('exact_work_required')
            if any(not v or not re.fullmatch(r'[0-9a-f]{64}',v) for v in recovery_fields[1:]):raise ValueError('exact_failure_evidence_required')
            argv+=['--work-id',args.work_id,'--failure-evidence-sha256',args.failure_evidence_sha256,'--failed-task-fingerprint',args.failed_task_fingerprint]
        elif any(v is not None for v in recovery_fields):raise ValueError('unexpected_recovery_fields')
        report={'recorded_at':now.isoformat(),'mode':args.mode,'status':'launch_started','automatic_retry':False}
        with path.open('x') as f:json.dump(report,f,indent=2)
        execution=run(['run','jobs','execute','hamle-account-manager','--region=europe-west1','--args='+','.join(argv),'--async'])
        report.update(status='submitted',execution=execution)
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'status':'submitted','execution':execution['metadata']['name']}))
    else:
        report=json.loads(path.read_text())
        name=report['execution']['metadata']['name']
        queries=[['run','jobs','executions','describe',name,'--region=europe-west1'],['logging','read','resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"="'+name+'"','--limit=30']]
        with ThreadPoolExecutor(max_workers=2) as pool:execution,logs=list(pool.map(run,queries))
        report.update(execution=execution,logs=logs)
        complete=next((c for c in execution.get('status',{}).get('conditions',[]) if c['type']=='Completed'),{})
        report['status']={'True':'completed','False':'failed'}.get(complete.get('status'),'running')
        path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'status':report['status'],'execution':name,'results':[r.get('jsonPayload') for r in logs if r.get('jsonPayload')]},ensure_ascii=False))
