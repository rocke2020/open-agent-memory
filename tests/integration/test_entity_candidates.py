"""Eligible per-name candidates offer identity context without resolving it."""

from pathlib import Path

import pytest
from test_recall_repository import add_fact
from test_recall_repository import recall_database as _recall_database

from oam.config import load_config
from oam.indexing.entities import EntityRepository

pytestmark = pytest.mark.integration
recall_database = _recall_database


def candidate_history(connection, authority):
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    definition = "Nova Research, also called NR, opened a laboratory."
    add_fact(connection, authority, "definition", definition, direction=-1.0)
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    identity = repository.publish_fact(
        "definition",
        ("Nova Research",),
        definition,
        aliases=(
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": "Nova Research, also called NR",
            },
        ),
    )[0]
    return config, repository, identity


def test_alias_name_can_propose_its_supporting_fact_without_semantic_similarity(recall_database):
    from oam.indexing.entity_candidates import eligible_entity_name_candidates

    connection, authority = recall_database
    config, _, identity = candidate_history(connection, authority)

    rows = eligible_entity_name_candidates(
        connection,
        config,
        generation_id="generation",
        text="I joined NR in June.",
        excluded_source_id="new-source",
    )

    assert [row["entity_id"] for row in rows] == [identity]
    assert rows[0]["names"][0]["name"] == "NR"
    assert rows[0]["names"][0]["memory_id"] == "definition"
    assert rows[0]["names"][0]["quote"] == "Nova Research, also called NR"


def extraction_store(connection, authority, config, text, *, max_facts=2):
    from contextlib import contextmanager
    from datetime import UTC, datetime, timedelta

    from oam.db.context import bind_user
    from oam.indexing.store import IndexStore
    from oam.workers.claims import ClaimedWork

    add_fact(connection, authority, "new-source", text)
    key = (authority.tenant_id, authority.user_id)
    connection.execute(
        "INSERT INTO principals(tenant_id,principal_id,role,state) "
        "VALUES (%s,'worker','reader','active')",
        (authority.tenant_id,),
    )
    connection.execute(
        "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,source_id,"
        "source_version,kind,schema_version,status) "
        "VALUES (%s,%s,'op','worker','new-source',1,'retain',1,'running')",
        key,
    )
    connection.execute(
        "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,input_digest,"
        "generation_id,status,owner_id,fence,lease_expires_at,attempts_remaining) "
        "VALUES (%s,%s,'work','op','index_source',%s,'generation','running','worker',1,"
        "clock_timestamp()+interval '1 hour',1)",
        (*key, b"d" * 32),
    )
    connection.execute(
        "INSERT INTO attempts(tenant_id,user_id,work_unit_id,attempt_id,"
        "ordinal,owner_id,fence,disposition) "
        "VALUES (%s,%s,'work','attempt',1,'worker',1,'claimed')",
        key,
    )

    class RollbackStore(IndexStore):
        @contextmanager
        def transaction(self):
            bind_user(connection, authority)
            yield connection

    store = object.__new__(RollbackStore)
    store.key = key
    store.worker_id = "worker"
    store.generation_id = "generation"
    store.claim = ClaimedWork(
        *key, "work", "op", "index_source", "attempt", 1, datetime.now(UTC) + timedelta(hours=1)
    )
    store.source = {
        "source_id": "new-source",
        "source_version": 1,
        "input_digest": b"d" * 32,
        "policy_epoch": 0,
    }
    store.config = config.model_copy(
        update={
            "extraction": config.extraction.model_copy(update={"reference_max_facts": max_facts})
        }
    )
    return store


def test_lexical_alias_reference_survives_full_dense_pool_in_frozen_input(recall_database):
    from oam.indexing.references import freeze_extraction_references

    connection, authority = recall_database
    config, _, _ = candidate_history(connection, authority)
    for memory_id in ("a_distractor", "b_distractor"):
        add_fact(connection, authority, memory_id, "An unrelated nearby semantic fact.")
    text = "I joined NR in June."
    store = extraction_store(connection, authority, config, text)
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)

    references = freeze_extraction_references(store, "new-source", vector, {"text": text})

    assert [reference["id"] for reference in references] == ["definition", "a_distractor"]
    assert any(name["name"] == "NR" for name in references[0]["entities"])


def test_matching_homonyms_remain_distinct_candidate_identities(recall_database):
    from oam.indexing.entity_candidates import eligible_entity_name_candidates

    connection, authority = recall_database
    config, entities, _ = candidate_history(connection, authority)
    ids = set()
    for memory_id, text in (
        ("coworker", "Alex is my coworker."),
        ("teacher", "Another Alex teaches swimming."),
    ):
        add_fact(connection, authority, memory_id, text)
        ids.update(entities.publish_fact(memory_id, ("Alex",), text))

    candidates = eligible_entity_name_candidates(
        connection,
        config,
        generation_id="generation",
        text="I met Alex again.",
        excluded_source_id="new-source",
    )

    assert {row["entity_id"] for row in candidates} == ids
    assert {row["names"][0]["memory_id"] for row in candidates} == {"coworker", "teacher"}


def test_more_matched_aliases_do_not_inflate_entity_rank_or_crowd_other_identity(recall_database):
    from oam.indexing.entity_candidates import eligible_entity_name_candidates

    connection, authority = recall_database
    config, entities, identity = candidate_history(connection, authority)
    config = config.model_copy(
        update={
            "index": config.index.model_copy(
                update={"entity_resolution_candidates": 2, "entity_search_names_per_entity": 2}
            )
        }
    )
    add_fact(connection, authority, "other", "NR Operations hired a technician.")
    other = entities.publish_fact("other", ("NR Operations",), "NR Operations hired a technician.")[
        0
    ]
    before = eligible_entity_name_candidates(
        connection, config, generation_id="generation", text="NR", excluded_source_id="new-source"
    )
    initial_rank = next(row["rank"] for row in before if row["entity_id"] == identity)
    aliases = tuple(
        {
            "canonical_name": "Nova Research",
            "alias": name,
            "quote": f"Nova Research is also called {name}.",
        }
        for name in ("NR lab", "NR institute", "NR workshop", "NR research")
    )
    text = " ".join(assertion["quote"] for assertion in aliases)
    add_fact(connection, authority, "more-names", text)
    entities.publish_fact(
        "more-names",
        ("Nova Research",),
        text,
        aliases=aliases,
        references=({"name": "Nova Research", "reference_id": "definition", "quote": text},),
    )

    after = eligible_entity_name_candidates(
        connection, config, generation_id="generation", text="NR", excluded_source_id="new-source"
    )

    assert {row["entity_id"] for row in after} == {identity, other}
    assert next(row["rank"] for row in after if row["entity_id"] == identity) == initial_rank
    assert all(len(row["names"]) <= 2 for row in after)


def test_revoked_alias_support_is_excluded_before_entity_candidate_cap(recall_database):
    from oam.indexing.entity_candidates import eligible_entity_name_candidates

    connection, authority = recall_database
    config, entities, _ = candidate_history(connection, authority)
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"entity_resolution_candidates": 1})}
    )
    add_fact(connection, authority, "visible", "NR Operations hired a technician.")
    visible = entities.publish_fact(
        "visible", ("NR Operations",), "NR Operations hired a technician."
    )[0]
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND user_id=%s "
        "AND evidence_id='definition'",
        (authority.tenant_id, authority.user_id),
    )

    rows = eligible_entity_name_candidates(
        connection,
        config,
        generation_id="generation",
        text="NR Nova Research",
        excluded_source_id="new-source",
    )

    assert [row["entity_id"] for row in rows] == [visible]


def test_candidate_name_scope_and_generation_are_filtered_before_bounds(recall_database):
    from oam.auth.credentials import AuthorizedUser
    from oam.db.context import bind_user
    from oam.indexing.entity_candidates import eligible_entity_name_candidates

    connection, authority = recall_database
    config, _, identity = candidate_history(connection, authority)
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"entity_resolution_candidates": 1})}
    )
    for tenant, user in (
        (authority.tenant_id + "-foreign", "scope"),
        (authority.tenant_id, "foreign"),
    ):
        if tenant != authority.tenant_id:
            connection.execute(
                "INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (tenant,)
            )
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,index_generation_id) "
            "VALUES (%s,%s,'synthetic','test','persistent','generation')",
            (tenant, user),
        )
        foreign = AuthorizedUser("key", tenant, "reader", user, frozenset({"memory:read"}))
        candidate_history(connection, foreign)
    bind_user(connection, authority)

    rows = eligible_entity_name_candidates(
        connection, config, generation_id="generation", text="NR", excluded_source_id="new-source"
    )

    assert [row["entity_id"] for row in rows] == [identity]
    assert (
        eligible_entity_name_candidates(
            connection,
            config,
            generation_id="old-generation",
            text="NR",
            excluded_source_id="new-source",
        )
        == []
    )
    assert (
        eligible_entity_name_candidates(
            connection,
            config,
            generation_id="generation",
            text="NR",
            excluded_source_id="definition",
        )
        == []
    )


def test_unfinished_freeze_removes_revoked_alias_metadata_without_reselecting(recall_database):
    from oam.indexing.references import freeze_extraction_references

    connection, authority = recall_database
    config, entities, _ = candidate_history(connection, authority)
    seed = "Nova Research, the organization I mentioned, moved its laboratory."
    add_fact(connection, authority, "a_seed", seed)
    entities.publish_fact(
        "a_seed",
        ("Nova Research",),
        seed,
        references=({"name": "Nova Research", "reference_id": "definition", "quote": seed},),
    )
    text = "I joined NR."
    store = extraction_store(connection, authority, config, text)
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    references = freeze_extraction_references(store, "new-source", vector, {"text": text})
    assert [ref["id"] for ref in references] == ["definition", "a_seed"]
    assert any(name["name"] == "NR" for name in references[1]["entities"])
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND user_id=%s "
        "AND evidence_id='definition'",
        store.key,
    )

    retried = freeze_extraction_references(store, "new-source", vector, {"text": text})

    assert [ref["id"] for ref in retried] == ["a_seed"]
    assert {name["name"] for name in retried[0]["entities"]} == {"Nova Research"}


@pytest.mark.parametrize("stage_key", ["extract:new-source", "extract:new-source:recipe=old"])
def test_completed_extraction_reuses_exact_frozen_context_after_support_changes(
    recall_database, stage_key
):
    from psycopg.types.json import Jsonb

    from oam.indexing.references import freeze_extraction_references
    from oam.indexing.store import input_digest

    connection, authority = recall_database
    config, _, _ = candidate_history(connection, authority)
    text = "I joined NR."
    inputs = {"text": text}
    store = extraction_store(connection, authority, config, text)
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    references = freeze_extraction_references(store, "new-source", vector, inputs)
    connection.execute(
        "INSERT INTO index_stage_results(tenant_id,user_id,source_id,source_version,"
        "generation_id,stage_key,input_digest,output,work_unit_id,attempt_id) "
        "VALUES (%s,%s,'new-source',1,'generation',%s,%s,%s,'work','attempt')",
        (
            *store.key,
            stage_key,
            input_digest({**inputs, "reference_facts": references}),
            Jsonb({"outcome": "facts"}),
        ),
    )
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND user_id=%s "
        "AND evidence_id='definition'",
        store.key,
    )

    assert freeze_extraction_references(store, "new-source", vector, inputs) == references


def test_unchanged_unfinished_freeze_keeps_selected_name_preference(recall_database):
    from oam.indexing.references import freeze_extraction_references

    connection, authority = recall_database
    config, _, _ = candidate_history(connection, authority)
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"entity_search_names_per_entity": 1})}
    )
    chosen = connection.execute(
        "SELECT name_text FROM entity_names WHERE tenant_id=%s AND user_id=%s "
        "ORDER BY name_id DESC LIMIT 1",
        (authority.tenant_id, authority.user_id),
    ).fetchone()[0]
    text = f"I joined {chosen}."
    store = extraction_store(connection, authority, config, text, max_facts=1)
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    inputs = {"text": text}
    original = freeze_extraction_references(store, "new-source", vector, inputs)
    assert original[0]["entities"][0]["name"] == chosen

    assert freeze_extraction_references(store, "new-source", vector, inputs) == original
