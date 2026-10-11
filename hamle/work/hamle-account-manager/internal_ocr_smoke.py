"""Build-time Linux smoke: real fixture pixels/font/OCR, zero provider access."""
from datetime import datetime, timezone
import hashlib
import json
import sys

from v11_artifact_inspector import inspect_artifact
from v11_internal_quality import STRINGS, _norm, output_spec
from v11_internal_worker import fixture_png


class SmokeError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise SmokeError(reason)


def smoke():
    require(sys.platform.startswith('linux'), 'linux_container_required_for_ocr_smoke')
    rows = []
    for stage, missing in (('main', True), ('main', False), ('story', False)):
        data = fixture_png(stage, missing_date=missing)
        result = inspect_artifact(data, declared_mime='image/png',
            expected_sha256=hashlib.sha256(data).hexdigest(), brief_sha256='b' * 64,
            output_spec=output_spec(stage), checked_at=datetime.now(timezone.utc).isoformat(),
            include_text=True, source={'verified': True, 'account_id': 5898865,
                'project_id': 49184053, 'task_id': 1, 'attachment_id': 1,
                'evidence_ref': 'build-only-generated-fixture:no-provider-read'})
        row = {'stage': stage, 'intentional_missing_date': missing,
               'artifact_sha256': result['artifact']['sha256'],
               'facts': result['artifact']['facts'], 'ocr': result['ocr'],
               'inspection_status': result['inspection_status'],
               'check_results': {key: value['result'] for key, value in result['checks'].items()},
               'text_sha256': result['text_sha256']}
        # Output only hashes, statuses and parser/version metadata, never OCR text.
        print(json.dumps({'fixture_check': row}, ensure_ascii=False), flush=True)
        require(result['ocr']['status'] == 'completed', 'real_cloud_ocr_not_completed')
        expected_text = ' '.join(value for key, value in STRINGS.items() if not (missing and key == 'date'))
        require(_norm(result['extracted_text']) == _norm(expected_text), 'ocr_fixture_exact_text_mismatch')
        require(result['inspection_status'] == ('failed' if missing else 'passed'), 'ocr_fixture_route_mismatch')
        for key, check in result['checks'].items():
            expected = 'fail' if missing and key == 'required_text:date' else 'pass'
            require(check['result'] == expected, 'ocr_fixture_check_mismatch')
        rows.append(row)
    return {'status': 'passed', 'scope': 'linux-real-font-pixel-ocr-only',
            'provider_reads': 0, 'provider_writes': 0, 'model_calls': 0,
            'synthetic_attachment_binding': True, 'fixtures': rows,
            'customer_or_artistic_acceptance': False}


if __name__ == '__main__':
    try:
        print(json.dumps(smoke(), ensure_ascii=False))
    except Exception as exc:
        reason = str(exc) if isinstance(exc, SmokeError) else getattr(exc, 'code', type(exc).__name__)
        print(json.dumps({'status': 'failed', 'reason': reason, 'provider_writes': 0, 'model_calls': 0}))
        raise SystemExit(1)
