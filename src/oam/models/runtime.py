"""Bounded model requests for indexing and non-generative recall scoring."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from oam.config import OAMConfig
from oam.models.bindings import ResolvedModelBinding, resolve_model_binding

MIN_GENERATION_OUTPUT_TOKENS = 1_024


class ModelOutputError(ValueError):
    """A model response cannot be consumed as a valid runtime result."""

    def __init__(self, message: str, *, usage: Usage | None = None):
        super().__init__(message)
        self.usage = usage


class FrozenResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Usage(FrozenResult):
    """Reported token totals; reasoning is a subset of completion, never an addend."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None
    coverage: Literal["complete", "partial", "unavailable"] = "unavailable"


class ModelResult(FrozenResult):
    content: str
    usage: Usage


class EmbeddingResult(FrozenResult):
    vectors: tuple[tuple[float, ...], ...]
    usage: Usage


class RerankResult(FrozenResult):
    scores: tuple[float, ...]
    usage: Usage


class RuntimeModels:
    def __init__(
        self, config: OAMConfig, environment: Mapping[str, str], http: httpx.Client
    ) -> None:
        self.config = config
        self._environment = dict(environment)
        self._http = http

    def generate(
        self,
        role: str,
        *,
        system: str,
        user: str,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ModelResult:
        if type(max_output_tokens) is not int or max_output_tokens < MIN_GENERATION_OUTPUT_TOKENS:
            raise ValueError("generation requires at least 1024 output tokens")
        if not system.strip() or not user.strip():
            raise ValueError("generation requires nonempty system and user content")
        binding = self._binding(role, "chat")
        body = self._post(
            binding,
            "/chat/completions",
            {
                "model": binding.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_output_tokens,
                "response_format": {"type": "json_object"},
            },
            timeout_seconds,
        )
        usage = _usage(body.get("usage"))
        try:
            choices = body["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            message = choice["message"]
            if not isinstance(message, dict) or choice.get("finish_reason") != "stop":
                raise ValueError
            content = message.get("content")
            if message.get("refusal") or not isinstance(content, str) or not content.strip():
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelOutputError(
                "generation requires nonempty, unrefused stop-only content", usage=usage
            ) from exc
        return ModelResult(content=content, usage=usage)

    def embed(self, texts: tuple[str, ...], *, timeout_seconds: float) -> EmbeddingResult:
        _require_texts(texts)
        binding = self._binding("embedding", "embeddings")
        body = self._post(
            binding,
            "/embeddings",
            {"model": binding.model, "input": list(texts)},
            timeout_seconds,
        )
        usage = _usage(body.get("usage"), generates=False)
        try:
            ordered = _indexed(body.get("data"), len(texts))
            vectors = []
            for item in ordered:
                raw = item.get("embedding")
                if not isinstance(raw, list) or len(raw) != self.config.index.embedding_dimensions:
                    raise ModelOutputError("embedding dimension differs from configured dimension")
                vector = tuple(_finite_number(value) for value in raw)
                if not any(vector):
                    raise ModelOutputError("zero embedding cannot support cosine retrieval")
                vectors.append(vector)
        except ModelOutputError as exc:
            raise ModelOutputError(str(exc), usage=usage) from exc
        return EmbeddingResult(vectors=tuple(vectors), usage=usage)

    def rerank(
        self,
        query: str,
        documents: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> RerankResult:
        _require_texts(documents)
        if not query.strip():
            raise ValueError("reranking query cannot be empty")
        binding = self._binding("reranker", "rerank")
        body = self._post(
            binding,
            "/rerank",
            {"model": binding.model, "query": query, "documents": list(documents)},
            timeout_seconds,
        )
        usage = _usage(body.get("usage"), generates=False)
        try:
            ordered = _indexed(body.get("results"), len(documents))
            scores = tuple(_finite_number(item.get("relevance_score")) for item in ordered)
        except ModelOutputError as exc:
            raise ModelOutputError(str(exc), usage=usage) from exc
        return RerankResult(scores=scores, usage=usage)

    def _binding(self, role: str, capability: str) -> ResolvedModelBinding:
        binding = resolve_model_binding(self.config.models, role, self._environment)
        if binding is None:
            raise ValueError(f"model role {role!r} is not configured")
        if capability not in binding.capabilities:
            raise ValueError(f"model role {role!r} does not provide {capability}")
        return binding

    def _post(
        self,
        binding: ResolvedModelBinding,
        path: str,
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("model timeout must be positive and finite")
        headers = {"content-type": "application/json"}
        if binding._api_key is not None:
            headers["authorization"] = f"Bearer {binding._api_key}"
        if path == "/chat/completions" and binding.reasoning_effort is not None:
            payload = {**payload, "reasoning_effort": binding.reasoning_effort}
        response = self._http.post(
            f"{binding.endpoint}{path}",
            headers=headers,
            json=payload,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelOutputError("model endpoint returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ModelOutputError("model endpoint returned a non-object response")
        return body


def _require_texts(texts: tuple[str, ...]) -> None:
    if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("model input requires nonempty text items")


def _finite_number(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ModelOutputError("model scores and vectors must contain finite numbers")
    return float(value)


def _indexed(raw: object, count: int) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list) or len(raw) != count:
        raise ModelOutputError("model response has the wrong item count")
    indexed: dict[int, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ModelOutputError("model response items must be objects")
        index = item.get("index")
        if type(index) is not int or not 0 <= index < count or index in indexed:
            raise ModelOutputError("model response requires unique in-range integer indices")
        indexed[index] = item
    return tuple(indexed[index] for index in range(count))


def _usage(raw: object, *, generates: bool = True) -> Usage:
    if raw is None or raw == {}:
        return Usage()
    if not isinstance(raw, dict):
        raise ModelOutputError("usage must be an object")

    def count(value: object) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise ModelOutputError("usage counts must be nonnegative integers")
        return value

    input_tokens = count(raw.get("prompt_tokens", raw.get("input_tokens")))
    output_tokens = count(raw.get("completion_tokens", raw.get("output_tokens")))
    if not generates and output_tokens is None:
        output_tokens = 0
    details = raw.get("completion_tokens_details")
    if details is None:
        details = {}
    if not isinstance(details, dict):
        raise ModelOutputError("completion token details must be an object")
    reasoning_tokens = count(details.get("reasoning_tokens", raw.get("reasoning_tokens")))
    total_tokens = count(raw.get("total_tokens"))
    if input_tokens is not None and output_tokens is not None:
        combined = input_tokens + output_tokens
        if total_tokens is not None and total_tokens != combined:
            raise ModelOutputError("usage total conflicts with input plus output")
        total_tokens = combined
    if (
        reasoning_tokens is not None
        and output_tokens is not None
        and reasoning_tokens > output_tokens
    ):
        raise ModelOutputError("reasoning tokens exceed completion tokens")
    available = (input_tokens, output_tokens, total_tokens)
    coverage = "complete" if all(value is not None for value in available) else "partial"
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=reasoning_tokens,
        coverage=coverage,
    )
