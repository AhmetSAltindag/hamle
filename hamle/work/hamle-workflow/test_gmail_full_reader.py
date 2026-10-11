"""Offline connector tests using the real helper with fake store/transport."""
import base64
import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import logging
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

import gmail_full_reader as app
import mail_ingest as normalizer

HELPER = app._local_module("_hamle_reader_test_helper", Path(__file__).resolve().parent.parent /
                           "hamle-gmail-connector" / "hamle_gmail.py")


def thread():
    return {"id":"f1", "messages":[{
        "id":"a1", "threadId":"f1", "internalDate":"1000", "labelIds":["INBOX"],
        "payload":{"mimeType":"text/plain", "headers":[
            {"name":"From","value":"sender@example.invalid"},
            {"name":"To","value":app.EXPECTED_EMAIL},
            {"name":"Subject","value":"SYNTHETIC_RAW_HEADER_PRIVATE"}],
            "body":{"data":base64.urlsafe_b64encode(b"SYNTHETIC_BODY_PRIVATE").decode()}}}]}


class Credentials:
    valid = True
    granted_scopes = HELPER.SCOPES
    refresh_token = "synthetic-refresh-token"
    def __init__(self):
        self.refresh_count = 0
    def refresh(self, request):
        self.refresh_count += 1
        self.valid = True
    def to_json(self):
        return json.dumps({"client_id":"synthetic.apps.googleusercontent.com",
                          "client_secret":"synthetic-secret", "refresh_token":self.refresh_token,
                          "token":"synthetic-access-token", "token_uri":HELPER.TOKEN_URI,
                          "scopes":list(self.granted_scopes)})


class Store:
    def __init__(self, record):
        self.record = record
        self.loads = 0
        self.saves = 0
    def load(self):
        self.loads += 1
        return None if self.record is None else HELPER.validate_record(self.record)
    def save(self, record):
        self.saves += 1
        self.record = HELPER.validate_record(record)


class Request:
    def __init__(self, result):
        self.result = result
    def execute(self, *, num_retries):
        if num_retries != 0:
            raise AssertionError("unexpected_retry_setting")
        if isinstance(self.result, Exception):
            raise self.result
        return copy.deepcopy(self.result)


class Service:
    def __init__(self):
        self.profile = {"emailAddress":app.EXPECTED_EMAIL}
        self.response = thread()
        self.profile_calls = []
        self.thread_calls = []
        self.closed = False
    def users(self): return self
    def threads(self): return self
    def getProfile(self, **kwargs):
        self.profile_calls.append(kwargs)
        return Request(self.profile)
    def get(self, **kwargs):
        self.thread_calls.append(kwargs)
        return Request(self.response)
    def close(self): self.closed = True


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.socket_patch = patch.object(socket,"socket",side_effect=AssertionError("network_forbidden"))
        self.connect_patch = patch.object(socket,"create_connection",side_effect=AssertionError("network_forbidden"))
        self.socket_patch.start(); self.connect_patch.start()
        self.addCleanup(self.socket_patch.stop); self.addCleanup(self.connect_patch.stop)
        self.credentials = Credentials()
        record = HELPER.make_record(self.credentials,HELPER.SCOPES,{"emailAddress":app.EXPECTED_EMAIL})
        self.store = Store(record)
        self.service = Service()

    def read(self, tid="f1", body=None):
        return app._read_verified(tid,body,helper=HELPER,normalizer=normalizer,store=self.store,
                                  service_factory=lambda _: self.service,
                                  credentials_factory=lambda _: self.credentials,
                                  request_factory=lambda: object())

    def test_default_one_explicit_full_read_without_body_or_raw_headers(self):
        output = self.read()
        self.assertEqual(self.service.profile_calls,[{"userId":"me","fields":"emailAddress"}])
        self.assertEqual(self.service.thread_calls,[{"userId":"me","id":"f1","format":"full",
                                                   "fields":app.THREAD_FIELDS}])
        self.assertEqual(output["observation"]["messages"][0]["body"],[])
        self.assertNotIn("SYNTHETIC_BODY_PRIVATE",json.dumps(output))
        self.assertNotIn("SYNTHETIC_RAW_HEADER_PRIVATE",json.dumps(output))
        self.assertTrue(self.service.closed)
        self.assertEqual(self.store.saves,0)

    def test_explicit_single_body_can_pass_existing_normalizer_gates(self):
        output = self.read("F1","A1")
        self.assertEqual(output["requested_thread_id"],"f1")
        self.assertEqual(output["observation"]["messages"][0]["body"][0]["text"],"SYNTHETIC_BODY_PRIVATE")
        self.assertFalse(output["external_actions_enabled"])
        self.assertFalse(output["observation"]["action_eligible"])
        self.assertIsNone(output["observation"]["messages"][0]["engine_evidence"]["sequence"])

    def test_invalid_caller_ids_fail_before_store_or_network(self):
        for tid,body in [("../path",None),(None,None),("f1",["a1"]),("f1","a1,a2")]:
            with self.subTest(tid=tid,body=body), self.assertRaises(app.ReadError): self.read(tid,body)
        self.assertEqual(self.store.loads,0)
        self.assertEqual(self.service.profile_calls,[])

    def test_public_invalid_input_does_not_load_helper_or_touch_keychain(self):
        with patch.object(app,"_local_module") as load, self.assertRaisesRegex(app.ReadError,"invalid_thread_id"):
            app.read_full_thread("not-a-canonical-thread")
        load.assert_not_called()

    def test_wrong_identity_prevents_thread_read(self):
        self.service.profile={"emailAddress":"ahmet@gamechangers.com.tr"}
        with self.assertRaisesRegex(app.ReadError,"wrong_account"): self.read()
        self.assertEqual(self.service.thread_calls,[])
        self.assertEqual(self.store.saves,0)
        self.assertTrue(self.service.closed)

    def test_missing_or_extra_scope_prevents_thread_read(self):
        for scopes in [list(HELPER.SCOPES[:1]),list(HELPER.SCOPES)+["https://mail.google.com/"]]:
            self.store.record["granted_scopes"]=scopes
            with self.assertRaisesRegex(app.ReadError,"scope_mismatch"): self.read()
        self.assertEqual(self.service.profile_calls,[])
        self.assertEqual(self.service.thread_calls,[])

    def test_not_connected_never_starts_oauth(self):
        self.store.record=None
        with self.assertRaisesRegex(app.ReadError,"not_connected"): self.read()
        self.assertEqual(self.service.profile_calls,[])

    def test_expired_credential_uses_existing_refresh_and_keychain_save_path(self):
        self.credentials.valid=False
        result=self.read()
        self.assertTrue(result["token_refreshed"])
        self.assertEqual(self.credentials.refresh_count,1)
        self.assertEqual(self.store.saves,1)

    def test_scope_change_on_refresh_prevents_profile_and_thread_read(self):
        self.credentials.valid=False
        self.credentials.granted_scopes=[HELPER.SCOPES[0]]
        with self.assertRaisesRegex(app.ReadError,"scope_mismatch"): self.read()
        self.assertEqual(self.service.profile_calls,[])
        self.assertEqual(self.service.thread_calls,[])
        self.assertEqual(self.store.saves,0)

    def test_keychain_refresh_save_failure_stops_before_mail_read(self):
        self.credentials.valid=False
        with patch.object(self.store,"save",side_effect=HELPER.SafeError("keychain_write_failed")):
            with self.assertRaisesRegex(app.ReadError,"keychain_write_failed"): self.read()
        self.assertEqual(self.service.thread_calls,[])
        self.assertTrue(self.service.closed)

    def test_provider_exception_has_no_raw_details_or_output(self):
        self.service.response=RuntimeError("SYNTHETIC_TOKEN_OR_MAIL_PRIVATE")
        stdout,stderr=io.StringIO(),io.StringIO()
        with redirect_stdout(stdout),redirect_stderr(stderr):
            with self.assertRaisesRegex(app.ReadError,"^provider_read_failed$") as captured: self.read()
        self.assertTrue(captured.exception.__suppress_context__)
        self.assertEqual(stdout.getvalue()+stderr.getvalue(),"")
        self.assertTrue(self.service.closed)

    def test_diagnostics_are_discarded_and_logging_settings_restored(self):
        previous=logging.root.manager.disable
        original=self.store.load
        def noisy_load():
            print("SYNTHETIC_PRIVATE_DIAGNOSTIC")
            logging.error("SYNTHETIC_PRIVATE_LOG")
            return original()
        stdout,stderr=io.StringIO(),io.StringIO()
        with patch.object(self.store,"load",side_effect=noisy_load),redirect_stdout(stdout),redirect_stderr(stderr):
            self.read()
        self.assertEqual(stdout.getvalue()+stderr.getvalue(),"")
        self.assertEqual(logging.root.manager.disable,previous)

    def test_wrong_thread_or_pagination_marker_is_rejected(self):
        for response in [{"id":"f2","messages":thread()["messages"]},dict(thread(),nextPageToken="private")]:
            self.service.response=response
            with self.assertRaisesRegex(app.ReadError,"provider_thread_invalid"): self.read()

    def test_malformed_provider_structure_is_safe_error(self):
        for response in [None, {"id":"f1"}, {"id":"f1","messages":[{"id":"a1"}]}]:
            self.service.response=response
            with self.assertRaises(app.ReadError) as captured: self.read()
            self.assertIn(str(captured.exception),{"provider_thread_invalid","thread_normalization_failed"})

    def test_requested_body_not_in_thread_is_rejected(self):
        with self.assertRaisesRegex(app.ReadError,"requested_body_message_not_in_thread"): self.read(body="a2")

    def test_to_cc_exclusion_still_withholds_requested_body(self):
        self.service.response["messages"][0]["payload"]["headers"][1]["value"]="someone@example.invalid"
        output=self.read(body="a1")
        self.assertEqual(output["observation"]["messages"][0]["body"],[])

    def test_older_requested_body_is_not_substituted_by_latest_body(self):
        newer=copy.deepcopy(self.service.response["messages"][0]);newer["id"]="a2";newer["internalDate"]="2000"
        self.service.response["messages"].append(newer)
        output=self.read(body="a1")
        self.assertTrue(all(m["body"]==[] for m in output["observation"]["messages"]))

    def test_equal_provider_dates_remain_inconclusive(self):
        newer=copy.deepcopy(self.service.response["messages"][0]);newer["id"]="a2"
        self.service.response["messages"].append(newer)
        output=self.read(body="a2")
        self.assertEqual(output["observation"]["order"]["status"],"inconclusive")
        self.assertTrue(all(m["body"]==[] for m in output["observation"]["messages"]))

    def test_unsupported_body_is_withheld_without_partial_success(self):
        self.service.response["messages"][0]["payload"]["mimeType"]="text/rtf"
        output=self.read(body="a1")["observation"]["messages"][0]
        self.assertEqual(output["body_status"],"withheld")
        self.assertIn("body_mime_unsupported",output["issues"])

    def test_public_adapter_is_portable_and_uses_native_store_factory(self):
        # Replace all factories before calling public API: no real Keychain/API.
        original=HELPER.verified_gmail
        with patch.object(app,"_local_module",side_effect=[normalizer,HELPER]) as load, \
             patch.object(HELPER,"MacKeychainStore",return_value=self.store) as store_factory, \
             patch.object(HELPER,"build_gmail",return_value=self.service), \
             patch.object(HELPER,"verified_gmail",wraps=HELPER.verified_gmail) as verified:
            # The real verified helper needs explicit fake credential injection.
            verified.side_effect=lambda store,factory,*args: original(
                store,factory,lambda _:self.credentials,lambda:object())
            output=app.read_full_thread("f1")
        store_factory.assert_called_once_with()
        self.assertEqual(output["email"],app.EXPECTED_EMAIL)
        self.assertEqual(load.call_args_list[1].args[1],Path(app.__file__).resolve().parent.parent /
                         "hamle-gmail-connector" / "hamle_gmail.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
