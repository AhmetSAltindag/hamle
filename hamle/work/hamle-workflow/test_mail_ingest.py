"""Synthetic, offline contract tests. No account, socket, auth or Keychain use."""
import base64
import copy
import importlib.util
from pathlib import Path
import socket
import unittest
from unittest.mock import patch

import mail_ingest as app


def encoded(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(mid="a1", date="1000", to=app.HAMLE):
    return {"id": mid, "threadId": "f1", "internalDate": date,
            "historyId": "999999", "labelIds": ["INBOX"],
            "payload": {"mimeType": "text/plain", "headers": [
                {"name": "From", "value": "Müşteri <customer@example.invalid>"},
                {"name": "To", "value": to},
                {"name": "Date", "value": "Fri, 9 Oct 2099 09:00:00 +0300"}],
                "body": {"data": encoded("Yalnız sentetik Türkçe metin."), "size": 32}}}


def header(msg, name, value):
    msg["payload"]["headers"].append({"name": name, "value": value})


def set_header(msg, name, value):
    msg["payload"]["headers"] = [h for h in msg["payload"]["headers"] if h["name"].casefold() != name.casefold()]
    header(msg, name, value)


def context(**changes):
    defaults = dict(mailbox_email=app.HAMLE, verified_provider_response=True,
                    complete_visible_snapshot=True, authorized_body_message_ids=frozenset({"a1", "a2"}))
    defaults.update(changes)
    return app.Context(**defaults)


def run(*messages, ctx=None):
    return app.normalize_thread({"id": "f1", "messages": list(messages)}, context=ctx or context())


class ParserTests(unittest.TestCase):
    def setUp(self):
        # Every test, including engine contract imports, runs with sockets blocked.
        self.socket_patch = patch.object(socket, "socket", side_effect=AssertionError("network_forbidden"))
        self.connect_patch = patch.object(socket, "create_connection", side_effect=AssertionError("network_forbidden"))
        self.socket_patch.start(); self.connect_patch.start()
        self.addCleanup(self.socket_patch.stop); self.addCleanup(self.connect_patch.stop)

    def test_explicit_to_decodes_authorized_body_without_action_grant(self):
        result = run(message())
        msg = result["messages"][0]
        self.assertEqual(msg["participation"], "included")
        self.assertEqual(msg["body"][0]["text"], "Yalnız sentetik Türkçe metin.")
        self.assertFalse(result["action_eligible"])
        self.assertIsNone(msg["engine_evidence"]["sequence"])
        self.assertFalse(result["order"]["global_order_verified"])
        self.assertFalse(result["order"]["unseen_messages_known"])

    def test_cc_exact_case_insensitive_address_with_display_name(self):
        m = message(to="other@example.invalid")
        header(m, "cC", '"Hamle ekip" <HAMLE@GAMECHANGERS.COM.TR>')
        self.assertEqual(run(m)["messages"][0]["direct_cc"], [app.HAMLE])
        self.assertEqual(run(m)["messages"][0]["body_status"], "decoded_unrendered")

    def test_display_name_near_addresses_and_group_alias_cannot_grant(self):
        for value in [f'"{app.HAMLE}" <other@example.invalid>', "hamle+test@gamechangers.com.tr",
                      "hamle@gamechangers.com.tr.evil.invalid", "hamle@gamechangers.co.tr",
                      "team@gamechangers.com.tr", "hаmle@gamechangers.com.tr"]:
            with self.subTest(value=value), patch.object(app, "_decode_bodies") as decode:
                result = run(message(to=value))
                self.assertNotEqual(result["messages"][0]["participation"], "included")
                decode.assert_not_called()

    def test_body_quote_bcc_delivery_and_reply_to_are_not_to_cc(self):
        m = message(to="other@example.invalid")
        for name in ["Bcc", "Delivered-To", "X-Original-To", "Reply-To"]:
            header(m, name, app.HAMLE)
        m["payload"]["body"]["data"] = encoded("Forwarded message\nTo: " + app.HAMLE)
        with patch.object(app, "_decode_bodies") as decode:
            result = run(m)
        decode.assert_not_called()
        self.assertEqual(result["messages"][0]["participation"], "excluded")
        self.assertNotIn("bcc", result["messages"][0])

    def test_duplicate_to_cc_or_from_is_ambiguous_even_identical(self):
        for name, value in [("TO", app.HAMLE), ("Cc", app.HAMLE), ("From", "customer@example.invalid")]:
            m = message()
            if name == "Cc": header(m, name, value)
            header(m, name, value)
            with self.subTest(name=name):
                result = run(m)["messages"][0]
                self.assertEqual(result["participation"], "uncertain")
                self.assertEqual(result["body"], [])

    def test_malformed_header_and_crlf_injection_fail_closed(self):
        for value in ["Hamle <" + app.HAMLE, app.HAMLE + "\r\nCc: other@example.invalid", "a@b.invalid,," + app.HAMLE]:
            m = message(to=value)
            self.assertEqual(run(m)["messages"][0]["body"], [])

    def test_missing_or_multiple_senders_block(self):
        for values in [[], ["a@example.invalid, b@example.invalid"]]:
            m = message()
            m["payload"]["headers"] = [h for h in m["payload"]["headers"] if h["name"] != "From"]
            for value in values: header(m, "From", value)
            self.assertEqual(run(m)["messages"][0]["participation"], "uncertain")

    def test_own_sender_sent_and_draft_are_inert(self):
        for mode in ["from", "SENT", "DRAFT"]:
            m = message()
            if mode == "from": set_header(m, "From", app.HAMLE.upper())
            else: m["labelIds"] = [mode]
            result = run(m)["messages"][0]
            self.assertEqual(result["direction"], "outbound")
            self.assertFalse(result["envelope_eligible"])
            self.assertEqual(result["body"], [])

    def test_auto_reply_and_list_markers_block(self):
        for name, value in [("Auto-Submitted", "auto-replied"), ("Auto-Submitted", "auto-generated"),
                            ("Precedence", "bulk"), ("X-Autoreply", "yes"),
                            ("X-Autorespond", "yes"), ("List-Id", "list.example.invalid")]:
            m = message(); header(m, name, value)
            result = run(m)["messages"][0]
            self.assertTrue(result["automated"])
            self.assertEqual(result["body"], [])

    def test_auto_submitted_no_is_not_an_auto_reply(self):
        m = message(); header(m, "Auto-Submitted", "no")
        self.assertFalse(run(m)["messages"][0]["automated"])

    def test_default_context_ahmet_and_metadata_format_do_not_decode(self):
        for ctx in [app.Context(app.HAMLE), context(mailbox_email="ahmet@gamechangers.com.tr"),
                    context(verified_provider_response=False), context(response_format="metadata")]:
            with patch.object(app, "_decode_bodies") as decode:
                result = run(message(), ctx=ctx)
                self.assertEqual(result["messages"][0]["participation"], "uncertain")
                decode.assert_not_called()

    def test_independent_body_permission_is_required(self):
        m = message(); m["authorized_body_message_ids"] = ["a1"]
        result = run(m, ctx=context(authorized_body_message_ids=frozenset()))
        self.assertEqual(result["messages"][0]["participation"], "included")
        self.assertEqual(result["messages"][0]["body"], [])

    def test_learning_replay_cannot_decode_or_create_action(self):
        result = run(message(), ctx=context(purpose="learning"))
        self.assertEqual(result["messages"][0]["body"], [])
        self.assertFalse(result["action_eligible"])

    def test_partial_snapshot_and_missing_date_hold_all_bodies(self):
        for ctx, date in [(context(complete_visible_snapshot=False), "1000"), (context(), None),
                          (context(), True), (context(), "-1"), (context(), "999999999999999")]:
            result = run(message(date=date), ctx=ctx)
            self.assertEqual(result["order"]["status"], "inconclusive")
            self.assertEqual(result["messages"][0]["body"], [])

    def test_equal_internal_dates_do_not_tiebreak_by_ids_or_history(self):
        a,b = message(),message("a2"); b["historyId"] = "100000000000000000"
        result = run(a,b)
        self.assertIsNone(result["order"]["latest_observed_incoming_id"])
        self.assertTrue(all(not m["body"] for m in result["messages"]))

    def test_date_header_history_and_array_order_do_not_create_sequence(self):
        a,b = message(date="2000"),message("a2", "1000")
        a["historyId"] = "1"; b["historyId"] = "9999999999"
        result = run(a,b)
        self.assertEqual(result["order"]["latest_observed_incoming_id"], "a1")
        self.assertIsNone(result["order"]["authority_sequence"])
        self.assertEqual(result["messages"][1]["body"], [])

    def test_latest_observed_exclusion_withholds_old_included_body(self):
        result = run(message(), message("a2", "2000", to="other@example.invalid"))
        self.assertEqual(result["order"]["latest_observed_incoming_id"], "a2")
        self.assertTrue(all(not m["body"] for m in result["messages"]))

    def test_fresh_reinclusion_is_metadata_only_not_old_work_replay(self):
        result = run(message(to="other@example.invalid"),message("a2","2000"))
        self.assertEqual(result["messages"][0]["body"], [])
        self.assertTrue(result["messages"][1]["body"])
        self.assertFalse(result["action_eligible"])

    def test_id_canonicalization_and_rfc_message_id_separation(self):
        m = message("A1"); m["threadId"] = "F1"; header(m,"Message-ID","<different@example.invalid>")
        result = run(m)
        self.assertEqual(result["message_ids"],["a1"])
        self.assertEqual(result["thread_id"],"f1")

    def test_invalid_duplicate_ids_and_thread_mismatch_rejected(self):
        for m in [dict(message(),id="../escape"),dict(message(),threadId="f2")]:
            with self.assertRaises(app.NormalizationError): run(m)
        with self.assertRaisesRegex(app.NormalizationError,"duplicate_message_id"):
            run(message("A1"),message("a1"))

    def test_minimal_existing_helper_response_is_insufficient(self):
        with self.assertRaises(app.NormalizationError):
            app.normalize_thread({"id":"f1","messages":[{"id":"a1"}]},context=context())

    def test_mime_alternatives_decode_but_html_never_renders(self):
        m = message(); p = m["payload"]; p["mimeType"]="multipart/alternative";p["body"]={}
        p["parts"]=[{"mimeType":mime,"headers":[],"body":{"data":encoded(text)}} for mime,text in
                    [("text/plain","Merhaba"),("text/html",'<script>bad()</script><img src="https://example.invalid">')]]
        result=run(m)["messages"][0]
        self.assertEqual(len(result["body"]),2)
        self.assertTrue(all(not x["rendered"] for x in result["body"]))

    def test_unsupported_nonattachment_leaf_withholds_entire_body(self):
        for mime in ["text/rtf", "application/rtf", "application/octet-stream", "", "text"]:
            with self.subTest(mime=mime), patch.object(app, "_decode_bodies") as decode:
                m=message();m["payload"]["mimeType"]=mime
                result=run(m)["messages"][0]
                self.assertEqual(result["body_status"],"withheld")
                self.assertEqual(result["body"],[])
                self.assertIn("body_mime_unsupported",result["issues"])
                decode.assert_not_called()

    def test_supported_plus_unsupported_body_never_returns_partial_text(self):
        for container in ["multipart/mixed", "multipart/alternative", "multipart/related"]:
            for mime_order in [("text/plain", "text/rtf"), ("text/rtf", "text/html")]:
                with self.subTest(container=container, order=mime_order), patch.object(app,"_decode_bodies") as decode:
                    m=message();p=m["payload"];p["mimeType"]=container;p["body"]={}
                    p["parts"]=[{"mimeType":mime,"headers":[],"body":{"data":encoded("Sentetik gövde")}}
                                for mime in mime_order]
                    result=run(m)["messages"][0]
                    self.assertEqual(result["body_status"],"withheld")
                    self.assertEqual(result["body"],[])
                    self.assertIn("body_mime_unsupported",result["issues"])
                    decode.assert_not_called()

    def test_ambiguous_mime_container_and_header_type_withhold(self):
        child={"mimeType":"text/plain","headers":[],"body":{"data":encoded("Sentetik")}}
        cases=[]
        m=message();m["payload"]["mimeType"]="multipart/mixed";m["payload"]["body"]={}
        cases.append((m,"body_container_children_missing"))
        m=message();m["payload"]["parts"]=[child]
        cases.append((m,"body_container_unsupported"))
        m=message();m["payload"]["mimeType"]="multipart/mixed";m["payload"]["parts"]=[child]
        cases.append((m,"body_container_data_ambiguous"))
        m=message();set_header(m,"Content-Type","text/rtf")
        cases.append((m,"body_mime_metadata_ambiguous"))
        for m,issue in cases:
            with self.subTest(issue=issue):
                result=run(m)["messages"][0]
                self.assertEqual(result["body_status"],"withheld")
                self.assertEqual(result["body"],[])
                self.assertIn(issue,result["issues"])

    def test_explicit_unsupported_attachment_remains_inventory_only(self):
        m=message();p=m["payload"];p["mimeType"]="multipart/mixed";p["body"]={}
        p["parts"]=[{"mimeType":"text/plain","headers":[],"body":{"data":encoded("Sentetik")}},
                    {"mimeType":"text/rtf","filename":"attachment.rtf","headers":[],
                     "body":{"data":"NOT-VALID-BODY-DATA!"}}]
        result=run(m)["messages"][0]
        self.assertEqual(result["body_status"],"decoded_unrendered")
        self.assertEqual(result["body"][0]["text"],"Sentetik")
        self.assertEqual(result["issues"],[])
        self.assertTrue(result["mime_inventory"][2]["is_attachment"])
        self.assertFalse(result["mime_inventory"][2]["content_inspected"])

    def test_attached_email_headers_do_not_grant_and_attachments_never_decode(self):
        m=message(to="other@example.invalid");p=m["payload"];p["mimeType"]="multipart/mixed";p["body"]={}
        nested=message()["payload"];nested["filename"]="forward.eml";nested["mimeType"]="message/rfc822"
        nested["parts"]=[{"mimeType":"text/plain","headers":[],"body":{"data":"!INVALID!"}}]
        p["parts"]=[nested,{"mimeType":"application/pdf","filename":"../untrusted.pdf","headers":[],"body":{"attachmentId":"synthetic-opaque-id","size":200}}]
        result=run(m)["messages"][0]
        self.assertEqual(result["participation"],"excluded")
        self.assertEqual(result["body"],[])
        self.assertEqual(len(result["mime_inventory"]),4)
        self.assertFalse(any(x["content_inspected"] for x in result["mime_inventory"]))

    def test_malformed_base64_and_unknown_charset_fail_without_partial_body(self):
        for mutation in ["base64","charset"]:
            m=message()
            if mutation=="base64":m["payload"]["body"]["data"]="%%%"
            else:header(m,"Content-Type","text/plain; charset=unknown-synthetic-charset")
            result=run(m)["messages"][0]
            self.assertEqual(result["body_status"],"decode_failed")
            self.assertEqual(result["body"],[])

    def test_remote_body_data_is_inventory_not_fetched_content(self):
        m=message();m["payload"]["body"]={"attachmentId":"synthetic-body-ref","size":500}
        result=run(m)["messages"][0]
        self.assertIn("body_external_data_not_fetched",result["issues"])
        self.assertEqual(result["body"],[])

    def test_ambiguous_child_mime_headers_hold_body_instead_of_silent_skip(self):
        for child_headers in [None, [{"name":"Content-Disposition","value":"inline"},
                                      {"name":"Content-Disposition","value":"attachment"}],
                              [{"name":"Content-Type","value":"text/plain"},
                               {"name":"Content-Type","value":"text/html"}]]:
            m=message();p=m["payload"];p["mimeType"]="multipart/mixed";p["body"]={}
            p["parts"]=[{"mimeType":"text/plain","headers":child_headers,
                         "body":{"data":encoded("Must be withheld")}}]
            result=run(m)
            self.assertEqual(result["order"]["status"],"inconclusive")
            self.assertTrue(result["messages"][0]["issues"])
            self.assertEqual(result["messages"][0]["body"],[])

    def test_missing_text_data_is_not_reported_as_complete_empty_body(self):
        m=message();m["payload"]["body"]={"size":50}
        result=run(m)["messages"][0]
        self.assertIn("body_data_missing",result["issues"])
        self.assertEqual(result["body_status"],"decode_failed")
        m["payload"]["body"]={"size":0}
        self.assertEqual(run(m)["messages"][0]["body_status"],"decoded_unrendered")

    def test_mime_cycle_depth_and_body_size_limits(self):
        m=message();m["payload"]["parts"]=[m["payload"]]
        with self.assertRaises(app.NormalizationError):run(m)
        m=message();p=m["payload"]
        for _ in range(app.MAX_DEPTH+1):
            child={"mimeType":"multipart/mixed","headers":[],"body":{}};p["parts"]=[child];p=child
        with self.assertRaises(app.NormalizationError):run(m)
        m=message();m["payload"]["body"]["data"]="A"*(4*app.MAX_BODY_BYTES//3+20)
        self.assertEqual(run(m)["messages"][0]["body_status"],"decode_failed")

    def test_normalization_does_not_modify_input(self):
        m=message(); before=copy.deepcopy(m);run(m);self.assertEqual(m,before)

    def test_offline_engine_rejects_parser_metadata_without_trusted_sequence(self):
        path=Path(__file__).resolve().parents[2]/"outputs/gamechangers-pilot/offline/engine.py"
        spec=importlib.util.spec_from_file_location("offline_engine_contract",path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        evidence=run(message())["messages"][0]["engine_evidence"]
        allowed,reason,sequence=module.Engine._mail_decision(evidence,"a1","f1")
        self.assertFalse(allowed);self.assertEqual(reason,"mail_evidence_uncertain");self.assertIsNone(sequence)


if __name__ == "__main__":
    unittest.main(verbosity=2)
