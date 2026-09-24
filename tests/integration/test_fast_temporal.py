"""Fast temporal ranking and traversal through the scoped PostgreSQL path."""

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from psycopg.types.range import Range
from test_recall_repository import FixedModels, TransactionPool, add_fact, add_insight
from test_recall_repository import recall_database as recall_database

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.repository import RecallRepository
from oam.retrieval.service import RecallService

pytestmark = pytest.mark.integration


def annotate(connection, authority, node, instant):
    connection.execute(
        "INSERT INTO unit_times VALUES (%s,%s,%s,%s,'event',%s,'{}')",
        (authority.tenant_id, authority.scope_id, node, node, Range(instant, instant, "[]")),
    )


def test_fast_last_week_six_hops_keeps_direct_outside_but_rejects_outside_bridge(
    recall_database, record_property
):
    connection, authority = recall_database
    instant = datetime(2026, 1, 8, tzinfo=UTC)
    for index in range(7):
        node = f"node{index}"
        add_fact(connection, authority, node, f"Event {index}", direction=1 if index == 0 else 0.2)
        annotate(connection, authority, node, instant)
        if index:
            previous = f"node{index - 1}"
            connection.execute(
                "INSERT INTO temporal_links VALUES (%s,%s,'event',%s,%s,%s,%s,0,1)",
                (authority.tenant_id, authority.scope_id, previous, node, previous, node),
            )
    add_fact(connection, authority, "outside", "last week what I did", direction=0.9)
    annotate(connection, authority, "outside", datetime(2025, 1, 8, tzinfo=UTC))
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={
                    "fast": config.search.fast.model_copy(update={"channel_candidates": 2}),
                    "graph_seed_max": 1,
                    "temporal_seed_max": 1,
                }
            )
        }
    )
    service = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="generation",
    )
    request = SearchRequest(
        query="last week what I did", recall_level="fast", reference_time="2026-01-14T12:00:00Z"
    )
    result = service.search(authority, request)
    items = {item["unit_id"]: item for item in result["items"]}
    assert set(items) == {"outside", *(f"node{i}" for i in range(7))}
    assert items["outside"]["paths"] == []
    assert items["node0"]["paths"][0]["relation"] == "time_match"
    assert items["node6"]["paths"][0]["nodes"] == [f"node{i}" for i in range(7)]
    assert len(items["node6"]["paths"][0]["edge_ids"]) == 6
    assert result["diagnostics"]["generative_calls"] == 0
    assert result["diagnostics"]["temporal_stop"] == "frontier_exhausted"
    elapsed = result["diagnostics"]["timing_ms"]
    record_property("query_time_ms", elapsed["query_time"])
    record_property("fast_total_ms", elapsed["total"])
    assert (
        0 <= elapsed["query_time"] < elapsed["total"] < config.recall.fast_deadline_seconds * 1000
    )
    connection.execute(
        "UPDATE unit_times SET time_span=tstzrange('2025-01-08','2025-01-08','[]') "
        "WHERE tenant_id=%s AND unit_id='node2'",
        (authority.tenant_id,),
    )
    explicit = SearchRequest(
        query="events",
        time_basis="event",
        time_window={"start": "2026-01-05T00:00:00Z", "end": "2026-01-12T00:00:00Z"},
    )
    repository = RecallRepository(
        connection, authority, config, deadline=time.monotonic() + 3, request=explicit
    )
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    # Revalidation must inspect the entire saved path, not only its final node.
    assert repository.valid_paths({"node6": items["node6"]["paths"]}, vector) == {"node6": []}
    assert [row["unit_id"] for row in repository.hydrate(("node2",))] == ["node2"]
    after = service.search(authority, request)
    assert {item["unit_id"] for item in after["items"]} == {"outside", "node0", "node1"}


def test_time_matched_raw_and_fully_supported_insight_receive_temporal_rank(recall_database):
    connection, authority = recall_database
    instant = datetime(2026, 1, 8, tzinfo=UTC)
    for node in ("a", "b"):
        add_fact(connection, authority, node, f"Shared plan {node}")
        annotate(connection, authority, node, instant)
    add_insight(connection, authority, "plan", ("a", "b"))
    connection.execute(
        "INSERT INTO search_units(tenant_id,scope_id,unit_id,kind,origin,source_id,source_version,"
        "evidence_id,passage_id,generation_id,content_text,locator,embedding,lexical_text) "
        "SELECT tenant_id,scope_id,'raw','passage','memory',source_id,source_version,evidence_id,"
        "'a',generation_id,content_text,locator,embedding,lexical_text FROM search_units "
        "WHERE tenant_id=%s AND unit_id='a'",
        (authority.tenant_id,),
    )
    annotate(connection, authority, "raw", instant)
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    service = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="generation",
    )
    request = SearchRequest(
        query="last week shared plan", recall_level="fast", reference_time="2026-01-14T12:00:00Z"
    )
    items = {item["unit_id"]: item for item in service.search(authority, request)["items"]}
    for node in ("raw", "plan"):
        assert items[node]["paths"] == [
            {"arm": "temporal", "relation": "time_match", "nodes": [node]}
        ]
    assert items["plan"]["time_evidence"] == []
    connection.execute(
        "UPDATE unit_times SET time_span=tstzrange('2025-01-08','2025-01-08','[]') "
        "WHERE tenant_id=%s AND unit_id='b'",
        (authority.tenant_id,),
    )
    items = {item["unit_id"]: item for item in service.search(authority, request)["items"]}
    assert items["plan"]["paths"] == []
    assert items["raw"]["paths"][0]["arm"] == "temporal"
