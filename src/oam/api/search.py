"""Request-level batch orchestration over the existing direct recall pipeline."""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files

import httpx
import psycopg
from pydantic import ValidationError

from oam.config import OAMConfig
from oam.contracts.search import SearchManyRequest, SearchRequest, TimeBasis
from oam.models.runtime import (
    MIN_GENERATION_OUTPUT_TOKENS,
    ModelDispatchUnavailable,
    ModelOutputError,
)
from oam.retrieval.context import CONTEXT_TOKENIZER
from oam.retrieval.query_time import infer_query_time
from oam.retrieval.service import RecallUnavailable
from oam.tokens import estimate_units, estimated_tokens

SearchOne = Callable[[SearchRequest, datetime], dict]
PLANNER_SYSTEM_TEMPLATE = (
    files("oam.api").joinpath("planner-prompt.txt").read_text(encoding="utf-8").rstrip("\n")
)


def search_many(
    config: OAMConfig,
    request: SearchManyRequest,
    search_one: SearchOne,
) -> dict:
    _validate_batch(config, request)
    deadline = _deadline(request, config.request.batch_deadline_seconds)
    return _run_batch(config, request, search_one, deadline)


def decompose_and_search(
    config: OAMConfig,
    request: SearchRequest,
    search_one: SearchOne,
    models,
) -> dict:
    request.validate_limits(config)
    _validate_queries(config, [request.query])
    if config.models.binding_for("planner") is None:
        raise NotImplementedError("the optional planner role is not configured")
    deadline = _deadline(request, config.request.delegated_deadline_seconds)
    reference_time = request.reference_time or datetime.now(UTC)
    request = request.model_copy(update={"reference_time": reference_time})
    if request.time_window is None:
        parsed = infer_query_time(
            request.query, reference_time, request.reference_timezone or "UTC"
        )
        if parsed.window is not None:
            request = request.model_copy(
                update={"time_basis": TimeBasis.EVENT, "time_window": parsed.window}
            )

    usage = None
    calls = 0
    fallback = None
    planner_skipped = config.search.max_queries == 1
    if planner_skipped:
        batch = _planned_batch(request, [request.query], deadline)
    else:
        system = PLANNER_SYSTEM_TEMPLATE.format(max_queries=config.search.max_queries - 1)
        user = json.dumps({"question": request.query}, ensure_ascii=False)
        if estimated_tokens(estimate_units(system + user)) > config.planner.max_context_tokens:
            raise ValueError("planner input exceeds configured context limit")
        try:
            generated = _call_planner_until_deadline(config, models, system, user, deadline)
        except (ModelOutputError, httpx.HTTPError, TimeoutError, ValueError) as exc:
            before_dispatch = isinstance(exc, ModelDispatchUnavailable) or (
                isinstance(exc, ValueError) and not isinstance(exc, ModelOutputError)
            )
            calls = 0 if before_dispatch else 1
            if isinstance(exc, ModelOutputError) and exc.usage is not None:
                usage = exc.usage.model_dump(mode="json")
            fallback = {"reason": "PlannerUnavailable"}
        else:
            calls = 1
            usage = generated.usage.model_dump(mode="json")
            try:
                content = json.loads(generated.content)
                if not isinstance(content, dict) or set(content) != {"queries"}:
                    raise ValueError("planner returned an invalid query object")
                queries = content["queries"]
                if not isinstance(queries, list) or not queries:
                    raise ValueError("planner returned an invalid query list")
                batch = _planned_batch(request, queries, deadline)
                _validate_batch(config, batch)
            except (ValueError, ValidationError):
                fallback = {"reason": "InvalidPlan"}

    if fallback is not None:
        batch = _planned_batch(request, [request.query], deadline)

    response = _run_batch(config, batch, search_one, deadline)
    response["plan"] = {"queries": batch.queries} if fallback is None else None
    response["planner"] = {
        "status": "skipped" if planner_skipped else "used" if fallback is None else "fallback",
        "calls": calls,
        "usage": usage,
        "cost": None,
    }
    response["fallback"] = fallback
    response["diagnostics"]["planner_calls"] = calls
    response["diagnostics"]["generative_calls"] = calls
    return response


def _call_planner_until_deadline(
    config: OAMConfig, models, system: str, user: str, deadline: datetime
):
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise TimeoutError("recall deadline is exhausted")
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oam-planner")
    future = executor.submit(
        models.generate,
        "planner",
        system=system,
        user=user,
        max_output_tokens=MIN_GENERATION_OUTPUT_TOKENS,
        timeout_seconds=min(config.model.attempt_timeout_seconds, remaining),
    )
    try:
        return future.result(timeout=max(0.0, (deadline - datetime.now(UTC)).total_seconds()))
    finally:
        executor.shutdown(wait=future.done(), cancel_futures=True)


def _planned_batch(
    request: SearchRequest, queries: list[str], deadline: datetime
) -> SearchManyRequest:
    batch = SearchManyRequest(
        queries=[request.query, *queries],
        recall_level=request.recall_level,
        fast_graph=request.fast_graph,
        include=request.include,
        allow_partial=request.allow_partial,
        deadline_at=deadline,
        time_basis=request.time_basis,
        time_window=request.time_window,
        reference_time=request.reference_time,
        reference_timezone=request.reference_timezone,
    )
    return batch.model_copy(update={"queries": list(dict.fromkeys(batch.queries))})


def _validate_queries(config: OAMConfig, queries: list[str]) -> None:
    for query in queries:
        if estimated_tokens(estimate_units(query)) > config.search.max_query_tokens:
            raise ValueError("query exceeds configured token limit")


def _validate_batch(config: OAMConfig, request: SearchManyRequest) -> None:
    request.validate_limits(config)
    _validate_queries(config, request.queries)


def _deadline(request, endpoint_seconds: float) -> datetime:
    now = datetime.now(UTC)
    deadline = min(
        now + timedelta(seconds=endpoint_seconds),
        request.deadline_at or datetime.max.replace(tzinfo=UTC),
    )
    if deadline <= datetime.now(UTC):
        raise TimeoutError("recall deadline is exhausted")
    return deadline


def _run_batch(
    config: OAMConfig,
    request: SearchManyRequest,
    search_one: SearchOne,
    deadline: datetime,
) -> dict:
    if deadline <= datetime.now(UTC):
        raise TimeoutError("recall deadline is exhausted")
    share, extra = divmod(request.context_limit(config), len(request.queries))
    budgets = [share + (index < extra) for index in range(len(request.queries))]
    reference_time = request.reference_time or datetime.now(UTC)

    def run(index: int, query: str) -> dict:
        if deadline <= datetime.now(UTC):
            raise TimeoutError("recall deadline is exhausted")
        child = SearchRequest(
            query=query,
            recall_level=request.recall_level,
            fast_graph=request.fast_graph,
            include=request.include,
            allow_partial=request.allow_partial,
            deadline_at=deadline,
            time_basis=request.time_basis,
            time_window=request.time_window,
            reference_time=reference_time,
            reference_timezone=request.reference_timezone,
        )
        child._context_max_tokens = budgets[index]
        return search_one(child, deadline)

    results = []
    executor = ThreadPoolExecutor(
        max_workers=min(config.search.query_concurrency, len(request.queries)),
        thread_name_prefix="oam-search-many",
    )
    futures = []
    try:
        futures = [
            executor.submit(run, index, query) for index, query in enumerate(request.queries)
        ]
        for index, (query, future) in enumerate(zip(request.queries, futures, strict=True)):
            try:
                remaining = max(0.0, (deadline - datetime.now(UTC)).total_seconds())
                result = future.result(timeout=remaining)
                results.append({"index": index, "query": query, "status": "ok", "result": result})
            except (RecallUnavailable, TimeoutError, psycopg.Error) as exc:
                future.cancel()
                code = (
                    "RecallDeadlineExceeded"
                    if isinstance(exc, TimeoutError)
                    else "RecallUnavailable"
                )
                results.append(
                    {"index": index, "query": query, "status": "failed", "error": {"code": code}}
                )
    finally:
        executor.shutdown(wait=all(future.done() for future in futures), cancel_futures=True)

    successes = [row for row in results if row["status"] == "ok"]
    if not successes:
        raise RecallUnavailable("every batch query failed")
    partial = len(successes) != len(results) or any(
        row["result"].get("diagnostics", {}).get("partial", False) for row in successes
    )
    return {
        "status": "partial" if partial else "ok",
        "results": results,
        "context_tokens": sum(row["result"]["context_tokens"] for row in successes),
        "tokenizer": CONTEXT_TOKENIZER,
        "diagnostics": {
            "recall_level": request.recall_level.value,
            "deadline_at": deadline.isoformat(),
            "generative_calls": 0,
            "planner_calls": 0,
        },
    }
