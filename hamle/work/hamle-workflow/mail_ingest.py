"""Pure Gmail full-response normalizer; no I/O, credentials or action authority."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from email import policy
from email.parser import HeaderParser
import re

HAMLE = "hamle@gamechangers.com.tr"
ID = re.compile(r"[0-9a-fA-F]{1,64}\Z")
ADDRESS = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
MAX_MESSAGES, MAX_PARTS, MAX_DEPTH, MAX_BODY_BYTES = 1000, 500, 20, 1048576
BODY_TYPES = frozenset({"text/plain", "text/html"})
BODY_CONTAINERS = frozenset({"multipart/mixed", "multipart/alternative", "multipart/related"})


class NormalizationError(ValueError):
    """Fixed codes only; never echo input mail or decoding exceptions."""


@dataclass(frozen=True)
class Context:
    mailbox_email: str
    verified_provider_response: bool = False
    response_format: str = "full"
    complete_visible_snapshot: bool = False
    purpose: str = "live"
    # Trusted caller's independent permission to read these message bodies.
    # The provider response cannot self-authorize by carrying a similar key.
    authorized_body_message_ids: frozenset[str] = field(default_factory=frozenset)


def canonical_id(value, kind="message"):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise NormalizationError("invalid_" + kind + "_id")
    return value.lower()


def _headers(payload):
    values, errors = {}, []
    rows = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > 200:
        return {}, ["headers_missing_or_invalid"]
    for row in rows:
        if not isinstance(row, dict):
            errors.append("header_invalid")
            continue
        name, value = row.get("name"), row.get("value")
        if (not isinstance(name, str) or not re.fullmatch(r"[!-9;-~]+", name)
                or not isinstance(value, str) or len(value) > 65536
                or any(ord(c) < 32 and c != "\t" for c in value) or "\x7f" in value):
            errors.append("header_invalid")
            continue
        values.setdefault(name.lower(), []).append(value)
    return values, errors


def _one(headers, name, errors, *, required=False):
    values = headers.get(name, [])
    if len(values) > 1:
        errors.append("duplicate_" + name.replace("-", "_"))
        return None
    if not values:
        if required:
            errors.append("missing_" + name)
        return None
    return values[0]


def _addresses(value, name, errors):
    if value is None:
        return []
    try:
        parsed = HeaderParser(policy=policy.default).parsestr(name + ": " + value + "\n\n")[name]
        if parsed.defects:
            raise ValueError()
        addresses = []
        for address in parsed.addresses:
            candidate = address.addr_spec
            if not candidate.isascii() or not ADDRESS.fullmatch(candidate):
                raise ValueError()
            addresses.append(candidate.casefold())
        if value.strip() and not addresses:
            raise ValueError()
        return list(dict.fromkeys(addresses))
    except Exception:
        errors.append("invalid_" + name.lower() + "_addresses")
        return []


def _date(value):
    # Gmail internalDate is ordering metadata, not a grant or global sequence.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,15}", value):
        return None
    number = int(value)
    return number if number <= 253402300799999 else None


def _inventory(payload):
    inventory, errors = [], []
    seen = set()

    def walk(part, path, depth, attached=False):
        if depth > MAX_DEPTH or len(inventory) >= MAX_PARTS:
            raise NormalizationError("mime_structure_limit")
        if not isinstance(part, dict) or id(part) in seen:
            raise NormalizationError("mime_structure_invalid")
        seen.add(id(part))
        mime = part.get("mimeType", "")
        filename = part.get("filename", "")
        body = part.get("body", {})
        if (not isinstance(mime, str) or not isinstance(filename, str)
                or len(mime) > 200 or len(filename) > 4096 or not isinstance(body, dict)):
            raise NormalizationError("mime_metadata_invalid")
        headers, header_errors = _headers(part)
        if header_errors:
            errors.append("mime_headers_missing_or_invalid")
        # Child MIME headers cannot grant participation. Ambiguous disposition
        # means that part may be an attachment and must not be body-decoded.
        dispositions = headers.get("content-disposition", [])
        if len(dispositions) > 1:
            errors.append("mime_disposition_ambiguous")
        if len(headers.get("content-type", [])) > 1:
            errors.append("mime_content_type_ambiguous")
        attached = (attached or bool(filename) or mime.lower() == "message/rfc822"
                    or bool(header_errors) or len(dispositions) > 1
                    or any(v.split(";", 1)[0].strip().lower() == "attachment" for v in dispositions))
        size = body.get("size")
        if size is not None and (type(size) is not int or size < 0):
            errors.append("mime_size_invalid")
            size = None
        attachment_id = body.get("attachmentId")
        if attachment_id is not None and (not isinstance(attachment_id, str) or not attachment_id or len(attachment_id) > 4096):
            raise NormalizationError("attachment_id_invalid")
        part_id = part.get("partId")
        if part_id is not None and not isinstance(part_id, str):
            raise NormalizationError("mime_part_id_invalid")
        inventory.append({"path": path, "part_id": part_id, "mime_type": mime.lower(),
                          "filename": filename, "size_bytes": size,
                          "is_attachment": attached, "attachment_id": attachment_id,
                          "inline_data_present": "data" in body,
                          "content_inspected": False})
        children = part.get("parts", [])
        if not isinstance(children, list):
            raise NormalizationError("mime_structure_invalid")
        if not attached:
            # Validate every body part before any decoding. An unsupported
            # alternative is not silently discarded in favor of a readable one.
            if children:
                if mime.lower() not in BODY_CONTAINERS:
                    errors.append("body_container_unsupported")
                if body.get("data") not in (None, "") or size not in (None, 0) or attachment_id:
                    errors.append("body_container_data_ambiguous")
            elif mime.lower().startswith("multipart/"):
                errors.append("body_container_children_missing")
            elif mime.lower() not in BODY_TYPES:
                errors.append("body_mime_unsupported")
            content_types = headers.get("content-type", [])
            if len(content_types) == 1:
                parsed_type = HeaderParser(policy=policy.default).parsestr(
                    "Content-Type: " + content_types[0] + "\n\n")
                if (parsed_type["Content-Type"].defects
                        or parsed_type.get_content_type().lower() != mime.lower()):
                    errors.append("body_mime_metadata_ambiguous")
        for index, child in enumerate(children):
            walk(child, path + "." + str(index), depth + 1, attached)
    walk(payload, "0", 0)
    return inventory, errors


def _decode_bodies(payload, inventory):
    texts, errors, total = [], [], 0
    parts = {"0": payload}
    # Depth/part limits were checked while inventorying; never open attachments.
    for item in inventory:
        part = parts[item["path"]]
        for index, child in enumerate(part.get("parts", [])):
            parts[item["path"] + "." + str(index)] = child
        mime = item["mime_type"]
        if item["is_attachment"] or mime in BODY_CONTAINERS:
            continue
        if mime not in BODY_TYPES:
            # Defensive check: inventory validation normally withholds before
            # this point. Never return a partial body if called independently.
            errors.append("body_mime_unsupported")
            continue
        body = part.get("body", {})
        if "attachmentId" in body:
            errors.append("body_external_data_not_fetched")
            continue
        encoded = body.get("data")
        if encoded is None:
            if body.get("size") != 0:
                errors.append("body_data_missing")
            continue
        try:
            if (not isinstance(encoded, str) or len(encoded) > 4 * MAX_BODY_BYTES // 3 + 8
                    or not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", encoded)):
                raise ValueError()
            raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
            total += len(raw)
            if total > MAX_BODY_BYTES:
                raise ValueError()
            headers, errors_here = _headers(part)
            if errors_here or len(headers.get("content-type", [])) > 1:
                raise ValueError()
            content_type = headers.get("content-type", [mime])[0]
            parsed = HeaderParser(policy=policy.default).parsestr("Content-Type: " + content_type + "\n\n")
            charset = parsed.get_content_charset() or "utf-8"
            text = raw.decode(charset, errors="strict")
            texts.append({"path": item["path"], "mime_type": mime,
                          "text": text, "untrusted_source_data": True,
                          "rendered": False})
        except (ValueError, binascii.Error, UnicodeError, LookupError):
            errors.append("body_decode_failed")
    # Partial success is not silently substituted for the complete body.
    return ([] if errors else texts), errors


def normalize_thread(response, *, context):
    """Normalize one already-fetched Gmail format=full Thread dictionary.

    No dictionary fields can provide trust, permission, sequence or side effects.
    Even a unique internalDate maximum does not authorize an engine action.
    """
    if not isinstance(context, Context) or not isinstance(response, dict):
        raise NormalizationError("invalid_input")
    thread_id = canonical_id(response.get("id"), "thread")
    rows = response.get("messages")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_MESSAGES:
        raise NormalizationError("thread_messages_missing_or_invalid")
    body_grants = {canonical_id(value) for value in context.authorized_body_message_ids}
    trusted = (context.verified_provider_response is True and context.response_format == "full"
               and isinstance(context.mailbox_email, str) and context.mailbox_email.casefold() == HAMLE)
    messages, payloads, seen_ids = [], {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise NormalizationError("message_invalid")
        message_id = canonical_id(row.get("id"))
        if message_id in seen_ids:
            raise NormalizationError("duplicate_message_id")
        seen_ids.add(message_id)
        if canonical_id(row.get("threadId"), "thread") != thread_id:
            raise NormalizationError("message_thread_mismatch")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise NormalizationError("full_payload_missing")
        headers, errors = _headers(payload)
        to_value, cc_value = _one(headers, "to", errors), _one(headers, "cc", errors)
        from_value = _one(headers, "from", errors, required=True)
        to, cc = _addresses(to_value, "To", errors), _addresses(cc_value, "Cc", errors)
        senders = _addresses(from_value, "From", errors)
        if len(senders) != 1:
            errors.append("sender_ambiguous")
        sender = senders[0] if len(senders) == 1 else None
        labels = row.get("labelIds")
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            errors.append("labels_missing_or_invalid")
            labels = []
        auto = _one(headers, "auto-submitted", errors)
        precedence = _one(headers, "precedence", errors)
        auto_reply = _one(headers, "x-autoreply", errors)
        auto_respond = _one(headers, "x-autorespond", errors)
        automated = bool((auto is not None and auto.strip().casefold() != "no")
                         or (precedence and precedence.strip().casefold() in {"bulk", "list", "junk"})
                         or auto_reply or auto_respond or headers.get("list-id"))
        outbound = sender == HAMLE or "SENT" in labels or "DRAFT" in labels
        participation = "uncertain" if errors or not trusted else ("included" if HAMLE in to + cc else "excluded")
        inventory, mime_errors = _inventory(payload)
        errors.extend(mime_errors)
        envelope_eligible = participation == "included" and not outbound and not automated and not errors
        metadata = {"message_id": message_id, "thread_id": thread_id,
                    "from_address": sender, "direct_to": to, "direct_cc": cc,
                    "direction": "outbound" if outbound else "inbound",
                    "label_ids": sorted(set(labels)), "internal_date_ms": _date(row.get("internalDate")),
                    "participation": participation, "automated": automated,
                    "envelope_eligible": envelope_eligible, "body": [],
                    "body_status": "withheld", "issues": errors,
                    "mime_inventory": inventory, "action_eligible": False,
                    "engine_evidence": {"message_id": message_id, "thread_id": thread_id,
                        "verified_direct_headers": trusted and not errors,
                        "sequence": None, "from_address": sender,
                        "direction": "outbound" if outbound else "inbound",
                        "direct_to": to, "direct_cc": cc}}
        messages.append(metadata)
        payloads[message_id] = payload
    incoming = [m for m in messages if m["direction"] == "inbound"]
    dates = [m["internal_date_ms"] for m in incoming]
    order_ok = (trusted and context.complete_visible_snapshot is True and incoming
                and all(d is not None for d in dates) and len(dates) == len(set(dates))
                and not any(m["issues"] for m in messages))
    latest = max(incoming, key=lambda m: m["internal_date_ms"]) if order_ok else None
    for message in messages:
        if (latest is message and context.purpose == "live" and message["envelope_eligible"]
                and message["message_id"] in body_grants):
            body, body_errors = _decode_bodies(payloads[message["message_id"]], message["mime_inventory"])
            message["body"] = body
            message["body_status"] = "decode_failed" if body_errors else "decoded_unrendered"
            message["issues"].extend(body_errors)
    return {"schema_version": 1, "thread_id": thread_id, "message_count": len(messages),
            "message_ids": [m["message_id"] for m in messages], "messages": messages,
            "order": {"status": "observed_internal_date_order_only" if order_ok else "inconclusive",
                      "latest_observed_incoming_id": latest["message_id"] if latest else None,
                      "authority_sequence": None, "global_order_verified": False,
                      "unseen_messages_known": False},
            "action_eligible": False, "hold_reason": "live_order_and_authority_integration_required",
            "provider_authenticity_proven_by_parser": False,
            "external_calls": 0, "external_writes": 0}
