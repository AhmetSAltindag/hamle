"""Local review/control demo. No provider client, dispatcher, or action authority."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

SUBJECT = "HAMLE-IC-PROVA-20261010-YENIMAC-01"
BRAND = "gamechangers_internal"
REVIEW_FRESHNESS_SECONDS = 300
_HEX = re.compile(r"[a-f0-9]{1,64}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_RESULT_KEYS = {
    "status", "reason", "test_subject", "review_item", "action_eligible",
    "mailbox_writes", "external_actions_enabled", "body_returned",
    "attachments_fetched", "token_refreshed", "read_scope", "unseen_cc_removal_resolved",
    "capture_observed_at",
}
_ITEM_KEYS = {
    "review_id", "brand", "thread_id", "message_id", "capture_fingerprint", "source",
    "control_generation", "human_renewed_delayed_review", "status", "action_eligible",
    "latest_scope", "unseen_cc_removal_resolved", "body_or_attachments_included",
}


class StoreError(ValueError):
    """Only static codes are emitted; invalid input is never echoed."""


def _stamp(value):
    try:
        valid = (type(value) in (int, float) and math.isfinite(value)
                 and 0 <= value <= 253402300799)
    except OverflowError:
        valid = False
    if not valid:
        raise StoreError("invalid_timestamp")
    return float(value)


def _validate_capture(result):
    if not isinstance(result, dict) or set(result) != _RESULT_KEYS:
        raise StoreError("invalid_capture_schema")
    if (result["status"] != "review_ready" or result["reason"] != "human_review_required"
            or result["test_subject"] != SUBJECT
            or type(result["mailbox_writes"]) is not int or result["mailbox_writes"] != 0
            or type(result["token_refreshed"]) is not bool
            or result["read_scope"] != "one_explicit_internal_test_thread_in_hamle_mailbox"
            or any(result[key] is not False for key in (
                "action_eligible", "external_actions_enabled", "body_returned",
                "attachments_fetched", "unseen_cc_removal_resolved"))):
        raise StoreError("capture_outside_review_scope")
    item = result["review_item"]
    if not isinstance(item, dict) or set(item) != _ITEM_KEYS:
        raise StoreError("invalid_review_schema")
    if (item["brand"] != BRAND or item["source"] != "captured_normalized"
            or item["status"] != "awaiting_human_review"
            or item["latest_scope"] != "latest_observed_in_captured_snapshot_only"
            or type(item["control_generation"]) is not int or item["control_generation"] != 0
            or any(item[key] is not False for key in (
                "action_eligible", "human_renewed_delayed_review",
                "unseen_cc_removal_resolved", "body_or_attachments_included"))):
        raise StoreError("review_outside_demo_scope")
    for key in ("thread_id", "message_id"):
        if not isinstance(item[key], str) or not _HEX.fullmatch(item[key]):
            raise StoreError("invalid_metadata_id")
    for key in ("review_id", "capture_fingerprint"):
        if not isinstance(item[key], str) or not _DIGEST.fullmatch(item[key]):
            raise StoreError("invalid_metadata_digest")
    expected = hashlib.sha256((item["thread_id"] + ":" + item["message_id"]).encode()).hexdigest()
    if item["review_id"] != expected:
        raise StoreError("review_id_mismatch")
    # This fresh object is the entire persisted mail-derived allowlist.
    return {key: item[key] for key in (
        "review_id", "thread_id", "message_id", "capture_fingerprint")}


class Store:
    """SQLite demo state; the trusted local caller must supply a real fresh read.

    begin_capture() must precede the actual read. import_capture() binds its
    sanitized result and completion timestamp to that ticket and generation.
    Neither a timestamp nor an actor string authenticates a human or Gmail.
    """
    def __init__(self, path, *, initialize_review_open=False, clock=time.time):
        if type(initialize_review_open) is not bool:
            raise StoreError("invalid_initialize_flag")
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink():
            raise StoreError("database_symlink_forbidden")
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if not self.path.is_file():
                raise StoreError("invalid_database_path") from None
        else:
            os.close(fd)
        os.chmod(self.path, 0o600)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS control (singleton INTEGER PRIMARY KEY CHECK(singleton=1), mode TEXT NOT NULL, generation INTEGER NOT NULL, changed_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS captures (ticket TEXT PRIMARY KEY, generation INTEGER NOT NULL, started_at REAL NOT NULL, result_digest TEXT, observed_at REAL, review_id TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS items (review_id TEXT NOT NULL, generation INTEGER NOT NULL, thread_id TEXT NOT NULL, message_id TEXT NOT NULL, capture_fingerprint TEXT NOT NULL, observed_at REAL NOT NULL, status TEXT NOT NULL, receipt_digest TEXT NOT NULL, PRIMARY KEY(review_id,generation))")
            db.execute("INSERT OR IGNORE INTO control VALUES (1,?,?,?)", (
                "review_open" if initialize_review_open else "stopped", 0, self._now(None)))

    def _now(self, now):
        return _stamp(self.clock() if now is None else now)

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _control(db):
        return dict(db.execute("SELECT mode,generation,changed_at FROM control WHERE singleton=1").fetchone())

    def begin_capture(self, *, now=None):
        now = self._now(now)
        with self._transaction() as db:
            control = self._control(db)
            if control["mode"] != "review_open":
                raise StoreError("human_control_stopped")
            if now < control["changed_at"]:
                raise StoreError("clock_before_control_event")
            ticket = uuid.uuid4().hex
            db.execute("INSERT INTO captures(ticket,generation,started_at) VALUES (?,?,?)",
                       (ticket, control["generation"], now))
        return ticket

    def import_capture(self, result, *, ticket, observed_at=None, now=None):
        item = _validate_capture(result)
        captured_at = _stamp(result["capture_observed_at"])
        observed_at = captured_at if observed_at is None else _stamp(observed_at)
        if observed_at != captured_at:
            raise StoreError("capture_timestamp_mismatch")
        now = self._now(now)
        if not isinstance(ticket, str) or not re.fullmatch(r"[a-f0-9]{32}", ticket):
            raise StoreError("invalid_capture_ticket")
        if observed_at > now or now - observed_at > REVIEW_FRESHNESS_SECONDS:
            raise StoreError("capture_not_fresh")
        receipt = hashlib.sha256(json.dumps(
            {**item, "ticket": ticket, "observed_at": observed_at},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._transaction() as db:
            control = self._control(db)
            capture = db.execute("SELECT * FROM captures WHERE ticket=?", (ticket,)).fetchone()
            if capture is None:
                raise StoreError("unknown_capture_ticket")
            if control["mode"] != "review_open":
                return self._result("blocked", "human_control_stopped")
            if capture["generation"] != control["generation"]:
                return self._result("blocked", "old_control_generation")
            if observed_at < capture["started_at"] or observed_at < control["changed_at"]:
                raise StoreError("capture_before_ticket_or_control")
            if capture["result_digest"] is not None:
                if capture["result_digest"] != receipt:
                    raise StoreError("capture_ticket_already_consumed")
                return self._result("duplicate", "same_capture", item["review_id"])
            previous = db.execute("SELECT * FROM items WHERE review_id=? ORDER BY generation DESC LIMIT 1", (item["review_id"],)).fetchone()
            if previous:
                if observed_at <= previous["observed_at"]:
                    raise StoreError("capture_not_newer_than_previous_read")
                if (previous["generation"] == control["generation"]
                        and previous["capture_fingerprint"] != item["capture_fingerprint"]):
                    raise StoreError("changed_snapshot_requires_new_review_generation")
            db.execute("UPDATE captures SET result_digest=?,observed_at=?,review_id=? WHERE ticket=?",
                       (receipt, observed_at, item["review_id"], ticket))
            if previous and previous["generation"] == control["generation"]:
                # A fresh read refreshes one display row; it never creates work.
                db.execute("UPDATE items SET observed_at=?,receipt_digest=? WHERE review_id=? AND generation=?",
                           (observed_at, receipt, item["review_id"], control["generation"]))
                outcome, reason = "duplicate", "same_review_item_fresh_read"
            else:
                db.execute("INSERT INTO items VALUES (?,?,?,?,?,?,?,?)", (
                    item["review_id"], control["generation"], item["thread_id"], item["message_id"],
                    item["capture_fingerprint"], observed_at, "awaiting_human_review", receipt))
                outcome, reason = ("reopened", "fresh_read_after_resume") if previous else ("created", "human_review_required")
        return self._result(outcome, reason, item["review_id"])

    @staticmethod
    def _result(outcome, reason, review_id=None):
        return {"outcome": outcome, "reason": reason, "review_id": review_id,
                "action_eligible": False, "external_actions_enabled": False}

    def _change_control(self, mode, now):
        now = self._now(now)
        with self._transaction() as db:
            control = self._control(db)
            if mode == "review_open" and now < control["changed_at"]:
                raise StoreError("clock_before_control_event")
            # A backward wall clock must never prevent a protective stop.
            changed_at = max(now, control["changed_at"])
            db.execute("UPDATE items SET status='cancelled' WHERE status='awaiting_human_review'")
            db.execute("UPDATE control SET mode=?,generation=generation+1,changed_at=? WHERE singleton=1", (mode, changed_at))
        return self.public_state(now=now)

    def stop(self, *, takeover=False, now=None):
        if type(takeover) is not bool:
            raise StoreError("invalid_control_event")
        return self._change_control("takeover" if takeover else "stopped", now)

    def takeover(self, *, now=None):
        return self.stop(takeover=True, now=now)

    def resume_review(self, *, now=None):
        """Reopen review intake only. Old rows/tickets remain cancelled/invalid."""
        return self._change_control("review_open", now)

    def public_state(self, *, now=None):
        now = self._now(now)
        with self._transaction() as db:
            control = self._control(db)
            rows = db.execute("SELECT i.* FROM items i WHERE generation=(SELECT MAX(generation) FROM items j WHERE j.review_id=i.review_id) ORDER BY observed_at DESC,review_id").fetchall()
        items = []
        for row in rows:
            item = {key: row[key] for key in (
                "review_id", "thread_id", "message_id", "capture_fingerprint",
                "observed_at", "generation", "status")}
            if item["status"] == "awaiting_human_review" and (
                    now < item["observed_at"] or now - item["observed_at"] > REVIEW_FRESHNESS_SECONDS):
                item["status"] = "held_stale"
            item.update(brand=BRAND, test_subject=SUBJECT, action_eligible=False)
            items.append(item)
        return {"control": control, "items": items, "test_subject": SUBJECT,
                "demo_only": True, "external_actions_enabled": False,
                "action_eligible": False, "unseen_cc_removal_resolved": False}


ReviewStore = Store
