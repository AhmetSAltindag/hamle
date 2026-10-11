"""Offline scenarios using the verified WAT roster and entirely synthetic briefs.

No provider clients are imported. Nothing here grants production authority.
"""
import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / 'work' / 'hamle-account-manager'
OUT = ROOT / 'outputs' / 'wat-pilot-20261010'
sys.path.insert(0, str(APP))
from v11_intake_plan import plan_intake, intake_requirements_digest


def main():
    catalog_path = OUT / 'wat-planning-catalog.json'
    catalog = json.loads(catalog_path.read_text())
    now = datetime.now(timezone.utc)
    source = {
        'channel': 'email', 'verified': True, 'headers_origin': 'provider',
        'latest_in_thread': True, 'evidence_ref': 'SYNTHETIC-NOT-A-REAL-MESSAGE',
        'message_id': 'synthetic-wat-message', 'thread_id': 'synthetic-wat-thread',
        'fingerprint': hashlib.sha256(b'synthetic-wat-source').hexdigest(),
        'observed_at': now.isoformat(), 'automatic': False, 'own_message': False,
        'human_takeover': False, 'sender': 'selina.mircavi@watmobilite.com',
        'to': ['hamle@gamechangers.com.tr'], 'cc': ['cansu@gamechangers.com.tr'],
        'origin': {'kind': 'direct_email'},
    }
    base = {
        'brand_id': catalog['brand_id'], 'risk_area': 'routine',
        'title': 'SENTETİK YÖNLENDİRME DENEMESİ',
        'objective': 'Gerçek iş içermeyen yerel yönlendirme kontrolü.',
        'audience': 'Sentetik test kitlesi',
        'deadline': '2026-10-15 12:00 Europe/Istanbul',
        'assets_ref': 'synthetic-assets-not-approved-for-live-use',
    }
    cases = [
        ('social_static', 'wat_social', 'social', ['graphic'], 8231779952, [47658175]),
        ('social_video', 'wat_social', 'social', ['video'], 8231779952, [50486696]),
        ('social_design_then_animation', 'wat_social', 'social', ['graphic', 'animation'], 8231779952, [47658175]),
        ('non_social_motion', 'wat_motion', 'non_social_motion', ['animation'], 9381497228, [50486696]),
        ('field_design', 'wat_design', 'atl_btl_field', ['graphic'], 8234557425, [47658175]),
        ('agency_whatsapp_relay', 'wat_social', 'social', ['graphic'], 8231779952, [47658175]),
    ]
    # This route is enabled only once its offline planning contract supports
    # a motion requirement; pure graphic work must still use Projeler.
    motion = next(r for r in catalog['routes'] if r['service_id'] == 'wat_motion')
    if motion.get('required_any_kinds'):
        cases.append(('non_social_design_then_animation', 'wat_motion', 'non_social_motion',
                      ['graphic', 'animation'], 9381497228, [47658175]))
    results = []
    for name, service, category, kinds, list_id, first_assignees in cases:
        message = copy.deepcopy(source)
        if name == 'agency_whatsapp_relay':
            message['sender'] = 'ozge@gamechangers.com.tr'
            message['origin'] = {
                'kind': 'agency_relay', 'medium': 'whatsapp', 'verified': True,
                'original_actor_email': 'selina.mircavi@watmobilite.com',
                'original_actor_name': 'Selina Mirçavi',
                'attested_by': 'ozge@gamechangers.com.tr',
                'evidence_ref': 'SYNTHETIC-ATTESTATION-NOT-A-REAL-CONVERSATION',
            }
        requirements = {**copy.deepcopy(base), 'service_id': service, 'category_key': category,
                        'deliverables': []}
        for kind in kinds:
            output = {'id': kind, 'kind': kind, 'quantity': 1,
                      'formats': ['image/png' if kind == 'graphic' else 'video/mp4'],
                      'specification': 'Sentetik 1080x1920 test çıktısı.'}
            if kind != 'graphic':
                output['duration_seconds'] = 10
            requirements['deliverables'].append(output)
        requirements['agreement'] = {
            'verified': True, 'source_message_id': message['message_id'],
            'evidence_ref': 'SYNTHETIC-AGREEMENT',
            'requirements_sha256': intake_requirements_digest(requirements),
        }
        result = plan_intake(message, requirements, catalog, now=now)
        assert result['status'] == 'ready_internal_task', (name, result)
        intent, = result['intents']
        payload = intent['payload']
        assert payload['target']['project_id'] == 40694338
        assert payload['target']['list_id'] == list_id, (name, payload)
        assert payload['assignee_ids'] == first_assignees, (name, payload)
        assert payload['subscriber_ids'] == [47470759, 48793256, 52096503]
        assert intent['live_enabled'] is False
        envelope = result['brief']['reply_envelope']
        assert envelope['cc'] == ['cansu@gamechangers.com.tr']
        if 'design_then_animation' in name:
            first, second = payload['production_stages']
            assert first['assignee_ids'] == [47658175]
            assert second['assignee_ids'] == [50486696]
            assert second['depends_on'] == [first['id']]
        if name == 'agency_whatsapp_relay':
            assert envelope['to'] == ['ozge@gamechangers.com.tr']
            assert 'selina.mircavi@watmobilite.com' not in envelope['to'] + envelope['cc']
        results.append({'case': name, 'passed': True, 'list_id': list_id,
                        'initial_assignee_ids': first_assignees,
                        'production_stages': payload['production_stages'],
                        'same_thread_and_cc_preserved': True,
                        'routing_basis': ('derived_planning_assumption' if name ==
                            'non_social_design_then_animation' else 'user_choices_and_read_only_roster'),
                        'live_accepted': False})
    evidence = {
        'recorded_at': now.isoformat(), 'kind': 'OFFLINE_SYNTHETIC_CATALOG_INTEGRATION',
        'catalog_sha256': hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
        'cases': results, 'passed': len(results), 'network_calls': 0,
        'paid_model_calls': 0, 'mail_task_comment_writes': 0,
        'real_wat_request_processed': False, 'live_customer_acceptance': False,
    }
    (OUT / 'planning-validation.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': len(results), 'network_calls': 0, 'live_customer_acceptance': False}))


if __name__ == '__main__':
    main()
