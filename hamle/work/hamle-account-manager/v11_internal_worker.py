"""Cloud-only, bounded real internal production rehearsal; customer mode absent.

Uses actual Gmail input, private Basecamp tasks/files, downloaded byte inspection,
the production stage engine and durable Firestore write fences. Test artwork is
explicitly a machine-created technical fixture, not designer/brand acceptance.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
import os
import re
import unicodedata

from runtime_authority import AUTHORIZATION_DEADLINE
from autonomy_policy import scope_digest
from cloud_store import FirestoreBackend, OperationsStore, proposal_hash
from cloud_providers import GoogleSecretManager
from v11_internal_providers import InternalProviders, SUBJECT, ACCOUNT, PROJECT, PARENT, HAMLE_ID
from v11_internal_state import InternalPilotStore
from v11_internal_quality import build_internal_brief, output_spec, build_stage_assessment_event
from v11_artifact_inspector import inspect_artifact
from v11_production_stages import initialize_production, advance_production, delivery_manifest, ready_stages

WORK = 'internal-production-20261010-01'
PHASES = ('advance', 'main-revision', 'main-corrected', 'story', 'deliver', 'status')


class PilotError(ValueError):
    pass


def need(ok, reason):
    if not ok:
        raise PilotError(reason)


def attachment_identity(receipt):
    value = receipt['provider_id']
    return int(value) if isinstance(value, str) and value.isdigit() else value


def fixture_png(stage, *, missing_date=False):
    """Visible fixed text on actual pixels; not generative/design production."""
    from PIL import Image, ImageDraw, ImageFont
    need(stage in ('main', 'story'), 'fixture_stage_invalid')
    size = (1080, 1080 if stage == 'main' else 1920)
    image = Image.new('RGB', size, 'white')
    draw = ImageDraw.Draw(image)
    font_path = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
    need(os.path.isfile(font_path), 'cloud_fixture_font_missing')
    heading = ImageFont.truetype(font_path, 60)
    regular = ImageFont.truetype(font_path, 48)
    draw.text((70, 180), 'HAMLE İÇ DENEME', font=heading, fill='black')
    if not missing_date:
        draw.text((70, 400), '12 EKİM 2026', font=heading, fill='black')
    draw.text((70, 610), 'Bu bir teknik denemedir.', font=regular, fill='black')
    stream = BytesIO(); image.save(stream, format='PNG')
    return stream.getvalue()


class InternalWorker:
    def __init__(self, ops, providers, *, clock=None, inspect=inspect_artifact, make_fixture=fixture_png):
        self.ops, self.pilot, self.providers = ops, InternalPilotStore(ops), providers
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.inspect, self.make_fixture = inspect, make_fixture

    def now(self):
        return self.clock().isoformat()

    def load(self):
        row = self.pilot.read()
        if row:
            self.providers.allowed_task_ids = frozenset(s['task']['task_id'] for s in row['state']['stages'].values() if s['task'])
        return row

    def bootstrap(self):
        old = self.load()
        if old:
            return old
        control = self.ops.control()
        need(control['mode'] == 'review_open', 'control_not_open')
        captured = self.providers.capture_source()
        source = captured['evidence']
        # This fixed rehearsal is accepted only for the exact requested content.
        def normalize(value):
            value = unicodedata.normalize('NFKD', value).casefold()
            value = ''.join(c for c in value if not unicodedata.combining(c))
            return re.sub(r'(?<=\d)\s*x\s*(?=\d)', 'x', ' '.join(value.split()).replace('×', 'x'))
        text = normalize(captured['source_text'])
        for phrase in ('1080x1080', '1080x1920', 'hamle iç deneme', '12 ekim 2026',
                       'bu bir teknik denemedir', 'png'):
            need(normalize(phrase) in text, 'internal_fixture_brief_mismatch')
        self.providers.verify_internal()
        now = self.clock()
        expires = min(now + timedelta(hours=3), datetime.fromisoformat(AUTHORIZATION_DEADLINE))
        brief = build_internal_brief(source, control['generation'], now.isoformat(), expires.isoformat())
        state = initialize_production(WORK, brief, mode='internal_pilot')
        state['internal_run'] = {'artifacts': {}, 'delivery': None, 'kind': 'technical_fixture_only',
                                 'wat_live_enabled': False, 'model_calls': 0, 'source': source}
        durable_source = {k: source[k] for k in ('subject', 'thread_id', 'message_id', 'captured_at')}
        durable_source.update(verified=True, source_fingerprint=source['snapshot_fingerprint'],
                              control_generation=control['generation'], expires_at=expires.isoformat(),
                              evidence_ref='gmail-read:' + source['message_id'])
        return self.pilot.initialize(state, durable_source)

    def guard(self):
        row = self.load()
        need(row is not None, 'pilot_missing')
        control = self.ops.control()
        need(control['generation'] == row['generation'] and control['mode'] == 'review_open', 'human_stop_or_takeover')
        source = self.providers.capture_source()['evidence']
        pinned = row['state']['internal_run']['source']
        need(all(source.get(k) == pinned.get(k) for k in
                 ('subject', 'thread_id', 'message_id', 'snapshot_fingerprint', 'source_sha256', 'latest_to', 'latest_cc')),
             'latest_source_changed')
        need(source.get('hamle_direct_recipient') is True, 'hamle_not_direct_recipient')
        self.providers.verify_internal()
        return source

    def action(self, key, payload, perform, verify):
        row = self.pilot.prepare_action(key, payload)
        if row['state'] == 'verified':
            return row
        need(row['state'] == 'prepared', 'prior_write_requires_readonly_reconciliation')
        def before():
            self.guard()
            started = self.pilot.begin_action(key, row['payload_sha256'])
            need(started['state'] == 'started' and started.get('dispatch_allowed') is True, 'write_not_new')
        try:
            receipt = perform(before)
            self.pilot.record_receipt(key, receipt)
            readback = verify(receipt)
            need(readback.get('identity_verified') is True and readback.get('matches_approved_payload') is True,
                 'provider_readback_unconfirmed')
            proof = dict(readback, verified=True, action_key=key, payload_sha256=row['payload_sha256'],
                         receipt_sha256=proposal_hash(receipt), evidence_ref='provider-readback:' + key)
            return self.pilot.finish_action(key, receipt, proof)
        except Exception:
            try:
                self.pilot.fail_action(key, 'provider_outcome_requires_reconciliation')
            except Exception:
                pass  # Preserve the original hold; a prepared action never dispatched.
            raise

    def save_state(self, state, row):
        return self.pilot.save(state, expected_revision=row['revision'])

    def event(self, kind, stage, **fields):
        row = self.load(); state = row['state']
        ev = dict(id=WORK + ':' + str(state['revision']) + ':' + kind, type=kind,
                  work_id=WORK, expected_revision=state['revision'], stage_id=stage,
                  actor_id=HAMLE_ID, verified=True, evidence_ref='internal-cloud:' + kind,
                  occurred_at=self.now(), **fields)
        result = advance_production(state, ev)
        need(result['status'] == 'applied', result['reason'] or 'production_event_held')
        return self.save_state(result['state'], row)

    def start_stage(self, stage):
        row = self.load(); state = row['state']; record = state['stages'][stage]
        if record['status'] != 'waiting':
            return row
        need(stage in [x['id'] for x in ready_stages(state)], 'dependency_not_accepted')
        if record['task'] is None:
            title = f'[HAMLE İÇ ÜRETİM] {"Ana görsel" if stage == "main" else "Story uyarlaması"} / 20261010-01'
            dims = '1080×1080' if stage == 'main' else '1080×1920'
            body = (f'Yalnız Gamechangers iç teknik denemesi. {dims} PNG.\n'
                    'Görünür metinler: HAMLE İÇ DENEME / 12 EKİM 2026 / Bu bir teknik denemedir.\n'
                    'Üretici: Hamle tarafından hazırlanan kontrollü teknik dosya; insan tasarımcı çalışması değildir.\n'
                    + ('Önce ana görsel kabulü gerekir.\n' if stage == 'story' else 'Tarih eksik ilk dosya kasıtlı hata örneğidir.\n')
                    + 'Brief SHA256: ' + state['brief_sha256'])
            done = self.action(stage + '-task', {'stage': stage, 'title': title, 'body': body},
                lambda before: self.providers.create_task(title, body, before_post=before),
                lambda r: self.providers.read_task(r['provider_id'], title, body))
            self.event('bind_stage_task', stage, task={
                'provider': 'basecamp', 'verified': True, 'evidence_ref': done['proof']['evidence_ref'],
                'account_id': ACCOUNT, 'project_id': PROJECT, 'list_id': PARENT,
                'task_id': int(done['receipt']['provider_id']), 'customer_visible': False,
                'assignee_ids': [HAMLE_ID], 'brief_sha256': state['brief_sha256']})
        return self.event('begin_stage', stage)

    def produce(self, stage, version, *, missing_date=False):
        if stage == 'main' and version == 'v2':
            prior = self.load()['state']['internal_run']['artifacts'].get('main-v1')
            need(prior is not None and prior['expected_failure'] is True
                 and prior['quality']['route'] == 'revision', 'verified_revision_required_before_correction')
            self.ensure_quality_note('main-v1')
        self.start_stage(stage)
        row = self.load(); state = row['state']; record = state['stages'][stage]
        tag = stage + '-' + version
        if tag in state['internal_run']['artifacts']:
            if state['stages'][stage]['status'] == 'quality_held':
                self._assess_cached(tag)
            self.ensure_quality_note(tag)
            return self.load()
        need(record['status'] in ('running', 'changes_requested'), 'stage_not_ready_for_submission')
        data = self.make_fixture(stage, missing_date=missing_date)
        sha = hashlib.sha256(data).hexdigest()
        name = 'hamle-' + tag + '.png'
        upload = self.action(tag + '-upload', {'filename': name, 'sha256': sha, 'size_bytes': len(data)},
            lambda before: self.providers.upload_attachment(name, data, 'image/png', before_post=before),
            lambda receipt: {'identity_verified': True, 'matches_approved_payload':
                receipt.get('sha256') == sha and receipt.get('byte_size') == len(data),
                'verification_kind': 'authenticated_upload_response_only'})
        body = ('İç deneme dosyası ' + tag + '. Kontrollü teknik örnektir.\n'
                + 'Dosya SHA256: ' + sha + '\nBrief SHA256: ' + state['brief_sha256'])
        task_id = record['task']['task_id']
        comment = self.action(tag + '-submission-comment', {'task_id': task_id, 'body': body, 'upload': upload['receipt']},
            lambda before: self.providers.create_comment(task_id, body, before_post=before, attachments=[upload['receipt']]),
            lambda r: self.providers.read_comment(r['provider_id'], task_id, body, attachments=[upload['receipt']]))
        receipt = comment['proof'].get('attachment_receipts', [upload['receipt']])[0]
        real = self.providers.download_attachment(task_id, comment['receipt']['provider_id'], receipt)
        need(real == data, 'actual_uploaded_file_mismatch')
        submitted_at = self.now()
        inspected = self.inspect(real, declared_mime='image/png', expected_sha256=sha,
            brief_sha256=state['brief_sha256'], output_spec=output_spec(stage), inspected_at=self.now(), include_text=True,
            source={'verified': True, 'account_id': ACCOUNT, 'project_id': PROJECT, 'task_id': task_id,
                    'attachment_id': attachment_identity(receipt), 'evidence_ref': comment['proof']['evidence_ref']})
        need(inspected['ocr']['status'] == 'completed', 'real_cloud_ocr_not_completed')
        item = dict(inspected['artifact'], kind='graphic', purpose='final', provider_attachment_id=str(receipt['provider_id']),
            inspection={'verified': True, 'bytes_read': True, 'readable': True, 'sha256': sha, 'size_bytes': len(real),
                        'evidence_ref': inspected['artifact']['evidence_ref'], 'inspected_at': inspected['checked_at']})
        submission = {'verified': True, 'evidence_ref': comment['proof']['evidence_ref'], 'producer_id': HAMLE_ID,
            'task_id': task_id, 'origin': 'internal_fixture', 'brief_sha256': state['brief_sha256'],
            'dependency_acceptances': copy.deepcopy(record['dependency_acceptances']), 'submitted_at': submitted_at,
            'artifact': {'version': version, 'sha256': sha}, 'outputs': [item], 'manifest_sha256': scope_digest([item])}
        self.event('submit_outputs', stage, submission=submission)
        row = self.load(); state = row['state']
        fresh_source = self.guard()
        control = self.ops.control()
        assessment_time = self.now()
        ev = build_stage_assessment_event(state, stage, inspected, fresh_source, assessment_time, row['generation'],
            control={'verified': True, 'generation': control['generation'], 'mode': 'active' if control['mode'] == 'review_open' else 'stopped',
                     'observed_at': assessment_time, 'evidence_ref': 'firestore-control:' + str(control['generation']), 'write_state': 'new'})
        assessed = advance_production(state, ev)
        need(assessed['status'] == 'applied', assessed['reason'] or 'internal_quality_failed')
        state = assessed['state']
        state['internal_run']['artifacts'][tag] = {
            'task_id': task_id, 'submission_comment_id': comment['receipt']['provider_id'],
            'attachment': receipt, 'inspection': inspected, 'expected_failure': missing_date,
            'quality': state['stages'][stage]['quality']}
        self.save_state(state, row)
        expected = 'changes_requested' if missing_date else 'accepted'
        need(state['stages'][stage]['status'] == expected, 'unexpected_quality_route')
        self.ensure_quality_note(tag)
        return self.load()

    def _assess_cached(self, tag):
        """Read-only QA recovery for the same persisted provider file/version."""
        row = self.load(); state = row['state']; a = state['internal_run']['artifacts'][tag]
        stage = tag.split('-', 1)[0]
        need(state['stages'][stage]['status'] == 'quality_held', 'only_held_quality_may_be_reassessed')
        need(state['stages'][stage]['submission']['artifact']['sha256'] == a['attachment']['sha256'], 'cached_submission_changed')
        real = self.providers.download_attachment(a['task_id'], a['submission_comment_id'], a['attachment'])
        inspected = self.inspect(real, declared_mime='image/png', expected_sha256=a['attachment']['sha256'],
            brief_sha256=state['brief_sha256'], output_spec=output_spec(stage), inspected_at=self.now(), include_text=True,
            source={'verified': True, 'account_id': ACCOUNT, 'project_id': PROJECT, 'task_id': a['task_id'],
                    'attachment_id': attachment_identity(a['attachment']), 'evidence_ref': 'reinspection:' + tag})
        fresh_source = self.guard(); control = self.ops.control(); assessed_at = self.now()
        ev = build_stage_assessment_event(state, stage, inspected, fresh_source, assessed_at, row['generation'],
            control={'verified': True, 'generation': control['generation'], 'mode': 'active' if control['mode'] == 'review_open' else 'stopped',
                     'observed_at': assessed_at, 'evidence_ref': 'firestore-control:' + str(control['generation']), 'write_state': 'new'})
        result = advance_production(state, ev)
        need(result['status'] == 'applied', result['reason'] or 'quality_reassessment_held')
        updated = result['state']; artifact = updated['internal_run']['artifacts'][tag]
        artifact.setdefault('quality_history', []).append({'quality': a['quality'], 'inspection_sha256': a['inspection']['evidence_sha256']})
        artifact.update(inspection=inspected, quality=updated['stages'][stage]['quality'])
        self.save_state(updated, row)
        need(updated['stages'][stage]['status'] == ('changes_requested' if a['expected_failure'] else 'accepted'),
             'reassessment_not_accepted')
        return self.load()

    def ensure_quality_note(self, tag):
        state = self.load()['state']; artifact = state['internal_run']['artifacts'][tag]
        need(artifact['quality']['route'] == ('revision' if artifact['expected_failure'] else 'delivery_candidate')
             and artifact['quality']['score'] is not None, 'quality_note_requires_decided_route')
        note = self.quality_note_body(tag, artifact, state); task_id = artifact['task_id']
        return self.action(tag + '-quality-note', {'task_id': task_id, 'body': note},
            lambda before: self.providers.create_comment(task_id, note, before_post=before),
            lambda r: self.providers.read_comment(r['provider_id'], task_id, note))

    def deliver(self):
        row = self.load(); state = row['state']
        if state['internal_run']['delivery']:
            return row
        manifest = delivery_manifest(state)
        need(len(manifest['outputs']) == 2, 'two_final_outputs_required')
        failed = state['internal_run']['artifacts'].get('main-v1')
        need(failed is not None and failed['expected_failure'] is True
             and failed['quality']['route'] == 'revision', 'revision_proof_required_for_test_delivery')
        body = ('Merhaba Ahmet,\n\n'
                'İç üretim denemesinin 1080×1080 ana görseli ve buna bağlı 1080×1920 story dosyası ekte.\n'
                'İlk dosyadaki eksik tarih revizyona döndü; düzeltilen dosya kontrolü geçtikten sonra story aşaması açıldı.\n'
                'Eklerin boyutları, zorunlu metinleri ve Basecamp’teki dosyalarla birebir aynı olduğu doğrulandı.\n'
                'Bunlar kontrollü teknik deneme dosyalarıdır; WAT için tasarım veya marka kalite onayı değildir.\n\nHamle')
        pinned = state['internal_run']['source']
        payload = {'body': body, 'thread_id': pinned['thread_id'], 'source_fingerprint': pinned['snapshot_fingerprint'],
                   'files': [{k: a['attachment'][k] for k in ('filename', 'sha256', 'byte_size')} for a in
                             (state['internal_run']['artifacts']['main-v2'], state['internal_run']['artifacts']['story-v1'])]}
        prior = self.pilot.prepare_action('final-delivery', payload)
        if prior['state'] == 'verified':
            # The provider readback already proved exact sent bytes. Our own
            # sent mail is now latest, so never demand/interpret it as new input.
            state['internal_run']['delivery'] = {'receipt': prior['receipt'], 'proof': prior['proof'], 'completed_at': self.now()}
            return self.save_state(state, row)
        need(prior['state'] == 'prepared', 'prior_delivery_requires_readonly_reconciliation')
        files = []
        for stage, version in (('main', 'v2'), ('story', 'v1')):
            tag = stage + '-' + version
            a = state['internal_run']['artifacts'][tag]
            # The exact immutable quality note must be confirmed before dispatch.
            proof = self.pilot.prepare_action(tag + '-quality-note', {
                'task_id': a['task_id'], 'body': self.quality_note_body(tag, a, state)})
            need(proof['state'] == 'verified', 'quality_note_not_confirmed')
            real = self.providers.download_attachment(a['task_id'], a['submission_comment_id'], a['attachment'])
            current = self.inspect(real, declared_mime='image/png', expected_sha256=a['attachment']['sha256'],
                brief_sha256=state['brief_sha256'], output_spec=output_spec(stage), inspected_at=self.now(),
                source={'verified': True, 'account_id': ACCOUNT, 'project_id': PROJECT, 'task_id': a['task_id'],
                        'attachment_id': attachment_identity(a['attachment']), 'evidence_ref': 'final-basecamp-byte-read:' + tag})
            need(current['inspection_status'] == 'passed' and current['ocr']['status'] == 'completed', 'final_bytes_not_accepted')
            files.append({'filename': a['attachment']['filename'], 'data': real, 'content_type': 'image/png'})
        source = self.guard()
        done = self.action('final-delivery', payload,
            lambda before: self.providers.send_delivery(source, body, files, before_post=before),
            lambda r: self.providers.read_delivery(r['provider_id'], source, body, files))
        row = self.load(); state = row['state']
        state['internal_run']['delivery'] = {'receipt': done['receipt'], 'proof': done['proof'], 'completed_at': self.now()}
        return self.save_state(state, row)

    @staticmethod
    def quality_note_body(tag, artifact, state):
        return ('İç teknik kontrol: ' + tag + '\nİç teknik uygunluk puanı: ' + str(artifact['quality']['score']) + '/100\n'
                + ('Revizyon: zorunlu 12 EKİM 2026 tarihi dosyada tespit edilmedi. Tarihi ekleyip yeniden gönderin.\n'
                   if artifact['expected_failure'] else 'PNG boyutu ve üç zorunlu metin gerçek dosyada doğrulandı.\n')
                +
                'Bu sonuç WAT marka/sanatsal kalite kabulü değildir.\nDosya SHA256: ' + artifact['attachment']['sha256']
                + '\nKontrol SHA256: ' + scope_digest(artifact['quality']))

    def advance_if_active(self):
        """One cloud-selected step per wakeup; no bootstrap from timer alone."""
        row = self.load()
        if row is None or row['state']['internal_run']['delivery']:
            return None
        for tag in row['state']['internal_run']['artifacts']:
            current = self.load()['state']; stage = current['stages'][tag.split('-', 1)[0]]
            if (stage['status'] == 'quality_held' and stage['submission']['artifact']['sha256'] ==
                    current['internal_run']['artifacts'][tag]['attachment']['sha256']):
                self._assess_cached(tag)
            self.ensure_quality_note(tag)
        artifacts = row['state']['internal_run']['artifacts']
        phase = ('main-revision' if 'main-v1' not in artifacts else
                 'main-corrected' if 'main-v2' not in artifacts else
                 'story' if 'story-v1' not in artifacts else 'deliver')
        return self.run(phase)

    def run(self, phase):
        need(phase in PHASES, 'unknown_phase')
        if phase == 'status':
            return self.pilot.summary()
        self.bootstrap()
        if phase == 'advance':
            return self.advance_if_active() or self.pilot.summary()
        if phase == 'main-revision': self.produce('main', 'v1', missing_date=True)
        elif phase == 'main-corrected': self.produce('main', 'v2')
        elif phase == 'story': self.produce('story', 'v1')
        elif phase == 'deliver': self.deliver()
        return self.pilot.summary()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=PHASES, required=True)
    args = parser.parse_args()
    need(os.environ.get('HAMLE_ALLOW_FIRESTORE_OPERATIONS') == 'yes'
         and os.environ.get('HAMLE_PROJECT_ID') == 'gamechangers-hamle'
         and os.environ.get('HAMLE_FIRESTORE_DATABASE') == 'hamle-operations'
         and os.environ.get('CLOUD_RUN_JOB') == 'hamle-account-manager',
         'authorized_cloud_runtime_required')
    backend = FirestoreBackend(opt_in='yes'); secrets = GoogleSecretManager()
    try:
        ops = OperationsStore(backend)
        worker = InternalWorker(ops, InternalProviders(secrets, claim_refresh=ops.claim_credential_refresh))
        result = worker.run(args.phase)
        print(json.dumps({'status': 'completed', 'phase': args.phase, 'summary': result}, ensure_ascii=False))
    finally:
        secrets.close(); backend.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # Only code-defined reasons; no provider payloads, raw mail or secrets.
        reason = getattr(exc, 'code', None)
        if isinstance(exc, PilotError): reason = str(exc)
        print(json.dumps({'status': 'held', 'reason': reason or type(exc).__name__, 'automatic_retry': False}))
        raise SystemExit(2)
