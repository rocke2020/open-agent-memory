"""Exact-ID removal through HTTP and workers in isolated PostgreSQL scopes."""

import hashlib
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb
from test_core_http import ROOT, use_created_user_key
from test_core_http import core_database as core_database

from oam.config import load_config
from oam.indexing.identity import index_generation
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration

USER = "lin"
SOURCE = "synthetic-chat"
REMOVED_MEMORY = "memory-selected"
SURVIVING_MEMORY = "memory-surviving"
OTHER_MEMORIES = ("memory-independent-a", "memory-independent-b")
INSIGHT = "insight-selected"
DEPENDENT_INSIGHT = "insight-dependent"
INDEPENDENT_INSIGHT = "insight-independent"
PROFILE_TEXT = "Synthetic Profile based on the selected memory and Insight."
VECTOR = json.dumps([1.0] + [0.0] * 1023)


@asynccontextmanager
async def _client(headers):
    app = create_app(ROOT / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            headers=headers,
        ) as client,
    ):
        yield client


def _content(dsn, tenant, user_id=USER):
    connection = psycopg.connect(make_conninfo(dsn, options="-c role=oam_content"))
    connection.execute(
        "SELECT set_config('oam.tenant_id',%s,true),set_config('oam.user_id',%s,true)",
        (tenant, user_id),
    )
    return connection


def _seed(dsn, tenant, generation, user_id=USER, prefix=""):
    key = (tenant, user_id)
    source_id, evidence_id, passage_id = (
        prefix + value for value in (SOURCE, "evidence", "passage")
    )
    original = json.dumps(
        {"messages": [{"role": "user", "content": "Synthetic learned preferences."}]}
    ).encode()
    digest = hashlib.sha256(original).digest()
    with _content(dsn, tenant, user_id) as connection:
        role = connection.execute(
            "SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone()
        assert role == ("oam_content", False, False)
        connection.execute(
            "UPDATE users SET accepted_seq=10,published_seq=10,index_generation_id=%s "
            "WHERE tenant_id=%s AND user_id=%s",
            (generation, *key),
        )
        connection.execute(
            "INSERT INTO sources(tenant_id,user_id,source_id,input_role,active_version,status) "
            "VALUES (%s,%s,%s,'interaction',1,'active')",
            (*key, source_id),
        )
        connection.execute(
            "INSERT INTO source_versions(tenant_id,user_id,source_id,source_version,media_type,"
            "original_bytes,content_sha256,byte_length,status) "
            "VALUES (%s,%s,%s,1,'application/json',%s,%s,%s,'active')",
            (*key, source_id, original, digest, len(original)),
        )
        connection.execute(
            "INSERT INTO evidence(tenant_id,user_id,evidence_id,source_id,source_version,"
            "accepted_seq,content_text,event_time,learning_eligible,status) "
            "VALUES (%s,%s,%s,%s,1,1,'Synthetic learned preferences.',"
            "'2026-10-01T00:00:00Z',true,'active')",
            (*key, evidence_id, source_id),
        )
        connection.execute(
            "INSERT INTO passages(tenant_id,user_id,passage_id,evidence_id,parse_revision,"
            "locator,normalized_text,generation_id,visible_from) "
            "VALUES (%s,%s,%s,%s,1,'{}','Synthetic learned preferences.',%s,1)",
            (*key, passage_id, evidence_id, generation),
        )
        for memory in (REMOVED_MEMORY, SURVIVING_MEMORY, *OTHER_MEMORIES):
            memory_id = prefix + memory
            text = f"Synthetic claim {memory_id}."
            connection.execute(
                "INSERT INTO memories(tenant_id,user_id,memory_id,canonical_claim,"
                "assertion_kind,generation_id,visible_from) VALUES (%s,%s,%s,%s,'stated',%s,1)",
                (*key, memory_id, text, generation),
            )
            connection.execute(
                "INSERT INTO support(tenant_id,user_id,memory_id,evidence_id,passage_id,"
                "support_kind,span) VALUES (%s,%s,%s,%s,%s,'source','{}')",
                (*key, memory_id, evidence_id, passage_id),
            )
            connection.execute(
                "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,source_id,"
                "source_version,evidence_id,memory_id,generation_id,content_text,locator,"
                "embedding,lexical_text) VALUES (%s,%s,%s,'fact','memory',%s,1,%s,%s,%s,%s,"
                "'{}',%s::vector,%s)",
                (
                    *key,
                    memory_id,
                    source_id,
                    evidence_id,
                    memory_id,
                    generation,
                    text,
                    VECTOR,
                    text,
                ),
            )
        for insight, revisions, supports in (
            (INSIGHT, (1, 2), (REMOVED_MEMORY, SURVIVING_MEMORY)),
            (DEPENDENT_INSIGHT, (1,), (REMOVED_MEMORY, OTHER_MEMORIES[0])),
            (INDEPENDENT_INSIGHT, (1,), OTHER_MEMORIES),
        ):
            insight_id = prefix + insight
            for revision in revisions:
                text = f"Synthetic synthesis {insight_id} revision {revision}."
                connection.execute(
                    "INSERT INTO insights(tenant_id,user_id,insight_id,revision,synthesis_text,"
                    "input_digest,recipe_id,generation_id,visible_from,visible_to) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'synthetic',%s,%s,%s)",
                    (
                        *key,
                        insight_id,
                        revision,
                        text,
                        digest,
                        generation,
                        revision,
                        2 if revision < max(revisions) else None,
                    ),
                )
                for memory in supports:
                    connection.execute(
                        "INSERT INTO insight_support(tenant_id,user_id,insight_id,"
                        "insight_revision,memory_id,support_revision) VALUES (%s,%s,%s,%s,%s,1)",
                        (*key, insight_id, revision, prefix + memory),
                    )
                connection.execute(
                    "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,"
                    "insight_id,insight_revision,generation_id,content_text,locator,embedding,"
                    "lexical_text) VALUES (%s,%s,%s,'insight','memory',%s,%s,%s,%s,"
                    "'{}',%s::vector,%s)",
                    (
                        *key,
                        f"{insight_id}:r{revision}",
                        insight_id,
                        revision,
                        generation,
                        text,
                        VECTOR,
                        text,
                    ),
                )
        connection.execute(
            "INSERT INTO profiles(tenant_id,user_id,profile_id,profile_revision,sections,"
            "content,support_ids,input_identity,policy_epoch,generation_id,visible_from) "
            "VALUES (%s,%s,'user',1,'{}',%s,%s,%s,0,%s,3)",
            (
                *key,
                PROFILE_TEXT,
                Jsonb([prefix + REMOVED_MEMORY, f"{prefix}{INSIGHT}:r2"]),
                digest,
                generation,
            ),
        )


@dataclass
class RemovalDatabase:
    dsn: str
    tenant: str
    headers: dict[str, str]
    other_headers: dict[str, str]
    root_headers: dict[str, str]

    def cleanup(self):
        for role in ("oam_api", "oam_content", "oam_queue"):
            with psycopg.connect(make_conninfo(self.dsn, options=f"-c role={role}")) as connection:
                assert connection.execute(
                    "SELECT current_user,rolsuper,rolbypassrls FROM pg_roles "
                    "WHERE rolname=current_user"
                ).fetchone() == (role, False, False)
        assert process_one(
            load_config(ROOT / "configs/oam.yaml").config,
            make_conninfo(self.dsn, options="-c role=oam_queue"),
            make_conninfo(self.dsn, options="-c role=oam_content"),
            models=object(),
            worker_id="synthetic-removal-cleanup",
        )


@pytest.fixture
async def removal_database(core_database):
    dsn, tenant, root_headers = core_database
    async with _client(root_headers) as client:
        await use_created_user_key(client, USER)
        headers = {"Authorization": client.headers["Authorization"]}
        client.headers.update(root_headers)
        await use_created_user_key(client, "other")
        other_headers = {"Authorization": client.headers["Authorization"]}
    generation = index_generation(load_config(ROOT / "configs/oam.yaml").config, os.environ)
    _seed(dsn, tenant, generation)
    _seed(dsn, tenant, generation, "other", "other-")
    return RemovalDatabase(dsn, tenant, headers, other_headers, root_headers)


async def _items(client):
    response = await client.get(f"/v1/users/{USER}/memories")
    assert response.status_code == 200, response.text
    return {item["unit_id"] for item in response.json()["items"]}


async def test_l1_removal_immediately_excludes_selected_and_dependents(removal_database):
    database = removal_database
    async with _client(database.headers) as client:
        before = await _items(client)
        assert before == {
            REMOVED_MEMORY,
            SURVIVING_MEMORY,
            *OTHER_MEMORIES,
            f"{INSIGHT}:r2",
            f"{DEPENDENT_INSIGHT}:r1",
            f"{INDEPENDENT_INSIGHT}:r1",
        }
        profile = await client.get(f"/v1/users/{USER}/profile")
        assert profile.status_code == 200, profile.text
        assert profile.json()["content"] == PROFILE_TEXT
        response = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        assert response.status_code == 202, response.text
        assert response.json()["memory_id"] == REMOVED_MEMORY
        assert response.json()["policy_epoch"] == 1
        assert await _items(client) == {
            SURVIVING_MEMORY,
            *OTHER_MEMORIES,
            f"{INDEPENDENT_INSIGHT}:r1",
        }
        profile = await client.get(f"/v1/users/{USER}/profile")
        assert profile.status_code == 200, profile.text
        assert profile.json()["content"] is None
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(
                "SELECT lifecycle_state FROM memories WHERE memory_id=%s", (REMOVED_MEMORY,)
            ).fetchone() == ("excluded",)
            assert connection.execute(
                "SELECT count(*) FROM insights WHERE insight_id=ANY(%s) AND visible_to IS NULL",
                ([INSIGHT, DEPENDENT_INSIGHT],),
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM profiles WHERE visible_to IS NULL"
            ).fetchone() == (0,)


async def test_direct_l2_cleanup_removes_every_revision_preserving_lower_layers(removal_database):
    database = removal_database
    async with _client(database.headers) as client:
        assert f"{INSIGHT}:r2" in await _items(client)
        response = await client.delete(f"/v1/users/{USER}/insights/{INSIGHT}")
        assert response.status_code == 202, response.text
        assert response.json()["insight_id"] == INSIGHT
        assert await _items(client) == {
            REMOVED_MEMORY,
            SURVIVING_MEMORY,
            *OTHER_MEMORIES,
            f"{DEPENDENT_INSIGHT}:r1",
            f"{INDEPENDENT_INSIGHT}:r1",
        }
        database.cleanup()
        operation = await client.get(
            f"/v1/users/{USER}/operations/{response.json()['operation_id']}"
        )
        assert operation.status_code == 200, operation.text
        assert operation.json()["status"] == "succeeded"
        with _content(database.dsn, database.tenant) as connection:
            for table in ("insights", "insight_support", "search_units"):
                assert connection.execute(
                    f"SELECT count(*) FROM {table} WHERE insight_id=%s", (INSIGHT,)
                ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM memories WHERE lifecycle_state='active'"
            ).fetchone() == (4,)
            assert connection.execute("SELECT count(*) FROM support").fetchone() == (4,)
            assert connection.execute("SELECT count(*) FROM passages").fetchone() == (1,)
            assert connection.execute("SELECT count(*) FROM evidence").fetchone() == (1,)
            assert connection.execute(
                "SELECT count(*) FROM sources WHERE status='active' AND active_version=1"
            ).fetchone() == (1,)
            original = connection.execute(
                "SELECT original_bytes,byte_length FROM source_versions "
                "WHERE tenant_id=%s AND user_id=%s AND source_id=%s AND source_version=1",
                (database.tenant, USER, SOURCE),
            ).fetchone()
            expected_original = json.dumps(
                {"messages": [{"role": "user", "content": "Synthetic learned preferences."}]}
            ).encode()
            assert original == (expected_original, len(expected_original))
            assert connection.execute(
                "SELECT count(*) FROM insights WHERE visible_to IS NULL"
            ).fetchone() == (2,)


@pytest.mark.parametrize("kind,target", [("memories", REMOVED_MEMORY), ("insights", INSIGHT)])
async def test_removal_reuses_original_receipt_through_cleanup_and_restart(
    removal_database, kind, target
):
    database = removal_database
    route = f"/v1/users/{USER}/{kind}/{target}"
    async with _client(database.headers) as client:
        first = await client.delete(route)
        assert first.status_code == 202, first.text
        receipt = first.json()
        before = await client.delete(route)
        assert before.status_code == 202, before.text
        assert before.json()["operation_id"] == receipt["operation_id"]
        assert before.json()["policy_epoch"] == receipt["policy_epoch"]
        database.cleanup()
        after = await client.delete(route)
        assert after.status_code == 202, after.text
        assert after.json()["operation_id"] == receipt["operation_id"]
        assert after.json()["cleanup_status"] == "complete"
    async with _client(database.headers) as restarted_client:
        restarted = await restarted_client.delete(route)
        assert restarted.status_code == 202, restarted.text
        assert restarted.json()["operation_id"] == receipt["operation_id"]
        assert restarted.json()["policy_epoch"] == receipt["policy_epoch"]
        assert restarted.json()["cleanup_status"] == "complete"
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(
                "SELECT count(*) FROM operations WHERE removal_target_id=%s", (target,)
            ).fetchone() == (1,)
            assert connection.execute("SELECT policy_epoch FROM users").fetchone() == (1,)


@pytest.mark.parametrize(
    "kind,target,credential",
    [
        ("memories", INSIGHT, "owner"),
        ("insights", REMOVED_MEMORY, "owner"),
        ("memories", "unknown", "owner"),
        ("insights", "unknown", "owner"),
        ("memories", "other-" + REMOVED_MEMORY, "owner"),
        ("memories", REMOVED_MEMORY, "other"),
        ("insights", INSIGHT, "other"),
        ("insights", INSIGHT, "root"),
        ("memories", REMOVED_MEMORY, "no_permission"),
        ("insights", INSIGHT, "no_permission"),
    ],
)
async def test_invalid_or_unauthorized_removal_is_404_without_mutation(
    removal_database, kind, target, credential
):
    database = removal_database
    if credential == "no_permission":
        with psycopg.connect(database.dsn) as connection:
            connection.execute(
                "UPDATE grants SET capabilities=ARRAY['memory:read','memory:append'] "
                "WHERE tenant_id=%s AND user_id=%s",
                (database.tenant, USER),
            )
    headers = {
        "owner": database.headers,
        "other": database.other_headers,
        "root": database.root_headers,
        "no_permission": database.headers,
    }[credential]
    async with _client(headers) as client:
        response = await client.delete(f"/v1/users/{USER}/{kind}/{target}")
    with _content(database.dsn, database.tenant) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone() == (0,)
        assert connection.execute("SELECT policy_epoch FROM users").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM memories WHERE lifecycle_state='active'"
        ).fetchone() == (4,)
        assert connection.execute(
            "SELECT count(*) FROM insights WHERE visible_to IS NULL"
        ).fetchone() == (3,)
    assert response.status_code == 404, response.text


class RepairModels:
    def __init__(self, *, malformed=False):
        self.calls = []
        self.malformed = malformed
        self.insight_inputs = []

    def generate(self, role, **kwargs):
        from oam.models.runtime import ModelResult, Usage

        self.calls.append(role)
        assert role == "insights"
        facts = json.loads(kwargs["user"])["facts"]
        self.insight_inputs.append(facts)
        return ModelResult(
            content="invalid"
            if self.malformed
            else json.dumps(
                {
                    "outcome": "insight",
                    "title": "Supported subject",
                    "text": "Regenerated eligible synthesis.",
                    "support_ids": [fact["id"] for fact in facts],
                    "subtype": "general_insights",
                }
            ),
            usage=Usage(input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"),
        )

    def embed(self, texts, **kwargs):
        from oam.models.runtime import EmbeddingResult, Usage

        self.calls.append("embedding")
        return EmbeddingResult(vectors=(tuple([1.0] + [0.0] * 1023),), usage=Usage())


def _run_repair(database, models):
    with _content(database.dsn, database.tenant) as connection:
        principal_id = connection.execute("SELECT principal_id FROM grants LIMIT 1").fetchone()[0]
        generation = connection.execute("SELECT index_generation_id FROM users").fetchone()[0]
        connection.execute(
            "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,source_id,"
            "source_version,kind,schema_version,status) "
            "VALUES (%s,%s,'fixture-index',%s,%s,1,'index_interaction',1,'succeeded') "
            "ON CONFLICT DO NOTHING",
            (database.tenant, USER, principal_id, SOURCE),
        )
        connection.execute(
            "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
            "input_digest,generation_id,status,attempts_remaining) "
            "VALUES (%s,%s,'fixture-index','fixture-index','index_source',%s,%s,'succeeded',0) "
            "ON CONFLICT DO NOTHING",
            (database.tenant, USER, bytes(32), generation),
        )
    return process_one(
        load_config(ROOT / "configs/oam.yaml").config,
        make_conninfo(database.dsn, options="-c role=oam_queue"),
        make_conninfo(database.dsn, options="-c role=oam_content"),
        models=models,
        worker_id="synthetic-insight-repair",
    )


async def test_l1_cleanup_then_repairs_same_id_and_deletes_insufficient_support(removal_database):
    database = removal_database
    annotation = {
        "role": "event",
        "start": "2026-07-03T00:00:00Z",
        "end": "2026-07-04T00:00:00Z",
        "quote": "on July 3",
    }
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,%s,2,%s,1)",
            (database.tenant, USER, INSIGHT, OTHER_MEMORIES[1]),
        )
        connection.execute(
            "UPDATE search_units SET locator=%s WHERE memory_id=%s",
            (Jsonb({"role": "assistant"}), SURVIVING_MEMORY),
        )
        connection.execute(
            "INSERT INTO unit_times(tenant_id,user_id,annotation_id,unit_id,role,time_span,detail) "
            "VALUES (%s,%s,'surviving-event',%s,'event',"
            "tstzrange('2026-07-03T00:00:00Z','2026-07-04T00:00:00Z','[)'),%s)",
            (database.tenant, USER, SURVIVING_MEMORY, Jsonb(annotation)),
        )
    async with _client(database.headers) as client:
        response = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        assert response.status_code == 202, response.text
        database.cleanup()
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(
                "SELECT count(*) FROM insights WHERE insight_id=ANY(%s)",
                ([INSIGHT, DEPENDENT_INSIGHT],),
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT canonical_claim,lifecycle_state FROM memories WHERE memory_id=%s",
                (REMOVED_MEMORY,),
            ).fetchone() == ("", "excluded")
        models = RepairModels()
        assert _run_repair(database, models)
        assert models.calls == ["insights", "embedding"]
        assert {fact["id"]: fact.get("message_role") for fact in models.insight_inputs[0]} == {
            SURVIVING_MEMORY: "assistant",
            OTHER_MEMORIES[1]: None,
        }
        supplied = {fact["id"]: fact for fact in models.insight_inputs[0]}
        assert supplied[SURVIVING_MEMORY]["temporal_annotations"] == [annotation]
        assert "temporal_annotations" not in supplied[OTHER_MEMORIES[1]]
        assert all("source_refs" not in fact for fact in supplied.values())
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(
                "SELECT revision,synthesis_text FROM insights WHERE insight_id=%s",
                (INSIGHT,),
            ).fetchall() == [(3, "Regenerated eligible synthesis.")]
            assert connection.execute(
                "SELECT memory_id FROM insight_support WHERE insight_id=%s ORDER BY memory_id",
                (INSIGHT,),
            ).fetchall() == [(OTHER_MEMORIES[1],), (SURVIVING_MEMORY,)]
            assert connection.execute(
                "SELECT count(*) FROM insights WHERE insight_id=%s",
                (DEPENDENT_INSIGHT,),
            ).fetchone() == (0,)
            state = connection.execute(
                "SELECT stage_state FROM operations WHERE kind='consolidate'",
            ).fetchone()[0]
            assert state["repair_outcomes"] == {
                INSIGHT: "updated",
                DEPENDENT_INSIGHT: "deleted",
            }
            assert connection.execute(
                "SELECT count(*) FROM profiles WHERE visible_to IS NULL"
            ).fetchone() == (0,)
        profile = await client.get(f"/v1/users/{USER}/profile")
        assert profile.json()["content"] is None
        items = (await client.get(f"/v1/users/{USER}/memories")).json()["items"]
        repaired = [item for item in items if item["insight_id"] == INSIGHT]
        assert len(repaired) == 1 and repaired[0]["insight_revision"] == 3


async def test_direct_remove_captured_insight_after_history_cleanup_blocks_repair(removal_database):
    database = removal_database
    async with _client(database.headers) as client:
        response = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        assert response.status_code == 202, response.text
        database.cleanup()
        direct = await client.delete(f"/v1/users/{USER}/insights/{INSIGHT}")
        assert direct.status_code == 202, direct.text
        # The earlier repair may run first; neither valid outcome needs a model.
        assert _run_repair(database, object())
        assert _run_repair(database, object())
        with _content(database.dsn, database.tenant) as connection:
            assert connection.execute(
                "SELECT count(*) FROM insights WHERE insight_id=%s", (INSIGHT,)
            ).fetchone() == (0,)
            state = connection.execute(
                "SELECT stage_state FROM operations WHERE kind='consolidate'"
            ).fetchone()[0]
            assert state["repair_outcomes"][INSIGHT] == "directly_removed"


async def test_malformed_repair_fails_without_valid_delete_or_resurrection(removal_database):
    database = removal_database
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,%s,2,%s,1)",
            (database.tenant, USER, INSIGHT, OTHER_MEMORIES[1]),
        )
    async with _client(database.headers) as client:
        response = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        assert response.status_code == 202, response.text
        database.cleanup()
        models = RepairModels(malformed=True)
        with pytest.raises(ValueError, match="not valid JSON"):
            _run_repair(database, models)
        with _content(database.dsn, database.tenant) as connection:
            state = connection.execute(
                "SELECT stage_state FROM operations WHERE kind='consolidate'"
            ).fetchone()[0]
            assert INSIGHT not in state.get("repair_outcomes", {})
            assert connection.execute(
                "SELECT count(*) FROM insights WHERE insight_id=%s", (INSIGHT,)
            ).fetchone() == (0,)


async def test_removal_resets_only_affected_unit_coverage_and_latest_support(removal_database):
    database = removal_database
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "UPDATE search_units SET unit_id='unit-survivor' WHERE memory_id=%s",
            (SURVIVING_MEMORY,),
        )
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,%s,1,%s,1)",
            (database.tenant, USER, INSIGHT, OTHER_MEMORIES[1]),
        )
        for operation_id, status, coverage in (
            (
                "prior-coverage",
                "succeeded",
                ["unit-survivor", OTHER_MEMORIES[0], OTHER_MEMORIES[1]],
            ),
            ("active-affected", "running", ["unit-survivor", OTHER_MEMORIES[1]]),
            ("active-unrelated", "running", [OTHER_MEMORIES[1]]),
        ):
            principal_id = connection.execute("SELECT principal_id FROM grants LIMIT 1").fetchone()[
                0
            ]
            connection.execute(
                "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,source_id,"
                "source_version,kind,schema_version,status,stage_state) VALUES "
                "(%s,%s,%s,%s,%s,1,'consolidate',1,%s,%s)",
                (
                    database.tenant,
                    USER,
                    operation_id,
                    principal_id,
                    SOURCE,
                    status,
                    Jsonb({"l2_examined_ids": coverage, "insights_ready": True}),
                ),
            )
    async with _client(database.headers) as client:
        response = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        assert response.status_code == 202, response.text
    with _content(database.dsn, database.tenant) as connection:
        states = dict(
            connection.execute(
                "SELECT operation_id,stage_state FROM operations WHERE kind='consolidate'"
            ).fetchall()
        )
        assert states["active-affected"]["l2_examined_ids"] == [OTHER_MEMORIES[1]]
        assert states["active-unrelated"] == {
            "l2_examined_ids": [OTHER_MEMORIES[1]],
            "insights_ready": True,
        }
        assert states["prior-coverage"]["l2_examined_ids"] == ["unit-survivor", *OTHER_MEMORIES]
        repair = next(value for value in states.values() if "insight_repairs" in value)
        selected = next(
            value for value in repair["insight_repairs"] if value["insight_id"] == INSIGHT
        )
        assert selected["support_ids"] == [SURVIVING_MEMORY]
        assert "unit-survivor" not in repair["l2_examined_ids"]


async def test_second_l1_removal_discovers_pending_support_after_history_purge(removal_database):
    database = removal_database
    with _content(database.dsn, database.tenant) as connection:
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,%s,2,%s,1)",
            (database.tenant, USER, INSIGHT, OTHER_MEMORIES[1]),
        )
    async with _client(database.headers) as client:
        first = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        assert first.status_code == 202
        database.cleanup()
        second = await client.delete(f"/v1/users/{USER}/memories/{SURVIVING_MEMORY}")
        assert second.status_code == 202
        with _content(database.dsn, database.tenant) as connection:
            state = connection.execute(
                "SELECT stage_state FROM operations WHERE operation_id=%s",
                (second.json()["operation_id"],),
            ).fetchone()[0]
            assert INSIGHT in state["affected_insight_ids"]
            assert state["repair_inputs"]["next_revision"][INSIGHT] == 2
