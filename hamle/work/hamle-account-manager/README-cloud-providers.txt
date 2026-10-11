Hamle internal cloud provider adapters — 2026-10-10

Status
Prepared locally and tested with fake transports. No real API/secret access or
cloud deployment was performed by this work. This is read-only intake and
credential plumbing, not a running independent agent or customer acceptance.

Stable interface
  GoogleSecretManager(credentials=None, session=None)
    access(name) -> secret bytes in memory
    add_gmail_version(record) -> confirmed new version resource name
    add_basecamp_version(record) -> confirmed independent Basecamp OAuth version
    close()
  GmailSecretStore(secrets).load() / .save(record)
  openai_key(secrets) -> key string in memory, never displayed
  verify_gmail_identity(store) -> live profile and exact scopes only, no mail read
  capture_internal_request(store, allow_body_for_internal_request=True)
    -> {source_text, evidence}; evidence has no body, attachment or credential
  verify_basecamp_internal(token) -> safe read verification summary
  ProviderError.code is a static code; retry_allowed is always false.

Fixed resources and minimum IAM
Project gamechangers-hamle / 286533609595 only. Runtime uses Application Default
Credentials through google-auth, normally the attached Cloud Run service account.
No downloadable service-account key is needed. Secret accessor may read only:
  hamle-gmail-oauth: original helper's validated OAuth JSON record
  hamle-openai-api-key: exact ASCII key bytes, without added newline
  hamle-basecamp-credential: independent cloud OAuth JSON from basecamp_cloud_auth
Both OAuth secrets need secretmanager.versions.add (Secret Version Adder),
because refresh is persisted as a new secret version before provider operations.
No general public secret write method exists. IAM policies must scope permissions
to these exact resources. Access and version writes use CRC32C validation.
Failed/uncertain version persistence stops the operation; no automatic retry.
This adapter does not delete historical secret versions. Gmail refresh needs a
single durable worker lease. Basecamp refresh additionally requires a permanent
atomic claim per refresh-token hash, before rotating the token. Never use copied
local CLI credentials or independently mounted stale token snapshots.

Gmail contract
The existing unchanged hamle_gmail.py validates exact readonly+send grants and
Hamle profile identity. Its verified_gmail context manager is reused, with a
Secret Manager store and a read-only HTTP transport. The existing unchanged
gmail_full_reader.py and mail_ingest.py normalize the thread and bound body
decoding. MacKeychainStore is never called by this adapter.

Only HAMLE-IC-IS-20261010-01 is searched, sender Ahmet and Hamle in real To/CC.
Search is discovery, not authority: exactly one result, exact Subject and real
headers are checked. Historical Gmail connection test 1a12270588d30c79 and
read-only review test 1a122c8fb08321fb are forbidden. Two full reads verify an
unchanged observed snapshot before exposing only the latest eligible message's
one plaintext body. A newer sent/self message, automatic response, different
sender, removed To/CC, ambiguous order, changed thread, attachment, HTML-only
body or >12000-byte plaintext body is held. No attachments are fetched.
The API still returns full thread payloads in memory; other bodies are withheld
from the returned result. Mail text is untrusted source data, never instruction
or provider access authority. Gmail reads cannot see an unreceived CC removal.

Source evidence includes an ISO UTC capture time and reply In-Reply-To/References
from actual message headers. Missing, ambiguous or unsafe RFC headers hold the
request; quoted/body text cannot supply them.
The caller must read the durable stop generation before capture and fence the
result after capture and again before model dispatch. Evidence action_eligible
is always false. This adapter supplies no mail send, Basecamp mutation, scheduler,
live customer authorization, send deduplication or human-identity control.
E04 remains unresolved; no customer automation should be inferred from this.

Basecamp contract
Only GET /5898865/my/profile.json, /5898865/projects/49184053.json,
/5898865/projects/49184053/people.json and
/5898865/buckets/49184053/todos/10391849167.json on https://3.basecampapi.com are used.
Hamle person 53358100/email, active project 'Hamle — İç Deneme', clients disabled,
and exactly Ahmet 47470759 plus Hamle 53358100 (both non-clients) are required.
The existing test task is only read. A fifth GET verifies its same-project private
parent Todoset or Todolist, returning todo_parent_id and todo_parent_type.
No task/list is recreated. Pagination is refused.
The separate basecamp_cloud_auth.py owns independent OAuth bootstrap/refresh;
verify_basecamp_internal itself performs no token refresh or provider write.

Independent Basecamp cloud bootstrap
  ../hamle-gmail-connector/.venv/bin/python -B basecamp_cloud_auth.py --begin-device-login
  ../hamle-gmail-connector/.venv/bin/python -B basecamp_cloud_auth.py --poll-device-once
The first command is an actual OAuth operation, not a check. Use only when the
user-authorized cloud login should begin. It pins live resource discovery to
https://app.basecamp.com, public client basecamp-cli and the previously approved
full scope. Device code stays in work/private-connections/basecamp-cloud-device.json
(0600); only the browser approval URL and safe metadata are displayed.
Each poll respects the issuance-anchored deadline and minimum polling interval.
Transport ambiguity blocks future automatic token polling. Explicit protocol
authorization_pending/slow_down responses can be polled later at the given time.

A received candidate is persisted privately as unverified before GET verification.
--recover-candidate resumes only identity GETs on a preserved unverified candidate;
it makes no OAuth token POST and refuses an expired candidate.
OAuth identity 28787068, account 5898865, Hamle person 53358100 and the private
project checks must all pass before work/private-connections/basecamp-cloud-oauth.json
(0600) becomes a usable local bootstrap artifact. That new cloud-only credential
is distinct from the Mac CLI login. Root transfers it to Secret Manager without
printing it; initial success does not itself prove that the cloud secret exists.

Runtime: basecamp_cloud_auth.access_token(secrets, claim_refresh=callback)
returns only an in-memory token. Unexpired ready records need no refresh claim.
Near expiry, callback(hash) must atomically and permanently claim the refresh
grant; false/missing prevents any token POST. Never release the claim after an
unknown result. The secret advances started -> new unverified -> ready. A failed
verification preserves the new rotating token but cannot use it automatically;
manual review/reconciliation is required. A repeated/stale grant cannot rotate
again. No automatic source fallback, Launchpad flow, credential copy or generic
CLI token export exists in this helper.

Transport and packaging
Requests has retries=0, trust_env=false, redirects=false, 10s connect/30s read.
Google auth token requests are endpoint restricted and repeated requests blocked.
Credential/provider operations silence diagnostic stdout/stderr/logging through
the original reader's serialized quiet context. This component is intended for
a single-task worker, not a multithreaded process with unrelated output.
All errors are safe static codes. Importing this module makes no network call.

Preserve these sibling source locations in the deployment package:
  hamle-account-manager/cloud_providers.py
  hamle-account-manager/basecamp_cloud_auth.py
  hamle-gmail-connector/hamle_gmail.py
  hamle-workflow/gmail_full_reader.py and mail_ingest.py
  hamle-internal-intake/intake.py
Dependencies reuse the Gmail connector environment (requests, google-auth,
google-api-python-client, httplib2). Existing source files were not modified.

Tests
  ../hamle-gmail-connector/.venv/bin/python -B -m unittest test_cloud_providers -v
  ../hamle-gmail-connector/.venv/bin/python -B -m unittest test_basecamp_cloud_auth -v
Fake credentials/transports only; sockets blocked. Covers resource/path scope,
CRC32C, wrong account/subject/To-CC, latest self/auto mail, changed snapshots,
body/attachment limits, refresh persistence, retry/redirect denial and Basecamp
private membership gates. A passing suite is not cloud provider acceptance.

Official sources fetched
https://docs.cloud.google.com/secret-manager/docs/reference/rest/v1/projects.secrets.versions/access
https://docs.cloud.google.com/secret-manager/docs/reference/rest/v1/projects.secrets/addVersion
https://docs.cloud.google.com/secret-manager/docs/reference/rest/v1/SecretPayload
https://docs.cloud.google.com/secret-manager/docs/reference/rest/v1/projects.secrets.versions
https://raw.githubusercontent.com/basecamp/bc-api/master/sections/people.md
https://raw.githubusercontent.com/basecamp/bc-api/master/sections/projects.md
https://raw.githubusercontent.com/basecamp/bc-api/master/sections/todos.md
https://raw.githubusercontent.com/basecamp/basecamp-cli/v0.13.0/internal/auth/auth.go
https://raw.githubusercontent.com/basecamp/basecamp-cli/v0.13.0/internal/auth/keyring.go
https://raw.githubusercontent.com/basecamp/basecamp-sdk/go/v0.24.0/go/pkg/basecamp/oauth/device.go
https://raw.githubusercontent.com/basecamp/basecamp-sdk/go/v0.24.0/go/pkg/basecamp/oauth/exchange.go
Public discovery GETs were also observed live (no authorization was initiated):
https://3.basecampapi.com/.well-known/oauth-protected-resource
https://app.basecamp.com/.well-known/oauth-authorization-server

Prompt change after first successful model proof
account_manager_prompt.txt now asks reply_draft to use natural representative
language instead of internal review/permission jargon. The earlier successful
live proof is preserved and does not establish a live result for this revision.

Bound internal follow-ups
capture_internal_request(..., continuation={thread_id, root_message_id,
root_rfc_message_id}) accepts a continuation only on fixed thread
1a122eec4ccdb53a. The store supplies the immutable binding from the original
verified proposal. Actual root message/header identity and original internal
subject must match. A new Re: message must include that root in References and
point In-Reply-To to an actual earlier message in the same thread. Missing or
quoted-body references cannot supply that relationship. Actual latest From must
remain Ahmet; Hamle must be in that latest To or CC. Own mail, automated mail,
ambiguous dates and unreceived CC removal retain their previous restrictions.

Evidence adds source_subject, is_continuation, root_message_id, root_rfc_message_id
and latest_to/latest_cc from only the latest actual normalized headers. These
source recipients are evidence, not an outbound recipient grant. The worker and
approval/writer enforce the final recipient policy separately.

The latest-sent-receipt no-work verifier accepts the same authorized internal CC
policy (currently Cansu), so a known sent reply with CC is not misclassified as a
new request. It still requires the exact ledger receipt ID, actual latest message,
Hamle identity, Ahmet recipient, expected subject, SENT label and no BCC/auto reply.
recipient_policy.py is now a required packaged sibling dependency.
