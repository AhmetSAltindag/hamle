"""Dijital Karne (Ahmet 2026-10-14 12:27Z: "her marka için küçük bir digital audit": dijital varlıklarda,
sosyal medyada ve genel web'de her şey yolunda mı, hatalı ya da güncellenmemiş bir şey var mı?). Plan: Claude Doc
"Hamle AI · Gerçekçi Vizyon", section 1. Ahmet 16:08Z: only the brands we work on most closely (WAT Mobilite,
Rabam, ya da Multicosmetics), a detailed report once a month instead of every Monday.

Mondays from 10:00 Istanbul (job hamle-digital-karne), for each brand of digital-karne-config.json:
- the first Monday of a month (plus the configured start day): the full karne.
  1. Checks Hamle measures itself (public web, GET only, no model): the home page answers and how fast, the
     SSL certificate and (non-.tr) domain expiry, up to 20 links of the home page, the © year, a KVKK / çerez
     link, the social links on the site vs the brand's accounts, the mobile PageSpeed score; App Store rating,
     last update and the last 30 days' reviews for configured apps.
  2. One Claude Opus 5.5 web-search call (role digital_karne, more searches than the daily research): the
     month's news and complaints, the social accounts, competitors, outdated content on the home page and
     next month's opportunities. A finding survives only with an https URL from the search evidence or a page
     Hamle opened itself; news, complaint and competitor findings also need a date in the last 30 days.
     Anything else is dropped, never repaired; the model's summary is shown only when nothing was dropped.
  3. One to-do "Hamle Karne: <Marka> · <Ay Yıl>" in the brand's Sosyal Medya list (catalog brand_routes:
     verified, hidden from clients, Hamle a member), assigned to the brand's social media lead, due Friday.
     Karne to-dos stay out of the team's due/overdue lists; the next day's digest shows one line per brand.
     The next karne gives the team's comments on the previous one to the model as feedback.
- the other Mondays: a silent alarm. Only the site checks (no model, no cost); a to-do "Hamle Alarm: <Marka> ·
  <gün>" opens only when there is a red finding that was not already reported the week before.

The karne is for the team only: no mail, nothing to clients, Hamle never posts or edits a site.
Writes: same rules as idea tracking (durable record before the POST, one attempt, readback, anything
uncertain is 'unknown' and never retried; 'dur' blocks every write). Resumable: a run stops before its
time budget and the next scheduled run continues with the brands left.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from html import escape
from html.parser import HTMLParser
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import time
import uuid
from urllib.parse import quote, urljoin, urlsplit
from zoneinfo import ZoneInfo

from v11_chain_models import _arr, _nstr, _obj, _str, key_for, source_digest
from v11_nb_tracking import (API, APP, HAMLE_PERSON, MONTHS, NB_PROJECT, NbBasecamp, _Text, _code, _line,
                              text_matches)
from v11_trend_search import (_iso_day, _research_gate, _routing, anthropic_research, normalize_url, route_fingerprint,
                              strict_json_text)

ISTANBUL = ZoneInfo("Europe/Istanbul")
CONFIG_NAME = "digital-karne-config.json"
PROMPT = Path(__file__).with_name("digital_karne_prompt.txt")
ROLE = "digital_karne"
PREFIX, ALARM_PREFIX = "Hamle Karne: ", "Hamle Alarm: "
AREAS = {"site": "Site", "icerik": "İçerik", "uygulama": "Uygulama", "sikayet": "Şikayet", "gundem": "Gündem",
         "sosyal": "Sosyal medya", "rakip": "Rakip", "firsat": "Fırsat"}
LEVELS = ("kirmizi", "sari", "firsat", "yesil", "bakilamadi")
ICONS = {"kirmizi": "🔴", "sari": "🟡", "firsat": "💡", "yesil": "🟢", "bakilamadi": "⚪"}
HEADS = {"kirmizi": "Hemen ele alınmalı", "sari": "Dikkat", "firsat": "Fırsatlar", "yesil": "Sorunsuz",
         "bakilamadi": "Bakılamadı"}
COUNT_WORDS = {"kirmizi": "acil", "sari": "dikkat", "firsat": "fırsat", "yesil": "sorunsuz", "bakilamadi": "bakılamadı"}
DATED_AREAS = ("gundem", "sikayet", "rakip")
MAX_FINDINGS, MAX_NOT_CHECKED, MAX_LINKS, LINK_WORKERS, FEEDBACK_WEEKS = 12, 3, 20, 6, 6
SLOW_SECONDS, APP_STALE_DAYS, LOW_RATING, LOW_REVIEWS, LOW_SPEED = 6.0, 180, 3.5, 5, 50
MAX_INPUT_BYTES, HOME_TEXT_CHARS, READ_SECONDS = 15_000, 3000, 30
LEADS = {52096503: "Özge", 53167312: "Esma", 47470759: "Ahmet", 48793256: "Cansu"}
SOCIAL_HOSTS = {"instagram.com": "instagram", "tiktok.com": "tiktok", "x.com": "x", "twitter.com": "x",
                "linkedin.com": "linkedin", "youtube.com": "youtube", "facebook.com": "facebook"}
PLATFORM_NAMES = {"instagram": "Instagram", "tiktok": "TikTok", "x": "X", "linkedin": "LinkedIn",
                  "youtube": "YouTube", "facebook": "Facebook"}
IGNORED_HANDLES = frozenset(("intent", "share", "sharer", "sharer.php", "home", "hashtag", "p", "reel", "reels",
                             "watch", "embed", "explore", "stories", "tr"))
PRIVACY = ("kvkk", "gizlilik", "privacy", "çerez", "cerez", "cookie", "aydınlatma", "aydinlatma", "kişisel veri",
           "kisisel-veri", "kisisel_veri")
UNCHECKABLE = (401, 403, 405, 406, 429)
FOOTER = ("Bu karne yalnız ekip içindir, müşteriye gitmez. Kırmızı maddeleri bu hafta içinde ele alalım. "
          "Yanlış bulduğunuz bir madde varsa bu to-do'ya yorum yazın; Hamle bir sonraki karneyi hazırlarken "
          "yorumları okur. Görevi kapatmak sizde.")
ALARM_FOOTER = ("Bu, Hamle'nin haftalık site alarmıdır: yalnız kırmızı bir sorun olduğunda açılır. Ayrıntılı karne "
                "her ayın ilk pazartesi gelir. Sorun giderilince görevi kapatabilirsiniz.")
PROSPECT_FOOTER = ("Bu ön karne, görüşme öncesi ajans içi hazırlıktır; markaya kendiliğinden gitmez. Hamle markanın "
                   "resmi hesaplarını bilmediği için sosyal medya bağlantısı ve uygulama kontrolleri yapılmadı.")
DIGEST_HINT = ("Karneler yalnız ekip içindir. Kırmızılar bu hafta içinde; yanlış bir madde varsa to-do'ya yorum "
               "yazın, Hamle bir sonraki karnede okur.")


class KarneError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise KarneError(code)


# ------------------------------------------------------------------ config
def load_config(path=None):
    root = Path(__file__).resolve().parents[2]
    candidates = [Path(path)] if path else [Path(__file__).with_name("chain-profile") / CONFIG_NAME,
                                            root / "outputs/gamechangers-internal" / CONFIG_NAME]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                raise KarneError("karne_config_unreadable") from None
    raise KarneError("karne_config_missing")


def _https(value):
    return isinstance(value, str) and normalize_url(value) is not None


def validate_config(doc):
    _need(isinstance(doc, dict) and doc.get("schema_version") == 1 and doc.get("kind") == "digital_karne_config",
          "karne_config_invalid")
    _need(type(doc.get("enabled")) is bool and isinstance(doc.get("version"), str), "karne_config_invalid")
    w = doc.get("window")
    _need(isinstance(w, dict) and w.get("weekday") in range(5)
          and all(isinstance(w.get(k), str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", w[k]) for k in ("start", "end"))
          and w["start"] < w["end"], "karne_window_invalid")
    _need(type(doc.get("time_budget_seconds")) is int and 300 <= doc["time_budget_seconds"] <= 3300,
          "karne_time_budget_invalid")
    _need(type(doc.get("period_days")) is int and 7 <= doc["period_days"] <= 45, "karne_period_invalid")
    _need(type(doc.get("full_first_monday")) is bool and type(doc.get("alarm_other_mondays")) is bool,
          "karne_cadence_invalid")
    extra = doc.get("extra_full_days") or []
    _need(isinstance(extra, list) and all(_iso_day(d) is not None and _iso_day(d).weekday() == w["weekday"]
                                          for d in extra), "karne_extra_days_invalid")
    brands, seen = [], set()
    for b in doc.get("brands") or []:
        _need(isinstance(b, dict) and isinstance(b.get("key"), str) and b["key"] not in seen
              and isinstance(b.get("brand"), str) and 0 < len(b["brand"]) <= 80, "karne_brand_invalid")
        _need(b.get("website") is None or _https(b["website"]), "karne_website_invalid")
        socials = b.get("socials") or {}
        _need(isinstance(socials, dict) and all(p in PLATFORM_NAMES and _https(u) for p, u in socials.items()),
              "karne_socials_invalid")
        apps = b.get("app_store_ids") or []
        _need(isinstance(apps, list) and len(apps) <= 3 and all(isinstance(a, str) and re.fullmatch(r"\d{6,12}", a)
                                                                for a in apps), "karne_apps_invalid")
        rivals = b.get("rivals") or []
        _need(isinstance(rivals, list) and all(isinstance(r, str) and 0 < len(r) <= 60 for r in rivals),
              "karne_rivals_invalid")
        _need(b.get("notes") is None or (isinstance(b["notes"], str) and len(b["notes"]) <= 400), "karne_notes_invalid")
        seen.add(b["key"])
        brands.append({"key": b["key"], "brand": b["brand"], "website": b.get("website"),
                       "website_verified": b.get("website_verified") is True, "socials": dict(socials),
                       "app_store_ids": list(apps), "rivals": list(rivals)[:8], "notes": b.get("notes")})
    _need(brands, "karne_no_brands")
    return {"enabled": doc["enabled"], "version": doc["version"], "window": dict(w),
            "time_budget_seconds": doc["time_budget_seconds"], "period_days": doc["period_days"],
            "full_first_monday": doc["full_first_monday"], "alarm_other_mondays": doc["alarm_other_mondays"],
            "extra_full_days": sorted(set(extra)), "brands": brands}


def kind_of(day, cfg):
    """'full' (first Monday of the month or a configured start day), 'alarm' (the other Mondays) or None."""
    if day.weekday() != cfg["window"]["weekday"]:
        return None
    if day.isoformat() in cfg["extra_full_days"] or (cfg["full_first_monday"] and day.day <= 7):
        return "full"
    return "alarm" if cfg["alarm_other_mondays"] else None


def destinations(catalog):
    """{brand_key: {'project_id', 'list_id', 'person_id', 'internal', 'card_project_id'}}.

    The brand's Sosyal Medya list and lead when the route is verified as client-hidden with Hamle a member
    (as idea tracking); otherwise the verified internal "Hamle İşleri" list for the brand's owner, when that
    owner is a read-back member of the internal project (Fenerium -> Ahmet)."""
    out, catalog = {}, catalog or {}
    bc = catalog.get("basecamp") or {}
    internal = next((r for r in catalog.get("routes") or [] if isinstance(r, dict) and r.get("category_key") == "internal"
                     and r.get("verified") is True and type(r.get("list_id")) is int), None)
    inside = bc.get("verified") is True and bc.get("customer_visible") is False and type(bc.get("project_id")) is int
    for key, r in (catalog.get("brand_routes") or {}).items():
        if not isinstance(r, dict):
            continue
        pid, lid, person = r.get("project_id"), (r.get("lists") or {}).get("social"), r.get("sm_person_id")
        if r.get("verified") is True and r.get("lists_visible_to_clients") is False and r.get("hamle_member") is True \
                and all(type(x) is int and x > 0 for x in (pid, lid, person)):
            out[key] = {"project_id": pid, "list_id": lid, "person_id": person, "internal": False, "card_project_id": pid}
        elif internal and inside and type(person) is int and person in (bc.get("member_ids_readback") or []):
            out[key] = {"project_id": bc["project_id"], "list_id": internal["list_id"], "person_id": person,
                        "internal": True, "card_project_id": pid}
    return out


def monday_of(day):
    return day - timedelta(days=day.weekday())


def state_key(day):
    """One record per run Monday (full karne or alarm)."""
    return "digital-karne-" + monday_of(day).isoformat()


def month_name(day):
    return f"{MONTHS[day.month - 1]} {day.year}"


def title(brand, day, kind="full"):
    if kind == "alarm":
        return ALARM_PREFIX + _line(f"{brand} · {day.day} {MONTHS[day.month - 1]}", 150)
    return PREFIX + _line(f"{brand} · {month_name(day)}", 150)


def is_karne_item(t):
    """A karne to-do (shown in its own digest lines, not listed as team work). Alarms are ordinary work."""
    return t.get("kind") == "todo" and str(t.get("title") or "").startswith(PREFIX)


def _when(d):
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def _num(value, digits=1):
    text = f"{value:,.{digits}f}" if digits else f"{int(value):,}"
    return text.replace(",", "§").replace(".", ",").replace("§", ".")


# ------------------------------------------------------------------ public web (GET only)
class Web:
    """Public pages for the karne: https GET only, no credentials, manual redirects (https, public hosts),
    bounded bodies. session_factory() -> requests-like session; resolve(host) -> getaddrinfo rows."""
    UA = "Mozilla/5.0 (compatible; HamleKarne/1.0; +https://gamechangers.com.tr)"

    def __init__(self, session_factory, *, resolve=None, cert_reader=None, timeout=(6, 15), clock=time.monotonic):
        self.session_factory, self.timeout, self.clock = session_factory, timeout, clock
        self.resolve = resolve or (lambda host: socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP))
        self.cert_reader = cert_reader or self._cert

    def _public(self, host):
        _need(isinstance(host, str) and "." in host and not host.endswith((".internal", ".local", ".localhost")),
              "web_host_not_public")
        try:
            ipaddress.ip_address(host)
            literal = True
        except ValueError:
            literal = False
        _need(not literal, "web_ip_literal")
        try:
            rows = self.resolve(host)
        except OSError:
            raise KarneError("web_dns_failed") from None
        _need(rows and all(ipaddress.ip_address(r[4][0]).is_global for r in rows), "web_host_not_public")

    def get(self, url, *, max_bytes=2_000_000, read_seconds=None, accept="text/html,application/json,*/*"):
        """{'status', 'url' (final), 'body', 'seconds', 'headers'}; at most 5 redirects."""
        started = self.clock()
        for _ in range(6):
            p = urlsplit(url)
            _need(p.scheme == "https" and p.port in (None, 443) and not p.username, "web_url_not_https")
            self._public(p.hostname)
            session, r = self.session_factory(), None
            try:
                r = session.request("GET", url, headers={"User-Agent": self.UA, "Accept": accept,
                                                         "Accept-Language": "tr-TR,tr;q=0.9"},
                                    timeout=(self.timeout[0], read_seconds or self.timeout[1]),
                                    allow_redirects=False, stream=True)
                headers = {k.lower(): v for k, v in dict(r.headers).items()}
                if r.status_code in (301, 302, 303, 307, 308) and headers.get("location"):
                    url = urljoin(url, headers["location"])
                    continue
                chunks, size = [], 0
                if max_bytes:
                    for chunk in r.iter_content(chunk_size=16384):
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= max_bytes or self.clock() - started > (read_seconds or READ_SECONDS) + 5:
                            break
                return {"status": r.status_code, "url": url, "body": b"".join(chunks)[:max_bytes],
                        "seconds": round(self.clock() - started, 2), "headers": headers}
            except KarneError:
                raise
            except Exception as exc:
                name = type(exc).__name__.lower()
                raise KarneError("web_timeout" if "timeout" in name else "web_unreachable") from None
            finally:
                if r is not None:
                    r.close()
                session.close()
        raise KarneError("web_too_many_redirects")

    def json(self, url, *, read_seconds=None, max_bytes=1_500_000):
        # a JSON Accept header: rdap.org sends browsers (text/html) to its web client instead of the data
        page = self.get(url, max_bytes=max_bytes, read_seconds=read_seconds,
                        accept="application/json, application/rdap+json;q=0.9")
        _need(page["status"] == 200, f"web_http_{page['status']}")
        try:
            return json.loads(page["body"].decode("utf-8"))
        except (UnicodeError, ValueError):
            raise KarneError("web_json_invalid") from None

    def cert_days(self, host, now):
        """Days until the TLS certificate expires; KarneError('ssl_invalid') when it does not verify."""
        self._public(host)
        return self.cert_reader(host, now)

    @staticmethod
    def _cert(host, now):
        ctx = ssl.create_default_context()
        try:
            with socket.create_connection((host, 443), timeout=10) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as s:
                    cert = s.getpeercert()
        except ssl.SSLCertVerificationError:
            raise KarneError("ssl_invalid") from None
        except OSError:
            raise KarneError("ssl_unreachable") from None
        return int((ssl.cert_time_to_seconds(cert["notAfter"]) - now.timestamp()) // 86400)


def public_web(**kw):
    import requests

    def session():
        s = requests.Session()
        s.trust_env = False
        s.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
        return s
    return Web(session, **kw)


# ------------------------------------------------------------------ deterministic checks
class _Page(HTMLParser):
    SKIP = ("script", "style", "noscript", "template", "svg")
    BREAKS = ("br", "p", "div", "li", "h1", "h2", "h3", "h4", "section", "footer", "header", "tr")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.title, self.parts, self._skip, self._title, self._a = [], "", [], 0, False, None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "title":
            self._title = True
        elif tag == "a":
            href = dict(attrs).get("href")
            self._a = [href, []] if href else None
            if self._a:
                self.links.append(self._a)
        if tag in self.BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._title = False
        elif tag == "a":
            self._a = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._title:
            self.title += data
            return
        self.parts.append(data)
        if self._a is not None:
            self._a[1].append(data)

    def text(self):
        lines = (" ".join(line.split()) for line in "".join(self.parts).split("\n"))
        return "\n".join(line for line in lines if line)


def parse_page(body):
    p = _Page()
    try:
        p.feed(body.decode("utf-8", errors="replace"))
        p.close()
    except Exception:
        pass
    return p


def _host(url):
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def social_of(url):
    """(platform, handle) of a social profile URL, or None."""
    p = urlsplit(url or "")
    host = (p.hostname or "").lower()
    for prefix in ("www.", "m.", "tr."):
        host = host[len(prefix):] if host.startswith(prefix) else host
    platform = SOCIAL_HOSTS.get(host)
    if platform is None:
        return None
    segs = [s for s in p.path.split("/") if s]
    if platform == "linkedin" and len(segs) >= 2 and segs[0] in ("company", "showcase", "school"):
        handle = segs[1]
    elif platform == "youtube" and len(segs) >= 2 and segs[0] in ("c", "channel", "user"):
        handle = segs[1]
    else:
        handle = segs[0] if segs else ""
    handle = handle.lstrip("@").casefold()
    return platform, ("" if handle in IGNORED_HANDLES else handle)


def _finding(area, level, text, url=None, suggestion=None):
    return {"area": area, "level": level, "text": _line(text, 400), "url": url,
            "suggestion": _line(suggestion, 240) or None}


def _link_status(web, url):
    try:
        return web.get(url, max_bytes=0)["status"]
    except KarneError as exc:
        return exc.code
    except Exception:
        return "web_unreachable"


def site_checks(b, web, today, now):
    """-> (findings, facts). facts: final_url, text, checked (URLs Hamle opened), title."""
    out, facts = [], {"checked": []}
    site = b.get("website")
    if not site:
        return [_finding("site", "bakilamadi", "Sitenin adresi verilmediği için site kontrolleri yapılamadı.")], facts
    try:
        page = web.get(site)
    except KarneError as exc:
        if b.get("website_verified"):
            out.append(_finding("site", "kirmizi", f"Site Hamle'nin kontrolünde açılmadı ({exc.code}).", site,
                                "Siteyi tarayıcıdan açıp kontrol edin; sorun sürüyorsa site ekibine iletin."))
        else:
            out.append(_finding("site", "bakilamadi", f"Site açılmadı ({exc.code}); karnedeki site adresi henüz "
                                "doğrulanmadı, doğru adresi Hamle'ye bildirin.", site))
        return out, facts
    status, final = page["status"], page["url"]
    facts.update(final_url=final, checked=[site, final])
    if status in UNCHECKABLE or (status == 503 and "cloudflare" in str(page["headers"].get("server", "")).lower()):
        out.append(_finding("site", "bakilamadi", f"Site otomatik kontrole izin vermedi (HTTP {status}); ana sayfaya "
                            "tarayıcıdan bakın.", final))
    elif status >= 400:
        out.append(_finding("site", "kirmizi", f"Ana sayfa hata veriyor (HTTP {status}).", final,
                            "Site ekibine hemen iletin."))
    elif page["seconds"] > SLOW_SECONDS:
        out.append(_finding("site", "sari", f"Ana sayfa yavaş açılıyor ({_num(page['seconds'])} sn).", final,
                            "Ağır görselleri küçültüp mobil hız testini (PageSpeed) yeniden yapın."))
    else:
        out.append(_finding("site", "yesil", f"Ana sayfa açılıyor ({_num(page['seconds'])} sn).", final))
    host = urlsplit(final).hostname
    try:
        days = web.cert_days(host, now)
        if days < 21:
            out.append(_finding("site", "kirmizi", f"SSL sertifikasının süresi {max(days, 0)} gün içinde doluyor.",
                                final, "Site ekibine sertifikayı yenilemesini hemen iletin."))
        elif days < 45:
            out.append(_finding("site", "sari", f"SSL sertifikasının süresi {days} gün sonra doluyor.", final,
                                "Site ekibine yenileme tarihini hatırlatın."))
        else:
            out.append(_finding("site", "yesil", f"SSL sertifikası {days} gün daha geçerli.", final))
    except KarneError as exc:
        if exc.code == "ssl_invalid":
            out.append(_finding("site", "kirmizi", "SSL sertifikası geçersiz; tarayıcı güvenlik uyarısı gösterebilir.",
                                final, "Site ekibine hemen iletin."))
        else:
            out.append(_finding("site", "bakilamadi", f"SSL sertifikası okunamadı ({exc.code})."))
    out += domain_check(web, host, today)
    if not 200 <= status < 300:
        return out, facts
    p = parse_page(page["body"])
    facts.update(title=_line(p.title, 120), text=p.text()[:HOME_TEXT_CHARS * 2])
    if len(p.links) < 5:
        out.append(_finding("site", "bakilamadi", "Ana sayfanın içeriği tarayıcıda oluşuyor; bağlantı, KVKK ve "
                            "sosyal medya bağlantısı kontrolleri yapılamadı.", final))
        return out, facts
    if not p.title.strip():
        out.append(_finding("site", "sari", "Ana sayfanın başlığı (title) boş.", final,
                            "Arama sonuçlarında görünen başlığı site ekibiyle birlikte yazın."))
    years = [int(y) for y in re.findall(r"(?:©|\(c\)|copyright)\s*(?:(?:19|20)\d{2}\s*[-–]\s*)?((?:19|20)\d{2})",
                                        p.text(), flags=re.I)]
    if years and max(years) < today.year and today.month > 1:
        out.append(_finding("icerik", "sari", f"Sitenin alt bilgisinde “© {max(years)}” yazıyor.", final,
                            "Yılı güncelleyin ya da otomatik yıla bağlayın."))
    anchors = [(urljoin(final, href.strip()), " ".join("".join(t).split())) for href, t in p.links]
    if not any(any(w in (u + " " + t).casefold() for w in PRIVACY) for u, t in anchors):
        out.append(_finding("icerik", "sari", "Ana sayfada KVKK, gizlilik ya da çerez politikası bağlantısı görünmüyor.",
                            final, "Alt bilgiye KVKK aydınlatma metni ve çerez politikası bağlantısı eklenmeli."))
    out += social_link_check(b, [u for u, _ in anchors], final)
    out += link_check(web, final, [u for u, _ in anchors], facts)
    return out, facts


def domain_check(web, host, today):
    """RDAP expiry for generic TLDs (.com.tr has no public RDAP: skipped quietly)."""
    name = _host("https://" + (host or ""))
    if not name or name.endswith(".tr") or name.count(".") < 1:
        return []
    domain = ".".join(name.split(".")[-2:])
    tld = domain.rsplit(".", 1)[-1]
    url = (f"https://rdap.verisign.com/{tld}/v1/domain/{domain}" if tld in ("com", "net")
           else f"https://rdap.org/domain/{domain}")
    try:
        data = web.json(url)
        ends = [e.get("eventDate") for e in data.get("events") or [] if isinstance(e, dict)
                and e.get("eventAction") == "expiration"]
        until = datetime.fromisoformat(str(ends[0]).replace("Z", "+00:00")).date()
    except Exception:
        return [_finding("site", "bakilamadi", f"Alan adının ({domain}) bitiş tarihi okunamadı.")]
    days = (until - today).days
    if days < 30:
        return [_finding("site", "kirmizi", f"Alan adının ({domain}) süresi {max(days, 0)} gün içinde doluyor.", None,
                         "Alan adını yenilemesi için müşteriye ve site ekibine hemen haber verin.")]
    if days < 60:
        return [_finding("site", "sari", f"Alan adının ({domain}) süresi {days} gün sonra doluyor.", None,
                         "Yenileme tarihini müşteriye hatırlatın.")]
    return [_finding("site", "yesil", f"Alan adı ({domain}) {_when(until)} tarihine kadar kayıtlı.")]


def _comparable(platform, handle):
    """A YouTube channel id (UC…) or a numeric LinkedIn company id says nothing about the handle: not a mismatch."""
    return not ((platform == "youtube" and re.fullmatch(r"uc[0-9a-z_-]{22}", handle))
                or (platform == "linkedin" and handle.isdigit()))


def social_link_check(b, urls, final):
    wanted = {p: social_of(u) for p, u in (b.get("socials") or {}).items()}
    wanted = {p: s[1] for p, s in wanted.items() if s and s[1]}
    if not wanted:
        return []
    found = {}
    for u in urls:
        s = social_of(u)
        if s and s[1]:
            found.setdefault(s[0], set()).add(s[1])
    missing = [PLATFORM_NAMES[p] for p in wanted if p not in found]
    wrong = [f"{PLATFORM_NAMES[p]} ({', '.join(sorted(found[p]))[:60]})" for p, h in wanted.items()
             if p in found and h not in found[p] and all(_comparable(p, x) for x in found[p])]
    out = []
    if wrong:
        out.append(_finding("sosyal", "sari", "Sitedeki sosyal medya bağlantısı başka bir hesaba gidiyor: "
                            + ", ".join(wrong) + ".", final, "Bağlantıları markanın resmi hesaplarıyla değiştirin."))
    if missing:
        out.append(_finding("sosyal", "sari", "Sitede şu hesapların bağlantısı yok: " + ", ".join(missing) + ".", final,
                            "Sitenin alt bilgisine resmi hesapların bağlantılarını ekleyin."))
    if not out:
        out.append(_finding("sosyal", "yesil", "Sitedeki sosyal medya bağlantıları doğru hesaplara gidiyor.", final))
    return out


def link_check(web, final, urls, facts):
    home, seen, todo = _host(final), {normalize_url(final)}, []
    for u in urls:
        n = normalize_url(u.split("#", 1)[0])
        if n is None or _host(n) != home or n in seen:
            continue
        seen.add(n)
        todo.append(n)
    todo = todo[:MAX_LINKS]
    if not todo:
        return []
    with ThreadPoolExecutor(max_workers=LINK_WORKERS) as pool:
        results = list(zip(todo, pool.map(lambda u: _link_status(web, u), todo)))
    facts["checked"] += todo
    broken = [u for u, s in results if s in (404, 410)]
    failing = [u for u, s in results if isinstance(s, int) and s >= 500]
    unknown = [u for u, s in results if not (isinstance(s, int) and (200 <= s < 400 or s in (404, 410) or s >= 500))]

    def paths(us):
        return ", ".join(_line(urlsplit(u).path or "/", 60) for u in us[:3]) + (" …" if len(us) > 3 else "")
    out = []
    if broken:
        out.append(_finding("site", "kirmizi", f"Ana sayfadaki {len(broken)} bağlantı açılmıyor (404): {paths(broken)}.",
                            broken[0], "Bağlantıları düzeltin ya da kaldırın; site ekibine iletin."))
    if failing:
        out.append(_finding("site", "sari", f"{len(failing)} bağlantı sunucu hatası verdi (HTTP 5xx): {paths(failing)}.",
                            failing[0], "Site ekibine iletin."))
    ok = len(results) - len(broken) - len(failing) - len(unknown)
    if not broken and not failing:
        out.append(_finding("site", "yesil", f"Ana sayfadaki {ok} bağlantı sağlam"
                            + (f", {len(unknown)} tanesi kontrol edilemedi." if unknown else "."), final))
    return out


def pagespeed_api(url, key=None):
    return ("https://www.googleapis.com/pagespeedonline/v5/runPagespeed?strategy=mobile&category=performance"
            "&fields=lighthouseResult%2Fcategories%2Fperformance%2Fscore&url=" + quote(url, safe="")
            + ("&key=" + quote(key, safe="") if key else ""))


def pagespeed_check(web, url, key=None):
    """Mobile performance score from the PageSpeed Insights API. key: hamle-pagespeed-api-key (keyless calls
    share a daily quota that is usually spent). The key never appears in a finding or an error."""
    if not url:
        return []
    try:
        score = web.json(pagespeed_api(url, key), read_seconds=90,
                         max_bytes=6_000_000)["lighthouseResult"]["categories"]["performance"]["score"]
        _need(isinstance(score, (int, float)) and 0 <= score <= 1, "pagespeed_score_invalid")
    except Exception as exc:
        reason = exc.code if isinstance(exc, KarneError) else "pagespeed_answer_invalid"
        return [_finding("site", "bakilamadi", f"Mobil hız skoru (PageSpeed) bu ay okunamadı ({reason}).")]
    points = round(score * 100)
    page = "https://pagespeed.web.dev/analysis?url=" + quote(url, safe="")
    if points < LOW_SPEED:
        return [_finding("site", "sari", f"Mobil hız skoru düşük: {points}/100 (PageSpeed).", page,
                         "Site ekibiyle görselleri ve açılışta yüklenen kodu küçültün.")]
    return [_finding("site", "yesil", f"Mobil hız skoru {points}/100 (PageSpeed).", page)]


def app_checks(b, web, today, days=7):
    """-> (findings, facts {'reviews': [...], 'checked': [app page URLs]}); reviews of the last `days` days."""
    out, facts = [], {"reviews": [], "checked": []}
    for app_id in b.get("app_store_ids") or []:
        try:
            rows = web.json(f"https://itunes.apple.com/lookup?id={app_id}&country=tr").get("results") or []
            app = rows[0]
            page = normalize_url(app.get("trackViewUrl")) or f"https://apps.apple.com/tr/app/id{app_id}"
            name = _line(app.get("trackName"), 60) or "Uygulama"
        except Exception:
            out.append(_finding("uygulama", "bakilamadi", f"App Store bilgisi okunamadı (uygulama {app_id})."))
            continue
        facts["checked"].append(page)
        rating, count = app.get("averageUserRating"), app.get("userRatingCount")
        try:
            updated = datetime.fromisoformat(str(app.get("currentVersionReleaseDate")).replace("Z", "+00:00")).date()
        except ValueError:
            updated = None
        notes = []
        if isinstance(rating, (int, float)) and isinstance(count, int) and count >= 20 and rating < LOW_RATING:
            out.append(_finding("uygulama", "sari", f"{name} App Store puanı {_num(rating)} ({_num(count, 0)} "
                                "değerlendirme).", page, "Düşük puanlı yorumlardaki ortak sorunu müşteriye iletin."))
        elif isinstance(rating, (int, float)) and isinstance(count, int):
            notes.append(f"puanı {_num(rating)} ({_num(count, 0)} değerlendirme)")
        if updated and (today - updated).days > APP_STALE_DAYS:
            out.append(_finding("uygulama", "sari", f"{name} {(today - updated).days // 30} aydır güncellenmemiş "
                                f"(son sürüm {_when(updated)}).", page, "Müşteriye uygulamanın güncelleme planını sorun."))
        elif updated:
            notes.append(f"son sürüm {_when(updated)}")
        try:
            feed = web.json(f"https://itunes.apple.com/tr/rss/customerreviews/id={app_id}/sortBy=mostRecent/json")
            entries = (feed.get("feed") or {}).get("entry") or []
            entries = [entries] if isinstance(entries, dict) else entries
        except Exception:
            entries = None
            out.append(_finding("uygulama", "bakilamadi", f"{name} için App Store yorumları okunamadı."))
        recent = []
        for e in entries or []:
            try:
                day = datetime.fromisoformat(e["updated"]["label"]).date()
                stars = int(e["im:rating"]["label"])
            except (KeyError, TypeError, ValueError):
                continue
            if today - timedelta(days=days) <= day <= today:
                recent.append((day, stars, _line((e.get("title") or {}).get("label"), 80),
                               _line((e.get("content") or {}).get("label"), 200)))
        low = [r for r in recent if r[1] <= 2]
        facts["reviews"] += [{"rating": s, "date": d.isoformat(), "title": t, "text": c} for d, s, t, c in low[:5]]
        if len(low) >= LOW_REVIEWS:
            out.append(_finding("uygulama", "sari", f"Son {days} günde App Store'da {len(low)} tane 1-2 yıldızlı "
                                "yorum geldi.", page,
                                "Yorumlardaki ortak sorunu müşteriye iletin; yorumlara yanıt verilmesini önerin."))
        elif len(low) >= 2 and 2 * len(low) >= len(recent):  # few reviews, but mostly 1-2 stars
            out.append(_finding("uygulama", "sari", f"Son {days} günde App Store'a gelen {len(recent)} yorumun "
                                f"{len(low)} tanesi 1-2 yıldızlı.", page,
                                "Yorumlardaki sorunu müşteriye iletin; yorumlara yanıt verilmesini önerin."))
        elif recent:
            notes.append(f"son {days} günde {len(recent)} yeni yorum ({len(low)} tanesi 1-2 yıldız)")
        if notes:
            out.append(_finding("uygulama", "yesil", f"{name}: " + ", ".join(notes) + ".", page))
    return out, facts


# ------------------------------------------------------------------ model (one web-search call per brand)
def karne_schema():
    return _obj({"summary": _nstr(600),
                 "findings": _arr(_obj({"area": {"type": "string", "enum": list(AREAS)},
                                        "level": {"type": "string", "enum": ["kirmizi", "sari", "yesil"]},
                                        "text": _str(280), "suggestion": _nstr(240), "url": _str(300),
                                        "date": _nstr(10)}), MAX_FINDINGS + 2),
                 "not_checked": _arr(_str(160), MAX_NOT_CHECKED)})


def _json_object(text):
    """The answer's JSON object: the whole text, or the last object with 'findings' when the model wrote
    a sentence around it (a search-heavy answer sometimes narrates between searches)."""
    try:
        return strict_json_text(text)
    except Exception:
        pass
    decoder, best = json.JSONDecoder(), None
    for m in re.finditer(r"\{", text or ""):
        try:
            value, _ = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if isinstance(value, dict) and "findings" in value:
            best = value
    _need(best is not None, "karne_output_not_json")
    return best


def read_output(text):
    """Model answer -> {'summary', 'findings', 'not_checked', 'skipped'}. A malformed finding is skipped and
    counted (as a dropped one: the summary is then not shown), never repaired; only lengths are trimmed."""
    value = _json_object(text)
    _need(isinstance(value.get("findings", []), list), "karne_output_invalid")

    def s(x, limit):
        return _line(x, limit) if isinstance(x, str) and x.strip() else None
    findings, skipped = [], 0
    for f in value.get("findings") or []:
        if not (isinstance(f, dict) and f.get("area") in AREAS and f.get("level") in ("kirmizi", "sari", "yesil", "firsat")
                and s(f.get("text"), 400) and s(f.get("url"), 600)):
            skipped += 1
            continue
        findings.append({"area": f["area"], "level": "sari" if f["level"] == "firsat" else f["level"],
                         "text": s(f["text"], 400), "suggestion": s(f.get("suggestion"), 240), "url": s(f["url"], 600),
                         "date": s(f.get("date"), 10)})
    not_checked = [s(x, 160) for x in value.get("not_checked") or [] if s(x, 160)] \
        if isinstance(value.get("not_checked"), list) else []
    return {"summary": s(value.get("summary"), 600), "findings": findings[:MAX_FINDINGS + 4],
            "not_checked": not_checked[:MAX_NOT_CHECKED], "skipped": skipped}


def model_input(b, card, today, checks, facts, feedback, period_days=30, purpose="client"):
    """Bounded request (<= 15000 bytes): the home page excerpt shrinks first. purpose 'prospect': a brand we are
    about to meet (not a client yet)."""
    inp = {"purpose": purpose, "brand": b["brand"], "sector": (card or {}).get("sector"), "website": b.get("website"),
           "socials": b.get("socials") or {}, "rivals": b.get("rivals") or [], "notes": b.get("notes"),
           "today": today.isoformat(), "period_from": (today - timedelta(days=period_days)).isoformat(),
           "site_checks": [f"{ICONS[f['level']]} {AREAS.get(f['area'], 'Diğer')}: {f['text']}" for f in checks][:24],
           "homepage_text": (facts.get("text") or "")[:HOME_TEXT_CHARS], "app_reviews": facts.get("reviews") or [],
           "team_feedback": [_line(x, 240) for x in feedback][:5]}

    def size():
        return len(json.dumps(inp, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    while size() > MAX_INPUT_BYTES and inp["homepage_text"]:
        inp["homepage_text"] = inp["homepage_text"][:max(0, len(inp["homepage_text"]) - 500)]
    while size() > MAX_INPUT_BYTES and (inp["app_reviews"] or inp["team_feedback"]):
        (inp["app_reviews"] or inp["team_feedback"]).pop()
    _need(size() <= MAX_INPUT_BYTES, "karne_input_too_large")
    return inp


def allowed_urls(b, facts):
    urls = [b.get("website"), facts.get("final_url"), *(b.get("socials") or {}).values(), *(facts.get("checked") or [])]
    return sorted({n for n in (normalize_url(u) for u in urls if u) if n})


def sourced(output, allowed, today, period_days=30):
    """Model findings with an https URL from the search evidence or a page Hamle opened; dated areas need a
    date in the period. Opportunities ('firsat') get their own group. -> (findings, not_checked, dropped)."""
    ok_urls = set(output.get("evidence") or []) | set(allowed)
    out, seen, dropped = [], set(), int(output.get("skipped") or 0)
    for f in output.get("findings") or []:
        url, day = normalize_url(f.get("url")), _iso_day(f.get("date")) if f.get("date") else None
        bad = url is None or url not in ok_urls or (f.get("date") and day is None) or (day is not None and day > today)
        if bad or (f["area"] in DATED_AREAS and (day is None or day < today - timedelta(days=period_days))):
            dropped += 1
            continue
        key = (f["area"], " ".join(f["text"].casefold().split()))
        if key in seen:
            continue
        seen.add(key)
        level = "firsat" if f["area"] == "firsat" else f["level"]
        out.append(_finding(f["area"], level, f["text"], url, f.get("suggestion")))
    return out[:MAX_FINDINGS], [_line(x, 160) for x in output.get("not_checked") or []][:MAX_NOT_CHECKED], dropped


def research_source(ops, load_key, catalog, *, routing=None, client=None):
    """research(request_input) -> raw output with evidence; one chance per request (ModelGate)."""
    route = _routing(routing).route(ROLE)
    _, hold, budget_ok, gate = _research_gate(ops, catalog, route)

    def research(request_input):
        _need(budget_ok(), "model_budget_unavailable")
        return gate().once(ROLE, source_digest(ROLE, request_input, route_fingerprint(route, PROMPT)),
                           lambda: anthropic_research(route, PROMPT, request_input, karne_schema(),
                                                      api_key=key_for(load_key, "anthropic"), client=client,
                                                      code="invalid_digital_karne_schema", reader=read_output),
                           provider=route.provider, hold=hold)
    research.timeout_seconds = route.timeout_seconds
    return research


# ------------------------------------------------------------------ the to-do text
def counts(findings):
    return {lvl: sum(1 for f in findings if f["level"] == lvl) for lvl in LEVELS}


def description(brand, day, findings, kind="full", summary=None):
    """-> (html, visible text). Basecamp rich text: div, strong, a, br only."""
    c = counts(findings)
    if kind == "prospect":
        lines = [f"<strong>{escape(f'Ön Dijital Karne · {brand} · {month_name(day)} ({_when(day)})')}</strong>",
                 escape("Özet: " + " · ".join(f"{ICONS[k]} {c[k]} {COUNT_WORDS[k]}" for k in LEVELS
                                              if c[k] or k not in ("firsat", "bakilamadi")))]
        if summary:
            lines.append(escape("Hamle'nin özeti: " + summary))
    elif kind == "alarm":
        lines = [f"<strong>{escape(f'Haftalık site alarmı · {brand} · {_when(day)}')}</strong>",
                 escape(f"🔴 {c['kirmizi']} acil sorun buldum.")]
    else:
        lines = [f"<strong>{escape(f'Aylık Dijital Karne · {brand} · {month_name(day)} ({_when(day)})')}</strong>",
                 escape("Özet: " + " · ".join(f"{ICONS[k]} {c[k]} {COUNT_WORDS[k]}" for k in LEVELS
                                              if c[k] or k not in ("firsat", "bakilamadi")))]
        if summary:
            lines.append(escape("Hamle'nin özeti: " + summary))
        if not c["kirmizi"] and not c["sari"]:
            lines.append(escape("Bu ay acil ya da dikkat isteyen bir konu bulmadım."))
    for lvl in LEVELS:
        group = [f for f in findings if f["level"] == lvl]
        if not group:
            continue
        lines += ["<br>", f"<strong>{escape(ICONS[lvl] + ' ' + HEADS[lvl])}</strong>"]
        for f in group:
            head = f"<strong>{escape(AREAS[f['area']])}:</strong> " if f.get("area") in AREAS else ""
            line = "• " + head + escape(f["text"])
            if f.get("suggestion"):
                line += " " + escape("Öneri: " + f["suggestion"])
            if f.get("url"):
                line += f' <a href="{escape(f["url"], quote=True)}">kaynak</a>'
            lines.append(line)
    lines += ["<br>", escape({"alarm": ALARM_FOOTER, "prospect": PROSPECT_FOOTER}.get(kind, FOOTER))]
    html = "".join(f"<div>{line}</div>" for line in lines)
    p = _Text()
    p.feed(html)
    p.close()
    return html, "".join(p.parts)


# ------------------------------------------------------------------ Basecamp
class KarneBasecamp(NbBasecamp):
    """Allow-listed calls only: the destination lists, their to-dos and comments, POST a to-do into them."""
    def __init__(self, account_id, dests, *, send):
        super().__init__(account_id, send=send)
        _need(isinstance(dests, dict) and dests, "karne_routes_missing")
        gets, posts = set(), set()
        for d in dests.values():
            b = rf"/{account_id}/buckets/{d['project_id']}"
            gets |= {rf"{b}/todolists/{d['list_id']}", rf"{b}/todos/[1-9][0-9]*",
                     rf"{b}/recordings/[1-9][0-9]*/comments"}
            posts.add(rf"{b}/todolists/{d['list_id']}/todos")
        self.allowed = {"GET": tuple(sorted(gets)), "POST": tuple(sorted(posts))}


def empty_state(day, kind="full"):
    return {"kind": "digital_karne", "schema_version": 1, "monday": monday_of(day).isoformat(), "run_kind": kind,
            "lease": None, "lists": {}, "brands": {}}


class KarneTracker:
    def __init__(self, ops, http, day, kind="full"):
        self.ops, self.http, self.day, self.kind, self.key = ops, http, day, kind, state_key(day)

    def base(self, pid):
        return f"{API}/{self.http.account}/buckets/{pid}"

    def _get(self, key):
        return self.ops._run(lambda tx, _: tx.get(key), allow_expired=True)

    def state(self):
        return self._get(self.key) or empty_state(self.day, self.kind)

    def _write(self, fn, *, fenced=True):
        def op(tx, now):
            if fenced:  # 'dur' / takeover: no new write starts
                c = self.ops._control(tx)
                self.ops._open(c, c["generation"], now)
            st = tx.get(self.key) or empty_state(self.day, self.kind)
            out = fn(st, now)
            tx.put(self.key, st)
            return out
        return self.ops._run(op, allow_expired=not fenced)

    def claim(self, token, seconds):
        def fn(st, now):
            lease = st.get("lease")
            if lease and lease.get("until", 0) > now and lease.get("token") != token:
                return False
            st["lease"] = {"token": token, "until": now + seconds}
            return True
        return self._write(fn)

    def release(self, token):
        def fn(st, now):
            if (st.get("lease") or {}).get("token") == token:
                st["lease"]["until"] = now
        self._write(fn, fenced=False)

    def save(self, key, fields):
        self._write(lambda st, now: st["brands"].setdefault(key, {}).update(fields, at=now))

    def ensure_list(self, pid, lid):
        if (self.state().get("lists") or {}).get(str(lid)) == "ready":
            return lid
        back = self.http.one(f"{self.base(pid)}/todolists/{lid}.json")
        _need(back.get("id") == lid and (back.get("bucket") or {}).get("id") == pid
              and back.get("visible_to_clients") is False and back.get("status") in (None, "active")
              and back.get("type") in (None, "Todolist"), "karne_list_unusable")
        self._write(lambda st, now: st.setdefault("lists", {}).update({str(lid): "ready"}))
        return lid

    def add(self, key, dest, name, html, text, due, summary):
        pid, lid, person = dest["project_id"], dest["list_id"], dest["person_id"]

        def begin(st, now):
            row = st["brands"].setdefault(key, {})
            if row.get("state") in ("writing", "ready", "unknown", "quiet"):
                return False
            row.update(state="writing", project_id=pid, list_id=lid, person_id=person, todo_id=None, app_url=None,
                       title=name, at=now, **summary)
            row.pop("input", None)
            return True
        if not self._write(begin):
            return "already_tracked"
        try:
            row, _ = self.http.call("POST", f"{self.base(pid)}/todolists/{lid}/todos.json",
                                    {"content": name, "description": html, "assignee_ids": [person],
                                     "completion_subscriber_ids": [], "notify": True, "due_on": due.isoformat()}, 201)
            tid = row.get("id") if isinstance(row, dict) else None
            _need(type(tid) is int and tid > 0, "karne_todo_receipt_invalid")
            back = self.http.one(f"{self.base(pid)}/todos/{tid}.json")
            parent = back.get("parent") or {}
            _need(back.get("id") == tid and back.get("content") == name and parent.get("id") == lid
                  and parent.get("type") == "Todolist" and (back.get("bucket") or {}).get("id") == pid
                  and (back.get("creator") or {}).get("id") == HAMLE_PERSON and back.get("visible_to_clients") is False
                  and sorted(a.get("id") for a in back.get("assignees") or [] if isinstance(a, dict)) == [person]
                  and back.get("due_on") == due.isoformat()
                  and text_matches(back.get("description"), text), "karne_todo_readback_mismatch")
        except Exception as exc:
            reason = _code(exc)
            self._write(lambda st, now: st["brands"][key].update(state="unknown", reason=reason), fenced=False)
            return "unknown"
        app = back.get("app_url")
        if not (isinstance(app, str) and app.startswith(f"{APP}/{self.http.account}/buckets/{pid}/todos/")
                and len(app) <= 300 and not any(c in app for c in " <>\"'")):
            app = f"{APP}/{self.http.account}/buckets/{pid}/todos/{tid}"
        self._write(lambda st, now: st["brands"][key].update(state="ready", todo_id=tid, app_url=app), fenced=False)
        return "created"

    def last_site_reds(self, key):
        """The red site findings of this brand on the previous Monday (karne or alarm), or None."""
        row = ((self._get(state_key(self.day - timedelta(days=7))) or {}).get("brands") or {}).get(key) or {}
        return row.get("site_reds") if isinstance(row.get("site_reds"), list) else None

    def feedback(self, key):
        """The team's comments on this brand's previous karne to-do (never Hamle's own), newest last."""
        row = None
        for weeks in range(1, FEEDBACK_WEEKS + 1):
            st = self._get(state_key(self.day - timedelta(days=7 * weeks))) or {}
            r = (st.get("brands") or {}).get(key) or {}
            if st.get("run_kind") == "full" and r.get("state") == "ready" and type(r.get("todo_id")) is int:
                row = r
                break
        if row is None:
            return []
        try:
            rows = self.http.all(f"{self.base(row['project_id'])}/recordings/{row['todo_id']}/comments.json", max_pages=2)
        except Exception:
            return []
        out = []
        for c in rows:
            if not isinstance(c, dict) or (c.get("creator") or {}).get("id") == HAMLE_PERSON:
                continue
            p = _Text()
            p.feed(str(c.get("content") or ""))
            p.close()
            text = _line("".join(p.parts), 240)
            if text:
                out.append(text)
        return out[-5:]


# ------------------------------------------------------------------ run
def _in_window(local, window):
    hm = local.strftime("%H:%M")
    return local.weekday() == window["weekday"] and window["start"] <= hm <= window["end"]


def card_for(cards, project_id):
    return next((c for c in (cards or {}).get("brands") or [] if isinstance(c, dict)
                 and c.get("project_id") == project_id), None)


def brand_report(b, card, web, today, now, feedback=(), period_days=30, psi_key=None, purpose="client"):
    """All checks for the full karne -> (checks, research request input, allowed URLs)."""
    checks, facts = site_checks(b, web, today, now)
    checks += pagespeed_check(web, facts.get("final_url"), psi_key)
    more, app_facts = app_checks(b, web, today, days=period_days)
    checks += more
    facts["reviews"] = app_facts["reviews"]
    facts["checked"] = list(facts.get("checked") or []) + app_facts["checked"]
    request_input = model_input(b, card, today, checks, facts, list(feedback), period_days, purpose)
    return checks, request_input, allowed_urls(b, facts)


def with_research(checks, request_input, allowed, research, today, period_days=30, when="bu ay"):
    """-> (findings, research status, the model's summary only when none of its findings was dropped)."""
    findings, status, summary = list(checks), "not_called", None
    if research is not None:
        try:
            output = research(request_input)
            found, not_checked, dropped = sourced(output, allowed, today, period_days)
            findings += found + [_finding(None, "bakilamadi", x) for x in not_checked]
            status = f"{len(found)}_sourced" + (f"_{dropped}_dropped" if dropped else "")
            if found and not dropped and output.get("summary"):
                summary = _line(output["summary"], 600)
        except Exception as exc:
            status = _code(exc)
            findings.append(_finding(None, "bakilamadi", "Gündem, şikayet, sosyal medya ve rakip araştırması "
                                     f"{when} alınamadı ({status})."))
    return findings, status, summary


def _site_reds(checks):
    return sorted(f["text"] for f in checks if f["level"] == "kirmizi")


def run_digital_karne(ops, *, config, catalog, cards, web, research, http, clock, monotonic=time.monotonic,
                      preview=None, only=None, psi_key=None):
    """Scheduled run (preview None: the full karne or the silent alarm, by the day) or an operator preview of
    the full karne ('checks': no model; 'full': with the model). A preview never writes to Basecamp or the
    karne record."""
    report = {"status": "digital_karne_completed", "created": 0, "unknown": 0, "already": 0, "quiet": 0,
              "skipped": [], "research": {}, "counts": {}, "model_calls": 0, "business_writes": 0}
    try:
        cfg = validate_config(config)
        now = clock()
        local = now.astimezone(ISTANBUL)
        today = local.date()
        period = cfg["period_days"]
        report["monday"] = monday_of(today).isoformat()
        _need(preview in (None, "checks", "full"), "karne_preview_invalid")
        brands = [b for b in cfg["brands"] if only is None or b["key"] == only]
        _need(brands, "karne_brand_unknown")
        _need(preview != "full" or len(brands) <= 3, "karne_full_preview_too_many_brands")
        dests = destinations(catalog)
        if preview is not None:
            out = []
            for b in brands:
                dest = dests.get(b["key"]) or {}
                checks, request_input, allowed = brand_report(b, card_for(cards, dest.get("card_project_id")), web,
                                                              today, now, period_days=period, psi_key=psi_key)
                findings, status, summary = with_research(checks, request_input, allowed,
                                                          research if preview == "full" else None, today, period)
                _, text = description(b["brand"], today, findings, "full", summary)
                out.append({"key": b["key"], "counts": counts(findings), "research": status,
                            "to": LEADS.get(dest.get("person_id")), "internal": dest.get("internal"), "text": text})
            return {"status": "digital_karne_preview", "monday": report["monday"], "brands": out,
                    "model_calls": sum(b["research"] != "not_called" for b in out), "business_writes": 0}
        if not cfg["enabled"]:
            return dict(report, status="no_new_work", reason="karne_disabled")
        if not _in_window(local, cfg["window"]):
            return dict(report, status="no_new_work", reason="outside_karne_window")
        kind = kind_of(today, cfg)
        if kind is None:
            return dict(report, status="no_new_work", reason="no_karne_today")
        report["kind"] = kind
        _need(http is not None, "karne_basecamp_missing")
        tracker, token, started = KarneTracker(ops, http, today, kind), uuid.uuid4().hex, monotonic()
        if not tracker.claim(token, cfg["time_budget_seconds"] + 300):
            return dict(report, status="slot_already_claimed", reason="karne_run_in_progress")
        # worst-case checks (+ one model call on the full karne)
        reserve = 240 + ((getattr(research, "timeout_seconds", None) or 600) if kind == "full" else 0)
        try:
            for b in brands:
                key, dest = b["key"], dests.get(b["key"])
                if dest is None:
                    report["skipped"].append(key + ":no_route")
                    continue
                row = tracker.state()["brands"].get(key) or {}
                if row.get("state") in ("writing", "ready", "unknown", "quiet"):
                    report["already"] += 1
                    continue
                if monotonic() - started + reserve > cfg["time_budget_seconds"]:
                    report["status"], report["left"] = "digital_karne_partial", report.get("left", 0) + 1
                    continue
                summary = None
                if kind == "alarm":
                    checks, _ = site_checks(b, web, today, now)
                    reds = _site_reds(checks)
                    if not reds or reds == tracker.last_site_reds(key):  # nothing new to say: stay silent
                        tracker.save(key, {"state": "quiet", "brand": b["brand"], "site_reds": reds})
                        report["quiet"] += 1
                        continue
                    findings, status = [f for f in checks if f["level"] == "kirmizi"], "not_called"
                else:
                    if row.get("input"):  # a run stopped after the checks: the same request, never a second paid one
                        checks, request_input, allowed = row["checks"], row["input"], row["allowed"]
                    else:
                        checks, request_input, allowed = brand_report(b, card_for(cards, dest["card_project_id"]),
                                                                      web, today, now, tracker.feedback(key), period,
                                                                      psi_key)
                        tracker.save(key, {"state": "checked", "brand": b["brand"], "checks": checks,
                                           "input": request_input, "allowed": allowed})
                    reds = _site_reds(checks)
                    findings, status, summary = with_research(checks, request_input, allowed, research, today, period)
                    report["research"][key] = status
                    report["model_calls"] += status != "not_called" and not status.startswith(("model_budget",
                                                                                               "internal_live"))
                tracker.ensure_list(dest["project_id"], dest["list_id"])
                html, text = description(b["brand"], today, findings, kind, summary)
                c = counts(findings)
                due = monday_of(today) + timedelta(days=4 if kind == "full" else 2)
                fields = {"brand": b["brand"], "counts": c, "who": LEADS.get(dest["person_id"]), "research": status,
                          "site_reds": reds, "checks": None, "allowed": None}
                outcome = tracker.add(key, dest, title(b["brand"], today, kind), html, text, due, fields)
                report["counts"][key] = c
                report["business_writes"] += outcome in ("created", "unknown")
                report[{"created": "created", "unknown": "unknown"}.get(outcome, "already")] += 1
        finally:
            tracker.release(token)
        return report
    except Exception as exc:
        return dict(report, status="held", reason=_code(exc))


def prospect_brand(name, website=None, notes=None):
    """A brand we are going to meet: name (2-80 chars) and its website (https added when missing)."""
    name = _line(name, 81)
    _need(2 <= len(name) <= 80 and not any(ord(c) < 32 for c in name), "karne_prospect_name_invalid")
    site = None
    if website:
        raw = str(website).strip()
        raw = "https://" + raw[len("http://"):] if raw.lower().startswith("http://") else raw
        site = normalize_url(raw if "://" in raw else "https://" + raw)
        _need(site is not None and len(site) <= 300, "karne_prospect_site_invalid")
    _need(notes is None or (isinstance(notes, str) and len(notes) <= 400), "karne_prospect_notes_invalid")
    return {"key": "prospect", "brand": name, "website": site, "website_verified": False, "socials": {},
            "app_store_ids": [], "rivals": [], "notes": notes}


def run_prospect_karne(name, website=None, *, web, research, clock, psi_key=None, period_days=30, notes=None):
    """One-off karne for a brand we are going to meet (Ahmet 2026-10-14: "görüşeceğimiz, ilk görüşmede etkilemek
    istediğimiz markalara da önerebiliriz"). Same checks and research as the monthly karne, no app or social link
    checks (Hamle does not know the brand's accounts). Writes nothing; the caller decides where the text goes."""
    try:
        b = prospect_brand(name, website, notes)
        now = clock()
        today = now.astimezone(ISTANBUL).date()
        checks, request_input, allowed = brand_report(b, None, web, today, now, (), period_days, psi_key, "prospect")
        findings, status, summary = with_research(checks, request_input, allowed, research, today, period_days,
                                                  "bu kez")
    except Exception as exc:
        return {"status": "held", "reason": _code(exc), "model_calls": 0, "business_writes": 0}
    html, text = description(b["brand"], today, findings, "prospect", summary)
    called = status != "not_called" and not status.startswith(("model_budget", "internal_live"))
    return {"status": "prospect_karne_ready", "brand": b["brand"], "website": b["website"], "counts": counts(findings),
            "research": status, "html": html, "text": text, "model_calls": int(called), "business_writes": 0}


# ------------------------------------------------------------------ prospect karne asked on Basecamp
# A team member tags Hamle on a New Business to-do ("@Hamle Koton için ön karne hazırla, site koton.com"): the
# mention desk (v11_basecamp_mentions, action 'karne') queues a request here; the next hamle-digital-karne run
# prepares the karne and writes it as one comment on that to-do.
REQUESTS_KEY = "prospect-karne-requests"
PROSPECT_PER_DAY, PROSPECT_PER_RUN, PROSPECT_LEASE, PROSPECT_ATTEMPTS = 5, 2, 1500, 2
PROSPECT_REASONS = {"karne_prospect_name_invalid": "marka adı okunamadı",
                    "karne_prospect_site_invalid": "site adresi okunamadı"}


def request_key(mention_key):
    return "prospect-karne-" + hashlib.sha256(str(mention_key).encode()).hexdigest()[:32]


def prospect_room(index, day):
    """True while fewer than PROSPECT_PER_DAY requests were queued on this Istanbul day."""
    return int(((index or {}).get("days") or {}).get(day, 0)) < PROSPECT_PER_DAY


def prospect_request(*, mention_key, project_id, todo_id, brand, website, requested_by, day, now):
    """The queued request row (written by the mention desk inside its own transaction)."""
    _need(project_id == NB_PROJECT and type(todo_id) is int and todo_id > 0, "prospect_karne_target_invalid")
    b = prospect_brand(brand, website)
    return {"key": request_key(mention_key), "kind": "prospect_karne_request", "state": "queued",
            "mention_key": mention_key, "project_id": project_id, "todo_id": todo_id, "brand": b["brand"],
            "website": b["website"], "requested_by": {"name": _line(requested_by.get("name"), 80),
                                                       "email": _line(requested_by.get("email"), 120)},
            "day": day, "requested_at": now, "attempts": 0}


def prospect_index(index, key, day):
    """The index after queueing `key` (only today's count is kept)."""
    _need(prospect_room(index, day), "prospect_karne_daily_limit")
    days = {day: int(((index or {}).get("days") or {}).get(day, 0)) + 1}
    keys = [k for k in (index or {}).get("keys") or [] if k != key] + [key]
    return {"kind": "prospect_karne_requests", "keys": keys[-30:], "days": days}


class ProspectBasecamp(NbBasecamp):
    """Allow-listed: read a New Business to-do and a comment, POST one comment on a to-do there."""
    def __init__(self, account_id, *, send):
        super().__init__(account_id, send=send)
        b = rf"/{account_id}/buckets/{NB_PROJECT}"
        self.allowed = {"GET": (rf"{b}/todos/[1-9][0-9]*", rf"{b}/comments/[1-9][0-9]*"),
                        "POST": (rf"{b}/recordings/[1-9][0-9]*/comments",)}


def _visible(html):
    p = _Text()
    p.feed(html)
    p.close()
    return "".join(p.parts)


def run_prospect_requests(ops, *, http, web, research, clock, monotonic=time.monotonic, psi_key=None,
                          budget_seconds=1500):
    """Prepare the queued prospect karnes (at most PROSPECT_PER_RUN per run) and write each as one comment on the
    to-do it was asked on. Same write rules as the karne to-dos: fenced claim ('dur' stops it), durable 'writing'
    record before the POST, readback, anything uncertain is 'unknown' and never retried."""
    report = {"done": 0, "failed": 0, "unknown": 0, "left": 0, "model_calls": 0, "business_writes": 0, "brands": []}

    def get(key):
        return ops._run(lambda tx, _: tx.get(key), allow_expired=True)

    def update(key, token, fenced, **fields):
        def op(tx, now):
            if fenced:
                c = ops._control(tx)
                ops._open(c, c["generation"], now)
            row = tx.get(key)
            if row is None or (token is not None and row.get("token") != token):
                return None
            row.update(fields, updated_at=now)
            tx.put(key, row)
            return row
        return ops._run(op, allow_expired=not fenced)
    if research is None:  # a karne without its research is not worth a comment: the requests wait
        return dict(report, status="held", reason="prospect_research_unavailable")
    try:
        keys = list((get(REQUESTS_KEY) or {}).get("keys") or [])
    except Exception as exc:
        return dict(report, status="held", reason=_code(exc))
    started, worked = monotonic(), 0
    reserve = 240 + (getattr(research, "timeout_seconds", None) or 600)
    for key in keys:
        row = get(key)
        now = clock().timestamp()
        if row is None or not (row.get("state") == "queued" or (row.get("state") == "running"
                                                                  and now >= row.get("lease_until", 0))):
            continue
        if worked >= PROSPECT_PER_RUN or monotonic() - started + reserve > budget_seconds:
            report["left"] += 1
            continue
        token = uuid.uuid4().hex

        def claim(tx, ts):
            c = ops._control(tx)
            ops._open(c, c["generation"], ts)
            r = tx.get(key)
            if r is None or not (r.get("state") == "queued" or (r.get("state") == "running"
                                                                 and ts >= r.get("lease_until", 0))):
                return None
            attempts = int(r.get("attempts") or 0) + 1
            r.update(state="running" if attempts <= PROSPECT_ATTEMPTS else "failed", token=token, attempts=attempts,
                     lease_until=ts + PROSPECT_LEASE, updated_at=ts,
                     **({} if attempts <= PROSPECT_ATTEMPTS else {"reason": "prospect_karne_attempts"}))
            tx.put(key, r)
            return r if attempts <= PROSPECT_ATTEMPTS else None
        try:
            row = ops._run(claim)
        except Exception as exc:
            report.setdefault("reasons", []).append(_code(exc))
            break  # 'dur' or a new generation: nothing more this run
        if row is None:
            continue
        worked += 1
        r = run_prospect_karne(row["brand"], row.get("website"), web=web, research=research, clock=clock,
                               psi_key=psi_key)
        report["model_calls"] += r.get("model_calls", 0)
        first = (row.get("requested_by") or {}).get("name", "").split(" ")[0] or "Merhaba"
        if r["status"] == "prospect_karne_ready":
            html = (f"<div>{escape(first)}, {escape(row['brand'])} için ön karne hazır.</div><div><br></div>"
                    + r["html"])
        else:
            why = PROSPECT_REASONS.get(r.get("reason"), "teknik bir hata oldu")
            html = (f"<div>{escape(first)}, {escape(row['brand'])} için ön karneyi hazırlayamadım ({escape(why)}). "
                    "Markanın adını ve sitesini kontrol edip beni yeniden etiketleyebilirsin.</div>")
        text = _visible(html)
        try:
            if update(key, token, True, state="writing", research=r.get("research"),
                      counts=r.get("counts"), karne_status=r["status"]) is None:
                continue
        except Exception as exc:
            update(key, token, False, state="queued", lease_until=0)  # nothing written: the next run tries again
            report.setdefault("reasons", []).append(_code(exc))
            break
        report["business_writes"] += 1
        base = f"{API}/{http.account}/buckets/{row['project_id']}"
        try:
            back, _ = http.call("POST", f"{base}/recordings/{row['todo_id']}/comments.json", {"content": html}, 201)
            cid = back.get("id") if isinstance(back, dict) else None
            _need(type(cid) is int and cid > 0, "prospect_comment_receipt_invalid")
            got = http.one(f"{base}/comments/{cid}.json")
            _need(got.get("id") == cid and (got.get("parent") or {}).get("id") == row["todo_id"]
                  and (got.get("bucket") or {}).get("id") == row["project_id"]
                  and (got.get("creator") or {}).get("id") == HAMLE_PERSON and got.get("visible_to_clients") is False
                  and text_matches(got.get("content"), text), "prospect_comment_readback_mismatch")
        except Exception as exc:
            update(key, token, False, state="unknown", reason=_code(exc))
            report["unknown"] += 1
            continue
        done = r["status"] == "prospect_karne_ready"
        update(key, token, False, state="ready" if done else "failed", comment_id=cid,
               reason=None if done else r.get("reason"))
        report["done" if done else "failed"] += 1
        report["brands"].append({"brand": row["brand"], "research": r.get("research"), "counts": r.get("counts")})
    return dict(report, status="prospect_karne_completed" if not report["left"] else "prospect_karne_partial")


def digest_rows(state, day):
    """The digest the day after a full karne: {'title', 'rows'} (one row per brand, reds first) or None."""
    if not isinstance(state, dict) or state.get("run_kind") != "full" or state.get("monday") != monday_of(day).isoformat():
        return None
    rows = []
    for key, r in (state.get("brands") or {}).items():
        if r.get("state") != "ready" or not r.get("app_url") or not isinstance(r.get("counts"), dict):
            continue
        rows.append({"brand": _line(r.get("brand") or key, 60), "url": r["app_url"], "who": r.get("who"),
                     "counts": {k: int(r["counts"].get(k) or 0) for k in LEVELS}})
    rows.sort(key=lambda r: (-r["counts"]["kirmizi"], -r["counts"]["sari"], r["brand"]))
    return {"title": f"Aylık dijital karne ({month_name(monday_of(day))})", "rows": rows} if rows else None


def digest_counts(row):
    return " · ".join(f"{ICONS[k]} {row['counts'][k]}" for k in ("kirmizi", "sari", "firsat", "yesil")
                      if k != "firsat" or row["counts"][k])


def run_worker_digital_karne(ops, secrets, *, preview=None, only=None, clock=None, prospect=None, site=None):
    """Entry for worker.py --mode digital_karne (existing credential loaders only). prospect + site: the one-off
    karne of a brand we are going to meet (operator preview, no write)."""
    import basecamp_cloud_auth
    import cloud_providers as provider
    from v11_chain_live import _memo, load_catalog
    from v11_daily_digest import load_brand_cards
    from v11_nb_tracking import session_send
    clock = clock or (lambda: datetime.now(ISTANBUL))
    catalog = load_catalog()
    research = None
    if preview != "checks":
        try:
            research = research_source(ops, {"anthropic": lambda: provider.anthropic_key(secrets)}, catalog)
        except Exception:
            research = None  # the to-do says the research could not be taken
    try:
        psi_key = provider.pagespeed_key(secrets)
    except Exception:
        psi_key = None  # keyless: the shared daily quota, usually spent (the karne says it could not read the score)
    http = None
    if preview is None:
        token = _memo(lambda: basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh))
        http = KarneBasecamp(catalog["basecamp"]["account_id"], destinations(catalog),
                             send=session_send(token, provider._session))
    if prospect is not None:
        r = run_prospect_karne(prospect, site, web=public_web(), research=research, clock=clock, psi_key=psi_key)
        if r["status"] != "prospect_karne_ready":
            return r
        return {"status": "digital_karne_preview", "prospect": True, "model_calls": r["model_calls"],
                "business_writes": 0, "brands": [{"key": "prospect", "brand": r["brand"], "website": r["website"],
                                                  "counts": r["counts"], "research": r["research"], "text": r["text"]}]}
    started = time.monotonic()
    web = public_web()
    result = run_digital_karne(ops, config=load_config(), catalog=catalog, cards=load_brand_cards(), web=web,
                               research=research, http=http, clock=clock, preview=preview, only=only, psi_key=psi_key)
    if preview is None:  # every scheduled run also prepares the prospect karnes asked on Basecamp
        result["prospects"] = run_prospect_requests(
            ops, http=ProspectBasecamp(catalog["basecamp"]["account_id"], send=session_send(token, provider._session)),
            web=web, research=research, clock=clock, psi_key=psi_key,
            budget_seconds=max(0, 1650 - int(time.monotonic() - started)))
    return result
