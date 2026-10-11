"""Recorded alias witnesses follow their exact supporting source lifecycle."""

import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from psycopg_pool import ConnectionPool
from test_recall_repository import add_fact
from test_recall_repository import recall_database as recall_database

from oam.config import load_config
from oam.db.sources import SourceRepository
from oam.debug.traces import TraceRepository
from oam.governance.cleanup import affected_trace_ids, cleanup_claim, cleanup_removal_claim
from oam.governance.removal import RemovalRepository
from oam.indexing.entities import EntityRepository
from oam.retrieval.context import render_context
from oam.retrieval.repository import RecallRepository
from oam.tokens import estimate_units, estimated_tokens
from oam.workers.claims import ClaimedWork

pytestmark = pytest.mark.integration


def alias_trace(connection, authority, *, independent=False, same_alias=False):
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute(
        "INSERT INTO principals(tenant_id,principal_id,role,state) VALUES "
        "(%s,'reader','user','active')",
        (authority.tenant_id,),
    )
    entities = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    definition = "Aster Station, also called AS, runs the field program."
    add_fact(connection, authority, "definition", definition)
    entities.publish_fact(
        "definition",
        ("Aster Station", "AS"),
        definition,
        aliases=(
            {
                "canonical_name": "Aster Station",
                "alias": "AS",
                "quote": "Aster Station, also called AS",
            },
        ),
    )
    for memory, name, quote in (
        (
            "seed",
            "AS" if same_alias else "Aster Station",
            "The AS station needs calibration."
            if same_alias
            else "Aster Station needs calibration.",
        ),
        ("target", "AS", "The AS sensor is in Room 418."),
    ):
        add_fact(connection, authority, memory, quote)
        entities.publish_fact(
            memory,
            (name,),
            quote,
            references=({"name": name, "reference_id": "definition", "quote": quote},),
        )
    if independent:
        quote = "Aster Station, also called AS, has a monitoring team."
        add_fact(connection, authority, "other-definition", quote)
        entities.publish_fact(
            "other-definition",
            ("Aster Station", "AS"),
            quote,
            aliases=(
                {
                    "canonical_name": "Aster Station",
                    "alias": "AS",
                    "quote": "Aster Station, also called AS",
                },
            ),
            references=({"name": "Aster Station", "reference_id": "seed", "quote": quote},),
        )
    row = connection.execute(
        "SELECT ns.support_id,ns.name_id,n.entity_id,ns.quote FROM "
        "entity_name_support ns JOIN entity_names n "
        "USING(tenant_id,user_id,name_id) WHERE ns.tenant_id=%s AND ns.user_id=%s "
        "AND ns.memory_id='definition' AND ns.support_kind='alias'",
        (authority.tenant_id, authority.user_id),
    ).fetchone()
    witness = {
        "support_id": row[0],
        "name_id": row[1],
        "entity_id": row[2],
        "quote": row[3],
        "citation": {
            "source_id": "definition",
            "source_version": 1,
            "span": {"source_id": "definition", "passage_id": "definition", "quote": row[3]},
        },
    }
    names = dict(
        connection.execute(
            "SELECT unit_id,name_id FROM unit_entities WHERE tenant_id=%s AND user_id=%s "
            "AND unit_id=ANY(%s)",
            (authority.tenant_id, authority.user_id, ["seed", "target"]),
        ).fetchall()
    )
    assert (names["seed"] == names["target"]) is same_alias
    result = {
        "items": [
            {
                "unit_id": "target",
                "kind": "fact",
                "origin": "memory",
                "text": "The AS sensor is in Room 418.",
                "citations": [],
                "time_evidence": [],
                "paths": [
                    {
                        "source": "entity",
                        "entity": witness["entity_id"],
                        "nodes": ["seed", "target"],
                        "name_ids": [names["seed"], names["target"]],
                        "alias_witnesses": [witness],
                    }
                ],
            }
        ],
        "tokenizer": "oam-unicode-quarter-v1",
    }
    result["context"] = render_context(result["items"])
    result["context_tokens"] = estimated_tokens(estimate_units(result["context"]))
    recall = RecallRepository(connection, authority, config, deadline=time.monotonic() + 30)
    assert recall.record_trace(
        "alias-trace",
        0,
        {"arms": {"dense": ["seed"], "graph": ["target"]}, "result_ids": ["target"]},
        result,
    )
    debug_authority = replace(authority, capabilities=frozenset({"debug:read"}))
    assert TraceRepository().get(connection, debug_authority, "alias-trace")["status"] == "recorded"
    return debug_authority


@pytest.mark.parametrize("same_alias", [False, True])
@pytest.mark.parametrize("change", ["source", "version", "memory"])
def test_recorded_alias_support_only_witness_revocation_redacts_debug_and_replay(
    recall_database, change, same_alias
):
    connection, authority = recall_database
    debug_authority = alias_trace(connection, authority, same_alias=same_alias)
    table, condition, assignment = {
        "source": ("sources", "source_id='definition'", "status='deleted'"),
        "version": ("source_versions", "source_id='definition'", "status='superseded'"),
        "memory": ("memories", "memory_id='definition'", "lifecycle_state='excluded'"),
    }[change]
    connection.execute(
        f"UPDATE {table} SET {assignment} WHERE tenant_id=%s AND user_id=%s AND {condition}",
        (authority.tenant_id, authority.user_id),
    )
    for mode in (None, "inspect_recorded", "reassemble_recorded"):
        debug = TraceRepository()
        result = (
            debug.get(connection, debug_authority, "alias-trace")
            if mode is None
            else debug.replay(connection, debug_authority, "alias-trace", mode)
        )
        assert result == {"trace_id": "alias-trace", "status": "policy_redacted"}


def test_recorded_witness_does_not_substitute_an_independent_alias_definition(recall_database):
    connection, authority = recall_database
    debug_authority = alias_trace(connection, authority, independent=True)
    connection.execute(
        "UPDATE sources SET status='deleted' WHERE tenant_id=%s AND user_id=%s AND "
        "source_id='definition'",
        (authority.tenant_id, authority.user_id),
    )
    assert TraceRepository().get(connection, debug_authority, "alias-trace") == {
        "trace_id": "alias-trace",
        "status": "policy_redacted",
    }


@pytest.mark.parametrize("kind", ["source", "memory"])
def test_cleanup_physically_redacts_support_only_alias_trace(recall_database, kind):
    connection, authority = recall_database
    alias_trace(connection, authority)
    delete_authority = replace(authority, capabilities=frozenset({"source:delete"}))
    if kind == "memory":
        operation = (
            RemovalRepository()
            .remove(
                connection,
                delete_authority,
                target_kind="memory",
                target_id="definition",
                attempts_remaining=1,
            )
            .operation_id
        )
    else:
        operation = "source-cleanup"
        SourceRepository().delete(
            connection,
            delete_authority,
            source_id="definition",
            resource_only=False,
            operation_id=operation,
            work_unit_id="source-work",
            attempts_remaining=1,
        )
    work_id = connection.execute(
        "UPDATE work_units SET "
        "owner_id='fixture-worker',fence=1,status='running',lease_expires_at=clock_t"
        "imestamp()+interval '1 hour' WHERE tenant_id=%s AND user_id=%s AND "
        "operation_id=%s RETURNING work_unit_id",
        (authority.tenant_id, authority.user_id, operation),
    ).fetchone()[0]
    claim = ClaimedWork(
        authority.tenant_id,
        authority.user_id,
        work_id,
        operation,
        "cleanup_" + kind,
        "attempt",
        1,
        datetime.now(UTC) + timedelta(hours=1),
    )

    class RollbackPool:
        @contextmanager
        def connection(self):
            yield connection

    cleanup = cleanup_removal_claim if kind == "memory" else cleanup_claim
    cleanup("unused", claim, "fixture-worker", pool=cast(ConnectionPool, RollbackPool()))
    assert connection.execute(
        "SELECT content,decisions FROM traces WHERE tenant_id=%s AND user_id=%s AND "
        "trace_id='alias-trace'",
        (authority.tenant_id, authority.user_id),
    ).fetchone() == (None, {})
    assert (
        connection.execute(
            "SELECT name_text FROM entity_names WHERE tenant_id=%s AND user_id=%s AND "
            "name_text='AS'",
            (authority.tenant_id, authority.user_id),
        ).fetchall()
        == []
    )
    assert connection.execute(
        "SELECT canonical_claim FROM memories WHERE tenant_id=%s AND user_id=%s AND "
        "memory_id='target'",
        (authority.tenant_id, authority.user_id),
    ).fetchone() == ("The AS sensor is in Room 418.",)


def remove_and_cleanup(connection, authority, memory_id):
    delete_authority = replace(authority, capabilities=frozenset({"source:delete"}))
    operation = (
        RemovalRepository()
        .remove(
            connection,
            delete_authority,
            target_kind="memory",
            target_id=memory_id,
            attempts_remaining=1,
        )
        .operation_id
    )
    work_id = connection.execute(
        "UPDATE work_units SET "
        "owner_id='fixture-worker',fence=1,status='running',lease_expires_at=clock_t"
        "imestamp()+interval '1 hour' WHERE tenant_id=%s AND user_id=%s AND "
        "operation_id=%s RETURNING work_unit_id",
        (authority.tenant_id, authority.user_id, operation),
    ).fetchone()[0]
    claim = ClaimedWork(
        authority.tenant_id,
        authority.user_id,
        work_id,
        operation,
        "cleanup_memory",
        "attempt",
        1,
        datetime.now(UTC) + timedelta(hours=1),
    )

    class RollbackPool:
        @contextmanager
        def connection(self):
            yield connection

    cleanup_removal_claim(
        "unused", claim, "fixture-worker", pool=cast(ConnectionPool, RollbackPool())
    )


def test_last_supported_name_cleanup_removes_entity_labels_and_memberships(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute(
        "INSERT INTO principals(tenant_id,principal_id,role,state) VALUES "
        "(%s,'reader','user','active')",
        (authority.tenant_id,),
    )
    quote = "Lumen Field Laboratory, also called LFL, opened yesterday."
    add_fact(connection, authority, "definition", quote)
    entities = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    entity_id = entities.publish_fact(
        "definition",
        ("Lumen Field Laboratory", "LFL"),
        quote,
        aliases=(
            {
                "canonical_name": "Lumen Field Laboratory",
                "alias": "LFL",
                "quote": "Lumen Field Laboratory, also called LFL",
            },
        ),
    )[0]
    remove_and_cleanup(connection, authority, "definition")
    assert (
        connection.execute(
            "SELECT entity_id FROM entity_units WHERE tenant_id=%s AND user_id=%s AND entity_id=%s",
            (authority.tenant_id, authority.user_id, entity_id),
        ).fetchall()
        == []
    )
    assert (
        connection.execute(
            "SELECT name_text FROM entity_names WHERE tenant_id=%s AND user_id=%s",
            (authority.tenant_id, authority.user_id),
        ).fetchall()
        == []
    )
    assert (
        connection.execute(
            "SELECT entity_id FROM unit_entities WHERE tenant_id=%s AND user_id=%s",
            (authority.tenant_id, authority.user_id),
        ).fetchall()
        == []
    )


def test_cleanup_refreshes_display_from_independent_supported_canonical_name(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute(
        "INSERT INTO principals(tenant_id,principal_id,role,state) VALUES "
        "(%s,'reader','user','active')",
        (authority.tenant_id,),
    )
    entities = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    add_fact(connection, authority, "old", "The Cedar Field Project opened in May.")
    entity_id = entities.publish_fact(
        "old", ("Cedar Field Project",), "The Cedar Field Project opened in May."
    )[0]
    quote = "The same project now uses the name Willow Field Project."
    add_fact(connection, authority, "new", quote)
    # Materialize the schema-valid alternate canonical name and its exact source support.
    name_id = entities._add_name(entity_id, "Willow Field Project", "canonical")
    entities._support(name_id, "new", quote)
    connection.execute(
        "INSERT INTO "
        "unit_entities(tenant_id,user_id,unit_id,entity_key,entity_id,name_id,observ"
        "ed_mention,mention_quote,generation_id) VALUES (%s,%s,'new','willow field "
        "project',%s,%s,'Willow Field Project',%s,'generation')",
        (authority.tenant_id, authority.user_id, entity_id, name_id, quote),
    )
    remove_and_cleanup(connection, authority, "old")
    assert connection.execute(
        "SELECT canonical_name FROM entity_units WHERE tenant_id=%s AND user_id=%s "
        "AND entity_id=%s",
        (authority.tenant_id, authority.user_id, entity_id),
    ).fetchone() == ("Willow Field Project",)
    assert connection.execute(
        "SELECT name_text FROM entity_names WHERE tenant_id=%s AND user_id=%s AND entity_id=%s",
        (authority.tenant_id, authority.user_id, entity_id),
    ).fetchall() == [("Willow Field Project",)]
    assert connection.execute(
        "SELECT observed_mention FROM unit_entities WHERE tenant_id=%s AND "
        "user_id=%s AND entity_id=%s",
        (authority.tenant_id, authority.user_id, entity_id),
    ).fetchall() == [("Willow Field Project",)]


def test_cleanup_preserves_trace_with_a_different_active_alias_witness(recall_database):
    connection, authority = recall_database
    debug_authority = alias_trace(connection, authority, independent=True)
    result = TraceRepository().get(connection, debug_authority, "alias-trace")["result"]
    witness = result["items"][0]["paths"][0]["alias_witnesses"][0]
    row = connection.execute(
        "SELECT support_id,quote FROM entity_name_support "
        "WHERE tenant_id=%s AND user_id=%s AND memory_id='other-definition' "
        "AND support_kind='alias'",
        (authority.tenant_id, authority.user_id),
    ).fetchone()
    witness.update(
        support_id=row[0],
        quote=row[1],
        citation={
            "source_id": "other-definition",
            "source_version": 1,
            "span": {
                "source_id": "other-definition",
                "passage_id": "other-definition",
                "quote": row[1],
            },
        },
    )
    result["context"] = render_context(result["items"])
    result["context_tokens"] = estimated_tokens(estimate_units(result["context"]))
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    assert RecallRepository(
        connection, authority, config, deadline=time.monotonic() + 30
    ).record_trace(
        "independent-trace",
        0,
        {"arms": {"dense": ["seed"]}, "result_ids": ["target"]},
        result,
    )
    remove_and_cleanup(connection, authority, "definition")
    independent = TraceRepository().get(connection, debug_authority, "independent-trace")
    assert independent["status"] == "recorded"
    assert independent["result"]["context"] == result["context"]
    assert (
        connection.execute(
            "SELECT content FROM traces WHERE tenant_id=%s AND user_id=%s "
            "AND trace_id='independent-trace'",
            (authority.tenant_id, authority.user_id),
        ).fetchone()[0]
        is not None
    )


@pytest.mark.parametrize("boundary", ["debug", "replay", "cleanup"])
def test_unselected_causal_path_node_revocation_redacts_recorded_trace(recall_database, boundary):
    from test_fast_graph_sql import history
    from test_recall_repository import FixedModels, TransactionPool

    from oam.contracts.search import SearchRequest
    from oam.retrieval.service import RecallService

    connection, authority = recall_database
    config = history(connection, authority)
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={
                    "relation_candidates": 1,
                    "fast": config.search.fast.model_copy(update={"channel_candidates": 1}),
                }
            )
        }
    )
    result = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="generation",
    ).search(authority, SearchRequest(query="anchorword", recall_level="fast", fast_graph=True))
    job = next(item for item in result["items"] if item["unit_id"] == "job")
    assert job["paths"][0]["nodes"] == ["effect", "rent", "job"]
    decisions = connection.execute(
        "SELECT decisions FROM traces WHERE tenant_id=%s AND user_id=%s AND trace_id=%s",
        (authority.tenant_id, authority.user_id, result["trace_id"]),
    ).fetchone()[0]
    assert "rent" not in decisions["result_ids"]
    assert all("rent" not in identifiers for identifiers in decisions["arms"].values())
    debug_authority = replace(authority, capabilities=frozenset({"debug:read"}))
    repository = TraceRepository()
    assert repository.get(connection, debug_authority, result["trace_id"])["status"] == "recorded"
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND user_id=%s "
        "AND evidence_id='rent'",
        (authority.tenant_id, authority.user_id),
    )

    if boundary == "cleanup":
        assert affected_trace_ids([(result["trace_id"], decisions, result)], ["rent"]) == [
            result["trace_id"]
        ]
    elif boundary == "replay":
        assert (
            repository.replay(connection, debug_authority, result["trace_id"], "inspect_recorded")[
                "status"
            ]
            == "policy_redacted"
        )
    else:
        assert repository.get(connection, debug_authority, result["trace_id"])["status"] == (
            "policy_redacted"
        )
