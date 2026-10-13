"""Sosyal medya fikir takibi (Ahmet 2026-10-13 07:57Z card "Görev aç"): Günün hamlesi ideas -> todos for the
brand's social media lead, morning reading of their comments. Fakes only."""
from datetime import date, datetime
import re
from unittest.mock import patch
from urllib.parse import urlsplit

import v11_daily_digest as dd
import v11_idea_tracking as it
import v11_nb_tracking as nb
from test_v11_daily_digest import ACC, APP, BASE, Base, config, todo
from test_v11_digest_ozu import spark
from v11_chain_live import load_catalog

TUE_0916 = datetime.fromisoformat("2026-10-13T09:16:00+03:00")
TUE = date(2026, 10, 13)
ALTIN, WAT, FENER = 40704523, 40694338, 44320349
ROUTES = it.routes(load_catalog())
ESMA, OZGE = 53167312, 52096503


def idea(brand, pid, hook, who, **kw):
    x = {"brand": brand, "project_id": pid, "hook": hook, "url": "https://example.org/" + hook.split()[0].lower(),
         "who": who, "idea": f"{hook} için kısa video.", "format": "Reels"}
    x.update(kw)
    return x


def stats(*ideas, **kw):
    s = {"ozu": {"fikirler": list(ideas) or [idea("Altınmarka", ALTIN, "Dünya Gıda Günü", "Esma", format="carousel"),
                                             idea("WAT Mobilite", WAT, "Mini vlog akımı", "Özge"),
                                             idea("Fenerium", FENER, "Derbi haftası", "Ahmet"),
                                             idea("Bikaldi", 40704530, "Kaynaksız", "Esma", url="http://x")]}}
    s.update(kw)
    return s


class FakeBasecamp:
    """Write-side fake for the routed Sosyal Medya lists."""
    def __init__(self, client_visible=()):
        self.calls, self.todos, self.next_id = [], {}, 9000
        self.fail_todo, self.client_visible = False, set(client_visible)
        self.lists = {r["list_id"]: pid for pid, r in ROUTES.items()}

    def send(self, method, url, body, expected):
        self.calls.append((method, url, body))
        path = urlsplit(url).path
        m = re.fullmatch(rf"/{ACC}/buckets/(\d+)/todolists/(\d+)(/todos)?\.json", path)
        if m and self.lists.get(int(m[2])) == int(m[1]):
            pid, lid = int(m[1]), int(m[2])
            if not m[3]:
                return {"id": lid, "name": "Sosyal Medya", "type": "Todolist", "status": "active", "bucket": {"id": pid},
                        "visible_to_clients": pid in self.client_visible}, {}
            assert method == "POST" and expected == 201
            if self.fail_todo:
                raise ConnectionError("reset")
            self.next_id += 1
            tid = self.next_id
            self.todos[tid] = {"id": tid, "content": body["content"], "description": body["description"],
                               "parent": {"id": lid, "type": "Todolist"}, "bucket": {"id": pid},
                               "creator": {"id": nb.HAMLE_PERSON}, "visible_to_clients": False,
                               "assignees": [{"id": i} for i in body["assignee_ids"]],
                               "due_on": body["due_on"], "app_url": f"{APP}/buckets/{pid}/todos/{tid}"}
            return {"id": tid}, {}
        m = re.fullmatch(rf"/{ACC}/buckets/(\d+)/todos/(\d+)\.json", path)
        if m:
            return dict(self.todos[int(m[2])]), {}
        raise AssertionError(url)

    def posts(self):
        return [(urlsplit(u).path, b) for m, u, b in self.calls if m == "POST"]


class Routes(Base):
    def test_only_verified_client_hidden_social_lists_with_their_lead(self):
        self.assertEqual(ROUTES[ALTIN], {"list_id": 8238555593, "person_id": ESMA, "brand_key": "altinmarka"})
        self.assertEqual(ROUTES[WAT], {"list_id": 8231779952, "person_id": OZGE, "brand_key": "wat_mobilite"})
        self.assertEqual(len(ROUTES), 8)
        self.assertNotIn(FENER, ROUTES)  # no verified Sosyal Medya list
        self.assertNotIn(40704547, ROUTES)  # New Biz has its own list
        cat = load_catalog()
        cat["brand_routes"]["altinmarka"] = dict(cat["brand_routes"]["altinmarka"], lists_visible_to_clients=True)
        self.assertNotIn(ALTIN, it.routes(cat))

    def test_disk_config_turns_idea_tracking_on_and_module_is_packaged(self):
        import package_chain_source as pkg
        self.assertIn("v11_idea_tracking.py", pkg.CHAIN_FILES)
        self.assertIs(dd.validate_config(dd.load_config())["idea_tracking"], True)
        self.assertIs(dd.validate_config(config())["idea_tracking"], False)
        with self.assertRaises(dd.DigestError):
            dd.validate_config(config(idea_tracking_enabled="true"))

    def test_only_routed_social_lists_are_reachable(self):
        http = it.IdeaBasecamp(ACC, ROUTES, send=lambda *a: ({}, {}))
        http.call("POST", f"{BASE}/buckets/{WAT}/todolists/8231779952/todos.json")
        for method, url in (("POST", f"{BASE}/buckets/{WAT}/todolists/8234557425/todos.json"),  # Projeler list
                            ("POST", f"{BASE}/buckets/{ALTIN}/todolists/8231779952/todos.json"),  # other brand's list
                            ("POST", f"{BASE}/buckets/{nb.NB_PROJECT}/todolists/{nb.LIST_ID}/todos.json"),
                            ("GET", f"{BASE}/buckets/{FENER}/todos/1.json"),
                            ("POST", f"{BASE}/buckets/{WAT}/recordings/1/comments.json"),
                            ("PUT", f"{BASE}/buckets/{WAT}/todos/1.json"),
                            ("GET", f"https://evil.example/{ACC}/buckets/{WAT}/todolists/8231779952.json")):
            with self.assertRaises(nb.NbError):
                http.call(method, url)


class Writes(Base):
    START = TUE_0916

    def run_ideas(self, fake, s=None, day=TUE):
        return it.run_idea_tracking(self.ops, http=it.IdeaBasecamp(ACC, ROUTES, send=fake.send), routes=ROUTES,
                                    stats=s or stats(), day=day)

    def state(self):
        return self.ops.backend.data.get(it.STATE_KEY)

    def test_each_routed_idea_becomes_one_todo_for_the_brand_lead_once(self):
        fake = FakeBasecamp()
        r = self.run_ideas(fake)
        self.assertEqual((r["status"], r["created"], r["business_writes"]), ("idea_tracking_completed", 2, 2), r)
        posts = fake.posts()
        self.assertEqual([p for p, b in posts], [f"/{ACC}/buckets/{ALTIN}/todolists/8238555593/todos.json",
                                                 f"/{ACC}/buckets/{WAT}/todolists/8231779952/todos.json"])
        a, w = (b for p, b in posts)
        self.assertEqual((a["content"], a["assignee_ids"], a["due_on"], a["notify"]),
                         ("Hamle Fikir: Altınmarka × Dünya Gıda Günü", [ESMA], "2026-10-13", True))
        self.assertEqual((w["content"], w["assignee_ids"]), ("Hamle Fikir: WAT Mobilite × Mini vlog akımı", [OZGE]))
        self.assertIn("Fikir: Dünya Gıda Günü için kısa video.<br>Format: carousel<br>Kaynak: https://example.org/dünya"
                      "<br>Öneren: Hamle, 13 Ekim 2026 (Günün hamlesi)", a["description"])
        self.assertIn("&quot;pas&quot;", a["description"])
        st = self.state()
        self.assertEqual(sorted((v["brand"], v["state"], v["todo_id"], v["who"]) for v in st["ideas"].values()),
                         [("Altınmarka", "ready", 9001, "Esma"), ("WAT Mobilite", "ready", 9002, "Özge")])
        self.assertEqual(st["lists"], {"8238555593": "ready", "8231779952": "ready"})
        self.assertEqual(self.run_ideas(fake)["status"], "no_new_work")
        self.assertEqual(len(fake.posts()), 2)

    def test_records_sent_before_the_idea_text_was_kept_still_open(self):
        legacy = {k: v for k, v in idea("WAT Mobilite", WAT, "Mini vlog akımı", "Özge").items() if k not in ("idea", "format")}
        fake = FakeBasecamp()
        self.assertEqual(self.run_ideas(fake, stats(legacy))["created"], 1)
        self.assertIn("Fikir: ayrıntısı 13 Ekim 2026 tarihli Hamle özet mailinin “Günün hamlesi” bölümünde<br>Kaynak: "
                      "https://example.org/mini", fake.posts()[0][1]["description"])

    def test_a_list_clients_could_see_is_never_written(self):
        fake = FakeBasecamp(client_visible={ALTIN})
        r = self.run_ideas(fake)
        self.assertEqual((r["created"], r["lists_unusable"]), (1, ["idea_list_unusable"]), r)
        self.assertEqual([p for p, b in fake.posts()], [f"/{ACC}/buckets/{WAT}/todolists/8231779952/todos.json"])

    def test_uncertain_todo_write_is_never_retried(self):
        one = stats(idea("WAT Mobilite", WAT, "Mini vlog akımı", "Özge"))
        fake = FakeBasecamp()
        fake.fail_todo = True
        r = self.run_ideas(fake, one)
        self.assertEqual((r["unknown"], r["business_writes"]), (1, 1), r)
        fake.fail_todo = False
        self.assertEqual(self.run_ideas(fake, one)["status"], "no_new_work")
        self.assertEqual(len(fake.posts()), 1)

    def test_global_stop_blocks_every_write(self):
        self.ops.stop("ahmet-dur")
        fake = FakeBasecamp()
        r = self.run_ideas(fake)
        self.assertEqual(r["status"], "held")
        self.assertEqual(fake.calls, [])

    def test_old_records_are_pruned(self):
        self.ops.backend.data[it.STATE_KEY] = {"kind": "idea_tracking", "schema_version": 1, "lists": {}, "ideas": {
            "eski:1": {"brand": "Rabam", "hook": "Eski", "state": "ready", "suggested_on": "2026-08-01"},
            "yeni:2": {"brand": "Rabam", "hook": "Yeni", "state": "ready", "suggested_on": "2026-10-01"}}}
        self.run_ideas(FakeBasecamp(), stats(idea("Fenerium", FENER, "Derbi", "Ahmet")))
        self.assertEqual(list(self.state()["ideas"]), ["yeni:2"])


class MorningReading(Base):
    """Tuesday digest with tracked idea todos in Altınmarka's Sosyal Medya list in every state."""
    START = TUE_0916
    LIST = 8238555593

    def setUp(self):
        super().setUp()
        projects, _ = self.bc.pages[f"{BASE}/projects.json"]
        projects.append({"id": ALTIN, "name": "Altınmarka", "status": "active",
                         "dock": [{"name": "todoset", "id": 51, "enabled": True}]})
        self.bc.pages[f"{BASE}/buckets/{ALTIN}/todosets/51/todolists.json"] = ([{"id": self.LIST}], {})
        hooks = {7001: ("Dünya Gıda Günü", "2026-10-12"), 7002: ("Cadılar Bayramı", "2026-10-05"),
                 7003: ("Kahve molası", "2026-10-05"), 7004: ("Kış menüsü", "2026-10-05"),
                 7005: ("Sonbahar", "2026-10-05")}
        self.bc.pages[f"{BASE}/buckets/{ALTIN}/todolists/{self.LIST}/todos.json"] = (
            [todo(t, f"Hamle Fikir: Altınmarka × {h}", c, [(ESMA, "Esma Zeki")], created_at=f"{c}T09:30:00Z")
             for t, (h, c) in hooks.items()], {})
        said = {7002: ("2026-10-12T15:00:00Z", "<p>Çekiyoruz, 16 Ekim.</p>"),
                7003: ("2026-10-12T16:00:00Z", "Bu marka için uygun değil."),
                7005: ("2026-10-07T10:00:00Z", "Müşteriye sunduk.")}
        for tid in hooks:
            c = said.get(tid)
            self.bc.pages[f"{BASE}/buckets/{ALTIN}/recordings/{tid}/comments.json"] = (
                [{"created_at": c[0], "creator": {"name": "Esma Zeki"}, "content": c[1]}] if c else [], {})
        ideas = {}
        for tid, (h, c) in list(hooks.items()) + [(7006, ("Eski fikir", "2026-10-05"))]:
            ideas[it.idea_key("Altınmarka", h)] = {
                "brand": "Altınmarka", "hook": h, "project_id": ALTIN, "list_id": self.LIST, "person_id": ESMA,
                "who": "Esma", "state": "ready", "todo_id": tid, "suggested_on": c,
                "app_url": f"{APP}/buckets/{ALTIN}/todos/{tid}"}
        ideas[it.idea_key("Altınmarka", "Sonbahar")]["seen_comment"] = nb._comment_id(
            {"at": "2026-10-07", "by": "Esma Zeki", "text": "Müşteriye sunduk."})
        self.ops.backend.data[it.STATE_KEY] = {"kind": "idea_tracking", "schema_version": 1,
                                               "lists": {str(self.LIST): "ready"}, "ideas": ideas}
        self.ops.backend.data["daily-digest-20261012"] = {"state": "sent", "stats": {"ozu": {"fikirler": [
            idea("Altınmarka", ALTIN, "Dünya Gıda Günü", "Esma"),
            idea("WAT Mobilite", WAT, "Mini vlog akımı", "Özge")]}}}

    def test_follow_up_rules_each_fire_once_and_answers_are_recorded(self):
        seen = []
        r = self.run_digest(config(idea_tracking_enabled=True),
                            spark_source=lambda day, avoid: seen.append(list(avoid)) or spark(day))
        self.assertEqual(r["status"], "digest_sent", r)
        text = self.gmail.text()
        section = text[text.index("DÜNKÜ ÖNERİLERİM NE OLDU?"):text.index("BUGÜN")]
        u = f"{APP}/buckets/{ALTIN}/todos"
        for needle in ("- @Esma, dün açtığım fikir görevlerinde (Altınmarka × Dünya Gıda Günü) henüz yorum yok; ne "
                       "düşünüyorsunuz?",
                       f"- 👍 “Hamle Fikir: Altınmarka × Cadılar Bayramı” ({u}/7002): “Çekiyoruz, 16 Ekim.” (Esma Zeki). "
                       "Teşekkürler, takipteyim.",
                       f"- 👌 “Hamle Fikir: Altınmarka × Kahve molası” ({u}/7003): “Bu marka için uygun değil.” (Esma Zeki). "
                       "Olumsuz olarak not aldım, bu fikri tekrar önermeyeceğim.",
                       f"- @Esma, “Hamle Fikir: Altınmarka × Kış menüsü” ({u}/7004) 8 gündür sessiz; yapalım mı, yoksa pas "
                       "mı geçelim?",
                       f"- @Esma, “Hamle Fikir: Altınmarka × Sonbahar” ({u}/7005) son yorumdan bu yana 6 gündür sessiz; "
                       "biraz hızlandıralım mı?",
                       f"- ✅ “Hamle Fikir: Altınmarka × Eski fikir” ({u}/7006) artık açık değil, tamamlanmış olmalı. "
                       "Teşekkürler @Esma!",
                       "- @Özge, dün önerdiğim WAT Mobilite × Mini vlog akımı (https://example.org/mini) fikri için bir "
                       "gelişme var mı?"):
            self.assertIn(needle, section)
        self.assertNotIn("Dünya Gıda Günü (https://example.org/dünya) fikri için", section)  # tracked: own line only
        rest = text[text.index("BUGÜN"):]
        self.assertNotIn("Hamle Fikir:", rest.split("GÜNÜN HAMLESİ")[0])  # overdue idea todos stay out of team lists
        self.assertIn(dd.IDEA_HINT, text)
        self.assertIn(dd.IDEA_HINT, self.gmail.html())
        eff = r["stats"]["ideas"]
        k = lambda h: it.idea_key("Altınmarka", h)
        self.assertEqual((sorted(eff["yes"]), sorted(eff["no"]), eff["done"], sorted(eff["reminded"])),
                         ([k("Cadılar Bayramı")], [k("Kahve molası")], [k("Eski fikir")], sorted([k("Kış menüsü"), k("Sonbahar")])))
        fake = FakeBasecamp()
        out = it.run_idea_tracking(self.ops, http=it.IdeaBasecamp(ACC, ROUTES, send=fake.send), routes=ROUTES,
                                   stats=r["stats"], day=TUE)
        self.assertEqual((out["created"], [b["content"] for p, b in fake.posts()]),
                         (1, ["Hamle Fikir: WAT Mobilite × Mini vlog akımı"]), out)  # Dünya Gıda Günü already tracked
        ideas = self.ops.backend.data[it.STATE_KEY]["ideas"]
        self.assertEqual((ideas[k("Kahve molası")]["answer"], ideas[k("Kahve molası")]["no_on"]), ("olumsuz", "2026-10-13"))
        self.assertEqual(ideas[k("Cadılar Bayramı")]["answer"], "olumlu")
        self.assertEqual(ideas[k("Eski fikir")]["done_on"], "2026-10-13")
        self.assertEqual(ideas[k("Kahve molası")]["history"][0]["text"], "Bu marka için uygun değil.")
        self.assertEqual(it.avoid_ideas(self.ops.backend.data[it.STATE_KEY], TUE), ["Altınmarka × Kahve molası"])
        items = self.reader().collect()["items"]
        rows, _ = it.idea_follow_ups(self.ops.backend.data[it.STATE_KEY], reader=self.reader(), items=items,
                                     day=date(2026, 10, 14), prev_day=TUE)
        self.assertEqual([x for x in rows if "Altınmarka" in x[3] or "Altınmarka" in x[1]], [])  # nothing repeats
        _, stats2 = dd.build_digest(self.reader(), day=date(2026, 10, 14), day_note=None, cards=self.cards,
                                    calendar=self.calendar, spark_source=lambda day, avoid: seen.append(list(avoid)) or spark(day),
                                    idea_state=self.ops.backend.data[it.STATE_KEY])
        self.assertIn("Altınmarka × Kahve molası", seen[-1])  # the team's no reaches the morning research

    def test_without_the_flag_no_hint(self):
        r = self.run_digest(spark_source=lambda day, avoid: spark(day))
        self.assertEqual(r["status"], "digest_sent", r)
        self.assertNotIn(dd.IDEA_HINT, self.gmail.text())


class OperatorOpen(Base):
    START = TUE_0916

    def test_nb_open_also_opens_todays_ideas_once_without_reapplying_effects(self):
        self.ops.backend.data["daily-digest-20261012"] = {"state": "sent", "stats": {"new_business_brands": []}}
        self.ops.backend.data["daily-digest-20261013"] = {"state": "sent", "stats": dict(stats(), ideas={"done": ["x"]})}
        self.ops.backend.data[nb.STATE_KEY] = nb.empty_state()
        fake = FakeBasecamp()
        with patch.object(nb, "session_send", lambda token, factory: fake.send):
            r = dd.run_worker_nb_open(self.ops, None, clock=lambda: self.now)
            self.assertEqual((r["status"], r["ideas"]["created"], r["ideas"]["effects"]), ("no_new_work", 2, 0), r)
            r = dd.run_worker_nb_open(self.ops, None, clock=lambda: self.now)
            self.assertEqual(r["ideas"]["status"], "no_new_work")
        self.assertEqual(len(fake.posts()), 2)
