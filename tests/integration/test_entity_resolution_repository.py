"""Resolved recurring objects and namesakes through the existing SQL membership path."""

import pytest
from test_core_http import core_database as core_database
from test_extraction_references import reference_client as reference_client
from test_recall_repository import add_fact
from test_recall_repository import recall_database as recall_database

from oam.config import load_config
from oam.db.inspection_entities import get_entity_memories, list_entities
from oam.indexing.entities import EntityRepository
from oam.paths import default_config_path

pytestmark = pytest.mark.integration


def repository(connection, authority):
    return EntityRepository(
        connection,
        load_config(default_config_path()).config,
        generation_id="generation",
        key=(authority.tenant_id, authority.user_id),
    )


def test_same_group_repeated_job_reuses_one_identity(recall_database):
    connection, authority = recall_database
    entities = repository(connection, authority)
    objects = {}
    identifiers = []
    for memory, quote in (
        ("first", "The daily-digest job ran on Monday."),
        ("second", "The daily-digest job ran on Tuesday."),
    ):
        add_fact(connection, authority, memory, quote, with_original=True)
        identifiers.append(
            entities.publish_fact(
                memory,
                (),
                quote,
                entity_mentions=(
                    {
                        "name": "daily-digest",
                        "quote": quote,
                        "object": "job",
                        "decision": "new",
                        "span": [4, 16],
                    },
                ),
                objects=objects,
            )
        )
    assert identifiers[0] == identifiers[1]
    page = list_entities(connection, authority)
    assert [(row["name"], row["memory_count"]) for row in page["items"]] == [("daily-digest", 2)]


def test_two_alex_in_one_fact_have_distinct_labels_and_original_spans(recall_database):
    connection, authority = recall_database
    quote = "Alex introduced another Alex."
    add_fact(connection, authority, "meeting", quote, with_original=True)
    entities = repository(connection, authority)
    ids = entities.publish_fact(
        "meeting",
        (),
        quote,
        entity_mentions=(
            {"name": "Alex", "quote": quote, "object": "left", "decision": "new", "span": [0, 4]},
            {
                "name": "Alex",
                "quote": quote,
                "object": "right",
                "decision": "different",
                "different_from": ["left"],
                "span": [24, 28],
            },
        ),
        objects={},
    )
    assert len(set(ids)) == 2
    page = list_entities(connection, authority)
    assert {row["name"] for row in page["items"]} == {"Alex-1", "Alex-2"}
    for entity_id, span in zip(ids, ([0, 4], [24, 28]), strict=True):
        detail = get_entity_memories(connection, authority, entity_id)
        assert detail["original_names"] == ["Alex"]
        assert detail["aliases"] == []
        chunk = detail["items"][0]["source_chunks"][0]
        assert chunk["text"] == quote
        assert chunk["mention_spans"] == [span]


@pytest.mark.parametrize("literal_first", [True, False])
def test_literal_suffix_collisions_never_merge_objects(recall_database, literal_first):
    connection, authority = recall_database
    entities, objects = repository(connection, authority), {}
    names = ("Alex-1", "Alex", "Alex") if literal_first else ("Alex", "Alex", "Alex-1")
    ids = []
    for index, name in enumerate(names):
        memory, quote = f"person-{index}", f"{name} is a distinct colleague number {index}."
        add_fact(connection, authority, memory, quote)
        ids.extend(
            entities.publish_fact(
                memory,
                (),
                quote,
                entity_mentions=(
                    {
                        "name": name,
                        "quote": quote,
                        "object": memory,
                        "decision": "different"
                        if name == "Alex" and "Alex" in names[:index]
                        else "new",
                        "different_from": list(objects) if index else [],
                        "span": [0, len(name)],
                    },
                ),
                objects=objects,
            )
        )
    assert len(set(ids)) == 3
    labels = [row["name"] for row in list_entities(connection, authority)["items"]]
    assert len(set(labels)) == 3
    assert set(labels) == (
        {"Alex-1", "Alex-2", "Alex-3"} if literal_first else {"Alex-1", "Alex-2", "Alex-1-1"}
    )


def test_unoffered_namesake_blocks_same_and_keeps_direct_fact(recall_database):
    connection, authority = recall_database
    entities = repository(connection, authority)
    for memory, quote in (("one", "Alex is a surgeon."), ("two", "Alex is a baker.")):
        add_fact(connection, authority, memory, quote)
        entities.publish_fact(memory, ("Alex",), quote)
    name = entities.reference_entities(["one"])["one"][0]
    quote = "Alex bought a bicycle."
    add_fact(connection, authority, "later", quote)
    result = entities.publish_fact(
        "later",
        (),
        quote,
        entity_mentions=(
            {
                "name": "Alex",
                "quote": quote,
                "object": "person",
                "decision": "same",
                "reference_id": "one",
                "reference_name_id": name["name_id"],
                "span": [0, 4],
            },
        ),
        candidates=({"name": "Alex", "reference_id": "one", "name_id": name["name_id"]},),
    )
    assert result == ()
    assert entities.diagnostics == ["incomplete_entity_candidates"]
    assert connection.execute(
        "SELECT content_text FROM search_units WHERE unit_id='later'"
    ).fetchone() == (quote,)


def test_prior_generation_identity_reused_without_relabelling_facts(recall_database):
    connection, authority = recall_database
    entities = repository(connection, authority)
    quote = "The daily-digest job delivers breakfast news."
    add_fact(connection, authority, "old", quote, with_original=True)
    first = entities.publish_fact(
        "old",
        (),
        quote,
        entity_mentions=(
            {
                "name": "daily-digest",
                "quote": quote,
                "object": "job",
                "decision": "new",
                "span": [4, 16],
            },
        ),
    )
    name = entities.reference_entities(["old"])["old"][0]
    quote = "The daily-digest job now delivers at 08:00."
    add_fact(connection, authority, "next", quote, with_original=True)
    for table in ("memories", "passages", "search_units"):
        key = "memory_id" if table == "memories" else "evidence_id"
        connection.execute(f"UPDATE {table} SET generation_id='next-generation' WHERE {key}='next'")
    entities.generation_id = "next-generation"
    second = entities.publish_fact(
        "next",
        (),
        quote,
        entity_mentions=(
            {
                "name": "daily-digest",
                "quote": quote,
                "object": "job",
                "decision": "same",
                "reference_id": "old",
                "reference_name_id": name["name_id"],
                "span": [4, 16],
            },
        ),
        candidates=({"name": "daily-digest", "reference_id": "old", "name_id": name["name_id"]},),
    )
    assert second == first
    assert list_entities(connection, authority)["items"][0]["memory_count"] == 2
    assert {row[0] for row in connection.execute("SELECT generation_id FROM unit_entities")} == {
        "generation",
        "next-generation",
    }


def test_explicit_alias_owned_by_one_namesake(recall_database):
    connection, authority = recall_database
    quote = "Alex introduced another Alex, also called Lex."
    add_fact(connection, authority, "meeting", quote, with_original=True)
    entities = repository(connection, authority)
    ids = entities.publish_fact(
        "meeting",
        (),
        quote,
        entity_mentions=(
            {"name": "Alex", "quote": quote, "object": "left", "decision": "new", "span": [0, 4]},
            {
                "name": "Alex",
                "quote": quote,
                "object": "right",
                "decision": "different",
                "different_from": ["left"],
                "span": [24, 28],
            },
            {
                "name": "Lex",
                "quote": quote,
                "object": "right",
                "decision": "different",
                "span": [42, 45],
            },
        ),
        aliases=({"canonical_name": "Alex", "alias": "Lex", "quote": quote, "object": "right"},),
    )
    assert get_entity_memories(connection, authority, ids[0])["aliases"] == []
    second = get_entity_memories(connection, authority, ids[1])
    assert second["aliases"] == ["Lex"]
    assert second["original_names"] == ["Alex"]
    assert second["items"][0]["source_chunks"][0]["mention_spans"] == [[24, 28], [42, 45]]


@pytest.mark.asyncio
async def test_recurring_job_through_public_retain_worker_and_recall(reference_client):
    import json

    from test_extraction_references import ReferenceModels, retain, run_source

    from oam.models.runtime import ModelResult, Usage

    client, dsn, tenant, config, app = reference_client

    class JobModels(ReferenceModels):
        def generate(self, role, **kwargs):
            if role != "extraction":
                return super().generate(role, **kwargs)
            self.generation_calls.append(role)
            envelope = json.loads(kwargs["user"])
            self.inputs.append(envelope)
            text = envelope["center"]["text"]
            candidates = [
                (ref, name)
                for ref in envelope.get("reference_facts", [])
                for name in ref.get("entities", [])
                if name["name"] == "daily-digest"
            ]
            mention = {"name": "daily-digest", "quote": text, "object": "job", "decision": "new"}
            if candidates:
                ref, name = candidates[0]
                mention.update(
                    decision="same", reference_id=ref["id"], reference_name_id=name["name_id"]
                )
            return ModelResult(
                content=json.dumps(
                    {
                        "outcome": "facts",
                        "facts": [
                            {
                                "text": text,
                                "quote": text,
                                "entity_mentions": [mention],
                            }
                        ],
                    }
                ),
                usage=Usage(
                    input_tokens=20, output_tokens=30, total_tokens=50, coverage="complete"
                ),
            )

    models = JobModels()
    app.state.runtime.models = models
    for label, text in (
        ("morning", "The daily-digest job delivers breakfast news."),
        ("changed", "The daily-digest job now delivers at 08:00."),
    ):
        await retain(client, label, [text])
        assert run_source(dsn, config, models)
    assert len(models.inputs) == 2
    assert models.generation_calls.count("extraction") == 2
    response = await client.get("/v1/users/lin/entities")
    assert response.status_code == 200, response.text
    entity = response.json()["items"][0]
    assert entity["name"] == "daily-digest" and entity["memory_count"] == 2
    assert response.json()["total"] == 1
    detail = await client.get(f"/v1/users/lin/entities/{entity['entity_id']}/memories")
    assert detail.status_code == 200, detail.text
    assert detail.json()["original_names"] == ["daily-digest"]
    assert len(detail.json()["items"]) == 2
    for mode in ("fast", "normal"):
        response = await client.post(
            "/v1/recall",
            json={
                "user_id": "lin",
                "query": "When does daily-digest deliver?",
                "recall_level": mode,
            },
        )
        assert response.status_code == 200, response.text
        assert "now delivers at 08:00" in response.json()["context"]


def test_names_paginate_independently_without_reloading_memories(recall_database, monkeypatch):
    import oam.db.inspection_entities as inspector

    connection, authority = recall_database
    entities = repository(connection, authority)
    names = ["Alex", "Ａｌｅｘ", "alex"]
    objects, ids = {}, []
    for index, name in enumerate(names):
        quote, memory = f"{name} maintains the same workshop.", f"workshop-{index}"
        add_fact(connection, authority, memory, quote, with_original=True)
        ids.append(
            entities.publish_fact(
                memory,
                (),
                quote,
                entity_mentions=(
                    {
                        "name": name,
                        "quote": quote,
                        "object": "owner",
                        "decision": "new",
                        "span": [0, 4],
                    },
                ),
                objects=objects,
            )[0]
        )
    assert len(set(ids)) == 1
    monkeypatch.setattr(inspector, "INSPECTION_PAGE_SIZE", 2)
    first = get_entity_memories(connection, authority, ids[0])
    assert first["original_names_total"] == 3 and first["names_next_after"]
    assert len(first["items"]) == 2 and first["next_after"]
    next_names = get_entity_memories(
        connection, authority, ids[0], names_after=first["names_next_after"]
    )
    assert next_names["items"] == []
    assert next_names["names_next_after"] is None
    assert set(first["original_names"] + next_names["original_names"]) == set(names)
    next_memories = get_entity_memories(connection, authority, ids[0], after=first["next_after"])
    assert len(next_memories["items"]) == 1
    with pytest.raises(ValueError, match="cursor"):
        get_entity_memories(connection, authority, ids[0], names_after='["original","missing"]')


def test_local_alias_reuse_still_requires_live_alias_declaration(recall_database):
    connection, authority = recall_database
    entities, objects = repository(connection, authority), {}
    quote = "Nova Research, also called NR, opened a laboratory."
    add_fact(connection, authority, "definition", quote)
    entity_id = entities.publish_fact(
        "definition",
        (),
        quote,
        entity_mentions=(
            {
                "name": "Nova Research",
                "quote": quote,
                "object": "lab",
                "decision": "new",
                "span": [0, 13],
            },
        ),
        aliases=(
            {"canonical_name": "Nova Research", "alias": "NR", "quote": quote, "object": "lab"},
        ),
        objects=objects,
    )[0]
    quote = "NR hired the user."
    add_fact(connection, authority, "alias-use", quote)
    entities.publish_fact(
        "alias-use",
        (),
        quote,
        entity_mentions=(
            {
                "name": "NR",
                "quote": quote,
                "object": "lab",
                "decision": "new",
                "span": [0, 2],
            },
        ),
        objects=objects,
    )
    assert get_entity_memories(connection, authority, entity_id)["memory_count"] == 2
    assert connection.execute(
        "SELECT n.name_kind FROM unit_entities m JOIN entity_names n "
        "USING(tenant_id,user_id,name_id) WHERE m.unit_id='alias-use'"
    ).fetchone() == ("alias",)
    # Keep an independent primary anchor while withdrawing only the alias declaration.
    quote = "Nova Research moved its laboratory."
    add_fact(connection, authority, "primary", quote)
    entities.publish_fact(
        "primary",
        (),
        quote,
        entity_mentions=(
            {
                "name": "Nova Research",
                "quote": quote,
                "object": "lab",
                "decision": "new",
                "span": [0, 13],
            },
        ),
        objects=objects,
    )
    connection.execute("UPDATE sources SET status='deleted' WHERE source_id='definition'")
    detail = get_entity_memories(connection, authority, entity_id)
    assert {item["memory_id"] for item in detail["items"]} == {"primary"}
    assert connection.execute(
        "SELECT content_text FROM search_units WHERE unit_id='alias-use'"
    ).fetchone() == ("NR hired the user.",)


@pytest.mark.parametrize("shared_handle", [False, True])
def test_same_fact_alias_then_primary_retains_independent_primary_membership(
    recall_database, shared_handle
):
    connection, authority = recall_database
    entities = repository(connection, authority)
    quote = "Nova Research, also called NR, opened a laboratory."
    add_fact(connection, authority, "definition", quote)
    entities.publish_fact(
        "definition",
        ("Nova Research",),
        quote,
        aliases=({"canonical_name": "Nova Research", "alias": "NR", "quote": quote},),
    )
    candidates = tuple(
        {"name": n["name"], "reference_id": "definition", "name_id": n["name_id"]}
        for n in entities.reference_entities(["definition"])["definition"]
    )
    quote = "NR, Nova Research, hired the user."
    add_fact(connection, authority, "both", quote, with_original=True)
    ids = entities.publish_fact(
        "both",
        (),
        quote,
        entity_mentions=tuple(
            {
                "name": name,
                "quote": quote,
                "object": "library" if shared_handle else name,
                "decision": "same",
                "reference_id": "definition",
                "span": span,
                "reference_name_id": next(c["name_id"] for c in candidates if c["name"] == name),
            }
            for name, span in (("NR", [0, 2]), ("Nova Research", [4, 17]))
        ),
        candidates=candidates,
    )
    assert len(ids) == 1
    connection.execute("UPDATE sources SET status='deleted' WHERE source_id='definition'")
    detail = get_entity_memories(connection, authority, ids[0])
    assert detail and detail["memory_count"] == 1
    assert detail["items"][0]["source_chunks"][0]["mention_spans"] == [[4, 17]]
    from psycopg.rows import dict_row

    from oam.exports.repository import ExportRepository

    with connection.cursor(row_factory=dict_row) as cursor:
        records = ExportRepository._entity_records(cursor)
    member = next(record for record in records if record["kind"] == "entity_membership")
    assert [mention["name"] for mention in member["source_mentions"]] == ["Nova Research"]


def test_legacy_literal_name_reserves_its_visible_label(recall_database):
    connection, authority = recall_database
    entities, objects = repository(connection, authority), {}
    quote = "Alex-1 owns a bakery."
    add_fact(connection, authority, "literal", quote)
    entities.publish_fact("literal", ("Alex-1",), quote)
    for index in range(2):
        memory, quote = f"person-{index}", f"Alex is distinct person {index}."
        add_fact(connection, authority, memory, quote)
        entities.publish_fact(
            memory,
            (),
            quote,
            entity_mentions=(
                {
                    "name": "Alex",
                    "quote": quote,
                    "object": memory,
                    "decision": "new" if not index else "different",
                    "span": [0, 4],
                },
            ),
            objects=objects,
        )
    assert {row["name"] for row in list_entities(connection, authority)["items"]} == {
        "Alex-1",
        "Alex-2",
        "Alex-3",
    }


def test_alias_declaration_cannot_silently_join_two_existing_identities(recall_database):
    connection, authority = recall_database
    entities = repository(connection, authority)
    for memory, name in (("long", "Nova Research"), ("short", "NR")):
        quote = f"{name} opened an office."
        add_fact(connection, authority, memory, quote)
        entities.publish_fact(memory, (name,), quote)
    candidates = tuple(
        {"name": n["name"], "reference_id": memory, "name_id": n["name_id"]}
        for memory, names in entities.reference_entities(["long", "short"]).items()
        for n in names
    )
    quote = "Nova Research, also called NR, hired the user."
    add_fact(connection, authority, "assertion", quote)
    entities.publish_fact(
        "assertion",
        (),
        quote,
        entity_mentions=(
            {
                "name": "Nova Research",
                "quote": quote,
                "object": "lab",
                "decision": "same",
                "reference_id": "long",
                "reference_name_id": next(
                    c["name_id"] for c in candidates if c["name"] == "Nova Research"
                ),
                "span": [0, 13],
            },
        ),
        aliases=(
            {"canonical_name": "Nova Research", "alias": "NR", "quote": quote, "object": "lab"},
        ),
        candidates=candidates,
    )
    assert "entity_identity_conflict" in entities.diagnostics
    assert connection.execute(
        "SELECT count(*) FROM entity_names WHERE name_key='nr'"
    ).fetchone() == (1,)
