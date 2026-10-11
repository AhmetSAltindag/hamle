"""Opt-in one-test Gmail read. No send, polling, mark-read, or attachment API."""
import argparse
from functools import lru_cache
import importlib.util
import json
from pathlib import Path
import sys
import time

from intake import AHMET, HAMLE, Capture, ReviewInbox, identifier, snapshot_fingerprint

SUBJECT = "HAMLE-IC-PROVA-20261010-YENIMAC-01"
OLD_THREAD = "1a12270588d30c79"
QUERY = f'from:{AHMET} {{to:{HAMLE} cc:{HAMLE}}} subject:"{SUBJECT}"'
SUBJECT_FIELDS = "id,messages(id,threadId,internalDate,payload/headers)"


class CaptureError(ValueError):
    """Static error codes, safe to display."""


@lru_cache(maxsize=1)
def _reader_module():
    path = Path(__file__).resolve().parent.parent / "hamle-workflow" / "gmail_full_reader.py"
    spec = importlib.util.spec_from_file_location("_hamle_internal_capture_reader", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _select_thread(response):
    if not isinstance(response, dict) or response.get("nextPageToken"):
        raise CaptureError("test_search_ambiguous")
    rows = response.get("threads", [])
    if not isinstance(rows, list):
        raise CaptureError("test_search_invalid")
    if not rows:
        raise CaptureError("test_not_found")
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise CaptureError("test_search_ambiguous")
    thread_id = identifier(rows[0].get("id"))
    if thread_id == OLD_THREAD:
        raise CaptureError("old_test_thread_forbidden")
    return thread_id


def _subject_snapshot(response, thread_id):
    if not isinstance(response, dict) or response.get("id") != thread_id or "nextPageToken" in response:
        raise CaptureError("subject_snapshot_invalid")
    rows = response.get("messages")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
        raise CaptureError("subject_snapshot_invalid")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("threadId") != thread_id:
            raise CaptureError("subject_snapshot_invalid")
        mid = identifier(row.get("id"))
        stamp = row.get("internalDate")
        if (mid in result or not isinstance(stamp, str) or not stamp.isascii()
                or not stamp.isdigit() or len(stamp) > 15):
            raise CaptureError("subject_snapshot_invalid")
        headers = row.get("payload", {}).get("headers") if isinstance(row.get("payload"), dict) else None
        if not isinstance(headers, list) or len(headers) > 200:
            raise CaptureError("subject_snapshot_invalid")
        subjects = []
        for header in headers:
            if (not isinstance(header, dict) or not isinstance(header.get("name"), str)
                    or not isinstance(header.get("value"), str)):
                raise CaptureError("subject_snapshot_invalid")
            if header["name"].casefold() == "subject":
                subjects.append(header["value"])
        if len(subjects) != 1:
            raise CaptureError("subject_missing_or_ambiguous")
        result[mid] = {"time_ms": int(stamp), "subject_matches": subjects[0] == SUBJECT}
    return result


def _capture_verified(*, helper, store, service_factory, read_thread, clock=time.time,
                      credentials_factory=None, request_factory=None):
    """Private fake seam. Production gets dependencies exclusively from local modules."""
    with helper.verified_gmail(store, service_factory, credentials_factory, request_factory) as (service, refreshed):
        search = service.users().threads().list(
            userId="me", q=QUERY, maxResults=2, includeSpamTrash=False,
            fields="threads/id,nextPageToken").execute(num_retries=0)
        thread_id = _select_thread(search)
        subjects = _subject_snapshot(service.users().threads().get(
            userId="me", id=thread_id, format="metadata", metadataHeaders=["Subject"],
            fields=SUBJECT_FIELDS).execute(num_retries=0), thread_id)
        # Search terms are only discovery. An exact real Subject must exist before
        # the original reader is allowed to fetch that one thread's full response.
        if not any(row["subject_matches"] for row in subjects.values()):
            raise CaptureError("exact_test_subject_not_found")
        captured = read_thread(thread_id, body_message_id=None)
    if (not isinstance(captured, dict) or captured.get("email") != HAMLE
            or captured.get("requested_thread_id") != thread_id
            or captured.get("requested_body_message_id") is not None
            or captured.get("mailbox_writes") != 0
            or captured.get("external_actions_enabled") is not False):
        raise CaptureError("reader_contract_invalid")
    observation = captured.get("observation")
    # Revalidate the original normalizer's safe metadata contract.
    digest = snapshot_fingerprint(observation)
    observed = {row["message_id"]: row["internal_date_ms"] for row in observation["messages"]}
    if (observation["thread_id"] != thread_id or set(observed) != set(subjects)
            or any(observed[mid] != subjects[mid]["time_ms"] for mid in observed)):
        raise CaptureError("thread_changed_between_reads")
    latest = observation["order"]["latest_observed_incoming_id"]
    if latest is None or not subjects[latest]["subject_matches"]:
        raise CaptureError("latest_incoming_subject_mismatch")
    if any(row.get("body") != [] or row.get("body_status") != "withheld"
           for row in observation["messages"]):
        raise CaptureError("body_withholding_contract_invalid")
    now = clock()
    context = Capture("captured_normalized", HAMLE, now, True, digest)
    decision = ReviewInbox().review(observation, capture=context, now=now)
    return {"status": "review_ready" if decision.outcome == "review_only" else "held",
            "reason": decision.reason, "test_subject": SUBJECT,
            "capture_observed_at": now,
            "review_item": decision.review_item, "action_eligible": False,
            "mailbox_writes": 0, "external_actions_enabled": False,
            "body_returned": False, "attachments_fetched": False,
            "token_refreshed": bool(refreshed or captured.get("token_refreshed")),
            "read_scope": "one_explicit_internal_test_thread_in_hamle_mailbox",
            "unseen_cc_removal_resolved": False}


def _read_internal_test():
    reader = _reader_module()
    with reader._quiet_call():
        try:
            directory = Path(__file__).resolve().parent.parent
            helper = reader._local_module("_hamle_internal_capture_helper",
                directory / "hamle-gmail-connector" / "hamle_gmail.py")
            return _capture_verified(helper=helper, store=helper.MacKeychainStore(),
                service_factory=helper.build_gmail, read_thread=reader.read_full_thread)
        except CaptureError:
            raise
        except Exception as error:
            raise CaptureError(reader._safe_code(error)) from None


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        raise CaptureError("invalid_cli_arguments")


def main(argv=None, *, _capture=None):
    try:
        parser = QuietParser(description="Read only the fixed Hamle internal test once.")
        parser.add_argument("--read-internal-test", action="store_true")
        args = parser.parse_args(argv)
        if not args.read_internal_test:
            raise CaptureError("explicit_read_flag_required")
        result = (_capture or _read_internal_test)()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "review_ready" else 2
    except CaptureError as error:
        print(json.dumps({"status": "held", "reason": str(error), "action_eligible": False,
                          "mailbox_writes": 0, "external_actions_enabled": False}))
        return 2
    except Exception:
        print(json.dumps({"status": "held", "reason": "internal_capture_failed",
                          "action_eligible": False, "mailbox_writes": 0,
                          "external_actions_enabled": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
