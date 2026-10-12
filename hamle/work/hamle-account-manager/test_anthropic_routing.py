"""Claude (Anthropic) provider path: routing config, transport, ledger, digest, worker.

Fakes only: a fake messages client stands in for anthropic.Anthropic; sockets are patched
to fail, so no Anthropic/OpenAI/Gmail/Basecamp/GCP call can happen.
"""
import contextlib
import copy
import io
import json
import socket
import sys
import types
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import cloud_providers
import model
import model_routing
import v11_chain_models as cm
import v11_daily_digest as dd
from cloud_store import ANTHROPIC_PROVIDER_SEED, OperationsStore, StoreError
from model import ModelError
from test_cloud_store import MemoryBackend, budget_snapshot
from test_model import draft
from test_v11_chain_models import interpretation
from v11_chain_live import load_catalog
from v11_work_queue import InMemoryBackend, QueueStore

KEY = "sk-ant-FAKE-KEY-NEVER-PERSIST-OR-PRINT-000"
NOW = datetime.fromisoformat("2026-10-12T09:00:00+03:00")
ROOT = Path(__file__).resolve().parents[2]


def usage(inp=1000, out=500, **kw):
    row = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    row.update(kw)
    return row


def message(output=None, *, model_id="claude-opus-5-5", stop="end_turn", use=None, content=None, rid="msg_01Fake"):
    if content is None:
        content = [{"type": "thinking", "thinking": "", "signature": "sig"},
                   {"type": "text", "text": json.dumps(output, ensure_ascii=False)}]
    return {"id": rid, "type": "message", "role": "assistant", "model": model_id, "stop_reason": stop,
            "stop_sequence": None, "content": content, "usage": use or usage()}


class FakeMessage:
    def __init__(self, data):
        self.data = data

    def to_dict(self):
        return copy.deepcopy(self.data)


class StatusError(Exception):
    """Shape of anthropic.APIStatusError (status_code, type, message)."""
    def __init__(self, status, error_type=None, message="secret detail sk-ant-LEAK"):
        super().__init__(message)
        self.status_code, self.type, self.message = status, error_type, message


class FakeClient:
    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeMessage(item)

    def stream(self, **kwargs):
        return FakeStream(self.create(**kwargs))


class FakeStream:
    """Shape of the SDK MessageStream context manager (events, then get_final_message)."""
    def __init__(self, final):
        self.final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(({"type": "message_start"}, {"type": "message_stop"}))

    def get_final_message(self):
        return self.final


class NoNetwork(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.routing = model_routing.load()


class RoutingConfig(NoNetwork):
    def test_disk_config_routes_every_role_to_claude(self):
        r = self.routing
        self.assertEqual(r.default_provider, "anthropic")
        opus = ("chain_interpreter", "chain_answerer", "customer_text", "trend_radar", "monday_special_days",
                "new_business_research", "daily_spark")
        for role in opus:
            self.assertEqual((r.route(role).provider, r.route(role).model), ("anthropic", "claude-opus-5-5"), role)
        for role in ("chain_classifier", "digest_classifier"):
            self.assertEqual((r.route(role).provider, r.route(role).model), ("anthropic", "claude-haiku-5-5"), role)
        for role in model_routing.ROLES:
            self.assertIn(r.route(role).effort, model_routing.EFFORTS)  # effort always explicit
        for role in model_routing.WEB_SEARCH_ROLES:
            self.assertEqual(r.route(role).web_search, {"type": "web_search_20260209", "max_uses": 4,
                                                        "fee_microdollars": 10_000})
        self.assertIsNone(r.route("chain_interpreter").web_search)
        self.assertTrue(r.flag("monday_special_days_research_enabled"))
        self.assertTrue(r.flag("new_business_research_enabled"))
        self.assertTrue(r.flag("daily_spark_research_enabled"))

    def test_openai_stays_selectable_but_never_default_and_bad_files_fail_closed(self):
        doc = json.loads((ROOT / "outputs/gamechangers-internal/model-routing.json").read_text(encoding="utf-8"))
        doc["roles"]["chain_classifier"] = {"provider": "openai", "profile": "astra-max-v1"}
        r = model_routing.validate(doc)
        self.assertEqual((r.route("chain_classifier").provider, r.route("chain_classifier").model),
                         ("openai", "gpt-6-astra"))
        bad_cases = [("default_provider", "openai"),
                     ("roles", {**doc["roles"], "chain_answerer": {**doc["roles"]["chain_interpreter"],
                                                                    "model": "claude-opus-5"}}),
                     ("roles", {**doc["roles"], "chain_answerer": {"provider": "openai",
                                                                    "profile": "sol-medium-candidate-v1"}}),
                     ("roles", {**doc["roles"], "chain_answerer": {**doc["roles"]["chain_interpreter"],
                                                                    "effort": None}}),
                     ("roles", {**doc["roles"], "trend_radar": {k: v for k, v in doc["roles"]["trend_radar"].items()
                                                                if k != "web_search"}}),
                     ("web_search", {**doc["web_search"], "fee_microdollars_per_search": 1}),
                     ("flags", {"monday_special_days_research_enabled": "yes", "new_business_research_enabled": False})]
        for key, value in bad_cases:
            with self.assertRaises(model_routing.RoutingError, msg=key):
                model_routing.validate({**doc, key: value})
        with self.assertRaises(model_routing.RoutingError):
            model_routing.load("/nonexistent/model-routing.json")

    def test_packaged_with_chain_image(self):
        import package_chain_source as pkg
        self.assertIn(pkg.APP + "model_routing.py", pkg.ALLOWLIST)
        self.assertEqual(pkg.ALLOWLIST[pkg.APP + "chain-profile/model-routing.json"],
                         "outputs/gamechangers-internal/model-routing.json")
        reqs = (Path(__file__).with_name("requirements-cloud.txt")).read_text().splitlines()
        self.assertIn("anthropic==1.8.0", reqs)


class Request(NoNetwork):
    def test_structured_request_shape(self):
        route = self.routing.route("chain_interpreter")
        kw = model.anthropic_request(route, "sys", "{}", schema=cm.INTERPRETER_SCHEMA)
        self.assertEqual(set(kw), {"model", "max_tokens", "system", "messages", "output_config"})
        self.assertEqual(kw["model"], "claude-opus-5-5")
        self.assertEqual(kw["output_config"]["effort"], "high")
        self.assertEqual(kw["output_config"]["format"]["type"], "json_schema")
        self.assertEqual([m["role"] for m in kw["messages"]], ["user"])  # no assistant prefill
        raw = json.dumps(kw)
        for forbidden in ('"thinking"', "budget_tokens", "tool_choice", "fallbacks", "betas", '"minLength"',
                          '"maxLength"', '"maxItems"', '"minimum"', '"maximum"', '"type": ["'):
            self.assertNotIn(forbidden, raw)
        schema = kw["output_config"]["format"]["schema"]
        title = schema["properties"]["requirements"]["properties"]["title"]
        self.assertEqual(title["anyOf"][1], {"type": "null"})
        self.assertFalse(schema["additionalProperties"])

    def test_web_search_tool_only_on_research_roles(self):
        kw = model.anthropic_request(self.routing.route("trend_radar"), "sys", "{}")
        self.assertEqual(kw["tools"], [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}])
        self.assertNotIn("format", kw["output_config"])
        self.assertNotIn("tools", model.anthropic_request(self.routing.route("digest_classifier"), "s", "{}"))

    def test_cost_from_usage(self):
        opus = usage(1000, 500, cache_read_input_tokens=1000, web_search_requests=0)
        u = {**opus}
        self.assertEqual(model.anthropic_cost_microdollars("claude-opus-5-5", u), 4000 + 10_000 + 200)
        u = {**usage(1000, 1000), "web_search_requests": 3}
        self.assertEqual(model.anthropic_cost_microdollars("claude-opus-5-5", u), 4000 + 20_000 + 30_000)
        u = {**usage(1000, 1000), "web_search_requests": 0}
        self.assertEqual(model.anthropic_cost_microdollars("claude-haiku-5-5", u), 100 + 500)
        u = {**usage(200_000, 1000), "web_search_requests": 0}  # > 100K prompt: long-context Haiku price
        self.assertEqual(model.anthropic_cost_microdollars("claude-haiku-5-5", u), 100_000 + 2500)
        u = {**usage(3, 1), "web_search_requests": 0}  # fractions round up
        self.assertEqual(model.anthropic_cost_microdollars("claude-haiku-5-5", u), 1)


class Transport(NoNetwork):
    def route(self, role="chain_interpreter"):
        return self.routing.route(role)

    def test_sdk_client_built_with_max_retries_zero_and_explicit_key(self):
        seen = {}
        fake = types.ModuleType("anthropic")
        def factory(**kwargs):
            seen.update(kwargs)
            return FakeClient(message(interpretation()))
        fake.Anthropic = factory
        with patch.dict(sys.modules, {"anthropic": fake}):
            data = model.anthropic_send(KEY, self.route(), {"model": "x"})
        self.assertEqual(seen, {"api_key": KEY, "max_retries": 0, "timeout": 300.0})
        self.assertEqual(data["id"], "msg_01Fake")

    def test_key_must_be_anthropic_key(self):
        for bad in ("sk-proj-" + "a" * 40, "", None, "sk-ant- has spaces inside it"):
            with self.assertRaises(ModelError) as ctx:
                model.anthropic_send(bad, self.route(), {}, client=FakeClient())
            self.assertEqual(ctx.exception.code, "credential_missing_or_invalid")

    def test_api_errors_map_to_fixed_codes_without_text(self):
        cases = [(StatusError(402, "billing_error"), "anthropic_credit_insufficient"),
                 (StatusError(400, "invalid_request_error", "Your credit balance is too low to access the API"),
                  "anthropic_credit_insufficient"),
                 (StatusError(400, "invalid_request_error"), "request_rejected"),
                 (StatusError(401, "authentication_error"), "authentication_rejected"),
                 (StatusError(429, "rate_limit_error"), "quota_or_rate_limit"),
                 (StatusError(529, "overloaded_error"), "provider_overloaded_no_retry"),
                 (StatusError(500, "api_error"), "http_error_no_retry"),
                 (TimeoutError("socket sk-ant-LEAK"), "transport_outcome_unknown")]
        for exc, code in cases:
            client = FakeClient(exc)
            with self.assertRaises(ModelError) as ctx:
                model.anthropic_send(KEY, self.route(), {"model": "x"}, client=client)
            self.assertEqual(ctx.exception.code, code)
            self.assertEqual(len(client.calls), 1)  # one attempt, never retried
            text = repr(ctx.exception) + str(ctx.exception.__dict__) + str(ctx.exception.__cause__)
            self.assertNotIn("LEAK", text); self.assertNotIn(KEY, text)

    def test_stop_reasons_refusal_max_tokens_pause_turn_are_held(self):
        for stop, code in (("refusal", "model_refusal"), ("max_tokens", "response_max_tokens"),
                           ("pause_turn", "response_pause_turn"), ("tool_use", "response_not_completed")):
            with self.assertRaises(ModelError) as ctx:
                model.anthropic_extract(self.route(), message(interpretation(), stop=stop))
            self.assertEqual(ctx.exception.code, code)
            self.assertFalse(ctx.exception.retry_allowed)
            receipt = ctx.exception.failure_receipt
            self.assertEqual((receipt["stop_reason"], receipt["cost_microdollars"]), (stop, 14_000))

    def test_wrong_model_tool_blocks_and_bad_usage_rejected(self):
        bad = [(message(interpretation(), model_id="claude-opus-5"), "model_mismatch"),
               (message(content=[{"type": "tool_use", "id": "t", "name": "x", "input": {}}]),
                "unexpected_tool_or_output"),
               (message(content=[{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}]),
                "ambiguous_or_oversize_output"),
               (message(interpretation(), use={"input_tokens": -1, "output_tokens": 1}), "usage_missing_or_invalid"),
               (message(interpretation(), use=usage(10, 20_000)), "usage_limit_exceeded"),
               ({**message(interpretation()), "id": "resp_x"}, "invalid_response_id")]
        for data, code in bad:
            with self.assertRaises(ModelError) as ctx:
                model.anthropic_extract(self.route(), data)
            self.assertEqual(ctx.exception.code, code)

    def test_thinking_is_skipped_and_receipt_has_cost(self):
        out = model.anthropic_extract(self.route(), message(interpretation()))
        self.assertEqual(json.loads(out["text"]), interpretation())
        self.assertEqual(out["receipt"], {"provider": "anthropic", "response_id": "msg_01Fake",
                                          "model": "claude-opus-5-5", "effort": "high",
                                          "usage": {**usage(), "web_search_requests": 0},
                                          "cost_microdollars": 14_000})


class Ledger(NoNetwork):
    def setUp(self):
        super().setUp()
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: NOW)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.gen = self.ops.resume_review_only("offline-test")["generation"]
        self.cache = QueueStore(InMemoryBackend(), clock=lambda: NOW.timestamp())
        self.keys = 0

    def key(self):
        self.keys += 1
        return KEY

    def models(self, client, cap=None):
        gate = cm.ModelGate(ledger=self.ops, cache=self.cache, generation=self.gen, cap_microdollars=cap)
        return cm.ChainModels(load_key={"anthropic": self.key}, gate=gate, client=client, routing=self.routing)

    def reservations(self):
        return [r for r in self.ops.backend.data["budget"]["reservations"]
                if r["reservation_key"].startswith("chain-model-")]

    def test_legacy_budget_gains_anthropic_prepaid_provider_on_first_hold(self):
        before = self.ops.budget_status()
        self.ops.budget_reserve("probe-claude", "anthropic", 1_000_000)
        b = self.ops.backend.data["budget"]
        self.assertEqual(b["providers"]["anthropic"], ANTHROPIC_PROVIDER_SEED)
        self.assertEqual(b["provider_additions"][0]["provider"], "anthropic")
        self.assertEqual(self.ops.budget_status()["total_committed_microdollars"],
                         before["total_committed_microdollars"] + 20_000_000)
        self.ops.budget_reserve("probe-claude-2", "anthropic", 1_000_000)
        self.assertEqual(len(self.ops.backend.data["budget"]["provider_additions"]), 1)

    def test_interpreter_on_opus_settles_actual_cost_and_never_repeats(self):
        client = FakeClient(message(interpretation()))
        models = self.models(client)
        out = models.interpreter("Ofis afişi lazım. Son tarih 14 Ekim 17:00. Üzerinde YENİ OFİS yazsın.",
                                 {"brand_id": "gamechangers", "subject": "Afiş"})
        self.assertEqual(out["requirements"]["deadline"], "14 Ekim 17:00")
        call = client.calls[0]
        self.assertEqual((call["model"], call["output_config"]["effort"]), ("claude-opus-5-5", "high"))
        self.assertIn("Hamle", call["system"])  # chain_interpreter_prompt.txt as system prompt
        [r] = self.reservations()
        self.assertEqual((r["provider"], r["state"], r["reserved_microdollars"], r["actual_microdollars"],
                          r["evidence_ref"]), ("anthropic", "settled", 2_000_000, 14_000, "msg_01Fake"))
        receipt = self.ops.model_receipt(r["reservation_key"])["receipt"]
        self.assertEqual((receipt["provider"], receipt["cost_microdollars"]), ("anthropic", 14_000))
        models.interpreter("Ofis afişi lazım. Son tarih 14 Ekim 17:00. Üzerinde YENİ OFİS yazsın.",
                           {"brand_id": "gamechangers", "subject": "Afiş"})
        self.assertEqual((len(client.calls), self.keys), (1, 1))  # cached, one paid chance

    def test_bad_stored_key_settles_zero_and_names_the_cause(self):
        def bad():
            raise cloud_providers.ProviderError("anthropic_credential_unavailable")
        gate = cm.ModelGate(ledger=self.ops, cache=self.ops, generation=self.gen)
        models = cm.ChainModels(load_key={"anthropic": bad}, gate=gate, client=FakeClient(), routing=self.routing)
        with self.assertRaises(cm.ChainModelError) as got:
            models.interpreter("Ofis afişi lazım.", {"brand_id": "gamechangers", "subject": "Afiş"})
        self.assertEqual(str(got.exception), "model_failed_no_retry:anthropic_credential_unavailable")
        [r] = self.reservations()
        self.assertEqual((r["state"], r["actual_microdollars"]), ("settled", 0))

    def test_reconcile_not_sent_settles_zero_and_allows_one_fresh_attempt(self):
        gate = cm.ModelGate(ledger=self.ops, cache=self.ops, generation=self.gen)
        def boom():
            raise RuntimeError("legacy unknown")  # pre-fix behaviour: generic exception -> unknown
        with self.assertRaises(cm.ChainModelError):
            gate.once("chain_interpreter", "a" * 64, boom, provider="anthropic")
        [r] = self.reservations()
        self.assertEqual(r["state"], "unknown")
        key = "chain-model-" + "a" * 64
        with self.assertRaises(cm.ChainModelError):
            cm.reconcile_not_sent(self.ops, [key], "bad evidence!")
        out = cm.reconcile_not_sent(self.ops, [key], "no_request_sent_invalid_stored_key")
        self.assertEqual((out["status"], out["count"]), ("reconciled_not_sent", 1))
        [r] = self.reservations()
        self.assertEqual((r["state"], r["actual_microdollars"]), ("settled", 0))
        with self.assertRaises(cm.ChainModelError):  # already reconciled: not 'started' any more
            cm.reconcile_not_sent(self.ops, [key], "no_request_sent_invalid_stored_key")
        result = {"output": {"ok": True}, "receipt": {"response_id": "msg_x", "cost_microdollars": 5}}
        gate2 = cm.ModelGate(ledger=self.ops, cache=self.ops, generation=self.gen)
        gate2.ledger.record_model_receipt = lambda *a, **k: None
        self.assertEqual(gate2.once("chain_interpreter", "a" * 64, lambda: result, provider="anthropic"), {"ok": True})
        keys = [r["reservation_key"] for r in self.reservations()]
        self.assertEqual(keys, ["chain-model-" + "a" * 48, "chain-model-" + "a" * 48 + "-a2"])

    def test_classifier_on_haiku(self):
        client = FakeClient(message({"class": "question", "evidence": "Soru soruyor."}, model_id="claude-haiku-5-5"))
        self.assertEqual(self.models(client).classifier("Bu ne zaman biter?", {"status": "x"}), "question")
        self.assertEqual((client.calls[0]["model"], client.calls[0]["output_config"]["effort"]),
                         ("claude-haiku-5-5", "low"))
        [r] = self.reservations()
        self.assertEqual(r["actual_microdollars"], 100 + 250)

    def test_refusal_is_held_settled_at_usage_and_never_retried(self):
        client = FakeClient(message(interpretation(), stop="refusal"))
        models = self.models(client)
        with self.assertRaises(cm.ChainModelError) as ctx:
            models.answerer("Soru?", {"brief": {"title": "x"}, "profile": {}})
        self.assertEqual(ctx.exception.code, "model_failed_no_retry:model_refusal")
        [r] = self.reservations()
        self.assertEqual((r["state"], r["actual_microdollars"]), ("settled", 14_000))
        failure = self.ops.model_failure(r["reservation_key"])
        self.assertEqual((failure["reason"], failure["receipt"]["stop_reason"]), ("model_refusal", "refusal"))
        with self.assertRaises(cm.ChainModelError) as ctx:
            models.answerer("Soru?", {"brief": {"title": "x"}, "profile": {}})
        self.assertEqual(ctx.exception.code, "model_outcome_unknown_no_retry")
        self.assertEqual(len(client.calls), 1)

    def test_credit_insufficient_is_held_with_fixed_code_and_zero_charge(self):
        client = FakeClient(StatusError(400, "invalid_request_error", "Your credit balance is too low"))
        with self.assertRaises(cm.ChainModelError) as ctx:
            self.models(client).classifier("x", {})
        self.assertEqual(ctx.exception.code, "model_failed_no_retry:anthropic_credit_insufficient")
        [r] = self.reservations()
        self.assertEqual((r["state"], r["actual_microdollars"], r["evidence_ref"]),
                         ("settled", 0, "anthropic_credit_insufficient"))

    def test_transport_unknown_keeps_full_hold(self):
        client = FakeClient(TimeoutError())
        with self.assertRaises(cm.ChainModelError):
            self.models(client).classifier("x", {})
        [r] = self.reservations()
        self.assertEqual((r["state"], r["reserved_microdollars"], r["actual_microdollars"]), ("unknown", 2_000_000, None))

    def test_cap_blocks_before_key_or_client(self):
        client = FakeClient()
        with self.assertRaises(cm.ChainModelError) as ctx:
            self.models(client, cap=1).classifier("x", {})
        self.assertEqual(ctx.exception.code, "internal_live_budget_cap_reached")
        self.assertEqual((client.calls, self.keys, self.reservations()), ([], 0, []))

    def test_anthropic_receipts_are_validated(self):
        self.ops.budget_reserve("c1", "anthropic", 1_000_000); self.ops.budget_begin("c1", self.gen)
        good = {"provider": "anthropic", "response_id": "msg_1", "model": "claude-opus-5-5", "effort": "high",
                "usage": {**usage(), "web_search_requests": 0}, "cost_microdollars": 14_000}
        for bad in ({**good, "model": "gpt-6-astra"}, {**good, "cost_microdollars": -1},
                    {**good, "usage": {"input_tokens": 1}}, {**good, "extra": 1}):
            with self.assertRaises(StoreError):
                self.ops.record_model_receipt("c1", bad)
        self.ops.record_model_receipt("c1", good)
        self.ops.budget_reserve("o1", "openai", 1_000_000); self.ops.budget_begin("o1", self.gen)
        with self.assertRaises(StoreError):  # anthropic receipt on an openai hold
            self.ops.record_model_receipt("o1", good)


class Digest(NoNetwork):
    def setUp(self):
        super().setUp()
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: NOW)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.ops.resume_review_only("offline-test")

    def test_digest_classifier_runs_on_haiku_through_the_gate(self):
        out = {"items": [{"ref": "o1", "status": "waiting_client", "reason": "Onay bekleniyor.",
                          "action": "Tarihi teyit edin."}]}
        client = FakeClient(message(out, model_id="claude-haiku-5-5"))
        classify = dd.model_classifier(self.ops, {"anthropic": lambda: KEY}, load_catalog(), client=client)
        self.assertEqual(classify({"today": "2026-10-12", "items": []}), out)
        self.assertEqual(client.calls[0]["model"], "claude-haiku-5-5")
        self.assertEqual(client.calls[0]["output_config"]["effort"], "low")
        self.assertEqual(classify({"today": "2026-10-12", "items": []}), out)  # byte-identical input: cached
        self.assertEqual(len(client.calls), 1)

    def test_digest_still_builds_when_claude_unavailable(self):
        from test_v11_daily_digest import Basecamp, ACC
        bc = Basecamp()
        client = FakeClient(StatusError(402, "billing_error"))
        classify = dd.model_classifier(self.ops, {"anthropic": lambda: KEY}, load_catalog(), client=client)
        body, stats = dd.build_digest(dd.BasecampDigestReader(ACC, get=bc.get), day=date(2026, 10, 12),
                                      day_note=None, classifier=classify, cards=dd.load_brand_cards(),
                                      calendar=dd.load_calendar())
        self.assertEqual(stats["model"], "unclassified:model_failed_no_retry:anthropic_credit_insufficient")
        self.assertIn("sınıflandırılmadı", body)

    def test_worker_digest_entry_uses_anthropic_key_loader(self):
        import inspect
        src = inspect.getsource(dd.run_worker_daily_digest)
        self.assertIn("provider.anthropic_key(secrets)", src)


class Secret(NoNetwork):
    def test_anthropic_key_loader(self):
        class Secrets:
            def __init__(self, value):
                self.value, self.names = value, []
            def access(self, name):
                self.names.append(name)
                return self.value
        s = Secrets(("  " + KEY + "\n").encode())
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            self.assertEqual(cloud_providers.anthropic_key(s), KEY)
        self.assertEqual((s.names, captured.getvalue()), (["hamle-anthropic-api-key"], ""))
        self.assertIn("hamle-anthropic-api-key", cloud_providers.SECRET_NAMES)
        for bad in (b"sk-proj-" + b"a" * 40, b"sk-ant-short", b"sk-ant-" + b"a" * 30 + b" x", b"\xff\xfe"):
            with self.assertRaises(cloud_providers.ProviderError) as ctx:
                cloud_providers.anthropic_key(Secrets(bad))
            self.assertEqual(str(ctx.exception), "anthropic_credential_unavailable")
            self.assertNotIn("aaaa", repr(ctx.exception.__dict__))


class Worker(NoNetwork):
    def test_review_on_opus_records_receipt_and_settles(self):
        from test_worker import WorkerTests
        harness = WorkerTests("test_valid_source_produces_exact_review_envelope_no_send")
        harness.setUp()
        analysis = draft(); analysis["brief"]["requested_deadline"] = None
        client = FakeClient(message(analysis))
        route = self.routing.route("customer_text")
        def call(key, request):
            return model.call_anthropic_once(key, request, route, client=client)
        call.model_provider = "anthropic"
        harness.open()
        result = harness.run_worker(model_call=call, load_key=lambda: KEY)
        self.assertEqual(result["status"], "review_required", result)
        self.assertEqual((result["model"], result["reasoning_effort"], result["model_provider"]),
                         ("claude-opus-5-5", "high", "anthropic"))
        [r] = [r for r in harness.store.backend.data["budget"]["reservations"] if r["provider"] == "anthropic"]
        self.assertEqual((r["state"], r["actual_microdollars"]), ("settled", 14_000))
        self.assertEqual(client.calls[0]["output_config"]["format"]["type"], "json_schema")

    def test_review_model_selection_follows_routing(self):
        import worker
        picked = worker._review_model(cloud_providers, None, routing=self.routing)
        self.assertEqual(getattr(picked["model_call"], "model_provider", None), "anthropic")
        picked = worker._review_model(cloud_providers, None, routing=model_routing.openai_only())
        self.assertIsNone(getattr(picked["model_call"], "model_provider", None))


if __name__ == "__main__":
    unittest.main()
