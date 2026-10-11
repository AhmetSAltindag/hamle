import base64
from contextlib import contextmanager
from copy import deepcopy
from email import policy
from email.parser import BytesParser
import json
import socket
import unittest
from unittest.mock import patch

import business_writers as app
from test_cloud_providers import Response, Session, FAKE_TOKEN


def task(parent_type="Todoset"):
    return {"account_id": 5898865, "project_id": 49184053, "todo_parent_id": 10384126975,
            "todo_parent_type": parent_type, "title": "İç eğitim rehberi", "description": "Ahmet'in talebi\nA < B & C",
            "assignee_id": 53358100, "due_at": None}


def reply():
    return {"from": app.provider.HAMLE, "to": [app.provider.AHMET], "cc": [],
            "subject": app.SUBJECT, "body": "Merhaba Ahmet,\nTalebini aldım. Tarihi ayrıca netleştireceğim.",
            "thread_id": "abc123", "in_reply_to": "<internal-test@example.invalid>",
            "references": "<earlier@example.invalid> <internal-test@example.invalid>"}


def task_receipt(payload=None):
    payload = payload or task()
    return {"id": 101, "bucket": {"id": 49184053},
            "parent": {"id": payload["todo_parent_id"], "type": payload["todo_parent_type"]},
            "creator": {"id": 53358100}, "status": "active", "visible_to_clients": False,
            "completed": False, "due_on": None, "content": payload["title"],
            "description": app._description(payload["description"]), "assignees": [{"id": 53358100}]}


def mail_receipt(payload=None, raw=None):
    payload = payload or reply()
    return {"id": "feed01", "threadId": payload["thread_id"], "labelIds": ["SENT"],
            "raw": base64.urlsafe_b64encode(raw or app._mime(payload)).decode()}


def work():
    return {"account_id": 5898865, "project_id": 49184053, "task_id": app.FOLLOWUP_TASK_ID,
            "todo_parent_id": 10384126975, "todo_parent_type": "Todoset"}


def existing_task():
    row = task_receipt(); row.update(id=app.FOLLOWUP_TASK_ID, updated_at="2026-10-10T12:00:00Z", comments_count=0)
    row["description"] = row["description"].replace("<div>", '<div dir="auto">')
    return row


def comment_receipt(body="Güncel iç takip notu"):
    return {"id": 202, "type": "Comment", "status": "active", "visible_to_clients": False,
            "bucket": {"id": 49184053}, "parent": {"id": app.FOLLOWUP_TASK_ID, "type": "Todo"},
            "creator": {"id": 53358100}, "content": app._description(body).replace("<div>", '<div dir="auto">'),
            "content_attachments": []}


class WritersTests(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("No network in tests"))
            p.start(); self.addCleanup(p.stop)
        self.writer = app.BusinessWriters(object())
    def error(self, code, function):
        with self.assertRaises(app.WriterError) as got: function()
        self.assertEqual(got.exception.code, code)
        self.assertFalse(got.exception.retry_allowed)
        self.assertNotIn(FAKE_TOKEN, str(got.exception))
    @contextmanager
    def fake_gmail(self): yield FAKE_TOKEN
    @contextmanager
    def session(self, session):
        with patch.object(app.provider, "_session", return_value=session), \
             patch.object(self.writer, "_basecamp", return_value=FAKE_TOKEN), \
             patch.object(self.writer, "_gmail", self.fake_gmail):
            yield session

    def test_callback_required_before_any_auth_or_provider_calls(self):
        with patch.object(self.writer, "_basecamp", side_effect=AssertionError("No preflight")), \
             patch.object(self.writer, "_gmail", side_effect=AssertionError("No preflight")):
            self.error("durable_before_post_required", lambda: self.writer.create_task(task()))
            self.error("durable_before_post_required", lambda: self.writer.send_reply(reply()))

    def test_task_post_uses_verified_parent_type_notify_false_escaped_html_no_due_date(self):
        for kind, path in (("Todoset", "todosets"), ("Todolist", "todolists")):
            events = []
            session = Session(Response(task_receipt(task(kind)), status=201))
            with self.session(session):
                result = self.writer.create_task(task(kind), before_post=lambda: events.append("fenced"))
            self.assertEqual(result, {"provider_id": "101"})
            self.assertEqual(events, ["fenced"])
            self.assertEqual(len(session.calls), 1)
            method, url, params = session.calls[0]
            self.assertEqual(method, "POST")
            self.assertEqual(url, app.BASECAMP_BASE + f"/{path}/10384126975/todos.json")
            body = json.loads(params["data"])
            self.assertEqual(body, {"content": task()["title"], "description": "<div>Ahmet&#x27;in talebi<br>A &lt; B &amp; C</div>",
                                    "assignee_ids": [53358100], "completion_subscriber_ids": [], "notify": False})
            self.assertFalse(params["allow_redirects"])
            self.assertEqual(params["timeout"], (10, 30))

    def test_stop_during_preflight_runs_final_callback_and_blocks_post(self):
        for method, payload in ((self.writer.create_task, task()), (self.writer.send_reply, reply())):
            events = []
            def refuse():
                events.append("fence"); raise app.WriterError("stopped")
            session = Session()
            with self.session(session):
                self.error("stopped", lambda: method(payload, before_post=refuse))
            self.assertEqual(events, ["fence"])
            self.assertEqual(session.calls, [])

    def test_scope_refuses_external_recipient_or_project_and_header_injection(self):
        for key, value in (("to", ["client@example.invalid"]), ("cc", [app.provider.AHMET]),
                           ("from", app.provider.AHMET), ("subject", "Another topic")):
            row = reply(); row[key] = value
            self.error("reply_scope_invalid", lambda: self.writer.send_reply(row, before_post=lambda: None))
        row = reply(); row["in_reply_to"] = "<x@a>\nBcc: outside@a"
        self.error("action_header_invalid", lambda: app._reply(row))
        for key, value in (("project_id", 1), ("assignee_id", 1), ("due_at", "2026-10-12"), ("todo_parent_type", "Chat")):
            row = task(); row[key] = value
            self.error("task_scope_invalid", lambda: self.writer.create_task(row, before_post=lambda: None))

    def test_send_preserves_exact_thread_headers_utf8_and_deterministic_message_id(self):
        session = Session(Response({"id": "feed01", "threadId": "abc123"}))
        order = []
        with self.session(session):
            result = self.writer.send_reply(reply(), before_post=lambda: order.append("fenced"))
        self.assertEqual(result, {"provider_id": "feed01"})
        self.assertEqual(order, ["fenced"])
        body = json.loads(session.calls[0][2]["data"])
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(body["raw"]))
        self.assertEqual(body["threadId"], "abc123")
        self.assertEqual(message["Message-ID"], app._message_id(reply()))
        self.assertEqual(message["Subject"], app.SUBJECT)
        self.assertEqual(message["In-Reply-To"], reply()["in_reply_to"])
        self.assertEqual(message.get_content().replace("\r\n", "\n"), reply()["body"] + "\n")
        self.assertFalse(message.is_multipart())
        self.assertIsNone(message["Bcc"])

    def test_unknown_timeout_is_one_attempt_and_redacts_exception(self):
        for method, payload in ((self.writer.create_task, task()), (self.writer.send_reply, reply())):
            session = Session(TimeoutError(FAKE_TOKEN))
            with self.session(session):
                self.error("provider_outcome_unknown_no_retry", lambda: method(payload, before_post=lambda: None))
            self.assertEqual(len(session.calls), 1)

    def test_redirect_or_bad_receipt_never_retries(self):
        for response in (Response({}, status=302), Response({"id": "x", "threadId": "abc123"})):
            session = Session(response)
            with self.session(session):
                with self.assertRaises(app.WriterError): self.writer.send_reply(reply(), before_post=lambda: None)
            self.assertEqual(len(session.calls), 1)

    def test_task_readback_checks_content_parent_identity_and_private_visibility(self):
        with self.session(Session(Response(task_receipt()))):
            self.assertEqual(self.writer.read_task("101", task()),
                {"provider_id": "101", "identity_verified": True, "matches_approved_payload": True})
        for key, value in (("content", "Changed"), ("description", "<div>wrong</div>"), ("visible_to_clients", True),
                           ("parent", {"id": 2, "type": "Todoset"}), ("creator", {"id": 1}), ("due_on", "2026-10-12")):
            row = task_receipt(); row[key] = value
            with self.session(Session(Response(row))):
                self.error("task_readback_mismatch", lambda: self.writer.read_task("101", task()))

    def test_basecamp_direction_wrapper_keeps_exact_text_and_rejects_other_attributes(self):
        row = task_receipt()
        row["description"] = row["description"].replace("<div>", '<div dir="auto">')
        with self.session(Session(Response(row))):
            self.assertTrue(self.writer.read_task("101", task())["matches_approved_payload"])
        for attrs in ('dir="auto" onclick="run()"', 'style="display:none"', 'dir="rtl"'):
            row["description"] = app._description(task()["description"]).replace("<div>", "<div " + attrs + ">")
            with self.session(Session(Response(row))):
                self.error("task_readback_mismatch", lambda: self.writer.read_task("101", task()))
        row["description"] = '<div dir="auto">Different text</div>'
        with self.session(Session(Response(row))):
            self.error("task_readback_mismatch", lambda: self.writer.read_task("101", task()))

    def test_mail_readback_matches_raw_reply_in_sent_thread(self):
        with self.session(Session(Response(mail_receipt()))):
            self.assertEqual(self.writer.read_reply("feed01", reply()),
                {"provider_id": "feed01", "identity_verified": True, "matches_approved_payload": True})
        for name in ("From", "To", "Subject", "Message-ID", "In-Reply-To"):
            message = BytesParser(policy=policy.default).parsebytes(app._mime(reply()))
            message.replace_header(name, "wrong@example.invalid")
            with self.session(Session(Response(mail_receipt(raw=message.as_bytes())))):
                self.error("mail_readback_header_mismatch", lambda: self.writer.read_reply("feed01", reply()))
        different = reply(); different["body"] = "Başka içerik"
        raw = app._mime(different).replace(app._message_id(different).encode(), app._message_id(reply()).encode())
        with self.session(Session(Response(mail_receipt(raw=raw)))):
            self.error("mail_readback_body_mismatch", lambda: self.writer.read_reply("feed01", reply()))

    def test_endpoint_allowlist_prevents_other_accounts_and_arbitrary_urls(self):
        session = Session()
        for url in ("https://evil.example/send", app.BASECAMP_BASE.replace("49184053", "42") + "/todosets/1/todos.json"):
            self.error("provider_endpoint_forbidden", lambda: app._request(session, "POST", url, FAKE_TOKEN, body={}))
        self.assertEqual(session.calls, [])

    def test_gmail_rewritten_rfc_id_requires_same_receipt_envelope_linkage_and_body(self):
        message = BytesParser(policy=policy.default).parsebytes(app._mime(reply()))
        message.replace_header("Message-ID", "<CAGsynthetic+original_receipt@mail.gmail.com>")
        with self.session(Session(Response(mail_receipt(raw=message.as_bytes())))):
            self.assertTrue(self.writer.read_reply("feed01", reply())["matches_approved_payload"])
        for name, value in (("Message-ID", "<foreign@example.invalid>"),
                            ("Message-ID", "invalid@mail.gmail.com"),
                            ("Message-ID", "<has space@mail.gmail.com>"),
                            ("From", "other@example.invalid"),
                            ("References", "<different@example.invalid>")):
            changed = deepcopy(message); changed.replace_header(name, value)
            with self.session(Session(Response(mail_receipt(raw=changed.as_bytes())))):
                self.error("mail_readback_header_mismatch", lambda: self.writer.read_reply("feed01", reply()))
        result = mail_receipt(raw=message.as_bytes()); result["id"] = "feed02"
        with self.session(Session(Response(result))):
            self.error("mail_readback_receipt_mismatch", lambda: self.writer.read_reply("feed01", reply()))
        changed = deepcopy(message); changed.set_content("Different body", charset="utf-8")
        with self.session(Session(Response(mail_receipt(raw=changed.as_bytes())))):
            self.error("mail_readback_body_mismatch", lambda: self.writer.read_reply("feed01", reply()))

    def test_verified_cansu_cc_is_sent_and_read_back_exactly(self):
        payload = reply(); payload["cc"] = ["cansu@gamechangers.com.tr"]
        message = BytesParser(policy=policy.default).parsebytes(app._mime(payload))
        self.assertEqual(str(message["Cc"]), "cansu@gamechangers.com.tr")
        with self.session(Session(Response(mail_receipt(payload, message.as_bytes())))):
            self.assertTrue(self.writer.read_reply("feed01", payload)["matches_approved_payload"])
        message.replace_header("Cc", "Cansu <cansu@gamechangers.com.tr>")
        with self.session(Session(Response(mail_receipt(payload, message.as_bytes())))):
            self.assertTrue(self.writer.read_reply("feed01", payload)["matches_approved_payload"])
        del message["Cc"]
        with self.session(Session(Response(mail_receipt(payload, message.as_bytes())))):
            self.error("mail_readback_header_mismatch", lambda: self.writer.read_reply("feed01", payload))
        message["Cc"] = "other@gamechangers.com.tr"
        with self.session(Session(Response(mail_receipt(payload, message.as_bytes())))):
            self.error("mail_readback_header_mismatch", lambda: self.writer.read_reply("feed01", payload))

    def test_unknown_duplicate_or_own_cc_is_held_and_not_silently_dropped(self):
        for cc in (["unknown@gamechangers.com.tr"], [app.provider.HAMLE], [app.provider.AHMET],
                   ["cansu@gamechangers.com.tr", "cansu@gamechangers.com.tr"]):
            payload = reply(); payload["cc"] = cc
            self.error("reply_scope_invalid", lambda: self.writer.send_reply(payload, before_post=lambda: None))
        sent = reply(); sent["cc"] = ["cansu@gamechangers.com.tr"]
        with self.session(Session(Response(mail_receipt(sent)))):
            self.error("mail_readback_header_mismatch", lambda: self.writer.read_reply("feed01", reply()))

    def test_live_target_must_match_payload_type_and_id_before_post(self):
        with patch.object(app.basecamp_cloud_auth, "access_token", return_value=FAKE_TOKEN), \
             patch.object(app.provider, "verify_basecamp_internal", return_value={"todo_parent_id": 10384126975, "todo_parent_type": "Todolist"}):
            self.error("basecamp_target_changed", lambda: self.writer._basecamp(task()))

    def comment_payload(self):
        with self.session(Session(Response(existing_task()))):
            status = self.writer.read_task_status(work())
        return {**work(), "body": "Güncel iç takip notu", "task_fingerprint": status["task_fingerprint"]}

    def test_status_read_has_semantic_fingerprint_and_bounded_plain_description(self):
        original = existing_task()
        with self.session(Session(Response(original))): first = self.writer.read_task_status(work())
        self.assertEqual(first["description"], task()["description"])
        self.assertEqual(first["task_id"], app.FOLLOWUP_TASK_ID)
        self.assertTrue(first["identity_verified"])
        changed = deepcopy(original); changed.update(updated_at="2026-10-10T12:05:00Z", comments_count=1)
        with self.session(Session(Response(changed))): second = self.writer.read_task_status(work())
        self.assertEqual(first["task_fingerprint"], second["task_fingerprint"])
        for key, value in (("completed", True), ("content", "Başka başlık"), ("due_on", "2026-10-12")):
            changed = deepcopy(original); changed[key] = value
            with self.session(Session(Response(changed))): current = self.writer.read_task_status(work())
            self.assertNotEqual(first["task_fingerprint"], current["task_fingerprint"])

    def test_status_rejects_foreign_parent_assignee_private_state_or_unsafe_description(self):
        for key, value in (("parent", {"id": 9, "type": "Todoset"}), ("visible_to_clients", True),
                           ("creator", {"id": 1}), ("assignees", [{"id": 1}]),
                           ("description", '<div onclick="run()">Content</div>')):
            row = existing_task(); row[key] = value
            with self.session(Session(Response(row))):
                with self.assertRaises(app.WriterError): self.writer.read_task_status(work())
        target = work(); target["task_id"] = 999
        self.error("work_scope_invalid", lambda: self.writer.read_task_status(target))

    def test_comment_post_requires_unchanged_open_task_and_final_callback(self):
        payload = self.comment_payload(); session = Session(Response(existing_task()), Response(comment_receipt(), 201))
        boundary = []
        with self.session(session):
            result = self.writer.create_comment(payload, before_post=lambda: boundary.append(len(session.calls)))
        self.assertEqual(result, {"provider_id": "202"})
        self.assertEqual(boundary, [1])
        self.assertEqual([method for method, _, _ in session.calls], ["GET", "POST"])
        self.assertEqual(session.calls[-1][1], app.BASECAMP_BASE + f"/recordings/{app.FOLLOWUP_TASK_ID}/comments.json")
        self.assertEqual(json.loads(session.calls[-1][2]["data"]), {"content": app._description(payload["body"])})
        self.assertFalse(session.calls[-1][2]["allow_redirects"])

    def test_task_change_or_stop_before_comment_blocks_post_without_retry(self):
        payload = self.comment_payload()
        for key, value in (("completed", True), ("content", "Changed"), ("status", "archived")):
            row = existing_task(); row[key] = value; session = Session(Response(row))
            with self.session(session):
                self.error("task_changed_before_comment", lambda: self.writer.create_comment(payload, before_post=lambda: None))
            self.assertEqual([m for m, _, _ in session.calls], ["GET"])
        session = Session(Response(existing_task()))
        def stop(): raise app.WriterError("stopped")
        with self.session(session): self.error("stopped", lambda: self.writer.create_comment(payload, before_post=stop))
        self.assertEqual([m for m, _, _ in session.calls], ["GET"])
        self.error("durable_before_post_required", lambda: self.writer.create_comment(payload))

    def test_comment_timeout_is_single_post_and_foreign_receipt_fails(self):
        payload = self.comment_payload()
        for response in (TimeoutError(FAKE_TOKEN), Response({**comment_receipt(), "parent": {"id": 9, "type": "Todo"}}, 201)):
            session = Session(Response(existing_task()), response)
            with self.session(session):
                with self.assertRaises(app.WriterError) as got: self.writer.create_comment(payload, before_post=lambda: None)
            self.assertNotIn(FAKE_TOKEN, str(got.exception))
            self.assertEqual([m for m, _, _ in session.calls], ["GET", "POST"])

    def test_comment_readback_uses_exact_receipt_body_parent_and_creator_not_old_task_hash(self):
        payload = self.comment_payload()
        changed = existing_task(); changed.update(completed=True, comments_count=1, updated_at="2026-10-10T13:00:00Z")
        with self.session(Session(Response(changed), Response(comment_receipt()))):
            result = self.writer.read_comment("202", payload)
        self.assertEqual(result, {"provider_id": "202", "identity_verified": True, "matches_approved_payload": True})
        for key, value in (("id", 203), ("parent", {"id": 9, "type": "Todo"}), ("creator", {"id": 1}),
                           ("content", "different"), ("visible_to_clients", True), ("content_attachments", [{"id": 1}])):
            row = comment_receipt(); row[key] = value
            with self.session(Session(Response(changed), Response(row))):
                self.error("comment_readback_mismatch", lambda: self.writer.read_comment("202", payload))


if __name__ == "__main__": unittest.main()
