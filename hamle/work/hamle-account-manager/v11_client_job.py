"""Client jobs end to end (Ahmet 2026-10-13 17:05Z): client mail -> brand Basecamp to-do -> visual check -> delivery.

"Hamle gelen mail'den işi alır, anlar, gerekli kontrollerden geçirir, Basecamp listelerinde ilgili to-do'yu
açar, orada işi takip eder ve oradan gelen işi müşteriye gönderir."

Runs inside v11_client_watch (mode 'reply', brands in the config's 'jobs' block):
1. A new client mail that the draft model marks job_ready opens one to-do per job in the brand's Basecamp
   project: social media work in the 'social' list, everything else in 'project' (never the motion list).
   The assignee comes from the config per list (WAT: Berkay for social, Cansu for project), followers are
   subscribed. The due date is the client's own date, else none. A later client mail in the same thread
   becomes a comment on the job to-dos (a revision after delivery reopens the job).
2. Every run, open jobs are read. A new comment from an agency person with an image or PDF is the work to
   check: Claude Opus scores it against the brief (once per upload).
   - score >= send_min_score (90): Hamle mails the files to the client in the original thread (reply-all of
     the newest incoming mail, exact To/CC) and notes it on the to-do.
   - approve_min_score..89: the approvers (Özge, Cansu, Ahmet) are asked on the to-do; "uygun" or
     "Hamle gönder" from one of them sends, "Hamle gönderme" leaves the delivery to the team. A revision
     they send to the designer is followed like any new upload.
   - below 80: Hamle writes the designer a reasoned revision request (max_revisions rounds), then hands the
     decision to the approvers.
   Files Hamle cannot score (video, large PDF) or a failed model call go to the approvers the same way.
3. A completed to-do closes the job. Hamle never changes dates or closes to-dos.
4. Jobs with no client mail (Ahmet 14 Oct: most client work comes by WhatsApp): the team writes the job on a
   brand to-do and tags Hamle (v11_basecamp_mentions, action 'follow'). The job is followed the same way; the
   team's comments are its client notes, and a work that passes (90+ or an approver's "uygun") is marked ready
   on the to-do for the team to send on WhatsApp (there is no thread to mail).

Same rules as client replies: durable record before every provider write, one attempt, exact readback,
anything uncertain is 'unknown' and never retried; global stop ('dur') fences every write; the day's send
limit is shared with client replies; recipients are only the client table's addresses of the same brand
(direct) and verified agency people.
"""
from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import io
import json
import re
import unicodedata
from datetime import date, datetime
from email import policy
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from autonomy_policy import HAMLE, scope_digest

REVIEW_PROMPT = Path(__file__).with_name("client_job_review_prompt.txt")
ROLE = "customer_text"
JOBS_KEY = "client-jobs"
LISTS = ("social", "project")
JOB_FIELDS = {"enabled", "brands", "send_min_score", "approve_min_score", "max_revisions", "max_scorings_per_run",
              "max_mail_mb"}
BRAND_FIELDS = {"title_prefix", "assignees", "followers", "approvers"}
FOLLOW_STATES = ("open", "awaiting_approval", "delivered")
MAX_JOBS_PER_MAIL = 3
MAX_FILES = 6
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 10
IMAGE_SIDE = 1568
FOLLOW_DAYS = 21
MAX_FAILURES = 8
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
SCORE_TYPES = IMAGE_TYPES + ("application/pdf",)
APPROVE = re.compile(r"\A\W*(?:uygun(?:dur)?|onayl[ıi](?:yorum)?|onay)(?![0-9a-zçğıöşü])", re.IGNORECASE)
NEGATION = re.compile(r"(?<![0-9a-zçğıöşü])(?:değil|degil|olmamış|olmadı|ama|fakat|ancak|revize|düzelt)",
                      re.IGNORECASE)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
GREETINGS = ("Merhaba", "Hi", "Hello", "Dear")


class ClientJobError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ClientJobError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


def _sha(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _line(value, limit=300):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def job_key(message_id, index):
    return "client-job-" + _sha(f"{message_id}|{index}")


def job_thread_key(thread_id):
    return "client-job-thread-" + _sha(thread_id)


def todo_job_key(todo_id):
    """A job with no client mail (WhatsApp, phone): the team wrote it on Basecamp and tagged Hamle."""
    return "client-job-bc-" + _sha(str(todo_id))


# ------------------------------------------------------------------ config
def agency_basecamp(catalog):
    """Verified agency people with a verified Basecamp id: email -> {'name', 'basecamp_id'} (any project)."""
    out = {}
    for row in catalog.get("contacts") or []:
        if (isinstance(row, dict) and row.get("verified") is True and row.get("side") == "agency"
                and row.get("basecamp_verified") is True and type(row.get("basecamp_id")) is int
                and isinstance(row.get("email"), str) and row["email"].endswith("@gamechangers.com.tr")
                and row["email"] != HAMLE):
            out[row["email"]] = {"name": row.get("name") or row["email"], "basecamp_id": row["basecamp_id"]}
    return out


def validate_jobs(block, catalog, reply):
    """Config 'jobs' block -> settings; None when absent or disabled."""
    if block is None:
        return None
    _need(isinstance(block, dict) and set(block) == JOB_FIELDS and type(block["enabled"]) is bool,
          "client_jobs_config_invalid")
    if not block["enabled"]:
        return None
    _need(reply is not None, "client_jobs_need_reply_mode")
    import v11_brand_routing as routing
    people = agency_basecamp(catalog)

    def person(email):
        _need(isinstance(email, str) and email in people, "client_jobs_person_unverified")
        return people[email]["basecamp_id"]
    for key, low, high in (("send_min_score", 80, 101), ("approve_min_score", 50, 100), ("max_revisions", 0, 3),
                           ("max_scorings_per_run", 1, 3), ("max_mail_mb", 1, 22)):
        _need(type(block[key]) is int and low <= block[key] <= high, "client_jobs_config_invalid")
    _need(block["approve_min_score"] <= block["send_min_score"], "client_jobs_config_invalid")
    brands = block["brands"]
    _need(isinstance(brands, dict) and 1 <= len(brands) <= 10, "client_jobs_brands_invalid")
    allowed = routing.allowlist(catalog)
    out = {}
    for key, row in brands.items():
        _need(key in reply["brands"] and isinstance(row, dict) and BRAND_FIELDS <= set(row) <= BRAND_FIELDS | {"when_done"},
              "client_jobs_brands_invalid")
        route = (catalog.get("brand_routes") or {}).get(key)
        _need(isinstance(route, dict) and routing.route_problem(catalog, key) is None
              and route.get("lists_visible_to_clients") is False, "client_jobs_route_unusable")
        lists = {}
        for name in LISTS:
            list_id = (route.get("lists") or {}).get(name)
            _need(type(list_id) is int and list_id in allowed.get(route["project_id"], ()), "client_jobs_route_unusable")
            lists[name] = list_id
        prefix = row["title_prefix"]
        _need(isinstance(prefix, str) and len(prefix) <= 20, "client_jobs_brands_invalid")
        assignees = row["assignees"]
        _need(isinstance(assignees, dict) and set(assignees) == set(LISTS), "client_jobs_brands_invalid")
        followers, approvers = row["followers"], row["approvers"]
        _need(isinstance(followers, list) and len(followers) <= 3 and len(set(followers)) == len(followers)
              and isinstance(approvers, list) and 1 <= len(approvers) <= 3 and len(set(approvers)) == len(approvers),
              "client_jobs_brands_invalid")
        # Basecamp "When done" people on every job to-do (Ahmet 13 Oct: Ahmet, Cansu, Özge, Barış for the demo week)
        when_done = row.get("when_done", [])
        _need(isinstance(when_done, list) and len(when_done) <= 6 and len(set(when_done)) == len(when_done),
              "client_jobs_brands_invalid")
        out[key] = {"name": route.get("brand_name") or key, "project_id": route["project_id"], "lists": lists,
                    "title_prefix": prefix,
                    "assignees": {n: {"id": person(e), "name": people[e]["name"]} for n, e in assignees.items()},
                    "followers": {person(e): people[e]["name"] for e in followers},
                    "approvers": {person(e): people[e]["name"] for e in approvers},
                    "when_done": sorted(person(e) for e in when_done)}
    return {"brands": out, "send_min_score": block["send_min_score"], "approve_min_score": block["approve_min_score"],
            "max_revisions": block["max_revisions"], "max_scorings_per_run": block["max_scorings_per_run"],
            "max_mail_bytes": block["max_mail_mb"] * 1024 * 1024,
            "agency_ids": {p["basecamp_id"]: p["name"] for p in people.values()},
            "account_id": catalog["basecamp"]["account_id"]}


# ------------------------------------------------------------------ draft fields (v11_client_watch.DRAFT_SCHEMA)
def job_items(draft):
    """The draft's jobs, cleaned: [{'title','list','brief','due_on'}] (due_on only a real future date)."""
    out = []
    for item in (draft.get("jobs") or [])[:MAX_JOBS_PER_MAIL]:
        if not isinstance(item, dict) or item.get("list") not in LISTS:
            continue
        title = re.sub(r"^\s*(?:[A-Za-z]{1,4}\s*//\s*)", "", _line(item.get("title"), 80)).strip()
        brief = str(item.get("brief") or "").strip()[:1500]
        if not title or not brief:
            continue
        due = item.get("due_on")
        try:
            due = date.fromisoformat(due).isoformat() if isinstance(due, str) and DATE.fullmatch(due) else None
        except ValueError:
            due = None
        out.append({"title": title, "list": item["list"], "brief": brief, "due_on": due})
    return out


def _when(ms):
    from v11_client_watch import ISTANBUL
    return datetime.fromtimestamp(ms / 1000, ISTANBUL).strftime("%d.%m.%Y %H:%M") if isinstance(ms, int) else "-"


def _tr_date(value):
    from v11_client_watch import MONTHS
    d = date.fromisoformat(value)
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def todo_title(brand, sender, item):
    return (brand["title_prefix"] + ("TEST – " if sender.get("test") else "") + item["title"])[:150]


def client_files(draft):
    """What the client's attachments are, as the draft model read them: [{'name', 'what'}] (Ahmet 14 Oct)."""
    return [{"name": _line(d.get("name"), 255), "what": _line(d.get("what"), 500)}
            for d in (draft or {}).get("attachments_read") or [] if isinstance(d, dict) and d.get("name")][:12]


def file_lines(files, unread=()):
    return (["- " + f["name"] + ": " + f["what"] for f in files]
            + ["- " + n + ": okunamadı, mailden açıp bakın" for n in unread])


def todo_description(brand, sender, source, item, settings, files=(), unread=()):
    """Short designer brief (Ahmet 13 Oct: no client mail text, no process paragraph) plus the client's files."""
    who = sender.get("name") or sender["email"]
    attached = (["", "Müşterinin mailindeki ekler (dosyalar hamle@ mailinde, aynı zincirde):"]
                + file_lines(files, unread) if files or unread else [])
    lines = (["TEST · müşteriye gitmez", ""] if sender.get("test") else []) + [
        item["brief"].strip()] + attached + ["",
        "Teslim: " + (_tr_date(item["due_on"]) if item["due_on"] else "müşteri tarih vermedi"),
        "Talep: " + who + " (" + brand["name"] + ") · " + _when(source.get("received_at_ms")),
        "Çalışmayı bu to-do'ya dosya olarak yükle; Hamle kontrol edip müşteriye iletir."]
    return "\n".join(lines)[:3900]


# ------------------------------------------------------------------ Basecamp: brand to-do writer
class JobTodoWriter:
    """One to-do in a brand list from the jobs config (client-hidden list), plus followers' subscription.

    write(payload) -> {'remote_id'}; readback(remote_id, payload) -> {'readback_sha256', 'app_url'};
    subscribe(payload, todo_id) -> sorted subscriber ids read back.
    """
    def __init__(self, catalog, settings, *, token_provider, session_factory=None):
        import cloud_providers as provider
        from v11_chain import BasecampCommentWriter
        self.settings = settings
        self._http = BasecampCommentWriter(catalog, token_provider=token_provider,
                                           session_factory=session_factory or provider._session)

    def _check(self, payload):
        done = payload.get("completion_subscriber_ids") if isinstance(payload, dict) else None
        _need(isinstance(done, list) and done == sorted(set(done)) and len(done) <= 6
              and all(i in self.settings["agency_ids"] for i in done), "client_job_payload_invalid")
        t = payload.get("target") if isinstance(payload, dict) else None
        brand = next((b for b in self.settings["brands"].values()
                      if isinstance(t, dict) and b["project_id"] == t.get("project_id")), None)
        _need(brand is not None and set(payload) == {"target", "title", "description", "assignee_ids", "due_on",
                                                         "completion_subscriber_ids"}
              and set(t) == {"account_id", "project_id", "list_id"} and t["account_id"] == self.settings["account_id"]
              and t["list_id"] in brand["lists"].values()
              and isinstance(payload["title"], str) and 0 < len(payload["title"]) <= 150
              and isinstance(payload["description"], str) and 0 < len(payload["description"]) <= 4000
              and isinstance(payload["assignee_ids"], list) and len(payload["assignee_ids"]) == 1
              and payload["assignee_ids"][0] in self.settings["agency_ids"]
              and (payload["due_on"] is None or (isinstance(payload["due_on"], str) and DATE.fullmatch(payload["due_on"]))),
              "client_job_payload_invalid")
        return f"{self._http.ORIGIN}/{t['account_id']}/buckets/{t['project_id']}"

    def write(self, payload):
        import business_writers as bw
        base = self._check(payload)
        body = {"content": payload["title"], "description": bw._description(payload["description"]),
                "assignee_ids": list(payload["assignee_ids"]),
                "completion_subscriber_ids": list(payload["completion_subscriber_ids"]), "notify": True}
        if payload["due_on"]:
            body["due_on"] = payload["due_on"]
        row = self._http._request("POST", f"{base}/todolists/{payload['target']['list_id']}/todos.json", body,
                                  expected=201)
        _need(type(row.get("id")) is int and row["id"] > 0, "client_job_receipt_unconfirmed")
        return {"remote_id": str(row["id"])}

    def readback(self, remote_id, payload):
        import business_writers as bw
        from v11_brand_task import HAMLE_PERSON
        base = self._check(payload)
        _need(isinstance(remote_id, str) and re.fullmatch(r"[0-9]{1,30}", remote_id), "client_job_receipt_invalid")
        row = self._http._request("GET", f"{base}/todos/{remote_id}.json")
        parent = row.get("parent") or {}
        url = row.get("app_url")
        _need(row.get("id") == int(remote_id) and (row.get("bucket") or {}).get("id") == payload["target"]["project_id"]
              and parent.get("id") == payload["target"]["list_id"] and parent.get("type") == "Todolist"
              and (row.get("creator") or {}).get("id") == HAMLE_PERSON and row.get("visible_to_clients") is False
              and row.get("content") == payload["title"] and row.get("completed") is False
              and sorted(a.get("id") for a in row.get("assignees") or [] if isinstance(a, dict))
              == payload["assignee_ids"] and (row.get("due_on") or None) == payload["due_on"]
              and sorted(p.get("id") for p in row.get("completion_subscribers") or [] if isinstance(p, dict))
              == payload["completion_subscriber_ids"]
              and bw._description_matches(row.get("description"), payload["description"])
              # the live app_url is https://app.basecamp.com/... (2026-10-13 18:04Z readback), older rows 3.basecamp.com
              and isinstance(url, str) and url.startswith(tuple(f"https://{host}/{payload['target']['account_id']}/"
                                                                for host in ("3.basecamp.com", "app.basecamp.com"))),
              "client_job_readback_mismatch")
        return {"readback_sha256": scope_digest(payload), "app_url": url}

    def subscribe(self, payload, todo_id, ids):
        base = self._check(payload)
        _need(type(todo_id) is int and todo_id > 0 and isinstance(ids, list) and 1 <= len(ids) <= 3
              and all(i in self.settings["agency_ids"] for i in ids), "client_job_subscription_invalid")
        row = self._http._request("PUT", f"{base}/recordings/{todo_id}/subscription.json", {"subscriptions": ids})
        got = {s.get("id") for s in row.get("subscribers") or [] if isinstance(s, dict)}
        _need(row.get("subscribed") in (True, False) and set(ids) <= got, "client_job_subscription_unconfirmed")
        return sorted(set(ids))


# ------------------------------------------------------------------ Basecamp: attachment download
def basecamp_downloader(account_id, *, token_provider, session_factory=None):
    """download(url, byte_size) -> bytes: one Basecamp GET, at most one storage redirect (no token on it)."""
    import cloud_providers as provider
    factory = session_factory or provider._session

    def download(url, byte_size):
        p = urlsplit(url if isinstance(url, str) else "")
        _need(p.scheme == "https" and p.netloc in ("3.basecampapi.com", "storage.3.basecamp.com",
                                                    "storage.app.basecamp.com")
              and p.path.startswith(f"/{account_id}/blobs/") and "/download/" in p.path and not p.fragment,
              "client_job_download_url_invalid")
        _need(byte_size is None or (type(byte_size) is int and 0 < byte_size <= MAX_FILE_BYTES),
              "client_job_file_too_large")
        limit = MAX_FILE_BYTES if byte_size is None else byte_size
        session, response = factory(), None
        try:
            agent = "Hamle Client Jobs (ahmet@gamechangers.com.tr)"
            response = session.request("GET", url, headers={"Authorization": "Bearer " + provider._token(token_provider()),
                                                            "User-Agent": agent},
                                       timeout=(10, 60), allow_redirects=False, stream=True)
            if response.status_code == 302:
                location = urlsplit(response.headers.get("Location", ""))
                _need(location.scheme == "https" and location.netloc == "storage.basecamp.com" and not location.fragment,
                      "client_job_redirect_forbidden")
                response.close()
                response = session.request("GET", location.geturl(), headers={"User-Agent": agent},
                                           timeout=(10, 60), allow_redirects=False, stream=True)
            _need(response.status_code == 200, "client_job_download_failed")
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                _need(total <= limit, "client_job_download_size_mismatch")
                chunks.append(chunk)
            data = b"".join(chunks)
            _need(len(data) > 0 and (byte_size is None or len(data) == byte_size), "client_job_download_size_mismatch")
            return data
        except ClientJobError:
            raise
        except Exception:
            raise ClientJobError("client_job_download_failed") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            session.close()
    return download


class _Attachments(HTMLParser):
    """<bc-attachment> tags of a rich-text comment (when the API row has no content_attachments)."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []

    def handle_starttag(self, tag, attrs):
        if tag == "bc-attachment":
            self.rows.append(dict(attrs))


def upload_files(comment):
    """Attachments of a Basecamp comment: [{'filename','content_type','byte_size'|None,'download_url'}]."""
    out = []
    rows = comment.get("content_attachments")
    if not isinstance(rows, list) or not rows:
        parser = _Attachments()
        try:
            parser.feed(comment.get("content") if isinstance(comment.get("content"), str) else "")
            parser.close()
        except Exception:
            return []
        rows = []
        for a in parser.rows:
            url = next((a[k] for k in ("href", "url") if isinstance(a.get(k), str) and "/download/" in a[k]), None)
            size = a.get("filesize")
            rows.append({"filename": a.get("filename"), "content_type": a.get("content-type"), "download_url": url,
                         "byte_size": int(size) if isinstance(size, str) and size.isdigit() else None})
    for a in rows:
        if (isinstance(a, dict) and isinstance(a.get("filename"), str) and 0 < len(a["filename"]) <= 255
                and isinstance(a.get("content_type"), str)
                and (a.get("byte_size") is None or (type(a["byte_size"]) is int and a["byte_size"] > 0))
                and isinstance(a.get("download_url"), str)):
            out.append({"filename": a["filename"], "content_type": a["content_type"].split(";")[0].strip().lower(),
                        "byte_size": a.get("byte_size"), "download_url": a["download_url"]})
    return out[:MAX_FILES]


def safe_filename(name):
    stem = unicodedata.normalize("NFKD", str(name).translate(str.maketrans("ıİşŞğĞçÇöÖüÜ", "iIsSgGcCoOuU")))
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem.encode("ascii", "ignore").decode()).strip("._") or "dosya"
    return stem[-100:]


# ------------------------------------------------------------------ files -> model blocks
def model_files(files):
    """[(meta, data)] -> (blocks, info, unscored names). Images are downscaled JPEG copies (model only)."""
    blocks, info, skipped = [], [], []
    for meta, data in files:
        kind = meta["content_type"]
        try:
            if kind in IMAGE_TYPES:
                from PIL import Image, ImageOps
                Image.MAX_IMAGE_PIXELS = 80_000_000
                with Image.open(io.BytesIO(data)) as im:
                    im.load()
                    im = ImageOps.exif_transpose(im)
                    width, height = im.size
                    if im.mode in ("RGBA", "LA", "P"):
                        rgba = im.convert("RGBA")
                        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
                        canvas.paste(rgba, mask=rgba.split()[-1])
                        im = canvas
                    elif im.mode != "RGB":
                        im = im.convert("RGB")
                    im.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
                    out = io.BytesIO()
                    im.save(out, "JPEG", quality=88)
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                           "data": base64.b64encode(out.getvalue()).decode("ascii")}})
                info.append({"name": meta["filename"], "type": kind, "width": width, "height": height,
                             "ratio": _ratio(width, height)})
            elif kind == "application/pdf" and len(data) <= MAX_PDF_BYTES:
                from pypdf import PdfReader
                pages = len(PdfReader(io.BytesIO(data)).pages)
                if not 1 <= pages <= MAX_PDF_PAGES:
                    skipped.append(meta["filename"])
                    continue
                blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                                              "data": base64.b64encode(data).decode("ascii")}})
                info.append({"name": meta["filename"], "type": kind, "pages": pages})
            else:
                skipped.append(meta["filename"])
        except Exception:
            skipped.append(meta["filename"])
    return blocks, info, skipped


def _ratio(w, h):
    from math import gcd
    g = gcd(w, h) or 1
    return f"{w // g}:{h // g}"


def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _str(maximum):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


REVIEW_SCHEMA = _obj({
    "score": {"type": "integer", "minimum": 0, "maximum": 100},
    "summary": _str(400),
    "missing": {"type": "array", "items": _str(240), "maxItems": 6},
    "designer_note": _str(1500),
    "client_mail": _str(1500)})


def review_source(ops, load_key, catalog, *, routing=None, client=None):
    """review(job, info, blocks, digest_parts) -> validated REVIEW_SCHEMA output; ModelGate once per upload."""
    import model as base
    import model_routing
    from v11_chain_live import budget_gate
    from v11_chain_models import ModelGate, internal_cap_microdollars, key_for, reservation_for, source_digest, validate
    from v11_trend_search import route_fingerprint
    route = (routing if routing is not None else model_routing.load()).route(ROLE)
    _need(route.provider == "anthropic", "client_job_route_not_claude")
    route = dataclasses.replace(route, effort="medium", max_tokens=min(route.max_tokens, 8000),
                                timeout_seconds=min(route.timeout_seconds, 240))
    cap = internal_cap_microdollars(catalog)
    hold = reservation_for(route)

    def review(request, blocks, parts):
        digest = source_digest("client_job_review", *parts, route_fingerprint(route, REVIEW_PROMPT))
        gate = ModelGate(ledger=ops, cache=ops, generation=ops.control()["generation"], cap_microdollars=cap)
        text = json.dumps(request, ensure_ascii=False, sort_keys=True)
        _need(len(text.encode("utf-8")) <= base.MAX_INPUT_BYTES, "client_job_review_input_too_large")

        def call():
            try:
                system = REVIEW_PROMPT.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                raise ClientJobError("client_job_prompt_unavailable") from None
            kwargs = base.anthropic_request(route, system, text, schema=REVIEW_SCHEMA)
            kwargs["messages"] = [{"role": "user", "content": list(blocks) + [{"type": "text", "text": text}]}]
            key = key_for(load_key, "anthropic")
            try:
                data = base.anthropic_send(key, route, kwargs, client=client)
            finally:
                del key
            result = base.anthropic_extract(route, data, structured=True)
            try:
                value = base._loads(result["text"])
                validate(value, REVIEW_SCHEMA)
            except Exception:
                raise base._anthropic_error("invalid_analysis_schema", data, route) from None
            return {"output": value, "receipt": result["receipt"]}
        return gate.once("client_job_review", digest, call, provider="anthropic", hold=hold)
    review.budget_ok = budget_gate(ops, hold, cap)
    return review


# ------------------------------------------------------------------ approval comments
def approval(content):
    """'send' | 'reject' | None from an approver's comment ("uygun", "Hamle gönder", "Hamle gönderme")."""
    import v11_client_reply as cr
    text = cr.comment_text(content)
    verb = cr.command(content)
    if verb is not None:
        return verb[0]
    if APPROVE.match(text) and not NEGATION.search(text):
        return "send"
    return None


# ------------------------------------------------------------------ client mail text
def client_mail(review, job):
    """The model's delivery mail when it is clean, else a short fixed one."""
    from v11_client_reply import SIGNATURES
    from v11_client_watch import internal_terms
    text = str((review or {}).get("client_mail") or "").strip()
    ok = (text.startswith(GREETINGS) and text.endswith(tuple(s.split("\n", 1)[1] for s in SIGNATURES))
          and not internal_terms(text) and not re.search(r"\b\d{1,3}\s*/\s*100\b|puan", text, re.I))
    if ok:
        return text
    first = (job.get("client_name") or "").split(" ")[0].strip("()")
    return (f"Merhaba{(' ' + first) if first else ''},\n\n{job['title_core']} için hazırladığımız çalışmayı ekte "
            f"paylaşıyoruz. Görüşlerinizi bekliyoruz.\n\n{SIGNATURES[0]}")


# ------------------------------------------------------------------ Gmail delivery writer
UPLOAD_SEND = "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send?uploadType=multipart"
JSON_SEND_LIMIT = 3_500_000
DELIVER_FIELDS = {"from", "to", "cc", "thread_id", "in_reply_to_message_id", "subject", "body", "files"}
NOT_SENT = ("newer_incoming_mail_in_thread", "reply_recipients_changed", "reply_subject_mismatch",
            "hamle_not_actual_to_or_cc", "mail_anchor_thread_mismatch", "mail_anchor_not_incoming",
            "mail_anchor_sender_invalid", "mail_anchor_rfc_id_invalid", "mail_anchor_references_invalid",
            "mail_headers_invalid", "mail_headers_ambiguous", "mail_thread_snapshot_invalid",
            "client_delivery_payload_invalid", "client_delivery_recipient_unverified", "client_delivery_files_invalid",
            "client_delivery_bytes_mismatch", "mail_payload_too_large", "mail_thread_invalid", "action_text_invalid",
            "action_header_invalid")


def delivery_writer(book, reply, *, token_context, session_factory):
    """Same-thread Gmail reply with the job's files attached. Recipients: client table (same brand) + agency.

    anchor(thread_id) -> newest incoming message of the thread with its reply-all recipients (read only);
    write(payload, data) -> {'remote_id'} (one POST); readback(remote_id, payload) -> {'readback_sha256'}.
    """
    import cloud_providers as provider
    import business_writers as bw
    from v11_chain_mail import (GMAIL, HEX, MAX_RAW, GmailThreadReplyWriter, _call, mail_html, reply_all,
                                reply_subject)
    from v11_client_reply import not_direct, recipients

    def _send_upload(session, token, raw, thread_id):
        boundary = "hamle_" + hashlib.sha256(raw).hexdigest()[:24]
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                + json.dumps({"threadId": thread_id}) + f"\r\n--{boundary}\r\nContent-Type: message/rfc822\r\n\r\n"
                ).encode("utf-8") + raw + f"\r\n--{boundary}--\r\n".encode("ascii")
        response = None
        try:
            response = session.request("POST", UPLOAD_SEND, headers={
                "Authorization": "Bearer " + provider._token(token),
                "Content-Type": f"multipart/related; boundary={boundary}",
                "User-Agent": "Hamle Client Jobs (ahmet@gamechangers.com.tr)"},
                data=body, timeout=(10, 120), allow_redirects=False, stream=True)
            _need(response.status_code == 200, "provider_response_unconfirmed")
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=16384):
                total += len(chunk)
                _need(total <= 262144, "provider_response_too_large")
                chunks.append(chunk)
            result = provider._json(b"".join(chunks))
            _need(isinstance(result, dict), "provider_response_invalid")
            return result
        except ClientJobError:
            raise
        except Exception:
            raise ClientJobError("provider_outcome_unknown_no_retry") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    def _get_full(session, token, message_id):
        _need(isinstance(message_id, str) and HEX.fullmatch(message_id), "mail_receipt_invalid")
        url = f"{GMAIL}/messages/{message_id}?format=full&fields=id%2CthreadId%2ClabelIds%2Cpayload"
        response = None
        try:
            response = session.request("GET", url, headers={
                "Authorization": "Bearer " + provider._token(token),
                "User-Agent": "Hamle Client Jobs (ahmet@gamechangers.com.tr)"},
                timeout=(10, 30), allow_redirects=False, stream=True)
            _need(response.status_code == 200, "provider_response_unconfirmed")
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=16384):
                total += len(chunk)
                _need(total <= 4 * 1024 * 1024, "provider_response_too_large")
                chunks.append(chunk)
            result = provider._json(b"".join(chunks))
            _need(isinstance(result, dict), "provider_response_invalid")
            return result
        except ClientJobError:
            raise
        except Exception:
            raise ClientJobError("provider_outcome_unknown_no_retry") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    class DeliveryWriter(GmailThreadReplyWriter):
        def _who(self, brand_key, to, cc):
            unknown = [a for a in to + cc if a not in reply["agency"]
                       and ((book.match(a) or {}).get("brand_key") != brand_key)]
            clients = [a for a in to + cc if a not in reply["agency"] and a not in unknown]
            return unknown, clients, not_direct(to, cc, book, reply)

        def anchor(self, thread_id, brand_key):
            _need(isinstance(thread_id, str) and HEX.fullmatch(thread_id)
                  and thread_id not in provider.FORBIDDEN_THREADS, "mail_thread_invalid")
            with self.token_context() as token:
                session = self.session_factory()
                try:
                    latest = self._latest_incoming(session, token, thread_id)
                    src = self._source(session, token, thread_id, latest)
                finally:
                    session.close()
            to, cc = reply_all(src["sender"], src["to"], src["cc"])
            unknown, clients, blocked = self._who(brand_key, to, cc)
            return {"anchor": latest, "to": to, "cc": cc, "subject": reply_subject(src["subject"]),
                    "unknown": unknown, "clients": clients, "not_direct": blocked}

        def _payload(self, payload, data):
            _need(isinstance(payload, dict) and set(payload) == DELIVER_FIELDS and payload["from"] == HAMLE
                  and isinstance(payload["thread_id"], str) and HEX.fullmatch(payload["thread_id"])
                  and payload["thread_id"] not in provider.FORBIDDEN_THREADS
                  and isinstance(payload["in_reply_to_message_id"], str)
                  and HEX.fullmatch(payload["in_reply_to_message_id"]), "client_delivery_payload_invalid")
            bw._text(payload["subject"], 300, header=True)
            bw._text(payload["body"], 16000)
            to, cc = payload["to"], payload["cc"]
            _need(isinstance(to, list) and isinstance(cc, list) and to and len(to) + len(cc) <= 20
                  and len(set(to + cc)) == len(to + cc) and HAMLE not in to + cc
                  and all(isinstance(a, str) and (a in reply["agency"] or book.match(a) is not None) for a in to + cc),
                  "client_delivery_recipient_unverified")
            files = payload["files"]
            _need(isinstance(files, list) and 1 <= len(files) <= MAX_FILES, "client_delivery_files_invalid")
            if data is not None:
                _need(isinstance(data, list) and len(data) == len(files)
                      and all(type(d) is bytes and len(d) == f["byte_size"] and hashlib.sha256(d).hexdigest() == f["sha256"]
                              for f, d in zip(files, data)), "client_delivery_bytes_mismatch")
            return files

        def _mime(self, payload, src, data):
            message = EmailMessage(policy=policy.SMTP)
            message["From"] = HAMLE
            message["To"] = ", ".join(payload["to"])
            if payload["cc"]:
                message["Cc"] = ", ".join(payload["cc"])
            message["Subject"] = payload["subject"]
            message["In-Reply-To"] = src["rfc_message_id"]
            message["References"] = src["references"]
            message["Message-ID"] = self._message_id(payload, src)
            message.set_content(payload["body"], subtype="plain", charset="utf-8")
            message.add_alternative(mail_html(payload["body"]), subtype="html", charset="utf-8")
            for f, d in zip(payload["files"], data):
                main, sub = f["content_type"].split("/", 1)
                message.add_attachment(d, maintype=main, subtype=sub, filename=f["filename"])
            return message.as_bytes()

        def write(self, payload, data):
            self._payload(payload, data)
            with self.token_context() as token:
                session = self.session_factory()
                try:
                    src = self._source(session, token, payload["thread_id"], payload["in_reply_to_message_id"])
                    _need(self._latest_incoming(session, token, payload["thread_id"]) == payload["in_reply_to_message_id"],
                          "newer_incoming_mail_in_thread")
                    to, cc = reply_all(src["sender"], src["to"], src["cc"])
                    _need(sorted(payload["to"]) == sorted(to) and sorted(payload["cc"]) == sorted(cc),
                          "reply_recipients_changed")
                    _need(payload["subject"] == reply_subject(src["subject"]), "reply_subject_mismatch")
                    raw = self._mime(payload, src, data)
                    _need(len(raw) <= 30 * 1024 * 1024, "mail_payload_too_large")
                    # Exactly one POST; anything after it is ambiguous for the caller.
                    if len(raw) <= min(JSON_SEND_LIMIT, MAX_RAW):
                        row = _call(session, "POST", GMAIL + "/messages/send", token,
                                    body={"threadId": payload["thread_id"],
                                          "raw": base64.urlsafe_b64encode(raw).decode("ascii")})
                    else:
                        row = _send_upload(session, token, raw, payload["thread_id"])
                finally:
                    session.close()
            mid = row.get("id")
            _need(isinstance(mid, str) and HEX.fullmatch(mid) and row.get("threadId") == payload["thread_id"],
                  "mail_receipt_unconfirmed")
            return {"remote_id": mid}

        def readback(self, remote_id, payload):
            files = self._payload(payload, None)
            with self.token_context() as token:
                session = self.session_factory()
                try:
                    src = self._source(session, token, payload["thread_id"], payload["in_reply_to_message_id"])
                    row = _get_full(session, token, remote_id)
                finally:
                    session.close()
            _need(row.get("id") == remote_id and row.get("threadId") == payload["thread_id"]
                  and "SENT" in (row.get("labelIds") or []), "mail_readback_receipt_mismatch")
            top = row.get("payload") or {}
            h = self._headers({"payload": top}, ("From", "To", "Cc", "Subject", "In-Reply-To", "Bcc"))
            from email.utils import getaddresses
            got_to = [a.lower() for _, a in getaddresses([h["To"] or ""])]
            got_cc = [a.lower() for _, a in getaddresses([h["Cc"] or ""])] if h["Cc"] else []
            _need(getaddresses([h["From"] or ""]) == [("", HAMLE)] and got_to == payload["to"] and got_cc == payload["cc"]
                  and not h["Bcc"] and h["Subject"] == payload["subject"] and h["In-Reply-To"] == src["rfc_message_id"],
                  "mail_readback_header_mismatch")
            parts, stack = [], [top]
            while stack:
                part = stack.pop()
                _need(isinstance(part, dict) and len(parts) <= 50, "mail_readback_invalid")
                if part.get("filename"):
                    parts.append((part["filename"], part.get("mimeType"), (part.get("body") or {}).get("size")))
                stack.extend(part.get("parts") or [])
            want = sorted((f["filename"], f["content_type"]) for f in files)
            _need(sorted((n, m) for n, m, _ in parts) == want, "mail_readback_attachment_mismatch")
            sizes = {n: s for n, _, s in parts}
            _need(all(type(sizes.get(f["filename"])) is int
                      and abs(sizes[f["filename"]] - f["byte_size"]) <= max(16, f["byte_size"] // 100) for f in files),
                  "mail_readback_attachment_mismatch")
            return {"readback_sha256": scope_digest(payload)}

    return DeliveryWriter({"contacts": []}, token_context=token_context, anchor_lookup=lambda tid: None,
                          session_factory=session_factory)


# ------------------------------------------------------------------ the desk
class JobDesk:
    """Opens and follows client jobs for ClientWatch (same ops store, fence and day counters)."""
    def __init__(self, watch, settings, *, todo_writer, comment_writer, basecamp_get, download, review,
                 delivery):
        self.watch, self.ops, self.s = watch, watch.ops, settings
        self.todo_writer, self.comment_writer, self.get = todo_writer, comment_writer, basecamp_get
        self.download, self.review, self.delivery = download, review, delivery

    # ----- store helpers
    def _get(self, key):
        return self.ops._run(lambda tx, _: tx.get(key))

    def _set(self, key, **fields):
        def op(tx, now):
            row = tx.get(key)
            _need(row is not None, "client_job_missing")
            row.update(updated_at=now, **fields)
            tx.put(key, row)
            return row
        return self.ops._run(op, allow_expired=True)

    def _index(self, add=(), drop=()):
        def op(tx, now):
            keys = [k for k in list((tx.get(JOBS_KEY) or {}).get("keys") or []) if k not in drop]
            tx.put(JOBS_KEY, {"kind": "client_jobs", "keys": list(dict.fromkeys(keys + list(add)))[-80:]})
        self.ops._run(op, allow_expired=True)

    @staticmethod
    def _work(job):
        return {"work_id": job["key"], "account_id": job["account_id"], "project_id": job["project_id"],
                "list_id": job["list_id"], "task_id": job["todo_id"]}

    def _comment(self, job, text, report):
        """One internal comment on the job to-do; one attempt, never retried. -> 'written' | reason."""
        payload = {"target": {"account_id": job["account_id"], "project_id": job["project_id"],
                              "list_id": job["list_id"], "task_id": job["todo_id"]},
                   "body": text[:6000], "mention_ids": [], "internal_only": True}

        def begin(tx, now):
            self.watch._fence(tx, now)
            row = tx.get(job["key"])
            _need(row is not None and int(row.get("comment_count") or 0) < 25, "client_job_comment_limit")
            row["comment_count"] = int(row.get("comment_count") or 0) + 1
            tx.put(job["key"], row)
        try:
            self.ops._run(begin)
        except Exception as exc:
            return "denied:" + _code(exc)
        try:
            report["business_writes"] += 1
            remote = self.comment_writer.write(copy.deepcopy(payload))["remote_id"]
            self.comment_writer.readback(remote, copy.deepcopy(payload))
        except Exception as exc:
            return "unknown:" + _code(exc)
        return "written"

    # ----- 1. from a client mail
    def from_mail(self, source, sender, draft, plan, report):
        """None (not a job mail) or {'kind': 'opened'|'followup', 'jobs': [...], 'comments': n} for the note."""
        brand = self.s["brands"].get(sender["brand_key"])
        if brand is None or plan is None:
            return None
        thread = self._get(job_thread_key(source["thread_id"]))
        mine = [k for k in (thread or {}).get("job_keys") or []]
        earlier = [self._get(k) for k in mine]
        earlier = [j for j in earlier if isinstance(j, dict) and j.get("message_id") != source["message_id"]]
        own = [self._get(k) for k in mine]
        own = [j for j in own if isinstance(j, dict) and j.get("message_id") == source["message_id"]]
        if own:  # re-entry after a released note: report what exists
            return {"kind": "opened", "jobs": [self._brief(j) for j in own], "comments": 0}
        if earlier:
            n = self._followup(earlier, source, sender, draft, report)
            return {"kind": "followup", "jobs": [self._brief(j) for j in earlier], "comments": n}
        items = job_items(draft)
        if (draft.get("sensitive") or draft.get("model_status") != "ok" or draft.get("job_ready") is not True
                or draft.get("category") not in ("request", "feedback_or_approval") or not items
                or not isinstance(draft.get("confidence"), int) or draft["confidence"] < self.s["approve_min_score"]):
            return None
        out = []
        for i, item in enumerate(items):
            out.append(self._open(i, item, brand, source, sender, report, draft=draft))
        return {"kind": "opened", "jobs": out, "comments": 0}

    # ----- 1b. a job the team wrote on Basecamp (WhatsApp, phone), Hamle tagged with 'follow'
    def todo_job(self, brand_key, todo, list_id, *, brief, files, request_text, followed_by, last_seen):
        """(job record, None) or (None, why in Turkish). No store write: MentionDesk writes it with its claim."""
        brand = self.s["brands"].get(brand_key)
        if brand is None:
            return None, "bu markada iş takibini henüz açmadık (şimdilik WAT Mobilite ve Rabam)"
        kind = next((n for n, i in brand["lists"].items() if i == list_id), None)
        if kind is None:
            return None, "işleri yalnız markanın Sosyal Medya ve Projeler listelerinde takip ediyorum"
        if todo.get("completed") is True:
            return None, "to-do tamamlanmış"
        people = [a.get("id") for a in todo.get("assignees") or [] if isinstance(a, dict)
                  and a.get("id") in self.s["agency_ids"]]
        default = brand["assignees"][kind]
        assignee_id = people[0] if people else default["id"]
        title = _line(todo.get("content") or todo.get("title"), 150) or brand["name"]
        core = re.sub(r"^[^/]{1,20}//\s*", "", title).strip() or title
        due = todo.get("due_on") if isinstance(todo.get("due_on"), str) and DATE.fullmatch(todo["due_on"]) else None
        return {"key": todo_job_key(todo["id"]), "kind": "client_job", "state": "open", "origin": "basecamp",
                "followed_by": followed_by, "brand_key": brand_key, "test": False, "message_id": None,
                "thread_id": None, "index": 0, "list": kind, "account_id": self.s["account_id"],
                "project_id": brand["project_id"], "list_id": list_id, "todo_id": todo["id"], "title": title,
                "title_core": core, "brief": str(brief).strip()[:1500], "client_text": str(request_text)[:1500],
                "due_on": due, "assignee_id": assignee_id,
                "assignee_name": self.s["agency_ids"].get(assignee_id) or default["name"], "client_name": None,
                "client_email": None, "client_notes": [], "client_files": list(files or [])[:12], "revisions": 0,
                "last_seen": int(last_seen or 0), "app_url": todo.get("app_url")}, None

    def _team_notes(self, job, fresh):
        """A WhatsApp job: the team's new plain comments carry the client's words; they join client_notes."""
        import v11_client_reply as cr
        seen = {n.get("id") for n in job.get("client_notes") or []}
        new = []
        for c in fresh:
            who = (c.get("creator") or {}).get("id")
            if who not in self.s["agency_ids"] or c["id"] in seen or upload_files(c) or approval(c.get("content")):
                continue
            text = cr.comment_text(c.get("content")).strip()
            if text:
                new.append({"id": c["id"], "at": str(c.get("created_at") or "")[:16].replace("T", " "),
                            "from": self.s["agency_ids"][who] + " (ekip)", "summary": "", "text": text[:800]})
        if not new:
            return job
        return self._set(job["key"], client_notes=(list(job.get("client_notes") or []) + new)[-5:])

    @staticmethod
    def _brief(job):
        return {"title": job.get("title"), "url": job.get("app_url"), "state": job.get("state"),
                "assignee": job.get("assignee_name")}

    def _open(self, index, item, brand, source, sender, report, draft=None):
        key, tkey = job_key(source["message_id"], index), job_thread_key(source["thread_id"])
        assignee = brand["assignees"][item["list"]]
        files = client_files(draft)
        unread = [n for n in (draft or {}).get("attachments_unread") or [] if n not in {f["name"] for f in files}]
        payload = {"target": {"account_id": self.s["account_id"], "project_id": brand["project_id"],
                              "list_id": brand["lists"][item["list"]]},
                   "title": todo_title(brand, sender, item),
                   "description": todo_description(brand, sender, source, item, self.s, files, unread),
                   "assignee_ids": [assignee["id"]], "due_on": item["due_on"],
                   "completion_subscriber_ids": list(brand.get("when_done") or [])}
        sha = scope_digest(payload)
        from v11_client_watch import latest_part
        job = {"key": key, "kind": "client_job", "state": "creating", "brand_key": sender["brand_key"],
               "test": bool(sender.get("test")), "message_id": source["message_id"], "thread_id": source["thread_id"],
               "index": index, "list": item["list"], "account_id": self.s["account_id"],
               "project_id": brand["project_id"], "list_id": payload["target"]["list_id"],
               "title": payload["title"], "title_core": item["title"], "brief": item["brief"],
               "client_text": latest_part(source.get("text"))[:1500], "due_on": item["due_on"],
               "assignee_id": assignee["id"], "assignee_name": assignee["name"], "client_name": sender.get("name"),
               "client_email": sender["email"], "client_notes": [], "client_files": files, "revisions": 0,
               "last_seen": 0, "payload_sha256": sha}

        def begin(tx, now):
            self.watch._fence(tx, now)
            if tx.get(key) is not None:
                return None
            thread = tx.get(tkey) or {"kind": "client_job_thread", "thread_id": source["thread_id"], "job_keys": []}
            thread["job_keys"] = list(dict.fromkeys(list(thread["job_keys"]) + [key]))[-10:]
            tx.put(tkey, thread)
            tx.put(key, dict(job, created_at=now, updated_at=now))
            return True
        if self.ops._run(begin) is None:
            return self._brief(self._get(key))
        try:
            report["business_writes"] += 1
            remote = self.todo_writer.write(copy.deepcopy(payload))["remote_id"]
            back = self.todo_writer.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == sha, "client_job_readback_mismatch")
        except Exception as exc:
            row = self._set(key, state="unknown", reason=_code(exc))
            return self._brief(row)
        row = self._set(key, state="open", todo_id=int(remote), app_url=back["app_url"],
                        opened_at=self.watch.clock().timestamp())
        self._index(add=[key])
        report.setdefault("jobs_opened", 0)
        report["jobs_opened"] += 1
        followers = sorted(i for i in brand["followers"] if i != assignee["id"])
        if followers:
            try:
                report["business_writes"] += 1
                got = self.todo_writer.subscribe(copy.deepcopy(payload), int(remote), followers)
                row = self._set(key, follower_ids=got)
            except Exception as exc:
                row = self._set(key, follower_ids=None, follower_reason=_code(exc))
        return self._brief(row)

    def _followup(self, jobs, source, sender, draft, report):
        """A later client mail in a job thread: a comment on each job to-do; a delivered job reopens."""
        from v11_client_watch import latest_part
        text = latest_part(source.get("text"))[:1500]
        note = {"at": _when(source.get("received_at_ms")), "from": sender.get("name") or sender["email"],
                "summary": _line(draft.get("summary"), 400), "text": text[:800]}
        files = client_files(draft)
        unread = [n for n in draft.get("attachments_unread") or [] if n not in {f["name"] for f in files}]
        attached = ("\n\nMaildeki ekler (hamle@ mailinde, aynı zincirde):\n" + "\n".join(file_lines(files, unread))
                    if files or unread else "")
        n = 0
        for job in jobs:
            if job.get("state") not in FOLLOW_STATES or job.get("todo_id") is None:
                continue
            body = ("Müşteriden bu işle ilgili yeni mail geldi (" + note["from"] + ", " + note["at"] + ").\n"
                    + ("Özet: " + note["summary"] + "\n" if note["summary"] else "") + "\nMailin yeni kısmı:\n" + text
                    + attached
                    + f"\n\n{job['assignee_name']}, bu notları dikkate alalım. Yeni dosya yüklendiğinde briefi bu "
                      "notlarla birlikte kontrol edeceğim.")
            if self._comment(job, body, report) == "written":
                n += 1
            notes = (list(job.get("client_notes") or []) + [note])[-5:]
            known = list(job.get("client_files") or [])
            known = (known + [f for f in files if f not in known])[-12:]
            if job["state"] == "delivered":  # the client's new notes start a fresh revision round
                self._set(job["key"], client_notes=notes, client_files=known, state="open", revisions=0)
            else:
                self._set(job["key"], client_notes=notes, client_files=known)
        return n

    # ----- 2. follow open jobs
    def follow(self, report, started):
        from v11_basecamp_reader import BasecampReader
        out = {"checked": 0, "scored": 0, "sent": 0, "asked": 0, "revisions": 0, "closed": 0, "held": 0}
        reader = BasecampReader(self.get)
        now = self.watch.clock()
        drop = []
        for key in list((self._get(JOBS_KEY) or {}).get("keys") or []):
            job = self._get(key)
            if isinstance(job, dict) and job.get("state") == "binding":  # a to-do the team opened (bind_job)
                if self._bind(job, report) in ("team", "held"):
                    drop.append(key)
                continue
            if not isinstance(job, dict) or job.get("state") not in FOLLOW_STATES:
                drop.append(key)
                continue
            if now.timestamp() - float(job.get("opened_at") or 0) > FOLLOW_DAYS * 86400:
                self._set(key, state="expired")
                drop.append(key)
                continue
            if self.watch.monotonic() - started > self.watch.cfg["run_seconds"]:
                break
            out["checked"] += 1
            try:
                outcome = self._one(job, reader, now, report, out)
            except Exception as exc:  # a Basecamp hiccup: try again next run, give up after MAX_FAILURES
                failures = int(job.get("failures") or 0) + 1
                held = failures >= MAX_FAILURES
                self._set(key, failures=failures, reason=_code(exc), **({"state": "held"} if held else {}))
                outcome = "held" if held else "waiting"
            else:
                if job.get("failures"):
                    self._set(key, failures=0)
            if outcome == "closed":
                out["closed"] += 1
                drop.append(key)
            elif outcome in ("held", "team", "unknown"):
                out["held"] += outcome == "held"
                drop.append(key)
        if drop:
            self._index(drop=drop)
        return out

    def _bind(self, job, report):
        """First run after bind_job: read the hand-opened to-do, fill the job from it and say so on the to-do."""
        url = (f"https://3.basecampapi.com/{job['account_id']}/buckets/{job['project_id']}/todos/"
               f"{job['todo_id']}.json")
        try:
            status, task, _ = self.get(url, {})
            parent = (task or {}).get("parent") if isinstance(task, dict) else None
            _need(status == 200 and isinstance(task, dict) and task.get("type") == "Todo"
                  and task.get("id") == job["todo_id"] and (task.get("bucket") or {}).get("id") == job["project_id"]
                  and isinstance(parent, dict) and parent.get("id") == job["list_id"], "client_job_bind_todo_unreadable")
        except Exception as exc:
            failures = int(job.get("failures") or 0) + 1
            held = failures >= MAX_FAILURES
            self._set(job["key"], failures=failures, reason=_code(exc), **({"state": "held"} if held else {}))
            return "held" if held else "waiting"
        if task.get("visible_to_clients") is not False or task.get("completed") is not False:
            self._set(job["key"], state="team", reason="bind_todo_visible_or_completed")
            return "team"
        import v11_client_reply as cr
        brand = self.s["brands"][job["brand_key"]]
        default = brand["assignees"][job["list"]]
        people = [a.get("id") for a in task.get("assignees") or [] if isinstance(a, dict)
                  and a.get("id") in self.s["agency_ids"]]
        assignee_id = people[0] if people else default["id"]
        assignee_name = self.s["agency_ids"].get(assignee_id) or default["name"]
        title = _line(task.get("content"), 150) or brand["name"]
        core = re.sub(r"^[^/]{1,20}//\s*", "", title).strip() or title
        brief = cr.comment_text(task.get("description") or "").strip()[:1500] or core
        due = task.get("due_on") if isinstance(task.get("due_on"), str) and DATE.fullmatch(task["due_on"]) else None
        client = self.watch.book.match(job["client_email"]) or {}
        row = self._set(job["key"], state="open", opened_at=self.watch.clock().timestamp(), title=title,
                        title_core=core, brief=brief, due_on=due, assignee_id=assignee_id, assignee_name=assignee_name,
                        client_name=client.get("name"), app_url=task.get("app_url"), failures=0)
        report.setdefault("jobs_bound", 0)
        report["jobs_bound"] += 1
        who = client.get("name") or job["client_email"]
        self._comment(row, f"Bu işi ben takip ediyorum: {who} ile olan mail zincirine bağladım. "
                           f"{assignee_name.split(' ')[0]}, çalışmayı bu to-do'ya dosya olarak yükleyince briefle "
                           "karşılaştırıp puanlayacağım; uygunsa müşteriye aynı zincirde ekli olarak göndereceğim. "
                           "Müşteriden bu işle ilgili yeni mail gelirse buraya yazacağım.", report)
        return "bound"

    def _one(self, job, reader, now, report, out):
        observation = reader.poll(self._work(job), previous=None, observed_at=now)
        if observation["task"].get("completed") is True:
            self._set(job["key"], state="closed", closed_at=now.timestamp())
            return "closed"
        from v11_brand_task import HAMLE_PERSON
        comments = sorted(observation["comments"], key=lambda c: int(c["id"]))
        fresh = [c for c in comments if int(c["id"]) > int(job.get("last_seen") or 0)
                 and (c.get("creator") or {}).get("id") != HAMLE_PERSON]
        uploads = [c for c in fresh if (c.get("creator") or {}).get("id") in self.s["agency_ids"] and upload_files(c)]
        if job.get("thread_id") is None:
            job = self._team_notes(job, fresh)
        brand = self.s["brands"][job["brand_key"]]
        verdicts = [(c, approval(c.get("content"))) for c in fresh if (c.get("creator") or {}).get("id") in brand["approvers"]]
        verdicts = [(c, v) for c, v in verdicts if v is not None]
        upload = uploads[-1] if uploads else None
        verdict = verdicts[-1] if verdicts else None
        if job["state"] == "delivered":
            if upload is None:
                if fresh:
                    self._set(job["key"], last_seen=int(fresh[-1]["id"]))
                return "waiting"
        pending = job.get("deliver_pending")
        if pending and upload is None and job["state"] in ("open", "awaiting_approval"):  # held back by a limit or stop
            return self._deliver(job, report, out, approved_by=None if pending == "auto" else pending)
        if upload is not None and (verdict is None or int(upload["id"]) > int(verdict[0]["id"])):
            if out["scored"] >= self.s["max_scorings_per_run"]:
                return "waiting"  # next run (nothing marked seen)
            return self._score(job, upload, fresh, report, out)
        if verdict is not None and job.get("upload") is not None and job["state"] in ("open", "awaiting_approval"):
            comment, verb = verdict
            who = brand["approvers"][comment["creator"]["id"]]
            if verb == "reject":
                self._set(job["key"], state="team", decided_by=who, last_seen=int(fresh[-1]["id"]))
                self._comment(job, "Tamam, bu işi müşteriye ben göndermiyorum; teslimi ekip yapacak.", report)
                return "team"
            self._set(job["key"], last_seen=int(fresh[-1]["id"]), deliver_pending=who)
            return self._deliver(self._get(job["key"]), report, out, approved_by=who)
        if fresh:
            self._set(job["key"], last_seen=int(fresh[-1]["id"]))
        return "waiting"

    def _score(self, job, upload, fresh, report, out):
        brand = self.s["brands"][job["brand_key"]]
        metas = upload_files(upload)
        designer = (upload.get("creator") or {}).get("name") or job["assignee_name"]
        seen = int(upload["id"])  # later comments (an approver's "uygun") are read next run

        def too_big(total):
            self._set(job["key"], last_seen=seen, upload=None, state="open")
            self._comment(job, f"Yüklenen dosyalar toplam {total / 1048576:.1f} MB; mail ekine sığmıyor (sınır "
                               f"{self.s['max_mail_bytes'] // 1048576} MB). {designer}, daha küçük bir sürüm (ör. JPG) "
                               "yükler misin? Yükleyince yeniden bakacağım.", report)
            return "waiting"
        known = sum(m["byte_size"] or 0 for m in metas)
        if known > self.s["max_mail_bytes"] or any((m["byte_size"] or 0) > MAX_FILE_BYTES for m in metas):
            return too_big(known)
        if not getattr(self.review, "budget_ok", lambda: True)():
            return "waiting"
        try:
            data = [self.download(m["download_url"], m["byte_size"]) for m in metas]
        except Exception as exc:
            self._set(job["key"], last_seen=seen, upload=None, state="open", reason=_code(exc))
            self._comment(job, f"Yüklenen dosyayı Basecamp'ten indiremedim ({_code(exc)}). {designer}, dosyayı "
                               "yeniden yükler misin? Olmazsa teslimi ekip yapsın.", report)
            return "waiting"
        if sum(len(d) for d in data) > self.s["max_mail_bytes"]:
            return too_big(sum(len(d) for d in data))
        files = [dict(m, sha256=hashlib.sha256(d).hexdigest(), filename=safe_filename(m["filename"]), byte_size=len(d))
                 for m, d in zip(metas, data)]
        stored = {"comment_id": int(upload["id"]), "designer": designer,
                  "files": [{k: f[k] for k in ("filename", "content_type", "byte_size", "sha256", "download_url")}
                            for f in files]}
        blocks, info, skipped = model_files(list(zip(metas, data)))
        self._set(job["key"], last_seen=seen, upload=stored, state="open")
        job = self._get(job["key"])
        if not blocks:
            self._set(job["key"], state="awaiting_approval", score=None)
            self._ask(job, None, "Yüklenen dosyayı (" + ", ".join(skipped[:3]) + ") puanlayamıyorum; video ya da "
                      "desteklemediğim bir tür", report)
            out["asked"] += 1
            return "asked"
        notes = [dict(n, text=str(n.get("text") or "")[:600]) for n in (job.get("client_notes") or [])[-3:]]
        request = {"brand": brand["name"], "job": job["title_core"], "list": job["list"], "brief": job["brief"],
                   "client_mail": job["client_text"][:1200], "client_notes": notes,
                   "client_files": [{"name": f["name"][:120], "what": f["what"][:300]}
                                    for f in list(job.get("client_files") or [])[-6:]],
                   "revision_round": int(job.get("revisions") or 0), "files": info, "not_reviewed": skipped,
                   "client_name": job.get("client_name"), "due_on": job.get("due_on"),
                   "today": self.watch.clock().date().isoformat()}
        if job.get("deliveries"):
            request["approved_before"] = {"by": job.get("accepted_by"), "accepted": list(job.get("accepted") or [])}
        out["scored"] += 1
        report["model_calls"] += 1
        try:
            result = self.review(request, blocks, [job["key"], stored["comment_id"],
                                                    [f["sha256"] for f in files], job.get("revisions") or 0])
        except Exception as exc:
            self._set(job["key"], state="awaiting_approval", score=None, reason=_code(exc))
            self._ask(job, None, "Dosyaya teknik bir sorun yüzünden bakamadım", report)
            out["asked"] += 1
            return "asked"
        score = result["score"]
        # Hamle karnesi: every scoring is kept (first-pass rate); `revisions` resets when the client reopens
        scores = (list(job.get("scores") or []) + [{"at": self.watch.clock().timestamp(), "score": score,
                                                    "round": int(job.get("revisions") or 0)}])[-20:]
        self._set(job["key"], score=score, review=result, scores=scores)
        job = self._get(job["key"])
        if score >= self.s["send_min_score"]:
            self._set(job["key"], deliver_pending="auto")
            return self._deliver(self._get(job["key"]), report, out, data=data)
        if score >= self.s["approve_min_score"]:
            self._set(job["key"], state="awaiting_approval")
            self._ask(job, result, None, report)
            out["asked"] += 1
            return "asked"
        if int(job.get("revisions") or 0) < self.s["max_revisions"]:
            n = int(job.get("revisions") or 0) + 1
            self._set(job["key"], state="open", revisions=n,
                      revision_rounds=int(job.get("revision_rounds") or 0) + 1)  # lifetime, never reset
            lines = [f"🔴 Puan: {score}/100 (revize {n}/{self.s['max_revisions']}). Briefle karşılaştırdım:",
                     result["designer_note"]]
            if result["missing"]:
                lines += ["", "Eksik ya da hatalı:"] + ["- " + m for m in result["missing"]]
            lines += ["", f"{designer}, revize edip yeni dosyayı bu to-do'ya yükler misin? Yükleyince yeniden bakacağım."]
            self._comment(job, "\n".join(lines), report)
            out["revisions"] += 1
            return "revision"
        self._set(job["key"], state="awaiting_approval")
        self._ask(job, result, f"{self.s['max_revisions']} revizeden sonra puan hâlâ {score}/100", report)
        out["asked"] += 1
        return "asked"

    def _ask(self, job, result, problem, report):
        brand = self.s["brands"][job["brand_key"]]
        names = ", ".join(brand["approvers"].values())
        if problem is None:
            lines = [f"🟡 Puan: {result['score']}/100. Briefe büyük ölçüde uyuyor; insan onayı gerekiyor.",
                     result["summary"]]
        else:
            lines = [f"🟡 {problem}."] + ([result["summary"]] if result else [])
        if result and result["missing"]:
            lines += ["", "Eksik ya da tartışmalı:"] + ["- " + m for m in result["missing"]]
        if job.get("thread_id") is None:
            lines += ["", f"{names}: uygunsa bu to-do'ya \"uygun\" yazın; iş WhatsApp'tan geldiği için dosyayı "
                          "müşteriye siz iletirsiniz. Revize gerekiyorsa tasarımcıya yazın; yeni dosyayı da takip edeceğim."]
        else:
            lines += ["", f"{names}: uygunsa bu to-do'ya \"uygun\" yazın, müşteriye hemen göndereyim. Revize "
                          "gerekiyorsa tasarımcıya yazın; yeni dosyayı da takip edeceğim. Teslimi siz yapacaksanız "
                          "\"Hamle gönderme\" yazın."]
        self._comment(job, "\n".join(lines), report)

    # ----- 3. delivery
    def _deliver(self, job, report, out, *, data=None, approved_by=None):
        upload = job.get("upload") or {}
        files = upload.get("files") or []
        if not files:
            return "waiting"
        if job.get("thread_id") is None:
            return self._ready(job, report, out, approved_by)
        brand = self.s["brands"][job["brand_key"]]
        reply = self.watch.reply
        try:
            plan = self.delivery.anchor(job["thread_id"], job["brand_key"])
        except Exception as exc:
            return self._team(job, "maili okuyamadım (" + _code(exc) + ")", report)
        if plan["unknown"] or not plan["clients"] or plan["not_direct"]:
            why = ("tanımadığım alıcı var: " + ", ".join(plan["unknown"][:3]) if plan["unknown"]
                   else "son mailde müşteri yok" if not plan["clients"]
                   else "bu kişilere onaysız yazmıyorum: " + ", ".join(plan["not_direct"][:3]))
            return self._team(job, why, report)
        if data is None:
            try:
                data = [self.download(f["download_url"], f["byte_size"]) for f in files]
            except Exception as exc:
                return self._team(job, "dosyayı yeniden indiremedim (" + _code(exc) + ")", report)
        body = client_mail(job.get("review"), job)
        payload = {"from": HAMLE, "to": plan["to"], "cc": plan["cc"], "thread_id": job["thread_id"],
                   "in_reply_to_message_id": plan["anchor"], "subject": plan["subject"], "body": body,
                   "files": [{k: f[k] for k in ("filename", "content_type", "byte_size", "sha256")} for f in files]}
        sha = scope_digest(payload)
        from v11_client_watch import ISTANBUL, day_key
        local = self.watch.clock().astimezone(ISTANBUL)
        dkey = day_key(local.date())

        def begin(tx, now):
            self.watch._fence(tx, now)
            row, day = tx.get(job["key"]), tx.get(dkey) or {}
            _need(row is not None and row.get("state") in ("open", "awaiting_approval"), "client_job_state_changed")
            if int(day.get("sent_count") or 0) >= reply["max_sends_per_day"]:
                raise ClientJobError("daily_send_limit")
            row.update(state="delivering", delivery_sha256=sha, delivering_at=now, delivery_to=payload["to"],
                       delivery_cc=payload["cc"], delivery_body=body, approved_by=approved_by)
            tx.put(job["key"], row)
            tx.put(dkey, dict(day, kind="client_day", day=local.date().isoformat(),
                              sent_count=int(day.get("sent_count") or 0) + 1))
        try:
            self.ops._run(begin)
        except ClientJobError as exc:
            if exc.code == "daily_send_limit":
                return "waiting"
            return "held"
        except Exception:
            return "waiting"  # stop / budget hold: nothing sent, tried again next run
        report["business_writes"] += 1
        try:
            remote = self.delivery.write(copy.deepcopy(payload), data)["remote_id"]
        except Exception as exc:
            if _code(exc) in NOT_SENT:  # failed a check before the POST: nothing went out
                return self._team(job, "son kontrol geçmedi (" + _code(exc) + ")", report)
            return self._unknown(job, exc, report)
        try:
            back = self.delivery.readback(remote, copy.deepcopy(payload))
            _need(back.get("readback_sha256") == sha, "client_delivery_readback_mismatch")
        except Exception as exc:
            return self._unknown(job, exc, report)
        # Ahmet 13 Oct: what a manager approved (or a 90+ score let through) is settled; later rounds never dock it
        accepted = list(dict.fromkeys(list(job.get("accepted") or [])
                                      + list((job.get("review") or {}).get("missing") or [])))[-12:]
        self._set(job["key"], state="delivered", delivered_at=self.watch.clock().timestamp(), delivery_remote_id=remote,
                  deliveries=int(job.get("deliveries") or 0) + 1, deliver_pending=None, accepted=accepted,
                  accepted_by=approved_by or f"Hamle ({job.get('score')}/100)")
        out["sent"] += 1
        report["sent"] += 1
        who = ", ".join(plan["to"]) + ("; CC: " + ", ".join(plan["cc"]) if plan["cc"] else "")
        head = (f"✅ Puan: {job['score']}/100. Briefle uyumlu, müşteriye gönderdim" if approved_by is None
                else f"✅ {approved_by} onayıyla müşteriye gönderdim")
        self._comment(job, head + f" ({who}).\n\nGönderilen mail:\n{body}\n\nEkler: "
                      + ", ".join(f["filename"] for f in files), report)
        return "sent"

    def _ready(self, job, report, out, approved_by):
        """No client mail thread (WhatsApp job): the work passed; the team sends it, Hamle says it is ready."""
        files = (job.get("upload") or {}).get("files") or []
        head = (f"✅ Puan: {job.get('score')}/100. Briefle uyumlu, müşteriye gitmeye hazır" if approved_by is None
                else f"✅ {approved_by} onayladı, müşteriye gitmeye hazır")
        written = self._comment(job, head + ". İş WhatsApp'tan geldiği için mail zinciri yok: dosyayı ("
                                + ", ".join(f["filename"] for f in files) + ") müşteriye siz iletin. Müşteri revize "
                                "isterse notunu buraya yazın; yeni dosyayı yine kontrol ederim.", report)
        if written.startswith("denied:"):
            return "waiting"  # 'dur' or a limit: deliver_pending stays, tried again next run
        accepted = list(dict.fromkeys(list(job.get("accepted") or [])
                                      + list((job.get("review") or {}).get("missing") or [])))[-12:]
        self._set(job["key"], state="delivered", delivered_at=self.watch.clock().timestamp(), delivered_by="team",
                  deliveries=int(job.get("deliveries") or 0) + 1, deliver_pending=None, accepted=accepted,
                  accepted_by=approved_by or f"Hamle ({job.get('score')}/100)")
        out["ready"] = out.get("ready", 0) + 1
        return "ready"

    def _unknown(self, job, exc, report):
        self._set(job["key"], state="send_unknown", reason=_code(exc), deliver_pending=None)
        self._comment(job, "Müşteriye gönderdim ama teyit edemedim; tekrar denemiyorum. hamle@ Gönderilmiş "
                           "kutusuna bakın, gerekirse teslimi elle yapın.", report)
        return "unknown"

    def _team(self, job, why, report):
        self._set(job["key"], state="team", reason=why[:200], deliver_pending=None)
        self._comment(job, f"Müşteriye göndermedim: {why}. Teslimi ekip yapsın.", report)
        return "team"


# ------------------------------------------------------------------ a to-do the team opened (Ahmet 14 Oct)
def bind_job(ops, catalog, config, *, thread_id, message_id, todo_id, list_name, brand_key, client_email, actor):
    """Make a brand to-do the team opened by hand Hamle's job for a client mail thread (Firestore only). The next
    client_watch run reads the to-do and says on it that Hamle follows; from then on the job runs like one Hamle
    opened: the client's mails in the thread are answered and written on the to-do, uploads are scored, and the
    approved work goes to the client in the same thread."""
    from v11_chain_mail import HEX
    from v11_client_watch import EMAIL
    _need(isinstance(actor, str) and actor.endswith("@gamechangers.com.tr") and actor != HAMLE, "actor_invalid")
    _need(all(isinstance(v, str) and HEX.fullmatch(v) for v in (thread_id, message_id)) and type(todo_id) is int
          and todo_id > 0 and list_name in LISTS and isinstance(client_email, str)
          and EMAIL.fullmatch(client_email.strip().lower()) and not client_email.endswith("@gamechangers.com.tr"),
          "client_job_bind_invalid")
    jobs = (config or {}).get("jobs") or {}
    route = (catalog.get("brand_routes") or {}).get(brand_key) or {}
    list_id = (route.get("lists") or {}).get(list_name)
    _need(jobs.get("enabled") is True and brand_key in (jobs.get("brands") or {}) and type(route.get("project_id")) is int
          and type(list_id) is int, "client_job_bind_brand_invalid")
    key, tkey = job_key(message_id, 0), job_thread_key(thread_id)
    job = {"key": key, "kind": "client_job", "state": "binding", "bound_by": actor, "brand_key": brand_key,
           "test": False, "message_id": message_id, "thread_id": thread_id, "index": 0, "list": list_name,
           "account_id": catalog["basecamp"]["account_id"], "project_id": route["project_id"], "list_id": list_id,
           "todo_id": todo_id, "client_email": client_email.strip().lower(), "client_text": "", "client_notes": [],
           "revisions": 0, "last_seen": 0}

    def op(tx, now):
        old, thread, index = tx.get(key), tx.get(tkey), tx.get(JOBS_KEY)  # Firestore: every read first
        _need(old is None, "client_job_exists")
        thread = thread or {"kind": "client_job_thread", "thread_id": thread_id, "job_keys": []}
        thread["job_keys"] = list(dict.fromkeys(list(thread.get("job_keys") or []) + [key]))[-10:]
        keys = list((index or {}).get("keys") or [])
        tx.put(key, dict(job, created_at=now, updated_at=now))
        tx.put(tkey, thread)
        tx.put(JOBS_KEY, {"kind": "client_jobs", "keys": list(dict.fromkeys(keys + [key]))[-80:]})
        return {"status": "client_job_bound", "job_key": key}
    return ops._run(op, allow_expired=True)


def main(argv=None):
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="Client jobs (Firestore only)")
    parser.add_argument("action", choices=("bind",))
    parser.add_argument("--thread", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--todo", type=int, required=True)
    parser.add_argument("--list", choices=LISTS, required=True)
    parser.add_argument("--brand", required=True)
    parser.add_argument("--client", required=True)
    parser.add_argument("--actor", required=True)
    args = parser.parse_args(argv)
    try:
        from cloud_store import FirestoreBackend, OperationsStore
        from import_cloud_credentials import credentials
        from v11_chain_live import load_catalog
        from v11_client_watch import load_config
        ops = OperationsStore(FirestoreBackend(opt_in="yes", credentials=credentials()))
        result = bind_job(ops, load_catalog(), load_config(), thread_id=args.thread, message_id=args.message,
                          todo_id=args.todo, list_name=args.list, brand_key=args.brand, client_email=args.client,
                          actor=args.actor)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except ClientJobError as exc:
        print(json.dumps({"status": "held", "reason": exc.code}))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "held", "reason": "client_job_command_failed:" + _code(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
