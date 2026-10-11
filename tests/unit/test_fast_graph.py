"""Opt-in graph evidence reaches fast context; disabled requests dispatch no graph work."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_query_time_recall import CONFIG, DatedRepository, EmbeddingOnly

from oam.contracts.search import SearchManyRequest, SearchRequest
from oam.retrieval.service import RecallService, RecallUnavailable


class CausalRepository(DatedRepository):
    def __init__(self, request, calls, failure=False):
        super().__init__(request)
        self.calls, self.failure = calls, failure
        self.rows = {
            "effect": {"text": "I moved because my rent increased."},
            "cause": {"text": "My rent increased because the lease changed."},
            "origin": {"text": "The lease changed after its fixed term."},
        }

    def lexical(self, query, limit):
        return [{"unit_id": "effect"}]

    def dense(self, vector, limit):
        self.calls.append(("dense", limit))
        return [{"unit_id": "effect", "kind": "fact", "score": 1.0}]

    def hydrate(self, ids):
        return [{**row, "kind": "fact"} for row in super().hydrate(ids)]

    def ordinary(self, *args):
        raise AssertionError("fast must not execute entity/semantic ordinary SQL")

    def fast_graph(self, seeds, vector):
        self.calls.append(("fast_graph", tuple(seeds)))
        if self.failure:
            raise RuntimeError("causal lookup failed")
        return ("cause", "origin"), {
            "cause": [
                {
                    "arm": "graph",
                    "relation": "caused_by",
                    "nodes": ["effect", "cause"],
                    "score": 1.0,
                }
            ],
            "origin": [
                {
                    "arm": "graph",
                    "relation": "caused_by",
                    "nodes": ["effect", "cause", "origin"],
                    "score": 1.0,
                }
            ],
        }

    def valid_paths(self, paths, vector):
        self.calls.append(("valid_paths",))
        return paths


class CausalService(RecallService):
    def __init__(self, *, failure=False):
        super().__init__(
            CONFIG, SimpleNamespace(max_size=1), EmbeddingOnly(), generation_id="generation"
        )
        self.calls, self.failure = [], failure

    @contextmanager
    def _repository(self, authority, request, deadline):
        yield CausalRepository(request, self.calls, self.failure)


@pytest.mark.parametrize("options", [{}, {"fast_graph": False}])
def test_disabled_fast_keeps_direct_context_and_original_dense_cap(options):
    service = CausalService()
    result = service.search(
        object(), SearchRequest(query="why move", recall_level="fast", **options)
    )
    assert [item["unit_id"] for item in result["items"]] == ["effect"]
    assert result["diagnostics"]["executed_arms"] == ["lexical", "dense"]
    assert service.calls == [("dense", CONFIG.search.fast.channel_candidates)]


def test_enabled_fast_delivers_two_supported_causes_in_final_context():
    service = CausalService()
    result = service.search(
        object(), SearchRequest(query="why move", recall_level="fast", fast_graph=True)
    )
    assert {item["unit_id"] for item in result["items"]} == {"effect", "cause", "origin"}
    assert "I moved because my rent increased." in result["context"]
    assert "My rent increased because the lease changed." in result["context"]
    assert result["diagnostics"]["executed_arms"] == ["lexical", "dense", "graph"]
    assert result["diagnostics"]["generative_calls"] == 0
    assert ("fast_graph", ("effect",)) in service.calls


@pytest.mark.parametrize("mode", ["mid", "deep"])
def test_fast_only_option_rejects_nonfast_modes(mode):
    with pytest.raises(ValidationError, match="fast_graph"):
        SearchRequest(query="why move", recall_level=mode, fast_graph=True)


@pytest.mark.parametrize("value", ["true", 1])
def test_fast_graph_does_not_coerce_nonboolean_input(value):
    with pytest.raises(ValidationError):
        SearchRequest(query="why move", recall_level="fast", fast_graph=value)


@pytest.mark.parametrize("partial", [False, True])
def test_causal_stage_failure_obeys_existing_partial_contract(partial):
    service = CausalService(failure=True)
    request = SearchRequest(
        query="why move", recall_level="fast", fast_graph=True, allow_partial=partial
    )
    if not partial:
        with pytest.raises(RecallUnavailable, match="graph"):
            service.search(object(), request)
    else:
        result = service.search(object(), request)
        assert [item["unit_id"] for item in result["items"]] == ["effect"]
        assert result["diagnostics"]["degraded"] == ["graph"]


def test_batch_and_planner_children_keep_opt_in():
    from oam.api.search import decompose_and_search, search_many
    from oam.models.runtime import ModelResult, Usage

    requests = []

    def one(request, deadline):
        requests.append(request)
        assert request.fast_graph is True
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "diagnostics": {"generative_calls": 0},
        }

    options = {"recall_level": "fast", "fast_graph": True, "max_context_tokens": 100}
    search_many(CONFIG, SearchManyRequest(queries=["why move", "why rent"], **options), one)

    class Planner:
        def generate(self, *args, **kwargs):
            return ModelResult(
                content='{"queries":["why move"]}', usage=Usage(coverage="unavailable")
            )

    config = CONFIG.model_copy(
        update={
            "models": CONFIG.models.model_copy(
                update={
                    "reasoning_effort": CONFIG.models.reasoning_effort.model_copy(
                        update={"planner": "none"}
                    )
                }
            )
        }
    )
    decompose_and_search(config, SearchRequest(query="why move", **options), one, Planner())
    assert len(requests) == 3


@pytest.mark.parametrize("partial", [False, True])
def test_graph_work_cannot_reset_fast_deadline(monkeypatch, partial):
    import time

    real_clock = time.monotonic
    elapsed = [0.0]
    original = CausalRepository.fast_graph

    def expire(repository, seeds, vector):
        result = original(repository, seeds, vector)
        elapsed[0] = CONFIG.recall.fast_deadline_seconds + 1
        return result

    monkeypatch.setattr(CausalRepository, "fast_graph", expire)
    monkeypatch.setattr("oam.retrieval.service.time.monotonic", lambda: real_clock() + elapsed[0])
    with pytest.raises(TimeoutError):
        CausalService().search(
            object(),
            SearchRequest(
                query="why move", recall_level="fast", fast_graph=True, allow_partial=partial
            ),
        )


def test_enabled_fast_keeps_original_embedding_query_and_skips_graph_without_fact_seed(monkeypatch):
    seen = []

    class Embeddings(EmbeddingOnly):
        def embed(self, texts, **kwargs):
            seen.extend(texts)
            return super().embed(texts, **kwargs)

    monkeypatch.setattr(
        CausalRepository,
        "dense",
        lambda self, vector, limit: [{"unit_id": "effect", "kind": "passage", "score": 1.0}],
    )
    service = CausalService()
    service.models = Embeddings()
    query = "Why did I move from 10 楼 to 十层?"
    result = service.search(
        object(), SearchRequest(query=query, recall_level="fast", fast_graph=True)
    )
    assert seen == [query]
    assert "graph" not in result["diagnostics"]["executed_arms"]
    assert service.calls == []


@pytest.mark.parametrize("has_nonseed", [False, True])
def test_fast_graph_checks_scoped_raw_destinations_before_expansion(has_nonseed):
    from oam.auth.credentials import AuthorizedUser
    from oam.retrieval.repository import RecallRepository

    repository = object.__new__(RecallRepository)
    repository.authority = AuthorizedUser(
        "key", "tenant", "reader", "user", frozenset({"memory:read"})
    )
    calls = []

    def execute(sql, parameters):
        calls.append((sql, parameters))
        assert "tenant_id=%(tenant)s" in sql and "user_id=%(user)s" in sql
        assert "kind='fact'" in sql and "unit_id<>ALL(%(seeds)s::text[])" in sql
        assert parameters == {"tenant": "tenant", "user": "user", "seeds": ["seed"]}
        return [{"unit_id": "destination"}] if has_nonseed else []

    def ordinary(seeds, vector, *, include_semantic):
        assert has_nonseed and not include_semantic
        calls.append((seeds, vector))
        return ("destination",), {"destination": []}

    repository._execute = execute
    repository.ordinary = ordinary
    result = repository.fast_graph(("seed",), (1.0,))
    assert result == ((("destination",), {"destination": []}) if has_nonseed else ((), {}))
    assert len(calls) == (2 if has_nonseed else 1)
