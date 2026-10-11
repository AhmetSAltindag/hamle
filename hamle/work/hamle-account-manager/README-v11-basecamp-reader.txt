Hamle v1.1 — Basecamp read-only observation collector
====================================================

v11_basecamp_reader.py reads the bound Todo and its paginated active comments.
It is not imported by the live V7 worker and has not made a live provider call.
test_v11_basecamp_reader.py exercises synthetic provider replies only.

Provider contract, checked 2026-10-10:
https://github.com/basecamp/bc-api/blob/master/sections/todos.md
https://github.com/basecamp/bc-api/blob/master/sections/comments.md
https://github.com/basecamp/bc-api/blob/master/README.md#pagination

The reader follows the provider's exact Link next URL, validates account,
recording and route before sending credentials, and reuses ETag / Last-Modified
on later reads. Task and comment pages are cached separately. No redirects,
provider writes, model calls, automatic retries or credential discovery.

SessionGet must receive an explicitly authorized token callback and the existing
zero-retry cloud_providers._session() transport. It independently validates URL
and conditional header allowlists before retrieving a token. Credentials and
provider error bodies are never included in its errors. A 429 stops this sweep;
the eventual scheduler needs bounded backoff (not an immediate retry loop).

BasecampReader.poll(work, previous=committed_checkpoint) returns:
- A bounded complete current task/comment observation.
- New/edited/absent comment observations, plus changed task observations.
- A sequence and checkpoint bound to work/account/project/list/task.
- Explicit false flags for artifact bytes, release, QA and delivery authority.

"Müşteriye hazır" written inside a comment is data, not authenticated final
release. This reader does not download or execute attachment/HTML URLs. The
provider's completion flag also grants no authority. Text must remain untrusted
when passed to a later classifier, renderer or model. An absent comment means
absent from the active-comment list; it is not proof of permanent deletion.

Defaults: at most12 pages,1000 comments,1MiB per response,64 cached pages. Limits
fail closed; no partial successful checkpoint. Each sweep reads the task again
after comments and checks its fingerprint and comment count for drift. Basecamp
pagination is not an atomic snapshot: comment edits with no matching task change
can still race a sweep. Final dispatch must re-read current task, exact file
bytes, release and source mail through its independently verified adapter.

Checkpoint journal is required, not optional
------------------------------------------
Do not enqueue a partial poll, discard its output and then re-poll after a
crash using an old checkpoint. Basecamp may have changed meanwhile. Persist an
immutable pending batch before enqueueing any of its events. Resume that exact
batch after a crash, using stable source identities and queue deduplication.
Commit the next checkpoint only after every frozen event is accounted for.
v11_poll_journal.py implements the local transaction contract for this bridge.
Reader output alone does not mean a persistent cursor or live polling is wired.

Work/task bindings and allowed project membership must be independently checked
before calling the reader; this is not the brand-authorization resolver. Delivery
Coordinator needs additional trusted artifact, brief, QA, producer-release,
scope and current-participation evidence. The invisible CC-removal boundary
cannot be resolved by checking Basecamp or by a recent Gmail poll.

Run:
  ../hamle-gmail-connector/.venv/bin/python -m unittest test_v11_basecamp_reader -v
