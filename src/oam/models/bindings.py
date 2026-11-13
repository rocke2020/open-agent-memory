from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from oam.config import ROLE_CAPABILITIES, ModelCapability, ModelsConfig


class ModelBindingResolutionError(ValueError):
    """A configured role cannot be resolved from the supplied environment."""


@dataclass(frozen=True, slots=True)
class ResolvedModelBinding:
    role: str
    alias: str
    protocol: str
    model: str
    endpoint: str
    capabilities: frozenset[ModelCapability]
    credential_configured: bool
    reasoning_effort: str | None = None
    reasoning_split: bool | None = None
    reasoning_mode: str = "legacy_chat"
    _api_key: str | None = field(default=None, repr=False, compare=False)

    @property
    def thinking(self) -> str | None:
        if self.reasoning_effort is None:
            return None
        return "disabled" if self.reasoning_effort == "none" else "enabled"

    def redacted(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "alias": self.alias,
            "protocol": self.protocol,
            "model": self.model,
            "endpoint": self.endpoint,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "credential_configured": self.credential_configured,
            "reasoning_effort": self.reasoning_effort,
            "reasoning_split": self.reasoning_split,
            "reasoning_mode": self.reasoning_mode,
            "thinking": self.thinking,
        }

    def chat_parameters(self) -> dict[str, Any]:
        if self.reasoning_effort not in {"none", "low", "high", "max"}:
            raise ModelBindingResolutionError(
                f"model role {self.role!r} requires explicit reasoning_effort"
            )
        params: dict[str, Any] = {}
        if self.reasoning_mode == "legacy_chat":
            params["thinking"] = {"type": self.thinking}
            if self.reasoning_effort != "none":
                params["reasoning_effort"] = self.reasoning_effort
        elif self.reasoning_mode == "effort":
            params["reasoning_effort"] = self.reasoning_effort
        if self.reasoning_split is not None:
            params["reasoning_split"] = self.reasoning_split
        return params


def resolve_model_binding(
    models: ModelsConfig,
    role: str,
    environment: Mapping[str, str],
) -> ResolvedModelBinding | None:
    if role not in ROLE_CAPABILITIES:
        raise ModelBindingResolutionError(f"unknown model role {role!r}")
    binding = models.binding_for(role)
    if binding is None:
        return None
    alias = role if role in {"embedding", "reranker", "decision"} else "vlm"
    model = getattr(binding, "model", None)
    if binding.model_env is not None:
        model = environment.get(binding.model_env, "").strip()
        if not model:
            if role == "decision":
                return None
            raise ModelBindingResolutionError(
                f"model role {role!r} requires environment variable {binding.model_env}"
            )
    if model is None:
        raise ModelBindingResolutionError(f"model role {role!r} has no model source")
    endpoint = environment.get(binding.endpoint_env, "").strip().rstrip("/")
    if not endpoint:
        raise ModelBindingResolutionError(
            f"model role {role!r} requires environment variable {binding.endpoint_env}"
        )
    try:
        url = urlsplit(endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError
    except ValueError:
        raise ModelBindingResolutionError(
            f"model role {role!r} requires {binding.endpoint_env} to be an HTTP(S) URL "
            "without userinfo, query, or fragment"
        ) from None
    api_key = None
    if binding.api_key_env is not None:
        api_key = environment.get(binding.api_key_env, "").strip()
        if not api_key:
            raise ModelBindingResolutionError(
                f"model role {role!r} requires environment variable {binding.api_key_env}"
            )
    return ResolvedModelBinding(
        role=role,
        alias=alias,
        protocol=binding.protocol,
        reasoning_split=getattr(binding, "reasoning_split", None),
        reasoning_mode=getattr(binding, "reasoning_mode", "legacy_chat"),
        model=model,
        endpoint=endpoint,
        capabilities=binding.capabilities,
        credential_configured=api_key is not None,
        reasoning_effort=(
            None
            if role in {"embedding", "reranker", "decision"}
            else getattr(models.reasoning_effort, role)
        ),
        _api_key=api_key,
    )
