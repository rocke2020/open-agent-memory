from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from importlib.resources import files
from io import BytesIO
from typing import Any

import httpx
from PIL import Image

from oam.config import OAMConfig, TokenizerConfig
from oam.models.bindings import ResolvedModelBinding
from oam.models.embedding_inputs import configured_embedding_admission, embedding_admission
from oam.models.preflight import (
    ChatProbeOutput,
    EmbeddingProbeOutput,
    RerankProbeOutput,
    VisionProbeOutput,
)
from oam.models.protocols import generation_request, normalize_generation
from oam.models.runtime import _usage

logger = logging.getLogger(__name__)

_VISION_PROBE_SIZE = 512
_CHAT_PROBE_PROMPT = (
    files("oam.models")
    .joinpath("preflight-chat-prompt.txt")
    .read_text(encoding="utf-8")
    .rstrip("\n")
)
_VISION_PROBE_PROMPT = (
    files("oam.models")
    .joinpath("preflight-vision-prompt.txt")
    .read_text(encoding="utf-8")
    .rstrip("\n")
)
_RERANK_PROBE_QUERY = (
    files("oam.models")
    .joinpath("preflight-rerank-query.txt")
    .read_text(encoding="utf-8")
    .rstrip("\n")
)


def _vision_probe_png_data_url() -> str:
    image = Image.new("RGB", (_VISION_PROBE_SIZE, _VISION_PROBE_SIZE), (255, 0, 0))
    image.paste((0, 0, 255), (_VISION_PROBE_SIZE // 2, 0, _VISION_PROBE_SIZE, _VISION_PROBE_SIZE))
    output = BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


_VISION_PROBE_PNG_DATA_URL = _vision_probe_png_data_url()


class ModelProtocolError(ValueError):
    """An OpenAI-compatible endpoint returned an unusable wire response."""


class OpenAICompatibleProbeClient:
    """Small real HTTP adapter used by deployment preflight."""

    def __init__(
        self,
        *,
        http: httpx.Client,
        config: OAMConfig | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._http = http
        self._embedding_admission = (
            configured_embedding_admission(config, environment)
            if config
            else embedding_admission(None, TokenizerConfig(type="o200k_base"))
        )

    def probe_chat(
        self, binding: ResolvedModelBinding, *, max_output_tokens: int
    ) -> ChatProbeOutput:
        path, headers, payload = generation_request(
            binding, "", _CHAT_PROBE_PROMPT, max_output_tokens
        )
        return self._probe_generation(binding, path, headers, payload)

    def probe_embeddings(
        self, binding: ResolvedModelBinding, texts: tuple[str, ...]
    ) -> EmbeddingProbeOutput:
        self._embedding_admission.validate(texts)
        payload = {"model": binding.model, "input": list(texts)}
        body = self._post(binding, "/embeddings", payload)
        try:
            data = body["data"]
            if not isinstance(data, list):
                raise TypeError
            indexed_vectors = {}
            for item in data:
                index = item["index"]
                if type(index) is not int or index in indexed_vectors:
                    raise ValueError
                indexed_vectors[index] = tuple(float(value) for value in item["embedding"])
            if set(indexed_vectors) != set(range(len(texts))):
                raise ValueError
            vectors = tuple(indexed_vectors[index] for index in range(len(texts)))
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelProtocolError("embedding response has an invalid data shape") from exc
        return EmbeddingProbeOutput(vectors=vectors)

    def probe_vision(
        self, binding: ResolvedModelBinding, *, max_output_tokens: int
    ) -> VisionProbeOutput:
        path, headers, payload = generation_request(
            binding, "", _VISION_PROBE_PROMPT, max_output_tokens, _VISION_PROBE_PNG_DATA_URL
        )
        output = self._probe_generation(binding, path, headers, payload)
        return VisionProbeOutput(
            content=output.content,
            finish_reason=output.finish_reason,
            reasoning_content=output.reasoning_content,
        )

    def _probe_generation(
        self,
        binding: ResolvedModelBinding,
        path: str,
        headers: dict[str, str],
        payload: dict[str, Any],
    ) -> ChatProbeOutput:
        body = normalize_generation(binding, self._post(binding, path, payload, headers=headers))
        usage = _usage(body.get("usage"))
        logger.info(
            "model_preflight protocol=%s role=%s model=%s input_tokens=%s cached_input_tokens=%s "
            "uncached_input_tokens=%s cache_creation_input_tokens=%s "
            "output_tokens=%s reasoning_tokens=%s",
            binding.protocol,
            binding.role,
            binding.model,
            usage.input_tokens,
            usage.cached_input_tokens,
            usage.uncached_input_tokens,
            usage.cache_creation_input_tokens,
            usage.output_tokens,
            usage.reasoning_tokens,
        )
        return self._chat_output(body)

    def probe_rerank(
        self, binding: ResolvedModelBinding, documents: tuple[str, ...]
    ) -> RerankProbeOutput:
        payload = {
            "model": binding.model,
            "query": _RERANK_PROBE_QUERY,
            "documents": list(documents),
        }
        body = self._post(binding, "/rerank", payload)
        try:
            results = body["results"]
            indexed_scores = {
                int(result["index"]): float(result["relevance_score"]) for result in results
            }
            scores = tuple(indexed_scores[index] for index in range(len(documents)))
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelProtocolError("rerank response has an invalid results shape") from exc
        return RerankProbeOutput(scores=scores)

    def _post(
        self,
        binding: ResolvedModelBinding,
        path: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if headers is None:
            headers = {"content-type": "application/json"}
            if binding._api_key is not None:
                headers["authorization"] = f"Bearer {binding._api_key}"
        response = self._http.post(
            f"{binding.endpoint}{path}",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise ModelProtocolError("model endpoint did not return JSON") from exc
        if not isinstance(body, dict):
            raise ModelProtocolError("model endpoint returned a non-object JSON response")
        return body

    @staticmethod
    def _chat_output(body: dict[str, Any]) -> ChatProbeOutput:
        try:
            choices = body["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise TypeError
            choice = choices[0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError
            content = message.get("content")
            reasoning_content = message.get("reasoning_content")
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelProtocolError("chat response has an invalid choices shape") from exc
        if message.get("refusal"):
            raise ModelProtocolError("chat response contains a refusal")
        if content is not None and not isinstance(content, str):
            raise ModelProtocolError("chat visible content must be a string or null")
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            raise ModelProtocolError("chat reasoning content must be a string or null")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise ModelProtocolError("chat finish reason must be a string or null")
        return ChatProbeOutput(
            content=content,
            finish_reason=finish_reason,
            reasoning_content=reasoning_content,
        )
