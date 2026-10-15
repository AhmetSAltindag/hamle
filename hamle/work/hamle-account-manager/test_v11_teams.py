"""Microsoft Teams v1 (2026-10-14): fakes only, no sockets, Microsoft, Basecamp or models."""
import base64
import copy
import io
import json
import time
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import model_routing
import v11_client_job as cj
import v11_client_watch as cw
import v11_teams as teams
from autonomy_policy import scope_digest
from test_anthropic_routing import FakeClient, KEY, message
from test_v11_client_watch import Base
from v11_client_reply import validate_reply

APP = "3f2c1b0a-1111-4222-8333-444455556666"
TENANT = "9a8b7c6d-aaaa-4bbb-8ccc-ddddeeeeffff"
SERVICE = "https://smba.trafficmanager.net/emea/"
WAT_CHANNEL = "19:wat-sosyal@thread.tacv2"
AGENCY_CHAT = "19:ajans-genel@thread.v2"
WAT, WAT_SOCIAL, RABAM = 40694338, 8231779952, 48639990
TODO_URL = "https://app.basecamp.com/5898865/buckets/40694338/todos/10402860490"
KEY_A = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY_B = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def jwk(key, kid, endorsements=("msteams",)):
    n = key.public_key().public_numbers()
    return {"kty": "RSA", "use": "sig", "kid": kid, "n": _b64(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
            "e": _b64(n.e.to_bytes(3, "big")), "endorsements": list(endorsements)}


def jwt(now, *, key=KEY_A, kid="k1", alg="RS256", **claims):
    body = {"iss": teams.ISSUER, "aud": APP, "exp": int(now) + 3600, "nbf": int(now) - 60, "serviceurl": SERVICE}
    body.update(claims)
    head = _b64(json.dumps({"alg": alg, "kid": kid, "typ": "JWT"}).encode())
    payload = _b64(json.dumps(body).encode())
    sig = key.sign(f"{head}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"Bearer {head}.{payload}.{_b64(sig)}"


class Metadata:
    """login.botframework.com, offline: OpenID metadata + keys."""
    def __init__(self):
        self.urls, self.keys = [], [jwk(KEY_A, "k1")]

    def __call__(self, url):
        self.urls.append(url)
        if url == teams.OPENID_URL:
            return {"issuer": teams.ISSUER, "jwks_uri": "https://login.botframework.com/v1/.well-known/keys"}
        assert url == "https://login.botframework.com/v1/.well-known/keys", url
        return {"keys": copy.deepcopy(self.keys)}


class Sender:
    def __init__(self):
        self.sent, self.fail = [], None

    def send(self, service_url, conversation_id, reply_to_id, text):
        self.sent.append({"service_url": service_url, "conversation_id": conversation_id,
                          "reply_to_id": reply_to_id, "text": text})
        if self.fail:
            raise self.fail
        return {"id": "r%d" % len(self.sent)}


class Todos:
    def __init__(self):
        self.calls = []

    def write(self, payload):
        self.calls.append(copy.deepcopy(payload))
        return {"remote_id": "777"}

    def readback(self, remote_id, payload):
        return {"readback_sha256": scope_digest(payload),
                "app_url": f"https://app.basecamp.com/5898865/buckets/{payload['target']['project_id']}/todos/{remote_id}"}


class Comments(Todos):
    def write(self, payload):
        self.calls.append(copy.deepcopy(payload))
        return {"remote_id": "888"}

    def readback(self, remote_id, payload):
        return {"readback_sha256": scope_digest(payload)}


def basecamp_get(url, conditional):
    todo = int(url.rsplit("/", 1)[1].split(".")[0])
    return 200, {"type": "Todo", "id": todo, "bucket": {"id": WAT}, "visible_to_clients": False, "completed": False,
                 "parent": {"id": WAT_SOCIAL, "type": "Todolist"}}, {}


def teams_block(**kw):
    block = {"enabled": True, "app_id": APP, "tenant_id": TENANT, "service_hosts": ["smba.trafficmanager.net"],
             "conversations": {WAT_CHANNEL: "wat_mobilite"}, "keep_messages": 40,
             "max_replies_per_conversation_per_day": 5, "max_model_calls_per_day": 20}
    block.update(kw)
    return block


def out(action="answer", reply="Özge, WAT'ın story'si Berkay'da, yarın sabah hazır olacak.", task=None):
    return {"action": action, "understood": "Özge işin durumunu sordu.", "reply": reply, "task": task}


def task_out(**kw):
    t = {"brand_key": None, "list": "social", "title": "Şarj istasyonu story", "brief": "Yeni istasyon için 1 story.",
         "due_on": None}
    t.update(kw)
    return out("task", "Özge, story işini Basecamp'e yazıyorum.", t)


class TeamsBase(Base):
    def setUp(self):
        super().setUp()
        self.meta, self.sender, self.todos_bc, self.comments_bc = Metadata(), Sender(), Todos(), Comments()
        self.later = []
        self.n = 0

    def settings(self, **kw):
        return teams.validate_teams(teams_block(**kw), self.catalog)

    def desk(self, run_later=None, catalog=None, **kw):
        catalog = catalog or self.catalog
        cfg = cw.load_config()
        jobs = cj.validate_jobs(cfg["jobs"], catalog, validate_reply(cfg["reply"], catalog))
        return teams.TeamsDesk(
            self.ops, teams.validate_teams(teams_block(**kw), catalog), clock=lambda: self.now,
            auth=teams.BotAuth(APP, fetch_json=self.meta, clock=lambda: self.now.timestamp()),
            model=teams.teams_source(self.ops, lambda: KEY, catalog, routing=model_routing.load(), client=self.client),
            sender=self.sender, jobs=jobs, todo_writer=self.todos_bc, comment_writer=self.comments_bc,
            basecamp_get=basecamp_get, brands=cw.brand_index(catalog, self.cards), run_later=run_later)

    def activity(self, text="Bugün WAT için yeni istasyon açıldı.", *, mention=False, conv=WAT_CHANNEL, kind="channel",
                 aid=None, files=(), tenant=TENANT, author="Özge Yağmur"):
        self.n += 1
        entities = [{"type": "mention", "text": "<at>Hamle</at>", "mentioned": {"id": "28:" + APP, "name": "Hamle"}}]
        return {"type": "message", "id": aid or "a%d" % self.n, "timestamp": "2026-10-13T07:00:00Z",
                "serviceUrl": SERVICE, "channelId": "msteams",
                "from": {"id": "29:ozge", "name": author, "aadObjectId": "aad-ozge"},
                "conversation": {"id": conv + (";messageid=1700" if kind == "channel" else ""),
                                 "conversationType": kind, "tenantId": tenant, "isGroup": kind != "personal"},
                "recipient": {"id": "28:" + APP, "name": "Hamle"},
                "text": ("<at>Hamle</at> " if mention else "") + text,
                "entities": entities if mention else [],
                "attachments": [{"contentType": "reference", "name": f, "contentUrl": "https://x.sharepoint.com/" + f}
                                for f in files],
                "channelData": {"tenant": {"id": tenant}, "team": {"name": "Gamechangers"},
                                "channel": {"name": "WAT Sosyal"}}}

    def post(self, desk, activity, auth=None):
        body = json.dumps(activity).encode("utf-8")
        return desk.handle({"Authorization": auth or jwt(self.now.timestamp())}, body)

    def stored(self):
        return {k: v for k, v in self.ops.backend.data.items() if k.startswith("teams-")}

    def conv(self, conv=WAT_CHANNEL):
        return self.ops.backend.data[teams.conversation_key(conv)]

    def mention_rows(self):
        return [v for k, v in self.ops.backend.data.items() if k.startswith("teams-mention-")]


class Config(TeamsBase):
    def test_shipped_config_is_off_with_placeholders_and_the_manifest_asks_rsc(self):
        cfg = cw.load_config()
        block = cfg["teams"]
        self.assertIs(block["enabled"], False)
        self.assertEqual((block["app_id"], block["conversations"]), (teams.PLACEHOLDER, {}))
        self.assertIsNone(teams.validate_teams(block, self.catalog))
        with self.assertRaises(teams.TeamsError):  # turned on with placeholders it refuses
            teams.validate_teams(dict(block, enabled=True), self.catalog)
        from pathlib import Path
        manifest = json.loads((Path(teams.__file__).with_name("teams-app") / "manifest.json").read_text("utf-8"))
        self.assertEqual(manifest["bots"][0]["scopes"], ["team", "groupChat", "personal"])
        self.assertEqual({p["name"] for p in manifest["authorization"]["permissions"]["resourceSpecific"]},
                         {"ChannelMessage.Read.Group", "ChatMessage.Read.Chat"})
        import package_chain_source as pkg
        for name in ("v11_teams.py", "teams_service.py", "teams_mention_prompt.txt"):
            self.assertIn(name, pkg.CHAIN_FILES)
        import cloud_providers
        self.assertIn("hamle-teams-bot-secret", cloud_providers.SECRET_NAMES)

    def test_conversations_map_only_to_usable_brand_routes(self):
        for bad in ({WAT_CHANNEL: "uydurma"}, {WAT_CHANNEL + ";messageid=1": "wat_mobilite"}):
            with self.assertRaises(teams.TeamsError):
                self.settings(conversations=bad)
        with self.assertRaises(teams.TeamsError):
            self.settings(service_hosts=["evil.example.com/x"])
        s = self.settings()
        self.assertEqual((s["conversations"], s["bot_id"]), ({WAT_CHANNEL: "wat_mobilite"}, "28:" + APP))


class Auth(TeamsBase):
    def test_bad_tokens_are_rejected_and_nothing_is_stored(self):
        desk = self.desk()
        now = self.now.timestamp()
        cases = {"auth_missing": "", "auth_token_malformed": "Bearer abc.def",
                 "auth_signature_invalid": jwt(now, key=KEY_B),
                 "auth_audience_invalid": jwt(now, aud="baska-bot"),
                 "auth_issuer_invalid": jwt(now, iss="https://sts.windows.net/x/"),
                 "auth_token_expired": jwt(now, exp=int(now) - 3600),
                 "auth_service_url_mismatch": jwt(now, serviceurl="https://smba.trafficmanager.net/amer/"),
                 "auth_algorithm_invalid": jwt(now, alg="HS256"),
                 "auth_key_unknown": jwt(now, kid="baska")}
        for reason, header in cases.items():
            with self.subTest(reason):
                status, body = desk.handle({"Authorization": header} if header else {},
                                           json.dumps(self.activity(mention=True)).encode())
                self.assertEqual((status, body["reason"]), (401, reason))
        self.assertEqual((self.stored(), self.sender.sent, self.client.calls), ({}, [], []))
        self.assertTrue(set(self.meta.urls) <= {teams.OPENID_URL, "https://login.botframework.com/v1/.well-known/keys"})

    def test_key_without_teams_endorsement_and_foreign_service_url(self):
        self.meta.keys = [jwk(KEY_A, "k1", endorsements=("skype",))]
        status, body = self.post(self.desk(), self.activity())
        self.assertEqual((status, body["reason"]), (401, "auth_channel_not_endorsed"))
        a = self.activity()
        a["serviceUrl"] = "https://evil.example.com/"
        status, body = self.post(self.desk(), a, auth=jwt(self.now.timestamp(), serviceurl=a["serviceUrl"]))
        self.assertEqual((status, body["reason"]), (401, "auth_service_url_forbidden"))
        self.assertEqual(self.stored(), {})


class Context(TeamsBase):
    def test_a_message_without_mention_is_stored_not_answered(self):
        status, body = self.post(self.desk(), self.activity("Story için görseller hazır.", files=["story.png"]))
        self.assertEqual((status, body["status"]), (200, "stored"))
        conv = self.conv()
        self.assertEqual((conv["brand_key"], conv["scope"], conv["type"]), ("wat_mobilite", "brand", "channel"))
        msg = conv["messages"][0]
        self.assertEqual((msg["author"], msg["text"], msg["files"], msg["to_hamle"]),
                         ("Özge Yağmur", "Story için görseller hazır.", [{"name": "story.png", "type": "reference"}],
                          False))
        self.assertEqual(self.ops.backend.data[teams.brand_index_key("wat_mobilite")]["keys"],
                         [teams.conversation_key(WAT_CHANNEL)])
        self.assertEqual((self.sender.sent, self.client.calls, self.mention_rows()), ([], [], []))
        self.assertEqual([m["text"] for m in teams.teams_context(self.ops, "wat_mobilite")],
                         ["Story için görseller hazır."])

    def test_unmapped_conversation_is_general_agency_context(self):
        self.post(self.desk(), self.activity("Cuma toplantısı 15:00'te.", conv=AGENCY_CHAT, kind="groupChat"))
        conv = self.conv(AGENCY_CHAT)
        self.assertEqual((conv["brand_key"], conv["scope"]), (None, "agency"))
        self.assertEqual(self.ops.backend.data[teams.AGENCY_INDEX]["keys"], [teams.conversation_key(AGENCY_CHAT)])
        self.assertNotIn(teams.brand_index_key("wat_mobilite"), self.ops.backend.data)

    def test_other_tenants_and_other_activities_are_ignored(self):
        desk = self.desk()
        status, body = self.post(desk, self.activity(tenant="11111111-2222-4333-8444-555555555555", mention=True))
        self.assertEqual((status, body["reason"]), (200, "teams_tenant_mismatch"))
        a = self.activity()
        a["type"] = "conversationUpdate"
        self.assertEqual(self.post(desk, a)[1]["reason"], "not_a_message")
        a = self.activity()
        a["conversation"]["conversationType"] = "meeting"
        self.assertEqual(self.post(desk, a)[1]["reason"], "teams_conversation_type_unsupported")
        self.assertEqual((self.stored(), self.client.calls), ({}, []))


class Mentions(TeamsBase):
    def test_mention_is_answered_in_the_same_thread_with_context(self):
        desk = self.desk()
        self.post(desk, self.activity("Berkay story'yi yarın sabah bitiriyor."))
        self.client.outputs = [message(out())]
        status, body = self.post(desk, self.activity("WAT story ne durumda?", mention=True))
        self.assertEqual((status, body["status"], body["outcome"]), (200, "answered", "answer"))
        sent = self.sender.sent[0]
        self.assertEqual((sent["service_url"], sent["conversation_id"], sent["reply_to_id"]),
                         (SERVICE, WAT_CHANNEL + ";messageid=1700", "a2"))
        self.assertEqual(sent["text"], out()["reply"])
        request = json.loads(self.client.calls[0]["messages"][0]["content"])
        self.assertEqual(request["message"]["text"], "@Hamle WAT story ne durumda?")
        self.assertEqual([m["text"] for m in request["context"]], ["Berkay story'yi yarın sabah bitiriyor."])
        self.assertEqual((request["brand"]["name"], request["task"]["possible"]), ("WAT Mobilite", True))
        row = self.mention_rows()[0]
        self.assertEqual((row["state"], row["action"], row["reply_id"]), ("done", "answer", "r1"))
        self.assertEqual(self.conv()["messages"][-1]["author"], "Hamle")  # Hamle's answer is context too

    def test_queued_answer_runs_after_the_http_reply(self):
        desk = self.desk(run_later=self.later.append)
        self.client.outputs = [message(out())]
        status, body = self.post(desk, self.activity("Durum?", mention=True))
        self.assertEqual((status, body["status"], self.sender.sent), (200, "queued", []))
        self.assertEqual(self.later.pop()(), "answer")
        self.assertEqual(len(self.sender.sent), 1)

    def test_task_opens_the_brand_todo_through_the_jobs_writer_and_links_it(self):
        self.client.outputs = [message(task_out(due_on="2026-10-15"))]
        status, body = self.post(self.desk(), self.activity("Hamle bunu Basecamp'e iş olarak açar mısın?", mention=True))
        self.assertEqual(body["outcome"], "task")
        todo = self.todos_bc.calls[0]
        self.assertEqual(todo["target"], {"account_id": 5898865, "project_id": WAT, "list_id": WAT_SOCIAL})
        self.assertEqual((todo["title"], todo["due_on"]), ("WM // Şarj istasyonu story", "2026-10-15"))
        self.assertEqual(len(todo["assignee_ids"]), 1)
        self.assertIn("Microsoft Teams", todo["description"])
        self.assertIn("https://app.basecamp.com/5898865/buckets/40694338/todos/777", self.sender.sent[0]["text"])
        row = self.mention_rows()[0]
        self.assertEqual((row["task_state"], row["task_kind"], row["task_remote_id"]), ("done", "create_todo", "777"))
        self.assertEqual(self.comments_bc.calls, [])

    def test_task_with_a_todo_link_is_a_comment_on_that_todo(self):
        self.client.outputs = [message(task_out())]
        self.post(self.desk(), self.activity(f"Hamle şunu bu to-do'ya yaz: müşteri tarih istedi {TODO_URL}",
                                             mention=True))
        comment = self.comments_bc.calls[0]
        self.assertEqual(comment["target"], {"account_id": 5898865, "project_id": WAT, "list_id": WAT_SOCIAL,
                                             "task_id": 10402860490})
        self.assertIs(comment["internal_only"], True)
        self.assertEqual(self.todos_bc.calls, [])
        self.assertIn(TODO_URL, self.sender.sent[0]["text"])

    def test_unmapped_chat_task_needs_a_brand(self):
        self.client.outputs = [message(task_out()), message(task_out(brand_key="rabam", list="project"))]
        desk = self.desk()
        self.post(desk, self.activity("Hamle bunu iş olarak aç", mention=True, conv=AGENCY_CHAT, kind="groupChat"))
        self.assertEqual(self.todos_bc.calls, [])
        self.assertIn("hangi markanın", self.sender.sent[0]["text"])
        self.post(desk, self.activity("Rabam için aç", mention=True, conv=AGENCY_CHAT, kind="groupChat"))
        self.assertEqual(self.todos_bc.calls[0]["target"]["project_id"], RABAM)

    def test_personal_chat_with_hamle_is_answered_without_a_mention(self):
        self.client.outputs = [message(out())]
        status, body = self.post(self.desk(), self.activity("Merhaba Hamle", conv="a:1on1-ozge", kind="personal"))
        self.assertEqual(body["outcome"], "answer")
        self.assertEqual(self.conv("a:1on1-ozge")["scope"], "personal")
        a = self.activity("x", conv="a:1on1-ozge", kind="personal")
        a["conversation"]["isGroup"] = True
        self.assertEqual(self.post(self.desk(), a)[1]["reason"], "teams_personal_chat_invalid")

    def test_model_failure_gets_a_fixed_answer(self):
        self.client.outputs = [RuntimeError("boom")]
        self.post(self.desk(), self.activity("Durum?", mention=True))
        self.assertIn("şu an işleyemedim", self.sender.sent[0]["text"])
        self.assertEqual(self.mention_rows()[0]["action"], "failed")


class Fences(TeamsBase):
    def test_same_activity_twice_is_answered_once(self):
        desk = self.desk()
        self.client.outputs = [message(out())]
        a = self.activity("Durum?", mention=True)
        self.post(desk, a)
        status, body = self.post(desk, a)
        self.assertEqual((status, body["status"]), (200, "duplicate"))
        self.assertEqual((len(self.client.calls), len(self.sender.sent), len(self.conv()["messages"])), (1, 1, 2))

    def test_dur_stops_reading_and_answering(self):
        desk = self.desk(run_later=self.later.append)
        self.client.outputs = [message(out())]
        self.post(desk, self.activity("Durum?", mention=True))
        self.ops.stop("ahmet@gamechangers.com.tr")  # 'dur' between the HTTP reply and the answer
        self.assertEqual(self.later.pop()(), "stopped")
        status, body = self.post(desk, self.activity("Bir şey daha", mention=True))
        self.assertEqual((status, body["status"]), (200, "stopped"))
        self.assertEqual((self.sender.sent, self.client.calls), ([], []))
        self.assertEqual(len(self.conv()["messages"]), 1)
        self.ops.resume_review_only("ahmet@gamechangers.com.tr")  # a new generation: the old mention stays stopped
        self.assertEqual(desk.answer(self.mention_rows()[0]["key"]), "already_recorded")

    def test_budget_cap_means_no_model_call(self):
        catalog = copy.deepcopy(self.catalog)
        catalog["budget"]["internal_live_cap_microdollars"] = 1
        self.post(self.desk(catalog=catalog), self.activity("Durum?", mention=True))
        self.assertEqual(self.client.calls, [])
        self.assertIn("bütçe sınırındayım", self.sender.sent[0]["text"])
        self.assertEqual(self.mention_rows()[0]["action"], "budget")

    def test_conversation_daily_limit_is_noted_once(self):
        desk = self.desk(max_replies_per_conversation_per_day=1)
        self.client.outputs = [message(out())]
        for text in ("Bir?", "İki?", "Üç?"):
            self.post(desk, self.activity(text, mention=True))
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(len(self.sender.sent), 2)
        self.assertIn("cevap sınırıma ulaştım", self.sender.sent[1]["text"])
        self.assertEqual(sorted(r["state"] for r in self.mention_rows()), ["done", "done", "held"])

    def test_global_daily_model_limit(self):
        desk = self.desk(max_model_calls_per_day=1)
        self.client.outputs = [message(out())]
        self.post(desk, self.activity("Bir?", mention=True))
        self.post(desk, self.activity("İki?", mention=True, conv=AGENCY_CHAT, kind="groupChat"))
        self.assertEqual(len(self.client.calls), 1)
        self.assertIn("model sınırına", self.sender.sent[1]["text"])

    def test_unconfirmed_teams_send_is_unknown_and_not_retried(self):
        desk = self.desk()
        self.client.outputs = [message(out())]
        self.sender.fail = RuntimeError("timeout")
        self.post(desk, self.activity("Durum?", mention=True))
        row = self.mention_rows()[0]
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(desk.answer(row["key"]), "already_recorded")
        self.assertEqual(len(self.sender.sent), 1)


class Service(TeamsBase):
    def call(self, app, method, path, body=b"", headers=None):
        env = {"REQUEST_METHOD": method, "PATH_INFO": path, "CONTENT_LENGTH": str(len(body)),
               "wsgi.input": io.BytesIO(body)}
        for k, v in (headers or {}).items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        got = {}
        chunks = app(env, lambda status, hdrs: got.update(status=status))
        return got["status"], json.loads(b"".join(chunks))

    def test_wsgi_routes_to_the_desk(self):
        import teams_service
        desk = self.desk()
        app = teams_service.create_app(lambda run_later: desk)
        self.assertEqual(self.call(app, "GET", "/health")[0], "200 OK")
        self.assertEqual(self.call(app, "GET", "/api/messages")[0], "405 Method Not Allowed")
        status, body = self.call(app, "POST", "/api/messages", json.dumps(self.activity()).encode(),
                                 {"Authorization": jwt(self.now.timestamp())})
        self.assertEqual((status, body["status"]), ("200 OK", "stored"))
        status, body = self.call(app, "POST", "/api/messages", b"{}", {})
        self.assertEqual(status, "401 Unauthorized")

    def test_disabled_config_holds_without_reading_anything(self):
        import teams_service

        def factory(run_later):
            raise teams.TeamsError("teams_disabled")
        status, body = self.call(teams_service.create_app(factory), "POST", "/api/messages", b"{}")
        self.assertEqual((status, body["reason"]), ("503 Service Unavailable", "teams_disabled"))


if __name__ == "__main__":
    unittest.main()
