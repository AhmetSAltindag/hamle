from copy import deepcopy
import json
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import basecamp_cloud_auth as app
from test_cloud_providers import Response, Session, FAKE_TOKEN
import test_cloud_providers as provider_tests

DEVICE = "synthetic-new-device-code-not-real"
REFRESH = "synthetic-new-refresh-token-not-real"
ROTATED = "synthetic-rotated-refresh-token-not-real"


def discovery():
    return [Response({"resource": app.RESOURCE_ORIGIN, "authorization_servers": [app.ISSUER]}),
        Response({"issuer": app.ISSUER, "token_endpoint": app.TOKEN_ENDPOINT,
                  "device_authorization_endpoint": app.DEVICE_ENDPOINT,
                  "grant_types_supported": [app.DEVICE_GRANT, "refresh_token"],
                  "token_endpoint_auth_methods_supported": ["none"], "scopes_supported": ["full"]})]


def device_response():
    return Response({"device_code": DEVICE, "user_code": "ABCD-EFGH", "expires_in": 900, "interval": 5,
                     "verification_uri": app.ISSUER + "/oauth/device_verification",
                     "verification_uri_complete": app.ISSUER + "/oauth/device_verification?user_code=ABCD-EFGH"})


def token_response(refresh=REFRESH):
    return {"access_token": FAKE_TOKEN, "refresh_token": refresh, "expires_in": 3600,
            "token_type": "Bearer", "scope": "full", "resource": "urn:bc:account:5898865"}


def record():
    return app._token_record(token_response(), 1000)


def verification(*, identity=app.IDENTITY):
    return [Response({"identity": {"id": identity, "email_address": app.provider.HAMLE},
                      "accounts": [{"id": app.ACCOUNT, "product": "bc3", "href": f"{app.RESOURCE_ORIGIN}/{app.ACCOUNT}"}]}),
            *provider_tests.ProviderTests().basecamp_session().responses]


class Secrets:
    def __init__(self, value=None, fail_save=False, stale=False):
        self.value = value or record(); self.writes = []; self.fail_save = fail_save; self.stale = stale
    def access(self, name):
        assert name == app.provider.BASECAMP_SECRET
        return json.dumps(self.value).encode()
    def add_basecamp_version(self, value):
        if self.fail_save: raise RuntimeError(FAKE_TOKEN)
        self.writes.append(deepcopy(value))
        if not self.stale: self.value = deepcopy(value)
        return "projects/gamechangers-hamle/secrets/hamle-basecamp-credential/versions/2"


class BasecampAuthTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pending = Path(self.temp.name) / "pending.json"
        self.credential = Path(self.temp.name) / "credential.json"
    def error(self, code, function):
        with self.assertRaises(app.AuthError) as got: function()
        self.assertEqual(got.exception.code, code)
        self.assertFalse(got.exception.retry_allowed)
        self.assertNotIn(FAKE_TOKEN, str(got.exception))
    def begin(self, session=None):
        return app.begin_device_login(pending_path=self.pending, credential_path=self.credential,
            session=session or Session(*discovery(), device_response()), clock=lambda: 1000)
    def poll(self, session, now=1006):
        return app.poll_device_once(pending_path=self.pending, credential_path=self.credential,
                                   session=session, clock=lambda: now)

    def test_begin_uses_new_device_flow_without_existing_credentials(self):
        session = Session(*discovery(), device_response())
        result = self.begin(session)
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertNotIn("device_code", result)
        self.assertNotIn(DEVICE, json.dumps(result))
        pending = json.loads(self.pending.read_text())
        self.assertEqual(pending["device_code"], DEVICE)
        self.assertEqual(stat.S_IMODE(self.pending.stat().st_mode), 0o600)
        form = parse_qs(session.calls[-1][2]["data"].decode())
        self.assertEqual(form, {"client_id": ["basecamp-cli"], "scope": ["full"], "login_hint": [app.provider.HAMLE]})
        self.assertFalse(session.calls[-1][2]["allow_redirects"])

    def test_existing_bootstrap_is_not_recreated(self):
        self.begin(); session = Session()
        self.error("cloud_bootstrap_already_exists", lambda: self.begin(session))
        self.assertEqual(session.calls, [])

    def test_discovery_rejects_wrong_issuer_before_device_post(self):
        responses = discovery()
        responses[0] = Response({"resource": app.RESOURCE_ORIGIN, "authorization_servers": ["https://evil.example"]})
        session = Session(*responses)
        self.error("oauth_resource_binding_mismatch", lambda: self.begin(session))
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(self.pending.exists())

    def test_approval_uri_must_be_fixed_host_and_bound_user_code(self):
        bad = device_response()
        payload = json.loads(bad.raw); payload["verification_uri_complete"] = "https://evil.example/?user_code=ABCD-EFGH"
        self.error("approval_url_invalid", lambda: self.begin(Session(*discovery(), Response(payload))))
        self.assertEqual(json.loads(self.pending.read_text())["state"], "device_request_started")

    def test_poll_interval_and_issuance_deadline_prevent_early_or_expired_call(self):
        self.begin(); session = Session()
        self.assertEqual(self.poll(session, now=1002)["status"], "waiting_interval")
        self.assertEqual(session.calls, [])
        self.error("device_code_expired", lambda: self.poll(session, now=1900))
        self.assertEqual(session.calls, [])

    def test_authorization_pending_is_explicit_protocol_poll_not_http_retry(self):
        self.begin(); session = Session(Response({"error": "authorization_pending"}, status=400))
        result = self.poll(session)
        self.assertEqual(result, {"status": "awaiting_approval", "wait_seconds": 5})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(json.loads(self.pending.read_text())["state"], "awaiting_approval")

    def test_slow_down_increases_next_poll_interval(self):
        self.begin(); session = Session(Response({"error": "slow_down"}, status=400))
        self.assertEqual(self.poll(session)["wait_seconds"], 10)
        self.assertEqual(json.loads(self.pending.read_text())["next_poll_at"], 1016)

    def test_unknown_poll_result_blocks_future_replay(self):
        self.begin(); session = Session(RuntimeError(FAKE_TOKEN))
        self.error("oauth_outcome_unknown", lambda: self.poll(session))
        self.error("device_outcome_requires_review", lambda: self.poll(Session()))
        self.assertEqual(len(session.calls), 1)

    def test_success_verifies_oauth_identity_account_and_private_project_before_save(self):
        self.begin(); session = Session(Response(token_response()), *verification())
        result = self.poll(session)
        self.assertEqual(result["status"], "cloud_credential_verified_locally")
        self.assertFalse(result["cloud_secret_uploaded"])
        self.assertEqual(result["identity_id"], 28787068)
        self.assertEqual(result["person_id"], 53358100)
        self.assertNotIn(FAKE_TOKEN, json.dumps(result))
        saved = app.validate_record(json.loads(self.credential.read_text()))
        self.assertEqual(saved["refresh_token"], REFRESH)
        self.assertEqual(saved["resource"], "urn:bc:account:5898865")
        self.assertEqual(stat.S_IMODE(self.credential.stat().st_mode), 0o600)
        self.assertNotIn("device_code", json.loads(self.pending.read_text()))
        self.assertEqual(len(session.calls), 7)

    def test_wrong_login_identity_never_saves_credential(self):
        self.begin(); session = Session(Response(token_response()), *verification(identity=1))
        self.error("basecamp_oauth_identity_mismatch", lambda: self.poll(session))
        self.assertFalse(self.credential.exists())
        pending = json.loads(self.pending.read_text())
        self.assertEqual(pending["state"], "received_unverified")
        self.assertEqual(pending["candidate"]["refresh_token"], REFRESH)
        self.assertEqual(pending["candidate"]["refresh_state"], "unverified")

    def test_received_candidate_recovery_only_gets_and_preserves_on_mismatch(self):
        self.begin()
        self.error("basecamp_oauth_identity_mismatch", lambda: self.poll(Session(Response(token_response()), *verification(identity=1))))
        session = Session(*verification())
        result = app.recover_candidate(pending_path=self.pending, credential_path=self.credential,
                                       session=session, clock=lambda: 1010)
        self.assertEqual(result["oauth_token_posts"], 0)
        self.assertEqual([m for m, _, _ in session.calls], ["GET"] * 6)
        self.assertEqual(json.loads(self.credential.read_text())["refresh_state"], "ready")

    def test_different_scope_resource_or_missing_refresh_rejected(self):
        for key, value, code in (("scope", "read", "token_scope_or_type_mismatch"),
                                 ("resource", "urn:bc:account:999", "token_resource_mismatch"),
                                 ("refresh_token", None, "oauth_token_invalid")):
            raw = token_response(); raw[key] = value
            self.error(code, lambda: app._token_record(raw, 1000))

    def test_live_token_has_no_refresh_or_claim_side_effect(self):
        secrets, session = Secrets(), Session()
        with patch.object(app.provider, "_session", side_effect=AssertionError("Injected session only")):
            self.assertEqual(app.access_token(secrets, session=session, clock=lambda: 1000), FAKE_TOKEN)
        self.assertEqual(session.calls, [])
        self.assertEqual(secrets.writes, [])

    def test_expired_token_requires_atomic_permanent_claim(self):
        secrets, session = Secrets(), Session()
        self.error("durable_refresh_claim_required", lambda: app.access_token(secrets, session=session, clock=lambda: 4590))
        self.assertEqual(session.calls, [])
        self.assertEqual(secrets.writes, [])

    def test_successful_refresh_persists_started_and_new_rotation_before_return(self):
        secrets = Secrets()
        session = Session(Response(token_response(ROTATED)), *verification())
        claims = []
        def claim(identifier): claims.append(identifier); return True
        self.assertEqual(app.access_token(secrets, claim_refresh=claim, session=session, clock=lambda: 4590), FAKE_TOKEN)
        self.assertEqual([r["refresh_state"] for r in secrets.writes], ["started", "unverified", "ready"])
        self.assertEqual(secrets.value["refresh_token"], ROTATED)
        self.assertEqual(len(claims), 1)
        self.assertEqual(len(claims[0]), 64)
        self.assertNotIn(REFRESH, claims[0])
        form = parse_qs(session.calls[0][2]["data"].decode())
        self.assertEqual(form["resource"], ["urn:bc:account:5898865"])
        self.assertEqual(form["client_id"], ["basecamp-cli"])

    def test_same_stale_grant_cannot_refresh_twice_even_with_stale_secret_read(self):
        secrets, session, claims = Secrets(stale=True), Session(RuntimeError(FAKE_TOKEN)), set()
        def claim(identifier):
            if identifier in claims: return False
            claims.add(identifier); return True
        self.error("oauth_outcome_unknown", lambda: app.access_token(secrets, claim_refresh=claim, session=session, clock=lambda: 4590))
        self.error("durable_refresh_claim_required", lambda: app.access_token(secrets, claim_refresh=claim, session=session, clock=lambda: 4590))
        self.assertEqual(len(session.calls), 1)

    def test_started_secret_blocks_retry_without_token_post(self):
        value = {**record(), "refresh_state": "started"}; session = Session()
        self.error("refresh_outcome_requires_review", lambda: app.access_token(Secrets(value), claim_refresh=lambda _: True, session=session, clock=lambda: 4590))
        self.assertEqual(session.calls, [])

    def test_rotated_candidate_survives_verification_failure_but_is_not_usable(self):
        secrets = Secrets()
        self.error("basecamp_oauth_identity_mismatch", lambda: app.access_token(secrets,
            claim_refresh=lambda _: True, session=Session(Response(token_response(ROTATED)), *verification(identity=1)),
            clock=lambda: 4590))
        self.assertEqual(secrets.value["refresh_state"], "unverified")
        self.assertEqual(secrets.value["refresh_token"], ROTATED)
        self.error("refresh_outcome_requires_review", lambda: app.access_token(secrets, session=Session(), clock=lambda: 4591))

    def test_failure_to_persist_started_state_prevents_token_post(self):
        session = Session()
        self.error("cloud_basecamp_access_failed", lambda: app.access_token(Secrets(fail_save=True), claim_refresh=lambda _: True, session=session, clock=lambda: 4590))
        self.assertEqual(session.calls, [])

    def test_refresh_refusal_or_missing_rotation_never_ready(self):
        for payload, status, code in (({"error": "invalid_grant"}, 400, "refresh_refused_no_retry"),
                                       (token_response(), 200, "rotated_refresh_token_missing")):
            secrets = Secrets()
            self.error(code, lambda: app.access_token(secrets, claim_refresh=lambda _: True,
                session=Session(Response(payload, status=status)), clock=lambda: 4590))
            self.assertEqual(secrets.value["refresh_state"], "started")


if __name__ == "__main__": unittest.main()
