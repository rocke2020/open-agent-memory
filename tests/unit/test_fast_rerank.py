"""Recall scoring is exclusive to Normal, regardless of model binding."""

from contextlib import contextmanager

import pytest
from test_local_evidence import fact, original
from test_query_time_recall import CONFIG, DatedRepository, DatedService, EmbeddingOnly
from test_source_context import SourceRepository, SourceService

from oam.contracts.search import SearchRequest
from oam.models.runtime import RerankResult, Usage


@pytest.mark.parametrize(
    ("source_seeds", "channel_limit", "keep_reply"),
    [(2, 2, True), (1, 2, False), (2, 1, False)],
)
def test_output_cap_does_not_reduce_the_configured_reply_witness_budget(
    source_seeds, channel_limit, keep_reply
):
    parent = original("z_parent", 4)
    witness = fact("z_witness", parent)
    reply = original("reply_body", 5, "assistant")
    reply["text"] = "Try a short trail away from busy parks."
    reply_fact = fact("reply_fact", reply)
    leading = original("leading_original", 20)
    rows = [parent, witness, reply, reply_fact, leading]
    for row in rows:
        if row["kind"] == "passage":
            row["locator"] = row["citations"][0]

    class Repository(SourceRepository):
        def dense(self, vector, limit):
            return [
                {
                    "unit_id": parent["unit_id"],
                    "kind": "passage",
                    "score": 0.9,
                    "source_id": "conversation",
                    "source_version": 1,
                    "passage_id": parent["unit_id"],
                },
                {"unit_id": witness["unit_id"], "kind": "fact", "score": 0.8},
            ][:limit]

        def lexical(self, query, limit):
            return [
                {"unit_id": node, "strict_match": False}
                for node in ("reply_fact", "leading_original")
            ][:limit]

    class Service(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield Repository(request, self.rows, {})

    values = CONFIG.model_dump(mode="json")
    values["search"]["partial_match_gate"] = "user_fact_exchange"
    values["search"]["fast"].update(
        max_results=1,
        source_seed_results=source_seeds,
        channel_candidates=channel_limit,
    )
    service = Service(rows, config=type(CONFIG).model_validate(values))
    result = service.search(
        object(), SearchRequest(query="Which activity suits my preference?", recall_level="fast")
    )
    assert (reply["text"] in result["context"]) is keep_reply
    if keep_reply:
        selected = {item["unit_id"]: item for item in result["items"]}
        assert selected["reply_body"]["text"] == reply["text"]
        assert selected["reply_body"]["citations"] == reply["citations"]
    assert sum(item["kind"] == "fact" for item in result["items"]) <= 1
    assert sum(item["kind"] == "passage" for item in result["items"]) <= 1
    assert result["diagnostics"]["generative_calls"] == 0


class DistinctRepository(DatedRepository):
    def __init__(self, request):
        super().__init__(request)
        self.rows["new"]["text"] = "The newer apple harvest was excellent."


class DistinctService(DatedService):
    @contextmanager
    def _repository(self, authority, request, deadline):
        yield DistinctRepository(request)


@pytest.mark.parametrize(
    ("mode", "selected", "expected_documents"),
    [
        ("fast", ["old", "new"], []),
        (
            "normal",
            ["new", "old"],
            [("Apple harvest was good.", "The newer apple harvest was excellent.")],
        ),
        (
            "deep",
            ["new", "old"],
            [("Apple harvest was good.", "The newer apple harvest was excellent.")],
        ),
    ],
)
def test_configured_reranker_runs_in_normal_and_deep(mode, selected, expected_documents):
    documents_scored = []

    class Scorer(EmbeddingOnly):
        def rerank(self, query, documents, *, timeout_seconds):
            assert query == "apple"
            assert timeout_seconds > 0
            documents_scored.append(documents)
            return RerankResult(scores=(0.1, 0.8), usage=Usage())

    values = CONFIG.model_dump(mode="json")
    values["models"]["reranker"] = {
        "protocol": "openai_compatible",
        "model": "synthetic-reranker",
        "endpoint_env": "TEST_RERANK_ENDPOINT",
        "capabilities": ["rerank"],
    }
    config = type(CONFIG).model_validate(values)
    result = DistinctService(config, None, Scorer(), generation_id="generation").search(
        object(), SearchRequest(query="apple", recall_level=mode)
    )
    assert [item["unit_id"] for item in result["items"]] == selected
    assert documents_scored == expected_documents
    assert ("rerank" in result["diagnostics"]["executed_stages"]) is bool(expected_documents)
