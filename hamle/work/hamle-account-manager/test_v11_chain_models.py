"""Offline tests for v11_chain_models: fake transports only, no sockets or credentials."""
from model_routing import openai_only
import copy
import json
import socket
import unittest
from datetime import datetime
from unittest.mock import patch

import v11_chain_models as cm
from cloud_store import OperationsStore
from model import ModelError
from test_cloud_store import MemoryBackend, budget_snapshot
from test_v11_chain import live_catalog
from v11_intake_plan import plan_intake
from v11_work_queue import InMemoryBackend, QueueStore

KEY = "FAKE-KEY-NEVER-PERSIST-OR-PRINT"
NOW = datetime.fromisoformat("2026-10-12T09:00:00+03:00")


def interpretation(**changes):
    out = {"requirements": {"title": "Ofis afişi", "objective": "Yeni ofis duyurusu", "audience": "Ekip",
                            "deadline": "14 Ekim 17:00", "assets_ref": "Gamechangers logo arşivi",
                            "risk_area": "routine",
                            "deliverables": [{"id": "Afiş 1", "kind": "graphic", "quantity": 1,
                                              "formats": ["image/png"], "specification": "1080x1080 post",
                                              "duration_seconds": None, "width": 1080, "height": 1080,
                                              "required_text": ["YENİ OFİS", "uydurma metin"]}]},
           "missing_questions": ["Afişte adres yer alsın mı?"], "context_notes": ["Önceki afiş referans alınabilir."],
           "follow_ups": ["Story uyarlaması"]}
    out.update(changes)
    return out


def envelope(output, **changes):
    row = {"id": "resp_chain1", "model": "gpt-6-astra", "status": "completed", "error": None,
           "incomplete_details": None,
           "output": [{"type": "reasoning", "summary": []},
                      {"type": "message", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": json.dumps(output, ensure_ascii=False)}]}],
           "usage": {"input_tokens": 900, "output_tokens": 300, "total_tokens": 1200}}
    row.update(changes)
    return row


class FakeResponse:
    def __init__(self, data, status=200):
        self.status_code, self.raw = status, json.dumps(data).encode()
    def iter_content(self, chunk_size):
        for i in range(0, len(self.raw), chunk_size):
            yield self.raw[i:i + chunk_size]
    def close(self):
        pass


class Transport:
    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []
    def __call__(self, url, **kwargs):
        self.calls.append(json.loads(kwargs["data"]))
        item = self.outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)


class Base(unittest.TestCase):
    def setUp(self):
        for name in ("socket", "create_connection"):
            p = patch.object(socket, name, side_effect=AssertionError("Network forbidden"))
            p.start(); self.addCleanup(p.stop)
        self.ops = OperationsStore(MemoryBackend(), clock=lambda: NOW)
        self.ops.initialize_control(); self.ops.initialize_budget(budget_snapshot())
        self.gen = self.ops.resume_review_only("offline-test")["generation"]
        self.cache = QueueStore(InMemoryBackend(), clock=lambda: NOW.timestamp())
        self.keys = 0

    def key(self):
        self.keys += 1
        return KEY

    def models(self, transport):
        gate = cm.ModelGate(ledger=self.ops, cache=self.cache, generation=self.gen)
        return cm.ChainModels(load_key=self.key, gate=gate, transport=transport, routing=openai_only()), gate

    def reservations(self):
        return [r for r in self.ops.backend.data["budget"]["reservations"] if r["reservation_key"].startswith("chain-model-")]


class RequestShape(Base):
    def test_request_is_existing_openai_astra_max_strict_json_no_tools(self):
        req = cm.build_request("interpreter", {"mail_text": "x"})
        self.assertEqual((req["model"], req["reasoning"], req["max_output_tokens"]), ("gpt-6-astra", {"effort": "max"}, 16384))
        self.assertEqual((req["tools"], req["tool_choice"], req["store"], req["truncation"]), ([], "none", False, "disabled"))
        fmt = req["text"]["format"]
        self.assertEqual((fmt["type"], fmt["strict"], fmt["name"]), ("json_schema", True, "hamle_chain_interpreter"))
        self.assertIn("GÜVENİLMEYEN KAYNAK VERİ", req["instructions"])
        for role in ("classifier", "answerer"):
            self.assertIn("JSON", cm.build_request(role, {"x": "y"})["instructions"])

    def test_schemas_are_closed_objects(self):
        def walk(schema):
            if schema.get("type") == "object":
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
                for sub in schema["properties"].values():
                    walk(sub)
            if schema.get("type") == "array":
                walk(schema["items"])
        for schema in cm.SCHEMAS.values():
            walk(schema)

    def test_extra_key_refusal_and_wrong_model_are_rejected(self):
        bad = interpretation(); bad["extra"] = 1
        for response, code in ((envelope(bad), "invalid_analysis_schema"),
                               (envelope(interpretation(), model="gpt-4o"), "model_mismatch"),
                               (envelope(interpretation(), status="incomplete"), "response_incomplete")):
            with self.assertRaises(ModelError) as got:
                cm.call_once(KEY, "interpreter", {"mail_text": "x"}, transport=Transport(response))
            self.assertEqual(got.exception.code, code)
        refusal = envelope(interpretation())
        refusal["output"][1]["content"] = [{"type": "refusal", "refusal": "no"}]
        with self.assertRaises(ModelError) as got:
            cm.call_once(KEY, "interpreter", {"mail_text": "x"}, transport=Transport(refusal))
        self.assertEqual(got.exception.code, "model_refusal")


class Interpreter(Base):
    TEXT = "Merhaba Hamle, 14 Ekim 17:00'a kadar 1080x1080 PNG afiş lazım. Üzerinde 'YENİ OFİS' yazsın."

    def test_grounding_drops_invented_literals_and_maps_to_plan_intake(self):
        models, gate = self.models(Transport(envelope(interpretation())))
        result = models.interpreter(self.TEXT, {"subject": "Ofis afişi", "brand_id": "gamechangers", "past_works": []})
        req = result["requirements"]
        self.assertEqual(req["deadline"], "14 Ekim 17:00")
        item, = req["deliverables"]
        self.assertEqual((item["id"], item["required_text"], item["width"]), ("afis-1", ["YENİ OFİS"], 1080))
        self.assertNotIn("duration_seconds", item)
        self.assertEqual(result["context_notes"], ["Önceki afiş referans alınabilir."])
        self.assertEqual((gate.calls, self.keys, len(self.reservations())), (1, 1, 1))
        self.assertEqual(self.reservations()[0]["reserved_microdollars"], 2_000_000)

    def test_ungrounded_deadline_becomes_a_clarification_not_a_guess(self):
        output = interpretation()
        output["requirements"]["deadline"] = "15 Ekim"  # not in the mail
        models, _ = self.models(Transport(envelope(output)))
        req = models.interpreter(self.TEXT.replace("14 Ekim 17:00'a kadar ", ""), {"subject": "Afiş"})["requirements"]
        self.assertNotIn("deadline", req)
        catalog = live_catalog()
        req.update(brand_id="gamechangers", service_id="gc_internal", category_key="internal")
        source = {"channel": "email", "verified": True, "headers_origin": "provider", "latest_in_thread": True,
                  "evidence_ref": "SYNTHETIC", "message_id": "m1", "thread_id": "t1", "fingerprint": "a" * 64,
                  "observed_at": NOW.isoformat(), "automatic": False, "own_message": False, "human_takeover": False,
                  "sender": "ahmet@gamechangers.com.tr", "to": ["hamle@gamechangers.com.tr"], "cc": [],
                  "origin": {"kind": "direct_email"}}
        plan = plan_intake(source, req, catalog, now=NOW)
        self.assertEqual(plan["status"], "needs_clarification")
        self.assertIn("deadline", [q["field"] for q in plan["questions"]])

    def test_same_mail_never_buys_a_second_model_chance(self):
        transport = Transport(envelope(interpretation()))
        models, gate = self.models(transport)
        first = models.interpreter(self.TEXT, {"subject": "Ofis afişi", "brand_id": "gamechangers"})
        second = models.interpreter(self.TEXT, {"subject": "Ofis afişi", "brand_id": "gamechangers",
                                                "past_works": [{"title": "x", "status": "y"}]})
        self.assertEqual(first, second)
        self.assertEqual((len(transport.calls), gate.calls, self.keys, len(self.reservations())), (1, 1, 1, 1))

    def test_started_chance_without_result_is_never_retried(self):
        transport = Transport(TimeoutError("synthetic disconnect"))
        models, _ = self.models(transport)
        with self.assertRaises(cm.ChainModelError) as got:
            models.interpreter(self.TEXT, {"subject": "Ofis afişi"})
        self.assertEqual(got.exception.code, "model_failed_no_retry:transport_outcome_unknown")
        self.assertEqual(self.reservations()[0]["state"], "unknown")  # hold kept until reconciliation
        transport.outputs.append(envelope(interpretation()))
        with self.assertRaises(cm.ChainModelError) as got:
            models.interpreter(self.TEXT, {"subject": "Ofis afişi"})
        self.assertEqual(got.exception.code, "model_outcome_unknown_no_retry")
        self.assertEqual((len(transport.calls), self.keys), (1, 1))

    def test_oversize_input_fails_before_ledger_or_key(self):
        models, gate = self.models(Transport())
        with self.assertRaises(Exception):
            models.interpreter("x" * 12000, {"subject": "s" * 300, "past_works": [{"title": "t" * 200, "status": "s"}] * 20,
                                             "brand_id": "gamechangers" * 400})
        self.assertEqual((gate.calls, self.keys, self.reservations()), (0, 0, []))


class ClassifierAnswerer(Base):
    def test_classifier_returns_enum_class(self):
        transport = Transport(envelope({"class": "submission", "evidence": "dosya ekte"}))
        models, _ = self.models(transport)
        self.assertEqual(models.classifier("Dosya ekte, teslim.", {"status": "in_production", "producer": True,
                                                                   "manager": True}), "submission")
        self.assertEqual(transport.calls[0]["text"]["format"]["schema"]["properties"]["class"]["enum"],
                         ["question", "submission", "approval", "other"])

    def test_answer_only_when_grounded_in_non_empty_brief_or_profile_fields(self):
        ctx = {"brief": {"title": "Afiş", "deadline": "14 Ekim 17:00", "audience": None}, "profile": {"brand_id": "gamechangers"}}
        cases = [({"answerable": True, "answer": "Teslim 14 Ekim 17:00.", "grounded_in": ["brief.deadline"]},
                  {"answer": "Teslim 14 Ekim 17:00.", "grounded_in": ["brief.deadline"]}),
                 ({"answerable": False, "answer": None, "grounded_in": []}, None),
                 ({"answerable": True, "answer": "Ekip.", "grounded_in": ["brief.audience"]}, None),
                 ({"answerable": True, "answer": "Bir şey", "grounded_in": []}, None)]
        for i, (output, expected) in enumerate(cases):
            models, _ = self.models(Transport(envelope(output)))
            self.assertEqual(models.answerer(f"soru {i}", ctx), expected)

    def test_unknown_reference_is_rejected_by_schema(self):
        models, _ = self.models(Transport(envelope({"answerable": True, "answer": "x", "grounded_in": ["web.search"]})))
        with self.assertRaises(cm.ChainModelError) as got:
            models.answerer("soru", {"brief": {}, "profile": {}})
        self.assertEqual(got.exception.code, "model_failed_no_retry:invalid_analysis_schema")


if __name__ == "__main__":
    unittest.main()
