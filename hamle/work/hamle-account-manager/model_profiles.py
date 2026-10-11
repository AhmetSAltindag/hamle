"""Immutable, allowlisted model selections; no credentials or network access.

The candidate profile exists for offline preparation only. Selecting it here
does not authorize a paid call, data transfer, routing or business action.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


class ProfileError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ModelProfile:
    profile_id: str
    provider: str
    model: str
    reasoning_effort: str
    max_output_tokens: int
    live_enabled: bool
    evaluation_only: bool = False


ASTRA_MAX = ModelProfile("astra-max-v1", "openai", "gpt-6-astra", "max", 16_384, True)
SOL_MEDIUM_CANDIDATE = ModelProfile(
    "sol-medium-candidate-v1", "openai", "gpt-6.1-sol", "medium", 8_192, False)
# An explicit, bounded customer-free comparison was authorized separately.
# The caller still owns its durable reservation, exact attempt and paid-call
# authorization; this profile grants no dispatch or ongoing routing authority.
SOL_MEDIUM_EVALUATION = ModelProfile(
    "sol-medium-evaluation-v1", "openai", "gpt-6.1-sol", "medium", 8_192, True, True)
DEFAULT_PROFILE = ASTRA_MAX
PROFILES = MappingProxyType({p.profile_id: p for p in
                            (ASTRA_MAX, SOL_MEDIUM_CANDIDATE, SOL_MEDIUM_EVALUATION)})


def resolve_profile(profile=None, *, for_live=False):
    """Resolve an exact profile. An altered dataclass cannot enable a candidate.

    Pure schema building/parsing may inspect the disabled candidate; the actual
    adapter must pass for_live=True before allocating a network session.
    """
    if profile is None:
        selected = DEFAULT_PROFILE
    elif type(profile) is str:
        selected = PROFILES.get(profile)
    elif type(profile) is ModelProfile:
        selected = PROFILES.get(profile.profile_id)
        if selected != profile:
            raise ProfileError("model_profile_mismatch")
    else:
        raise ProfileError("model_profile_invalid")
    if selected is None:
        raise ProfileError("model_profile_unknown")
    if selected.provider != "openai":
        raise ProfileError("model_provider_unsupported")
    if for_live and not selected.live_enabled:
        raise ProfileError("model_profile_live_disabled")
    return selected
