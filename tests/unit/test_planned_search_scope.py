from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from oam.api.search import decompose_and_search, search_many
from oam.config import load_config
from oam.contracts.search import SearchManyRequest, SearchRequest
from oam.models.runtime import ModelResult, Usage


class Planner:
    def __init__(self, queries: list[str]):
        self.queries = queries
        self.calls: list[tuple[str, dict[str, object]]] = []

    def generate(self, role, **kwargs):
        self.calls.append((role, kwargs))
        return ModelResult(
            content=json.dumps({"queries": self.queries}),
            usage=Usage(coverage="unavailable"),
        )


def planner_config():
    config = load_config("configs/oam.yaml").config
    return config.model_copy(
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


def empty_result(request, deadline):
    return {
        "items": [],
        "context": "",
        "context_tokens": 0,
        "tokenizer": "oam-unicode-quarter-v1",
        "diagnostics": {"generative_calls": 0},
    }


def test_planned_search_keeps_original_first_and_deduplicates_expansions():
    config = planner_config()
    query = "What changed in kiln K-17 between 2025-03-01 and 2025-03-05?"
    models = Planner(["initial conditions", query, "measured changes", "initial conditions"])
    result = decompose_and_search(config, SearchRequest(query=query), empty_result, models)

    assert [row["query"] for row in result["results"]] == [
        query,
        "initial conditions",
        "measured changes",
    ]
    assert result["plan"]["queries"] == [row["query"] for row in result["results"]]
    assert len(result["results"]) <= config.search.max_queries
    assert result["planner"]["calls"] == 1


def test_single_query_capacity_searches_original_without_planner():
    config = planner_config()
    config = config.model_copy(
        update={"search": config.search.model_copy(update={"max_queries": 1})}
    )
    models = Planner(["an expansion that cannot fit"])
    result = decompose_and_search(
        config, SearchRequest(query="What changed in kiln K-17?"), empty_result, models
    )

    assert models.calls == []
    assert [row["query"] for row in result["results"]] == ["What changed in kiln K-17?"]
    assert result["planner"]["calls"] == 0
    assert result["diagnostics"]["generative_calls"] == 0


def test_planned_search_preserves_original_inferred_window_for_date_free_facets():
    config = planner_config()
    seen = []

    def search_one(request, deadline):
        seen.append(request)
        return empty_result(request, deadline)

    result = decompose_and_search(
        config,
        SearchRequest(
            query="What changed in kiln K-17 from 2025-03-01 to 2025-03-05?",
            reference_time=datetime(2026, 10, 18, tzinfo=UTC),
        ),
        search_one,
        Planner(["initial conditions", "measured changes"]),
    )

    assert result["status"] == "ok"
    assert all(request.time_basis is not None for request in seen)
    assert all(request.time_basis.value == "event" for request in seen)
    assert all(request.time_window.start == datetime(2025, 3, 1, tzinfo=UTC) for request in seen)
    assert all(request.time_window.end == datetime(2025, 3, 6, tzinfo=UTC) for request in seen)
    assert len({request.reference_time for request in seen}) == 1


def test_planned_search_does_not_invent_window_for_ambiguous_original_dates():
    seen = []

    def search_one(request, deadline):
        seen.append(request)
        return empty_result(request, deadline)

    decompose_and_search(
        planner_config(),
        SearchRequest(query="Compare the inspections on 2025-03-01 and 2025-03-05"),
        search_one,
        Planner(["inspection differences"]),
    )

    assert all(request.time_window is None for request in seen)
    assert all(request.time_basis is None for request in seen)


@pytest.mark.parametrize("planned", [False, True])
def test_orchestration_uses_endpoint_deadline_instead_of_child_mode_cap(planned):
    config = planner_config()
    config = config.model_copy(
        update={
            "request": config.request.model_copy(
                update={"batch_deadline_seconds": 8, "delegated_deadline_seconds": 8}
            )
        }
    )
    deadlines = []

    def search_one(request, deadline):
        deadlines.append(deadline)
        assert request.recall_level.value == "fast"
        return empty_result(request, deadline)

    started = datetime.now(UTC)
    if planned:
        decompose_and_search(
            config,
            SearchRequest(query="What changed in kiln K-17?", recall_level="fast"),
            search_one,
            Planner(["measured changes"]),
        )
    else:
        search_many(
            config,
            SearchManyRequest(queries=["kiln changes", "fan changes"], recall_level="fast"),
            search_one,
        )

    assert len(set(deadlines)) == 1
    assert 7 < (deadlines[0] - started).total_seconds() <= 8.1


def test_planned_endpoint_preserves_shorter_caller_deadline():
    deadline = datetime.now(UTC) + timedelta(seconds=1)
    deadlines = []

    def search_one(request, deadline_at):
        deadlines.append(deadline_at)
        return empty_result(request, deadline_at)

    decompose_and_search(
        planner_config(),
        SearchRequest(query="What changed in kiln K-17?", deadline_at=deadline),
        search_one,
        Planner(["measured changes"]),
    )

    assert all(value == deadline for value in deadlines)
