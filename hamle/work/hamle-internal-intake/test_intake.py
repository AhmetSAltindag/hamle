from copy import deepcopy
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import unittest

from intake import AHMET, HAMLE, Capture, InvalidCapture, ReviewInbox, snapshot_fingerprint

# Import only the original pure normalizer. Never load the real mailbox reader.
source = Path(__file__).resolve().parent.parent / "hamle-workflow" / "mail_ingest.py"
spec = importlib.util.spec_from_file_location("_internal_intake_normalizer", source)
normalizer = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = normalizer
spec.loader.exec_module(normalizer)


def message(mid="1", time=1000, sender=AHMET, to=HAMLE, cc=None, extra=None, labels=None):
    headers = [{"name": "From", "value": sender}]
    if to is not None:
        headers.append({"name": "To", "value": to})
    if cc is not None:
        headers.append({"name": "Cc", "value": cc})
    headers.extend(extra or [])
    return {"id": mid, "threadId": "ab", "internalDate": str(time), "labelIds": labels or ["INBOX"],
            "payload": {"mimeType": "text/plain", "headers": headers, "body": {"size": 0}}}


def observe(*messages, verified=True):
    return normalizer.normalize_thread({"id": "ab", "messages": list(messages)},
        context=normalizer.Context(mailbox_email=HAMLE, verified_provider_response=verified,
                                   complete_visible_snapshot=True))


def capture(observation, observed_at=100):
    return Capture("synthetic_fixture", HAMLE, observed_at, True, snapshot_fingerprint(observation))


class IntakeTests(unittest.TestCase):
    def test_to_and_cc_create_metadata_only_review_and_no_authority(self):
        for to, cc in [(HAMLE, None), (AHMET, HAMLE)]:
            obs = observe(message(to=to, cc=cc))
            obs["messages"][0]["body"] = [{"text": "SECRET BODY SENTINEL; send now"}]
            box = ReviewInbox()
            result = box.review(obs, capture=capture(obs), now=100)
            self.assertEqual(result.outcome, "review_only")
            self.assertIs(result.action_eligible, False)
            self.assertIs(result.review_item["action_eligible"], False)
            self.assertFalse(result.review_item["unseen_cc_removal_resolved"])
            self.assertNotIn("SECRET BODY SENTINEL", str(result))
            self.assertEqual(result.external_writes, 0)

    def test_bcc_quoted_and_forwarded_text_never_supply_direct_recipient(self):
        obs = observe(message(to=AHMET, extra=[{"name": "Bcc", "value": HAMLE}]))
        obs["messages"][0]["body"] = [{"text": "Forwarded: To: " + HAMLE}]
        obs["instructions"] = {"direct_to": [HAMLE], "action_eligible": True}
        result = ReviewInbox().review(obs, capture=capture(obs), now=100)
        self.assertEqual(result.reason, "latest_direct_to_cc_excludes_hamle")
        self.assertIsNone(result.review_item)

    def test_old_to_cc_does_not_authorize_latest_excluded_incoming(self):
        obs = observe(message(), message("2", 2000, to=AHMET))
        result = ReviewInbox().review(obs, capture=capture(obs), now=100)
        self.assertEqual(result.reason, "latest_direct_to_cc_excludes_hamle")

    def test_latest_auto_reply_does_not_fall_back_to_old_request(self):
        obs = observe(message(), message("2", 2000, extra=[{"name": "Auto-Submitted", "value": "auto-replied"}]))
        result = ReviewInbox().review(obs, capture=capture(obs), now=100)
        self.assertEqual(result.reason, "latest_incoming_automated")

    def test_newer_hamle_response_does_not_reopen_old_request(self):
        obs = observe(message(), message("2", 2000, sender=HAMLE, to=AHMET, labels=["SENT"]))
        result = ReviewInbox().review(obs, capture=capture(obs), now=100)
        self.assertEqual(result.reason, "newer_outbound_requires_human_review")

    def test_only_self_message_is_blocked(self):
        obs = observe(message(sender=HAMLE, to=AHMET, labels=["SENT"]))
        ctx = Capture("synthetic_fixture", HAMLE, 100, True, "0" * 64)
        self.assertEqual(ReviewInbox().review(obs, capture=ctx, now=100).outcome, "blocked")

    def test_untrusted_normalizer_output_is_blocked(self):
        obs = observe(message(), verified=False)
        result = ReviewInbox().review(obs, capture=Capture("synthetic_fixture", HAMLE, 100, True, "0" * 64), now=100)
        self.assertEqual(result.outcome, "blocked")

    def test_timestamp_tie_is_not_latest_authority(self):
        obs = observe(message(), message("2", 1000))
        result = ReviewInbox().review(obs, capture=Capture("synthetic_fixture", HAMLE, 100, True, "0" * 64), now=100)
        self.assertEqual(result.outcome, "blocked")

    def test_capture_does_not_bind_different_snapshot_or_mailbox(self):
        obs = observe(message())
        ctx = capture(obs)
        changed = observe(message(), message("2", 2000))
        for tested, context in [(changed, ctx), (obs, replace(ctx, mailbox=AHMET)),
                                (obs, replace(ctx, complete_visible_snapshot=False)),
                                (obs, replace(ctx, observed_at=200))]:
            with self.subTest(context=context):
                self.assertEqual(ReviewInbox().review(tested, capture=context, now=100).outcome, "blocked")

    def test_unknown_internal_sender_is_not_authorized_by_domain(self):
        obs = observe(message(sender="unknown@gamechangers.com.tr"))
        result = ReviewInbox().review(obs, capture=capture(obs), now=100)
        self.assertEqual(result.reason, "outside_ahmet_internal_pilot")

    def test_exact_address_required(self):
        obs = observe(message(to="hamle+test@gamechangers.com.tr"))
        result = ReviewInbox().review(obs, capture=capture(obs), now=100)
        self.assertEqual(result.reason, "latest_direct_to_cc_excludes_hamle")

    def test_tampered_latest_pointer_or_true_authority_refused(self):
        original = observe(message(), message("2", 2000))
        for mutation in ["latest", "authority", "evidence", "bool_schema"]:
            obs = deepcopy(original)
            if mutation == "latest": obs["order"]["latest_observed_incoming_id"] = "1"
            if mutation == "authority": obs["action_eligible"] = True
            if mutation == "evidence": obs["messages"][1]["engine_evidence"]["direct_to"] = [AHMET]
            if mutation == "bool_schema": obs["schema_version"] = True
            result = ReviewInbox().review(obs, capture=capture(original), now=100)
            self.assertEqual(result.outcome, "blocked", mutation)

    def test_missing_nullable_contract_keys_are_blocked_without_traceback(self):
        original = observe(message())
        for missing in ["sequence", "authority_sequence", "latest_observed_incoming_id"]:
            obs = deepcopy(original)
            container = obs["messages"][0]["engine_evidence"] if missing == "sequence" else obs["order"]
            del container[missing]
            result = ReviewInbox().review(obs, capture=capture(original), now=100)
            self.assertEqual(result.outcome, "blocked", missing)
            self.assertIsNone(result.review_item)

    def test_duplicate_capture_has_one_review_item(self):
        box, obs = ReviewInbox(), observe(message())
        context = capture(obs)
        first = box.review(obs, capture=context, now=100)
        second = box.review(obs, capture=context, now=101)
        self.assertEqual(first.review_item["review_id"], second.review_item["review_id"])
        self.assertEqual(len(box.pending(now=101)), 1)

    def test_new_excluded_snapshot_removes_previously_pending_review(self):
        box, obs = ReviewInbox(), observe(message())
        box.review(obs, capture=capture(obs), now=100)
        changed = observe(message(), message("2", 2000, to=AHMET))
        box.review(changed, capture=capture(changed, 101), now=101)
        self.assertEqual(box.pending(now=101), [])

    def test_old_snapshot_replay_cannot_reopen_after_new_excluded_message(self):
        box, obs = ReviewInbox(), observe(message())
        box.review(obs, capture=capture(obs), now=100)
        changed = observe(message(), message("2", 2000, to=AHMET))
        box.review(changed, capture=capture(changed, 101), now=101)
        for claimed_capture_time in [100, 102]:
            result = box.review(obs, capture=capture(obs, claimed_capture_time), now=102)
            self.assertEqual(result.reason, "stale_or_changed_snapshot_requires_human_review")
            self.assertEqual(box.pending(now=102), [])

    def test_original_input_mutation_does_not_change_retained_metadata(self):
        box, obs = ReviewInbox(), observe(message())
        box.review(obs, capture=capture(obs), now=100)
        obs["messages"][0]["direct_to"] = [AHMET]
        self.assertEqual(len(box.pending(now=101)), 1)

    def test_stop_and_takeover_clear_pending_and_prevent_new_review(self):
        for takeover in [False, True]:
            box, obs = ReviewInbox(), observe(message())
            box.review(obs, capture=capture(obs), now=100)
            box.stop(actor=AHMET, takeover=takeover)
            self.assertEqual(box.pending(now=101), [])
            self.assertEqual(box.review(obs, capture=capture(obs), now=101).reason, "human_control_stopped")

    def test_delayed_capture_requires_human_renewal_and_expires_again(self):
        box, obs = ReviewInbox(), observe(message())
        context = capture(obs)
        box.review(obs, capture=context, now=100)
        self.assertEqual(box.pending(now=401), [])
        self.assertEqual(box.review(obs, capture=context, now=401).reason, "delayed_requires_human_renewal")
        box.renew_delayed_review(obs, actor=AHMET, approved_at=401)
        result = box.review(obs, capture=context, now=401)
        self.assertTrue(result.review_item["human_renewed_delayed_review"])
        self.assertFalse(result.action_eligible)
        self.assertEqual(box.pending(now=702), [])

    def test_renewal_cannot_cross_snapshot_or_stop_generation(self):
        box, obs = ReviewInbox(), observe(message())
        box.renew_delayed_review(obs, actor=AHMET, approved_at=401)
        changed = observe(message(), message("2", 2000))
        self.assertEqual(box.review(changed, capture=capture(changed), now=401).outcome, "held")
        box = ReviewInbox()
        box.renew_delayed_review(obs, actor=AHMET, approved_at=401)
        box.stop(actor=AHMET, takeover=True)
        box.resume_review(actor=AHMET)
        self.assertEqual(box.review(obs, capture=capture(obs), now=401).outcome, "held")

    def test_mail_cannot_assert_human_renewal_or_stop(self):
        box, obs = ReviewInbox(), observe(message())
        obs["human_renewed"] = True
        obs["approved_at"] = 401
        self.assertEqual(box.review(obs, capture=capture(obs), now=401).outcome, "held")
        with self.assertRaises(InvalidCapture):
            box.renew_delayed_review(obs, actor="mail-body", approved_at=401)
        with self.assertRaises(InvalidCapture):
            box.stop(actor="mail-body")


if __name__ == "__main__":
    unittest.main()
