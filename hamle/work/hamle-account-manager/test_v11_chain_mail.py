"""Offline tests for v11_chain_mail: fake Gmail REST session and fake Gmail SDK only."""
import base64
from contextlib import contextmanager
import hashlib
import json
import socket
import unittest
from unittest.mock import patch

import v11_chain_mail as cmail
from autonomy_policy import scope_digest
from recipient_policy import RecipientError
from test_cloud_providers import Cred, Gmail, Store, message
from test_v11_chain import live_catalog

TOKEN = "synthetic-placeholder-token-never-live"
HAMLE = "hamle@gamechangers.com.tr"
AHMET, CANSU, OZGE = (f"{n}@gamechangers.com.tr" for n in ("ahmet", "cansu", "ozge"))
PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-bytes" * 10
SHA = hashlib.sha256(PNG).hexdigest()


class Resp:
    def __init__(self, data, status=200):
        self.status_code, self.raw = status, json.dumps(data).encode()
    def iter_content(self, chunk_size):
        for i in range(0, len(self.raw), chunk_size):
            yield self.raw[i:i + chunk_size]
    def close(self):
        pass


class Rest:
    """Fake Gmail REST mailbox: one thread 'ab', incoming anchor 'a1', sends stored as raw."""

    def __init__(self):
        self.messages = {"a1": {"labels": ["INBOX"], "internal": "1000", "headers": {
            "From": f"Ahmet <{AHMET}>", "To": f"{HAMLE}, {CANSU}", "Cc": OZGE, "Subject": "Ofis afişi",
            "Message-ID": "<a1@mail.example>", "References": "<a0@mail.example>"}}}
        self.sent, self.calls = {}, []
        self.fail_send = None
        self.mutate_raw = None
        self.closed = 0

    def close(self):
        self.closed += 1

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        assert kwargs["allow_redirects"] is False and kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
        if method == "POST":
            if self.fail_send:
                raise self.fail_send
            body = json.loads(kwargs["data"])
            mid = "f%03d" % (len(self.sent) + 1)
            raw = base64.urlsafe_b64decode(body["raw"])
            if self.mutate_raw:
                raw = self.mutate_raw(raw)
            self.sent[mid] = {"thread": body["threadId"], "raw": raw}
            return Resp({"id": mid, "threadId": body["threadId"], "labelIds": ["SENT"]})
        path = url.split("/gmail/v1/users/me/", 1)[1]
        if path.startswith("threads/"):
            rows = [{"id": k, "labelIds": v["labels"], "internalDate": v["internal"]} for k, v in self.messages.items()]
            rows += [{"id": k, "labelIds": ["SENT"], "internalDate": "5000"} for k in self.sent]
            return Resp({"id": "ab", "messages": rows})
        mid = path.split("/")[1].split("?")[0]
        if "format=raw" in url:
            row = self.sent[mid]
            return Resp({"id": mid, "threadId": row["thread"], "labelIds": ["SENT"],
                         "raw": base64.urlsafe_b64encode(row["raw"]).decode().rstrip("=")})
        row = self.messages[mid]
        return Resp({"id": mid, "threadId": "ab", "labelIds": row["labels"], "internalDate": row["internal"],
                     "payload": {"headers": [{"name": k, "value": v} for k, v in row["headers"].items()]}})


def clarify(**changes):
    payload = {"from": HAMLE, "to": [AHMET], "cc": [], "thread_id": "ab", "in_reply_to_message_id": "a1",
               "subject": "Re: Ofis afişi", "body": "Merhaba, teslim tarihi nedir?"}
    payload.update(changes)
    return payload


def deliver(**changes):
    payload = {"from": HAMLE, "to": [AHMET, CANSU], "cc": [OZGE], "thread_id": "ab", "subject": "Re: Ofis afişi",
               "body": "Merhaba, iş ektedir.", "artifact": {"brand_id": "gamechangers", "work_id": "w1",
                                                            "version": 1, "sha256": SHA, "mime_type": "image/png"}}
    payload.update(changes)
    return payload


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.rest = Rest()
        self.files = {"data": PNG}

        @contextmanager
        def token():
            yield TOKEN
        self.writer = cmail.GmailThreadReplyWriter(
            live_catalog(), token_context=token, anchor_lookup=lambda tid: "a1",
            attachment_source=lambda art: {"filename": "afis.png", "data": self.files["data"],
                                           "content_type": "image/png"},
            session_factory=lambda: self.rest)

    def posts(self):
        return [c for c in self.rest.calls if c[0] == "POST"]

    def code(self, fn):
        with self.assertRaises(cmail.ChainMailError) as got:
            fn()
        return got.exception.code


class Writer(Base):
    def test_clarification_goes_only_to_requester_in_same_thread_and_reads_back(self):
        payload = clarify()
        self.assertTrue(self.writer.preflight("clarify_brief", payload))
        receipt = self.writer.write(payload)
        self.assertEqual(len(self.posts()), 1)
        raw = self.rest.sent[receipt["remote_id"]]["raw"].decode()
        self.assertIn("In-Reply-To: <a1@mail.example>", raw)
        self.assertIn("References: <a0@mail.example> <a1@mail.example>", raw)
        self.assertIn("To: " + AHMET, raw)
        self.assertNotIn("Cc:", raw)
        self.assertEqual(self.writer.readback(receipt["remote_id"], payload),
                         {"readback_sha256": scope_digest(payload)})
        self.assertEqual(len(self.posts()), 1)

    def test_delivery_is_reply_all_with_one_hash_checked_attachment(self):
        payload = deliver()
        self.assertTrue(self.writer.preflight("deliver_file", payload))
        receipt = self.writer.write(payload)
        self.assertEqual(self.writer.readback(receipt["remote_id"], payload)["readback_sha256"], scope_digest(payload))
        raw = self.rest.sent[receipt["remote_id"]]["raw"]
        self.assertIn(b"Cc: " + OZGE.encode(), raw)
        self.assertEqual(len(self.posts()), 1)

    def test_exact_to_cc_and_subject_are_required(self):
        for payload, code in ((deliver(to=[AHMET], cc=[CANSU, OZGE]), "reply_recipients_changed"),
                              (clarify(to=[AHMET], cc=[CANSU]), "reply_recipients_changed"),
                              (clarify(subject="Re: başka"), "reply_subject_mismatch")):
            self.assertEqual(self.code(lambda: self.writer.write(payload)), code)
        self.assertEqual(self.posts(), [])

    def test_unverified_or_external_recipient_is_refused_before_any_read(self):
        with self.assertRaises(RecipientError):
            self.writer.write(clarify(to=["someone@example.com"]))
        with self.assertRaises(RecipientError):
            self.writer.write(clarify(to=[AHMET, HAMLE]))
        self.assertEqual(self.rest.calls, [])

    def test_newer_incoming_mail_blocks_preflight(self):
        self.rest.messages["a2"] = dict(self.rest.messages["a1"], internal="2000")
        self.assertEqual(self.code(lambda: self.writer.preflight("clarify_brief", clarify())),
                         "newer_incoming_mail_in_thread")
        self.assertEqual(self.posts(), [])

    def test_changed_attachment_bytes_never_send(self):
        self.files["data"] = PNG + b"x"
        self.assertEqual(self.code(lambda: self.writer.write(deliver())), "attachment_bytes_mismatch")
        self.assertEqual(self.rest.calls, [])

    def test_post_failure_is_ambiguous_and_never_retried_here(self):
        self.rest.fail_send = TimeoutError("synthetic")
        self.assertEqual(self.code(lambda: self.writer.write(clarify())), "provider_outcome_unknown_no_retry")
        self.assertEqual(len(self.posts()), 1)

    def test_readback_mismatch_is_detected(self):
        self.rest.mutate_raw = lambda raw: raw.replace(b"teslim tarihi", b"TESLIM TARIHI")
        receipt = self.writer.write(clarify())
        self.assertEqual(self.code(lambda: self.writer.readback(receipt["remote_id"], clarify())),
                         "mail_readback_body_mismatch")

    def test_endpoint_allowlist(self):
        self.assertEqual(self.code(lambda: cmail._call(self.rest, "POST", cmail.GMAIL + "/drafts", TOKEN)),
                         "gmail_endpoint_forbidden")
        self.assertEqual(self.code(lambda: cmail._call(self.rest, "DELETE", cmail.GMAIL + "/messages/a1?format=raw&x", TOKEN)),
                         "gmail_endpoint_forbidden")

    def test_source_evidence_reads_only(self):
        evidence = self.writer.source_evidence("ab", "a1")
        self.assertTrue(evidence["actual_latest"])
        self.assertEqual((evidence["sender"], evidence["to"], evidence["cc"]), (AHMET, [HAMLE, CANSU], [OZGE]))
        self.assertEqual(self.posts(), [])


class Subject(unittest.TestCase):
    def test_reply_prefixes_never_stack(self):
        for raw in ("Görsel Çalışma Talebi", "Re: Görsel Çalışma Talebi", "Re: Re: Re: Görsel Çalışma Talebi",
                    "RE: ynt: Fwd: Görsel Çalışma Talebi"):
            self.assertEqual(cmail.reply_subject(raw), "Re: Görsel Çalışma Talebi")
        self.assertEqual(cmail.reply_subject("Rekor kampanya"), "Re: Rekor kampanya")


class Reader(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)

    def read(self, gmail):
        reader = cmail.ChainMailReader(Store(), live_catalog(), clock=lambda: 100,
                                       _service_factory=lambda _: gmail, _credentials_factory=lambda _: Cred(),
                                       _request_factory=lambda: object())
        return reader(), reader

    def test_verified_latest_incoming_mail_becomes_chain_source(self):
        gmail = Gmail([message("a1", 1000, subject="Ofis afişi", text="1080x1080 PNG afiş lazım.")])
        sources, reader = self.read(gmail)
        src, = sources
        self.assertEqual((src["thread_id"], src["message_id"], src["sender"], src["text"]),
                         ("ab", "a1", AHMET, "1080x1080 PNG afiş lazım."))
        self.assertEqual((src["source_order"], src["rfc_message_id"], len(src["fingerprint"])),
                         (1000, "<a1@example.invalid>", 64))
        query = next(kw for name, kw in gmail.calls if name == "list")["q"]
        self.assertIn("newer_than:3d", query)
        self.assertEqual(reader.skipped, {})

    def test_hamle_spoke_last_means_no_new_work(self):
        own = message("a2", 2000, sender=HAMLE, recipient=AHMET)
        own["labelIds"] = ["SENT"]
        sources, _ = self.read(Gmail([message("a1", 1000), own]))
        self.assertEqual(sources, [])

    def test_unverified_sender_is_skipped_not_guessed(self):
        sources, reader = self.read(Gmail([message("a1", 1000, sender="stranger@example.com")]))
        self.assertEqual(sources, [])
        self.assertTrue(reader.skipped)


if __name__ == "__main__":
    unittest.main()
