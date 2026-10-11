"""Local schedule contracts. No scheduler, holiday feed, or mail transport.

The caller must durably claim a slot and summary intent before acting. A
summary intent is a candidate record, not authority to send an email.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


ISTANBUL = ZoneInfo("Europe/Istanbul")
HOLIDAY_POLICY = "unknown"


def _local(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_required")
    return value.astimezone(ISTANBUL)


def slots_for_day(day: date) -> tuple[datetime, ...]:
    """Return the requested weekday slots; official holidays are not inferred."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise ValueError("date_required")
    if day.weekday() >= 5:
        return ()
    start = datetime.combine(day, time(8, 30), ISTANBUL)
    return tuple(start + timedelta(minutes=30 * i) for i in range(21))


def day_plan(day: date) -> dict:
    return {
        "date": day.isoformat(),
        "timezone": "Europe/Istanbul",
        "holiday_policy": HOLIDAY_POLICY,
        "holiday_calendar_connected": False,
        "slots": [slot.isoformat() for slot in slots_for_day(day)],
        "scheduler_installed": False,
    }


def slot_key(slot: datetime) -> str:
    local = _local(slot)
    if local not in slots_for_day(local.date()):
        raise ValueError("not_scheduled_slot")
    return "hamle-check:" + local.isoformat()


def next_slot(now: datetime) -> datetime:
    """Return the strictly next requested slot; holiday policy remains unknown."""
    local = _local(now)
    for offset in range(8):
        for slot in slots_for_day(local.date() + timedelta(days=offset)):
            if slot > local:
                return slot
    raise AssertionError("weekday_not_found")


def summary_intent(slot: datetime, *, outcome: str, completed_at: datetime,
                   existing_intent_keys=()) -> dict | None:
    """Suggest one daily summary after a successful run of the 08:30 slot.

    Repeated successful attempts use the same key. Persisting that key with a
    unique constraint is the caller's responsibility; this pure function alone
    cannot prevent two workers from sending. A failed 08:30 run followed by a
    successful 09:00 slot does not qualify. Late next-day completions are stale.
    """
    key = slot_key(slot)
    local, completed = _local(slot), _local(completed_at)
    if outcome not in {"success", "failed", "unknown"}:
        raise ValueError("invalid_run_outcome")
    if completed < local:
        raise ValueError("completion_before_slot")
    if (outcome != "success" or local.time() != time(8, 30)
            or completed.date() != local.date()):
        return None
    intent_key = "hamle-morning-summary:" + local.date().isoformat()
    if intent_key in existing_intent_keys:
        return None
    return {
        "idempotency_key": intent_key,
        "source_slot_key": key,
        "completed_at": completed.isoformat(),
        "sender": "hamle@gamechangers.com.tr",
        "recipient": "ahmet@gamechangers.com.tr",
        "kind": "morning_summary",
        "state": "intent_only",
        "send_authorized": False,
        "external_writes": 0,
    }
