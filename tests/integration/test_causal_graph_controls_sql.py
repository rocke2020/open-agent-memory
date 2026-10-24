"""Execute graph flag combinations with read-only SQL and CTE-only fixture facts."""

import os
import time

import psycopg
import pytest

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.paths import default_config_path
from oam.retrieval import repository as graph_sql

pytestmark = pytest.mark.integration

FIXTURE = """
WITH filtered AS (
 SELECT 'tenant'::text tenant_id,'scope'::text user_id,id unit_id,'fact'::text kind,
 '[1,0]'::vector embedding FROM unnest(ARRAY['seed','cause','origin','entity','semantic']) id
), seeds AS (SELECT * FROM filtered WHERE unit_id='seed'), unit_links AS (
 SELECT 'tenant'::text tenant_id,'scope'::text user_id,source_id,target_id,kind,weight
 FROM (VALUES ('seed','cause','caused_by',0.9::float8),
 ('cause','origin','caused_by',0.8),('seed','semantic','semantic',1.0))
 links(source_id,target_id,kind,weight)
)
"""

ENTITIES = """
, memberships AS (
 SELECT 'tenant'::text tenant_id,'scope'::text user_id,unit_id,
 'organization'::text entity_id,'name'::text name_id
 FROM unnest(ARRAY['seed','entity']) unit_id
), name_support AS (
 SELECT NULL::text support_id,NULL::text name_id,NULL::text entity_id,NULL::text quote,
 NULL::text source_id,NULL::integer source_version,NULL::jsonb locator,
 NULL::text support_kind WHERE false
)
"""


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("causal", [False, True])
@pytest.mark.parametrize("graph", ["off", "light", "full"])
def test_readonly_ordinary_sql_honors_independent_graph_controls(monkeypatch, mode, causal, graph):
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    config = load_config(default_config_path()).config
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={
                    mode: getattr(config.search, mode).model_copy(update={"causal_graph": causal})
                }
            )
        }
    )
    authority = AuthorizedUser("key", "tenant", "reader", "scope", frozenset({"memory:read"}))
    with psycopg.connect(dsn) as connection:
        connection.read_only = True
        assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
        repository = graph_sql.RecallRepository(
            connection,
            authority,
            config,
            deadline=time.monotonic() + 30,
            request=SearchRequest(query="why change", recall_level=mode),
        )
        options = {"include_semantic": graph == "full", "include_entities": graph != "off"}
        # Parse and execute the full production eligibility SQL against an empty scope first.
        vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
        assert repository.ordinary(("seed",), vector, **options) == ((), {})
        # Exercise actual traversal/ranking with isolated fixture relations and no data writes.
        monkeypatch.setattr(graph_sql, "_graph_fact_eligibility", lambda **kwargs: FIXTURE)
        monkeypatch.setattr(graph_sql, "ENTITY_PROJECTIONS_FROM_SEED_ENTITIES", ENTITIES)
        ranks, paths = repository.ordinary(("seed",), (1.0, 0.0), **options)
        expected = set()
        if causal:
            expected |= {"cause", "origin"}
            assert paths["origin"][0]["nodes"] == ["seed", "cause", "origin"]
        if graph != "off":
            expected.add("entity")
        if graph == "full":
            expected.add("semantic")
        assert set(ranks) == expected
        assert all(
            path["relation"] != "caused_by" or causal for group in paths.values() for path in group
        )
