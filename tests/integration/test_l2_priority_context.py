"""Read-only PostgreSQL proof of exact priority ordering and bounded distance work."""

from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.rows import dict_row
from test_core_http import core_database as core_database

from oam.config import load_config
from oam.indexing import consolidation

pytestmark = pytest.mark.integration


def _distance_loops(node):
    own = (
        [node.get("Actual Loops", 0)]
        if node.get("Node Type") == "Aggregate"
        and any("<=>" in value for value in node.get("Output", []))
        else []
    )
    return own + [value for child in node.get("Plans", []) for value in _distance_loops(child)]


@pytest.mark.parametrize("priority_count", [63, 64, 65])
@pytest.mark.parametrize("seed_only", [False, True])
def test_priority_context_exact_order_and_bounded_distance_work(
    monkeypatch, priority_count, seed_only, core_database
):
    dsn = core_database[0]
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    eligible = (
        "SELECT u.* FROM (SELECT 'priority-test'::text AS tenant_id, 'scope'::text AS user_id,"
        "'fact'::text AS kind,'f'||lpad(i::text,3,'0') AS unit_id,"
        "'m'||lpad(i::text,3,'0') AS memory_id, 'fixture'::text AS content_text,"
        "'fixture-generation'::text AS generation_id,"
        "'source'::text AS source_id,1 AS source_version,NULL::timestamptz AS mentioned_at,"
        "'{}'::jsonb AS locator,CASE WHEN i<2 OR i>="
        + str(priority_count)
        + " THEN '[1,0]'::vector ELSE '[0,1]'::vector END AS embedding "
        "FROM generate_series(0,256) AS n(i)) u WHERE u.unit_id<>'f256'"
    )
    monkeypatch.setattr(consolidation, "ELIGIBLE_UNITS", eligible)
    original_rows = consolidation._rows
    monkeypatch.setattr(consolidation, "_rows", lambda _connection, query, params: (query, params))
    seeds = [{"unit_id": "f000"}, {"unit_id": "f001"}]
    targets = [{"supports": [{"id": f"m{i:03}"} for i in range(priority_count)] + [{"id": "m256"}]}]
    query, params = consolidation._fact_context(
        SimpleNamespace(config=config, generation_id="fixture-generation"),
        None,
        seeds,
        targets,
        seed_only=seed_only,
    )
    monkeypatch.setattr(consolidation, "_rows", original_rows)
    with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as connection:
        assert connection.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()
        expected = ["f000", "f001"] if seed_only else [f"f{i:03}" for i in range(64)]
        assert [row["unit_id"] for row in rows] == expected
        assert all(row["annotations"] is None for row in rows)
        plan = connection.execute(
            "EXPLAIN (ANALYZE,VERBOSE,FORMAT JSON) " + query, params
        ).fetchone()[0][0]["Plan"]
    loops = _distance_loops(plan)
    assert loops, "the analyzed query must expose actual cosine-distance work"
    if seed_only or priority_count >= 64:
        assert max(loops) <= (2 if seed_only else priority_count), loops
    else:
        assert max(loops) >= 256, "insufficient priority must retain the complete fallback"
