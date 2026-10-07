import json
from dataclasses import dataclass, field

import httpx
import pytest

from oam.config import ModelBinding, ModelCapability, ModelRoles, ModelsConfig
from oam.models.bindings import ResolvedModelBinding
from oam.models.client import OpenAICompatibleProbeClient
from oam.models.preflight import (
    ChatProbeOutput,
    EmbeddingProbeOutput,
    PreflightStatus,
    RerankProbeOutput,
    VisionProbeOutput,
    run_model_preflight,
)


def _models() -> ModelsConfig:
    bindings = {
        "chat": ModelBinding(
            protocol="openai_compatible",
            model="chat-model",
            endpoint_env="CHAT_URL",
            capabilities=frozenset({ModelCapability.CHAT, ModelCapability.VISION}),
            reasoning_effort="low",
            thinking="disabled",
        ),
        "embedding": ModelBinding(
            protocol="openai_compatible",
            model="embedding-model",
            endpoint_env="EMBEDDING_URL",
            capabilities=frozenset({ModelCapability.EMBEDDINGS}),
        ),
    }
    return ModelsConfig(
        roles=ModelRoles(
            extraction="chat",
            insights="chat",
            embedding="embedding",
            abstract="chat",
            profile="chat",
        ),
        bindings=bindings,
    )


@dataclass
class FakeProbeClient:
    chat: ChatProbeOutput = ChatProbeOutput(
        content='{"ok": true, "message": "visible capability probe"}',
        finish_reason="stop",
    )
    embedding: EmbeddingProbeOutput = EmbeddingProbeOutput(
        vectors=((0.25, -0.5, 0.75), (0.5, 0.0, -0.5))
    )
    vision: VisionProbeOutput = VisionProbeOutput(
        content='{"left":"red","right":"blue"}',
        finish_reason="stop",
    )
    rerank: RerankProbeOutput = RerankProbeOutput(scores=(0.1, 0.9))
    requested_output_tokens: list[int] = field(default_factory=list)

    def probe_chat(
        self, binding: ResolvedModelBinding, *, max_output_tokens: int
    ) -> ChatProbeOutput:
        self.requested_output_tokens.append(max_output_tokens)
        return self.chat

    def probe_embeddings(
        self, binding: ResolvedModelBinding, texts: tuple[str, ...]
    ) -> EmbeddingProbeOutput:
        return self.embedding

    def probe_vision(
        self, binding: ResolvedModelBinding, *, max_output_tokens: int
    ) -> VisionProbeOutput:
        self.requested_output_tokens.append(max_output_tokens)
        return self.vision

    def probe_rerank(
        self, binding: ResolvedModelBinding, documents: tuple[str, ...]
    ) -> RerankProbeOutput:
        return self.rerank


def test_preflight_probes_every_required_role_and_never_reports_credentials() -> None:
    client = FakeProbeClient()
    report = run_model_preflight(
        _models(),
        {
            "CHAT_URL": "https://models.example.test/v1",
            "EMBEDDING_URL": "https://embeddings.example.test/v1",
        },
        client,
    )

    assert report.runnable is True
    assert {result.role for result in report.roles if result.status is PreflightStatus.PASSED} == {
        "abstract",
        "embedding",
        "extraction",
        "insights",
        "profile",
    }
    assert all(limit >= 1_024 for limit in client.requested_output_tokens)
    assert "credential" not in str(report).lower()


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (ChatProbeOutput(content="", finish_reason="stop"), "empty visible content"),
        (
            ChatProbeOutput(
                content=None,
                reasoning_content="internal reasoning only",
                finish_reason="stop",
            ),
            "reasoning-only",
        ),
        (
            ChatProbeOutput(content='{"ok": true}', finish_reason="length"),
            "length termination",
        ),
        (ChatProbeOutput(content="not-json", finish_reason="stop"), "malformed JSON"),
    ],
)
def test_chat_preflight_rejects_unusable_visible_output(
    response: ChatProbeOutput, reason: str
) -> None:
    report = run_model_preflight(
        _models(),
        {
            "CHAT_URL": "https://models.example.test/v1",
            "EMBEDDING_URL": "https://embeddings.example.test/v1",
        },
        FakeProbeClient(chat=response),
    )

    extraction = next(result for result in report.roles if result.role == "extraction")
    assert extraction.status is PreflightStatus.FAILED
    assert reason in extraction.detail
    assert report.runnable is False


@pytest.mark.parametrize(
    ("vectors", "reason"),
    [
        ((), "no vectors"),
        (((0.1, 0.2), (0.3,)), "inconsistent dimensions"),
        (((0.1, float("nan")), (0.3, 0.4)), "non-finite"),
    ],
)
def test_embedding_preflight_rejects_invalid_vectors(
    vectors: tuple[tuple[float, ...], ...], reason: str
) -> None:
    report = run_model_preflight(
        _models(),
        {
            "CHAT_URL": "https://models.example.test/v1",
            "EMBEDDING_URL": "https://embeddings.example.test/v1",
        },
        FakeProbeClient(embedding=EmbeddingProbeOutput(vectors=vectors)),
    )

    embedding = next(result for result in report.roles if result.role == "embedding")
    assert embedding.status is PreflightStatus.FAILED
    assert reason in embedding.detail


@pytest.mark.parametrize(
    "response",
    [
        VisionProbeOutput(content="", finish_reason="stop"),
        VisionProbeOutput(content="image", finish_reason="length"),
        VisionProbeOutput(content="I cannot see images.", finish_reason="stop"),
        VisionProbeOutput(content="A generic description of an image.", finish_reason="stop"),
        VisionProbeOutput(content='{"left":"blue","right":"red"}', finish_reason="stop"),
    ],
)
def test_vision_preflight_requires_meaningful_complete_visible_output(
    response: VisionProbeOutput,
) -> None:
    report = run_model_preflight(
        _models(),
        {
            "CHAT_URL": "https://models.example.test/v1",
            "EMBEDDING_URL": "https://embeddings.example.test/v1",
        },
        FakeProbeClient(vision=response),
    )

    vision = next(result for result in report.roles if result.role == "extraction")
    assert vision.status is PreflightStatus.FAILED


@pytest.mark.parametrize("capability", ["chat", "vision"])
@pytest.mark.parametrize(
    ("finish_reason", "refusal", "reason"),
    [("content_filter", None, "content_filter"), ("stop", "I cannot comply.", "refusal")],
)
def test_preflight_rejects_filtered_or_refused_wire_output(
    capability: str, finish_reason: str, refusal: str | None, reason: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 0, "embedding": [0.1, 0.2]},
                        {"index": 1, "embedding": [0.3, 0.4]},
                    ]
                },
            )
        vision = isinstance(json.loads(request.content)["messages"][-1]["content"], list)
        filtered = vision == (capability == "vision")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": finish_reason if filtered else "stop",
                        "message": {
                            "content": (
                                '{"left":"red","right":"blue"}' if vision else '{"ok":true}'
                            ),
                            "refusal": refusal if filtered else None,
                        },
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        report = run_model_preflight(
            _models(),
            {
                "CHAT_URL": "https://models.example.test/v1",
                "EMBEDDING_URL": "https://embeddings.example.test/v1",
            },
            OpenAICompatibleProbeClient(http=http),
        )

    result = next(result for result in report.roles if result.role == "extraction")
    assert result.status is PreflightStatus.FAILED
    assert reason in result.detail
    assert report.runnable is False


@dataclass
class TransportFailingProbeClient(FakeProbeClient):
    probed_capabilities: list[str] = field(default_factory=list)

    def probe_chat(
        self, binding: ResolvedModelBinding, *, max_output_tokens: int
    ) -> ChatProbeOutput:
        self.probed_capabilities.append("chat")
        request = httpx.Request("POST", binding.endpoint)
        raise httpx.ConnectError("transport contained top-secret-key", request=request)

    def probe_embeddings(
        self, binding: ResolvedModelBinding, texts: tuple[str, ...]
    ) -> EmbeddingProbeOutput:
        self.probed_capabilities.append("embeddings")
        return super().probe_embeddings(binding, texts)

    def probe_vision(
        self, binding: ResolvedModelBinding, *, max_output_tokens: int
    ) -> VisionProbeOutput:
        self.probed_capabilities.append("vision")
        return super().probe_vision(binding, max_output_tokens=max_output_tokens)


def test_transport_failure_is_redacted_per_role_and_later_probes_continue() -> None:
    client = TransportFailingProbeClient()

    report = run_model_preflight(
        _models(),
        {
            "CHAT_URL": "https://models.example.test/v1",
            "EMBEDDING_URL": "https://embeddings.example.test/v1",
        },
        client,
    )

    extraction = next(result for result in report.roles if result.role == "extraction")
    embedding = next(result for result in report.roles if result.role == "embedding")
    assert extraction.status is PreflightStatus.FAILED
    assert "chat: model request failed: ConnectError" in extraction.detail
    assert "vision: visible image colors correctly identified" in extraction.detail
    assert "top-secret-key" not in str(report)
    assert embedding.status is PreflightStatus.PASSED
    assert "embeddings" in client.probed_capabilities
    assert "vision" in client.probed_capabilities


def test_disabled_feature_role_failure_does_not_block_required_preflight() -> None:
    models = _models()
    values = models.model_dump(mode="json")
    values["roles"]["profile"] = "profile"
    values["bindings"]["profile"] = {
        "protocol": "openai_compatible",
        "model": "profile-model",
        "endpoint_env": "PROFILE_URL",
        "capabilities": ["chat"],
        "reasoning_effort": "low",
        "thinking": "disabled",
    }
    models = ModelsConfig.model_validate(values)
    report = run_model_preflight(
        models,
        {
            "CHAT_URL": "https://models.example.test/v1",
            "EMBEDDING_URL": "https://embeddings.example.test/v1",
        },
        FakeProbeClient(),
        required_roles=frozenset({"extraction", "embedding", "abstract"}),
    )

    profile = next(result for result in report.roles if result.role == "profile")
    assert profile.status is PreflightStatus.FAILED
    assert report.runnable is True
