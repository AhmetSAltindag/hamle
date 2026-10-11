"""Narrow WAT media evidence, never a brand/creative score or delivery grant.

PNG/JPEG reuse the actual-byte inspector. Optional local MP4 probing and sparse
frame extraction remain explicitly incomplete content review. No provider/model
I/O, install, or cloud mutation occurs. Trusted adapters own source authority.
"""
from __future__ import annotations
import copy
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

import v11_artifact_inspector as byte_inspector

VERSION = "wat-media-inspector-v1"
VIDEO_MIME = "video/mp4"
MAX_VIDEO_BYTES = 64 * 1024 * 1024
MAX_VIDEO_SECONDS = 60
MAX_VIDEO_PIXELS = 3840 * 2160
MAX_FPS = 60
MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_FRAME_SAMPLES = 8
TOOL_PATH = "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
ENV = {"PATH": TOOL_PATH, "LANG": "C.UTF-8", "OMP_THREAD_LIMIT": "1", "PYTHONDONTWRITEBYTECODE": "1"}


class MediaInspectionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(ok, reason):
    if not ok:
        raise MediaInspectionError(reason)


def _hash(value):
    return byte_inspector._digest(value)


def availability():
    """Presence only, not successful decode/installation verification."""
    return {name: shutil.which(name, path=TOOL_PATH) is not None for name in ("ffprobe", "ffmpeg")}


def _ratio(value, reason):
    try:
        _need(type(value) in (str, int, float), reason)
        result = Fraction(str(value))
        _need(result >= 0, reason)
        return result
    except (ValueError, ZeroDivisionError, OverflowError):
        raise MediaInspectionError(reason) from None


def _mp4_signature(data):
    _need(len(data) >= 24, "video_mp4_header_invalid")
    size, kind = struct.unpack(">I4s", data[:8])
    _need(kind == b"ftyp" and 16 <= size <= min(len(data), 4096), "video_mp4_header_invalid")
    _need(data[8:12] in (b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"M4V "),
          "video_mp4_brand_unsupported")


def _video_spec(spec):
    allowed = {"id", "mime", "min_bytes", "max_bytes", "width", "height", "duration_seconds",
               "duration_tolerance_seconds", "audio_required", "frame_rate"}
    _need(isinstance(spec, dict) and set(spec).issubset(allowed)
          and byte_inspector._text(spec.get("id")) and spec.get("mime") == VIDEO_MIME,
          "video_output_spec_invalid")
    low, high = spec.get("min_bytes", 24), spec.get("max_bytes", MAX_VIDEO_BYTES)
    _need(type(low) is int and type(high) is int and 24 <= low <= high <= MAX_VIDEO_BYTES,
          "video_spec_size_invalid")
    out = copy.deepcopy(spec)
    out.update(min_bytes=low, max_bytes=high)
    for key in ("width", "height"):
        if key in spec:
            _need(type(spec[key]) is int and 0 < spec[key] <= 3840, "video_spec_dimensions_invalid")
    for key in ("duration_seconds", "duration_tolerance_seconds", "frame_rate"):
        if key in spec:
            value = _ratio(spec[key], "video_spec_number_invalid")
            _need(0 <= value <= (1 if key == "duration_tolerance_seconds" else 60)
                  and (value > 0 or key == "duration_tolerance_seconds"), "video_spec_number_invalid")
            out[key] = str(value)
    if "duration_tolerance_seconds" in spec:
        _need("duration_seconds" in spec, "video_duration_required_for_tolerance")
    if "duration_seconds" in spec:
        out.setdefault("duration_tolerance_seconds", "1/20")
    if "audio_required" in spec:
        _need(type(spec["audio_required"]) is bool, "video_spec_audio_invalid")
    return out


def _video_facts(probe):
    _need(isinstance(probe, dict) and isinstance(probe.get("streams"), list)
          and 1 <= len(probe["streams"]) <= 2 and all(isinstance(s, dict) for s in probe["streams"])
          and isinstance(probe.get("format"), dict),
          "video_probe_incomplete")
    container = probe["format"].get("format_name")
    _need(isinstance(container, str) and "mp4" in container.split(","), "video_container_not_mp4")
    streams = probe["streams"]
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    _need(len(videos) == 1 and len(audios) <= 1 and len(videos) + len(audios) == len(streams),
          "video_stream_layout_unsupported")
    video = videos[0]
    disposition = video.get("disposition", {})
    _need(video.get("codec_name") == "h264" and isinstance(disposition, dict)
          and disposition.get("attached_pic", 0) == 0,
          "video_codec_unsupported")
    if audios:
        _need(audios[0].get("codec_name") == "aac", "video_audio_codec_unsupported")
    width, height = video.get("width"), video.get("height")
    _need(type(width) is int and type(height) is int and 0 < width <= 3840 and 0 < height <= 3840
          and width * height <= MAX_VIDEO_PIXELS, "video_dimensions_exceed_limit")
    sar = video.get("sample_aspect_ratio", "1:1")
    _need(sar in ("1:1", "1/1"), "non_square_video_pixels_require_review")
    side_data = video.get("side_data_list", [])
    _need(isinstance(side_data, list) and all(isinstance(s, dict) for s in side_data),
          "video_rotation_ambiguous")
    rotations = [s["rotation"] for s in side_data if "rotation" in s]
    _need(len(rotations) <= 1, "video_rotation_ambiguous")
    rotation = rotations[0] if rotations else 0
    _need(type(rotation) in (int, float) and rotation % 90 == 0, "video_rotation_unsupported")
    rotation = int(rotation) % 360
    shown_width, shown_height = (height, width) if rotation in (90, 270) else (width, height)
    duration = _ratio(video.get("duration", probe["format"].get("duration")), "video_duration_unknown")
    container_duration = _ratio(probe["format"].get("duration"), "video_duration_unknown")
    _need(0 < duration <= MAX_VIDEO_SECONDS and 0 < container_duration <= MAX_VIDEO_SECONDS,
          "video_duration_exceeds_limit")
    _need(abs(duration - container_duration) <= 1, "video_duration_metadata_disagrees")
    fps = _ratio(video.get("avg_frame_rate"), "video_frame_rate_unknown")
    _need(0 < fps <= MAX_FPS, "video_frame_rate_exceeds_limit")
    return {"width": shown_width, "height": shown_height, "encoded_width": width,
            "encoded_height": height, "rotation": rotation, "duration_seconds": str(duration),
            "container_duration_seconds": str(container_duration), "frame_rate": str(fps),
            "audio_present": bool(audios), "video_codec": "h264", "audio_codec": "aac" if audios else None,
            "verification_level": "local_container_stream_metadata", "full_decode_verified": False}


def _common_args(path):
    # Generated local file only. External MOV data references stay disabled.
    return ["-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov",
            "-enable_drefs", "0", "-use_absolute_path", "0", "-threads", "1", "-f", "mov", "-i", str(path)]


def _run_child(mode, path, digest, index=None):
    command = [sys.executable, "-B", str(Path(__file__).resolve()), "--bounded-media", mode, str(path), digest]
    if index is not None:
        command.append(str(index))
    limit = MAX_FRAME_BYTES if mode == "frame" else 100_000
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=25, check=False, env=ENV)
        _need(len(result.stdout) <= limit, "media_parser_output_exceeds_limit")
        if result.returncode != 0:
            try:
                failure = json.loads(result.stdout)
                code = failure["error"]
                _need(isinstance(code, str) and re.fullmatch(r"[a-z_]+", code), "media_parser_failed")
            except (ValueError, TypeError, KeyError):
                code = "media_parser_failed"
            raise MediaInspectionError(code)
        return result.stdout
    except subprocess.TimeoutExpired:
        raise MediaInspectionError("media_parser_timeout") from None
    except OSError:
        raise MediaInspectionError("media_parser_unavailable") from None


def _probe(path, digest):
    try:
        return json.loads(_run_child("probe", path, digest))
    except MediaInspectionError:
        raise
    except (ValueError, TypeError):
        raise MediaInspectionError("video_probe_json_invalid") from None


def _seal(result):
    result["evidence_sha256"] = _hash(result)
    return result


def inspect_wat_media(data, *, declared_mime, expected_sha256, brief_sha256,
                      output_spec, source, checked_at, include_text=False,
                      frame_indices=(), include_frame_bytes=False):
    """JSON evidence plus optional in-memory frame_bytes (excluded from its hash).

    Image byte failures raise the existing InspectionError. Unsupported formats
    and unavailable video dependencies return held evidence, never a false pass.
    No result here is a complete semantic assessment or authorization to deliver.
    """
    _need(type(data) is bytes and 0 < len(data) <= MAX_VIDEO_BYTES, "media_byte_limit_invalid")
    _need(isinstance(declared_mime, str) and 0 < len(declared_mime) <= 100, "media_mime_invalid")
    _need(isinstance(expected_sha256, str) and byte_inspector.HASH.fullmatch(expected_sha256)
          and isinstance(brief_sha256, str) and byte_inspector.HASH.fullmatch(brief_sha256), "media_hash_invalid")
    _need(hashlib.sha256(data).hexdigest() == expected_sha256, "media_bytes_changed")
    source = byte_inspector._source(source)
    stamp = byte_inspector._stamp(checked_at)
    _need(type(include_text) is bool and type(include_frame_bytes) is bool, "media_include_flag_invalid")
    _need(isinstance(frame_indices, (tuple, list)) and len(frame_indices) <= MAX_FRAME_SAMPLES
          and all(type(n) is int and 0 <= n < MAX_VIDEO_SECONDS * MAX_FPS for n in frame_indices)
          and len(set(frame_indices)) == len(frame_indices), "video_frame_samples_invalid")
    result = {"schema_version": 1, "inspector_id": VERSION, "checked_at": stamp,
              "source": source, "source_sha256": _hash(source), "brief_sha256": brief_sha256,
              "artifact_sha256": expected_sha256, "size_bytes": len(data), "declared_mime": declared_mime,
              "technical_status": "held", "reason": None, "artifact": None, "checks": {},
              "inspection": None, "frames": [], "sampling_status": "not_requested",
              "coverage": {"full_content_review": False, "complete_visual_semantics": False,
                           "complete_audio_review": False},
              "brand_or_creative_accepted": False, "delivery_authorized": False,
              "model_calls": 0, "network_reads": 0, "network_writes": 0}
    if declared_mime in ("image/png", "image/jpeg"):
        _need(not frame_indices, "frame_sampling_requires_video")
        inspected = byte_inspector.inspect_artifact(data, declared_mime=declared_mime,
            expected_sha256=expected_sha256, brief_sha256=brief_sha256, output_spec=output_spec,
            source=source, checked_at=stamp, include_text=include_text)
        result.update(inspection=inspected, artifact=inspected["artifact"], checks=inspected["checks"],
                      output_spec_sha256=inspected["output_spec_sha256"],
                      technical_status=inspected["inspection_status"], reason="semantic_review_still_required")
        result["coverage"].update(kind="single_static_image", pixel_ocr_completed=bool(
            inspected.get("ocr") and inspected["ocr"]["status"] == "completed"))
        return _seal(result)
    if declared_mime != VIDEO_MIME:
        result.update(reason="format_not_in_narrow_wat_media_scope", output_spec_sha256=_hash(output_spec))
        result["coverage"].update(kind="unsupported", pdf_visible_content_verified=False)
        return _seal(result)
    spec = _video_spec(output_spec)
    result["output_spec_sha256"] = _hash(spec)
    result["coverage"].update(kind="video_metadata_and_optional_sparse_frames", full_decode_verified=False,
                              requested_frame_indices=list(frame_indices), decoded_frame_indices=[])
    frames_data = []
    try:
        _mp4_signature(data)
        tools = availability()
        result["tool_availability"] = tools
        _need(tools["ffprobe"], "ffprobe_unavailable_human_review_required")
        with tempfile.TemporaryDirectory(prefix="hamle-wat-media-") as directory:
            path = Path(directory).resolve() / "input.mp4"
            path.write_bytes(data)
            path.chmod(0o600)
            measured = _probe(path, expected_sha256)
            facts = _video_facts(measured)
            probe_hash = _hash(measured)
            checks = {}
            def check(name, good, observed):
                checks[name] = {"result": "pass" if good else "fail", "critical": True,
                    "method": "deterministic", "evaluator_id": VERSION, "artifact_sha256": expected_sha256,
                    "brief_sha256": brief_sha256, "evidence_ref": "probe-sha256:" + probe_hash,
                    "location": "actual MP4 container/stream metadata", "observed": observed,
                    "finding": "Measured " + name + "=" + str(observed) + "; requested condition "
                               + ("passed." if good else "failed.")}
            check("mime_type", True, VIDEO_MIME)
            check("size_bytes", spec["min_bytes"] <= len(data) <= spec["max_bytes"], len(data))
            for key in ("width", "height"):
                if key in spec:
                    check("fact:" + key, spec[key] == facts[key], facts[key])
            if "duration_seconds" in spec:
                good = abs(Fraction(spec["duration_seconds"]) - Fraction(facts["duration_seconds"])) <= Fraction(spec["duration_tolerance_seconds"])
                check("fact:duration_seconds", good, facts["duration_seconds"])
            if "frame_rate" in spec:
                check("fact:frame_rate", Fraction(spec["frame_rate"]) == Fraction(facts["frame_rate"]), facts["frame_rate"])
            if "audio_required" in spec:
                check("fact:audio_present", spec["audio_required"] == facts["audio_present"], facts["audio_present"])
            result.update(probe_sha256=probe_hash, checks=checks,
                artifact={"id": spec["id"], "sha256": expected_sha256, "mime_type": VIDEO_MIME,
                          "size_bytes": len(data), "facts": facts, "facts_bound_sha256": expected_sha256,
                          "evidence_ref": "probe-sha256:" + probe_hash},
                technical_status="failed" if any(c["result"] == "fail" for c in checks.values()) else "passed",
                reason="video_metadata_only_semantic_review_required")
            if frame_indices:
                _need(tools["ffmpeg"], "ffmpeg_unavailable_for_requested_samples")
                for index in frame_indices:
                    png = _run_child("frame", path, expected_sha256, index)
                    digest = hashlib.sha256(png).hexdigest()
                    inspection = byte_inspector.inspect_artifact(png, declared_mime="image/png",
                        expected_sha256=digest, brief_sha256=brief_sha256, source=source, checked_at=stamp,
                        output_spec={"id": spec["id"] + "-frame-" + str(index), "mime": "image/png"})
                    frame = {"frame_index": index, "video_sha256": expected_sha256,
                             "sha256": digest, "mime_type": "image/png", "size_bytes": len(png),
                             "facts": inspection["artifact"]["facts"], "evidence_sha256": inspection["evidence_sha256"],
                             "timestamp_seconds": None, "timestamp_verified": False,
                             "ocr_performed": False, "semantic_review_performed": False}
                    result["frames"].append(frame)
                    result["coverage"]["decoded_frame_indices"].append(index)
                    frames_data.append({"frame_index": index, "sha256": digest, "data": png})
                result["sampling_status"] = "completed_sparse_only"
                result["reason"] = "sparse_frames_do_not_establish_full_video_content"
    except (MediaInspectionError, byte_inspector.InspectionError) as exc:
        result.update(technical_status="held", reason=exc.code)
        if frame_indices:
            result["sampling_status"] = "incomplete_human_review_required"
    _seal(result)
    if include_frame_bytes:
        result["frame_bytes"] = frames_data
    return result


def _child_limits():
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_FRAME_BYTES,) * 2)
    if sys.platform.startswith("linux"):
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)


def _child():
    _need(len(sys.argv) in (5, 6) and sys.argv[1] == "--bounded-media", "media_child_arguments_invalid")
    mode, path, digest = sys.argv[2], Path(sys.argv[3]), sys.argv[4]
    _need(mode in ("probe", "frame") and len(sys.argv) == (6 if mode == "frame" else 5), "media_child_arguments_invalid")
    _need(path.is_absolute() and path.name == "input.mp4" and path.parent.name.startswith("hamle-wat-media-")
          and path.is_file() and not path.is_symlink() and path.parent.resolve() == path.parent
          and 0 < path.stat().st_size <= MAX_VIDEO_BYTES, "media_child_local_file_required")
    _need(byte_inspector.HASH.fullmatch(digest), "media_child_hash_invalid")
    _child_limits()
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    _need(actual == digest, "media_child_bytes_changed")
    executable = shutil.which("ffprobe" if mode == "probe" else "ffmpeg", path=TOOL_PATH)
    _need(executable is not None, "media_tool_unavailable")
    if mode == "probe":
        command = [executable, *_common_args(path), "-show_entries",
            "format=format_name,duration:stream=codec_name,codec_type,width,height,avg_frame_rate,duration,sample_aspect_ratio:stream_disposition=attached_pic:stream_side_data=rotation",
            "-of", "json"]
        limit = 100_000
    else:
        _need(re.fullmatch(r"[0-9]{1,4}", sys.argv[5]), "media_child_frame_invalid")
        index = int(sys.argv[5]); _need(index < MAX_VIDEO_SECONDS * MAX_FPS, "media_child_frame_invalid")
        command = [executable, "-nostdin", *_common_args(path), "-map", "0:v:0", "-an", "-sn", "-dn",
                   "-filter_threads", "1", "-vf", f"select=eq(n\\,{index})", "-frames:v", "1", "-vsync", "0",
                   "-threads", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"]
        limit = MAX_FRAME_BYTES
    with tempfile.TemporaryFile(mode="w+b") as output:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                                timeout=20, check=False, env=ENV)
        _need(result.returncode == 0, "media_tool_failed")
        output.seek(0); raw = output.read(limit + 1)
    _need(0 < len(raw) <= limit, "media_tool_output_invalid")
    sys.stdout.buffer.write(raw)


if __name__ == "__main__":
    try:
        _child()
    except Exception as exc:
        reason = exc.code if isinstance(exc, MediaInspectionError) else "media_child_failed_or_limited"
        sys.stdout.write(json.dumps({"error": reason}))
        raise SystemExit(1)
