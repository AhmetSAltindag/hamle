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
            {"brand": "Hisense", "sector": "elektronik", "reason": "Türkiye lansmanı.", "url": "https://example.org/h"},
            {"brand": "lululemon", "sector": "spor giyim", "reason": "İlk mağaza.", "url": "https://example.org/l"}],
         "ozu": {"firsat": {"kind": "new_business", "brand": "Alo Yoga", "headline": "Alo Yoga İstanbul'a geliyor.",
                            "proposal": "Şunu önerebiliriz: açılış için UGC serisi.", "url": "https://example.org/alo"}}}
    s.update(kw)
    return s


class FakeBasecamp:
    """Write-side fake for project 40704547 only."""
    def __init__(self, lists=()):
        self.calls, self.lists, self.todos, self.next_id = [], [dict(x) for x in lists], {}, 9000
        self.fail_todo, self.client_visible = False, False

    def send(self, method, url, body, expected):
        self.calls.append((method, url, body))
        path = urlsplit(url).path
        if path == f"/{ACC}/projects/{NB}.json":
            return {"id": NB, "dock": [{"name": "todoset", "id": 71, "enabled": True}]}, {}
        if path == f"/{ACC}/buckets/{NB}/todosets/71/todolists.json":
            if method == "GET":
                return [dict(x) for x in self.lists], {}
            assert expected == 201
            self.lists.append({"id": 701, "name": body["name"], "title": body["name"], "bucket": {"id": NB},
                               "visible_to_clients": self.client_visible, "creator": {"id": nb.HAMLE_PERSON}})
            return {"id": 701}, {}
        m = re.fullmatch(rf"/{ACC}/buckets/{NB}/todolists/(\d+)\.json", path)
        if m:
            return next(dict(x) for x in self.lists if x["id"] == int(m[1])), {}
        m = re.fullmatch(rf"/{ACC}/buckets/{NB}/todolists/(\d+)/todos\.json", path)
        if m and method == "POST":
            if self.fail_todo:
                raise ConnectionError("reset")
            self.next_id += 1
            tid = self.next_id
            self.todos[tid] = {"id": tid, "content": body["content"],
                               "description": body["description"].replace("https://example.org/h",
                                                                          '<a href="https://example.org/h">https://example.org/h</a>'),
                               "parent": {"id": int(m[1]), "type": "Todolist"}, "bucket": {"id": NB},
                               "creator": {"id": nb.HAMLE_PERSON}, "visible_to_clients": False,
                               "assignees": [{"id": i, "name": "Cansu Akiş Kabil"} for i in body["assignee_ids"]],
                               "app_url": f"{APP}/buckets/{NB}/todos/{tid}"}
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

    def test_each_suggestion_becomes_one_todo_for_cansu_once(self):
        fake = FakeBasecamp()
        r = self.run_nb(fake)
        self.assertEqual((r["status"], r["created"], r["business_writes"]), ("nb_tracking_completed", 3, 3), r)
        posts = fake.posts()
        self.assertEqual(posts[0], (f"/{ACC}/buckets/{NB}/todosets/71/todolists.json",
                                    {"name": "Hamle Önerileri", "description": nb._html(nb.LIST_DESCRIPTION)}))
        todos = [b for p, b in posts[1:]]
        self.assertEqual([b["content"] for b in todos], ["NB // Hisense", "NB // lululemon", "NB // Alo Yoga"])
        self.assertTrue(all(b["assignee_ids"] == [nb.CANSU] and b["notify"] is True for b in todos))
        self.assertIn("Neden şimdi: Türkiye lansmanı.<br>Kaynak: https://example.org/h<br>İlk adım önerisi: "
                      + nb.FIRST_STEP, todos[0]["description"])
        self.assertIn("İlk adım önerisi: Şunu önerebiliriz: açılış için UGC serisi.", todos[2]["description"])
        self.assertIn("Öneren: Hamle, 12 Ekim 2026 (Günün özü fırsatı)", todos[2]["description"])
        st = self.state()
        self.assertEqual(st["list"]["id"], 701)
        self.assertEqual({k: (v["state"], v["todo_id"]) for k, v in st["brands"].items()},
                         {"hisense": ("ready", 9001), "lululemon": ("ready", 9002), "aloyoga": ("ready", 9003)})
        self.assertEqual(st["brands"]["hisense"]["app_url"], f"{APP}/buckets/{NB}/todos/9001")
        r = self.run_nb(fake)
        self.assertEqual(r["status"], "no_new_work")
        self.assertEqual(len(fake.posts()), 4)

    def test_this_weeks_monday_list_is_caught_up_with_its_own_date(self):
        fake = FakeBasecamp()
        monday = stats(ozu={}, new_business_brands=stats()["new_business_brands"] + [
            {"brand": "Getir Yemek (Uber)", "reason": "Ortaklık.", "url": "https://example.org/g"}])
        r = self.run_nb(fake, stats(new_business_brands=[]), day=date(2026, 10, 14),
                        earlier=[(MON, monday)])
        self.assertEqual(r["created"], 4, r)
        todos = [b for p, b in fake.posts()[1:]]
        self.assertEqual([b["content"] for b in todos],
                         ["NB // Alo Yoga", "NB // Hisense", "NB // lululemon", "NB // Getir Yemek (Uber)"])
        self.assertIn("14 Ekim 2026 (Günün özü fırsatı)", todos[0]["description"])
        self.assertIn("12 Ekim 2026 (Pazartesi New Business listesi)", todos[1]["description"])
        self.assertEqual(self.state()["brands"]["hisense"]["suggested_on"], "2026-10-12")

    def test_existing_list_is_reused_by_its_exact_name(self):
        fake = FakeBasecamp(lists=[{"id": 555, "name": "Hamle Önerileri"}, {"id": 556, "name": "Hisense"}])
        r = self.run_nb(fake, stats(ozu={}))
        self.assertEqual(r["created"], 2, r)
        self.assertEqual({p for p, b in fake.posts()}, {f"/{ACC}/buckets/{NB}/todolists/555/todos.json"})

    def test_list_that_clients_could_see_stops_everything_for_good(self):
        fake = FakeBasecamp()
        fake.client_visible = True
        r = self.run_nb(fake)
        self.assertEqual((r["status"], r["reason"]), ("held", "nb_list_outcome_unknown"))
        self.assertEqual(self.state()["list"]["state"], "unknown")
        r = self.run_nb(fake)
        self.assertEqual((r["status"], r["reason"]), ("held", "nb_list_outcome_unknown"))
        self.assertEqual(len(fake.posts()), 1)  # the one list create, never a second one, no todos

    def test_uncertain_todo_write_is_never_retried(self):
        fake = FakeBasecamp()
        fake.fail_todo = True
        r = self.run_nb(fake, stats(ozu={}, new_business_brands=stats()["new_business_brands"][:1]))
        self.assertEqual((r["unknown"], r["business_writes"]), (1, 1), r)
        self.assertEqual(self.state()["brands"]["hisense"]["state"], "unknown")
        fake.fail_todo = False
        r = self.run_nb(fake, stats(ozu={}, new_business_brands=stats()["new_business_brands"][:1]))
        self.assertEqual(r["status"], "no_new_work")
        self.assertEqual(len(fake.posts()), 2)  # list + the one uncertain todo

    def test_global_stop_blocks_every_write(self):
        self.ops.stop("ahmet-dur")
        fake = FakeBasecamp()
        r = self.run_nb(fake)
        self.assertEqual(r["status"], "held")
        self.assertEqual(fake.posts(), [])

    def test_only_the_new_biz_project_is_reachable(self):
        http = nb.NbBasecamp(ACC, send=lambda *a: ({}, {}))
        for method, url in (("POST", f"{BASE}/buckets/40694338/todolists/1/todos.json"),
                            ("GET", f"{BASE}/buckets/{NB}/todos/1.json?page=1&x=2"),
                            ("POST", f"{BASE}/buckets/{NB}/todos/1/comments.json"),
                            ("PUT", f"{BASE}/buckets/{NB}/todos/1.json"),
                            ("GET", f"https://evil.example/{ACC}/projects/{NB}.json")):
            with self.assertRaises(nb.NbError):
                http.call(method, url)


class NbProject(Base):
    """Digest fake with the New Biz project, three tracked NB todos and one closed (no tests here)."""
    def setUp(self):
        super().setUp()
        projects, _ = self.bc.pages[f"{BASE}/projects.json"]
        projects.append({"id": NB, "name": "New Biz", "status": "active",
                         "dock": [{"name": "todoset", "id": 71, "enabled": True}]})
        self.bc.pages[f"{BASE}/buckets/{NB}/todosets/71/todolists.json"] = ([{"id": 701}], {})
        self.bc.pages[f"{BASE}/buckets/{NB}/todolists/701/todos.json"] = (
            [todo(t, f"NB // {b}", None, [(nb.CANSU, "Cansu Akiş Kabil")], created_at="2026-10-05T09:00:00Z")
             for t, b in ((9001, "Hisense"), (9002, "lululemon"), (9003, "Alo Yoga"))], {})
        self.bc.pages[f"{BASE}/buckets/{NB}/recordings/9001/comments.json"] = (
            [{"created_at": "2026-10-12T15:00:00Z", "creator": {"name": "Cansu Akiş Kabil"},
              "content": "<p>Aradık, toplantı 20 Ekim.</p>"}], {})
        self.bc.pages[f"{BASE}/buckets/{NB}/recordings/9002/comments.json"] = (
            [{"created_at": "2026-10-12T16:00:00Z", "creator": {"name": "Cansu Akiş Kabil"}, "content": "Şimdilik pas."}], {})
        self.bc.pages[f"{BASE}/buckets/{NB}/recordings/9003/comments.json"] = ([], {})
        brands = {}
        for key, name, tid in (("hisense", "Hisense", 9001), ("lululemon", "lululemon", 9002),
                               ("aloyoga", "Alo Yoga", 9003), ("getir", "Getir", 9004)):
            brands[key] = {"brand": name, "state": "ready", "todo_id": tid, "suggested_on": "2026-10-05",
                           "app_url": f"{APP}/buckets/{NB}/todos/{tid}", "source": "Pazartesi New Business listesi"}
        self.ops.backend.data[nb.STATE_KEY] = {"kind": "nb_tracking", "schema_version": 1,
                                               "list": {"state": "ready", "id": 701}, "brands": brands}



class MorningReading(NbProject):
    START = TUE_0916

    def test_comment_pass_quiet_and_closed_todos_each_get_one_line_once(self):
        r = self.run_digest()
        self.assertEqual(r["status"], "digest_sent", r)
        text = self.gmail.text()
        section = text[text.index("ÖNERİLERİM NE OLDU?"):text.index("BUGÜN")]
        for needle in (f"- 👍 “NB // Hisense” ({APP}/buckets/{NB}/todos/9001): “Aradık, toplantı 20 Ekim.” "
                       "(Cansu Akiş Kabil). Teşekkürler, takipteyim.",
                       f"- 👌 “NB // lululemon” ({APP}/buckets/{NB}/todos/9002) için pas dendi; bu markayı 3 ay "
                       "önermeyeceğim.",
                       f"- @Cansu, “NB // Alo Yoga” ({APP}/buckets/{NB}/todos/9003) 8 gündür sessiz; bir adım atalım mı?",
                       f"- ✅ “NB // Getir” ({APP}/buckets/{NB}/todos/9004) artık açık değil, tamamlanmış olmalı. "
                       "Teşekkürler @Cansu!"):
            self.assertIn(needle, section)
        effects = r["stats"]["nb"]
        self.assertEqual((effects["passes"], effects["reminded"], effects["done"], sorted(effects["seen"])),
                         (["lululemon"], ["aloyoga"], ["getir"], ["hisense", "lululemon"]))
        out = nb.run_nb_tracking(self.ops, http=nb.NbBasecamp(ACC, send=FakeBasecamp().send),
                                 stats=r["stats"], day=date(2026, 10, 13))
        self.assertEqual((out["status"], out["effects"]), ("no_new_work", 5), out)
        brands = self.ops.backend.data[nb.STATE_KEY]["brands"]
        self.assertEqual(brands["lululemon"]["passed_until"], "2027-01-11")
        self.assertEqual((brands["aloyoga"]["reminded_on"], brands["getir"]["done_on"]), ("2026-10-13", "2026-10-13"))
        items = self.reader().collect()["items"]
        rows, _ = nb.nb_follow_ups(self.ops.backend.data[nb.STATE_KEY], reader=self.reader(), items=items,
                                   day=date(2026, 10, 14))
        self.assertEqual(rows, [])  # nothing repeats: comment seen, pass kept, one reminder, done thanked
        self.assertEqual(nb.avoid_brands(self.ops.backend.data[nb.STATE_KEY], date(2026, 10, 14)),
                         ["Hisense", "lululemon", "Alo Yoga", "Getir"])



class MondayList(NbProject):
    START = datetime.fromisoformat("2026-10-12T09:16:00+03:00")

    def test_monday_list_skips_tracked_brands_and_hamle_todos_are_not_praised(self):
        found = {"status": "ok", "brands": [
            {"brand": "Hisense", "sector": "elektronik", "reason": "Lansman.", "url": "https://example.org/h"},
            {"brand": "Vestel", "sector": "elektronik", "reason": "Yeni seri.", "url": "https://example.org/v"}]}
        r = self.run_digest(new_business_source=lambda day: found)
        self.assertEqual(r["status"], "digest_sent", r)
        self.assertEqual([b["brand"] for b in r["stats"]["new_business_brands"]], ["Vestel"])
        prev = {"day": "2026-10-12", "new_business_brands": [{"brand": "Hisense"}], "ozu": {}}
        items = [dict(t, created_on="2026-10-12") for t in self.reader().collect()["items"]]
        out = dd.follow_ups(prev, reader=self.reader(), items=items, day=date(2026, 10, 13))
        self.assertIn(("", "@Cansu, dün New Business'ta önerdiğim ", None, "Hisense", " için bir adım attık mı?"),
                      out["rows"])
