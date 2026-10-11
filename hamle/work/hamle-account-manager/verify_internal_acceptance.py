"""Summarize actual private Firestore/Cloud Run readbacks, never run providers."""
import hashlib
import json
from datetime import datetime
from pathlib import Path
from cloud_store import proposal_hash

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'outputs/cloud-v11-internal-20261010'


def check():
    row = json.loads((OUT/'v9/pilot-private-readback.json').read_text())
    state = row['state']; internal = state['internal_run']
    assert proposal_hash(state) == row['state_sha256']
    assert state['mode'] == 'internal_pilot' and state['status'] == 'production_complete'
    assert internal['wat_live_enabled'] is False and internal['model_calls'] == 0
    assert set(internal['artifacts']) == {'main-v1', 'main-v2', 'story-v1'}
    bad, main, story = [internal['artifacts'][k] for k in ('main-v1', 'main-v2', 'story-v1')]
    assert bad['task_id'] == main['task_id'] == 10392824665
    assert main['task_id'] != story['task_id']
    assert {main['task_id'], story['task_id']}.isdisjoint({10391849167, 10392247471})
    assert bad['quality']['route'] == 'revision' and float(bad['quality']['score']) < 80
    assert bad['inspection']['checks']['required_text:date']['result'] == 'fail'
    assert bad['attachment']['sha256'] != main['attachment']['sha256']
    for stage, artifact in (('main', main), ('story', story)):
        assert state['stages'][stage]['status'] == 'accepted'
        assert artifact['inspection']['ocr']['status'] == 'completed'
        assert artifact['inspection']['inspection_status'] == 'passed'
        assert artifact['quality']['route'] == 'delivery_candidate' and float(artifact['quality']['score']) == 100
    assert state['stages']['story']['dependency_acceptances']['main'] == state['stages']['main']['acceptance']['acceptance_sha256']
    delivery = internal['delivery']; assert delivery is not None
    assert delivery['proof']['identity_verified'] is True and delivery['proof']['matches_approved_payload'] is True
    assert delivery['proof']['attachment_sha256'] == [main['attachment']['sha256'], story['attachment']['sha256']]
    before = json.loads((OUT/'v9/before-replay.json').read_text())
    assert before['state_sha256'] == row['state_sha256'] and before['action_keys'] == row['action_keys']
    summary = json.loads((OUT/'v9/final-journal-summary.json').read_text())
    assert len(summary['actions']) == 12 and all(x['state'] == 'verified' for x in summary['actions'])
    assert summary['state_sha256'] == row['state_sha256']
    assert {x['action_key'] for x in summary['actions']} == set(row['action_keys'])
    final_action = next(x for x in summary['actions'] if x['action_key'] == 'final-delivery')
    assert delivery['proof']['receipt_sha256'] == proposal_hash(delivery['receipt'])
    assert delivery['proof']['payload_sha256'] == final_action['payload_sha256']
    executions = []
    for path in sorted((OUT/'v9').glob('hamle-account-manager-*-status.json')):
        r = json.loads(path.read_text())
        completed = next((x for x in r['status'].get('conditions', []) if x['type'] == 'Completed'), {})
        assert completed.get('status') == 'True'
        executions.append({'name': r['name'], 'completed_at': r['status'].get('completionTime')})
    assert len(executions) >= 4
    last_completion = max(datetime.fromisoformat(x['completed_at'].replace('Z', '+00:00')).timestamp()
                          for x in executions)
    assert (OUT/'v9/pilot-private-readback.json').stat().st_mtime > last_completion
    assert (OUT/'v9/final-journal-summary.json').stat().st_mtime > last_completion
    inbox = 'pending_user_observation'
    confirmation_path = OUT/'recipient-confirmation.json'
    if confirmation_path.exists():
        confirmation = json.loads(confirmation_path.read_text())
        assert confirmation['thread_id'] == internal['source']['thread_id']
        assert confirmation['hamle_sent_message_id'] == delivery['receipt']['provider_id']
        assert confirmation['confirmation_source'] == 'direct_user_answer_in_current_conversation'
        assert confirmation['both_png_attachments_observed'] is True
        assert confirmation['recipient_inbox_confirmation'] == 'confirmed_by_user'
        inbox = 'confirmed_by_user'
    report = {'status': 'passed', 'scope': 'internal_technical_png_cloud_flow_only',
        'subject': internal['source']['subject'], 'thread_id': internal['source']['thread_id'],
        'source_message_id': internal['source']['message_id'],
        'basecamp_project_id': 49184053, 'task_ids': [main['task_id'], story['task_id']],
        'technical_scores': {'missing_date': bad['quality']['score'], 'corrected_main': main['quality']['score'], 'story': story['quality']['score']},
        'final_files': [{k: a['attachment'][k] for k in ('filename', 'provider_id', 'sha256', 'byte_size')} for a in (main, story)],
        'delivery_message_id': delivery['receipt']['provider_id'], 'sent_raw_mime_bytes_verified': True,
        'recipient_inbox_confirmation': inbox,
        'verified_provider_actions': 12, 'cloud_executions': executions,
        'replay_added_provider_writes': 0, 'replay_state_unchanged': True,
        'initial_runtime_error': 'QA control timestamp ordering; same real task/file reused after fix',
        'wat_live_enabled': False, 'professional_brand_or_artistic_acceptance': False,
        'actual_scheduler_trigger_acceptance': False, 'manual_cloud_wakeups_used': True,
        'paid_model_calls': 0, 'additional_cloud_budget_reserved_usd': 2,
        'cloud_invoice_reconciled': False, 'state_sha256': row['state_sha256']}
    (OUT/'acceptance-summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    check()
