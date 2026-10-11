"""Offline v1.1 frozen observation journal; no provider calls or live imports.

Stage a complete bounded Basecamp poll before enqueueing its first event. Drain
the persisted batch before polling again. Checkpoint commit happens only after
every event is durably present in QueueStore. A crash after enqueue but before
acknowledgement replays the identical event, which QueueStore deduplicates.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

from v11_work_queue import QueueError, require, _fingerprint, positive, text_id

MAX_CHANGES = 100
MAX_CHECKPOINT_BYTES = 20_000
MAX_BATCH_BYTES = 58_000
BINDING_FIELDS = ("work_id", "account_id", "project_id", "list_id", "task_id")


def checkpoint_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _json_size(value):
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
    except (ValueError, TypeError, RecursionError):
        raise QueueError("poll_record_invalid") from None


class PollJournal:
    def __init__(self, queue):
        self.queue = queue

    @staticmethod
    def _key(work_id):
        text_id(work_id)
        return "poll-journal-" + checkpoint_digest(work_id)

    def checkpoint(self, work_id):
        """Return the last fully drained checkpoint (None before the first poll)."""
        def op(tx, _):
            self.queue._work(tx, work_id)
            row = tx.get(self._key(work_id))
            return copy.deepcopy(row["checkpoint"]) if row else None
        return self.queue._run(op)

    def pending(self, work_id):
        """Recover this batch first; do not fetch a newer provider snapshot."""
        def op(tx, _):
            self.queue._work(tx, work_id)
            row = tx.get(self._key(work_id))
            return tx.get(row["pending_batch_id"]) if row and row["pending_batch_id"] else None
        return self.queue._run(op)

    @staticmethod
    def _validate_checkpoint(checkpoint, binding):
        require(isinstance(checkpoint, dict)
                and set(checkpoint) == {"binding", "task_sha256", "comments", "sequence"}
                and checkpoint["binding"] == binding, "poll_checkpoint_invalid")
        _fingerprint(checkpoint["task_sha256"])
        require(type(checkpoint["sequence"]) is int and checkpoint["sequence"] >= 0,
                "poll_checkpoint_invalid")
        require(isinstance(checkpoint["comments"], dict) and len(checkpoint["comments"]) <= MAX_CHANGES,
                "poll_checkpoint_limit")
        for key, value in checkpoint["comments"].items():
            require(isinstance(key, str) and re.fullmatch(r"[1-9][0-9]{0,15}", key), "poll_comment_identity_invalid")
            _fingerprint(value)
        require(_json_size(checkpoint) <= MAX_CHECKPOINT_BYTES, "poll_checkpoint_limit")

    @staticmethod
    def _validate_changes(changes, checkpoint, old_checkpoint, binding):
        require(isinstance(changes, list) and len(changes) <= MAX_CHANGES, "poll_change_limit")
        old = old_checkpoint or {"sequence": 0, "task_sha256": None, "comments": {}}
        expected = []
        if old["task_sha256"] != checkpoint["task_sha256"]:
            expected.append({"kind": "task_observed", "record_id": binding["task_id"],
                             "record_sha256": checkpoint["task_sha256"]})
        for key in sorted(set(old["comments"]) | set(checkpoint["comments"]), key=int):
            new_hash, old_hash = checkpoint["comments"].get(key), old["comments"].get(key)
            if new_hash != old_hash:
                expected.append({"kind": "comment_observed" if new_hash else "comment_absent",
                                 "record_id": int(key), "record_sha256": new_hash})
        require(len(expected) == len(changes)
                and checkpoint["sequence"] == old["sequence"] + len(changes), "poll_changes_checkpoint_mismatch")
        ids = set()
        for offset, (base, actual) in enumerate(zip(expected, changes), start=1):
            order = old["sequence"] + offset
            require(isinstance(actual, dict)
                    and set(actual) == set(base) | {"source_order", "event_id", "untrusted_content", "grants_authority"}
                    and all(actual[k] == v for k, v in base.items())
                    and type(actual["source_order"]) is int and actual["source_order"] == order
                    and actual["untrusted_content"] is True and actual["grants_authority"] is False,
                    "poll_change_invalid")
            # Reader event identity binds exact snapshot change, binding and
            # source order. The journal never accepts a caller's random ID.
            require(actual["event_id"] == "bc-" + checkpoint_digest([binding, order, base])
                    and actual["event_id"] not in ids, "poll_change_identity_invalid")
            ids.add(actual["event_id"])

    def stage(self, observation, *, expected_checkpoint_digest, expected_generation):
        """Atomically freeze small complete observations; never store raw text.

        Limits are 100 changes, 100 active-comment hashes, 20 KB checkpoint and
        58 KB total batch. Larger tasks fail closed with no checkpoint advance.
        The caller must surface this for review, never truncate or skip events.
        """
        _fingerprint(expected_checkpoint_digest)
        require(isinstance(observation, dict) and observation.get("complete") is True
                and observation.get("has_more") is False, "poll_incomplete")
        binding = observation.get("binding")
        require(isinstance(binding, dict) and set(binding) == set(BINDING_FIELDS), "poll_binding_invalid")
        work_id = text_id(binding.get("work_id"))
        for key in BINDING_FIELDS[1:]:
            positive(binding.get(key))
        checkpoint = observation.get("checkpoint")
        changes = observation.get("changes")
        self._validate_checkpoint(checkpoint, binding)
        require(isinstance(changes, list) and len(changes) <= MAX_CHANGES, "poll_change_limit")
        new_digest = checkpoint_digest(checkpoint)
        require(observation.get("fingerprint") == new_digest, "poll_fingerprint_mismatch")
        batch_id = "poll-batch-" + checkpoint_digest([binding, expected_checkpoint_digest, checkpoint, changes])
        journal_key = self._key(work_id)
        def op(tx, now):
            c = self.queue._control(tx); self.queue._open(c, expected_generation, now)
            work = self.queue._work(tx, work_id)
            require(all(work[k] == binding[k] for k in BINDING_FIELDS), "poll_work_binding_mismatch")
            journal = tx.get(journal_key)
            batch = tx.get(batch_id)
            if journal and journal["pending_batch_id"]:
                require(journal["pending_batch_id"] == batch_id, "pending_poll_must_drain")
                require(batch is not None and batch["state"] == "pending", "poll_batch_missing")
                return batch
            old_checkpoint = journal["checkpoint"] if journal else None
            # An ambiguous stage/drain reply can be reconciled by its stable ID
            # even after the checkpoint has already committed.
            if batch and batch["state"] == "committed":
                return batch
            require(checkpoint_digest(old_checkpoint) == expected_checkpoint_digest, "poll_checkpoint_stale")
            self._validate_changes(changes, checkpoint, old_checkpoint, binding)
            row = {"id": batch_id, "kind": "poll_batch", "work_id": work_id, "binding": copy.deepcopy(binding),
                   "base_checkpoint_digest": expected_checkpoint_digest, "checkpoint": copy.deepcopy(checkpoint),
                   "changes": copy.deepcopy(changes), "next_index": 0, "state": "pending" if changes else "committed",
                   "created_at": now, "committed_at": None if changes else now}
            require(_json_size(row) <= MAX_BATCH_BYTES, "poll_batch_limit")
            j = {"id": journal_key, "kind": "poll_journal", "work_id": work_id,
                 "checkpoint": copy.deepcopy(old_checkpoint) if changes else copy.deepcopy(checkpoint),
                 "pending_batch_id": batch_id if changes else None, "updated_at": now}
            tx.put(batch_id, row); tx.put(journal_key, j)
            return row
        return self.queue._run(op)

    def _ack(self, work_id, batch_id, index):
        """No provider write: recording progress is allowed even after a stop."""
        def op(tx, now):
            self.queue._work(tx, work_id)
            journal = tx.get(self._key(work_id))
            batch = tx.get(batch_id)
            require(journal is not None and batch is not None and batch["work_id"] == work_id,
                    "poll_batch_missing")
            if batch["state"] == "committed":
                return batch
            require(journal["pending_batch_id"] == batch_id, "poll_batch_changed")
            require(0 <= index <= batch["next_index"], "poll_ack_out_of_order")
            if index < batch["next_index"]:
                return batch
            # Independently confirm the exact source dedup record and queue
            # event exist before moving a durable checkpoint.
            change = batch["changes"][index]
            from v11_work_queue import digest
            source_key = "source-" + digest("basecamp", change["event_id"])
            source = tx.get(source_key)
            require(source is not None and source["work_id"] == work_id
                    and source["fingerprint"] == checkpoint_digest(change), "poll_event_not_durable")
            event = tx.get(source["queue_event_id"])
            require(event is not None and event["work_id"] == work_id
                    and event["source_id"] == change["event_id"]
                    and event["source_order"] == change["source_order"]
                    and event["fingerprint"] == checkpoint_digest(change), "poll_event_not_durable")
            batch["next_index"] += 1
            if batch["next_index"] == len(batch["changes"]):
                batch.update(state="committed", committed_at=now)
                journal.update(checkpoint=copy.deepcopy(batch["checkpoint"]), pending_batch_id=None, updated_at=now)
            tx.put(batch_id, batch); tx.put(journal["id"], journal)
            return batch
        return self.queue._run(op)

    def drain(self, work_id, *, expected_generation, max_events=25):
        require(type(max_events) is int and 1 <= max_events <= MAX_CHANGES, "poll_drain_limit_invalid")
        processed = 0
        batch = self.pending(work_id)
        if batch is None:
            return {"status": "idle", "events_accounted": 0, "checkpoint": self.checkpoint(work_id)}
        batch_id = batch["id"]
        while batch["state"] == "pending" and processed < max_events:
            index = batch["next_index"]
            change = batch["changes"][index]
            self.queue.enqueue_event(work_id, event_id=change["event_id"], source_kind="basecamp",
                fingerprint=checkpoint_digest(change), source_order=change["source_order"],
                expected_generation=expected_generation)
            batch = self._ack(work_id, batch_id, index)
            processed += 1
        return {"status": batch["state"], "batch_id": batch_id, "events_accounted": processed,
                "remaining": len(batch["changes"]) - batch["next_index"],
                "checkpoint": copy.deepcopy(batch["checkpoint"]) if batch["state"] == "committed" else None}
