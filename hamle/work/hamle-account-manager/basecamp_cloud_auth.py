"""Independent Basecamp device OAuth bootstrap and fenced cloud refresh.

Sources: official basecamp-cli v0.13.0 internal/auth/{auth,keyring}.go and
basecamp-sdk go/v0.24.0 go/pkg/basecamp/oauth/{device,exchange}.go.
Never reads the existing Mac CLI/Keychain credential. No import-time network.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlencode, urlsplit, parse_qs

import cloud_providers as provider

ISSUER = "https://app.basecamp.com"
RESOURCE_ORIGIN = "https://3.basecampapi.com"
DEVICE_ENDPOINT = ISSUER + "/oauth/device_authorizations"
TOKEN_ENDPOINT = ISSUER + "/oauth/tokens"
CLIENT_ID = "basecamp-cli"
SCOPE = "full"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
IDENTITY = 28787068
ACCOUNT = 5898865
PERSON = 53358100
DIRECTORY = Path(__file__).resolve().parent.parent / "private-connections"
PENDING_PATH = DIRECTORY / "basecamp-cloud-device.json"
CREDENTIAL_PATH = DIRECTORY / "basecamp-cloud-oauth.json"
ALLOWED_RESOURCES = (None, RESOURCE_ORIGIN, "urn:bc:account:5898865")


class AuthError(Exception):
    retry_allowed = False
    def __init__(self, code):
        super().__init__(code); self.code = code


def _stamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value < 253402300799:
        raise AuthError("invalid_clock")
    return value


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _safe_token(value):
    try: return provider._token(value)
    except Exception: raise AuthError("oauth_token_invalid") from None


def _oauth_request(session, method, url, *, form=None, bearer=None):
    allowed = {("GET", RESOURCE_ORIGIN + "/.well-known/oauth-protected-resource"),
               ("GET", ISSUER + "/.well-known/oauth-authorization-server"),
               ("GET", RESOURCE_ORIGIN + "/authorization.json"),
               ("POST", DEVICE_ENDPOINT), ("POST", TOKEN_ENDPOINT)}
    if (method, url) not in allowed:
        raise AuthError("oauth_endpoint_not_allowed")
    headers = {"User-Agent": "Hamle Cloud Bootstrap (ahmet@gamechangers.com.tr)"}
    if bearer is not None: headers["Authorization"] = "Bearer " + _safe_token(bearer)
    body = None
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urlencode(form).encode("ascii")
    response = None
    try:
        response = session.request(method, url, headers=headers, data=body,
            timeout=(10, 30), allow_redirects=False, stream=True)
        if response.status_code not in (200, 400):
            raise AuthError("oauth_request_rejected")
        chunks, count = [], 0
        for chunk in response.iter_content(chunk_size=16384):
            count += len(chunk)
            if count > 131072: raise AuthError("oauth_response_too_large")
            chunks.append(chunk)
        result = provider._json(b"".join(chunks))
        if not isinstance(result, dict): raise AuthError("oauth_response_invalid")
        if response.status_code == 400:
            error = result.get("error")
            if url == TOKEN_ENDPOINT and error in ("authorization_pending", "slow_down", "access_denied", "expired_token", "invalid_grant"):
                return {"oauth_error": error}
            raise AuthError("oauth_request_rejected")
        if "error" in result: raise AuthError("oauth_response_invalid")
        return result
    except AuthError:
        raise
    except Exception:
        raise AuthError("oauth_outcome_unknown") from None
    finally:
        if response is not None:
            try: response.close()
            except Exception: pass


def discover(session):
    resource = _oauth_request(session, "GET", RESOURCE_ORIGIN + "/.well-known/oauth-protected-resource")
    if (resource.get("resource") != RESOURCE_ORIGIN
            or ISSUER not in resource.get("authorization_servers", [])):
        raise AuthError("oauth_resource_binding_mismatch")
    config = _oauth_request(session, "GET", ISSUER + "/.well-known/oauth-authorization-server")
    if (config.get("issuer") != ISSUER or config.get("token_endpoint") != TOKEN_ENDPOINT
            or config.get("device_authorization_endpoint") != DEVICE_ENDPOINT
            or DEVICE_GRANT not in config.get("grant_types_supported", [])
            or "refresh_token" not in config.get("grant_types_supported", [])
            or "none" not in config.get("token_endpoint_auth_methods_supported", [])
            or SCOPE not in config.get("scopes_supported", [])):
        raise AuthError("oauth_discovery_binding_mismatch")
    return {"issuer": ISSUER, "token_endpoint": TOKEN_ENDPOINT,
            "device_authorization_endpoint": DEVICE_ENDPOINT, "scope": SCOPE}


def _approval_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise AuthError("approval_url_invalid")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "app.basecamp.com"
            or parsed.path not in ("/oauth/device_verification", "/oauth/device_verification/new")
            or parsed.fragment or any(ord(c) < 33 for c in value)
            or set(parse_qs(parsed.query)) - {"user_code"}):
        raise AuthError("approval_url_invalid")
    return value


def _directory(path):
    directory = path.parent
    if directory.is_symlink(): raise AuthError("private_directory_required")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise AuthError("private_directory_required")


def _read(path):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 131072):
            raise AuthError("private_file_required")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            return provider._json(stream.read())
    except AuthError:
        raise
    except Exception:
        raise AuthError("private_file_unavailable") from None
    finally:
        if fd is not None: os.close(fd)


def _write(path, record, *, exclusive=False):
    _directory(path)
    temporary = path.with_name(path.name + ".writing")
    fd = None
    try:
        if path.is_symlink() or (exclusive and path.exists()):
            raise AuthError("private_file_already_exists")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(provider._encode(record)); stream.flush(); os.fsync(stream.fileno())
        # Callers hold a lock throughout the bootstrap. No overwrite of another
        # existing credential is allowed; pending state advances atomically.
        if exclusive:
            os.link(temporary, path)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
    except AuthError:
        raise
    except Exception:
        raise AuthError("private_file_write_failed") from None
    finally:
        if fd is not None: os.close(fd)


@contextmanager
def _lock(path):
    _directory(path)
    fd = os.open(path.with_name(path.name + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AuthError("private_lock_required")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError:
        raise AuthError("bootstrap_already_running") from None
    finally:
        os.close(fd)


def begin_device_login(*, pending_path=PENDING_PATH, credential_path=CREDENTIAL_PATH,
                       session=None, clock=time.time):
    own = session is None
    session = session or provider._session()
    try:
        with _lock(pending_path), provider._modules()[1]._quiet_call():
            if pending_path.exists() or credential_path.exists():
                raise AuthError("cloud_bootstrap_already_exists")
            discover(session)
            issued = _stamp(clock())
            _write(pending_path, {"state": "device_request_started", "issued_at": issued}, exclusive=True)
            response = _oauth_request(session, "POST", DEVICE_ENDPOINT,
                form={"client_id": CLIENT_ID, "scope": SCOPE, "login_hint": provider.HAMLE})
            device = _safe_token(response.get("device_code"))
            code = response.get("user_code")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9-]{4,64}", code):
                raise AuthError("device_response_invalid")
            expires, interval = response.get("expires_in"), response.get("interval", 5)
            if (type(expires) not in (int, float) or not float(expires).is_integer() or not 1 <= expires <= 3600
                    or type(interval) not in (int, float) or not float(interval).is_integer() or not 1 <= interval <= 60):
                raise AuthError("device_response_invalid")
            plain = _approval_url(response.get("verification_uri"))
            complete = response.get("verification_uri_complete")
            target = _approval_url(complete) if complete else plain + "?" + urlencode({"user_code": code})
            if parse_qs(urlsplit(target).query).get("user_code") != [code]:
                raise AuthError("approval_code_binding_mismatch")
            deadline = issued + expires
            if _stamp(clock()) >= deadline: raise AuthError("device_code_expired")
            _write(pending_path, {"state": "awaiting_approval", "device_code": device,
                "issued_at": issued, "expires_at": deadline, "interval": interval,
                "next_poll_at": issued + interval, "approval_url": target})
            return {"status": "awaiting_approval", "approval_url": target,
                    "expires_at": _iso(deadline), "requested_scope": SCOPE,
                    "expected_email": provider.HAMLE, "credential_source": "new_independent_cloud_login"}
    except AuthError:
        raise
    except Exception:
        raise AuthError("cloud_bootstrap_failed") from None
    finally:
        if own: session.close()


def _token_record(response, issued, *, previous=None):
    if (not isinstance(response, dict) or response.get("token_type", "").casefold() != "bearer"
            or response.get("scope", previous["scope"] if previous else None) != SCOPE):
        raise AuthError("token_scope_or_type_mismatch")
    expiry = response.get("expires_in")
    if type(expiry) not in (int, float) or not float(expiry).is_integer() or not 60 < expiry <= 31_536_000:
        raise AuthError("token_expiry_unverified")
    resource = response.get("resource", previous["resource"] if previous else None)
    if resource not in ALLOWED_RESOURCES or (previous and resource != previous["resource"]):
        raise AuthError("token_resource_mismatch")
    refresh = response.get("refresh_token", previous["refresh_token"] if previous else None)
    return {"version": 1, "kind": "basecamp_device_oauth", "issuer": ISSUER,
        "token_endpoint": TOKEN_ENDPOINT, "client_id": CLIENT_ID, "scope": SCOPE,
        "resource": resource, "access_token": _safe_token(response.get("access_token")),
        "refresh_token": _safe_token(refresh), "expires_at": issued + expiry,
        "identity_id": IDENTITY, "account_id": ACCOUNT, "person_id": PERSON,
        "verified_email": provider.HAMLE, "verified_at": _iso(issued), "refresh_state": "ready"}


def _verify_identity(record, session):
    authorization = _oauth_request(session, "GET", RESOURCE_ORIGIN + "/authorization.json",
                                   bearer=record["access_token"])
    identity = authorization.get("identity")
    accounts = authorization.get("accounts")
    if (not isinstance(identity, dict) or identity.get("id") != IDENTITY
            or identity.get("email_address") != provider.HAMLE or not isinstance(accounts, list)):
        raise AuthError("basecamp_oauth_identity_mismatch")
    matches = [a for a in accounts if isinstance(a, dict) and a.get("id") == ACCOUNT and a.get("product") == "bc3"]
    if len(matches) != 1 or matches[0].get("href") != f"{RESOURCE_ORIGIN}/{ACCOUNT}":
        raise AuthError("basecamp_oauth_account_mismatch")
    provider.verify_basecamp_internal(record["access_token"], session=session)


def validate_record(record):
    keys = {"version", "kind", "issuer", "token_endpoint", "client_id", "scope", "resource",
            "access_token", "refresh_token", "expires_at", "identity_id", "account_id", "person_id",
            "verified_email", "verified_at", "refresh_state"}
    try:
        if (not isinstance(record, dict) or set(record) != keys or record["version"] != 1
                or record["kind"] != "basecamp_device_oauth" or record["issuer"] != ISSUER
                or record["token_endpoint"] != TOKEN_ENDPOINT or record["client_id"] != CLIENT_ID
                or record["scope"] != SCOPE or record["resource"] not in ALLOWED_RESOURCES
                or record["identity_id"] != IDENTITY or record["account_id"] != ACCOUNT
                or record["person_id"] != PERSON or record["verified_email"] != provider.HAMLE
                or record["refresh_state"] not in ("ready", "started", "unverified")):
            raise ValueError()
        _safe_token(record["access_token"]); _safe_token(record["refresh_token"])
        _stamp(record["expires_at"])
        if datetime.fromisoformat(record["verified_at"]).tzinfo is None: raise ValueError()
        return record
    except Exception:
        raise AuthError("cloud_credential_record_invalid") from None


def poll_device_once(*, pending_path=PENDING_PATH, credential_path=CREDENTIAL_PATH,
                     session=None, clock=time.time):
    own = session is None
    session = session or provider._session()
    try:
        with _lock(pending_path), provider._modules()[1]._quiet_call():
            pending = _read(pending_path)
            if pending.get("state") == "complete":
                return {"status": "already_complete", "expected_email": provider.HAMLE}
            if pending.get("state") != "awaiting_approval":
                raise AuthError("device_outcome_requires_review")
            now = _stamp(clock())
            if now < pending["issued_at"] or now >= pending["expires_at"]:
                raise AuthError("device_code_expired")
            if now < pending["next_poll_at"]:
                return {"status": "waiting_interval", "wait_seconds": math.ceil(pending["next_poll_at"] - now)}
            _write(pending_path, {**pending, "state": "token_poll_started"})
            response = _oauth_request(session, "POST", TOKEN_ENDPOINT, form={
                "grant_type": DEVICE_GRANT, "device_code": _safe_token(pending["device_code"]), "client_id": CLIENT_ID})
            error = response.get("oauth_error")
            if error in ("authorization_pending", "slow_down"):
                interval = pending["interval"] + (5 if error == "slow_down" else 0)
                if interval > 120: raise AuthError("device_poll_limit_reached")
                _write(pending_path, {**pending, "state": "awaiting_approval", "interval": interval,
                                     "next_poll_at": _stamp(clock()) + interval})
                return {"status": "awaiting_approval", "wait_seconds": interval}
            if error: raise AuthError("device_approval_refused_or_expired")
            if _stamp(clock()) >= pending["expires_at"]: raise AuthError("device_code_expired")
            record = _token_record(response, now)
            # Preserve a received rotating grant before any verification GET.
            # This is a private candidate, never an accepted usable credential.
            _write(pending_path, {"state": "received_unverified", "candidate": {**record, "refresh_state": "unverified"},
                                  "received_at": _stamp(clock())})
            _verify_identity(record, session)
            validate_record(record)
            _write(credential_path, record, exclusive=True)
            _write(pending_path, {"state": "complete", "completed_at": _stamp(clock()),
                                  "credential_source": "new_independent_cloud_login"})
            return {"status": "cloud_credential_verified_locally", "identity_id": IDENTITY,
                    "person_id": PERSON, "account_id": ACCOUNT, "scope": SCOPE,
                    "expires_at": _iso(record["expires_at"]), "expected_email": provider.HAMLE,
                    "cloud_secret_uploaded": False}
    except AuthError:
        raise
    except Exception:
        raise AuthError("device_completion_failed") from None
    finally:
        if own: session.close()


def recover_candidate(*, pending_path=PENDING_PATH, credential_path=CREDENTIAL_PATH,
                      session=None, clock=time.time):
    """Finish only identity GETs on a durably received candidate; never poll/refresh."""
    own = session is None
    session = session or provider._session()
    try:
        with _lock(pending_path), provider._modules()[1]._quiet_call():
            pending = _read(pending_path)
            if pending.get("state") != "received_unverified":
                raise AuthError("received_candidate_required")
            record = validate_record(pending.get("candidate"))
            if record["refresh_state"] != "unverified" or _stamp(clock()) >= record["expires_at"] - 120:
                raise AuthError("candidate_expired_or_invalid")
            _verify_identity(record, session)
            ready = {**record, "refresh_state": "ready", "verified_at": _iso(_stamp(clock()))}
            validate_record(ready)
            _write(credential_path, ready, exclusive=True)
            _write(pending_path, {"state": "complete", "completed_at": _stamp(clock()),
                                  "credential_source": "new_independent_cloud_login"})
            return {"status": "cloud_credential_verified_locally", "identity_id": IDENTITY,
                    "person_id": PERSON, "account_id": ACCOUNT, "scope": SCOPE,
                    "expires_at": _iso(ready["expires_at"]), "expected_email": provider.HAMLE,
                    "cloud_secret_uploaded": False, "oauth_token_posts": 0}
    except AuthError:
        raise
    except Exception:
        raise AuthError("candidate_verification_failed") from None
    finally:
        if own: session.close()


def access_token(secrets, *, claim_refresh=None, session=None, clock=time.time):
    """Read the latest independent cloud record; rotate only under caller lease.

    claim_refresh(opaque_hash) must atomically create a permanent durable claim
    and return True only once for this refresh grant. Never release that claim
    after an ambiguous result. A persisted 'started' state also blocks retries.
    Never copy this record into another refresher. The claim hash is not logged.
    """
    own = session is None
    session = session or provider._session()
    try:
        with provider._modules()[1]._quiet_call():
            record = validate_record(provider._json(secrets.access(provider.BASECAMP_SECRET)))
            if record["refresh_state"] != "ready": raise AuthError("refresh_outcome_requires_review")
            now = _stamp(clock())
            if now < record["expires_at"] - 120:
                return record["access_token"]
            identifier = hashlib.sha256((ISSUER + "\0" + record["refresh_token"]).encode()).hexdigest()
            if not callable(claim_refresh) or claim_refresh(identifier) is not True:
                raise AuthError("durable_refresh_claim_required")
            secrets.add_basecamp_version({**record, "refresh_state": "started"})
            form = {"grant_type": "refresh_token", "refresh_token": record["refresh_token"], "client_id": CLIENT_ID}
            if record["resource"] is not None: form["resource"] = record["resource"]
            response = _oauth_request(session, "POST", TOKEN_ENDPOINT, form=form)
            if "oauth_error" in response: raise AuthError("refresh_refused_no_retry")
            updated = _token_record(response, now, previous=record)
            if updated["refresh_token"] == record["refresh_token"]:
                raise AuthError("rotated_refresh_token_missing")
            secrets.add_basecamp_version({**updated, "refresh_state": "unverified"})
            _verify_identity(updated, session)
            secrets.add_basecamp_version(updated)
            return updated["access_token"]
    except AuthError:
        raise
    except provider.ProviderError as exc:
        # Surface the fixed verification code (e.g. basecamp_membership_mismatch).
        raise AuthError(exc.code) from None
    except Exception:
        raise AuthError("cloud_basecamp_access_failed") from None
    finally:
        if own: session.close()


def main(argv=None):
    try:
        parser = argparse.ArgumentParser(description="Independent Hamle cloud Basecamp OAuth")
        choice = parser.add_mutually_exclusive_group(required=True)
        choice.add_argument("--begin-device-login", action="store_true")
        choice.add_argument("--poll-device-once", action="store_true")
        choice.add_argument("--recover-candidate", action="store_true")
        args = parser.parse_args(argv)
        result = begin_device_login() if args.begin_device_login else (recover_candidate() if args.recover_candidate else poll_device_once())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except AuthError as error:
        print(json.dumps({"status": "held", "reason": error.code, "automatic_retry_allowed": False}))
        return 2
    except Exception:
        print(json.dumps({"status": "held", "reason": "cloud_auth_failed", "automatic_retry_allowed": False}))
        return 2


if __name__ == "__main__": raise SystemExit(main())
