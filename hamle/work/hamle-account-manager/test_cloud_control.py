"""Offline WSGI authentication/CSRF/control tests; never calls a provider."""
import base64
import copy
from datetime import datetime, timezone
import hashlib
import io
import json
import re
import sys
import types
import unittest
from unittest.mock import patch

from cloud_control import (ALLOWED_EMAIL, COOKIE, IAP_CERTS_URL, IAP_ISSUER,
                           Config, create_app, google_iap_verifier)

ENV = {"IAP_AUDIENCE": "/projects/286533609595/locations/europe-west1/services/hamle-control",
       "PUBLIC_ORIGIN": "https://hamle-control-example.run.app"}
NOW = 1_791_586_800
SUBJECT = "accounts.google.com:1234567890"


def proposal_fixture(*, strategy=False):
    analysis = {"request_summary": "İç prova özeti", "intent": "routine_request", "known_facts": ["İç deneme"],
                "missing_questions": [], "brief": {"deliverables": ["İç görev"], "audience": None,
                "requested_deadline": None, "confirmed_deadline": None},
                "reply_draft": {"subject": "İç prova", "body": "Ahmet, iç prova hazır."},
                "task_draft": {"title": "İç görev", "description": "İç kapsam", "assignee": None, "due_at": None},
                "escalation": {"owner": "none", "reason": None}}
    if strategy:
        analysis.update(intent="complaint_strategy", escalation={"owner": "cansu_and_ahmet", "reason": "Strateji insan değerlendirmesi gerektiriyor."})
    payload = {"scope": "gamechangers_internal", "analysis": analysis,
               "reply": {"from": "hamle@gamechangers.com.tr", "to": [ALLOWED_EMAIL], "cc": [],
                         "subject": "İç prova", "body": "Ahmet, iç prova hazır.", "thread_id": "abc123",
                         "in_reply_to": "<internal@example.com>", "references": "<internal@example.com>"},
               "task": {"account_id": 5898865, "project_id": 49184053, "todo_parent_id": 1234, "todo_parent_type": "Todoset",
                        "title": "İç görev", "description": "İç kapsam", "assignee_id": 53358100, "due_at": None},
               "source": {"thread_id": "abc123", "message_id": "def456", "snapshot_fingerprint": "a" * 64,
                          "captured_at": "2026-10-10T02:00:00+03:00"}}
    if strategy:
        payload["reply"]["subject"] = "Re: HAMLE-IC-IS-20261010-01"
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return {"proposal_id": "p1", "proposal_hash": digest, "source_fingerprint": "a" * 64,
            "generation": 1, "status": "awaiting_human_review", "payload": payload}


def rehash(proposal):
    proposal["proposal_hash"] = hashlib.sha256(json.dumps(proposal["payload"], sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return proposal


def followup_fixture(*, intent="routine_request", inherited=True, cc=None):
    proposal = proposal_fixture()
    p = proposal["payload"]
    p.pop("task")
    p["analysis"]["intent"] = intent
    owner = {"routine_request": "none", "social_planning": "ozge_and_hamle", "unclear": "human_review",
             "commercial": "ahmet", "complaint_strategy": "cansu_and_ahmet"}[intent]
    p["analysis"]["escalation"] = {"owner": owner, "reason": None if owner == "none" else "İnsan değerlendirmesi"}
    p["reply"]["subject"] = "Re: HAMLE-IC-IS-20261010-01"
    p["reply"]["cc"] = [] if cc is None else cc
    p["work"] = {"work_id": "w-internal", "thread_id": "abc123", "task_id": 10392247471,
                 "account_id": 5898865, "project_id": 49184053, "version": 1,
                 "escalation_status": "open" if inherited else "not_required",
                 "escalation_owner": "cansu_and_ahmet" if inherited else "none"}
    p["comment"] = {"account_id": 5898865, "project_id": 49184053, "task_id": 10392247471,
                    "todo_parent_id": 10384126975, "todo_parent_type": "Todoset",
                    "body": "Aynı iç görev için yeni takip notu.", "task_fingerprint": "d" * 64}
    return rehash(proposal)


def tracked_work_fixture():
    work = copy.deepcopy(followup_fixture()["payload"]["work"])
    work.update(revision_count=1, pending_event_id="private-event-id", secret_debug="private-debug",
                latest_observation={"account_id": 5898865, "project_id": 49184053, "task_id": 10392247471,
                    "todo_parent_id": 10384126975, "todo_parent_type": "Todoset", "title": "Özel görev başlığı",
                    "description": "private-full-description", "status": "active", "completed": False,
                    "due_on": "2026-10-12", "assignee_ids": [53358100], "updated_at": "2026-10-10T13:00:00+03:00",
                    "task_fingerprint": "d" * 64, "observed_at": "2026-10-10T13:01:00+03:00",
                    "identity_verified": True})
    return work


def jwt_header(algorithm="ES256"):
    encoded = base64.urlsafe_b64encode(json.dumps({"alg": algorithm, "kid": "test-key"}).encode()).decode().rstrip("=")
    return encoded + ".payload.signature"


class FakeVerifier:
    def __init__(self):
        self.claims = {"iss": IAP_ISSUER, "aud": ENV["IAP_AUDIENCE"], "email": ALLOWED_EMAIL,
                       "sub": SUBJECT, "iat": NOW - 60, "exp": NOW + 540}
        self.calls = []
        self.failed = False

    def __call__(self, token, audience):
        self.calls.append((token, audience))
        if self.failed:
            raise ValueError("private provider error must not escape")
        return copy.deepcopy(self.claims)


class FakeStore:
    def __init__(self):
        self.calls = []
        self.state = {"control": {"mode": "review_open", "generation": 1}, "proposals": [],
                      "actions": [], "works": [], "budget": None, "external_actions_enabled": False,
                      "unseen_cc_removal_resolved": False}

    def public_state(self):
        return copy.deepcopy(self.state)

    def stop(self, actor):
        self.calls.append(("stop", actor))
        self.state["control"].update(mode="stopped", generation=2)

    def takeover(self, actor):
        self.calls.append(("takeover", actor))
        self.state["control"].update(mode="takeover", generation=2)

    def resume_review_only(self, actor):
        self.calls.append(("resume_review_only", actor))
        self.state["control"].update(mode="review_open", generation=3)

    def approve(self, *args, purpose):
        self.calls.append(("approve", *args, purpose))
        self.state["proposals"][0]["status"] = "approved"
        self.state["proposals"][0]["approval"] = {"purpose": purpose,
            "escalation_status": "open" if purpose == "internal_coordination_only" else "not_required",
            "decision_authorized": False, "production_authorized": False}


class CloudControlTests(unittest.TestCase):
    def setUp(self):
        self.verifier = FakeVerifier()
        self.store = FakeStore()
        self.now = NOW
        self.app = create_app(ENV, self.store, self.verifier, lambda: self.now)
        self.cookie = self.csrf = None

    def call(self, method="GET", path="/", data=None, extra=None, authenticated=True):
        raw = json.dumps(data).encode() if data is not None else b""
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": "",
               "HTTP_HOST": "hamle-control-example.run.app", "wsgi.input": io.BytesIO(raw),
               "CONTENT_LENGTH": str(len(raw)), "CONTENT_TYPE": "application/json"}
        if authenticated:
            env["HTTP_X_GOOG_IAP_JWT_ASSERTION"] = jwt_header()
        if self.cookie:
            env["HTTP_COOKIE"] = self.cookie
        if method == "POST":
            env["HTTP_ORIGIN"] = ENV["PUBLIC_ORIGIN"]
            env["HTTP_X_CSRF_TOKEN"] = self.csrf or ""
        env.update(extra or {})
        output = {}
        def start(status, headers):
            output.update(status=int(status.split()[0]), headers=dict(headers))
        output["body"] = b"".join(self.app(env, start))
        return output

    def open_panel(self):
        result = self.call()
        self.assertEqual(result["status"], 200)
        self.cookie = result["headers"]["Set-Cookie"].split(";", 1)[0]
        self.csrf = re.search(rb'name="csrf-token" content="([^"]+)"', result["body"]).group(1).decode()
        return result

    def test_missing_configuration_fails_before_store_or_network(self):
        for values in ({}, {"PUBLIC_ORIGIN": ENV["PUBLIC_ORIGIN"]}, {**ENV, "PUBLIC_ORIGIN": "http://localhost"},
                       {**ENV, "PUBLIC_ORIGIN": ENV["PUBLIC_ORIGIN"] + "/"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                create_app(values, self.store, self.verifier)
        self.assertEqual(self.verifier.calls, [])

    def test_legacy_email_header_without_signed_iap_is_rejected(self):
        result = self.call(authenticated=False, extra={"HTTP_X_GOOG_AUTHENTICATED_USER_EMAIL": "accounts.google.com:" + ALLOWED_EMAIL})
        self.assertEqual(result["status"], 401)
        self.assertEqual(self.store.calls, [])

    def test_signature_failure_is_sanitized(self):
        self.verifier.failed = True
        result = self.call()
        self.assertEqual(result["status"], 403)
        self.assertNotIn(b"private provider error", result["body"])

    def test_wrong_issuer_audience_email_subject_and_expiry_rejected(self):
        changes = {"iss": "https://accounts.google.com", "aud": "other", "email": "someone@example.com",
                   "sub": "", "exp": NOW - 31, "iat": NOW + 31}
        baseline = copy.deepcopy(self.verifier.claims)
        for key, value in changes.items():
            with self.subTest(key=key):
                self.verifier.claims = {**baseline, key: value}
                self.assertEqual(self.call()["status"], 403)
        self.verifier.claims = {**baseline, "exp": NOW + 1000}
        self.assertEqual(self.call()["status"], 403)
        self.assertEqual(self.store.calls, [])

    def test_non_es256_is_rejected_before_verifier(self):
        self.assertEqual(self.call(extra={"HTTP_X_GOOG_IAP_JWT_ASSERTION": jwt_header("none")})["status"], 403)
        self.assertEqual(self.verifier.calls, [])

    def test_optional_stable_subject_pin_enforced(self):
        self.app = create_app({**ENV, "IAP_ALLOWED_SUBJECT": "accounts.google.com:another"}, self.store, self.verifier, lambda: NOW)
        self.assertEqual(self.call()["status"], 403)

    def test_authenticated_html_defensive_cookie_csp(self):
        result = self.open_panel()
        cookie = result["headers"]["Set-Cookie"]
        for required in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(required, cookie)
        self.assertIn("frame-ancestors 'none'", result["headers"]["Content-Security-Policy"])
        self.assertEqual(result["headers"]["Cache-Control"], "no-store, max-age=0")

    def test_stop_and_takeover_write_verified_subject(self):
        self.open_panel()
        for action in ("stop", "takeover", "resume_review_only"):
            result = self.call("POST", "/api/control", {"action": action})
            self.assertEqual(result["status"], 200)
            self.assertEqual(self.store.calls[-1], (action, SUBJECT))

    def test_host_origin_csrf_missing_and_wrong_are_rejected(self):
        self.open_panel()
        for env in ({"HTTP_HOST": "evil.example"}, {"HTTP_ORIGIN": "https://evil.example"},
                    {"HTTP_ORIGIN": ""}, {"HTTP_X_CSRF_TOKEN": "wrong"},
                    {"HTTP_X_CSRF_TOKEN": "\xe9"}, {"HTTP_X_CSRF_TOKEN": ""},
                    {"HTTP_SEC_FETCH_SITE": "cross-site"}):
            with self.subTest(env=env):
                self.assertEqual(self.call("POST", "/api/control", {"action": "stop"}, env)["status"], 403)
        self.assertEqual(self.store.calls, [])

    def test_session_is_bound_to_signed_subject_and_restart(self):
        self.open_panel()
        self.verifier.claims["sub"] = "accounts.google.com:another"
        self.assertEqual(self.call("POST", "/api/control", {"action": "stop"})["status"], 403)
        self.verifier.claims["sub"] = SUBJECT
        self.app = create_app(ENV, self.store, self.verifier, lambda: self.now)
        self.assertEqual(self.call("POST", "/api/control", {"action": "stop"})["status"], 403)
        self.assertEqual(self.store.calls, [])

    def test_tampered_and_expired_session_fail(self):
        self.open_panel()
        self.cookie += "0"
        self.assertEqual(self.call("POST", "/api/control", {"action": "stop"})["status"], 403)
        self.open_panel()
        self.now += 1801
        self.verifier.claims.update(iat=self.now - 10, exp=self.now + 500)
        self.assertEqual(self.call("POST", "/api/control", {"action": "stop"})["status"], 403)
        self.assertEqual(self.store.calls, [])

    def test_get_and_unknown_actions_do_not_mutate(self):
        self.open_panel()
        self.assertEqual(self.call("GET", "/api/control")["status"], 405)
        for action in ("send", "run", "delete", "resume"):
            self.assertEqual(self.call("POST", "/api/control", {"action": action})["status"], 400)
        self.assertEqual(self.call("POST", "/api/control", {"action": "stop", "subject": "other"})["status"], 400)
        self.assertEqual(self.store.calls, [])

    def test_oversize_and_duplicate_json_rejected(self):
        self.open_panel()
        self.assertEqual(self.call("POST", "/api/control", {"action": "stop"}, {"CONTENT_LENGTH": "9999"})["status"], 413)
        raw = b'{"action":"stop","action":"takeover"}'
        self.assertEqual(self.call("POST", "/api/control", extra={"wsgi.input": io.BytesIO(raw), "CONTENT_LENGTH": str(len(raw))})["status"], 400)
        self.assertEqual(self.store.calls, [])

    def test_google_verifier_uses_official_fixed_key_endpoint_and_audience(self):
        calls = {}
        def verify(token, transport, **kwargs):
            calls.update(kwargs)
            calls["token"] = token
            transport(IAP_CERTS_URL, method="GET")
            return {"sub": SUBJECT}
        class Request:
            def __call__(self, url, **kwargs):
                calls["transport"] = (url, kwargs)
        modules = {
            "google.auth.transport.requests": types.SimpleNamespace(Request=Request),
            "google.oauth2.id_token": types.SimpleNamespace(verify_token=verify),
            "google.oauth2": types.SimpleNamespace(id_token=types.SimpleNamespace(verify_token=verify)),
        }
        with patch.dict(sys.modules, modules):
            self.assertEqual(google_iap_verifier("fake-token", ENV["IAP_AUDIENCE"]), {"sub": SUBJECT})
        self.assertEqual(calls["certs_url"], IAP_CERTS_URL)
        self.assertEqual(calls["audience"], ENV["IAP_AUDIENCE"])
        self.assertEqual(calls["clock_skew_in_seconds"], 30)
        self.assertEqual(calls["transport"][1]["timeout"], 10)

    def test_approval_requires_displayed_exact_hash_and_generation(self):
        proposal = proposal_fixture()
        self.store.state["proposals"] = [proposal]
        self.open_panel()
        data = {"action": "approve", "proposal_id": "p1", "proposal_hash": proposal["proposal_hash"], "generation": 1, "purpose": "routine_execution"}
        self.assertEqual(self.call("POST", "/api/control", data)["status"], 409)
        self.assertEqual(self.call("GET", "/api/state")["status"], 200)
        for changed in ({"proposal_hash": "b" * 64}, {"generation": 2}, {"proposal_id": "not-displayed"}):
            self.assertEqual(self.call("POST", "/api/control", {**data, **changed})["status"], 409)
        self.assertEqual(self.store.calls, [])
        result = self.call("POST", "/api/control", data)
        self.assertEqual(result["status"], 200)
        self.assertEqual(self.store.calls, [("approve", "p1", proposal["proposal_hash"], 1, SUBJECT, "routine_execution")])
        self.assertEqual(json.loads(result["body"])["proposals"][0]["status"], "approved")

    def test_strategy_requires_displayed_explicit_coordination_purpose(self):
        proposal = proposal_fixture(strategy=True)
        self.store.state["proposals"] = [proposal]
        html = self.open_panel()["body"].decode()
        self.assertIn("Yalnız iç koordinasyonu onayla", html)
        self.assertIn("Strateji, üretim, bütçe veya teslim kararı vermez", html)
        self.assertIn("Cansu ve Özge", html)
        state = json.loads(self.call("GET", "/api/state")["body"])
        self.assertEqual(state["proposals"][0]["available_approval_purpose"], "internal_coordination_only")
        self.assertTrue(state["proposals"][0]["human_escalation_open"])
        data = {"action": "approve", "proposal_id": "p1", "proposal_hash": proposal["proposal_hash"], "generation": 1}
        self.assertEqual(self.call("POST", "/api/control", data)["status"], 400)
        self.assertEqual(self.call("POST", "/api/control", {**data, "purpose": "routine_execution"})["status"], 409)
        self.assertEqual(self.call("POST", "/api/control", {**data, "purpose": "strategy_approved"})["status"], 400)
        self.assertEqual(self.store.calls, [])
        result = self.call("POST", "/api/control", {**data, "purpose": "internal_coordination_only"})
        self.assertEqual(result["status"], 200)
        p = json.loads(result["body"])["proposals"][0]
        self.assertEqual(p["recorded_approval_purpose"], "internal_coordination_only")
        self.assertTrue(p["human_escalation_open"])
        self.assertEqual(p["payload"], proposal["payload"])

    def test_coordination_purpose_cannot_be_used_for_a_routine_proposal(self):
        proposal = proposal_fixture()
        self.store.state["proposals"] = [proposal]
        self.open_panel(); self.call("GET", "/api/state")
        result = self.call("POST", "/api/control", {"action": "approve", "proposal_id": "p1",
            "proposal_hash": proposal["proposal_hash"], "generation": 1, "purpose": "internal_coordination_only"})
        self.assertEqual(result["status"], 409)
        self.assertEqual(self.store.calls, [])

    def test_followup_inherits_open_strategy_and_explicit_cc_without_approving_strategy(self):
        for intent in ("routine_request", "social_planning", "unclear", "complaint_strategy"):
            with self.subTest(intent=intent):
                self.store = FakeStore()
                fixture = followup_fixture(intent=intent, cc=["cansu@gamechangers.com.tr"])
                self.store.state["proposals"] = [fixture]
                self.app = create_app(ENV, self.store, self.verifier, lambda: self.now)
                self.open_panel()
                result = self.call("GET", "/api/state")
                self.assertEqual(result["status"], 200)
                p = json.loads(result["body"])["proposals"][0]
                self.assertEqual(p["payload"], fixture["payload"])
                self.assertNotIn("task", p["payload"])
                self.assertEqual(p["available_approval_purpose"], "internal_coordination_only")
                self.assertTrue(p["human_escalation_open"])
                data = {"action": "approve", "proposal_id": "p1", "proposal_hash": fixture["proposal_hash"],
                        "generation": 1, "purpose": "routine_execution"}
                self.assertEqual(self.call("POST", "/api/control", data)["status"], 409)
                self.assertEqual(self.store.calls, [])
                result = self.call("POST", "/api/control", {**data, "purpose": "internal_coordination_only"})
                self.assertEqual(result["status"], 200)
                self.assertEqual(self.store.state["proposals"][0]["approval"], {
                    "purpose": "internal_coordination_only", "escalation_status": "open",
                    "decision_authorized": False, "production_authorized": False})

    def test_followup_commercial_decision_stays_blocked_even_with_inherited_strategy(self):
        fixture = followup_fixture(intent="commercial")
        self.store.state["proposals"] = [fixture]
        self.open_panel()
        result = self.call("GET", "/api/state")
        self.assertEqual(result["status"], 200)
        self.assertIsNone(json.loads(result["body"])["proposals"][0]["available_approval_purpose"])
        for purpose in ("routine_execution", "internal_coordination_only"):
            self.assertEqual(self.call("POST", "/api/control", {"action": "approve", "proposal_id": "p1",
                "proposal_hash": fixture["proposal_hash"], "generation": 1, "purpose": purpose})["status"], 409)
        self.assertEqual(self.store.calls, [])

    def test_followup_routine_without_inherited_escalation_keeps_routine_purpose(self):
        fixture = followup_fixture(inherited=False)
        self.store.state["proposals"] = [fixture]
        self.open_panel()
        result = self.call("GET", "/api/state")
        self.assertEqual(result["status"], 200)
        p = json.loads(result["body"])["proposals"][0]
        self.assertEqual(p["available_approval_purpose"], "routine_execution")
        self.assertFalse(p["human_escalation_open"])

    def test_new_escalation_on_previously_routine_work_requires_separate_human_resolution(self):
        self.open_panel()
        for intent in ("complaint_strategy", "social_planning", "unclear", "commercial"):
            fixture = followup_fixture(intent=intent, inherited=False)
            self.store.state["proposals"] = [fixture]
            result = self.call("GET", "/api/state")
            self.assertEqual(result["status"], 200)
            p = json.loads(result["body"])["proposals"][0]
            self.assertIsNone(p["available_approval_purpose"])
            self.assertTrue(p["human_escalation_open"])
            self.assertEqual(self.call("POST", "/api/control", {"action": "approve", "proposal_id": "p1",
                "proposal_hash": fixture["proposal_hash"], "generation": 1,
                "purpose": "internal_coordination_only"})["status"], 409)
        self.assertEqual(self.store.calls, [])

    def test_followup_malformed_parent_work_binding_and_extra_actions_fail_even_with_correct_hash(self):
        self.open_panel()
        changes = (lambda p: p["comment"].update(todo_parent_type="Project"),
                   lambda p: p["comment"].update(todo_parent_id=True),
                   lambda p: p["comment"].update(todo_parent_id=0),
                   lambda p: p["comment"].update(task_id=1234),
                   lambda p: p["comment"].update(project_id=9999),
                   lambda p: p["comment"].update(task_fingerprint="bad"),
                   lambda p: p["work"].update(thread_id="bbbbbb"),
                   lambda p: p["work"].update(version=2),
                   lambda p: p["work"].update(escalation_owner="none"),
                   lambda p: p.update(task=proposal_fixture()["payload"]["task"]),
                   lambda p: p["comment"].update(url="https://evil.example"))
        for change in changes:
            fixture = followup_fixture()
            change(fixture["payload"])
            self.store.state["proposals"] = [rehash(fixture)]
            self.assertEqual(self.call("GET", "/api/state")["status"], 503)
        fixture = followup_fixture()
        fixture["payload"]["comment"]["body"] = "Changed after review hash"
        self.store.state["proposals"] = [fixture]
        self.assertEqual(self.call("GET", "/api/state")["status"], 503)
        self.assertEqual(self.store.calls, [])

    def test_explicit_internal_cc_is_visible_unknown_duplicate_and_header_injection_fail(self):
        self.open_panel()
        for fixture in (proposal_fixture(), followup_fixture()):
            fixture["payload"]["reply"]["cc"] = ["cansu@gamechangers.com.tr"]
            self.store.state["proposals"] = [rehash(fixture)]
            result = self.call("GET", "/api/state")
            self.assertEqual(result["status"], 200)
            self.assertEqual(json.loads(result["body"])["proposals"][0]["payload"]["reply"]["cc"], ["cansu@gamechangers.com.tr"])
        for cc in (["customer@example.com"], ["cansu@gamechangers.com.tr"] * 2,
                   ["cansu@gamechangers.com.tr\r\nBcc:other@example.com"], "cansu@gamechangers.com.tr", [ALLOWED_EMAIL]):
            self.store.state["proposals"] = [followup_fixture(cc=cc)]
            self.assertEqual(self.call("GET", "/api/state")["status"], 503)
        self.assertEqual(self.store.calls, [])

    def test_work_observation_projection_shows_status_without_private_metadata(self):
        self.store.state["works"] = [tracked_work_fixture()]
        html = self.open_panel()["body"].decode()
        for expected in ("Mevcut göreve eklenecek takip notu", "Son kontrol", "Cansu’nun CC’de bulunması",
                         "https://app.basecamp.com/5898865/buckets/49184053/todos/"):
            self.assertIn(expected, html)
        result = self.call("GET", "/api/state")
        self.assertEqual(result["status"], 200)
        work = json.loads(result["body"])["works"][0]
        self.assertEqual(work["revision_count"], 1)
        self.assertTrue(work["pending_escalation"])
        self.assertEqual(work["latest_observation"], {"status": "active", "completed": False,
            "due_on": "2026-10-12", "observed_at": "2026-10-10T13:01:00+03:00"})
        for private in (b"private-event-id", b"private-debug", b"private-full-description", b"assignee_ids"):
            self.assertNotIn(private, result["body"])

    def test_unverified_wrong_task_and_invalid_observation_fail_closed(self):
        self.open_panel()
        for patch_values in ({"identity_verified": False}, {"task_id": 9876}, {"completed": 1},
                             {"due_on": "2026-02-30"}, {"observed_at": "2026-10-10T13:00:00"},
                             {"status": "https://evil.example"}):
            work = tracked_work_fixture()
            work["latest_observation"].update(patch_values)
            self.store.state["works"] = [work]
            self.assertEqual(self.call("GET", "/api/state")["status"], 503)
        work = tracked_work_fixture()
        work["latest_observation"] = None
        self.store.state["works"] = [work]
        result = self.call("GET", "/api/state")
        self.assertEqual(result["status"], 200)
        self.assertIsNone(json.loads(result["body"])["works"][0]["latest_observation"])

    def test_all_approval_content_visible_but_private_store_fields_removed(self):
        proposal = proposal_fixture()
        proposal["secret_internal_debug"] = "must-not-escape"
        self.store.state["proposals"] = [proposal]
        self.store.state["control"]["actor_subject"] = "private-control-actor"
        self.open_panel()
        result = self.call("GET", "/api/state")
        self.assertEqual(result["status"], 200)
        returned = json.loads(result["body"])
        self.assertEqual(returned["proposals"][0]["payload"], proposal["payload"])
        self.assertNotIn(b"must-not-escape", result["body"])
        self.assertNotIn(b"private-control-actor", result["body"])

    def test_wrong_recipient_project_extra_payload_and_changed_hash_fail_closed(self):
        self.open_panel()
        for change in (lambda p: p["payload"]["reply"].update(to=["customer@example.com"]),
                       lambda p: p["payload"]["task"].update(project_id=1234),
                       lambda p: p["payload"]["task"].update(todo_parent_type="Other"),
                       lambda p: p["payload"].update(hidden_action="send elsewhere"),
                       lambda p: p["payload"]["reply"].update(body="Changed after hash")):
            proposal = proposal_fixture()
            change(proposal)
            self.store.state["proposals"] = [proposal]
            self.assertEqual(self.call("GET", "/api/state")["status"], 503)
        self.assertEqual(self.store.calls, [])

    def test_receipts_distinguish_accepted_verified_unknown_and_strip_lease_tokens(self):
        self.store.state["proposals"] = [proposal_fixture()]
        self.store.state["actions"] = [
            {"proposal_id": "p1", "kind": "gmail_reply", "state": "accepted", "provider_id": "a1b2", "token": "private-lease"},
            {"proposal_id": "p1", "kind": "basecamp_todo", "state": "verified", "provider_id": "123456"},
            {"proposal_id": "p1", "kind": "basecamp_comment", "state": "unknown", "provider_id": None}]
        self.open_panel()
        result = self.call("GET", "/api/state")
        self.assertEqual(result["status"], 200)
        self.assertEqual([a["state"] for a in json.loads(result["body"])["actions"]], ["accepted", "verified", "unknown"])
        self.assertNotIn(b"private-lease", result["body"])
        self.store.state["actions"][1]["provider_id"] = "https://evil.example"
        self.assertEqual(self.call("GET", "/api/state")["status"], 503)

    def test_real_operations_store_approval_then_takeover_cancels_and_resume_does_not_revive(self):
        from cloud_store import OperationsStore
        from test_cloud_store import MemoryBackend
        backend = MemoryBackend()
        store = OperationsStore(backend, clock=lambda: datetime.fromtimestamp(self.now, timezone.utc))
        store.initialize_control()
        generation = store.resume_review_only(SUBJECT)["generation"]
        fixture = proposal_fixture()
        fixture["source_fingerprint"] = "b" * 64
        self.assertNotEqual(fixture["source_fingerprint"], fixture["payload"]["source"]["snapshot_fingerprint"])
        store.put_proposal("p1", fixture["source_fingerprint"], fixture["payload"], generation)
        self.app = create_app(ENV, store, self.verifier, lambda: self.now)
        self.open_panel()
        self.assertEqual(self.call("GET", "/api/state")["status"], 200)
        result = self.call("POST", "/api/control", {"action": "approve", "proposal_id": "p1",
                           "proposal_hash": fixture["proposal_hash"], "generation": generation, "purpose": "routine_execution"})
        self.assertEqual(result["status"], 200)
        self.assertEqual(store.public_state()["proposals"][0]["approval"]["actor_subject"], SUBJECT)
        self.assertEqual(self.call("POST", "/api/control", {"action": "takeover"})["status"], 200)
        restart = OperationsStore(backend, clock=lambda: datetime.fromtimestamp(self.now, timezone.utc))
        self.assertEqual(restart.public_state()["control"]["mode"], "takeover")
        self.assertEqual(restart.public_state()["proposals"][0]["status"], "cancelled")
        self.assertEqual(self.call("POST", "/api/control", {"action": "resume_review_only"})["status"], 200)
        self.assertEqual(restart.public_state()["proposals"][0]["status"], "cancelled")

    def test_real_store_coordination_approval_keeps_escalation_open_and_exact_payload(self):
        from cloud_store import OperationsStore
        from test_cloud_store import MemoryBackend
        store = OperationsStore(MemoryBackend(), clock=lambda: datetime.fromtimestamp(self.now, timezone.utc))
        store.initialize_control()
        generation = store.resume_review_only(SUBJECT)["generation"]
        fixture = proposal_fixture(strategy=True)
        store.put_proposal("p1", fixture["source_fingerprint"], fixture["payload"], generation)
        self.app = create_app(ENV, store, self.verifier, lambda: self.now)
        self.open_panel(); self.call("GET", "/api/state")
        result = self.call("POST", "/api/control", {"action": "approve", "proposal_id": "p1",
            "proposal_hash": fixture["proposal_hash"], "generation": generation, "purpose": "internal_coordination_only"})
        self.assertEqual(result["status"], 200)
        p = store.public_state()["proposals"][0]
        self.assertEqual(p["payload"], fixture["payload"])
        self.assertEqual(p["approval"]["purpose"], "internal_coordination_only")
        self.assertEqual(p["approval"]["escalation_status"], "open")
        self.assertFalse(p["approval"]["decision_authorized"])
        self.assertFalse(p["approval"]["production_authorized"])

    def test_real_store_followup_work_observation_and_inherited_approval_match_panel(self):
        from cloud_store import OperationsStore, WORK_REF_FIELDS, action_id
        from test_cloud_store import MemoryBackend
        store = OperationsStore(MemoryBackend(), clock=lambda: datetime.fromtimestamp(self.now, timezone.utc))
        store.initialize_control()
        generation = store.resume_review_only(SUBJECT)["generation"]
        seed = proposal_fixture(strategy=True)
        seed["payload"]["task"]["todo_parent_id"] = 10384126975
        rehash(seed)
        store.put_proposal("p1", seed["source_fingerprint"], seed["payload"], generation)
        store.approve("p1", seed["proposal_hash"], generation, SUBJECT, purpose="internal_coordination_only")
        for kind, provider_id in (("basecamp_todo", "10392247471"), ("gmail_reply", "abc789")):
            aid = action_id("p1", kind)
            lease = store.claim_action(aid, "p1", kind, seed["proposal_hash"], "offline-worker", generation)
            store.begin_dispatch(lease, seed["source_fingerprint"])
            store.record_receipt(lease, provider_id)
            store.confirm_readback(aid, provider_id)
        work = store.bind_verified_work("p1")
        observation = tracked_work_fixture()["latest_observation"]
        observation["observed_at"] = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
        store.record_work_observation(work["work_id"], observation)
        fixture = followup_fixture(intent="social_planning", cc=["cansu@gamechangers.com.tr"])
        fixture["payload"]["work"] = {k: work[k] for k in WORK_REF_FIELDS}
        fixture["payload"]["source"]["message_id"] = "def789"
        claim = store.claim_followup(work["work_id"], "def789", "b" * 64, generation)
        p = store.put_followup_proposal(claim, fixture["payload"])
        self.app = create_app(ENV, store, self.verifier, lambda: self.now)
        self.open_panel()
        result = self.call("GET", "/api/state")
        self.assertEqual(result["status"], 200)
        state = json.loads(result["body"])
        self.assertEqual(state["works"][0]["task_id"], 10392247471)
        self.assertEqual(state["works"][0]["revision_count"], 0)
        self.assertFalse(state["works"][0]["latest_observation"]["completed"])
        result = self.call("POST", "/api/control", {"action": "approve", "proposal_id": p["proposal_id"],
            "proposal_hash": p["proposal_hash"], "generation": generation, "purpose": "internal_coordination_only"})
        self.assertEqual(result["status"], 200)
        saved = next(row for row in store.public_state()["proposals"] if row["proposal_id"] == p["proposal_id"])
        self.assertEqual(saved["approval"]["purpose"], "internal_coordination_only")
        self.assertEqual(saved["approval"]["escalation_status"], "open")
        self.assertEqual(store.get_work(work["work_id"])["revision_count"], 0)

    def test_pending_failure_is_visible_without_raw_diagnostics_or_provider_content(self):
        work = tracked_work_fixture()
        work["followup_status"] = {"state": "held", "reason": "response_incomplete", "attempt": 1,
                                   "debug": "PRIVATE-PROVIDER-CONTENT"}
        self.store.state["works"] = [work]
        self.open_panel()
        result = self.call("GET", "/api/state")
        self.assertEqual(result["status"], 200)
        state = json.loads(result["body"])
        self.assertEqual(state["works"][0]["followup_status"],
                         {"state": "held", "reason": "response_incomplete", "attempt": 1})
        self.assertNotIn("PRIVATE-PROVIDER-CONTENT", result["body"].decode())
        work["followup_status"]["reason"] = "PRIVATE-UNKNOWN-ERROR"
        result = self.call("GET", "/api/state")
        self.assertEqual(json.loads(result["body"])["works"][0]["followup_status"]["reason"], "human_review_required")
        self.assertNotIn("PRIVATE-UNKNOWN-ERROR", result["body"].decode())

    def test_real_es256_signature_verifies_and_tampering_is_rejected_offline(self):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        key = ec.generate_private_key(ec.SECP256R1())
        public_key = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        def encode(value):
            return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")
        header = encode({"alg": "ES256", "kid": "test-key"})
        payload = encode(self.verifier.claims)
        message = (header + "." + payload).encode()
        r, s = decode_dss_signature(key.sign(message, ec.ECDSA(hashes.SHA256())))
        signature = base64.urlsafe_b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode().rstrip("=")
        signed = header + "." + payload + "." + signature
        self.app = create_app(ENV, self.store, google_iap_verifier, lambda: self.now)
        now_naive = datetime.fromtimestamp(self.now, timezone.utc).replace(tzinfo=None)
        with patch("google.oauth2.id_token._fetch_certs", return_value={"test-key": public_key}), \
             patch("google.auth._helpers.utcnow", return_value=now_naive):
            self.assertEqual(self.call(extra={"HTTP_X_GOOG_IAP_JWT_ASSERTION": signed})["status"], 200)
            tampered = header + "." + encode({**self.verifier.claims, "iat": NOW - 59}) + "." + signature
            self.assertEqual(self.call(extra={"HTTP_X_GOOG_IAP_JWT_ASSERTION": tampered})["status"], 403)
        self.assertEqual(self.store.calls, [])


class WorkControlTests(unittest.TestCase):
    """Real authority/queue stores with offline transactions; no providers."""
    call = CloudControlTests.call
    open_panel = CloudControlTests.open_panel

    def setUp(self):
        from cloud_store import OperationsStore
        from test_cloud_store import MemoryBackend
        from v11_work_authority_store import WorkAuthorityStore
        self.now=NOW; self.verifier=FakeVerifier(); self.cookie=self.csrf=None
        self.backend=MemoryBackend()
        self.store=OperationsStore(self.backend, clock=lambda:datetime.fromtimestamp(self.now,timezone.utc))
        self.store.initialize_control(); self.generation=self.store.resume_review_only('offline-owner')['generation']
        self.authority=WorkAuthorityStore(self.store)
        self.work=self.authority.queue().bind_work(brand_id='wat_mobilite',thread_id='synthetic-thread',
            account_id=5898865,project_id=49184053,list_id=123,service_id='synthetic_authority_stop_probe',
            expected_generation=self.generation)
        self.wid=self.work['work_id']
        self.app=create_app(ENV,self.store,self.verifier,lambda:self.now)

    def displayed(self):
        self.open_panel()
        result=self.call('GET','/api/work-authority/'+self.wid)
        self.assertEqual(result['status'],200,result['body'])
        return json.loads(result['body'])['works'][0]

    def payload(self,row):
        return {'action':'takeover',**{k:row[k] for k in
                ('work_id','snapshot_sha256','work_generation','global_generation')}}

    def test_exact_and_list_read_are_sanitized_and_synthetic_is_clear(self):
        page=self.open_panel()
        self.assertIn('Sentetik deneme'.encode(),page['body'])
        result=self.call('GET','/api/work-authority')
        self.assertEqual(result['status'],200)
        row=json.loads(result['body'])['works'][0]
        self.assertEqual(row['work_id'],self.wid)
        self.assertTrue(row['synthetic'])
        self.assertFalse(row['customer_dispatch_enabled'])
        self.assertEqual(row['grant_status'],'not_granted')
        self.assertNotIn('thread_id',row)
        self.assertNotIn('grant',row)
        self.assertNotIn('source',row)
        self.assertEqual(self.call('GET','/api/work-authority/'+self.wid)['status'],200)
        self.assertEqual(self.call('GET','/api/work-authority/extra/'+self.wid)['status'],404)

    def test_seen_work_takeover_has_authenticated_actor_and_fresh_durable_receipt(self):
        from autonomy_policy import scope_digest
        row=self.displayed()
        result=self.call('POST','/api/work-control',self.payload(row))
        self.assertEqual(result['status'],200,result['body'])
        data=json.loads(result['body']); saved=data['works'][0]
        self.assertTrue(data['stop_receipt_verified'])
        self.assertEqual(saved['mode'],'takeover')
        self.assertEqual(saved['work_generation'],row['work_generation']+1)
        self.assertFalse(saved['lease_present'])
        actual=self.authority.observe_work(self.wid)
        stop=actual['record']['last_stop']
        self.assertEqual(stop['actor'],ALLOWED_EMAIL)
        self.assertEqual(stop['channel'],'authenticated_control_screen')
        self.assertEqual(stop['stop_id'],scope_digest({'work_id':self.wid,'actor':ALLOWED_EMAIL,
            'subject':SUBJECT,'evidence_ref':saved['last_stop_ref'],'mode':'takeover','verification_pending':False}))
        self.assertEqual(self.store.control()['generation'],self.generation)
        self.assertEqual(self.store.control()['mode'],'review_open')
        self.assertEqual(self.call('POST','/api/work-control',self.payload(row))['status'],409)

    def test_unseen_changed_and_stale_work_never_mutates(self):
        self.open_panel()
        from cloud_control import public_work_authority
        unseen=public_work_authority(self.authority.observe_work(self.wid))
        self.assertEqual(self.call('POST','/api/work-control',self.payload(unseen))['status'],409)
        row=json.loads(self.call('GET','/api/work-authority')['body'])['works'][0]
        changed=self.payload(row); changed['snapshot_sha256']='f'*64
        self.assertEqual(self.call('POST','/api/work-control',changed)['status'],409)
        self.store.stop('offline-global-stop')
        self.assertEqual(self.call('POST','/api/work-control',self.payload(row))['status'],409)
        self.assertEqual(self.authority.observe_work(self.wid)['work']['mode'],'active')

    def test_work_control_requires_iap_csrf_and_exact_fields(self):
        row=self.displayed(); data=self.payload(row)
        for extra in ({'HTTP_X_CSRF_TOKEN':'wrong'},{'HTTP_ORIGIN':'https://evil.invalid'},
                      {'HTTP_X_GOOG_IAP_JWT_ASSERTION':''}):
            self.assertIn(self.call('POST','/api/work-control',data,extra)['status'],(401,403))
        self.verifier.claims['email']='other@example.test'
        self.assertEqual(self.call('POST','/api/work-control',data)['status'],403)
        self.verifier.claims['email']=ALLOWED_EMAIL
        self.assertEqual(self.call('POST','/api/work-control',{**data,'actor':'other@example.test'})['status'],400)
        self.assertEqual(self.authority.observe_work(self.wid)['work']['mode'],'active')

    def test_fresh_read_after_global_stop_can_still_stop_one_work(self):
        self.store.stop('offline-global-stop')
        row=self.displayed()
        self.assertEqual(self.call('POST','/api/work-control',self.payload(row))['status'],200)
        self.assertEqual(self.store.control()['mode'],'stopped')

    def test_seen_work_expires_without_automatic_retry(self):
        row=self.displayed(); self.now+=301
        self.verifier.claims.update(iat=self.now-10,exp=self.now+500)
        self.assertEqual(self.call('POST','/api/work-control',self.payload(row))['status'],409)
        self.assertEqual(self.authority.observe_work(self.wid)['work']['mode'],'active')

    def test_readback_failure_does_not_show_success_or_repeat_stop(self):
        from v11_work_authority_store import WorkAuthorityStore
        row=self.displayed(); original=WorkAuthorityStore.observe_work; calls=[]
        def observe(store,wid):
            calls.append(wid)
            if len(calls)==2:
                raise ValueError('private provider detail')
            return original(store,wid)
        with patch.object(WorkAuthorityStore,'observe_work',observe):
            result=self.call('POST','/api/work-control',self.payload(row))
        self.assertEqual(result['status'],409)
        self.assertNotIn(b'private provider',result['body'])
        self.assertNotIn(b'stop_receipt_verified',result['body'])
        self.assertEqual(self.authority.observe_work(self.wid)['work']['mode'],'takeover')
        self.assertEqual(self.call('POST','/api/work-control',self.payload(row))['status'],409)


if __name__ == "__main__":
    unittest.main()
