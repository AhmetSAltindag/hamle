"""New Business takibi (Ahmet 2026-10-13 07:04Z): NB suggestions -> todos in New Biz, morning reading. Fakes only."""
from datetime import date, datetime
import re
from urllib.parse import urlsplit

import v11_daily_digest as dd
import v11_nb_tracking as nb
from test_v11_daily_digest import ACC, APP, BASE, Base, todo

NB = nb.NB_PROJECT
TUE_0916 = datetime.fromisoformat("2026-10-13T09:16:00+03:00")
MON = date(2026, 10, 12)


def stats(**kw):
    s = {"new_business_brands": [
            {"brand": "Hisense", "sector": "elektronik", "reason": "Türkiye lansmanı.", "url": "https://example.org/h",
             "contacts": [{"email": "info@hisense.com.tr", "basis": "kaynakta", "url": "https://example.org/h"},
                          {"email": "pazarlama@hisense.com.tr", "basis": "kalıp", "url": None}]},
            {"brand": "lululemon", "sector": "spor giyim", "reason": "İlk mağaza.", "url": "https://example.org/l"},
            {"brand": "Alo Yoga", "sector": "spor giyim", "reason": "İstanbul'a geliyor.", "url": "https://example.org/alo"}],
         "ozu": {"firsat": {"kind": "new_business", "brand": "Vestel", "headline": "Yeni seri.", "proposal": "x",
                            "url": "https://example.org/v"}}}
    s.update(kw)
    return s


class FakeBasecamp:
    """Write-side fake for Ahmet's list in project 40704547 only."""
    def __init__(self, client_visible=False):
        self.calls, self.todos, self.next_id = [], {}, 9000
        self.fail_todo, self.client_visible = False, client_visible

    def send(self, method, url, body, expected):
        self.calls.append((method, url, body))
        path = urlsplit(url).path
        if path == f"/{ACC}/buckets/{NB}/todolists/{nb.LIST_ID}.json":
            return {"id": nb.LIST_ID, "name": "Hamle Öneriler", "type": "Todolist", "status": "active",
                    "bucket": {"id": NB}, "visible_to_clients": self.client_visible}, {}
        if path == f"/{ACC}/buckets/{NB}/todolists/{nb.LIST_ID}/todos.json" and method == "POST":
            assert expected == 201
            if self.fail_todo:
                raise ConnectionError("reset")
            self.next_id += 1
            tid = self.next_id
            self.todos[tid] = {"id": tid, "content": body["content"],
                               "description": body["description"].replace("https://example.org/h",
                                                                          '<a href="https://example.org/h">https://example.org/h</a>'),
                               "parent": {"id": nb.LIST_ID, "type": "Todolist"}, "bucket": {"id": NB},
                               "creator": {"id": nb.HAMLE_PERSON}, "visible_to_clients": False,
                               "assignees": [{"id": i, "name": "Cansu Akiş Kabil"} for i in body["assignee_ids"]],
                               "due_on": body["due_on"], "app_url": f"{APP}/buckets/{NB}/todos/{tid}"}
            return {"id": tid}, {}
        m = re.fullmatch(rf"/{ACC}/buckets/{NB}/todos/(\d+)\.json", path)
        if m:
            return dict(self.todos[int(m[1])]), {}
        raise AssertionError(url)

    def posts(self):
        return [(urlsplit(u).path, b) for m, u, b in self.calls if m == "POST"]


class Writes(Base):
    def run_nb(self, fake, s=None, day=MON, earlier=()):
        return nb.run_nb_tracking(self.ops, http=nb.NbBasecamp(ACC, send=fake.send), stats=s or stats(), day=day,
                                  earlier=earlier)

    def state(self):
        return self.ops.backend.data.get(nb.STATE_KEY)

    def test_each_monday_brand_becomes_one_todo_for_cansu_once(self):
        fake = FakeBasecamp()
        r = self.run_nb(fake)
        self.assertEqual((r["status"], r["created"], r["business_writes"]), ("nb_tracking_completed", 3, 3), r)
        posts = fake.posts()
        self.assertEqual({p for p, b in posts}, {f"/{ACC}/buckets/{NB}/todolists/{nb.LIST_ID}/todos.json"})
        todos = [b for p, b in posts]
        self.assertEqual([b["content"] for b in todos], ["Hamle Öneri: Hisense", "Hamle Öneri: lululemon", "Hamle Öneri: Alo Yoga"])  # no fırsat
        self.assertTrue(all(b["assignee_ids"] == [nb.CANSU] and b["notify"] is True and b["due_on"] == "2026-10-12"
                            for b in todos))  # Ahmet 07:38Z: deadline = the day it opens
        self.assertIn("Neden şimdi: Türkiye lansmanı.<br>Kaynak: https://example.org/h<br>İletişim (tahmini, "
                      "doğrulanmamış): info@hisense.com.tr (kaynak: https://example.org/h), pazarlama@hisense.com.tr "
                      "(kalıp)<br>İlk adım önerisi: " + nb.FIRST_STEP, todos[0]["description"])
        self.assertNotIn("İletişim", todos[1]["description"])
        self.assertIn("Öneren: Hamle, 12 Ekim 2026 (Pazartesi New Business listesi)", todos[1]["description"])
        st = self.state()
        self.assertEqual(st["list"]["id"], nb.LIST_ID)
        self.assertEqual({k: (v["state"], v["todo_id"]) for k, v in st["brands"].items()},
                         {"hisense": ("ready", 9001), "lululemon": ("ready", 9002), "aloyoga": ("ready", 9003)})
        self.assertEqual(st["brands"]["hisense"]["app_url"], f"{APP}/buckets/{NB}/todos/9001")
        r = self.run_nb(fake)
        self.assertEqual(r["status"], "no_new_work")
        self.assertEqual(len(fake.posts()), 3)

    def test_this_weeks_monday_list_is_caught_up_with_its_own_date(self):
        fake = FakeBasecamp()
        r = self.run_nb(fake, stats(new_business_brands=[]), day=date(2026, 10, 14), earlier=[(MON, stats())])
        self.assertEqual(r["created"], 3, r)
        self.assertIn("12 Ekim 2026 (Pazartesi New Business listesi)", fake.posts()[0][1]["description"])
        self.assertEqual(fake.posts()[0][1]["due_on"], "2026-10-14")
        self.assertEqual(self.state()["brands"]["hisense"]["suggested_on"], "2026-10-12")

    def test_a_list_clients_could_see_is_never_written(self):
        fake = FakeBasecamp(client_visible=True)
        r = self.run_nb(fake)
        self.assertEqual((r["status"], r["reason"]), ("held", "nb_list_unusable"))
        self.assertEqual(fake.posts(), [])

    def test_uncertain_todo_write_is_never_retried(self):
        one = stats(new_business_brands=stats()["new_business_brands"][:1])
        fake = FakeBasecamp()
        fake.fail_todo = True
        r = self.run_nb(fake, one)
        self.assertEqual((r["unknown"], r["business_writes"]), (1, 1), r)
        self.assertEqual(self.state()["brands"]["hisense"]["state"], "unknown")
        fake.fail_todo = False
        r = self.run_nb(fake, one)
        self.assertEqual(r["status"], "no_new_work")
        self.assertEqual(len(fake.posts()), 1)

    def test_global_stop_blocks_every_write(self):
        self.ops.stop("ahmet-dur")
        fake = FakeBasecamp()
        r = self.run_nb(fake)
        self.assertEqual(r["status"], "held")
        self.assertEqual(fake.posts(), [])

    def test_only_ahmets_list_is_reachable(self):
        http = nb.NbBasecamp(ACC, send=lambda *a: ({}, {}))
        for method, url in (("POST", f"{BASE}/buckets/40694338/todolists/{nb.LIST_ID}/todos.json"),
                            ("POST", f"{BASE}/buckets/{NB}/todolists/1/todos.json"),
                            ("POST", f"{BASE}/buckets/{NB}/todosets/71/todolists.json"),
                            ("GET", f"{BASE}/buckets/{NB}/todos/1.json?page=1&x=2"),
                            ("POST", f"{BASE}/buckets/{NB}/todos/1/comments.json"),
                            ("PUT", f"{BASE}/buckets/{NB}/todos/1.json"),
                            ("GET", f"https://evil.example/{ACC}/buckets/{NB}/todolists/{nb.LIST_ID}.json")):
            with self.assertRaises(nb.NbError):
                http.call(method, url)


class NbProject(Base):
    """Digest fake with the New Biz project and tracked NB todos in every state (no tests here)."""
    def setUp(self):
        super().setUp()
        projects, _ = self.bc.pages[f"{BASE}/projects.json"]
        projects.append({"id": NB, "name": "New Biz", "status": "active",
                         "dock": [{"name": "todoset", "id": 71, "enabled": True}]})
        self.bc.pages[f"{BASE}/buckets/{NB}/todosets/71/todolists.json"] = ([{"id": nb.LIST_ID}], {})
        rows = [todo(t, f"Hamle Öneri: {b}", c, [(nb.CANSU, "Cansu Akiş Kabil")], created_at=f"{c}T09:00:00Z")
                for t, b, c in ((9001, "Hisense", "2026-10-05"), (9002, "lululemon", "2026-10-05"),
                                (9003, "Alo Yoga", "2026-10-05"), (9005, "Vestel", "2026-10-12"),
                                (9006, "Arçelik", "2026-10-05"))]
        rows.append(todo(9100, "Hamle New Business Öneriler", "2026-10-18", [(nb.CANSU, "Cansu Akiş Kabil")]))
        self.bc.pages[f"{BASE}/buckets/{NB}/todolists/{nb.LIST_ID}/todos.json"] = (rows, {})
        said = {9001: ("2026-10-12T15:00:00Z", "<p>Aradık, toplantı 20 Ekim.</p>"),
                9002: ("2026-10-12T16:00:00Z", "Şimdilik pas."), 9006: ("2026-10-07T10:00:00Z", "Sunum gönderildi.")}
        for tid in (9001, 9002, 9003, 9005, 9006):
            c = said.get(tid)
            self.bc.pages[f"{BASE}/buckets/{NB}/recordings/{tid}/comments.json"] = (
                [{"created_at": c[0], "creator": {"name": "Cansu Akiş Kabil"}, "content": c[1]}] if c else [], {})
        brands = {}
        for key, name, tid, on in (("hisense", "Hisense", 9001, "2026-10-05"), ("lululemon", "lululemon", 9002, "2026-10-05"),
                                   ("aloyoga", "Alo Yoga", 9003, "2026-10-05"), ("getir", "Getir", 9004, "2026-10-05"),
                                   ("vestel", "Vestel", 9005, "2026-10-12"), ("arçelik", "Arçelik", 9006, "2026-10-05")):
            brands[key] = {"brand": name, "state": "ready", "todo_id": tid, "suggested_on": on,
                           "app_url": f"{APP}/buckets/{NB}/todos/{tid}", "source": "Pazartesi New Business listesi"}
        brands["arçelik"]["seen_comment"] = nb._comment_id(
            {"at": "2026-10-07", "by": "Cansu Akiş Kabil", "text": "Sunum gönderildi."})
        self.state = {"kind": "nb_tracking", "schema_version": 1, "list": {"state": "ready", "id": nb.LIST_ID},
                      "brands": brands}
        self.ops.backend.data[nb.STATE_KEY] = self.state


class MorningReading(NbProject):
    START = TUE_0916

    def test_ahmets_follow_up_rules_each_fire_once_at_the_bottom(self):
        r = self.run_digest()
        self.assertEqual(r["status"], "digest_sent", r)
        text = self.gmail.text()
        self.assertNotIn("ÖNERİLERİM NE OLDU?", text)  # NB lines are not in the top follow-up section
        top = text[:text.index("NEW BUSINESS (@Cansu)")]
        self.assertNotIn("Hamle Öneri:", top)  # overdue suggestion todos stay out of the team lists
        section = text[text.index("NEW BUSINESS (@Cansu)"):]
        self.assertGreater(text.index("NEW BUSINESS (@Cansu)"), text.index("BUGÜN"))
        for needle in ("- @Cansu, dün açtığım New Business görevlerinde (Vestel) henüz hareket yok; bir adım attık mı?",
                       f"- 👍 “Hamle Öneri: Hisense” ({APP}/buckets/{NB}/todos/9001): “Aradık, toplantı 20 Ekim.” "
                       "(Cansu Akiş Kabil). Teşekkürler, takipteyim.",
                       f"- 👌 “Hamle Öneri: lululemon” ({APP}/buckets/{NB}/todos/9002) için pas dendi; bu markayı 3 ay "
                       "önermeyeceğim.",
                       f"- @Cansu, “Hamle Öneri: Alo Yoga” ({APP}/buckets/{NB}/todos/9003) 8 gündür sessiz; bir adım atalım mı?",
                       f"- @Cansu, “Hamle Öneri: Arçelik” ({APP}/buckets/{NB}/todos/9006) son yorumdan bu yana 6 gündür "
                       "sessiz; biraz hızlandıralım mı?",
                       f"- ✅ “Hamle Öneri: Getir” ({APP}/buckets/{NB}/todos/9004) artık açık değil, tamamlanmış olmalı. "
                       "Teşekkürler @Cansu!", dd.NB_HINT):
            self.assertIn(needle, section)
        self.assertIn("🚀 New Business (@Cansu)", self.gmail.html())
        effects = r["stats"]["nb"]
        self.assertEqual((effects["passes"], effects["reminded"], effects["done"], sorted(effects["seen"])),
                         (["lululemon"], {"aloyoga": "2026-10-05", "arçelik": "2026-10-07"}, ["getir"],
                          ["hisense", "lululemon"]))
        self.assertEqual([h["kind"] for h in effects["history"]["lululemon"]], ["olumsuz"])
        self.assertEqual([h["kind"] for h in effects["history"]["hisense"]], ["ilerleme"])
        out = nb.run_nb_tracking(self.ops, http=nb.NbBasecamp(ACC, send=FakeBasecamp().send),
                                 stats=r["stats"], day=date(2026, 10, 13))
        self.assertEqual((out["status"], out["effects"]), ("no_new_work", 6), out)
        brands = self.ops.backend.data[nb.STATE_KEY]["brands"]
        self.assertEqual(brands["lululemon"]["passed_until"], "2027-01-11")
        self.assertEqual((brands["aloyoga"]["reminded_for"], brands["getir"]["done_on"]), ("2026-10-05", "2026-10-13"))
        self.assertEqual(brands["hisense"]["history"][0]["text"], "Aradık, toplantı 20 Ekim.")
        items = self.reader().collect()["items"]
        rows, _ = nb.nb_follow_ups(self.ops.backend.data[nb.STATE_KEY], reader=self.reader(), items=items,
                                   day=date(2026, 10, 14), prev_day=date(2026, 10, 13))
        self.assertEqual(rows, [])  # nothing repeats the next day
        self.assertEqual(nb.avoid_brands(self.ops.backend.data[nb.STATE_KEY], date(2026, 10, 14)),
                         ["Hisense", "lululemon", "Alo Yoga", "Getir", "Vestel", "Arçelik"])


class MondayList(NbProject):
    START = datetime.fromisoformat("2026-10-12T09:16:00+03:00")

    def test_monday_list_skips_tracked_brands_shows_contacts_at_the_bottom(self):
        found = {"status": "ok", "brands": [
            {"brand": "Hisense", "sector": "elektronik", "reason": "Lansman.", "url": "https://example.org/h", "contacts": []},
            {"brand": "Beko", "sector": "beyaz eşya", "reason": "Yeni seri.", "url": "https://example.org/b",
             "contacts": [{"email": "info@beko.com.tr", "basis": "kaynakta", "url": "https://example.org/b"}]}]}
        r = self.run_digest(new_business_source=lambda day: found)
        self.assertEqual(r["status"], "digest_sent", r)
        self.assertEqual([b["brand"] for b in r["stats"]["new_business_brands"]], ["Beko"])
        text = self.gmail.text()
        self.assertIn("- Beko (beyaz eşya): Yeni seri. · https://example.org/b · İletişim (tahmini, doğrulanmamış): "
                      "info@beko.com.tr (kaynak: https://example.org/b)", text)
        self.assertGreater(text.index("Bu haftanın hedef markaları:"), text.index("BUGÜN"))
        self.assertIn("tahmini, doğrulanmamış", self.gmail.html())

    def test_untracked_brands_keep_the_old_question_tracked_ones_do_not(self):
        prev = {"day": "2026-10-09", "new_business_brands": [{"brand": "Hisense"}, {"brand": "Beko"}], "ozu": {}}
        items = [dict(t, created_on="2026-10-12") for t in self.reader().collect()["items"]]
        out = dd.follow_ups(prev, reader=self.reader(), items=items, day=date(2026, 10, 12), nb_state=self.state)
        self.assertIn(("", "@Cansu, Cuma günü New Business'ta önerdiğim ", None, "Beko", " için bir adım attık mı?"),
                      out["nb_rows"])
        self.assertEqual(out["rows"], [])


class OperatorOpen(NbProject):
    START = TUE_0916

    def test_opens_this_weeks_monday_brands_now_once(self):
        from unittest.mock import patch
        self.assertEqual(dd.run_worker_nb_open(self.ops, None, clock=lambda: self.now)["reason"], "monday_digest_not_sent")
        self.ops.backend.data["daily-digest-20261012"] = {"state": "sent", "stats": stats()}
        self.ops.backend.data[nb.STATE_KEY] = nb.empty_state()
        fake = FakeBasecamp()
        with patch.object(nb, "session_send", lambda token, factory: fake.send):
            r = dd.run_worker_nb_open(self.ops, None, clock=lambda: self.now)
            self.assertEqual(r["created"], 3, r)
            self.assertEqual({b["due_on"] for p, b in fake.posts()}, {"2026-10-13"})
            self.assertEqual(dd.run_worker_nb_open(self.ops, None, clock=lambda: self.now)["status"], "no_new_work")
        self.assertEqual(len(fake.posts()), 3)
