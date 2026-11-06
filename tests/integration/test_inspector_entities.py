"""Entity inspection uses current fact support, including alias anchors."""

import pytest
from test_core_http import core_database as core_database
from test_entity_graph import alias_history
from test_recall_repository import recall_database as recall_database

from oam.db.inspection_entities import entity_graph, get_entity_memories, list_entities

pytestmark = pytest.mark.integration


def test_entities_search_and_current_support(recall_database):
    connection, authority = recall_database
    _, _, entity_id = alias_history(connection, authority)
    page = list_entities(connection, authority, query="NR")
    assert [item["entity_id"] for item in page["items"]] == [entity_id]
    assert page["total"] == 1
    assert page["items"][0]["first_mentioned_at"] is not None
    assert page["items"][0]["name"] == "Nova Research"
    graph = entity_graph(connection, authority)
    assert graph["total"] == 1
    assert graph["nodes"][0]["entity_id"] == entity_id
    assert graph["edges"] == []
    detail = get_entity_memories(connection, authority, entity_id)
    assert {item["memory_id"] for item in detail["items"]} == {"definition", "seed", "continuation"}
    connection.execute("UPDATE sources SET status='deleted' WHERE source_id='definition'")
    assert list_entities(connection, authority, query="NR")["items"] == []
    detail = get_entity_memories(connection, authority, entity_id)
    assert {item["memory_id"] for item in detail["items"]} == {"seed"}
    assert get_entity_memories(connection, authority, "missing") is None


def test_graph_cooccurrence_counts_distinct_facts_and_revocation(recall_database):
    from pathlib import Path

    from test_recall_repository import add_fact

    from oam.config import load_config
    from oam.indexing.entities import EntityRepository

    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    for memory_id in ("shared_one", "shared_two"):
        text = "Nova Research works with Luna Labs."
        add_fact(connection, authority, memory_id, text, with_original=True)
        repository.publish_fact(
            memory_id,
            ("Nova Research", "Luna Labs"),
            text,
            references=tuple(
                {"name": name, "reference_id": "shared_one", "quote": text}
                for name in ("Nova Research", "Luna Labs")
            )
            if memory_id == "shared_two"
            else (),
        )
    graph = entity_graph(connection, authority)
    assert graph["total"] == 2
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["weight"] == 2
    assert {graph["edges"][0]["source"], graph["edges"][0]["target"]} == {
        node["entity_id"] for node in graph["nodes"]
    }
    connection.execute("UPDATE sources SET status='deleted' WHERE source_id='shared_one'")
    assert entity_graph(connection, authority)["edges"][0]["weight"] == 1
    connection.execute("UPDATE sources SET status='deleted' WHERE source_id='shared_two'")
    graph = entity_graph(connection, authority)
    assert graph["nodes"] == []
    assert graph["edges"] == []


def test_entity_timeline_uses_mention_time_and_current_entity_cursor(recall_database):
    connection, authority = recall_database
    _, _, entity_id = alias_history(connection, authority)
    connection.execute(
        "UPDATE search_units SET mentioned_at=CASE unit_id "
        "WHEN 'seed' THEN '2000-01-01'::timestamptz "
        "WHEN 'definition' THEN '2001-01-01'::timestamptz "
        "ELSE NULL END"
    )
    page = get_entity_memories(connection, authority, entity_id)
    assert [item["unit_id"] for item in page["items"]] == ["seed", "definition", "continuation"]
    assert page["items"][0]["mention_time"].year == 2000
    assert page["items"][0]["mention_time"] < page["items"][0]["created_at"]
    assert page["items"][-1]["mention_time"] == page["items"][-1]["created_at"]
    rest = get_entity_memories(connection, authority, entity_id, after="seed")
    assert [item["unit_id"] for item in rest["items"]] == ["definition", "continuation"]
    with pytest.raises(ValueError, match="cursor"):
        get_entity_memories(connection, authority, entity_id, after="missing")


def test_entity_list_orders_counts_globally_and_cursor_preserves_ties(recall_database):
    connection, authority = recall_database
    _, _, entity_id = alias_history(connection, authority)
    # Existing canonical/alias history contributes three memories to one identity.
    from pathlib import Path

    from test_recall_repository import add_fact

    from oam.config import load_config
    from oam.indexing.entities import EntityRepository

    repository = EntityRepository(
        connection,
        load_config(Path(__file__).parents[2] / "configs/oam.yaml").config,
        generation_id="generation",
        key=(authority.tenant_id, authority.user_id),
    )
    add_fact(
        connection, authority, "other", "Aster Labs works with Zenith Labs.", with_original=True
    )
    repository.publish_fact(
        "other", ("Aster Labs", "Zenith Labs"), "Aster Labs works with Zenith Labs."
    )
    page = list_entities(connection, authority)
    assert page["total"] == 3
    assert page["items"][0]["entity_id"] == entity_id
    assert [item["memory_count"] for item in page["items"]] == [3, 1, 1]
    tied_ids = [item["entity_id"] for item in page["items"][1:]]
    assert tied_ids == sorted(tied_ids)
    rest = list_entities(connection, authority, after=entity_id)
    assert [item["entity_id"] for item in rest["items"]] == tied_ids
    assert rest["total"] == 3
    assert [
        item["entity_id"]
        for item in list_entities(connection, authority, after=tied_ids[0])["items"]
    ] == tied_ids[1:]
    with pytest.raises(ValueError, match="cursor"):
        list_entities(connection, authority, after="missing")


def test_entity_detail_returns_original_chunk_and_observed_alias(recall_database):
    connection, authority = recall_database
    _, _, entity_id = alias_history(connection, authority)
    original = (
        "During our conversation I said: I joined NR in June, "
        "the organization I mentioned before. More source context."
    )
    connection.execute(
        "UPDATE passages SET normalized_text=%s WHERE passage_id='continuation'", (original,)
    )
    connection.execute(
        "UPDATE search_units SET content_text='I joined the organization in June.' "
        "WHERE unit_id='continuation'"
    )
    page = get_entity_memories(connection, authority, entity_id)
    item = next(row for row in page["items"] if row["unit_id"] == "continuation")
    assert item["text"] == "I joined the organization in June."
    assert item["source_chunks"] == [
        {
            "source_id": "continuation",
            "source_version": 1,
            "passage_id": "continuation",
            "text": original,
            "mentions": ["NR"],
            "mention_spans": None,
            "quote": "I joined NR in June, the organization I mentioned before.",
        }
    ]
    connection.execute("UPDATE sources SET status='deleted' WHERE source_id='continuation'")
    assert all(
        row["unit_id"] != "continuation"
        for row in get_entity_memories(connection, authority, entity_id)["items"]
    )
