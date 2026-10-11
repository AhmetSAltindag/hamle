Hamle v1.1 byte inspector — internal pilot, 10 October 2026

Scope
This component reads supplied actual bytes only. It has no provider client,
credentials, network operation, model call or cloud mutation. It never declares
WAT artwork accepted or a customer delivery authorized. The provider adapter must
first authenticate Hamle, verify private project/task membership and read the
actual attachment metadata and bytes. A verified flag received in a mail, task
comment or model output cannot provide that authority.

API
  inspect_artifact(data: bytes, *, declared_mime, expected_sha256,
      brief_sha256, output_spec, source, inspected_at, include_text=False)

  source = {
    "verified": true, "account_id": 5898865, "project_id": 49184053,
    "task_id": <real provider task ID>, "attachment_id": <real attachment ID>,
    "evidence_ref": <verified read receipt>
  }

  output_spec = {
    "id": "main", "mime": "image/png",
    "facts": {"width": 1080, "height": 1080},
    "text_inspection": "tesseract",
    "required_text": [
      {"id": "heading", "text": "HAMLE İÇ DENEME", "critical": true},
      {"id": "date", "text": "12 EKİM 2026", "critical": true},
      {"id": "disclaimer", "text": "Bu bir teknik denemedir", "critical": true}
    ]
  }

mime_types can replace singular mime. min_bytes/max_bytes constrain the actual
complete bytes; width/height/page_count top-level aliases can replace facts.
forbidden_text has the same row shape as required_text. Unknown spec fields,
unsupported measurements, duplicate IDs or boolean dimensions are rejected.
expected_sha256 comes from the caller's immutable downloaded-file manifest; an
old manifest cannot be reused for different bytes. The current accepted brief
hash, normalized output spec, source receipt, parsed facts and checked time all
bind the evidence. Source correctness itself remains the adapter's duty.

Return value
artifact is compatible with a v1.1 delivery manifest row:
  id, sha256, mime_type, size_bytes, facts, facts_bound_sha256, evidence_ref
checks contains measured format/byte size/geometry/text requirement results.
inspection_status is passed / failed / unassessed for these checks only.
delivery_authorized is always false; semantic_review_required is always true.
evidence_sha256 binds the complete result excluding optional exposed raw text.
text_sha256 binds the extracted text. include_text=True returns that bounded text
in memory for a further trusted inspector; it must not be put into ordinary logs.
InspectionError.code is static and does not echo file contents or parser errors.

Real checks and limits
- PNG: complete chunk CRC, order, critical chunks, pixel bounds, deflate end,
  exact scanline length and filter bytes. Non-interlaced static PNG only.
- JPEG: full actual image decode via Pillow, strict truncated-image rejection,
  EXIF orientation accounted for in displayed width/height. Encoded dimensions
  also retained. No multiple frames or trailing payload accepted.
- PDF: pypdf strict parsing, real page count and text extraction. Encrypted files,
  page annotations, forms, active actions and embedded files are held. A PDF text
  match is NOT proof of visibility: hidden/invisible text can exist. Rendering
  review is still needed for visual requirements.
- TXT: real strict UTF-8 text, exact NFC literal matches. Binary and common active
  document signatures are rejected even if declared text/plain.
- PSD, ZIP, videos, APNG and unknown types are unsupported. They do not get a
  fabricated score or successful technical-inspection flag.

Input <=16 MiB; <=24 million pixels; PNG expansion <=96 MiB;
PDF <=30 pages; extracted text <=250,000 characters. JPEG/PDF/OCR parsers run in
separate processes with a 12-second parent timeout. CPU <=10 seconds, no core
dump, <=64 open files, <=2 MiB written file size. Cloud Linux additionally has a
512 MiB address-space limit. macOS lacks that Linux memory limit but keeps the
pixel/input/CPU/wall limits. The child inherits no Cloud/provider credentials.

Optional actual pixel OCR
Only explicit text_inspection=tesseract starts OCR for PNG/JPEG. It requires a
real tesseract executable in the fixed PATH and both eng and tur language packs.
The executable version and languages are checked; image bytes go through stdin.
Text output uses a private automatically removed temporary file, bounded output
and an 8-second OCR timeout. No image file is persisted by this component.
Missing binary/language, failed process, limits or timeout yield unassessed,
never a made-up text result. Default image text stays unassessed without OCR.
The text match uses NFKD, case folding, removal of combining marks and whitespace
collapse. Digits, currency and unit strings remain unchanged. An OCR miss means
required text was not detected, not proof that it is visually absent. An OCR
pass does not establish legibility, composition, brand compliance or asset rights.
Visible required-text fixtures are a narrow technical internal rehearsal only.

Dependencies (optional formats fail closed when not installed)
  Pillow==12.3.0  (JPEG decode; root also uses it for internal fixture creation)
  pypdf==6.10.0   (PDF page/text inspection)
  Debian packages for the cloud image:
    tesseract-ocr, tesseract-ocr-tur, fonts-dejavu-core
Root owns requirements-cloud.txt and Dockerfile integration.

Validation
  ../hamle-gmail-connector/.venv/bin/python -B -m unittest test_v11_artifact_inspector -v
Unit tests build real PNG, TXT, JPEG and PDF bytes. JPEG/PDF real-process tests
skip only if their optional dependencies are absent. OCR contract tests explicitly
mock the OCR adapter and are not claimed as real OCR acceptance. Root's deployed
internal pilot must demonstrate the actual cloud Tesseract process on real pixels,
including a missing-date revision and the corrected file before final delivery.
