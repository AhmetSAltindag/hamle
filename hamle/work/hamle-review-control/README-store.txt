LOCAL REVIEW / CONTROL DEMO

store.py persists a strictly allowlisted, sanitized capture from the one fixed
internal test subject HAMLE-IC-PROVA-20261010-YENIMAC-01. It stores IDs, a snapshot
fingerprint, receipt digest, read timestamps and control generations. Mail
bodies, addresses, attachments, credentials and arbitrary extra fields are not
accepted. This is local SQLite state, not a distributed cloud work store.

API
  store = Store(path)                         # initial default: stopped
  store = Store(path, initialize_review_open=True)  # first creation only
  ticket = store.begin_capture()              # before actual fresh Gmail read
  result = trusted_existing_capture_reader()  # caller; store has no Gmail API
  store.import_capture(result, ticket=ticket)
  store.public_state()
  store.stop()                               # cancels pending review rows
  store.takeover()                           # same cancellation, takeover mode
  store.resume_review()                      # review intake only; no old revival

ReviewStore is an alias of Store. All methods accept keyword now for deterministic
offline tests. The capture_observed_at timestamp comes from the capture result;
an optional observed_at argument must equal it. Timestamps and sanitized results are trusted
assertions from the local caller, not cryptographic evidence of a Gmail read.
Never load an old file and label it a fresh read. There is intentionally no
actor-string authentication. A loopback UI must enforce its own request access
controls; this demo does not prove the identity of the human at the keyboard.

Capture tickets bind a result fingerprint and completion timestamp to the
durable control generation. Stop, takeover and resume each increment generation.
Stop and takeover still cancel pending rows if the wall clock moves backward;
their persisted changed_at never decreases. Resume and capture retain clock
regression checks.
Old tickets cannot be imported after resume. Pending rows are cancelled; reopening
requires a new ticket surrounding a real read with a newer completion timestamp.
Repeating the same capture is idempotent. A later fresh read can refresh the
same one review row. Prior cancelled rows remain in storage for local audit.
The capture reader's separate in-memory control_generation is required to be 0;
it is never substituted for this store's durable generation.

The 300-second freshness limit controls this demo's display only. No review
state, resumption, timestamp or UI click permits a provider action. The system
cannot see a mail removing Hamle from CC if Hamle never receives that mail.
E04 and customer action authority remain unresolved. There is no dispatch,
Gmail/Basecamp write, cloud call, model call, recurring polling or send retry.

Verification: python -m unittest -v test_store (from this directory). Offline
tests exercise restart, idempotency, stale reads, generation fences, strict
schemas, and separate-process stop/import races.
