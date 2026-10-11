HAMLE — CLOUD INTERNAL REQUEST TO REVIEW PROPOSAL

worker.py implements the first real cloud application step: one explicitly named
internal email, one fixed Astra/max analysis, one durable proposal for Ahmet.
Review mode performs no business writes. Explicit dispatch mode is wired to the
separately reviewed BusinessWriters adapters and dispatch boundary. It requires
an exact authenticated UI approval plus fresh provider/control checks. Wiring
and offline tests do not prove live business write/readback acceptance.

Commands (inside the application image):
  python /app/work/hamle-account-manager/worker.py --mode review
  python /app/work/hamle-account-manager/worker.py --mode scheduled_review
  python /app/work/hamle-account-manager/worker.py --mode verify
  python /app/work/hamle-account-manager/worker.py --mode track
  python /app/work/hamle-account-manager/worker.py --mode recover_followup --work-id w-<64-hex> --failure-evidence-sha256 <64-hex> --failed-task-fingerprint <64-hex>
  python /app/work/hamle-account-manager/worker.py --mode dispatch --proposal-id p-<64-hex>
Required environment:
  HAMLE_ALLOW_FIRESTORE_OPERATIONS=yes
  HAMLE_PROJECT_ID=gamechangers-hamle
  HAMLE_FIRESTORE_DATABASE=hamle-operations
Never put credentials in environment, argv, image, log or this file.

Review mode reads Firestore control FIRST. Stopped/takeover -> zero provider
requests, secret reads or model calls. It does not initialize/reopen control.
Explicit verify mode is a separate read-only bootstrap probe: Gmail profile
and exact scopes, then Basecamp Hamle identity/private internal project/member
and existing todo parent-list checks. No model call or mailbox body in verify.
Both modes enforce the authorization date. Verify does not arm automation.
Dispatch is never a default or a review side effect. A missing/malformed proposal
ID fails before cloud clients. Dispatch creates the approved task and reads it
back before the approved reply. Existing receipts permit reads only, no resend.

Dispatch approval has an explicit purpose independent of model classification.
routine_execution accepts routine_request/owner=none. internal_coordination_only
accepts complaint_strategy/owner=cansu_and_ahmet only for the exact internal
Ahmet reply and Hamle task. Bound follow-ups inherit an open Cansu/Ahmet gate
even if the next model classification is routine/social/unclear. Commercial
decisions remain blocked. Actual latest-message CC now preserves the verified
Cansu address through recipient_policy; a new unverified recipient is held.
Its authenticated human approval keeps escalation
open and decision_authorized/production_authorized false. Cansu notification
and Özge consultation are not inferred merely from approval. Due-date promises,
commercial or customer decisions are not enabled. No model output or original
proposal content is changed; no extra model invocation is caused by this path.
Preparing or deploying it is not approval of the live draft. Existing approval
cannot change purpose or refresh its original five-minute validity.

scheduled_review is a separate, non-default preparation path. It claims an
Istanbul weekday slot once (08:30 through 18:30; first five minutes only), checks
deadline/control/budget, and never dispatches. An exact persisted Gmail receipt
plus live proof that it is the latest own reply can yield no_new_work; an
unrecognized own message cannot. The user-selected default is now 15 minutes:
41 weekday slots. The injected helper optionally accepts interval_minutes=30
(21 slots); both share the same calendar slot keys so changing cadence cannot
replay an already claimed time. This does not create or activate a scheduler.

Only subject HAMLE-IC-IS-20261010-01 is eligible. capture_internal_request reads
Hamle's own mailbox, exact Ahmet sender and actual Hamle To/CC, non-self/non-auto
latest incoming, bounded plain text, no attachments, real RFC Message-ID and
References. Existing historical technical test messages remain forbidden.
Raw source text stays in memory. The model receives it as untrusted request
content. No requested/confirmed due date is invented or auto-parsed; current
model input requested_deadline=null and final task due_at=null.

Provider interface (cloud_providers.py):
  secrets = GoogleSecretManager()
  gmail = GmailSecretStore(secrets)
  capture_internal_request(gmail, allow_body_for_internal_request=True)
  verify_gmail_identity(gmail)
  verify_basecamp_internal(token)
  openai_key(secrets)
Dedicated cloud Basecamp refresh is provided by basecamp_cloud_auth.access_token
(secrets, claim_refresh=store.claim_credential_refresh); no local Mac token clone
and no reinterpretation of an OAuth JSON
record as a static PAT. Auth adapter and provider helpers belong to separate
modules and must be present in the final reviewed image.

Proposal and model-budget identity hashes ONLY thread ID and immutable latest
message ID. A changed snapshot for that same message cannot create a second job
or another model chance. Authority freshness separately hashes thread/message
IDs, metadata fingerprint and body SHA256, excluding capture timestamp. A changed
snapshot is held; an unchanged one returns the existing proposal.

Same-thread continuation is now wired. An explicit one-time store migration
bind_verified_work(seed_proposal_id) derives the root mail, task and sent reply
only from the initial approved payload and both verified receipts. It does not
alter the initial proposal or create a new task. When this binding exists,
review reads and persists the current task status, then captures only the bound
thread/root and a valid RFC reply chain. The source must still be the actual
latest Ahmet message with Hamle in To/CC. Normal review as well as scheduled
review can return no_new_work only after proving the recorded sent reply is
still latest; task status is still updated. Polling alone calls no model.

A continuation has one permanent pending-event claim before model work. Only
one unresolved event is allowed per work. A failed/unknown analysis, uncertain
commit, incomplete comment or pending reply does not release that claim. The
original approved analysis, previous approved response, current task snapshot
and latest mail are labeled source data inside bounded request_text, never
trusted instructions. Oversized context is held without truncation. The model
schema and fixed Astra/max selection are unchanged.

The follow-up envelope replaces task with:
  work: work_id, thread_id, task_id, account_id, project_id, version,
        escalation_status, escalation_owner
  comment: account_id, project_id, task_id, todo_parent_id, todo_parent_type,
           body, task_fingerprint
reply and source keep their existing fields; actual latest CC is retained by
the verified recipient policy. Exact human approval covers comment and reply.
Dispatch adds the comment to the existing task, verifies it, then replies and
verifies the mail. Only both readbacks complete the event and increment the
separate revision_count. Binding version stays 1; reconciliation of old receipts
cannot be broken by our own progress. Unknown sends/comments are never retried.
New escalation on a work previously marked not_required is held pending an
explicit escalation migration; it cannot silently become routine next time.

track mode reads the single bound task, persists its observation, and optionally
checks the latest known sent receipt. Its entry point has no model/writer
callback. It is stopped by the same control/deadline. Task completion is an
observed Basecamp state; it does not close the separate strategic decision.
Before paid work, live Basecamp target identity/membership/private visibility and list
relationship must be verified. The source actual body hash is independently
checked and capture age must be <=300 seconds. RFC headers cannot carry CR/LF.

Each new immutable request gets model-<request-identity-digest> reservation, 2 USD
for the fixed Astra/max 16,384 output-token cap. Existing 1 USD holds are retained.
budget_begin transaction gives at most one invocation the model chance.
Unknown response, process crash, another worker or result persistence failure
does not reopen it. Costs remain fully held even after a valid result, because
token counts are not provider billing reconciliation. OpenAI prepaid overlap
is handled by the common cloud store; no separate 50 USD reset is created.

Control generation is checked before/after provider reads, key load and model.
Proposal persistence rechecks it transactionally. A takeover during inference
can consume the bounded ongoing call but prevents its stale result becoming an
active proposal. The worker cannot recall an already-started provider request.

Exact immutable payload presented to Ahmet:
  scope: gamechangers_internal
  analysis: validated model analysis
  reply: from, to=[ahmet@gamechangers.com.tr], cc=verified current CC, subject, body,
         thread_id, in_reply_to, references
  task: account_id=5898865, project_id=49184053,
        verified todo_parent_id and todo_parent_type (Todoset or Todolist),
        title, description, assignee_id=53358100, due_at=null
  source: thread_id, message_id, snapshot_fingerprint, captured_at (ISO timezone)
The full payload is hashed. Source raw text, API key and raw provider response
are not persisted by worker.py. Analysis can of course quote relevant source
facts as part of the human-visible proposal. Actual reply subject is fixed as
Re: HAMLE-IC-IS-20261010-01 to retain the Gmail thread; model draft subject stays
separately in analysis. No customer scope or E04 resolution
is inferred from this internal proposal.

stdout contains only fixed status/reason, proposal IDs/hash, model choice,
reservation ID/amount, or explicit dispatch receipts/new-write count. Exception text and raw
provider errors are suppressed. Failed bootstrap is held. No automatic retry
is configured here; Cloud Run Job must also have max-retries=0.

Offline checks:
  ../hamle-gmail-connector/.venv/bin/python -B -m unittest test_cloud_store test_worker
Continuation and recovery implementation uses injected
providers and an in-memory transaction fake, with no live API/model/cloud call.
Deployment, real cloud connections and independent-Mac acceptance have separate
root evidence records; these offline tests are not production acceptance evidence.
Final accounting/dispatch integration
-------------------------------------
A completed model result first creates an immutable, allowlisted model receipt
in Firestore, before the control-generation check and proposal publication.
The receipt contains only response ID, fixed model/reasoning choice and token
usage. It survives a concurrent stop; the full attempt hold remains until billing
reconciliation. Worker output exposes this safe accounting metadata, never raw
mail, draft bodies, secret keys or reasoning text. Known static provider/model/
OAuth error codes are preserved; unrecognized exception text never escapes.
Explicit dispatch uses the writer's mandatory final before_post callback; the
callback rechecks source, target, authority and lease after slower preflight.
Current store/worker/dispatch/UI/provider/writer/auth/model integration passed
251 offline tests.
Historical V3 evidence is unchanged; these local changes make no model call,
deploy no image, mutate no cloud ledger and do not activate Cloud Scheduler.

EXPLICIT TERMINAL-FAILURE RECOVERY
recover_followup is an operator-only maintenance mode; normal review and
scheduled_review never call it. It accepts one terminal response_incomplete
event, the exact bound work, unchanged source/task fingerprints, same control
generation, no proposal and no business action record. The failed execution's
SHA256 evidence and original task fingerprint are mandatory CLI arguments.
The maintenance actor is codex-authorized-maintenance; content_approval=false.
This audit is not a claim that Ahmet approved a business response.

The original pending event, canonical proposal ID, failure record and cost hold
are preserved. A permanent recovery-<event>-2 claim permits only attempt 2 and
reserves a new model-<request>-attempt2 hold of 2 USD before model work. A third
attempt, uncertain network result or missing/changed context is held. Even a
failed second attempt cannot be retried. Success publishes the same canonical
proposal, still awaiting exact human content approval; it creates no task/mail.

Every started failed model attempt now persists a separate allowlisted failure
receipt (response ID, fixed model/reasoning, status, known incomplete reason and
integer usage; unknown fields remain null). Partial output/reasoning/text and
secrets are never included. Historical v6 only logged response_incomplete; its
provider response ID/cause/usage are explicitly absent, marked as a legacy
observability gap bound to operator-supplied execution evidence, never inferred.
public_state work.followup_status exposes only state claimed/proposed/held,
allowlisted reason, and attempt 1/2. It is a computed view, not a binding mutation.

