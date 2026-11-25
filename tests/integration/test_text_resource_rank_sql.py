"""Run the eligibility joins on synthetic VALUES rows in a read-only transaction."""

import os

import psycopg
import pytest

from oam.db.context import SET_USER_CONTEXT_SQL
from oam.retrieval import resource_sql

pytestmark = pytest.mark.integration


def test_openings_and_neighbors_exclude_other_scopes_and_unpublished_versions():
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("requires PostgreSQL for the read-only synthetic SQL fixture")
    # Each row represents one indexed passage. Tables are statement-local CTEs.
    fixture = """
    WITH fixture(tenant_id,user_id,source_id,source_version,unit_id,ordinal,generation_id,
                 media_type,evidence_status,visible_to) AS (VALUES
        ('tenant','user','doc',1,'left',0,'gen','text/plain','active',NULL::int),
        ('tenant','user','doc',1,'seed',1,'gen','text/plain','active',NULL),
        ('tenant','user','doc',1,'right',2,'gen','text/plain','active',NULL),
        ('tenant','user','doc',1,'far',3,'gen','text/plain','active',NULL),
        ('tenant','user','doc',1,'z-left',0,'gen','text/plain','active',NULL),
        ('tenant','user','doc',0,'stale',2,'gen','text/plain','active',NULL),
        ('tenant','user','doc',1,'gone',2,'gen','text/plain','excluded',NULL),
        ('tenant','user','doc',1,'hidden',2,'gen','text/plain','active',9),
        ('tenant','user','doc',1,'oldgen',2,'old','text/plain','active',NULL),
        ('tenant','user','pdf',1,'pdf',0,'gen','application/pdf','active',NULL),
        ('tenant','user','other-doc',1,'other-unit',0,'gen','text/plain','active',NULL),
        ('other','user','doc',1,'seed',1,'gen','text/plain','active',NULL),
        ('tenant','other','doc',1,'seed',1,'gen','text/plain','active',NULL)
    ), users AS (
        SELECT DISTINCT tenant_id,user_id,'active' AS state,'gen' AS index_generation_id
        FROM fixture
    ), sources AS (
        SELECT DISTINCT tenant_id,user_id,source_id,1 AS active_version,'active' AS status,
            'evidence_resource' AS input_role FROM fixture
    ), source_versions AS (
        SELECT DISTINCT tenant_id,user_id,source_id,source_version,media_type,250 AS byte_length,
            'active' AS status FROM fixture
    ), search_units AS (
        SELECT *,unit_id AS passage_id,unit_id AS evidence_id,'resource_passage' AS kind,
            'evidence' AS origin FROM fixture
    ), passages AS (
        SELECT tenant_id,user_id,unit_id AS passage_id,unit_id AS evidence_id,visible_to,
            generation_id FROM fixture
    ), evidence AS (
        SELECT tenant_id,user_id,source_id,source_version,unit_id AS evidence_id,
            evidence_status AS status,false AS learning_eligible,
            repeat(CASE WHEN source_id='other-doc' THEN 'γδ' ELSE 'αβ' END,125) AS content_text
        FROM fixture
    ),
    """
    params = {
        "ann_tenant": "tenant",
        "ann_user": "user",
        "generation": "gen",
        "radius": 1,
        "ids": ["seed", "stale", "gone", "hidden", "oldgen", "pdf"],
    }
    with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as connection:
        connection.execute(SET_USER_CONTEXT_SQL, ("tenant", "user"))
        openings = fixture + resource_sql.OPENINGS.lstrip().removeprefix("WITH ")
        neighbors = fixture + resource_sql.NEIGHBORS.lstrip().removeprefix("WITH ")
        assert connection.execute(openings, params).fetchall() == [("seed", "doc", "αβ" * 80)]
        opening_batch = {**params, "ids": [*params["ids"], "left", "other-unit", "seed"]}
        assert sorted(connection.execute(openings, opening_batch).fetchall()) == [
            ("left", "doc", "αβ" * 80),
            ("other-unit", "other-doc", "γδ" * 80),
            ("seed", "doc", "αβ" * 80),
        ]
        assert connection.execute(neighbors, params).fetchall() == [
            ("seed", "left"),
            ("seed", "seed"),
            ("seed", "right"),
        ]
        assert connection.execute(neighbors, {**params, "generation": "old"}).fetchall() == []
        assert connection.execute(
            neighbors, {**params, "radius": resource_sql.NEIGHBOR_RADIUS}
        ).fetchall() == [("seed", "left"), ("seed", "seed"), ("seed", "right"), ("seed", "far")]
        # Overlapping windows retain their own seeds. A tied ordinal contributes only one unit.
        batch = {**params, "ids": ["right", "z-left", "seed", "right", "invalid"]}
        assert connection.execute(neighbors, batch).fetchall() == [
            ("right", "seed"),
            ("right", "right"),
            ("right", "far"),
            ("seed", "left"),
            ("seed", "seed"),
            ("seed", "right"),
            ("z-left", "z-left"),
            ("z-left", "seed"),
        ]
