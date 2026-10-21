from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event, Lock
from time import sleep

import httpx
import psycopg
import pytest

from oam.config import load_config
from oam.contracts.search import SearchManyRequest, SearchRequest
from oam.models.runtime import ModelResult, RuntimeModels, Usage


def test_batch_shares_one_budget_and_runs_two_queries_concurrently() -> None:
    from oam.api.search import search_many

    config = load_config("configs/oam.yaml").config
    barrier = Barrier(2)
    lock = Lock()
    calls = []

    def search_one(request, deadline_at):
        barrier.wait(timeout=2)
        with lock:
            calls.append((request.query, request.context_limit(config), deadline_at))
        return {
            "items": [{"unit_id": request.query}],
            "context": "x" * (4 * request.context_limit(config)),
            "context_tokens": request.context_limit(config),
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    response = search_many(
        config,
        SearchManyRequest(
            queries=["current city", "preferred style"],
            recall_level="fast",
        ),
        search_one,
    )

    assert response["status"] == "ok"
    assert [row["query"] for row in response["results"]] == [
        "current city",
        "preferred style",
    ]
    assert response["context_tokens"] == 18000
    assert sorted(budget for _, budget, _ in calls) == [9000, 9000]
    assert len({deadline for _, _, deadline in calls}) == 1
    assert response["diagnostics"]["generative_calls"] == 0


def test_batch_query_overlap_stays_within_configured_cap() -> None:
    from oam.api.search import search_many

    config = load_config("configs/oam.yaml").config
    lock = Lock()
    two_started = Event()
    release = Event()
    active = 0
    started = 0
    peak = 0

    def search_one(request, deadline_at):
        nonlocal active, started, peak
        with lock:
            active += 1
            started += 1
            peak = max(peak, active)
            if started == 2:
                two_started.set()
        assert release.wait(timeout=2)
        with lock:
            active -= 1
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(
            search_many,
            config,
            SearchManyRequest(queries=["one", "two", "three"], recall_level="fast"),
            search_one,
        )
        try:
            assert two_started.wait(timeout=2)
            with lock:
                assert started == 2
        finally:
            release.set()
        assert future.result(timeout=2)["status"] == "ok"
    assert started == 3
    assert peak == 2


def test_batch_splits_total_in_stable_order_without_erasing_zero_share() -> None:
    from oam.api.search import search_many

    config = load_config("configs/oam.yaml").config
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={"normal": config.search.normal.model_copy(update={"context_max_tokens": 2})}
            )
        }
    )
    calls = {}

    def search_one(request, deadline_at):
        calls[request.query] = (request.context_limit(config), request.include.chunks)
        return {
            "items": [{"unit_id": request.query, "kind": "passage"}],
            "context": "Original with citations and qualifiers",
            "context_tokens": 10,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    response = search_many(
        config,
        SearchManyRequest(
            queries=["one", "two", "three"],
            include={"chunks": False},
        ),
        search_one,
    )

    assert calls == {"one": (1, False), "two": (1, False), "three": (0, False)}
    assert response["status"] == "ok"
    assert response["context_tokens"] == 30
    assert response["tokenizer"] == "oam-unicode-quarter-v1"
    assert all(row["result"]["items"] for row in response["results"])


def test_batch_resolves_mode_default_before_splitting() -> None:
    from oam.api.search import search_many

    config = load_config("configs/oam.yaml").config
    calls = []

    def search_one(request, deadline_at):
        calls.append((request.context_limit(config), request.include.chunks))
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    search_many(
        config,
        SearchManyRequest(queries=["one", "two"]),
        search_one,
    )
    assert calls == [(16384, True), (16384, True)]


def test_invalid_later_batch_query_dispatches_nothing() -> None:
    from oam.api.search import search_many

    config = load_config("configs/oam.yaml").config
    calls = []

    def search_one(request, deadline_at):
        calls.append(request.query)
        return {}

    with pytest.raises(ValueError, match="query exceeds configured token limit"):
        search_many(
            config,
            SearchManyRequest(queries=["valid", "x" * 2050]),
            search_one,
        )
    assert calls == []


def test_batch_reports_failed_query_without_hiding_successful_empty_result() -> None:
    from oam.api.search import search_many
    from oam.retrieval.service import RecallUnavailable

    config = load_config("configs/oam.yaml").config

    def search_one(request, deadline_at):
        if request.query == "broken":
            raise RecallUnavailable("private failure details")
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    result = search_many(config, SearchManyRequest(queries=["broken", "no matches"]), search_one)

    assert result["status"] == "partial"
    assert result["results"][0]["error"] == {"code": "RecallUnavailable"}
    assert result["results"][1]["status"] == "ok"
    assert result["results"][1]["result"]["items"] == []
    assert "private failure details" not in str(result)


def test_batch_isolates_database_failure_after_authorized_dispatch() -> None:
    from oam.api.search import search_many

    config = load_config("configs/oam.yaml").config

    def search_one(request, deadline_at):
        if request.query == "database failure":
            raise psycopg.OperationalError("private database endpoint")
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    result = search_many(
        config, SearchManyRequest(queries=["database failure", "healthy"]), search_one
    )

    assert result["status"] == "partial"
    assert result["results"][0]["error"] == {"code": "RecallUnavailable"}
    assert result["results"][1]["status"] == "ok"
    assert "private database endpoint" not in str(result)


def test_batch_marks_partial_when_a_child_has_partial_coverage() -> None:
    from oam.api.search import search_many

    config = load_config("configs/oam.yaml").config

    def search_one(request, deadline_at):
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {
                "generative_calls": 0,
                "partial": request.query == "incomplete source",
            },
        }

    result = search_many(
        config,
        SearchManyRequest(queries=["incomplete source", "complete source"], allow_partial=True),
        search_one,
    )

    assert result["status"] == "partial"
    assert result["results"][0]["result"]["diagnostics"]["partial"] is True


def test_expired_child_does_not_hold_batch_response_open() -> None:
    from oam.api.search import search_many

    config = load_config("configs/oam.yaml").config
    release = Event()
    drained = Event()

    def search_one(request, deadline_at):
        if request.query == "held":
            release.wait(timeout=2)
            drained.set()
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    request = SearchManyRequest(
        queries=["ready", "held"],
        deadline_at=datetime.now(UTC) + timedelta(milliseconds=100),
    )
    with ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(search_many, config, request, search_one)
        try:
            result = future.result(timeout=0.5)
        finally:
            release.set()
    assert drained.wait(timeout=1)
    assert result["status"] == "partial"
    assert result["results"][1]["error"]["code"] == "RecallDeadlineExceeded"


def test_batch_preserves_explicit_time_window_for_each_query() -> None:
    from oam.api.search import search_many

    config = load_config("configs/oam.yaml").config
    seen = []

    def search_one(request, deadline_at):
        seen.append((request.query, request.time_basis.value, request.time_window.start))
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    request = SearchManyRequest(
        queries=["June location", "June preference"],
        time_basis="event",
        time_window={"start": "2026-06-01T00:00:00Z", "end": "2026-07-01T00:00:00Z"},
    )
    result = search_many(config, request, search_one)

    assert result["status"] == "ok"
    assert sorted(seen) == [
        ("June location", "event", request.time_window.start),
        ("June preference", "event", request.time_window.start),
    ]


def test_batch_freezes_one_implicit_time_reference_for_all_queries() -> None:
    from oam.api.search import search_many

    config = load_config("configs/oam.yaml").config
    references = []

    def search_one(request, deadline_at):
        references.append(request.reference_time)
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    search_many(config, SearchManyRequest(queries=["today", "yesterday"]), search_one)

    assert references[0] is not None
    assert references[0] == references[1]


def test_database_runtime_authorizes_batch_once_before_search(monkeypatch) -> None:
    from oam.api.runtime import DatabaseRuntime
    from oam.config import ModelsConfig
    from oam.retrieval.service import RecallService

    config = load_config("configs/oam.yaml").config.model_copy(update={"models": ModelsConfig()})
    authorizations = []
    searched = []

    class Pool:
        @contextmanager
        def connection(self):
            yield object()

    def authorize(self, connection, actor, user_id, capability):
        authorizations.append((actor, user_id, capability))
        return "authorized-lin"

    def search(self, authority, request, *, deadline_at):
        assert authority == "authorized-lin"
        searched.append(request.query)
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    monkeypatch.setattr(DatabaseRuntime, "_authorize", authorize)
    monkeypatch.setattr(RecallService, "search", search)
    runtime = DatabaseRuntime(config, Pool(), models=object())
    try:
        result = runtime.search_many(
            "caller",
            "lin",
            SearchManyRequest(queries=["current city", "preferred style"]),
        )
    finally:
        runtime._http.close()

    assert result["status"] == "ok"
    assert authorizations == [("caller", "lin", "memory:read")]
    assert sorted(searched) == ["current city", "preferred style"]


def test_delegated_search_uses_one_valid_plan_and_exposes_usage(tmp_path: Path) -> None:
    from oam.api.search import decompose_and_search

    config = _planner_config(tmp_path)
    model_calls = []
    searched = []
    budgets = {}

    def respond(request):
        model_calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"queries":["Lin city","Lin response style"]}'},
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            },
        )

    def search_one(request, deadline_at):
        searched.append((request.query, deadline_at, request.time_basis))
        budgets[request.query] = (request.context_limit(config), request.include.chunks)
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        models = RuntimeModels(config, {"MODEL_URL": "http://planner.test"}, http)
        result = decompose_and_search(
            config,
            SearchRequest(
                query="Lin city and response style",
                include={"chunks": False},
                time_basis="event",
                time_window={"start": "2026-06-01T00:00:00Z", "end": "2026-07-01T00:00:00Z"},
            ),
            search_one,
            models,
        )

    assert len(model_calls) == 1
    assert model_calls[0].url.path == "/chat/completions"
    assert sorted(query for query, _, _ in searched) == [
        "Lin city",
        "Lin city and response style",
        "Lin response style",
    ]
    assert len({deadline for _, deadline, _ in searched}) == 1
    assert all(basis.value == "event" for _, _, basis in searched)
    assert budgets == {
        "Lin city and response style": (10923, False),
        "Lin city": (10923, False),
        "Lin response style": (10922, False),
    }
    assert result["planner"]["status"] == "used"
    assert result["planner"]["calls"] == 1
    assert result["planner"]["usage"]["total_tokens"] == 20
    assert result["planner"]["cost"] is None
    assert result["fallback"] is None


def _planner_config(tmp_path: Path):
    config_path = tmp_path / "planner.yaml"
    config_path.write_text(
        "models:\n"
        "  reasoning_effort:\n"
        "    planner: none\n"
        "  vlm:\n"
        "    protocol: openai_chat\n"
        "    model: local-planner\n"
        "    model_env: null\n"
        "    endpoint_env: MODEL_URL\n"
        "    api_key_env: null\n"
        "    capabilities: [text, vision]\n",
        encoding="utf-8",
    )
    return load_config(config_path).config


def test_invalid_plan_falls_back_to_direct_query_with_measured_usage(tmp_path: Path) -> None:
    from oam.api.search import decompose_and_search

    config = _planner_config(tmp_path)
    model_calls = []
    searched = []

    def respond(request):
        model_calls.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"queries":["' + "x" * 2050 + '"]}'},
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            },
        )

    def search_one(request, deadline_at):
        searched.append((request.query, deadline_at))
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        models = RuntimeModels(config, {"MODEL_URL": "http://planner.test"}, http)
        result = decompose_and_search(
            config,
            SearchRequest(query="Lin city and response style"),
            search_one,
            models,
        )

    assert len(model_calls) == 1
    assert [query for query, _ in searched] == ["Lin city and response style"]
    assert result["planner"]["status"] == "fallback"
    assert result["planner"]["usage"]["total_tokens"] == 20
    assert result["fallback"] == {"reason": "InvalidPlan"}


def test_database_runtime_denies_delegated_request_before_planner(monkeypatch, tmp_path) -> None:
    from oam.api.runtime import DatabaseRuntime

    config = _planner_config(tmp_path)

    class Pool:
        @contextmanager
        def connection(self):
            yield object()

    class Models:
        def generate(self, *args, **kwargs):
            pytest.fail("unauthorized request dispatched the planner")

    def deny(self, connection, actor, user_id, capability):
        raise PermissionError("denied")

    monkeypatch.setattr(DatabaseRuntime, "_authorize", deny)
    runtime = DatabaseRuntime(config, Pool(), models=Models())
    try:
        with pytest.raises(PermissionError, match="denied"):
            runtime.decompose_and_search("caller", "lin", SearchRequest(query="Lin city and style"))
    finally:
        runtime._http.close()


def test_unresolved_planner_binding_falls_back_without_claiming_a_model_call(
    tmp_path: Path,
) -> None:
    from oam.api.search import decompose_and_search

    config = _planner_config(tmp_path)
    searched = []

    def forbidden(request):
        pytest.fail("unresolved binding dispatched an HTTP model call")

    def search_one(request, deadline_at):
        searched.append(request.query)
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as http:
        models = RuntimeModels(config, {}, http)
        result = decompose_and_search(
            config, SearchRequest(query="current city"), search_one, models
        )

    assert searched == ["current city"]
    assert result["planner"]["status"] == "fallback"
    assert result["planner"]["calls"] == 0
    assert result["planner"]["usage"] is None


def test_planner_capacity_timeout_before_dispatch_reports_zero_calls(tmp_path: Path) -> None:
    from oam.api.search import decompose_and_search

    config = _planner_config(tmp_path)
    searched = []

    class NoCapacity:
        def acquire(self, timeout):
            assert timeout > 0
            return False

        def release(self):
            pytest.fail("capacity was never acquired")

    def forbidden(request):
        pytest.fail("planner dispatched an HTTP request without capacity")

    def search_one(request, deadline_at):
        searched.append(request.query)
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    with httpx.Client(transport=httpx.MockTransport(forbidden)) as http:
        models = RuntimeModels(config, {"MODEL_URL": "http://planner.test"}, http)
        models._llm_slots = NoCapacity()
        result = decompose_and_search(
            config, SearchRequest(query="current city"), search_one, models
        )

    assert searched == ["current city"]
    assert result["planner"]["status"] == "fallback"
    assert result["planner"]["calls"] == 0
    assert result["diagnostics"]["planner_calls"] == 0
    assert result["diagnostics"]["generative_calls"] == 0


def test_expired_delegated_deadline_starts_no_search_after_planner(tmp_path: Path) -> None:
    from oam.api.search import decompose_and_search

    config = _planner_config(tmp_path)
    searched = []

    class SlowPlanner:
        def generate(self, role, **kwargs):
            sleep(0.05)
            return ModelResult(
                content='{"queries":["city"]}',
                usage=Usage(coverage="unavailable"),
            )

    def search_one(request, deadline_at):
        searched.append(request.query)
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    with pytest.raises(TimeoutError):
        decompose_and_search(
            config,
            SearchRequest(
                query="city",
                deadline_at=datetime.now(UTC) + timedelta(milliseconds=20),
            ),
            search_one,
            SlowPlanner(),
        )
    assert searched == []


def test_delegated_deadline_does_not_wait_for_a_stalled_planner(tmp_path: Path) -> None:
    from oam.api.search import decompose_and_search

    config = _planner_config(tmp_path)
    release = Event()
    drained = Event()
    searched = []
    model_requests = []

    def respond(request):
        model_requests.append(request)
        release.wait(timeout=2)
        drained.set()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"finish_reason": "stop", "message": {"content": '{"queries":["city"]}'}}
                ]
            },
        )

    def search_one(request, deadline_at):
        searched.append(request.query)
        return {
            "items": [],
            "context": "",
            "context_tokens": 0,
            "tokenizer": "oam-unicode-quarter-v1",
            "diagnostics": {"generative_calls": 0},
        }

    request = SearchRequest(
        query="city",
        deadline_at=datetime.now(UTC) + timedelta(milliseconds=100),
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        models = RuntimeModels(config, {"MODEL_URL": "http://planner.test"}, http)
        with ThreadPoolExecutor(max_workers=1) as caller:
            future = caller.submit(decompose_and_search, config, request, search_one, models)
            try:
                with pytest.raises(TimeoutError, match="recall deadline is exhausted"):
                    future.result(timeout=0.5)
            finally:
                release.set()
    assert drained.wait(timeout=1)
    assert len(model_requests) == 1
    assert searched == []
