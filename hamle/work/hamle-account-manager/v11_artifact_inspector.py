"""Bounded byte inspection. No network, model, credential or provider writes.

Evidence measures actual bytes, never a producer's MIME/size/quality claim.
It cannot establish visual meaning, rights, source authority or delivery consent.
Provider identity and attachment/task membership must be verified by the caller.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
import warnings
import zlib


INSPECTOR_ID = "hamle-byte-inspector-v1"
MAX_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_EXPANDED_BYTES = 96 * 1024 * 1024
MAX_TEXT_CHARS = 250_000
MAX_PAGES = 30
MAX_PARSE_SECONDS = 12
MIMES = frozenset(("image/png", "image/jpeg", "application/pdf", "text/plain"))
HASH = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_FIELDS = frozenset(("verified", "account_id", "project_id", "task_id",
                           "attachment_id", "evidence_ref"))


class InspectionError(ValueError):
    """Static error codes only; do not echo file bytes or parser diagnostics."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _need(condition, code):
    if not condition:
        raise InspectionError(code)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _literal_present(needle, haystack):
    # Do not accept a price of 1399 TL for a required 399 TL or detect a brand
    # abbreviation inside another word. Punctuation in the requirement remains
    # literal; this is deliberately not a language-model semantic comparison.
    _need(bool(needle), "output_spec_normalized_text_empty")
    prefix = r"(?<!\w)" if needle[0].isalnum() or needle[0] == "_" else ""
    suffix = r"(?!\w)" if needle[-1].isalnum() or needle[-1] == "_" else ""
    return re.search(prefix + re.escape(needle) + suffix, haystack) is not None


def _text(value, maximum=500):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not any(ord(c) < 32 and c not in "\n\t" for c in value))


def _stamp(value):
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        _need(moment.tzinfo is not None and moment.utcoffset() is not None,
              "inspection_timestamp_requires_timezone")
        return moment.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        raise InspectionError("inspection_timestamp_invalid") from None


def _source(source):
    _need(isinstance(source, dict) and set(source) == SOURCE_FIELDS,
          "attachment_source_fields_invalid")
    _need(source["verified"] is True and _text(source["evidence_ref"]),
          "attachment_source_unverified")
    _need(all(type(source[k]) is int and source[k] > 0 for k in
              ("account_id", "project_id", "task_id")),
          "attachment_source_identity_invalid")
    attachment = source["attachment_id"]
    _need((type(attachment) is int and attachment > 0) or
          (isinstance(attachment, str) and re.fullmatch(r"basecamp-sgid-sha256:[0-9a-f]{64}", attachment)),
          "attachment_source_identity_invalid")
    return dict(source)


def _spec(spec):
    fields = {"id", "mime", "mime_types", "min_bytes", "max_bytes", "facts",
              "width", "height", "page_count", "required_text", "forbidden_text", "text_inspection"}
    _need(isinstance(spec, dict) and set(spec).issubset(fields) and _text(spec.get("id")),
          "output_spec_invalid")
    _need(("mime" in spec) != ("mime_types" in spec), "output_spec_mime_required")
    mimes = [spec["mime"]] if "mime" in spec else spec["mime_types"]
    _need(isinstance(mimes, list) and 0 < len(mimes) <= len(MIMES)
          and all(isinstance(v, str) and v in MIMES for v in mimes)
          and len(set(mimes)) == len(mimes), "output_spec_mime_invalid")
    low, high = spec.get("min_bytes", 1), spec.get("max_bytes", MAX_BYTES)
    _need(type(low) is int and type(high) is int and 1 <= low <= high <= MAX_BYTES,
          "output_spec_size_invalid")
    facts = spec.get("facts", {})
    _need(isinstance(facts, dict) and set(facts).issubset({"width", "height", "page_count"}),
          "output_spec_facts_unsupported")
    facts = dict(facts)
    for key in ("width", "height", "page_count"):
        if key in spec:
            _need(key not in facts, "output_spec_fact_duplicated")
            facts[key] = spec[key]
    _need(all(type(value) is int and 0 < value <= 100_000 for value in facts.values()),
          "output_spec_fact_invalid")
    text_inspection = spec.get("text_inspection", "none")
    _need(text_inspection in ("none", "tesseract"), "text_inspection_unsupported")
    strings, ids = {}, set()
    for key in ("required_text", "forbidden_text"):
        rows = spec.get(key, [])
        _need(isinstance(rows, list) and len(rows) <= 50, "output_spec_text_invalid")
        strings[key] = []
        for row in rows:
            _need(isinstance(row, dict) and set(row) == {"id", "text", "critical"}
                  and _text(row["id"]) and _text(row["text"], 1000)
                  and type(row["critical"]) is bool and row["id"] not in ids,
                  "output_spec_text_requirement_invalid")
            ids.add(row["id"])
            strings[key].append(dict(row))
    return {"id": spec["id"], "mime_types": mimes, "min_bytes": low,
            "max_bytes": high, "facts": facts, "text_inspection": text_inspection, **strings}


def _sniff(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    # Deny common binary/active formats before considering plain UTF-8 text.
    if data.startswith((b"PK\x03\x04", b"8BPS", b"GIF8", b"RIFF", b"\x1f\x8b")):
        raise InspectionError("artifact_format_unsupported")
    try:
        value = data.decode("utf-8-sig", errors="strict")
    except UnicodeError:
        raise InspectionError("artifact_format_unsupported") from None
    _need(value and not any((ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127
                           for c in value), "artifact_plaintext_invalid")
    _need(not value.lstrip().lower().startswith(("<!doctype html", "<html", "<?xml", "<svg")),
          "artifact_active_text_unsupported")
    return "text/plain"


def _png(data):
    """Check full chunk CRCs and complete bounded non-interlaced scanlines."""
    position, chunks, idat, ended = 8, [], [], False
    width = height = depth = color = None
    palette_entries = None
    seen_idat = idat_closed = False
    while position < len(data):
        _need(position + 12 <= len(data), "png_chunk_truncated")
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        _need(re.fullmatch(b"[A-Za-z]{4}", kind) is not None
              and not (kind[2] & 32), "png_chunk_type_invalid")
        end = position + 12 + length
        _need(end <= len(data), "png_chunk_truncated")
        payload = data[position + 8:position + 8 + length]
        checksum, = struct.unpack(">I", data[position + 8 + length:end])
        _need(zlib.crc32(kind + payload) & 0xffffffff == checksum, "png_crc_invalid")
        _need(kind not in (b"acTL", b"fcTL", b"fdAT"), "animated_png_unsupported")
        if not chunks:
            _need(kind == b"IHDR" and length == 13, "png_header_invalid")
        if kind == b"IHDR":
            _need(not chunks and length == 13, "png_header_invalid")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            _need(0 < width <= 100_000 and 0 < height <= 100_000
                  and width * height <= MAX_PIXELS, "image_dimensions_exceed_limit")
            allowed = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8),
                       4: (8, 16), 6: (8, 16)}
            _need(color in allowed and depth in allowed[color] and compression == filtering == 0,
                  "png_header_invalid")
            _need(interlace == 0, "interlaced_png_unsupported")
        elif kind == b"PLTE":
            _need(palette_entries is None and not seen_idat and color not in (0, 4)
                  and length % 3 == 0 and 0 < length <= 768, "png_palette_invalid")
            palette_entries = length // 3
            _need(color != 3 or palette_entries <= 2 ** depth, "png_palette_invalid")
        elif kind == b"IDAT":
            _need(not idat_closed and (color != 3 or palette_entries is not None), "png_idat_invalid")
            seen_idat = True
            idat.append(payload)
        elif kind == b"IEND":
            _need(length == 0 and seen_idat and end == len(data), "png_end_invalid")
            ended = True
        else:
            _need(kind[0] & 32, "png_unknown_critical_chunk")
        if seen_idat and kind != b"IDAT":
            idat_closed = True
        chunks.append(kind)
        _need(len(chunks) <= 4096, "png_too_many_chunks")
        position = end
    _need(ended, "png_end_missing")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    row_bytes = (width * channels * depth + 7) // 8 + 1
    expected = row_bytes * height
    _need(expected <= MAX_EXPANDED_BYTES, "image_expansion_exceeds_limit")
    try:
        decoder = zlib.decompressobj()
        scanlines = decoder.decompress(b"".join(idat), expected + 1)
        _need(len(scanlines) == expected and decoder.eof and not decoder.unused_data
              and not decoder.unconsumed_tail, "png_scanlines_invalid")
        _need(all(scanlines[row * row_bytes] <= 4 for row in range(height)),
              "png_filter_invalid")
    except zlib.error:
        raise InspectionError("png_compression_invalid") from None
    return {"facts": {"width": width, "height": height, "bit_depth": depth},
            "text": None, "parser": "stdlib-png-crc-zlib-v1"}


def _limits():
    """Cloud Linux workers have hard memory/CPU limits in addition to timeout."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024,) * 2)
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        return "linux-memory-cpu-wall" if sys.platform.startswith("linux") else "cpu-wall-pixel-input"
    except (ImportError, ValueError, OSError):
        raise InspectionError("parser_resource_limits_unavailable") from None


def _jpeg_child(data):
    try:
        from PIL import Image, ImageFile
        from PIL import __version__
    except ImportError:
        raise InspectionError("jpeg_parser_dependency_missing") from None
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as im:
                _need(im.format == "JPEG", "jpeg_format_invalid")
                width, height = im.size
                _need(width > 0 and height > 0 and width * height <= MAX_PIXELS,
                      "image_dimensions_exceed_limit")
                _need(getattr(im, "n_frames", 1) == 1, "multi_frame_image_unsupported")
                im.load()
                _need(data.endswith(b"\xff\xd9"), "jpeg_end_or_trailing_data_invalid")
                # EXIF orientation changes displayed axes; retain both measured forms.
                orientation = im.getexif().get(274, 1)
                _need(type(orientation) is int and 1 <= orientation <= 8,
                      "jpeg_orientation_invalid")
                dw, dh = (height, width) if orientation in (5, 6, 7, 8) else (width, height)
                return {"facts": {"width": dw, "height": dh, "encoded_width": width,
                                  "encoded_height": height, "orientation": orientation},
                        "text": None, "parser": "Pillow-" + __version__}
    except InspectionError:
        raise
    except Exception:
        raise InspectionError("jpeg_decode_failed") from None


def _pdf_child(data):
    try:
        import pypdf
    except ImportError:
        raise InspectionError("pdf_parser_dependency_missing") from None
    try:
        reader = pypdf.PdfReader(io.BytesIO(data), strict=True)
        _need(not reader.is_encrypted, "encrypted_pdf_unsupported")
        _need(0 < len(reader.pages) <= MAX_PAGES, "pdf_page_count_exceeds_limit")
        root = reader.trailer["/Root"]
        # Reading never executes JS, but active content/embedded attachments are not
        # accepted as customer-facing static assets by this narrow inspector.
        _need(not any(k in root for k in ("/OpenAction", "/AA", "/AcroForm")),
              "pdf_active_content_unsupported")
        names = root.get("/Names")
        if names is not None:
            names = names.get_object()
            _need(not any(k in names for k in ("/JavaScript", "/EmbeddedFiles")),
                  "pdf_active_content_unsupported")
        text, lengths = [], 0
        for page in reader.pages:
            _need(not any(k in page for k in ("/AA", "/Annots")),
                  "pdf_annotations_unsupported")
            box = page.mediabox
            _need(all(math.isfinite(float(v)) for v in box)
                  and 0 < float(box.width) <= 20000 and 0 < float(box.height) <= 20000,
                  "pdf_page_dimensions_invalid")
            current = page.extract_text() or ""
            lengths += len(current) + 1
            _need(lengths <= MAX_TEXT_CHARS, "pdf_text_exceeds_limit")
            text.append(current)
        _need(data.rstrip().endswith(b"%%EOF"), "pdf_end_or_trailing_data_invalid")
        return {"facts": {"page_count": len(reader.pages)}, "text": "\n".join(text),
                "parser": "pypdf-" + pypdf.__version__}
    except InspectionError:
        raise
    except Exception:
        raise InspectionError("pdf_decode_failed") from None


def _ocr_child(data):
    executable = shutil.which("tesseract")
    _need(executable is not None and Path(executable).is_file(), "ocr_dependency_missing")
    try:
        version = subprocess.run([executable, "--version"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, timeout=2, check=False)
        first_line = version.stdout.splitlines()[0].decode("ascii", errors="strict")
        _need(version.returncode == 0 and len(version.stdout) < 65536
              and re.fullmatch(r"tesseract [0-9]+\.[0-9]+(?:\.[0-9]+)?(?:[-a-zA-Z0-9.]*)", first_line),
              "ocr_version_unverified")
        languages = subprocess.run([executable, "--list-langs"], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=2, check=False)
        available = set(languages.stdout.decode("utf-8", errors="strict").splitlines())
        _need(languages.returncode == 0 and len(languages.stdout) < 65536
              and {"eng", "tur"}.issubset(available), "ocr_languages_missing")
        # A private temporary output bounds text storage; attachment bytes only
        # cross stdin. No path, raw OCR text or parser logs is exposed by default.
        with tempfile.TemporaryFile(mode="w+b") as output:
            result = subprocess.run([executable, "stdin", "stdout", "-l", "eng+tur", "--psm", "6"],
                                    input=data, stdout=output, stderr=subprocess.DEVNULL,
                                    timeout=8, check=False)
            _need(result.returncode == 0, "ocr_failed")
            output.seek(0)
            text = output.read(MAX_TEXT_CHARS * 4 + 1)
        _need(len(text) <= MAX_TEXT_CHARS * 4, "ocr_text_exceeds_limit")
        text = text.decode("utf-8", errors="strict")
        _need(len(text) <= MAX_TEXT_CHARS, "ocr_text_exceeds_limit")
        return {"facts": {}, "text": text, "parser": first_line,
                "languages": ["eng", "tur"]}
    except subprocess.TimeoutExpired:
        raise InspectionError("ocr_timeout") from None
    except InspectionError:
        raise
    except Exception:
        raise InspectionError("ocr_failed") from None


def _isolated(data, mime, *, ocr=False):
    try:
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()),
                                 "--bounded-ocr" if ocr else "--bounded-parser", mime], input=data,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=MAX_PARSE_SECONDS, check=False,
                                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                                     "OMP_THREAD_LIMIT": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        _need(result.returncode == 0 and len(result.stdout) <= 2_000_000,
              "artifact_parser_failed_or_limited")
        parsed = json.loads(result.stdout)
        if "error" in parsed:
            _need(re.fullmatch(r"[a-z_]+", parsed["error"]) is not None,
                  "artifact_parser_response_invalid")
            raise InspectionError(parsed["error"])
        required = {"facts", "text", "parser", "resource_limits"}
        if ocr:
            required.add("languages")
        _need(isinstance(parsed, dict) and set(parsed) == required,
              "artifact_parser_response_invalid")
        return parsed
    except subprocess.TimeoutExpired:
        raise InspectionError("artifact_parser_timeout") from None
    except InspectionError:
        raise
    except Exception:
        raise InspectionError("artifact_parser_failed_or_limited") from None


def inspect_artifact(data, *, declared_mime, expected_sha256, brief_sha256,
                     output_spec, source, inspected_at=None, checked_at=None, include_text=False):
    """Inspect trusted-read attachment bytes, with immutable source/brief binding.

    `source.verified` is an adapter assertion, not a grant to an untrusted caller.
    Raw mail/model/comment dictionaries MUST NOT supply it. A result never means
    quality approved or customer delivery authorized. No text/image OCR is implied.
    """
    _need(type(data) is bytes and 0 < len(data) <= MAX_BYTES, "artifact_byte_limit_invalid")
    _need(type(include_text) is bool, "include_text_invalid")
    _need(isinstance(expected_sha256, str) and HASH.fullmatch(expected_sha256)
          and isinstance(brief_sha256, str) and HASH.fullmatch(brief_sha256),
          "artifact_or_brief_hash_invalid")
    actual_hash = hashlib.sha256(data).hexdigest()
    _need(actual_hash == expected_sha256, "artifact_bytes_changed")
    _need((inspected_at is None) != (checked_at is None), "inspection_timestamp_required_once")
    current_source, spec, checked_at = (_source(source), _spec(output_spec),
                                      _stamp(inspected_at if inspected_at is not None else checked_at))
    _need(isinstance(declared_mime, str) and declared_mime in MIMES,
          "artifact_declared_mime_unsupported")
    mime = _sniff(data)
    _need(mime == declared_mime, "artifact_mime_mismatch")
    if mime == "image/png":
        parsed = _png(data)
        parsed["resource_limits"] = "bounded-byte-pixel-zlib"
    elif mime in ("image/jpeg", "application/pdf"):
        parsed = _isolated(data, mime)
    else:
        content = data.decode("utf-8-sig")
        _need(len(content) <= MAX_TEXT_CHARS, "artifact_text_exceeds_limit")
        parsed = {"facts": {"character_count": len(content)}, "text": content,
                  "parser": "stdlib-utf8-v1", "resource_limits": "bounded-byte-text"}
    facts, text = parsed["facts"], parsed["text"]
    ocr = None
    if spec["text_inspection"] == "tesseract":
        _need(mime in ("image/png", "image/jpeg"), "ocr_format_unsupported")
        try:
            measured = _isolated(data, mime, ocr=True)
            text = measured["text"]
            ocr = {"status": "completed", "parser": measured["parser"],
                   "languages": measured["languages"], "normalization": "nfkd-casefold-marks-whitespace-v1"}
        except InspectionError as exc:
            ocr = {"status": "unassessed", "reason": exc.code}
    ref = "sha256:" + _digest({"artifact_sha256": actual_hash, "brief_sha256": brief_sha256,
                                "source": current_source, "spec": spec, "checked_at": checked_at,
                                "inspector": INSPECTOR_ID, "parser": parsed["parser"], "ocr": ocr})
    checks = {}
    def check(key, result, critical, location, finding):
        checks[key] = {"result": result, "critical": critical, "method": "deterministic",
                       "evaluator_id": INSPECTOR_ID, "evidence_ref": ref, "location": location,
                       "finding": finding, "artifact_sha256": actual_hash,
                       "brief_sha256": brief_sha256}
    check("mime_type", "pass" if mime in spec["mime_types"] else "fail", True,
          "file signature and decoded format", "Measured MIME: " + mime)
    check("size_bytes", "pass" if spec["min_bytes"] <= len(data) <= spec["max_bytes"] else "fail",
          True, "complete file bytes", "Measured bytes: " + str(len(data)))
    if ocr:
        check("text_inspection", "pass" if ocr["status"] == "completed" else "unassessed",
              True, "actual image pixel OCR process",
              "Bounded eng+tur OCR completed." if ocr["status"] == "completed"
              else "A successful OCR process was not available.")
    for key, expected in spec["facts"].items():
        result = "unassessed" if key not in facts else ("pass" if facts[key] == expected else "fail")
        check("fact:" + key, result, True, "decoded file structure",
              "Expected " + key + "=" + str(expected) + "; measured=" + str(facts.get(key)))
    def normalize(value):
        if ocr and ocr["status"] == "completed":
            normalized = unicodedata.normalize("NFKD", value).casefold()
            return " ".join("".join(c for c in normalized if not unicodedata.combining(c)).split())
        return unicodedata.normalize("NFC", value)
    normalized = normalize(text) if text is not None else None
    for key in ("required_text", "forbidden_text"):
        for row in spec[key]:
            if normalized is None:
                result, finding = "unassessed", "Image text not established by a successful OCR or visual-model inspection."
            else:
                present = _literal_present(normalize(row["text"]), normalized)
                good = present if key == "required_text" else not present
                result = "pass" if good else "fail"
                if ocr and key == "forbidden_text" and not present:
                    # OCR silence cannot prove a prohibited/internal phrase is
                    # absent from the visible image. Require separate review.
                    result = "unassessed"
                finding = "Literal present in extracted text." if present else "Literal absent from extracted text."
                if ocr:
                    finding = ("Required literal detected in actual pixel OCR." if present else
                               "Literal not detected by pixel OCR; this is not proof of visual absence.")
            check(key + ":" + row["id"], result, row["critical"],
                  "UTF-8 text" if mime == "text/plain" else (
                      "actual image pixels via OCR" if ocr and ocr["status"] == "completed"
                      else "extracted text (visibility not established)"), finding)
    values = [row["result"] for row in checks.values()]
    status = "failed" if "fail" in values else ("unassessed" if "unassessed" in values else "passed")
    limitations = ["Deterministic checks do not establish brand meaning, visual quality, rights or current send authority."]
    if mime.startswith("image/"):
        limitations.append("No visual-semantic review or approved-logo comparison was performed.")
        limitations.append("OCR is fallible and cannot prove that undetected text is visually absent." if ocr and ocr["status"] == "completed"
                           else "Image text remains unassessed without a successful OCR process.")
    if mime == "application/pdf":
        limitations.append("Extracted PDF text may be hidden or non-visible; layout and visible text need separate rendering review.")
    result = {"schema_version": 1, "inspector_id": INSPECTOR_ID, "parser": parsed["parser"],
              "resource_limits": parsed["resource_limits"], "checked_at": checked_at,
              "source": current_source, "source_sha256": _digest(current_source),
              "brief_sha256": brief_sha256, "output_spec_sha256": _digest(spec),
              "artifact": {"id": spec["id"], "sha256": actual_hash, "mime_type": mime,
                           "size_bytes": len(data), "facts": facts,
                           "facts_bound_sha256": actual_hash, "evidence_ref": ref},
              "checks": checks, "inspection_status": status,
              "ocr": ocr,
              "text_sha256": hashlib.sha256(text.encode()).hexdigest() if text is not None else None,
              "semantic_review_required": True, "delivery_authorized": False,
              "limitations": limitations, "network_writes": 0, "model_calls": 0}
    result["evidence_sha256"] = _digest(result)
    if include_text:
        result["extracted_text"] = text
    return result


def _main():
    # Internal subprocess entrypoint; no output other than bounded JSON.
    if (len(sys.argv) != 3 or sys.argv[1] not in ("--bounded-parser", "--bounded-ocr")
            or sys.argv[2] not in ("image/png", "image/jpeg", "application/pdf")):
        return 2
    try:
        limits = _limits()
        data = sys.stdin.buffer.read(MAX_BYTES + 1)
        _need(0 < len(data) <= MAX_BYTES, "artifact_byte_limit_invalid")
        if sys.argv[1] == "--bounded-ocr":
            _need(sys.argv[2] in ("image/png", "image/jpeg"), "ocr_format_unsupported")
            value = _ocr_child(data)
        else:
            value = _jpeg_child(data) if sys.argv[2] == "image/jpeg" else _pdf_child(data)
        value["resource_limits"] = limits
    except InspectionError as exc:
        value = {"error": exc.code}
    except Exception:
        value = {"error": "artifact_parser_failed_or_limited"}
    sys.stdout.write(json.dumps(value, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
