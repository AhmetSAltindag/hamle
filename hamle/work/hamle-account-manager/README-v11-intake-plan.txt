V1.1 intake / brief planning — OFFLINE FOUNDATION

Files: v11_intake_plan.py and test_v11_intake_plan.py.
No network calls, credentials, model calls, persistence or live API writes.
Not imported by the current cloud worker. Passing tests is not customer acceptance.

plan_intake(source, requirements, catalog, now=...)
  Trusted mail adapter supplies actual latest sender/To/CC, Gmail message/thread
  IDs, fingerprint, authenticated observations and stop state. Hamle must really
  be in To/CC. Quoted headers, BCC, own mail and automated replies cannot enter.
  A verified sender must have request permission for that exact service.
  An agency relay names the original authorized requester and records the agency
  actor's attestation and source medium, e.g. WhatsApp. This is not direct access
  to WhatsApp, nor proof of an approval inferred from quoted customer words.

  Requirements are explicit classified facts. Missing/ambiguous static, motion,
  video, quantity, format, specification, video duration, deadline, audience,
  objective or approved assets produces a clarification intent. Monthly service
  volumes and general turnaround times never fill a particular task's brief.
  Complete facts need source-bound agreement evidence before task planning.
  agreement.requirements_sha256 = intake_requirements_digest(requirements)
  binds that evidence to these exact extracted facts, not just the message ID.
  Commercial, complaint/strategy and social planning keep their human gates.

  Catalog version + digest bind exact service/category routing. Provisional
  workbook data must not be passed as verified live authority. Route fields:
    service_id, category_key, category_name, list_id, verified, verification_ref,
    allowed_kinds, assignee_ids_by_kind.
  Basecamp account/project/list/member facts must be independently verified.
  basecamp_people is a dictionary keyed by the decimal person ID. Each row has
    basecamp_id, basecamp_verified, side='agency', project_member,
    agency_roles (array), production_kinds (array).
  Email is NOT required for producer assignment or producer questions. Mail
  contacts are separate; known agency email-to-person bindings authorize human
  amendments. Legacy email-backed assignees_by_kind remains supported offline.
  Internal subscribers require social_manager, principal and strategy_owner.
  Customer contacts are never added as Basecamp subscribers or project members.

  Mixed deliverables require a verified production sequence, e.g. graphic first
  then animation, with explicit stage IDs/kinds/depends_on and sequence_verified.
  Prefer production_sequences keyed by '+'.join(sorted(deliverable kinds)), e.g.
  'animation+graphic' or 'graphic+video'; each exact combination lists its stages.
  Extra kinds in another sequence are not silently pruned. Single-kind work has
  one production stage. Legacy production_stages remains supported offline.
  Optional required_any_kinds requires at least one of the listed output kinds.
  A Motion route may allow graphic+animation/video as one sequential work while
  required_any_kinds=['animation','video'] rejects standalone graphic work there.
  The WAT planning catalog treats non-social graphic+motion as one Motion-list
  work, derived from the confirmed Motion routing and graphic-before-motion rule.
  This is recorded as a planning assumption, not separate live scope acceptance.
  Initial task assigns only dependency-free stages. Stage advancement, separate
  production tasks and provider writes need future runtime integration. Unknown
  category mappings or unagreed sequences remain held, never guessed.

  Actual email reply-all is preserved. No requester mentioned in a WhatsApp relay
  is silently added to the recipient list. An agency-only email stays agency-only;
  this is not evidence of a usable customer delivery thread. Final sending still
  requires the current delivery policy, exact recipient scope and fresh authority.

amend_brief(work, change, catalog, now=...)
  Only authorized agency actors may provide structured amendments through a
  real current email or authenticated Basecamp event bound to this exact task.
  Every change has an expected brief version, stable ID, reason, trusted current
  risk classification and source evidence. Cross-thread/task, route/assignee
  changes and ambiguous new outputs are held for separate review.
  classification.proposal_sha256 = amendment_proposal_digest(change) binds the
  risk decision to the actual patch/source/reason, excluding observation time.
  A change increments brief version and archives/revokes previous QA, release
  and pending delivery evidence. Basecamp changes preserve the actual email
  envelope. A same-ID changed event is a conflict, not a silent duplicate.

record_producer_question(work, question, catalog)
  Accepts authenticated questions only from verified assigned Basecamp people
  on this exact task. No producer email is needed. Records unresolved question,
  holds delivery, archives prior release/QA/pending evidence and identifies the
  internal overseers. Does not re-post the existing question or email a customer.
  An authorized brief amendment may explicitly resolve recorded question IDs.

Runtime obligations still outstanding:
  Persist work/version + queue invalidation atomically. Acquire work lease and
  rerun fresh action authorization immediately before each API write. Preserve
  uncertain started writes for reconciliation; cancellation cannot undo an
  already accepted provider write. Use comment revision IDs for edited comments.
  Current snapshots cannot reveal unseen CC removal. Delayed actions need the
  agreed visible stop/participation mechanism. No live enable switch exists here.

Validation (synthetic only):
  ../hamle-gmail-connector/.venv/bin/python -m unittest test_v11_intake_plan -q
