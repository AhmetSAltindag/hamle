"""Aylık İçerik Planı -> Basecamp (Ahmet 2026-10-17: "plan Basecamp'te yaşasın"; SM & MT step 2).

One plan = one Basecamp document in the brand project's Docs & Files + one to-do in the brand's social list that
points at it. The to-do goes to the brand's social media lead (assignee); the strategy owner, Ahmet and the
designers in content-plan-config.json follow it (completion subscribers). Whoever sends the plan to the client
writes the link as a comment on that to-do; Hamle reads it later. Hamle never sends the plan to a client.

Off by default: the write runs only when content-plan-config.json has enabled=true AND basecamp_write=true, and only
into a brand project whose catalog route says clients_enabled=false. Every call is allow-listed to that project.
Record content-plan-<brand>-<YYYYMM> (own key): building -> writing -> ready, or failed (model, nothing written) /
unknown (a write was attempted and could not be confirmed). Never retried automatically; never two plans a month.
"""
from __future__ import annotations

import html
import re
from urllib.parse import quote, urlsplit

import v11_content_plan as cp
from v11_nb_tracking import NbBasecamp

API = "https://3.basecampapi.com"
APP = "https://3.basecamp.com"
HAMLE_PERSON = 53358100
LEASE_SECONDS = 3600
_need, _code = cp._need, cp._code


# ------------------------------------------------------------------ targets (from the catalog, never guessed)
def target(catalog, cfg, key):
    """Brand -> where the plan goes and who gets it. Fails closed on anything unverified."""
    route = (catalog.get("brand_routes") or {}).get(key) or {}
    people = catalog.get("basecamp_people") or {}
    _need(route.get("verified") is True and route.get("clients_enabled") is False
          and route.get("lists_visible_to_clients") is False and route.get("hamle_member") is True,
          "content_plan_route_unverified")
    pid, lid, sm = route.get("project_id"), (route.get("lists") or {}).get("social"), route.get("sm_person_id")
    _need(all(type(x) is int and x > 0 for x in (pid, lid, sm)), "content_plan_route_incomplete")
    owner = next((int(i) for i, p in people.items() if "strategy_owner" in (p.get("agency_roles") or [])), None)
    principal = (catalog.get("brand_routing") or {}).get("principal_person_id")
    follow = [owner, principal] + list(cfg["brands"][key]["designers"])
    _need(all(type(x) is int and str(x) in people for x in [sm] + follow), "content_plan_person_unknown")
    subs = sorted({x for x in follow if x != sm})
    return {"project_id": pid, "list_id": lid, "assignee_ids": [sm], "subscriber_ids": subs, "principal_id": principal,
            "account_id": (catalog.get("basecamp") or {}).get("account_id")}


# ------------------------------------------------------------------ document and to-do text
def _e(value):
    return html.escape(str(value or ""), quote=False)


def doc_title(name, first):
    return f"{name} · {cp.month_label(first)} İçerik Planı (Hamle taslağı)"


def doc_html(name, first, plan, findings, score, material):
    """The plan in Basecamp rich text (div, h1, strong, em, br, ul/li only), in the PDF's order (v11_content_plan_files)."""
    import v11_content_plan_files as pf
    lo, hi = material["brand"]["posts_per_month"]
    n = pf.counts(plan)
    br = lambda t: _e(t).replace(chr(10), "<br>")
    out = [f"<div><em>Hamle AI iç taslağı · müşteriye gönderilmez. Ayın her günü için bir öneri var; ⭐ {n['rec']} gün "
           f"sözleşmedeki {lo}-{hi} paylaşım için önerilen seçim. Seçim, eleme ve ekleme sosyal medya ekibinde.</em></div>",
           f"<div>{n['days']} gün · ⭐ {n['rec']} önerilen · Instagram {n['ig']} · LinkedIn {n['li']} · ikisi birden "
           f"{n['both']} · Hamle kontrolü {score}/100</div>"]
    notes = pf.notes(plan, findings)
    if notes:
        out += ["<h1>Ekibin bakması gerekenler</h1>",
                "<ul>" + "".join(f"<li><strong>{_e(t.upper())}</strong> {_e(x)}</li>" for t, x in notes) + "</ul>"]
    out += ["<h1>Strateji ve içerik sütunları</h1>",
            "<ul>" + "".join(f"<li>{_e(x)}</li>" for x in plan["strategy"]) + "</ul>",
            f"<div><em>{_e(cp.STRATEGY_NOTE)}</em></div>",
            "<ul>" + "".join(f"<li><strong>{_e(p['name'])}</strong>: {_e(p['why'])}</li>" for p in plan["pillars"]) + "</ul>"]
    specials = " · ".join(f"{cp._dm(s['date'])} {_e(s['name'])}" for s in material.get("special_days") or [])
    out.append("<div>" + (f"Özel günler: {specials}<br>" if specials else "") + f"Format dağılımı: {_e(n['formats'])}</div>")
    for title, lines in pf.extras(plan, material):
        out += [f"<h1>{_e(title)}</h1>", "<ul>" + "".join(f"<li>{br(x)}</li>" for x in lines) + "</ul>"]
    out.append("<h1>Takvim</h1>")
    rows = []
    for r in plan["days"]:
        if r.get("missing"):
            rows.append(f"<li>{cp._day(r)} · öneri yok</li>")
            continue
        head = (f"{'⭐ ' if r.get('recommended') else ''}{cp._day(r)} · {_e(cp.FORMAT_LABELS.get(r['format'], r['format']))}"
                f" · {_e(cp.platform_label(r['platform']))}")
        rows.append(f"<li><strong>{head}</strong> · {_e(r['pillar'])}"
                    + (f" · Seri: {_e(r['series'])}" if r.get("series") else "") + f" · {_e(r['topic'])}"
                    + (f" · Özel gün: {_e(r['special_day'])}" if r.get("special_day") else "") + "</li>")
    out.append("<ul>" + "".join(rows) + "</ul>")
    out.append("<h1>Günlerin detayı</h1>")
    for r in plan["days"]:
        if r.get("missing") or not r.get("caption"):
            continue
        out.append(f"<div><strong>{'⭐ ' if r.get('recommended') else ''}{cp._day(r)} · "
                   f"{_e(cp.platform_label(r['platform']))} · {_e(cp.FORMAT_LABELS.get(r['format'], r['format']))} · "
                   f"{_e(r['topic'])}</strong></div>")
        out.append("<ul>" + "".join(f"<li><strong>{_e(k)}:</strong><br>{br(v)}</li>" if kind != "kv" else
                                     f"<li><strong>{_e(k)}:</strong> {br(v)}</li>"
                                     for k, v, kind in pf.detail_fields(r)) + "<li><strong>Durum:</strong> Taslak</li></ul>")
    out.append("<div><em>Hamle AI · Gamechangers iç taslak · Kaynak: marka hafızası (son 90 gün), Basecamp, özel günler "
               "takvimi, Platform Kural Defteri. PDF (sunum) ve DOCX (düzenleme) ekte.</em></div>")
    return "".join(out)


def todo_title(first):
    return f"{cp.month_label(first)} İçerik Planı: Hamle taslağı hazır"


def todo_html(doc_url, rec, lo, hi):
    return (f"<div>Hamle bu ayın iç taslağını hazırladı: <a href=\"{_e(doc_url)}\">planı aç</a>.</div>"
            f"<div>Her gün için bir fikir var; ⭐ {rec} gün sözleşmedeki {lo}-{hi} paylaşım için önerildi. "
            "Seçimi ve düzenlemeyi sosyal medya sorumlusu yapar, tasarım ekibi brief'leri buradan alır.</div>"
            "<div><strong>Planı müşteriye gönderen kişi, gönderdiği linki bu göreve yorum olarak yazsın.</strong> "
            "Hamle müşteriye hiçbir şey göndermez.</div>")


# ------------------------------------------------------------------ Basecamp (one brand project only)
class PlanBasecamp(NbBasecamp):
    """Allow-listed calls only: the project dock, its Docs & Files vault, documents in it, the social list and
    its to-dos, comments on a recording; plus the plan's own PDF/DOCX upload (send_raw) for the document."""
    def __init__(self, account_id, project_id, list_id, *, send, send_raw=None):
        super().__init__(account_id, send=send)
        self.send_raw = send_raw
        b = rf"/{account_id}/buckets/{project_id}"
        self.allowed = {"GET": (rf"/{account_id}/projects/{project_id}", rf"{b}/vaults/[1-9][0-9]*",
                                rf"{b}/documents/[1-9][0-9]*", rf"{b}/todolists/{list_id}", rf"{b}/todos/[1-9][0-9]*",
                                rf"{b}/recordings/[1-9][0-9]*/comments"),
                        "POST": (rf"{b}/vaults/[1-9][0-9]*/documents", rf"{b}/todolists/{list_id}/todos")}


    def upload(self, name, data, mime):
        """One file -> attachable_sgid (POST /attachments.json, binary body)."""
        _need(self.send_raw is not None, "content_plan_upload_unavailable")
        _need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,120}\.(pdf|docx)", name) is not None and isinstance(data, bytes)
              and 0 < len(data) <= 10 * 1024 * 1024, "content_plan_upload_invalid")
        row = self.send_raw("POST", f"{API}/{self.account}/attachments.json?name={quote(name, safe='')}", data, mime, 201)
        sgid = row.get("attachable_sgid") if isinstance(row, dict) else None
        _need(isinstance(sgid, str) and re.fullmatch(r"[A-Za-z0-9+/=_-]{10,600}", sgid) is not None,
              "content_plan_upload_receipt_invalid")
        return sgid


def raw_send(token_provider, session_factory):
    """Production binary transport for PlanBasecamp.upload (existing Basecamp OAuth token, no redirects)."""
    import cloud_providers as provider

    def send(method, url, data, mime, expected):
        p = urlsplit(url)
        _need(method == "POST" and p.scheme == "https" and p.netloc == "3.basecampapi.com"
              and re.fullmatch(r"/[1-9][0-9]*/attachments\.json", p.path) is not None, "content_plan_upload_scope")
        session, r = session_factory(), None
        try:
            r = session.request(method, url, headers={
                "Authorization": "Bearer " + provider._token(token_provider()), "Content-Type": mime,
                "Content-Length": str(len(data)), "User-Agent": "Hamle Content Plan (ahmet@gamechangers.com.tr)"},
                data=data, timeout=(10, 60), allow_redirects=False, stream=True)
            _need(r.status_code == expected, "content_plan_upload_unconfirmed")
            chunks, size = [], 0
            for chunk in r.iter_content(chunk_size=16384):
                size += len(chunk)
                _need(size <= 65536, "content_plan_upload_response_too_large")
                chunks.append(chunk)
            return provider._json(b"".join(chunks))
        finally:
            if r is not None:
                r.close()
            session.close()
    return send


def _clean_url(value, prefix, fallback):
    ok = isinstance(value, str) and value.startswith(prefix) and len(value) <= 300 and not any(
        c in value for c in " <>\"'")
    return value if ok else fallback


class PlanWriter:
    """Record + Basecamp writes for one brand month. Fenced by the control generation ('dur' stops new writes)."""
    def __init__(self, ops, http, key, first):
        self.ops, self.http, self.brand, self.first = ops, http, key, first
        self.key = cp.record_key(key, first)

    def _write(self, fn, *, fenced=True):
        def op(tx, now):
            if fenced:
                c = self.ops._control(tx)
                self.ops._open(c, c["generation"], now)
            row = tx.get(self.key)
            out = fn(row, now)
            if isinstance(row, dict):
                tx.put(self.key, row)
            return out
        return self.ops._run(op, allow_expired=not fenced)

    def claim(self, token):
        """One plan per brand month: a new record, or a 'building' one whose lease ran out (model crash)."""
        def op(tx, now):
            c = self.ops._control(tx)
            self.ops._open(c, c["generation"], now)
            row = tx.get(self.key)
            if isinstance(row, dict) and not (row.get("state") == "building" and row.get("lease_until", 0) <= now):
                return False
            tx.put(self.key, {"kind": "content_plan", "schema_version": 1, "brand": self.brand,
                              "month": f"{self.first:%Y-%m}", "state": "building", "token": token,
                              "lease_until": now + LEASE_SECONDS, "at": now})
            return True
        return self.ops._run(op)

    def mark(self, **fields):
        self._write(lambda row, now: row.update(fields, at=now), fenced=False)

    def write(self, dest, title, body, todo_name, todo_body, files=(), notify=True):
        """Document, then the to-do pointing at it. Returns the receipts; any doubt -> state unknown, no retry.
        notify=False (a weekend write): only the principal is subscribed to the document and the to-do is assigned
        without a notification; the team sees it in Basecamp and the morning digest."""
        pid, lid, acct = dest["project_id"], dest["list_id"], self.http.account
        base = f"{API}/{acct}/buckets/{pid}"

        def begin(row, now):
            _need(isinstance(row, dict) and row.get("state") == "building", "content_plan_record_lost")
            row.update(state="writing", project_id=pid, list_id=lid, doc_id=None, todo_id=None)
        self._write(begin)
        step = "dock"
        try:
            project = self.http.one(f"{API}/{acct}/projects/{pid}.json")
            vault = next((d for d in project.get("dock") or [] if isinstance(d, dict) and d.get("name") == "vault"
                          and d.get("enabled") is True and type(d.get("id")) is int), None)
            _need(project.get("id") == pid and vault is not None, "content_plan_vault_missing")
            vid = vault["id"]
            back = self.http.one(f"{base}/vaults/{vid}.json")
            _need(back.get("id") == vid and (back.get("bucket") or {}).get("id") == pid
                  and back.get("status") in (None, "active"), "content_plan_vault_unusable")
            lst = self.http.one(f"{base}/todolists/{lid}.json")
            _need(lst.get("id") == lid and (lst.get("bucket") or {}).get("id") == pid
                  and lst.get("visible_to_clients") is False and lst.get("status") in (None, "active"),
                  "content_plan_list_unusable")
            step = "attachment"
            sgids = [self.http.upload(n, data, mime) for n, data, mime in files]
            if sgids:
                self.mark(attachments=[n for n, _, _ in files])
                body += "<div><strong>Ekler:</strong> PDF sunum için, DOCX ekibin düzenlemesi için.</div>" + "".join(
                    f'<bc-attachment sgid="{html.escape(x, quote=True)}"></bc-attachment>' for x in sgids)
            step = "document"
            row, _ = self.http.call("POST", f"{base}/vaults/{vid}/documents.json",
                                    dict({"title": title, "content": body, "status": "active"},
                                         **({} if notify else {"subscriptions": [dest["principal_id"]]})), 201)
            did = row.get("id") if isinstance(row, dict) else None
            _need(type(did) is int and did > 0, "content_plan_doc_receipt_invalid")
            self.mark(doc_id=did)
            doc = self.http.one(f"{base}/documents/{did}.json")
            _need(doc.get("id") == did and doc.get("title") == title and (doc.get("bucket") or {}).get("id") == pid
                  and (doc.get("parent") or {}).get("id") == vid and (doc.get("creator") or {}).get("id") == HAMLE_PERSON
                  and doc.get("visible_to_clients") is not True, "content_plan_doc_readback_mismatch")
            doc_url = _clean_url(doc.get("app_url"), f"{APP}/{acct}/buckets/{pid}/documents/",
                                 f"{APP}/{acct}/buckets/{pid}/documents/{did}")
            self.mark(doc_url=doc_url)
            step = "todo"
            note = todo_body(doc_url)
            row, _ = self.http.call("POST", f"{base}/todolists/{lid}/todos.json",
                                    {"content": todo_name, "description": note, "assignee_ids": dest["assignee_ids"],
                                     "completion_subscriber_ids": dest["subscriber_ids"], "notify": bool(notify)}, 201)
            tid = row.get("id") if isinstance(row, dict) else None
            _need(type(tid) is int and tid > 0, "content_plan_todo_receipt_invalid")
            self.mark(todo_id=tid)
            todo = self.http.one(f"{base}/todos/{tid}.json")
            _need(todo.get("id") == tid and todo.get("content") == todo_name
                  and (todo.get("parent") or {}).get("id") == lid and (todo.get("bucket") or {}).get("id") == pid
                  and (todo.get("creator") or {}).get("id") == HAMLE_PERSON and todo.get("visible_to_clients") is False
                  and sorted(a.get("id") for a in todo.get("assignees") or [] if isinstance(a, dict))
                  == dest["assignee_ids"]
                  and sorted(a.get("id") for a in todo.get("completion_subscribers") or [] if isinstance(a, dict))
                  == dest["subscriber_ids"], "content_plan_todo_readback_mismatch")
        except Exception as exc:
            reason = f"{step}:{_code(exc)}"
            self.mark(state="unknown", reason=reason)
            return {"status": "unknown", "reason": reason}
        todo_url = _clean_url(todo.get("app_url"), f"{APP}/{acct}/buckets/{pid}/todos/",
                              f"{APP}/{acct}/buckets/{pid}/todos/{tid}")
        self.mark(state="ready", todo_url=todo_url, token=None)
        return {"status": "written", "doc_id": did, "doc_url": doc_url, "todo_id": tid, "todo_url": todo_url}


def summary(plan):
    """What next month's plan reads as 'last month' (gather: previous['plan'])."""
    return {"pillars": [{"name": p["name"]} for p in plan["pillars"]],
            "days": [{"date": r["date"], "topic": r.get("topic"), "format": r.get("format"),
                      "recommended": bool(r.get("recommended"))} for r in plan["days"] if not r.get("missing")]}
