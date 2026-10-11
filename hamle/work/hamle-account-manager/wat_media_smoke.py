"""Linux image build gate: real H.264 generation, probe and frame decode only."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from v11_wat_media_inspector import availability, ENV, TOOL_PATH, inspect_wat_media
from v11_wat_timed_frame import inspect_deadline_frame


def smoke():
    if not all(availability().values()):
        raise ValueError('video_tools_missing')
    with tempfile.TemporaryDirectory(prefix='hamle-wat-build-fixture-') as directory:
        path = Path(directory) / 'fixture.mp4'
        process = subprocess.run([shutil.which('ffmpeg', path=TOOL_PATH), '-nostdin', '-v', 'error',
            '-f', 'lavfi', '-i', 'color=c=white:s=16x16:r=2:d=1', '-an', '-c:v', 'libx264',
            '-threads', '1', '-pix_fmt', 'yuv420p', '-y', str(path)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False, env=ENV)
        if process.returncode:
            raise ValueError('video_fixture_generation_failed')
        data = path.read_bytes()
    result = inspect_wat_media(data, declared_mime='video/mp4',
        expected_sha256=hashlib.sha256(data).hexdigest(), brief_sha256='b' * 64,
        output_spec={'id': 'build-only-fixture', 'mime': 'video/mp4', 'width': 16, 'height': 16,
                     'duration_seconds': 1, 'frame_rate': 2, 'audio_required': False},
        source={'verified': True, 'account_id': 5898865, 'project_id': 49184053,
                'task_id': 1, 'attachment_id': 1, 'evidence_ref': 'build-only-generated-video'},
        checked_at=datetime.now(timezone.utc).isoformat(), frame_indices=[0], include_frame_bytes=True)
    if (result['technical_status'] != 'passed' or result['sampling_status'] != 'completed_sparse_only'
            or len(result.get('frame_bytes', [])) != 1
            or not result['frame_bytes'][0]['data'].startswith(b'\x89PNG\r\n\x1a\n')):
        raise ValueError('real_probe_or_frame_decode_failed')
    timed=inspect_deadline_frame(data,expected_sha256=hashlib.sha256(data).hexdigest(),
                                 deadline_seconds='1/2',include_frame_bytes=True)
    if (timed['status']!='verified_timestamp_frame' or not timed['frame']['timestamp_verified']
            or timed['frame']['elapsed_seconds']!='1/2'):
        raise ValueError('real_frame_timestamp_binding_failed')
    return {'status': 'passed', 'scope': 'generated-video-probe-and-frame-only',
            'actual_frame_pts_matched':True,
            'network_calls': 0, 'model_calls': 0, 'business_writes': 0,
            'customer_or_creative_acceptance': False}


if __name__ == '__main__':
    try:
        print(json.dumps(smoke()))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'reason': type(exc).__name__}))
        raise SystemExit(1)
