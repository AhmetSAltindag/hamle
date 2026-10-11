"""Synthetic, offline evidence only; does not validate a live provider/model."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from autonomy_policy import ACTIONS, authorize_action, scope_digest


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)


def stamp(seconds=0):
    return (NOW + timedelta(seconds=seconds)).isoformat()


def fixture():
    scope = {
        "schema_version": 1, "brand_id": "wat_mobilite", "enabled": True,
        "authority": {"verified_by": "owner@example.test", "verified_at": stamp(-3600),
                      "expires_at": stamp(3600), "evidence_ref": "synthetic-contract-1"},
        "customer_contacts": ["client@example.test"],
        "agency_contacts": ["owner@example.test", "producer@example.test"],
        "allowed_cc_contacts": ["cc@example.test"], "agency_owner": "owner@example.test",
        "basecamp": {"account_id": 10, "project_id": 20, "list_ids": [30],
                     "customer_visible": False, "verification_ref": "synthetic-project-1"},
        "services": [{"service_id": "coordination", "actions": sorted(ACTIONS),
                      "revision_limit": 2, "list_id": 30, "assignee_ids": [40],
                      "immediate_action_window_seconds": 300,
                      "delivery": {"accepted_qa_policy_ref": "qa-v1", "approved_assets_ref": "assets-v1",
                                   "release_actors": ["producer@example.test"],
                                   "allowed_mime_types": ["application/pdf"]}}]}
    digest = scope_digest(scope)
    request = {
        "brand_id": "wat_mobilite", "service_id": "coordination", "work_id": "work-1",
        "action": "acknowledge", "action_state": "new", "risk_area": "routine",
        "open_escalation": False, "revision_index": 0,
        "control": {"global_mode": "routine_enabled", "work_mode": "active",
                    "global_generation": 2, "work_generation": 3,
                    "work_id": "work-1", "observed_at": stamp(-1)},
        "delegation": {"scope_digest": digest, "work_id": "work-1", "service_id": "coordination",
                       "brand_id": "wat_mobilite", "actor": "owner@example.test",
                       "evidence_ref": "synthetic-delegation-1", "issued_at": stamp(-600),
                       "expires_at": stamp(600), "revoked": False,
                       "global_generation": 2, "work_generation": 3,
                       "thread_id": "thread-1", "actions": sorted(ACTIONS)},
        "source": {"actual_latest": True, "own_or_automatic": False,
                   "provider_evidence_ref": "synthetic-gmail-read-1", "message_id": "message-1",
                   "thread_id": "thread-1", "fingerprint": "a" * 64,
                   "sender": "client@example.test", "to": ["hamle@gamechangers.com.tr"],
                   "cc": ["cc@example.test"], "received_at": stamp(-30), "captured_at": stamp(-1)},
        "reply_to": ["client@example.test"], "reply_cc": ["cc@example.test"],
        "basecamp_target": {"account_id": 10, "project_id": 20, "list_id": 30,
                            "customer_visible": False, "verification_ref": "synthetic-current-target",
                            "verified_at": stamp(-1), "task_id": 41, "task_fingerprint": "d" * 64},
        "work_binding": {"work_id": "work-1", "brand_id": "wat_mobilite", "service_id": "coordination",
                         "thread_id": "thread-1", "account_id": 10, "project_id": 20,
                         "list_id": 30, "task_id": 41},
        "action_payload": {"from": "hamle@gamechangers.com.tr", "to": ["client@example.test"],
                           "cc": ["cc@example.test"], "thread_id": "thread-1",
                           "subject": "Synthetic subject", "body": "Synthetic request received."}}
    request["prepared_action"] = {
        "action": "acknowledge", "work_id": "work-1", "brand_id": "wat_mobilite",
        "service_id": "coordination", "source_fingerprint": "a" * 64,
        "payload_sha256": scope_digest(request["action_payload"]),
        "task_id": 41, "task_fingerprint": "d" * 64}
    return scope, request


def select_action(request, action):
    request["action"] = action
    request["prepared_action"]["action"] = action
    if action == "create_task":
        for key in ("prepared_action", "basecamp_target", "work_binding"):
            request[key]["task_id"] = None
    if action in ("create_task", "update_task", "add_comment", "assign_task"):
        request["action_payload"] = {
            "target": {k: request["basecamp_target"][k] for k in ("account_id", "project_id", "list_id", "task_id")},
            "body": "Synthetic task content."}
        if action == "assign_task":
            request["assignee_id"] = 40
            request["action_payload"]["assignee_id"] = 40
    request["prepared_action"]["payload_sha256"] = scope_digest(request["action_payload"])


def delivery(request):
    select_action(request, "deliver_file")
    artifact = {"brand_id": "wat_mobilite", "work_id": "work-1", "version": "v1",
                "sha256": "b" * 64, "mime_type": "application/pdf"}
    request["artifact"] = artifact
    request["action_payload"]["artifact"] = artifact.copy()
    request["prepared_action"]["payload_sha256"] = scope_digest(request["action_payload"])
    request["qa"] = {**artifact, "accepted": True, "evidence_ref": "synthetic-qa",
                     "policy_ref": "qa-v1", "assets_ref": "assets-v1",
                     "internal_notes_excluded": True, "rights_checked": True,
                     "checked_at": stamp(-50)}
    request["release"] = {**artifact, "actor": "producer@example.test", "revoked": False,
                          "customer_delivery": True, "evidence_ref": "synthetic-release",
                          "issued_at": stamp(-40), "expires_at": stamp(300),
                          "reply_to": request["reply_to"].copy(), "reply_cc": request["reply_cc"].copy(),
                          "source_fingerprint": request["source"]["fingerprint"],
                          "service_id": request["service_id"], "scope_digest": request["delegation"]["scope_digest"],
                          "action_payload_sha256": request["prepared_action"]["payload_sha256"]}


def fresh_authority(scope, request):
    request["fresh_work_authorization"] = {
        "actor": "owner@example.test", "basis": "explicit_current_participation_confirmation",
        "hamle_participation_confirmed": True, "revoked": False,
        "evidence_ref": "synthetic-fresh-authority", "work_id": "work-1",
        "scope_digest": scope_digest(scope), "source_fingerprint": request["source"]["fingerprint"],
        "action": request["action"], "global_generation": 2, "work_generation": 3,
        "prepared_action_digest": scope_digest(request["prepared_action"]),
        "issued_at": stamp(-10), "expires_at": stamp(290),
        "artifact_sha256": request.get("artifact", {}).get("sha256"),
        "artifact_version": request.get("artifact", {}).get("version")}


class AutonomyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.scope, self.request = fixture()

    def result(self):
        return authorize_action(self.scope, self.request, now=NOW)

    def deny(self, reason):
        result = self.result()
        self.assertFalse(result["allowed"], result)
        self.assertEqual(result["reason"], reason)

    def test_valid_routine_is_only_a_pure_policy_decision(self):
        before = copy.deepcopy((self.scope, self.request))
        result = self.result()
        self.assertTrue(result["allowed"], result)
        self.assertFalse(result["live_enabled_by_this_module"])
        self.assertFalse(result["unseen_cc_removal_detected"])
        self.assertEqual(before, (self.scope, self.request))

    def test_each_scoped_action_accepts_matching_canonical_envelope(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                self.scope, self.request = fixture()
                if action == "deliver_file":
                    delivery(self.request)
                else:
                    select_action(self.request, action)
                self.assertTrue(self.result()["allowed"], self.result())

    def test_all_eight_templates_disabled_and_unpopulated(self):
        template = json.loads(Path(__file__).with_name("customer_scope_template.json").read_text())
        self.assertEqual(len(template["brands"]), 8)
        self.assertEqual(len({r["brand_id"] for r in template["brands"]}), 8)
        self.assertIn("ya_da", [r["brand_id"] for r in template["brands"]])
        self.assertIn("multicosmetics", [r["brand_id"] for r in template["brands"]])
        for scope in template["brands"]:
            self.assertFalse(scope["enabled"])
            self.assertIsNone(scope["authority"])
            self.assertIsNone(scope["services"])
            self.assertFalse(authorize_action(scope, self.request, now=NOW)["allowed"])

    def test_missing_scope(self):
        self.scope = None
        self.deny("scope_missing")

    def test_unverified_contact_scope(self):
        self.scope["customer_contacts"] = None
        self.deny("contact_list_invalid")

    def test_invalid_revision_limit(self):
        self.scope["services"][0]["revision_limit"] = True
        self.deny("revision_limit_missing")

    def test_expired_scope(self):
        self.scope["authority"]["expires_at"] = stamp(-1)
        self.deny("scope_authority_expired")

    def test_different_brand_cannot_reuse_scope(self):
        self.request["brand_id"] = "rabam"
        self.deny("brand_scope_mismatch")

    def test_basecamp_cross_brand_target(self):
        select_action(self.request, "create_task")
        self.request["basecamp_target"]["project_id"] = 21
        self.deny("basecamp_target_mismatch")

    def test_customer_visibility_change(self):
        select_action(self.request, "add_comment")
        self.request["basecamp_target"]["customer_visible"] = True
        self.deny("basecamp_target_mismatch")

    def test_unverified_cc_is_held_not_dropped(self):
        self.request["source"]["cc"].append("unknown@example.test")
        self.deny("latest_recipient_out_of_scope")

    def test_known_cc_must_be_preserved(self):
        self.request["reply_cc"] = []
        self.deny("reply_all_recipients_mismatch")

    def test_historical_cc_is_not_current_authority(self):
        self.request["source"]["to"] = ["owner@example.test"]
        self.request["source"]["quoted_cc"] = ["hamle@gamechangers.com.tr"]
        self.deny("hamle_absent_from_actual_latest_to_cc")

    def test_wrong_thread_cannot_reuse_work_delegation(self):
        self.request["source"]["thread_id"] = "thread-2"
        self.deny("latest_source_unverified")

    def test_own_or_automatic_not_new_request(self):
        self.request["source"]["own_or_automatic"] = True
        self.deny("latest_source_unverified")

    def test_stop_and_per_work_takeover(self):
        for key, value in (("global_mode", "stopped"), ("work_mode", "takeover")):
            with self.subTest(key=key):
                self.scope, self.request = fixture()
                self.request["control"][key] = value
                self.deny("human_stop_or_takeover")

    def test_restarted_work_rejects_old_generation(self):
        self.request["control"]["work_generation"] += 1
        self.deny("delegation_generation_stale")

    def test_changed_policy_invalidates_delegation(self):
        self.scope["services"][0]["revision_limit"] = 3
        self.deny("work_delegation_invalid")

    def test_expired_delegation(self):
        self.request["delegation"]["expires_at"] = stamp(-1)
        self.deny("work_delegation_expired")

    def test_revision_count_above_contract(self):
        self.request["revision_index"] = 3
        self.deny("revision_limit_exceeded_or_unknown")

    def test_risky_intents_require_existing_owners(self):
        cases = {"commercial": "ahmet_commercial_decision_required",
                 "complaint_strategy": "cansu_and_ahmet_decision_required",
                 "social_planning": "ozge_collaboration_required",
                 "unclear": "intent_requires_human_review"}
        for risk, reason in cases.items():
            with self.subTest(risk=risk):
                self.request["risk_area"] = risk
                self.deny(reason)

    def test_routine_label_cannot_close_existing_escalation(self):
        self.request["open_escalation"] = True
        self.deny("open_escalation_blocks_routine")

    def test_long_delayed_send_cannot_use_fresh_mailbox_poll_alone(self):
        self.request["source"]["received_at"] = stamp(-86_400)
        self.request["source"]["captured_at"] = stamp()
        self.deny("delayed_action_requires_fresh_work_authorization")

    def test_delayed_basecamp_write_requires_same_fresh_authority(self):
        select_action(self.request, "update_task")
        self.request["source"]["received_at"] = stamp(-3600)
        self.deny("delayed_action_requires_fresh_work_authorization")

    def test_explicit_fresh_authority_allows_delayed_action(self):
        self.request["source"]["received_at"] = stamp(-3600)
        fresh_authority(self.scope, self.request)
        self.assertTrue(self.result()["allowed"])

    def test_fresh_authority_has_short_expiry(self):
        self.request["source"]["received_at"] = stamp(-3600)
        fresh_authority(self.scope, self.request)
        self.request["fresh_work_authorization"]["issued_at"] = stamp(-301)
        self.deny("fresh_work_authorization_stale")

    def test_delayed_delivery_authority_is_bound_to_exact_version(self):
        delivery(self.request)
        self.request["source"]["received_at"] = stamp(-3600)
        fresh_authority(self.scope, self.request)
        self.request["fresh_work_authorization"]["artifact_version"] = "v0"
        self.deny("fresh_authority_artifact_mismatch")

    def test_delivery_needs_actual_qa_and_release(self):
        delivery(self.request)
        del self.request["qa"]
        self.deny("delivery_evidence_missing")

    def test_accepted_delivery_version(self):
        delivery(self.request)
        self.assertTrue(self.result()["allowed"])

    def test_changed_artifact_rejects_previous_qa(self):
        delivery(self.request)
        self.request["artifact"]["sha256"] = "c" * 64
        self.deny("artifact_changed_after_qa_or_release")

    def test_internal_notes_and_rights_check_required(self):
        delivery(self.request)
        self.request["qa"]["internal_notes_excluded"] = False
        self.deny("artifact_qa_not_accepted")

    def test_unknown_send_state_never_retries(self):
        for state in ("unknown", "dispatch_started", "verified"):
            with self.subTest(state=state):
                self.request["action_state"] = state
                self.deny("action_already_started_or_unknown")

    def test_malformed_nested_evidence_holds(self):
        self.request["source"]["cc"] = [{}]
        self.deny("malformed_authority_evidence")

    def test_control_from_different_work_is_rejected(self):
        self.request["control"]["work_id"] = "work-2"
        self.deny("control_work_mismatch")

    def test_old_control_observation_is_rejected(self):
        self.request["control"]["observed_at"] = stamp(-61)
        self.deny("control_observation_stale")

    def test_changed_source_after_preparation_is_rejected(self):
        self.request["source"]["fingerprint"] = "e" * 64
        self.deny("prepared_action_source_or_scope_changed")

    def test_changed_outgoing_body_after_preparation_is_rejected(self):
        self.request["action_payload"]["body"] = "Different unreviewed proposal."
        self.deny("prepared_action_payload_changed")

    def test_existing_task_identity_must_match_binding(self):
        select_action(self.request, "update_task")
        self.request["basecamp_target"]["task_id"] = 999
        self.deny("task_identity_mismatch")

    def test_existing_task_semantic_change_invalidates_preparation(self):
        select_action(self.request, "add_comment")
        self.request["basecamp_target"]["task_fingerprint"] = "f" * 64
        self.deny("task_changed_after_preparation")

    def test_new_task_cannot_replace_existing_task(self):
        select_action(self.request, "create_task")
        self.request["work_binding"]["task_id"] = 41
        self.deny("work_already_has_task_or_binding_missing")

    def test_delivery_release_does_not_cover_new_recipient(self):
        delivery(self.request)
        self.request["release"]["reply_cc"] = []
        self.deny("artifact_release_destination_or_scope_mismatch")

    def test_fresh_authority_cannot_cover_another_prepared_note(self):
        self.request["source"]["received_at"] = stamp(-3600)
        fresh_authority(self.scope, self.request)
        self.request["action_payload"]["body"] = "Second note."
        self.request["prepared_action"]["payload_sha256"] = scope_digest(self.request["action_payload"])
        self.deny("fresh_authority_prepared_action_mismatch")

    def test_executable_mail_cannot_hide_unlisted_recipient(self):
        self.request["action_payload"]["to"] = ["outsider@example.test"]
        self.request["prepared_action"]["payload_sha256"] = scope_digest(self.request["action_payload"])
        self.deny("executable_mail_envelope_mismatch")

    def test_executable_mail_cannot_add_bcc(self):
        self.request["action_payload"]["bcc"] = ["outsider@example.test"]
        self.request["prepared_action"]["payload_sha256"] = scope_digest(self.request["action_payload"])
        self.deny("executable_mail_envelope_mismatch")

    def test_executable_task_cannot_escape_checked_project(self):
        select_action(self.request, "add_comment")
        self.request["action_payload"]["target"]["project_id"] = 999
        self.request["prepared_action"]["payload_sha256"] = scope_digest(self.request["action_payload"])
        self.deny("executable_task_envelope_mismatch")

    def test_executable_assignment_cannot_escape_checked_assignee(self):
        select_action(self.request, "assign_task")
        self.request["action_payload"]["assignee_id"] = 999
        self.request["prepared_action"]["payload_sha256"] = scope_digest(self.request["action_payload"])
        self.deny("executable_assignee_mismatch")

    def test_executable_delivery_cannot_replace_checked_artifact(self):
        delivery(self.request)
        self.request["action_payload"]["artifact"]["sha256"] = "f" * 64
        digest = scope_digest(self.request["action_payload"])
        self.request["prepared_action"]["payload_sha256"] = digest
        self.request["release"]["action_payload_sha256"] = digest
        self.deny("executable_artifact_mismatch")


if __name__ == "__main__":
    unittest.main()
