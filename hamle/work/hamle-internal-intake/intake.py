"""Offline metadata review inbox. It cannot read mail or authorize actions."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re

HAMLE = "hamle@gamechangers.com.tr"
AHMET = "ahmet@gamechangers.com.tr"
MAX_REVIEW_AGE_SECONDS = 300
HEX_ID = re.compile(r"[a-f0-9]{1,64}\Z")
ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")


class InvalidCapture(ValueError):
    """Fixed reason codes only, never source values."""


def timestamp(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise InvalidCapture("invalid_clock")
    return float(value)


def identifier(value):
    if not isinstance(value, str) or not HEX_ID.fullmatch(value):
        raise InvalidCapture("invalid_message_or_thread_id")
    return value


def addresses(value):
    if (not isinstance(value, list) or len(value) > 200
            or any(not isinstance(x, str) or not ADDRESS.fullmatch(x)
                   or x != x.casefold() for x in value)):
        raise InvalidCapture("invalid_direct_recipients")
    return sorted(set(value))


def metadata(observation):
    """Validate the normalizer contract; discard bodies, attachments and instructions."""
    if (not isinstance(observation, dict) or type(observation.get("schema_version")) is not int
            or observation["schema_version"] != 1 or observation.get("action_eligible") is not False):
        raise InvalidCapture("invalid_observation_contract")
    thread = identifier(observation.get("thread_id"))
    rows = observation.get("messages")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise InvalidCapture("invalid_message_list")
    result, ids = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise InvalidCapture("invalid_message_metadata")
        mid = identifier(row.get("message_id"))
        if mid in ids or row.get("thread_id") != thread:
            raise InvalidCapture("duplicate_or_mismatched_message")
        ids.add(mid)
        sender = row.get("from_address")
        if not isinstance(sender, str) or not ADDRESS.fullmatch(sender) or sender != sender.casefold():
            raise InvalidCapture("invalid_sender")
        direct_to, direct_cc = addresses(row.get("direct_to")), addresses(row.get("direct_cc"))
        labels, issues = row.get("label_ids"), row.get("issues")
        if (not isinstance(labels, list) or any(not isinstance(x, str) for x in labels)
                or issues != [] or row.get("action_eligible") is not False
                or type(row.get("automated")) is not bool
                or type(row.get("envelope_eligible")) is not bool
                or row.get("participation") not in ("included", "excluded")
                or type(row.get("internal_date_ms")) is not int
                or not 0 <= row["internal_date_ms"] <= 253402300799999):
            raise InvalidCapture("incomplete_or_ambiguous_metadata")
        outbound = sender == HAMLE or "SENT" in labels or "DRAFT" in labels
        direction = "outbound" if outbound else "inbound"
        included = HAMLE in direct_to + direct_cc
        evidence = row.get("engine_evidence")
        if (row.get("direction") != direction
                or row["participation"] != ("included" if included else "excluded")
                or row["envelope_eligible"] != (included and not outbound and not row["automated"])
                or not isinstance(evidence, dict) or evidence.get("verified_direct_headers") is not True
                or "sequence" not in evidence
                or evidence.get("sequence") is not None
                or evidence.get("message_id") != mid or evidence.get("thread_id") != thread
                or evidence.get("from_address") != sender or evidence.get("direction") != direction
                or addresses(evidence.get("direct_to")) != direct_to
                or addresses(evidence.get("direct_cc")) != direct_cc):
            raise InvalidCapture("inconsistent_or_unverified_metadata")
        result.append({"id": mid, "from": sender, "to": direct_to, "cc": direct_cc,
                       "direction": direction, "automated": row["automated"],
                       "time_ms": row["internal_date_ms"]})
    if (type(observation.get("message_count")) is not int or observation["message_count"] != len(result)
            or observation.get("message_ids") != [r["id"] for r in result]):
        raise InvalidCapture("incomplete_snapshot")
    order = observation.get("order")
    if (not isinstance(order, dict) or order.get("status") != "observed_internal_date_order_only"
            or "authority_sequence" not in order or "latest_observed_incoming_id" not in order
            or order.get("authority_sequence") is not None
            or order.get("global_order_verified") is not False
            or order.get("unseen_messages_known") is not False):
        raise InvalidCapture("order_not_established")
    dates = [row["time_ms"] for row in result]
    if len(dates) != len(set(dates)):
        raise InvalidCapture("ambiguous_message_order")
    incoming = [row for row in result if row["direction"] == "inbound"]
    latest = max(incoming, key=lambda row: row["time_ms"]) if incoming else None
    if order.get("latest_observed_incoming_id") != (latest["id"] if latest else None):
        raise InvalidCapture("latest_message_mismatch")
    return {"thread": thread, "messages": result, "latest": latest}


def snapshot_fingerprint(observation):
    value = metadata(observation)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Capture:
    """Trusted caller assertion, not proof supplied by mail and not a digital signature."""
    source: str
    mailbox: str
    observed_at: float
    complete_visible_snapshot: bool
    fingerprint: str


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    review_item: dict | None = None
    action_eligible: bool = False
    external_writes: int = 0


class ReviewInbox:
    """In-memory review state. Human event methods require a trusted caller/UI.

    This does not authenticate a human, persist across processes, or drive the
    SyntheticLedger. In particular it never enqueues dispatch actions.
    """
    def __init__(self):
        self.generation = 0
        self.stopped = False
        self._items = {}
        self._renewals = {}
        self._seen = {}

    @staticmethod
    def _human(actor):
        if actor != AHMET:
            raise InvalidCapture("human_actor_not_allowed")

    def stop(self, *, actor, takeover=False):
        self._human(actor)
        if type(takeover) is not bool:
            raise InvalidCapture("invalid_human_event")
        self.stopped = True
        self.generation += 1
        self._items.clear()
        self._renewals.clear()
        return "human_takeover" if takeover else "human_stop"

    def resume_review(self, *, actor):
        self._human(actor)
        self.stopped = False
        self.generation += 1
        self._items.clear()
        self._renewals.clear()

    def renew_delayed_review(self, observation, *, actor, approved_at):
        self._human(actor)
        if self.stopped:
            raise InvalidCapture("human_control_stopped")
        digest = snapshot_fingerprint(observation)
        self._renewals[digest] = (self.generation, timestamp(approved_at))

    def review(self, observation, *, capture, now):
        if self.stopped:
            return Decision("blocked", "human_control_stopped")
        try:
            now = timestamp(now)
            normalized = metadata(observation)
            digest = snapshot_fingerprint(observation)
            if (not isinstance(capture, Capture) or capture.source not in ("synthetic_fixture", "captured_normalized")
                    or capture.mailbox != HAMLE or capture.complete_visible_snapshot is not True
                    or capture.fingerprint != digest):
                raise InvalidCapture("capture_context_invalid_or_mismatched")
            observed_at = timestamp(capture.observed_at)
            if observed_at > now:
                raise InvalidCapture("capture_from_future")
            # A newer captured thread view invalidates every previous candidate
            # in that thread, even when the new last message excludes Hamle.
            self._items = {key: value for key, value in self._items.items()
                           if not key.startswith(normalized["thread"] + ":")}
            visible_time = max(row["time_ms"] for row in normalized["messages"])
            visible_ids = frozenset(row["id"] for row in normalized["messages"])
            previous = self._seen.get(normalized["thread"])
            if previous is not None:
                prior_at, prior_time, prior_digest, prior_ids = previous
                if (observed_at < prior_at or visible_time < prior_time
                        or not prior_ids.issubset(visible_ids)
                        or (observed_at == prior_at and digest != prior_digest)
                        or (visible_ids == prior_ids and digest != prior_digest)):
                    return Decision("blocked", "stale_or_changed_snapshot_requires_human_review")
            self._seen[normalized["thread"]] = (observed_at, visible_time, digest, visible_ids)
            candidate = normalized["latest"]
            if candidate is None:
                return Decision("blocked", "no_incoming_message")
            key = normalized["thread"] + ":" + candidate["id"]
            if candidate["automated"]:
                return Decision("blocked", "latest_incoming_automated")
            if HAMLE not in candidate["to"] + candidate["cc"]:
                return Decision("blocked", "latest_direct_to_cc_excludes_hamle")
            if candidate["from"] != AHMET:
                return Decision("blocked", "outside_ahmet_internal_pilot")
            if any(row["time_ms"] > candidate["time_ms"] for row in normalized["messages"]):
                return Decision("blocked", "newer_outbound_requires_human_review")
            renewed = False
            if now - observed_at > MAX_REVIEW_AGE_SECONDS:
                approval = self._renewals.get(digest)
                if (not approval or approval[0] != self.generation
                        or not observed_at <= approval[1] <= now
                        or now - approval[1] > MAX_REVIEW_AGE_SECONDS):
                    return Decision("held", "delayed_requires_human_renewal")
                renewed = True
            item = {"review_id": hashlib.sha256(key.encode()).hexdigest(),
                    "brand": "gamechangers_internal", "thread_id": normalized["thread"],
                    "message_id": candidate["id"], "capture_fingerprint": digest,
                    "source": capture.source, "control_generation": self.generation,
                    "human_renewed_delayed_review": renewed,
                    "status": "awaiting_human_review", "action_eligible": False,
                    "latest_scope": "latest_observed_in_captured_snapshot_only",
                    "unseen_cc_removal_resolved": False, "body_or_attachments_included": False}
            # Retain only the normalizer fields checked above. Mail bodies,
            # attachments and arbitrary input keys never enter the review inbox.
            snapshot = {name: observation[name] for name in
                        ("schema_version", "thread_id", "message_count", "message_ids", "order", "action_eligible")}
            snapshot["messages"] = [{name: row[name] for name in
                ("message_id", "thread_id", "from_address", "direct_to", "direct_cc", "label_ids",
                 "issues", "action_eligible", "automated", "envelope_eligible", "participation",
                 "internal_date_ms", "direction", "engine_evidence")} for row in observation["messages"]]
            snapshot["order"] = {name: observation["order"][name] for name in
                ("status", "latest_observed_incoming_id", "authority_sequence",
                 "global_order_verified", "unseen_messages_known")}
            for row in snapshot["messages"]:
                row["engine_evidence"] = {name: row["engine_evidence"][name] for name in
                    ("message_id", "thread_id", "verified_direct_headers", "sequence",
                     "from_address", "direction", "direct_to", "direct_cc")}
            snapshot = json.loads(json.dumps(snapshot))
            self._items[key] = (snapshot, capture, item)
            return Decision("review_only", "human_review_required", dict(item))
        except InvalidCapture as error:
            return Decision("blocked", str(error))

    def pending(self, *, now):
        # Re-evaluate age/control before displaying; old copies never grant actions.
        prior = list(self._items.values())
        self._items.clear()
        for observation, capture, _ in prior:
            self.review(observation, capture=capture, now=now)
        return [dict(item) for _, _, item in self._items.values()]
