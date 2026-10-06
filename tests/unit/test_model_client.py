import base64
import json
from io import BytesIO

import httpx
import pytest
from PIL import Image

from oam.config import ModelBinding, ModelCapability, ModelRoles, ModelsConfig
from oam.models.bindings import resolve_model_binding
from oam.models.client import ModelProtocolError, OpenAICompatibleProbeClient


def _binding(capability: ModelCapability):
    models = ModelsConfig(
        roles=ModelRoles(extraction="model"),
        bindings={
            "model": ModelBinding(
                protocol="openai_compatible",
                model="synthetic-model",
                endpoint_env="MODEL_URL",
                api_key_env="MODEL_KEY",
                capabilities=frozenset({ModelCapability.CHAT, ModelCapability.VISION, capability}),
                reasoning_effort="low",
                thinking="disabled",
            )
        },
    )
    binding = resolve_model_binding(
        models,
        "extraction",
        {"MODEL_URL": "https://models.example.test/v1", "MODEL_KEY": "secret"},
    )
    assert binding is not None
    return binding


def test_openai_compatible_client_uses_visible_chat_content_and_output_limit() -> None:
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"ok":true,"message":"visible"}',
                            "reasoning_content": "hidden",
                        },
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http:
        result = OpenAICompatibleProbeClient(http=http).probe_chat(
            _binding(ModelCapability.CHAT), max_output_tokens=1_024
        )

    assert result.content == '{"ok":true,"message":"visible"}'
    assert result.reasoning_content == "hidden"
    assert seen_payload["max_tokens"] == 1_024
    assert seen_payload["model"] == "synthetic-model"
    assert seen_payload["reasoning_effort"] == "low"
    assert seen_payload["thinking"] == {"type": "disabled"}


def test_openai_compatible_client_decodes_embedding_and_rerank_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.3, 0.4]},
                        {"index": 0, "embedding": [0.1, 0.2]},
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.2},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleProbeClient(http=http)
        binding = _binding(ModelCapability.EMBEDDINGS)
        embeddings = client.probe_embeddings(binding, ("first", "second"))
        reranked = client.probe_rerank(binding, ("first", "second"))

    assert embeddings.vectors == ((0.1, 0.2), (0.3, 0.4))
    assert reranked.scores == (0.2, 0.9)


def test_openai_compatible_vision_probe_sends_a_real_image_part() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        content = payload["messages"][0]["content"]
        assert content[1]["type"] == "image_url"
        prefix, encoded = content[1]["image_url"]["url"].split(",", 1)
        assert prefix == "data:image/png;base64"
        with Image.open(BytesIO(base64.b64decode(encoded))) as image:
            pixels = image.convert("RGB")
            assert pixels.size == (512, 512)
            assert pixels.getpixel((128, 256)) == (255, 0, 0)
            assert pixels.getpixel((384, 256)) == (0, 0, 255)
        assert "left" in content[0]["text"]
        assert "right" in content[0]["text"]
        assert "red" not in content[0]["text"].lower()
        assert "blue" not in content[0]["text"].lower()
        assert payload["max_tokens"] >= 1_024
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"left":"red","right":"blue"}'},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        result = OpenAICompatibleProbeClient(http=http).probe_vision(
            _binding(ModelCapability.VISION), max_output_tokens=1_024
        )

    assert result.content == '{"left":"red","right":"blue"}'


@pytest.mark.parametrize("message", [None, [], "malformed"])
def test_chat_client_rejects_non_object_messages_as_protocol_errors(message) -> None:
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"choices": [{"message": message}]})
            )
        ) as http,
        pytest.raises(ModelProtocolError, match="choices shape"),
    ):
        OpenAICompatibleProbeClient(http=http).probe_chat(
            _binding(ModelCapability.CHAT), max_output_tokens=1_024
        )


@pytest.mark.parametrize(
    "indices",
    [(0, 0), (0, 2), (0, -1), (0, "1"), (0, True), (0, None)],
)
def test_embedding_client_rejects_invalid_response_indices(indices) -> None:
    data = [{"index": index, "embedding": [0.1, 0.2]} for index in indices]
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": data}))
        ) as http,
        pytest.raises(ModelProtocolError, match="data shape"),
    ):
        OpenAICompatibleProbeClient(http=http).probe_embeddings(
            _binding(ModelCapability.EMBEDDINGS), ("first", "second")
        )
