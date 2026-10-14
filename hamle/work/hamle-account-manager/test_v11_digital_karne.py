"""Dijital Karne (Ahmet 2026-10-14 12:27Z, 16:08Z): WAT Mobilite, Rabam and ya da only; a detailed karne on the
first Monday of the month (19 Oct as the start), a silent site alarm on the other Mondays, the day-after digest
line. Fakes only (public web, Basecamp, Anthropic, Operations store)."""
from datetime import date, datetime, timedelta
import io
import json
import re
from contextlib import redirect_stdout
from urllib.parse import urlsplit

import model_routing
import v11_digital_karne as kw
import worker
from test_anthropic_research import research_message, routing
from test_anthropic_routing import FakeClient, KEY
from test_v11_daily_digest import ACC, APP, Base, todo
from v11_chain_live import load_catalog
from v11_nb_tracking import HAMLE_PERSON, text_matches

MON_1000 = datetime.fromisoformat("2026-10-19T10:00:00+03:00")
MON = date(2026, 10, 19)
ALARM_MON = date(2026, 10, 26)
TUE_0916 = datetime.fromisoformat("2026-10-20T09:16:00+03:00")
WAT, RABAM, YADA = 40694338, 48639990, 48917059
WAT_LIST, RABAM_LIST, YADA_LIST = 8231779952, 10239606739, 10310036095
OZGE, ESMA = 52096503, 53167312
CATALOG = load_catalog()
DESTS = kw.destinations(CATALOG)
PSI = ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed?strategy=mobile&category=performance&url="
       "https%3A%2F%2Fwatmobilite.com")

WAT_HOME = """<html><head><title>WAT Mobilite</title></head><body>
<a href="/">Ana sayfa</a><a href="/istasyonlar/">İstasyonlar</a><a href="/kampanyalar">Kampanyalar</a>
<a href="/blog">Blog</a><a href="/iletisim#form">İletişim</a>
<a href="https://www.instagram.com/watmobilite/">IG</a><a href="https://www.linkedin.com/company/watmobilite">in</a>
<a href="https://x.com/baskahesap">X</a><a href="https://twitter.com/intent/tweet?text=x">paylaş</a>
<script>var c = "© 2019";</script>
<p>Şarj kampanyası 30 Eylül 2026'ya kadar geçerli.</p>
<footer>© 2024 WAT Mobilite. Tüm hakları saklıdır.</footer></body></html>"""
JS_HOME = "<html><head><title>Rabam</title></head><body><div id='root'></div><a href='/app'>Uygulama</a></body></html>"
FULL_HOME = """<html><head><title>Marka</title></head><body><a href="/a">A</a><a href="/b">B</a><a href="/c">C</a>
<a href="/kvkk">KVKK Aydınlatma Metni</a><a href="/cerez">Çerez politikası</a><footer>© 2026</footer></body></html>"""
EVIDENCE = "https://www.sikayetvar.com/wat-mobilite/sarj-istasyonu-arizali"
NEWS = "https://www.example-haber.com/2026/10/16/wat-mobilite-yeni-istasyon"
EVENT = "https://www.example-fuar.com/2026/kasim/e-mobilite-zirvesi"


def lookup(rating=2.9, count=1500, released="2026-01-10T07:00:00Z"):
    return {"resultCount": 1, "results": [{"trackName": "Rabam", "averageUserRating": rating, "userRatingCount": count,
                                           "version": "3.2", "currentVersionReleaseDate": released,
                                           "trackViewUrl": "https://apps.apple.com/tr/app/rabam/id1571183769?uo=4"}]}


def review(day, stars, title="Başlık", author="Gerçek Kişi Adı"):
    return {"author": {"name": {"label": author}}, "updated": {"label": f"{day}T08:00:00-07:00"},
            "im:rating": {"label": str(stars)}, "title": {"label": title}, "content": {"label": f"{title} uzun metin"}}


def psi(score):
    return {"lighthouseResult": {"categories": {"performance": {"score": score}}}}


class FakeWeb(kw.Web):
    def __init__(self, pages=None, certs=None):
        self.pages, self.certs, self.calls = dict(pages or {}), dict(certs or {}), []

    def get(self, url, *, max_bytes=2_000_000, read_seconds=None):
        self.calls.append(url)
        raw = any(x in url for x in ("itunes", "rdap", "googleapis"))
        v = self.pages.get(url if raw else kw.normalize_url(url))
        if v is None:
            raise kw.KarneError("web_unreachable")
        status, body = v[0], v[1]
        body = json.dumps(body).encode() if isinstance(body, (dict, list)) else body.encode()
        return {"status": status, "url": v[3] if len(v) > 3 else url, "body": body if max_bytes else b"",
                "seconds": v[2] if len(v) > 2 else 0.8, "headers": v[4] if len(v) > 4 else {}}

    def cert_days(self, host, now):
        self.calls.append("cert:" + host)
        value = self.certs.get(host, 120)
        if isinstance(value, Exception):
            raise value
        return value


def wat_web():
    return FakeWeb({
        "https://watmobilite.com": (200, WAT_HOME, 0.8),
        "https://watmobilite.com/istasyonlar": (200, ""), "https://watmobilite.com/kampanyalar": (404, ""),
        "https://watmobilite.com/blog": (403, ""), "https://watmobilite.com/iletisim": (200, ""),
        "https://rdap.org/domain/watmobilite.com": (200, {"events": [{"eventAction": "expiration",
                                                                      "eventDate": "2027-03-01T00:00:00Z"}]}),
        PSI: (200, psi(0.42)),
        "https://rabam.com": (200, JS_HOME, 1.1),
        "https://itunes.apple.com/lookup?id=1571183769&country=tr": (200, lookup()),
        "https://itunes.apple.com/tr/rss/customerreviews/id=1571183769/sortBy=mostRecent/json": (200, {"feed": {"entry": [
            review("2026-10-18", 1, "Uygulama açılmıyor"), review("2026-10-17", 2, "Randevu hatası"),
            review("2026-10-15", 1, "Giriş sorunu"), review("2026-10-14", 5, "Harika"),
            review("2026-10-01", 1, "Ödeme hatası"), review("2026-09-25", 2, "Yavaş"),
            review("2026-09-01", 1, "Eski yorum")]}}),
        "https://hanspaces.com": (200, FULL_HOME, 7.5),
        "https://hanspaces.com/a": (200, ""), "https://hanspaces.com/b": (500, ""), "https://hanspaces.com/c": (200, ""),
        "https://hanspaces.com/kvkk": (200, ""), "https://hanspaces.com/cerez": (200, ""),
    }, certs={"watmobilite.com": 30, "rabam.com": 10, "hanspaces.com": kw.KarneError("ssl_invalid")})


def brand(key):
    return next(b for b in kw.validate_config(kw.load_config())["brands"] if b["key"] == key)


def other(website, verified=True):
    """A brand outside the karne config (site checks only)."""
    return {"key": "x", "brand": "X", "website": website, "website_verified": verified, "socials": {},
            "app_store_ids": [], "rivals": [], "notes": None}


def only(*keys):
    doc = kw.load_config()
    return dict(doc, brands=[b for b in doc["brands"] if b["key"] in keys])


def found(findings, level=None, area=None):
    return [f["text"] for f in findings if (level is None or f["level"] == level) and (area is None or f["area"] == area)]


class FakeBasecamp:
    """Write-side fake for the karne destination lists (+ comments of earlier karne to-dos)."""
    def __init__(self):
        self.calls, self.todos, self.comments, self.next_id, self.fail_todo = [], {}, {}, 7000, set()
        self.lists = {d["list_id"]: d["project_id"] for d in DESTS.values()}

    def send(self, method, url, body, expected):
        self.calls.append((method, url, body))
        path = urlsplit(url).path
        m = re.fullmatch(rf"/{ACC}/buckets/(\d+)/todolists/(\d+)(/todos)?\.json", path)
        if m and self.lists.get(int(m[2])) == int(m[1]):
            pid, lid = int(m[1]), int(m[2])
            if not m[3]:
                return {"id": lid, "type": "Todolist", "status": "active", "bucket": {"id": pid},
                        "visible_to_clients": False}, {}
            assert method == "POST" and expected == 201
            if pid in self.fail_todo:
                raise ConnectionError("reset")
            self.next_id += 1
            tid = self.next_id
            self.todos[tid] = {"id": tid, "content": body["content"], "description": body["description"],
                               "parent": {"id": lid, "type": "Todolist"}, "bucket": {"id": pid},
                               "creator": {"id": HAMLE_PERSON}, "visible_to_clients": False,
                               "assignees": [{"id": i} for i in body["assignee_ids"]],
                               "due_on": body["due_on"], "app_url": f"{APP}/buckets/{pid}/todos/{tid}"}
            return {"id": tid}, {}
        m = re.fullmatch(rf"/{ACC}/buckets/(\d+)/todos/(\d+)\.json", path)
        if m:
            return dict(self.todos[int(m[2])]), {}
        m = re.fullmatch(rf"/{ACC}/buckets/(\d+)/recordings/(\d+)/comments\.json", path)
        if m:
            return list(self.comments.get(int(m[2]), [])), {}
        raise AssertionError(url)

    def posts(self):
        return [(urlsplit(u).path, b) for m, u, b in self.calls if m == "POST"]


class Research:
    def __init__(self, output=None, fail=None):
        self.inputs, self.output, self.fail, self.timeout_seconds = [], output, fail, 600

    def __call__(self, request_input):
        self.inputs.append(request_input)
        if self.fail:
            raise self.fail
        return self.output or {"summary": None, "findings": [], "not_checked": [], "evidence": []}


class Karne(Base):
    START = MON_1000

    def setUp(self):
        super().setUp()
        self.web, self.http_fake, self.tick = wat_web(), FakeBasecamp(), [0.0]
        self.http = kw.KarneBasecamp(ACC, DESTS, send=self.http_fake.send)

    def run_karne(self, research=None, **k):
        k.setdefault("config", kw.load_config())
        return kw.run_digital_karne(self.ops, catalog=CATALOG, cards=self.cards, web=self.web,
                                   research=research if research is not None else Research(), http=self.http,
                                   clock=lambda: self.now, monotonic=lambda: self.tick[0], **k)

    def state(self, day=MON):
        return self.ops._run(lambda tx, _: tx.get(kw.state_key(day)), allow_expired=True)


class Setup(Karne):
    def test_three_brands_monthly_with_a_team_only_destination(self):
        cfg = kw.validate_config(kw.load_config())
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["window"], {"weekday": 0, "start": "09:50", "end": "13:00"})
        self.assertEqual((cfg["period_days"], cfg["full_first_monday"], cfg["alarm_other_mondays"],
                          cfg["extra_full_days"]), (30, True, True, ["2026-10-19"]))
        keys = [b["key"] for b in cfg["brands"]]
        self.assertEqual(keys, ["wat_mobilite", "rabam", "ya_da"])  # Ahmet 16:08Z: only these three
        self.assertEqual(DESTS["wat_mobilite"], {"project_id": WAT, "list_id": WAT_LIST, "person_id": OZGE,
                                                 "internal": False, "card_project_id": WAT})
        self.assertEqual((DESTS["rabam"]["list_id"], DESTS["rabam"]["person_id"]), (RABAM_LIST, OZGE))
        self.assertEqual((DESTS["ya_da"]["project_id"], DESTS["ya_da"]["list_id"], DESTS["ya_da"]["person_id"]),
                         (YADA, YADA_LIST, ESMA))
        self.assertEqual(brand("rabam")["app_store_ids"], ["1571183769"])

    def test_config_rejects_bad_values(self):
        doc = kw.load_config()
        for change in ({"window": {"weekday": 6, "start": "10:00", "end": "12:00"}},
                       {"time_budget_seconds": 5000}, {"enabled": "yes"}, {"period_days": 90},
                       {"full_first_monday": "evet"}, {"extra_full_days": ["2026-10-20"]},  # a Tuesday
                       {"extra_full_days": ["19 Ekim"]},
                       {"brands": [dict(doc["brands"][0], website="http://watmobilite.com")]},
                       {"brands": [dict(doc["brands"][0], socials={"myspace": "https://myspace.com/x"})]},
                       {"brands": [dict(doc["brands"][0], app_store_ids=["abc"])]},
                       {"brands": [doc["brands"][0], doc["brands"][0]]}, {"brands": []}):
            with self.assertRaises(kw.KarneError, msg=change):
                kw.validate_config(dict(doc, **change))

    def test_first_monday_is_the_full_karne_other_mondays_the_alarm(self):
        cfg = kw.validate_config(kw.load_config())
        days = {"2026-10-19": "full", "2026-10-26": "alarm", "2026-11-02": "full", "2026-11-09": "alarm",
                "2026-11-30": "alarm", "2026-12-07": "full", "2026-10-20": None, "2026-10-12": "alarm"}
        for day, kind in days.items():
            self.assertEqual(kw.kind_of(date.fromisoformat(day), cfg), kind, day)
        quiet = kw.validate_config(dict(kw.load_config(), alarm_other_mondays=False, extra_full_days=[]))
        self.assertEqual([kw.kind_of(date.fromisoformat(d), quiet) for d in ("2026-10-19", "2026-10-26", "2026-11-02")],
                         [None, None, "full"])

    def test_research_role_is_opus_with_more_searches_and_packaged(self):
        import package_chain_source as pkg
        r = model_routing.load().route("digital_karne")
        self.assertEqual((r.provider, r.model, r.effort, r.max_tokens, r.timeout_seconds),
                         ("anthropic", "claude-opus-5-5", "medium", 12000, 600))
        self.assertEqual(r.web_search["max_uses"], 8)
        self.assertEqual(model_routing.load().route("daily_spark").web_search["max_uses"], 4)  # others unchanged
        for name in ("v11_digital_karne.py", "digital_karne_prompt.txt"):
            self.assertIn(name, pkg.CHAIN_FILES)
        self.assertIn(pkg.APP + "chain-profile/digital-karne-config.json", pkg.ALLOWLIST)
        doc = json.loads((model_routing.Path(model_routing.__file__).resolve().parents[2]
                          / "outputs/gamechangers-internal/model-routing.json").read_text(encoding="utf-8"))
        for role, change in (("digital_karne", {"provider": "openai", "profile": "astra-max-v1"}),
                             ("digital_karne", dict(doc["roles"]["digital_karne"], web_search_max_uses=11)),
                             ("digital_karne", dict(doc["roles"]["digital_karne"], web_search_max_uses="8")),
                             ("digest_classifier", dict(doc["roles"]["digest_classifier"], web_search_max_uses=2))):
            bad = json.loads(json.dumps(doc))
            bad["roles"][role] = change
            with self.assertRaises(model_routing.RoutingError, msg=(role, change)):
                model_routing.validate(bad)

    def test_month_title_alarm_title_and_state_key(self):
        self.assertEqual(kw.state_key(MON), "digital-karne-2026-10-19")
        self.assertEqual(kw.state_key(date(2026, 10, 25)), "digital-karne-2026-10-19")
        self.assertEqual(kw.title("WAT Mobilite", MON), "Hamle Karne: WAT Mobilite · Ekim 2026")
        self.assertEqual(kw.title("WAT Mobilite", ALARM_MON, "alarm"), "Hamle Alarm: WAT Mobilite · 26 Ekim")
        self.assertTrue(kw.is_karne_item({"kind": "todo", "title": "Hamle Karne: Rabam · Ekim 2026"}))
        self.assertFalse(kw.is_karne_item({"kind": "card", "title": "Hamle Karne: Rabam · Ekim 2026"}))
        self.assertFalse(kw.is_karne_item({"kind": "todo", "title": "Hamle Alarm: Rabam · 26 Ekim"}))  # team work


class Checks(Karne):
    def test_site_checks_find_broken_links_old_year_privacy_and_social_links(self):
        findings, facts = kw.site_checks(brand("wat_mobilite"), self.web, MON, MON_1000)
        self.assertEqual(found(findings, "kirmizi"), ["Ana sayfadaki 1 bağlantı açılmıyor (404): /kampanyalar."])
        self.assertEqual(found(findings, "sari"), [
            "SSL sertifikasının süresi 30 gün sonra doluyor.",
            "Sitenin alt bilgisinde “© 2024” yazıyor.",  # the script's © is not page text
            "Ana sayfada KVKK, gizlilik ya da çerez politikası bağlantısı görünmüyor.",
            "Sitedeki sosyal medya bağlantısı başka bir hesaba gidiyor: X (baskahesap).",
            "Sitede şu hesapların bağlantısı yok: TikTok, YouTube."])
        self.assertEqual(found(findings, "yesil"), ["Ana sayfa açılıyor (0,8 sn).",
                                                    "Alan adı (watmobilite.com) 1 Mart 2027 tarihine kadar kayıtlı."])
        self.assertIn("30 Eylül 2026", facts["text"])
        self.assertEqual(sorted(facts["checked"][2:]), ["https://watmobilite.com/blog", "https://watmobilite.com/iletisim",
                                                        "https://watmobilite.com/istasyonlar",
                                                        "https://watmobilite.com/kampanyalar"])
        self.assertNotIn("https://x.com/baskahesap", facts["checked"])  # other hosts are never opened
        self.assertTrue(all(f["url"] or f["level"] != "kirmizi" for f in findings))

    def test_slow_site_invalid_ssl_server_errors_and_tr_domains(self):
        findings, _ = kw.site_checks(other("https://hanspaces.com"), self.web, MON, MON_1000)
        self.assertEqual(found(findings, "kirmizi"),
                         ["SSL sertifikası geçersiz; tarayıcı güvenlik uyarısı gösterebilir."])
        self.assertEqual(found(findings, "sari"), ["Ana sayfa yavaş açılıyor (7,5 sn).",
                                                   "1 bağlantı sunucu hatası verdi (HTTP 5xx): /b."])
        self.assertIn("Alan adının (hanspaces.com) bitiş tarihi okunamadı.", found(findings, "bakilamadi"))
        self.assertNotIn("Ana sayfada KVKK", " ".join(found(findings)))
        self.web.calls.clear()
        self.assertEqual(kw.domain_check(self.web, "www.altinmarka.com.tr", MON), [])  # no public RDAP for .tr
        self.assertEqual(self.web.calls, [])

    def test_js_pages_unreachable_and_blocked_sites_are_not_guessed(self):
        findings, _ = kw.site_checks(brand("rabam"), self.web, MON, MON_1000)
        self.assertEqual(found(findings, "kirmizi"), ["SSL sertifikasının süresi 10 gün içinde doluyor."])
        self.assertIn("tarayıcıda oluşuyor", found(findings, "bakilamadi")[-1])
        findings, _ = kw.site_checks(brand("ya_da"), self.web, MON, MON_1000)
        self.assertEqual(found(findings), ["Site Hamle'nin kontrolünde açılmadı (web_unreachable)."])
        findings, _ = kw.site_checks(other("https://yeni-marka.example.com", verified=False), self.web, MON, MON_1000)
        self.assertEqual([f["level"] for f in findings], ["bakilamadi"])
        self.assertIn("doğrulanmadı", findings[0]["text"])
        self.web.pages["https://bikaldi.com.tr"] = (403, "", 0.5, "https://bikaldi.com.tr", {"server": "cloudflare"})
        findings, _ = kw.site_checks(other("https://bikaldi.com.tr"), self.web, MON, MON_1000)
        self.assertEqual(found(findings, "bakilamadi")[0],
                         "Site otomatik kontrole izin vermedi (HTTP 403); ana sayfaya tarayıcıdan bakın.")
        self.assertEqual(found(findings, "kirmizi"), [])

    def test_mobile_pagespeed_score(self):
        self.assertEqual(found(kw.pagespeed_check(self.web, "https://watmobilite.com")),
                         ["Mobil hız skoru düşük: 42/100 (PageSpeed)."])
        [f] = kw.pagespeed_check(self.web, "https://watmobilite.com")
        self.assertEqual((f["level"], f["url"]),
                         ("sari", "https://pagespeed.web.dev/analysis?url=https%3A%2F%2Fwatmobilite.com"))
        self.web.pages[PSI] = (200, psi(0.9))
        self.assertEqual(found(kw.pagespeed_check(self.web, "https://watmobilite.com"), "yesil"),
                         ["Mobil hız skoru 90/100 (PageSpeed)."])
        for broken in ((429, {"error": "quota"}), (200, {"lighthouseResult": {}}), (200, psi(7))):
            self.web.pages[PSI] = broken
            self.assertEqual(found(kw.pagespeed_check(self.web, "https://watmobilite.com"), "bakilamadi"),
                             ["Mobil hız skoru (PageSpeed) bu ay okunamadı."], broken)
        self.assertEqual(kw.pagespeed_check(self.web, None), [])

    def test_app_store_rating_update_and_the_months_reviews_without_names(self):
        findings, facts = kw.app_checks(brand("rabam"), self.web, MON, days=30)
        self.assertEqual(found(findings, "sari"), [
            "Rabam App Store puanı 2,9 (1.500 değerlendirme).",
            "Rabam 9 aydır güncellenmemiş (son sürüm 10 Ocak 2026).",
            "Son 30 günde App Store'da 5 tane 1-2 yıldızlı yorum geldi."])
        self.assertEqual([r["title"] for r in facts["reviews"]],
                         ["Uygulama açılmıyor", "Randevu hatası", "Giriş sorunu", "Ödeme hatası", "Yavaş"])
        self.assertNotIn("Gerçek Kişi", json.dumps(facts, ensure_ascii=False))
        self.assertEqual(facts["checked"], ["https://apps.apple.com/tr/app/rabam/id1571183769?uo=4"])
        findings, _ = kw.app_checks(brand("rabam"), self.web, MON, days=7)  # fewer than 5 low reviews: a note
        self.assertEqual(found(findings, "yesil"), ["Rabam: son 7 günde 4 yeni yorum (3 tanesi 1-2 yıldız)."])

    def test_social_handles_and_share_links(self):
        self.assertEqual(kw.social_of("https://www.linkedin.com/company/WatMobilite/"), ("linkedin", "watmobilite"))
        self.assertEqual(kw.social_of("https://www.youtube.com/@Rabam"), ("youtube", "rabam"))
        self.assertEqual(kw.social_of("https://twitter.com/intent/tweet?x=1"), ("x", ""))
        self.assertEqual(kw.social_of("https://www.instagram.com/p/Cxyz/"), ("instagram", ""))
        self.assertIsNone(kw.social_of("https://watmobilite.com/instagram"))

    def test_public_web_refuses_private_hosts_http_and_ip_literals(self):
        rows = {"watmobilite.com": [(2, 1, 6, "", ("93.184.216.34", 443))],
                "intranet.example.com": [(2, 1, 6, "", ("10.0.0.5", 443))]}
        sessions = []

        class Session:
            def __init__(self, status=301, location="https://intranet.example.com/x"):
                self.status, self.location = status, location

            def request(self, method, url, **kwargs):
                assert method == "GET" and kwargs["allow_redirects"] is False
                sessions.append((url, kwargs["timeout"]))
                r = type("R", (), {})()
                r.status_code, r.headers = self.status, {"Location": self.location}
                r.iter_content = lambda chunk_size: iter([b"ok"])
                r.close = lambda: None
                return r

            def close(self):
                pass
        web = kw.Web(Session, resolve=lambda host: rows[host])
        for url, code in (("http://watmobilite.com", "web_url_not_https"), ("https://10.0.0.5/", "web_ip_literal"),
                          ("https://watmobilite.com", "web_host_not_public"),  # redirect to a private address
                          ("https://metadata.google.internal/x", "web_host_not_public")):
            with self.assertRaises(kw.KarneError) as ctx:
                web.get(url, read_seconds=90)
            self.assertEqual(ctx.exception.code, code)
        self.assertEqual(sessions, [("https://watmobilite.com", (6, 90))])


class Model(Karne):
    def test_only_sourced_findings_of_the_month_survive(self):
        allowed = ["https://watmobilite.com"]
        output = {"evidence": [EVIDENCE, NEWS, EVENT], "not_checked": ["Instagram profili giriş istediği için açılamadı."],
                  "findings": [
                      {"area": "sikayet", "level": "kirmizi", "text": "Arızalı istasyon şikayetleri arttı.",
                       "suggestion": "Müşteriye yanıt önerin.", "url": EVIDENCE, "date": "2026-10-17"},
                      {"area": "gundem", "level": "yesil", "text": "Yeni istasyon haberi.", "suggestion": None,
                       "url": NEWS + "?utm_source=x", "date": "2026-09-25"},  # inside the 30 days
                      {"area": "icerik", "level": "sari", "text": "Eylül kampanyası hâlâ ana sayfada.",
                       "suggestion": "Kaldırın.", "url": "https://watmobilite.com/", "date": None},
                      {"area": "firsat", "level": "sari", "text": "Kasım'daki e-mobilite zirvesi.",
                       "suggestion": "Zirve için içerik planlayın.", "url": EVENT, "date": None},
                      {"area": "gundem", "level": "sari", "text": "Tarihsiz haber.", "suggestion": None,
                       "url": NEWS, "date": None},
                      {"area": "rakip", "level": "sari", "text": "Eski rakip haberi.", "suggestion": None,
                       "url": NEWS, "date": "2026-09-10"},  # before the period
                      {"area": "sosyal", "level": "sari", "text": "Uydurma kaynak.", "suggestion": None,
                       "url": "https://never-searched.example/x", "date": None},
                      {"area": "gundem", "level": "sari", "text": "Gelecek tarihli.", "suggestion": None,
                       "url": NEWS, "date": "2026-10-25"}]}
        kept, not_checked, dropped = kw.sourced(output, allowed, MON, 30)
        self.assertEqual([f["text"] for f in kept], ["Arızalı istasyon şikayetleri arttı.", "Yeni istasyon haberi.",
                                                    "Eylül kampanyası hâlâ ana sayfada.",
                                                    "Kasım'daki e-mobilite zirvesi."])
        self.assertEqual(kept[1]["url"], NEWS)
        self.assertEqual(kept[3]["level"], "firsat")  # opportunities get their own group
        self.assertEqual(not_checked, ["Instagram profili giriş istediği için açılamadı."])
        self.assertEqual(dropped, 4)
        kept, _, dropped = kw.sourced(output, allowed, MON, 7)  # a weekly period drops the September news
        self.assertNotIn("Yeni istasyon haberi.", [f["text"] for f in kept])
        self.assertEqual(dropped, 5)

    def test_the_summary_is_shown_only_when_nothing_was_dropped(self):
        good = {"area": "sikayet", "level": "kirmizi", "text": "Arızalı istasyon şikayetleri arttı.",
                "suggestion": None, "url": EVIDENCE, "date": "2026-10-17"}
        bad = dict(good, text="Uydurma.", url="https://never-searched.example/x")
        out = {"summary": "Şikayetler bu ayın ana konusu.", "evidence": [EVIDENCE], "not_checked": [],
               "findings": [good]}
        findings, status, summary = kw.with_research([], {}, [], Research(out), MON, 30)
        self.assertEqual((found(findings), status, summary),
                         (["Arızalı istasyon şikayetleri arttı."], "1_sourced", "Şikayetler bu ayın ana konusu."))
        findings, status, summary = kw.with_research([], {}, [], Research(dict(out, findings=[good, bad])), MON, 30)
        self.assertEqual((status, summary), ("1_sourced_1_dropped", None))
        _, status, summary = kw.with_research([], {}, [], Research(dict(out, findings=[])), MON, 30)
        self.assertEqual((status, summary), ("0_sourced", None))
        findings, status, _ = kw.with_research([], {}, [], Research(fail=kw.KarneError("model_timeout")), MON, 30)
        self.assertEqual(status, "model_timeout")
        self.assertIn("bu ay alınamadı (model_timeout)", found(findings, "bakilamadi")[0])

    def test_research_request_is_bounded_and_one_paid_call_per_input(self):
        b = brand("wat_mobilite")
        checks, facts = kw.site_checks(b, self.web, MON, MON_1000)
        facts["text"] = "ş" * 20000
        inp = kw.model_input(b, {"sector": "şarj"}, MON, checks, facts, ["Hamle yanlış: KVKK linki footer'da var."], 30)
        self.assertLessEqual(len(json.dumps(inp, ensure_ascii=False, separators=(",", ":")).encode()), 15000)
        self.assertEqual(inp["team_feedback"], ["Hamle yanlış: KVKK linki footer'da var."])
        self.assertEqual((inp["today"], inp["period_from"], inp["rivals"][0]), ("2026-10-19", "2026-09-19", "Trugo"))
        self.assertTrue(inp["site_checks"][0].startswith("🟢 Site: Ana sayfa açılıyor"))
        value = {"summary": None,
                 "findings": [{"area": "sikayet", "level": "sari", "text": "Şikayet var.", "suggestion": None,
                               "url": "https://news.example.com/2026/10/09/kampanya", "date": "2026-10-17"}],
                 "not_checked": []}
        client = FakeClient(research_message(value))
        research = kw.research_source(self.ops, {"anthropic": lambda: KEY}, CATALOG, routing=routing(), client=client)
        out = research(inp)
        self.assertEqual(out["findings"][0]["text"], "Şikayet var.")
        self.assertEqual(research(inp), out)  # same input: cached, never a second paid call
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual((call["model"], call["tools"][0]["max_uses"], call["max_tokens"], call["output_config"]),
                         ("claude-opus-5-5", 8, 12000, {"effort": "medium"}))
        self.assertIn("aylık \"Dijital Karne\"", call["system"])
        self.assertEqual(research.timeout_seconds, 600)


class Description(Karne):
    def test_monthly_todo_groups_by_level_with_links_summary_and_visible_text_matches(self):
        findings = [kw._finding("site", "kirmizi", "Ana sayfadaki 1 bağlantı açılmıyor (404): /kampanyalar.",
                                "https://watmobilite.com/kampanyalar", "Bağlantıyı düzeltin."),
                    kw._finding("sosyal", "sari", "Sitede şu hesapların bağlantısı yok: TikTok.", "https://watmobilite.com"),
                    kw._finding("firsat", "firsat", "Kasım'daki e-mobilite zirvesi.", EVENT, "İçerik planlayın."),
                    kw._finding("site", "yesil", "Ana sayfa açılıyor (0,8 sn).", "https://watmobilite.com"),
                    kw._finding(None, "bakilamadi", "Instagram profili açılamadı.")]
        html, text = kw.description("WAT Mobilite", MON, findings, "full", "Şikayetler bu ayın ana konusu.")
        self.assertTrue(text_matches(html, text))
        flat = " ".join(text.split())
        self.assertIn("Aylık Dijital Karne · WAT Mobilite · Ekim 2026 (19 Ekim 2026)", flat)
        self.assertIn("Özet: 🔴 1 acil · 🟡 1 dikkat · 💡 1 fırsat · 🟢 1 sorunsuz · ⚪ 1 bakılamadı", flat)
        self.assertIn("Hamle'nin özeti: Şikayetler bu ayın ana konusu.", flat)
        self.assertIn("🔴 Hemen ele alınmalı • Site: Ana sayfadaki 1 bağlantı açılmıyor (404): /kampanyalar. "
                      "Öneri: Bağlantıyı düzeltin. kaynak", flat)
        self.assertIn("💡 Fırsatlar • Fırsat: Kasım'daki e-mobilite zirvesi. Öneri: İçerik planlayın. kaynak", flat)
        self.assertLess(flat.index("🟡 Dikkat"), flat.index("💡 Fırsatlar"))
        self.assertLess(flat.index("💡 Fırsatlar"), flat.index("🟢 Sorunsuz"))
        self.assertIn('<a href="https://watmobilite.com/kampanyalar">kaynak</a>', html)
        self.assertIn("⚪ Bakılamadı • Instagram profili açılamadı.", flat)
        self.assertIn("müşteriye gitmez", flat)
        self.assertNotIn("http", text)  # links are titles, never raw URLs
        _, text = kw.description("X", MON, [kw._finding("site", "yesil", "Ana sayfa açılıyor (0,8 sn).")])
        flat = " ".join(text.split())
        self.assertIn("Özet: 🔴 0 acil · 🟡 0 dikkat · 🟢 1 sorunsuz", flat)
        self.assertNotIn("fırsat", flat.split("Bu ay")[0])
        self.assertNotIn("Hamle'nin özeti", flat)
        self.assertIn("Bu ay acil ya da dikkat isteyen bir konu bulmadım.", text)

    def test_alarm_text_lists_only_the_reds(self):
        reds = [kw._finding("site", "kirmizi", "SSL sertifikasının süresi 10 gün içinde doluyor.", "https://rabam.com",
                            "Site ekibine sertifikayı yenilemesini hemen iletin.")]
        html, text = kw.description("Rabam", ALARM_MON, reds, "alarm")
        self.assertTrue(text_matches(html, text))
        flat = " ".join(text.split())
        self.assertTrue(flat.startswith("Haftalık site alarmı · Rabam · 26 Ekim 2026 🔴 1 acil sorun buldum."), flat)
        self.assertIn("🔴 Hemen ele alınmalı • Site: SSL sertifikasının süresi 10 gün içinde doluyor.", flat)
        self.assertIn("Ayrıntılı karne her ayın ilk pazartesi gelir.", flat)
        self.assertNotIn("Özet:", flat)


class Run(Karne):
    def research_output(self):
        return {"summary": "Şikayetler bu ayın ana konusu.", "evidence": [EVIDENCE], "not_checked": [],
                "findings": [{"area": "sikayet", "level": "kirmizi", "text": "Arızalı istasyon şikayetleri arttı.",
                              "suggestion": None, "url": EVIDENCE, "date": "2026-10-17"}]}

    def test_start_monday_opens_one_monthly_todo_per_brand_for_its_lead(self):
        research = Research(self.research_output())
        r = self.run_karne(research)
        self.assertEqual(r["status"], "digital_karne_completed", r)
        self.assertEqual((r["kind"], r["monday"], r["created"], r["unknown"], r["business_writes"], r["model_calls"]),
                         ("full", "2026-10-19", 3, 0, 3, 3))
        self.assertEqual([i["period_from"] for i in research.inputs], ["2026-09-19"] * 3)
        posts = {b["content"]: (p, b) for p, b in self.http_fake.posts()}
        self.assertEqual(sorted(posts), ["Hamle Karne: Rabam · Ekim 2026", "Hamle Karne: WAT Mobilite · Ekim 2026",
                                         "Hamle Karne: ya da Multicosmetics · Ekim 2026"])
        path, body = posts["Hamle Karne: WAT Mobilite · Ekim 2026"]
        self.assertEqual(path, f"/{ACC}/buckets/{WAT}/todolists/{WAT_LIST}/todos.json")
        self.assertEqual((body["assignee_ids"], body["due_on"], body["notify"], body["completion_subscriber_ids"]),
                         ([OZGE], "2026-10-23", True, []))  # due Friday
        self.assertIn("Arızalı istasyon şikayetleri arttı.", body["description"])
        self.assertIn("Mobil hız skoru düşük: 42/100", body["description"])
        self.assertIn("Hamle&#x27;nin özeti: Şikayetler bu ayın ana konusu.", body["description"])
        path, body = posts["Hamle Karne: ya da Multicosmetics · Ekim 2026"]
        self.assertEqual((path, body["assignee_ids"]), (f"/{ACC}/buckets/{YADA}/todolists/{YADA_LIST}/todos.json", [ESMA]))
        self.assertEqual(posts["Hamle Karne: Rabam · Ekim 2026"][0], f"/{ACC}/buckets/{RABAM}/todolists/{RABAM_LIST}/todos.json")
        self.assertIn("Son 30 günde App Store", posts["Hamle Karne: Rabam · Ekim 2026"][1]["description"])
        st = self.state()
        self.assertEqual((st["run_kind"], st["monday"]), ("full", "2026-10-19"))
        wat = st["brands"]["wat_mobilite"]
        self.assertEqual((wat["state"], wat["who"], wat["counts"]["kirmizi"]), ("ready", "Özge", 2))
        self.assertEqual(wat["site_reds"], ["Ana sayfadaki 1 bağlantı açılmıyor (404): /kampanyalar."])
        self.assertNotIn("input", wat)
        self.assertIsNone(wat["checks"])
        self.assertLess(st["lease"]["until"], MON_1000.timestamp() + 1)  # released
        # a second run the same Monday opens nothing again
        r = self.run_karne(research)
        self.assertEqual((r["status"], r["created"], r["already"]), ("digital_karne_completed", 0, 3))
        self.assertEqual(len(self.http_fake.posts()), 3)
        self.assertEqual(len(research.inputs), 3)

    def test_research_failure_still_opens_the_todo_and_says_so(self):
        r = self.run_karne(Research(fail=kw.KarneError("model_budget_unavailable")), config=only("wat_mobilite"))
        self.assertEqual((r["created"], r["research"], r["model_calls"]),
                         (1, {"wat_mobilite": "model_budget_unavailable"}, 0))
        [(_, body)] = self.http_fake.posts()
        self.assertIn("araştırması bu ay alınamadı (model_budget_unavailable)", body["description"])

    def test_outside_window_off_days_disabled_and_stopped_write_nothing(self):
        self.now = datetime.fromisoformat("2026-10-20T10:00:00+03:00")  # Tuesday
        self.assertEqual(self.run_karne()["reason"], "outside_karne_window")
        self.now = datetime.fromisoformat("2026-10-19T13:30:00+03:00")
        self.assertEqual(self.run_karne()["reason"], "outside_karne_window")
        self.now = datetime.fromisoformat("2026-10-26T10:00:00+03:00")
        self.assertEqual(self.run_karne(config=dict(kw.load_config(), alarm_other_mondays=False))["reason"],
                         "no_karne_today")
        self.now = MON_1000
        self.assertEqual(self.run_karne(config=dict(kw.load_config(), enabled=False))["reason"], "karne_disabled")
        self.ops.set_mode("stopped", "ahmet-dur")
        r = self.run_karne()
        self.assertEqual(r["status"], "held")
        self.assertEqual((self.http_fake.calls, self.web.calls), ([], []))

    def test_time_budget_stops_and_the_next_run_continues_without_a_second_check(self):
        research = Research(self.research_output())
        real = kw.with_research

        def slow(*a, **k):
            self.tick[0] += 400  # each brand's model call takes 400 s
            return real(*a, **k)
        kw.with_research, saved = slow, kw.with_research
        try:
            r = self.run_karne(research)
        finally:
            kw.with_research = saved
        self.assertEqual((r["status"], r["created"], r["left"]), ("digital_karne_partial", 2, 1))
        self.now, self.tick[0] = MON_1000 + timedelta(minutes=30), 0.0
        calls = len(self.web.calls)
        r = self.run_karne(research)
        self.assertEqual((r["status"], r["created"], r["already"]), ("digital_karne_completed", 1, 2))
        self.assertEqual(len(self.http_fake.posts()), 3)
        self.assertGreater(len(self.web.calls), calls)

    def test_stored_input_is_reused_after_a_stop_between_check_and_write(self):
        research = Research(self.research_output())
        tracker = kw.KarneTracker(self.ops, self.http, MON)
        checks, inp, allowed = kw.brand_report(brand("wat_mobilite"), None, self.web, MON, MON_1000)
        tracker.save("wat_mobilite", {"state": "checked", "brand": "WAT Mobilite", "checks": checks, "input": inp,
                                      "allowed": allowed})
        self.web.calls.clear()
        r = self.run_karne(research, config=only("wat_mobilite"))
        self.assertEqual(r["created"], 1)
        self.assertEqual(self.web.calls, [])  # no second site check: the same research input
        self.assertEqual(research.inputs, [inp])

    def test_unknown_write_is_never_retried(self):
        self.http_fake.fail_todo.add(WAT)
        r = self.run_karne(config=only("wat_mobilite"))
        self.assertEqual((r["unknown"], r["business_writes"]), (1, 1))
        self.assertEqual(self.state()["brands"]["wat_mobilite"]["state"], "unknown")
        self.http_fake.fail_todo.clear()
        r = self.run_karne(config=only("wat_mobilite"))
        self.assertEqual((r["created"], r["already"]), (0, 1))
        self.assertEqual(len(self.http_fake.posts()), 1)

    def test_a_running_karne_holds_a_second_run(self):
        tracker = kw.KarneTracker(self.ops, self.http, MON)
        self.assertTrue(tracker.claim("other-run", 1800))
        r = self.run_karne()
        self.assertEqual((r["status"], r["reason"]), ("slot_already_claimed", "karne_run_in_progress"))
        self.assertEqual(self.http_fake.calls, [])

    def test_the_next_monthly_karne_reads_the_team_comments_on_the_last_one(self):
        self.run_karne(config=only("wat_mobilite"))
        [tid] = list(self.http_fake.todos)
        self.http_fake.comments[tid] = [
            {"creator": {"id": OZGE}, "content": "<div>Hamle yanlış: KVKK linki footer'da var.</div>"},
            {"creator": {"id": HAMLE_PERSON}, "content": "<div>Hamle notu</div>"}]
        self.now = MON_1000 + timedelta(days=7)  # the alarm Monday in between (same red: silent)
        self.assertEqual(self.run_karne(config=only("wat_mobilite"))["quiet"], 1)
        self.now = MON_1000 + timedelta(days=14)  # 2 Nov: the first Monday of November
        research = Research()
        r = self.run_karne(research, config=only("wat_mobilite"))
        self.assertEqual((r["kind"], r["created"], r["monday"]), ("full", 1, "2026-11-02"))
        self.assertEqual(research.inputs[0]["team_feedback"], ["Hamle yanlış: KVKK linki footer'da var."])
        self.assertIn("Hamle Karne: WAT Mobilite · Kasım 2026", [b["content"] for _, b in self.http_fake.posts()])

    def test_previews_never_write(self):
        research = Research(self.research_output())

        def preview(kind, **k):
            return kw.run_digital_karne(self.ops, config=k.pop("config", kw.load_config()), catalog=CATALOG,
                                        cards=self.cards, web=self.web, research=research, http=None,
                                        clock=lambda: MON_1000 + timedelta(days=3), preview=kind, **k)
        r = preview("checks")
        self.assertEqual((r["status"], r["model_calls"], r["business_writes"], len(r["brands"])),
                         ("digital_karne_preview", 0, 0, 3))
        wat = r["brands"][0]
        self.assertEqual((wat["key"], wat["to"], wat["research"]), ("wat_mobilite", "Özge", "not_called"))
        self.assertIn("/kampanyalar", wat["text"])
        self.assertIn("Aylık Dijital Karne · WAT Mobilite · Ekim 2026", " ".join(wat["text"].split()))
        self.assertEqual(research.inputs, [])
        r = preview("full")  # all three brands with the model
        self.assertEqual(([b["key"] for b in r["brands"]], r["model_calls"]), (["wat_mobilite", "rabam", "ya_da"], 3))
        self.assertEqual(r["brands"][2]["to"], "Esma")
        r = preview("full", only="rabam")
        self.assertEqual([b["key"] for b in r["brands"]], ["rabam"])
        self.assertEqual(len(research.inputs), 4)
        doc = kw.load_config()
        r = preview("full", config=dict(doc, brands=doc["brands"] + [dict(doc["brands"][0], key="dorduncu")]))
        self.assertEqual((r["status"], r["reason"]), ("held", "karne_full_preview_too_many_brands"))
        self.assertEqual((self.http_fake.calls, self.state()), ([], None))

    def test_worker_karne_arguments_only_with_the_karne_mode(self):
        for argv in (["--mode", "daily_digest", "--karne-preview", "checks"],
                     ["--mode", "digital_karne", "--karne-brand", "wat_mobilite"],
                     ["--mode", "digital_karne", "--karne-preview", "checks", "--karne-brand", "WAT;rm"]):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(worker.main(argv), 2)
            self.assertEqual(json.loads(out.getvalue())["reason"], "karne_preview_arguments_invalid", argv)
        for status in ("digital_karne_completed", "digital_karne_partial", "digital_karne_preview"):
            self.assertIn(status, worker.OK_STATUSES)


class Alarm(Karne):
    START = datetime.fromisoformat("2026-10-26T10:00:00+03:00")

    def test_other_mondays_open_a_todo_only_for_a_red_site_finding(self):
        research = Research()
        r = self.run_karne(research, config=only("wat_mobilite"))
        self.assertEqual((r["status"], r["kind"], r["created"], r["model_calls"]), ("digital_karne_completed", "alarm", 1, 0))
        self.assertEqual(research.inputs, [])  # no model on alarm Mondays
        self.assertFalse(any("googleapis" in c or "itunes" in c for c in self.web.calls))  # site checks only
        [(path, body)] = self.http_fake.posts()
        self.assertEqual(path, f"/{ACC}/buckets/{WAT}/todolists/{WAT_LIST}/todos.json")
        self.assertEqual((body["content"], body["assignee_ids"], body["due_on"]),
                         ("Hamle Alarm: WAT Mobilite · 26 Ekim", [OZGE], "2026-10-28"))  # due Wednesday
        self.assertIn("/kampanyalar", body["description"])
        self.assertNotIn("SSL", body["description"])  # yellow findings wait for the monthly karne
        st = self.state(ALARM_MON)
        self.assertEqual((st["run_kind"], st["brands"]["wat_mobilite"]["state"]), ("alarm", "ready"))

    def test_no_red_or_the_same_red_as_last_week_stays_silent(self):
        reds = ["Ana sayfadaki 1 bağlantı açılmıyor (404): /kampanyalar."]
        self.ops._run(lambda tx, _: tx.put(kw.state_key(MON), dict(kw.empty_state(MON), brands={
            "wat_mobilite": {"state": "ready", "brand": "WAT Mobilite", "site_reds": reds}})))
        self.web.pages["https://rabam.com"] = (200, FULL_HOME.replace("Marka", "Rabam"), 0.9)
        self.web.certs["rabam.com"] = 200
        for page in ("a", "b", "c", "kvkk", "cerez"):
            self.web.pages[f"https://rabam.com/{page}"] = (200, "")
        self.web.pages["https://rdap.org/domain/rabam.com"] = (200, {"events": [{"eventAction": "expiration",
                                                                                 "eventDate": "2027-06-01T00:00:00Z"}]})
        r = self.run_karne(config=only("wat_mobilite", "rabam"))
        self.assertEqual((r["created"], r["quiet"], r["business_writes"]), (0, 2, 0))
        self.assertEqual(self.http_fake.calls, [])
        st = self.state(ALARM_MON)["brands"]
        self.assertEqual((st["wat_mobilite"]["state"], st["wat_mobilite"]["site_reds"]), ("quiet", reds))
        self.assertEqual((st["rabam"]["state"], st["rabam"]["site_reds"]), ("quiet", []))
        # a new red the next Monday is reported
        self.now = self.now + timedelta(days=14)  # 9 Nov, an alarm Monday after the 2 Nov karne
        self.web.certs["rabam.com"] = 5
        r = self.run_karne(config=only("rabam"))
        self.assertEqual((r["kind"], r["created"]), ("alarm", 1))
        self.assertEqual(self.http_fake.posts()[0][1]["content"], "Hamle Alarm: Rabam · 9 Kasım")


class Digest(Base):
    START = TUE_0916

    def karne_state(self, day=MON, kind="full"):
        return dict(kw.empty_state(day, kind), brands={
            "wat_mobilite": {"state": "ready", "brand": "WAT Mobilite", "app_url": f"{APP}/buckets/{WAT}/todos/7001",
                             "who": "Özge", "counts": {"kirmizi": 2, "sari": 3, "firsat": 1, "yesil": 4, "bakilamadi": 1}},
            "rabam": {"state": "ready", "brand": "Rabam", "app_url": f"{APP}/buckets/{RABAM}/todos/7002", "who": "Özge",
                      "counts": {"kirmizi": 0, "sari": 1, "yesil": 5, "bakilamadi": 0}},
            "ya_da": {"state": "unknown", "brand": "ya da Multicosmetics"}})

    def test_the_day_after_the_monthly_karne_the_digest_lists_it(self):
        url = f"{APP}/buckets/{WAT}/todos/7001"
        self.ops._run(lambda tx, _: tx.put(kw.state_key(MON), self.karne_state()))
        base = f"https://3.basecampapi.com/{ACC}"
        page, headers = self.bc.pages[f"{base}/buckets/2/todolists/201/todos.json"]
        self.bc.pages[f"{base}/buckets/2/todolists/201/todos.json"] = (
            page + [todo(2003, "Hamle Karne: WAT Mobilite · Ekim 2026", "2026-10-20", [(OZGE, "Özge")])], headers)
        r = self.run_digest()
        self.assertEqual(r["status"], "digest_sent", r)
        self.assertEqual(r["stats"]["karne"], 2)
        text = self.gmail.text()
        self.assertIn("AYLIK DİJİTAL KARNE (EKİM 2026)", text)
        self.assertLess(text.index(f"- WAT Mobilite ({url}): 🔴 2 · 🟡 3 · 💡 1 · 🟢 4 @Özge"),
                        text.index("- Rabam (" + APP + f"/buckets/{RABAM}/todos/7002): 🔴 0 · 🟡 1 · 🟢 5 @Özge"))
        self.assertNotIn("ya da", text.split("AYLIK DİJİTAL KARNE")[1].split("\n\n")[0])
        self.assertNotIn("Hamle Karne:", text)  # the to-do itself is not listed as team work
        html = self.gmail.html()
        self.assertIn("🩺 Aylık dijital karne (Ekim 2026)", html)
        self.assertIn(f'href="{url}"', html)
        self.assertIn(kw.DIGEST_HINT, text)

    def test_alarm_weeks_and_other_days_have_no_karne_section(self):
        self.ops._run(lambda tx, _: tx.put(kw.state_key(MON), self.karne_state(kind="alarm")))
        r = self.run_digest()
        self.assertEqual((r["status"], r["stats"]["karne"]), ("digest_sent", 0))
        self.assertNotIn("KARNE", self.gmail.text())

    def test_wednesday_has_no_karne_section(self):
        self.now = datetime.fromisoformat("2026-10-21T09:16:00+03:00")
        self.ops._run(lambda tx, _: tx.put(kw.state_key(MON), self.karne_state()))
        r = self.run_digest()
        self.assertEqual((r["status"], r["stats"]["karne"]), ("digest_sent", 0))
        self.assertNotIn("KARNE", self.gmail.text())

    def test_digest_rows_only_for_the_full_karne_of_this_week(self):
        st = self.karne_state()
        self.assertEqual(kw.digest_rows(st, date(2026, 10, 20))["title"], "Aylık dijital karne (Ekim 2026)")
        self.assertIsNone(kw.digest_rows(st, date(2026, 10, 27)))  # a week later: stale
        self.assertIsNone(kw.digest_rows(self.karne_state(kind="alarm"), date(2026, 10, 20)))
        self.assertEqual(kw.digest_counts({"counts": {"kirmizi": 1, "sari": 0, "firsat": 0, "yesil": 2}}),
                         "🔴 1 · 🟡 0 · 🟢 2")
