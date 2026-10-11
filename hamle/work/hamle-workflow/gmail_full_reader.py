"""Explicit, one-thread Gmail observation. Importing does not access an account.

No CLI, search, polling, send, attachment fetch, or action-authority interface.
The production entry point is read_full_thread(thread_id, body_message_id=None).
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from functools import lru_cache
import importlib.util
import logging
from pathlib import Path
import re
import sys
import threading

EXPECTED_EMAIL = "hamle@gamechangers.com.tr"
ID_PATTERN = re.compile(r"[a-fA-F0-9]{1,64}\Z")
THREAD_FIELDS = "id,messages(id,threadId,labelIds,internalDate,payload)"
_LOCK = threading.RLock()
_SAFE_HELPER_CODES = frozenset({
    "not_connected", "macos_keychain_required", "keychain_unavailable",
    "keychain_read_failed", "keychain_write_failed", "invalid_keychain_record",
    "wrong_account", "scope_mismatch", "granted_scopes_unverified",
    "refresh_token_missing", "credential_serialization_failed",
})


class ReadError(Exception):
    """Only fixed codes; original provider errors are not exposed."""


class _DiscardOutput:
    def write(self, value):
        return len(value)

    def flush(self):
        pass


@contextmanager
def _quiet_call():
    # A manual, serialized adapter: no token/provider diagnostics in stdout or
    # logging. Restore caller settings even after a failure. Not a worker pool.
    with _LOCK:
        disabled = logging.root.manager.disable
        logging.disable(max(disabled, logging.CRITICAL))
        try:
            with redirect_stdout(_DiscardOutput()), redirect_stderr(_DiscardOutput()):
                yield
        finally:
            logging.disable(disabled)


@lru_cache(maxsize=2)
def _local_module(name, path):
    # Paths derive from this file, never CWD, user input, or PYTHONPATH.
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ReadError("local_dependency_unavailable")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(name)
    sys.modules[name] = module  # dataclass/type annotation lookup during import
    try:
        spec.loader.exec_module(module)
    except Exception:
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous
        raise ReadError("local_dependency_unavailable") from None
    return module


def _canonical_id(value, error_code):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ReadError(error_code)
    return value.lower()


def read_full_thread(thread_id, *, body_message_id=None):
    """Read one explicit Hamle thread; optionally request one authorized body.

    This performs real reads only when the caller invokes it. Production uses
    the existing helper's native Keychain, exact scopes and fresh profile check.
    Returned body permission never overrides the normalizer's To/CC/order gates.
    There is intentionally no public injectable account/service/endpoint input.
    """
    thread_id = _canonical_id(thread_id, "invalid_thread_id")
    body_message_id = (None if body_message_id is None else
                       _canonical_id(body_message_id, "invalid_body_message_id"))
    with _quiet_call():
        try:
            directory = Path(__file__).resolve().parent
            normalizer = _local_module("_hamle_reader_mail_ingest", directory / "mail_ingest.py")
            helper = _local_module("_hamle_reader_gmail_helper",
                                   directory.parent / "hamle-gmail-connector" / "hamle_gmail.py")
            store = helper.MacKeychainStore()
            return _read_verified(thread_id, body_message_id, helper=helper, normalizer=normalizer,
                                  store=store, service_factory=helper.build_gmail)
        except ReadError:
            raise
        except Exception as error:
            raise ReadError(_safe_code(error)) from None


def _safe_code(error):
    # Do not trust provider exception class names/messages as safe diagnostics.
    return str(error) if str(error) in _SAFE_HELPER_CODES else "provider_read_failed"


def _read_verified(thread_id, body_message_id, *, helper, normalizer, store,
                   service_factory, credentials_factory=None, request_factory=None):
    """Private dependency seam for offline tests; not an authorization API."""
    with _quiet_call():
        try:
            thread_id = _canonical_id(thread_id, "invalid_thread_id")
            body_message_id = (None if body_message_id is None else
                               _canonical_id(body_message_id, "invalid_body_message_id"))
            # verified_gmail validates stored exact grants, refreshes if needed,
            # verifies getProfile emailAddress, and closes the service in finally.
            with helper.verified_gmail(store, service_factory, credentials_factory,
                                       request_factory) as (service, refreshed):
                response = service.users().threads().get(
                    userId="me", id=thread_id, format="full", fields=THREAD_FIELDS,
                ).execute(num_retries=0)
                if (not isinstance(response, dict)
                        or _canonical_id(response.get("id"), "provider_thread_invalid") != thread_id
                        or "nextPageToken" in response):
                    raise ReadError("provider_thread_invalid")
                # Complete means only the single unfiltered provider response,
                # never all real-world/unseen mail or a freshness fence.
                context = normalizer.Context(
                    mailbox_email=EXPECTED_EMAIL, verified_provider_response=True,
                    response_format="full", complete_visible_snapshot=True, purpose="live",
                    authorized_body_message_ids=(frozenset() if body_message_id is None
                                                 else frozenset({body_message_id})),
                )
                try:
                    observation = normalizer.normalize_thread(response, context=context)
                except normalizer.NormalizationError:
                    raise ReadError("thread_normalization_failed") from None
                if body_message_id is not None and body_message_id not in observation["message_ids"]:
                    raise ReadError("requested_body_message_not_in_thread")
                # Defense in depth: never expose a different or default body,
                # and never let this adapter activate engine actions.
                observation["action_eligible"] = False
                for message in observation["messages"]:
                    if body_message_id is None or message["message_id"] != body_message_id:
                        message["body"] = []
                        message["body_status"] = "withheld"
                    message["action_eligible"] = False
                    message["engine_evidence"]["sequence"] = None
                return {
                    "status": "full_thread_normalized", "email": EXPECTED_EMAIL,
                    "requested_thread_id": thread_id, "requested_body_message_id": body_message_id,
                    "token_refreshed": bool(refreshed),
                    "gmail_read_operations": ["profile", "thread_full"],
                    "mailbox_writes": 0, "external_actions_enabled": False,
                    "observation": observation,
                }
        except ReadError:
            raise
        except Exception as error:
            raise ReadError(_safe_code(error)) from None

