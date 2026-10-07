from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import httpx

from oam.config import REQUIRED_MODEL_ROLES, ROLE_CAPABILITIES, ModelCapability, ModelsConfig
from oam.models.bindings import (
    ModelBindingResolutionError,
    ResolvedModelBinding,
    resolve_model_binding,
)

MIN_GENERATIVE_PROBE_OUTPUT_TOKENS = 1_024
_EMBEDDING_PROBE_TEXTS = (
    "synthetic report contains a sodium measurement",
    "synthetic conversation records a hiking preference",
)
_RERANK_PROBE_DOCUMENTS = (
    "The synthetic report records sodium at 140 mmol/L.",
    "The synthetic user prefers weekend hiking.",
)


@dataclass(frozen=True, slots=True)
class ChatProbeOutput:
    content: str | None
    finish_reason: str | None
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingProbeOutput:
    vectors: tuple[tuple[float, ...], ...]


@dataclass(frozen=True, slots=True)
class VisionProbeOutput:
    content: str | None
    finish_reason: str | None
    reasoning_content: str | None = None


@dataclass(frozen=True, slots=True)
class RerankProbeOutput:
    scores: tuple[float, ...]


class CapabilityProbeClient(Protocol):
    def probe_chat(
        self, binding: ResolvedModelBinding, *, max_output_tokens: int
    ) -> ChatProbeOutput: ...

    def probe_embeddings(
        self, binding: ResolvedModelBinding, texts: tuple[str, ...]
    ) -> EmbeddingProbeOutput: ...

    def probe_vision(
        self, binding: ResolvedModelBinding, *, max_output_tokens: int
    ) -> VisionProbeOutput: ...

    def probe_rerank(
        self, binding: ResolvedModelBinding, documents: tuple[str, ...]
    ) -> RerankProbeOutput: ...


class PreflightStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class RolePreflightResult:
    role: str
    status: PreflightStatus
    capability: ModelCapability
    model: str | None
    endpoint: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class ModelPreflightReport:
    runnable: bool
    roles: tuple[RolePreflightResult, ...]


def run_model_preflight(
    models: ModelsConfig,
    environment: Mapping[str, str],
    client: CapabilityProbeClient,
    *,
    required_roles: frozenset[str] | None = None,
) -> ModelPreflightReport:
    results: list[RolePreflightResult] = []
    required = REQUIRED_MODEL_ROLES if required_roles is None else required_roles
    for role, capabilities in ROLE_CAPABILITIES.items():
        capability = capabilities[0]
        binding: ResolvedModelBinding | None = None
        try:
            binding = resolve_model_binding(models, role, environment)
            if binding is None:
                results.append(
                    RolePreflightResult(
                        role=role,
                        status=PreflightStatus.DISABLED,
                        capability=capability,
                        model=None,
                        endpoint=None,
                        detail="role is not configured",
                    )
                )
                continue
            status = PreflightStatus.PASSED
            details = []
            for required_capability in capabilities:
                try:
                    detail = _probe(binding, required_capability, client)
                except httpx.HTTPError as exc:
                    detail = f"model request failed: {type(exc).__name__}"
                    status = PreflightStatus.FAILED
                except (ValueError, RuntimeError) as exc:
                    detail = str(exc)
                    status = PreflightStatus.FAILED
                details.append(f"{required_capability.value}: {detail}")
            detail = "; ".join(details)
        except (ModelBindingResolutionError, ValueError, RuntimeError) as exc:
            detail = str(exc)
            status = PreflightStatus.FAILED
        results.append(
            RolePreflightResult(
                role=role,
                status=status,
                capability=capability,
                model=binding.model if binding is not None else None,
                endpoint=binding.endpoint if binding is not None else None,
                detail=detail,
            )
        )

    by_role = {result.role: result for result in results}
    runnable = all(by_role[role].status is PreflightStatus.PASSED for role in required)
    return ModelPreflightReport(runnable=runnable, roles=tuple(results))


def _probe(
    binding: ResolvedModelBinding,
    capability: ModelCapability,
    client: CapabilityProbeClient,
) -> str:
    if capability is ModelCapability.TEXT:
        response = client.probe_chat(
            binding,
            max_output_tokens=MIN_GENERATIVE_PROBE_OUTPUT_TOKENS,
        )
        return _validate_chat(response)
    if capability is ModelCapability.EMBEDDINGS:
        response = client.probe_embeddings(binding, _EMBEDDING_PROBE_TEXTS)
        return _validate_embeddings(response, expected_count=len(_EMBEDDING_PROBE_TEXTS))
    if capability is ModelCapability.VISION:
        response = client.probe_vision(
            binding,
            max_output_tokens=MIN_GENERATIVE_PROBE_OUTPUT_TOKENS,
        )
        return _validate_vision(response)
    response = client.probe_rerank(binding, _RERANK_PROBE_DOCUMENTS)
    return _validate_rerank(response, expected_count=len(_RERANK_PROBE_DOCUMENTS))


def _validate_chat(response: ChatProbeOutput) -> str:
    if response.finish_reason == "length":
        raise ValueError("chat probe failed: length termination")
    if response.finish_reason == "content_filter":
        raise ValueError("chat probe failed: content_filter termination")
    content = (response.content or "").strip()
    if not content:
        if (response.reasoning_content or "").strip():
            raise ValueError("chat probe failed: reasoning-only output")
        raise ValueError("chat probe failed: empty visible content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("chat probe failed: malformed JSON visible content") from exc
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise ValueError("chat probe failed: malformed JSON capability acknowledgement")
    return "visible structured output validated"


def _validate_embeddings(response: EmbeddingProbeOutput, *, expected_count: int) -> str:
    vectors = response.vectors
    if not vectors:
        raise ValueError("embedding probe failed: no vectors")
    if len(vectors) != expected_count:
        raise ValueError("embedding probe failed: wrong vector count")
    dimensions = {len(vector) for vector in vectors}
    if 0 in dimensions:
        raise ValueError("embedding probe failed: empty vector")
    if len(dimensions) != 1:
        raise ValueError("embedding probe failed: inconsistent dimensions")
    if any(not math.isfinite(value) for vector in vectors for value in vector):
        raise ValueError("embedding probe failed: non-finite value")
    dimension = dimensions.pop()
    return f"{len(vectors)} finite vectors with dimension {dimension} validated"


def _validate_vision(response: VisionProbeOutput) -> str:
    if response.finish_reason == "length":
        raise ValueError("vision probe failed: length termination")
    if response.finish_reason == "content_filter":
        raise ValueError("vision probe failed: content_filter termination")
    content = (response.content or "").strip()
    if not content:
        if (response.reasoning_content or "").strip():
            raise ValueError("vision probe failed: reasoning-only output")
        raise ValueError("vision probe failed: empty visible content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("vision probe failed: malformed JSON visible content") from exc
    if parsed != {"left": "red", "right": "blue"}:
        raise ValueError("vision probe failed: image colors were not correctly identified")
    return "visible image colors correctly identified"


def _validate_rerank(response: RerankProbeOutput, *, expected_count: int) -> str:
    if len(response.scores) != expected_count:
        raise ValueError("rerank probe failed: wrong score count")
    if any(not math.isfinite(score) for score in response.scores):
        raise ValueError("rerank probe failed: non-finite score")
    if len(set(response.scores)) < 2:
        raise ValueError("rerank probe failed: no meaningful ordering")
    return "finite ordered scores validated"
