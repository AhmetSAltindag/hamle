HAMLE INTERNAL FIRESTORE OPERATIONS STORE

cloud_store.py implements the durable internal workflow boundary, not an agent
or provider adapter. No Gmail, Basecamp, model, browser, secret or billing API is
called. Firestore is lazy-imported only by explicit FirestoreBackend(opt_in="yes")
with project gamechangers-hamle and database hamle-operations. The SDK remains
the existing google-cloud-firestore 2.34.0. A single collection hamle_internal_v1
contains bounded control, proposals, source deduplication, actions, schedule
slot claims, model accounting receipts, thread/work/task bindings, continuation
events, current task observations and budget.

Construction:
  backend = FirestoreBackend(project="gamechangers-hamle",
      database="hamle-operations", opt_in="yes")
  store = OperationsStore(backend)
  store.initialize_control()  # default stopped; existing state never reset
No environment fallback, credentials file or secret is inspected by this module.
Caller closes backend.close(). OperationsStore accepts an injectable aware clock.

CONTROL / UI API
  control(), public_state() (snapshot alias)
  stop(actor_subject), takeover(actor_subject), resume_review_only(actor_subject)
  set_mode(mode, actor_subject, expected_generation=None)
  put_proposal(proposal_id, source_fingerprint, payload, expected_generation)
  approve(proposal_id, proposal_hash, generation, actor_subject,
      purpose="routine_execution")
The UI MUST verify signed IAP identity and its exact human allowlist before
passing actor_subject. A subject string is an audit label, not authentication.
The store itself cannot prove that Ahmet clicked. No arbitrary client payload
may be accepted as a proposal: the internal worker validates the source and
constructs the payload. Display every approved destination, exact action and
explicit approval purpose.

V3 approval purpose is recorded separately from the immutable model payload.
routine_execution is the default; proposals carrying model analysis must have
intent=routine_request and escalation.owner=none. internal_coordination_only
requires gamechangers_internal, intent=complaint_strategy and owner=cansu_and_ahmet,
with Hamle->Ahmet, only verified current CC through recipient_policy, the fixed
internal reply subject, and a task in
account5898865/project49184053 assigned to Hamle53358100 with due_at=null.
The coordination approval records purpose=internal_coordination_only,
escalation_status=open, decision_authorized=false and production_authorized=false.
Routine approval records escalation_status=not_required. No classification,
proposal content, source digest or model result is rewritten by approval.
IAP UI must bind the explicitly displayed purpose to the same hash/generation.
An existing approval with absent purpose is treated as routine_execution only.
A different-purpose replay fails approval_purpose_conflict; same-purpose replay
retains the original timestamp. The same 300-second dispatch validity applies.
Coordination approval alone does not establish Cansu notification or Özge consultation, and
does not grant strategic, production, commercial or customer-operation authority.

Every control transition increments generation. Stop/takeover invalidate all
old pending work atomically without scanning. public_state marks old-generation
proposals cancelled and begun-but-unfinished actions unknown. A resume cannot
resurrect them. Stored historical records remain unchanged for evidence. Stop
and takeover remain available after expiry and clock regression. Review resume,
approval, proposal creation, claim, dispatch and budget start enforce current
authorization deadline 12 October 2026 23:59:59 Europe/Istanbul.

PAYLOAD AND EFFECT API
Proposal payload is bounded JSON (60KB), immutable by its canonical SHA256.
source_fingerprint must be a stable source snapshot/content digest, NOT a digest
of capture time or a random value. A source index prevents bypassing deduplication
by choosing another proposal ID. A cancelled source cannot be silently requeued;
a new request or explicit future renewal workflow is required. Initial queue is
limited to 50 proposals. This limit is deliberate for the first internal scope.
Thread/work/task correlation is now a separate immutable record:
  bind_verified_work(seed_proposal_id)
  get_work(work_id), work_for_thread(thread_id)
  record_work_observation(work_id, snapshot)
  claim_followup(work_id, message_id, source_fingerprint, expected_generation)
  put_followup_proposal(claim, payload), hold_followup(claim, reason)
  complete_followup(work_id, proposal_id)
Binding derives all identities from the initial approved proposal and verified
Basecamp-task/Gmail-reply receipts. Arbitrary provider IDs cannot be supplied.
The work stores immutable root thread/message/RFC IDs, task/account/project and
parent IDs/type, seed evidence, version=1, inherited escalation, latest task
observation, pending event, last processed incoming/sent IDs and revision_count.
Rebinding the same seed returns current state without resetting progress.

One unresolved event is allowed per work. A message identity permanently claims
its event once; unknown commit/model outcome cannot replay or release it.
Only verified comment AND reply receipts complete the event, clear pending and
increment revision_count. Binding version remains 1. Repeated completion is a
no-op, including after a later event starts. Accounting completion is permitted
after stop/expiry but starts no new provider action. A completed/archived/trashed
task blocks new continuation claims. An observation never closes escalation.
public_state now returns works as well as proposals/actions. UI projects only
explicitly allowed display fields; raw event/lease data is not an HTTP surface.

The follow-up envelope has scope,analysis,work,comment,reply,source, with no task
creation payload. An inherited open Cansu/Ahmet escalation requires explicit
internal_coordination_only even if new analysis is routine/social/unclear;
commercial intent stays blocked. A previously not_required work encountering
new escalation is held for a future explicit migration. Human/model content is
never rewritten to remove that boundary. Claim and dispatch recheck the same
bound work and pending event transactionally.

  aid = action_id(proposal_id, kind)
  lease = claim_action(aid, proposal_id, kind, whole_proposal_hash,
      worker, expected_generation, lease_seconds=60)
  begin_dispatch(lease, current_source_fingerprint,
      approval_max_age_seconds=300)
  record_receipt(lease, provider_id) OR mark_unknown(lease)
  confirm_readback(aid, same_provider_id)
Allowed kinds: gmail_reply, basecamp_todo, basecamp_comment. One canonical action
per proposal/kind. Another ID is rejected. An existing action is never reclaimed,
even after lease expiration or ambiguous commit. Expired pre-dispatch leases
also require human reconciliation; there is no automatic requeue. Approval
replay does not extend its timestamp. UI state omits the internal lease token.
For worker.py proposals, current_source_fingerprint is the COMPOSITE digest of
thread/message IDs, capture metadata fingerprint and actual body SHA256, computed
by worker._capture. It differs from payload.source.snapshot_fingerprint, which
is only the original metadata digest. Future dispatch MUST use that same stable
composite; capture time never participates in it. Actions must be declared in
the approved payload: reply maps to gmail_reply, task maps to basecamp_todo,
comment maps to basecamp_comment;
an explicit actions map is also supported by the low-level store.

Before begin_dispatch, the worker MUST reread actual Hamle source, exact To/CC,
self/auto checks, trusted scope, account identity, target project membership and
visibility. It must match the external write to the complete approved payload.
This store cannot solve E04: an unseen CC removal cannot be inferred. Customer
scope stays closed. action_eligible/external_actions_enabled is not granted by
public_state. A proposal may contain several approved internal actions, but no
unlisted action may be invented by a worker.

Firestore transaction bodies contain ONLY document reads/writes. Provider calls
must happen outside them and never receive transaction retry semantics. SDK
application transaction attempts=1. Unknown commits are fixed StoreError codes,
not permission to repeat. After begin_dispatch returns success, a stop can still
race the upstream network call; there is no claim of absolute recall. A later
receipt/readback may be saved after stop/deadline without reopening control.
Receipts are evidence assertions supplied by the trusted provider adapter;
confirm_readback does not itself contact the provider.

BUDGET API
  initialize_budget(snapshot)  # immutable import fingerprint, no automatic reset
  budget_status()
  budget_reserve(key, provider, integer_microdollars)
  budget_begin(key, expected_generation)
  budget_unknown(key)
  budget_settle(key, actual_microdollars, evidence_ref)
Snapshot shape:
  policy_id = hamle-shared-20261009-20261012
  limit_microdollars = 50000000
  expires_at = 2026-10-12T23:59:59+03:00
  overrun_hold = bool
  providers = map with google_cloud/openai/visual_generation, each containing
    baseline_microdollars, paid_microdollars (nonnegative ints), billing_ready bool
  reservations = list of reservation_key, provider, reserved_microdollars,
    actual_microdollars (null unless settled), state reserved/started/unknown/
    settled, evidence_ref (required for settled)
  evidence_ref = sanitized local export filename
Root must export the real existing local budget, including all historical holds,
prepayment, settled costs and overrun flag. The format does not itself verify a
provider bill. The current 0.21571 USD first-Astra token estimate is NOT a settled
invoice: keep its 1 USD hold until reconciliation. Preserve Google's 3 USD hold
and any newly reserved infrastructure amount. OpenAI's 10 USD prepaid purchase
counts once: provider commitment=max(prepaid purchases, baseline+usage+holds).
Concurrent reservations serialize against one budget document. Unknown remains
held; actual overspend freezes future reservations. There is no provider hard cap
or automated billing reconciliation. Call start and action gates are distinct;
an aborted later gate can conservatively retain an unused reservation.

ROTATING CREDENTIAL REFRESH
claim_credential_refresh(record_hash) atomically returns True once, False on
every later claim for that SHA256 identifier. It writes only an irreversible
refresh claim and timestamp, no credential value. It requires a current date
within authorization but may run while workflow is stopped, for explicit
read-only connection verification. A crash or uncertain token/secret write
requires manual reconciliation; no lease expiry or automatic reset permits a
second refresh. A new successfully persisted credential version has a new hash.

INITIAL OPERATIONAL LIMITS
50 proposals and 200 budget reservations; bounded JSON. No archive, renewal,
database restore, identity provision, secret rotation, live scheduler or provider
reconciliation implementation. No customer rollout accepted. Cloud deployment
and real persistence acceptance are still separate from fake-store tests.

Tests use a locked, rollback-capable in-memory transaction fake that rejects
read-after-write like Firestore. They cover concurrent single action ownership,
stop/dispatch race, replay under a new ID, ambiguous commit, immutable approval,
expiry, post-stop evidence, budget migration, prepaid overlap and racing spend.
They perform no network, billing, provider mutation or emulator installation.
Model response accounting receipt
--------------------------------
record_model_receipt(reservation_key, receipt) stores only response_id,
model=gpt-6-astra, reasoning_effort=max, and bounded integer token usage (optional
cached/reasoning token counts). The stable receipt document is immutable and
requires an already-started OpenAI reservation. Extra content or secret/text
fields fail validation. model_receipt(reservation_key) reads the same record.
Stop and budget expiry do not prevent recording evidence from an in-flight call.
Usage is not an invoice: this operation never settles or releases held funds.
Continuation store suite: 47 offline tests passed. Store/worker/dispatch/UI/
provider/writer/auth/model integration passed 251 tests. Historical V3 evidence is unchanged; these
local changes cause no live model call, deployment or cloud-ledger mutation.

SCHEDULE PREPARATION
claim_scheduled_slot(generation, interval_minutes=15) uses trusted Istanbul
time, weekdays 08:30 through 18:30 inclusive and a five-minute starting grace.
The new user-selected default is 41 quarter-hour slots. The optional 30-minute
helper interval preserves 21 slots; identical times share permanent claim keys.
Stop, deadline and shared budget are checked before a claim. Claims are never
released on crash or generation change. No automatic business dispatch occurs;
scheduler creation/activation is a separate operation not performed here.

TERMINAL INCOMPLETE RESPONSE RECOVERY
  record_model_failure(reservation_key, reason, receipt=None)
  model_failure(reservation_key)
  claim_followup_recovery(work_id, source_fingerprint, task_fingerprint,
      expected_generation, failure_evidence_hash)
Failure receipts are separate immutable documents, not usable drafts or billing
settlement. Strict allowlists retain only provider response metadata and usage.
A maintenance recovery is allowed only for the same held response_incomplete
event, same generation/source/task, a still active task, existing original hold,
no successful receipt/proposal/action, and no prior recovery claim. Historical
v6 lacked a failure receipt; its explicit legacy gap is preserved with the
execution-evidence hash and null receipt. No old provider ID/cause/usage is
invented. A present but unverified/nonterminal failure cannot use that exception.

The immutable authorization inside the permanent recovery record names
codex-authorized-maintenance, content_approval=false, exact evidence/fingerprints
and max_attempts=2. The existing event token/identity and first failure remain.
The worker reserves a separate 2 USD attempt2 hold; it never releases/repurposes
the original hold. Success publishes the same proposal ID and retains the usual
human-approval gate. Failure/unknown recovery commit cannot reclaim its record.
The store does not authenticate a CLI caller; only the authenticated operator
may invoke the separate maintenance mode. Scheduler/default review do not use it.

works[].followup_status is a computed three-field display view:
  {state: claimed|proposed|held, reason: null|allowlisted_code, attempt: 1|2}
No pending event yields null. It contains no provider text, token, lease,
response body or raw model reasoning, and never changes the stored binding.

