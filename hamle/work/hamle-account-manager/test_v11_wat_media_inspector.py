"""Real PNG bytes plus explicitly mocked video metadata/decodes; no model/provider.

Mocked MP4 tests verify guards and evidence binding, not real video acceptance.
The optional actual FFmpeg test is skipped unless local binaries already exist.
"""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

import v11_wat_media_inspector as media


def digest(data):
    return hashlib.sha256(data).hexdigest()


def chunk(kind, payload):
    return (struct.pack(">I", len(payload)) + kind + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff))


def png(width=3, height=2, padding=0):
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    rows = (b"\0" + b"\xff\xff\xff" * width) * height
    extra = chunk(b"tEXt", b"Comment\0" + b"x" * padding) if padding else b""
    return b"\x89PNG\r\n\x1a\n" + header + extra + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def mp4_header():
    # Deliberately NOT a valid movie: used only with explicit mocked probing.
    return struct.pack(">I4s4sI4s4s", 24, b"ftyp", b"isom", 512, b"isom", b"iso2")


def probe_metadata(*, audio=True, width=1080, height=1920, duration="15.000000", fps="30/1"):
    streams = [{"codec_type": "video", "codec_name": "h264", "width": width, "height": height,
                "sample_aspect_ratio": "1:1", "duration": duration, "avg_frame_rate": fps,
                "disposition": {"attached_pic": 0}}]
    if audio:
        streams.append({"codec_type": "audio", "codec_name": "aac"})
    return {"streams": streams, "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": duration}}


def inspect(data=None, *, mime="image/png", spec=None, **overrides):
    data = png() if data is None else data
    args = dict(declared_mime=mime, expected_sha256=digest(data), brief_sha256="b" * 64,
                output_spec=spec or {"id": "main", "mime": mime},
                source={"verified": True, "account_id": 5898865, "project_id": 40694338,
                        "task_id": 123, "attachment_id": 456, "evidence_ref": "unit-test-source-proof"},
                checked_at="2026-10-10T19:30:00+03:00")
    args.update(overrides)
    return media.inspect_wat_media(data, **args)


def mock_video(*, metadata=None, data=None, **kwargs):
    with patch.object(media, "availability", return_value={"ffprobe": True, "ffmpeg": True}), \
            patch.object(media, "_probe", return_value=metadata or probe_metadata()):
        return inspect(data or mp4_header(), mime="video/mp4", **kwargs)


class StaticMediaTests(unittest.TestCase):
    def test_actual_png_dimensions_source_brief_and_evidence_bound(self):
        raw = png()
        result = inspect(raw, spec={"id": "main", "mime": "image/png", "width": 3, "height": 2})
        self.assertEqual(result["technical_status"], "passed")
        self.assertEqual(result["artifact"]["facts"]["width"], 3)
        self.assertEqual(result["artifact_sha256"], digest(raw))
        self.assertEqual(result["brief_sha256"], "b" * 64)
        self.assertEqual(result["source_sha256"], media._hash(result["source"]))
        proof = copy.deepcopy(result)
        claimed = proof.pop("evidence_sha256")
        self.assertEqual(claimed, media._hash(proof))
        self.assertFalse(result["coverage"]["full_content_review"])
        self.assertFalse(result["brand_or_creative_accepted"])
        self.assertFalse(result["delivery_authorized"])
        self.assertEqual((result["model_calls"], result["network_reads"], result["network_writes"]), (0, 0, 0))

    def test_actual_png_larger_than_internal_2mib_fixture_limit_is_allowed(self):
        raw = png(padding=2 * 1024 * 1024 + 10)
        result = inspect(raw)
        self.assertGreater(result["size_bytes"], 2 * 1024 * 1024)
        self.assertEqual(result["technical_status"], "passed")

    def test_image_still_obeys_existing_16mib_limit(self):
        with self.assertRaisesRegex(media.byte_inspector.InspectionError, "artifact_byte_limit_invalid"):
            inspect(b"x" * (16 * 1024 * 1024 + 1))

    def test_changed_bytes_rejected_before_any_parser(self):
        with patch.object(media.byte_inspector, "inspect_artifact") as parser:
            with self.assertRaisesRegex(media.MediaInspectionError, "media_bytes_changed"):
                inspect(expected_sha256="f" * 64)
            parser.assert_not_called()

    def test_unverified_source_and_opaque_id_constraints_are_preserved(self):
        source = inspect()["source"]
        source["verified"] = False
        with self.assertRaisesRegex(media.byte_inspector.InspectionError, "attachment_source_unverified"):
            inspect(source=source)
        source.update(verified=True, attachment_id="basecamp-sgid-sha256:" + "a" * 64)
        self.assertEqual(inspect(source=source)["source"]["attachment_id"], source["attachment_id"])
        source["attachment_id"] = "filename.png"
        with self.assertRaisesRegex(media.byte_inspector.InspectionError, "attachment_source_identity_invalid"):
            inspect(source=source)

    def test_wrong_dimensions_fail_and_no_pixel_text_uses_png_metadata(self):
        spec = {"id": "main", "mime": "image/png", "width": 4,
                "required_text": [{"id": "price", "text": "399 TL", "critical": True}]}
        result = inspect(spec=spec)
        self.assertEqual(result["technical_status"], "failed")
        self.assertEqual(result["checks"]["fact:width"]["result"], "fail")
        self.assertEqual(result["checks"]["required_text:price"]["result"], "unassessed")
        self.assertFalse(result["coverage"]["pixel_ocr_completed"])

    def test_unavailable_requested_ocr_is_unassessed(self):
        with patch.object(media.byte_inspector, "_isolated", side_effect=media.byte_inspector.InspectionError("ocr_dependency_missing")):
            result = inspect(spec={"id": "main", "mime": "image/png", "text_inspection": "tesseract"})
        self.assertEqual(result["technical_status"], "unassessed")
        self.assertFalse(result["coverage"]["pixel_ocr_completed"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow unavailable; no dependency installed by tests")
    def test_actual_jpeg_reuses_decode_and_orientation_inspector(self):
        from PIL import Image
        stream = io.BytesIO()
        Image.new("RGB", (3, 2), "white").save(stream, format="JPEG")
        result = inspect(stream.getvalue(), mime="image/jpeg")
        self.assertEqual(result["artifact"]["facts"]["width"], 3)
        self.assertEqual(result["technical_status"], "passed")

    def test_pdf_psd_text_and_unknown_formats_are_held_not_visual_acceptance(self):
        for mime, raw in (("application/pdf", b"%PDF-1.7 content"), ("image/vnd.adobe.photoshop", b"8BPSdata"),
                          ("text/plain", b"brief only"), ("video/quicktime", b"movie")):
            with self.subTest(mime=mime):
                result = inspect(raw, mime=mime)
                self.assertEqual(result["technical_status"], "held")
                self.assertFalse(result["coverage"]["pdf_visible_content_verified"])
                self.assertIsNone(result["artifact"])

    def test_bad_mime_flags_timestamp_and_image_frame_request_rejected(self):
        for overrides in ({"declared_mime": None}, {"include_frame_bytes": 1}, {"include_text": "yes"},
                          {"checked_at": "2026-10-10"}, {"frame_indices": (0,)}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                inspect(**overrides)


class VideoEvidenceTests(unittest.TestCase):
    def test_unavailable_ffprobe_is_hold_without_parser_or_network(self):
        with patch.object(media, "availability", return_value={"ffprobe": False, "ffmpeg": False}), \
                patch.object(media, "_probe") as parser:
            result = inspect(mp4_header(), mime="video/mp4")
            parser.assert_not_called()
        self.assertEqual(result["technical_status"], "held")
        self.assertEqual(result["reason"], "ffprobe_unavailable_human_review_required")

    def test_mocked_metadata_pass_never_claims_full_video_or_brand_review(self):
        spec = {"id": "story", "mime": "video/mp4", "width": 1080, "height": 1920,
                "duration_seconds": 15, "frame_rate": "30/1", "audio_required": True}
        result = mock_video(spec=spec)
        self.assertEqual(result["technical_status"], "passed")
        self.assertEqual(result["artifact"]["facts"]["duration_seconds"], "15")
        self.assertEqual(result["artifact"]["facts_bound_sha256"], digest(mp4_header()))
        self.assertFalse(result["coverage"]["full_decode_verified"])
        self.assertFalse(result["coverage"]["complete_visual_semantics"])
        self.assertFalse(result["coverage"]["complete_audio_review"])
        self.assertFalse(result["delivery_authorized"])
        self.assertEqual(result["sampling_status"], "not_requested")

    def test_requested_duration_dimensions_audio_and_frame_rate_fail_independently(self):
        spec = {"id": "story", "mime": "video/mp4", "width": 1920, "height": 1080,
                "duration_seconds": 12, "frame_rate": 25, "audio_required": False}
        result = mock_video(spec=spec)
        self.assertEqual(result["technical_status"], "failed")
        for key in ("width", "height", "duration_seconds", "frame_rate", "audio_present"):
            self.assertEqual(result["checks"]["fact:" + key]["result"], "fail")
            self.assertTrue(result["checks"]["fact:" + key]["critical"])

    def test_duration_fraction_and_explicit_tolerance_are_exact(self):
        result = mock_video(metadata=probe_metadata(duration="15.04"),
                            spec={"id": "story", "mime": "video/mp4", "duration_seconds": 15})
        self.assertEqual(result["technical_status"], "passed")
        result = mock_video(metadata=probe_metadata(duration="15.06"),
                            spec={"id": "story", "mime": "video/mp4", "duration_seconds": 15})
        self.assertEqual(result["technical_status"], "failed")

    def test_rotation_uses_display_dimensions_and_retains_encoded_dimensions(self):
        metadata = probe_metadata(width=1920, height=1080)
        metadata["streams"][0]["side_data_list"] = [{"rotation": -90}]
        facts = media._video_facts(metadata)
        self.assertEqual((facts["width"], facts["height"], facts["encoded_width"], facts["rotation"]),
                         (1080, 1920, 1920, 270))

    def test_scope_constraints_hold_video_instead_of_inventing_facts(self):
        cases = [({"codec_name": "hevc"}, "video_codec_unsupported"),
                 ({"sample_aspect_ratio": "4:3"}, "non_square_video_pixels_require_review"),
                 ({"width": 8000}, "video_dimensions_exceed_limit"),
                 ({"width": 3840, "height": 3840}, "video_dimensions_exceed_limit"),
                 ({"duration": "61"}, "video_duration_exceeds_limit"),
                 ({"duration": "0"}, "video_duration_exceeds_limit"),
                 ({"duration": "N/A"}, "video_duration_unknown"),
                 ({"avg_frame_rate": "0/0"}, "video_frame_rate_unknown"),
                 ({"avg_frame_rate": "120/1"}, "video_frame_rate_exceeds_limit"),
                 ({"avg_frame_rate": "nan"}, "video_frame_rate_unknown"),
                 ({"disposition": {"attached_pic": 1}}, "video_codec_unsupported"),
                 ({"side_data_list": [{"rotation": 45}]}, "video_rotation_unsupported")]
        for patch_fields, reason in cases:
            with self.subTest(fields=patch_fields):
                metadata = probe_metadata()
                metadata["streams"][0].update(patch_fields)
                result = mock_video(metadata=metadata)
                self.assertEqual(result["technical_status"], "held")
                self.assertEqual(result["reason"], reason)

    def test_malformed_metadata_is_static_hold(self):
        for metadata in ({"streams": [None], "format": {}},
                         {"streams": [], "format": {}},
                         {"streams": [{"codec_type": "video"}], "format": {"format_name": []}}):
            with self.subTest(metadata=metadata):
                self.assertEqual(mock_video(metadata=metadata)["technical_status"], "held")

    def test_unknown_stream_audio_codec_or_conflicting_duration_is_held(self):
        for mutation in (lambda m: m["streams"][1].update(codec_name="mp3"),
                         lambda m: m["streams"][1].update(codec_type="subtitle"),
                         lambda m: m["format"].update(duration="20")):
            metadata = probe_metadata()
            mutation(metadata)
            self.assertEqual(mock_video(metadata=metadata)["technical_status"], "held")

    def test_video_spec_rejects_guessed_semantic_fields_and_invalid_limits(self):
        for fields in ({"quality": 100}, {"required_text": ["399 TL"]}, {"max_bytes": 65 * 1024 * 1024},
                       {"duration_seconds": 61}, {"duration_seconds": float("nan")}, {"audio_required": 1},
                       {"width": True}, {"duration_tolerance_seconds": 1}, {"frame_rate": 0}):
            with self.subTest(fields=fields), self.assertRaises(media.MediaInspectionError):
                mock_video(spec={"id": "story", "mime": "video/mp4", **fields})

    def test_forged_movie_extension_or_quicktime_brand_never_enters_probe(self):
        for raw in (b"not an actual MP4" * 2, mp4_header().replace(b"isom", b"qt  ", 1)):
            with patch.object(media, "_probe") as parser:
                result = inspect(raw, mime="video/mp4")
                parser.assert_not_called()
            self.assertEqual(result["technical_status"], "held")

    def test_no_ffmpeg_for_requested_frames_is_hold(self):
        with patch.object(media, "availability", return_value={"ffprobe": True, "ffmpeg": False}), \
                patch.object(media, "_probe", return_value=probe_metadata()):
            result = inspect(mp4_header(), mime="video/mp4", frame_indices=[0])
        self.assertEqual(result["technical_status"], "held")
        self.assertEqual(result["reason"], "ffmpeg_unavailable_for_requested_samples")
        self.assertEqual(result["frames"], [])

    def test_sparse_real_png_samples_are_hash_bound_not_full_content_claim(self):
        raw_frame = png(4, 3)
        with patch.object(media, "_run_child", return_value=raw_frame):
            result = mock_video(frame_indices=[0, 149], include_frame_bytes=True)
        self.assertEqual(result["sampling_status"], "completed_sparse_only")
        self.assertEqual(result["coverage"]["decoded_frame_indices"], [0, 149])
        self.assertFalse(result["coverage"]["full_content_review"])
        self.assertEqual(result["frames"][1]["video_sha256"], digest(mp4_header()))
        self.assertEqual(result["frames"][1]["sha256"], digest(raw_frame))
        self.assertFalse(result["frames"][1]["semantic_review_performed"])
        self.assertFalse(result["frames"][1]["timestamp_verified"])
        self.assertIsNone(result["frames"][1]["timestamp_seconds"])
        self.assertEqual(result["frame_bytes"][1]["data"], raw_frame)
        result.pop("frame_bytes")
        claimed = result.pop("evidence_sha256")
        self.assertEqual(claimed, media._hash(result))

    def test_invalid_or_failed_frame_extraction_cannot_claim_completed_sampling(self):
        with patch.object(media, "_run_child", side_effect=[png(), media.MediaInspectionError("media_tool_failed")]):
            result = mock_video(frame_indices=[0, 40])
        self.assertEqual(result["technical_status"], "held")
        self.assertEqual(result["sampling_status"], "incomplete_human_review_required")
        self.assertEqual(len(result["frames"]), 1)
        self.assertEqual(result["coverage"]["decoded_frame_indices"], [0])

    def test_frame_count_index_duplicate_and_type_limits(self):
        for samples in ([0] * 2, list(range(9)), [-1], [3600], [True], [1.5], "0"):
            with self.subTest(samples=samples), self.assertRaisesRegex(media.MediaInspectionError, "video_frame_samples_invalid"):
                mock_video(frame_indices=samples)


class SubprocessBoundaryTests(unittest.TestCase):
    @unittest.skipUnless(all(media.availability().values()), "ffprobe/ffmpeg unavailable; tests never install binaries")
    def test_actual_generated_h264_probe_and_frame_when_tools_already_exist(self):
        # A generated technical fixture only; no WAT producer/creative acceptance.
        with tempfile.TemporaryDirectory(prefix="hamle-wat-fixture-") as directory:
            path = Path(directory).resolve() / "fixture.mp4"
            executable = media.shutil.which("ffmpeg", path=media.TOOL_PATH)
            result = subprocess.run([executable, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                "color=c=white:s=16x16:r=2:d=1", "-an", "-c:v", "libx264", "-threads", "1",
                "-pix_fmt", "yuv420p", "-y", str(path)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                check=False, env=media.ENV)
            if result.returncode != 0:
                self.skipTest("installed FFmpeg cannot generate the libx264 technical fixture")
            raw = path.read_bytes()
        result = inspect(raw, mime="video/mp4", frame_indices=[0], include_frame_bytes=True,
                         spec={"id": "technical_fixture", "mime": "video/mp4", "width": 16,
                               "height": 16, "duration_seconds": 1, "frame_rate": 2,
                               "audio_required": False})
        self.assertEqual(result["technical_status"], "passed", result["reason"])
        self.assertEqual(result["sampling_status"], "completed_sparse_only")
        self.assertEqual(result["frames"][0]["video_sha256"], digest(raw))
        self.assertEqual(result["frame_bytes"][0]["data"][:8], b"\x89PNG\r\n\x1a\n")
        self.assertFalse(result["coverage"]["full_content_review"])

    def test_outer_timeout_and_parser_static_reason_remain_distinct(self):
        with patch.object(media.subprocess, "run", side_effect=subprocess.TimeoutExpired("python", 25)):
            with self.assertRaisesRegex(media.MediaInspectionError, "media_parser_timeout"):
                media._probe(Path("/ignored/input.mp4"), "a" * 64)
        with patch.object(media.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=b'{"error":"media_tool_failed"}')):
            with self.assertRaisesRegex(media.MediaInspectionError, "media_tool_failed"):
                media._probe(Path("/ignored/input.mp4"), "a" * 64)

    def test_outer_subprocess_drops_credentials_and_bounds_time(self):
        with patch.object(media.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"{}")) as run:
            media._probe(Path("/ignored/input.mp4"), "a" * 64)
        args, kwargs = run.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertEqual(kwargs["timeout"], 25)
        self.assertEqual(kwargs["env"], media.ENV)
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_child_checks_local_bytes_and_uses_only_bounded_local_demuxer(self):
        raw = mp4_header()
        with tempfile.TemporaryDirectory(prefix="hamle-wat-media-") as directory:
            path = Path(directory).resolve() / "input.mp4"
            path.write_bytes(raw)
            for mode, payload in (("probe", b"{}"), ("frame", png())):
                commands = []
                def fake_run(command, **kwargs):
                    commands.append((command, kwargs))
                    kwargs["stdout"].write(payload)
                    return SimpleNamespace(returncode=0)
                argv = [str(Path(media.__file__).resolve()), "--bounded-media", mode, str(path), digest(raw)]
                if mode == "frame":
                    argv.append("12")
                captured = io.BytesIO()
                with patch.object(sys, "argv", argv), patch.object(media, "_child_limits"), \
                        patch.object(media.shutil, "which", return_value="/usr/bin/ff" + ("probe" if mode == "probe" else "mpeg")), \
                        patch.object(media.subprocess, "run", side_effect=fake_run), \
                        patch.object(sys, "stdout", SimpleNamespace(buffer=captured)):
                    media._child()
                self.assertEqual(captured.getvalue(), payload)
                command, options = commands[0]
                self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file,pipe")
                self.assertEqual(command[command.index("-format_whitelist") + 1], "mov")
                self.assertEqual(command[command.index("-enable_drefs") + 1], "0")
                self.assertEqual(command[command.index("-use_absolute_path") + 1], "0")
                self.assertEqual(command[command.index("-i") + 1], str(path))
                self.assertEqual(options["timeout"], 20)
                self.assertEqual(options["env"], media.ENV)
                self.assertEqual(options["stdin"], subprocess.DEVNULL)
                self.assertNotIn("shell", options)
                if mode == "frame":
                    self.assertEqual(command[command.index("-vf") + 1], "select=eq(n\\,12)")

    def test_child_refuses_changed_digest_before_tool(self):
        with tempfile.TemporaryDirectory(prefix="hamle-wat-media-") as directory:
            path = Path(directory).resolve() / "input.mp4"
            path.write_bytes(mp4_header())
            argv = ["media", "--bounded-media", "probe", str(path), "f" * 64]
            with patch.object(sys, "argv", argv), patch.object(media, "_child_limits"), \
                    patch.object(media.shutil, "which") as which:
                with self.assertRaisesRegex(media.MediaInspectionError, "media_child_bytes_changed"):
                    media._child()
                which.assert_not_called()

    def test_child_rejects_remote_uri_unrelated_path_and_symlink(self):
        for value in ("https://example.invalid/movie.mp4", "/tmp/not-private/input.mp4", "relative/input.mp4"):
            with patch.object(sys, "argv", ["media", "--bounded-media", "probe", value, "a" * 64]):
                with self.assertRaisesRegex(media.MediaInspectionError, "media_child_local_file_required"):
                    media._child()
        with tempfile.TemporaryDirectory(prefix="hamle-wat-media-") as directory:
            parent = Path(directory).resolve()
            original = parent / "original.mp4"
            original.write_bytes(mp4_header())
            link = parent / "input.mp4"
            link.symlink_to(original)
            with patch.object(sys, "argv", ["media", "--bounded-media", "probe", str(link), digest(mp4_header())]):
                with self.assertRaisesRegex(media.MediaInspectionError, "media_child_local_file_required"):
                    media._child()


if __name__ == "__main__":
    unittest.main()
