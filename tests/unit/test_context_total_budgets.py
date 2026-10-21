"""Recall budgets cover the actual answer context, with independent raw evidence."""

from contextlib import contextmanager
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError
from test_query_time_recall import CONFIG
from test_source_context import SourceRepository, SourceService
from test_source_context import evidence_rows as evidence_rows

from oam.api.app import create_app
from oam.api.search import search_many
from oam.contracts.search import SearchManyRequest, SearchRequest
from oam.retrieval.context import context_token_count


@pytest.mark.parametrize(
    "request_type,query",
    [(SearchRequest, {"query": "launch"}), (SearchManyRequest, {"queries": ["launch"]})],
)
def test_chunks_default_to_enabled(request_type, query):
    assert request_type(**query).include.chunks is True


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}, {"max_tokens": 100}])
def test_chunks_reject_non_boolean_values(value):
    with pytest.raises(ValidationError):
        SearchRequest(query="launch", include={"chunks": value})


def test_removed_fact_budget_is_rejected():
    with pytest.raises(ValidationError):
        SearchRequest(query="launch", max_tokens=100)


def ranked_service(rows, mode="normal", budget=2048, **tier_options):
    class RankedRepository(SourceRepository):
        def lexical(self, query, limit):
            return [{"unit_id": row["unit_id"]} for row in rows][:limit]

    class RankedService(SourceService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield RankedRepository(request, self.rows, self.graph_paths)

    tier = getattr(CONFIG.search, mode).model_copy(
        update={"context_max_tokens": budget, **tier_options}
    )
    config = CONFIG.model_copy(update={"search": CONFIG.search.model_copy(update={mode: tier})})
    return RankedService(rows, config=config)


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize(
    "answer", ["Mira Chen directs the observatory.", "The repair shop is on Willow Street."]
)
def test_extreme_budget_preserves_answer_only_in_original(evidence_rows, mode, answer):
    original = deepcopy(evidence_rows[2])
    original["text"] = answer + " Background." * 550
    lower_fact = deepcopy(evidence_rows[0])
    lower_fact["text"] = "An unrelated detail. " * 200
    lower_fact["citations"][0]["span"]["quote"] = answer
    service = ranked_service([original, lower_fact], mode)
    result = service.search(object(), SearchRequest(query="source detail", recall_level=mode))

    assert [item["unit_id"] for item in result["items"]] == ["original"]
    assert result["items"][0]["text"] == original["text"]
    assert answer in result["context"]
    assert 0 < result["context_tokens"] <= 2048
    assert result["context_tokens"] == context_token_count(result["context"])


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_oversized_original_is_omitted_whole(evidence_rows, mode):
    original = deepcopy(evidence_rows[2])
    original["text"] = "Evidence." * 1100
    result = ranked_service([original], mode).search(
        object(), SearchRequest(query="source detail", recall_level=mode)
    )
    assert result["items"] == []
    assert result["context"] == ""
    assert result["context_tokens"] == 0


def test_total_counts_citation_quotes_and_metadata(evidence_rows):
    fact = deepcopy(evidence_rows[0])
    fact["citations"][0]["span"]["quote"] = "Evidence " * 200
    original = deepcopy(evidence_rows[2])
    original["text"] = fact["citations"][0]["span"]["quote"]
    second = {**deepcopy(fact), "unit_id": "fact2"}
    result = ranked_service([fact, second, original], budget=100).search(
        object(), SearchRequest(query="workshop", include={"chunks": False})
    )
    assert result["items"] == []
    assert result["context_tokens"] == 0


def test_false_omits_original_bodies_without_inventing_a_fact(evidence_rows):
    result = ranked_service([evidence_rows[2]]).search(
        object(), SearchRequest(query="workshop", include={"chunks": False})
    )
    assert result["items"] == []
    assert result["context"] == ""


def test_fact_and_source_share_total_and_deduplicate(evidence_rows):
    second = deepcopy(evidence_rows[0])
    second.update(unit_id="fact2", text="An extra shift conflicted with the workshop.")
    result = ranked_service([evidence_rows[0], second, evidence_rows[2]]).search(
        object(), SearchRequest(query="workshop")
    )
    assert {item["unit_id"] for item in result["items"]} == {"fact", "fact2", "original"}
    assert result["context"].count(evidence_rows[2]["text"]) == 1
    assert result["context_tokens"] <= 2048


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_only_fact_retains_its_full_oversized_bundle(evidence_rows, mode):
    fact, original = deepcopy(evidence_rows[0]), deepcopy(evidence_rows[2])
    original["text"] += " Background." * 1000
    result = ranked_service([fact, original], mode).search(
        object(), SearchRequest(query="workshop", recall_level=mode)
    )
    assert {item["unit_id"] for item in result["items"]} == {"fact", "original"}
    assert (
        next(item for item in result["items"] if item["kind"] == "passage")["text"]
        == original["text"]
    )
    assert result["context_tokens"] > 2048
    assert result["diagnostics"]["context_budget"]["oversized_single_fact"] is True
    assert (
        result["diagnostics"]["context_budget"]["overrun_tokens"] == result["context_tokens"] - 2048
    )


def test_two_oversized_facts_have_no_singleton_exception(evidence_rows):
    fact, original = deepcopy(evidence_rows[0]), deepcopy(evidence_rows[2])
    second = {**deepcopy(fact), "unit_id": "fact2"}
    original["text"] += " Background." * 1000
    result = ranked_service([fact, second, original]).search(
        object(), SearchRequest(query="workshop")
    )
    assert result["items"] == []


def test_zero_internal_share_has_no_singleton_exception(evidence_rows):
    request = SearchRequest(query="workshop")
    request._context_max_tokens = 0
    result = ranked_service([evidence_rows[0], evidence_rows[2]]).search(object(), request)
    assert result["items"] == []


@pytest.mark.parametrize("mode,expected_count", [("fast", 5), ("normal", 6)])
def test_native_mode_ceiling_controls_actual_original_admission(
    evidence_rows, mode, expected_count
):
    originals = []
    for index in range(6):
        row = deepcopy(evidence_rows[2])
        row.update(unit_id=f"original{index}", text="x" * 14000)
        row["citations"][0].update(message_id=str(index), message_index=index)
        originals.append(row)
    budget = getattr(CONFIG.search, mode).context_max_tokens
    result = ranked_service(originals, mode, budget=budget).search(
        object(), SearchRequest(query="original detail", recall_level=mode)
    )
    assert [row["unit_id"] for row in result["items"]] == [
        f"original{i}" for i in range(expected_count)
    ]
    assert result["context_tokens"] <= budget
    assert result["diagnostics"]["context_budget"]["oversized_single_fact"] is False


@pytest.mark.parametrize("mode", ["fast", "normal"])
async def test_http_uses_real_recall_packing_and_batch_shares(
    evidence_rows, mode, tmp_path, caplog
):
    original = deepcopy(evidence_rows[2])
    original["text"] = "Mira Chen directs the observatory." + " Background." * 550
    service = ranked_service([original], mode)

    class FixtureRuntime:
        def authenticate(self, actor):
            pass

        def search(self, actor, user_id, body):
            return service.search(object(), body)

        def search_many(self, actor, user_id, body):
            return search_many(
                service.config,
                body,
                lambda child, deadline: service.search(object(), child, deadline_at=deadline),
            )

    config_path = tmp_path / "oam.yaml"
    config_path.write_text(f"search:\n  {mode}:\n    context_max_tokens: 2048\n")
    app = create_app(config_path, runtime=FixtureRuntime())
    caplog.set_level("INFO", logger="oam.retrieval.service")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer synthetic-fixture"},
    ) as client:
        response = await client.post(
            "/v1/recall",
            json={"user_id": "scope", "query": "observatory director", "recall_level": mode},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert original["text"] in payload["context"]
        assert payload["items"][0]["text"] == original["text"]
        assert 1024 < payload["context_tokens"] <= 2048
        hidden = await client.post(
            "/v1/recall",
            json={
                "user_id": "scope",
                "query": "observatory director",
                "recall_level": mode,
                "include": {"chunks": False},
            },
        )
        assert hidden.status_code == 200, hidden.text
        assert hidden.json()["items"] == []
        batch = await client.post(
            "/v1/users/scope/search-many",
            json={"queries": ["director", "observatory"], "recall_level": mode},
        )
        assert batch.status_code == 200, batch.text
        assert batch.json()["context_tokens"] == 0
        assert [
            row["result"]["diagnostics"]["context_budget"]["limit"]
            for row in batch.json()["results"]
        ] == [1024, 1024]
    assert "include_chunks=True" in caplog.text
    assert "include_chunks=False" in caplog.text
    assert "context_tokens=" in caplog.text
