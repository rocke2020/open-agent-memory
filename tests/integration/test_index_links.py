"""Link construction on the explicitly isolated local indexing test database."""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.db.context import bind_user
from oam.indexing.links import build_links

pytestmark = pytest.mark.integration
START = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def database():
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    target = conninfo_to_dict(dsn)
    if target.get("host") not in {"localhost", "127.0.0.1"} or target.get("port") not in {
        "15433",
        "15435",
    }:
        pytest.skip("index link fixtures require an approved local test port")
    with (
        psycopg.connect(dsn, autocommit=True) as connection,
        connection.transaction(force_rollback=True),
    ):
        tenant = f"links-{uuid4().hex}"
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (tenant,))
        for scope in ("batch", "incremental", "foreign"):
            connection.execute(
                "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,"
                "index_generation_id) "
                "VALUES (%s,%s,'synthetic','test','persistent','generation')",
                (tenant, scope),
            )
        authority = AuthorizedUser(
            "key", tenant, "worker", "batch", frozenset({"memory:append", "memory:read"})
        )
        config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
        config = config.model_copy(
            update={
                "index": config.index.model_copy(
                    update={
                        "semantic_link_min_similarity": 0.5,
                        "semantic_link_max_neighbors": 1,
                        "temporal_link_max_neighbors": 1,
                        "temporal_link_horizon_seconds": 10 * 86400,
                    }
                )
            }
        )
        yield connection, authority, config


def other_scope(authority, scope):
    return AuthorizedUser(
        authority.credential_id,
        authority.tenant_id,
        authority.principal_id,
        scope,
        authority.capabilities,
    )


def add_fact(
    connection,
    authority,
    node_id,
    *,
    day=None,
    eligible=True,
    stale=False,
    generation="generation",
    vector=(1.0, 0.0),
):
    tenant, scope = authority.tenant_id, authority.user_id
    connection.execute(
        "INSERT INTO sources(tenant_id,user_id,source_id,input_role,active_version,status) "
        "VALUES (%s,%s,%s,'interaction',1,'active')",
        (tenant, scope, node_id),
    )
    connection.execute(
        "INSERT INTO source_versions(tenant_id,user_id,source_id,source_version,media_type,"
        "original_bytes,content_sha256,byte_length,status) "
        "VALUES (%s,%s,%s,1,'text/plain',%s,%s,1,%s)",
        (tenant, scope, node_id, b"x", b"x" * 32, "superseded" if stale else "active"),
    )
    connection.execute(
        "INSERT INTO evidence(tenant_id,user_id,evidence_id,source_id,source_version,accepted_seq,"
        "content_text,learning_eligible,status) VALUES (%s,%s,%s,%s,1,1,'x',%s,'active')",
        (tenant, scope, node_id, node_id, eligible),
    )
    connection.execute(
        "INSERT INTO passages(tenant_id,user_id,passage_id,evidence_id,parse_revision,locator,"
        "normalized_text,generation_id,visible_from) VALUES (%s,%s,%s,%s,1,'{}','x',%s,1)",
        (tenant, scope, node_id, node_id, generation),
    )
    connection.execute(
        "INSERT INTO memories(tenant_id,user_id,memory_id,canonical_claim,assertion_kind,"
        "generation_id,visible_from) VALUES (%s,%s,%s,'x','stated',%s,1)",
        (tenant, scope, node_id, generation),
    )
    connection.execute(
        "INSERT INTO support(tenant_id,user_id,memory_id,evidence_id,passage_id,"
        "support_kind,span) "
        "VALUES (%s,%s,%s,%s,%s,'quote',%s)",
        (tenant, scope, node_id, node_id, node_id, Jsonb({"start": 0, "end": 1})),
    )
    dimensions = connection.execute("SELECT embedding_dimensions FROM index_settings").fetchone()[0]
    embedding = str(list(vector) + [0.0] * (dimensions - len(vector)))
    timestamp = START + timedelta(days=day) if day is not None else None
    connection.execute(
        "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,source_id,source_version,"
        "evidence_id,memory_id,generation_id,content_text,locator,embedding,lexical_text,"
        "mentioned_at,source_created_at,source_updated_at) "
        "VALUES (%s,%s,%s,'fact','memory',%s,1,%s,%s,%s,'x','{}',%s::vector,'x',%s,%s,%s)",
        (
            tenant,
            scope,
            node_id,
            node_id,
            node_id,
            node_id,
            generation,
            embedding,
            timestamp,
            timestamp,
            timestamp,
        ),
    )


def annotate(connection, authority, unit_id, annotation_id, role, day):
    timestamp = START + timedelta(days=day)
    connection.execute(
        "INSERT INTO unit_times(tenant_id,user_id,annotation_id,unit_id,role,time_span,detail) "
        "VALUES (%s,%s,%s,%s,%s,tstzrange(%s,%s,'[]'),%s)",
        (
            authority.tenant_id,
            authority.user_id,
            annotation_id,
            unit_id,
            role,
            timestamp,
            timestamp,
            Jsonb({"meaning": "instant"}),
        ),
    )


def temporal_rows(connection, authority):
    return connection.execute(
        "SELECT time_basis,left_id,right_id,left_annotation_id,right_annotation_id,"
        "gap_seconds,weight FROM temporal_links WHERE tenant_id=%s AND user_id=%s "
        "ORDER BY time_basis,left_id,right_id",
        (authority.tenant_id, authority.user_id),
    ).fetchall()


def test_semantic_neighbors_exclude_stale_foreign_and_wrong_generation_before_cap(database):
    connection, authority, config = database
    add_fact(connection, authority, "seed")
    add_fact(connection, authority, "a_hidden", eligible=False)
    add_fact(connection, authority, "b_stale", stale=True)
    add_fact(connection, authority, "c_old_generation", generation="old")
    add_fact(connection, other_scope(authority, "foreign"), "d_foreign")
    add_fact(connection, authority, "good", vector=(0.8, 0.6))
    add_fact(connection, authority, "opposite", vector=(-1.0, 0.0))
    connection.execute("SET LOCAL ROLE oam_content")
    connection.execute("SET LOCAL jit = on")
    first = build_links(connection, authority, config, "generation", ["seed", "b_stale"])
    assert connection.execute("SHOW jit").fetchone()[0] == "off"
    assert first == {"semantic_links": 1, "temporal_links": 0, "coverage": "complete"}
    rows = connection.execute("SELECT source_id,target_id,weight FROM unit_links").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("seed", "good")]
    assert rows[0][2] == pytest.approx(0.8)
    repeated = build_links(connection, authority, config, "generation", ["seed"])
    assert repeated["semantic_links"] == 0


def test_semantic_prior_edges_do_not_consume_the_remaining_neighbor_probe(database):
    connection, authority, config = database
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"semantic_link_max_neighbors": 2})}
    )
    add_fact(connection, authority, "seed")
    add_fact(connection, authority, "prior")
    add_fact(connection, authority, "next", vector=(0.8, 0.6))
    add_fact(connection, authority, "farther", vector=(0.6, 0.8))
    connection.execute(
        "INSERT INTO unit_links(tenant_id,user_id,source_id,target_id,kind,weight) "
        "VALUES (%s,%s,'seed','prior','semantic',1)",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute("SET LOCAL ROLE oam_content")

    result = build_links(connection, authority, config, "generation", ["seed"])

    assert result["semantic_links"] == 1
    rows = connection.execute(
        "SELECT source_id,target_id FROM unit_links ORDER BY target_id"
    ).fetchall()
    assert rows == [("seed", "next"), ("seed", "prior")]


def test_undated_facts_skip_temporal_link_queries(database, monkeypatch):
    from oam.indexing import links

    connection, authority, config = database
    add_fact(connection, authority, "first")
    add_fact(connection, authority, "second")
    connection.execute("SET LOCAL ROLE oam_content")
    monkeypatch.setattr(links, "_INTERVAL", "SELECT 1/0")

    def absent_point_query(_column):
        raise AssertionError("undated facts must not run a point-time link query")

    monkeypatch.setattr(links, "_point_query", absent_point_query)
    result = build_links(connection, authority, config, "generation", ["first"])
    assert result["semantic_links"] == 1
    assert result["temporal_links"] == 0


def test_temporal_within_batch_and_existing_proposals_have_same_canonical_witnesses(database):
    connection, authority, config = database
    incremental = other_scope(authority, "incremental")
    for selected in (authority, incremental):
        add_fact(connection, selected, "a", day=0)
        annotate(connection, selected, "a", "a-event-2", "event", 0)
        annotate(connection, selected, "a", "a-event-1", "event", 0)
        annotate(connection, selected, "a", "a-reference", "date_reference", 1)
    first = build_links(connection, incremental, config, "generation", ["a"])
    assert first["temporal_links"] == 0
    for selected in (authority, incremental):
        add_fact(connection, selected, "b", day=2)
        annotate(connection, selected, "b", "b-event-2", "event", 2)
        annotate(connection, selected, "b", "b-event-1", "event", 2)
        annotate(connection, selected, "b", "b-reference", "date_reference", 2)
    connection.execute("SET LOCAL ROLE oam_content")
    within = build_links(connection, authority, config, "generation", ["a", "b"])
    within_rows = temporal_rows(connection, authority)
    cross = build_links(connection, incremental, config, "generation", ["b"])
    assert within["temporal_links"] == cross["temporal_links"] == 5
    assert temporal_rows(connection, incremental) == within_rows
    assert [(row[0], row[1], row[2]) for row in within_rows] == [
        ("created", "a", "b"),
        ("event", "a", "b"),
        ("mentioned", "a", "b"),
        ("referenced", "a", "b"),
        ("updated", "a", "b"),
    ]
    event = next(row for row in within_rows if row[0] == "event")
    assert event[3:5] == ("a-event-1", "b-event-1")
    assert event[5:] == (2 * 86400.0, 0.8)
    reference = next(row for row in within_rows if row[0] == "referenced")
    assert reference[3:5] == ("a-reference", "b-reference")
    assert reference[6] == 0.9
    assert (
        build_links(connection, incremental, config, "generation", ["a", "b"])["temporal_links"]
        == 0
    )


def test_temporal_horizon_and_eligibility_are_applied_before_neighbor_cap(database):
    connection, authority, config = database
    add_fact(connection, authority, "seed", day=0)
    add_fact(connection, authority, "a_hidden", day=0.1, eligible=False)
    add_fact(connection, authority, "b_stale", day=0.2, stale=True)
    add_fact(connection, other_scope(authority, "foreign"), "c_foreign", day=0.3)
    add_fact(connection, authority, "good", day=2)
    add_fact(connection, authority, "boundary", day=10)
    add_fact(connection, authority, "outside", day=11)
    connection.execute("SET LOCAL ROLE oam_content")
    build_links(connection, authority, config, "generation", ["seed"])
    rows = temporal_rows(connection, authority)
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("created", "good", "seed"),
        ("mentioned", "good", "seed"),
        ("updated", "good", "seed"),
    ]
    assert all(row[5:] == (2 * 86400.0, 0.8) for row in rows)


def test_temporal_horizon_excludes_boundary_without_nearer_candidates(database):
    connection, authority, config = database
    add_fact(connection, authority, "seed", day=0)
    add_fact(connection, authority, "boundary", day=10)
    add_fact(connection, authority, "outside", day=11)
    annotate(connection, authority, "seed", "seed-event", "event", 0)
    annotate(connection, authority, "boundary", "boundary-event", "event", 10)
    annotate(connection, authority, "outside", "outside-event", "event", 11)
    connection.execute("SET LOCAL ROLE oam_content")
    result = build_links(connection, authority, config, "generation", ["seed"])
    assert result["temporal_links"] == 0
    assert temporal_rows(connection, authority) == []


def test_temporal_period_gap_treats_touching_and_overlapping_ranges_as_zero(database):
    connection, authority, config = database
    config = config.model_copy(
        update={
            "index": config.index.model_copy(update={"temporal_link_max_neighbors": 2}),
        }
    )
    for node_id, start_day, end_day in (("seed", 0, 5), ("overlap", 3, 4), ("touching", 5, 6)):
        add_fact(connection, authority, node_id)
        connection.execute(
            "INSERT INTO unit_times(tenant_id,user_id,annotation_id,unit_id,role,"
            "time_span,detail) "
            "VALUES (%s,%s,%s,%s,'event',tstzrange(%s,%s,'[)'),%s)",
            (
                authority.tenant_id,
                authority.user_id,
                node_id,
                node_id,
                START + timedelta(days=start_day),
                START + timedelta(days=end_day),
                Jsonb({"meaning": "possible_period"}),
            ),
        )
    connection.execute("SET LOCAL ROLE oam_content")
    result = build_links(connection, authority, config, "generation", ["seed"])
    assert result["temporal_links"] == 2
    assert {row[5:] for row in temporal_rows(connection, authority)} == {(0.0, 1.0)}


def test_semantic_link_query_has_an_hnsw_access_path(database):
    from oam.indexing import links

    connection, authority, config = database
    add_fact(connection, authority, "seed")
    add_fact(connection, authority, "near", vector=(0.8, 0.6))
    add_fact(connection, authority, "far", vector=(-1.0, 0.0))
    connection.execute("SET LOCAL ROLE oam_content")
    bind_user(connection.cursor(), authority)
    # Expose the indexed path even on this deliberately tiny fixture. This
    # proves planner eligibility, not the default plan on a deployed history.
    connection.execute("SET LOCAL enable_seqscan = off")
    connection.execute("SET LOCAL enable_sort = off")
    plan = connection.execute(
        "EXPLAIN (FORMAT JSON) " + links._SEMANTIC,
        {
            "tenant": authority.tenant_id,
            "user": authority.user_id,
            "generation": "generation",
            "ids": ["seed"],
            "similarity": config.index.semantic_link_min_similarity,
            "semantic_neighbors": config.index.semantic_link_max_neighbors,
        },
    ).fetchone()[0][0]["Plan"]

    def indexes(node):
        return {node.get("Index Name")} | set().union(
            *(indexes(child) for child in node.get("Plans", []))
        )

    assert "search_units_embedding" in indexes(plan)


def test_point_time_probes_preserve_equal_times_and_id_ties_across_directions(database):
    connection, authority, config = database
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"temporal_link_max_neighbors": 2})}
    )
    for node_id, day in (
        ("seed", 5),
        ("same", 5),
        ("a_right", 6),
        ("b_left", 4),
        ("c_left", 4),
        ("d_left", 3),
    ):
        add_fact(connection, authority, node_id, day=day)
    connection.execute("SET LOCAL ROLE oam_content")

    build_links(connection, authority, config, "generation", ["seed"])

    rows = temporal_rows(connection, authority)
    assert [(row[0], row[1], row[2]) for row in rows] == [
        ("created", "a_right", "seed"),
        ("created", "same", "seed"),
        ("mentioned", "a_right", "seed"),
        ("mentioned", "same", "seed"),
        ("updated", "a_right", "seed"),
        ("updated", "same", "seed"),
    ]
    assert [row[5:] for row in rows] == [(86400.0, 0.9), (0.0, 1.0)] * 3


def test_interval_neighbors_reduce_annotation_witnesses_before_unit_cap(database):
    connection, authority, config = database
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"temporal_link_max_neighbors": 2})}
    )
    for node_id in ("seed", "a", "b", "c"):
        add_fact(connection, authority, node_id)
    for annotation_id, day in (("seed-0", 0), ("seed-10", 10)):
        annotate(connection, authority, "seed", annotation_id, "event", day)
    for day in (1, 2, 9, 11):
        annotate(connection, authority, "a", f"a-{day}", "event", day)
    annotate(connection, authority, "b", "b-2", "event", 2)
    annotate(connection, authority, "c", "c-3", "event", 3)
    connection.execute("SET LOCAL ROLE oam_content")

    build_links(connection, authority, config, "generation", ["seed"])

    assert temporal_rows(connection, authority) == [
        ("event", "a", "seed", "a-1", "seed-0", 86400.0, 0.9),
        ("event", "b", "seed", "b-2", "seed-0", 172800.0, 0.8),
    ]
