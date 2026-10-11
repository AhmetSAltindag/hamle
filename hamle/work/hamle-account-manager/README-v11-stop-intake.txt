V11 visible stop intake — read-only mail, authority reduction only
==============================================================
v11_stop_intake.py is wired into worker.run_scheduled_review immediately after
its existing durable schedule-slot claim/global fence and before internal pilot
or model review. Old V7 internal capture and approved writer rules are unchanged.

Only active work in the bound Operations/V11 queue namespace is inspected.
HamleStopReader uses existing verified_gmail + full-thread normalizer: exact
Hamle account/scopes/profile, userId=me, explicit work thread ID, metadata first,
one permitted latest incoming plaintext body, then unchanged snapshot equality.
No mailbox search, another account, attachment fetch, Gmail write, Basecamp call,
model call, grant issuance or customer reply is present.

Current-authored text containing Turkish stop/takeover wording is conservative:
quoted lines and standard forwarded-header boundaries are excluded; ambiguous
wording may stop rather than risk continuing. This is no semantic expertise
claim. Existing agency identities use stop_work. With current WAT live catalog
installation closed, customer messages use hold_received_stop, recording honest
provider_stop_verification_pending provenance and atomically revoking/fencing.
The customer is never represented as Ahmet/Cansu/Özge or as a grant issuer.

Failure behavior is explicit: reader/auth/normalization/plaintext failure,
changed snapshot, unknown receipt, incomplete bounded discovery or more than one
unreviewed real incoming message holds the scheduled slot before any routine
work. Latest automated mail or latest Hamle-To/CC exclusion cannot conceal an
unreviewed real incoming message behind it. Multiple older unread bodies are not
silently decoded outside the existing latest-only body authorization; manual
reconciliation is needed. Discovery is at most four pages of 25 work records.
Already stopped work is skipped and cannot resume itself.

No provider integration proof is fabricated: the local 12 intake regressions
cover customer/agency stop, same durable fence, no raw/model proof, quote handling,
read failure, incomplete sweep, pending older mail, and callback ordering before
pilot/model work. 83 worker+store+intake tests pass. Real received-customer stop
and deployed scheduler acceptance remain required before WAT live enablement.
The adapter leaves customer_live_enabled=false and never creates a grant.
