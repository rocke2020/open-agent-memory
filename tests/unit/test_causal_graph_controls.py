"""Causal traversal is independent of entity/semantic and temporal retrieval."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from test_query_time_recall import EmbeddingOnly, SupportedFactsRepository

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.repository import RecallRepository
from oam.retrieval.service import RecallService


def configuration(tmp_path, mode, graph, causal):
    path = tmp_path / "independent.yaml"
    path.write_text(
        f'search:\n  {mode}:\n    graph: "{graph}"\n    causal_graph: {str(causal).lower()}\n'
    )
    return load_config(path).config


class IndependentRepository(SupportedFactsRepository):
    def __init__(self, request, calls):
        super().__init__(request)
        self.calls = calls

    def lexical(self, query, limit):
        return [{"unit_id": "seed"}]

    def dense(self, vector, limit):
        return [{"unit_id": "seed", "kind": "fact", "score": 1.0}]

    def fast_graph(self, seeds, vector, *, include_entities=True, include_causal=False):
        return self.ordinary(
            seeds,
            vector,
            include_semantic=False,
            include_entities=include_entities,
            include_causal=include_causal,
        )

    def ordinary(
        self, seeds, vector, *, include_semantic=True, include_entities=True, include_causal=True
    ):
        self.calls.append((include_entities, include_semantic, include_causal))
        return tuple(
            node
            for node, enabled in (
                ("entity", include_entities),
                ("semantic", include_semantic),
                ("cause", include_causal),
            )
            if enabled
        ), {}


class IndependentService(RecallService):
    def __init__(self, config):
        super().__init__(config, None, EmbeddingOnly(), generation_id="generation")
        self.calls = []

    @contextmanager
    def _repository(self, authority, request, deadline):
        yield IndependentRepository(request, self.calls)


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("graph", ["off", "light", "full"])
@pytest.mark.parametrize("causal", [False, True])
def test_independent_controls_reach_the_actual_answer_context(tmp_path, mode, graph, causal):
    config = configuration(tmp_path, mode, graph, causal)
    service = IndependentService(config)
    result = service.search(
        object(), SearchRequest(query="history", recall_level=mode, include={"chunks": False})
    )
    expected = {"seed"}
    if graph != "off":
        expected.add("entity")
    if graph == "full":
        expected.add("semantic")
    if causal:
        expected.add("cause")
    assert {item["unit_id"] for item in result["items"]} == expected
    assert result["diagnostics"]["causal_graph"] is causal
    assert ("graph" in result["diagnostics"]["executed_arms"]) is (graph != "off" or causal)


@pytest.mark.parametrize("override", [False, True])
def test_fast_override_does_not_override_causal_control(tmp_path, override):
    service = IndependentService(configuration(tmp_path, "fast", "full", True))
    result = service.search(
        object(),
        SearchRequest(
            query="history", recall_level="fast", fast_graph=override, include={"chunks": False}
        ),
    )
    expected = {"seed", "cause", "entity"} if override else {"seed", "cause"}
    assert {item["unit_id"] for item in result["items"]} == expected


def test_causal_only_preserves_the_larger_graph_seed_pool(tmp_path, monkeypatch):
    config = configuration(tmp_path, "fast", "off", True)
    tier = config.search.fast.model_copy(update={"channel_candidates": 1})
    config = config.model_copy(
        update={"search": config.search.model_copy(update={"fast": tier, "graph_seed_max": 3})}
    )

    def dense(repository, vector, limit):
        assert limit == 3
        return [{"unit_id": f"seed{i}", "kind": "fact", "score": 1.0} for i in range(limit)]

    def causal(repository, seeds, vector, **selection):
        assert seeds == ("seed0", "seed1", "seed2")
        assert selection == {"include_entities": False, "include_causal": True}
        return ("cause",), {}

    monkeypatch.setattr(IndependentRepository, "dense", dense)
    monkeypatch.setattr(IndependentRepository, "fast_graph", causal)
    result = IndependentService(config).search(
        object(), SearchRequest(query="history", recall_level="fast", include={"chunks": False})
    )
    assert "cause" in {item["unit_id"] for item in result["items"]}


class CapturedRepository(RecallRepository):
    def __init__(self, config, request):
        self.config, self.request = config, request
        self.authority = SimpleNamespace(tenant_id="tenant", user_id="user")
        self._has_insights = False
        self.time_parameters, self.statements = {}, []

    def _execute(self, sql, parameters=()):
        self.statements.append(sql)
        return []


@pytest.mark.parametrize("causal", [False, True])
def test_temporal_search_keeps_temporal_edges_and_optionally_causal_edges(tmp_path, causal):
    config = configuration(tmp_path, "fast", "off", causal)
    request = SearchRequest(
        query="history",
        recall_level="fast",
        time_basis="mentioned",
        time_window={"start": "2025-01-01T00:00:00Z", "end": "2025-02-01T00:00:00Z"},
    )
    repository = CapturedRepository(config, request)
    repository.temporal_neighbors_batch(("seed",), (1.0,), 4)
    repository.valid_paths(
        {"target": [{"arm": "temporal", "nodes": ["seed", "target"], "edge_ids": ["edge"]}]},
        (1.0,),
    )
    assert all("temporal_links" in sql for sql in repository.statements if "edge_id" in sql)
    assert all(
        ("FROM unit_links" in sql) is causal for sql in repository.statements if "edge_id" in sql
    )


@pytest.mark.parametrize("entities", [False, True])
@pytest.mark.parametrize("causal", [False, True])
def test_disabled_graph_branches_are_absent_from_sql(tmp_path, entities, causal):
    config = configuration(tmp_path, "fast", "full" if entities else "off", causal)
    repository = CapturedRepository(config, SearchRequest(query="history", recall_level="fast"))
    repository.ordinary(
        ("seed",),
        (1.0,),
        include_entities=entities,
        include_semantic=entities,
        include_causal=causal,
    )
    sql = "\n".join(repository.statements)
    assert ("causal_rows" in sql) is causal
    assert ("first_edges" in sql) is causal
    assert ("entity_candidates" in sql) is entities
    assert ("semantic_rows" in sql) is entities
    if not causal:
        assert "caused_by" not in sql
    if not entities:
        assert "unit_entities" not in sql and "entity_name_support" not in sql
