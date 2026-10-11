"""Brand -> Basecamp project routing: detection, route validation, chain ticks, guards. Fakes only."""
import copy
import hashlib
import json
import re
import unittest
from unittest import mock
from datetime import datetime, timezone

import v11_brand_routing as routing
import v11_chain as chain
import v11_chain_live as live
from autonomy_policy import HAMLE
from test_autonomy_policy import NOW
from test_cloud_providers import FAKE_TOKEN, Response, Session
from test_v11_chain import ACC, AHMET, CANSU, LIST, OZGE, PROJ, Harness, World, disk_catalog, live_catalog, \
    requirements
from v11_brand_task import HAMLE_PERSON, BrandTaskWriter
from v11_work_queue import QueueError

ORIGIN = "https://3.basecampapi.com"
ESMA = 53167312
WAT, WAT_SM, WAT_PROJ, WAT_MOTION = 40694338, 8231779952, 8234557425, 9381497228
RABAM, RABAM_SM, RABAM_PROJ = 48639990, 10239606739, 10239607231
HAN, HAN_SM = 48562916, 10385835270
CACAONLY = 47875291


def lists_of(catalog):
    out = {LIST: PROJ}
    for r in catalog["brand_routes"].values():
        for v in (r.get("lists") or {}).values():
            if v:
                out[v] = r["project_id"]
    return out


class MultiWorld(World):
    """World over many projects: every read-back list is client-hidden unless listed in visible_lists."""

    def __init__(self, catalog):
        super().__init__()
        self.lists = lists_of(catalog)
        self.visible_lists = set()
        self.todo_project = {}

    def add_comment(self, tid, author, html_text):
        cid = super().add_comment(tid, author, html_text)
        self.comments[tid][-1]["bucket"] = {"id": self.todo_project[tid]}
        return cid

    def session(self):
        world = self

        class S(Session):
            def request(self, method, url, **kwargs):
                world.calls.append((method, url))
                body = json.loads(kwargs["data"]) if kwargs.get("data") else None
                m = re.fullmatch(ORIGIN + r"/(\d+)/buckets/(\d+)/(.+)", url)
                assert m and int(m.group(1)) == ACC, url
                proj, rest = int(m.group(2)), m.group(3)
                lm = re.fullmatch(r"todolists/(\d+)(/todos)?\.json", rest)
                if lm and world.lists.get(int(lm.group(1))) == proj:
                    lid = int(lm.group(1))
                    if method == "GET" and not lm.group(2):
                        return Response({"id": lid, "type": "Todolist", "bucket": {"id": proj}, "status": "active",
                                         "visible_to_clients": lid in world.visible_lists})
                    if method == "POST":
                        tid = world._id()
                        world.todo_project[tid] = proj
                        world.todos[tid] = {
                            "id": tid, "type": "Todo", "status": "active",
                            "visible_to_clients": lid in world.visible_lists, "bucket": {"id": proj},
                            "parent": {"id": lid, "type": "Todolist"}, "creator": {"id": HAMLE_PERSON},
                            "completed": False, "due_on": None, "content": body["content"],
                            "description": body["description"],
                            "assignees": [{"id": i} for i in body["assignee_ids"]],
                            "completion_subscribers": [{"id": i} for i in body["completion_subscriber_ids"]],
                            "comments_url": f"{ORIGIN}/{ACC}/buckets/{proj}/recordings/{tid}/comments.json"}
                        world.comments[tid] = []
                        return Response({"id": tid, "bucket": {"id": proj}}, 201)
                tm = re.fullmatch(r"todos/(\d+)\.json", rest)
                if method == "GET" and tm and world.todo_project.get(int(tm.group(1))) == proj:
                    return Response(world.todo(int(tm.group(1))))
                rm = re.fullmatch(r"recordings/(\d+)/comments\.json", rest)
                if method == "POST" and rm and world.todo_project.get(int(rm.group(1))) == proj:
                    return Response({"id": world.add_comment(int(rm.group(1)), HAMLE_PERSON, body["content"])}, 201)
                cm = re.fullmatch(r"comments/(\d+)\.json", rest)
                if method == "GET" and cm:
                    return Response(next(c for rows in world.comments.values() for c in rows
                                         if c["id"] == int(cm.group(1))))
                raise AssertionError("unexpected fake request " + method + " " + url)
        return S()

    def get(self, url, headers):
        self.calls.append(("READ", url))
        tm = re.search(r"/todos/(\d+)\.json$", url)
        if tm:
            return 200, self.todo(int(tm.group(1))), {}
        tid = int(url.split("/recordings/")[1].split("/")[0])
        return 200, copy.deepcopy(self.comments[tid]), {}

    def todo_posts(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/todos.json")]


class BrandHarness(Harness):
    def __init__(self, catalog=None):
        super().__init__(catalog or live_catalog())
        self.world = MultiWorld(self.catalog)
        self.deps.task_writer_factory = lambda cat, st: BrandTaskWriter(cat, st, token_provider=lambda: FAKE_TOKEN,
                                                                       session_factory=self.world.session)
        self.deps.basecamp_get = self.world.get
        self.deps.comment_writer = chain.BasecampCommentWriter(self.catalog, token_provider=lambda: FAKE_TOKEN,
                                                               session_factory=self.world.session)

    def brand_mail(self, thread, subject, text, req=None, sender="ahmet@gamechangers.com.tr"):
        self.order += 1
        mid = f"synthetic-b{self.order}"
        self.requirements[text] = req or requirements(subject.strip()[:60] or "İş")
        self.inbox = [m for m in self.inbox if m["thread_id"] != thread] + [{
            "channel": "email", "verified": True, "headers_origin": "provider", "latest_in_thread": True,
            "evidence_ref": "SYNTHETIC-gmail:" + mid, "message_id": mid, "thread_id": thread,
            "fingerprint": hashlib.sha256(mid.encode()).hexdigest(), "observed_at": NOW.isoformat(),
            "automatic": False, "own_message": False, "human_takeover": False, "sender": sender,
            "to": [HAMLE], "cc": [], "origin": {"kind": "direct_email"}, "subject": subject, "text": text,
            "source_order": self.order}]
        return mid

    def task(self, thread):
        rec, work = self.record(thread)
        return rec, work, self.world.todos.get(rec["task_id"])


def motion_req(title):
    req = requirements(title, kinds=("animation",))
    req["deliverables"][0].update(id="tanitim", specification="10 sn ürün tanıtım animasyonu, 16:9",
                                  duration_seconds=10, formats=["video/mp4"])
    return req


# ------------------------------------------------------------------ detection
class Detection(unittest.TestCase):
    def setUp(self):
        self.cat = disk_catalog()

    def d(self, subject, text=""):
        return routing.detect_brand(self.cat, subject, text)

    def test_single_match_subject_or_body(self):
        self.assertEqual(self.d("WAT Mobilite kampanya görseli")["brand_key"], "wat_mobilite")
        r = self.d("Yeni iş", "Merhaba, Rabam için 2 post lazım.")
        self.assertEqual((r["status"], r["brand_key"], r["brand_name"]), ("single", "rabam", "Rabam"))
        self.assertEqual(self.d("ALTINMARKA story")["brand_key"], "altinmarka")
        self.assertEqual(self.d("Altınmarka İçin Post")["brand_key"], "altinmarka")
        self.assertEqual(self.d("bikaldı reels")["brand_key"], "bikaldi")

    def test_none_and_ambiguous(self):
        self.assertEqual(self.d("İç duyuru görseli", "Ofis için afiş.")["status"], "none")
        r = self.d("WAT ve Rabam ortak post")
        self.assertEqual((r["status"], r["brand_key"], sorted(r["matches"])), ("ambiguous", None, ["rabam", "wat_mobilite"]))

    def test_no_substring_or_signature_or_quote_matches(self):
        self.assertEqual(self.d("Watch party görseli")["status"], "none")  # 'wat' only as a whole word
        self.assertEqual(self.d("Afiş", "Bir post ya da story olabilir.")["status"], "none")  # 'ya da' is not an alias
        body = "Afiş lazım.\n--\nAhmet\nahmet@gamechangers.com.tr\nhttps://gamechangers.com.tr"
        self.assertEqual(self.d("Afiş", body)["status"], "none")
        self.assertEqual(self.d("Afiş", "Gamechangers ekibi olarak afiş lazım.")["status"], "none")  # subject-only brand
        self.assertEqual(self.d("Gamechangers LinkedIn postu")["brand_key"], "gamechangers")
        quoted = "Afiş lazım.\n\nOn Mon, Oct 12, 2026 Cansu wrote:\n> Rabam için post"
        self.assertEqual(self.d("Afiş", quoted)["status"], "none")
        self.assertEqual(self.d("Afiş", "Afiş lazım.\n> WAT eski mail")["status"], "none")

    def test_cacaonly_is_excluded(self):
        r = self.d("Cacaonly post")
        self.assertEqual((r["status"], r["brand_key"], r["excluded"]), ("single", "cacaonly", True))
        self.assertNotIn("cacaonly", self.cat["brand_routes"])
        self.assertNotIn(CACAONLY, routing.allowlist(self.cat))
        self.assertFalse(any(r["project_id"] == CACAONLY for r in self.cat["brand_routes"].values()))


# ------------------------------------------------------------------ catalog + validation
class CatalogRoutes(unittest.TestCase):
    def test_disk_brand_routes_match_mac_readback(self):
        cat = disk_catalog()
        self.assertTrue(cat["brand_routing"]["enabled"])
        r = cat["brand_routes"]
        expect = {"wat_mobilite": (40694338, OZGE, CANSU), "rabam": (48639990, OZGE, AHMET),
                  "bwi": (43886681, OZGE, AHMET), "han_spaces": (48562916, ESMA, AHMET),
                  "ya_da": (48917059, ESMA, CANSU), "altinmarka": (40704523, ESMA, CANSU),
                  "bikaldi": (40704530, ESMA, CANSU), "gamechangers": (40704459, AHMET, ESMA),
                  "fenerium": (44320349, AHMET, AHMET), "new_biz": (40704547, CANSU, CANSU)}
        self.assertEqual({k: (v["project_id"], v["sm_person_id"], v["rep_person_id"]) for k, v in r.items()}, expect)
        usable = sorted(k for k in r if routing.usable(cat, k))
        self.assertEqual(usable, ["altinmarka", "bikaldi", "bwi", "gamechangers", "han_spaces", "rabam",
                                  "wat_mobilite", "ya_da"])
        self.assertEqual(routing.route_problem(cat, "fenerium"), "brand_route_unverified")
        self.assertEqual(routing.route_problem(cat, "new_biz"), "brand_route_unverified")
        self.assertEqual((r["han_spaces"]["clients_enabled"], r["ya_da"]["clients_enabled"]), (True, True))
        self.assertEqual(r["altinmarka"]["list_id"], 10388829175)
        self.assertEqual(r["rabam"]["lists"]["motion"], None)
        allowed = routing.allowlist(cat)
        self.assertEqual(allowed[49184053], {10393654435})
        self.assertEqual(allowed[WAT], {WAT_SM, WAT_PROJ, WAT_MOTION})
        self.assertNotIn(44320349, allowed)

    def test_route_problems_fail_closed(self):
        base = disk_catalog()
        cases = [("verified", False, "brand_route_unverified"),
                 ("clients_enabled", None, "brand_clients_setting_unknown"),
                 ("lists_visible_to_clients", None, "brand_list_client_visibility_unconfirmed"),
                 ("hamle_member", False, "hamle_not_member_of_brand_project"),
                 ("sm_person_id", None, "brand_sm_unknown"),
                 ("project_id", CACAONLY, "brand_project_not_allowed"),
                 ("project_id", 49184053, "brand_project_not_allowed")]
        for field, value, code in cases:
            cat = copy.deepcopy(base)
            cat["brand_routes"]["han_spaces"][field] = value
            self.assertEqual(routing.route_problem(cat, "han_spaces"), code, field)
            self.assertNotIn(HAN, routing.allowlist(cat))
        cat = copy.deepcopy(base)
        cat["brand_routes"]["han_spaces"]["lists"]["social"] = None
        self.assertEqual(routing.route_problem(cat, "han_spaces"), "brand_list_unconfirmed")
        cat = copy.deepcopy(base)
        cat["brand_routing"]["enabled"] = False
        self.assertEqual(list(routing.allowlist(cat)), [49184053])
        self.assertFalse(routing.usable(cat, "wat_mobilite"))

    def test_list_choice_is_deterministic(self):
        route = disk_catalog()["brand_routes"]["wat_mobilite"]
        post = requirements("x")["deliverables"]
        self.assertEqual(routing.select_list(route, post), WAT_SM)
        self.assertEqual(routing.select_list(route, motion_req("x")["deliverables"]), WAT_MOTION)
        reels = [{"id": "reels", "kind": "video", "specification": "15 sn"}]
        self.assertEqual(routing.select_list(route, reels), WAT_SM)
        afis = [{"id": "afis", "kind": "graphic", "specification": "A3 basılı afiş"}]
        self.assertEqual(routing.select_list(route, afis), WAT_PROJ)
        self.assertEqual(routing.select_list(route, post + afis), WAT_PROJ)  # mixed -> Projeler
        rabam = disk_catalog()["brand_routes"]["rabam"]
        self.assertEqual(routing.select_list(rabam, motion_req("x")["deliverables"]), RABAM_PROJ)

    def test_brand_catalog_roles(self):
        cat = routing.brand_catalog(live_catalog(), "han_spaces", HAN_SM)
        self.assertEqual((cat["basecamp"]["project_id"], cat["routes"][0]["list_id"]), (HAN, HAN_SM))
        self.assertEqual(routing.managers(cat), [AHMET, ESMA])
        self.assertTrue(all(ids == [ESMA] for ids in cat["routes"][0]["assignee_ids_by_kind"].values()))
        self.assertNotIn("brand_routes", cat)
        with self.assertRaises(routing.RoutingError):
            routing.brand_catalog(live_catalog(), "fenerium", 1)
        with self.assertRaises(routing.RoutingError):
            routing.brand_catalog(live_catalog(), "wat_mobilite", HAN_SM)  # another brand's list


class Requesters(unittest.TestCase):
    def test_team_is_verified_agency_requesters(self):
        contacts = {c["email"]: c for c in disk_catalog()["contacts"]}
        for name in ("ahmet", "cansu", "ozge", "baris", "berkay", "ipek", "esma"):
            c = contacts[name + "@gamechangers.com.tr"]
            self.assertEqual((c["side"], c["verified"], c["can_request"], c["can_receive"], c["can_relay"]),
                             ("agency", True, True, True, False), name)
            self.assertIn("gc_internal", c["service_ids"])
        self.assertFalse(any("samet" in e for e in contacts))

    def test_new_requester_opens_brand_task_and_unknown_sender_is_ignored(self):
        h = BrandHarness()
        h.brand_mail("t-esma", "HAN Spaces post", "HAN Spaces için post lazım.", sender="esma@gamechangers.com.tr")
        h.brand_mail("t-x", "WAT post", "WAT post.", sender="samet@gamechangers.com.tr")
        report = h.tick()
        self.assertEqual([m["reason"] for m in report["ignored_mail"]], ["sender_unverified"])
        rec, work, todo = h.task("t-esma")
        self.assertEqual((work["project_id"], work["list_id"], todo["visible_to_clients"]), (HAN, HAN_SM, False))


# ------------------------------------------------------------------ chain ticks
class ChainRouting(unittest.TestCase):
    def test_single_brand_task_in_brand_project_with_sm_rep_ahmet(self):
        h = BrandHarness()
        h.brand_mail("t-wat", "WAT Mobilite ekim kampanyası", "Merhaba Hamle, WAT için post lazım.")
        report = h.tick()
        self.assertEqual(report["provider_writes"], 2, report)  # task + internal context note
        rec, work, todo = h.task("t-wat")
        self.assertEqual((work["project_id"], work["list_id"], rec["status"]), (WAT, WAT_SM, "in_production"))
        self.assertEqual(todo["bucket"]["id"], WAT)
        self.assertEqual([a["id"] for a in todo["assignees"]], [OZGE])
        self.assertEqual(sorted(s["id"] for s in todo["completion_subscribers"]), [AHMET, CANSU, OZGE])
        self.assertFalse(todo["content"].startswith("["))
        self.assertEqual([p[1] for p in h.world.todo_posts()],
                         [f"{ORIGIN}/{ACC}/buckets/{WAT}/todolists/{WAT_SM}/todos.json"])
        note = h.world.comments[rec["task_id"]][0]
        self.assertEqual((note["bucket"]["id"], note["visible_to_clients"]), (WAT, False))

    def test_motion_request_moves_to_motion_list_before_task(self):
        h = BrandHarness()
        h.brand_mail("t-m", "WAT tanıtım animasyonu", "WAT için 10 sn animasyon.", req=motion_req("WAT animasyon"))
        h.tick()
        rec, work, todo = h.task("t-m")
        self.assertEqual((work["list_id"], todo["parent"]["id"]), (WAT_MOTION, WAT_MOTION))

    def test_no_brand_goes_to_internal_route(self):
        h = BrandHarness()
        h.brand_mail("t-i", "İç duyuru görseli", "Ofis için duyuru görseli.")
        h.tick()
        rec, work, todo = h.task("t-i")
        self.assertEqual((work["project_id"], work["list_id"], todo["content"]), (PROJ, LIST, "İç duyuru görseli"))
        self.assertEqual([a["id"] for a in todo["assignees"]], [AHMET])

    def test_ambiguous_goes_internal_with_note(self):
        h = BrandHarness()
        h.brand_mail("t-a", "WAT ve Rabam ortak post", "İki marka için.")
        h.tick()
        rec, work, todo = h.task("t-a")
        self.assertEqual((work["project_id"], work["list_id"]), (PROJ, LIST))
        self.assertIn("birden fazla marka", h.world.comments[rec["task_id"]][0]["content"])

    def test_unverified_brand_falls_back_with_brand_in_title_and_owner_tag(self):
        h = BrandHarness()
        h.brand_mail("t-f", "Fenerium maç günü postu", "Fenerium için post lazım.")
        h.tick()
        rec, work, todo = h.task("t-f")
        self.assertEqual((work["project_id"], work["list_id"]), (PROJ, LIST))
        self.assertTrue(todo["content"].startswith("[Fenerium] "), todo["content"])
        note = h.world.comments[rec["task_id"]][0]["content"]
        self.assertIn("Fenerium", note)
        self.assertIn("doğrulanmadı", note)
        self.assertNotIn(44320349, [int(c[1].split("/buckets/")[1].split("/")[0]) for c in h.world.calls
                                    if "/buckets/" in c[1]])

    def test_cacaonly_never_targets_its_project(self):
        h = BrandHarness()
        h.brand_mail("t-c", "Cacaonly post", "Cacaonly için post.")
        h.tick()
        rec, work, todo = h.task("t-c")
        self.assertEqual(work["project_id"], PROJ)
        self.assertTrue(todo["content"].startswith("[Cacaonly] "))
        self.assertFalse(any(f"/buckets/{CACAONLY}/" in c[1] for c in h.world.calls))

    def test_unverified_client_enabled_brand_falls_back(self):
        cat = live_catalog()
        cat["brand_routes"]["han_spaces"]["lists_visible_to_clients"] = None
        h = BrandHarness(cat)
        h.brand_mail("t-h", "HAN Spaces story", "HAN Spaces için story.")
        h.tick()
        rec, work, todo = h.task("t-h")
        self.assertEqual(work["project_id"], PROJ)
        self.assertTrue(todo["content"].startswith("[HAN Spaces] "))

    def test_client_enabled_brand_never_posts_into_client_visible_list(self):
        h = BrandHarness()
        h.world.visible_lists.add(HAN_SM)  # list changed after the Mac readback
        h.brand_mail("t-h", "HAN Spaces story", "HAN Spaces için story.")
        h.tick()
        rec, work = h.record("t-h")
        self.assertEqual((rec["status"], rec["task_id"], work["project_id"]), ("held", None, HAN))
        self.assertEqual(h.world.todo_posts(), [])
        self.assertEqual(rec["reason"], "basecamp_target_changed")

    def test_client_enabled_brand_task_is_hidden(self):
        h = BrandHarness()
        h.brand_mail("t-y", "Multicosmetics post", "Multicosmetics için post.")
        h.tick()
        rec, work, todo = h.task("t-y")
        self.assertEqual((work["project_id"], todo["visible_to_clients"]), (48917059, False))
        self.assertEqual([a["id"] for a in todo["assignees"]], [ESMA])
        self.assertEqual(sorted(s["id"] for s in todo["completion_subscribers"]), sorted([AHMET, CANSU, ESMA]))

    def test_routing_disabled_pins_everything_internal(self):
        cat = live_catalog()
        cat["brand_routing"]["enabled"] = False
        h = BrandHarness(cat)
        h.brand_mail("t-d", "WAT post", "WAT için post.")
        h.tick()
        rec, work, todo = h.task("t-d")
        self.assertEqual((work["project_id"], todo["content"]), (PROJ, "WAT post"))

    def test_route_revoked_after_binding_holds_work(self):
        h = BrandHarness()
        h.brand_mail("t-r", "WAT post", "WAT için post.", req=requirements("WAT post", complete=False))
        h.tick()  # clarification only, work bound to WAT
        rec, work = h.record("t-r")
        self.assertEqual((rec["status"], work["project_id"]), ("awaiting_clarification", WAT))
        h.catalog["brand_routes"]["wat_mobilite"]["verified"] = False
        h2 = BrandHarness(h.catalog)
        h2.queue, h2.world = h.queue, h.world
        h2.deps.queue = h.queue
        h2.brand_mail("t-r", "Re: WAT post", "Tarih 14 Ekim.")
        report = h2.tick()
        self.assertEqual([m["reason"] for m in report["ignored_mail"]], ["route_target_not_allowed"])
        self.assertEqual(h.world.todo_posts(), [])


# ------------------------------------------------------------------ guards
class Guards(unittest.TestCase):
    def comment(self, project, list_id):
        return {"target": {"account_id": ACC, "project_id": project, "list_id": list_id, "task_id": 5},
                "body": "x", "mention_ids": [], "internal_only": True}

    def test_comment_writer_allow_list(self):
        w = chain.BasecampCommentWriter(live_catalog(), token_provider=lambda: FAKE_TOKEN)
        self.assertTrue(w._check(self.comment(WAT, WAT_SM)).endswith(f"/buckets/{WAT}"))
        self.assertTrue(w._check(self.comment(PROJ, LIST)).endswith(f"/buckets/{PROJ}"))
        for project, list_id in ((CACAONLY, 1), (44320349, 1), (WAT, HAN_SM), (WAT, 1)):
            with self.assertRaises(chain.ChainError):
                w._check(self.comment(project, list_id))
        off = live_catalog()
        off["brand_routing"]["enabled"] = False
        with self.assertRaises(chain.ChainError):
            chain.BasecampCommentWriter(off, token_provider=lambda: FAKE_TOKEN)._check(self.comment(WAT, WAT_SM))

    def test_authorizer_task_and_comment_targets(self):
        class Ops:
            def control(self): return {"mode": "review_open", "generation": 1}
            def budget_status(self): return {"overrun_hold": False}

        class Q:
            def control(self): return {"mode": "queue_open", "generation": 1}
        clock = lambda: datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)
        stamp = mock.patch.object(live, "_stamp", lambda c: c())  # authorization window is not under test
        stamp.start()
        self.addCleanup(stamp.stop)
        auth = live.ChainAuthorizer(ops=Ops(), ops_generation=1, queue=Q(), queue_generation=1,
                                    catalog=live_catalog(), mail_writer=None, clock=clock)
        task = lambda p, l, notify=False: {"payload": {"task": {"account_id": ACC, "project_id": p,
                                                                "todolist_id": l, "notify": notify}}}
        self.assertTrue(auth("create_task", task(WAT, WAT_SM)))
        self.assertTrue(auth("create_task", task(PROJ, LIST)))
        self.assertFalse(auth("create_task", task(CACAONLY, 1)))
        self.assertFalse(auth("create_task", task(WAT, RABAM_SM)))
        self.assertFalse(auth("create_task", task(WAT, WAT_SM, notify=True)))
        self.assertTrue(auth("add_comment", self.comment(RABAM, RABAM_PROJ)))
        self.assertFalse(auth("add_comment", self.comment(44320349, 1)))
        self.assertEqual(auth.decisions[2], ("create_task", "task_target_out_of_scope"))

    def test_queue_retarget_only_before_task_and_within_allow_list(self):
        h = BrandHarness()
        h.brand_mail("t-q", "WAT post", "WAT için post.")
        h.tick()
        rec, work = h.record("t-q")
        h.queue.enqueue_message(work["work_id"], message_id="m-extra", fingerprint="e" * 64, source_order=99,
                                expected_generation=h.queue.control()["generation"])
        claim = h.queue.claim_next(work["work_id"], worker_id="t", expected_generation=h.queue.control()["generation"])
        with self.assertRaises(QueueError) as ctx:
            h.queue.retarget_list(claim, list_id=WAT_MOTION, allowed_list_ids=[WAT_MOTION])
        self.assertEqual(ctx.exception.code, "work_list_frozen")
        with self.assertRaises(QueueError):
            h.queue.retarget_list(claim, list_id=WAT_MOTION, allowed_list_ids=[WAT_SM])


class Packaging(unittest.TestCase):
    def test_routing_module_ships_with_chain(self):
        import package_chain_source as pkg
        self.assertIn("v11_brand_routing.py", pkg.CHAIN_FILES)


class MacReadbackHelper(unittest.TestCase):
    def test_readback_is_get_only_and_proposes_verified_patch(self):
        import brand_routes_readback as rb
        cat = disk_catalog()
        calls = []

        def call(url, body=None):
            calls.append((url, body))
            if url.endswith("/projects/40694338.json"):
                return {"name": "WAT", "clients_enabled": False, "dock": [{"name": "todoset", "id": 8231360562}]}
            if url.endswith("/people.json"):
                return [{"id": i} for i in (53358100, OZGE, CANSU, AHMET)]
            return [{"id": i, "name": "L", "status": "active", "visible_to_clients": False}
                    for i in (WAT_SM, WAT_PROJ, WAT_MOTION)]
        facts = rb.readback(cat, "wat_mobilite", call)
        self.assertTrue(facts["proposed_patch"]["verified"])
        self.assertTrue(all(body is None for _, body in calls))
        self.assertEqual(len(calls), 3)

    def test_create_requires_explicit_flags_and_token(self):
        import brand_routes_readback as rb
        with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(SystemExit):
            rb.main(["--create-hamle-list", "--brand", "rabam", "--confirm-create"])
        with mock.patch.dict("os.environ", {"BASECAMP_ACCESS_TOKEN": "x"}), \
                mock.patch.object(rb, "http_get_factory", side_effect=AssertionError("no network")), \
                self.assertRaises(SystemExit):
            rb.main(["--create-hamle-list", "--brand", "rabam"])


if __name__ == "__main__":
    unittest.main()
