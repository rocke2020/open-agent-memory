"""Execute bounded graph SQL against read-only, synthetic eligible relations."""

import os
import time
from pathlib import Path

import psycopg
import pytest

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval import repository as graph

pytestmark = pytest.mark.integration

# Replace the eligibility boundary with VALUES; execute the production fanout,
# evidence-path and activation code without inserting or modifying database rows.
ELIGIBLE_FACTS = """
WITH filtered(tenant_id,user_id,unit_id,kind,embedding) AS (
    VALUES ('tenant','scope','seed_a','fact','[1,0]'::vector),
           ('tenant','scope','seed_b','fact','[1,0]'::vector),
           ('tenant','scope','neighbor','fact','[0.8,0.6]'::vector),
           ('tenant','scope','lower_ranked','fact','[0.7,0.7141428429]'::vector),
           ('tenant','scope','causal_only','fact','[0.6,0.8]'::vector)
), seeds AS (
    SELECT * FROM filtered WHERE unit_id=ANY(%(seeds)s::text[])
), unit_links(tenant_id,user_id,source_id,target_id,kind,weight) AS (
    {links}
)
"""
CAUSAL_LINKS = """
VALUES ('tenant','scope','seed_a','seed_b','caused_by',0.8::float8),
       ('tenant','scope','seed_b','causal_only','caused_by',0.7::float8)
"""

ENTITY_PROJECTIONS = """
, memberships(tenant_id,user_id,unit_id,entity_id,name_id) AS (
    VALUES ('tenant','scope','seed_a','shared','canonical'),
           ('tenant','scope','seed_b','shared','canonical'),
           ('tenant','scope','neighbor','shared','canonical'),
           ('tenant','scope','lower_ranked','shared','canonical')
), name_support AS (
    SELECT NULL::text AS support_id,NULL::text AS name_id,NULL::text AS entity_id,
           NULL::text AS quote,NULL::text AS source_id,NULL::int AS source_version,
           NULL::jsonb AS locator,NULL::text AS support_kind
    WHERE false
)
"""


@pytest.fixture
def graph_repository(monkeypatch):
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={
                    "entity_candidates_per_entity": 1,
                    "links_per_seed": 1,
                    "normal": config.search.normal.model_copy(update={"causal_graph": True}),
                }
            )
        }
    )
    monkeypatch.setattr(
        graph, "_graph_fact_eligibility", lambda **kwargs: ELIGIBLE_FACTS.format(links=CAUSAL_LINKS)
    )
    monkeypatch.setattr(graph, "ENTITY_PROJECTIONS_FROM_SEED_ENTITIES", ENTITY_PROJECTIONS)
    authority = AuthorizedUser("key", "tenant", "reader", "scope", frozenset({"memory:read"}))
    with psycopg.connect(dsn) as connection:
        connection.read_only = True
        assert connection.execute("SHOW transaction_read_only").fetchone() == ("on",)
        yield graph.RecallRepository(
            connection,
            authority,
            config,
            deadline=time.monotonic() + 10,
            request=SearchRequest(query="What changed in the workshop?"),
        )


@pytest.mark.parametrize("include_semantic", [False, True])
def test_other_graph_seeds_do_not_consume_entity_fanout(graph_repository, include_semantic):
    ranked, paths = graph_repository.ordinary(
        ("seed_a", "seed_b"), (1.0, 0.0), include_semantic=include_semantic
    )

    assert set(ranked) == {"neighbor", "causal_only"}
    assert {tuple(path["nodes"]) for path in paths["neighbor"]} == {
        ("seed_a", "neighbor"),
        ("seed_b", "neighbor"),
    }
    assert {path["relation"] for path in paths["neighbor"]} == {"entity"}
    assert any(
        path["nodes"] == ["seed_a", "seed_b", "causal_only"] and path["relation"] == "caused_by"
        for path in paths["causal_only"]
    )


@pytest.mark.parametrize("direction", ["outgoing", "incoming"])
def test_other_graph_seeds_do_not_consume_semantic_fanout(graph_repository, monkeypatch, direction):
    links = {
        "outgoing": """
            VALUES ('tenant','scope','seed_a','seed_b','semantic',1.0::float8),
                   ('tenant','scope','seed_a','causal_only','semantic',0.9::float8)
        """,
        "incoming": """
            VALUES ('tenant','scope','seed_b','seed_a','semantic',1.0::float8),
                   ('tenant','scope','causal_only','seed_a','semantic',0.9::float8)
        """,
    }[direction]
    monkeypatch.setattr(
        graph, "_graph_fact_eligibility", lambda **kwargs: ELIGIBLE_FACTS.format(links=links)
    )
    ranked, paths = graph_repository.ordinary(("seed_a", "seed_b"), (1.0, 0.0))

    assert set(ranked) == {"neighbor", "causal_only"}
    assert paths["causal_only"] == [
        {"arm": "graph", "relation": "semantic", "nodes": ["seed_a", "causal_only"], "score": 0.9}
    ]


@pytest.mark.parametrize("include_semantic", [False, True])
def test_other_graph_seeds_do_not_consume_terminal_causal_fanout(
    graph_repository, monkeypatch, include_semantic
):
    links = """
        VALUES ('tenant','scope','seed_a','lower_ranked','caused_by',1.0::float8),
               ('tenant','scope','lower_ranked','seed_b','caused_by',1.0::float8),
               ('tenant','scope','lower_ranked','causal_only','caused_by',0.9::float8)
    """
    monkeypatch.setattr(
        graph, "_graph_fact_eligibility", lambda **kwargs: ELIGIBLE_FACTS.format(links=links)
    )
    ranked, paths = graph_repository.ordinary(
        ("seed_a", "seed_b"), (1.0, 0.0), include_semantic=include_semantic
    )

    assert set(ranked) == {"neighbor", "lower_ranked", "causal_only"}
    assert paths["causal_only"] == [
        {
            "arm": "graph",
            "relation": "caused_by",
            "nodes": ["seed_a", "lower_ranked", "causal_only"],
            "score": 0.9,
        }
    ]
