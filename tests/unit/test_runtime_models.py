import base64
import json
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from oam.config import ModelsConfig, load_config
from oam.models.runtime import ModelOutputError, RuntimeModels


def runtime_config():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    models = ModelsConfig.model_validate(
        {
            "roles": {
                "extraction": "chat",
                "insights": "chat",
                "abstract": "chat",
                "embedding": "vector",
                "document_vision": "vision",
                "reranker": "rank",
            },
            "bindings": {
                alias: {
                    "protocol": "openai_compatible",
                    "model": f"synthetic-{alias}",
                    "endpoint_env": "MODEL_URL",
                    "api_key_env": "MODEL_KEY",
                    "capabilities": [capability],
                }
                for alias, capability in (
                    ("chat", "chat"),
                    ("vector", "embeddings"),
                    ("vision", "vision"),
                    ("rank", "rerank"),
                )
            },
        }
    )
    return config.model_copy(
        update={
            "models": models,
            "index": config.index.model_copy(update={"embedding_dimensions": 2}),
        }
    )


@pytest.mark.parametrize("effort", ["low", "none"])
def test_explicit_reasoning_effort_reaches_generation_request_and_index_identity(effort):
    from oam.indexing.identity import index_generation

    base = runtime_config()
    values = base.models.model_dump(mode="json")
    values["bindings"]["chat"]["reasoning_effort"] = effort
    configured = base.model_copy(update={"models": ModelsConfig.model_validate(values)})
    environment = {"MODEL_URL": "https://model.example/v1", "MODEL_KEY": "synthetic"}
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        RuntimeModels(configured, environment, http).generate(
            "extraction",
            system="Extract.",
            user="A fact.",
            max_output_tokens=1024,
            timeout_seconds=2,
        )
    assert seen[0]["reasoning_effort"] == effort
    assert index_generation(base, environment) != index_generation(configured, environment)


def test_runtime_generation_preserves_visible_content_and_does_not_add_reasoning_twice():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"facts":[]}', "reasoning_content": "hidden"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "total_tokens": 140,
                    "completion_tokens_details": {"reasoning_tokens": 30},
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        result = RuntimeModels(
            runtime_config(),
            {"MODEL_URL": "https://models.example.test/v1", "MODEL_KEY": "synthetic-secret"},
            http,
        ).generate(
            "extraction",
            system="Extract supported facts.",
            user="I prefer quiet rooms.",
            max_output_tokens=1024,
            timeout_seconds=4.5,
        )

    assert result.content == '{"facts":[]}'
    assert result.usage.model_dump() == {
        "input_tokens": 100,
        "output_tokens": 40,
        "total_tokens": 140,
        "reasoning_tokens": 30,
        "coverage": "complete",
    }
    payload = json.loads(requests[0].content)
    assert payload["messages"] == [
        {"role": "system", "content": "Extract supported facts."},
        {"role": "user", "content": "I prefer quiet rooms."},
    ]
    assert payload["model"] == "synthetic-chat"
    assert payload["max_tokens"] == 1024
    assert requests[0].extensions["timeout"]["read"] == 4.5
    assert requests[0].headers["authorization"] == "Bearer synthetic-secret"


def test_runtime_document_vision_sends_image_bytes_and_reads_visible_json():
    image = BytesIO()
    Image.new("RGB", (12, 8), "white").save(image, format="PNG")

    def respond(request):
        payload = json.loads(request.content)
        assert payload["model"] == "synthetic-vision"
        assert payload["max_tokens"] >= 1024
        content = payload["messages"][1]["content"]
        assert content[0] == {"type": "text", "text": "Read the report table."}
        prefix, encoded = content[1]["image_url"]["url"].split(",", 1)
        assert prefix == "data:image/png;base64"
        assert base64.b64decode(encoded) == image.getvalue()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"blocks":[{"text":"Glucose 92 mg/dL"}]}'},
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        result = RuntimeModels(
            runtime_config(),
            {"MODEL_URL": "https://models.example.test/v1", "MODEL_KEY": "synthetic"},
            http,
        ).generate_vision(
            image.getvalue(),
            media_type="image/png",
            system="Extract cited details.",
            user="Read the report table.",
            max_output_tokens=1024,
            timeout_seconds=2,
        )

    assert json.loads(result.content) == {"blocks": [{"text": "Glucose 92 mg/dL"}]}


@pytest.mark.parametrize(
    ("finish_reason", "message"),
    [
        ("length", {"content": "incomplete"}),
        ("stop", {"content": " ", "reasoning_content": "hidden only"}),
        ("stop", {"content": "visible", "refusal": "refused"}),
        ("content_filter", {"content": "blocked"}),
        (None, {"content": "unknown termination"}),
    ],
)
def test_generation_rejects_unusable_or_nonterminal_visible_output(finish_reason, message):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [{"finish_reason": finish_reason, "message": message}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                },
            )
        )
    ) as http:
        runtime = RuntimeModels(
            runtime_config(), {"MODEL_URL": "http://model.test", "MODEL_KEY": "k"}, http
        )
        with pytest.raises(ModelOutputError) as captured:
            runtime.generate(
                "extraction", system="s", user="u", max_output_tokens=1024, timeout_seconds=1
            )
        assert captured.value.usage.model_dump() == {
            "input_tokens": 100,
            "output_tokens": 40,
            "total_tokens": 140,
            "reasoning_tokens": None,
            "coverage": "complete",
        }


def test_generation_rejects_low_output_budget_and_invalid_deadline_before_dispatch():
    def forbidden(request):
        pytest.fail("Invalid admission must not dispatch HTTP")

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as http:
        runtime = RuntimeModels(
            runtime_config(), {"MODEL_URL": "http://model.test", "MODEL_KEY": "k"}, http
        )
        for tokens, timeout in [(100, 1), (1024, 0), (1024, float("nan"))]:
            with pytest.raises(ValueError):
                runtime.generate(
                    "extraction",
                    system="s",
                    user="u",
                    max_output_tokens=tokens,
                    timeout_seconds=timeout,
                )


def test_missing_usage_remains_unknown_and_http_errors_are_not_hidden():
    responses = iter(
        [
            httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
            ),
            httpx.Response(429, json={"error": {"message": "limited"}}),
        ]
    )
    with httpx.Client(transport=httpx.MockTransport(lambda request: next(responses))) as http:
        runtime = RuntimeModels(
            runtime_config(), {"MODEL_URL": "http://model.test", "MODEL_KEY": "k"}, http
        )
        result = runtime.generate(
            "extraction", system="s", user="u", max_output_tokens=1024, timeout_seconds=1
        )
        assert result.usage.input_tokens is None
        assert result.usage.total_tokens is None
        assert result.usage.coverage == "unavailable"
        with pytest.raises(httpx.HTTPStatusError) as captured:
            runtime.generate(
                "extraction", system="s", user="u", max_output_tokens=1024, timeout_seconds=1
            )
        assert captured.value.response.status_code == 429


def test_embedding_and_reranking_restore_input_order_and_preserve_actual_query():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(
                200,
                json={
                    "data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}],
                    "usage": {
                        "prompt_tokens": 9,
                        "completion_tokens": 0,
                        "total_tokens": 9,
                        "completion_tokens_details": None,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.8},
                    {"index": 0, "relevance_score": 0.2},
                ],
                "usage": {"total_tokens": 7},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        runtime = RuntimeModels(
            runtime_config(), {"MODEL_URL": "http://model.test", "MODEL_KEY": "k"}, http
        )
        embedded = runtime.embed(("first", "second"), timeout_seconds=1)
        ranked = runtime.rerank("actual query", ("first", "second"), timeout_seconds=1)
    assert embedded.vectors == ((1.0, 0.0), (0.0, 1.0))
    assert embedded.usage.total_tokens == 9
    assert embedded.usage.output_tokens == 0
    assert ranked.scores == (0.2, 0.8)
    assert ranked.usage.coverage == "partial"
    assert requests[0]["input"] == ["first", "second"]
    assert requests[1]["query"] == "actual query"
    assert requests[1]["documents"] == ["first", "second"]


@pytest.mark.parametrize("indices", [(0, 0), (0, 2), (0, True), (0, "1"), (0,)])
@pytest.mark.parametrize("method", ["embed", "rerank"])
def test_scoring_responses_require_exact_unique_integer_indices(indices, method):
    body = (
        {"data": [{"index": index, "embedding": [1, 0]} for index in indices]}
        if method == "embed"
        else {"results": [{"index": index, "relevance_score": 0.5} for index in indices]}
    )
    body["usage"] = {"prompt_tokens": 9, "total_tokens": 9}
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as http:
        runtime = RuntimeModels(
            runtime_config(), {"MODEL_URL": "http://model.test", "MODEL_KEY": "k"}, http
        )
        with pytest.raises(ModelOutputError) as captured:
            if method == "embed":
                runtime.embed(("a", "b"), timeout_seconds=1)
            else:
                runtime.rerank("q", ("a", "b"), timeout_seconds=1)
        assert captured.value.usage.input_tokens == 9
        assert captured.value.usage.output_tokens == 0
        assert captured.value.usage.total_tokens == 9
        assert captured.value.usage.coverage == "complete"


@pytest.mark.parametrize("vector", [[1], [1, 2, 3], [0, 0], [True, 1], ["NaN", 1]])
def test_embedding_rejects_wrong_dimensions_zero_vectors_and_nonfinite_components(vector):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "data": [{"index": 0, "embedding": vector}],
                    "usage": {"prompt_tokens": 9, "total_tokens": 9},
                },
            )
        )
    ) as http:
        runtime = RuntimeModels(
            runtime_config(), {"MODEL_URL": "http://model.test", "MODEL_KEY": "k"}, http
        )
        with pytest.raises(ModelOutputError) as captured:
            runtime.embed(("a",), timeout_seconds=1)
        assert captured.value.usage.total_tokens == 9
        assert captured.value.usage.coverage == "complete"


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": -1},
        {"prompt_tokens": True},
        {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 9},
        {"completion_tokens": 3, "completion_tokens_details": {"reasoning_tokens": 4}},
        {"completion_tokens_details": []},
    ],
)
def test_generation_rejects_inconsistent_token_accounting(usage):
    body = {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}], "usage": usage}
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as http:
        runtime = RuntimeModels(
            runtime_config(), {"MODEL_URL": "http://model.test", "MODEL_KEY": "k"}, http
        )
        with pytest.raises(ModelOutputError) as captured:
            runtime.generate(
                "extraction", system="s", user="u", max_output_tokens=1024, timeout_seconds=1
            )
        assert captured.value.usage is None


def test_generation_invalid_content_without_usage_keeps_metering_unavailable():
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": []}))
    ) as http:
        runtime = RuntimeModels(
            runtime_config(), {"MODEL_URL": "http://model.test", "MODEL_KEY": "k"}, http
        )
        with pytest.raises(ModelOutputError) as captured:
            runtime.generate(
                "extraction", system="s", user="u", max_output_tokens=1024, timeout_seconds=1
            )
        assert captured.value.usage.total_tokens is None
        assert captured.value.usage.coverage == "unavailable"
