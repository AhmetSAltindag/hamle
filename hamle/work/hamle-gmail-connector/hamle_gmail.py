"""Hamle Gmail bootstrap with one fixed internal mail test; no worker."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from email.message import EmailMessage
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
from datetime import datetime, timezone

EXPECTED_EMAIL = "hamle@gamechangers.com.tr"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
)
SERVICE = "com.gamechangers.hamle.gmail.desktop.v1"
TOKEN_URI = "https://oauth2.googleapis.com/token"
AUTH_URIS = {
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
}
ID_PATTERN = re.compile(r"[a-fA-F0-9]{1,64}\Z")
TEST_ID_PATTERN = re.compile(r"GC-HAMLE-GMAIL-[0-9]{8}-[0-9]{2}\Z")
TEST_RECIPIENT = "ahmet@gamechangers.com.tr"
TEST_SUBJECT = "HAMLE — manuel iç Gmail bağlantı testi"
TEST_BODY = (
    "Merhaba Ahmet,\n\n"
    "Bu ileti, talep ettiğin Hamle Gmail bağlantısının gönderim kontrolü için "
    "manuel olarak başlatılan teknik iç testtir. Müşteri işi, görev veya "
    "onay talebi içermez; otomatik takip ya da tekrar gönderim başlatmaz.\n\n"
    "Hamle\n"
)
ATTEMPT_DIRECTORY = Path(__file__).resolve().parent / ".send-test-attempts"


class SafeError(Exception):
    """Only fixed application codes may reach the terminal."""


def exact_scopes(value):
    if isinstance(value, str):
        value = value.split()
    if not isinstance(value, (list, tuple, set)) or not all(
        isinstance(item, str) for item in value
    ):
        raise SafeError("granted_scopes_unverified")
    if set(value) != set(SCOPES):
        raise SafeError("scope_mismatch")
    return sorted(SCOPES)


def require_identity(profile):
    email = profile.get("emailAddress") if isinstance(profile, dict) else None
    if not isinstance(email, str) or email.casefold() != EXPECTED_EMAIL:
        raise SafeError("wrong_account")
    return EXPECTED_EMAIL


def validate_client(config):
    if not isinstance(config, dict) or set(config) != {"installed"}:
        raise SafeError("desktop_client_required")
    client = config["installed"]
    if not isinstance(client, dict):
        raise SafeError("invalid_client_configuration")
    if client.get("auth_uri") not in AUTH_URIS or client.get("token_uri") != TOKEN_URI:
        raise SafeError("unexpected_oauth_endpoint")
    client_id = client.get("client_id")
    if not isinstance(client_id, str) or not re.fullmatch(
        r"[A-Za-z0-9-]+\.apps\.googleusercontent\.com", client_id
    ):
        raise SafeError("invalid_client_configuration")
    if not isinstance(client.get("client_secret"), str) or not client["client_secret"]:
        raise SafeError("invalid_client_configuration")
    return config


def read_client(path):
    """Read an existing private Desktop JSON; never copy or rewrite it."""
    fd = None
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise SafeError("client_file_not_private")
        if info.st_mode & 0o077 or info.st_size > 131072:
            raise SafeError("client_file_not_private")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            return validate_client(json.load(stream))
    except SafeError:
        raise
    except Exception:
        raise SafeError("client_file_unreadable") from None
    finally:
        if fd is not None:
            os.close(fd)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_record(record):
    try:
        if record["version"] != 1 or record["verified_email"] != EXPECTED_EMAIL:
            raise SafeError("invalid_keychain_record")
        exact_scopes(record["granted_scopes"])
        info = record["credentials"]
        exact_scopes(info["scopes"])
        if (
            info["client_id"] != record["client_id"]
            or info["token_uri"] != TOKEN_URI
            or not isinstance(info["refresh_token"], str)
            or not info["refresh_token"]
            or not isinstance(info["client_secret"], str)
            or not info["client_secret"]
        ):
            raise SafeError("invalid_keychain_record")
        validate_client({"installed": {
            "client_id": info["client_id"], "client_secret": info["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": info["token_uri"],
        }})
        stamp = datetime.fromisoformat(record["verified_at"])
        if stamp.tzinfo is None:
            raise SafeError("invalid_keychain_record")
        expiry = info.get("expiry")
        if expiry is not None:
            datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        return record
    except SafeError:
        raise
    except Exception:
        raise SafeError("invalid_keychain_record") from None


class MacKeychainStore:
    """Explicit native backend. Never discover backends or fall back to files."""

    def __init__(self, backend=None):
        if backend is None:
            if sys.platform != "darwin" or os.environ.get("KEYCHAIN_PATH"):
                raise SafeError("macos_keychain_required")
            try:
                from keyring.backends.macOS import Keyring
                if Keyring.priority <= 0:
                    raise RuntimeError()
                backend = Keyring()
            except Exception:
                raise SafeError("keychain_unavailable") from None
        self.backend = backend

    def load(self):
        try:
            raw = self.backend.get_password(SERVICE, EXPECTED_EMAIL)
        except Exception:
            raise SafeError("keychain_read_failed") from None
        if raw is None:
            return None
        try:
            return validate_record(json.loads(raw))
        except SafeError:
            raise
        except Exception:
            raise SafeError("invalid_keychain_record") from None

    def save(self, record):
        validate_record(record)
        encoded = json.dumps(record, separators=(",", ":"))
        try:
            self.backend.set_password(SERVICE, EXPECTED_EMAIL, encoded)
            if self.backend.get_password(SERVICE, EXPECTED_EMAIL) != encoded:
                raise RuntimeError()
        except Exception:
            raise SafeError("keychain_write_failed") from None


def make_record(credentials, granted, profile):
    email = require_identity(profile)
    scopes = exact_scopes(granted)
    if not credentials.refresh_token:
        raise SafeError("refresh_token_missing")
    try:
        info = json.loads(credentials.to_json())
    except Exception:
        raise SafeError("credential_serialization_failed") from None
    return validate_record({
        "version": 1, "verified_email": email, "verified_at": utc_now(),
        "client_id": info.get("client_id"), "granted_scopes": scopes,
        "credentials": info,
    })


def build_gmail(credentials):
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=credentials,
                 cache_discovery=False, static_discovery=True)


class SingleAttemptHttp:
    """Google-auth transport without HTTP, redirect, or 401 request replays."""

    def __init__(self, credentials):
        from google.auth.transport.requests import AuthorizedSession
        from requests.adapters import HTTPAdapter
        def no_implicit_refresh(*args, **kwargs):
            # verified_gmail explicitly refreshes and saves before operations.
            # Expiry between profile and send must stop, not silently refresh.
            raise SafeError("send_transport_refresh_blocked")
        self.session = AuthorizedSession(
            credentials, max_refresh_attempts=0, auth_request=no_implicit_refresh
        )
        self.session.trust_env = False
        self.session.mount("https://", HTTPAdapter(max_retries=0))

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        from httplib2 import Response
        response = self.session.request(
            method, uri, data=body, headers=headers, timeout=30,
            allow_redirects=False,
        )
        return Response({**response.headers, "status": str(response.status_code)}), response.content

    def close(self):
        self.session.close()


def build_send_gmail(credentials):
    from googleapiclient.discovery import build
    return build("gmail", "v1", http=SingleAttemptHttp(credentials),
                 cache_discovery=False, static_discovery=True)


def profile_only(service):
    return service.users().getProfile(userId="me", fields="emailAddress").execute(
        num_retries=0
    )


def authorize(client_path, store, port=0, timeout=600, flow_class=None,
              service_factory=build_gmail):
    # Fail before opening OAuth if Keychain is inaccessible/corrupt.
    store.load()
    config = read_client(client_path)
    if flow_class is None:
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow_class = InstalledAppFlow
    flow = flow_class.from_client_config(
        config, scopes=list(SCOPES), autogenerate_code_verifier=True
    )
    credentials = flow.run_local_server(
        host="127.0.0.1", bind_addr="127.0.0.1", port=port,
        open_browser=False, timeout_seconds=timeout,
        authorization_prompt_message="OPEN_AUTH_URL {url}",
        success_message="Authorization received. Check the terminal for account verification.",
        login_hint=EXPECTED_EMAIL, prompt="consent", include_granted_scopes="false",
    )
    # This is actual grant evidence, not merely credentials.scopes (requested).
    granted = credentials.granted_scopes
    if granted is None:
        granted = flow.oauth2session.token.get("scope")
    exact_scopes(granted)
    service = service_factory(credentials)
    try:
        record = make_record(credentials, granted, profile_only(service))
    finally:
        service.close()
    # A rejected account/scope never replaces the prior saved account.
    store.save(record)
    return {"status": "connected", "email": EXPECTED_EMAIL,
            "scopes": sorted(SCOPES), "storage": "macOS Keychain",
            "send_mode": "manual_fixed_internal_test_only"}


def status(store):
    """Local Keychain inspection only; not a current provider verification."""
    record = store.load()
    if record is None:
        return {"status": "not_connected", "expected_email": EXPECTED_EMAIL,
                "network_used": False, "send_mode": "manual_fixed_internal_test_only"}
    expiry = record["credentials"].get("expiry")
    expired = None
    if expiry:
        stamp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        expired = stamp.replace(tzinfo=stamp.tzinfo or timezone.utc) <= datetime.now(timezone.utc)
    return {"status": "stored_not_revalidated", "email": EXPECTED_EMAIL,
            "previously_verified_at": record["verified_at"],
            "scopes": sorted(SCOPES), "access_token_expired": expired,
            "network_used": False, "send_mode": "manual_fixed_internal_test_only"}


@contextmanager
def verified_gmail(store, service_factory, credentials_factory=None, request_factory=None):
    record = store.load()
    if record is None:
        raise SafeError("not_connected")
    if credentials_factory is None:
        from google.oauth2.credentials import Credentials
        credentials_factory = Credentials.from_authorized_user_info
    credentials = credentials_factory(record["credentials"])
    refreshed = False
    if not credentials.valid:
        if request_factory is None:
            from google.auth.transport.requests import Request
            request_factory = Request
        credentials.refresh(request_factory())
        refreshed = True
        # A refresh response may legitimately omit scope; retain the prior
        # actual grant evidence, but reject any explicit changed scope set.
        if credentials.granted_scopes is not None:
            exact_scopes(credentials.granted_scopes)
    service = service_factory(credentials)
    try:
        profile = profile_only(service)
        require_identity(profile)
        new_record = make_record(credentials, record["granted_scopes"], profile)
        # Save a refreshed token only in Keychain, before any mail operation.
        if refreshed:
            store.save(new_record)
        yield service, refreshed
    finally:
        service.close()


def read_test(thread_id, store, service_factory=build_gmail,
              credentials_factory=None, request_factory=None):
    if not isinstance(thread_id, str) or not ID_PATTERN.fullmatch(thread_id):
        raise SafeError("invalid_thread_id")
    with verified_gmail(store, service_factory, credentials_factory, request_factory) as (service, refreshed):
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="minimal", fields="id,messages/id"
        ).execute(num_retries=0)
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise SafeError("thread_proof_invalid")
        messages = thread.get("messages", [])
        if not isinstance(messages, list):
            raise SafeError("thread_proof_invalid")
        ids = [m.get("id") if isinstance(m, dict) else None for m in messages]
        if any(not isinstance(mid, str) or not ID_PATTERN.fullmatch(mid) for mid in ids):
            raise SafeError("thread_proof_invalid")
        return {"status": "read_test_passed", "email": EXPECTED_EMAIL,
                "thread_id": thread_id, "message_ids": ids, "message_count": len(ids),
                "body_or_headers_requested": False, "token_refreshed": refreshed,
                "send_mode": "manual_fixed_internal_test_only"}


def require_test_id(test_id):
    if not isinstance(test_id, str) or not TEST_ID_PATTERN.fullmatch(test_id):
        raise SafeError("invalid_test_id")
    return test_id


def fixed_test_message(test_id):
    require_test_id(test_id)
    message = EmailMessage()
    message["From"] = EXPECTED_EMAIL
    message["To"] = TEST_RECIPIENT
    message["Subject"] = TEST_SUBJECT
    message["Message-ID"] = f"<{test_id.lower()}@gamechangers.com.tr>"
    message["X-Hamle-Test-ID"] = test_id
    message.set_content(TEST_BODY)
    return message


def validate_test_message(message, test_id):
    """Fail closed even if a future caller tries to alter the fixed fixture."""
    expected = fixed_test_message(test_id)
    if message.get_all("From") != [EXPECTED_EMAIL]:
        raise SafeError("test_sender_rejected")
    if message.get_all("To") != [TEST_RECIPIENT]:
        raise SafeError("test_recipient_rejected")
    if (set(message.keys()) != set(expected.keys())
            or len(message.items()) != len(expected.items())
            or any(message.get_all(key) != expected.get_all(key) for key in expected.keys())
            or message.is_multipart() or message.get_content() != TEST_BODY):
        raise SafeError("test_message_changed")


class SendAttemptJournal:
    """Non-secret one-shot claims. Never delete/release a claim automatically."""

    def __init__(self, directory=ATTEMPT_DIRECTORY):
        self.directory = Path(directory)

    def _create(self, test_id, suffix, value):
        require_test_id(test_id)
        directory_fd = None
        file_fd = None
        try:
            self.directory.mkdir(mode=0o700, exist_ok=True)
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            info = os.fstat(directory_fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise SafeError("attempt_storage_not_private")
            # Persist a newly created journal directory's entry as well.
            parent_fd = os.open(self.directory.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            file_fd = os.open(test_id + suffix, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=directory_fd)
            with os.fdopen(file_fd, "w", encoding="utf-8") as stream:
                file_fd = None
                json.dump(value, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(directory_fd)
        except FileExistsError:
            raise SafeError("test_id_already_attempted") from None
        except SafeError:
            raise
        except Exception:
            raise SafeError("attempt_storage_failed") from None
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if directory_fd is not None:
                os.close(directory_fd)

    def claim(self, test_id):
        self._create(test_id, ".attempt.json", {
            "version": 1, "test_id": test_id, "claimed_at": utc_now(),
            "state": "attempt_reserved_do_not_retry", "sender": EXPECTED_EMAIL,
            "recipient": TEST_RECIPIENT,
        })

    def accepted(self, test_id, proof):
        self._create(test_id, ".accepted.json", proof)


def send_test(test_id, store, journal=None, service_factory=build_send_gmail,
              credentials_factory=None, request_factory=None):
    # No caller-supplied addresses, content, attachments, replies, or recipients.
    message = fixed_test_message(test_id)
    validate_test_message(message, test_id)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    journal = journal if journal is not None else SendAttemptJournal()
    with verified_gmail(store, service_factory, credentials_factory, request_factory) as (service, refreshed):
        # Atomic and durable before send. Any error/crash leaves this claim
        # blocked. Message-ID is diagnostic, not a provider idempotency promise.
        journal.claim(test_id)
        try:
            proof = service.users().messages().send(
                userId="me", body={"raw": raw}, fields="id,threadId"
            ).execute(num_retries=0)
            if not isinstance(proof, dict) or any(
                not isinstance(proof.get(key), str) or not ID_PATTERN.fullmatch(proof[key])
                for key in ("id", "threadId")
            ):
                raise ValueError()
            result = {"status": "send_test_accepted", "sender": EXPECTED_EMAIL,
                      "recipient": TEST_RECIPIENT, "test_id": test_id,
                      "message_id": proof["id"], "thread_id": proof["threadId"],
                      "delivery_confirmed": False, "automatic_retry": False,
                      "token_refreshed": refreshed}
            journal.accepted(test_id, result)
            return result
        except (Exception, KeyboardInterrupt):
            # HTTP failure, timeout, malformed response, interruption, or failed
            # local receipt storage cannot prove whether Gmail accepted it.
            raise SafeError("send_outcome_unknown_do_not_retry") from None


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise SafeError("invalid_arguments")


def parse_args(argv):
    parser = SafeParser(description="Hamle Gmail bootstrap; fixed manual internal send test only.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=SafeParser)
    sub.add_parser("status", help="Local Keychain status; no network.")
    auth = sub.add_parser("auth", help="Explicit interactive Desktop OAuth.")
    auth.add_argument("--client-secrets", required=True)
    auth.add_argument("--port", type=int, default=0)
    auth.add_argument("--timeout", type=int, default=600)
    test = sub.add_parser("read-test", help="Read only an explicitly supplied known thread.")
    test.add_argument("--thread-id", required=True)
    send = sub.add_parser("send-test", help="One fixed technical test from Hamle to Ahmet.")
    send.add_argument("--test-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "auth" and (not 0 <= args.port <= 65535 or not 30 <= args.timeout <= 1800):
        raise SafeError("invalid_arguments")
    return args


def main(argv=None, store_factory=MacKeychainStore):
    # OAuth/library HTTP diagnostics can contain callback codes or tokens.
    # No debug flag and no raw exception/traceback output are provided.
    logging.disable(logging.CRITICAL)
    # The URL must be visible while the loopback server is still waiting,
    # including when an orchestrator captures stdout through a pipe.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    try:
        args = parse_args(argv)
        store = store_factory()
        if args.command == "status":
            result = status(store)
        elif args.command == "auth":
            result = authorize(args.client_secrets, store, args.port, args.timeout)
        elif args.command == "read-test":
            result = read_test(args.thread_id, store)
        else:
            result = send_test(args.test_id, store)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except SafeError as error:
        print(json.dumps({"status": "error", "code": str(error)}), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('{"status":"error","code":"interrupted"}', file=sys.stderr)
        return 130
    except Exception:
        print('{"status":"error","code":"operation_failed"}', file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
