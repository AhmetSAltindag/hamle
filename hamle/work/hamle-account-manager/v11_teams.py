"""Microsoft Teams v1 (2026-10-14): Hamle reads the agency's Teams channels and group chats and answers when tagged.

The agency talks internally on Teams; Basecamp stays the source of truth for work. Hamle is a Teams bot with the
resource-specific consent (RSC) permissions ChannelMessage.Read.Group and ChatMessage.Read.Chat, so Teams delivers
EVERY message of the channels and group chats it was installed into (not only @mentions):
https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/channel-messages-with-rsc

Runs in a small always-on Cloud Run service (teams_service.py, POST /api/messages), not in the 15-minute jobs:
1. Every request carries a Bot Framework JWT. It is checked against the OpenID metadata of login.botframework.com
   (RS256, issuer, audience = our app id, expiry, the 'msteams' endorsement of the key, serviceUrl claim). The
   metadata fetch is injected (tests stay offline). Only message activities of our tenant are read.
2. Every channel / group chat message (sender, text, file names) is kept as context per conversation in the
   operations store (bounded). config teams.conversations maps a conversation id to a brand key (its Basecamp
   project); an unmapped conversation is kept as general agency context. A 1:1 chat with Hamle itself is kept as
   'personal'. 1:1 chats between people never reach a bot; Hamle never reads Teams through Graph.
3. When Hamle is @mentioned (or written to 1:1), one Claude call (route 'customer_text', teams_mention_prompt.txt)
   picks one action: answer / ask / cannot / task. 'task' writes to Basecamp with the existing brand to-do code:
   a Basecamp to-do link in the message -> one comment on that to-do (v11_chain.BasecampCommentWriter), otherwise a
   new to-do in the brand's list with the jobs config's assignee and "when done" people
   (v11_client_job.JobTodoWriter, same writer client mails use). The Teams reply links the to-do.
   Replies go through the Bot Connector API (serviceUrl of the activity) with an injected sender.

Same rules as the other modules: global stop ('dur') or a new control generation fences every write and nothing
is read while stopped; the internal model cap and the monthly budget hold gate every model call; one model call
per mention; durable record before every provider write; one attempt, anything uncertain is 'unknown' and never
retried. Bot Framework redelivers on timeouts: every activity id is recorded once (dedupe). Per conversation per day
at most max_replies_per_conversation_per_day answers; at most max_model_calls_per_day model calls in total.
"""
from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import json
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from autonomy_policy import scope_digest

ISTANBUL = ZoneInfo("Europe/Istanbul")
PROMPT = Path(__file__).with_name("teams_mention_prompt.txt")
ROLE = "customer_text"
OPENID_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
ISSUER = "https://api.botframework.com"
METADATA_HOST = "login.botframework.com"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
CONNECTOR_SCOPE = "https://api.botframework.com/.default"
CHANNEL = "msteams"
CONVERSATION_TYPES = ("channel", "groupChat", "personal")
ACTIONS = ("answer", "ask", "cannot", "task")
LISTS = ("social", "project")
FIELDS = {"enabled", "app_id", "tenant_id", "service_hosts", "conversations", "keep_messages",
          "max_replies_per_conversation_per_day", "max_model_calls_per_day"}
PLACEHOLDER = "00000000-0000-0000-0000-000000000000"
GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+")
CONVERSATION_ID = re.compile(r"[A-Za-z0-9:@._=+/-]{3,300}")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
SKEW_SECONDS = 300
KEYS_TTL_SECONDS = 24 * 3600
KEYS_REFRESH_MIN_SECONDS = 300
MAX_BODY_BYTES = 256 * 1024
MAX_KEYS_DOC_BYTES = 4 * 1024 * 1024  # Bot Framework's key list carries x5c certificates: ~1 MB, 270 keys (Oct 2026)
MAX_KEYS = 1000
KEY_FIELDS = ("kid", "kty", "use", "n", "e", "endorsements")
MAX_TOKEN_CHARS = 8192
MAX_TEXT = 4000
MAX_STORED_TEXT = 1500
MAX_CONTEXT_TEXT = 600
MAX_FILES = 10
MAX_RECORD_BYTES = 48_000
MAX_INDEX = 80
CONTEXT_FOR_MODEL = 20
CORE_KEY = "teams-core-basecamp"   # cached read-only snapshot of open Basecamp work (all brand projects)
CORE_TTL_SECONDS = 15 * 60
CORE_MAX_ITEMS = 200               # the cached record must stay under the store's 60 KB record limit
CORE_FOR_MODEL = 45
CORE_JOBS_FOR_MODEL = 20
CORE_MEMORY_EVENTS = 25            # v11_brand_memory: the brand's last 30 days, only in a brand's conversation
CORE_MEMORY_MIN_EVENTS = 10        # kept before the open Basecamp items are trimmed
# Brand memory in an agency chat: only when the message names exactly one brand. "ya da" alone is everyday Turkish.
MEMORY_NAMES = {"wat_mobilite": ("wat mobilite", "wat"), "han_spaces": ("han spaces",),
                "ya_da": ("ya da multicosmetics", "multicosmetics", "yada")}
MEMORY_SKIP = ("gamechangers", "new_biz")
ELSEWHERE_FOR_MODEL = 0  # off: a wide chat must not hear what was written in a narrower one (Heads -> ALL TEAM)
MAX_REQUEST_BYTES = 30_000
LEASE_SECONDS = 600
AGENCY_INDEX = "teams-agency"


class TeamsError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise TeamsError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _sha(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _line(value, limit=300):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _first(name):
    return (str(name or "").split() or ["-"])[0]


def _b64(text):
    _need(isinstance(text, str) and re.fullmatch(r"[A-Za-z0-9_-]*", text), "auth_token_malformed")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def base_conversation(conversation_id):
    """A channel thread's id is '<channel id>;messageid=<root>': the channel is the conversation."""
    return str(conversation_id or "").split(";", 1)[0]


def conversation_key(conversation_id):
    return "teams-conv-" + _sha(base_conversation(conversation_id))


def activity_key(conversation_id, activity_id):
    return "teams-act-" + _sha(f"{base_conversation(conversation_id)}|{activity_id}")


def mention_key(conversation_id, activity_id):
    return "teams-mention-" + _sha(f"{base_conversation(conversation_id)}|{activity_id}")


def brand_index_key(brand_key):
    return "teams-brand-" + brand_key


def day_key(day):
    return f"teams-day-{day}"


# ------------------------------------------------------------------ config
def validate_teams(block, catalog):
    """Config 'teams' block (client-watch-config.json) -> settings; None when absent or disabled."""
    if block is None:
        return None
    _need(isinstance(block, dict) and set(block) == FIELDS and type(block["enabled"]) is bool, "teams_config_invalid")
    _need(isinstance(block["conversations"], dict) and isinstance(block["service_hosts"], list),
          "teams_config_invalid")
    if not block["enabled"]:
        return None
    for key in ("app_id", "tenant_id"):
        _need(isinstance(block[key], str) and GUID.fullmatch(block[key]) and block[key] != PLACEHOLDER,
              "teams_" + key + "_missing")
    hosts = block["service_hosts"]
    _need(1 <= len(hosts) <= 5 and len(set(hosts)) == len(hosts)
          and all(isinstance(h, str) and HOST.fullmatch(h) for h in hosts), "teams_service_hosts_invalid")
    for key, low, high in (("keep_messages", 10, 60), ("max_replies_per_conversation_per_day", 1, 50),
                           ("max_model_calls_per_day", 1, 200)):
        _need(type(block[key]) is int and low <= block[key] <= high, "teams_config_invalid")
    import v11_brand_routing as routing
    brands = {}
    for key, route in (catalog.get("brand_routes") or {}).items():
        if isinstance(route, dict) and routing.usable(catalog, key):
            brands[key] = {"name": route.get("brand_name") or key, "project_id": route["project_id"],
                           "lists": {n: route["lists"][n] for n in LISTS}}
    conversations = {}
    _need(len(block["conversations"]) <= 50, "teams_conversations_invalid")
    for cid, brand_key in block["conversations"].items():
        _need(isinstance(cid, str) and CONVERSATION_ID.fullmatch(cid) and ";" not in cid
              and brand_key in brands, "teams_conversations_invalid")
        conversations[cid] = brand_key
    return {"app_id": block["app_id"], "bot_id": "28:" + block["app_id"], "tenant_id": block["tenant_id"],
            "service_hosts": tuple(hosts), "conversations": conversations, "brands": brands,
            "keep_messages": block["keep_messages"], "max_replies": block["max_replies_per_conversation_per_day"],
            "max_model_calls": block["max_model_calls_per_day"], "account_id": catalog["basecamp"]["account_id"]}


def service_url_ok(url, settings):
    p = urlsplit(url if isinstance(url, str) else "")
    return (p.scheme == "https" and p.hostname in settings["service_hosts"] and p.port is None
            and not p.query and not p.fragment and not p.username and len(url) <= 300)


# ------------------------------------------------------------------ Bot Framework JWT
class BotAuth:
    """verify(authorization_header, service_url=..., channel_id=...) -> claims, or raises TeamsError('auth_...').

    fetch_json(url) -> dict is injected: the OpenID metadata and its keys are fetched through it only (tests pass a
    fake). Keys are cached for a day; an unknown key id refreshes them at most every five minutes.
    """
    def __init__(self, app_id, *, fetch_json, clock=time.time):
        self.app_id, self.fetch_json, self.clock = app_id, fetch_json, clock
        self.keys, self.fetched_at = {}, None

    def _refresh(self):
        meta = self.fetch_json(OPENID_URL)
        _need(isinstance(meta, dict) and meta.get("issuer") == ISSUER, "auth_metadata_invalid")
        uri = meta.get("jwks_uri")
        p = urlsplit(uri if isinstance(uri, str) else "")
        _need(p.scheme == "https" and p.hostname == METADATA_HOST, "auth_metadata_invalid")
        doc = self.fetch_json(uri)
        rows = doc.get("keys") if isinstance(doc, dict) else None
        _need(isinstance(rows, list) and 0 < len(rows) <= MAX_KEYS, "auth_keys_invalid")
        self.keys = {k["kid"]: {f: k[f] for f in KEY_FIELDS if f in k}  # certificates are not kept in memory
                     for k in rows if isinstance(k, dict) and isinstance(k.get("kid"), str)}
        self.fetched_at = self.clock()

    def _key(self, kid):
        now = self.clock()
        if self.fetched_at is None or now - self.fetched_at > KEYS_TTL_SECONDS:
            self._refresh()
        elif kid not in self.keys and now - self.fetched_at > KEYS_REFRESH_MIN_SECONDS:
            self._refresh()
        _need(kid in self.keys, "auth_key_unknown")
        return self.keys[kid]

    @staticmethod
    def _rsa_verify(jwk, signed, signature):
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        _need(jwk.get("kty") == "RSA" and jwk.get("use", "sig") == "sig", "auth_key_invalid")
        try:
            n = int.from_bytes(_b64(jwk["n"]), "big")
            e = int.from_bytes(_b64(jwk["e"]), "big")
            key = rsa.RSAPublicNumbers(e, n).public_key()
        except TeamsError:
            raise
        except Exception:
            raise TeamsError("auth_key_invalid") from None
        try:
            key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature:
            raise TeamsError("auth_signature_invalid") from None

    def verify(self, header, *, service_url, channel_id):
        _need(isinstance(header, str) and header.startswith("Bearer ") and len(header) <= MAX_TOKEN_CHARS,
              "auth_missing")
        parts = header[len("Bearer "):].strip().split(".")
        _need(len(parts) == 3, "auth_token_malformed")
        try:
            head, claims = json.loads(_b64(parts[0])), json.loads(_b64(parts[1]))
        except TeamsError:
            raise
        except Exception:
            raise TeamsError("auth_token_malformed") from None
        _need(isinstance(head, dict) and isinstance(claims, dict), "auth_token_malformed")
        _need(head.get("alg") == "RS256" and isinstance(head.get("kid"), str), "auth_algorithm_invalid")
        jwk = self._key(head["kid"])
        endorsements = jwk.get("endorsements")
        _need(isinstance(endorsements, list) and channel_id in endorsements, "auth_channel_not_endorsed")
        self._rsa_verify(jwk, (parts[0] + "." + parts[1]).encode("ascii"), _b64(parts[2]))
        now = self.clock()
        _need(claims.get("iss") == ISSUER, "auth_issuer_invalid")
        aud = claims.get("aud")
        _need(aud == self.app_id or (isinstance(aud, list) and aud == [self.app_id]), "auth_audience_invalid")
        exp, nbf = claims.get("exp"), claims.get("nbf")
        _need(isinstance(exp, (int, float)) and now <= exp + SKEW_SECONDS, "auth_token_expired")
        _need(nbf is None or (isinstance(nbf, (int, float)) and nbf <= now + SKEW_SECONDS), "auth_token_not_yet_valid")
        _need(claims.get("serviceurl") == service_url, "auth_service_url_mismatch")
        return claims


def https_json(session_factory=None, *, limit=MAX_KEYS_DOC_BYTES):
    """fetch_json for BotAuth: one GET to login.botframework.com, no redirects, bounded body."""
    def fetch(url):
        import cloud_providers as provider
        p = urlsplit(url)
        _need(p.scheme == "https" and p.hostname == METADATA_HOST, "auth_metadata_host_forbidden")
        session, response = (session_factory or provider._session)(), None
        try:
            response = session.request("GET", url, timeout=(10, 20), allow_redirects=False, stream=True)
            _need(response.status_code == 200, "auth_metadata_unavailable")
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                raw += chunk
                _need(len(raw) <= limit, "auth_metadata_too_large")
            return json.loads(bytes(raw))
        finally:
            if response is not None:
                response.close()
            session.close()
    return fetch


# ------------------------------------------------------------------ Bot Connector: replies
class ConnectorSender:
    """send(service_url, conversation_id, reply_to_id, text) -> {'id'}: one POST to the Bot Connector API.

    The app token comes from the agency's single-tenant Entra app (client credentials, scope
    api.botframework.com/.default); secret_loader() returns the client secret from Secret Manager.
    """
    def __init__(self, settings, *, secret_loader, session_factory=None, clock=time.time):
        self.s, self.secret_loader, self.clock = settings, secret_loader, clock
        self.session_factory = session_factory
        self.token, self.token_until = None, 0

    def _session(self):
        import cloud_providers as provider
        return (self.session_factory or provider._session)()

    def _call(self, method, url, *, data=None, body=None, headers=None, expected=(200,)):
        session, response = self._session(), None
        try:
            if body is not None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            response = session.request(method, url, data=data, headers=headers or {}, timeout=(10, 30),
                                       allow_redirects=False, stream=True)
            _need(response.status_code in expected, "teams_connector_response_unconfirmed")
            raw = b"".join(response.iter_content(chunk_size=16384))
            _need(len(raw) <= 65536, "teams_connector_response_too_large")
            return json.loads(raw) if raw else {}
        finally:
            if response is not None:
                response.close()
            session.close()

    def _access_token(self):
        if self.token is not None and self.clock() < self.token_until - 120:
            return self.token
        secret = self.secret_loader()
        try:
            row = self._call("POST", TOKEN_URL.format(tenant=self.s["tenant_id"]),
                             data=urlencode({"grant_type": "client_credentials", "client_id": self.s["app_id"],
                                             "client_secret": secret, "scope": CONNECTOR_SCOPE}).encode("ascii"),
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
        finally:
            del secret
        token, life = row.get("access_token"), row.get("expires_in")
        _need(isinstance(token, str) and 20 <= len(token) <= 8192 and isinstance(life, int) and life > 0,
              "teams_token_invalid")
        self.token, self.token_until = token, self.clock() + life
        return token

    def send(self, service_url, conversation_id, reply_to_id, text):
        _need(service_url_ok(service_url, self.s), "teams_service_url_forbidden")
        _need(isinstance(conversation_id, str) and 0 < len(conversation_id) <= 400
              and isinstance(reply_to_id, str) and 0 < len(reply_to_id) <= 200
              and isinstance(text, str) and 0 < len(text) <= 6000, "teams_reply_invalid")
        url = (service_url.rstrip("/") + "/v3/conversations/" + quote(conversation_id, safe="")
               + "/activities/" + quote(reply_to_id, safe=""))
        row = self._call("POST", url, body={"type": "message", "text": text, "textFormat": "markdown",
                                            "replyToId": reply_to_id},
                         headers={"Authorization": "Bearer " + self._access_token(),
                                  "Content-Type": "application/json; charset=utf-8"}, expected=(200, 201))
        _need(isinstance(row.get("id"), str) and 0 < len(row["id"]) <= 200, "teams_reply_receipt_unconfirmed")
        return {"id": row["id"]}


# ------------------------------------------------------------------ activities
def plain_text(html):
    text = re.sub(r"<at>(.*?)</at>", r"@\1", str(html or ""), flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.IGNORECASE)
    text = unescape(re.sub(r"<[^>]{0,2000}>", " ", text))
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)[:MAX_TEXT]


def files_of(activity):
    out = []
    for a in activity.get("attachments") or []:
        if not isinstance(a, dict) or len(out) >= MAX_FILES:
            continue
        kind = str(a.get("contentType") or "")
        if kind == "text/html" or kind.startswith("application/vnd.microsoft.card."):
            continue  # Teams' own copy of the message body / cards
        name = _line(a.get("name"), 255)
        if not name:
            continue
        url = a.get("contentUrl")
        out.append({"name": name, "type": _line(kind, 120),
                    "url": url[:500] if isinstance(url, str) and url.startswith("https://") else None})
    return out


def parse_activity(activity, settings):
    """A verified message activity -> normalized message; TeamsError for anything outside the agreed scope."""
    conv = activity.get("conversation") or {}
    data = activity.get("channelData") or {}
    kind = conv.get("conversationType")
    _need(kind in CONVERSATION_TYPES, "teams_conversation_type_unsupported")
    tenant = conv.get("tenantId") or (data.get("tenant") or {}).get("id")
    _need(tenant == settings["tenant_id"], "teams_tenant_mismatch")
    _need((activity.get("recipient") or {}).get("id") == settings["bot_id"], "teams_recipient_mismatch")
    if kind == "personal":
        # Only a 1:1 chat WITH Hamle reaches here (Teams never gives a bot the 1:1 chats between people).
        _need(conv.get("isGroup") in (None, False), "teams_personal_chat_invalid")
    cid, aid = conv.get("id"), activity.get("id")
    _need(isinstance(cid, str) and 0 < len(cid) <= 400 and CONVERSATION_ID.fullmatch(base_conversation(cid))
          and isinstance(aid, str) and 0 < len(aid) <= 200, "teams_activity_invalid")
    sender = activity.get("from") or {}
    _need(isinstance(sender.get("id"), str) and sender["id"] != settings["bot_id"], "teams_sender_invalid")
    mentioned = kind == "personal" or any(
        isinstance(e, dict) and e.get("type") == "mention" and (e.get("mentioned") or {}).get("id") == settings["bot_id"]
        for e in activity.get("entities") or [])
    base = base_conversation(cid)
    brand_key = settings["conversations"].get(base)
    team, channel = (data.get("team") or {}).get("name"), (data.get("channel") or {}).get("name")
    return {"activity_id": aid, "conversation_id": cid, "base": base, "type": kind,
            "service_url": activity.get("serviceUrl"), "reply_to_id": aid, "brand_key": brand_key,
            "scope": "personal" if kind == "personal" else ("brand" if brand_key else "agency"),
            "name": _line(" / ".join(x for x in (team, channel, conv.get("name")) if x), 200) or None,
            "author": _line(sender.get("name"), 120) or "-", "author_aad": _line(sender.get("aadObjectId"), 64) or None,
            "text": plain_text(activity.get("text")), "files": files_of(activity), "mentioned": mentioned,
            "at": _line(activity.get("timestamp"), 40) or None}


def _fit(row, keep):
    row["messages"] = row["messages"][-keep:]
    while len(json.dumps(row, ensure_ascii=False).encode("utf-8")) > MAX_RECORD_BYTES and len(row["messages"]) > 1:
        row["messages"].pop(0)
    return row


def teams_context(ops, brand_key=None, limit=30):
    """Recent Teams messages for a brand (or the agency's unmapped conversations): newest last. Read only."""
    def read(tx, _):
        index = tx.get(brand_index_key(brand_key) if brand_key else AGENCY_INDEX) or {}
        rows = []
        for key in index.get("keys") or []:
            conv = tx.get(key) or {}
            rows += [dict(m, conversation=conv.get("name"), conversation_key=key) for m in conv.get("messages") or []]
        return rows
    rows = ops._run(read, allow_expired=True)
    return sorted(rows, key=lambda m: m.get("received_at") or 0)[-limit:]


def named_brand(text, brands):
    """The one brand a message names (whole words, Turkish-safe), else None; never Gamechangers or New Biz."""
    from v11_brand_memory import _fold
    folded = _fold(text)
    hits = set()
    for key, brand in brands.items():
        if key in MEMORY_SKIP:
            continue
        for name in MEMORY_NAMES.get(key, (brand.get("name") or key,)):
            if re.search(r"(?<!\w)" + re.escape(_fold(name)) + r"(?!\w)", folded):
                hits.add(key)
    return hits.pop() if len(hits) == 1 else None


# ------------------------------------------------------------------ Hamle's core (read only)
class CoreContext:
    """What the rest of Hamle knows, for Teams answers: open Basecamp work, client mail jobs, the last digest özü and,
    in a brand's conversation, that brand's memory (v11_brand_memory: profile + the last 30 days' events).

    reader_factory() -> v11_daily_digest.BasecampDigestReader (GET-only, allowlisted paths). Its listing is cached
    in the operations store for CORE_TTL_SECONDS so a Teams answer usually costs no Basecamp reads. Nothing here
    writes to Basecamp or mail. __call__ never raises: a missing part is reported as {"unavailable": code}.
    """
    def __init__(self, ops, *, reader_factory, clock, ttl=CORE_TTL_SECONDS):
        self.ops, self.reader_factory, self.clock, self.ttl = ops, reader_factory, clock, ttl

    @staticmethod
    def _compact(items, today):
        rows = []
        for it in items:
            due = it.get("due_on")
            rows.append({"title": _line(it.get("title"), 110), "project": _line(it.get("project"), 60),
                         "project_id": it.get("project_id"), "due_on": due,
                         "who": [_line(n, 40) for n in (it.get("assignees") or [])][:3],
                         "url": it.get("app_url")})
        rows.sort(key=lambda r: (r["due_on"] is None, r["due_on"] or "", r["project"]))
        while rows and (len(rows) > CORE_MAX_ITEMS
                        or len(json.dumps(rows, ensure_ascii=False).encode("utf-8")) > MAX_RECORD_BYTES):
            rows.pop()  # undated / furthest first
        return rows

    def basecamp(self, today):
        row = self.ops._run(lambda tx, _: tx.get(CORE_KEY), allow_expired=True)
        now = self.clock().timestamp()
        if isinstance(row, dict) and now - float(row.get("at") or 0) <= self.ttl:
            return row
        try:
            listing = self.reader_factory().collect()
            fresh = {"key": CORE_KEY, "kind": "teams_core_basecamp", "at": now, "projects": listing.get("projects"),
                     "open_items": len(listing.get("items") or []),
                     "items": self._compact(listing.get("items") or [], today)}
            self.ops._run(lambda tx, _: tx.put(CORE_KEY, fresh), allow_expired=True)
            return fresh
        except Exception as exc:
            if isinstance(row, dict):
                return dict(row, stale=True)
            return {"unavailable": _code(exc)}

    def jobs(self):
        def read(tx, _):
            out = []
            for key in list((tx.get("client-jobs") or {}).get("keys") or [])[-60:]:
                j = tx.get(key) or {}
                if j.get("kind") != "client_job" or j.get("test") or j.get("state") in ("creating", "binding"):
                    continue
                out.append({"brand": j.get("brand_key"), "title": _line(j.get("title_core") or j.get("title"), 100),
                            "state": j.get("state"), "due_on": j.get("due_on"), "who": j.get("assignee_name"),
                            "client": j.get("client_name"), "origin": j.get("origin") or "mail",
                            "revisions": j.get("revisions"), "url": j.get("app_url")})
            return out
        return self.ops._run(read, allow_expired=True)[-CORE_JOBS_FOR_MODEL:]

    def digest(self, today):
        def read(tx, _):
            for back in range(0, 5):
                day = today - timedelta(days=back)
                row = tx.get("daily-digest-" + day.strftime("%Y%m%d"))
                if isinstance(row, dict) and row.get("state") == "sent" and isinstance(row.get("stats"), dict):
                    st = row["stats"]
                    return {"day": day.isoformat(), "open_items": st.get("open_items"), "overdue": st.get("overdue"),
                            "ozu": st.get("ozu")}
            return None
        return self.ops._run(read, allow_expired=True)

    def brand_memory(self, today, brand_key):
        from v11_brand_memory import brand_memory_context
        return brand_memory_context(self.ops, brand_key, today, days=30, limit=CORE_MEMORY_EVENTS, detail=200)

    def __call__(self, today, project_id=None, brand_key=None):
        out = {}
        parts = [("basecamp", lambda: self.basecamp(today)), ("jobs", self.jobs), ("digest", lambda: self.digest(today))]
        if brand_key:
            parts.append(("brand_memory", lambda: self.brand_memory(today, brand_key)))
        for name, fn in parts:
            try:
                out[name] = fn()
            except Exception as exc:
                out[name] = {"unavailable": _code(exc)}
        bc = out.get("basecamp")
        if isinstance(bc, dict) and isinstance(bc.get("items"), list):
            day = today.isoformat()
            week = (today + timedelta(days=7)).isoformat()
            items = bc["items"]
            overdue = [r for r in items if r["due_on"] and r["due_on"] < day]
            soon = [r for r in items if r["due_on"] and day <= r["due_on"] <= week]
            brand = [r for r in items if project_id is not None and r["project_id"] == project_id]
            picked = list({r["url"] or id(r): r for r in brand[:20] + overdue + soon}.values())[:CORE_FOR_MODEL]
            out["basecamp"] = {"as_of": datetime.fromtimestamp(float(bc.get("at") or 0), ISTANBUL)
                               .strftime("%d.%m.%Y %H:%M"), "stale": bool(bc.get("stale")),
                               "open_items_total": bc.get("open_items"), "overdue_total": len(overdue),
                               "due_this_week_total": len(soon), "items": picked}
        return out


# ------------------------------------------------------------------ model
def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _str(maximum):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


TEAMS_SCHEMA = _obj({
    "action": {"type": "string", "enum": list(ACTIONS)},
    "understood": _str(300),
    "reply": _str(3000),
    "task": {"type": ["object", "null"], "properties": {
        "brand_key": {"type": ["string", "null"], "minLength": 1, "maxLength": 60},
        "list": {"type": "string", "enum": list(LISTS)},
        "title": _str(80),
        "brief": _str(1500),
        "due_on": {"type": ["string", "null"], "minLength": 1, "maxLength": 10}},
        "required": ["brand_key", "list", "title", "brief", "due_on"], "additionalProperties": False}})


def teams_source(ops, load_key, catalog, *, routing=None, client=None):
    """decide(request, parts) -> validated TEAMS_SCHEMA output; ModelGate once per mention (internal cap)."""
    import model as base
    import model_routing
    from v11_chain_live import budget_gate
    from v11_chain_models import ModelGate, internal_cap_microdollars, key_for, reservation_for, source_digest, validate
    from v11_trend_search import route_fingerprint
    route = (routing if routing is not None else model_routing.load()).route(ROLE)
    _need(route.provider == "anthropic", "teams_route_not_claude")
    route = dataclasses.replace(route, effort="medium", max_tokens=min(route.max_tokens, 8000),
                                timeout_seconds=min(route.timeout_seconds, 120))
    cap = internal_cap_microdollars(catalog)
    hold = reservation_for(route)

    def decide(request, parts):
        digest = source_digest("teams_mention", *parts, route_fingerprint(route, PROMPT))
        gate = ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)

        def call():
            try:
                system = PROMPT.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise TeamsError("teams_prompt_unavailable") from None
            key = key_for(load_key, "anthropic")
            try:
                return base.anthropic_json(route, system, request, TEAMS_SCHEMA, api_key=key, client=client,
                                           validator=lambda v: validate(v, TEAMS_SCHEMA) or v)
            finally:
                del key
        return gate.once("teams_mention", digest, call, provider="anthropic", hold=hold)
    decide.budget_ok = budget_gate(ops, hold, cap)
    return decide


def failure_text(first):
    return (f"{first}, mesajını okudum ama şu an işleyemedim (teknik bir sorun). Biraz sonra yeniden yazabilir ya da "
            "işi Basecamp'te bir to-do'da beni etiketleyerek bırakabilirsin.")


# ------------------------------------------------------------------ the desk
class TeamsDesk:
    """One Teams bot endpoint: handle(headers, body) -> (http status, json dict); answer(mention key) -> outcome.

    run_later(fn) runs the mention answer after the HTTP reply (teams_service: one background thread); None runs it
    inline (tests). todo_writer / comment_writer / basecamp_get / jobs are the existing Basecamp adapters.
    """
    def __init__(self, ops, settings, *, auth, model, sender, clock, jobs=None, todo_writer=None,
                 comment_writer=None, basecamp_get=None, brands=None, run_later=None, core=None):
        self.ops, self.s, self.auth, self.model, self.sender, self.clock = ops, settings, auth, model, sender, clock
        self.core = core
        self.jobs, self.todos, self.comments, self.get = jobs, todo_writer, comment_writer, basecamp_get
        self.brands = brands or {}
        self.run_later = run_later

    # ----- store helpers
    def _get(self, key):
        return self.ops._run(lambda tx, _: tx.get(key), allow_expired=True)

    def _set(self, key, **fields):
        def op(tx, now):
            row = tx.get(key)
            _need(row is not None, "teams_record_missing")
            row.update(updated_at=now, **fields)
            tx.put(key, row)
            return row
        return self.ops._run(op, allow_expired=True)

    def _fence(self, tx, now, generation):
        c = self.ops._control(tx)
        self.ops._open(c, generation, now)  # global stop ('dur') or a new generation -> no write
        _need(not self.ops._budget(tx)["overrun_hold"], "budget_overrun_hold")

    def _today(self):
        return self.clock().astimezone(ISTANBUL).date().isoformat()

    # ----- HTTP
    def handle(self, headers, body):
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        if not isinstance(body, (bytes, bytearray)) or len(body) > MAX_BODY_BYTES:
            return 413, {"status": "rejected", "reason": "body_too_large"}
        try:
            activity = json.loads(body)
        except ValueError:
            return 400, {"status": "rejected", "reason": "body_not_json"}
        if not isinstance(activity, dict):
            return 400, {"status": "rejected", "reason": "body_not_json"}
        service_url = activity.get("serviceUrl")
        try:
            _need(activity.get("channelId") == CHANNEL, "auth_channel_invalid")
            _need(service_url_ok(service_url, self.s), "auth_service_url_forbidden")
            self.auth.verify(headers.get("authorization"), service_url=service_url, channel_id=CHANNEL)
        except Exception as exc:
            code = _code(exc)
            return 401, {"status": "rejected", "reason": code if code.startswith("auth_") else "auth_failed"}
        if activity.get("type") != "message":
            return 200, {"status": "ignored", "reason": "not_a_message"}
        try:
            msg = parse_activity(activity, self.s)
        except TeamsError as exc:
            return 200, {"status": "ignored", "reason": exc.code}
        try:
            c = self.ops.control()
        except Exception:
            return 503, {"status": "held", "reason": "store_unavailable"}
        if c["mode"] != "review_open":
            return 200, {"status": "stopped", "reason": "human_control_stopped"}
        try:
            outcome = self._store(msg, c["generation"])
        except Exception as exc:
            code = _code(exc)
            if code == "control_stopped_or_stale":
                return 200, {"status": "stopped", "reason": code}
            return 503, {"status": "held", "reason": code}  # Bot Framework sends it again; dedupe holds
        if outcome != "queued":
            return 200, {"status": outcome}
        mkey = mention_key(msg["conversation_id"], msg["activity_id"])
        if self.run_later is None:
            return 200, {"status": "answered", "outcome": self.answer(mkey)}
        self.run_later(lambda: self.answer(mkey))
        return 200, {"status": "queued"}

    def _store(self, msg, generation):
        akey, ckey = activity_key(msg["base"], msg["activity_id"]), conversation_key(msg["base"])
        ikey = brand_index_key(msg["brand_key"]) if msg["brand_key"] else (AGENCY_INDEX if msg["scope"] == "agency"
                                                                          else None)
        mkey = mention_key(msg["base"], msg["activity_id"])

        def op(tx, now):
            c = self.ops._control(tx)
            self.ops._open(c, generation, now)
            if tx.get(akey) is not None:
                return "duplicate"
            conv = tx.get(ckey) or {"key": ckey, "kind": "teams_conversation", "conversation_id": msg["base"],
                                    "messages": [], "created_at": now}
            index = tx.get(ikey) if ikey else None
            conv.update(type=msg["type"], scope=msg["scope"], brand_key=msg["brand_key"],
                        name=msg["name"] or conv.get("name"), updated_at=now)
            conv["messages"].append({"id": msg["activity_id"], "at": msg["at"], "received_at": now,
                                     "author": msg["author"], "text": msg["text"][:MAX_STORED_TEXT],
                                     "files": [{"name": f["name"], "type": f["type"]} for f in msg["files"]],
                                     "to_hamle": msg["mentioned"]})
            tx.put(ckey, _fit(conv, self.s["keep_messages"]))
            tx.put(akey, {"key": akey, "kind": "teams_activity", "conversation_key": ckey, "received_at": now,
                          "mentioned": msg["mentioned"]})
            if ikey:
                keys = list((index or {}).get("keys") or [])
                tx.put(ikey, {"kind": "teams_index", "keys": list(dict.fromkeys(keys + [ckey]))[-MAX_INDEX:]})
            if msg["mentioned"]:
                tx.put(mkey, {"key": mkey, "kind": "teams_mention", "state": "queued", "generation": generation,
                              "conversation_key": ckey, "message": copy.deepcopy(msg), "created_at": now})
                return "queued"
            return "stored"
        return self.ops._run(op)

    # ----- one mention
    def answer(self, mkey):
        token = uuid.uuid4().hex

        def claim(tx, now):
            row = tx.get(mkey)
            if row is None or not (row.get("state") == "queued"
                                   or (row.get("state") == "claimed" and now >= row.get("lease_until", 0))):
                return None
            row.update(state="claimed", token=token, lease_until=now + LEASE_SECONDS, claimed_at=now)
            tx.put(mkey, row)
            return row
        try:
            row = self.ops._run(claim, allow_expired=True)
        except Exception as exc:
            return "error:" + _code(exc)
        if row is None:
            return "already_recorded"
        try:
            return self._answer(mkey, row, token)
        except Exception as exc:
            try:
                self._set(mkey, state="held", reason=_code(exc), lease_until=0)
            except Exception:
                pass
            return "error:" + _code(exc)

    def _answer(self, mkey, row, token):
        msg, generation, ckey = row["message"], row["generation"], row["conversation_key"]
        first = _first(msg["author"])
        c = self.ops.control()
        if c["mode"] != "review_open" or c["generation"] != generation:
            self._set(mkey, state="stopped", reason="human_control_stopped", lease_until=0)
            return "stopped"
        local = self._today()
        conv = self._get(ckey) or {}
        replies = int(conv.get("replies") or 0) if conv.get("day") == local else 0
        if replies >= self.s["max_replies"]:
            if conv.get("day") == local and conv.get("limit_noted"):
                self._set(mkey, state="held", reason="teams_conversation_daily_limit", lease_until=0)
                return "held"
            return self._reply(mkey, token, msg, generation, f"{first}, bu sohbette bugünkü cevap sınırıma ulaştım. "
                               "Acil bir şeyse Basecamp'te ilgili to-do'da beni etiketleyebilirsin.", action="limit",
                               limit_note=True)
        if not getattr(self.model, "budget_ok", lambda: True)():
            return self._reply(mkey, token, msg, generation, f"{first}, şu an model bütçe sınırındayım; bu mesajı "
                               "cevaplayamıyorum. Ahmet'e haber verebilir misin?", action="budget", count=False)
        request, task_info = self._request(msg, conv)

        def count(tx, now):
            self._fence(tx, now, generation)
            day = tx.get(day_key(local)) or {"kind": "teams_day", "day": local, "model_calls": 0}
            _need(int(day.get("model_calls") or 0) < self.s["max_model_calls"], "teams_daily_model_limit")
            day["model_calls"] = int(day.get("model_calls") or 0) + 1
            tx.put(day_key(local), day)
        try:
            self.ops._run(count)
        except Exception as exc:
            code = _code(exc)
            if code == "teams_daily_model_limit":
                return self._reply(mkey, token, msg, generation, f"{first}, bugün Teams için ayrılan model sınırına "
                                   "ulaştım; yarın yeniden yazabilir ya da Basecamp'te beni etiketleyebilirsin.",
                                   action="limit", count=False)
            self._set(mkey, state="stopped" if code == "control_stopped_or_stale" else "held", reason=code,
                      lease_until=0)
            return "stopped" if code == "control_stopped_or_stale" else "held"
        try:
            result, status = self.model(request, [mkey, scope_digest(request)]), "ok"
        except Exception as exc:
            result, status = None, "failed:" + _code(exc)
        if not isinstance(result, dict):
            return self._reply(mkey, token, msg, generation, failure_text(first), action="failed", model_status=status)
        action, text = result["action"], result["reply"]
        if action == "task":
            action, text = self._task(mkey, token, msg, generation, result, task_info)
        return self._reply(mkey, token, msg, generation, text, action=action, model_status=status,
                           understood=result.get("understood"))

    def _request(self, msg, conv):
        local = self.clock().astimezone(ISTANBUL)
        brand_key = msg["brand_key"]
        info = self.brands.get(brand_key) or {}
        brand = None
        if brand_key:
            brand = {"key": brand_key, "name": self.s["brands"][brand_key]["name"], "sector": info.get("sector"),
                     "audience": info.get("audience"), "main_platform": info.get("main_platform")}
        link = self._todo_link(msg["text"])
        why = None
        if self.todos is None and self.comments is None:
            why = "Basecamp yazımı bu kurulumda kapalı"
        elif brand_key and link is None and not self._can_create(brand_key):
            why = "bu markada Hamle'nin iş açması henüz açık değil (iş takibi açık markalar: " + \
                  ", ".join(sorted((self.jobs or {}).get("brands") or {})) + ")"
        context = [{"author": m.get("author"), "at": m.get("at"), "text": str(m.get("text") or "")[:MAX_CONTEXT_TEXT],
                    "files": [f.get("name") for f in m.get("files") or []]}
                   for m in (conv.get("messages") or []) if m.get("id") != msg["activity_id"]][-CONTEXT_FOR_MODEL:]
        task = {"possible": why is None, "why": why, "lists": list(LISTS),
                "todo_link": link["url"] if link else None}
        request = {"today": local.date().isoformat(), "now": local.strftime("%d.%m.%Y %H:%M"), "agency": "Gamechangers",
                   "message": {"author": msg["author"], "at": msg["at"], "text": msg["text"],
                               "files": [{"name": f["name"], "type": f["type"]} for f in msg["files"]]},
                   "conversation": {"type": msg["type"], "name": msg["name"], "brand": brand["name"] if brand else None},
                   "context": context, "brand": brand,
                   "brands": [{"key": k, "name": b["name"], "task_possible": self._can_create(k)}
                              for k, b in sorted(self.s["brands"].items())],
                   "task": task}
        if self.core is not None:
            pid = self.s["brands"][brand_key]["project_id"] if brand_key else None
            try:
                memory_key = brand_key or named_brand(msg["text"], self.s["brands"])
                request["core"] = self.core(local.date(), pid, memory_key)
            except Exception as exc:
                request["core"] = {"unavailable": _code(exc)}
            try:
                request["teams_elsewhere"] = [] if not ELSEWHERE_FOR_MODEL else [
                    {"conversation": m.get("conversation"), "author": m.get("author"), "at": m.get("at"),
                     "text": str(m.get("text") or "")[:300]}
                    for m in teams_context(self.ops, None) + (teams_context(self.ops, brand_key) if brand_key else [])
                    if m.get("conversation_key") != conv.get("key")][-ELSEWHERE_FOR_MODEL:]
            except Exception:
                request["teams_elsewhere"] = []

        def size():
            return len(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        while size() > MAX_REQUEST_BYTES and request.get("teams_elsewhere"):
            request["teams_elsewhere"].pop(0)
        while size() > MAX_REQUEST_BYTES and len(request["context"]) > 5:
            request["context"].pop(0)
        memory = ((request.get("core") or {}).get("brand_memory") or {}).get("events")
        while size() > MAX_REQUEST_BYTES and memory and len(memory) > CORE_MEMORY_MIN_EVENTS:
            memory.pop(0)  # oldest first
        core_items = ((request.get("core") or {}).get("basecamp") or {}).get("items")
        while size() > MAX_REQUEST_BYTES and core_items:
            core_items.pop()
        while size() > MAX_REQUEST_BYTES and memory:
            memory.pop(0)
        while size() > MAX_REQUEST_BYTES and request["context"]:
            request["context"].pop(0)
        return request, {"link": link}

    def _can_create(self, brand_key):
        return (self.todos is not None and self.jobs is not None and brand_key in (self.jobs.get("brands") or {}))

    def _todo_link(self, text):
        from v11_basecamp_mentions import LINK
        for m in LINK.finditer(text or ""):
            if int(m.group(1)) == self.s["account_id"]:
                return {"project_id": int(m.group(2)), "todo_id": int(m.group(3)), "url": m.group(0).split("#")[0]}
        return None

    # ----- task: Basecamp is the source of truth
    def _task(self, mkey, token, msg, generation, result, info):
        first = _first(msg["author"])
        t = result.get("task") or {}
        brand_key = msg["brand_key"] or (t.get("brand_key") if t.get("brand_key") in self.s["brands"] else None)
        if brand_key is None:
            return "ask", f"{first}, bu işi hangi markanın Basecamp projesine yazayım?"
        brand = self.s["brands"][brand_key]
        link = info["link"]
        if link is not None:
            if link["project_id"] != brand["project_id"] or self.comments is None or self.get is None:
                return "cannot", (f"{first}, bu bağlantı {brand['name']} projesinde değil; to-do'ya ancak sohbetin "
                                  "markasının projesinde yazabilirim.")
            return self._comment(mkey, token, msg, generation, result, brand, link)
        if not self._can_create(brand_key):
            return "cannot", (f"{first}, {brand['name']} için Basecamp'te iş açmam henüz açık değil; to-do'yu ekipten "
                              "biri açıp beni orada etiketlerse takip ederim.")
        jb = self.jobs["brands"][brand_key]
        list_name = t.get("list") if t.get("list") in LISTS else "project"
        due = t.get("due_on")
        try:
            due = date.fromisoformat(due).isoformat() if isinstance(due, str) and DATE.fullmatch(due) else None
        except ValueError:
            due = None
        where = msg["name"] or {"personal": "Hamle ile birebir", "groupChat": "grup sohbeti"}.get(msg["type"], "Teams")
        description = "\n".join([str(t.get("brief") or result["reply"]).strip(), "",
                                 "Teslim: " + (due if due else "tarih verilmedi"),
                                 f"Talep: {msg['author']} · Microsoft Teams ({where}) · "
                                 + self.clock().astimezone(ISTANBUL).strftime("%d.%m.%Y %H:%M"),
                                 "Bu to-do Teams'ten açıldı; işin kaydı burasıdır. Sorular ve dosyalar bu to-do'ya."])[:3900]
        payload = {"target": {"account_id": self.s["account_id"], "project_id": jb["project_id"],
                              "list_id": jb["lists"][list_name]},
                   "title": (jb["title_prefix"] + _line(t.get("title"), 80))[:150], "description": description,
                   "assignee_ids": [jb["assignees"][list_name]["id"]], "due_on": due,
                   "completion_subscriber_ids": list(jb.get("when_done") or [])}
        try:
            self._intent(mkey, token, generation, "create_todo", payload)
        except Exception as exc:
            return "held_write", _code(exc)
        try:
            remote = self.todos.write(copy.deepcopy(payload))["remote_id"]
        except Exception as exc:
            self._set(mkey, task_state="unknown", task_reason=_code(exc))
            return "cannot", (f"{first}, Basecamp'te to-do'yu açarken bir sorun oldu ve açılıp açılmadığını teyit "
                              f"edemedim; tekrar denemiyorum. {brand['name']} projesine bakar mısın?")
        try:
            back = self.todos.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == scope_digest(payload), "teams_todo_readback_mismatch")
            url = back.get("app_url") or (f"https://3.basecamp.com/{self.s['account_id']}/buckets/"
                                          f"{jb['project_id']}/todos/{remote}")
        except Exception as exc:
            self._set(mkey, task_state="unknown", task_reason=_code(exc), task_remote_id=remote)
            return "task", (result["reply"] + "\n\nTo-do'yu açtım ama son kontrolü teyit edemedim: "
                            f"https://3.basecamp.com/{self.s['account_id']}/buckets/{jb['project_id']}/todos/{remote}")
        self._set(mkey, task_state="done", task_remote_id=remote, task_url=url)
        who = jb["assignees"][list_name]["name"]
        return "task", f"{result['reply']}\n\nBasecamp'e yazdım ({_first(who)}'e atandı): {url}"

    def _comment(self, mkey, token, msg, generation, result, brand, link):
        first = _first(msg["author"])
        status, todo, _ = self.get(f"https://3.basecampapi.com/{self.s['account_id']}/buckets/{link['project_id']}"
                                   f"/todos/{link['todo_id']}.json", {})
        parent = (todo or {}).get("parent") or {}
        if not (status == 200 and isinstance(todo, dict) and todo.get("type") == "Todo"
                and todo.get("id") == link["todo_id"] and (todo.get("bucket") or {}).get("id") == brand["project_id"]
                and todo.get("visible_to_clients") is False):
            return "cannot", f"{first}, bu to-do'yu okuyamadım ya da müşteriye açık; oraya yazmıyorum."
        if todo.get("completed") is True:
            return "cannot", f"{first}, bu to-do tamamlanmış; yeni bir iş için yeni to-do gerekir."
        t = result.get("task") or {}
        body = "\n".join([str(t.get("brief") or result["reply"]).strip(), "",
                          f"Teams'ten {msg['author']} yazdı (" + (msg["name"] or "Microsoft Teams") + ")."])
        list_id = parent.get("id") if parent.get("id") in brand["lists"].values() else None
        payload = {"target": {"account_id": self.s["account_id"], "project_id": brand["project_id"],
                              "list_id": list_id, "task_id": link["todo_id"]},
                   "body": body[:6000], "mention_ids": [], "internal_only": True}
        try:
            self._intent(mkey, token, generation, "comment_todo", payload)
        except Exception as exc:
            return "held_write", _code(exc)
        try:
            remote = self.comments.write(copy.deepcopy(payload))["remote_id"]
            back = self.comments.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == scope_digest(payload), "teams_comment_readback_mismatch")
        except Exception as exc:
            self._set(mkey, task_state="unknown", task_reason=_code(exc))
            return "cannot", (f"{first}, to-do'ya yazarken bir sorun oldu ve teyit edemedim; tekrar denemiyorum. "
                              f"Bakar mısın: {link['url']}")
        self._set(mkey, task_state="done", task_remote_id=remote, task_url=link["url"])
        return "task", f"{result['reply']}\n\nTo-do'ya not düştüm: {link['url']}"

    def _intent(self, mkey, token, generation, kind, payload):
        """Durable intent before the Basecamp POST; 'dur' / budget hold / a lost claim stop it."""
        def op(tx, now):
            self._fence(tx, now, generation)
            row = tx.get(mkey)
            _need(row is not None and row.get("token") == token and row.get("state") == "claimed", "teams_claim_lost")
            _need(row.get("task_state") is None, "teams_task_already_started")
            row.update(task_state="writing", task_kind=kind, task_payload_sha256=scope_digest(payload),
                       task_started_at=now)
            tx.put(mkey, row)
        self.ops._run(op)

    # ----- the Teams reply
    def _reply(self, mkey, token, msg, generation, text, *, action, model_status=None, understood=None,
               count=True, limit_note=False):
        if action == "held_write":  # 'dur' or budget hold right before the Basecamp write: nothing was written
            code = text
            self._set(mkey, state="stopped" if code == "control_stopped_or_stale" else "held", reason=code,
                      lease_until=0)
            return "stopped" if code == "control_stopped_or_stale" else "held"
        local = self._today()
        ckey = conversation_key(msg["base"])

        def begin(tx, now):
            self._fence(tx, now, generation)
            row, conv = tx.get(mkey), tx.get(ckey) or {}
            _need(row is not None and row.get("token") == token and row.get("state") == "claimed", "teams_claim_lost")
            if count or limit_note:
                n = int(conv.get("replies") or 0) if conv.get("day") == local else 0
                noted = bool(conv.get("limit_noted")) if conv.get("day") == local else False
                conv.update(day=local, replies=n + (1 if count and not limit_note else 0),
                            limit_noted=noted or limit_note)
                tx.put(ckey, conv)
            row.update(state="writing", action=action, model_status=model_status, understood=understood,
                       reply_sha256=_sha(text), writing_at=now)
            tx.put(mkey, row)
        try:
            self.ops._run(begin)
        except Exception as exc:
            code = _code(exc)
            if code == "teams_claim_lost":
                return "already_recorded"
            self._set(mkey, state="stopped" if code == "control_stopped_or_stale" else "held", reason=code,
                      lease_until=0)
            return "stopped" if code == "control_stopped_or_stale" else "held"
        try:
            receipt = self.sender.send(msg["service_url"], msg["conversation_id"], msg["reply_to_id"], text[:6000])
            _need(isinstance(receipt, dict) and isinstance(receipt.get("id"), str), "teams_reply_receipt_unconfirmed")
        except Exception as exc:
            self._set(mkey, state="unknown", reason=_code(exc))
            return "unknown"
        self._set(mkey, state="done", reply_id=receipt["id"])

        def remember(tx, now):  # Hamle's own answer is context too
            conv = tx.get(ckey)
            if conv is None:
                return
            conv["messages"].append({"id": receipt["id"], "at": None, "received_at": now, "author": "Hamle",
                                     "text": text[:MAX_STORED_TEXT], "files": [], "to_hamle": False})
            tx.put(ckey, _fit(conv, self.s["keep_messages"]))
        try:
            self.ops._run(remember, allow_expired=True)
        except Exception:
            pass
        return action
