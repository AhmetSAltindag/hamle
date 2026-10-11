"""Single runtime authorization window and weekday schedule windows (no I/O).

AUTHORIZATION_DEADLINE is the ONE code value that bounds every live runtime
action (cloud_store.DEADLINE, chain slot/manual runs, daily digest, budget import).
To extend after Ahmet confirms, change only this line (one-line change):
    AUTHORIZATION_DEADLINE = "2026-11-12T23:59:59+03:00"
That code change alone does NOT extend data-side expiries; see DATED_DATA below.
"""
from datetime import datetime

AUTHORIZATION_START = "2026-10-09T00:00:00+03:00"
AUTHORIZATION_DEADLINE = "2027-12-31T23:59:59+03:00"  # Ahmet 2026-10-11: no end date; stop = "dur"

# Istanbul local weekday windows "HH:MM" (inclusive slot starts), Mon-Fri.
SCHEDULED_REVIEW_WINDOW = ("08:30", "18:30")  # unchanged legacy scheduled_review rule
CHAIN_TICK_WINDOW = ("09:15", "18:30")
DAILY_DIGEST_WINDOW = ("09:00", "18:30")  # 09:00 run prepares (Claude research), sends at 09:15
DAILY_DIGEST_SEND_AT = "09:15"

# Data that also carries the date and must be re-accepted/re-imported separately
# when the deadline moves (not read from this module on purpose: they are records
# of a human decision or of provider state):
DATED_DATA = (
    "Firestore hamle_internal_v1/budget.expires_at (imported snapshot; WorkAuthorityStore budget expiry)",
    "outputs/gamechangers-internal/gamechangers-internal-catalog.json acceptance.expires_at, budget.expires_at",
    "outputs/gamechangers-internal/gamechangers-internal-quality-rubric.json expires_at",
    "outputs/gamechangers-internal/gamechangers-internal-work-authority-policy.json work_authority_policy.acceptance.expires_at",
    "outputs/Hamle_Butce_Onayi_2026-10-09.json ends_at (budget approval record)",
    "scheduler plans authorization_deadline + IAM condition timestamps",
)


def deadline_timestamp():
    return datetime.fromisoformat(AUTHORIZATION_DEADLINE).timestamp()


def start_timestamp():
    return datetime.fromisoformat(AUTHORIZATION_START).timestamp()


def window_minutes(window):
    start, end = (int(v[:2]) * 60 + int(v[3:]) for v in window)
    if not (len(window) == 2 and 0 <= start < end <= 23 * 60 + 59):
        raise ValueError("schedule_window_invalid")
    return start, end
