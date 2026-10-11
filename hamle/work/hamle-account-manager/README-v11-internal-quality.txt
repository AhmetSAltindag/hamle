Hamle — internal technical QA adapter
====================================

v11_internal_quality.py and test_v11_internal_quality.py are pure adapters.
No network, paid model, credential, cloud or provider-write calls are made.

This accepted rubric is LIMITED to the explicitly requested new internal proof:
  Subject: HAMLE-IC-URETIM-20261010-01
  Actual sender: ahmet@gamechangers.com.tr
  Hamle in actual To/CC; no external recipients
  Basecamp account 5898865 / private project 49184053 / parent 10384126975
  Hamle software producer ID 53358100, explicitly labelled internal_fixture
  main: 1080x1080 PNG -> story: 1080x1920 PNG
  Both final outputs show these exact visible strings:
    HAMLE İÇ DENEME
    12 EKİM 2026
    Bu bir teknik denemedir.

A technical score of 100 asserts that these fixed checks passed against actual
attachment bytes and pixel OCR. It is not artistic, professional design, WAT
brand approval, production by Berkay/Samet, or customer-send authority. OCR is
fallible; a missed literal requests revision, not a claim of proven absence.
No WAT/customer scope can call this adapter successfully. Normal production
mode and non-Hamle fixture submissions are rejected.

API
---
build_internal_brief(source, control_generation, now, expires) -> brief
  source is capture_internal_request(...exact internal subject) result or its
  evidence field; source capture must be within 60 seconds. If raw source_text
  accompanies evidence, its hash is checked. The calling worker additionally
  checks that the actual internal request specifies these fixed requirements;
  the adapter does not treat arbitrary mail as accepting a hard-coded brief.
  The brief includes stage output_ids and explicit fixture-only policy metadata.
  Stage IDs are main and story. Both files purpose=final. The source hash, exact
  thread/message, current control generation and explicit expiry are pinned.

output_spec(stage_id) -> strict v11_artifact_inspector output specification
  Requires image/png, bounded bytes, exact dimensions and tesseract OCR of all
  three critical text strings. Call inspect_artifact(...include_text=True) on
  bytes re-downloaded from the newly bound private Basecamp task/attachment.
  A positive parser/OCR result must not be synthesized from expected content.

build_assessment(state, stage_id, inspection, source, now, generation,
                 control={...}) -> {policy, assessment, binding, safety, result}
  state is submitted v11_production_stages internal_pilot state.
  source must be a fresh provider observation of the same original email.
  control is fresh trusted global control readback, e.g.
    {verified:true, generation:7, mode:'active', observed_at:ISO,
     evidence_ref:'firestore-control:7', write_state:'new'}
  Missing, stale, stopped or uncertain-write control does not become allowed.
  Changed source or control generation requires explicit resolution.

  The inspector's complete evidence hash, normalized output-spec hash, actual
  artifact/manifest/attachment identity, private task, brief and timestamps are
  checked. extracted_text must hash to the inspector's text_sha256. Numeric or
  provider-verified canonical SGID-hash attachment IDs remain source-bound.

  Existing evaluate_quality is called directly. Weights (technical only):
    format 10; dimensions 25; title 15; date 25; disclosure 15; exact text 10.
  Missing date is a critical revision and scores at most 75 (65 when exact text
  also differs). Unknown OCR produces unassessed checks and holds, never zero.
  Missing dimensions, extra detected text, wrong file or missing disclosure are
  mandatory failures. Actual byte structure/hash is not inferred from a URL.
  The required generic quality checks are explicitly interpreted only within
  this internal fixture: identity is the internal heading; rights/disclosure is
  declared internally generated content with its visible technical disclaimer;
  excluded internal notes is the fixed OCR token allowlist, not a broad content
  or legal review. No external asset rights or brand quality are inferred.

build_stage_assessment_event(state, stage_id, inspection, source, now,
                             generation, control={...}) -> assess_stage event
  Ready for advance_production; actor_id=Hamle and evidence records technical
  evaluation by software, never named human approval. Existing stage state and
  idempotency/revision fence apply. The returned policy thresholds stay 90/80;
  a stage acceptance is not a mail-send permission.

Persistence/dispatch duties remain with the cloud worker: fresh action authority,
private provider membership verification, start-before-write ledger, uncertain
write hold, exact readback, bounded revision, durable quality note before final
mail, final re-download/re-check, recipient preservation and one delivery.

Tests
-----
../hamle-gmail-connector/.venv/bin/python -m unittest test_v11_internal_quality -q
24 tests use real PNG byte structures and SYNTHETIC OCR adapter responses. They
verify translation/binding/threshold behaviour, not real OCR or cloud acceptance.
The deployment's separate real rendered-PNG + actual OCR proof is still required.
