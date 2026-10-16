"""Hamle Gece Sağlık Kontrolü (owner 2026-10-16 15:52Z: "İyileşme ve Gelişme Laboratuvarı", phase 1).

Every night after midnight (scheduler "5 0 * * *", config timezone) Hamle checks the previous day from its own
records: did each scheduled run happen, did anything get stuck, is any send unconfirmed, how is the budget. There is
NO model call and NO change to any work record (cost 0); the only writes are the night's own record and, when a
finding is urgent, ONE alert mail to the config recipients.

Why from records, not from the cloud console: on 16 Oct the brand memory's morning scheduler pointed at a broken URI
and nothing noticed. A missing run leaves a missing record, whatever the cause; the check names the gap, the expected
and actual behaviour, the evidence (store keys), the affected work and how sure it is of the cause. It never claims
a root cause it could not verify ("kök neden doğrulanmadı").

Durability:
- health-check-YYYYMMDD (the night's date): one run per night through a lease (state running -> done); a done night
  is never run again; a crashed run's lease expires and the next trigger of the SAME night may finish it. Missed
  nights are never replayed (only the current night's key is ever claimed).
- health-alert-YYYYMMDD: the urgent mail through the daily digest's one-mail path (claim_mail / send_claimed); never
  a second alert for a night, never resent when its outcome is unknown.
- The 09:15 digest reads health-check-<its day> (digest_rows) and shows a short "Hamle sağlık" section.

Checks (each one independent; a failing check is itself a finding, the others still run):
  runs:     chain ticks (slot-* rows of the day's 15-minute window), daily digest, brand memory, weekly summary
  sends:    client replies left 'sending' / 'send_unknown' (days in the lookback), replies waiting approval > 1 day
  jobs:     client jobs stuck in 'creating' / 'binding' over an hour
  control:  Hamle stopped ('dur')
  budget:   overrun hold, month-to-date committed over the warn line, unsettled holds
  teams:    yesterday's model calls (an information line)

Tenant independence: recipients, timezone, windows and budget lines come from health-check-config.json; no names.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import re
import uuid
from zoneinfo import ZoneInfo

import v11_daily_digest as dd

CONFIG_NAME = "health-check-config.json"
LEASE_SECONDS = 600
SEVERITIES = ("red", "amber", "info")
ICONS = {"red": "🔴", "amber": "🟡", "info": "ℹ️"}
TITLE = "Hamle sağlık"
KEYS = {
    "night": lambda d: f"health-check-{d:%Y%m%d}",
    "alert": lambda d: f"health-alert-{d:%Y%m%d}",
    "slot": lambda d, hhmm: f"slot-{d:%Y%m%d}T{hhmm}",
    "digest": lambda d: f"daily-digest-{d:%Y%m%d}",
    "memory": lambda d: f"brand-memory-run-{d:%Y%m%d}",
    "weekly": lambda d: "weekly-summary-{}{:02d}".format(*d.isocalendar()[:2]),
    "client_day": lambda d: f"client-day-{d:%Y%m%d}",
    "teams_day": lambda d: f"teams-day-{d.isoformat()}",
}
SENT_STATES = ("sent",)
UNSURE_SEND = ("sending", "send_unknown", "unknown")
STUCK_JOB_STATES = ("creating", "binding")


class HealthCheckError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise HealthCheckError(code)


def _code(exc):
    return re.sub(r"[^A-Za-z0-9_.:@-]", "_", str(getattr(exc, "code", None) or type(exc).__name__))[:120]


# ------------------------------------------------------------------ config
def _hhmm(value):
    return isinstance(value, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value) is not None


def load_config(path=None):
    return dd._load(CONFIG_NAME, path)


def validate_config(cfg):
    _need(isinstance(cfg, dict) and cfg.get("schema_version") == 1 and type(cfg.get("enabled")) is bool,
          "health_config_invalid")
    recipients = cfg.get("alert_recipients")
    _need(isinstance(recipients, list) and 0 < len(recipients) <= 5 and len(set(recipients)) == len(recipients)
          and all(dd.internal_address(r) for r in recipients), "health_recipients_invalid")
    try:
        tz = ZoneInfo(cfg.get("timezone") or "")
    except Exception:
        raise HealthCheckError("health_timezone_invalid") from None
    win = cfg.get("run_window") or {}
    tick = cfg.get("chain_tick") or {}
    _need(_hhmm(win.get("start")) and _hhmm(win.get("end")) and win["start"] < win["end"], "health_window_invalid")
    _need(_hhmm(tick.get("first")) and _hhmm(tick.get("last")) and tick["first"] <= tick["last"]
          and tick.get("interval_minutes") in (15, 30) and type(tick.get("missing_amber")) is int
          and type(tick.get("missing_red")) is int and 0 < tick["missing_amber"] <= tick["missing_red"],
          "health_tick_invalid")
    budget = cfg.get("budget") or {}
    _need(all(isinstance(budget.get(k), (int, float)) and budget[k] > 0 for k in ("warn_usd",)), "health_budget_invalid")
    lookback = cfg.get("lookback_days")
    _need(type(lookback) is int and 1 <= lookback <= 7, "health_lookback_invalid")
    return {"enabled": cfg["enabled"], "recipients": list(recipients), "tz": tz, "window": (win["start"], win["end"]),
            "tick": dict(tick), "warn_micro": int(budget["warn_usd"] * 1_000_000), "lookback": lookback}


def in_window(local, window):
    lo, hi = (int(v[:2]) * 60 + int(v[3:]) for v in window)
    return lo <= local.hour * 60 + local.minute <= hi


# ------------------------------------------------------------------ findings
def finding(check, severity, title, *, expected, actual, evidence=(), affected=None, cause=None, confidence="düşük"):
    """One finding: what should have happened, what did, the store keys that show it, what it touches, the likely
    cause and how sure we are ('doğrulandı' only when the records prove it)."""
    _need(severity in SEVERITIES, "health_severity_invalid")
    return {"check": check, "severity": severity, "title": title, "expected": expected, "actual": actual,
            "evidence": list(evidence)[:10], "affected": affected, "cause": cause or "kök neden doğrulanmadı",
            "confidence": confidence}


def _read(ops, keys):
    keys = list(dict.fromkeys(keys))
    return ops._run(lambda tx, _: {k: tx.get(k) for k in keys}, allow_expired=True)


def slot_times(tick):
    step = tick["interval_minutes"]
    lo, hi = (int(v[:2]) * 60 + int(v[3:]) for v in (tick["first"], tick["last"]))
    return [f"{m // 60:02d}{m % 60:02d}" for m in range(lo, hi + 1, step)]


def check_runs(ops, day, cfg):
    """The scheduled work of `day` left its records: chain ticks, digest, brand memory, Friday's weekly summary."""
    out = []
    if day.weekday() >= 5:
        return out
    times = slot_times(cfg["tick"])
    rows = _read(ops, [KEYS["slot"](day, t) for t in times] + [KEYS["digest"](day), KEYS["memory"](day)]
                 + ([KEYS["weekly"](day)] if day.weekday() == 4 else []))
    missing = [t for t in times if rows.get(KEYS["slot"](day, t)) is None]
    held = {}
    for t in times:
        r = rows.get(KEYS["slot"](day, t))
        if isinstance(r, dict) and r.get("state") == "held":
            held[r.get("reason") or "?"] = held.get(r.get("reason") or "?", 0) + 1
    if len(missing) >= cfg["tick"]["missing_amber"]:
        whole = len(missing) == len(times)
        out.append(finding(
            "runs.chain_tick", "red" if len(missing) >= cfg["tick"]["missing_red"] else "amber",
            "Mail ve Basecamp turu hiç çalışmadı" if whole else f"Mail ve Basecamp turlarından {len(missing)} tanesi çalışmadı",
            expected=f"{len(times)} tur ({cfg['tick']['first']}-{cfg['tick']['last']}, {cfg['tick']['interval_minutes']} dk)",
            actual=f"{len(times) - len(missing)} tur kaydı var; eksik: {', '.join(t[:2] + ':' + t[2:] for t in missing[:8])}"
                   + (" …" if len(missing) > 8 else ""),
            evidence=[KEYS["slot"](day, t) for t in missing[:5]],
            affected="Müşteri mailleri, ekip istekleri ve iş takibi o saatlerde işlenmedi.",
            cause=("Zamanlayıcı ya da job hiç başlamamış görünüyor (zamanlayıcı adresi, izin ya da imaj)." if whole else
                   "Turlar başlamamış ya da süre penceresini kaçırmış."), confidence="orta" if whole else "düşük"))
    if held:
        top = sorted(held.items(), key=lambda kv: -kv[1])[:3]
        out.append(finding("runs.chain_tick_held", "amber", "Bazı turlar işi yapmadan durdu",
                           expected="Turlar 'no_new_work' ya da iş yaparak biter.",
                           actual=", ".join(f"{reason} × {n}" for reason, n in top),
                           evidence=[f"slot-* state=held reason={reason}" for reason, _ in top],
                           cause="Kayıttaki durdurma sebebi: " + top[0][0], confidence="orta"))
    digest = rows.get(KEYS["digest"](day))
    if not (isinstance(digest, dict) and digest.get("state") in SENT_STATES):
        out.append(finding("runs.daily_digest", "red", "Sabah özeti gitmedi",
                           expected="09:15'te ekip özeti gönderilir (state=sent).",
                           actual="kayıt yok" if digest is None else f"state={digest.get('state')}"
                           + (f", sebep={digest.get('last_failure')}" if digest.get("last_failure") else ""),
                           evidence=[KEYS["digest"](day)], affected="Ekip günün iş özetini almadı.",
                           cause=None if digest is None else "Gönderim kaydı yarım kaldı.",
                           confidence="düşük" if digest is None else "orta"))
    memory = rows.get(KEYS["memory"](day))
    status = (memory or {}).get("status") if isinstance(memory, dict) else None
    if memory is None or (status and not str(status).startswith("brand_memory_completed")
                          and status != "no_new_work"):
        out.append(finding("runs.brand_memory", "amber" if memory is not None else "red",
                           "Marka hafızası tazelenmedi" if memory is None else "Marka hafızası yarım kaldı",
                           expected="07:30'da her markanın hafızası tazelenir.",
                           actual="çalışma kaydı yok" if memory is None
                           else f"status={status}, sebep={memory.get('reason')}",
                           evidence=[KEYS["memory"](day)],
                           affected="Teams cevapları ve haftalık/aylık raporlar eski bilgiyle çalışır.",
                           cause="16 Ekim'deki gibi bozuk zamanlayıcı adresi olabilir." if memory is None
                           else "Kayıttaki sebep: " + str(memory.get("reason")),
                           confidence="düşük" if memory is None else "orta"))
    if day.weekday() == 4:
        weekly = rows.get(KEYS["weekly"](day))
        if not (isinstance(weekly, dict) and weekly.get("state") in SENT_STATES):
            out.append(finding("runs.weekly_summary", "amber", "Hafta kapanışı gitmedi",
                               expected="Cuma 14:00'te hafta kapanışı gönderilir.",
                               actual="kayıt yok" if weekly is None else f"state={weekly.get('state')}",
                               evidence=[KEYS["weekly"](day)], affected="Ekip haftalık marka özetini almadı."))
    return out


def check_sends(ops, day, cfg, now):
    """Client replies whose send is not confirmed, and replies waiting for approval over a day."""
    out = []
    days = [day - timedelta(days=n) for n in range(cfg["lookback"])]
    index = _read(ops, [KEYS["client_day"](d) for d in days])
    keys = [k for d in days for k in ((index.get(KEYS["client_day"](d)) or {}).get("message_keys") or [])][-400:]
    rows = _read(ops, keys) if keys else {}
    unsure = [k for k, r in rows.items() if isinstance(r, dict) and not r.get("test")
              and r.get("reply_state") in UNSURE_SEND]
    waiting = [k for k, r in rows.items() if isinstance(r, dict) and not r.get("test")
               and r.get("reply_state") == "awaiting_approval"
               and now - float(r.get("updated_at") or r.get("received_at_ms", 0) / 1000 or now) > 86400]
    if unsure:
        out.append(finding("sends.unconfirmed", "red", f"{len(unsure)} müşteri yanıtının gönderimi doğrulanmadı",
                           expected="Her gönderim Gmail'de 'gönderildi' olarak geri okunur.",
                           actual="reply_state sending/send_unknown; Hamle kural gereği tekrar göndermedi.",
                           evidence=unsure[:5], affected="Müşteri yanıtı almamış ya da almış olabilir.",
                           cause="Gönderim sırasında zaman aşımı ya da okuma hatası; Gmail'den elle teyit gerekir.",
                           confidence="orta"))
    if waiting:
        out.append(finding("sends.waiting_approval", "amber",
                           f"{len(waiting)} müşteri yanıtı bir günden uzun süredir onay bekliyor",
                           expected="Taslaklar aynı gün onaylanır ya da reddedilir.",
                           actual="reply_state=awaiting_approval", evidence=waiting[:5],
                           affected="Müşteri cevap bekliyor.", cause="Onay ekipte bekliyor.", confidence="doğrulandı"))
    return out


def check_jobs(ops, now):
    from v11_brand_memory import client_jobs
    stuck = [j for j in client_jobs(ops) if j.get("state") in STUCK_JOB_STATES
             and now - float(j.get("updated_at") or j.get("created_at") or now) > 3600]
    if not stuck:
        return []
    return [finding("jobs.stuck", "amber", f"{len(stuck)} müşteri işi açılırken takıldı",
                    expected="İş bir turda Basecamp'te açılır ve 'open' olur.",
                    actual=", ".join(sorted({j['state'] for j in stuck})),
                    evidence=[j.get("key") for j in stuck[:5]], affected="Bu işler Basecamp'te görünmüyor olabilir.",
                    cause="To-do yazımı ya da bağlama adımı yarım kaldı.", confidence="orta")]


def check_budget(ops, cfg):
    b = ops.budget_status()
    out = []
    usd = b["total_committed_microdollars"] / 1_000_000
    if b["overrun_hold"]:
        out.append(finding("budget.overrun_hold", "red", "Bütçe kilidi açık: ücretli işler duruyor",
                           expected="overrun_hold=false", actual="overrun_hold=true", evidence=["budget"],
                           affected="Model kullanan tüm işler (müşteri yanıtı, özet, rapor) bekliyor.",
                           cause="Bir çağrı ayrılan tutarı aştı; kilit yalnız onayla açılır.", confidence="doğrulandı"))
    elif b["total_committed_microdollars"] >= cfg["warn_micro"]:
        out.append(finding("budget.warn", "amber", f"Bu ayın harcaması {usd:.2f} USD'ye ulaştı",
                           expected=f"< {cfg['warn_micro'] / 1_000_000:.0f} USD", actual=f"{usd:.2f} USD",
                           evidence=["budget"], cause="Aylık kullanım uyarı çizgisini geçti.", confidence="doğrulandı"))
    return out, {"committed_usd": round(usd, 2), "held_usd": round(b["held_microdollars"] / 1_000_000, 2),
                 "limit_usd": round(b["limit_microdollars"] / 1_000_000, 2), "period": b.get("period")}


def check_control(ops):
    c = ops.control()
    if c.get("mode") == "review_open":
        return []
    return [finding("control.stopped", "red", "Hamle durdurulmuş durumda",
                    expected="mode=review_open", actual=f"mode={c.get('mode')}", evidence=["control"],
                    affected="Turlar, özetler ve müşteri işleri çalışmıyor.",
                    cause="Biri 'dur' dedi ya da kontrol elle değişti.", confidence="doğrulandı")]


def check_teams(ops, day):
    row = _read(ops, [KEYS["teams_day"](day)]).get(KEYS["teams_day"](day))
    return {"model_calls": int((row or {}).get("model_calls") or 0)}


CHECKS = ("control", "runs", "sends", "jobs", "budget", "teams")


def gather(ops, day, cfg, now):
    findings, facts, broken = [], {}, []
    for name in CHECKS:
        try:
            if name == "control":
                findings += check_control(ops)
            elif name == "runs":
                findings += check_runs(ops, day, cfg)
            elif name == "sends":
                findings += check_sends(ops, day, cfg, now)
            elif name == "jobs":
                findings += check_jobs(ops, now)
            elif name == "budget":
                more, facts["budget"] = check_budget(ops, cfg)
                findings += more
            else:
                facts["teams"] = check_teams(ops, day)
        except Exception as exc:  # one broken check never stops the others
            broken.append(name)
            findings.append(finding("check." + name, "amber", f"'{name}' kontrolü çalışmadı",
                                    expected="Kontrol kayıtları okur.", actual=_code(exc), evidence=[],
                                    cause="Kontrolün kendisi hata verdi.", confidence="doğrulandı"))
    findings.sort(key=lambda f: SEVERITIES.index(f["severity"]))
    return findings, dict(facts, checks=len(CHECKS) - len(broken), broken=broken)


# ------------------------------------------------------------------ text
def summary_line(findings, facts):
    red = sum(1 for f in findings if f["severity"] == "red")
    amber = sum(1 for f in findings if f["severity"] == "amber")
    if not findings:
        return f"Gece kontrolü temiz: {facts.get('checks', 0)} kontrol, sorun yok."
    return f"Gece kontrolü: {red} acil, {amber} dikkat."


def render_alert(findings, facts, night, checked):
    lines = [f"Hamle gece kontrolü · {checked:%d.%m.%Y} günü · {night:%d.%m.%Y} gecesi", "",
             summary_line(findings, facts), ""]
    for f in findings:
        lines += [f"{ICONS[f['severity']]} {f['title']}",
                  f"  Beklenen: {f['expected']}", f"  Gerçekleşen: {f['actual']}"]
        if f.get("affected"):
            lines.append(f"  Etkisi: {f['affected']}")
        lines.append(f"  Olası sebep: {f['cause']} (güven: {f['confidence']})")
        if f["evidence"]:
            lines.append("  Kanıt: " + ", ".join(f["evidence"]))
        lines.append("")
    lines += ["Hamle hiçbir şeyi kendisi değiştirmedi; düzeltme önerisi sabah özetinde.", "",
              "Sevgiler,", "Hamle AI"]
    return "\n".join(lines)


def digest_rows(row, day):
    """For the 09:15 digest: {'title', 'line', 'rows'} from health-check-<day>, or None (no record or not done)."""
    if not (isinstance(row, dict) and row.get("state") == "done" and row.get("night") == day.isoformat()):
        return None
    findings = [f for f in row.get("findings") or [] if isinstance(f, dict) and f.get("severity") in ICONS]
    return {"title": TITLE, "line": str(row.get("summary") or "")[:200],
            "rows": [f"{ICONS[f['severity']]} {str(f.get('title'))[:160]}" for f in findings[:5]]}


# ------------------------------------------------------------------ run
class _Night:
    def __init__(self, ops, key):
        self.ops, self.key = ops, key

    def claim(self, token, fields):
        def op(tx, now):
            row = tx.get(self.key)
            if isinstance(row, dict) and (row.get("state") == "done" or row.get("lease_until", 0) > now):
                return None
            new = dict(row or {}, **fields, key=self.key, kind="health_check", state="running", token=token,
                       claimed_at=now, lease_until=now + LEASE_SECONDS, runs=int((row or {}).get("runs") or 0) + 1)
            tx.put(self.key, new)
            return new
        return self.ops._run(op, allow_expired=True)

    def finish(self, token, **fields):
        def op(tx, now):
            row = tx.get(self.key)
            _need(isinstance(row, dict) and row.get("token") == token and row.get("state") == "running",
                  "health_claim_lost")
            row.update(fields, state="done", finished_at=now, lease_until=now)
            tx.put(self.key, row)
            return row
        return self.ops._run(op, allow_expired=True)

    def release(self, token, reason):
        def op(tx, now):
            row = tx.get(self.key)
            if isinstance(row, dict) and row.get("token") == token and row.get("state") == "running":
                row.update(lease_until=now, last_failure=reason)
                tx.put(self.key, row)
        try:
            self.ops._run(op, allow_expired=True)
        except Exception:
            pass


def run_health_check(ops, *, config, writer, clock, preview=False):
    """One night's check (config window), or a preview (any time, no record, no mail)."""
    report = {"status": None, "night": None, "model_calls": 0, "business_writes": 0}
    night = token = None
    try:
        now = clock()
        _need(isinstance(now, datetime) and now.tzinfo is not None, "aware_clock_required")
        cfg = validate_config(config)
        local = now.astimezone(cfg["tz"])
        night_day = local.date()
        checked = night_day - timedelta(days=1)
        report.update(night=night_day.isoformat(), checked=checked.isoformat())
        if not preview:
            if not cfg["enabled"]:
                return dict(report, status="no_new_work", reason="health_check_disabled")
            if not in_window(local, cfg["window"]):
                return dict(report, status="no_new_work", reason="outside_health_check_window")
            night, token = _Night(ops, KEYS["night"](night_day)), uuid.uuid4().hex
            if night.claim(token, {"night": night_day.isoformat(), "checked": checked.isoformat()}) is None:
                return dict(report, status="already_recorded", reason="health_check_done_or_running")
        findings, facts = gather(ops, checked, cfg, now.timestamp())
        line = summary_line(findings, facts)
        if preview:
            return dict(report, status="health_check_preview", summary=line, findings=findings, facts=facts,
                        alert=render_alert(findings, facts, night_day, checked) if findings else None)
        alert = "none"
        red = [f for f in findings if f["severity"] == "red"]
        if red and writer is not None:
            alert = send_alert(ops, cfg, writer, findings, facts, night_day, checked)
        night.finish(token, summary=line, findings=findings, facts=facts, alert=alert)
        return dict(report, status="health_check_completed", summary=line, red=len(red),
                    amber=sum(1 for f in findings if f["severity"] == "amber"), alert=alert,
                    business_writes=1 if alert == "sent" else 0)
    except Exception as exc:
        if night is not None:
            night.release(token, _code(exc))
        return dict(report, status="held", reason=_code(exc), automatic_retry_allowed=False)


def send_alert(ops, cfg, writer, findings, facts, night_day, checked):
    """ONE urgent mail per night (health-alert-<night>); 'sent', 'already', 'unknown' or 'failed:<code>'."""
    claim = None
    try:
        claim = dd.claim_mail(ops, KEYS["alert"](night_day), {"kind": "health_alert", "token": uuid.uuid4().hex,
                                                             "date": night_day.isoformat()})
        if claim is None:
            return "already"
        red = sum(1 for f in findings if f["severity"] == "red")
        payload = {"from": dd.HAMLE, "to": cfg["recipients"], "cc": [],
                   "subject": f"Hamle gece kontrolü: {red} acil konu ({checked:%d.%m.%Y})",
                   "body": render_alert(findings, facts, night_day, checked)}
        if dd.send_claimed(ops, claim, payload, {"red": red}, writer) is None:
            return "unknown"
        return "sent"
    except Exception as exc:
        dd.release_claim(ops, claim, _code(exc))
        return "failed:" + _code(exc)


def run_worker_health_check(ops, secrets, *, clock=None, preview=False):
    """Entry for worker.py --mode health_check [--health-preview]."""
    import cloud_providers as provider
    clock = clock or (lambda: datetime.now(timezone.utc))
    writer = None
    if not preview:
        from v11_chain_mail import gmail_token_context
        writer = dd.DigestMailWriter(token_context=gmail_token_context(secrets), session_factory=provider._session)
    return run_health_check(ops, config=load_config(), writer=writer, clock=clock, preview=preview)
