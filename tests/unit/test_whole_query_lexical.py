"""Mode configuration selects whole-query lexical scores without per-term fusion."""

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.repository import RecallRepository


def test_configured_whole_query_returns_database_relevance_order():
    base = load_config("configs/oam.yaml").config
    values = base.model_dump(mode="json")
    values["search"]["fast"]["lexical_ranking"] = "whole_query"
    config = type(base).model_validate(values)
    rows = [
        {"unit_id": "specific", "score": 0.8, "strict_match": True},
        {"unit_id": "partial", "score": 0.2, "strict_match": False},
    ]

    class CapturedRepository(RecallRepository):
        def __init__(self):
            self.config = config
            self.request = SearchRequest(query="observatory maintenance", recall_level="fast")
            self._has_insights = False
            self.calls = []

        def _execute(self, sql, parameters=()):
            self.calls.append((sql, parameters))
            return rows

    repository = CapturedRepository()
    assert repository.lexical("observatory maintenance", 64) == rows
    assert len(repository.calls) == 1
    assert repository.calls[0][1]["limit"] == 64
    assert repository.calls[0][1]["normalization"] == 0
