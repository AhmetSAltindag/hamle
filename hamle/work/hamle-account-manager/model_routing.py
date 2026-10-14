"""Versioned role -> {provider, model, effort} routing; no credentials or network access.

The routing file (outputs/gamechangers-internal/model-routing.json, shipped as
chain-profile/model-routing.json) decides which provider serves each paid model role.
Default provider is "anthropic". "openai" stays selectable per role (existing
model_profiles allowlist) but is never chosen automatically: there is no
cross-provider fallback. An invalid or missing file is an error, never a default.
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path

from model_profiles import PROFILES

NAME = "model-routing.json"
SCHEMA_VERSION = 1
ROLES = ("chain_interpreter", "chain_answerer", "customer_text", "chain_classifier", "digest_classifier",
         "trend_radar", "monday_special_days", "new_business_research", "daily_spark", "weekly_karne")
WEB_SEARCH_ROLES = ("trend_radar", "monday_special_days", "new_business_research", "daily_spark", "weekly_karne")
PROVIDERS = ("anthropic", "openai")
ANTHROPIC_MODELS = ("claude-opus-5-5", "claude-haiku-5-5")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
WEB_SEARCH_TOOL_TYPES = ("web_search_20260209", "web_search_20250305")
FLAGS = ("monday_special_days_research_enabled", "new_business_research_enabled", "daily_spark_research_enabled")
# Cost of one web search on the Claude API: 10 USD per 1000 searches.
WEB_SEARCH_FEE_MICRODOLLARS = 10_000


class RoutingError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _need(ok, code="model_routing_invalid"):
    if not ok:
        raise RoutingError(code)


@dataclass(frozen=True, slots=True)
class Route:
    role: str
    provider: str
    model: str
    effort: str | None = None          # anthropic: output_config.effort (always sent explicitly)
    max_tokens: int | None = None      # anthropic only
    timeout_seconds: int | None = None  # anthropic only
    profile_id: str | None = None      # openai only (model_profiles allowlist)
    web_search: dict | None = None     # anthropic research roles only: {"type", "max_uses", "fee_microdollars"}

    def reservation_extra(self):
        """Hold added on top of the token hold for the worst-case number of searches."""
        return self.web_search["max_uses"] * self.web_search["fee_microdollars"] if self.web_search else 0


@dataclass(frozen=True)
class Routing:
    config_id: str
    default_provider: str
    routes: dict
    flags: dict

    def route(self, role):
        _need(role in self.routes, "model_role_unknown")
        return self.routes[role]

    def flag(self, name):
        _need(name in FLAGS, "model_routing_flag_unknown")
        return self.flags[name] is True


def _route(role, raw, web):
    _need(isinstance(raw, dict))
    provider = raw.get("provider")
    _need(provider in PROVIDERS, "model_provider_unsupported")
    if provider == "openai":
        _need(set(raw) == {"provider", "profile"}, "model_routing_invalid")
        profile = PROFILES.get(raw["profile"]) if isinstance(raw["profile"], str) else None
        _need(profile is not None and profile.live_enabled and not profile.evaluation_only,
              "model_profile_unknown")
        _need(role not in WEB_SEARCH_ROLES or role == "trend_radar", "model_provider_unsupported")
        return Route(role, "openai", profile.model, profile.reasoning_effort, profile.max_output_tokens,
                     None, profile.profile_id)
    allowed = {"provider", "model", "effort", "max_tokens", "timeout_seconds", "web_search"}
    _need(set(raw) <= allowed and {"provider", "model", "effort", "max_tokens", "timeout_seconds"} <= set(raw))
    _need(raw["model"] in ANTHROPIC_MODELS, "model_unknown")
    _need(raw["effort"] in EFFORTS, "model_effort_invalid")
    # Non-streaming requests: keep max_tokens <= 16000 (SDK HTTP timeout guidance).
    _need(type(raw["max_tokens"]) is int and 1024 <= raw["max_tokens"] <= 16_000, "model_max_tokens_invalid")
    # Web-search roles stream (wall-clock bound, up to 900 s); others stay non-streaming (<= 540 s).
    _need(type(raw["timeout_seconds"]) is int and 30 <= raw["timeout_seconds"]
          <= (900 if raw.get("web_search") is True else 540), "model_timeout_invalid")
    search = None
    if role in WEB_SEARCH_ROLES:
        _need(raw.get("web_search") is True, "web_search_required")
        search = dict(web)
    else:
        _need("web_search" not in raw, "web_search_not_allowed")
    return Route(role, "anthropic", raw["model"], raw["effort"], raw["max_tokens"], raw["timeout_seconds"],
                 None, search)


def validate(doc):
    _need(isinstance(doc, dict) and doc.get("schema_version") == SCHEMA_VERSION, "model_routing_version_invalid")
    _need(isinstance(doc.get("config_id"), str) and 0 < len(doc["config_id"]) <= 80)
    _need(doc.get("default_provider") == "anthropic", "model_routing_default_provider_invalid")
    ws = doc.get("web_search")
    _need(isinstance(ws, dict) and set(ws) == {"tool_type", "max_uses", "fee_microdollars_per_search"},
          "web_search_config_invalid")
    _need(ws["tool_type"] in WEB_SEARCH_TOOL_TYPES and type(ws["max_uses"]) is int and 1 <= ws["max_uses"] <= 10
          and ws["fee_microdollars_per_search"] == WEB_SEARCH_FEE_MICRODOLLARS, "web_search_config_invalid")
    web = {"type": ws["tool_type"], "max_uses": ws["max_uses"], "fee_microdollars": ws["fee_microdollars_per_search"]}
    flags = doc.get("flags")
    _need(isinstance(flags, dict) and set(flags) == set(FLAGS) and all(type(v) is bool for v in flags.values()),
          "model_routing_flags_invalid")
    roles = doc.get("roles")
    _need(isinstance(roles, dict) and set(roles) == set(ROLES), "model_routing_roles_invalid")
    routes = {role: _route(role, roles[role], web) for role in ROLES}
    return Routing(doc["config_id"], "anthropic", routes, dict(flags))


def load(path=None):
    """chain-profile/ first (image), then the repository output folder. No fallback config."""
    root = Path(__file__).resolve().parents[2]
    candidates = [Path(path)] if path else [Path(__file__).with_name("chain-profile") / NAME,
                                            root / "outputs/gamechangers-internal" / NAME]
    for candidate in candidates:
        if candidate.is_file():
            try:
                doc = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                raise RoutingError("model_routing_unreadable") from None
            return validate(doc)
    raise RoutingError("model_routing_missing")


def openai_only(base=None):
    """Explicit operator/test selection of the standby OpenAI path for every OpenAI-capable role."""
    doc = copy.deepcopy(base) if base is not None else {
        "schema_version": SCHEMA_VERSION, "config_id": "openai-standby", "default_provider": "anthropic",
        "web_search": {"tool_type": "web_search_20260209", "max_uses": 5,
                       "fee_microdollars_per_search": WEB_SEARCH_FEE_MICRODOLLARS},
        "flags": {f: False for f in FLAGS}, "roles": {}}
    for role in ROLES:
        if role in WEB_SEARCH_ROLES and role != "trend_radar":
            doc["roles"].setdefault(role, {"provider": "anthropic", "model": "claude-opus-5-5", "effort": "high",
                                           "max_tokens": 16000, "timeout_seconds": 420, "web_search": True})
        else:
            doc["roles"][role] = {"provider": "openai", "profile": "astra-max-v1"}
    return validate(doc)
