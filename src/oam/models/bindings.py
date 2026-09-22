from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from oam.config import ModelCapability, ModelsConfig


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
    _api_key: str | None = field(default=None, repr=False, compare=False)

    def redacted(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "alias": self.alias,
            "protocol": self.protocol,
            "model": self.model,
            "endpoint": self.endpoint,
            "capabilities": sorted(capability.value for capability in self.capabilities),
            "credential_configured": self.credential_configured,
        }


def resolve_model_binding(
    models: ModelsConfig,
    role: str,
    environment: Mapping[str, str],
) -> ResolvedModelBinding | None:
    if role not in type(models.roles).model_fields:
        raise ModelBindingResolutionError(f"unknown model role {role!r}")
    alias = getattr(models.roles, role)
    if alias is None:
        return None
    binding = models.bindings[alias]
    model = binding.model
    if binding.model_env is not None:
        model = environment.get(binding.model_env, "").strip()
        if not model:
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
        model=model,
        endpoint=endpoint,
        capabilities=binding.capabilities,
        credential_configured=api_key is not None,
        _api_key=api_key,
    )
