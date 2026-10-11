"""Transfer existing budget authority once, preserving every purchase and hold."""
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from cloud_store import FirestoreBackend, OperationsStore, StoreError
from import_cloud_credentials import credentials, save_exclusive

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'work/hamle-runtime'))
from budget import BudgetLedger
OUT = ROOT/'outputs/new-mac-2026-10-10'
SNAPSHOT = OUT/'cloud-budget-import-snapshot.json'


def export():
    if SNAPSHOT.exists():
        with BudgetLedger(ROOT/'work/private-connections/shared-budget.sqlite3') as local:
            authority = local.db.execute("SELECT value FROM budget_settings WHERE key='cloud_authority'").fetchone()
            if authority is None or authority[0] != 'hamle-operations':
                raise StoreError('snapshot_without_local_freeze_reconcile_required')
        return json.loads(SNAPSHOT.read_text())
    with BudgetLedger(ROOT/'work/private-connections/shared-budget.sqlite3') as local:
        local.mark_unknown('cloud-account-manager-foundation-20261010-v1')
        with local._transaction():
            if local.db.execute("SELECT value FROM budget_settings WHERE key='cloud_authority'").fetchone():
                raise StoreError('local_handoff_without_snapshot_reconcile_required')
            snapshot={'policy_id':'hamle-shared-20261009-20261012','limit_microdollars':50_000_000,
                      'expires_at':'2026-10-12T23:59:59+03:00','overrun_hold':False,
                      'providers':{},'reservations':[], 'evidence_ref':SNAPSHOT.name}
            snapshot['overrun_hold']=local.db.execute("SELECT value FROM budget_settings WHERE key='overrun_hold'").fetchone()[0]=='1'
            for row in local.db.execute('SELECT * FROM budget_providers'):
                name=row['provider']
                paid=local.db.execute('SELECT COALESCE(SUM(paid_microdollars),0) FROM budget_prepaid_purchases WHERE provider=?',(name,)).fetchone()[0]
                snapshot['providers'][name]={'baseline_microdollars':row['baseline_microdollars'],
                    'paid_microdollars':paid,'billing_ready':local._billing_state(name)['billing_ready']==1}
            for row in local.db.execute('SELECT * FROM budget_reservations ORDER BY reservation_key'):
                snapshot['reservations'].append({k:row[k] for k in ('reservation_key','provider',
                    'reserved_microdollars','actual_microdollars','state','evidence_ref')})
            local.db.execute("INSERT INTO budget_settings VALUES('cloud_authority','hamle-operations')")
        # Freeze commits first. A crash before the export now fails closed and
        # requires reconciliation, rather than leaving two spending authorities.
        save_exclusive(SNAPSHOT,snapshot)
        return snapshot


def main():
    if sys.argv[1:] != ['--seed-approved-cloud-state']:
        raise StoreError('explicit_seed_required')
    backend=FirestoreBackend(opt_in='yes',credentials=credentials())
    try:
        store=OperationsStore(backend)
        snapshot=export()
        control=store.initialize_control()
        budget=store.initialize_budget(snapshot)
        if control['mode']!='stopped' or control['generation']!=0:
            raise StoreError('bootstrap_control_not_stopped')
        report={'recorded_at':datetime.now(ZoneInfo('Europe/Istanbul')).isoformat(),
                'status':'passed','control':store.control(),'budget':store.budget_status(),
                'local_paid_calls_disabled':True,'new_budget_reset':False,
                'provider_reconciliation_connected':False}
        (OUT/'cloud-state-seed.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
    finally: backend.close()


if __name__=='__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'status':'held','reason':exc.code if isinstance(exc,StoreError)
                          else 'cloud_seed_failed','automatic_retry':False}))
        raise SystemExit(2)
