from __future__ import annotations

from typing import Any

import httpx

from oam.models.bindings import ResolvedModelBinding
from oam.models.preflight import (
    ChatProbeOutput,
    EmbeddingProbeOutput,
    RerankProbeOutput,
    VisionProbeOutput,
)

_VISION_PROBE_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAf0lEQVR4nNXOAQ0AIBCAQJ7+nTWE+zmuAMxh"
    "17BbkDiJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJ"
    "kziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJkziJ8/fAqwsyVwJ/S+vergAAAABJRU5ErkJggg=="
)


class ModelProtocolError(ValueError):
    """An OpenAI-compatible endpoint returned an unusable wire response."""


class OpenAICompatibleProbeClient:
    """Small real HTTP adapter used by deployment preflight."""

    def __init__(self, *, http: httpx.Client) -> None:
        self._http = http

    def probe_chat(
        self, binding: ResolvedModelBinding, *, max_output_tokens: int
    ) -> ChatProbeOutput:
        payload = {
            "model": binding.model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Return one JSON object with ok=true and a short non-empty "
                        "message. Do not return markdown."
                    ),
                }
            ],
            "max_tokens": max_output_tokens,
            "response_format": {"type": "json_object"},
        }
        return self._chat_output(self._post(binding, "/chat/completions", payload))

    def probe_embeddings(
        self, binding: ResolvedModelBinding, texts: tuple[str, ...]
    ) -> EmbeddingProbeOutput:
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
        payload = {
            "model": binding.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Identify the colors in the left and right halves of this image. "
                                'Return only JSON with keys "left" and "right", each containing '
                                "one lowercase English color name."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": _VISION_PROBE_PNG_DATA_URL},
                        },
                    ],
                }
            ],
            "max_tokens": max_output_tokens,
        }
        output = self._chat_output(self._post(binding, "/chat/completions", payload))
        return VisionProbeOutput(
            content=output.content,
            finish_reason=output.finish_reason,
            reasoning_content=output.reasoning_content,
        )

    def probe_rerank(
        self, binding: ResolvedModelBinding, documents: tuple[str, ...]
    ) -> RerankProbeOutput:
        payload = {
            "model": binding.model,
            "query": "Which document concerns a medical measurement?",
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
    ) -> dict[str, Any]:
        headers = {"content-type": "application/json"}
        if binding._api_key is not None:
            headers["authorization"] = f"Bearer {binding._api_key}"
        if path == "/chat/completions" and binding.reasoning_effort is not None:
            payload = {**payload, "reasoning_effort": binding.reasoning_effort}
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
            choice = body["choices"][0]
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
