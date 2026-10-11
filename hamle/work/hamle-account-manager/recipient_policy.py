"""Reply-all boundary for the explicitly authorized internal pilot.

Cansu's address was observed in Ahmet's actual latest CC header on 2026-10-10
(followup-latest-recipient-evidence.json), not inferred from a name. A newly
encountered recipient is held for scope verification, never silently dropped.
This module does not send mail or grant customer authority.
"""
from __future__ import annotations

AHMET = "ahmet@gamechangers.com.tr"
HAMLE = "hamle@gamechangers.com.tr"
AUTHORIZED_INTERNAL_CC = frozenset({"cansu@gamechangers.com.tr"})


class RecipientError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_reply_recipients(to, cc):
    if (to != [AHMET] or not isinstance(cc, list) or len(cc) > 10
            or any(not isinstance(v, str) or v not in AUTHORIZED_INTERNAL_CC for v in cc)
            or len(set(cc)) != len(cc)):
        raise RecipientError("reply_recipient_scope_unverified")
    return {"to": list(to), "cc": list(cc)}


def derive_reply_recipients(latest_to, latest_cc):
    if (not isinstance(latest_to, list) or not isinstance(latest_cc, list)
            or len(latest_to) + len(latest_cc) > 20
            or any(not isinstance(v, str) or v != v.lower() or not v for v in latest_to + latest_cc)
            or HAMLE not in latest_to + latest_cc
            or any(v not in (AHMET, HAMLE) for v in latest_to)):
        raise RecipientError("latest_recipient_scope_unverified")
    # Preserve current CC order; never consult an older message or quoted body.
    cc = list(dict.fromkeys(v for v in latest_cc if v not in (AHMET, HAMLE)))
    return validate_reply_recipients([AHMET], cc)
