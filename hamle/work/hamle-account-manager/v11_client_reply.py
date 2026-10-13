"""Client reply mode for v11_client_watch (Ahmet 2026-10-13: "yarın aldığı ilk maillere cevap vermesi").

Only for brands listed in the config's reply block (Ahmet's GO per brand) and only for mails
received after reply.since. Everything else stays a silent draft exactly as before.

Auto-send (no human step) only when every check passes: category in auto_categories, no question
in the reply and no missing info (unless allow_questions: Ahmet's own replies ask production
questions), the mail starts a new thread (when new_threads_only), no internal words, confidence >= auto_min_confidence,
greeting and Hamle signature present, the thread has no earlier Hamle reply, the day's send
limit is not reached, every reply-all recipient is a known client address of the same brand
or a verified agency contact, and every client recipient is marked "direct" in the contact table
(Markalar.xlsx "Hamle bu kişiye doğrudan yazabilir mi?" = Evet). Price / contract / complaint mails get Ahmet's fixed sentence with
Ahmet and Cansu added to CC (decision 6). Any failed check -> the draft waits for approval: the
Basecamp note is assigned to the brand's owner, and an approver writing "Hamle gönder" on it
sends the draft (or the corrected text written under that line); "Hamle gönderme" drops it.

The send is the chain's same-thread Gmail reply (exact To/CC re-derived from the anchor, one
POST, raw readback). The anchor must still be the newest incoming mail of the thread: if the
client wrote again or a teammate answered first, Hamle does not send.
"""
from __future__ import annotations

import html
import re
from datetime import datetime

from autonomy_policy import HAMLE

AUTO_CATEGORIES = ("request", "feedback_or_approval", "question", "information", "meeting")
SIGNATURES = ("Sevgiler,\nHamle\nGamechangers", "Best,\nHamle\nGamechangers")
APPROVAL_WINDOW_SECONDS = 3 * 24 * 3600
REPLY_FIELDS = {"brands", "auto_min_confidence", "auto_categories", "since", "max_sends_per_day", "approvers",
                "owners", "sensitive_cc", "allow_questions", "new_threads_only"}
ONGOING = re.compile(r"\A\s*(?:re|ynt|yanıt|cevap|aw|sv)\s*:", re.IGNORECASE)  # a reply in an existing thread
SEND = re.compile(r"(?<![0-9a-zçğıöşü])hamle\W{0,3}gönder(?![a-zçğıöşü])", re.IGNORECASE)
DONT_SEND = re.compile(r"(?<![0-9a-zçğıöşü])hamle\W{0,3}gönderme(?![a-zçğıöşü])", re.IGNORECASE)
MIN_EDIT_CHARS = 20


class ClientReplyError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ClientReplyError(code)


def agency_people(catalog):
    """Verified agency contacts that may receive mail: email -> {'name', 'basecamp_id'}."""
    out = {}
    for row in catalog.get("contacts") or []:
        if (isinstance(row, dict) and row.get("verified") is True and row.get("side") == "agency"
                and row.get("can_receive") is True and isinstance(row.get("email"), str)
                and row["email"].endswith("@gamechangers.com.tr") and row["email"] != HAMLE):
            bc = row.get("basecamp_id") if row.get("basecamp_verified") is True and row.get("project_member") else None
            out[row["email"]] = {"name": row.get("name"), "basecamp_id": bc if type(bc) is int else None}
    return out


def validate_reply(block, catalog):
    """Config 'reply' block -> resolved settings. Approvers and owners must be verified Basecamp members."""
    people = agency_people(catalog)
    _need(isinstance(block, dict) and set(block) == REPLY_FIELDS, "client_reply_config_invalid")
    brands = block["brands"]
    _need(isinstance(brands, list) and 1 <= len(brands) <= 10 and len(set(brands)) == len(brands)
          and all(isinstance(b, str) and re.fullmatch(r"[a-z0-9_]{2,40}", b) for b in brands),
          "client_reply_brands_invalid")
    low = block["auto_min_confidence"]
    _need(type(low) is int and 80 <= low <= 101, "client_reply_confidence_invalid")  # 101 = never auto
    cats = block["auto_categories"]
    _need(isinstance(cats, list) and len(set(cats)) == len(cats) and set(cats) <= set(AUTO_CATEGORIES),
          "client_reply_categories_invalid")
    try:
        since = datetime.fromisoformat(str(block["since"]).replace("Z", "+00:00"))
    except ValueError:
        raise ClientReplyError("client_reply_since_invalid") from None
    _need(since.tzinfo is not None, "client_reply_since_invalid")
    _need(type(block["max_sends_per_day"]) is int and 1 <= block["max_sends_per_day"] <= 30,
          "client_reply_limit_invalid")
    _need(type(block["allow_questions"]) is bool and type(block["new_threads_only"]) is bool,
          "client_reply_config_invalid")

    def member(email):
        _need(isinstance(email, str) and email in people and people[email]["basecamp_id"] is not None,
              "client_reply_person_unverified")
        return people[email]["basecamp_id"]
    approvers = block["approvers"]
    _need(isinstance(approvers, list) and 1 <= len(approvers) <= 3 and len(set(approvers)) == len(approvers),
          "client_reply_approvers_invalid")
    owners = block["owners"]
    _need(isinstance(owners, dict) and set(owners) == set(brands), "client_reply_owners_invalid")
    cc = block["sensitive_cc"]
    _need(isinstance(cc, list) and 1 <= len(cc) <= 2 and len(set(cc)) == len(cc) and all(c in people for c in cc),
          "client_reply_sensitive_cc_invalid")
    return {"brands": set(brands), "auto_min_confidence": low, "auto_categories": set(cats),
            "since_ms": int(since.timestamp() * 1000), "max_sends_per_day": block["max_sends_per_day"],
            "approver_ids": {member(e): people[e]["name"] for e in approvers}, "first_approver": member(approvers[0]),
            "owner_ids": {b: member(e) for b, e in owners.items()}, "sensitive_cc": list(cc),
            "agency": set(people), "allow_questions": block["allow_questions"],
            "new_threads_only": block["new_threads_only"]}


def reply_all(sender, to, cc):
    to_out = [v for v in dict.fromkeys([sender] + list(to)) if v != HAMLE]
    cc_out = [v for v in dict.fromkeys(cc) if v != HAMLE and v not in to_out]
    return to_out, cc_out


def recipients(source, sender, book, settings, *, sensitive):
    """(to, cc, unknown): reply-all of the client mail (+ Ahmet/Cansu for a sensitive one)."""
    to, cc = reply_all(sender["email"], [a.lower() for a in source.get("to") or []],
                       [a.lower() for a in source.get("cc") or []])
    if sensitive:
        cc += [a for a in settings["sensitive_cc"] if a not in to and a not in cc]
    unknown = []
    for address in to + cc:
        if address in settings["agency"]:
            continue
        match = book.match(address)
        if match is None or match["brand_key"] != sender["brand_key"]:
            unknown.append(address)
    return to, cc, unknown


def not_direct(to, cc, book, settings):
    """Client recipients Hamle may not write to by itself (Markalar: 'doğrudan yazabilir mi' = Hayır,
    or only a company-domain match): the reply waits for an approver."""
    out = []
    for address in to + cc:
        match = None if address in settings["agency"] else book.match(address)
        if match is not None and match.get("direct") is not True:
            out.append(match.get("name") or address)
    return out


def fixed_body(fixed):
    return f"Merhaba,\n\n{fixed}\n\n{SIGNATURES[0]}"


# In a thread Hamle answered itself and opened the Basecamp job for (Ahmet 13 Oct), the client's later mails are
# answered too, at most this many times; the team's older threads stay with the team.
OWN_THREAD_MAX_REPLIES = 5


def check(draft, source, sender, settings, *, thread_replied, sent_today, unknown, not_direct=(), own_thread=False):
    """Reasons the draft may NOT go out by itself (empty list = auto-send)."""
    reasons = []
    if not draft.get("sensitive"):
        reply = str(draft.get("reply") or "")
        if draft.get("model_status") != "ok":
            reasons.append("taslak alınamadı")
        if draft.get("category") not in settings["auto_categories"]:
            reasons.append("mail türü kendi başıma cevaplayacağım türden değil")
        if "?" in reply and not settings["allow_questions"]:
            reasons.append("cevapta soru var")
        if draft.get("missing_info") and not settings["allow_questions"]:
            reasons.append("eksik bilgi var")
        if draft.get("internal_terms"):
            reasons.append("taslakta iç ifade var")
        confidence = draft.get("confidence")
        if not isinstance(confidence, int) or confidence < settings["auto_min_confidence"]:
            reasons.append(f"güvenim {confidence if isinstance(confidence, int) else '-'}/100")
        if not reply.lstrip().startswith(("Merhaba", "Hi", "Hello", "Dear")) or not reply.rstrip().endswith(
                tuple(s.split("\n", 1)[1] for s in SIGNATURES)):
            reasons.append("selamlama ya da imza eksik")
    if settings["new_threads_only"] and not own_thread and ONGOING.match(str(source.get("subject") or "")):
        reasons.append("devam eden bir yazışma; kendiliğinden yalnız yeni maillere cevap veriyorum")
    if unknown:
        reasons.append("tanımadığım alıcı var: " + ", ".join(unknown[:3]))
    if not_direct:
        reasons.append("bu kişilere onaysız yazmıyorum (Markalar: Hayır): " + ", ".join(not_direct[:3]))
    if thread_replied and not own_thread:
        reasons.append("bu zincire daha önce cevap verdim")
    if own_thread and int(thread_replied or 0) >= OWN_THREAD_MAX_REPLIES:
        reasons.append(f"bu zincire kendiliğinden {OWN_THREAD_MAX_REPLIES} kez cevap verdim")
    if sent_today >= settings["max_sends_per_day"]:
        reasons.append("günlük gönderim sınırı doldu")
    return reasons


def body_for(draft):
    return fixed_body(draft["reply"]) if draft.get("sensitive") else str(draft.get("reply") or "").strip()


# ------------------------------------------------------------------ approval comments
def comment_text(content):
    """Basecamp comment HTML -> plain text with line breaks kept (never rendered)."""
    text = re.sub(r"<br\s*/?>", "\n", content if isinstance(content, str) else "", flags=re.I)
    text = re.sub(r"</(?:div|p|li|h[1-6]|blockquote)>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]*>", "", text)).replace("\r\n", "\n").replace("\xa0", " ")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()[:8000]


def command(content):
    """('reject', None) | ('send', corrected_text_or_None) | None."""
    text = comment_text(content)
    if DONT_SEND.search(text):
        return ("reject", None)
    match = SEND.search(text)
    if not match:
        return None
    rest = text[match.end():].lstrip(" :.,-–\t").strip()
    return ("send", rest if len(rest) >= MIN_EDIT_CHARS else None)


# ------------------------------------------------------------------ Gmail writer
def client_reply_writer(book, settings, *, token_context, session_factory):
    """GmailThreadReplyWriter for kind 'client_reply': recipients are the client table + agency."""
    import business_writers as bw
    import cloud_providers as provider
    from v11_chain_mail import CLARIFY_FIELDS, HEX, GmailThreadReplyWriter

    class ClientReplyWriter(GmailThreadReplyWriter):
        def _shape(self, payload):
            _need(isinstance(payload, dict) and payload.get("from") == HAMLE and set(payload) == CLARIFY_FIELDS,
                  "client_reply_payload_invalid")
            _need(isinstance(payload["thread_id"], str) and HEX.fullmatch(payload["thread_id"])
                  and payload["thread_id"] not in provider.FORBIDDEN_THREADS
                  and isinstance(payload["in_reply_to_message_id"], str)
                  and HEX.fullmatch(payload["in_reply_to_message_id"]), "client_reply_anchor_invalid")
            bw._text(payload["subject"], 300, header=True)
            bw._text(payload["body"], 16000)
            to, cc = payload["to"], payload["cc"]
            _need(isinstance(to, list) and isinstance(cc, list) and to and len(to) + len(cc) <= 20
                  and len(set(to + cc)) == len(to + cc) and HAMLE not in to + cc
                  and all(isinstance(a, str) and (a in settings["agency"] or book.match(a) is not None)
                          for a in to + cc), "client_reply_recipient_unverified")
            return "client_reply"

        def _verify(self, kind, payload, src):
            to, cc = reply_all(src["sender"], src["to"], src["cc"])
            # same people in any order (13 Oct 18:24Z: To "esma, cansu" was planned sorted); added CC only Ahmet/Cansu
            extra = [a for a in payload["cc"] if a not in cc]
            _need(sorted(payload["to"]) == sorted(to) and set(cc) <= set(payload["cc"])
                  and all(a in settings["sensitive_cc"] for a in extra), "reply_recipients_changed")
            from v11_chain_mail import reply_subject
            _need(payload["subject"] == reply_subject(src["subject"]), "reply_subject_mismatch")

    return ClientReplyWriter({"contacts": []}, token_context=token_context, anchor_lookup=lambda tid: None,
                             session_factory=session_factory)
