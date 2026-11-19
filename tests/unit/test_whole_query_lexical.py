"""Mode configuration selects whole-query lexical scores without per-term fusion."""

import pytest

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.repository import RecallRepository


@pytest.mark.parametrize(
    "mode,query,entity_names,normalization",
    [
        ("fast", "observatory maintenance", [], 0),
        ("normal", "observatory maintenance", [], 2),
        (None, "observatory maintenance", [], 0),
        ("normal", "can", [{"entity_id": "named-entity", "name_text": "can"}], 0),
    ],
)
@pytest.mark.parametrize("source", ["config", "request"])
def test_configured_whole_query_returns_database_relevance_order(
    mode, query, entity_names, normalization, source
):
    base = load_config("configs/oam.yaml").config
    values = base.model_dump(mode="json")
    values["search"][mode or "normal"]["lexical_ranking"] = (
        "per_term" if source == "request" and mode else "whole_query"
    )
    values["search"]["query_entity_boost"] = 0.5 if entity_names else 0
    config = type(base).model_validate(values)
    rows = [
        {"unit_id": "specific", "score": 0.8, "strict_match": True},
        {"unit_id": "partial", "score": 0.2, "strict_match": False},
    ]

    class CapturedRepository(RecallRepository):
        def __init__(self):
            self.config = config
            self.request = (
                SearchRequest(
                    query=query,
                    recall_level=mode,
                    overrides={"lexical_ranking": "whole_query"} if source == "request" else {},
                )
                if mode
                else None
            )
            self._has_insights = False
            self.calls = []

        def _execute(self, sql, parameters=()):
            self.calls.append((sql, parameters))
            return rows

        def _query_entity_names(self, query):
            return entity_names

    repository = CapturedRepository()
    assert repository.lexical(query, 64) == rows
    assert len(repository.calls) == 1
    assert repository.calls[0][1]["limit"] == 64
    assert repository.calls[0][1]["normalization"] == normalization
