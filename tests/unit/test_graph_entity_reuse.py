"""Graph entity candidates are shared across seeds before per-seed exclusion."""

import time
from pathlib import Path

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.repository import RecallRepository


def test_graph_reads_each_distinct_entity_candidate_set_once():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = object.__new__(RecallRepository)
    repository.config = config
    repository.request = SearchRequest(query="Where is my book?", recall_level="mid")
    repository.deadline = time.monotonic() + 5
    repository.has_insights = False
    repository._eligible = lambda **kwargs: "WITH filtered AS (SELECT 1) "
    statements = []
    repository._execute = lambda sql, parameters: statements.append(sql) or []

    assert repository.ordinary(("seed_a", "seed_b"), (1.0,)) == ((), {})
    statement = statements[0]
    assert "seed_entities AS MATERIALIZED" in statement
    assert "entity_candidates AS MATERIALIZED" in statement
    assert "SELECT DISTINCT entity_key FROM seed_entities" in statement
    assert "LIMIT %(per_entity)s+1" in statement
    assert "c.unit_id<>s.seed_id" in statement
