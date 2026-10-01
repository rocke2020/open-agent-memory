import json
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from oam.config import ModelsConfig, load_config
from oam.indexing.chunker import TextChunk
from oam.indexing.extraction import ExtractionError, extract_facts
from oam.models.runtime import RuntimeModels


def test_default_extraction_requests_a_64000_token_completion_budget():
    requests = []
    with extraction_models({"outcome": "no_facts", "facts": []}, requests) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk("Hello.", 0, 6, "", 2),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert result.outcome == "no_facts"
    assert requests[0]["max_tokens"] == 64000


def test_default_extraction_uses_offered_existing_memory_context():
    reply = {
        "outcome": "facts",
        "facts": [
            {
                "text": "The user prefers tea.",
                "quote": "I prefer tea.",
                "related_existing_ids": ["old-tea"],
            }
        ],
    }
    references = ({"id": "old-tea", "text": "The user prefers tea.", "role": "user"},)
    requests = []
    with extraction_models(reply, requests) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk("I prefer tea.", 0, 13, "", 4),
            config=config,
            message_role="user",
            timeout_seconds=1,
            references=references,
        )
    assert result.facts[0].related_existing_ids == ("old-tea",)
    assert json.loads(requests[0]["messages"][1]["content"])["reference_facts"] == list(references)


def test_reference_hints_are_validated_without_rejecting_new_source_support():
    reply = {
        "outcome": "facts",
        "facts": [
            {
                "text": "The user prefers tea.",
                "quote": "I prefer tea.",
                "related_existing_ids": ["old-tea", "foreign", "old-tea"],
            }
        ],
    }
    requests = []
    with extraction_models(reply, requests) as (models, config):
        config = config.model_copy(
            update={
                "extraction": config.extraction.model_copy(
                    update={"reference_max_facts": 8, "reference_max_input_tokens": 512}
                )
            }
        )
        result = extract_facts(
            models,
            chunk=TextChunk("I prefer tea.", 0, 13, "", 4),
            config=config,
            message_role="user",
            timeout_seconds=1,
            references=(
                {
                    "id": "old-tea",
                    "text": "The user prefers tea.",
                    "role": "user",
                    "mentioned_at": "2026-09-01T09:00:00Z",
                },
            ),
        )
    assert len(result.facts) == 1
    assert result.facts[0].quote == "I prefer tea."
    assert result.facts[0].related_existing_ids == ("old-tea",)
    assert [(d.field, d.code) for d in result.diagnostics] == [
        ("related_existing_ids", "invalid_reference")
    ]
    envelope = json.loads(requests[0]["messages"][1]["content"])
    assert envelope["reference_facts"][0]["id"] == "old-tea"


def test_reference_budget_never_truncates_new_evidence_or_reference_claims():
    requests = []
    text = "I prefer tea."
    with extraction_models({"outcome": "no_facts", "facts": []}, requests) as (models, config):
        config = config.model_copy(
            update={
                "extraction": config.extraction.model_copy(
                    update={"reference_max_facts": 8, "reference_max_input_tokens": 40}
                )
            }
        )
        extract_facts(
            models,
            chunk=TextChunk(text, 0, len(text), "", 4),
            config=config,
            message_role="user",
            timeout_seconds=1,
            references=({"id": "long", "text": "old claim " * 100, "role": "user"},),
        )
    envelope = json.loads(requests[0]["messages"][1]["content"])
    assert envelope["center"]["text"] == text
    assert envelope["reference_facts"] == []


def test_old_reference_quote_cannot_become_new_support():
    with extraction_models(
        {
            "outcome": "facts",
            "facts": [
                {"text": "The user prefers tea.", "quote": "I prefer tea."},
                {"text": "The user now prefers coffee.", "quote": "I now prefer coffee."},
            ],
        }
    ) as (models, config):
        config = config.model_copy(
            update={
                "extraction": config.extraction.model_copy(
                    update={"reference_max_facts": 8, "reference_max_input_tokens": 512}
                )
            }
        )
        result = extract_facts(
            models,
            chunk=TextChunk("I now prefer coffee.", 0, 20, "", 5),
            config=config,
            message_role="user",
            timeout_seconds=1,
            references=({"id": "old", "text": "I prefer tea.", "role": "user"},),
        )
    assert [fact.text for fact in result.facts] == ["The user now prefers coffee."]


def test_disabled_reference_input_preserves_original_model_envelope():
    requests = []
    with extraction_models({"outcome": "no_facts", "facts": []}, requests) as (models, config):
        config = config.model_copy(
            update={"extraction": config.extraction.model_copy(update={"reference_max_facts": 0})}
        )
        extract_facts(
            models,
            chunk=TextChunk("Hello.", 0, 6, "", 2),
            config=config,
            message_role="user",
            timeout_seconds=1,
            references=({"id": "ignored", "text": "Old claim.", "role": "user"},),
        )
    envelope = json.loads(requests[0]["messages"][1]["content"])
    assert set(envelope) == {
        "message_role",
        "mentioned_at",
        "reference_timezone",
        "center",
        "previous_context_only",
        "max_facts",
    }


@pytest.mark.parametrize("hint", [None, "old", [None, [], "foreign"]])
def test_malformed_optional_reference_hint_keeps_a_valid_fact(hint):
    with extraction_models(
        {
            "outcome": "facts",
            "facts": [{"text": "Tea.", "quote": "Tea.", "related_existing_ids": hint}],
        }
    ) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk("Tea.", 0, 4, "", 1),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert len(result.facts) == 1
    assert "related_existing_ids" not in result.model_dump(mode="json")["facts"][0]
    assert result.diagnostics[0].code == "invalid_reference"


def test_reference_instructions_cannot_displace_a_full_new_evidence_input():
    from oam.indexing.extraction import EXTRACTION_SYSTEM, extraction_envelope, model_input_tokens

    with extraction_models({"outcome": "no_facts", "facts": []}) as (models, config):
        chunk = TextChunk("An eligible assertion." * 100, 0, 2200, "", 550)
        envelope = extraction_envelope(chunk=chunk, config=config, message_role="user")
        budget = model_input_tokens(EXTRACTION_SYSTEM, envelope)
        config = config.model_copy(
            update={
                "extraction": config.extraction.model_copy(
                    update={"reference_max_facts": 8, "max_input_tokens": budget}
                )
            }
        )
        result = extract_facts(
            models,
            chunk=chunk,
            config=config,
            message_role="user",
            timeout_seconds=1,
            references=({"id": "old", "text": "Old."},),
        )
    assert result.outcome == "no_facts"


def test_user_subject_wording_is_explicit_in_fact_and_insight_prompts():
    from oam.indexing.extraction import EXTRACTION_SYSTEM, INSIGHT_SYSTEM

    for prompt in (EXTRACTION_SYSTEM, INSIGHT_SYSTEM):
        assert "the user" in prompt
        assert "they/them/their" in prompt


def test_rejected_completed_output_retains_usage_without_becoming_valid_no_facts():
    with (
        extraction_models({"outcome": "facts", "facts": []}) as (models, config),
        pytest.raises(ExtractionError) as failure,
    ):
        extract_facts(
            models,
            chunk=TextChunk("A question?", 0, 11, "", 3),
            config=config,
            message_role="user",
            timeout_seconds=10,
        )
    assert failure.value.usage.total_tokens == 20
    assert failure.value.retryable is True


def test_record_bound_failure_is_metered_and_uses_existing_retry_budget():
    limit = load_config(
        Path(__file__).parents[2] / "configs/oam.yaml"
    ).config.extraction.max_facts_per_group
    reply = {"outcome": "facts", "facts": [{"text": "A.", "quote": "A."}] * (limit + 1)}
    with extraction_models(reply) as (models, config), pytest.raises(ExtractionError) as failure:
        extract_facts(
            models,
            chunk=TextChunk("A.", 0, 2, "", 1),
            config=config,
            message_role="user",
            timeout_seconds=10,
        )
    assert failure.value.usage.total_tokens == 20
    assert failure.value.retryable is True


@contextmanager
def extraction_models(reply, requests=None, *, config=None):
    config = config or load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={
            "models": ModelsConfig.model_validate(
                {
                    "roles": {"extraction": "chat", "insights": "chat"},
                    "bindings": {
                        "chat": {
                            "protocol": "openai_compatible",
                            "model": "synthetic-chat",
                            "endpoint_env": "MODEL_URL",
                            "capabilities": ["chat"],
                        }
                    },
                }
            )
        }
    )

    def respond(request):
        if requests is not None:
            requests.append(json.loads(request.content))
        if callable(reply):
            return reply(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": reply if isinstance(reply, str) else json.dumps(reply),
                        },
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        yield RuntimeModels(config, {"MODEL_URL": "http://synthetic.test/v1"}, http), config


def test_extraction_keeps_grounded_records_and_original_reference_indices():
    reply = {
        "outcome": "facts",
        "facts": [
            {"text": "Rent rose.", "quote": "Rent rose.", "start": 20, "end": 30},
            {"text": "A previous preference", "quote": "I prefer coffee.", "start": 0, "end": 16},
            {
                "text": "Moved because rent rose.",
                "quote": "I moved because rent rose.",
                "start": 31,
                "end": 57,
                "caused_by": [0],
            },
        ],
    }
    requests = []
    with extraction_models(reply, requests) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk(
                "Rent rose. I moved because rent rose.", 20, 57, "I prefer coffee.", 10
            ),
            config=config,
            message_role="user",
            timeout_seconds=2,
        )
    assert result.outcome == "partial"
    assert [(fact.output_index, fact.start, fact.end) for fact in result.facts] == [
        (0, 20, 30),
        (2, 31, 57),
    ]
    assert result.facts[1].caused_by == (0,)
    assert result.diagnostics[0].output_index == 1
    assert result.usage.total_tokens == 20
    envelope = json.loads(requests[0]["messages"][1]["content"])
    assert envelope["message_role"] == "user"
    assert envelope["mentioned_at"] is None
    assert envelope["center"]["start"] == 20
    assert requests[0]["max_tokens"] >= 1024
    system = requests[0]["messages"][0]["content"]
    assert "one concise, self-contained sentence" in system
    assert "Separate independently removable claims" in system


def test_valid_no_facts_is_distinct_from_malformed_or_empty_generation():
    chunk = TextChunk("Hello.", 0, 6, "", 2)
    with extraction_models({"outcome": "no_facts", "facts": []}) as (models, config):
        result = extract_facts(
            models, chunk=chunk, config=config, message_role="assistant", timeout_seconds=1
        )
    assert result.outcome == "no_facts"
    assert result.facts == () and result.diagnostics == ()
    for reply in [
        "not JSON",
        {},
        {"facts": [{"text": "Hello.", "quote": "Hello."}]},
        {"outcome": "facts", "facts": []},
        {"outcome": "no_facts", "facts": [{}]},
    ]:
        with extraction_models(reply) as (models, config), pytest.raises(ExtractionError):
            extract_facts(
                models, chunk=chunk, config=config, message_role="user", timeout_seconds=1
            )


def test_extraction_resolves_unique_exact_quotes_without_trusting_model_character_counts():
    reply = {
        "type": "json_object",
        "outcome": "facts",
        "facts": [
            {"text": "Lin prefers tea.", "quote": "Lin prefers tea.", "start": 0, "end": 2},
            {"text": "Lin runs.", "quote": "Lin runs."},
            {"text": "Invented.", "quote": "Invented."},
        ],
    }
    source = "Lin prefers tea. Lin runs."
    with extraction_models(reply) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk(source, 20, 20 + len(source), "", 8),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert [(fact.text, fact.start, fact.end) for fact in result.facts] == [
        ("Lin prefers tea.", 20, 36),
        ("Lin runs.", 37, 46),
    ]
    assert result.outcome == "partial"
    assert result.diagnostics[0].output_index == 2


def test_extraction_does_not_guess_between_repeated_quote_occurrences():
    source = "Tea. Tea."
    reply = {"outcome": "facts", "facts": [{"text": "Tea.", "quote": "Tea."}]}
    with extraction_models(reply) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk(source, 0, len(source), "", 3),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert result.facts == () and result.outcome == "partial"


def test_annotation_keeps_event_and_reference_roles_and_rejects_wrong_quote():
    annotation = {
        "role": "event",
        "start": "2024-01-01T00:00:00Z",
        "end": "2025-01-01T00:00:00Z",
        "instant": False,
        "granularity": "year",
        "meaning": "possible_period",
        "quote": "2024",
        "span_start": 0,
        "span_end": 16,
    }
    reply = {
        "outcome": "facts",
        "facts": [
            {
                "text": "Moved in 2024.",
                "quote": "I moved in 2024.",
                "start": 0,
                "end": 16,
                "annotations": [
                    annotation,
                    {**annotation, "role": "date_reference", "quote": "2025"},
                ],
            }
        ],
    }
    with extraction_models(reply) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk("I moved in 2024.", 0, 16, "", 4),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert result.outcome == "partial"
    assert len(result.facts) == 1 and len(result.facts[0].annotations) == 1
    assert result.facts[0].annotations[0].role == "event"
    assert result.facts[0].annotations[0].meaning == "possible_period"
    assert (result.facts[0].annotations[0].span_start, result.facts[0].annotations[0].span_end) == (
        11,
        15,
    )
    assert result.model_dump(mode="json")["facts"][0]["annotations"][0]["start"].startswith(
        "2024-01-01"
    )
    assert result.diagnostics


def test_extraction_enforces_prompt_bound_before_dispatch():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={"extraction": config.extraction.model_copy(update={"max_input_tokens": 10})}
    )
    requests = []
    with (
        extraction_models({"outcome": "no_facts", "facts": []}, requests, config=config) as (
            models,
            selected,
        ),
        pytest.raises(ExtractionError, match="input"),
    ):
        extract_facts(
            models,
            chunk=TextChunk("A source.", 0, 9, "", 3),
            config=selected,
            message_role="user",
            timeout_seconds=1,
        )
    assert requests == []


def test_extraction_drops_invented_quote_and_forward_relation_without_reindexing():
    reply = {
        "outcome": "facts",
        "facts": [
            {"text": "Likes tea", "quote": "I like tea.", "start": 0, "end": 11, "caused_by": [1]},
            {"text": "Likes coffee", "quote": "I like coffee.", "start": 0, "end": 11},
        ],
    }
    requests = []
    with extraction_models(reply, requests) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk("I like tea.", 0, 11, "", 3),
            config=config,
            message_role="assistant",
            timeout_seconds=1,
        )
    assert result.outcome == "partial"
    assert len(result.facts) == 1
    assert result.facts[0].output_index == 0
    assert result.facts[0].caused_by == ()
    assert {(item.output_index, item.field) for item in result.diagnostics} == {
        (0, "caused_by"),
        (1, "fact"),
    }
    assert json.loads(requests[0]["messages"][1]["content"])["message_role"] == "assistant"


@pytest.mark.parametrize(
    "annotation_change",
    [
        {"start": "2024-01-01T00:00:00"},
        {"end": "2023-01-01T00:00:00Z"},
        {"instant": True},
        {"instant": 0},
        {"start": 1704067200},
        {"quote": "1999"},
    ],
)
def test_invalid_temporal_annotation_preserves_the_supported_fact(annotation_change):
    annotation = {
        "role": "date_reference",
        "start": "2024-01-01T00:00:00Z",
        "end": "2025-01-01T00:00:00Z",
        "instant": False,
        "granularity": "year",
        "meaning": "possible_period",
        "quote": "2024",
        "span_start": 11,
        "span_end": 15,
        **annotation_change,
    }
    reply = {
        "outcome": "facts",
        "facts": [
            {
                "text": "Moved",
                "quote": "I moved in 2024.",
                "start": 0,
                "end": 16,
                "annotations": [annotation],
            }
        ],
    }
    with extraction_models(reply) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk("I moved in 2024.", 0, 16, "", 4),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert result.outcome == "partial"
    assert len(result.facts) == 1 and result.facts[0].annotations == ()


@pytest.mark.parametrize(
    "subtype",
    ["preferences", "patterns", "projects", "relationships", "experiences", "general_insights"],
)
def test_insight_requires_two_distinct_known_supports_and_preserves_source_references(subtype):
    from oam.indexing.extraction import InsightInput, synthesize_insight

    inputs = (
        InsightInput(id="f1", text="Walked before breakfast on Monday.", source_refs=("m1:0:35",)),
        InsightInput(id="f2", text="Walked before breakfast on Tuesday.", source_refs=("m2:0:35",)),
    )
    requests = []
    with extraction_models(
        {
            "outcome": "insight",
            "subtype": subtype,
            "type": "json_object",
            "text": "Walking before breakfast occurred twice.",
            "support_ids": ["f1", "f2"],
        },
        requests,
    ) as (models, config):
        result = synthesize_insight(models, facts=inputs, config=config, timeout_seconds=1)
    assert result.outcome == "insight"
    assert result.subtype == subtype
    assert result.support_ids == ("f1", "f2")
    assert result.model_dump(mode="json")["usage"]["total_tokens"] == 20
    payload = json.loads(requests[0]["messages"][1]["content"])
    assert payload["facts"][0]["source_refs"] == ["m1:0:35"]


@pytest.mark.parametrize(
    "reply",
    [
        {
            "outcome": "insight",
            "subtype": "general_insights",
            "text": "A claim",
            "support_ids": ["f1", "invented"],
        },
        {
            "outcome": "insight",
            "subtype": "general_insights",
            "text": "A claim",
            "support_ids": ["f1", "f1"],
        },
        {
            "outcome": "insight",
            "subtype": "general_insights",
            "text": " ",
            "support_ids": ["f1", "f2"],
        },
        {"outcome": "no_insight", "subtype": None, "text": "A claim", "support_ids": []},
        {"outcome": "no_insight", "subtype": None, "text": None, "support_ids": ["f1"]},
        {},
    ],
)
def test_insight_rejects_unusable_or_invented_support(reply):
    from oam.indexing.extraction import InsightInput, synthesize_insight

    inputs = (
        InsightInput(id="f1", text="A", source_refs=("s1",)),
        InsightInput(id="f2", text="B", source_refs=("s2",)),
    )
    with extraction_models(reply) as (models, config), pytest.raises(ExtractionError):
        synthesize_insight(models, facts=inputs, config=config, timeout_seconds=1)


def test_insight_distinguishes_valid_no_insight_and_rejects_duplicate_input_identity():
    from oam.indexing.extraction import InsightInput, synthesize_insight

    first = InsightInput(id="f1", text="A", source_refs=("s1",))
    second = InsightInput(id="f2", text="B", source_refs=("s2",))
    requests = []
    with extraction_models(
        {"outcome": "no_insight", "subtype": None, "text": None, "support_ids": []}, requests
    ) as (models, config):
        result = synthesize_insight(models, facts=(first, second), config=config, timeout_seconds=1)
        assert result.outcome == "no_insight" and result.text is None
        assert result.subtype is None
        with pytest.raises(ExtractionError):
            synthesize_insight(models, facts=(first, first), config=config, timeout_seconds=1)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"subtype": None},
        {"subtype": "fact"},
        {"subtype": "preference"},
        {"subtype": "unknown"},
        {"subtype": 1},
    ],
)
def test_insight_rejects_missing_or_unsupported_subtype(change):
    from oam.indexing.extraction import InsightInput, synthesize_insight

    facts = (
        InsightInput(id="f1", text="A", source_refs=("s1",)),
        InsightInput(id="f2", text="B", source_refs=("s2",)),
    )
    reply = {
        "outcome": "insight",
        "text": "A supported synthesis.",
        "support_ids": ["f1", "f2"],
        **change,
    }
    with extraction_models(reply) as (models, config), pytest.raises(ExtractionError):
        synthesize_insight(models, facts=facts, config=config, timeout_seconds=1)


def test_no_insight_cannot_publish_a_subtype():
    from oam.indexing.extraction import InsightInput, synthesize_insight

    facts = (
        InsightInput(id="f1", text="A", source_refs=("s1",)),
        InsightInput(id="f2", text="B", source_refs=("s2",)),
    )
    reply = {
        "outcome": "no_insight",
        "subtype": "general_insights",
        "text": None,
        "support_ids": [],
    }
    with extraction_models(reply) as (models, config), pytest.raises(ExtractionError):
        synthesize_insight(models, facts=facts, config=config, timeout_seconds=1)
