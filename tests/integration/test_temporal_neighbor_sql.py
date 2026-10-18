"""Read-only SQL counterexamples for eligible temporal top-k selection."""

import json
import os
import time
from pathlib import Path

import psycopg
import pytest

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.repository import RecallRepository

pytestmark = pytest.mark.integration

FIXTURE = """
WITH raw_units(unit_id,embedding,active,mentioned_at) AS (
 SELECT 'n'||lpad(i::text,2,'0'),'[1,0]'::vector,true,'2025-01-02'::timestamptz
 FROM generate_series(1,14) i
 UNION ALL VALUES
 ('seed','[1,0]'::vector,true,'2025-01-02'::timestamptz),
 ('withdrawn','[1,0]'::vector,false,'2025-01-02'::timestamptz),
 ('low_cosine','[0,1]'::vector,true,'2025-01-02'::timestamptz),
 ('outside','[1,0]'::vector,true,'2024-01-02'::timestamptz)
), fixture_units AS (
 SELECT 'tenant'::text tenant_id,'scope'::text user_id,unit_id,'fact'::text kind,
 embedding,active,mentioned_at FROM raw_units
), filtered AS (
 SELECT * FROM fixture_units WHERE active AND mentioned_at >= %(time_start)s
 AND mentioned_at < %(time_end)s
), unit_times AS (
 SELECT tenant_id,user_id,unit_id,'valid'::text annotation_id,'event'::text role,
 tstzrange(mentioned_at,mentioned_at+interval '1 day','[)') time_span
 FROM fixture_units
), temporal_links AS (
 SELECT tenant_id,user_id,%(basis)s::text time_basis,'seed'::text left_id,
 unit_id right_id,CASE WHEN unit_id LIKE 'n%%' THEN 0.8 ELSE 1.0 END::float8 weight,
 'valid'::text left_annotation_id,'valid'::text right_annotation_id
 FROM fixture_units WHERE unit_id<>'seed'
 UNION ALL
 SELECT tenant_id,user_id,%(basis)s,unit_id,'seed',
 CASE WHEN unit_id LIKE 'n%%' THEN 0.8 ELSE 1.0 END::float8,'valid','valid'
 FROM fixture_units WHERE unit_id<>'seed'
 UNION ALL VALUES
 ('tenant','scope','event','seed','n14',1.0::float8,'valid','missing')
), unit_links AS (
 SELECT tenant_id,user_id,'seed'::text source_id,unit_id target_id,'caused_by'::text kind,
 CASE WHEN unit_id LIKE 'n%%' THEN 0.8 ELSE 1.0 END::float8 weight
 FROM fixture_units WHERE unit_id<>'seed'
)
"""


@pytest.mark.parametrize("basis", ["mentioned", "event"])
@pytest.mark.parametrize("sources", [("seed",), ("seed", "seed")])
def test_temporal_topk_filters_before_caps_and_orders_ties_across_directions(
    monkeypatch, basis, sources
):
    """Raw limits would waste slots on withdrawn, out-of-window, weak or invalid-witness edges."""
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    authority = AuthorizedUser("key", "tenant", "reader", "scope", frozenset({"memory:read"}))
    request = SearchRequest(
        query="What happened during January?",
        time_basis=basis,
        time_window={"start": "2025-01-01T00:00:00Z", "end": "2025-02-01T00:00:00Z"},
    )
    with psycopg.connect(dsn) as connection:
        connection.read_only = True
        assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
        repository = RecallRepository(
            connection, authority, config, deadline=time.monotonic() + 10, request=request
        )
        repository.time_parameters = {
            "time_start": request.time_window.start,
            "time_end": request.time_window.end,
            "time_role": "event",
        }
        monkeypatch.setattr(repository, "_eligible", lambda **kwargs: FIXTURE)
        rows = repository._temporal_edges(sources, (1.0, 0.0), limit=12, per_source=True)
        constrained = repository._temporal_edges(
            sources,
            (1.0, 0.0),
            limit=12,
            per_source=True,
            edge_ids=(json.dumps(["caused_by", "seed", "n14"]),),
        )
    assert [row["unit_id"] for row in constrained] == ["n14"]
    assert len(rows) == 12
    assert [row["unit_id"] for row in rows] == [f"n{i:02d}" for i in range(1, 5) for _ in range(3)]
    assert [row["relation"] for row in rows] == ["caused_by", "temporal", "temporal"] * 4
    assert [row["neighbor_rank"] for row in rows] == list(range(1, 13))
    assert all(row["weight"] == 0.8 and row["cosine"] == 1.0 for row in rows)
    assert all("missing" not in row["edge_id"] for row in rows)
    assert rows == sorted(
        rows, key=lambda row: (row["source_id"], -row["weight"], row["unit_id"], row["edge_id"])
    )
