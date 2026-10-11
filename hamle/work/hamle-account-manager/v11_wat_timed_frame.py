"""Actual presentation-timestamp evidence for a frame at/before a deadline.

This is a positive-witness aid, never proof that absent OCR means absent text.
No network, model, customer write, full-content approval or automatic delivery.
"""
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

import v11_wat_media_inspector as media

VERSION = 'wat-timed-frame-v1'
MAX_PROBE = 2 * 1024 * 1024
MAX_FRAMES = 3600


class TimedFrameError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def need(ok, code):
    if not ok:
        raise TimedFrameError(code)


def ratio(value, code):
    try:
        need(type(value) in (str, int), code)
        result = Fraction(str(value))
        need(abs(result) <= 1000000, code)
        return result
    except (ValueError, ZeroDivisionError, OverflowError):
        raise TimedFrameError(code) from None


def select_deadline_frame(probe, deadline_seconds='2'):
    """Select from decoded frame PTS, never index divided by average FPS."""
    deadline = ratio(deadline_seconds, 'timing_deadline_invalid')
    need(0 < deadline <= 10, 'timing_deadline_invalid')
    need(isinstance(probe, dict) and isinstance(probe.get('streams'), list)
         and len(probe['streams']) == 1 and isinstance(probe.get('format'), dict), 'timing_probe_invalid')
    stream = probe['streams'][0]
    need(stream.get('codec_type') == 'video' and stream.get('codec_name') == 'h264', 'timing_stream_unsupported')
    need(type(stream.get('width')) is int and type(stream.get('height')) is int
         and 0 < stream['width'] * stream['height'] <= media.MAX_VIDEO_PIXELS, 'timing_dimensions_invalid')
    fmt = probe['format']
    need('mp4' in str(fmt.get('format_name', '')).split(','), 'timing_container_unsupported')
    duration = ratio(fmt.get('duration'), 'timing_duration_invalid')
    need(0 < duration <= 60 and deadline <= duration, 'timing_duration_invalid')
    origin = ratio(fmt.get('start_time'), 'timing_origin_unknown')
    need(abs(origin) <= 1, 'timing_origin_out_of_scope')
    time_base = ratio(stream.get('time_base'), 'timing_timebase_invalid')
    need(0 < time_base <= 1, 'timing_timebase_invalid')
    frames = probe.get('frames')
    need(isinstance(frames, list) and 0 < len(frames) <= MAX_FRAMES, 'timing_frame_count_invalid')
    rows, previous = [], None
    for index, frame in enumerate(frames):
        need(isinstance(frame, dict) and frame.get('media_type') == 'video'
             and type(frame.get('best_effort_timestamp')) is int, 'timing_frame_pts_missing')
        ticks = frame['best_effort_timestamp']; pts = ticks * time_base
        need(previous is None or pts > previous, 'timing_pts_not_strictly_increasing')
        previous = pts
        elapsed = pts - origin
        need(-1 <= elapsed <= 61, 'timing_pts_out_of_scope')
        if 0 <= elapsed <= deadline:
            rows.append({'frame_index':index, 'pts_ticks':ticks, 'time_base':str(time_base),
                'presentation_seconds':str(pts), 'elapsed_seconds':str(elapsed)})
    need(rows, 'timing_no_frame_at_or_before_deadline')
    chosen = rows[-1]
    return {**chosen, 'deadline_seconds':str(deadline), 'timeline_origin_seconds':str(origin),
            'timeline_origin':'container_format_start_time', 'decoded_pts_count':len(frames),
            'selected_from_actual_pts':True}


def _run(mode, path, digest, index=None):
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--wat-timed-frame', mode, str(path), digest]
    if index is not None: command.append(str(index))
    limit = MAX_PROBE if mode == 'pts' else media.MAX_FRAME_BYTES + 4096
    try:
        run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=25, check=False, env=media.ENV)
        need(run.returncode == 0 and 0 < len(run.stdout) <= limit, 'timing_parser_failed_or_limited')
        return run.stdout
    except (subprocess.TimeoutExpired, OSError):
        raise TimedFrameError('timing_parser_unavailable_or_timeout') from None


def inspect_deadline_frame(data, *, expected_sha256, deadline_seconds='2', include_frame_bytes=False):
    need(type(data) is bytes and 24 <= len(data) <= media.MAX_VIDEO_BYTES, 'timing_bytes_invalid')
    need(isinstance(expected_sha256, str) and re.fullmatch('[0-9a-f]{64}', expected_sha256)
         and hashlib.sha256(data).hexdigest() == expected_sha256, 'timing_bytes_changed')
    need(type(include_frame_bytes) is bool, 'timing_include_flag_invalid')
    result = {'version':VERSION, 'artifact_sha256':expected_sha256, 'status':'held',
        'reason':None, 'frame':None, 'message_presence_verified':False,
        'exact_first_appearance_verified':False, 'absence_is_failure':False,
        'full_content_review':False, 'delivery_authorized':False, 'model_calls':0}
    frame_data = None
    try:
        media._mp4_signature(data)
        with tempfile.TemporaryDirectory(prefix='hamle-wat-media-') as directory:
            path=Path(directory).resolve()/'input.mp4';path.write_bytes(data);path.chmod(0o600)
            raw=_run('pts',path,expected_sha256)
            probe=json.loads(raw)
            chosen=select_deadline_frame(probe,deadline_seconds)
            packed=_run('frame',path,expected_sha256,chosen['frame_index'])
            need(len(packed) >= 5, 'timing_frame_response_invalid')
            header_size=struct.unpack('>I',packed[:4])[0]
            need(0 < header_size <= 2048 and len(packed) > header_size+4, 'timing_frame_response_invalid')
            meta=json.loads(packed[4:4+header_size]);frame_data=packed[4+header_size:]
            actual=ratio(meta.get('showinfo_pts'), 'timing_showinfo_pts_invalid')*ratio(meta.get('showinfo_time_base'),'timing_showinfo_timebase_invalid')
            need(actual == Fraction(chosen['presentation_seconds']), 'timing_probe_decode_pts_mismatch')
            need(frame_data.startswith(b'\x89PNG\r\n\x1a\n') and len(frame_data) <= media.MAX_FRAME_BYTES, 'timing_frame_png_invalid')
            result.update(status='verified_timestamp_frame', reason='content_presence_still_requires_ocr_or_visual_evidence',
                probe_sha256=hashlib.sha256(raw).hexdigest(),
                frame={**chosen,'sha256':hashlib.sha256(frame_data).hexdigest(),'byte_size':len(frame_data),
                    'ffmpeg_showinfo_pts_verified':True,'timestamp_verified':True})
    except (TimedFrameError,media.MediaInspectionError) as error:
        result['reason']=error.code
        frame_data=None
    except (ValueError,TypeError,KeyError,IndexError,OverflowError):
        result['reason']='timing_evidence_invalid';frame_data=None
    result['evidence_sha256']=hashlib.sha256(json.dumps(result,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if include_frame_bytes and frame_data is not None:result['frame_data']=frame_data
    return result


def _child():
    need(len(sys.argv) in (5,6) and sys.argv[1]=='--wat-timed-frame','timing_child_arguments_invalid')
    mode,path,digest=sys.argv[2],Path(sys.argv[3]),sys.argv[4]
    need(mode in ('pts','frame') and len(sys.argv)==(6 if mode=='frame' else 5),'timing_child_arguments_invalid')
    need(path.is_absolute() and path.name=='input.mp4' and path.parent.name.startswith('hamle-wat-media-')
         and path.is_file() and not path.is_symlink() and path.parent.resolve()==path.parent
         and 24<=path.stat().st_size<=media.MAX_VIDEO_BYTES,'timing_child_file_invalid')
    need(re.fullmatch('[0-9a-f]{64}',digest),'timing_child_hash_invalid')
    media._child_limits()
    with path.open('rb') as handle:need(hashlib.file_digest(handle,'sha256').hexdigest()==digest,'timing_child_bytes_changed')
    executable=shutil.which('ffprobe' if mode=='pts' else 'ffmpeg',path=media.TOOL_PATH)
    need(executable is not None,'timing_tool_unavailable')
    if mode=='pts':
        command=[executable,*media._common_args(path),'-select_streams','v:0','-show_frames','-show_entries',
            'format=format_name,duration,start_time:stream=index,codec_name,codec_type,time_base,width,height:frame=media_type,best_effort_timestamp','-of','json']
        limit=MAX_PROBE
    else:
        need(re.fullmatch('[0-9]{1,4}',sys.argv[5]) and int(sys.argv[5])<MAX_FRAMES,'timing_child_frame_invalid')
        index=int(sys.argv[5])
        command=[executable,'-nostdin','-copyts',*media._common_args(path),'-loglevel','info','-map','0:v:0','-an','-sn','-dn',
            '-filter_threads','1','-vf',f'select=eq(n\\,{index}),showinfo','-frames:v','1','-vsync','0',
            '-threads','1','-c:v','png','-f','image2pipe','pipe:1']
        limit=media.MAX_FRAME_BYTES
    with tempfile.TemporaryFile(mode='w+b') as output,tempfile.TemporaryFile(mode='w+b') as errors:
        run=subprocess.run(command,stdin=subprocess.DEVNULL,stdout=output,stderr=errors,timeout=20,check=False,env=media.ENV)
        need(run.returncode==0,'timing_tool_failed')
        output.seek(0);raw=output.read(limit+1)
        need(0<len(raw)<=limit,'timing_tool_output_invalid')
        if mode=='frame':
            errors.seek(0);log=errors.read(1024*1024+1)
            need(len(log)<=1024*1024,'timing_log_exceeds_limit')
            text=log.decode('utf-8','replace')
            bases=re.findall(r'Parsed_showinfo[^\n]*config in time_base:\s*([0-9]+/[0-9]+)',text)
            pts=re.findall(r'Parsed_showinfo[^\n]*\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:',text)
            need(len(bases)==1 and len(pts)==1,'timing_showinfo_ambiguous')
            header=json.dumps({'showinfo_pts':pts[0],'showinfo_time_base':bases[0]}).encode()
            raw=struct.pack('>I',len(header))+header+raw
    sys.stdout.buffer.write(raw)


if __name__=='__main__':
    try:_child()
    except Exception:
        sys.stdout.write('{"error":"timing_child_failed_or_limited"}')
        raise SystemExit(1)
