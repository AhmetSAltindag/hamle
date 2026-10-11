"""Real in-memory byte fixtures; no provider/model/network operations."""
import copy
import hashlib
import importlib.util
import io
import json
import struct
import subprocess
import unittest
from unittest.mock import patch
import zlib

import v11_artifact_inspector as inspector
from v11_artifact_inspector import inspect_artifact, InspectionError


def chunk(kind, payload):
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)


def png(width=3, height=2, *, text=None, interlace=0, bad_filter=False):
    header = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, interlace))
    rows = (bytes([5 if bad_filter else 0]) + b"\xff\xff\xff" * width) * height
    extra = chunk(b"tEXt", b"Comment\0" + text.encode()) if text else b""
    return b"\x89PNG\r\n\x1a\n" + header + extra + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def spec(mime="image/png", **kwargs):
    return {"id": "main", "mime": mime, **kwargs}


def run(data=None, *, output_spec=None, declared_mime="image/png", **kwargs):
    data = png() if data is None else data
    args = dict(declared_mime=declared_mime, expected_sha256=hashlib.sha256(data).hexdigest(),
                brief_sha256="b" * 64, output_spec=output_spec or spec(),
                source={"verified": True, "account_id": 5898865, "project_id": 49184053,
                        "task_id": 123, "attachment_id": 456, "evidence_ref": "synthetic-provider-read"},
                inspected_at="2026-10-10T12:00:00+03:00")
    args.update(kwargs)
    return inspect_artifact(data, **args)


def text_requirement(value="399 TL"):
    return {"id": "price", "text": value, "critical": True}


class ArtifactInspectorTests(unittest.TestCase):
    def test_real_png_scanlines_dimensions_and_bytes_are_bound(self):
        data = png()
        result = run(data, output_spec=spec(facts={"width": 3, "height": 2}))
        self.assertEqual(result["inspection_status"], "passed")
        self.assertEqual(result["artifact"]["facts"], {"width": 3, "height": 2, "bit_depth": 8})
        self.assertEqual(result["artifact"]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertFalse(result["delivery_authorized"])
        self.assertTrue(result["semantic_review_required"])
        self.assertEqual((result["model_calls"], result["network_writes"]), (0, 0))
        self.assertNotIn("extracted_text", result)

    def test_changed_bytes_never_reuse_old_manifest(self):
        with self.assertRaisesRegex(InspectionError, "artifact_bytes_changed"):
            run(expected_sha256="f" * 64)

    def test_wrong_sniff_vs_declared_mime_is_rejected(self):
        with self.assertRaisesRegex(InspectionError, "artifact_mime_mismatch"):
            run(declared_mime="text/plain")

    def test_wrong_format_against_actual_spec_is_failure(self):
        self.assertEqual(run(output_spec=spec("application/pdf"))["checks"]["mime_type"]["result"], "fail")

    def test_wrong_size_and_dimensions_are_critical_failures(self):
        result = run(output_spec=spec(max_bytes=10, width=4))
        self.assertEqual(result["inspection_status"], "failed")
        self.assertEqual(result["checks"]["fact:width"]["result"], "fail")
        self.assertTrue(result["checks"]["size_bytes"]["critical"])

    def test_png_text_chunk_cannot_fake_visible_price(self):
        result = run(png(text="399 TL"), output_spec=spec(required_text=[text_requirement()]))
        self.assertEqual(result["checks"]["required_text:price"]["result"], "unassessed")
        self.assertIsNone(result["text_sha256"])

    def test_unavailable_fact_is_not_invented(self):
        result = run(output_spec=spec(facts={"page_count": 1}))
        self.assertEqual(result["inspection_status"], "unassessed")

    def test_png_crc_corruption_is_rejected(self):
        data = bytearray(png())
        data[30] ^= 1
        with self.assertRaisesRegex(InspectionError, "png_crc_invalid"):
            run(bytes(data))

    def test_png_trailing_data_is_rejected(self):
        with self.assertRaisesRegex(InspectionError, "png_end_invalid"):
            run(png() + b"other file")

    def test_png_truncation_is_rejected(self):
        with self.assertRaisesRegex(InspectionError, "png_chunk_truncated"):
            run(png()[:-1])

    def test_png_invalid_filter_is_rejected(self):
        with self.assertRaisesRegex(InspectionError, "png_filter_invalid"):
            run(png(bad_filter=True))

    def test_interlaced_png_is_explicitly_held(self):
        with self.assertRaisesRegex(InspectionError, "interlaced_png_unsupported"):
            run(png(interlace=1))

    def test_expansion_more_than_image_geometry_is_rejected(self):
        header = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        data = b"\x89PNG\r\n\x1a\n" + header + chunk(b"IDAT", zlib.compress(b"x" * 100000)) + chunk(b"IEND", b"")
        with self.assertRaisesRegex(InspectionError, "png_scanlines_invalid"):
            run(data)

    def test_huge_geometry_is_rejected_before_decompressing(self):
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 100000, 100000, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(b"")) + chunk(b"IEND", b""))
        with self.assertRaisesRegex(InspectionError, "image_dimensions_exceed_limit"):
            run(data)

    def test_apng_is_explicitly_unsupported(self):
        data = png()
        data = data[:33] + chunk(b"acTL", struct.pack(">II", 2, 0)) + data[33:]
        with self.assertRaisesRegex(InspectionError, "animated_png_unsupported"):
            run(data)

    def test_unknown_critical_png_chunk_is_not_ignored(self):
        data = png()
        data = data[:33] + chunk(b"TEST", b"x") + data[33:]
        with self.assertRaisesRegex(InspectionError, "png_unknown_critical_chunk"):
            run(data)

    def test_plaintext_required_price_actual_presence_and_missing(self):
        settings = spec("text/plain", required_text=[text_requirement()])
        good = run(b"Price: 399 TL", declared_mime="text/plain", output_spec=settings, include_text=True)
        bad = run(b"Price: pending", declared_mime="text/plain", output_spec=settings)
        self.assertEqual(good["inspection_status"], "passed")
        self.assertEqual(good["extracted_text"], "Price: 399 TL")
        self.assertEqual(bad["inspection_status"], "failed")
        self.assertTrue(bad["checks"]["required_text:price"]["critical"])

    def test_plaintext_keeps_numeric_currency_and_unit_differences(self):
        settings = spec("text/plain", required_text=[text_requirement("399 TL")])
        for value in (b"39 TL", b"399 USD", b"399TL", b"1399 TL", b"399 TLD"):
            self.assertEqual(run(value, declared_mime="text/plain", output_spec=settings)["inspection_status"], "failed")

    def test_forbidden_internal_note_is_detected(self):
        settings = spec("text/plain", forbidden_text=[text_requirement("INTERNAL ONLY")])
        result = run(b"INTERNAL ONLY", declared_mime="text/plain", output_spec=settings)
        self.assertEqual(result["checks"]["forbidden_text:price"]["result"], "fail")

    def test_binary_and_active_formats_not_treated_as_plaintext(self):
        for data in (b"8BPSsomepsd", b"PK\x03\x04zipfile", b"<html>hello", b"<?xml version", b"null\x00value"):
            with self.subTest(data=data), self.assertRaises(InspectionError):
                run(data, declared_mime="text/plain", output_spec=spec("text/plain"))

    def test_empty_or_oversized_files_are_rejected(self):
        for data in (b"", b"a" * (inspector.MAX_BYTES + 1)):
            with self.assertRaisesRegex(InspectionError, "artifact_byte_limit_invalid"):
                run(data)

    def test_text_character_limit(self):
        with self.assertRaisesRegex(InspectionError, "artifact_text_exceeds_limit"):
            run(b"x" * (inspector.MAX_TEXT_CHARS + 1), declared_mime="text/plain", output_spec=spec("text/plain"))

    def test_source_metadata_must_be_bound_and_verified(self):
        for source in ({}, {"verified": True, "url": "https://untrusted.invalid/file"}):
            with self.assertRaisesRegex(InspectionError, "attachment_source_fields_invalid"):
                run(source=source)
        source = run()["source"]
        source["verified"] = False
        with self.assertRaisesRegex(InspectionError, "attachment_source_unverified"):
            run(source=source)

    def test_source_task_and_brief_changes_change_evidence(self):
        first = run()
        source = first["source"].copy()
        source["task_id"] += 1
        second = run(source=source)
        third = run(brief_sha256="c" * 64)
        self.assertNotEqual(first["evidence_sha256"], second["evidence_sha256"])
        self.assertNotEqual(first["artifact"]["evidence_ref"], third["artifact"]["evidence_ref"])

    def test_provider_verified_sgid_digest_is_real_opaque_attachment_identity(self):
        source = run()["source"]
        source["attachment_id"] = "basecamp-sgid-sha256:" + "d" * 64
        result = run(source=source)
        self.assertEqual(result["source"]["attachment_id"], source["attachment_id"])
        for bad in ("123", "basecamp-sgid-sha256:unknown", -1, True):
            source["attachment_id"] = bad
            with self.assertRaisesRegex(InspectionError, "attachment_source_identity_invalid"):
                run(source=source)

    def test_checked_at_alias_matches_explicit_single_timestamp(self):
        stamp = "2026-10-10T12:00:00+03:00"
        self.assertEqual(run(), run(inspected_at=None, checked_at=stamp))
        with self.assertRaisesRegex(InspectionError, "inspection_timestamp_required_once"):
            run(checked_at=stamp)

    def test_reject_unknown_spec_or_impossible_requirement(self):
        for settings in (spec(quality=100), spec(facts={"duration": 10}), spec(width=True),
                         spec(required_text=[{"id": "price", "text": "", "critical": True}]),
                         {"id": "main", "mime": "image/png", "mime_types": ["image/png"]}):
            with self.assertRaises(InspectionError):
                run(output_spec=settings)

    def test_default_does_not_attempt_ocr(self):
        with patch.object(inspector, "_isolated", side_effect=AssertionError("unexpected process")):
            self.assertIsNone(run()["ocr"])

    def test_requested_ocr_missing_binary_is_unassessed(self):
        with patch.object(inspector, "_isolated", side_effect=InspectionError("ocr_dependency_missing")):
            result = run(output_spec=spec(text_inspection="tesseract", required_text=[text_requirement()]))
        self.assertEqual(result["inspection_status"], "unassessed")
        self.assertEqual(result["ocr"]["reason"], "ocr_dependency_missing")

    def test_ocr_timeout_never_turns_into_missing_price_revision(self):
        with patch.object(inspector, "_isolated", side_effect=InspectionError("artifact_parser_timeout")):
            result = run(output_spec=spec(text_inspection="tesseract", required_text=[text_requirement()]))
        self.assertEqual(result["checks"]["required_text:price"]["result"], "unassessed")

    def test_requested_ocr_without_literal_rows_still_requires_real_process(self):
        with patch.object(inspector, "_isolated", side_effect=InspectionError("ocr_dependency_missing")):
            result = run(output_spec=spec(text_inspection="tesseract"))
        self.assertEqual(result["inspection_status"], "unassessed")

    def test_successful_ocr_adapter_text_normalizes_turkish_and_whitespace_only(self):
        # Contract unit test; does not claim Tesseract actually ran on this fixture.
        measured = {"facts": {}, "text": "HAMLE İÇ DENEME\n12 EKİM 2026\n399 TL",
                    "parser": "tesseract 5.5.0", "languages": ["eng", "tur"], "resource_limits": "test"}
        with patch.object(inspector, "_isolated", return_value=measured):
            result = run(output_spec=spec(text_inspection="tesseract", required_text=[text_requirement("hamle ic deneme 12 ekim 2026")]))
        self.assertEqual(result["checks"]["required_text:price"]["result"], "pass")
        self.assertIn("pixel OCR", result["checks"]["required_text:price"]["finding"])

    def test_successful_ocr_missing_required_date_fails_measured_requirement(self):
        measured = {"facts": {}, "text": "HAMLE İÇ DENEME", "parser": "tesseract 5.5.0",
                    "languages": ["eng", "tur"], "resource_limits": "test"}
        with patch.object(inspector, "_isolated", return_value=measured):
            result = run(output_spec=spec(text_inspection="tesseract", required_text=[text_requirement("12 EKİM 2026")]))
        self.assertEqual(result["inspection_status"], "failed")
        self.assertIn("not proof", result["checks"]["required_text:price"]["finding"])

    def test_ocr_silence_cannot_prove_internal_text_is_absent(self):
        measured = {"facts": {}, "text": "HAMLE İÇ DENEME", "parser": "tesseract 5.5.0",
                    "languages": ["eng", "tur"], "resource_limits": "test"}
        with patch.object(inspector, "_isolated", return_value=measured):
            result = run(output_spec=spec(text_inspection="tesseract", forbidden_text=[text_requirement("INTERNAL ONLY")]))
        self.assertEqual(result["checks"]["forbidden_text:price"]["result"], "unassessed")

    def test_pdf_cannot_claim_pixel_ocr(self):
        parsed = {"facts": {"page_count": 1}, "text": "399 TL", "parser": "test", "resource_limits": "test"}
        with patch.object(inspector, "_isolated", return_value=parsed):
            with self.assertRaisesRegex(InspectionError, "ocr_format_unsupported"):
                run(b"%PDF-1.4", declared_mime="application/pdf", output_spec=spec("application/pdf", text_inspection="tesseract"))

    def test_parser_timeout_is_static_no_byte_or_exception_exposure(self):
        with patch.object(inspector.subprocess, "run", side_effect=subprocess.TimeoutExpired("secret path", 12)):
            with self.assertRaisesRegex(InspectionError, "^artifact_parser_timeout$"):
                inspector._isolated(b"secret file contents", "application/pdf")

    def test_optional_text_not_in_evidence_hash_or_default_output(self):
        args = dict(declared_mime="text/plain", output_spec=spec("text/plain"))
        hidden, exposed = run(b"private content", **args), run(b"private content", include_text=True, **args)
        self.assertNotIn("private content", json.dumps(hidden))
        self.assertEqual(hidden["evidence_sha256"], exposed["evidence_sha256"])

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional dependency not installed")
    def test_real_jpeg_bytes_decoded_in_subprocess(self):
        from PIL import Image
        target = io.BytesIO()
        Image.new("RGB", (11, 7), "white").save(target, format="JPEG")
        result = run(target.getvalue(), declared_mime="image/jpeg", output_spec=spec("image/jpeg", width=11, height=7))
        self.assertEqual(result["inspection_status"], "passed")
        self.assertTrue(result["parser"].startswith("Pillow-"))

    @unittest.skipUnless(importlib.util.find_spec("PIL"), "Pillow optional dependency not installed")
    def test_real_jpeg_corruption_rejected(self):
        from PIL import Image
        target = io.BytesIO()
        Image.new("RGB", (11, 7), "white").save(target, format="JPEG")
        with self.assertRaises(InspectionError):
            run(target.getvalue()[:-8], declared_mime="image/jpeg", output_spec=spec("image/jpeg"))

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf optional dependency not installed")
    def test_real_pdf_pages_and_content_stream_inspected_in_subprocess(self):
        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=300, height=200)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 20 Tf 20 100 Td (Price: 399 TL) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        target = io.BytesIO()
        writer.write(target)
        result = run(target.getvalue(), declared_mime="application/pdf",
                     output_spec=spec("application/pdf", page_count=1, required_text=[text_requirement()]), include_text=True)
        self.assertEqual(result["inspection_status"], "passed")
        self.assertIn("399 TL", result["extracted_text"])
        self.assertTrue(result["semantic_review_required"])
        self.assertIn("hidden", " ".join(result["limitations"]))

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "pypdf optional dependency not installed")
    def test_real_pdf_encryption_held(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=300, height=200)
        writer.encrypt("not-a-secret-test-password")
        target = io.BytesIO()
        writer.write(target)
        with self.assertRaisesRegex(InspectionError, "encrypted_pdf_unsupported"):
            run(target.getvalue(), declared_mime="application/pdf", output_spec=spec("application/pdf"))


if __name__ == "__main__":
    unittest.main()
