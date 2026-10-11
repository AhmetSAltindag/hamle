Hamle: bounded customer-representative model component
Prepared 2026-10-10 (Europe/Istanbul)

Purpose
One Responses API call prepares a Turkish request assessment, brief, response
draft and Basecamp task draft for human review. It performs no email, Basecamp,
web browsing, scheduling or filesystem write and grants no outbound authority.
The supplied synthetic_request.json contains no customer data.

Caller interface
  build_request(input_dict, *, profile=None) -> bounded API request dictionary
  extract_response(response_dict, input_dict=None, *, profile=None) -> validated review result
  call_once(api_key, input_dict, *, transport=None, profile=None) -> validated review result
  validate_analysis(analysis, input_dict=None) -> copied, provider-neutral domain draft
  ModelError.code: fixed safe error code; never raw provider text
  ModelError.usage: validated usage when available, including incomplete results
  ModelError.retry_allowed: always false

The caller loads a confirmed credential securely and must durably reserve money
and mark the attempt started BEFORE call_once. A timeout/disconnect can still
incur charges. Do not retry automatically. Reconcile usage/cost in the shared
budget ledger; this component is not a provider billing cutoff or a durable
cross-process deduplication system. No CLI/live call is run by this module.

Accepted default configuration
gpt-6-astra; reasoning.effort=max; service_tier=default; store=false;
tools=[]; tool_choice=none; max_output_tokens=16384 (including reasoning).
One POST to https://api.openai.com/v1/responses. No redirects, no retries,
environment proxies disabled, TLS verification remains enabled, 10s connect
and 360s socket-read timeouts. This timeout is not an absolute API cancellation.
Input <=16000 UTF-8 bytes; entire JSON request <=32768 bytes; streamed response
<=262144 bytes. API errors and transport exceptions expose only static codes.
The API may use the full output allowance for reasoning and return incomplete;
incomplete or refused output must not be accepted as a finished draft.

Local profile preparation and bounded evaluation, not live hybrid routing
model_profiles.py has immutable allowlisted profiles. None defaults to the
accepted astra-max-v1 profile. sol-medium-candidate-v1 selects GPT-6.1 Sol,
medium effort, and an 8192-output-token candidate cap. It is disabled for live
production calls: call_once rejects it before creating a network session. Only pure
request/response preparation and explicit test transports can use it locally.
Unknown or altered profiles are rejected. No profile is chosen from mail text.
There is no fallback, implicit model substitution, provider switch or extra call.
The selected model and effort must match parsed provider metadata when present;
OpenAI does not always return effort metadata, so an absent field proves only
the effort requested, not a separately attested execution setting.

After the user's separate <=1 USD customer-free comparison authorization,
sol-medium-evaluation-v1 permits an explicit evaluation call with the same Sol
medium/8192 limits. It is marked evaluation_only=True. It does not change the
default or enable the production candidate; the existing worker and store still
reject Sol. The explicit caller must reserve/begin the evaluation budget and
record each single attempt before network entry. At most five fixture calls fit
the conservative $0.832 allowance at the recorded rates; stop on any error and
retain the full reservation until reconciliation. No API call is made by import
or by the fixture file. A paid call requires the separate evaluation runner.

Only the OpenAI Responses adapter exists. validate_analysis is the reusable
domain boundary for a future provider adapter, not proof of Claude compatibility.
Worker, cloud ledger, secret permissions and deployed selection still use the
accepted Astra path. New routing needs user selection, bounded quality and access
checks, ledger integration and deliberate activation. Draft validation never
grants action authority. Local profile code has not been deployed. The five
customer-free cases and human review rubric are in model-evaluation-cases.json;
passing them is preliminary evidence and does not grant customer autonomy.

Schema and domain checks
All output keys are required; extra keys rejected. No confirmed deadline,
assignee or task due date can be emitted (those fields must be null). Requested
deadline must remain identical to the provided source. Intent escalation routes
are enforced after generation. These checks do not prove semantic correctness;
the complete draft still needs human review and later task-specific acceptance.
The prompt separates eight brands and treats request content as source data.
No automatic authority is inferred from a model answer. E04 (unreceived CC
removal) is not solved here. Authenticity, To/CC, stops, budget, service scope,
deduplication and delayed-action authorization belong to the runtime.

Cost bound for a single attempt
Official model page on 2026-10-10 lists $10/1M input tokens, $50/1M output;
cache writes $12.50/1M. A conservative allowance of 33792 input tokens
(request UTF-8 byte cap plus 1024 framing allowance) and 16384 output tokens
is $1.2416 even charging every input token at the cache-write rate.
The current caller holds $2 in the existing shared budget before the attempt.
This is a bounded-attempt estimate, not an invoice or account-level hard cap.

Verification (no network / no credential access)
From this folder use the existing Gmail connector virtualenv Python:
  ../hamle-gmail-connector/.venv/bin/python -B -m unittest test_model test_model_profiles -v
Tests use fake transports only. No live model access is established by tests.

Official sources actually fetched
https://developers.openai.com/api/docs/models/gpt-6-astra
https://developers.openai.com/api/docs/guides/structured-outputs
https://developers.openai.com/api/reference/resources/responses/methods/create
(The general reference open failed; text.format contract was verified using
the fetched Structured Outputs guide.)
