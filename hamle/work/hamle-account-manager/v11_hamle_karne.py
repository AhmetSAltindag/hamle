"""Hamle'nin kendi karnesi (Ahmet 2026-10-15 20:53Z: "patronlar Hamle'ye bayılacak").

ONE internal mail (+ HTML twin) per month to the agency owners named in hamle-karne-config.json (agency addresses only,
the daily digest's rule), on the first weekday of the month inside the config window (default 10:30-12:30, the
monthly brand report's window), about the previous calendar month. It runs at the start of worker.py --mode
monthly_report (no new job, no new scheduler) and as worker.py --mode hamle_karne --hamle-karne-preview (any day, no
mail, no record).

Every number is computed here from the operations store; there is NO model call (cost 0). Sections:
  1. Ayın özeti (built from the numbers)       5. Hamle'nin ürettikleri (digest, weekly, monthly, karne, Teams)
  2. Müşteri mailleri (who answered)           6. Maliyet (budget period_history of the month, else the live totals)
  3. İlk yanıt süresi (median / p90)           7. Kazandırılan zaman (counts x config minutes, labelled tahmini)
  4. Teslimler ve kalite, Verilen sözler
What the records cannot show yet is said plainly ("henüz kaydedilmiyor"), never estimated.

Tenant independence (SaaS direction 2026-10-15): no person, address, project id or agency name in this module;
recipients, timezone, window and the minutes table come from the config; brand names from the catalog; store keys
are built in KEYS below (one place).

Sources (read only, small transactions):
- client-day-YYYYMMDD -> message_keys -> client-mail-<sha> rows (v11_client_watch): reply_state, approved_by, edited,
  received_at_ms, sent_at, brand_key; test and shadow rows are left out.
- 'client-jobs' index -> client jobs (v11_brand_memory.client_jobs): opened / delivered / team / revisions / scores.
- brand-memory-<brand>-<YYYYMM> 'soz' events and brand-profile-<brand> open_promises.
- daily-digest-YYYYMMDD, weekly-summary-YYYYWW, monthly-report-<brand>-<YYYYMM>, client-summary-YYYYMMDD,
  digital-karne-<monday>, teams-day-YYYY-MM-DD.
- budget: period_history[period == month] (the roll on the first run of the new month writes it).

Durability: hamle-karne-<YYYYMM> through the daily digest's one-mail path (claim_mail / send_claimed / release_claim);
never a second mail for a month.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
import re
import uuid
from zoneinfo import ZoneInfo

import v11_brand_memory as bm
import v11_daily_digest as dd
import v11_monthly_report as mr

CONFIG_NAME = "hamle-karne-config.json"
MINUTE_KEYS = ("client_reply_auto", "client_reply_approved", "client_job_opened", "delivery_checked", "daily_digest",
               "weekly_summary", "monthly_report", "digital_karne", "client_summary", "teams_answer")
MINUTE_LABELS = {"client_reply_auto": "Hamle'nin kendi gönderdiği müşteri yanıtı",
                 "client_reply_approved": "Onayla giden müşteri yanıtı (taslak Hamle'den)",
                 "client_job_opened": "Müşteri işi için brief ve to-do",
                 "delivery_checked": "Teslim kontrolü ve puanlama",
                 "daily_digest": "Günlük özet", "weekly_summary": "Haftalık marka özeti",
                 "monthly_report": "Aylık marka raporu", "digital_karne": "Dijital karne",
                 "client_summary": "Akşam müşteri maili özeti", "teams_answer": "Teams'te cevaplanan mesaj"}
REPLY_STATES = ("sent", "awaiting_approval", "rejected", "approval_expired", "approval_stale", "stale", "manual",
                "send_unknown", "sending")
TITLE = "Hamle Karnesi"
INTRO = "Hamle'nin kendi iş sonuçları. Sayılar kayıtlardan hesaplandı, model yazmadı; tahmini olanlar belirtildi."
NOT_RECORDED = "henüz kaydedilmiyor"
FOOTER = ("Hamle AI Müşteri Temsilcisi · Hamle'nin aylık karnesi · Her ayın ilk iş günü · "
          "Sayılar kayıtlardan hesaplanır, tahmini olanlar belirtilir.")
KEYS = {
    "karne": lambda first: f"hamle-karne-{first:%Y%m}",
    "client_day": lambda d: f"client-day-{d:%Y%m%d}",
    "digest": lambda d: f"daily-digest-{d:%Y%m%d}",
    "client_summary": lambda d: f"client-summary-{d:%Y%m%d}",
    "weekly": lambda d: "weekly-summary-{}{:02d}".format(*d.isocalendar()[:2]),
    "monthly": lambda brand, first: f"monthly-report-{brand}-{first:%Y%m}",
    "digital_karne": lambda monday: f"digital-karne-{monday.isoformat()}",
    "teams_day": lambda d: f"teams-day-{d.isoformat()}",
}


class HamleKarneError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise HamleKarneError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


# ------------------------------------------------------------------ config
def load_config(path=None):
    return dd._load(CONFIG_NAME, path)


def _hhmm(value):
    return isinstance(value, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) is not None


def validate_config(cfg):
    _need(isinstance(cfg, dict) and cfg.get("schema_version") == 1 and type(cfg.get("enabled")) is bool,
          "karne_config_invalid")
    recipients = cfg.get("recipients")
    _need(isinstance(recipients, list) and 0 < len(recipients) <= 10 and len(set(recipients)) == len(recipients),
          "karne_recipients_invalid")
    for r in recipients:
        _need(dd.internal_address(r), "karne_recipient_not_internal_agency")
    try:
        tz = ZoneInfo(cfg.get("timezone") or "")
    except Exception:
        raise HamleKarneError("karne_timezone_invalid") from None
    window = cfg.get("window") or {}
    _need(_hhmm(window.get("start")) and _hhmm(window.get("end")) and window["start"] < window["end"],
          "karne_window_invalid")
    minutes = cfg.get("minutes_saved")
    _need(isinstance(minutes, dict) and set(minutes) == set(MINUTE_KEYS)
          and all(type(v) is int and 0 <= v <= 600 for v in minutes.values()), "karne_minutes_invalid")
    return {"enabled": cfg["enabled"], "recipients": list(recipients), "tz": tz,
            "window": (window["start"], window["end"]), "minutes": dict(minutes)}


# ------------------------------------------------------------------ calendar
def report_month(day):
    last = day.replace(day=1) - timedelta(days=1)
    return last.replace(day=1), last


def first_weekday(year, month):
    d = date(year, month, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def in_window(local, window):
    lo, hi = (int(v[:2]) * 60 + int(v[3:]) for v in window)
    return local.date() == first_weekday(local.year, local.month) and lo <= local.hour * 60 + local.minute <= hi


def days_of(first, last):
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def month_name(first):
    return f"{dd.MONTHS[first.month - 1]} {first.year}"


# ------------------------------------------------------------------ small number helpers
def _ts(value):
    return bm._ts(value)


def _in(stamp, lo, hi):
    return stamp is not None and lo <= stamp < hi


def percentile(values, q):
    """Nearest-rank percentile of a non-empty list (q in 0..100)."""
    s = sorted(values)
    k = max(1, -(-len(s) * q // 100))
    return s[int(k) - 1]


def duration(seconds):
    """Plain Turkish duration: '45 sn', '12 dk', '2 sa 5 dk', '1 gün 3 sa'."""
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s} sn"
    m = s // 60
    if m < 60:
        return f"{m} dk"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} sa" + (f" {m} dk" if m else "")
    d, h = divmod(h, 24)
    return f"{d} gün" + (f" {h} sa" if h else "")


def usd(micro):
    return f"{micro / 1_000_000:.2f}".replace(".", ",") + " USD"


def pct(part, whole):
    return f"%{round(100 * part / whole)}" if whole else "-"


def hours(minutes):
    h = minutes / 60
    return (f"{h:.1f}".replace(".", ",") if h < 10 else str(round(h))) + " saat"


# ------------------------------------------------------------------ material (read only)
def _read(ops, keys):
    def op(tx, _):
        return {k: tx.get(k) for k in keys}
    return ops._run(op, allow_expired=True)


def client_mails(ops, first, last):
    """The month's real client mail rows (test and shadow rows left out), day by day (one small read per day)."""
    rows = []
    for d in days_of(first, last):
        day = _read(ops, [KEYS["client_day"](d)])[KEYS["client_day"](d)] or {}
        keys = [k for k in day.get("message_keys") or [] if isinstance(k, str)]
        if keys:
            rows += [r for r in _read(ops, keys).values()
                     if isinstance(r, dict) and r.get("kind") == "client_mail" and not r.get("test")
                     and not r.get("shadow")]
    return rows


def mail_numbers(rows, brands):
    by_state = Counter(r.get("reply_state") for r in rows)
    sent = [r for r in rows if r.get("reply_state") == "sent"]
    auto = [r for r in sent if not r.get("approved_by")]
    approved = [r for r in sent if r.get("approved_by")]

    def secs(r):
        got, out = r.get("received_at_ms"), r.get("sent_at")
        if type(got) is int and isinstance(out, (int, float)) and not isinstance(out, bool) and out * 1000 >= got:
            return out - got / 1000
        return None
    auto_t = [t for t in map(secs, auto) if t is not None]
    appr_t = [t for t in map(secs, approved) if t is not None]
    per_brand = Counter(r.get("brand_key") for r in rows)
    return {"received": len(rows), "auto": len(auto), "approved": len(approved),
            "edited": sum(1 for r in approved if r.get("edited")),
            "not_sent": by_state["rejected"] + by_state["approval_expired"] + by_state["approval_stale"]
            + by_state["stale"],
            "rejected": by_state["rejected"], "expired": by_state["approval_expired"],
            "stale": by_state["approval_stale"] + by_state["stale"],
            "manual": by_state["manual"], "waiting": by_state["awaiting_approval"],
            "unknown": by_state["send_unknown"] + by_state["sending"], "draft_only": by_state[None],
            "by_brand": [(brands.get(k, {}).get("name") or str(k), n) for k, n in per_brand.most_common()],
            "auto_median": percentile(auto_t, 50) if auto_t else None,
            "auto_p90": percentile(auto_t, 90) if auto_t else None,
            "approved_median": percentile(appr_t, 50) if appr_t else None,
            "timed_auto": len(auto_t), "timed_approved": len(appr_t)}


def job_numbers(jobs, first, last):
    lo, hi = bm.day_start(first), bm.day_start(last + timedelta(days=1))
    opened = [j for j in jobs if _in(_ts(j.get("opened_at")) or _ts(j.get("created_at")), lo, hi)]
    delivered = [j for j in jobs if _in(_ts(j.get("delivered_at")), lo, hi)]
    auto = [j for j in delivered if str(j.get("accepted_by") or "").startswith("Hamle (")]

    def first_pass(j):
        scores = j.get("scores")
        if isinstance(scores, list) and scores:
            return len(scores) == 1
        return not int(j.get("revision_rounds") or j.get("revisions") or 0)
    return {"opened": len(opened), "delivered": len(delivered), "auto": len(auto),
            "approved": len(delivered) - len(auto),
            "team_ready": sum(1 for j in delivered if j.get("delivered_by") == "team"),
            "first_pass": sum(1 for j in delivered if first_pass(j)),
            "revision_rounds": sum(int(j.get("revision_rounds") or j.get("revisions") or 0) for j in delivered),
            "to_team": sum(1 for j in opened if j.get("state") == "team"),
            "client_notes": sum(mr._notes_in(j, first, last) for j in jobs)}


def promise_numbers(ops, brands, first, last):
    keys = [bm.month_key(b, first) for b in brands] + [bm.profile_key(b) for b in brands]
    got = _read(ops, keys)
    made, open_now = Counter(), Counter()
    for b in brands:
        month = got.get(bm.month_key(b, first)) or {}
        made[b] = sum(1 for e in month.get("events") or [] if isinstance(e, dict) and e.get("kind") == "soz"
                      and first.isoformat() <= str(e.get("date")) <= last.isoformat())
        open_now[b] = sum(1 for p in (got.get(bm.profile_key(b)) or {}).get("open_promises") or []
                          if isinstance(p, dict) and p.get("what"))
    return {"made": sum(made.values()), "open": sum(open_now.values()),
            "by_brand": [(brands[b]["name"], made[b], open_now[b]) for b in brands if made[b] or open_now[b]]}


def output_numbers(ops, brands, first, last):
    days = days_of(first, last)
    prev_first = (first - timedelta(days=1)).replace(day=1)
    mondays = [d for d in days if d.weekday() == 0]
    weekly = sorted({KEYS["weekly"](d) for d in days if d.weekday() == 4})
    keys = ([KEYS["digest"](d) for d in days] + [KEYS["client_summary"](d) for d in days] + weekly
            + [KEYS["monthly"](b, prev_first) for b in brands] + [KEYS["digital_karne"](m) for m in mondays]
            + [KEYS["teams_day"](d) for d in days])
    got = _read(ops, keys)

    def sent(k):
        return isinstance(got.get(k), dict) and got[k].get("state") == "sent"
    karne = sum(1 for m in mondays for row in ((got.get(KEYS["digital_karne"](m)) or {}).get("brands") or {}).values()
                if isinstance(row, dict) and row.get("state") == "ready")
    teams = sum(int((got.get(KEYS["teams_day"](d)) or {}).get("model_calls") or 0) for d in days)
    return {"daily_digest": sum(sent(KEYS["digest"](d)) for d in days),
            "client_summary": sum(sent(KEYS["client_summary"](d)) for d in days),
            "weekly_summary": sum(sent(k) for k in weekly),
            "monthly_report": sum(sent(KEYS["monthly"](b, prev_first)) for b in brands),
            "digital_karne": karne, "teams_answer": teams}


def cost_numbers(ops, first):
    period = f"{first:%Y-%m}"
    budget = ops._run(lambda tx, _: ops._budget(tx), allow_expired=True)
    row = next((h for h in reversed(budget.get("period_history") or []) if h.get("period") == period), None)
    if row is not None:
        totals, chain = row.get("totals") or {}, row.get("chain_microdollars")
    elif budget.get("period") == period:  # preview inside the month itself
        totals, chain = ops._totals(budget), ops._chain_committed(budget)
    else:
        return None
    spent = int(totals.get("accounted_spend_microdollars") or 0) + int(totals.get("held_microdollars") or 0)
    return {"spent": spent, "limit": int(totals.get("limit_microdollars") or 0), "chain": int(chain or 0)}


def saved_minutes(n, minutes):
    counts = {"client_reply_auto": n["mails"]["auto"], "client_reply_approved": n["mails"]["approved"],
              "client_job_opened": n["jobs"]["opened"], "delivery_checked": n["jobs"]["delivered"],
              **{k: n["outputs"][k] for k in ("daily_digest", "weekly_summary", "monthly_report", "digital_karne",
                                              "client_summary", "teams_answer")}}
    rows = [(k, counts[k], counts[k] * minutes[k]) for k in MINUTE_KEYS if counts[k] and minutes[k]]
    return {"total": sum(r[2] for r in rows), "rows": sorted(rows, key=lambda r: -r[2])}


def gather(ops, brands, first, last, minutes, jobs):
    n = {"mails": mail_numbers(client_mails(ops, first, last), brands), "jobs": job_numbers(jobs, first, last),
         "promises": promise_numbers(ops, brands, first, last), "outputs": output_numbers(ops, brands, first, last),
         "cost": cost_numbers(ops, first)}
    n["saved"] = saved_minutes(n, minutes)
    return n


# ------------------------------------------------------------------ text
def subject(first):
    return f"Hamle AI: {month_name(first)} {TITLE}"


def summary(n, first):
    m, j, s, c = n["mails"], n["jobs"], n["saved"], n["cost"]
    out = []
    if m["received"]:
        out.append(f"{dd.MONTHS[first.month - 1]} ayında Hamle {m['received']} müşteri mailini gördü; "
                   f"{m['auto']} tanesini kendisi yanıtladı, {m['approved']} tanesi ekip onayıyla gitti.")
    else:
        out.append(f"{dd.MONTHS[first.month - 1]} ayında Hamle'nin gördüğü müşteri maili kaydı yok.")
    if m["auto_median"] is not None:
        out.append(f"Kendi yanıtlarında müşteriye ilk dönüş süresi ortanca {duration(m['auto_median'])}.")
    if j["delivered"]:
        out.append(f"{j['delivered']} teslimin {j['auto']} tanesi 90+ puanla doğrudan çıktı.")
    if c is not None:
        out.append(f"Ayın kayıtlı maliyeti {usd(c['spent'])}.")
    if s["total"]:
        out.append(f"Hamle'nin kazandırdığı ekip zamanı tahminen {hours(s['total'])}.")
    return " ".join(out)


def sections(n, minutes):
    m, j, p, o, c, s = n["mails"], n["jobs"], n["promises"], n["outputs"], n["cost"], n["saved"]
    mails = [f"Gelen müşteri maili: {m['received']}"
             + (" (" + ", ".join(f"{name} {k}" for name, k in m["by_brand"]) + ")" if m["by_brand"] else ""),
             f"Hamle'nin kendi gönderdiği yanıt: {m['auto']}",
             f"\"Hamle gönder\" onayıyla giden yanıt: {m['approved']}"
             + (f" ({m['edited']} tanesi düzeltilerek)" if m["edited"] else ""),
             f"Gönderilmeyen taslak: {m['not_sent']} (gönderilmemesi istendi {m['rejected']}, onay gelmedi "
             f"{m['expired']}, zincirde daha yeni mail vardı {m['stale']})",
             f"Ekibe bırakılan (Hamle yanıt yazmadı): {m['manual']}"]
    if m["waiting"]:
        mails.append(f"Ay sonunda onay bekleyen: {m['waiting']}")
    if m["unknown"]:
        mails.append(f"Gönderimi belirsiz kalan (tekrar denenmedi): {m['unknown']}")
    if m["draft_only"]:
        mails.append(f"Yanıt modu açık olmayan markalarda yalnız taslak: {m['draft_only']}")
    timing = [("Hamle'nin kendi yanıtları: ortanca " + duration(m["auto_median"])
               + f", 10 yanıttan 9'u {duration(m['auto_p90'])} içinde ({m['timed_auto']} yanıt)")
              if m["auto_median"] is not None else "Hamle'nin kendi yanıtı yok.",
              ("Onayla giden yanıtlar: ortanca " + duration(m["approved_median"]) + f" ({m['timed_approved']} yanıt)")
              if m["approved_median"] is not None else "Onayla giden yanıt yok.",
              f"Ekibin kendi yanıt süresi: {NOT_RECORDED}."]
    jobs = [f"Açılan müşteri işi: {j['opened']}",
            f"Müşteriye teslim edilen: {j['delivered']} (Hamle 90+ puanla gönderdi {j['auto']}, yönetici onayıyla "
            f"{j['approved']}" + (f"; {j['team_ready']} tanesi WhatsApp işi, ekip iletti" if j["team_ready"] else "")
            + ")",
            f"İlk kontrolde geçen teslim: {j['first_pass']}/{j['delivered']} ({pct(j['first_pass'], j['delivered'])}, "
            "yaklaşık)" if j["delivered"] else "İlk kontrolde geçen teslim: teslim yok.",
            f"Hamle'nin tasarıma istediği revize turu: {j['revision_rounds']}",
            f"Müşterinin ek notu ya da revize isteği: {j['client_notes']}",
            f"Ekibe devredilen iş: {j['to_team']}"]
    promises = [f"Bu ay kayda geçen söz: {p['made']}", f"Bugün açık görünen söz: {p['open']}"] \
        + [f"{name}: {made} yeni söz, {opened} açık" for name, made, opened in p["by_brand"]] \
        + [f"Sözün tutulup tutulmadığı: {NOT_RECORDED}."]
    outputs = [f"{MINUTE_LABELS[k]}: {o[k]}" for k in ("daily_digest", "weekly_summary", "monthly_report",
                                                      "digital_karne", "client_summary")] \
        + [f"Teams'te cevaplanan mesaj (yaklaşık): {o['teams_answer']}"]
    cost = ([f"Kayıtlı harcama (model ve bulut): {usd(c['spent'])}"
             + (f", aylık {usd(c['limit'])} sınırının {pct(c['spent'], c['limit'])}'i" if c["limit"] else ""),
             f"Bunun iç ajans zincirine giden kısmı: {usd(c['chain'])}"]
            if c is not None else ["Bu ayın bütçe kaydı alınamadı."])
    saved = ([f"Toplam: yaklaşık {hours(s['total'])} (tahmini)"]
             + [f"{MINUTE_LABELS[k]}: {count} × {minutes[k]} dk = {hours(total)}" for k, count, total in s["rows"]]
             + ["İş başına dakikalar varsayımdır; ayar dosyasından değiştirilebilir."]
             if s["total"] else ["Bu ay hesaplanacak iş yok."])
    return [("Müşteri mailleri", mails), ("İlk yanıt süresi", timing), ("Teslimler ve kalite", jobs),
            ("Verilen sözler", promises), ("Hamle'nin ürettikleri", outputs), ("Maliyet", cost),
            ("Kazandırılan zaman (tahmini)", saved)]


def _period(first, last):
    return f"{first.day} – {last.day} {month_name(first)}"


def render(n, first, last, minutes):
    lines = [subject(first), f"Dönem: {_period(first, last)}", INTRO, "", dd._upper_tr("Ayın özeti"),
             summary(n, first)]
    for title, rows in sections(n, minutes):
        lines += ["", dd._upper_tr(title)] + ["- " + r for r in rows]
    return "\n".join(lines + ["", FOOTER])


def render_html(n, first, last, minutes):
    out = [f'<div style="font-size:19px;font-weight:bold;margin:0 0 4px;">{dd._e(subject(first))}</div>',
           dd._p(dd._e(f"Dönem {_period(first, last)}"), f"color:{dd.GREY};"), dd._p(dd._grey(INTRO)),
           dd._h2("Ayın özeti"), dd._p(dd._e(summary(n, first)))]
    for title, rows in sections(n, minutes):
        out += [dd._h2(title), dd._ul([dd._li(dd._e(r)) for r in rows])]
    out.append(f'<div style="margin-top:26px;padding-top:8px;border-top:1px solid #e5e7eb;font-size:12px;'
               f'color:{dd.GREY};">{dd._e(FOOTER)}</div>')
    return ('<!DOCTYPE html><html><head><meta charset="utf-8"></head><body style="margin:0;padding:0;">'
            f'<div style="font-family:{dd.FONT};font-size:14px;line-height:1.55;color:{dd.TEXT};width:100%;">'
            + "".join(out) + "</div></body></html>")


# ------------------------------------------------------------------ run
def _brands(catalog):
    found = bm.brands_of(catalog)
    return {k: found[k] for k in catalog["brand_routes"] if k in found}


def run_hamle_karne(ops, *, config, catalog, writer, clock, preview=False):
    """One owners' mail per month (first weekday, config window), or a preview (no send, no record)."""
    from runtime_authority import deadline_timestamp, start_timestamp
    report = {"status": None, "month": None, "model_calls": 0, "business_writes": 0}
    claim = None
    try:
        now = clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        _need(start_timestamp() <= now.timestamp() <= deadline_timestamp(), "authorization_expired")
        cfg = validate_config(config)
        local = now.astimezone(cfg["tz"])
        first, last = report_month(local.date())
        report["month"] = f"{first:%Y-%m}"
        if preview:
            _need(ops.control()["mode"] == "review_open", "control_stopped_or_stale")
        elif not cfg["enabled"]:
            return dict(report, status="no_new_work", reason="hamle_karne_disabled")
        elif not in_window(local, cfg["window"]):
            return dict(report, status="no_new_work", reason="outside_hamle_karne_window")
        key = KEYS["karne"](first)
        if not preview:
            claim = dd.claim_mail(ops, key, {"kind": "hamle_karne", "token": uuid.uuid4().hex,
                                             "month": report["month"], "date": local.date().isoformat()})
            if claim is None:
                return dict(report, status="already_recorded", reason="hamle_karne_already_sent")
        n = gather(ops, _brands(catalog), first, last, cfg["minutes"], bm.client_jobs(ops))
        body = render(n, first, last, cfg["minutes"])
        stats = {"numbers": json.loads(json.dumps(n, default=str))}
        if preview:
            return dict(report, status="hamle_karne_preview", subject="[ÖNİZLEME] " + subject(first), body=body,
                        stats=stats, recipients=len(cfg["recipients"]))
        payload = {"from": dd.HAMLE, "to": cfg["recipients"], "cc": [], "subject": subject(first), "body": body,
                   "html": render_html(n, first, last, cfg["minutes"])}
        if dd.send_claimed(ops, claim, payload, stats, writer) is None:
            return dict(report, status="held", reason="hamle_karne_outcome_unknown_no_retry",
                        automatic_retry_allowed=False)
        return dict(report, status="hamle_karne_sent", business_writes=1, recipients=len(cfg["recipients"]))
    except Exception as exc:
        dd.release_claim(ops, claim, _code(exc))
        return dict(report, status="held", reason=_code(exc), automatic_retry_allowed=False)


# ------------------------------------------------------------------ live wiring
def run_worker_hamle_karne(ops, secrets, *, clock=None, preview=False):
    """Entry for worker.py --mode monthly_report (first) and --mode hamle_karne --hamle-karne-preview."""
    import cloud_providers as provider
    from v11_chain_live import load_catalog
    clock = clock or (lambda: datetime.now(timezone.utc))
    writer = None
    if not preview:
        from v11_chain_mail import gmail_token_context
        writer = dd.DigestMailWriter(token_context=gmail_token_context(secrets), session_factory=provider._session)
    return run_hamle_karne(ops, config=load_config(), catalog=load_catalog(), writer=writer, clock=clock,
                           preview=preview)
