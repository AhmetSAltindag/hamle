"""Haftalık Dijital Karne (Ahmet 2026-10-14 12:27Z): site checks, one research call per brand, one to-do per brand
for its social media lead, Tuesday digest line. Fakes only (public web, Basecamp, Anthropic, Operations store)."""
from datetime import date, datetime, timedelta
import io
import json
import re
from contextlib import redirect_stdout
from urllib.parse import urlsplit

import model_routing
import v11_daily_digest as dd
import v11_weekly_karne as kw
import worker
from test_anthropic_research import research_message, routing
from test_anthropic_routing import FakeClient, KEY
from test_v11_daily_digest import ACC, APP, Base, config, todo
from v11_chain_live import load_catalog
from v11_nb_tracking import HAMLE_PERSON, text_matches

MON_1000 = datetime.fromisoformat("2026-10-19T10:00:00+03:00")
MON = date(2026, 10, 19)
TUE_0916 = datetime.fromisoformat("2026-10-20T09:16:00+03:00")
WAT, RABAM, FENER, INTERNAL, INTERNAL_LIST = 40694338, 48639990, 44320349, 49184053, 10393654435
OZGE, ESMA, AHMET = 52096503, 53167312, 47470759
CATALOG = load_catalog()
DESTS = kw.destinations(CATALOG)

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


def lookup(rating=2.9, count=1500, released="2026-01-10T07:00:00Z"):
    return {"resultCount": 1, "results": [{"trackName": "Rabam", "averageUserRating": rating, "userRatingCount": count,
                                           "version": "3.2", "currentVersionReleaseDate": released,
                                           "trackViewUrl": "https://apps.apple.com/tr/app/rabam/id1571183769?uo=4"}]}


def review(day, stars, title="Başlık", author="Gerçek Kişi Adı"):
    return {"author": {"name": {"label": author}}, "updated": {"label": f"{day}T08:00:00-07:00"},
            "im:rating": {"label": str(stars)}, "title": {"label": title}, "content": {"label": f"{title} uzun metin"}}


class FakeWeb(kw.Web):
    def __init__(self, pages=None, certs=None):
        self.pages, self.certs, self.calls = dict(pages or {}), dict(certs or {}), []

    def get(self, url, *, max_bytes=2_000_000):
        self.calls.append(url)
        key = kw.normalize_url(url) if "itunes" not in url and "rdap" not in url else url
        v = self.pages.get(key)
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
        "https://rabam.com": (200, JS_HOME, 1.1),
        "https://itunes.apple.com/lookup?id=1571183769&country=tr": (200, lookup()),
        "https://itunes.apple.com/tr/rss/customerreviews/id=1571183769/sortBy=mostRecent/json": (200, {"feed": {"entry": [
            review("2026-10-18", 1, "Uygulama açılmıyor"), review("2026-10-17", 2, "Randevu hatası"),
            review("2026-10-15", 1, "Giriş sorunu"), review("2026-10-14", 5, "Harika"),
            review("2026-09-01", 1, "Eski yorum")]}}),
        "https://hanspaces.com": (200, FULL_HOME, 7.5),
        "https://hanspaces.com/a": (200, ""), "https://hanspaces.com/b": (500, ""), "https://hanspaces.com/c": (200, ""),
        "https://hanspaces.com/kvkk": (200, ""), "https://hanspaces.com/cerez": (200, ""),
    }, certs={"watmobilite.com": 30, "rabam.com": 10, "hanspaces.com": kw.KarneError("ssl_invalid")})


def brand(key):
    return next(b for b in kw.validate_config(kw.load_config())["brands"] if b["key"] == key)


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
        self.inputs, self.output, self.fail, self.timeout_seconds = [], output, fail, 420

    def __call__(self, request_input):
        self.inputs.append(request_input)
        if self.fail:
            raise self.fail
        return self.output or {"findings": [], "not_checked": [], "evidence": []}


class Karne(Base):
    START = MON_1000

    def setUp(self):
        super().setUp()
        self.web, self.http_fake, self.tick = wat_web(), FakeBasecamp(), [0.0]
        self.http = kw.KarneBasecamp(ACC, DESTS, send=self.http_fake.send)

    def run_karne(self, research=None, **k):
        k.setdefault("config", kw.load_config())
        return kw.run_weekly_karne(self.ops, catalog=CATALOG, cards=self.cards, web=self.web,
                                   research=research if research is not None else Research(), http=self.http,
                                   clock=lambda: self.now, monotonic=lambda: self.tick[0], **k)

    def state(self, day=MON):
        return self.ops._run(lambda tx, _: tx.get(kw.state_key(day)), allow_expired=True)


class Setup(Karne):
    def test_every_configured_brand_has_a_team_only_destination(self):
        cfg = kw.validate_config(kw.load_config())
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["window"], {"weekday": 0, "start": "09:50", "end": "13:00"})
        keys = [b["key"] for b in cfg["brands"]]
        self.assertEqual(keys, ["wat_mobilite", "rabam", "bwi", "han_spaces", "ya_da", "altinmarka", "bikaldi",
                                "fenerium", "gamechangers"])
        self.assertTrue(set(keys) <= set(DESTS))
        self.assertEqual(DESTS["wat_mobilite"], {"project_id": WAT, "list_id": 8231779952, "person_id": OZGE,
                                                 "internal": False, "card_project_id": WAT})
        self.assertEqual(DESTS["altinmarka"]["list_id"], 8238555593)
        self.assertEqual(DESTS["han_spaces"]["person_id"], ESMA)
        # Fenerium has no verified list: the internal "Hamle İşleri" list, for Ahmet
        self.assertEqual(DESTS["fenerium"], {"project_id": INTERNAL, "list_id": INTERNAL_LIST, "person_id": AHMET,
                                             "internal": True, "card_project_id": FENER})
        self.assertNotIn("cacaonly", keys)
        self.assertIs(brand("bwi")["website_verified"], False)
        self.assertEqual(brand("rabam")["app_store_ids"], ["1571183769"])

    def test_config_rejects_bad_values(self):
        doc = kw.load_config()
        for change in ({"window": {"weekday": 6, "start": "10:00", "end": "12:00"}},
                       {"time_budget_seconds": 5000}, {"enabled": "yes"},
                       {"brands": [dict(doc["brands"][0], website="http://watmobilite.com")]},
                       {"brands": [dict(doc["brands"][0], socials={"myspace": "https://myspace.com/x"})]},
                       {"brands": [dict(doc["brands"][0], app_store_ids=["abc"])]},
                       {"brands": [doc["brands"][0], doc["brands"][0]]}, {"brands": []}):
            with self.assertRaises(kw.KarneError, msg=change):
                kw.validate_config(dict(doc, **change))

    def test_research_role_is_opus_with_web_search_and_packaged(self):
        import package_chain_source as pkg
        r = model_routing.load().route("weekly_karne")
        self.assertEqual((r.provider, r.model, r.effort, r.max_tokens, r.timeout_seconds),
                         ("anthropic", "claude-opus-5-5", "medium", 8000, 420))
        self.assertEqual(r.web_search["max_uses"], 4)
        for name in ("v11_weekly_karne.py", "weekly_karne_prompt.txt"):
            self.assertIn(name, pkg.CHAIN_FILES)
        self.assertIn(pkg.APP + "chain-profile/weekly-karne-config.json", pkg.ALLOWLIST)
        doc = json.loads((model_routing.Path(model_routing.__file__).resolve().parents[2]
                          / "outputs/gamechangers-internal/model-routing.json").read_text(encoding="utf-8"))
        doc["roles"]["weekly_karne"] = {"provider": "openai", "profile": "astra-max-v1"}
        with self.assertRaises(model_routing.RoutingError):
            model_routing.validate(doc)

    def test_week_title_and_state_key(self):
        self.assertEqual(kw.state_key(MON), "weekly-karne-2026-W43")
        self.assertEqual(kw.state_key(date(2026, 10, 25)), "weekly-karne-2026-W43")
        self.assertEqual(kw.title("WAT Mobilite", MON), "Hamle Karne: WAT Mobilite · 43. Hafta")
        self.assertTrue(kw.is_karne_item({"kind": "todo", "title": "Hamle Karne: Rabam · 43. Hafta"}))
        self.assertFalse(kw.is_karne_item({"kind": "card", "title": "Hamle Karne: Rabam · 43. Hafta"}))


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
        findings, _ = kw.site_checks(brand("han_spaces"), self.web, MON, MON_1000)
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
        findings, _ = kw.site_checks(brand("bwi"), self.web, MON, MON_1000)
        self.assertEqual([f["level"] for f in findings], ["bakilamadi"])
        self.assertIn("doğrulanmadı", findings[0]["text"])
        self.web.pages["https://bikaldi.com.tr"] = (403, "", 0.5, "https://bikaldi.com.tr", {"server": "cloudflare"})
        findings, _ = kw.site_checks(brand("bikaldi"), self.web, MON, MON_1000)
        self.assertEqual(found(findings, "bakilamadi")[0],
                         "Site otomatik kontrole izin vermedi (HTTP 403); ana sayfaya tarayıcıdan bakın.")
        self.assertEqual(found(findings, "kirmizi"), [])

    def test_app_store_rating_update_and_last_week_reviews_without_names(self):
        findings, facts = kw.app_checks(brand("rabam"), self.web, MON)
        self.assertEqual(found(findings, "sari"), [
            "Rabam App Store puanı 2,9 (1.500 değerlendirme).",
            "Rabam 9 aydır güncellenmemiş (son sürüm 10 Ocak 2026).",
            "Son 7 günde App Store'da 3 tane 1-2 yıldızlı yorum geldi."])
        self.assertEqual([r["title"] for r in facts["reviews"]], ["Uygulama açılmıyor", "Randevu hatası", "Giriş sorunu"])
        self.assertNotIn("Gerçek Kişi", json.dumps(facts, ensure_ascii=False))
        self.assertEqual(facts["checked"], ["https://apps.apple.com/tr/app/rabam/id1571183769?uo=4"])

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
                sessions.append(url)
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
                web.get(url)
            self.assertEqual(ctx.exception.code, code)
        self.assertEqual(sessions, ["https://watmobilite.com"])


class Model(Karne):
    def test_only_sourced_recent_findings_survive(self):
        allowed = ["https://watmobilite.com"]
        output = {"evidence": [EVIDENCE, NEWS], "not_checked": ["Instagram profili giriş istediği için açılamadı."],
                  "findings": [
                      {"area": "sikayet", "level": "kirmizi", "text": "Arızalı istasyon şikayetleri arttı.",
                       "suggestion": "Müşteriye yanıt önerin.", "url": EVIDENCE, "date": "2026-10-17"},
                      {"area": "gundem", "level": "yesil", "text": "Yeni istasyon haberi.", "suggestion": None,
                       "url": NEWS + "?utm_source=x", "date": "2026-10-16"},
                      {"area": "icerik", "level": "sari", "text": "Eylül kampanyası hâlâ ana sayfada.",
                       "suggestion": "Kaldırın.", "url": "https://watmobilite.com/", "date": None},
                      {"area": "gundem", "level": "sari", "text": "Tarihsiz haber.", "suggestion": None,
                       "url": NEWS, "date": None},
                      {"area": "rakip", "level": "sari", "text": "Eski rakip haberi.", "suggestion": None,
                       "url": NEWS, "date": "2026-09-01"},
                      {"area": "sosyal", "level": "sari", "text": "Uydurma kaynak.", "suggestion": None,
                       "url": "https://never-searched.example/x", "date": None},
                      {"area": "gundem", "level": "sari", "text": "Gelecek tarihli.", "suggestion": None,
                       "url": NEWS, "date": "2026-10-25"}]}
        kept, not_checked = kw.sourced(output, allowed, MON)
        self.assertEqual([f["text"] for f in kept], ["Arızalı istasyon şikayetleri arttı.", "Yeni istasyon haberi.",
                                                    "Eylül kampanyası hâlâ ana sayfada."])
        self.assertEqual(kept[1]["url"], NEWS)
        self.assertEqual(not_checked, ["Instagram profili giriş istediği için açılamadı."])

    def test_research_request_is_bounded_and_one_paid_call_per_input(self):
        b = brand("wat_mobilite")
        checks, facts = kw.site_checks(b, self.web, MON, MON_1000)
        facts["text"] = "ş" * 20000
        inp = kw.model_input(b, {"sector": "şarj"}, MON, checks, facts, ["Hamle yanlış: KVKK linki footer'da var."])
        self.assertLessEqual(len(json.dumps(inp, ensure_ascii=False, separators=(",", ":")).encode()), 15000)
        self.assertEqual(inp["team_feedback"], ["Hamle yanlış: KVKK linki footer'da var."])
        self.assertEqual((inp["today"], inp["week_from"], inp["rivals"][0]), ("2026-10-19", "2026-10-12", "Trugo"))
        self.assertTrue(inp["site_checks"][0].startswith("🟢 Site: Ana sayfa açılıyor"))
        value = {"findings": [{"area": "sikayet", "level": "sari", "text": "Şikayet var.", "suggestion": None,
                               "url": "https://news.example.com/2026/10/09/kampanya", "date": "2026-10-17"}],
                 "not_checked": []}
        client = FakeClient(research_message(value))
        research = kw.research_source(self.ops, {"anthropic": lambda: KEY}, CATALOG, routing=routing(), client=client)
        out = research(inp)
        self.assertEqual(out["findings"][0]["text"], "Şikayet var.")
        self.assertEqual(research(inp), out)  # same input: cached, never a second paid call
        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual((call["model"], call["tools"][0]["max_uses"], call["output_config"]),
                         ("claude-opus-5-5", 4, {"effort": "medium"}))
        self.assertIn("Dijital Karne", call["system"])
        self.assertEqual(research.timeout_seconds, 420)


class Description(Karne):
    def test_todo_text_groups_by_level_with_links_and_visible_text_matches(self):
        findings = [kw._finding("site", "kirmizi", "Ana sayfadaki 1 bağlantı açılmıyor (404): /kampanyalar.",
                                "https://watmobilite.com/kampanyalar", "Bağlantıyı düzeltin."),
                    kw._finding("sosyal", "sari", "Sitede şu hesapların bağlantısı yok: TikTok.", "https://watmobilite.com"),
                    kw._finding("site", "yesil", "Ana sayfa açılıyor (0,8 sn).", "https://watmobilite.com"),
                    kw._finding(None, "bakilamadi", "Instagram profili açılamadı.")]
        html, text = kw.description("WAT Mobilite", MON, findings)
        self.assertTrue(text_matches(html, text))
        flat = " ".join(text.split())
        self.assertIn("Haftalık Dijital Karne · 43. Hafta (19 Ekim 2026)", flat)
        self.assertIn("Özet: 🔴 1 acil · 🟡 1 dikkat · 🟢 1 sorunsuz · ⚪ 1 bakılamadı", flat)
        self.assertIn("🔴 Bu hafta ele alınmalı • Site: Ana sayfadaki 1 bağlantı açılmıyor (404): /kampanyalar. "
                      "Öneri: Bağlantıyı düzeltin. kaynak", flat)
        self.assertIn('<a href="https://watmobilite.com/kampanyalar">kaynak</a>', html)
        self.assertIn("⚪ Bakılamadı • Instagram profili açılamadı.", flat)
        self.assertIn("müşteriye gitmez", flat)
        self.assertNotIn("http", text)  # links are titles, never raw URLs
        _, text = kw.description("X", MON, [kw._finding("site", "yesil", "Ana sayfa açılıyor (0,8 sn).")])
        self.assertIn("Özet: 🔴 0 acil · 🟡 0 dikkat · 🟢 1 sorunsuz", " ".join(text.split()))
        self.assertIn("Bu hafta acil ya da dikkat isteyen bir konu bulmadım.", text)


class Run(Karne):
    def research_output(self):
        return {"evidence": [EVIDENCE], "not_checked": [],
                "findings": [{"area": "sikayet", "level": "kirmizi", "text": "Arızalı istasyon şikayetleri arttı.",
                              "suggestion": None, "url": EVIDENCE, "date": "2026-10-17"}]}

    def test_monday_run_opens_one_todo_per_brand_for_its_lead(self):
        research = Research(self.research_output())
        r = self.run_karne(research)
        self.assertEqual(r["status"], "weekly_karne_completed", r)
        self.assertEqual((r["created"], r["unknown"], r["business_writes"], r["week"]), (9, 0, 9, "2026-W43"))
        self.assertEqual(len(research.inputs), 9)
        posts = {b["content"]: (p, b) for p, b in self.http_fake.posts()}
        path, body = posts["Hamle Karne: WAT Mobilite · 43. Hafta"]
        self.assertEqual(path, f"/{ACC}/buckets/{WAT}/todolists/8231779952/todos.json")
        self.assertEqual((body["assignee_ids"], body["due_on"], body["notify"], body["completion_subscriber_ids"]),
                         ([OZGE], "2026-10-21", True, []))
        self.assertIn("Arızalı istasyon şikayetleri arttı.", body["description"])
        path, body = posts["Hamle Karne: Fenerium · 43. Hafta"]
        self.assertEqual((path, body["assignee_ids"]), (f"/{ACC}/buckets/{INTERNAL}/todolists/{INTERNAL_LIST}/todos.json",
                                                         [AHMET]))
        self.assertEqual(posts["Hamle Karne: HAN Spaces · 43. Hafta"][1]["assignee_ids"], [ESMA])
        st = self.state()
        wat = st["brands"]["wat_mobilite"]
        self.assertEqual((wat["state"], wat["who"], wat["counts"]["kirmizi"]), ("ready", "Özge", 2))
        self.assertNotIn("input", wat)
        self.assertIsNone(wat["checks"])
        self.assertLess(st["lease"]["until"], MON_1000.timestamp() + 1)  # released
        # a second run the same week opens nothing again
        r = self.run_karne(research)
        self.assertEqual((r["status"], r["created"], r["already"]), ("weekly_karne_completed", 0, 9))
        self.assertEqual(len(self.http_fake.posts()), 9)
        self.assertEqual(len(research.inputs), 9)

    def test_research_failure_still_opens_the_todo_and_says_so(self):
        r = self.run_karne(Research(fail=kw.KarneError("model_budget_unavailable")),
                           config=dict(kw.load_config(), brands=[kw.load_config()["brands"][0]]))
        self.assertEqual((r["created"], r["research"]), (1, {"wat_mobilite": "model_budget_unavailable"}))
        [(_, body)] = self.http_fake.posts()
        self.assertIn("araştırması bu hafta alınamadı (model_budget_unavailable)", body["description"])

    def test_outside_window_disabled_and_stopped_write_nothing(self):
        self.now = datetime.fromisoformat("2026-10-20T10:00:00+03:00")  # Tuesday
        self.assertEqual(self.run_karne()["reason"], "outside_karne_window")
        self.now = datetime.fromisoformat("2026-10-19T13:30:00+03:00")
        self.assertEqual(self.run_karne()["reason"], "outside_karne_window")
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
        self.assertEqual((r["status"], r["created"], r["left"]), ("weekly_karne_partial", 3, 6))
        self.now, self.tick[0] = MON_1000 + timedelta(minutes=40), 0.0
        calls = len(self.web.calls)
        r = self.run_karne(research)
        self.assertEqual((r["status"], r["created"], r["already"]), ("weekly_karne_completed", 6, 3))
        self.assertEqual(len(self.http_fake.posts()), 9)
        self.assertGreater(len(self.web.calls), calls)

    def test_stored_input_is_reused_after_a_stop_between_check_and_write(self):
        research = Research(self.research_output())
        cfg = dict(kw.load_config(), brands=[kw.load_config()["brands"][0]])
        tracker = kw.KarneTracker(self.ops, self.http, MON)
        checks, inp, allowed = kw.brand_report(brand("wat_mobilite"), None, self.web, MON, MON_1000)
        tracker.save("wat_mobilite", {"state": "checked", "brand": "WAT Mobilite", "checks": checks, "input": inp,
                                      "allowed": allowed})
        self.web.calls.clear()
        r = self.run_karne(research, config=cfg)
        self.assertEqual(r["created"], 1)
        self.assertEqual(self.web.calls, [])  # no second site check: the same research input
        self.assertEqual(research.inputs, [inp])

    def test_unknown_write_is_never_retried(self):
        self.http_fake.fail_todo.add(WAT)
        cfg = dict(kw.load_config(), brands=[kw.load_config()["brands"][0]])
        r = self.run_karne(config=cfg)
        self.assertEqual((r["unknown"], r["business_writes"]), (1, 1))
        self.assertEqual(self.state()["brands"]["wat_mobilite"]["state"], "unknown")
        self.http_fake.fail_todo.clear()
        r = self.run_karne(config=cfg)
        self.assertEqual((r["created"], r["already"]), (0, 1))
        self.assertEqual(len(self.http_fake.posts()), 1)

    def test_a_running_karne_holds_a_second_run(self):
        tracker = kw.KarneTracker(self.ops, self.http, MON)
        self.assertTrue(tracker.claim("other-run", 1800))
        r = self.run_karne()
        self.assertEqual((r["status"], r["reason"]), ("slot_already_claimed", "karne_run_in_progress"))
        self.assertEqual(self.http_fake.calls, [])

    def test_last_weeks_team_comments_go_to_the_model(self):
        cfg = dict(kw.load_config(), brands=[kw.load_config()["brands"][0]])
        self.run_karne(config=cfg)
        [tid] = list(self.http_fake.todos)
        self.http_fake.comments[tid] = [
            {"creator": {"id": OZGE}, "content": "<div>Hamle yanlış: KVKK linki footer'da var.</div>"},
            {"creator": {"id": HAMLE_PERSON}, "content": "<div>Hamle notu</div>"}]
        self.now = MON_1000 + timedelta(days=7)
        research = Research()
        r = self.run_karne(research, config=cfg)
        self.assertEqual((r["created"], r["week"]), (1, "2026-W44"))
        self.assertEqual(research.inputs[0]["team_feedback"], ["Hamle yanlış: KVKK linki footer'da var."])
        self.assertIn("Hamle Karne: WAT Mobilite · 44. Hafta", [b["content"] for _, b in self.http_fake.posts()])

    def test_previews_never_write(self):
        r = kw.run_weekly_karne(self.ops, config=kw.load_config(), catalog=CATALOG, cards=self.cards, web=self.web,
                                research=Research(), http=None, clock=lambda: MON_1000 + timedelta(days=3),
                                preview="checks")
        self.assertEqual((r["status"], r["model_calls"], r["business_writes"], len(r["brands"])),
                         ("weekly_karne_preview", 0, 0, 9))
        wat = r["brands"][0]
        self.assertEqual((wat["key"], wat["to"], wat["research"]), ("wat_mobilite", "Özge", "not_called"))
        self.assertIn("/kampanyalar", wat["text"])
        research = Research(self.research_output())
        r = kw.run_weekly_karne(self.ops, config=kw.load_config(), catalog=CATALOG, cards=self.cards, web=self.web,
                                research=research, http=None, clock=lambda: MON_1000, preview="full", only="rabam")
        self.assertEqual([b["key"] for b in r["brands"]], ["rabam"])
        self.assertEqual(len(research.inputs), 1)
        r = kw.run_weekly_karne(self.ops, config=kw.load_config(), catalog=CATALOG, cards=self.cards, web=self.web,
                                research=research, http=None, clock=lambda: MON_1000, preview="full")
        self.assertEqual((r["status"], r["reason"]), ("held", "karne_full_preview_needs_one_brand"))
        self.assertEqual((self.http_fake.calls, self.state()), ([], None))

    def test_worker_karne_arguments_only_with_the_karne_mode(self):
        for argv in (["--mode", "daily_digest", "--karne-preview", "checks"],
                     ["--mode", "weekly_karne", "--karne-brand", "wat_mobilite"],
                     ["--mode", "weekly_karne", "--karne-preview", "full"],
                     ["--mode", "weekly_karne", "--karne-preview", "checks", "--karne-brand", "WAT;rm"]):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(worker.main(argv), 2)
            self.assertEqual(json.loads(out.getvalue())["reason"], "karne_preview_arguments_invalid", argv)
        for status in ("weekly_karne_completed", "weekly_karne_partial", "weekly_karne_preview"):
            self.assertIn(status, worker.OK_STATUSES)


class Digest(Base):
    START = TUE_0916

    def test_tuesday_digest_lists_the_karne_and_keeps_karne_todos_out_of_team_work(self):
        url = f"{APP}/buckets/{WAT}/todos/7001"
        state = dict(kw.empty_state(MON), brands={
            "wat_mobilite": {"state": "ready", "brand": "WAT Mobilite", "app_url": url, "who": "Özge",
                             "counts": {"kirmizi": 2, "sari": 3, "yesil": 4, "bakilamadi": 1}},
            "rabam": {"state": "ready", "brand": "Rabam", "app_url": f"{APP}/buckets/{RABAM}/todos/7002", "who": "Özge",
                      "counts": {"kirmizi": 0, "sari": 1, "yesil": 5, "bakilamadi": 0}},
            "bwi": {"state": "unknown", "brand": "BWI"}})
        self.ops._run(lambda tx, _: tx.put(kw.state_key(MON), state))
        base = f"https://3.basecampapi.com/{ACC}"
        page, headers = self.bc.pages[f"{base}/buckets/2/todolists/201/todos.json"]
        self.bc.pages[f"{base}/buckets/2/todolists/201/todos.json"] = (
            page + [todo(2003, "Hamle Karne: WAT Mobilite · 43. Hafta", "2026-10-20", [(OZGE, "Özge")])], headers)
        r = self.run_digest()
        self.assertEqual(r["status"], "digest_sent", r)
        self.assertEqual(r["stats"]["karne"], 2)
        text = self.gmail.text()
        self.assertIn("HAFTALIK DİJİTAL KARNE (43. HAFTA)", text)
        self.assertLess(text.index(f"- WAT Mobilite ({url}): 🔴 2 · 🟡 3 · 🟢 4 @Özge"),
                        text.index("- Rabam (" + APP + f"/buckets/{RABAM}/todos/7002): 🔴 0 · 🟡 1 · 🟢 5 @Özge"))
        self.assertNotIn("BWI", text.split("HAFTALIK DİJİTAL KARNE")[1].split("\n\n")[0])
        self.assertNotIn("Hamle Karne:", text)  # the to-do itself is not listed as team work
        html = self.gmail.html()
        self.assertIn("Haftalık dijital karne (43. hafta)", html)
        self.assertIn(f'href="{url}"', html)
        self.assertIn(kw.DIGEST_HINT, text)

    def test_other_days_have_no_karne_section(self):
        self.now = datetime.fromisoformat("2026-10-21T09:16:00+03:00")
        self.ops._run(lambda tx, _: tx.put(kw.state_key(MON), dict(kw.empty_state(MON), brands={
            "rabam": {"state": "ready", "brand": "Rabam", "app_url": f"{APP}/buckets/{RABAM}/todos/7002",
                      "who": "Özge", "counts": {"kirmizi": 0, "sari": 1, "yesil": 5, "bakilamadi": 0}}})))
        r = self.run_digest()
        self.assertEqual((r["status"], r["stats"]["karne"]), ("digest_sent", 0))
        self.assertNotIn("KARNE", self.gmail.text())
