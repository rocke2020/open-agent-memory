from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from oam.config import load_config
from oam.contracts.search import RecallLevel, SearchManyRequest, SearchRequest


def test_search_defaults_to_mid_without_profiles_or_generation() -> None:
    request = SearchRequest(query="What changed?")
    batch = SearchManyRequest(queries=["What changed?"])

    assert request.recall_level is RecallLevel.MID
    assert batch.recall_level is RecallLevel.MID
    assert request.profile_sections == []
    assert request.allow_partial is False


def test_search_rejects_blank_query() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="  ")


def test_mid_accepts_a_strict_time_basis_and_window() -> None:
    request = SearchRequest(
        query="What happened in March?",
        recall_level="mid",
        time_basis="event",
        time_window={"start": "2026-03-01T00:00:00Z", "end": "2026-04-01T00:00:00Z"},
    )
    assert request.recall_level.value == "mid"
    assert request.time_basis.value == "event"
    assert request.time_window.end == datetime(2026, 4, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "options",
    [
        {"time_basis": "event"},
        {"time_window": {"start": "2026-03-01T00:00:00Z", "end": "2026-04-01T00:00:00Z"}},
        {
            "time_basis": "event",
            "time_window": {"start": "2026-04-01T00:00:00Z", "end": "2026-03-01T00:00:00Z"},
        },
    ],
)
def test_temporal_request_cannot_drop_or_invert_its_strict_filter(options) -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="events", **options)


def test_search_many_rejects_the_entire_oversized_batch() -> None:
    config = load_config(Path(__file__).parents[2] / "configs" / "oam.yaml").config
    request = SearchManyRequest(queries=[str(index) for index in range(5)])
    with pytest.raises(ValueError, match="4 queries"):
        request.validate_limits(config)


def test_search_accepts_explicit_ranking_window_and_output_budget() -> None:
    request = SearchRequest(
        query="Where did Lin live in June?",
        recall_level="deep",
        time_basis="event",
        time_window={
            "start": "2026-06-01T00:00:00Z",
            "end": "2026-07-01T00:00:00Z",
        },
        max_context_tokens=1024,
        include={"chunks": {"max_tokens": 2048}},
    )

    assert request.recall_level is RecallLevel.DEEP
    assert request.time_window.start == datetime(2026, 6, 1, tzinfo=UTC)
    assert request.max_context_tokens == 1024
    assert request.include.chunks is not None
    assert request.include.chunks.max_tokens == 2048


def test_trusted_turn_identity_is_not_a_public_body_field() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="What changed?", turn_key="turn-1")


def test_caller_deadline_requires_offset_and_preserves_absolute_time():
    request = SearchRequest(query="events", deadline_at="2026-09-23T09:00:00+08:00")
    assert request.deadline_at.astimezone(UTC) == datetime(2026, 9, 23, 1, tzinfo=UTC)
    with pytest.raises(ValidationError, match="timezone-aware"):
        SearchRequest(query="events", deadline_at="2026-09-23T09:00:00")


@pytest.mark.parametrize(
    "time_window",
    [
        {"start": "2026-06-01T00:00:00", "end": "2026-07-01T00:00:00Z"},
        {"start": "2026-06-01T00:00:00Z", "end": "2026-07-01T00:00:00"},
    ],
)
def test_search_rejects_naive_ranking_window_bounds(time_window: dict[str, str]) -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        SearchRequest(query="What changed?", time_basis="event", time_window=time_window)


def test_search_rejects_an_independent_event_time_filter():
    with pytest.raises(ValidationError, match="Extra inputs"):
        SearchRequest(
            query="What changed?",
            event_time={"from": "2026-06-01T00:00:00Z", "to": "2026-07-01T00:00:00Z"},
        )


@pytest.mark.parametrize("limit", [2, 6])
def test_search_batch_limit_follows_selected_config(tmp_path: Path, limit: int) -> None:
    path = tmp_path / "operator.yaml"
    path.write_text(f"search:\n  max_queries: {limit}\n")
    config = load_config(path).config

    SearchManyRequest(queries=["q"] * limit).validate_limits(config)
    with pytest.raises(ValueError, match=f"{limit} queries"):
        SearchManyRequest(queries=["q"] * (limit + 1)).validate_limits(config)


@pytest.mark.parametrize("request_type", [SearchRequest, SearchManyRequest])
def test_search_output_budgets_follow_selected_config(tmp_path: Path, request_type: type) -> None:
    path = tmp_path / "operator.yaml"
    path.write_text(
        "context:\n  default_tokens: 10\n  max_tokens: 20\n"
        "chunks:\n  default_tokens: 30\n  max_tokens: 40\n"
    )
    config = load_config(path).config
    query = {"query": "q"} if request_type is SearchRequest else {"queries": ["q"]}

    request_type(
        **query, max_context_tokens=20, include={"chunks": {"max_tokens": 40}}
    ).validate_limits(config)
    with pytest.raises(ValueError, match="20"):
        request_type(**query, max_context_tokens=21).validate_limits(config)
    with pytest.raises(ValueError, match="40"):
        request_type(**query, include={"chunks": {"max_tokens": 41}}).validate_limits(config)
