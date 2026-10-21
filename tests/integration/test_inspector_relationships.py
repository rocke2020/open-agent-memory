"""Inspector connections expose stored evidence under current memory policy."""

import pytest
from test_core_http import core_database as core_database
from test_entity_graph import alias_history
from test_recall_repository import add_fact
from test_recall_repository import recall_database as recall_database

from oam.db.inspection import SourceInspector

pytestmark = pytest.mark.integration


def test_detail_exposes_entities_direction_and_temporal_basis(recall_database):
    connection, authority = recall_database
    _, _, entity_id = alias_history(connection, authority)
    key = (authority.tenant_id, authority.user_id)
    connection.execute(
        "UPDATE search_units SET mentioned_at=CASE unit_id WHEN 'definition' THEN "
        "'2026-10-01T00:00:00Z'::timestamptz ELSE '2026-10-02T00:00:00Z'::timestamptz END",
    )
    connection.execute(
        "INSERT INTO unit_links VALUES (%s,%s,'seed','definition','caused_by',1),"
        "(%s,%s,'continuation','seed','updates',1)",
        (*key, *key),
    )
    connection.execute(
        "INSERT INTO temporal_links(tenant_id,user_id,time_basis,left_id,right_id,"
        "gap_seconds,weight) VALUES (%s,%s,'mentioned','definition','seed',86400,0.5)",
        key,
    )
    detail = SourceInspector().get_memory(connection, authority, "seed", kind="fact")
    assert detail is not None
    assert detail["entities"] == [
        {"entity_id": entity_id, "name": "Nova Research", "mention": "Nova Research"}
    ]
    relationships = detail["relationships"]
    causal = next(item for item in relationships if item["kind"] == "caused_by")
    assert causal["direction"] == "outgoing"
    assert causal["memory_id"] == "definition"
    assert causal["text"] == "Nova Research, also called NR, opened a laboratory."
    update = next(item for item in relationships if item["kind"] == "updates")
    assert (update["direction"], update["memory_id"]) == ("incoming", "continuation")
    temporal = next(item for item in relationships if item["kind"] == "temporal")
    assert (temporal["time_basis"], temporal["gap_seconds"]) == ("mentioned", 86400)
    assert temporal["time"]["memory_start"].startswith("2026-10-02")
    assert temporal["time"]["related_start"].startswith("2026-10-01")
    shared = [item for item in relationships if item["kind"] == "shared_entity"]
    assert {item["memory_id"] for item in shared} == {"definition", "continuation"}
    assert all(item["entity_id"] == entity_id for item in shared)
    assert detail["relationships_truncated"] is False


def test_connections_exclude_withdrawn_endpoints_and_entity_witnesses(recall_database):
    connection, authority = recall_database
    alias_history(connection, authority)
    key = (authority.tenant_id, authority.user_id)
    add_fact(connection, authority, "hidden", "Private withdrawn detail.", eligible=False)
    connection.execute("INSERT INTO unit_links VALUES (%s,%s,'seed','hidden','caused_by',1)", key)
    connection.execute(
        "UPDATE sources SET status='deleted' WHERE tenant_id=%s AND user_id=%s "
        "AND source_id='definition'",
        key,
    )
    detail = SourceInspector().get_memory(connection, authority, "seed", kind="fact")
    assert detail is not None
    # The seed still directly supports its canonical mention; the alias loses its anchor.
    assert [entity["name"] for entity in detail["entities"]] == ["Nova Research"]
    assert detail["relationships"] == []
