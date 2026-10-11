Work authority and shared durable stop — 10 October 2026
======================================================
This code creates no active customer policy or grant by import. WAT remains off.
A server-side trusted service must explicitly set live_enabled=True before this
store can install a grant or begin any provider action. Current user acceptance
of visible stop is a policy decision, not proof that dispatch/QA acceptance passed.

Public API
----------
store = WorkAuthorityStore(existing_operations_store)
queue = store.queue()
queue.bind_work(...), bind_task(...), enqueue_message(...), claim_next(...)
store.install_grant(trusted_scope, trusted_request, intake=TrustedIntakeEvidence(...))
  or issuer=current authenticated explicit human grant evidence
store.observe_work(work_id)
store.rebind_grant(scope, request, same_scope_followup_evidence, expected_grant_id=...)
store.stop_work(work_id, TrustedWorkStop(...), expected_work_generation=..., mode='takeover')
store.begin_authorized_write(claim, trusted_scope, current_request, action_key=...)

TrustedWorkStop must be constructed from an authenticated UI identity or a real
Hamle Gmail read. A dict/model output is rejected. UI actor must be Ahmet, Cansu
or Özge from the verified catalogue; the caller validates IAP/session subject and
CSRF/origin. Gmail stop additionally needs actual source sender/message ID/fingerprint,
work thread, Hamle actual To/CC and verified Hamle mailbox identity. A verified
customer requester for this brand/service who is in the current grant's exact
bound reply recipients may stop using trusted_scope + trusted_catalog. This
terminal revocation records the actual customer actor, not an agency approval.
If genuine stop mail arrives but scope/role verification cannot finish, call
hold_received_stop(work_id, typed_receipt, expected_work_generation=...). It
revokes and fences work with provider_stop_verification_pending provenance.
This safe hold is only for actual thread-bound provider mail, not raw JSON/UI.
The scheduled worker now calls v11_stop_intake BEFORE pilot/model routine work.
Real customer stop-mail/deployed scheduled acceptance is still a launch gate;
the API wiring itself is implemented and regression-tested.

Physical atomicity and generations
----------------------------------
The adapter stores queue documents in the EXISTING hamle_internal_v1 Operations
collection, with waq- prefix and namespace marker. Work grant documents use
work-authority- prefix. It does not open/migrate hamle_v11_work_queue or V7 work
records. Passing a separate QueueBackend is rejected because two independent
transactions cannot atomically revoke and fence work. Existing IAM applies.

Shared Operations control is the single global generation. Its review_open maps
to a PREPARATION queue_open only. It does not enable customer dispatch. Global
stopped/takeover or generation change blocks an old action even after reopening.
A direct QueueStore control/mode/action-start write is denied; use this store.
Queue scans are bounded namespace queries; they are discovery, not permission.
New Firestore compound query indexes may be necessary and need live validation.

stop_work reads work+grant in the same transaction and writes terminal revocation,
work mode, work_generation+1, work version+1 and lease=None atomically. Original
grant history is retained. The same authenticated stop receipt can be repeated
without incrementing generations again; unknown commit is read/reconciled before
continuing. Stopping also works after budget expiry, during global stop, and
before any grant existed. An in-flight action ID remains unknown/reconciliation
only. No operation can unsend a request already accepted by Gmail/Basecamp.

begin_authorized_write reads current control, grant and shared budget inside one
transaction, rechecks work/brief/scope/delegation/source/recipients, runs the full
central authorize_action (including QA), then starts the single-attempt action
ledger in that SAME transaction. A stop committed first wins. The caller still
owns current provider identity/readback, charge reservation, final network
attempt and receipt/unknown reconciliation. No provider API is called here.

Same-scope follow-up rebind
--------------------------
Pure rebind_work_authority_from_followup accepts only a typed current trusted
same_scope_followup intake. The actual new mail must retain authorized sender,
Hamle actual To/CC, scope, brief hash, reply-all recipients and work generations.
Only source message/fingerprint/arrival binding may change. The original issuance,
2-business-day expiry and budget deadline NEVER change. A reread cannot renew.
Each rebind preserves the previous grant in a separate history document and
fences any pending old-source lease. A source change is not a new human grant.
The grant records accepted_brand_policy_derivation provenance honestly. A
maximum of 32 same-scope rebindings is conservative; further messages hold for
renewal rather than growing an unbounded document. Superseded IDs cannot be used
against the current durable observation. Scope/brief/recipient changes require
new authority; no reactivation/renewal endpoint is implemented yet.

Operational boundaries
----------------------
Visible-stop acceptance does not detect an unseen CC removal. Received stop-mail
latency remains the scheduler interval; UI stop is immediate on durable commit.
Current v1.1 customer worker does not automatically use this adapter until root
integration/deployment acceptance. Request/scope must come from trusted server
adapters, never POSTed JSON. Typed evidence is a programming boundary, not a
cryptographic identity proof. Model output never becomes authentication.
Prepared WAT policy uses the real decision record timestamp prospectively; old
mail before acceptance requires explicit authority. Shared 12 October spending
deadline still caps every grant even if two business days would end later.

Offline verification
--------------------
../hamle-gmail-connector/.venv/bin/python -m unittest test_v11_work_authority_store test_v11_work_authority test_autonomy_policy test_scored_autonomy test_v11_scored_delivery test_v11_queue_delivery -q
189 tests passed: 28 new atomic-store integration tests plus existing policy,
quality and queue/delivery regressions. They cover stop/lease fences, unsafe
backend refusal, durable restart/unknown commit, expiry/budget, no source-age
reset, exact same-scope rebind, verified customer stop and uncertain-role safe hold,
central gates and no direct queue bypass.
