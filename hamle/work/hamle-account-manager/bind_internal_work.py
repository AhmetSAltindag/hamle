"""Migrate the already verified internal task/mail receipts; no provider writes."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from provision_metadata import OUT
from import_cloud_credentials import credentials
from cloud_store import FirestoreBackend, OperationsStore

SEED = 'p-a0627d6de33573f9284e8799b2b537af6da59d22bdfcff812158e64f3490067d'

if __name__ == '__main__':
    assert json.loads((OUT/'cloud-app-v6-local-validation.json').read_text())['status'] == 'passed'
    backend = FirestoreBackend(opt_in='yes', credentials=credentials())
    try:
        store = OperationsStore(backend)
        before = store.public_state()
        work = store.bind_verified_work(SEED)
        after = store.public_state()
        assert work['task_id'] == 10392247471 and work['thread_id'] == '1a122eec4ccdb53a'
        assert before['actions'] == after['actions']
        report = {'recorded_at': datetime.now(ZoneInfo('Europe/Istanbul')).isoformat(),
                  'status': 'existing_verified_work_bound', 'work': work,
                  'business_writes': 0, 'model_calls': 0, 'old_evidence_preserved': True}
        (OUT/'followup-existing-work-binding.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({'status': report['status'], 'work_id': work['work_id'], 'task_id': work['task_id']}))
    finally:
        backend.close()
