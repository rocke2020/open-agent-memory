"""Read-only ANN proof of required priority ordering and bounded distance work."""

from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from test_core_http import core_database as core_database
from test_index_links import add_fact

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.db.context import bind_user
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
    dsn, tenant, _ = core_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    authority = AuthorizedUser("key", tenant, "reader", "scope", frozenset({"memory:read"}))
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,index_generation_id) "
            "VALUES (%s,'scope','synthetic','test','persistent','generation')",
            (tenant,),
        )
        for index in range(256):
            vector = (
                (1.0, 0.0)
                if index < 2
                else (0.0, 1.0)
                if index < priority_count
                else (1.0, (index - priority_count + 1) / 256)
            )
            add_fact(connection, authority, f"f{index:03}", vector=vector)
        add_fact(connection, authority, "f256", generation="historical", vector=(1.0, 0.0))

    original_rows = consolidation._rows
    monkeypatch.setattr(
        consolidation, "_rows", lambda _connection, query, params, **kwargs: (query, params)
    )
    seeds = [{"unit_id": "f000"}, {"unit_id": "f001"}]
    targets = [{"supports": [{"id": f"f{i:03}"} for i in range(priority_count)] + [{"id": "f256"}]}]
    with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as connection:
        assert connection.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        connection.execute("SET LOCAL ROLE oam_content")
        bind_user(connection.cursor(), authority)
        query, params = consolidation._fact_context(
            SimpleNamespace(config=config, generation_id="generation", authority=authority),
            connection,
            seeds,
            targets,
            seed_only=seed_only,
        )
        monkeypatch.setattr(consolidation, "_rows", original_rows)
        rows = original_rows(connection, query, params, prepare=True)
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
        assert max(loops) <= priority_count + len(seeds) * 64, loops
