"""Configured graph defaults and request overrides reach the fast context."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_query_time_recall import CONFIG, DatedRepository, EmbeddingOnly

from oam.auth.credentials import AuthorizedUser
from oam.config import ConfigurationError, load_config
from oam.contracts.search import RecallLevel, SearchManyRequest, SearchRequest
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
    def __init__(self, *, failure=False, config=CONFIG):
        super().__init__(
            config, SimpleNamespace(max_size=1), EmbeddingOnly(), generation_id="generation"
        )
        self.calls, self.failure = [], failure

    @contextmanager
    def _repository(self, authority, request, deadline):
        yield CausalRepository(request, self.calls, self.failure)


@pytest.mark.parametrize(
    ("configured", "override", "expected"),
    [("off", None, False), ("light", None, True), ("light", False, False), ("off", True, True)],
)
def test_fast_graph_uses_yaml_default_unless_request_overrides(
    tmp_path: Path, configured, override, expected
):
    path = tmp_path / "oam.yaml"
    path.write_text(f'search:\n  fast:\n    graph: "{configured}"\n')
    service = CausalService(config=load_config(path).config)
    options = {} if override is None else {"fast_graph": override}
    result = service.search(
        AuthorizedUser("test", "tenant", "principal", "user", frozenset()),
        SearchRequest(query="why move", recall_level=RecallLevel.FAST, **options),
    )
    assert {item["unit_id"] for item in result["items"]} == (
        {"effect", "cause", "origin"} if expected else {"effect"}
    )
    assert ("graph" in result["diagnostics"]["executed_arms"]) is expected


@pytest.mark.parametrize("planned", [False, True])
@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        ("off", {"effect"}),
        ("light", {"effect", "cause", "origin"}),
        ("full", {"effect", "cause", "origin", "semantic"}),
    ],
)
def test_batch_and_planner_inherit_mode_graph_config(
    tmp_path: Path, planned, mode, graph, expected
):
    from oam.api.search import decompose_and_search, search_many
    from oam.models.runtime import ModelResult, Usage

    path = tmp_path / "oam.yaml"
    path.write_text(f'search:\n  {mode}:\n    graph: "{graph}"\n')
    config = load_config(path).config
    service = GraphService(config=config)

    def one(request, deadline):
        return service.search(object(), request, deadline_at=deadline)

    if planned:

        class Planner:
            def generate(self, *args, **kwargs):
                return ModelResult(
                    content='{"queries":["why move"]}', usage=Usage(coverage="unavailable")
                )

        config = config.model_copy(
            update={
                "models": config.models.model_copy(
                    update={
                        "reasoning_effort": config.models.reasoning_effort.model_copy(
                            update={"planner": "none"}
                        )
                    }
                )
            }
        )
        result = decompose_and_search(
            config, SearchRequest(query="why move", recall_level=mode), one, Planner()
        )
    else:
        result = search_many(
            config,
            SearchManyRequest(queries=["why move"], recall_level=mode),
            one,
        )
    assert result["status"] == "ok"
    child = result["results"][0]["result"]
    assert {item["unit_id"] for item in child["items"]} == expected
    assert child["diagnostics"]["graph"] == graph
    if planned:
        assert result["planner"]["status"] == "used"


@pytest.mark.parametrize("value", [False, True, "false"])
def test_config_rejects_removed_global_fast_graph(tmp_path: Path, value):
    path = tmp_path / "oam.yaml"
    import yaml

    path.write_text(yaml.safe_dump({"search": {"fast_graph": value}}))
    with pytest.raises(ConfigurationError, match="fast_graph"):
        load_config(path)


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("value", [False, True, "auto", "false"])
def test_graph_config_rejects_invalid_selections(tmp_path: Path, mode, value):
    import yaml

    path = tmp_path / "oam.yaml"
    path.write_text(yaml.safe_dump({"search": {mode: {"graph": value}}}))
    with pytest.raises(ConfigurationError, match="graph"):
        load_config(path)


class GraphRepository(CausalRepository):
    def __init__(self, *args):
        super().__init__(*args)
        self.rows["semantic"] = {"text": "A related moving announcement."}

    def ordinary(self, seeds, vector, *, include_semantic=True):
        self.calls.append(("ordinary", include_semantic))
        ranks, paths = super().fast_graph(seeds, vector)
        if include_semantic:
            ranks = (*ranks, "semantic")
            paths["semantic"] = [
                {"arm": "graph", "relation": "semantic", "nodes": ["effect", "semantic"]}
            ]
        return ranks, paths


class GraphService(CausalService):
    @contextmanager
    def _repository(self, authority, request, deadline):
        yield GraphRepository(request, self.calls, self.failure)


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        ("off", {"effect"}),
        ("light", {"effect", "cause", "origin"}),
        ("full", {"effect", "cause", "origin", "semantic"}),
    ],
)
def test_mode_graph_config_controls_final_context(tmp_path: Path, caplog, mode, graph, expected):
    caplog.set_level("INFO", logger="oam.retrieval.service")
    path = tmp_path / "oam.yaml"
    path.write_text(f'search:\n  {mode}:\n    graph: "{graph}"\n')
    service = GraphService(config=load_config(path).config)
    result = service.search(
        AuthorizedUser("test", "tenant", "principal", "user", frozenset()),
        SearchRequest(query="why move", recall_level=mode),
    )

    assert {item["unit_id"] for item in result["items"]} == expected
    assert "I moved because my rent increased." in result["context"]
    assert ("My rent increased because the lease changed." in result["context"]) == (graph != "off")
    assert ("A related moving announcement." in result["context"]) == (graph == "full")
    assert result["diagnostics"]["graph"] == graph
    assert ("graph" in result["diagnostics"]["executed_arms"]) == (graph != "off")
    assert "dense" in result["diagnostics"]["executed_arms"]
    assert result["diagnostics"]["generative_calls"] == 0
    assert result["context_tokens"] <= service.config.context.default_tokens
    assert f"graph={graph}" in caplog.text
    assert result["trace_id"] in caplog.text


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_canonical_graph_defaults_reach_final_context(mode):
    result = GraphService().search(object(), SearchRequest(query="why move", recall_level=mode))
    assert {item["unit_id"] for item in result["items"]} == (
        {"effect"} if mode == "fast" else {"effect", "cause", "origin"}
    )


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (None, {"effect", "cause", "origin", "semantic"}),
        (False, {"effect"}),
        (True, {"effect", "cause", "origin"}),
    ],
)
def test_fast_request_boolean_overrides_configured_full_graph(tmp_path: Path, override, expected):
    path = tmp_path / "oam.yaml"
    path.write_text("search:\n  fast:\n    graph: full\n")
    service = GraphService(config=load_config(path).config)
    result = service.search(
        AuthorizedUser("test", "tenant", "principal", "user", frozenset()),
        SearchRequest(query="why move", recall_level=RecallLevel.FAST, fast_graph=override),
    )
    assert {item["unit_id"] for item in result["items"]} == expected


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


@pytest.mark.parametrize("mode", ["normal"])
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
