"""Per-mode causal use is independent of entity graph mode and index generation."""

from datetime import UTC, datetime

import pytest
from test_fast_graph import CausalRepository, CausalService

from oam.auth.credentials import AuthorizedUser
from oam.config import ModelsConfig, load_config
from oam.contracts.search import SearchRequest
from oam.indexing.identity import index_generation
from oam.paths import default_config_path
from oam.retrieval.repository import RecallRepository


def selected_config(mode, causal):
    config = load_config(default_config_path()).config
    tier = getattr(config.search, mode).model_copy(update={"graph": "off", "causal_graph": causal})
    return config.model_copy(update={"search": config.search.model_copy(update={mode: tier})})


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("causal", [False, True])
def test_query_flag_gates_ordinary_and_temporal_causal_sql(mode, causal):
    repository = object.__new__(RecallRepository)
    repository.config = selected_config(mode, causal)
    repository.request = SearchRequest(
        query="why move",
        recall_level=mode,
        time_basis="event",
        time_window={
            "start": datetime(2026, 1, 1, tzinfo=UTC),
            "end": datetime(2027, 1, 1, tzinfo=UTC),
        },
    )
    repository.time_parameters = {}
    repository._eligible = lambda **kwargs: "WITH filtered AS (SELECT 1) "
    queries = []

    def execute(sql, parameters):
        queries.append(sql)
        return []

    repository._execute = execute
    repository.ordinary(("seed",), (1.0, 0.0), include_semantic=False)
    assert ("'caused_by'" in queries[-1]) is causal
    repository._temporal_edges(("seed",), (1.0, 0.0), limit=2)
    assert ("'caused_by'" in queries[-1]) is causal
    assert "temporal_links" in queries[-1]


class CausalOnlyRepository(CausalRepository):
    def ordinary(self, seeds, vector, *, include_semantic=True, include_entities=True):
        assert include_entities is False
        assert include_semantic is False
        return self.fast_graph(seeds, vector)


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_causal_only_recall_can_run_with_entity_graph_off(mode):
    from contextlib import contextmanager

    class Service(CausalService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield CausalOnlyRepository(request, self.calls)

    config = selected_config(mode, True)
    service = Service(config=config)
    result = service.search(
        AuthorizedUser("test", "tenant", "principal", "user", frozenset()),
        SearchRequest(query="why move", recall_level=mode),
    )
    assert {"effect", "cause", "origin"} <= {item["unit_id"] for item in result["items"]}
    assert result["diagnostics"]["graph"] == "off"
    assert result["diagnostics"]["causal_graph"] is True
    assert "graph" in result["diagnostics"]["executed_arms"]
    assert result["diagnostics"]["generative_calls"] == 0


def test_query_causal_flags_do_not_require_reindexing():
    disabled = selected_config("fast", False).model_copy(update={"models": ModelsConfig()})
    enabled = selected_config("fast", True).model_copy(update={"models": ModelsConfig()})
    assert index_generation(disabled, {}) == index_generation(enabled, {})
    indexed = disabled.model_copy(
        update={"index": disabled.index.model_copy(update={"enable_causal_graph": True})}
    )
    assert index_generation(disabled, {}) != index_generation(indexed, {})


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("causal", [False, True])
def test_revalidation_rejects_disabled_stored_causal_paths(mode, causal):
    repository = object.__new__(RecallRepository)
    repository.config = selected_config(mode, causal)
    repository.request = SearchRequest(query="why move", recall_level=mode)
    repository._execute = lambda *args: [{"unit_id": node} for node in ("effect", "cause")]
    path = {"arm": "graph", "relation": "caused_by", "nodes": ["effect", "cause"]}
    assert repository.valid_paths({"cause": [path]}, (1.0, 0.0)) == {
        "cause": [path] if causal else []
    }
