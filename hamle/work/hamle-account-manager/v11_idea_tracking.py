"""Sosyal medya fikir takibi (Ahmet 2026-10-13: New Business'taki takip kültürü sosyal medya
fikirlerinde de; markanın sosyal medya sorumlusuna sor, olumlu ya da olumsuz cevabı kaydet).

Each idea of the sent team digest's "Günün hamlesi" (Günün özü 'fikirler') becomes one todo
"Hamle Fikir: <Marka> × <konu>" in that brand's Sosyal Medya list (catalog brand_routes: verified,
lists hidden from clients, Hamle a member), assigned to the brand's social media lead, due the day
it opens (as New Business). These todos stay out of the team's due/overdue lists; the next weekday
digests read their comments (idea_follow_ups) and show the lines in "Dünkü önerilerim ne oldu?":
- the day after with no comment: one grouped question per lead;
- a comment: recorded as a yes (olumlu) and thanked; "pas" / "olmaz" / "hayır" / "uygun değil" /
  "yapmayalım" / "gerek yok": recorded as a no (olumsuz) and the idea is not suggested again;
- a yes, then 5 quiet days: a reminder asking to speed up; no comment for 7 days: a kind reminder;
  each once per quiet stretch;
- the todo no longer open: thanks once (kapandı).
Hamle never closes a task or changes a date. Off unless daily-digest-config idea_tracking_enabled.

Writes: POST todos only into the routed Sosyal Medya lists (allow-listed URLs). Same rules as New
Business: durable record before every write, one attempt, readback, anything uncertain is
'unknown' and never retried. Global stop ('dur') blocks every write.
"""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import re

from v11_nb_tracking import (APP, API, HAMLE_PERSON, MONTHS, NbBasecamp, _and, _code, _comment_id, _html, _line,
                             brand_key, text_matches)

PREFIX = "Hamle Fikir: "
STATE_KEY = "idea-tracking"
QUIET_DAYS, STALL_DAYS, MAX_NEW, MAX_READS, MAX_HISTORY, KEEP_DAYS, MAX_AVOID = 7, 5, 3, 15, 8, 45, 6
NO = re.compile(r"(?<![0-9a-zçğıöşü])(pas|olmaz|hayır|yapmayalım|uygun değil|gerek yok)(?![0-9a-zçğıöşü])")


class IdeaError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise IdeaError(code)


def empty_state():
    return {"kind": "idea_tracking", "schema_version": 1, "lists": {}, "ideas": {}}


def routes(catalog):
    """{project_id: {'list_id', 'person_id', 'brand_key'}}: the brand's Sosyal Medya list and lead,
    only for routes the Mac readback verified as client-hidden with Hamle a member."""
    out = {}
    for key, r in ((catalog or {}).get("brand_routes") or {}).items():
        if not isinstance(r, dict):
            continue
        pid, lid, person = r.get("project_id"), (r.get("lists") or {}).get("social"), r.get("sm_person_id")
        if r.get("verified") is True and r.get("lists_visible_to_clients") is False and r.get("hamle_member") is True \
                and all(type(x) is int and x > 0 for x in (pid, lid, person)):
            out[pid] = {"list_id": lid, "person_id": person, "brand_key": key}
    return out


def idea_key(brand, hook):
    norm = " ".join(str(hook or "").casefold().split())
    return f"{brand_key(brand)}:{hashlib.sha256(norm.encode('utf-8')).hexdigest()[:10]}"


def title(x):
    return PREFIX + _line(f"{x['brand']} × {x['hook']}", 150)


def is_idea_item(t):
    """A tracked idea todo (followed up in its own lines, not listed as team work)."""
    return t.get("kind") == "todo" and str(t.get("title") or "").startswith(PREFIX)


def tracked_keys(state):
    return {k for k, r in ((state or {}).get("ideas") or {}).items() if r.get("state") == "ready"}


def avoid_ideas(state, day):
    """'Marka × konu' of ideas the team said no to recently: the morning research must not repeat them."""
    since = (day - timedelta(days=KEEP_DAYS)).isoformat()
    rows = [r for r in ((state or {}).get("ideas") or {}).values()
            if r.get("answer") == "olumsuz" and (r.get("no_on") or "") >= since]
    rows.sort(key=lambda r: r.get("no_on") or "", reverse=True)
    return [f"{r['brand']} × {r['hook']}" for r in rows if r.get("brand") and r.get("hook")][:MAX_AVOID]


def description(x, day):
    return (f"Fikir: {_line(x['idea'], 600)}\n" + (f"Format: {_line(x['format'], 80)}\n" if x.get("format") else "")
            + f"Kaynak: {x['url']}\nÖneren: Hamle, {day.day} {MONTHS[day.month - 1]} {day.year} (Günün hamlesi)\n\n"
            "Ne düşündüğünüzü bu görevin altına tek satır yorum olarak yazın (ör. \"çekiyoruz, 16 Ekim\", "
            "\"müşteriye sunduk\"). Uygun değilse \"pas\" yazmanız yeterli; Hamle bunu olumsuz olarak not alır ve "
            "bu fikri bir daha önermez. Görevi kapatmak sizde.")


def suggestions(stats, rts):
    """The ideas of one sent team digest that have a verified Sosyal Medya route -> todo inputs."""
    out, seen = [], set()
    for x in (((stats or {}).get("ozu") or {}).get("fikirler")) or []:
        if not (isinstance(x, dict) and x.get("brand") and x.get("hook") and x.get("idea")
                and str(x.get("url") or "").startswith("https://") and x.get("project_id") in rts):
            continue
        key = idea_key(x["brand"], x["hook"])
        if len(key) > 11 and key not in seen:
            seen.add(key)
            out.append({"key": key, "brand": _line(x["brand"], 80), "hook": _line(x["hook"], 100),
                        "idea": _line(x["idea"], 600), "format": _line(x.get("format"), 80), "url": x["url"],
                        "project_id": x["project_id"], "who": _line(x.get("who"), 30) or None})
    return out


# ------------------------------------------------------------------ morning read (digest follow-ups)
def idea_follow_ups(state, *, reader, items, day, limit=8, prev_day=None, when="dün"):
    """Tracked idea todos -> follow-up rows (icon, prefix, url, text, suffix) + effects to keep once
    the team digest is sent: {'seen': {key: comment id}, 'yes': {key: day}, 'no': {key: day},
    'reminded': {key: last activity day}, 'done': [key], 'history': {key: [entry]}}."""
    rows, eff = [], {"seen": {}, "yes": {}, "no": {}, "reminded": {}, "done": [], "history": {}}
    open_by_id = {t["id"]: t for t in items if t.get("kind") == "todo"}
    groups, reads = {}, 0

    def note(key, kind, text=""):
        eff["history"].setdefault(key, []).append({"day": day.isoformat(), "kind": kind, "text": _line(text, 160)})

    def at(r):
        return f"@{r['who']}, " if r.get("who") else ""
    for key, r in sorted(((state or {}).get("ideas") or {}).items(), key=lambda kv: kv[1].get("suggested_on") or ""):
        if len(rows) + len(groups) >= limit:
            break
        if r.get("state") != "ready" or r.get("done_on") or r.get("answer") == "olumsuz":
            continue
        name = f"“{PREFIX}{r['brand']} × {r['hook']}”"
        t = open_by_id.get(r.get("todo_id"))
        if t is None or t.get("project_id") != r.get("project_id"):
            rows.append(("✅", "", r.get("app_url"), name, " artık açık değil, tamamlanmış olmalı."
                         + (f" Teşekkürler @{r['who']}!" if r.get("who") else " Teşekkürler!")))
            eff["done"].append(key)
            note(key, "kapandı")
            continue
        if reads >= MAX_READS:
            continue
        reads += 1
        try:
            comments = [c for c in reader.last_comments(t, n=3) if not str(c.get("by") or "").startswith("Hamle")]
        except Exception:
            continue
        latest = comments[-1] if comments else None
        if latest is not None and _comment_id(latest) != r.get("seen_comment"):
            eff["seen"][key] = _comment_id(latest)
            said = f": “{_line(latest.get('text'), 90)}” ({_line(latest.get('by'), 40)})"
            if NO.search(str(latest.get("text") or "").casefold()):
                rows.append(("👌", "", t["app_url"], name, said + ". Olumsuz olarak not aldım, bu fikri tekrar önermeyeceğim."))
                eff["no"][key] = day.isoformat()
                note(key, "olumsuz", latest.get("text"))
            else:
                rows.append(("👍", "", t["app_url"], name, said + ". Teşekkürler, takipteyim."))
                eff["yes"][key] = day.isoformat()
                note(key, "olumlu", latest.get("text"))
            continue
        opened = str(t.get("created_on") or r.get("suggested_on") or "")[:10]
        if not comments and prev_day is not None and opened == prev_day.isoformat():
            groups.setdefault(r.get("who") or "", []).append(f"{r['brand']} × {r['hook']}")
            continue
        last = max([str(c.get("at") or "")[:10] for c in comments if c.get("at")] + [opened])
        try:
            quiet = (day - date.fromisoformat(last)).days
        except ValueError:
            continue
        if r.get("reminded_for") == last:
            continue  # one reminder per quiet stretch
        if comments and quiet >= STALL_DAYS:
            rows.append(("", at(r), t["app_url"], name, f" son yorumdan bu yana {quiet} gündür sessiz; biraz hızlandıralım mı?"))
        elif not comments and quiet >= QUIET_DAYS:
            rows.append(("", at(r), t["app_url"], name, f" {quiet} gündür sessiz; yapalım mı, yoksa pas mı geçelim?"))
        else:
            continue
        eff["reminded"][key] = last
        note(key, "hatırlatma")
    lead = [("", (f"@{who}, " if who else "") + f"{when} açtığım fikir görevlerinde (", None, _and(names),
             ") henüz yorum yok; ne düşünüyorsunuz?") for who, names in sorted(groups.items())]
    return (lead + rows)[:limit], eff


# ------------------------------------------------------------------ Basecamp (routed Sosyal Medya lists only)
class IdeaBasecamp(NbBasecamp):
    """Allow-listed calls only: the routed lists, their todos, POST a todo into a routed list."""
    def __init__(self, account_id, rts, *, send):
        super().__init__(account_id, send=send)
        _need(isinstance(rts, dict) and rts, "idea_routes_missing")
        gets, posts = [], []
        for pid, r in rts.items():
            b = rf"/{account_id}/buckets/{pid}"
            gets += [rf"{b}/todolists/{r['list_id']}", rf"{b}/todos/[1-9][0-9]*"]
            posts.append(rf"{b}/todolists/{r['list_id']}/todos")
        self.allowed = {"GET": tuple(gets), "POST": tuple(posts)}


# ------------------------------------------------------------------ writes after the team digest is sent
class IdeaTracker:
    def __init__(self, ops, http, rts):
        self.ops, self.http, self.routes = ops, http, rts

    def base(self, pid):
        return f"{API}/{self.http.account}/buckets/{pid}"

    def state(self):
        return self.ops._run(lambda tx, _: tx.get(STATE_KEY), allow_expired=True) or empty_state()

    def _write(self, fn, *, fenced=True):
        def op(tx, now):
            if fenced:  # 'dur' / takeover: no new write starts
                c = self.ops._control(tx)
                self.ops._open(c, c["generation"], now)
            st = tx.get(STATE_KEY) or empty_state()
            out = fn(st, now)
            tx.put(STATE_KEY, st)
            return out
        return self.ops._run(op, allow_expired=not fenced)

    def apply(self, effects, day):
        """Yesterday's reading, kept only after that digest was really sent; old records pruned."""
        effects = effects if isinstance(effects, dict) else {}
        cutoff = (day - timedelta(days=KEEP_DAYS)).isoformat()

        def fn(st, now):
            n, ideas = 0, st["ideas"]
            for field, name in (("seen", "seen_comment"), ("reminded", "reminded_for")):
                for key, value in (effects.get(field) or {}).items():
                    if key in ideas:
                        ideas[key][name] = value; n += 1
            for field, answer in (("yes", "olumlu"), ("no", "olumsuz")):
                for key, value in (effects.get(field) or {}).items():
                    if key in ideas:
                        ideas[key].update(answer=answer, **{field + "_on": value}); n += 1
            for key in effects.get("done") or []:
                if key in ideas:
                    ideas[key]["done_on"] = day.isoformat(); n += 1
            for key, entries in (effects.get("history") or {}).items():
                if key in ideas and isinstance(entries, list):
                    ideas[key]["history"] = (list(ideas[key].get("history") or []) + entries)[-MAX_HISTORY:]
            for key in [k for k, r in ideas.items() if (r.get("suggested_on") or "") < cutoff]:
                del ideas[key]  # never re-opened: today's ideas are new research
            return n
        return self._write(fn, fenced=False)

    def ensure_list(self, pid):
        """The brand's Sosyal Medya list, checked once: in that project, hidden from clients, active."""
        lid = self.routes[pid]["list_id"]
        if (self.state().get("lists") or {}).get(str(lid)) == "ready":
            return lid
        back = self.http.one(f"{self.base(pid)}/todolists/{lid}.json")
        _need(back.get("id") == lid and (back.get("bucket") or {}).get("id") == pid
              and back.get("visible_to_clients") is False and back.get("status") in (None, "active")
              and back.get("type") in (None, "Todolist"), "idea_list_unusable")
        self._write(lambda st, now: st.setdefault("lists", {}).update({str(lid): "ready"}))
        return lid

    def add(self, x, day, due):
        pid = x["project_id"]
        route, key, name, text = self.routes[pid], x["key"], title(x), description(x, day)
        lid, person = route["list_id"], route["person_id"]

        def begin(st, now):
            if key in st["ideas"]:
                return False
            st["ideas"][key] = {"brand": x["brand"], "hook": x["hook"], "project_id": pid, "list_id": lid,
                                "person_id": person, "who": x["who"], "state": "writing", "todo_id": None,
                                "app_url": None, "suggested_on": day.isoformat(), "url": x["url"], "at": now}
            return True
        if not self._write(begin):
            return "already_tracked"
        try:
            row, _ = self.http.call("POST", f"{self.base(pid)}/todolists/{lid}/todos.json",
                                    {"content": name, "description": _html(text), "assignee_ids": [person],
                                     "completion_subscriber_ids": [], "notify": True, "due_on": due.isoformat()}, 201)
            tid = row.get("id") if isinstance(row, dict) else None
            _need(type(tid) is int and tid > 0, "idea_todo_receipt_invalid")
            back = self.http.one(f"{self.base(pid)}/todos/{tid}.json")
            parent = back.get("parent") or {}
            _need(back.get("id") == tid and back.get("content") == name and parent.get("id") == lid
                  and parent.get("type") == "Todolist" and (back.get("bucket") or {}).get("id") == pid
                  and (back.get("creator") or {}).get("id") == HAMLE_PERSON and back.get("visible_to_clients") is False
                  and sorted(a.get("id") for a in back.get("assignees") or [] if isinstance(a, dict)) == [person]
                  and back.get("due_on") == due.isoformat()
                  and text_matches(back.get("description"), text), "idea_todo_readback_mismatch")
        except Exception as exc:
            reason = _code(exc)
            self._write(lambda st, now: st["ideas"][key].update(state="unknown", reason=reason), fenced=False)
            return "unknown"
        app = back.get("app_url")
        if not (isinstance(app, str) and app.startswith(f"{APP}/{self.http.account}/buckets/{pid}/todos/")
                and len(app) <= 300 and not any(c in app for c in " <>\"'")):
            app = f"{APP}/{self.http.account}/buckets/{pid}/todos/{tid}"
        self._write(lambda st, now: st["ideas"][key].update(state="ready", todo_id=tid, app_url=app), fenced=False)
        return "created"


def run_idea_tracking(ops, *, http, routes, stats, day):
    """After the team digest is sent: keep this morning's reading effects, then open today's ideas."""
    report = {"status": "idea_tracking_completed", "created": 0, "already_tracked": 0, "unknown": 0, "effects": 0,
              "business_writes": 0}
    try:
        tracker = IdeaTracker(ops, http, routes)
        report["effects"] = tracker.apply((stats or {}).get("ideas"), day)
        tracked = tracker.state()["ideas"]
        fresh = [x for x in suggestions(stats, routes) if x["key"] not in tracked][:MAX_NEW]
        if not fresh:
            return dict(report, status="no_new_work")
        tracker._write(lambda st, now: None)  # 'dur' / takeover: held before any Basecamp call
        for x in fresh:
            try:
                tracker.ensure_list(x["project_id"])
            except Exception as exc:
                report.setdefault("lists_unusable", []).append(_code(exc))
                continue
            outcome = tracker.add(x, day, due=day)
            report["business_writes"] += outcome in ("created", "unknown")
            report[outcome] += 1
        return report
    except Exception as exc:
        return dict(report, status="held", reason=_code(exc))
