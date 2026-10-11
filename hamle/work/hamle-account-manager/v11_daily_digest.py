"""Daily internal team digest mail (worker.py --mode daily_digest; NON-default).

Once per Istanbul weekday, at the first run at/after 09:15 (runtime_authority.
DAILY_DIGEST_WINDOW): read-only listing of OPEN Basecamp todos across the projects
Hamle's Basecamp identity can see, then ONE plain Turkish mail from Hamle to the
configured internal recipients (@gamechangers.com.tr only). No model call.

Durability (hamle_internal_v1, key daily-digest-YYYYMMDD):
  claimed (lease, nothing sent yet; reclaimable after the lease)
  -> sending (intent + payload digest recorded BEFORE the single POST)
  -> sent (Sent-copy readback verified) | unknown (never resent, human reconciles)
Gates: authorization deadline, global control review_open + generation (checked at
claim and again in the sending transaction), budget overrun hold, config enabled.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
import json
from pathlib import Path
import re
import uuid
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from autonomy_policy import HAMLE, scope_digest
from runtime_authority import DAILY_DIGEST_WINDOW, deadline_timestamp, start_timestamp, window_minutes

ISTANBUL = ZoneInfo("Europe/Istanbul")
CONFIG_NAME = "daily-digest-config.json"
INTERNAL = re.compile(r"[a-z0-9][a-z0-9._%+-]{0,63}@gamechangers\.com\.tr\Z")
API = "https://3.basecampapi.com"
LEASE_SECONDS = 600
WEEKDAYS = ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar")


class DigestError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise DigestError(code)


# ------------------------------------------------------------------ config
def load_config(path=None):
    root = Path(__file__).resolve().parents[2]
    candidates = [Path(path)] if path else [Path(__file__).with_name("chain-profile") / CONFIG_NAME,
                                            root / "outputs/gamechangers-internal" / CONFIG_NAME]
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise DigestError("digest_config_missing")


def validate_config(cfg):
    """Internal recipients only; any other domain refuses the whole config."""
    _need(isinstance(cfg, dict) and cfg.get("schema_version") == 1 and type(cfg.get("enabled")) is bool,
          "digest_config_invalid")
    recipients = cfg.get("recipients")
    _need(isinstance(recipients, list) and len(recipients) <= 20, "digest_recipients_invalid")
    for r in recipients:
        _need(isinstance(r, str) and INTERNAL.fullmatch(r) is not None and r != HAMLE,
              "digest_recipient_not_internal_agency")
    _need(len(set(recipients)) == len(recipients), "digest_recipients_duplicate")
    note = cfg.get("day_note")
    _need(note is None or (isinstance(note, str) and len(note) <= 2000
                           and not any(ord(c) < 32 and c not in "\n\t" for c in note)), "digest_day_note_invalid")
    _need(type(cfg.get("basecamp_account_id")) is int and cfg["basecamp_account_id"] > 0, "digest_account_invalid")
    return {"enabled": cfg["enabled"], "recipients": list(recipients),
            "day_note": note.strip() if isinstance(note, str) and note.strip() else None,
            "account_id": cfg["basecamp_account_id"]}


# ------------------------------------------------------------------ Basecamp read-only listing
class BasecampDigestReader:
    """GET-only, allowlisted paths, page query only, no redirects, bounded pagination.

    get(url) -> (payload, headers); production uses cloud_providers._session() with the
    existing basecamp_cloud_auth token (same transport rules as v11_basecamp_reader.SessionGet).
    """
    def __init__(self, account_id, *, get, max_projects=30, max_lists=80, max_pages=10, max_todos=2000):
        self.account, self.get = account_id, get
        self.max_projects, self.max_lists, self.max_pages, self.max_todos = max_projects, max_lists, max_pages, max_todos
        self.pages = 0

    def _check(self, url):
        p = urlsplit(url)
        a = str(self.account)
        _need(p.scheme == "https" and p.netloc == "3.basecampapi.com" and not p.fragment
              and re.fullmatch(rf"/{a}/(?:projects|buckets/[1-9][0-9]*/todosets/[1-9][0-9]*/todolists"
                               rf"|buckets/[1-9][0-9]*/todolists/[1-9][0-9]*/todos)\.json", p.path),
              "digest_url_out_of_scope")
        q = parse_qs(p.query, keep_blank_values=True)
        _need(not q or (set(q) == {"page"} and len(q["page"]) == 1 and re.fullmatch(r"[1-9][0-9]{0,3}", q["page"][0])),
              "digest_query_invalid")

    @staticmethod
    def _next(headers):
        link = {k.lower(): v for k, v in (headers or {}).items()}.get("link", "")
        _need(isinstance(link, str) and len(link) <= 4096, "digest_link_invalid")
        nexts = []
        for part in [x for x in link.split(",") if x.strip()]:
            m = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="([a-z ]+)"\s*', part)
            _need(m is not None, "digest_link_invalid")
            if "next" in m[2].split():
                nexts.append(m[1])
        _need(len(nexts) <= 1, "digest_link_ambiguous")
        return nexts[0] if nexts else None

    def _all(self, url):
        out, base = [], urlsplit(url).path
        for _ in range(self.max_pages):
            self._check(url)
            self.pages += 1
            _need(self.pages <= 400, "digest_read_budget_exceeded")
            payload, headers = self.get(url)
            _need(isinstance(payload, list), "digest_payload_invalid")
            out.extend(payload)
            url = self._next(headers)
            if url is None:
                return out
            _need(urlsplit(url).path == base, "digest_link_out_of_scope")
        raise DigestError("digest_pagination_unbounded")

    def collect(self):
        a = self.account
        projects = [p for p in self._all(f"{API}/{a}/projects.json")
                    if isinstance(p, dict) and p.get("status") == "active"]
        _need(len(projects) <= self.max_projects, "digest_too_many_projects")
        todos, lists = [], 0
        for project in projects:
            pid = project.get("id")
            _need(type(pid) is int and pid > 0, "digest_project_invalid")
            sets = [d for d in project.get("dock") or [] if isinstance(d, dict) and d.get("name") == "todoset"
                    and d.get("enabled") is True]
            for dock in sets:
                tid = dock.get("id")
                _need(type(tid) is int and tid > 0, "digest_todoset_invalid")
                for tl in self._all(f"{API}/{a}/buckets/{pid}/todosets/{tid}/todolists.json"):
                    lists += 1
                    _need(lists <= self.max_lists, "digest_too_many_lists")
                    lid = tl.get("id") if isinstance(tl, dict) else None
                    _need(type(lid) is int and lid > 0, "digest_list_invalid")
                    for t in self._all(f"{API}/{a}/buckets/{pid}/todolists/{lid}/todos.json"):
                        if not isinstance(t, dict) or t.get("completed") is not False or t.get("status") != "active":
                            continue
                        todos.append({"project": str(project.get("name") or pid)[:80],
                                      "title": str(t.get("title") or t.get("content") or "")[:120],
                                      "due_on": t.get("due_on") if isinstance(t.get("due_on"), str) else None,
                                      "assignees": sorted({str(x.get("name") or x.get("id"))[:60]
                                                           for x in t.get("assignees") or [] if isinstance(x, dict)})})
                        _need(len(todos) <= self.max_todos, "digest_too_many_todos")
        return {"projects": len(projects), "todos": todos}


# ------------------------------------------------------------------ text
def render(listing, *, day, day_note):
    todos = listing["todos"]
    today = day.isoformat()
    due = [t for t in todos if t["due_on"] == today]
    overdue = [t for t in todos if t["due_on"] and t["due_on"] < today]
    per = {}
    for t in todos:
        for name in t["assignees"] or ["(atanmamış)"]:
            per[name] = per.get(name, 0) + 1
    lines = [f"Günaydın,", "", f"{day.strftime('%d.%m.%Y')} {WEEKDAYS[day.weekday()]} iş özeti "
             f"(Hamle'nin üyesi olduğu {listing['projects']} Basecamp projesi):", "",
             f"Açık iş toplamı: {len(todos)}", f"Bugün teslim: {len(due)}", f"Gecikmiş: {len(overdue)}", "",
             "Kişi başına açık iş:"]
    lines += [f"- {name}: {n}" for name, n in sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))] or ["- yok"]
    if due:
        lines += ["", "Bugün teslim olanlar:"]
        lines += [f"- [{t['project']}] {t['title']} — {', '.join(t['assignees']) or 'atanmamış'}" for t in due[:40]]
    if day_note:
        lines += ["", "Günün notu:", day_note]
    lines += ["", "Bu özet Basecamp'ten salt-okunur alınmıştır; Hamle hiçbir işi değiştirmemiştir.", "", "Hamle"]
    return "\n".join(lines)


# ------------------------------------------------------------------ mail (new message, one POST, readback)
class DigestMailWriter:
    """Reuses v11_chain_mail's allowlisted Gmail REST call and the verified Gmail token."""
    def __init__(self, *, token_context, session_factory):
        self.token_context, self.session_factory = token_context, session_factory

    @staticmethod
    def message_id(payload):
        return f"<hamle-digest-{scope_digest(payload)}@gamechangers.com.tr>"

    def _raw(self, payload):
        m = EmailMessage(policy=policy.SMTP)
        m["From"] = HAMLE
        m["To"] = ", ".join(payload["to"])
        m["Subject"] = payload["subject"]
        m["Message-ID"] = self.message_id(payload)
        m.set_content(payload["body"], subtype="plain", charset="utf-8")
        return m.as_bytes()

    def write(self, payload):
        from v11_chain_mail import GMAIL, HEX, _call
        raw = self._raw(payload)
        with self.token_context() as token:
            session = self.session_factory()
            try:
                row = _call(session, "POST", GMAIL + "/messages/send", token,
                            body={"raw": base64.urlsafe_b64encode(raw).decode("ascii")})
            finally:
                session.close()
        _need(isinstance(row.get("id"), str) and HEX.fullmatch(row["id"]), "digest_receipt_unconfirmed")
        return {"remote_id": row["id"]}

    def readback(self, remote_id, payload):
        from v11_chain_mail import GMAIL, HEX, MAX_READ, _call
        _need(isinstance(remote_id, str) and HEX.fullmatch(remote_id), "digest_receipt_invalid")
        with self.token_context() as token:
            session = self.session_factory()
            try:
                row = _call(session, "GET", f"{GMAIL}/messages/{remote_id}?format=raw&fields=id%2ClabelIds%2Craw",
                            token, limit=MAX_READ)
            finally:
                session.close()
        _need(row.get("id") == remote_id and "SENT" in (row.get("labelIds") or [])
              and isinstance(row.get("raw"), str), "digest_readback_mismatch")
        raw = row["raw"]
        msg = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        to = [a.lower() for _, a in getaddresses([str(msg["To"] or "")])]
        _need(getaddresses([str(msg["From"])]) == [("", HAMLE)] and to == payload["to"] and not msg.get_all("Cc")
              and not msg.get_all("Bcc") and str(msg["Subject"]) == payload["subject"]
              and msg.get_content_type() == "text/plain"
              and msg.get_content().replace("\r\n", "\n").rstrip("\n") == payload["body"].rstrip("\n"),
              "digest_readback_mismatch")
        return {"readback_sha256": scope_digest(payload)}


# ------------------------------------------------------------------ run
def _day_key(local):
    return "daily-digest-" + local.strftime("%Y%m%d")


def run_daily_digest(ops, *, config, reader_factory, writer, clock, window=DAILY_DIGEST_WINDOW):
    """One digest per Istanbul weekday. Returns a status dict; never retries a send."""
    claim = None
    try:
        now = clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        _need(start_timestamp() <= now.timestamp() <= deadline_timestamp(), "authorization_expired")
        cfg = validate_config(config)
        if not cfg["enabled"] or not cfg["recipients"]:
            return {"status": "no_new_work", "reason": "digest_disabled_or_no_recipients", "business_writes": 0}
        local = now.astimezone(ISTANBUL)
        first, last = window_minutes(window)
        minute = local.hour * 60 + local.minute
        if local.weekday() >= 5 or not first <= minute <= last:
            return {"status": "no_new_work", "reason": "outside_digest_window", "business_writes": 0}
        key = _day_key(local)
        token = uuid.uuid4().hex

        def claim_op(tx, ts):
            c = ops._control(tx); b = ops._budget(tx); row = tx.get(key)
            ops._open(c, c["generation"], ts)
            _need(not b["overrun_hold"], "budget_overrun_hold")
            if row is not None and not (row["state"] == "claimed" and ts >= row["lease_until"]):
                return None  # claimed/sending/sent/unknown: never a second digest that day
            new = {"key": key, "kind": "daily_digest", "state": "claimed", "token": token, "generation": c["generation"],
                   "claimed_at": ts, "lease_until": ts + LEASE_SECONDS, "date": local.date().isoformat()}
            tx.put(key, new)
            return new
        claim = ops._run(claim_op)
        if claim is None:
            return {"status": "already_recorded", "reason": "digest_already_claimed_or_sent_today", "business_writes": 0}

        listing = reader_factory().collect()
        body = render(listing, day=local.date(), day_note=cfg["day_note"])
        payload = {"from": HAMLE, "to": cfg["recipients"], "cc": [],
                   "subject": f"Hamle günlük iş özeti — {local.strftime('%d.%m.%Y')}", "body": body}
        sha = scope_digest(payload)

        def begin_op(tx, ts):
            c = ops._control(tx); b = ops._budget(tx); row = tx.get(key)
            _need(row is not None and row["token"] == token and row["state"] == "claimed", "digest_claim_lost")
            ops._open(c, claim["generation"], ts)  # stop/generation change after claim -> no send
            _need(not b["overrun_hold"], "budget_overrun_hold")
            row.update(state="sending", payload_sha256=sha, recipients=list(payload["to"]),
                       subject=payload["subject"], open_todos=len(listing["todos"]), sending_at=ts)
            tx.put(key, row)
            return row
        ops._run(begin_op)

        def mark(state, **extra):
            def op(tx, ts):
                row = tx.get(key)
                _need(row is not None and row["token"] == token, "digest_claim_lost")
                row.update(state=state, updated_at=ts, **extra)
                tx.put(key, row)
                return row
            return ops._run(op, allow_expired=True)
        try:
            remote = writer.write(payload)["remote_id"]
            back = writer.readback(remote, payload)
            _need(back.get("readback_sha256") == sha, "digest_readback_mismatch")
        except Exception as exc:
            try:
                mark("unknown", reason=str(getattr(exc, "code", None) or type(exc).__name__)[:120])
            except Exception:
                pass
            return {"status": "held", "reason": "digest_outcome_unknown_no_retry", "business_writes": None,
                    "automatic_retry_allowed": False}
        mark("sent", remote_id=remote)
        return {"status": "digest_sent", "key": key, "recipients": len(payload["to"]),
                "open_todos": len(listing["todos"]), "business_writes": 1, "model_calls": 0}
    except Exception as exc:
        reason = re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]
        if claim is not None:
            # Nothing was sent (send failures return above). Release so a later run may retry.
            try:
                def release(tx, ts):
                    row = tx.get(claim["key"])
                    if row is not None and row["token"] == claim["token"] and row["state"] == "claimed":
                        row.update(lease_until=ts, last_failure=reason)
                        tx.put(claim["key"], row)
                ops._run(release, allow_expired=True)
            except Exception:
                pass
        return {"status": "held", "reason": reason, "business_writes": 0, "automatic_retry_allowed": False}


def run_worker_daily_digest(ops, secrets, *, clock=None):
    """Entry for worker.py --mode daily_digest (existing credential loaders only)."""
    import basecamp_cloud_auth
    import cloud_providers as provider
    from v11_chain_mail import gmail_token_context
    clock = clock or (lambda: datetime.now(timezone.utc))
    config = load_config()
    sessions = []

    def reader_factory():
        token = basecamp_cloud_auth.access_token(secrets, claim_refresh=ops.claim_credential_refresh)
        session = provider._session()
        sessions.append(session)
        def get(url):
            r = None
            try:
                r = session.request("GET", url, headers={"Authorization": "Bearer " + provider._token(token),
                                                          "User-Agent": "Hamle Daily Digest (ahmet@gamechangers.com.tr)",
                                                          "Accept": "application/json"},
                                    timeout=(5, 15), allow_redirects=False, stream=True)
                _need(r.status_code == 200, "basecamp_read_rejected")
                chunks, size = [], 0
                for chunk in r.iter_content(chunk_size=16384):
                    size += len(chunk)
                    _need(size <= 1_048_576, "basecamp_page_too_large")
                    chunks.append(chunk)
                return provider._json(b"".join(chunks)), dict(r.headers)
            except DigestError:
                raise
            except Exception:
                raise DigestError("basecamp_read_failed") from None
            finally:
                if r is not None:
                    r.close()
        return BasecampDigestReader(validate_config(config)["account_id"], get=get)

    writer = DigestMailWriter(token_context=gmail_token_context(secrets), session_factory=provider._session)
    try:
        return run_daily_digest(ops, config=config, reader_factory=reader_factory, writer=writer, clock=clock)
    finally:
        for s in sessions:
            s.close()
