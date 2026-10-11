"""IAP-authenticated WSGI review/control surface; no model or mail credentials."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

IAP_ISSUER = "https://cloud.google.com/iap"
IAP_CERTS_URL = "https://www.gstatic.com/iap/verify/public_key"
ALLOWED_EMAIL = "ahmet@gamechangers.com.tr"
COOKIE = "__Host-hamle-cloud-session"
SESSION_SECONDS = 1800
MAX_SESSIONS = 64
MAX_BODY_BYTES = 4096
TEMPLATE = Path(__file__).with_name("cloud_ui.html")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SAFE_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,180}\Z")
WORK_ID = re.compile(r"work-[0-9a-f]{64}\Z")


class ControlError(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = item
    return value


def _loads(raw):
    return json.loads(raw, object_pairs_hook=_unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


@dataclass(frozen=True)
class Config:
    audience: str
    public_origin: str
    allowed_subject: str | None = None

    @classmethod
    def from_env(cls, env):
        audience = env.get("IAP_AUDIENCE", "")
        origin = env.get("PUBLIC_ORIGIN", "")
        subject = env.get("IAP_ALLOWED_SUBJECT") or None
        if not re.fullmatch(r"/projects/[0-9]+/locations/[a-z0-9-]+/services/[a-z0-9-]+", audience):
            raise ValueError("IAP_AUDIENCE_required_for_direct_Cloud_Run_IAP")
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path or parsed.query or parsed.fragment or parsed.port
                or parsed.netloc != parsed.hostname
                or not re.fullmatch(r"[a-z0-9.-]+", parsed.hostname)):
            raise ValueError("PUBLIC_ORIGIN_must_be_exact_HTTPS_origin")
        if subject is not None and not valid_subject(subject):
            raise ValueError("invalid_IAP_ALLOWED_SUBJECT")
        return cls(audience, origin, subject)


def valid_subject(value):
    return isinstance(value, str) and bool(SAFE_ID.fullmatch(value))


def google_iap_verifier(token, audience):
    """Official Google verifier; only the fixed IAP public-key endpoint is used."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token
    transport = Request()

    def bounded_request(url, method="GET", **kwargs):
        if url != IAP_CERTS_URL or method != "GET":
            raise ValueError("unexpected_certificate_endpoint")
        kwargs["timeout"] = 10
        return transport(url, method=method, **kwargs)

    return id_token.verify_token(token, bounded_request, audience=audience,
                                certs_url=IAP_CERTS_URL, clock_skew_in_seconds=30)


def authenticate(environ, config, verifier, now):
    token = environ.get("HTTP_X_GOOG_IAP_JWT_ASSERTION", "")
    if not isinstance(token, str) or not 10 < len(token) <= 16384 or not token.isascii() or "," in token:
        raise ControlError(401, "iap_identity_required")
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError()
        header = _loads(base64.urlsafe_b64decode(parts[0] + "=" * (-len(parts[0]) % 4)))
        if (not isinstance(header, dict) or header.get("alg") != "ES256"
                or not isinstance(header.get("kid"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", header["kid"])):
            raise ValueError()
        claims = verifier(token, config.audience)
        if (claims.get("iss") != IAP_ISSUER or claims.get("aud") != config.audience
                or claims.get("email") != ALLOWED_EMAIL or not valid_subject(claims.get("sub"))
                or (config.allowed_subject is not None and claims["sub"] != config.allowed_subject)):
            raise ValueError()
        issued, expiry = claims.get("iat"), claims.get("exp")
        if (type(issued) not in (int, float) or type(expiry) not in (int, float)
                or not math.isfinite(issued) or not math.isfinite(expiry)
                or issued > now + 30 or expiry < now - 30 or expiry <= issued or expiry - issued > 660):
            raise ValueError()
        return claims["sub"]
    except Exception:
        raise ControlError(403, "iap_identity_not_authorized") from None


class Sessions:
    def __init__(self):
        self._key = secrets.token_bytes(32)
        self._values = {}
        self._lock = threading.Lock()

    def _signature(self, session_id, subject):
        return hmac.new(self._key, (session_id + "\n" + subject).encode(), hashlib.sha256).hexdigest()

    def new(self, subject, now):
        with self._lock:
            self._values = {k: v for k, v in self._values.items() if v["expires"] > now}
            if len(self._values) >= MAX_SESSIONS:
                oldest = min(self._values, key=lambda k: self._values[k]["expires"])
                del self._values[oldest]
            sid = secrets.token_urlsafe(32)
            value = {"subject": subject, "expires": now + SESSION_SECONDS,
                     "csrf": secrets.token_urlsafe(32), "seen": {}, "seen_works": {}}
            self._values[sid] = value
            return sid + "." + self._signature(sid, subject), value["csrf"]

    def check(self, cookie_header, subject, now, csrf=None):
        try:
            cookie = SimpleCookie()
            cookie.load(cookie_header)
            token = cookie[COOKIE].value
            sid, signature = token.split(".")
            if (not re.fullmatch(r"[A-Za-z0-9_-]{43}", sid)
                    or not HEX64.fullmatch(signature)
                    or not hmac.compare_digest(signature, self._signature(sid, subject))):
                raise ValueError()
            with self._lock:
                value = self._values.get(sid)
                if value is None or value["subject"] != subject or value["expires"] <= now:
                    raise ValueError()
                if csrf is not None and (not isinstance(csrf, str) or not csrf.isascii()
                                         or not hmac.compare_digest(csrf, value["csrf"])):
                    raise ValueError()
                return sid
        except (CookieError, KeyError, TypeError, ValueError):
            raise ControlError(403, "reload_authenticated_panel") from None

    def seen(self, sid, proposals, generation):
        with self._lock:
            self._values[sid]["seen"] = {p["proposal_id"]: (p["proposal_hash"], generation, p["available_approval_purpose"]) for p in proposals}

    def require_seen(self, sid, proposal_id, proposal_hash, generation, purpose):
        with self._lock:
            if self._values[sid]["seen"].get(proposal_id) != (proposal_hash, generation, purpose):
                raise ControlError(409, "proposal_not_currently_displayed")

    def seen_works(self, sid, rows, now):
        with self._lock:
            seen = self._values[sid]["seen_works"]
            seen.update({r["work_id"]: (r["snapshot_sha256"], r["work_generation"],
                                       r["global_generation"], now) for r in rows})
            while len(seen) > 50:
                del seen[min(seen, key=lambda k: seen[k][3])]

    def require_work_seen(self, sid, data, now):
        with self._lock:
            seen = self._values[sid]["seen_works"].get(data["work_id"])
            if (seen is None or seen[:3] != (data["snapshot_sha256"], data["work_generation"],
                                             data["global_generation"])
                    or not 0 <= now - seen[3] <= 300):
                raise ControlError(409, "work_not_currently_displayed")


class ControlApplication:
    def __init__(self, config, store, verifier=google_iap_verifier, clock=time.time):
        self.config, self.store, self.verifier, self.clock = config, store, verifier, clock
        self.sessions = Sessions()

    def __call__(self, environ, start_response):
        nonce = secrets.token_urlsafe(24)
        try:
            result, content_type, extra = self._handle(environ, nonce)
            status = 200
        except ControlError as exc:
            status, result, content_type, extra = exc.status, _json_bytes({"error": exc.code}), "application/json; charset=utf-8", []
        except Exception:
            status, result, content_type, extra = 503, _json_bytes({"error": "control_unavailable"}), "application/json; charset=utf-8", []
        phrases = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                   405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large", 415: "Unsupported Media Type", 503: "Service Unavailable"}
        headers = [("Content-Type", content_type), ("Content-Length", str(len(result))),
                   ("Cache-Control", "no-store, max-age=0"), ("X-Content-Type-Options", "nosniff"),
                   ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                   ("Cross-Origin-Resource-Policy", "same-origin"),
                   ("Strict-Transport-Security", "max-age=31536000"),
                   ("Content-Security-Policy", "default-src 'none'; " + f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")]
        start_response(f"{status} {phrases[status]}", headers + extra)
        return [result]

    def _handle(self, env, nonce):
        if env.get("HTTP_HOST") != urlsplit(self.config.public_origin).netloc:
            raise ControlError(403, "wrong_host")
        subject = authenticate(env, self.config, self.verifier, self.clock())
        method, path = env.get("REQUEST_METHOD"), env.get("PATH_INFO")
        if env.get("QUERY_STRING"):
            raise ControlError(400, "query_not_supported")
        if method == "GET" and path == "/":
            token, csrf = self.sessions.new(subject, self.clock())
            page = TEMPLATE.read_text(encoding="utf-8").replace("__CSP_NONCE__", nonce).replace("__CSRF_TOKEN__", csrf)
            cookie = f"{COOKIE}={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={SESSION_SECONDS}"
            return page.encode(), "text/html; charset=utf-8", [("Set-Cookie", cookie)]
        if method not in ("GET", "POST"):
            raise ControlError(405, "method_not_allowed")
        work_read = path == "/api/work-authority" or (isinstance(path, str)
                    and re.fullmatch(r"/api/work-authority/work-[0-9a-f]{64}", path))
        if method == "GET" and path != "/api/state" and not work_read:
            raise ControlError(405 if path == "/api/control" else 404, "route_not_available")
        if method == "POST" and path not in ("/api/control", "/api/work-control"):
            raise ControlError(404, "route_not_available")
        if env.get("HTTP_SEC_FETCH_SITE") == "cross-site":
            raise ControlError(403, "wrong_origin")
        csrf = None
        if method == "POST":
            if env.get("HTTP_ORIGIN") != self.config.public_origin:
                raise ControlError(403, "wrong_origin")
            csrf = env.get("HTTP_X_CSRF_TOKEN", "")
        sid = self.sessions.check(env.get("HTTP_COOKIE", ""), subject, self.clock(), csrf)
        if work_read or path == "/api/work-control":
            from v11_work_authority_store import WorkAuthorityStore
            authority = WorkAuthorityStore(self.store)
            if method == "POST":
                result = self._mutate_work(env, sid, subject, authority)
            else:
                if path == "/api/work-authority":
                    page = authority.queue().page(kind="work", limit=25)
                    rows = [public_work_authority(authority.observe_work(w["work_id"])) for w in page["items"]]
                    result = {"works": rows, "more_available": page["next_cursor"] is not None}
                else:
                    row = public_work_authority(authority.observe_work(path.rsplit("/", 1)[-1]))
                    result = {"works": [row], "more_available": False}
            self.sessions.seen_works(sid, result["works"], self.clock())
            return _json_bytes(result), "application/json; charset=utf-8", []
        if method == "POST":
            self._mutate(env, sid, subject)
        state = self._state()
        self.sessions.seen(sid, state["proposals"], state["control"]["generation"])
        return _json_bytes(state), "application/json; charset=utf-8", []

    def _request_data(self, env):
        if env.get("CONTENT_TYPE") != "application/json" or env.get("HTTP_TRANSFER_ENCODING"):
            raise ControlError(415, "json_required")
        length = env.get("CONTENT_LENGTH", "")
        if not isinstance(length, str) or not re.fullmatch(r"[0-9]{1,4}", length) or not 0 < int(length) <= MAX_BODY_BYTES:
            raise ControlError(413, "request_size_limit")
        try:
            body = env["wsgi.input"].read(int(length))
            if len(body) != int(length):
                raise ValueError()
            data = _loads(body)
            if not isinstance(data, dict):
                raise ValueError()
        except (ValueError, UnicodeError, KeyError, TypeError):
            raise ControlError(400, "invalid_json") from None
        return data

    def _mutate_work(self, env, sid, subject, authority):
        from v11_work_authority_store import TrustedWorkStop
        data = self._request_data(env)
        if (set(data) != {"action", "work_id", "snapshot_sha256", "work_generation", "global_generation"}
                or data.get("action") not in ("takeover", "stop")
                or not isinstance(data.get("work_id"), str) or not WORK_ID.fullmatch(data["work_id"])
                or not isinstance(data.get("snapshot_sha256"), str) or not HEX64.fullmatch(data["snapshot_sha256"])
                or any(type(data.get(k)) is not int or data[k] < 0 for k in ("work_generation", "global_generation"))):
            raise ControlError(400, "invalid_work_control_binding")
        self.sessions.require_work_seen(sid, data, self.clock())
        try:
            fresh = public_work_authority(authority.observe_work(data["work_id"]))
            if any(fresh[k] != data[k] for k in ("snapshot_sha256", "work_generation", "global_generation")):
                raise ControlError(409, "work_changed_refresh_required")
            mode = "takeover" if data["action"] == "takeover" else "stopped"
            evidence = "control-screen:" + hashlib.sha256((sid + "\n" + data["work_id"] + "\n"
                        + data["snapshot_sha256"] + "\n" + mode).encode()).hexdigest()
            stop = TrustedWorkStop(work_id=data["work_id"], actor=ALLOWED_EMAIL, identity_subject=subject,
                evidence_ref=evidence, observed_at=datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(),
                channel="authenticated_control_screen", stop_intent="human_takeover" if mode == "takeover" else "explicit_stop")
            authority.stop_work(data["work_id"], stop, expected_work_generation=data["work_generation"], mode=mode)
            readback = public_work_authority(authority.observe_work(data["work_id"]))
            if (readback["mode"] != mode or readback["work_generation"] <= data["work_generation"]
                    or readback["last_stop_ref"] != evidence or readback["lease_present"]
                    or readback["grant_status"] not in ("revoked", "not_granted")):
                raise ControlError(409, "work_stop_readback_unverified")
            return {"works": [readback], "more_available": False, "stop_receipt_verified": True}
        except ControlError:
            raise
        except Exception:
            raise ControlError(409, "work_changed_refresh_required") from None

    def _mutate(self, env, sid, subject):
        data = self._request_data(env)
        action = data.get("action")
        keywords = {}
        if action in ("stop", "takeover", "resume_review_only") and set(data) == {"action"}:
            operation = getattr(self.store, action)
            arguments = (subject,)
        elif action == "approve" and set(data) == {"action", "proposal_id", "proposal_hash", "generation", "purpose"}:
            pid, fingerprint, generation = data["proposal_id"], data["proposal_hash"], data["generation"]
            purpose = data["purpose"]
            if (not isinstance(pid, str) or not SAFE_ID.fullmatch(pid)
                    or not isinstance(fingerprint, str) or not HEX64.fullmatch(fingerprint)
                    or type(generation) is not int or generation < 0
                    or purpose not in ("routine_execution", "internal_coordination_only")):
                raise ControlError(400, "invalid_approval_binding")
            self.sessions.require_seen(sid, pid, fingerprint, generation, purpose)
            operation, arguments = self.store.approve, (pid, fingerprint, generation, subject)
            keywords = {"purpose": purpose}
        else:
            raise ControlError(400, "action_not_allowed")
        try:
            operation(*arguments, **keywords)
        except Exception:
            raise ControlError(409, "control_changed_refresh_required") from None

    def _state(self):
        return public_state(self.store.public_state())


def public_work_authority(raw):
    """Small allowlisted view; source mail, recipients and grant body stay private."""
    def require(ok):
        if not ok:
            raise ValueError("invalid_work_authority_state")
    try:
        work, control, record = raw["work"], raw["global_control"], raw["record"]
        wid = work["work_id"]
        require(isinstance(wid, str) and WORK_ID.fullmatch(wid) and work["id"] == wid)
        require(raw["customer_dispatch_enabled_by_this_read"] is False)
        require(work["mode"] in ("active", "stopped", "takeover"))
        require(control["mode"] in ("review_open", "stopped", "takeover"))
        require(all(type(v) is int and v >= 0 for v in (work["work_generation"], work["version"], control["generation"])))
        require(all(type(work[k]) is int and work[k] > 0 for k in ("account_id", "project_id", "list_id")))
        require(work["account_id"] == 5898865 and (work["task_id"] is None or type(work["task_id"]) is int and work["task_id"] > 0))
        grant = record.get("grant") if record else None
        require(grant is None or grant["status"] in ("active", "revoked"))
        if record:
            require(record["work_id"] == wid and type(record["version"]) is int and record["version"] >= 0)
        synthetic = work.get("service_id") == "synthetic_authority_stop_probe"
        result = {"work_id": wid, "brand_id": work["brand_id"], "service_id": work["service_id"],
                  "account_id": work["account_id"], "project_id": work["project_id"], "task_id": work["task_id"],
                  "mode": work["mode"], "work_generation": work["work_generation"], "work_version": work["version"],
                  "global_generation": control["generation"], "global_mode": control["mode"],
                  "grant_status": grant["status"] if grant else "not_granted",
                  "grant_id": grant["grant_id"] if grant else None,
                  "grant_revision": grant["revision"] if grant else None,
                  "record_version": record["version"] if record else None,
                  "lease_present": work.get("lease") is not None,
                  "inflight_outcome_pending": work.get("inflight_action_id") is not None,
                  "last_stop_ref": (record.get("last_stop") or {}).get("evidence_ref") if record else None,
                  "synthetic": synthetic, "customer_dispatch_enabled": False}
        result["snapshot_sha256"] = hashlib.sha256(_json_bytes(result)).hexdigest()
        result["observed_at"] = raw["observed_at"]
        require(len(_json_bytes(result)) <= 5000)
        return result
    except Exception:
        raise ControlError(503, "invalid_work_authority_state") from None


def public_state(raw):
    """Only exact internal proposals and receipt evidence reach the browser."""
    def require(condition):
        if not condition:
            raise ControlError(503, "invalid_control_state")
    require(isinstance(raw, dict))
    control = raw.get("control", {})
    require(control.get("mode") in ("review_open", "stopped", "takeover"))
    require(type(control.get("generation")) is int and control["generation"] >= 0)
    require(raw.get("external_actions_enabled") is False and raw.get("unseen_cc_removal_resolved") is False)
    proposals, actions = raw.get("proposals"), raw.get("actions")
    require(isinstance(proposals, list) and len(proposals) <= 50 and isinstance(actions, list) and len(actions) <= 150)
    clean_proposals = []
    proposal_ids = set()
    for proposal in proposals:
        require(isinstance(proposal, dict))
        pid, digest, payload = proposal.get("proposal_id"), proposal.get("proposal_hash"), proposal.get("payload")
        require(isinstance(pid, str) and bool(SAFE_ID.fullmatch(pid)) and pid not in proposal_ids)
        proposal_ids.add(pid)
        require(isinstance(digest, str) and bool(HEX64.fullmatch(digest)))
        validate_payload(payload)
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        require(len(encoded) <= 60000 and hashlib.sha256(encoded).hexdigest() == digest)
        # Store dedup uses a composite source digest; Gmail snapshot is a
        # separate observation hash. Both are valid hashes, not equal fields.
        source_digest = proposal.get("source_fingerprint")
        require(isinstance(source_digest, str) and bool(HEX64.fullmatch(source_digest)))
        require(type(proposal.get("generation")) is int and proposal["generation"] >= 0)
        status = proposal.get("status")
        require(status in ("awaiting_human_review", "approved", "cancelled"))
        if control["mode"] != "review_open" or proposal["generation"] != control["generation"]:
            status = "cancelled"
        purpose = approval_purpose(payload)
        recorded = proposal.get("approval")
        recorded_purpose = recorded.get("purpose", "routine_execution") if isinstance(recorded, dict) else None
        require(recorded_purpose is None or recorded_purpose in ("routine_execution", "internal_coordination_only"))
        if recorded_purpose == "internal_coordination_only":
            require(purpose == recorded_purpose and recorded.get("escalation_status") == "open"
                    and recorded.get("decision_authorized") is False and recorded.get("production_authorized") is False)
        clean_proposals.append({"proposal_id": pid, "proposal_hash": digest, "source_fingerprint": source_digest, "generation": proposal["generation"],
                                "status": status, "payload": payload,
                                "available_approval_purpose": purpose,
                                "recorded_approval_purpose": recorded_purpose,
                                "human_escalation_open": payload["analysis"]["escalation"]["owner"] != "none"
                                or payload.get("work", {}).get("escalation_status") == "open"})
    clean_actions = []
    for action in actions:
        require(isinstance(action, dict) and action.get("proposal_id") in proposal_ids)
        kind, state, receipt = action.get("kind"), action.get("state"), action.get("provider_id")
        require(kind in ("gmail_reply", "basecamp_todo", "basecamp_comment"))
        require(state in ("leased", "dispatch_started", "accepted", "verified", "unknown", "cancelled"))
        if receipt is not None:
            pattern = r"[0-9a-f]{1,32}" if kind == "gmail_reply" else r"[0-9]{1,20}"
            require(isinstance(receipt, str) and bool(re.fullmatch(pattern, receipt)))
        require(state not in ("accepted", "verified") or receipt is not None)
        clean_actions.append({"proposal_id": action["proposal_id"], "kind": kind,
                              "state": state, "receipt_id": receipt})
    budget = raw.get("budget")
    clean_budget = None
    if budget is not None:
        fields = ("limit_microdollars", "accounted_spend_microdollars", "held_microdollars",
                  "prepaid_purchases_microdollars", "total_committed_microdollars", "remaining_microdollars")
        require(isinstance(budget, dict) and all(type(budget.get(k)) is int and 0 <= budget[k] <= 9_000_000_000_000_000 for k in fields))
        require(type(budget.get("overrun_hold")) is bool and budget.get("provider_hard_cap_installed") is False)
        clean_budget = {k: budget[k] for k in fields}
        clean_budget.update(overrun_hold=budget["overrun_hold"], provider_hard_cap_installed=False)
    works = raw.get("works", [])
    require(isinstance(works, list) and len(works) <= 50)
    clean_works, work_ids = [], set()
    for work in works:
        require(isinstance(work, dict))
        reference = {key: work.get(key) for key in WORK_FIELDS}
        validate_work_reference(reference)
        require(reference["work_id"] not in work_ids)
        work_ids.add(reference["work_id"])
        require(type(work.get("revision_count")) is int and 0 <= work["revision_count"] <= 1_000_000)
        observation = work.get("latest_observation")
        clean_observation = None
        if observation is not None:
            require(isinstance(observation, dict) and observation.get("identity_verified") is True)
            require(all(type(observation.get(k)) is int and observation[k] == reference[k]
                        for k in ("account_id", "project_id", "task_id")))
            require(observation.get("status") in ("active", "archived", "trashed"))
            require(type(observation.get("completed")) is bool)
            due_on = observation.get("due_on")
            require(due_on is None or valid_date(due_on))
            require(valid_timestamp(observation.get("observed_at")))
            clean_observation = {k: observation[k] for k in ("status", "completed", "due_on", "observed_at")}
        pending = work.get("followup_status")
        clean_pending = None
        if pending is not None:
            require(isinstance(pending, dict) and pending.get("state") in ("claimed", "proposed", "held"))
            require(type(pending.get("attempt")) is int and pending["attempt"] in (1, 2))
            reason = pending.get("reason")
            require(reason is None or isinstance(reason, str))
            clean_pending = {"state": pending["state"], "attempt": pending["attempt"],
                             "reason": "response_incomplete" if reason == "response_incomplete" else
                                       ("human_review_required" if reason else None)}
        clean_works.append({**reference, "revision_count": work["revision_count"],
                            "pending_escalation": reference["escalation_status"] == "open",
                            "followup_status": clean_pending,
                            "latest_observation": clean_observation})
    return {"control": {"mode": control["mode"], "generation": control["generation"]},
            "proposals": clean_proposals, "actions": clean_actions, "budget": clean_budget,
            "works": clean_works,
            "external_actions_enabled": False, "unseen_cc_removal_resolved": False}


def approval_purpose(payload):
    """A separate coordination choice never approves the underlying strategy."""
    analysis = payload["analysis"]
    if analysis["intent"] == "commercial":
        return None
    work = payload.get("work")
    if work:
        if work["escalation_status"] == "open":
            return "internal_coordination_only" if work["escalation_owner"] == "cansu_and_ahmet" else None
        # A newly detected escalation cannot silently disappear from a work
        # whose persistent ownership has not yet been updated.
        return "routine_execution" if analysis["intent"] == "routine_request" and analysis["escalation"]["owner"] == "none" else None
    if analysis["intent"] == "routine_request" and analysis["escalation"]["owner"] == "none":
        return "routine_execution"
    if (analysis["intent"] == "complaint_strategy" and analysis["escalation"]["owner"] == "cansu_and_ahmet"
            and payload["reply"]["subject"] == "Re: HAMLE-IC-IS-20261010-01"):
        return "internal_coordination_only"
    return None


WORK_FIELDS = ("work_id", "thread_id", "task_id", "account_id", "project_id", "version",
               "escalation_status", "escalation_owner")


def valid_timestamp(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        return False
    try:
        stamp = datetime.fromisoformat(value)
        return stamp.tzinfo is not None and stamp.utcoffset() is not None
    except ValueError:
        return False


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def validate_work_reference(work):
    if (not isinstance(work, dict) or set(work) != set(WORK_FIELDS)
            or not isinstance(work["work_id"], str) or not SAFE_ID.fullmatch(work["work_id"])
            or not isinstance(work["thread_id"], str) or not re.fullmatch(r"[0-9a-f]{1,32}", work["thread_id"])
            or type(work["task_id"]) is not int or not 0 < work["task_id"] <= 9_007_199_254_740_991
            or type(work["account_id"]) is not int or work["account_id"] != 5898865
            or type(work["project_id"]) is not int or work["project_id"] != 49184053
            or type(work["version"]) is not int or work["version"] != 1
            or (work["escalation_status"], work["escalation_owner"]) not in
               (("open", "cansu_and_ahmet"), ("not_required", "none"))):
        raise ControlError(503, "invalid_internal_proposal")


def validate_payload(payload):
    def require(condition):
        if not condition:
            raise ControlError(503, "invalid_internal_proposal")
    def exact(value, keys):
        require(isinstance(value, dict) and set(value) == set(keys.split()))
    def text(value, maximum, empty=False):
        require(isinstance(value, str) and (empty or bool(value.strip())) and len(value) <= maximum)
    require(isinstance(payload, dict))
    followup = "work" in payload
    exact(payload, "scope analysis reply work comment source" if followup else "scope analysis reply task source")
    require(payload["scope"] == "gamechangers_internal")
    from model import OUTPUT_SCHEMA, _validate
    try:
        _validate(payload["analysis"], OUTPUT_SCHEMA, "invalid_analysis")
    except Exception:
        raise ControlError(503, "invalid_internal_proposal") from None
    reply, source = payload["reply"], payload["source"]
    exact(reply, "from to cc subject body thread_id in_reply_to references")
    require(reply["from"] == "hamle@gamechangers.com.tr")
    from recipient_policy import validate_reply_recipients
    try:
        validate_reply_recipients(reply["to"], reply["cc"])
    except Exception:
        raise ControlError(503, "invalid_internal_proposal") from None
    text(reply["subject"], 200); text(reply["body"], 4000)
    require("\r" not in reply["subject"] and "\n" not in reply["subject"])
    require(isinstance(reply["thread_id"], str) and bool(re.fullmatch(r"[0-9a-f]{1,32}", reply["thread_id"])))
    require(isinstance(reply["in_reply_to"], str) and bool(re.fullmatch(r"<[^<>\r\n\s]{1,996}>", reply["in_reply_to"])))
    text(reply["references"], 4000, empty=True)
    require("\r" not in reply["references"] and "\n" not in reply["references"])
    if followup:
        work, comment = payload["work"], payload["comment"]
        validate_work_reference(work)
        exact(comment, "account_id project_id task_id todo_parent_id todo_parent_type body task_fingerprint")
        require(all(type(comment[k]) is int and comment[k] == work[k] for k in ("account_id", "project_id", "task_id")))
        require(type(comment["todo_parent_id"]) is int and 0 < comment["todo_parent_id"] <= 9_007_199_254_740_991)
        require(comment["todo_parent_type"] in ("Todoset", "Todolist"))
        require(isinstance(comment["task_fingerprint"], str) and bool(HEX64.fullmatch(comment["task_fingerprint"])))
        require(work["thread_id"] == reply["thread_id"])
        text(comment["body"], 4000)
    else:
        task = payload["task"]
        exact(task, "account_id project_id todo_parent_id todo_parent_type title description assignee_id due_at")
        require(type(task["account_id"]) is int and task["account_id"] == 5898865)
        require(type(task["project_id"]) is int and task["project_id"] == 49184053)
        require(type(task["assignee_id"]) is int and task["assignee_id"] == 53358100)
        require(type(task["todo_parent_id"]) is int and 0 < task["todo_parent_id"] <= 9_007_199_254_740_991 and task["due_at"] is None)
        require(task["todo_parent_type"] in ("Todoset", "Todolist"))
        text(task["title"], 200); text(task["description"], 4000)
    exact(source, "thread_id message_id snapshot_fingerprint captured_at")
    require(source["thread_id"] == reply["thread_id"])
    require(isinstance(source["message_id"], str) and bool(re.fullmatch(r"[0-9a-f]{1,32}", source["message_id"])))
    require(isinstance(source["snapshot_fingerprint"], str) and bool(HEX64.fullmatch(source["snapshot_fingerprint"])))
    require(valid_timestamp(source["captured_at"]))


def create_app(env=None, store=None, verifier=google_iap_verifier, clock=time.time):
    config = Config.from_env(os.environ if env is None else env)
    if store is None:
        from cloud_store import FirestoreBackend, OperationsStore
        settings = os.environ if env is None else env
        store = OperationsStore(FirestoreBackend(opt_in=settings.get("HAMLE_ALLOW_FIRESTORE_OPERATIONS")))
    return ControlApplication(config, store, verifier, clock)
