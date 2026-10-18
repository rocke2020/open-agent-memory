"""The dense SQL ordering boundary for recall modes."""

import time
from pathlib import Path

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.repository import RecallRepository


class _Connection:
    def execute(self, statement, parameters):
        raise AssertionError("dense setup must be sent with its candidate query")


def test_larger_dense_pools_order_user_vectors_before_eligibility():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    statements = {}
    for mode, limit in (("fast", 32), ("fast", 64), ("mid", 64), ("deep", 64)):
        repository = object.__new__(RecallRepository)
        repository.connection = _Connection()
        repository.config = config
        repository.request = SearchRequest(query="Where is my book?", recall_level=mode)
        repository.deadline = time.monotonic() + 5
        repository._eligible = lambda **kwargs: "WITH filtered AS (SELECT 1) "
        repository._execute = lambda sql, parameters, key=(mode, limit), **options: (
            statements.setdefault(key, sql) and []
        )
        repository.dense((1.0,), limit)

    assert "ORDER BY u.embedding <=> %(vector)s::vector LIMIT" in statements["fast", 32]
    for key in (("fast", 64), ("mid", 64), ("deep", 64)):
        assert "ORDER BY distance OFFSET 0" in statements[key]
        assert "WHERE (SELECT true FROM filtered f WHERE " in statements[key]
        assert "ORDER BY u.distance,u.unit_id LIMIT" in statements[key]
