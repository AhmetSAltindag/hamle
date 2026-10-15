"""Hamle'nin kendi karnesi: fakes only, no sockets, Gmail, Basecamp or models."""
import ast
import copy
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
import io
import json
import socket
import unittest
from unittest.mock import patch

import v11_daily_digest as dd
import v11_hamle_karne as hk
from cloud_store import OperationsStore
from test_cloud_store import MemoryBackend, budget_snapshot
from test_v11_daily_digest import Gmail
from v11_chain_live import load_catalog

MON_1045 = datetime.fromisoformat("2026-11-02T10:45:00+03:00")  # Sunday 1 Nov: Monday 2 Nov is the first weekday
SUBJECT = "Hamle AI: Ekim 2026 Hamle Karnesi"
TOKEN = "synthetic-placeholder-token-never-live"


def ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def ts(text):
    return datetime.fromisoformat(text).timestamp()


def mail(brand, state, received, after=None, **kw):
    row = {"kind": "client_mail", "brand_key": brand, "reply_state": state, "received_at_ms": ms(received),
           "test": False}
    if after is not None:
        row["sent_at"] = ts(received) + after
    row.update(kw)
    return row


def config(**kw):
    c = copy.deepcopy(hk.load_config())
    c.update(kw)
    return c


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.now = datetime.fromisoformat("2026-10-12T09:00:00+03:00")
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: self.now)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.ops.resume_review_only("offline-test")
        self.ops.roll_budget_period()  # stamps the October period
        self.now = datetime.fromisoformat("2026-11-01T00:30:00+03:00")
        self.ops.roll_budget_period()  # October goes to period_history, as worker.py does before every run
        self.now = MON_1045
        self.catalog = load_catalog()
        self.gmail = Gmail()

        @contextmanager
        def token():
            yield TOKEN
        self.writer = dd.DigestMailWriter(token_context=token, session_factory=lambda: self.gmail)
        data = self.ops.backend.data
        data["client-day-20261013"] = {"message_keys": ["m1", "m2", "m3", "m4", "m5", "m6"]}
        data["client-day-20261014"] = {"message_keys": ["m7", "missing"]}
        data["client-day-20261101"] = {"message_keys": ["m8"]}  # November: not this karne's month
        data["m1"] = mail("wat_mobilite", "sent", "2026-10-13T10:00:00+03:00", 600)
        data["m2"] = mail("rabam", "sent", "2026-10-13T11:00:00+03:00", 3600, approved_by="Yönetici", edited=True)
        data["m3"] = mail("wat_mobilite", "rejected", "2026-10-13T12:00:00+03:00", decided_by="Yönetici")
        data["m4"] = mail("rabam", "manual", "2026-10-13T13:00:00+03:00", reply_reasons=["Alıcı doğrulanamadı"])
        data["m5"] = mail("wat_mobilite", "sent", "2026-10-13T14:00:00+03:00", 60, test=True)
        data["m6"] = mail("wat_mobilite", None, "2026-10-13T15:00:00+03:00", shadow=True)
        data["m7"] = mail("wat_mobilite", "sent", "2026-10-14T09:00:00+03:00", 1200)
        data["m8"] = mail("wat_mobilite", "sent", "2026-11-01T09:00:00+03:00", 30)
        data["client-jobs"] = {"keys": ["j1", "j2", "j3", "j4"]}
        data["j1"] = {"kind": "client_job", "brand_key": "wat_mobilite", "state": "delivered",
                      "opened_at": ts("2026-10-14T09:00:00+03:00"), "delivered_at": ts("2026-10-15T09:00:00+03:00"),
                      "accepted_by": "Hamle (92/100)", "scores": [{"at": 1, "score": 92, "round": 0}]}
        data["j2"] = {"kind": "client_job", "brand_key": "rabam", "state": "delivered",
                      "opened_at": ts("2026-10-16T09:00:00+03:00"), "delivered_at": ts("2026-10-20T09:00:00+03:00"),
                      "accepted_by": "Yönetici", "delivered_by": "team", "revision_rounds": 1, "revisions": 0,
                      "scores": [{"at": 1, "score": 70, "round": 0}, {"at": 2, "score": 85, "round": 1}],
                      "client_notes": [{"at": "21.10.2026 10:00"}, {"at": "01.09.2026 10:00"}]}
        data["j3"] = {"kind": "client_job", "brand_key": "wat_mobilite", "state": "team",
                      "created_at": "2026-10-21T09:00:00+03:00"}
        data["j4"] = {"kind": "client_job", "brand_key": "wat_mobilite", "state": "delivered", "test": True,
                      "opened_at": ts("2026-10-14T09:00:00+03:00"), "delivered_at": ts("2026-10-15T09:00:00+03:00")}
        data["brand-memory-wat_mobilite-202610"] = {"events": [
            {"date": "2026-10-03", "kind": "soz", "title": "a"}, {"date": "2026-10-09", "kind": "soz", "title": "b"},
            {"date": "2026-10-09", "kind": "teslim", "title": "c"}]}
        data["brand-profile-wat_mobilite"] = {"open_promises": [{"what": "Kasım takvimi", "due": "2026-11-05"}]}
        for d in ("20261013", "20261014"):
            data["daily-digest-" + d] = {"state": "sent"}
        data["daily-digest-20261015"] = {"state": "unknown"}
        data["weekly-summary-202642"] = {"state": "sent"}
        data["client-summary-20261013"] = {"state": "sent"}
        data["digital-karne-2026-10-19"] = {"brands": {"wat_mobilite": {"state": "ready"}, "rabam": {"state": "ready"},
                                                       "ya_da": {"state": "quiet"}}}
        data["teams-day-2026-10-15"] = {"model_calls": 7}

    def go(self, cfg=None, **kw):
        return hk.run_hamle_karne(self.ops, config=cfg or config(), catalog=self.catalog, writer=self.writer,
                                  clock=lambda: self.now, **kw)

    def text(self):
        return self.gmail.text().replace("\r\n", "\n")


class Window(Base):
    def test_first_weekday_window_one_mail_to_the_owners_never_twice(self):
        for when in ("2026-11-01T10:45:00+03:00", "2026-11-02T10:29:00+03:00", "2026-11-02T12:31:00+03:00",
                     "2026-11-03T10:45:00+03:00"):
            self.now = datetime.fromisoformat(when)
            r = self.go()
            self.assertEqual((r["status"], r["reason"], r["business_writes"]),
                             ("no_new_work", "outside_hamle_karne_window", 0), when)
        self.now = MON_1045
        r = self.go()
        self.assertEqual((r["status"], r["business_writes"], r["model_calls"]), ("hamle_karne_sent", 1, 0))
        msg = self.gmail.message()
        self.assertEqual(str(msg["Subject"]), SUBJECT)
        self.assertEqual(str(msg["To"]), ", ".join(hk.load_config()["recipients"]))
        self.assertEqual(self.ops.backend.data["hamle-karne-202610"]["state"], "sent")
        self.assertEqual(self.go()["status"], "already_recorded")
        self.assertEqual(len(self.gmail.posts()), 1)

    def test_disabled_and_preview(self):
        r = self.go(config(enabled=False))
        self.assertEqual((r["status"], r["reason"]), ("no_new_work", "hamle_karne_disabled"))
        self.now = datetime.fromisoformat("2026-11-10T22:00:00+03:00")
        r = self.go(preview=True)
        self.assertEqual(r["status"], "hamle_karne_preview")
        self.assertTrue(r["subject"].startswith("[ÖNİZLEME] "))
        self.assertNotIn("hamle-karne-202610", self.ops.backend.data)
        self.assertEqual(self.gmail.posts(), [])


class Numbers(Base):
    def test_numbers_are_computed_from_the_records(self):
        self.go()
        text = self.text()
        for line in ("- Gelen müşteri maili: 5 (WAT Mobilite 3, Rabam 2)",
                     "- Hamle'nin kendi gönderdiği yanıt: 2",
                     "- \"Hamle gönder\" onayıyla giden yanıt: 1 (1 tanesi düzeltilerek)",
                     "- Gönderilmeyen taslak: 1 (gönderilmemesi istendi 1, onay gelmedi 0, zincirde daha yeni mail "
                     "vardı 0)",
                     "- Ekibe bırakılan (Hamle yanıt yazmadı): 1",
                     "- Hamle'nin kendi yanıtları (2 yanıt): ortanca 10 dk, en uzunu 20 dk",
                     "- Onayla giden yanıtlar (1 yanıt): ortanca 1 sa",
                     "- Ekibin kendi yanıt süresi: henüz kaydedilmiyor.",
                     "- Açılan müşteri işi: 3",
                     "- Müşteriye teslim edilen: 2 (Hamle 90+ puanla onayladı 1, yönetici onayladı 1). Bu 2 teslimin "
                     "1 tanesi WhatsApp işiydi; dosyayı müşteriye ekip iletti",
                     "Dönem: 1 – 31 Ekim 2026 (müşteri maili kayıtları 13 Ekim'de başladı)",
                     "- İlk kontrolde geçen teslim: 1/2 (%50, yaklaşık)",
                     "- Hamle'nin tasarıma istediği revize turu: 1",
                     "- Müşterinin ek notu ya da revize isteği: 1",
                     "- Ekibe devredilen iş: 1",
                     "- Bu ay kayda geçen söz: 2", "- Bugün açık görünen söz: 1", "- WAT Mobilite: 2 yeni söz, 1 açık",
                     "- Sözün tutulup tutulmadığı: henüz kaydedilmiyor.",
                     "- Günlük özet: 2", "- Haftalık marka özeti: 1", "- Aylık marka raporu: 0", "- Dijital karne: 2",
                     "- Akşam müşteri maili özeti: 1", "- Teams'te cevaplanan mesaj (yaklaşık): 7",
                     "- Toplam: yaklaşık 7,8 saat (tahmini)",
                     "- Dijital karne: 2 × 90 dk = 3,0 saat",
                     "- İş başına dakikalar varsayımdır; ayar dosyasından değiştirilebilir."):
            self.assertIn(line, text)
        self.assertIn("Ekim ayında Hamle 5 müşteri mailini gördü; 2 tanesini kendisi yanıtladı, 1 tanesi ekip "
                      "onayıyla gitti.", text)
        self.assertIn("Kayıtlı harcama (model ve bulut): ", text)
        self.assertIn("Hamle'nin kazandırdığı ekip zamanı tahminen 7,8 saat.", text)
        self.assertIn(hk.FOOTER, text)
        self.assertIn("Hamle Karnesi", self.gmail.html())

    def test_saved_minutes_follow_the_config_table(self):
        minutes = dict(hk.load_config()["minutes_saved"], teams_answer=0, digital_karne=60)
        self.go(config(minutes_saved=minutes))
        text = self.text()
        self.assertIn("- Dijital karne: 2 × 60 dk = 2,0 saat", text)
        self.assertNotIn("Teams'te cevaplanan mesaj: 7 ×", text)

    def test_p90_needs_ten_replies_and_no_start_note_when_older_records_exist(self):
        data = self.ops.backend.data
        keys = []
        for n in range(10):
            data[f"a{n}"] = mail("wat_mobilite", "sent", "2026-10-20T09:00:00+03:00", 60 * (n + 1))
            keys.append(f"a{n}")
        data["client-day-20261020"] = {"message_keys": keys}
        data["client-day-20260925"] = {"message_keys": []}
        self.go()
        text = self.text()
        self.assertIn("- Hamle'nin kendi yanıtları (12 yanıt): ortanca 6 dk, 10 yanıttan 9'u 10 dk içinde", text)
        self.assertIn("Dönem: 1 – 31 Ekim 2026\n", text)

    def test_helpers(self):
        self.assertEqual([hk.duration(s) for s in (5, 600, 3660, 90000)], ["5 sn", "10 dk", "1 sa 1 dk", "1 gün 1 sa"])
        self.assertEqual(hk.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90), 9)
        self.assertEqual(hk.percentile([5], 50), 5)
        self.assertEqual(hk.usd(31_204_000), "31,20 USD")


class Config(Base):
    def test_only_agency_recipients_and_a_full_minutes_table(self):
        for cfg, reason in ((config(recipients=["patron@example.com"]), "karne_recipient_not_internal_agency"),
                            (config(recipients=[]), "karne_recipients_invalid"),
                            (config(minutes_saved={"daily_digest": 30}), "karne_minutes_invalid"),
                            (config(timezone="Mars/Olympus"), "karne_timezone_invalid"),
                            (config(window={"start": "12:30", "end": "10:30"}), "karne_window_invalid")):
            r = self.go(cfg)
            self.assertEqual((r["status"], r["reason"]), ("held", reason))
        self.assertEqual(self.gmail.posts(), [])

    def test_no_tenant_constant_in_the_code(self):
        """SaaS rule (2026-10-15): names, addresses, ids and the agency live in config, not in this module."""
        with open(hk.__file__, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        doc = ast.get_docstring(tree, clean=False)
        texts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                 and n.value != doc]
        for word in ("gamechangers", "ahmet", "cansu", "özge", "wat", "rabam", "istanbul", ".com"):
            self.assertFalse([t for t in texts if word in t.lower()], word)
        self.assertFalse([n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and type(n.value) is int
                          and n.value > 1_000_000 and n.value != 1_000_000])


class Wiring(unittest.TestCase):
    def test_worker_flags_statuses_and_package(self):
        import package_chain_source as pkg
        import worker
        for argv, reason in ((["--mode", "hamle_karne"], "hamle_karne_runs_inside_monthly_report"),
                             (["--mode", "monthly_report", "--hamle-karne-preview"],
                              "hamle_karne_preview_requires_hamle_karne")):
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(worker.main(argv), 2)
            self.assertEqual(json.loads(buf.getvalue())["reason"], reason, argv)
        for status in ("hamle_karne_sent", "hamle_karne_preview"):
            self.assertIn(status, worker.OK_STATUSES)
        self.assertIn("v11_hamle_karne.py", pkg.CHAIN_FILES)
        self.assertIn(pkg.APP + "chain-profile/hamle-karne-config.json", pkg.ALLOWLIST)


if __name__ == "__main__":
    unittest.main()
