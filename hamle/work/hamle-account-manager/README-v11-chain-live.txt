Hamle v1.1 internal chain: live adapters (written offline 2026-10-11, NOT deployed)
=================================================================================

What it is
  worker.py --mode chain_tick (NON-default) runs one Istanbul slot:
  global control review_open -> slot claim -> generation/deadline fence -> received-stop
  sweep (v11_stop_intake) -> chain queue control queue_open -> budget gate -> v11_chain tick
  with real adapters:
    v11_chain_mail.py     Gmail reader (verified requesters, Hamle in To/CC, latest incoming)
                          + same-thread reply writer (exact To/CC, In-Reply-To/References,
                          one POST, Sent-copy raw readback, no resend on uncertain outcome)
    v11_chain_models.py   OpenAI gpt-6-astra (existing model.py profile) interpreter /
                          classifier / answerer, strict JSON schemas, one chance per source
                          digest, 2 USD hold per attempt in the shared ledger
    v11_chain_quality.py  draft technical rubric -> real bytes from Basecamp -> inspector
                          -> evaluate_quality -> release record -> DeliveryCoordinator
    v11_chain_live.py     assembly, authorizer, Firestore queue, slot runner, queue CLI
  Prompts: chain_*_prompt.txt (Turkish). Rubric: outputs/gamechangers-internal/
  gamechangers-internal-quality-rubric.json (weights_status=draft_not_accepted).

Decisions kept
  >=90 delivery candidate (still needs send authority, exact recipients/CC, fresh file
  re-download + hash, stop/control checks); 80-89 needs one of Ozge/Ahmet/Cansu
  (record_quality_approval store API; no UI yet); <80 internal revision, max 2 rounds,
  then human. No duplicate sends; any write whose outcome is uncertain is held, never
  retried. Only OpenAI via model.py.

The deployed chain HOLDS (safe) until all of these are true
  1. Catalog: real Todolist id in routes[0].list_id, verified=true, live_enabled=true,
     provider_mutation_allowed_by_this_file=true, agency_direct_request_counts_as_agreement
     .verified=true (Ahmet).
  2. Rubric accepted by Ahmet (status/weights_status=accepted, verified=true, accepted_by,
     accepted_at, evidence_ref, rights_declaration.accepted=true). Otherwise every
     submission is quality_held and managers are tagged.
  3. Send authority: v11_chain_authority.py derives a visible-stop work grant at intake
     (existing v11_work_authority, accepted-brand-policy derivation) from the prepared
     outputs/gamechangers-internal/gamechangers-internal-work-authority-policy.json.
     That file is PENDING Ahmet (live_enabled=false, acceptance=null): until he accepts
     it no grant is derived and every delivery is held (managers tagged). Expiry =
     earliest of 2 Istanbul weekdays from mail arrival, scope/rubric, acceptance, budget.
  4. Chain queue control opened by a named operator (below).

Known gaps
  - Stops: "queue stop" bumps the chain queue generation (all chain claims/writes and
    grant bindings fence); a received "dur/durdur/devral" mail on a chain thread is
    found by chain_stop_sweep (reuses HamleStopReader + stop_intent) before each tick,
    bumps that work's generation and revokes its grant. v11_stop_intake itself still
    sweeps only hamle_internal_v1 works.
  - scheduler_pilot.py is pinned to scheduled_review; arm with the gcloud steps in
    scheduler-plan-20261012-chain.json (status local_plan_only_not_applied).
  - Image OCR needs tesseract (in the Dockerfile); without OCR required text and brand
    identity are unassessed -> held.
  - Budget: internal-live cap 30 USD (catalog budget.internal_live_cap_microdollars) on
    chain model holds/actuals, inside the 50 USD shared ledger; 2 USD hold per attempt.
  - 2026-10-11: Ahmet accepted catalog/agreement rule, rubric and work-authority policy
    (rubric rights_declaration still false -> rights check unassessed -> held).

Mac operator commands (from work/hamle-account-manager)
  python3 -B -m unittest test_v11_chain test_v11_chain_models test_v11_chain_mail test_v11_chain_live
  python3 v11_chain_live.py queue status
  python3 v11_chain_live.py queue open --expected-generation N --actor ahmet@gamechangers.com.tr   # Firestore write
  python3 v11_chain_live.py queue stop --expected-generation N --actor ahmet@gamechangers.com.tr   # Firestore write
  python3 package_chain_source.py --create        # local files only
  (build/deploy/scheduler: see scheduler-plan-20261012-chain.json -> cloud_run / tooling)

Manual internal run (outside the weekday 08:30-18:30 slots; never scheduler/default args)
  gcloud run jobs execute hamle-account-manager --project=gamechangers-hamle --region=europe-west1 \
    --args=worker.py,--mode,chain_tick,--manual-internal,--operator,ahmet@gamechangers.com.tr --wait
  Needs run.jobs.runWithOverrides (e.g. roles/run.developer or roles/run.admin; run.invoker is not
  enough). Costs Cloud Run time + any model calls (30 USD chain cap). Internal profile/works only;
  unique manual-chain-* claim, one at a time (900 s lease); every other gate unchanged.
