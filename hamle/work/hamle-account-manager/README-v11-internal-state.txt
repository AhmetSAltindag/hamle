Hamle v1.1 — durable internal production pilot journal

Fixed pilot: HAMLE-IC-URETIM-20261010-01. This module uses the existing
OperationsStore backend and its transaction/clock/50-USD-budget rules. It creates
separate pilot/action documents inside that backend; no provider call, credential
access, model call or cloud resource change occurs in the module itself.

Constructor and record API
  store = InternalPilotStore(operations_store)
  store.initialize(state, source_evidence) -> pilot record
  store.read() -> pilot record or None
  store.save(state, expected_revision) -> pilot record
  store.summary() -> safe status/action summary, without mail/payload/receipt body

source_evidence EXACT fields:
  verified: true
  subject: HAMLE-IC-URETIM-20261010-01
  thread_id, message_id: actual Gmail hex IDs
  source_fingerprint: 64 lowercase hex digest of authenticated current source
  control_generation: current OperationsStore control generation
  captured_at: aware ISO time, at most 300 seconds old when first initialized
  expires_at: aware ISO time, no later than 12 October 2026 23:59:59 Istanbul
  evidence_ref: safe label for actual authenticated source-read evidence

state is bounded JSON and requires mode=internal_pilot, work_id and
control=active/stopped/takeover. When state has a brief, brief.reply_envelope's
thread_id must match the pinned source; brief.binding must target actual account
5898865 / private internal project49184053. WAT project40694338 is rejected.
Production state remains opaque beyond these scope checks. Existing state
machines own semantic transitions. State/whole document size must stay <=60 KB;
the root worker should keep bulky OCR text, histories and file bytes out of it.

The returned record has state, state_sha256, revision, generation, expires_at,
source_evidence, source_evidence_sha256, initial_state_sha256 and action_keys.
Record revision is a store CAS counter, separate from production state.revision.
initialize is idempotent only for the exact first source/initial state. It returns
current saved progress and never resets the pilot. read and summary remain usable
after stop/expiry for evidence. A conflicting state/source requires explicit new
design, not a reset. Stop/takeover never automatically resumes this fixed pilot.

Action API and required order
  row = store.prepare_action(action_key, payload)
  fresh authenticated mail + Hamle To/CC + current private Basecamp preflight
  row = store.begin_action(action_key, row['payload_sha256'])
  if row['dispatch_allowed'] is True:
      response = perform EXACT provider POST represented by payload
      row = store.record_receipt(action_key, safe_response_receipt)
      readback = authenticated provider GET / exact response verification
      row = store.finish_action(action_key, safe_response_receipt, proof)
  elif row['state'] == 'verified':
      reuse row['receipt'] and row['proof']; DO NOT POST

finish proof required fields:
  verified=true, action_key, payload_sha256,
  receipt_sha256=cloud_store.proposal_hash(receipt), evidence_ref
Additional actual-provider readback evidence may be included and is hash-bound.
receipt is a nonempty JSON object with the actual provider's returned identifier
or equivalent attachment receipt. No secret or raw binary belongs in either.

The immutable payload must include all externally effective request data and any
attachment content hash/length. The provider dispatcher must build its exact POST
from that payload, verify thread/target/people before every write, and never treat
untrusted mail/model/comment data's verified flag as authority. The journal cannot
prove these provider facts by itself. It is not a general customer-send grant.

At most one successful transaction returns dispatch_allowed=true. This transient
flag is never persisted, so rereading a started document never re-grants a POST.
prepared -> started is irreversible. Started, receipt_saved and unknown actions
cannot automatically dispatch again; uncertain Firestore commit cannot be retried
as a provider call. Repeating prepare with identical payload only returns the
existing row; changed payload under the same key is rejected. A state change
after preparation invalidates its dispatch. Duplicate concurrent begins permit
exactly one caller. Maximum64 immutable actions for this fixed pilot.

record_receipt MUST run immediately after provider response and before readback,
so a failed GET does not lose the only returned provider ID. On failure call:
  store.fail_action(action_key, static_reason)
The action becomes unknown and keeps any saved receipt. Evidence may later be
reconciled by finish_action with exact authenticated readback proof; no POST is
re-enabled. Receipt/proof conflicts are refused. Exact verified completion is
idempotent. record_receipt/finish/fail may persist evidence after global stop or
expiry; they never alter control, budget, source or provider data.

Control/budget fencing
initialize/save/prepare/begin require review_open with the exact pinned generation,
current time before the pilot expiry and existing global authorization deadline,
Google Cloud billing_ready, no overrun hold, committed shared budget <50 USD.
Global stop/resume advances generation and invalidates this old pilot rather than
silently resuming it. begin/prepare also require work control active. A state stop
or takeover is permanent in this fixed pilot. This ledger does not install a
provider billing hard cap or reserve model spend. The existing paid-call budget
API is still required before any model/image generation request.

Every transaction finishes all reads before any write (Firestore rule). Network
calls must stay outside transactions. Firestore max_attempts=1 and stable IDs from
OperationsStore preserve ambiguity for explicit reconciliation. A provider call
already accepted upstream cannot be rolled back by a later stop.

Validation
  ../hamle-gmail-connector/.venv/bin/python -B -m unittest test_v11_internal_state -v
Tests use the existing serializable MemoryBackend enforcing read-before-write.
They cover restart, conflicting sources, stale CAS, concurrent first dispatch,
unknown-after-commit, stored receipt before failed readback, proof/receipt mismatch,
global/work takeover, no implicit resume, expired authorization, budget hold,
payload conflict, unchanged initial state and post-stop evidence preservation.
