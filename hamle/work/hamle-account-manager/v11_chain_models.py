"""Chain model callables on the existing OpenAI Responses path (model.py, gpt-6-astra).

Three bounded, strict-JSON roles for v11_chain.ChainDeps:
  interpreter(text, context) -> {'requirements','missing_questions','context_notes','follow_ups'}
  classifier(text, context)  -> 'question' | 'submission' | 'approval' | 'other'
  answerer(text, context)    -> {'answer','grounded_in'} only when grounded in brief/profile, else None

Every call goes through ModelGate: ONE model chance per stable source digest.
The gate durably reserves and begins the shared budget (cloud_store ledger) and
records a cache row before the POST. A completed result is cached and reused;
a started-but-unfinished chance is never retried (model_outcome_unknown_no_retry).
The provider, model, effort, timeouts, no-retry transport and response parser
rules are those of model.py; no other provider exists here. Model output never
grants authority: plan_intake, the quality gate and dispatch gates still decide.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from pathlib import Path

import requests

import model as base
from model import ModelError

HERE = Path(__file__).resolve().parent
PROMPTS = {"interpreter": HERE / "chain_interpreter_prompt.txt",
           "classifier": HERE / "chain_classifier_prompt.txt",
           "answerer": HERE / "chain_answerer_prompt.txt"}
# Same conservative per-attempt hold as worker.MODEL_RESERVATION (2 USD); the
# shared ledger keeps it until billing reconciliation.
MODEL_RESERVATION = 2_000_000
CLASSES = ("question", "submission", "approval", "other")
MIMES = ("image/png", "image/jpeg", "application/pdf", "text/plain", "video/mp4")
BRIEF_REFS = ("title", "objective", "audience", "deadline", "assets_ref", "deliverables")
PROFILE_REFS = ("terms", "brand_id", "verification_scope")
ANSWER_REFS = tuple("brief." + k for k in BRIEF_REFS) + tuple("profile." + k for k in PROFILE_REFS)


class ChainModelError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code):
    if not ok:
        raise ChainModelError(code)


# ---------------------------------------------------------------- schemas
def _obj(props):
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _str(maximum):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _nstr(maximum):
    return {"type": ["string", "null"], "minLength": 1, "maxLength": maximum}


def _nint(low, high):
    return {"type": ["integer", "null"], "minimum": low, "maximum": high}


def _arr(items, maximum):
    return {"type": "array", "items": items, "maxItems": maximum}


INTERPRETER_SCHEMA = _obj({
    "requirements": _obj({
        "title": _nstr(200), "objective": _nstr(1000), "audience": _nstr(500), "deadline": _nstr(120),
        "assets_ref": _nstr(500),
        "risk_area": {"type": "string", "enum": ["routine", "commercial", "complaint_strategy",
                                                 "social_planning", "unclear"]},
        "deliverables": _arr(_obj({
            "id": _str(40),
            "kind": {"type": "string", "enum": ["graphic", "animation", "video", "text", "unclear"]},
            "quantity": _nint(1, 50),
            "formats": _arr({"type": "string", "enum": list(MIMES)}, 5),
            "specification": _nstr(500), "duration_seconds": _nint(1, 600),
            "width": _nint(1, 10000), "height": _nint(1, 10000),
            "required_text": _arr(_str(200), 5)}), 10)}),
    "missing_questions": _arr(_str(300), 3),
    "context_notes": _arr(_str(300), 5),
    "follow_ups": _arr(_str(300), 5)})
CLASSIFIER_SCHEMA = _obj({"class": {"type": "string", "enum": list(CLASSES)}, "evidence": _str(300)})
ANSWERER_SCHEMA = _obj({"answerable": {"type": "boolean"}, "answer": _nstr(2000),
                        "grounded_in": _arr({"type": "string", "enum": list(ANSWER_REFS)}, 4)})
SCHEMAS = {"interpreter": INTERPRETER_SCHEMA, "classifier": CLASSIFIER_SCHEMA, "answerer": ANSWERER_SCHEMA}


def validate(value, schema, code="invalid_model_output"):
    """Strict structural check of the subset of JSON Schema used above."""
    kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    if value is None:
        _need("null" in kinds, code)
        return
    kind = next((k for k in kinds if k != "null"), None)
    if kind == "object":
        _need(isinstance(value, dict) and set(value) == set(schema["required"]), code)
        for key, sub in schema["properties"].items():
            validate(value[key], sub, code)
    elif kind == "array":
        _need(isinstance(value, list) and len(value) <= schema["maxItems"], code)
        for item in value:
            validate(item, schema["items"], code)
    elif kind == "string":
        _need(isinstance(value, str) and value.strip() and len(value) <= schema.get("maxLength", 300)
              and not any(ord(c) < 32 and c not in "\n\t" for c in value), code)
        _need("enum" not in schema or value in schema["enum"], code)
    elif kind == "integer":
        _need(type(value) is int and schema["minimum"] <= value <= schema["maximum"], code)
    elif kind == "boolean":
        _need(type(value) is bool, code)
    else:
        raise ChainModelError("internal_schema_error")


# ---------------------------------------------------------------- one bounded POST
def build_request(role, request_input, *, profile=None):
    selected = base._selected_profile(profile)
    _need(role in SCHEMAS, "model_role_unknown")
    source = base._encoded(request_input)
    _need(len(source) <= base.MAX_INPUT_BYTES, "input_byte_limit")
    try:
        instructions = PROMPTS[role].read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ChainModelError("prompt_unavailable") from None
    payload = {"model": selected.model, "reasoning": {"effort": selected.reasoning_effort},
               "instructions": instructions, "input": [{"role": "user", "content": source.decode("utf-8")}],
               "store": False, "background": False, "service_tier": "default", "tools": [],
               "tool_choice": "none", "max_output_tokens": selected.max_output_tokens, "truncation": "disabled",
               "text": {"format": {"type": "json_schema", "name": "hamle_chain_" + role, "strict": True,
                                   "schema": copy.deepcopy(SCHEMAS[role])}}}
    _need(len(base._encoded(payload)) <= base.MAX_REQUEST_BYTES, "request_byte_limit")
    return payload


def extract(role, response, *, profile=None):
    """model.extract_response rules with the role's schema instead of the review schema."""
    selected = base._selected_profile(profile)
    def fail(code):
        raise ModelError(code, response=response, profile=selected)
    if not isinstance(response, dict):
        fail("invalid_response")
    if response.get("model") != selected.model:
        fail("model_mismatch")
    effort = response.get("reasoning")
    if effort is not None and (not isinstance(effort, dict) or effort.get("effort") != selected.reasoning_effort):
        fail("reasoning_mismatch")
    if response.get("status") != "completed":
        fail("response_incomplete" if response.get("status") == "incomplete" else "response_not_completed")
    if response.get("error") is not None or response.get("incomplete_details") is not None:
        fail("response_error")
    usage = base._usage(response)
    if usage is None:
        fail("usage_missing_or_invalid")
    if usage["output_tokens"] > selected.max_output_tokens or usage["input_tokens"] > base.MAX_REQUEST_BYTES + 1024:
        fail("usage_limit_exceeded")
    rid = response.get("id")
    if not isinstance(rid, str) or not re.fullmatch(r"resp_[A-Za-z0-9_\-]{1,200}", rid):
        fail("invalid_response_id")
    output = response.get("output")
    if not isinstance(output, list) or not output:
        fail("invalid_output")
    texts = []
    for item in output:
        if not isinstance(item, dict):
            fail("invalid_output")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message":
            fail("unexpected_tool_or_output")
        if item.get("role") != "assistant" or item.get("status") != "completed":
            fail("invalid_output_message")
        content = item.get("content")
        if not isinstance(content, list) or not content:
            fail("invalid_output_content")
        for part in content:
            if not isinstance(part, dict):
                fail("invalid_output_content")
            if part.get("type") == "refusal":
                fail("model_refusal")
            if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                fail("invalid_output_content")
            texts.append(part["text"])
    if len(texts) != 1 or len(texts[0].encode("utf-8")) > base.MAX_RESPONSE_BYTES:
        fail("ambiguous_or_oversize_output")
    try:
        value = base._loads(texts[0])
        validate(value, SCHEMAS[role])
    except (ModelError, ChainModelError, KeyError, TypeError):
        fail("invalid_analysis_schema")
    return {"output": value, "receipt": {"response_id": rid, "model": selected.model,
                                         "reasoning_effort": selected.reasoning_effort, "usage": usage}}


def call_once(api_key, role, request_input, *, transport=None, profile=None):
    """One POST, no retries, no redirects, proxies off; same contract as model.call_once."""
    selected = base._selected_profile(profile, for_live=transport is None)
    if (not isinstance(api_key, str) or not 20 <= len(api_key) <= 512
            or not api_key.isascii() or any(c.isspace() for c in api_key)):
        raise ModelError("credential_missing_or_invalid")
    body = base._encoded(build_request(role, request_input, profile=selected))
    session = response = None
    try:
        if transport is None:
            session = requests.Session()
            session.trust_env = False
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            transport = session.post
        response = transport(base.ENDPOINT, headers={"Authorization": "Bearer " + api_key,
                                                     "Content-Type": "application/json"},
                             data=body, timeout=base.TIMEOUT, allow_redirects=False, stream=True)
        if response.status_code != 200:
            raise ModelError({401: "authentication_rejected", 403: "access_rejected", 429: "quota_or_rate_limit",
                              400: "request_rejected"}.get(response.status_code, "http_error_no_retry"))
        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=16_384):
            size += len(chunk)
            if size > base.MAX_RESPONSE_BYTES:
                raise ModelError("response_byte_limit")
            chunks.append(chunk)
        return extract(role, base._loads(b"".join(chunks)), profile=selected)
    except ModelError:
        raise
    except Exception:
        raise ModelError("transport_outcome_unknown") from None
    finally:
        for closable in (response, session):
            if closable is not None:
                try:
                    closable.close()
                except Exception:
                    pass


# ---------------------------------------------------------------- one chance per digest
def source_digest(role, *parts):
    raw = json.dumps([role, *parts], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ModelGate:
    """Durable single model chance per (role, stable source digest).

    ledger: cloud_store.OperationsStore (budget_reserve/begin/unknown, model receipts).
    cache: v11_work_queue.QueueStore (chain records in the same Firestore backend).
    generation: current OperationsStore control generation (budget_begin fences it).
    """
    def __init__(self, *, ledger, cache, generation, reservation=MODEL_RESERVATION):
        self.ledger, self.cache, self.generation, self.reservation = ledger, cache, generation, reservation
        self.calls = 0

    def _get(self, key):
        return self.cache._run(lambda tx, _: tx.get(key))

    def _put(self, key, row):
        def op(tx, now):
            row["updated_at"] = now
            tx.put(key, row)
            return row
        return self.cache._run(op)

    def once(self, role, digest, call):
        key = "chain-model-" + digest
        row = self._get(key)
        if row is not None:
            _need(row.get("kind") == "chain_model_result" and row.get("role") == role, "model_cache_conflict")
            _need(row.get("state") == "completed", "model_outcome_unknown_no_retry")
            return copy.deepcopy(row["output"])
        reservation = "chain-model-" + digest[:48]
        self.ledger.budget_reserve(reservation, "openai", self.reservation)
        self.ledger.budget_begin(reservation, self.generation)  # non-replayable: second begin fails
        self._put(key, {"id": key, "kind": "chain_model_result", "role": role, "state": "started",
                        "reservation_key": reservation, "output": None})
        self.calls += 1
        try:
            result = call()
        except ModelError as exc:
            try:
                self.ledger.record_model_failure(reservation, exc.code, exc.failure_receipt)
            except Exception:
                pass
            try:
                self.ledger.budget_unknown(reservation)
            except Exception:
                pass
            raise ChainModelError("model_failed_no_retry:" + exc.code) from None
        except Exception:
            try:
                self.ledger.budget_unknown(reservation)
            except Exception:
                pass
            raise ChainModelError("model_outcome_unknown_no_retry") from None
        self.ledger.record_model_receipt(reservation, result["receipt"])
        self.ledger.budget_unknown(reservation)  # usage is not a bill: keep the hold
        self._put(key, {"id": key, "kind": "chain_model_result", "role": role, "state": "completed",
                        "reservation_key": reservation, "output": copy.deepcopy(result["output"]),
                        "response_id": result["receipt"]["response_id"]})
        return copy.deepcopy(result["output"])


# ---------------------------------------------------------------- roles
def _norm(value):
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(value.split())


def _grounded_literal(value, text):
    return isinstance(value, str) and value.strip() and _norm(value) in _norm(text)


_TR_ASCII = str.maketrans("şığüöçâîû", "siguocaiu")


def requirements_from_output(output, text):
    """Model draft -> plan_intake requirements. Missing/unsupported facts become absent.

    deadline and required_text must be literal substrings of the mail, otherwise
    they are dropped (plan_intake then asks the requester instead of guessing).
    """
    req = output["requirements"]
    out = {"risk_area": "routine" if req["risk_area"] == "routine" else req["risk_area"]}
    for key in ("title", "objective", "audience", "assets_ref"):
        if req[key] is not None:
            out[key] = req[key].strip()
    if req["deadline"] is not None and _grounded_literal(req["deadline"], text):
        out["deadline"] = req["deadline"].strip()
    items, seen = [], set()
    for row in req["deliverables"]:
        ident = re.sub(r"[^a-z0-9_-]+", "-", _norm(row["id"]).translate(_TR_ASCII))[:40].strip("-") or "cikti"
        while ident in seen:
            ident = (ident + "-x")[:40]
        seen.add(ident)
        item = {"id": ident}
        if row["kind"] != "unclear":
            item["kind"] = row["kind"]
        for key in ("quantity", "specification", "duration_seconds", "width", "height"):
            if row[key] is not None:
                item[key] = row[key]
        if row["formats"]:
            item["formats"] = list(dict.fromkeys(row["formats"]))
        literals = [t.strip() for t in row["required_text"] if _grounded_literal(t, text)]
        if literals:
            item["required_text"] = list(dict.fromkeys(literals))
        items.append(item)
    if items:
        out["deliverables"] = items
    return out


class ChainModels:
    """Bound role callables for ChainDeps. load_key() reads the existing OpenAI secret lazily."""
    def __init__(self, *, load_key, gate, transport=None, profile=None):
        self.load_key, self.gate, self.transport, self.profile = load_key, gate, transport, profile

    def _call(self, role, request_input, digest):
        base._selected_profile(self.profile)  # profile errors before any ledger write
        build_request(role, request_input, profile=self.profile)  # size/shape before the single chance
        def call():
            key = self.load_key()
            try:
                return call_once(key, role, request_input, transport=self.transport, profile=self.profile)
            finally:
                del key
        return self.gate.once(role, digest, call)

    def interpreter(self, text, context):
        _need(isinstance(text, str) and text.strip(), "mail_text_missing")
        subject = str(context.get("subject") or "")[:300]
        past = [{"title": str(p.get("title"))[:200], "status": str(p.get("status"))[:60]}
                for p in (context.get("past_works") or [])[:20] if isinstance(p, dict)]
        request_input = {"brand_id": str(context.get("brand_id")), "subject": subject,
                         "mail_text": text[:12000], "past_works": past}
        output = self._call("interpreter", request_input, source_digest("interpreter", context.get("brand_id"),
                                                                        subject, text))
        return {"requirements": requirements_from_output(output, subject + "\n" + text),
                "missing_questions": list(output["missing_questions"]),
                "context_notes": list(output["context_notes"]), "follow_ups": list(output["follow_ups"])}

    def classifier(self, text, context):
        flags = {k: context.get(k) for k in ("status", "producer", "manager")}
        request_input = {"comment_text": (text or "")[:6000], **flags}
        output = self._call("classifier", request_input, source_digest("classifier", text, flags))
        return output["class"]

    def answerer(self, text, context):
        brief, profile = context.get("brief") or {}, context.get("profile") or {}
        request_input = {"question": (text or "")[:4000], "brief": brief, "profile": profile}
        output = self._call("answerer", request_input, source_digest("answerer", text, brief, profile))
        if output["answerable"] is not True or output["answer"] is None or not output["grounded_in"]:
            return None
        values = {"brief": brief, "profile": profile}
        for ref in output["grounded_in"]:
            area, key = ref.split(".", 1)
            if values[area].get(key) in (None, "", [], {}):
                return None  # cites an empty field: not grounded
        return {"answer": output["answer"].strip(), "grounded_in": list(dict.fromkeys(output["grounded_in"]))}
