"""Source-guided text Resource retrieval through the recall service."""

from contextlib import contextmanager

import pytest
from test_query_time_recall import CONFIG, DatedRepository, EmbeddingOnly

from oam.config import ModelBinding
from oam.contracts.search import SearchRequest
from oam.models.runtime import RerankResult, Usage
from oam.retrieval.context import assemble_context, context_token_count, render_context
from oam.retrieval.service import RecallService

TEST_QUERY = "What is the binding clause?"


def resource(unit_id, source_id, ordinal, text):
    citation = {"source_id": source_id, "source_version": 1, "block": f"text:{ordinal}"}
    return {
        "unit_id": unit_id,
        "kind": "resource_passage",
        "origin": "evidence",
        "text": text,
        "citations": [citation],
        "location": citation,
        "time_evidence": [],
        "paths": [],
    }


class ResourceRepository(DatedRepository):
    expected_query = TEST_QUERY

    def __init__(self, mixed=False):
        super().__init__(None)
        self.rows = {
            "global": resource("global", "general", 0, "General discussion."),
            "rescue": resource("rescue", "focused", 6, "The binding clause."),
            "neighbor": resource("neighbor", "focused", 5, "The clause applies to the account."),
        }
        if mixed:
            self.rows["global"]["kind"] = "passage"
        self.trace = None
        self.document_calls = 0

    def lexical(self, query, limit):
        return [{"unit_id": "global"}]

    def dense(self, vector, limit):
        return [
            {
                "unit_id": "global",
                "kind": self.rows["global"]["kind"],
                "score": 0.8,
                "source_id": "general",
                "source_version": 1,
                "passage_id": "global",
            }
        ]

    def hydrate(self, unit_ids):
        return [self.rows[unit_id] for unit_id in unit_ids if unit_id in self.rows]

    def resource_document_rank(self, query, generation_id, limit):
        assert query == self.expected_query
        assert generation_id == "generation"
        self.document_calls += 1
        return ["focused"]

    def resource_clause_rank(self, query, generation_id, source_ids, limit):
        assert source_ids[0] == "focused"
        return {"focused": ["rescue"]}

    def resource_source_openings(self, unit_ids, generation_id):
        return {
            unit_id: (
                self.rows[unit_id]["citations"][0]["source_id"],
                "Opening material for ranking only.",
            )
            for unit_id in unit_ids
            if self.rows[unit_id]["kind"] == "resource_passage"
        }

    def resource_neighbors(self, unit_ids, generation_id):
        assert "rescue" in unit_ids
        return {"rescue": ["neighbor", "rescue"], "global": ["global"]}

    def record_trace(self, trace_id, policy_epoch, decisions, result):
        self.trace = decisions
        return super().record_trace(trace_id, policy_epoch, decisions, result)


class Reranker(EmbeddingOnly):
    expected_query = TEST_QUERY

    def __init__(self):
        self.documents = ()

    def rerank(self, query, documents, *, timeout_seconds):
        assert query == self.expected_query
        self.documents = documents
        return RerankResult(
            scores=tuple(float("The binding clause." in body) for body in documents),
            usage=Usage(input_tokens=1, total_tokens=1),
        )


class ResourceService(RecallService):
    def __init__(self, *, guidance, mixed=False):
        search = CONFIG.search.model_copy(
            update={
                "resource_document_guidance": guidance,
                "normal": CONFIG.search.normal.model_copy(
                    update={"max_results": 1, "rerank_candidates": 3}
                ),
            }
        )
        model = ModelBinding(
            model="test-reranker",
            protocol="openai_compatible",
            endpoint_env="TEST_RERANK_ENDPOINT",
            capabilities=frozenset({"rerank"}),
        )
        settings = CONFIG.model_copy(
            update={
                "search": search,
                "models": CONFIG.models.model_copy(update={"reranker": model}),
            }
        )
        self.repo = ResourceRepository(mixed=mixed)
        super().__init__(settings, None, Reranker(), generation_id="generation")

    @contextmanager
    def _repository(self, authority, request, deadline):
        yield self.repo


def recall(service, mode="normal", chunks=True, allow_partial=False, query=TEST_QUERY):
    return service.search(
        object(),
        SearchRequest(
            query=query,
            recall_level=mode,
            include={"chunks": chunks},
            allow_partial=allow_partial,
        ),
    )


def test_reranked_rescue_outside_fusion_reaches_native_context_with_eligible_neighbor():
    service = ResourceService(guidance=True)
    result = recall(service)
    assert service.repo.document_calls == 1
    assert service.models.documents[1] == (
        "Source opening: Opening material for ranking only.\nPassage: The binding clause."
    )
    assert [v["unit_id"] for v in result["items"]] == ["rescue"]
    assert result["items"][0]["text"] == (
        "The clause applies to the account.\n\nThe binding clause."
    )
    assert [c["block"] for c in result["items"][0]["citations"]] == ["text:5", "text:6"]
    assert "Opening material" not in result["context"]
    assert result["context_tokens"] == context_token_count(result["context"])
    assert "resource_neighbors" in result["diagnostics"]["executed_stages"]
    assert result["diagnostics"]["timing_ms"]["resource_document_rank"] >= 0
    assert [row["unit_id"] for row in service.repo.trace["fusion"]] == ["global"]
    assert service.repo.trace["resource_rerank_ids"] == ["global", "rescue"]
    assert result["items"][0]["paths"][0]["nodes"] == ["neighbor", "rescue"]


@pytest.mark.parametrize(
    ("guidance", "mode", "chunks", "mixed"),
    [
        (False, "normal", True, False),
        (True, "fast", True, False),
        (True, "deep", True, False),
        (True, "normal", False, False),
        (True, "normal", True, True),
    ],
)
def test_other_recall_paths_do_not_fetch_document_candidates(guidance, mode, chunks, mixed):
    service = ResourceService(guidance=guidance, mixed=mixed)
    result = recall(service, mode, chunks)
    assert service.repo.document_calls == 0
    assert all(v["unit_id"] != "rescue" for v in result["items"])
    assert "resource_rerank_ids" not in service.repo.trace
    if mode == "fast":
        assert not service.models.documents


def test_resource_window_falls_back_to_seed_when_complete_window_does_not_fit():
    seed = resource("seed", "doc", 1, "Small binding clause.")
    neighbor = resource("neighbor", "doc", 0, "Too long. " * 500)
    budget = context_token_count(render_context([seed]))
    items, _, _, _ = assemble_context(
        [seed],
        [],
        context_max_tokens=budget,
        include_chunks=True,
        max_results=5,
        resource_windows={"seed": [neighbor, seed]},
    )
    assert items == [seed]


def test_resource_window_keeps_distinct_passage_time_evidence_separate():
    service = ResourceService(guidance=True)
    service.repo.rows["neighbor"]["time_evidence"] = [{"basis": "event", "start": "2024-01-01"}]
    result = recall(service)
    assert result["items"][0]["text"] == "The binding clause."
    assert [c["block"] for c in result["items"][0]["citations"]] == ["text:6"]


def test_resource_window_stops_at_a_gap_in_eligible_passages():
    seed = resource("seed", "doc", 4, "The marked clause.")
    earlier = resource("earlier", "doc", 2, "Earlier text across the gap.")
    following = resource("following", "doc", 5, "The next paragraph.")
    later = resource("later", "doc", 6, "Its continuation.")
    items, _, _, _ = assemble_context(
        [seed],
        [],
        context_max_tokens=4096,
        include_chunks=True,
        max_results=5,
        resource_windows={"seed": [earlier, seed, following, later]},
    )
    assert items[0]["text"] == "The marked clause.\n\nThe next paragraph.\n\nIts continuation."
    assert [c["block"] for c in items[0]["citations"]] == ["text:4", "text:5", "text:6"]


def test_neighbor_lookup_failure_keeps_seed_in_partial_recall(monkeypatch):
    service = ResourceService(guidance=True)

    def fail_lookup(*_args):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(service.repo, "resource_neighbors", fail_lookup)
    result = recall(service, allow_partial=True)
    assert result["items"][0]["text"] == "The binding clause."
    assert "resource_neighbors" in result["diagnostics"]["degraded"]
