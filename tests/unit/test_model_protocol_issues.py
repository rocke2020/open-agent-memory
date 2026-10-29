import json

import httpx
import pytest
from test_runtime_models import runtime_config

from oam.config import ModelsConfig
from oam.indexing.chunker import TextChunk
from oam.indexing.extraction import ExtractionError, extract_facts
from oam.models.bindings import resolve_model_binding
from oam.models.client import OpenAICompatibleProbeClient
from oam.models.preflight import _validate_chat
from oam.models.runtime import RuntimeModels


def _configured(protocol="openai_chat"):
    config = runtime_config()
    values = config.models.model_dump(mode="json")
    values["vlm"].update(protocol=protocol, reasoning_mode="provider_default")
    return config.model_copy(update={"models": ModelsConfig.model_validate(values)})


def test_messages_usage_counts_cached_and_uncached_input():
    response = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "{}"}],
        "usage": {
            "input_tokens": 1,
            "cache_read_input_tokens": 2194,
            "cache_creation_input_tokens": 12,
            "output_tokens": 161,
            "output_tokens_details": {"thinking_tokens": 130},
        },
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        result = RuntimeModels(
            _configured("anthropic_messages"),
            {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"},
            http,
        ).generate(
            "extraction", system="JSON", user="Input", max_output_tokens=1024, timeout_seconds=2
        )
    assert result.usage.input_tokens == 2207
    assert result.usage.output_tokens == 161
    assert result.usage.total_tokens == 2368
    assert result.usage.reasoning_tokens == 130


def test_chat_extraction_parses_json_after_complete_leading_think_block():
    data = {
        "outcome": "facts",
        "facts": [{"text": "The user prefers jasmine tea.", "quote": "I prefer jasmine tea."}],
    }
    response = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "<think>some reasoning</think>\n" + json.dumps(data)},
            }
        ]
    }
    config = _configured()
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        text = "I prefer jasmine tea."
        result = extract_facts(
            RuntimeModels(config, {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"}, http),
            chunk=TextChunk(text, 0, len(text), "", 8),
            config=config,
            message_role="user",
            timeout_seconds=2,
        )
    assert result.outcome == "facts"
    assert result.facts[0].text == "The user prefers jasmine tea."


def test_preflight_parses_json_after_complete_leading_think_block():
    config = _configured()
    binding = resolve_model_binding(
        config.models, "extraction", {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"}
    )
    response = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '<think>reasoning</think>\n{"ok":true}'},
            }
        ]
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        result = OpenAICompatibleProbeClient(http=http).probe_chat(binding, max_output_tokens=1024)
    assert _validate_chat(result) == "visible structured output validated"


@pytest.mark.parametrize(
    "content",
    ["<think>unfinished", "<think>reasoning</think>", "<think>nested <think>bad</think></think>{}"],
)
def test_chat_extraction_rejects_invalid_reasoning_blocks(content):
    response = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    config = _configured()
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as http,
        pytest.raises((ValueError, ExtractionError)),
    ):
        text = "I prefer jasmine tea."
        extract_facts(
            RuntimeModels(config, {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"}, http),
            chunk=TextChunk(text, 0, len(text), "", 8),
            config=config,
            message_role="user",
            timeout_seconds=2,
        )


def test_chat_does_not_remove_reasoning_tags_inside_json():
    from oam.models.protocols import normalize_generation

    config = _configured()
    binding = resolve_model_binding(
        config.models, "extraction", {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"}
    )
    content = '{"quote":"<think>example</think>"}'
    body = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    assert normalize_generation(binding, body)["choices"][0]["message"]["content"] == content


def test_chat_reasoning_prefix_preserves_unwanted_trailing_text():
    from oam.models.protocols import normalize_generation

    config = _configured()
    binding = resolve_model_binding(
        config.models, "extraction", {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"}
    )
    body = {
        "choices": [
            {"finish_reason": "stop", "message": {"content": "<think>r</think>{} trailing"}}
        ]
    }
    answer = normalize_generation(binding, body)["choices"][0]["message"]["content"]
    with pytest.raises(ValueError):
        json.loads(answer)


@pytest.mark.parametrize(
    "field,value",
    [
        ("input_tokens", True),
        ("cache_read_input_tokens", -1),
        ("cache_creation_input_tokens", "9"),
        ("output_tokens", True),
    ],
)
def test_messages_rejects_invalid_token_counts(field, value):
    usage = {
        "input_tokens": 1,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 3,
        "output_tokens": 4,
    }
    usage[field] = value
    response = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "{}"}],
        "usage": usage,
    }
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as http,
        pytest.raises(ValueError),
    ):
        RuntimeModels(
            _configured("anthropic_messages"),
            {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"},
            http,
        ).generate(
            "extraction", system="JSON", user="Input", max_output_tokens=1024, timeout_seconds=2
        )


def test_messages_missing_input_is_partial():
    response = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "{}"}],
        "usage": {
            "cache_read_input_tokens": 20,
            "cache_creation_input_tokens": 10,
            "output_tokens": 3,
        },
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        result = RuntimeModels(
            _configured("anthropic_messages"),
            {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"},
            http,
        ).generate(
            "extraction", system="JSON", user="Input", max_output_tokens=1024, timeout_seconds=2
        )
    assert result.usage.input_tokens is None
    assert result.usage.coverage == "partial"


def test_truncated_reasoning_remains_output_limit_error():
    from oam.models.runtime import ModelOutputTooLongError

    response = {
        "choices": [{"finish_reason": "length", "message": {"content": "<think>unfinished"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 12},
    }
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as http,
        pytest.raises(ModelOutputTooLongError),
    ):
        RuntimeModels(
            _configured(), {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"}, http
        ).generate(
            "extraction", system="JSON", user="Input", max_output_tokens=1024, timeout_seconds=2
        )


def test_messages_malformed_output_preserves_cached_usage_on_error():
    from oam.models.runtime import ModelOutputError

    response = {
        "stop_reason": "end_turn",
        "content": [None],
        "usage": {
            "input_tokens": 1,
            "cache_read_input_tokens": 2194,
            "cache_creation_input_tokens": 0,
            "output_tokens": 20,
        },
    }
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as http,
        pytest.raises(ModelOutputError) as caught,
    ):
        RuntimeModels(
            _configured("anthropic_messages"),
            {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"},
            http,
        ).generate(
            "extraction", system="JSON", user="Input", max_output_tokens=1024, timeout_seconds=2
        )
    assert caught.value.usage.input_tokens == 2195
    assert caught.value.usage.total_tokens == 2215


def test_messages_null_cache_count_is_unknown_not_zero():
    response = {
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "{}"}],
        "usage": {
            "input_tokens": 1,
            "cache_read_input_tokens": None,
            "cache_creation_input_tokens": 0,
            "output_tokens": 20,
        },
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        result = RuntimeModels(
            _configured("anthropic_messages"),
            {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"},
            http,
        ).generate(
            "extraction", system="JSON", user="Input", max_output_tokens=1024, timeout_seconds=2
        )
    assert result.usage.input_tokens is None
    assert result.usage.coverage == "partial"


@pytest.mark.parametrize("missing", ["cache_read_input_tokens", "cache_creation_input_tokens"])
def test_messages_missing_cache_count_is_unknown_not_zero(missing):
    from oam.models.protocols import _messages_usage
    from oam.models.runtime import _usage

    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 70,
        "cache_creation_input_tokens": 20,
        "output_tokens": 10,
    }
    del usage[missing]
    result = _usage(_messages_usage(usage))
    assert result.input_tokens is None
    assert result.coverage == "partial"


@pytest.mark.parametrize(
    "protocol,usage,expected",
    [
        (
            "openai_chat",
            {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 70},
            },
            (100, 70, 30, None),
        ),
        (
            "openai_responses",
            {
                "input_tokens": 100,
                "output_tokens": 10,
                "input_tokens_details": {"cached_tokens": 70},
            },
            (100, 70, 30, None),
        ),
        (
            "anthropic_messages",
            {
                "input_tokens": 10,
                "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 20,
                "output_tokens": 10,
            },
            (100, 70, 10, 20),
        ),
    ],
)
def test_generation_usage_exposes_cache_breakdown(protocol, usage, expected):
    if protocol == "openai_chat":
        response = {
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": usage,
        }
    elif protocol == "openai_responses":
        response = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "{}"}],
                }
            ],
            "usage": usage,
        }
    else:
        response = {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "{}"}],
            "usage": usage,
        }
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as http:
        result = RuntimeModels(
            _configured(protocol), {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"}, http
        ).generate(
            "extraction", system="JSON", user="Input", max_output_tokens=1024, timeout_seconds=2
        )
    u = result.usage
    assert (
        u.input_tokens,
        u.cached_input_tokens,
        u.uncached_input_tokens,
        u.cache_creation_input_tokens,
    ) == expected
    assert u.coverage == "complete"


def test_usage_unknown_cache_breakdown_is_not_fabricated():
    from oam.models.runtime import _usage

    result = _usage({"prompt_tokens": 100, "completion_tokens": 10})
    assert result.cached_input_tokens is None
    assert result.uncached_input_tokens is None
    assert result.cache_creation_input_tokens is None
    assert result.coverage == "complete"


def test_cache_subsets_cannot_exceed_total_input():
    from oam.models.runtime import ModelOutputError, _usage

    with pytest.raises(ModelOutputError):
        _usage(
            {
                "prompt_tokens": 10,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 11},
            }
        )


def test_retry_usage_does_not_report_partial_breakdown_as_complete():
    from oam.indexing.extraction import _sum_extraction_usage
    from oam.models.runtime import Usage

    result = _sum_extraction_usage(
        [
            Usage(input_tokens=10, cached_input_tokens=8, uncached_input_tokens=2),
            Usage(input_tokens=5),
        ]
    )
    assert result.input_tokens == 15
    assert result.cached_input_tokens is None
    assert result.uncached_input_tokens is None


def test_operation_cache_summary_is_unknown_when_an_attempt_outcome_is_unknown():
    from types import SimpleNamespace

    from oam.indexing.store import IndexStore

    recorded = {}
    usage = {
        "input_tokens": 10,
        "output_tokens": 3,
        "total_tokens": 13,
        "reasoning_tokens": 0,
        "cached_input_tokens": 8,
        "uncached_input_tokens": 2,
        "cache_creation_input_tokens": 0,
        "coverage": "complete",
    }

    class Connection:
        def execute(self, sql, args):
            rows = (
                [("extract:1", usage)]
                if "FROM index_stage_results" in sql
                else [("unknown_external_outcome", "extract:2", None)]
            )
            return SimpleNamespace(fetchall=lambda: rows)

    fake = SimpleNamespace(
        claim=SimpleNamespace(stage="extract"),
        key=("tenant", "user"),
        source={"source_id": "s", "source_version": 1},
        generation_id="g",
        readiness=lambda conn, **kwargs: recorded.update(kwargs),
    )
    IndexStore.summarize_usage(fake, Connection())
    assert recorded["usage"]["coverage"] == "partial"
    assert recorded["usage"]["cached_input_tokens"] is None
    assert recorded["usage"]["uncached_input_tokens"] is None
    assert recorded["usage"]["cache_creation_input_tokens"] is None


def test_preflight_logs_reported_cache_tokens(caplog):
    import logging

    config = _configured()
    binding = resolve_model_binding(
        config.models, "extraction", {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"}
    )
    response = {
        "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 70},
        },
    }
    with (
        caplog.at_level(logging.INFO, logger="oam.models.client"),
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as http,
    ):
        result = OpenAICompatibleProbeClient(http=http).probe_chat(binding, max_output_tokens=1024)
    assert _validate_chat(result)
    assert "cached_input_tokens=70" in caplog.text
    assert "uncached_input_tokens=30" in caplog.text


def test_truncated_output_preserves_cache_breakdown():
    from oam.models.runtime import ModelOutputTooLongError

    response = {
        "choices": [{"finish_reason": "length", "message": {"content": "partial"}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 70},
        },
    }
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as http,
        pytest.raises(ModelOutputTooLongError) as captured,
    ):
        RuntimeModels(
            _configured(), {"MODEL_URL": "https://model.example", "MODEL_KEY": "x"}, http
        ).generate(
            "extraction", system="JSON", user="Input", max_output_tokens=1024, timeout_seconds=2
        )
    assert captured.value.usage.cached_input_tokens == 70
    assert captured.value.usage.uncached_input_tokens == 30
