Hamle v1.1 internal production provider adapter — 10 October 2026

Purpose and boundaries
v11_internal_providers.InternalProviders has real single-attempt adapters but
performs no import-time I/O. Only Basecamp account 5898865 / private internal
project 49184053 / Todoset 10384126975 / Hamle assignee 53358100 are allowed.
Historical tasks 10391849167 and 10392247471 are always blocked for new writes.
An allowlist of fresh task IDs must come from the caller's durable verified
receipts. This adapter does not enable WAT or other customer operations.

Constructor
  InternalProviders(secrets, claim_refresh=None, allowed_task_ids=())
Existing GoogleSecretManager and credential refresh claim callbacks are reused.
Never log secret records, returned source body, Basecamp signed download URLs,
raw MIME, attachment bytes or OAuth headers. API errors expose static codes.

Public interface
  capture_source()
    Exact HAMLE-IC-URETIM-20261010-01 latest eligible mail from Ahmet; Hamle
    must be in actual current To/CC. Returns source_text and evidence in memory.
    cloud_providers.capture_internal_request now has exact_subject optional;
    old default remains HAMLE-IC-IS-20261010-01. No arbitrary subject or new
    continuation scope is accepted, and no global is changed at runtime.
  verify_internal()
    Existing provider identity/private membership/parent readback.
  create_task(title, body, *, before_post)
  read_task(provider_id, title, body)
    Uses BusinessWriters narrow task adapter and full task content readback.
    Caller records create receipt before readback. No implicit task allow grant.
  upload_attachment(name, data: bytes, mime, *, before_post)
    PNG only, 2 MiB each maximum; safe ASCII basename; no paths. Raw bytes go
    once to /attachments.json?name=... with exact MIME and length headers.
    Returns provider_id, identifier_kind, attachable_sgid, filename,
    content_type, byte_size and sha256. Native optional metadata is checked.
    A legacy SGID-only response uses provider_id=basecamp-sgid-sha256:<digest>
    with identifier_kind=sgid_digest; this is explicitly NOT a numeric API ID.
  create_comment(task_id, body, *, before_post, attachments=())
  read_comment(provider_id, task_id, body, attachments=())
    Require fresh ledger-bound task, Hamle ownership/assignment, active private
    task with fixed parent. Plain text is escaped and explicit supplied SGIDs
    are rendered as bc-attachment. Full readback checks exact body, parent,
    author, visibility, attachment IDs/SGIDs/name/type/size. Returns safe
    attachment_receipts with actual native IDs when the API supplies them.
  download_attachment(task_id, comment_id, attachment_receipt) -> bytes
    Re-reads current task/comment and binds exact supplied receipt to returned
    native content_attachments. Requires exact account in metadata download
    URL path. Initial authenticated GET only to known Basecamp API/storage
    domains; one 302 to exact storage.basecamp.com is supported WITHOUT OAuth
    header. No other redirects, domains, extra downloads or credential retry.
    Full bytes must match immutable expected length and SHA256.
  send_delivery(source_evidence, body, attachments, *, before_post)
  read_delivery(provider_id, source_evidence, body, attachments)
    attachments is exactly two dictionaries with filename, data, content_type.
    Sends only to Ahmet in the new exact thread, preserving approved actual CC
    (currently Cansu only). Does not add recipients from earlier threads.
    Readback fetches recorded Gmail Sent receipt as raw MIME and checks sender,
    To/CC/noBcc, subject/thread/RFC reply links, body and both attachment hashes.
    This is Sent-side readback, not Ahmet-inbox delivery confirmation.

Caller responsibilities
Every provider POST has a mandatory before_post callback, called immediately
before that one request after preflight reads. It must durably claim the exact
operation and fence human takeover, control generation, source authority,
authorized time and shared budget. The adapter deliberately does not implement
business authorization from model text. A callback is not a complete workflow:
caller must never automatically retry a dispatch-started or unknown operation.
Persist POST receipt before GET verification; a failed readback must not resend.
Readbacks may be explicitly reconciled using the retained exact receipt.

Each upload and each comment is a distinct operation and requires its own
ledger entry. Uploading does not attach a file until the separate comment POST.
A read proof never declares visual/semantic quality or customer send authority.
The unseen-CC-removal limitation remains unresolved; no Ahmet mailbox monitoring
or forwarding is installed here.

Official API references inspected
https://github.com/basecamp/bc-api/blob/master/sections/attachments.md
https://github.com/basecamp/bc-api/blob/master/sections/comments.md
https://github.com/basecamp/bc-api/blob/master/sections/rich_text.md
Current native API was also read against old internal comment 10391852105:
attachment native ID 903148527, 264-byte text file; content_attachments includes
id/attachable_sgid/sgid/byte_size/content_type/filename/download_url. The Basecamp
CLI transforms these entries; its derived shape must not be mistaken for native
API shape. Existing old comment and file were not modified.

Tests
  ../hamle-gmail-connector/.venv/bin/python -m unittest \
    test_v11_internal_providers test_cloud_providers test_business_writers -q
85 targeted tests passed: 25 adapter tests and 60 preserved regression tests.
Network prohibited during tests. Live mutating proof is owned by root worker;
this component's author performed only authorized old internal readbacks.

Scheduled pilot hook
worker.run_scheduled_review now accepts optional advance_internal_pilot callback.
The production main entry wires it ONLY for scheduled_review, to the trusted
InternalWorker.advance_if_active method. It is called after the existing 15-minute
slot claim and generation fence. None (no/completed pilot) preserves old review.
A verified exact pilot summary consumes the slot as already_recorded and skips
old review/model calls. Any unknown/error/invalid summary holds; no fallback and
no slot replay. It never initializes a pilot from a scheduler alarm. The callback
and durable action store still own individual at-most-once writes and expiry.
Business write count is reported unknown (null) after callback entry rather than
falsely claiming zero when a later readback might fail. 10 hook tests plus all
43 existing worker tests passed (53 total).
