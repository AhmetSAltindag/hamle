Hamle v1.1 multi-work queue — OFFLINE FOUNDATION, NOT LIVE
========================================================

Files: v11_work_queue.py, test_v11_work_queue.py. Existing V7 runtime, cloud
collection, scheduler, secrets and provider adapters are not modified.

What exists
-----------
- Independent, stable work binding for a Hamle Gmail thread, brand and Basecamp
  account/project. Service/list cannot silently change. One task cannot belong
  to two works in the same Basecamp project.
- Durable per-source identity and fingerprint dedup; one event record per
  message/change, ordered sequence per work. Each source must be ingested
  oldest first. Source ordering is checked separately for Gmail and Basecamp.
  Basecamp event IDs must include account/project/task/change identity.
- Per-work transaction lease, generation and version fencing. Many works can
  interleave. One work can have only one active lease. An expired lease may be
  reclaimed only if no unresolved provider write exists.
- Per-work takeover/stop; global stop/takeover generation fences all old leases.
  A global/work resume never silently revives a stale claim.
- Before-provider-write record and minimal exact-readback receipt. Started or
  unknown outcomes are never automatically retried. An exact receipt may be
  reconciled after a stop, but does not resume the work. The provider may have
  accepted a request already in flight when a human presses stop.
- Bounded keyset pages (1–100 rows plus one lookahead), separate documents,
  no global array and no 50-work ceiling. Query is discovery, not authority.

Minimal API example (in-memory, no real accounts)
-----------------------------------------------
  from v11_work_queue import QueueStore, InMemoryBackend, digest
  q = QueueStore(InMemoryBackend())
  q.initialize()  # stopped, generation zero
  generation = q.set_control('queue_open', actor='offline-test',
                             expected_generation=0)['generation']
  work = q.bind_work(brand_id='gamechangers', thread_id='synthetic-1',
                    account_id=1, project_id=2, list_id=3, service_id='copy',
                    expected_generation=generation)
  q.enqueue_message(work['work_id'], message_id='synthetic-message-1',
                    fingerprint=digest('synthetic source'), source_order=1,
                    expected_generation=generation)
  claim = q.claim_next(work['work_id'], worker_id='offline-worker',
                      expected_generation=generation)
  q.complete_event(claim, outcome='no_action', evidence_ref='offline-check')

Delivery integration
--------------------
enqueue_event(... event_id, fingerprint, source_order, source_kind='basecamp')
joins Basecamp observations to the same work sequence. DeliveryCoordinator can
produce a stable immutable action intent. Pass its action ID as action_key to
begin_write(claim, action_kind='deliver_file', payload_sha256=..., action_key=...,
            prepared_binding=...). The required prepared_binding has exactly
work_id, brand_id, thread_id, account_id, project_id, list_id, service_id, task_id
from the bound work plus queue_event_fingerprint from the source queue event.
It is captured when the immutable intent is prepared. Changing the work or
source beneath an intent fails closed. Do not reconstruct it from another claim
after preparation to disguise mismatched authority evidence.
Immediately before begin_write, a trusted caller must authorize the exact
prepared payload with autonomy_policy, verify global/work generations, current
latest To/CC, brand/service/project/assignee, artifact/QA/release, and budget.
QueueStore never infers authorization from a model decision.

After one provider write, read back and canonically compare the exact payload.
confirm_write(action_id, claim_token, payload_sha256,
              receipt={'provider': 'gmail' or 'basecamp', 'remote_id': str,
                       'readback_sha256': exact_payload_sha256})
If output is unknown, hold_unknown(action_id). Do not retry, mint a new action
key to evade the hold, or mark the event complete. Exact readback reconciliation
is allowed; unconfirmed non-execution requires a separate human recovery design.

After an event is completed, claim_next exposes the next same-work event.
complete_event(... outcome='held_for_human', evidence_ref=...) advances the
source cursor but puts the entire work in takeover mode. Later events remain
stored but cannot run until explicit human resumption. 'superseded' lets a
trusted worker discard an older Gmail source after it reads the actual latest
mail; processing an old queued event never authorizes a stale-source write.

Persistent adapter and limits
-----------------------------
FirestoreQueueBackend requires opt_in='v11-queue-explicit', explicit credentials,
project gamechangers-hamle and database hamle-operations. It uses a NEW collection
hamle_v11_work_queue. It is not constructed/imported by the current runtime.
Its transact(callback) uses get/put contract compatible with cloud_store's
Firestore transaction adapter. All transaction reads precede writes; max one
application transaction attempt. A store_outcome_unknown must be reconciled
using stable record identity, never blindly retried.

Production Firestore query indexes/IAM, provider polling/history pagination,
cross-provider timestamp mapping, live ingestion cursors, crash reconciliation,
customer-approved scopes, human UI, model/budget calls and dispatch integration
are NOT implemented/enabled here. Page cursors use immutable document IDs. A
new work sorting before an active cursor appears on the next full sweep; query
filters may change mid-sweep, so dispatch must always transactionally claim it.

The generation here belongs only to this queue. A live adapter must couple it
to the existing global control or replace it in a separately reviewed migration;
opening this queue does not open the V7 control or authorize routine customers.
Unseen CC removal remains unsolved by polling; delayed actions need the explicit
fresh-participation rule from autonomy_policy. No provider credentials, remote
mail, test writes or cloud cost were used to build or verify this module.

Run offline tests from the app directory:
  ../hamle-gmail-connector/.venv/bin/python -m unittest test_v11_work_queue -v

Crash-safe Basecamp observation checkpoint bridge (also offline)
---------------------------------------------------------------
v11_poll_journal.py contains PollJournal(queue). It consumes complete reader
observations but never imports or calls a network reader. It stores only
binding, hashes, change metadata and checkpoints; no comment/mail text.

Required caller protocol, in this order:
1. journal.pending(work_id): if a batch exists, drain that exact stored batch.
   Do not replace it with a newer provider poll after a crash.
2. previous = journal.checkpoint(work_id); call the reader with previous.
3. journal.stage(observation,
       expected_checkpoint_digest=checkpoint_digest(previous),
       expected_generation=current_queue_generation)
   Atomically captures the entire immutable change batch and its next
   checkpoint BEFORE the first queue event. A different pending batch or stale
   base checkpoint fails closed. A lost stage reply is reconciled by stable ID.
4. journal.drain(work_id, expected_generation=..., max_events=25)
   Enqueues bounded events in sequence, verifies each exact durable queue event,
   and advances the batch offset. Crash after enqueue/before offset update
   repeats the identical identity/fingerprint and is deduplicated by the queue.
5. Only the final acknowledged event commits the next checkpoint. A new worker
   resumes this process using the persistent backend; it does not need the old
   in-memory reader response. After global stop, drain cannot enqueue more; an
   explicit new generation can resume the same read-only observation batch.

The journal caps a poll at 100 changes, 100 active comment hashes, 20 KB
checkpoint and 58 KB batch. The reader's independent default can be higher.
An initial task observation plus 100 comments has 101 changes and is held;
initial scopes with up to 99 comments fit. Large threads must be surfaced for
explicit handling; never truncate them, advance their checkpoint or call the
poll complete. A future chunked/blob snapshot design would remove this bounded
foundation limit without weakening crash recovery.

For a given work, ALL source_kind='basecamp' queue events must come through this
journal. A second direct Basecamp enqueue path has an independent counter and
can conflict with last_source_order; it is unsupported. Gmail uses its own
source ordering and can interleave normally. Neither journal nor reader opens
customer authority, runs delivery QA, sends a reply or performs a provider write.
The reader cannot provide an atomic Basecamp snapshot; all send facts still
require a fresh trusted preflight. Workers should scan bound active tasks for
new Basecamp observations even while their mail event queue is idle.

Additional tests:
  ../hamle-gmail-connector/.venv/bin/python -m unittest test_v11_poll_journal -v
