import json

import httpx
import pytest
from test_extraction_groups import passage
from test_indexing_extraction import extraction_models

from oam.indexing.chunker import TextChunk
from oam.indexing.extraction import ExtractionError, extract_facts
from oam.indexing.grouping import extract_group
from oam.models.runtime import ModelOutputError
from oam.tokens import estimate_units, estimated_tokens


@pytest.mark.parametrize("grouped", [False, True])
@pytest.mark.parametrize("escaped", ["\\", "\x01"])
def test_escaped_source_input_splits_before_dispatch_without_losing_support(grouped, escaped):
    from oam.indexing.extraction import model_input_tokens

    text = "".join(f"Line {index}: " + escaped * 50 + ".\n" for index in range(120))
    requests = []

    def respond(request):
        messages = json.loads(request.content)["messages"]
        envelope = json.loads(messages[1]["content"])
        assert model_input_tokens(messages[0]["content"], envelope) <= 8192
        center = envelope["passages"][0] if grouped else envelope
        quote = center["center"]["text"]
        fact = {"text": quote, "quote": quote}
        if grouped:
            fact["passage_id"] = center["passage_id"]
        return response(json.dumps({"outcome": "facts", "facts": [fact]}))

    with extraction_models(respond, requests) as (models, config):
        chunk = TextChunk(text, 50, 50 + len(text), "prior", estimated_tokens(estimate_units(text)))
        assert chunk.estimated_tokens <= config.chunk.ingestion.max_estimated_tokens
        if grouped:
            member = passage("canonical", text, 0)
            result = extract_group(
                models,
                passages=(member,),
                config=config,
                timeout_seconds=1,
            )
        else:
            result = extract_facts(
                models,
                chunk=chunk,
                config=config,
                message_role="user",
                timeout_seconds=1,
            )
    assert len(requests) >= 2
    centers = [json.loads(request["messages"][1]["content"]) for request in requests]
    assert (
        "".join(
            (center["passages"][0] if grouped else center)["center"]["text"] for center in centers
        )
        == text
    )
    assert "".join(fact.quote for fact in result.facts) == text
    assert result.outcome == "facts"
    assert result.usage.coverage == "complete"
    assert result.usage.total_tokens == 20 * len(requests)
    assert all(fact.passage_id == ("canonical" if grouped else None) for fact in result.facts)


@pytest.mark.parametrize("text", ["Tea.", "\\" * 7000])
def test_unsplittable_input_bound_fails_without_dispatch(text):
    requests = []
    with extraction_models({"outcome": "no_facts", "facts": []}, requests) as (models, config):
        config = config.model_copy(
            update={"extraction": config.extraction.model_copy(update={"max_input_tokens": 1})}
        )
        with pytest.raises(ExtractionError, match="input exceeds") as failure:
            extract_facts(
                models,
                chunk=TextChunk(text, 0, len(text), "", estimated_tokens(estimate_units(text))),
                config=config,
                message_role="user",
                timeout_seconds=1,
            )
    assert requests == []
    assert failure.value.usage is None


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


def test_single_passage_overflow_preserves_exact_source_quotes_and_whitespace():
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
    assert all({"start", "end"}.isdisjoint(f.model_dump()) for f in result.facts)
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
        config = config.model_copy(
            update={"index": config.index.model_copy(update={"enable_causal_graph": True})}
        )
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
        if len(members) > 1 or members[0]["center"]["text"] == "Coffee.":
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


def test_split_quotes_remain_unique_in_canonical_owner():
    repeated = "a" * 300 + ". "
    text = repeated * 2

    def respond(request):
        center = json.loads(json.loads(request.content)["messages"][1]["content"])["center"]
        if len(center["text"]) > 500:
            return response("", "length")
        return response(
            json.dumps(
                {"outcome": "facts", "facts": [{"text": "An occurrence.", "quote": center["text"]}]}
            )
        )

    with extraction_models(respond) as (models, config):
        result = extract_facts(
            models,
            chunk=TextChunk(text, 0, len(text), "", 151),
            config=config,
            message_role="user",
            timeout_seconds=1,
        )
    assert result.facts == ()
    assert result.outcome == "partial"
