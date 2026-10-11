"""Offline, synthetic action ledger. This module cannot call a live provider.

``accepted`` means a simulated provider accepted an action, never delivery.
Mail, mail headers, ingestion sequence numbers and authority decisions are not
interpreted here. A production adapter would need independent, fresh authority
checks and a provider-aware reconciliation design; this is not that adapter.

SQLite serializes claims across local processes. This is local persistence, not
a deployed cloud database or a claim of exactly-once delivery. An action whose
simulated dispatch started becomes ``unknown`` after a crash/timeout and is
never automatically retried. Pending/leased work may be recovered after expiry.
Times are trusted caller-supplied Unix seconds (or time.time), not mail dates.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid


class LedgerError(ValueError):
    """Fixed error codes; payloads and credentials are never echoed."""


class PayloadConflict(LedgerError):
    pass


class JobStopped(LedgerError):
    pass


class ActionUnavailable(LedgerError):
    pass


class JobBusy(LedgerError):
    pass


class StaleLease(LedgerError):
    pass


def _label(value):
    if (type(value) is not str or not value.strip() or len(value) > 256
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise LedgerError("invalid_identifier")
    return value


def _timestamp(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise LedgerError("invalid_timestamp")
    return float(value)


def _payload_hash(payload):
    def validate(value, depth=0):
        if depth > 64:
            raise LedgerError("invalid_json_payload")
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is float and math.isfinite(value):
            return
        if type(value) is list:
            for item in value:
                validate(item, depth + 1)
            return
        if type(value) is dict and all(type(key) is str for key in value):
            for item in value.values():
                validate(item, depth + 1)
            return
        raise LedgerError("invalid_json_payload")

    validate(payload)
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise LedgerError("invalid_json_payload") from None
    if len(canonical) > 1_048_576:
        raise LedgerError("payload_too_large")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Scope:
    brand: str
    thread: str
    job: str
    version: int

    def __post_init__(self):
        for value in (self.brand, self.thread, self.job):
            _label(value)
        if type(self.version) is not int or not 1 <= self.version <= 2**63 - 1:
            raise LedgerError("invalid_version")

    @property
    def job_key(self):
        return (self.brand, self.thread, self.job)


@dataclass(frozen=True)
class Lease:
    action_id: str
    token: str
    generation: int
    worker: str
    expires_at: float


@dataclass(frozen=True)
class Action:
    action_id: str
    scope: Scope
    idempotency_key: str
    payload_hash: str
    state: str
    generation: int | None
    simulated_receipt: str | None


class SyntheticLedger:
    """Own one connection per process/thread; close it after use.

    Identifiers must be application-controlled. Payloads are opaque data and
    only their hashes are stored. The caller must keep idempotency keys stable
    for one intended action; changing a key/version is not retry permission.
    """

    def __init__(self, path, *, clock=time.time):
        self.clock = clock
        path = Path(path)
        # Create private files without briefly exposing a newly created DB.
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                brand TEXT NOT NULL, thread TEXT NOT NULL, job TEXT NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                stopped INTEGER NOT NULL DEFAULT 0 CHECK (stopped IN (0, 1)),
                active_action_id TEXT,
                PRIMARY KEY (brand, thread, job)
            );
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                brand TEXT NOT NULL, thread TEXT NOT NULL, job TEXT NOT NULL,
                version INTEGER NOT NULL CHECK (version > 0),
                idempotency_key TEXT NOT NULL, payload_hash TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN
                    ('pending', 'leased', 'dispatching', 'accepted', 'unknown', 'cancelled')),
                generation INTEGER, lease_token TEXT, worker TEXT, lease_until REAL,
                simulated_receipt TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE (brand, thread, job, version, idempotency_key),
                FOREIGN KEY (brand, thread, job) REFERENCES jobs (brand, thread, job)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_job
                ON actions (brand, thread, job) WHERE state IN ('leased', 'dispatching');
            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_id TEXT NOT NULL REFERENCES actions (action_id),
                event TEXT NOT NULL, generation INTEGER, at REAL NOT NULL
            );
        """)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def _now(self, now):
        return _timestamp(self.clock() if now is None else now)

    def _row(self, action_id):
        _label(action_id)
        row = self.db.execute("SELECT * FROM actions WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            raise ActionUnavailable("action_missing")
        return row

    def _event(self, row, event, now):
        self.db.execute("INSERT INTO events(action_id, event, generation, at) VALUES (?, ?, ?, ?)",
                        (row["action_id"], event, row["generation"], now))

    @staticmethod
    def _action(row):
        return Action(row["action_id"], Scope(row["brand"], row["thread"], row["job"], row["version"]),
                      row["idempotency_key"], row["payload_hash"], row["state"],
                      row["generation"], row["simulated_receipt"])

    def get(self, action_id):
        return self._action(self._row(action_id))

    def events(self, action_id):
        self._row(action_id)
        return [dict(row) for row in self.db.execute(
            "SELECT event_id, event, generation, at FROM events WHERE action_id = ? ORDER BY event_id",
            (action_id,))]

    def enqueue_synthetic(self, scope, idempotency_key, payload, *, now=None):
        if not isinstance(scope, Scope):
            raise LedgerError("invalid_scope")
        _label(idempotency_key)
        digest, now = _payload_hash(payload), self._now(now)
        with self._transaction():
            row = self.db.execute("""SELECT * FROM actions WHERE brand = ? AND thread = ?
                AND job = ? AND version = ? AND idempotency_key = ?""",
                (*scope.job_key, scope.version, idempotency_key)).fetchone()
            if row is not None:
                if row["payload_hash"] != digest:
                    raise PayloadConflict("idempotency_payload_conflict")
                return self._action(row)
            self.db.execute("INSERT OR IGNORE INTO jobs(brand, thread, job) VALUES (?, ?, ?)", scope.job_key)
            job = self.db.execute("SELECT stopped FROM jobs WHERE brand = ? AND thread = ? AND job = ?",
                                  scope.job_key).fetchone()
            if job["stopped"]:
                raise JobStopped("job_stopped")
            action_id = uuid.uuid4().hex
            self.db.execute("""INSERT INTO actions
                (action_id, brand, thread, job, version, idempotency_key, payload_hash, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (action_id, *scope.job_key, scope.version, idempotency_key, digest, now, now))
            row = self._row(action_id)
            self._event(row, "synthetic_enqueued", now)
            return self._action(row)

    def _recover_expired(self, now):
        rows = self.db.execute("""SELECT * FROM actions WHERE state IN ('leased', 'dispatching')
            AND lease_until <= ?""", (now,)).fetchall()
        for row in rows:
            state = "unknown" if row["state"] == "dispatching" else "pending"
            self.db.execute("""UPDATE actions SET state = ?, lease_token = NULL, worker = NULL,
                lease_until = NULL, updated_at = ? WHERE action_id = ?""", (state, now, row["action_id"]))
            self.db.execute("""UPDATE jobs SET generation = generation + 1, active_action_id = NULL
                WHERE brand = ? AND thread = ? AND job = ?""", (row["brand"], row["thread"], row["job"]))
            self._event(row, "dispatch_expired_unknown" if state == "unknown" else "lease_expired_requeued", now)
        return len(rows)

    def recover_expired(self, *, now=None):
        """Run on restart/tick. Dispatch expiry never requeues the action."""
        now = self._now(now)
        with self._transaction():
            return self._recover_expired(now)

    def claim(self, action_id, worker, *, lease_seconds=30, now=None):
        _label(worker)
        now = self._now(now)
        if type(lease_seconds) not in (int, float) or not 0 < lease_seconds <= 3600:
            raise LedgerError("invalid_lease_seconds")
        # Recover separately so a subsequent unavailable/unknown claim cannot
        # roll recovery back and leave a crashed dispatch marked in flight.
        self.recover_expired(now=now)
        with self._transaction():
            row = self._row(action_id)
            job_key = (row["brand"], row["thread"], row["job"])
            job = self.db.execute("SELECT * FROM jobs WHERE brand = ? AND thread = ? AND job = ?", job_key).fetchone()
            if job["stopped"]:
                raise JobStopped("job_stopped")
            if row["state"] != "pending":
                raise ActionUnavailable("action_not_pending")
            if job["active_action_id"] is not None:
                raise JobBusy("job_has_active_action")
            token, generation = uuid.uuid4().hex, job["generation"] + 1
            expires_at = now + lease_seconds
            self.db.execute("""UPDATE jobs SET generation = ?, active_action_id = ?
                WHERE brand = ? AND thread = ? AND job = ?""", (generation, action_id, *job_key))
            self.db.execute("""UPDATE actions SET state = 'leased', generation = ?, lease_token = ?,
                worker = ?, lease_until = ?, updated_at = ? WHERE action_id = ?""",
                (generation, token, worker, expires_at, now, action_id))
            self._event(self._row(action_id), "lease_claimed", now)
            return Lease(action_id, token, generation, worker, expires_at)

    def _leased_row(self, lease, state, now):
        if not isinstance(lease, Lease):
            raise StaleLease("invalid_lease")
        row = self._row(lease.action_id)
        job = self.db.execute("SELECT * FROM jobs WHERE brand = ? AND thread = ? AND job = ?",
                              (row["brand"], row["thread"], row["job"])).fetchone()
        if (row["state"] != state or row["lease_token"] != lease.token
                or row["worker"] != lease.worker or row["generation"] != lease.generation
                or job["generation"] != lease.generation or job["stopped"]
                or job["active_action_id"] != lease.action_id
                or row["lease_until"] is None or row["lease_until"] <= now):
            raise StaleLease("stale_or_expired_lease")
        return row

    def begin_simulated_dispatch(self, lease, *, now=None):
        """Persist the uncertainty boundary; performs no external action."""
        now = self._now(now)
        self.recover_expired(now=now)
        with self._transaction():
            row = self._leased_row(lease, "leased", now)
            self.db.execute("UPDATE actions SET state = 'dispatching', updated_at = ? WHERE action_id = ?",
                            (now, lease.action_id))
            self._event(row, "simulated_dispatch_started", now)
            return self.get(lease.action_id)

    def _finish(self, lease, state, receipt, now):
        self.recover_expired(now=now)
        with self._transaction():
            row = self._leased_row(lease, "dispatching", now)
            self.db.execute("""UPDATE actions SET state = ?, simulated_receipt = ?, lease_token = NULL,
                worker = NULL, lease_until = NULL, updated_at = ? WHERE action_id = ?""",
                (state, receipt, now, lease.action_id))
            self.db.execute("""UPDATE jobs SET generation = generation + 1, active_action_id = NULL
                WHERE brand = ? AND thread = ? AND job = ?""", (row["brand"], row["thread"], row["job"]))
            self._event(row, "simulated_provider_accepted" if state == "accepted" else "dispatch_result_unknown", now)
            return self.get(lease.action_id)

    def record_simulated_acceptance(self, lease, simulated_receipt, *, now=None):
        """Record synthetic acceptance. No delivery or live acceptance claim."""
        return self._finish(lease, "accepted", _label(simulated_receipt), self._now(now))

    def mark_unknown(self, lease, *, now=None):
        """A simulated timeout/ambiguous outcome is terminal for auto-retry."""
        return self._finish(lease, "unknown", None, self._now(now))

    def stop_job(self, scope, *, now=None):
        """Trusted local stop/takeover: cancel waiting work across all versions.

        In-flight dispatch becomes unknown. Accepted/unknown/cancelled records
        remain intact. Stopping is atomic with claim and dispatch transitions.
        """
        if not isinstance(scope, Scope):
            raise LedgerError("invalid_scope")
        now = self._now(now)
        with self._transaction():
            self.db.execute("INSERT OR IGNORE INTO jobs(brand, thread, job) VALUES (?, ?, ?)", scope.job_key)
            self.db.execute("""UPDATE jobs SET stopped = 1, generation = generation + 1, active_action_id = NULL
                WHERE brand = ? AND thread = ? AND job = ?""", scope.job_key)
            rows = self.db.execute("""SELECT * FROM actions WHERE brand = ? AND thread = ? AND job = ?
                AND state IN ('pending', 'leased', 'dispatching')""", scope.job_key).fetchall()
            for row in rows:
                state = "unknown" if row["state"] == "dispatching" else "cancelled"
                self.db.execute("""UPDATE actions SET state = ?, lease_token = NULL, worker = NULL,
                    lease_until = NULL, updated_at = ? WHERE action_id = ?""", (state, now, row["action_id"]))
                self._event(row, "takeover_inflight_unknown" if state == "unknown" else "takeover_cancelled", now)
            return len(rows)

    def resume_job(self, scope, *, now=None):
        """Explicit local control. Terminal actions stay terminal after resume."""
        if not isinstance(scope, Scope):
            raise LedgerError("invalid_scope")
        self._now(now)
        with self._transaction():
            job = self.db.execute("SELECT * FROM jobs WHERE brand = ? AND thread = ? AND job = ?", scope.job_key).fetchone()
            if job is None or not job["stopped"]:
                raise LedgerError("job_not_stopped")
            self.db.execute("""UPDATE jobs SET stopped = 0, generation = generation + 1
                WHERE brand = ? AND thread = ? AND job = ?""", scope.job_key)
