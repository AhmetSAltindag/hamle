"""Offline, synthetic workflow proof of concept. No network or model calls.

This is an executable control-layer sample, not a deployed agency service.
Semantic inputs (brand matching, factual validity, conflicting directions) are
provided by fixtures. This module does not claim to infer them from language.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Istanbul")
HAMLE_ADDRESS = "hamle@gamechangers.com.tr"
REPRESENTATIVE = "account_manager"
PRODUCTION_ROLES = {"copywriter", "graphic_designer", "motion_designer"}
REQUIRED_FIELDS = ("objective", "deliverable", "source_refs")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def slots_for_day(day):
    """Weekdays only; no official holiday calendar has been configured."""
    if day.weekday() >= 5:
        return []
    start = datetime.combine(day, time(8, 30), TZ)
    return [start + timedelta(minutes=30 * index) for index in range(21)]


def next_slot(now):
    """Return the strictly next planned control after timezone-aware `now`."""
    if now.tzinfo is None:
        raise ValueError("timezone_required")
    local = now.astimezone(TZ)
    for offset in range(8):
        for slot in slots_for_day(local.date() + timedelta(days=offset)):
            if slot > local:
                return slot
    raise AssertionError("weekday_not_found")


class RuleBlocked(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Engine:
    """SQLite-backed offline action ledger with a simulated dispatch boundary."""

    def __init__(self, path, policy):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=2)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT OR IGNORE INTO settings VALUES('stopped','0');
        CREATE TABLE IF NOT EXISTS jobs(
          id INTEGER PRIMARY KEY, brand TEXT NOT NULL, request_ref TEXT NOT NULL,
          thread TEXT NOT NULL, state TEXT NOT NULL, owner TEXT NOT NULL,
          brief_version INTEGER NOT NULL DEFAULT 0,
          human_answered INTEGER NOT NULL DEFAULT 0,
          UNIQUE(brand, request_ref));
        CREATE TABLE IF NOT EXISTS events(
          channel TEXT NOT NULL, source_id TEXT NOT NULL, brand TEXT NOT NULL,
          job_id INTEGER REFERENCES jobs(id), received_at TEXT NOT NULL,
          body TEXT NOT NULL, archive INTEGER NOT NULL,
          PRIMARY KEY(channel, source_id));
        CREATE TABLE IF NOT EXISTS briefs(
          job_id INTEGER NOT NULL REFERENCES jobs(id), version INTEGER NOT NULL,
          created_at TEXT NOT NULL, data TEXT NOT NULL, digest TEXT NOT NULL,
          PRIMARY KEY(job_id,version));
        CREATE TRIGGER IF NOT EXISTS immutable_brief_update BEFORE UPDATE ON briefs
          BEGIN SELECT RAISE(ABORT,'immutable_brief'); END;
        CREATE TRIGGER IF NOT EXISTS immutable_brief_delete BEFORE DELETE ON briefs
          BEGIN SELECT RAISE(ABORT,'immutable_brief'); END;
        CREATE TABLE IF NOT EXISTS actions(
          id INTEGER PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
          job_id INTEGER NOT NULL REFERENCES jobs(id), kind TEXT NOT NULL,
          brand TEXT NOT NULL, recipient TEXT NOT NULL, thread TEXT NOT NULL,
          role TEXT NOT NULL, brief_version INTEGER NOT NULL,
          state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          evidence TEXT, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS action_log(
          id INTEGER PRIMARY KEY, action_id INTEGER REFERENCES actions(id),
          event TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS checkpoints(
          source TEXT PRIMARY KEY, cursor TEXT NOT NULL, checked_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS read_failures(
          id INTEGER PRIMARY KEY, source TEXT NOT NULL, detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS locks(
          key TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS approvals(
          job_id INTEGER NOT NULL REFERENCES jobs(id), file_version TEXT NOT NULL,
          person TEXT NOT NULL, evidence TEXT NOT NULL,
          PRIMARY KEY(job_id,file_version));
        CREATE TABLE IF NOT EXISTS event_authority(
          channel TEXT NOT NULL, source_id TEXT NOT NULL,
          actionable INTEGER NOT NULL, reason TEXT NOT NULL, evidence TEXT,
          mail_epoch TEXT,
          PRIMARY KEY(channel,source_id));
        CREATE TABLE IF NOT EXISTS mail_authority(
          brand TEXT NOT NULL, thread TEXT NOT NULL, sequence INTEGER NOT NULL,
          source_id TEXT NOT NULL, allowed INTEGER NOT NULL, reason TEXT NOT NULL,
          PRIMARY KEY(brand,thread));
        CREATE TABLE IF NOT EXISTS mail_order_uncertainties(
          brand TEXT NOT NULL, thread TEXT NOT NULL, source_id TEXT NOT NULL,
          PRIMARY KEY(brand,thread,source_id));
        """)
        # Existing synthetic ledgers remain readable. Old briefs have no event
        # authority and cannot dispatch until a fresh verified event/brief exists.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(briefs)")}
        for field in ("trigger_channel", "trigger_source_id"):
            if field not in columns:
                self.db.execute(f"ALTER TABLE briefs ADD COLUMN {field} TEXT")
        if "mail_epoch" not in {row[1] for row in self.db.execute("PRAGMA table_info(event_authority)")}:
            self.db.execute("ALTER TABLE event_authority ADD COLUMN mail_epoch TEXT")
        encoded = canonical(policy)
        existing = self.db.execute("SELECT value FROM settings WHERE key='policy'").fetchone()
        if existing and existing[0] != encoded:
            raise RuleBlocked("policy_change_requires_migration")
        self.db.execute("INSERT OR IGNORE INTO settings VALUES('policy',?)", (encoded,))
        # Read-only by convention in this sample. Real deployments need separate
        # credentials/permissions so agents cannot change grants or this database.
        self.policy = json.loads(encoded)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def _brand(self, brand):
        if brand not in self.policy["brands"]:
            raise RuleBlocked("brand_not_authorized")
        return self.policy["brands"][brand]

    @staticmethod
    def _bare_address(address):
        return (isinstance(address, str) and address.isascii()
                and address.count("@") == 1 and all(address.split("@"))
                and not any(c.isspace() or ord(c) < 32 or c in '<>,;"\\'
                            for c in address))

    @staticmethod
    def _mail_decision(evidence, source_id, thread):
        """Consume trusted adapter metadata, never quoted/body/forward headers.

        This sample does not authenticate a provider. `sequence` is the trusted
        adapter's per-thread message order, not local arrival order or email Date.
        Parsed To/CC contain bare addresses only. BCC/delivery copies never grant.
        """
        if not isinstance(evidence, dict):
            return False, "mail_evidence_missing", None
        sequence = evidence.get("sequence")
        verified = (evidence.get("verified_direct_headers") is True
                    and evidence.get("message_id") == source_id
                    and evidence.get("thread_id") == thread
                    and type(sequence) is int and sequence >= 0)
        if not verified:
            return False, "mail_evidence_uncertain", None
        sender = evidence.get("from_address")
        direction = evidence.get("direction")
        if not Engine._bare_address(sender) or direction not in {"inbound", "outbound"}:
            return False, "mail_evidence_uncertain", sequence
        if direction == "outbound" or sender.casefold() == HAMLE_ADDRESS:
            return False, "own_or_outbound_mail", sequence
        to, cc = evidence.get("direct_to"), evidence.get("direct_cc")
        if not all(isinstance(items, list) and all(Engine._bare_address(a) for a in items)
                   for items in (to, cc)):
            return False, "mail_evidence_uncertain", sequence
        included = any(address.casefold() == HAMLE_ADDRESS for address in to + cc)
        return included, "direct_to_cc" if included else "not_direct_to_cc", sequence

    def _observe_mail_authority(self, brand, thread, source_id, allowed, reason, sequence, evidence):
        current = self.db.execute("SELECT * FROM mail_authority WHERE brand=? AND thread=?",
                                  (brand, thread)).fetchone()
        if sequence is not None and current and sequence < current["sequence"]:
            return False, "older_mail_event"
        if sequence is not None and current and sequence == current["sequence"]:
            allowed, reason = False, "ambiguous_mail_order"
        if sequence is None or reason == "ambiguous_mail_order":
            self.db.execute("INSERT OR IGNORE INTO mail_order_uncertainties VALUES(?,?,?)",
                            (brand, thread, source_id))
        elif reason in {"direct_to_cc", "not_direct_to_cc"}:
            # A fresh numeric sequence alone cannot outrank an observed message
            # whose position was unknown. The trusted adapter must explicitly
            # resolve every such observation as older than this incoming event.
            resolved = evidence.get("verified_newer_than_source_ids", [])
            if isinstance(resolved, list) and all(isinstance(item, str) for item in resolved):
                for item in resolved:
                    self.db.execute("DELETE FROM mail_order_uncertainties "
                                    "WHERE brand=? AND thread=? AND source_id=?", (brand, thread, item))
        if self.db.execute("SELECT 1 FROM mail_order_uncertainties WHERE brand=? AND thread=?",
                           (brand, thread)).fetchone():
            allowed, reason = False, "mail_order_unresolved"
        # Missing/uncertain evidence for an observed, reliably correlated event
        # closes the gate. No polling for unobservable messages is implied.
        order = sequence if sequence is not None else (current["sequence"] if current else -1)
        self.db.execute("INSERT INTO mail_authority VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(brand,thread) DO UPDATE SET sequence=excluded.sequence,"
                        "source_id=excluded.source_id,allowed=excluded.allowed,reason=excluded.reason",
                        (brand, thread, order, source_id, int(allowed), reason))
        state = "stale_mail_event" if allowed else "mail_authorization_revoked"
        self.db.execute("UPDATE actions SET state=? WHERE job_id IN "
                        "(SELECT id FROM jobs WHERE brand=? AND thread=?) "
                        "AND state IN ('planned','dispatch_started')", (state, brand, thread))
        return allowed, reason

    def ingest(self, *, channel, source_id, brand, request_ref, thread,
               received_at, body, archive=False, automated=False, mail_evidence=None):
        """request_ref must already be reliably correlated; no semantic matching.

        Source bodies are stored as data and never executed or used as grants.
        Archive/automatic/outbound mail cannot grant authority or revoke a live
        grant. Known incoming removal revokes pending actions for this thread.
        """
        self._brand(brand)
        if channel not in {"email", "basecamp"}:
            raise RuleBlocked("channel_not_enabled")
        with self.transaction():
            existing = self.db.execute(
                "SELECT job_id,brand FROM events WHERE channel=? AND source_id=?",
                (channel, source_id)).fetchone()
            if existing:
                if archive or automated:
                    return None
                if existing["brand"] != brand:
                    raise RuleBlocked("source_evidence_conflict")
                gate = self.db.execute("SELECT actionable FROM event_authority "
                                       "WHERE channel=? AND source_id=?", (channel, source_id)).fetchone()
                if channel == "email":
                    prior = self.db.execute("SELECT evidence FROM event_authority "
                                            "WHERE channel=? AND source_id=?", (channel, source_id)).fetchone()
                    encoded = canonical(mail_evidence)
                    if (not prior or prior[0] != encoded or not isinstance(mail_evidence, dict)
                            or mail_evidence.get("message_id") != source_id
                            or mail_evidence.get("thread_id") != thread):
                        raise RuleBlocked("source_evidence_conflict")
                if not existing[0] or not gate or not gate[0]:
                    return None
                try:
                    self._trigger_guard(existing[0], (channel, source_id))
                except RuleBlocked:
                    return None
                return existing[0]
            job_id = None
            allowed, reason = True, "native_basecamp_event"
            if archive or automated:
                allowed, reason = False, "archive_or_automated"
            elif channel == "email":
                allowed, reason, sequence = self._mail_decision(mail_evidence, source_id, thread)
                if reason != "own_or_outbound_mail":
                    allowed, reason = self._observe_mail_authority(
                        brand, thread, source_id, allowed, reason, sequence, mail_evidence)
            current = self.db.execute("SELECT * FROM mail_authority WHERE brand=? AND thread=?",
                                      (brand, thread)).fetchone()
            if channel == "basecamp" and current and not current["allowed"]:
                allowed, reason = False, "mail_not_directly_included"
            if allowed:
                self.db.execute(
                    "INSERT OR IGNORE INTO jobs(brand,request_ref,thread,state,owner) "
                    "VALUES(?,?,?,'reviewing',?)", (brand, request_ref, thread, REPRESENTATIVE))
                job_id = self.db.execute(
                    "SELECT id FROM jobs WHERE brand=? AND request_ref=?",
                    (brand, request_ref)).fetchone()[0]
            self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?)",
                            (channel, source_id, brand, job_id, received_at,
                             body, int(archive)))
            self.db.execute("INSERT INTO event_authority VALUES(?,?,?,?,?,?)",
                            (channel, source_id, int(allowed), reason,
                             canonical(mail_evidence) if channel == "email" else None,
                             current["source_id"] if current else None))
            return job_id

    def _trigger_guard(self, job_id, trigger):
        if not isinstance(trigger, (list, tuple)) or len(trigger) != 2:
            raise RuleBlocked("trigger_evidence_required")
        row = self.db.execute("SELECT e.job_id,a.actionable,a.mail_epoch FROM events e JOIN event_authority a "
                              "ON e.channel=a.channel AND e.source_id=a.source_id "
                              "WHERE e.channel=? AND e.source_id=?", tuple(trigger)).fetchone()
        if not row or row["job_id"] != job_id or not row["actionable"]:
            raise RuleBlocked("trigger_not_authorized")
        job = self.job(job_id)
        current = self.db.execute("SELECT * FROM mail_authority WHERE brand=? AND thread=?",
                                  (job["brand"], job["thread"])).fetchone()
        if current and not current["allowed"]:
            raise RuleBlocked("mail_not_directly_included")
        if current and row["mail_epoch"] != current["source_id"]:
            raise RuleBlocked("stale_mail_event")
        if trigger[0] == "email":
            if not current or current["source_id"] != trigger[1]:
                raise RuleBlocked("stale_mail_event")

    def job(self, job_id):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise RuleBlocked("job_not_found")
        return dict(row)

    def add_brief(self, job_id, data, created_at, *, trigger):
        with self.transaction():
            job = self.job(job_id)
            if job["owner"] == "human":
                raise RuleBlocked("human_takeover")
            self._trigger_guard(job_id, trigger)
            if data["brand"] != job["brand"]:
                raise RuleBlocked("wrong_brand_brief")
            version = job["brief_version"] + 1
            encoded = canonical(data)
            self.db.execute("INSERT INTO briefs VALUES(?,?,?,?,?,?,?)",
                            (job_id, version, created_at, encoded,
                             hashlib.sha256(encoded.encode()).hexdigest(), *trigger))
            state = "waiting_information" if self.missing_fields(data) else "brief_ready"
            self.db.execute("UPDATE jobs SET brief_version=?,state=? WHERE id=?",
                            (version, state, job_id))
            self.db.execute("UPDATE actions SET state='stale_brief' "
                            "WHERE job_id=? AND state='planned'", (job_id,))
            return version

    @staticmethod
    def missing_fields(data):
        return [field for field in REQUIRED_FIELDS if not data.get(field)]

    @staticmethod
    def unanswered_questions(required_fields, known_answers):
        # Structured answer lookup only; does not understand historical prose.
        return [field for field in required_fields if not known_answers.get(field)]

    def _brief(self, job_id, version):
        row = self.db.execute("SELECT data FROM briefs WHERE job_id=? AND version=?",
                              (job_id, version)).fetchone()
        if not row:
            raise RuleBlocked("brief_not_found")
        return json.loads(row[0])

    def _guard(self, action, today):
        if self.db.execute("SELECT value FROM settings WHERE key='stopped'").fetchone()[0] == "1":
            raise RuleBlocked("stopped")
        job = self.job(action["job_id"])
        if job["owner"] == "human":
            raise RuleBlocked("human_takeover")
        if job["human_answered"]:
            raise RuleBlocked("human_already_answered")
        if action["role"] != REPRESENTATIVE:
            raise RuleBlocked("external_communication_role")
        policy = self._brand(job["brand"])
        if action["brand"] != job["brand"]:
            raise RuleBlocked("wrong_brand")
        if action["kind"] not in {"email_reply", "basecamp_task"}:
            raise RuleBlocked("action_kind_not_enabled")
        allowed = policy["recipients"] if action["kind"] == "email_reply" else policy["projects"]
        if action["recipient"] not in allowed:
            raise RuleBlocked("recipient_not_authorized")
        if action["thread"] != job["thread"]:
            raise RuleBlocked("wrong_thread")
        if action["brief_version"] != job["brief_version"]:
            raise RuleBlocked("stale_brief")
        origin = self.db.execute("SELECT trigger_channel,trigger_source_id FROM briefs "
                                 "WHERE job_id=? AND version=?",
                                 (job["id"], action["brief_version"])).fetchone()
        self._trigger_guard(job["id"], tuple(origin) if origin else None)
        brief = self._brief(job["id"], action["brief_version"])
        if self.missing_fields(brief):
            raise RuleBlocked("incomplete_brief")
        if brief.get("conflicting_instructions"):
            raise RuleBlocked("conflicting_instructions")
        if brief.get("scope_change") and not brief.get("scope_change_authorized"):
            raise RuleBlocked("scope_change_not_authorized")
        if brief.get("delivery_promise") and not brief.get("capacity_confirmed"):
            raise RuleBlocked("capacity_not_confirmed")
        for source in brief["source_refs"]:
            if source["brand"] != job["brand"]:
                raise RuleBlocked("wrong_brand_source")
            if source.get("status") != "current" or source.get("valid_until", today) < today:
                raise RuleBlocked("stale_information")
        for asset in brief.get("assets", []):
            if asset["brand"] != job["brand"]:
                raise RuleBlocked("wrong_brand_asset")
            if not asset.get("readable") or not asset.get("verified"):
                raise RuleBlocked("unreadable_or_unverified_asset")
        for claim in brief.get("claims", []):
            if not claim.get("source_id") or not claim.get("verified"):
                raise RuleBlocked("unsupported_claim")

    def prepare_action(self, *, key, job_id, kind, brand, recipient, thread,
                       role, brief_version, payload, today):
        """Store only reviewed/structured sample actions; no semantic body QA."""
        candidate = dict(job_id=job_id, kind=kind, brand=brand, recipient=recipient,
                         thread=thread, role=role, brief_version=brief_version,
                         payload=canonical(payload))
        with self.transaction():
            existing = self.db.execute("SELECT * FROM actions WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                if any(existing[field] != value for field, value in candidate.items()):
                    raise RuleBlocked("idempotency_key_payload_conflict")
                return existing["id"]
            self._guard(candidate, today)
            cursor = self.db.execute(
                "INSERT INTO actions(idempotency_key,job_id,kind,brand,recipient,thread,role,"
                "brief_version,state,payload) VALUES(?,?,?,?,?,?,?,?,'planned',?)",
                (key, job_id, kind, brand, recipient, thread, role, brief_version, canonical(payload)))
            self.db.execute("INSERT INTO action_log(action_id,event,detail) VALUES(?,'planned','offline only')",
                            (cursor.lastrowid,))
            return cursor.lastrowid

    def begin_dispatch(self, action_id, today):
        """Persist intent first. This never calls a provider."""
        with self.transaction():
            row = self.db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row or row["state"] != "planned":
                raise RuleBlocked("action_not_replayable")
            self._guard(dict(row), today)
            self.db.execute("UPDATE actions SET state='dispatch_started' WHERE id=?", (action_id,))
            self.db.execute("INSERT INTO action_log(action_id,event,detail) VALUES(?,'dispatch_started','no provider call')",
                            (action_id,))

    def simulate_provider_boundary(self, action_id, today, outcome="accepted"):
        """Atomic last gate plus fake transport. NO EMAIL/BASECAMP IS SENT.

        A real adapter would need equivalent fencing/transaction coordination.
        A network side effect cannot be rolled back by SQLite.
        """
        if outcome not in {"accepted", "disconnect"}:
            raise ValueError("simulation_outcome_required")
        with self.transaction():
            row = self.db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row or row["state"] != "dispatch_started":
                raise RuleBlocked("action_not_replayable")
            self._guard(dict(row), today)
            state = "simulated_accepted" if outcome == "accepted" else "unknown"
            self.db.execute("UPDATE actions SET state=?,attempts=attempts+1 WHERE id=?", (state, action_id))
            self.db.execute("INSERT INTO action_log(action_id,event,detail) VALUES(?,?,?)",
                            (action_id, state, "synthetic transport outcome"))
            return state

    def recover_after_restart(self):
        # A crash after persisted intent has an uncertain result. Never replay it.
        with self.transaction():
            self.db.execute("UPDATE actions SET state='unknown' WHERE state='dispatch_started'")

    def reconcile_unknown(self, action_id, evidence, provider_status):
        """Requires supplied provider evidence; this sample does not fetch it."""
        if not evidence or provider_status not in {"accepted", "bounced", "not_sent"}:
            raise RuleBlocked("provider_evidence_required")
        with self.transaction():
            row = self.db.execute("SELECT state FROM actions WHERE id=?", (action_id,)).fetchone()
            if not row or row[0] != "unknown":
                raise RuleBlocked("not_unknown")
            state = {"accepted": "simulated_accepted", "bounced": "simulated_bounced",
                     "not_sent": "confirmed_not_sent_requires_review"}[provider_status]
            self.db.execute("UPDATE actions SET state=?,evidence=? WHERE id=?", (state, evidence, action_id))

    def stop(self):
        self.db.execute("UPDATE settings SET value='1' WHERE key='stopped'")

    def human_takeover(self, job_id, already_answered=False):
        with self.transaction():
            self.db.execute("UPDATE jobs SET owner='human',state='human_takeover',human_answered=? WHERE id=?",
                            (int(already_answered), job_id))
            self.db.execute("UPDATE actions SET state='human_takeover' WHERE job_id=? AND state='planned'", (job_id,))

    def producer_status(self, job_id, status):
        if status not in {"accepted", "rejected", "late"}:
            raise ValueError("invalid_producer_status")
        state = "in_production" if status == "accepted" else "needs_reassignment"
        self.db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job_id))
        return state

    def approve(self, job_id, person, file_version, evidence):
        job = self.job(job_id)
        if person not in self._brand(job["brand"])["approvers"]:
            raise RuleBlocked("approver_not_authorized")
        if not file_version or not evidence:
            raise RuleBlocked("approval_evidence_required")
        self.db.execute("INSERT INTO approvals VALUES(?,?,?,?)", (job_id, file_version, person, evidence))

    def read_brief(self, job_id, role, authorized_brand):
        job = self.job(job_id)
        if authorized_brand != job["brand"]:
            raise RuleBlocked("cross_brand_read")
        if role not in PRODUCTION_ROLES | {REPRESENTATIVE}:
            raise RuleBlocked("role_not_authorized")
        brief = self._brief(job_id, job["brief_version"])
        if role in PRODUCTION_ROLES:
            # Narrow allowlist: no commercial/private fields or raw email bodies.
            visible = {"brand", "objective", "deliverable", "source_refs", "assets", "claims"}
            brief = {key: value for key, value in brief.items() if key in visible}
        return brief

    def save_checkpoint(self, source, cursor, checked_at, read_ok=True):
        if not read_ok:
            self.db.execute("INSERT INTO read_failures(source,detail) VALUES(?,'source_read_failed')", (source,))
            return "read_failed"
        self.db.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(source) "
                        "DO UPDATE SET cursor=excluded.cursor,checked_at=excluded.checked_at",
                        (source, cursor, checked_at))
        return "saved"

    def acquire_lease(self, key, worker, now, seconds=60):
        with self.transaction():
            row = self.db.execute("SELECT owner,expires_at FROM locks WHERE key=?", (key,)).fetchone()
            stamp = now.isoformat()
            if row and row[1] > stamp and row[0] != worker:
                return False
            expiry = (now + timedelta(seconds=seconds)).isoformat()
            self.db.execute("INSERT INTO locks VALUES(?,?,?) ON CONFLICT(key) "
                            "DO UPDATE SET owner=excluded.owner,expires_at=excluded.expires_at",
                            (key, worker, expiry))
            return True
