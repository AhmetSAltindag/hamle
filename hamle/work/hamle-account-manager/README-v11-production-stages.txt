Hamle v1.1 — persistent multi-stage production state
===================================================

Files owned by this module:
  v11_production_stages.py
  test_v11_production_stages.py

No Gmail, Basecamp, database, model, cloud, billing or scheduler calls happen in
this module. It does not turn on a customer scope. The cloud adapter persists
and executes only independently authorized provider operations. Tests use
synthetic evidence and are not proof of real producer work or live acceptance.

API
---
initialize_production(work_id, brief, mode='production') -> state
  brief is the trusted, accepted intake brief, not a raw email/model response.
  Required fields: verified, evidence_ref, version (positive integer), brand_id,
  service_id, reply_envelope.thread_id, deliverables and binding.
  Each deliverable has id, kind ('graphic','animation','video','text'), purpose
  ('final','source','reference') and its concrete brief requirements.
  binding contains account_id, project_id, list_id, subscriber_ids (authorized
  internal overseers), production_stages. Each ordered stage contains id, kinds,
  depends_on and assignee_ids. Explicit output_ids may split two graphic outputs
  into sequential stages, e.g. main 1080x1080 -> story 1080x1920. If omitted,
  output_ids are derived by kind. Every output belongs to exactly one stage;
  forward/cyclic dependencies and duplicate output coverage are rejected.
  mode='internal_pilot' accepts internal_fixture producer submissions and stays
  labelled internal_pilot throughout; mode='production' rejects those fixtures.

ready_stages(state) -> list of stage specifications
  Only waiting stages whose dependencies have acceptance records are returned.
  Any open production question or human stop prevents all new stages. This is
  planning information, not permission to create/update a provider task.

advance_production(state, event) -> result
  result.state is a NEW JSON-serializable state; original input is untouched.
  result.status is applied, duplicate or held. Invalid input returns unchanged
  state and a stable reason. Persist the result atomically under the per-work
  transaction/lease. expected_revision fences stale plans. The module cannot
  replace the existing work queue, action ledger or uncertain-write handling.

  Common event fields:
    id, type, work_id, expected_revision, stage_id (except work-level changes),
    actor_id, verified=true, evidence_ref, occurred_at (timezone-aware ISO).
  Event IDs are stable provider/change identities. An exact repeated event is
  a no-op, including after later events. Reusing its ID with changed content
  fails closed. Store immutable event evidence outside state; state holds hashes
  and minimal receipts/history, never token material or raw file bytes.

  bind_stage_task:
    An authorized internal overseer binds an independently read-back Basecamp
    task. event.task includes provider='basecamp', verified, evidence_ref,
    account_id/project_id/list_id/task_id, customer_visible=false, assignee_ids
    equal to the stage plan, brief_sha256 equal to current state.brief_sha256.
    One task cannot be bound to two stages. Downstream tasks cannot be bound
    before dependency acceptance. Existing task IDs cannot be silently replaced.

  begin_stage:
    Overseer or assigned producer starts a bound, waiting stage. The task must
    contain/read back the current brief. Exact dependency acceptance hashes are
    captured; later submissions must repeat these hashes. Starting is a state
    record of the task being made ready, not a claim the producer has worked.

  submit_outputs:
    actor_id must be one of that stage's assigned producer IDs. event.submission:
      verified, evidence_ref, producer_id, task_id, origin='producer_submission'
      (or internal_fixture ONLY in internal_pilot), brief_sha256,
      dependency_acceptances (copy stage.dependency_acceptances), submitted_at,
      artifact={version, sha256}, outputs=[...], manifest_sha256.
    Each output has id, kind, purpose (all matching its exact brief requirement),
    sha256, size_bytes, mime_type, provider_attachment_id and inspection:
      {verified:true, bytes_read:true, readable:true, sha256, size_bytes,
       evidence_ref, inspected_at}.
    Inspections bind actual downloaded bytes and follow the submitted version.
    manifest_sha256 = autonomy_policy.scope_digest(outputs), including their
    exact inspection evidence. The artifact may be one real file or a separately
    verified transport package; its hash is NOT invented from metadata.
    URL-only metadata, an old reference assessment, source->final relabelling,
    someone else's upload and a wrong task/version/hash never count as production.
    A new submission after requested changes archives the previous one.

  assess_stage:
    Authorized overseer/trusted adapter passes policy, assessment, binding,
    safety and optional human_approval directly to existing evaluate_quality.
    There is NO second scoring system here. Binding must match this exact work,
    thread, brief, artifact and output manifest; assessment follows submission.
    Existing rules remain: >=90 candidate -> accepted stage; 80<=score<90 awaits
    one configured authorized person's exact approval; <80/critical failure ->
    changes_requested; missing/invalid evidence -> quality_held. The thresholds
    are the accepted policy's existing fixed thresholds, never model confidence.
    A stage acceptance binds submission, QA, brief and dependency acceptances.
    That acceptance advances production only; final customer delivery still
    requires full customer-facing artifact QA, score-note receipt, email/CC,
    fresh authority, takeover, budget, dispatch preflight and write ledger.
    Trusted adapters may be software, but their event actor must be explicitly
    represented in the authorized internal overseers. They must not impersonate
    a named human approval; that remains a separate exact human_approval record.

  raise_question:
    Assigned producer or overseer supplies question_id, question_ref and exact
    task_id. It opens a blocking question, archives and invalidates that stage's
    submission/acceptance plus every descendant's evidence; history is retained.
    No duplicated question is posted by the engine. All work waits for resolution.

  amend_brief:
    Authorized overseer supplies the next validated intake brief and optional
    resolves_question_ids. This MUST follow the normal intake/business-risk
    authorization; this module is not a replacement for it. Version increments
    by one; project/list/people/graph/output identities cannot silently change.
    A changed route or deliverable identity requires separate reviewed replan.
    All old stage evidence is archived, approvals invalidated, previous brief
    retained; tasks remain bound. Each task must receive/read back its updated
    brief before restarting. Resolving a question never reuses old acceptance.

  stop / resume:
    Explicit overseer event changes the work control. Stop prevents progression.
    Resume does not resend anything or revive old intents. Fresh action authority
    and existing queue generations still gate every actual provider operation.

 delivery_manifest(state) -> final-purpose outputs + exact stage hashes
    Allowed only after all stages accepted and no active stop/question. Source
    and reference files are excluded. It never packages files or authorizes a
    send. customer_send_allowed always stays false; requires_delivery_preflight
    stays true. Internal pilot provenance is preserved for downstream adapters.

Minimal cloud adapter sequence
------------------------------
1. Initialize from approved internal email and concrete byte-check requirements.
2. Persist state, select ready stage, then prepare/preflight/ledger ONE create-task
   write. After exact provider readback, record bind_stage_task and begin_stage.
3. Read the producer's actual submission; download/check bytes with the trusted
   byte inspector; record submit_outputs. A Hamle-produced internal fixture is
   labelled as such, not as Berkay or Samet having completed work.
4. Compute existing quality evidence from actual checks; assess_stage. If low,
   independently write/read back the bounded internal revision; new submission
   goes through checks again. Never count a revision note as a completed output.
5. Only after current stage accepted can ready_stages expose its dependent stage.
6. After final stage accepted, delivery_manifest excludes all source/reference
   attachments. Existing DeliveryCoordinator must still do complete final QA,
   durable score note, current recipient/authority and single-send write ledger.
7. Persist all events and state under the work transaction. After crash, reconcile
   started/unknown provider writes rather than minting another task/event ID.

Targeted offline verification (40 tests)
---------------------------------------
../hamle-gmail-connector/.venv/bin/python -m unittest test_v11_production_stages -q
Covers sequential acceptance, same-kind adaptation, exact replay/conflicting IDs,
state fences, private task/producer checks, real-byte bindings, thresholds and
critical failures, internal-fixture separation, questions, amendments, stop,
source exclusion and dependency invalidation with history retention.
