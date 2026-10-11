Hamle v1.1 — bounded work authority with visible stop
====================================================

Status: pure core DEFAULT DISABLED; accepted WAT policy prepared, customer live
still OFF. The user explicitly accepted the visible-stop protocol; the real
record is outputs/wat-pilot-20261010/work-authority-user-decision-20261010.json.
Prospective service policies are in work-authority-prepared-policies-20261010.json,
with live_enabled=false. No real WAT policy/grant is installed in runtime.
The core module has no I/O. Its separate durable store passed a real Firestore
SYNTHETIC grant/lease/revoke test with zero customer/model operations. This does
not claim that Hamle can detect an email it never receives after CC removal.

Files: v11_work_authority.py, test_v11_work_authority.py, plus an opt-in branch
in autonomy_policy.py; durable adapter v11_work_authority_store.py. Existing
fresh_work_authorization remains a distinct
short-lived, exact-action human confirmation path.

Product contract represented by the enabled policy
-------------------------------------------------
A known requester starts a permitted routine work with Hamle in actual To/CC.
The accepted brand/service policy can derive up to TWO weekday-date days of
same-work authority. Visible stop/handover or an actual received stop prevents
later writes. Unseen CC removal cannot be detected; this protocol therefore
uses the explicit recorded product decision above. That agreement does not
prove deployment acceptance, lift risk/quality gates, or extend spending limits.

Two weekday dates means the same local clock time Europe/Istanbul, skipping
Saturday/Sunday: Friday 18:30 -> Tuesday 18:30. Weekend issue -> Tuesday at the
same time. This is NOT 48 staffed hours or a Turkish public-holiday calendar;
holidays have not been supplied. Dispatcher work-hour rules stay separate.
Expiry is always the earliest of the two-day maximum, accepted scope expiry,
accepted policy expiry, existing work delegation expiry, and shared budget
permission deadline. Expiry requires a new authorized decision; reading a grant
or mail again never extends it. Provider budget reservation remains separate.

Two honest provenance types
---------------------------
1. grant_work_authority(policy, scope, request, issuer, budget, now=...)
   A fresh explicitly authenticated Ahmet/Cansu/Özge decision for this exact
   binding. issuer_kind='authenticated_human_decision'. The human decision's
   issued_at is the time origin; replaying that same decision cannot add time.
   Requires issuer decision='grant_visible_stop_work_authority', evidence_ref,
   authenticated=true, verified=true, actor in accepted policy + agency catalog,
   binding_sha256, policy_digest and issued_at no more than 300 seconds old.

2. derive_work_authority_from_intake(policy, scope, request, intake, budget,
                                    now=...)
   One accepted brand/service policy derives authority for a new routine work.
   No per-job human click or new human approval is claimed. Provenance is
   issuer_kind='accepted_brand_policy_derivation'; actor/evidence point to the
   real parent policy acceptance, never a model or fabricated recent human act.
   Duration starts at original actual source.received_at. Mail before the parent
   policy acceptance cannot be activated by this path. Re-reading a late mail
   cannot restart its time window. Renewing an expired work is a separate act.

   intake must be an actual TrustedIntakeEvidence frozen Python record, built
   ONLY by the trusted authenticated Gmail/intake adapter after checks:
     adapter_id='hamle-authenticated-intake-v1'
     provider_identity='hamle@gamechangers.com.tr'
     evidence_ref, classification_evidence_ref
     binding_sha256=scope_digest(build_work_authority_binding(scope,request))
     parent_policy_digest=scope_digest(policy), observed_at
     intake_kind='new_work', source_kind='authenticated_gmail_api'
     classification='routine', model_supplied_authority=false.
   A raw model/mail/comment JSON dictionary with verified/authenticated flags
   is rejected. This type is a boundary against accidental model-dict ingestion,
   NOT cryptographic authentication. A trusted caller must actually establish
   provider identity/headers and independently verify the routine classification;
   a boolean or Python constructor cannot establish real-world truth by itself.

   request.delegation must honestly record its parent provenance too:
     basis='accepted_brand_policy_derivation'
     parent_policy_digest, parent_acceptance_ref, source_fingerprint
     actor equal to parent policy acceptance actor.
   The existing work/thread/scope/actions/generation/time checks still apply.
   No model directly mints a work delegation. Its durable trusted adapter is
   still required; this module never writes or invents that stored record.

Policy and bindings
-------------------
default_work_authority_policy(brand_id, service_id) returns enabled=false,
status=draft, actions=[], acceptance=None. validate_work_authority_policy rejects
it for use. Place an explicitly accepted policy on service.work_authority_policy.
Acceptance must be authenticated/verified by a catalogued Ahmet, Cansu or Özge,
carry the real evidence_ref, accepted_at/expires_at and decision:
  accept_visible_stop_work_authority
The exact prepared terms are pinned with:
  acceptance.policy_terms_sha256 = policy_terms_digest(policy)
This digest excludes only acceptance itself. Editing actions, duration or any
other term invalidates that acceptance; a verified flag alone does not approve
changed terms. Allowed actions must be a subset of the service and delegation.

Every grant is bound to work/brand/service/thread, actual source message ID and
fingerprint, original source.received_at, full scope digest, work_brief_sha256,
exact derived reply-all recipients, global_generation and work_generation.
Delivery quality_brief_sha256 must equal work_brief_sha256. Captured_at is NOT
used as original mail age. Policy/actor/action set/budget deadline remain immutable. Changed brief,
recipients, scope or generations hold the old grant. Controlled same-scope
follow-up rebind IS implemented: rebind_work_authority_from_followup requires a
current TrustedIntakeEvidence with intake_kind=same_scope_followup. It rechecks
actual sender, Hamle To/CC, exact scope/brief/recipient hashes and current
policy-backed delegation. Only source message/fingerprint/arrival may change.
Original issued_at, two-business-day expiry and budget cap NEVER change.
Every previous binding remains in immutable lineage (maximum 32 entries); the
durable store archives the old grant and invalidates the old-source lease.

Current observation and denial
-----------------------------
authorize_work_authority(policy,scope,request,grant,observation,budget,now=...)
returns allowed/reason only, never a send capability. observation comes from a
fresh trusted read of the CURRENT durable grant (at most 300 seconds):
  verified, evidence_ref, grant_id, grant_sha256=scope_digest(grant),
  grant_revision, observed_at.
It cannot substitute a cached active version for a revoked current record.

budget is fresh trusted shared-budget evidence:
  verified=true, allowed=true, overrun_hold=false, policy_id, evidence_ref,
  observed_at, expires_at, positive integer remaining_microdollars.
Budget exhaustion/unknown state/expiry or a different budget policy holds the
work. This is not a spending reservation or a billing export claim.

revoke_work_authority(grant,issuer,now=...,reason='human_takeover') produces a
terminal revoked copy, increments revision and retains the old record/history.
An authenticated revoker must be in the grant's accepted issuer set and the
current trusted authorized_catalog_issuers list, with exact grant_id and
revoke_visible_stop_work_authority decision. Repeated revoke is a no-op.
There is no un-revoke function. Runtime stop must ATOMICALLY persist revocation,
set the work mode/takeover and increase work generation through the shared
control/queue transaction; calling this pure function alone cannot stop workers.
Unknown or already-started provider actions remain reconciliation-only.

Central policy integration and non-bypass guarantees
---------------------------------------------------
autonomy_policy.validate_scope accepts no active work policy unless its exact
terms/acceptance validate. In authorize_action, when work_authority_grant is
present, this dedicated branch validates it regardless of source age. A bad or
revoked presented grant never falls back to the other fresh-human class.
Without a work grant, all old behavior remains: beyond the <=300-second immediate
window, fresh_work_authorization is still mandatory and exact-action-bound.

All the existing gates execute normally: enabled brand/service; routine risk;
no commercial/strategy/complaint/social-plan exception; revision limits; actual
latest incoming source sender and actual To/CC; exact reply-all; approved
recipients; source/control freshness; work delegation/global/work generations;
private correct Basecamp target/assignee; exact executable payload; release/QA;
score-note receipt; new action state. The new module's <=300-second observation
ceiling DOES NOT weaken central source/control <=60-second checks.

The grant does not approve all future content. Each generated response remains
bound to the current authorized brief, recipients and payload; every final file
still needs current byte/quality/release evidence and persisted score note. QA
90+ does not imply work authority. Scored 80–89 human approval stays separate.
A standing grant cannot override a stop, unknown write, budget deadline or bad QA.

Durable implementation and remaining runtime obligations
------------------------------------------------------
WorkAuthorityStore uses the existing Operations Firestore collection and one
shared global generation. install_grant, observe_work, rebind_grant, stop_work
and begin_authorized_write bind work/grant/lease/budget atomically. Independent
legacy queue backends are rejected; V7 work is not silently migrated. Every
service must independently be live_enabled=True before grants/action starts.
The current prepared WAT services remain False.

Authenticated Ahmet/Cansu/Özge UI and actual received agency stop mail revoke
and fence work. A verified customer requester in this exact brand/service AND
the bound work recipients can also stop via actual provider sender/message/hash,
thread and Hamle To/CC evidence. This decreases authority, never makes that
customer a grant issuer. If trusted mail ingestion cannot finish stop-role
verification, hold_received_stop records pending-verification provenance and
revokes/fences rather than allowing routine actions to continue silently.
Raw JSON, model flags and fake provider identity cannot invoke either path.

Remaining before live WAT:
- Deploy/verify the authenticated control UI per-work endpoints, real permitted
  IAP roles, origin/CSRF protection and customer-visible stop instructions.
- Scheduled stop ingestion is NOW connected in worker.run_scheduled_review:
  v11_stop_intake scans only existing active V11 work BEFORE pilot/model review.
  It uses the existing Hamle profile/scope-verified reader, exact latest incoming
  body and unchanged snapshot; agency stops revoke, customer stops use durable
  role-verification-pending hold while runtime customer catalogues remain off.
  Reader failure, more-than-bounded work discovery or unreviewed earlier mail
  blocks the slot. A later auto/ToCC-excluded mail cannot conceal an older stop.
  This adapter is tested locally; real customer stop-mail and deployed scheduled
  acceptance are still required before opening WAT.
- Install current accepted customer scope and delegation adapter, then integrate
  queue/production/delivery through begin_authorized_write. Current source and
  provider identity/readback remain adapter obligations. Own sent mail never
  becomes a new customer request; follow-ups use exact trusted rebind.
- Finish WAT QA/asset/producer/content acceptance and budget reservations before
  customer action. The accepted score thresholds do not replace work authority.

A received stop mail has scheduler latency (15 minutes currently); UI stop takes
effect at its durable commit. Already accepted upstream sends cannot be undone.
No renewal/un-revoke endpoint exists; expiry, scope/recipient changes and visible
stop require fresh authority. Scope/QA/expiry gates stay closed on unknown data.

Verification and cloud evidence
-------------------------------
../hamle-gmail-connector/.venv/bin/python -m unittest test_v11_work_authority_store test_v11_work_authority test_autonomy_policy test_scored_autonomy test_v11_scored_delivery test_v11_queue_delivery -q
189 tests passed: 45 pure work-authority tests, 28 atomic-store integration tests,
plus central policy/QA/queue regressions. These include same-scope derived rebind,
no source-age reset, customer and agency stops, uncertain-role safe hold, no model
grant/stop proof, deadline/budget caps and old lease rejection.

Real Firestore synthetic evidence (no real incoming customer mail was used):
outputs/wat-pilot-20261010/real-output-shadow/authority-cloud-probe.json
outputs/wat-pilot-20261010/real-output-shadow/authority-cloud-probe-revalidation.json
Synthetic work-7c3d3839c3f3ab85f28a610b08293a0aef774b4c5d0b9043b50b1fe043f68c37:
grant active -> revoked, work takeover/generation1, lease=None, old dispatch
refused; fresh Store read agrees. Global control/generation1 unchanged. Private
project49184053, fake example.test source; no Basecamp task/mail/model operation.
The grant expiry was capped at 12 October23:59:59 Istanbul by shared budget.
This proves durable state fencing, not real customer intake or live WAT service.
