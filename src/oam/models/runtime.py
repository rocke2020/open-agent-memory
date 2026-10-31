"""Bounded model requests for indexing and non-generative recall scoring."""

from __future__ import annotations

import base64
import logging
import math
import time
from collections.abc import Mapping
from contextlib import suppress
from threading import BoundedSemaphore, Event
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict

from oam.capacity import CapacityUnavailable, PostgresCapacity
from oam.config import OAMConfig
from oam.models.bindings import ResolvedModelBinding, resolve_model_binding
from oam.models.protocols import generation_request, normalize_generation

logger = logging.getLogger(__name__)

MIN_GENERATION_OUTPUT_TOKENS = 1_024


def retryable_model_http_error(error: BaseException) -> bool:
    return isinstance(error, httpx.TransportError) or (
        isinstance(error, httpx.HTTPStatusError)
        and (error.response.status_code == 429 or 500 <= error.response.status_code < 600)
    )


class ModelOutputError(ValueError):
    """A model response cannot be consumed as a valid runtime result."""

    def __init__(self, message: str, *, usage: Usage | None = None):
        super().__init__(message)
        self.usage = usage


class ModelOutputTooLongError(ModelOutputError):
    """The provider terminated generation at its completion-token limit."""


class ModelEmptyContentError(ModelOutputError):
    """Generation stopped without visible content, a refusal or another output."""


class ModelDispatchUnavailable(httpx.ConnectTimeout):
    """Local model capacity expired before an HTTP request was sent."""


class FrozenResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Usage(FrozenResult):
    """Reported token totals; reasoning is a subset of completion, never an addend."""

    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    uncached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
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
        self,
        config: OAMConfig,
        environment: Mapping[str, str],
        http: httpx.Client,
        *,
        background: bool = False,
        admission: PostgresCapacity | None = None,
        stop_event: Event | None = None,
    ) -> None:
        self.config = config
        self._environment = dict(environment)
        self._http = http
        self._background = background
        self._admission = admission
        self._stop_event = stop_event
        self._llm_slots = BoundedSemaphore(config.provider.llm_concurrency)
        self._embedding_slots = BoundedSemaphore(
            config.provider.embedding_concurrency
            - (config.provider.embedding_search_reserved if background else 0)
        )
        self._rerank_slots = BoundedSemaphore(config.provider.rerank_concurrency)

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
        binding = self._binding(role, "text")
        path, headers, payload = generation_request(binding, system, user, max_output_tokens)
        call_id = uuid4().hex
        body = self._post(binding, path, payload, timeout_seconds, headers=headers, call_id=call_id)
        return self._generation_result(binding, body, call_id)

    def generate_vision(
        self,
        image: bytes,
        *,
        media_type: str,
        system: str,
        user: str,
        max_output_tokens: int,
        timeout_seconds: float,
    ) -> ModelResult:
        if type(max_output_tokens) is not int or max_output_tokens < MIN_GENERATION_OUTPUT_TOKENS:
            raise ValueError("generation requires at least 1024 output tokens")
        if not image or media_type not in {"image/png", "image/jpeg"}:
            raise ValueError("vision requires nonempty PNG or JPEG bytes")
        if not system.strip() or not user.strip():
            raise ValueError("vision requires nonempty system and user content")
        binding = self._binding("extraction", "vision")
        encoded = base64.b64encode(image).decode("ascii")
        image_url = f"data:{media_type};base64,{encoded}"
        path, headers, payload = generation_request(
            binding, system, user, max_output_tokens, image_url
        )
        call_id = uuid4().hex
        body = self._post(binding, path, payload, timeout_seconds, headers=headers, call_id=call_id)
        return self._generation_result(binding, body, call_id)

    def _generation_result(
        self, binding: ResolvedModelBinding, body: dict[str, Any], call_id: str
    ) -> ModelResult:
        normalized = normalize_generation(binding, body)
        choices = normalized.get("choices")
        choice = (
            choices[0]
            if isinstance(choices, list) and choices and isinstance(choices[0], dict)
            else {}
        )
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        reasoning = message.get("reasoning_content")
        reasoning_chars = (
            len(reasoning)
            if binding.protocol == "openai_chat" and isinstance(reasoning, str)
            else None
        )
        try:
            result = _generation_result(normalized)
        except ModelOutputError as exc:
            logger.info(
                "model_result protocol=%s role=%s outcome=%s finish_reason=%s "
                "completion_tokens=%s reasoning_chars=%s input_tokens=%s cached_input_tokens=%s "
                "uncached_input_tokens=%s cache_creation_input_tokens=%s model=%s call_id=%s "
                "reasoning_tokens=%s total_tokens=%s",
                binding.protocol,
                binding.role,
                type(exc).__name__,
                choice.get("finish_reason"),
                exc.usage.output_tokens if exc.usage else None,
                reasoning_chars,
                exc.usage.input_tokens if exc.usage else None,
                exc.usage.cached_input_tokens if exc.usage else None,
                exc.usage.uncached_input_tokens if exc.usage else None,
                exc.usage.cache_creation_input_tokens if exc.usage else None,
                binding.model,
                call_id,
                exc.usage.reasoning_tokens if exc.usage else None,
                exc.usage.total_tokens if exc.usage else None,
            )
            raise
        logger.info(
            "model_result protocol=%s role=%s outcome=success finish_reason=%s "
            "completion_tokens=%s reasoning_chars=%s input_tokens=%s cached_input_tokens=%s "
            "uncached_input_tokens=%s cache_creation_input_tokens=%s model=%s call_id=%s "
            "reasoning_tokens=%s total_tokens=%s",
            binding.protocol,
            binding.role,
            choice.get("finish_reason"),
            result.usage.output_tokens,
            reasoning_chars,
            result.usage.input_tokens,
            result.usage.cached_input_tokens,
            result.usage.uncached_input_tokens,
            result.usage.cache_creation_input_tokens,
            binding.model,
            call_id,
            result.usage.reasoning_tokens,
            result.usage.total_tokens,
        )
        return result

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
        *,
        headers: dict[str, str] | None = None,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("model timeout must be positive and finite")
        if headers is None:
            headers = {"content-type": "application/json"}
            if binding._api_key is not None:
                headers["authorization"] = f"Bearer {binding._api_key}"
        deadline = time.monotonic() + timeout_seconds
        kind = "embedding" if path == "/embeddings" else "llm"
        slots = (
            self._rerank_slots
            if path == "/rerank"
            else self._embedding_slots
            if kind == "embedding"
            else self._llm_slots
        )
        acquired = False
        permit = None
        sent = False

        def remaining_time():
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (self._stop_event is not None and self._stop_event.is_set()):
                raise ModelDispatchUnavailable("model dispatch deadline expired or worker stopped")
            return remaining

        try:
            if not slots.acquire(timeout=remaining_time()):
                raise ModelDispatchUnavailable("model capacity wait expired before dispatch")
            acquired = True
            if self._admission is not None and path != "/rerank":
                permit = self._admission.acquire_model(
                    kind, background=self._background, timeout=remaining_time()
                )
                permit.dispatched()
                headers["X-Request-ID"] = str(permit.permit_id)
            remaining = remaining_time()
            started = time.monotonic()
            try:
                sent = True
                response = self._http.post(
                    f"{binding.endpoint}{path}", headers=headers, json=payload, timeout=remaining
                )
            except httpx.TransportError as exc:
                logger.info(
                    "model_dispatch protocol=%s role=%s outcome=%s duration_ms=%.0f call_id=%s "
                    "local_call_finished=true response_usage=unknown",
                    binding.protocol,
                    binding.role,
                    type(exc).__name__,
                    (time.monotonic() - started) * 1000,
                    call_id,
                )
                if permit is not None:
                    permit.complete()
                raise
            except BaseException as exc:
                logger.info(
                    "model_dispatch protocol=%s role=%s outcome=%s duration_ms=%.0f call_id=%s",
                    binding.protocol,
                    binding.role,
                    type(exc).__name__,
                    (time.monotonic() - started) * 1000,
                    call_id,
                )
                if permit is not None:
                    permit.uncertain()
                raise
            else:
                logger.info(
                    "model_dispatch protocol=%s role=%s status=%s duration_ms=%.0f call_id=%s",
                    binding.protocol,
                    binding.role,
                    response.status_code,
                    (time.monotonic() - started) * 1000,
                    call_id,
                )
                if permit is not None:
                    permit.complete()
        except CapacityUnavailable as exc:
            if not sent:
                raise ModelDispatchUnavailable(str(exc)) from exc
            raise
        finally:
            try:
                if permit is not None and not sent:
                    with suppress(CapacityUnavailable):
                        permit.complete()
            finally:
                if acquired:
                    slots.release()
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelOutputError("model endpoint returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise ModelOutputError("model endpoint returned a non-object response")
        return body


def _generation_result(body: dict[str, Any]) -> ModelResult:
    usage = _usage(body.get("usage"))
    try:
        choices = body["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError
        choice = choices[0]
        message = choice["message"]
        if choice.get("finish_reason") == "length":
            raise ModelOutputTooLongError(
                "generation reached its completion-token limit", usage=usage
            )
        if not isinstance(message, dict) or choice.get("finish_reason") != "stop":
            raise ValueError
        content = message["content"]
        if message.get("refusal"):
            raise ValueError
        if content is None or (isinstance(content, str) and not content.strip()):
            if message.get("reasoning_content") or message.get("tool_calls"):
                raise ValueError
            raise ModelEmptyContentError("generation stopped with empty content", usage=usage)
        if not isinstance(content, str):
            raise ValueError
    except (ModelOutputTooLongError, ModelEmptyContentError):
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelOutputError(
            "generation requires nonempty, unrefused stop-only content", usage=usage
        ) from exc
    return ModelResult(content=content, usage=usage)


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
    input_details = raw.get("prompt_tokens_details", raw.get("input_tokens_details"))
    if input_details is not None and not isinstance(input_details, dict):
        raise ModelOutputError("input token details must be an object")
    cached = count(raw.get("cached_input_tokens"))
    uncached = count(raw.get("uncached_input_tokens"))
    cache_creation = count(raw.get("cache_creation_input_tokens"))
    if cached is None and isinstance(input_details, dict):
        cached = count(input_details.get("cached_tokens"))
        if cached is not None and input_tokens is not None:
            if cached > input_tokens:
                raise ModelOutputError("cached tokens exceed input tokens")
            uncached = input_tokens - cached
    if input_tokens is not None and cached is not None and cached > input_tokens:
        raise ModelOutputError("cached tokens exceed input tokens")
    output_tokens = count(raw.get("completion_tokens", raw.get("output_tokens")))
    if not generates and output_tokens is None:
        output_tokens = 0
    details = raw.get("completion_tokens_details", raw.get("output_tokens_details"))
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
        cached_input_tokens=cached,
        uncached_input_tokens=uncached,
        cache_creation_input_tokens=cache_creation,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=reasoning_tokens,
        coverage=coverage,
    )
