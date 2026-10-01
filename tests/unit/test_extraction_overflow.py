import json

import httpx
import pytest
from test_extraction_groups import passage
from test_indexing_extraction import extraction_models

from oam.indexing.chunker import TextChunk
from oam.indexing.extraction import ExtractionError, extract_facts
from oam.indexing.grouping import extract_group
from oam.models.runtime import ModelOutputError


def response(content, finish_reason="stop"):
    return httpx.Response(
        200,
        json={
            "choices": [{"finish_reason": finish_reason, "message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        },
    )


def test_group_overflow_recursively_splits_and_preserves_passage_ownership_and_usage():
    requests = []

    def respond(request):
        envelope = json.loads(json.loads(request.content)["messages"][1]["content"])
        members = envelope["passages"]
        if len(members) > 1:
            return response("", "length")
        member = members[0]
        text = member["center"]["text"]
        return response(
            json.dumps(
                {
                    "outcome": "facts",
                    "facts": [{"passage_id": member["passage_id"], "text": text, "quote": text}],
                }
            )
        )

    inputs = (passage("a", "Tea.", 0), passage("b", "Coffee.", 1), passage("c", "Water.", 2))
    with extraction_models(respond, requests) as (models, config):
        result = extract_group(models, passages=inputs, config=config, timeout_seconds=1)
    assert [(f.output_index, f.passage_id, f.quote) for f in result.facts] == [
        (0, "a", "Tea."),
        (1, "b", "Coffee."),
        (2, "c", "Water."),
    ]
    assert len(requests) == 5
    assert result.outcome == "facts"
    assert result.usage.total_tokens == 100


def test_single_passage_overflow_preserves_exact_source_offsets_and_whitespace():
    text = "a" * 300 + ". " + "b" * 300 + ". "
    requests = []

    def respond(request):
        envelope = json.loads(json.loads(request.content)["messages"][1]["content"])
        center = envelope["center"]
        if len(center["text"]) > 500:
            return response("{", "length")
        return response(
            json.dumps(
                {
                    "outcome": "facts",
                    "facts": [{"text": center["text"], "quote": center["text"]}],
                }
            )
        )

    with extraction_models(respond, requests) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk(text, 50, 50 + len(text), "prior", 151),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert "".join(f.quote for f in result.facts) == text
    assert [(f.start, f.end) for f in result.facts] == [(50, 352), (352, 654)]
    assert [f.output_index for f in result.facts] == [0, 1]
    assert len(requests) == 3
    assert result.usage.total_tokens == 60


def test_unsplittable_overflow_remains_a_failure_and_does_not_loop():
    requests = []
    with (
        extraction_models(lambda request: response("", "length"), requests) as (models, config),
        pytest.raises(ModelOutputError) as failure,
    ):
        extract_facts(
            models,
            chunk=TextChunk("Tea.", 0, 4, "", 1),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert len(requests) == 1
    assert failure.value.usage.total_tokens == 20


def test_malformed_json_does_not_trigger_an_overflow_split():
    requests = []
    text = "a" * 604
    with (
        extraction_models(lambda request: response("not JSON"), requests) as (models, config),
        pytest.raises(ExtractionError),
    ):
        extract_facts(
            models,
            chunk=TextChunk(text, 0, len(text), "", 151),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert len(requests) == 1


def test_split_result_relations_and_diagnostics_keep_their_own_output_indices():
    def respond(request):
        members = json.loads(json.loads(request.content)["messages"][1]["content"])["passages"]
        if len(members) > 1:
            return response("", "length")
        member = members[0]
        first, second = member["center"]["text"].split(" ")
        owner = member["passage_id"]
        return response(
            json.dumps(
                {
                    "outcome": "facts",
                    "facts": [
                        {"passage_id": owner, "text": first, "quote": first},
                        {"passage_id": owner, "text": "Invalid.", "quote": "Missing."},
                        {
                            "passage_id": owner,
                            "text": second,
                            "quote": second,
                            "caused_by": [0],
                            "updates": [0],
                        },
                    ],
                }
            )
        )

    with extraction_models(respond) as (models, config):
        result = extract_group(
            models,
            passages=(passage("a", "Tea. Cake.", 0), passage("b", "Coffee. Water.", 1)),
            config=config,
            timeout_seconds=1,
        )
    assert [f.output_index for f in result.facts] == [0, 2, 3, 5]
    assert result.facts[-1].caused_by == (3,)
    assert result.facts[-1].updates == (3,)
    assert [d.output_index for d in result.diagnostics] == [1, 4]
    assert result.outcome == "partial"


def test_later_unsplittable_overflow_counts_parent_and_successful_sibling_usage():
    requests = []

    def respond(request):
        members = json.loads(json.loads(request.content)["messages"][1]["content"])["passages"]
        if len(members) > 1 or members[0]["passage_id"] == "b":
            return response("", "length")
        return response(json.dumps({"outcome": "no_facts", "facts": []}))

    with (
        extraction_models(respond, requests) as (models, config),
        pytest.raises(ModelOutputError) as failure,
    ):
        extract_group(
            models,
            passages=(passage("a", "Tea.", 0), passage("b", "Coffee.", 1)),
            config=config,
            timeout_seconds=1,
        )
    assert len(requests) == 3
    assert failure.value.usage.total_tokens == 60


def test_overflow_incomplete_usage_remains_partial():
    def respond(request):
        members = json.loads(json.loads(request.content)["messages"][1]["content"])["passages"]
        reply = (
            response("", "length")
            if len(members) > 1
            else response(json.dumps({"outcome": "no_facts", "facts": []}))
        )
        body = reply.json()
        body["usage"] = {"prompt_tokens": 10}
        return httpx.Response(200, json=body)

    with extraction_models(respond) as (models, config):
        result = extract_group(
            models,
            passages=(passage("a", "Tea.", 0), passage("b", "Coffee.", 1)),
            config=config,
            timeout_seconds=1,
        )
    assert result.usage.input_tokens == 30
    assert result.usage.output_tokens is None
    assert result.usage.total_tokens is None
    assert result.usage.coverage == "partial"
