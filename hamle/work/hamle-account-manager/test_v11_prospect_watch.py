"""Prospect watch: fakes only (no sockets, no keys, no Gmail/Basecamp/Anthropic). All names are fictional."""
import copy
import json
import re
import socket
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import model_routing
import v11_prospect_watch as pw
from autonomy_policy import HAMLE, scope_digest
from cloud_store import OperationsStore
from test_anthropic_routing import FakeClient, KEY, message, usage
from test_cloud_store import MemoryBackend, budget_snapshot
from v11_nb_tracking import _html

SAT_1000 = datetime.fromisoformat("2026-10-17T10:00:00+03:00")
OWNER, LEAD, REP, OTHER = ("selin@gamechangers.com.tr", "emre@gamechangers.com.tr", "tuna@gamechangers.com.tr",
                           "irmak@gamechangers.com.tr")
OWNER_ID, LEAD_ID, REP_ID, OTHER_ID, HAMLE_ID = 1001, 1002, 1003, 1004, 5000
ACCOUNT, PROJECT, TODOSET, NOTES, RESEARCH = 999, 40704547, 77, 11, 12
AUTH = ["mx.google.com; dkim=pass header.i=@{d} header.s=s1; spf=pass smtp.mailfrom=x@{d}; dmarc=pass header.from={d}"]


def agency(email, name, bc, member=True):
    return {"email": email, "name": name, "side": "agency", "verified": True, "can_receive": True,
            "can_request": True, "basecamp_id": bc, "basecamp_verified": True, "project_member": member}


CATALOG = {"contacts": [agency(OWNER, "Selin Demir", OWNER_ID), agency(LEAD, "Emre Aydın", LEAD_ID),
                        agency(REP, "Tuna Yıldız", REP_ID), agency(OTHER, "Irmak Su", OTHER_ID, member=False)],
           "basecamp": {"account_id": ACCOUNT, "hamle_person_id": HAMLE_ID},
           "brand_routes": {"deneme_sut": {"brand_name": "Deneme Süt", "aliases": [], "rep_person_id": REP_ID},
                            "kurgu_finans": {"brand_name": "Kurgu Finans", "aliases": ["Kurgu"], "rep_person_id": 7},
                            "gamechangers": {"brand_name": "Gamechangers", "aliases": []}},
           "budget": {"internal_live_cap_microdollars": 50_000_000}}


def config(**kw):
    c = pw.load_config()
    c = copy.deepcopy(c)
    c.update(enabled=True, roles={"owner": OWNER, "new_business_lead": LEAD}, since="2026-10-01T00:00:00+03:00",
             client_domains={"denemesut.com": "deneme_sut"}, own_brand_keys=["gamechangers"])
    for k, v in kw.items():
        c[k] = v
    return c


class Book:
    """The client contact table (exact addresses + company domains)."""
    domains = {"musteri-marka.com.tr": "kurgu_finans"}
    emails = {"bireysel.musteri@gmail.com"}

    def match(self, address):
        return "brand" if address in self.emails or address.split("@")[1] in self.domains else None


def mail(n, *, sender="deniz@acmeyapi.com.tr", name="Deniz Kaya", subject="Hamle AI demo talebi", text=None,
         thread=None, headers=None, to=None, ms=None, **extra):
    domain = sender.split("@")[1]
    row = {"message_id": f"a{n}", "thread_id": thread or f"b{n}", "sender": sender, "sender_name": name,
           "to": to or [HAMLE], "cc": [], "subject": subject,
           "text": text or "Merhaba,\nSitenizi gördüm, Hamle AI için bir demo rica ediyoruz.\nDeniz",
           "received_at_ms": ms or int(SAT_1000.timestamp() * 1000) - 60_000 + n, "fingerprint": f"fp{n}",
           "has_attachments": False,
           "headers": headers if headers is not None else {"authentication-results": [AUTH[0].format(d=domain)]},
           "thread_agency": False, "thread_hamle": False}
    row.update(extra)
    return row


def reply_text(first="Deniz", company="Acme Yapı", owner="Selin", extra="", lang="tr"):
    if lang == "en":
        return (f"Hi {first},\nThank you for writing to us! We would love to hear more about {company}.\n"
                f"Could you share two time slots that suit you this week? {owner} will join the meeting too.{extra}\n"
                "Best,\nHamle AI\nGamechangers")
    return (f"Merhaba {first},\nBize yazdığınız için çok teşekkürler! {company} gibi büyüyen bir markada iletişimin "
            f"hızlı akması çok önemli.\nSize kısa bir tanışma önermek isteriz. Bu hafta uygun olduğunuz iki zaman "
            f"dilimini yazar mısınız? {owner} de görüşmede olacak.{extra}\nSevgiler,\nHamle AI\nGamechangers")


SENTENCE = "Kurumunuz için istediğiniz araştırmayı da not aldık; ekibimiz değerlendirip size ayrıca dönecek."


def classified(category="demo_or_first_contact", language="tr", target="none", claimed=None):
    return {"category": category, "language": language, "request_summary": "Hamle AI için demo istiyor.",
            "research_target": target, "claimed_company": claimed}


def researched(title="Pazarlama Direktörü", match="clear", person_url="https://acmeyapi.com.tr/ekip"):
    return {"company": {"name": "Acme Yapı", "sector": "Yapı malzemeleri",
                        "summary": "Konut projeleri için yapı malzemesi üretir.", "url": "https://acmeyapi.com.tr"},
            "person": {"title": title, "match": match, "url": person_url},
            "evidence": ["https://acmeyapi.com.tr", "https://acmeyapi.com.tr/ekip"]}


def reported(mail_text=None):
    return {"summary": "Kurum yapı malzemesi üretiyor; dijitalde katalog ağırlıklı.",
            "observations": [{"text": "Sitede ürün kataloğu öne çıkıyor.", "url": "https://acmeyapi.com.tr"},
                             {"text": "Uydurma gözlem.", "url": "https://baska.example.com/x"}],
            "campaigns": [{"text": "Bahar kataloğu kampanyası.", "url": "https://acmeyapi.com.tr/bahar",
                           "date": "2026-04-01"}],
            "first_moves": [{"text": "Katalogu kısa videolara bölmek.", "basis_url": "https://acmeyapi.com.tr"},
                            {"text": "Proje hikâyeleri.", "basis_url": "https://acmeyapi.com.tr/bahar"},
                            {"text": "Arama görünürlüğü.", "basis_url": "https://acmeyapi.com.tr"}],
            "mail": mail_text if mail_text is not None else
            "Merhaba Deniz,\nİstediğiniz değerlendirmeyi hazırladık: https://acmeyapi.com.tr\nSevgiler,\nHamle AI\nGamechangers",
            "evidence": ["https://acmeyapi.com.tr", "https://acmeyapi.com.tr/bahar"]}


class Models:
    def __init__(self):
        self.calls = []
        self.out = {"classify": [classified()], "research": [researched()], "reply": [{"reply": reply_text()}],
                    "report": [reported()]}
        self.budget = {"classify": True, "research": True, "reply": True, "report": True}
        self.models = {}
        for kind in self.out:
            setattr(self, kind, self._make(kind))

    def _make(self, kind):
        def call(request):
            self.calls.append((kind, copy.deepcopy(request)))
            queue = self.out[kind]
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return copy.deepcopy(item)
        return call

    def budget_ok(self, kind):
        return self.budget[kind]

    def kinds(self):
        return [k for k, _ in self.calls]


class Basecamp:
    """In-memory Basecamp for the project: lists, to-dos and comments (exact readback shapes)."""
    def __init__(self):
        self.lists = [{"id": NOTES, "name": "Hamle İşleri", "visible_to_clients": False, "status": "active"},
                      {"id": RESEARCH, "name": "Hamle Araştırma İstekleri", "visible_to_clients": False,
                       "status": "active"},
                      {"id": 13, "name": "Başka", "visible_to_clients": True, "status": "active"}]
        self.todos, self.comments, self.calls, self.next = {}, {}, [], 100
        self.fail_post = False

    def human(self, todo_id, person, text):
        self.next += 1
        self.comments[self.next] = {"id": self.next, "parent": {"id": todo_id}, "creator": {"id": person},
                                    "content": _html(text), "bucket": {"id": PROJECT}, "visible_to_clients": False}
        return self.next

    def send(self, method, url, body, expected):
        self.calls.append((method, url, copy.deepcopy(body)))
        path = url.split("3.basecampapi.com", 1)[1].split("?")[0]
        b = f"/{ACCOUNT}/buckets/{PROJECT}"
        if method == "POST" and self.fail_post:
            raise RuntimeError("basecamp_down")
        if path == f"/{ACCOUNT}/projects/{PROJECT}.json":
            return {"id": PROJECT, "dock": [{"name": "todoset", "id": TODOSET, "enabled": True}]}, {}
        if path == f"{b}/todosets/{TODOSET}/todolists.json":
            return copy.deepcopy(self.lists), {}
        m = re.fullmatch(rf"{b}/todolists/(\d+)/todos\.json", path)
        if m and method == "POST":
            self.next += 1
            self.todos[self.next] = {"id": self.next, "content": body["content"], "description": body["description"],
                                     "parent": {"id": int(m[1]), "type": "Todolist"}, "bucket": {"id": PROJECT},
                                     "creator": {"id": HAMLE_ID}, "visible_to_clients": False,
                                     "assignees": [{"id": i} for i in body["assignee_ids"]],
                                     "due_on": body.get("due_on")}
            return {"id": self.next}, {}
        m = re.fullmatch(rf"{b}/todos/(\d+)\.json", path)
        if m:
            return copy.deepcopy(self.todos[int(m[1])]), {}
        m = re.fullmatch(rf"{b}/recordings/(\d+)/comments\.json", path)
        if m and method == "POST":
            self.next += 1
            self.comments[self.next] = {"id": self.next, "parent": {"id": int(m[1])}, "creator": {"id": HAMLE_ID},
                                        "content": body["content"], "bucket": {"id": PROJECT},
                                        "visible_to_clients": False}
            return {"id": self.next}, {}
        if m:
            return [copy.deepcopy(c) for c in self.comments.values() if c["parent"]["id"] == int(m[1])], {}
        m = re.fullmatch(rf"{b}/comments/(\d+)\.json", path)
        if m:
            return copy.deepcopy(self.comments[int(m[1])]), {}
        raise AssertionError("unexpected Basecamp call " + method + " " + url)

    def todo_text(self, tid):
        from v11_client_reply import comment_text
        return comment_text(self.todos[tid]["description"])

    def comment_text(self, cid):
        from v11_client_reply import comment_text
        return comment_text(self.comments[cid]["content"])

    def posted(self, list_id):
        return [t for t in self.todos.values() if t["parent"]["id"] == list_id]


class ReplyWriter:
    def __init__(self):
        self.preflights, self.writes, self.fail, self.blocked = [], [], None, None
        self.anchor = None

    def preflight(self, kind, payload):
        self.preflights.append((kind, copy.deepcopy(payload)))
        if self.blocked:
            raise self.blocked
        return True

    def write(self, payload):
        self.writes.append(copy.deepcopy(payload))
        if self.fail:
            raise self.fail
        return {"remote_id": "f%d" % len(self.writes)}

    def readback(self, remote_id, payload):
        return {"readback_sha256": scope_digest(payload)}

    def latest_anchor(self, thread_id):
        return copy.deepcopy(self.anchor)


class CopyWriter(ReplyWriter):
    pass


class FakeWeb:
    def __getattr__(self, name):
        from v11_digital_karne import KarneError
        raise KarneError("offline")


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start()
            self.addCleanup(p.stop)
        self.now = SAT_1000
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: self.now)
        self.ops.initialize_control()
        self.ops.initialize_budget(budget_snapshot())
        self.ops.resume_review_only("offline-test")
        self.book = Book()
        self.cfg = config()
        self.settings = pw.resolve(self.cfg, CATALOG, self.book)
        self.mails, self.people = [], []
        self.models, self.bc = Models(), Basecamp()
        self.writer, self.copies = ReplyWriter(), CopyWriter()
        self.web = None

    def reader(self):
        return copy.deepcopy(self.mails)

    def watch(self, settings=None):
        s = settings or self.settings
        return pw.ProspectWatch(self.ops, settings=s, book=self.book, people=self.people, reader=self.reader,
                                models=self.models, http=pw.basecamp_http(s, send=self.bc.send),
                                reply_writer=self.writer, copy_writer=self.copies, clock=lambda: self.now,
                                web=self.web, monotonic=lambda: 0.0)

    def run_once(self, settings=None):
        return self.watch(settings).run()

    def get(self, key):
        return self.ops._run(lambda tx, _: tx.get(key), allow_expired=True)

    def note(self):
        notes = self.bc.posted(NOTES)
        self.assertEqual(len(notes), 1)
        return notes[0], self.bc.todo_text(notes[0]["id"])


# ------------------------------------------------------------------ pure rules
class Prefilter(Base):
    def check(self, headers, sender="deniz@acmeyapi.com.tr"):
        h = {"authentication-results": [AUTH[0].format(d=sender.split("@")[1])]}
        h.update(headers)
        return pw.prefilter(h, sender, self.settings)

    def test_clean_mail_passes(self):
        self.assertIsNone(self.check({}))
        self.assertIsNone(self.check({"auto-submitted": ["no"]}))

    def test_automatic_and_bulk_mail_is_skipped_without_a_model(self):
        self.assertEqual(self.check({"auto-submitted": ["auto-replied"]}), "auto_submitted")
        self.assertEqual(self.check({"auto-submitted": ["auto-generated"]}), "auto_submitted")
        self.assertEqual(self.check({"x-autoreply": ["yes"]}), "auto_reply")
        self.assertEqual(self.check({"x-autorespond": ["1"]}), "auto_reply")
        for value in ("bulk", "List", " junk "):
            self.assertEqual(self.check({"precedence": [value]}), "bulk")
        self.assertEqual(self.check({"list-unsubscribe": ["<mailto:u@x.com>"]}), "list_mail")
        self.assertEqual(self.check({"list-id": ["<news.acme>"]}), "list_mail")

    def test_noreply_like_senders_and_ignored_domains(self):
        for local in ("noreply", "no-reply", "do-not-reply", "mailer-daemon", "postmaster", "newsletter"):
            self.assertEqual(self.check({}, sender=f"{local}@acmeyapi.com.tr"), "noreply_sender", local)
        self.assertEqual(self.check({}, sender="biri@app.basecamp.com"), "ignored_domain")
        self.assertEqual(pw.prefilter({}, "bozuk-adres", self.settings), "sender_invalid")

    def test_authentication_results(self):
        d = "acmeyapi.com.tr"
        verdict = lambda v: pw.auth_verdict(v, "deniz@" + d, "mx.google.com")  # noqa: E731
        self.assertIsNone(verdict([AUTH[0].format(d=d)]))
        self.assertEqual(verdict([]), "auth_results_missing")
        self.assertEqual(verdict(None), "auth_results_missing")
        # a line from another server (a sender can add its own) is ignored
        self.assertEqual(verdict([f"evil.example; dmarc=pass header.from={d}"]), "auth_results_missing")
        self.assertEqual(verdict([f"mx.google.com; spf=fail smtp.mailfrom=x@{d}; dkim=pass header.i=@{d}"]),
                         "auth_fail")
        self.assertEqual(verdict([f"mx.google.com; dkim=pass header.i=@{d}; dmarc=fail header.from={d}"]),
                         "auth_fail")
        self.assertEqual(verdict(["mx.google.com; spf=softfail smtp.mailfrom=x@other.com"]), "auth_fail")
        self.assertIsNone(verdict([f"mx.google.com; dkim=pass (good) header.i=@mail.{d}"]))
        self.assertEqual(verdict(["mx.google.com; dkim=pass header.i=@bulkmailer.com; spf=pass "
                                  "smtp.mailfrom=b@bulkmailer.com"]), "auth_unverified")
        self.assertIsNone(verdict([f"mx.google.com; spf=pass smtp.mailfrom=bounce@{d}"]))

    def test_run_counts_filtered_mail_and_writes_nothing(self):
        self.mails = [mail(1, headers={"auto-submitted": ["auto-replied"],
                                       "authentication-results": [AUTH[0].format(d="acmeyapi.com.tr")]}),
                      mail(2, headers={}), mail(3, sender="no-reply@acmeyapi.com.tr"),
                      {"filtered": "list_mail", "thread_id": "c1", "message_id": "c2"}]
        r = self.run_once()
        self.assertEqual(r["filtered"], {"auto_submitted": 1, "auth_results_missing": 1, "noreply_sender": 1,
                                         "list_mail": 1})
        self.assertEqual(self.models.calls, [])
        self.assertEqual((self.bc.posted(NOTES), self.writer.writes), ([], []))
        self.assertIsNone(self.get(pw.mail_key("a1")))  # filtered mail leaves no durable record (KVKK)

    def test_mail_before_since_is_not_touched(self):
        self.mails = [mail(1, ms=int(datetime.fromisoformat("2026-09-30T10:00:00+03:00").timestamp() * 1000))]
        r = self.run_once()
        self.assertEqual(r["filtered"], {"before_since": 1})
        self.assertEqual(self.models.calls, [])


class DomainAndNames(Base):
    def test_company_comes_only_from_the_domain(self):
        self.assertEqual(pw.domain_kind("gmail.com", self.settings), ("individual", None))
        self.assertEqual(pw.domain_kind("denemesut.com", self.settings), ("client", "deneme_sut"))
        self.assertEqual(pw.domain_kind("tr.denemesut.com", self.settings), ("client", "deneme_sut"))
        self.assertEqual(pw.domain_kind("denemesut.com.evil.io", self.settings), ("corporate", None))
        self.assertEqual(pw.domain_kind("acmeyapi.com.tr", self.settings), ("corporate", None))
        s = pw.resolve(config(free_mail_domains=["kendi-postam.net"]), CATALOG, self.book)
        self.assertEqual(pw.domain_kind("kendi-postam.net", s), ("individual", None))

    def test_agency_and_known_client_contacts_are_not_prospects(self):
        for a in (OWNER, HAMLE, "yeni@gamechangers.com.tr", "x@sub.gamechangers.com.tr",
                  "bireysel.musteri@gmail.com", "ali@musteri-marka.com.tr"):
            self.assertTrue(pw.excluded(a, self.settings, self.book), a)
        for a in ("deniz@acmeyapi.com.tr", "baska@gmail.com", "x@denemesut.com"):
            self.assertFalse(pw.excluded(a, self.settings, self.book), a)

    def test_first_name(self):
        self.assertEqual(pw.first_name("Deniz Kaya"), "Deniz")
        self.assertEqual(pw.first_name('"Kaya, Deniz"'), "Deniz")
        self.assertEqual(pw.first_name("ece yıldırım"), "Ece")
        self.assertEqual(pw.first_name("ilker su"), "İlker")
        self.assertIsNone(pw.first_name("deniz@acmeyapi.com.tr"))
        self.assertIsNone(pw.first_name("123 Ltd"))
        self.assertIsNone(pw.first_name(None))
        self.assertEqual(pw.first_name("Ece K.", {"name": "Deniz Kaya"}), "Deniz")

    def test_meeting_phrase_vowel_harmony(self):
        self.assertEqual(pw.meeting_phrase("Selin", "tr"), "Selin de görüşmede olacak.")
        self.assertEqual(pw.meeting_phrase("Okan", "tr"), "Okan da görüşmede olacak.")
        self.assertEqual(pw.meeting_phrase("Selin", "en"), "Selin will join the meeting too.")

    def test_fold_and_approval(self):
        self.assertEqual(pw.fold("İSTANBUL ışık"), "istanbul isik")
        for text in ("Onaylıyorum", "ONAYLIYORUM", "  onaylıyorum. ", "<div>Onaylıyorum!</div>", "onayliyorum",
                     "ONAYLİYORUM"):
            self.assertTrue(pw.is_approval(text), text)
        for text in ("Onaylıyorum ama sonra", "onaylamıyorum", "Onay", "Hamle gönder", ""):
            self.assertFalse(pw.is_approval(text), text)


class ReplyChecks(Base):
    def check(self, text, **kw):
        args = dict(settings=self.settings, language="tr", first="Deniz", category="demo_or_first_contact",
                    kind="corporate", brand_key=None, include_research=False)
        args.update(kw)
        return pw.check_reply(text, **args)

    def test_good_reply_passes(self):
        self.assertEqual(self.check(reply_text()), [])
        self.assertEqual(self.check(reply_text(lang="en"), language="en"), [])
        self.assertEqual(self.check(reply_text(extra="\n" + SENTENCE), category="research_request",
                                    include_research=True), [])

    def test_each_rule(self):
        cases = {
            "imza eksik ya da farklı": reply_text().replace("Hamle AI\nGamechangers", "Hamle"),
            "selamlama eksik": reply_text().replace("Merhaba Deniz,", "Sayın Deniz,"),
            "ilk adla hitap yok": reply_text().replace("Merhaba Deniz,", "Merhaba,"),
            "Hanım/Bey ya da cinsiyetli hitap": reply_text(first="Deniz Hanım"),
            "fiyat ya da para ifadesi": reply_text(extra=" Fiyatlarımız uygundur."),
            "tarih, gün ya da söz ifadesi": reply_text(extra=" Salı 14:00 olur mu?"),
            "bağlantı, adres ya da telefon": reply_text(extra=" https://gamechangers.com.tr"),
            "araştırma yapıldığını ima ediyor": reply_text(extra=" LinkedIn profilinize baktık."),
            "başka marka adı geçiyor": reply_text(extra=" Kurgu Finans için de çalışıyoruz."),
            "toplantıda kimin olacağı yazmıyor": reply_text(owner="Biri"),
            "onaylı araştırma cümlesi yok": None,
        }
        for reason, text in cases.items():
            if text is None:
                continue
            self.assertTrue(any(p.startswith(reason) for p in self.check(text)), (reason, self.check(text)))
        self.assertIn("onaylı araştırma cümlesi yok", self.check(reply_text(), category="research_request",
                                                                  include_research=True))
        self.assertIn("araştırma cümlesi yanlış yerde", self.check(reply_text(extra=" " + SENTENCE)))
        self.assertTrue(any(p.startswith("iç ifade") for p in self.check(reply_text(extra=" Basecamp'e yazdık."))))
        self.assertIn("cevap çok uzun", self.check(reply_text(extra=" uzun" * 300)))

    def test_known_brand_may_be_named_only_for_its_own_domain(self):
        text = reply_text(company="Deneme Süt")
        self.assertIn("başka marka adı geçiyor", self.check(text))
        self.assertEqual(self.check(text, kind="client", brand_key="deneme_sut",
                                    category="known_client_new_person"), [])

    def test_individuals_need_no_meeting_line(self):
        text = "Merhaba Deniz,\nYazdığınız için teşekkürler! Hangi şirketten yazdığınızı paylaşır mısınız?\n" \
               "Sevgiler,\nHamle AI\nGamechangers"
        self.assertEqual(self.check(text, kind="individual"), [])


class Facts(Base):
    def test_company_facts_need_a_source_or_the_own_site(self):
        self.assertEqual(pw.company_facts(researched(), "acmeyapi.com.tr")["name"], "Acme Yapı")
        out = researched()
        out["company"]["url"] = "https://uydurma.example.com"
        self.assertIsNone(pw.company_facts(out, "acmeyapi.com.tr"))
        self.assertIsNone(pw.company_facts(None, "acmeyapi.com.tr"))

    def test_person_title_only_on_a_clear_professional_match(self):
        self.assertEqual(pw.person_facts(researched())["title"], "Pazarlama Direktörü")
        self.assertIsNone(pw.person_facts(researched(match="unclear"))["title"])
        social = researched(person_url="https://www.instagram.com/denizkaya")
        social["evidence"].append("https://www.instagram.com/denizkaya")
        p = pw.person_facts(social)
        self.assertEqual((p["title"], p["url"], p["dropped_social"]), (None, None, True))
        self.assertIsNone(pw.person_facts(researched(person_url="https://kaynak-yok.example.com"))["title"])

    def test_reply_input_is_isolated(self):
        req = pw.reply_input(today=SAT_1000.date(), language="tr", category="demo_or_first_contact",
                             kind="corporate", first="Deniz", company=pw.company_facts(researched(), "acmeyapi.com.tr"),
                             brand_name="Deneme Süt", known={"name": "Deniz Kaya", "note": "gizli not",
                                                             "where_met": "bir panelde"},
                             include_research=False, source=mail(1))
        self.assertEqual(tuple(req), pw.REPLY_INPUT_KEYS)
        blob = json.dumps(req, ensure_ascii=False)
        for secret in ("Deneme Süt", "gizli not", "Pazarlama Direktörü", "https://", OWNER):
            self.assertNotIn(secret, blob)
        self.assertEqual(req["known_person"], {"where_met": "bir panelde"})


class Config(Base):
    def test_repository_config_is_valid_and_off(self):
        cfg = pw.load_config()
        self.assertIs(pw.validate_config(cfg)["enabled"], False)
        self.assertEqual(cfg["basecamp"]["project_id"], 40704547)
        self.assertEqual(cfg["signatures"]["tr"], "Sevgiler,\nHamle AI\nGamechangers")
        self.assertEqual(cfg["signatures"]["en"], "Best,\nHamle AI\nGamechangers")
        self.assertEqual(cfg["approved_sentences"]["research_request"]["tr"], SENTENCE)
        self.assertEqual(cfg["limits"]["max_auto_replies_per_day"], 10)
        self.assertEqual(cfg["window"], {"start": "08:00", "end": "22:00"})
        self.assertEqual(cfg["retention_days"], 365)

    def test_repository_config_resolves_against_the_repository_catalog(self):
        from v11_chain_live import load_catalog
        s = pw.resolve(pw.load_config(), load_catalog())
        self.assertEqual(set(s["roles"]), {"owner", "new_business_lead"})
        self.assertTrue(all(type(r["basecamp_id"]) is int for r in s["roles"].values()))

    def test_invalid_configs_fail_closed(self):
        bad = {
            "prospect_mailbox_mismatch": dict(mailbox="baska@gamechangers.com.tr"),
            "prospect_roles_invalid": dict(roles={"owner": "x@baska.com", "new_business_lead": LEAD}),
            "prospect_window_invalid": dict(window={"start": "22:00", "end": "08:00"}),
            "prospect_limits_invalid": dict(limits=dict(config()["limits"], max_auto_replies_per_day=500)),
            "prospect_signatures_invalid": dict(signatures={"tr": "Sevgiler"}),
            "prospect_retention_invalid": dict(retention_days=5000),
            "prospect_since_invalid": dict(since="2026-10-16T08:00:00"),
            "prospect_basecamp_invalid": dict(basecamp={"project_id": 1, "notes_list": "A liste",
                                                        "research_list": "a lıste"}),
        }
        for code, kw in bad.items():
            with self.assertRaises(pw.ProspectError, msg=code) as ctx:
                pw.validate_config(config(**kw))
            self.assertEqual(ctx.exception.code, code)
        with self.assertRaises(pw.ProspectError):
            pw.validate_config(dict(config(), extra=True))

    def test_roles_must_be_verified_basecamp_members(self):
        with self.assertRaises(pw.ProspectError) as ctx:
            pw.resolve(config(roles={"owner": OWNER, "new_business_lead": OTHER}), CATALOG, self.book)
        self.assertEqual(ctx.exception.code, "prospect_role_unverified:new_business_lead")
        with self.assertRaises(pw.ProspectError):
            pw.resolve(config(client_domains={"x.com": "bilinmeyen"}), CATALOG, self.book)

    def test_brand_lead_falls_back_to_the_new_business_lead(self):
        self.assertEqual(self.settings["brands"]["deneme_sut"]["lead_id"], REP_ID)
        self.assertEqual(self.settings["brands"]["kurgu_finans"]["lead_id"], LEAD_ID)
        self.assertNotIn("gamechangers", self.settings["brands"])


class KnownPeople(Base):
    DOC = {"schema_version": 1, "people": [
        {"name": "Deniz Kaya", "company": "Acme Yapı", "title": "Genel Müdür", "where_met": "bir sektör panelinde",
         "note": "Eski bir tanıdık", "emails": ["deniz@acmeyapi.com.tr"]},
        {"name": "Ece Yıldırım", "company": "Örnek Holding", "domains": ["ornekholding.com"]}]}

    def test_import_and_match(self):
        r = pw.import_people(self.ops, self.DOC, actor=OWNER, settings=self.settings)
        self.assertEqual((r["status"], r["people"]), ("prospect_people_imported", 2))
        self.assertEqual(pw.import_people(self.ops, self.DOC, actor=OWNER, settings=self.settings)["status"],
                         "already_current")
        people = pw.load_people(self.ops)
        self.assertEqual(pw.match_person(people, "deniz@acmeyapi.com.tr", None, "acmeyapi.com.tr")["title"],
                         "Genel Müdür")
        self.assertEqual(pw.match_person(people, "e.y@ornekholding.com", "ECE  YILDIRIM", "ornekholding.com")["name"],
                         "Ece Yıldırım")
        self.assertIsNone(pw.match_person(people, "e.y@baska.com", "Ece Yıldırım", "baska.com"))
        self.assertEqual(pw.people_status(self.ops)["people"], 2)

    def test_bad_rows_and_actor(self):
        with self.assertRaises(pw.ProspectError):
            pw.import_people(self.ops, self.DOC, actor="dis@baska.com", settings=self.settings)
        with self.assertRaises(pw.ProspectError):
            pw.validate_people({"schema_version": 1, "people": [{"name": "Kimse"}]})
        with self.assertRaises(pw.ProspectError):
            pw.validate_people({"schema_version": 1, "people": [{"name": "Deniz", "age": 3, "emails": ["a@b.co"]}]})

    def test_known_person_skips_the_person_search_and_warms_the_reply(self):
        pw.import_people(self.ops, self.DOC, actor=OWNER, settings=self.settings)
        self.people = pw.load_people(self.ops)
        self.mails = [mail(1, name="D. K.")]
        self.run_once()
        research = dict(self.models.calls)["research"]
        self.assertIs(research["search_person"], False)
        self.assertIsNone(research["sender_name"])
        reply = dict(self.models.calls)["reply"]
        self.assertEqual((reply["first_name"], reply["known_person"]), ("Deniz", {"where_met": "bir sektör panelinde"}))
        _, text = self.note()
        self.assertIn("Tanıdığımız kişi: Deniz Kaya · nerede: bir sektör panelinde · not: Eski bir tanıdık", text)


# ------------------------------------------------------------------ the run
class FirstContact(Base):
    def test_corporate_demo_gets_one_reply_a_copy_and_a_note(self):
        self.mails = [mail(1)]
        r = self.run_once()
        self.assertEqual((r["status"], r["replied"], r["model_calls"]), ("prospect_watch_completed", 1, 3))
        self.assertEqual(self.models.kinds(), ["classify", "research", "reply"])
        research = dict(self.models.calls)["research"]
        self.assertEqual(research, {"domain": "acmeyapi.com.tr", "website": "https://acmeyapi.com.tr",
                                    "sender_name": "Deniz Kaya", "search_person": True})
        self.assertNotIn("Deniz", json.dumps(dict(self.models.calls)["classify"]["domain"]))
        # the reply: same thread, to the sender, the owner always in CC (owner 2026-10-16), approved signature
        self.assertEqual(len(self.writer.writes), 1)
        sent = self.writer.writes[0]
        self.assertEqual((sent["to"], sent["cc"], sent["thread_id"], sent["in_reply_to_message_id"]),
                         (["deniz@acmeyapi.com.tr"], [OWNER], "b1", "a1"))
        self.assertEqual(sent["subject"], "Re: Hamle AI demo talebi")
        self.assertTrue(sent["body"].endswith("Sevgiler,\nHamle AI\nGamechangers"))
        self.assertEqual(self.writer.preflights[0][0], "client_reply")
        # the person's title never reaches the reply model or the reply
        reply_req = json.dumps(dict(self.models.calls)["reply"], ensure_ascii=False)
        self.assertNotIn("Pazarlama Direktörü", reply_req)
        self.assertNotIn("Pazarlama Direktörü", sent["body"])
        # the owner's copy is a separate internal mail
        self.assertEqual(len(self.copies.writes), 1)
        self.assertEqual(self.copies.writes[0]["to"], [OWNER])
        self.assertIn(sent["body"], self.copies.writes[0]["body"])
        # the note: notes list, new business lead + owner, everything the team needs
        todo, text = self.note()
        self.assertEqual(sorted(a["id"] for a in todo["assignees"]), [OWNER_ID, LEAD_ID])
        self.assertIn("Acme Yapı (acmeyapi.com.tr)", todo["content"])
        for part in ("GÖNDERİLDİ", "Gönderen: Deniz Kaya <deniz@acmeyapi.com.tr>", "Tür: demo / ilk temas",
                     "Ne istiyor: Hamle AI için demo istiyor.", "unvan Pazarlama Direktörü",
                     "Şirket hakkında: Yapı malzemeleri", sent["body"].split("\n")[1], "> Merhaba,",
                     f"CC'de {OWNER}. Ayrı kopyası da gitti."):
            self.assertIn(part, text)
        row = self.get(pw.mail_key("a1"))
        self.assertEqual((row["state"], row["reply_state"], row["kind"]), ("noted", "sent", "prospect_mail"))
        self.assertTrue(self.get(pw.addr_key("deniz@acmeyapi.com.tr"))["replied"])
        self.assertEqual(self.get(pw.day_key(SAT_1000.date()))["auto_replies"], 1)

    def test_same_mail_twice_is_handled_once(self):
        self.mails = [mail(1)]
        self.run_once()
        r = self.run_once()
        self.assertEqual((r["replied"], r["skipped"]), (0, 1))
        self.assertEqual((len(self.writer.writes), len(self.models.calls), len(self.bc.todos)), (1, 3, 1))

    def test_one_automatic_reply_per_address_ever(self):
        self.mails = [mail(1)]
        self.run_once()
        todo_id = self.note()[0]["id"]
        self.mails = [mail(1), mail(2, thread="b9", text="Bir de şunu sormak istiyorum.")]
        r = self.run_once()
        self.assertEqual(r["notes"], 1)
        self.assertEqual(len(self.writer.writes), 1)  # no second reply
        self.assertEqual(len(self.models.calls), 3)  # no model call for the follow-up
        comment = next(c for c in self.bc.comments.values() if c["parent"]["id"] == todo_id)
        self.assertIn("Aynı kişi yeniden yazdı", self.bc.comment_text(comment["id"]))
        self.assertIn("> Bir de şunu sormak istiyorum.", self.bc.comment_text(comment["id"]))

    def test_daily_cap_turns_into_a_human_note(self):
        self.ops._run(lambda tx, now: tx.put(pw.day_key(SAT_1000.date()), {"auto_replies": 10}))
        self.mails = [mail(1)]
        r = self.run_once()
        self.assertEqual((r["replied"], r["notes"]), (0, 1))
        self.assertEqual(self.writer.writes, [])
        self.assertEqual(self.models.kinds(), ["classify"])
        _, text = self.note()
        self.assertIn("Bugünkü otomatik cevap sınırı (10) doldu", text)

    def test_spam_and_vendor_are_logged_only(self):
        self.models.out["classify"] = [classified("spam"), classified("vendor_or_job")]
        self.mails = [mail(1, sender="a@spamci.biz"), mail(2, sender="b@tedarik.com.tr")]
        r = self.run_once()
        self.assertEqual((r["logged"], r["replied"], r["notes"]), (2, 0, 0))
        self.assertEqual(self.models.kinds(), ["classify", "classify"])
        self.assertEqual((self.writer.writes, self.bc.todos), ([], {}))
        self.assertIsNone(self.get(pw.mail_key("a1"))["digest_line"])
        self.assertIn("tedarikçi / iş başvurusu: b@tedarik.com.tr", self.get(pw.mail_key("a2"))["digest_line"])

    def test_unsure_gets_no_reply_and_a_note_to_the_lead(self):
        self.models.out["classify"] = [classified("unsure")]
        self.mails = [mail(1)]
        r = self.run_once()
        self.assertEqual((r["replied"], r["notes"]), (0, 1))
        self.assertEqual(self.writer.writes, [])
        todo, text = self.note()
        self.assertIn("emin olamadı", text)
        self.assertEqual(sorted(a["id"] for a in todo["assignees"]), [OWNER_ID, LEAD_ID])

    def test_classifier_failure_is_unsure(self):
        self.models.out["classify"] = [RuntimeError("model down")]
        self.mails = [mail(1)]
        self.run_once()
        self.assertEqual(self.writer.writes, [])
        self.assertIn("Sınıflandırma: failed:RuntimeError", self.note()[1])

    def test_failed_reply_check_sends_nothing_and_opens_a_human_note(self):
        self.models.out["reply"] = [{"reply": reply_text(extra=" Fiyatımız 1.000 TL.")}]
        self.mails = [mail(1)]
        r = self.run_once()
        self.assertEqual((r["replied"], r["notes"]), (0, 1))
        self.assertEqual((self.writer.writes, self.writer.preflights, self.copies.writes), ([], [], []))
        _, text = self.note()
        self.assertIn("İNSAN BAKMALI", text)
        self.assertIn("Hamle'nin cevap taslağı (GÖNDERİLMEDİ):", text)
        self.assertIn("fiyat ya da para ifadesi", text)
        self.assertIsNone(self.get(pw.addr_key("deniz@acmeyapi.com.tr")))  # a human may still answer

    def test_preflight_block_sends_nothing(self):
        self.writer.blocked = RuntimeError("newer_incoming_mail_in_thread")
        self.mails = [mail(1)]
        self.run_once()
        self.assertEqual(self.writer.writes, [])
        self.assertIn("Gönderim öncesi kontrol geçmedi", self.note()[1])

    def test_unconfirmed_send_is_never_retried(self):
        self.writer.fail = RuntimeError("timeout")
        self.mails = [mail(1)]
        r = self.run_once()
        self.assertEqual(r["replied"], 1)
        self.assertIn("GÖNDERİM BELİRSİZ", self.note()[1])
        self.assertEqual(self.get(pw.mail_key("a1"))["reply_state"], "send_unknown")
        self.run_once()
        self.assertEqual(len(self.writer.writes), 1)

    def test_individual_is_asked_for_the_company_and_not_researched(self):
        self.models.out["classify"] = [classified("research_request", target="organisation", claimed="Deneme Süt")]
        self.models.out["reply"] = [{"reply": "Merhaba Deniz,\nYazdığınız için teşekkürler! Hangi kurumdan "
                                              "yazdığınızı paylaşır mısınız?\nSevgiler,\nHamle AI\nGamechangers"}]
        self.mails = [mail(1, sender="deniz.kaya@gmail.com")]
        self.run_once()
        self.assertEqual(self.models.kinds(), ["classify", "reply"])
        req = dict(self.models.calls)["reply"]
        self.assertEqual((req["domain_kind"], req["company"], req["known_brand"], req["include_research_sentence"]),
                         ("individual", None, None, False))
        self.assertEqual(len(self.writer.writes), 1)
        self.assertEqual(self.bc.posted(RESEARCH), [])  # no research request for an individual
        todo, text = self.note()
        self.assertIn("bireysel (gmail.com)", todo["content"])
        self.assertIn("Kendini tanıttığı şirket: Deneme Süt (alan adı bunu doğrulamıyor)", text)

    def test_gmail_claiming_a_client_brand_is_not_that_brand(self):
        self.models.out["classify"] = [classified("known_client_new_person", claimed="Deneme Süt")]
        self.models.out["reply"] = [{"reply": "Merhaba Deniz,\nYazdığınız için teşekkürler! Hangi kurumdan "
                                              "yazdığınızı paylaşır mısınız?\nSevgiler,\nHamle AI\nGamechangers"}]
        self.mails = [mail(1, sender="deniz.kaya@gmail.com")]
        self.run_once()
        req = dict(self.models.calls)["reply"]
        self.assertEqual((req["category"], req["known_brand"]), ("demo_or_first_contact", None))
        todo, _ = self.note()
        self.assertEqual(sorted(a["id"] for a in todo["assignees"]), [OWNER_ID, LEAD_ID])

    def test_client_brand_domain_gets_a_warm_reply_and_asks_the_brand_lead(self):
        self.models.out["research"] = [dict(researched(), company={"name": "Deneme Süt", "sector": None,
                                                                    "summary": None, "url": "https://denemesut.com"},
                                            evidence=["https://denemesut.com"])]
        self.models.out["reply"] = [{"reply": reply_text(first="Ece", company="Deneme Süt")}]
        self.mails = [mail(1, sender="ece@denemesut.com", name="Ece Yıldırım")]
        self.run_once()
        self.assertEqual(self.models.kinds(), ["classify", "reply"])  # no web search about a client brand
        req = dict(self.models.calls)["reply"]
        self.assertEqual((req["category"], req["known_brand"]), ("known_client_new_person", "Deneme Süt"))
        self.assertEqual(len(self.writer.writes), 1)
        todo, text = self.note()
        self.assertEqual(sorted(a["id"] for a in todo["assignees"]), [OWNER_ID, REP_ID])
        self.assertIn("Bu kişi kim, ekleyelim mi?", text)

    def test_english_mail_gets_an_english_reply(self):
        self.models.out["classify"] = [classified(language="en")]
        self.models.out["reply"] = [{"reply": reply_text(lang="en")}]
        self.mails = [mail(1, text="Hello, we would like a demo.")]
        self.run_once()
        self.assertTrue(self.writer.writes[0]["body"].endswith("Best,\nHamle AI\nGamechangers"))
        self.assertEqual(dict(self.models.calls)["reply"]["language"], "en")

    def test_agency_thread_or_cc_only_gets_a_note_and_no_reply(self):
        self.mails = [mail(1, thread_agency=True), mail(2, sender="ece@baska.com.tr", to=["x@baska.com.tr"],
                                                         cc=[HAMLE])]
        r = self.run_once()
        self.assertEqual((r["replied"], r["notes"]), (0, 2))
        self.assertEqual((self.models.calls, self.writer.writes), ([], []))

    def test_known_client_contact_is_left_to_the_client_watch(self):
        self.mails = [mail(1, sender="ali@musteri-marka.com.tr")]
        r = self.run_once()
        self.assertEqual((r["skipped"], self.models.calls, self.bc.todos), (1, [], {}))

    def test_person_research_requests_are_refused(self):
        self.models.out["classify"] = [classified("research_request", target="person")]
        self.mails = [mail(1, text="Merhaba, şu kişi hakkında araştırma yapar mısınız?")]
        self.run_once()
        self.assertEqual(self.bc.posted(RESEARCH), [])
        self.assertFalse(dict(self.models.calls)["reply"]["include_research_sentence"])
        self.assertIn("Hamle kişileri araştırmaz", self.note()[1])

    def test_model_budget_unavailable_leaves_the_mail_for_later(self):
        self.models.budget["classify"] = False
        self.mails = [mail(1)]
        r = self.run_once()
        self.assertEqual((r["replied"], r["skipped"], self.bc.todos), (0, 1, {}))
        self.models.budget["classify"] = True
        self.assertEqual(self.run_once()["replied"], 1)

    def test_per_run_limit(self):
        self.mails = [mail(i, sender=f"k{i}@firma{i}.com.tr") for i in range(1, 7)]
        r = self.run_once()
        self.assertEqual((r["replied"], r["skipped"]), (4, 2))

    def test_karne_checks_go_to_the_note_only(self):
        self.web = FakeWeb()
        calls = []

        def fake_karne(name, website=None, **kw):
            calls.append((name, website, kw["research"]))
            return {"status": "prospect_karne_ready", "counts": {"ok": 3}, "text": "Site hızlı açılıyor."}
        with patch("v11_digital_karne.run_prospect_karne", fake_karne):
            self.mails = [mail(1)]
            self.run_once()
        self.assertEqual(calls, [("Acme Yapı", "https://acmeyapi.com.tr", None)])  # no model inside the karne
        self.assertIn("Site hızlı açılıyor.", self.note()[1])
        self.assertNotIn("Site hızlı", self.writer.writes[0]["body"])


class Gates(Base):
    def test_outside_the_window_nothing_runs(self):
        self.mails = [mail(1)]
        for when in ("2026-10-17T07:59:00+03:00", "2026-10-17T22:06:00+03:00"):
            self.now = datetime.fromisoformat(when)
            self.assertEqual(self.run_once()["reason"], "outside_prospect_window")
        self.now = datetime.fromisoformat("2026-10-17T22:03:00+03:00")  # a late 22:00 start still runs
        self.assertEqual(self.run_once()["replied"], 1)

    def test_disabled_and_global_stop(self):
        self.mails = [mail(1)]
        s = pw.resolve(config(enabled=False), CATALOG, self.book)
        self.assertEqual(self.run_once(s)["reason"], "prospect_watch_disabled")
        self.ops.stop("offline-test")
        r = self.run_once()
        self.assertEqual(r["status"], "stopped")
        self.assertEqual((self.models.calls, self.bc.calls, self.writer.writes), ([], [], []))

    def test_stop_during_the_run_fences_the_send(self):
        self.mails = [mail(1)]
        original = self.models.reply

        def reply_then_stop(request):
            out = original(request)
            self.ops.stop("offline-test")
            return out
        self.models.reply = reply_then_stop
        self.run_once()
        self.assertEqual(self.writer.writes, [])
        self.assertEqual(self.bc.posted(NOTES), [])
        self.assertIsNone(self.get(pw.addr_key("deniz@acmeyapi.com.tr")))

    def test_missing_or_client_visible_list_holds_the_run_before_any_reply(self):
        self.mails = [mail(1)]
        self.bc.lists = self.bc.lists[1:]
        r = self.run_once()
        self.assertEqual((r["status"], r["reason"]), ("held", "prospect_list_missing:notes"))
        self.bc.lists = [dict(x, visible_to_clients=True) if x["id"] == NOTES else x for x in Basecamp().lists]
        self.assertEqual(self.run_once()["reason"], "prospect_list_unusable:notes")
        self.assertEqual((self.models.calls, self.writer.writes), ([], []))

    def test_note_failure_after_a_send_is_never_repeated(self):
        self.mails = [mail(1)]
        self.bc.fail_post = True
        self.run_once()
        self.assertEqual(len(self.writer.writes), 1)
        self.assertEqual(self.get(pw.mail_key("a1"))["state"], "note_unknown")
        self.bc.fail_post = False
        self.run_once()
        self.assertEqual((len(self.writer.writes), self.bc.todos), (1, {}))

    def test_concurrent_run_is_refused(self):
        w = self.watch()
        w.generation = self.ops.control()["generation"]
        self.assertTrue(w._lease("other-token"))
        self.mails = [mail(1)]
        self.assertEqual(self.run_once()["status"], "slot_already_claimed")


class ResearchRequests(Base):
    def setUp(self):
        super().setUp()
        self.models.out["classify"] = [classified("research_request", target="organisation")]
        self.models.out["reply"] = [{"reply": reply_text(extra="\n" + SENTENCE)}]
        self.mails = [mail(1, text="Merhaba, kurumumuz için dijital bir değerlendirme rica ediyoruz.")]

    def opened(self):
        self.run_once()
        todos = self.bc.posted(RESEARCH)
        self.assertEqual(len(todos), 1)
        return todos[0]

    def test_request_opens_an_owner_todo_due_today_and_researches_nothing(self):
        todo = self.opened()
        self.assertEqual(([a["id"] for a in todo["assignees"]], todo["due_on"]), ([OWNER_ID], "2026-10-17"))
        text = self.bc.todo_text(todo["id"])
        self.assertIn('"Onaylıyorum"', text)
        self.assertIn("yaklaşık 1-3 USD, ~20 dakika", text)
        self.assertIn(SENTENCE, self.writer.writes[0]["body"])
        self.assertTrue(dict(self.models.calls)["reply"]["include_research_sentence"])
        self.assertIn("Araştırma isteği (onay bekliyor):", self.note()[1])
        self.assertNotIn("report", self.models.kinds())
        self.run_once()  # no approval: still no report call
        self.assertNotIn("report", self.models.kinds())

    def test_only_the_owners_exact_approval_starts_the_research(self):
        self.approved()

    def approved(self):
        todo = self.opened()
        self.bc.human(todo["id"], LEAD_ID, "Onaylıyorum")
        self.bc.human(todo["id"], OWNER_ID, "Onaylıyorum ama yarın")
        self.run_once()
        self.assertNotIn("report", self.models.kinds())
        self.bc.human(todo["id"], OWNER_ID, "  ONAYLIYORUM ")
        r = self.run_once()
        self.assertEqual(r["requests"]["approved"], 1)
        self.assertEqual(r["requests"]["reports"], 1)
        req = dict(self.models.calls)["report"]
        self.assertEqual(req["domain"], "acmeyapi.com.tr")
        self.assertNotIn("Deniz Kaya", json.dumps(req, ensure_ascii=False))
        comment = max(c["id"] for c in self.bc.comments.values() if c["creator"]["id"] == HAMLE_ID)
        text = self.bc.comment_text(comment)
        for part in ("Araştırma raporu", "Sitede ürün kataloğu öne çıkıyor.", "Biz olsak ilk 3 hamlemiz:",
                     "1. Katalogu kısa videolara bölmek.", "Kaynağı doğrulanamayan 1 madde atıldı",
                     "Hamle gönder"):
            self.assertIn(part, text)
        self.assertNotIn("Uydurma gözlem", text)
        self.assertEqual(len(self.writer.writes), 1)  # the report has NOT gone to the prospect
        return todo

    def test_report_goes_only_after_the_owners_hamle_gonder(self):
        todo = self.approved()
        self.writer.anchor = {"message_id": "a1", "sender": "deniz@acmeyapi.com.tr", "subject": "Hamle AI demo talebi"}
        self.bc.human(todo["id"], LEAD_ID, "Hamle gönder")
        self.run_once()
        self.assertEqual(len(self.writer.writes), 1)
        self.bc.human(todo["id"], OWNER_ID, "Hamle gönder")
        r = self.run_once()
        self.assertEqual(r["requests"]["sent"], 1)
        sent = self.writer.writes[-1]
        self.assertEqual((sent["to"], sent["cc"], sent["thread_id"]), (["deniz@acmeyapi.com.tr"], [OWNER], "b1"))
        self.assertTrue(sent["body"].startswith("Merhaba Deniz,"))
        self.run_once()
        self.assertEqual(len(self.writer.writes), 2)

    def test_report_is_not_sent_when_the_prospect_is_no_longer_last(self):
        todo = self.approved()
        self.writer.anchor = {"message_id": "a7", "sender": "baska@acmeyapi.com.tr", "subject": "x"}
        self.bc.human(todo["id"], OWNER_ID, "Hamle gönder")
        self.run_once()
        self.assertEqual(len(self.writer.writes), 1)

    def test_no_report_without_budget(self):
        todo = self.opened()
        self.bc.human(todo["id"], OWNER_ID, "Onaylıyorum")
        self.models.budget["report"] = False
        self.run_once()
        self.assertNotIn("report", self.models.kinds())

    def test_one_open_request_per_domain(self):
        self.opened()
        self.models.out["reply"] = [{"reply": reply_text(first="Ece", extra="\n" + SENTENCE)}]
        self.mails = [mail(2, sender="ece@acmeyapi.com.tr", name="Ece Yıldırım")]
        self.run_once()
        self.assertEqual(len(self.bc.posted(RESEARCH)), 1)
        text = self.bc.todo_text(max(t["id"] for t in self.bc.posted(NOTES)))
        self.assertIn("Bu alan adı için açık bir araştırma isteği zaten var", text)

    def test_one_reminder_a_day_after_the_reminder_time(self):
        todo = self.opened()
        self.now = datetime.fromisoformat("2026-10-17T21:40:00+03:00")
        self.assertEqual(self.run_once()["requests"]["reminded"], 1)
        self.assertEqual(self.run_once()["requests"]["reminded"], 0)
        texts = [self.bc.comment_text(c["id"]) for c in self.bc.comments.values() if c["parent"]["id"] == todo["id"]]
        self.assertEqual(sum("Hatırlatma" in t for t in texts), 1)

    def test_request_expires(self):
        self.opened()
        self.now = SAT_1000 + timedelta(days=8)
        self.assertEqual(self.run_once()["requests"]["expired"], 1)

    def test_hamle_dur_from_an_agency_member_stops_the_request(self):
        todo = self.opened()
        self.bc.human(todo["id"], LEAD_ID, "Hamle dur")
        r = self.run_once()
        self.assertEqual(r["requests"]["stopped"], 1)
        self.bc.human(todo["id"], OWNER_ID, "Onaylıyorum")
        self.run_once()
        self.assertNotIn("report", self.models.kinds())


class Retention(Base):
    def seed(self, month, n, address):
        mkey, akey = pw.mail_key(f"old{n}"), pw.addr_key(address)

        def op(tx, now):
            tx.put(mkey, {"kind": "prospect_mail", "state": "noted", "sender": address, "subject": "eski"})
            tx.put(akey, {"kind": "prospect_address", "replied": True, "address": address, "todo_id": 5})
            tx.put(pw.index_key(month), {"kind": "prospect_index", "month": month, "keys": [mkey, akey],
                                         "purged": False})
        self.ops._run(op)
        return mkey, akey

    def test_old_months_become_data_free_tombstones(self):
        old_mail, old_addr = self.seed("202508", 1, "deniz@acmeyapi.com.tr")
        new_mail, _ = self.seed("202510", 2, "ece@baska.com.tr")  # Oct 2025 + 365 days is still ahead
        r = self.run_once()
        self.assertEqual(r["purge"], {"status": "purge_completed", "months": 1, "records": 2})
        self.assertEqual(self.get(old_mail), {"kind": "prospect_purged", "purged_at": SAT_1000.timestamp()})
        self.assertEqual(self.get(old_addr), {"kind": "prospect_address", "replied": True,
                                              "purged_at": SAT_1000.timestamp()})
        self.assertEqual(self.get(new_mail)["sender"], "ece@baska.com.tr")
        self.assertEqual(self.get(pw.index_key("202508"))["keys"], [])
        self.assertEqual(self.run_once()["purge"]["status"], "already_done")
        # the one-reply rule survives the purge: no model call, no reply, a note for the team
        self.mails = [mail(5)]
        self.now = SAT_1000 + timedelta(minutes=15)
        r = self.run_once()
        self.assertEqual((r["replied"], r["notes"], self.models.calls, self.writer.writes), (0, 1, [], []))
        self.assertIn("daha önce otomatik cevap verdi", self.note()[1])

    def test_recent_months_are_kept(self):
        self.mails = [mail(1)]
        self.run_once()
        self.now = SAT_1000 + timedelta(days=200)
        self.mails = []
        self.run_once()
        self.assertEqual(self.get(pw.mail_key("a1"))["state"], "noted")


# ------------------------------------------------------------------ real model plumbing (FakeClient)
class Models_(Base):
    def test_routes_and_isolation_through_the_model_gate(self):
        from test_anthropic_research import research_message
        cited = "https://acmeyapi.com.tr"
        client = FakeClient(
            message(classified(), model_id="claude-haiku-5-5", use=usage(800, 100)),
            research_message({k: v for k, v in researched().items() if k != "evidence"}, results=(cited, "https://acmeyapi.com.tr/ekip"), cited=(cited,)),
            message({"reply": reply_text()}))
        models = pw.model_sources(self.ops, lambda: KEY, CATALOG, self.settings, routing=model_routing.load(),
                                  client=client)
        self.assertEqual(models.models, {"classify": "claude-haiku-5-5", "research": "claude-opus-5-5",
                                         "reply": "claude-opus-5-5", "report": "claude-opus-5-5"})
        self.models = models
        self.mails = [mail(1)]
        r = self.run_once()
        self.assertEqual(r["replied"], 1)
        self.assertEqual(self.get(pw.mail_key("a1"))["company"]["name"], "Acme Yapı")
        classify, research, reply = client.calls
        self.assertEqual(classify["model"], "claude-haiku-5-5")
        self.assertNotIn("tools", classify)
        self.assertEqual(research["tools"][0]["max_uses"], 2)
        self.assertEqual(reply["model"], "claude-opus-5-5")
        self.assertNotIn("tools", reply)
        blob = json.dumps(reply, ensure_ascii=False)
        for secret in ("Kurgu Finans", "Deneme Süt", "Pazarlama Direktörü", OWNER, "brand_memory"):
            self.assertNotIn(secret, blob)
        self.assertIn("Selin de görüşmede olacak.", reply["system"] if isinstance(reply["system"], str)
                      else json.dumps(reply["system"], ensure_ascii=False))
        # a second run reuses the cached results (one durable chance per input)
        self.assertEqual(len(client.calls), 3)

    def test_prompts_are_tenant_independent(self):
        for path in pw.PROMPTS.values():
            text = path.read_text(encoding="utf-8")
            from v11_chain_live import load_catalog
            for row in load_catalog()["contacts"]:  # no real agency person is named in a prompt
                self.assertNotIn(row["name"].split()[0], text)
            self.assertNotIn("gamechangers", text.lower())
        rendered = pw.render_prompt("reply", self.settings)
        self.assertNotIn("{{", rendered)
        self.assertIn("Sevgiler,\nHamle AI\nGamechangers", rendered)


# ------------------------------------------------------------------ Gmail adapters (no network)
class Adapters(Base):
    def meta(self, *rows):
        return {"messages": [{"id": mid, "internalDate": str(t),
                              "payload": {"headers": [{"name": k, "value": v} for k, v in heads]}}
                             for mid, t, heads in rows]}

    def test_thread_facts(self):
        meta = self.meta(("m1", 1, [("From", "Deniz Kaya <deniz@acmeyapi.com.tr>")]),
                         ("m2", 2, [("From", "Emre <emre@gamechangers.com.tr>")]),
                         ("m3", 3, [("From", "Deniz Kaya <Deniz@AcmeYapi.com.tr>"), ("List-Id", "<x>"),
                                    ("Authentication-Results", "mx.google.com; dmarc=pass"), ("X-Other", "1")]))
        f = pw.thread_facts(meta, "gamechangers.com.tr")
        self.assertEqual((f["id"], f["from"], f["name"], f["agency_in_thread"]),
                         ("m3", "deniz@acmeyapi.com.tr", "Deniz Kaya", True))
        self.assertEqual(set(f["headers"]), {"list-id", "authentication-results"})

    def test_reader_query_and_headers(self):
        reader = pw.prospect_mail_reader(object(), self.settings, self.book)
        q = reader._query(reader._requesters())
        self.assertTrue(q.startswith(f"{{to:{HAMLE} cc:{HAMLE}}} "))
        for d in ("gamechangers.com.tr", "musteri-marka.com.tr", "basecamp.com"):
            self.assertIn("-from:" + d, q)
        self.assertTrue(q.endswith("newer_than:2d"))
        self.assertIn("Authentication-Results", reader.meta_headers)
        self.assertIn("From", reader.meta_headers)

    def test_chain_reader_query_is_unchanged(self):
        from v11_chain_mail import ChainMailReader
        r = ChainMailReader(object(), {"contacts": []}, newer_than_days=3)
        self.assertEqual(r._query(["a@gamechangers.com.tr", "b@gamechangers.com.tr"]),
                         f"{{to:{HAMLE} cc:{HAMLE}}} {{from:a@gamechangers.com.tr from:b@gamechangers.com.tr}} "
                         "newer_than:3d")
        self.assertEqual(r.meta_headers, ("Subject", "Message-ID", "References"))

    def test_reply_writer_shape_the_prospect_and_the_owner_in_cc(self):
        w = pw.prospect_reply_writer(self.settings, self.book, token_context=None, session_factory=None)
        good = {"from": HAMLE, "to": ["deniz@acmeyapi.com.tr"], "cc": [OWNER], "thread_id": "abc",
                "in_reply_to_message_id": "def", "subject": "Re: x", "body": "Merhaba"}
        self.assertEqual(w._shape(good), "client_reply")
        for bad in (dict(good, cc=[]), dict(good, cc=[OWNER, LEAD]), dict(good, cc=[LEAD]),
                    dict(good, to=["deniz@acmeyapi.com.tr", "ece@acmeyapi.com.tr"]),
                    dict(good, to=[OWNER]), dict(good, to=["ali@musteri-marka.com.tr"]), dict(good, thread_id="XYZ")):
            with self.assertRaises(Exception):
                w._shape(bad)
        with self.assertRaises(Exception):
            w._verify("client_reply", good, {"sender": "ece@acmeyapi.com.tr", "subject": "x"})
        w._verify("client_reply", good, {"sender": "deniz@acmeyapi.com.tr", "subject": "x"})



class Preview(Base):
    """Operator preview: real decisions, no mail, Basecamp or prospect records."""
    def preview(self, **kw):
        source = {"sender": kw.pop("sender", "deniz@acmeyapi.com.tr"), "sender_name": kw.pop("name", "Deniz Kaya"),
                  "subject": "Tanışalım", "text": kw.pop("text", "Merhaba, Hamle'yi gördüm, tanışalım mı?")}
        return pw.preview_mail(source, settings=self.settings, people=self.people, models=self.models,
                               clock=lambda: self.now)

    def test_first_contact_runs_classify_research_reply_and_writes_nothing(self):
        out = self.preview()
        self.assertEqual(out["status"], "prospect_watch_preview")
        self.assertEqual(self.models.kinds(), ["classify", "research", "reply"])
        self.assertEqual((out["decision"], out["reply_problems"], out["writes"]), ("would_send", [], 0))
        self.assertEqual(out["company"]["name"], "Acme Yapı")
        self.assertNotIn("research_todo", out)
        self.assertEqual((self.bc.posted(NOTES), self.writer.writes, self.copies.writes), ([], [], []))
        self.assertIsNone(self.get(pw.addr_key("deniz@acmeyapi.com.tr")))

    def test_research_request_shows_the_owner_todo_without_a_report(self):
        self.models.out["classify"] = [classified("research_request", target="organisation")]
        self.models.out["reply"] = [{"reply": reply_text(extra=" " + SENTENCE)}]
        out = self.preview(text="Kurumumuz hakkında ne düşünüyorsunuz, bir araştırma yapar mısınız?")
        self.assertEqual(out["decision"], "would_send", out["reply_problems"])
        self.assertIn("Onaylıyorum", out["research_todo"]["text"])
        self.assertIn("Acme Yapı", out["research_todo"]["title"])
        self.assertNotIn("report", self.models.kinds())
        self.assertEqual(self.bc.posted(RESEARCH), [])

    def test_free_mail_skips_research_and_spam_gets_no_reply(self):
        out = self.preview(sender="deniz.kaya@gmail.com")
        self.assertEqual(self.models.kinds(), ["classify", "reply"])
        self.assertEqual(out["domain_kind"], "individual")
        self.models.calls.clear()
        self.models.out["classify"] = [classified("spam")]
        out = self.preview()
        self.assertEqual((out["decision"], self.models.kinds()), ("no_reply", ["classify"]))

    def test_a_bad_reply_is_shown_as_held(self):
        self.models.out["reply"] = [{"reply": "Merhaba Deniz,\nFiyatımız 500 TL.\nSevgiler,\nHamle AI\nGamechangers"}]
        out = self.preview()
        self.assertEqual(out["decision"], "would_hold_for_a_human")
        self.assertTrue(out["reply_problems"])

class TestRequests(Base):
    """Operator test (owner 2026-10-16): a real test request + to-do; the report only ever reaches the owner."""
    SOURCE = {"sender": "deniz@acmeyapi.com.tr", "sender_name": "Deniz Kaya", "subject": "Bizi araştırır mısınız?",
              "text": "Merhaba, kurumumuz için kısa bir dijital araştırma rica ediyoruz.\nDeniz"}
    FACTS = {"first_name": "Deniz", "language": "tr", "request_summary": "Kurum için dijital araştırma istiyor.",
             "company": {"name": "Acme Yapı", "sector": "Yapı malzemeleri"}}

    def opened(self):
        out = self.watch().open_test_request(self.SOURCE, self.FACTS)
        self.assertEqual((out["status"], out["business_writes"]), ("prospect_test_request_opened", 1))
        todos = self.bc.posted(RESEARCH)
        self.assertEqual(len(todos), 1)
        todo = todos[0]
        self.assertTrue(todo["content"].startswith("TEST · Araştırma isteği · Acme Yapı"))
        self.assertEqual(([a["id"] for a in todo["assignees"]], todo["due_on"]), ([OWNER_ID], "2026-10-17"))
        self.assertIn("yalnız sana (" + OWNER + ")", self.bc.todo_text(todo["id"]))
        self.assertEqual((self.writer.writes, self.copies.writes, self.bc.posted(NOTES)), ([], [], []))
        self.assertIsNone(self.get(pw.domain_key("acmeyapi.com.tr")))  # a real request later is not blocked
        self.assertIsNone(self.get(pw.addr_key("deniz@acmeyapi.com.tr")))
        return todo

    def test_full_test_flow_report_goes_only_to_the_owner(self):
        todo = self.opened()
        r = self.watch().run_test_requests()
        self.assertEqual((r["status"], r["requests"]["checked"], r["requests"]["approved"]),
                         ("prospect_test_requests_completed", 1, 0))
        self.assertNotIn("report", self.models.kinds())
        self.bc.human(todo["id"], OWNER_ID, "Onaylıyorum")
        r = self.watch().run_test_requests()
        self.assertEqual((r["requests"]["approved"], r["requests"]["reports"]), (1, 1))
        self.bc.human(todo["id"], OWNER_ID, "Hamle gönder")
        r = self.watch().run_test_requests()
        self.assertEqual(r["requests"]["sent"], 1)
        self.assertEqual(self.writer.writes, [])  # never the prospect
        self.assertEqual(len(self.copies.writes), 1)
        sent = self.copies.writes[0]
        self.assertEqual(sent["to"], [OWNER])
        self.assertTrue(sent["subject"].startswith("TEST · Hamle AI araştırma raporu"))
        self.assertIn("Kuruma hiçbir şey gönderilmedi", sent["body"])
        self.assertIn("Merhaba Deniz,", sent["body"])
        last = max(c["id"] for c in self.bc.comments.values() if c["creator"]["id"] == HAMLE_ID)
        self.assertIn("yalnız sana", self.bc.comment_text(last))
        self.watch().run_test_requests()
        self.assertEqual(len(self.copies.writes), 1)

    def test_test_run_leaves_real_requests_alone(self):
        self.mails = [mail(1, subject="Araştırma", text="Merhaba, kurumumuzu araştırır mısınız?\nDeniz")]
        self.models.out["classify"] = [classified("research_request", target="organisation")]
        self.models.out["reply"] = [{"reply": reply_text(extra=" " + SENTENCE)}]
        self.run_once()
        real = self.bc.posted(RESEARCH)[0]
        self.bc.human(real["id"], OWNER_ID, "Onaylıyorum")
        r = self.watch().run_test_requests()
        self.assertEqual((r["requests"]["checked"], r["requests"]["approved"]), (0, 0))

    def test_stopped_control_opens_nothing(self):
        self.ops.stop("offline-test")
        with self.assertRaises(pw.ProspectError):
            self.watch().open_test_request(self.SOURCE, self.FACTS)
        self.assertEqual(self.bc.posted(RESEARCH), [])


class Wiring(unittest.TestCase):
    def test_package_worker_and_routing(self):
        import package_chain_source as pkg
        import worker
        for name in ("v11_prospect_watch.py", "prospect_classifier_prompt.txt", "prospect_research_prompt.txt",
                     "prospect_reply_prompt.txt", "prospect_report_prompt.txt"):
            self.assertIn(name, pkg.CHAIN_FILES)
        self.assertIn(pkg.APP + "chain-profile/prospect-watch-config.json", pkg.ALLOWLIST)
        self.assertIn("prospect_watch_completed", worker.OK_STATUSES)
        with open(worker.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"prospect_watch"', src)
        self.assertIn("run_worker_prospect_watch(store, secrets, started_at=job_started)", src)
        self.assertIn('if args.mode in ("chain_tick", "prospect_watch", "daily_digest"', src)  # budget roll first
        r = model_routing.load()
        self.assertEqual(r.route("prospect_classifier").model, "claude-haiku-5-5")
        self.assertIsNone(r.route("prospect_classifier").web_search)
        self.assertEqual(r.route("prospect_research").web_search["max_uses"], 2)
        self.assertEqual(r.route("prospect_report").web_search["max_uses"], 8)

    def test_scheduler_plan(self):
        from pathlib import Path
        plan = json.loads((Path(pw.__file__).resolve().parent / "scheduler-plan-prospect-watch.json")
                          .read_text(encoding="utf-8"))
        jobs = {j["name"]: j for j in plan["scheduler_jobs"]}
        self.assertEqual({j["schedule"] for j in jobs.values()}, {"*/15 8-21 * * *", "0 22 * * *"})
        self.assertTrue(all(j["time_zone"] == "Europe/Istanbul" for j in jobs.values()))
        job = plan["cloud_run_jobs"][0]
        self.assertEqual(job["name"], "hamle-prospect-watch")
        self.assertEqual(job["args"], ["worker.py", "--mode", "prospect_watch"])

    def test_worker_entry_off_by_default(self):
        r = pw.run_worker_prospect_watch(None, None)
        self.assertEqual((r["status"], r["reason"]), ("no_new_work", "prospect_watch_disabled"))


if __name__ == "__main__":
    unittest.main()
