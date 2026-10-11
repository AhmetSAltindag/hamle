import unittest
from datetime import date, datetime, timedelta, timezone

from schedule import day_plan, next_slot, slot_key, slots_for_day, summary_intent


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.slots = slots_for_day(date(2026, 10, 12))

    def test_exact_21_inclusive_slots(self):
        self.assertEqual(len(self.slots), 21)
        self.assertEqual(len(set(map(slot_key, self.slots))), 21)
        self.assertEqual(self.slots[0].isoformat(), "2026-10-12T08:30:00+03:00")
        self.assertEqual(self.slots[-1].isoformat(), "2026-10-12T18:30:00+03:00")
        self.assertTrue(all(b-a == timedelta(minutes=30) for a, b in zip(self.slots, self.slots[1:])))

    def test_weekend_empty_and_friday_goes_to_monday(self):
        self.assertEqual(slots_for_day(date(2026, 10, 10)), ())
        self.assertEqual(slots_for_day(date(2026, 10, 11)), ())
        self.assertEqual(next_slot(datetime.fromisoformat("2026-10-09T18:30:00+03:00")), self.slots[0])

    def test_utc_instant_has_same_slot_key(self):
        utc_slot = self.slots[0].astimezone(timezone.utc)
        self.assertEqual(slot_key(utc_slot), slot_key(self.slots[0]))
        self.assertEqual(next_slot(utc_slot), self.slots[1])

    def test_naive_and_non_slot_rejected(self):
        for slot in (datetime(2026, 10, 12, 8, 30), self.slots[0] - timedelta(minutes=30),
                     self.slots[-1] + timedelta(minutes=30), self.slots[0] + timedelta(seconds=1)):
            with self.assertRaises(ValueError):
                slot_key(slot)

    def test_holidays_are_explicitly_unknown(self):
        plan = day_plan(date(2026, 10, 29))
        self.assertEqual(plan["holiday_policy"], "unknown")
        self.assertFalse(plan["holiday_calendar_connected"])
        self.assertFalse(plan["scheduler_installed"])
        self.assertEqual(len(plan["slots"]), 21)

    def test_successful_0830_creates_only_unsent_intent(self):
        intent = summary_intent(self.slots[0], outcome="success", completed_at=self.slots[0]+timedelta(minutes=4))
        self.assertEqual(intent["idempotency_key"], "hamle-morning-summary:2026-10-12")
        self.assertEqual(intent["state"], "intent_only")
        self.assertFalse(intent["send_authorized"])
        self.assertEqual(intent["external_writes"], 0)

    def test_failed_unknown_and_later_slot_do_not_create_summary(self):
        for outcome, slot in (("failed", self.slots[0]), ("unknown", self.slots[0]), ("success", self.slots[1])):
            self.assertIsNone(summary_intent(slot, outcome=outcome, completed_at=slot+timedelta(minutes=1)))

    def test_retry_uses_same_daily_key_and_recorded_key_is_suppressed(self):
        first = summary_intent(self.slots[0], outcome="success", completed_at=self.slots[0]+timedelta(minutes=2))
        retry = summary_intent(self.slots[0], outcome="success", completed_at=self.slots[0]+timedelta(minutes=10))
        self.assertEqual(first["idempotency_key"], retry["idempotency_key"])
        self.assertIsNone(summary_intent(self.slots[0], outcome="success", completed_at=self.slots[0]+timedelta(minutes=10),
                                        existing_intent_keys={first["idempotency_key"]}))

    def test_stale_or_impossible_completion(self):
        self.assertIsNone(summary_intent(self.slots[0], outcome="success", completed_at=self.slots[0]+timedelta(days=1)))
        with self.assertRaisesRegex(ValueError, "completion_before_slot"):
            summary_intent(self.slots[0], outcome="success", completed_at=self.slots[0]-timedelta(seconds=1))


if __name__ == "__main__":
    unittest.main()
