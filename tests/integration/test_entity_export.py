"""Entity export and exact-memory cleanup in rollback-only PostgreSQL scopes."""

import hashlib
import json
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
from oam.exports.repository import ExportRepository
from oam.exports.validation import validate_directory
from oam.governance.cleanup import cleanup_removal_claim
from oam.governance.removal import RemovalRepository
from oam.indexing.entities import EntityRepository
from oam.workers.claims import ClaimedWork

pytestmark = pytest.mark.integration


def seed_alias(connection, authority):
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute(
        "INSERT INTO principals(tenant_id,principal_id,role,state) "
        "VALUES (%s,'reader','user','active')",
        (authority.tenant_id,),
    )
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    for identifier, text in (
        ("definition", "Nova Research, also called NR, opened a laboratory."),
        ("independent", "Nova Research, also called NR, hired Li."),
    ):
        add_fact(connection, authority, identifier, text)
        connection.execute(
            "UPDATE source_versions SET content_sha256=%s "
            "WHERE tenant_id=%s AND user_id=%s AND source_id=%s",
            (
                hashlib.sha256(text.encode()).digest(),
                authority.tenant_id,
                authority.user_id,
                identifier,
            ),
        )
        repository.publish_fact(
            identifier,
            ("Nova Research", "NR"),
            text,
            aliases=(
                {
                    "canonical_name": "Nova Research",
                    "alias": "NR",
                    "quote": "Nova Research, also called NR",
                },
            ),
            references=({"name": "Nova Research", "reference_id": "definition", "quote": text},)
            if identifier == "independent"
            else (),
        )
    return config


def snapshot(connection, authority, config, export_id, destination=None):
    exporter = ExportRepository(config)
    export_authority = replace(authority, capabilities=frozenset({"export:read"}))
    exporter.create(connection, export_authority, export_id=export_id, retention_hours=1)
    manifest = exporter.get_manifest(connection, export_authority, export_id)
    records = [
        json.loads(line)
        for p in manifest["parts"]
        for line in exporter.get_part(
            connection, export_authority, export_id, p["index"]
        ).splitlines()
    ]
    if destination is not None:
        (destination / "manifest.json").write_text(json.dumps(manifest))
        for part in manifest["parts"]:
            path = destination / part["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                exporter.get_part(connection, export_authority, export_id, part["index"])
            )
        for original in manifest["originals"]:
            path = destination / original["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                exporter.get_original(connection, export_authority, export_id, original["index"])[0]
            )
    return records


def test_portable_export_preserves_current_entity_alias_witnesses(recall_database, tmp_path):
    connection, authority = recall_database
    config = seed_alias(connection, authority)
    connection.execute(
        "INSERT INTO entity_units VALUES (%s,%s,'orphan','generation','Unsupported name')",
        (authority.tenant_id, authority.user_id),
    )
    records = snapshot(connection, authority, config, "initial", tmp_path)
    assert validate_directory(tmp_path)["entity_name_support"] == 4
    entities = [r for r in records if r["kind"] == "entity"]
    names = [r for r in records if r["kind"] == "entity_name"]
    supports = [r for r in records if r["kind"] == "entity_name_support"]
    memberships = [r for r in records if r["kind"] == "entity_membership"]
    assert len(entities) == 1
    assert {r["name_text"] for r in names} == {"Nova Research", "NR"}
    assert len(supports) == 4
    assert {r["memory_id"] for r in memberships} == {"definition", "independent"}
    assert all(
        r["generation_id"] == "generation" for r in entities + names + supports + memberships
    )
    assert {r["quote"] for r in supports if r["support_kind"] == "alias"} == {
        "Nova Research, also called NR"
    }


def test_export_excludes_revoked_support_and_keeps_old_generation_registry(recall_database):
    connection, authority = recall_database
    config = seed_alias(connection, authority)
    connection.execute(
        "UPDATE sources SET status='deleted' WHERE tenant_id=%s AND user_id=%s "
        "AND source_id='definition'",
        (authority.tenant_id, authority.user_id),
    )
    records = snapshot(connection, authority, config, "revoked")
    assert {r["memory_id"] for r in records if r["kind"] == "entity_name_support"} == {
        "independent"
    }
    assert {r["memory_id"] for r in records if r["kind"] == "entity_membership"} == {"independent"}
    connection.execute(
        "UPDATE users SET index_generation_id='new-generation' WHERE tenant_id=%s AND user_id=%s",
        (authority.tenant_id, authority.user_id),
    )
    records = snapshot(connection, authority, config, "new-generation")
    assert {r["memory_id"] for r in records if r["kind"] == "entity_name_support"} == {
        "independent"
    }


def test_exact_memory_cleanup_purges_only_its_entity_support_quotes(recall_database):
    connection, authority = recall_database
    seed_alias(connection, authority)
    removal_authority = replace(authority, capabilities=frozenset({"source:delete"}))
    receipt = RemovalRepository().remove(
        connection,
        removal_authority,
        target_kind="memory",
        target_id="definition",
        attempts_remaining=1,
    )
    work_id = connection.execute(
        "UPDATE work_units SET owner_id='fixture-worker',fence=1,status='running',"
        "lease_expires_at=clock_timestamp()+interval '1 hour' "
        "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s RETURNING work_unit_id",
        (authority.tenant_id, authority.user_id, receipt.operation_id),
    ).fetchone()[0]
    claim = ClaimedWork(
        authority.tenant_id,
        authority.user_id,
        work_id,
        receipt.operation_id,
        "cleanup_memory",
        "fixture-attempt",
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
    rows = connection.execute(
        "SELECT memory_id,quote FROM entity_name_support WHERE tenant_id=%s AND user_id=%s",
        (authority.tenant_id, authority.user_id),
    ).fetchall()
    assert {row[0] for row in rows} == {"independent"}
    assert len(rows) == 2
    assert connection.execute(
        "SELECT canonical_claim,lifecycle_state FROM memories "
        "WHERE tenant_id=%s AND user_id=%s AND memory_id='definition'",
        (authority.tenant_id, authority.user_id),
    ).fetchone() == ("", "excluded")
    assert connection.execute(
        "SELECT status FROM sources WHERE tenant_id=%s AND user_id=%s AND source_id='definition'",
        (authority.tenant_id, authority.user_id),
    ).fetchone() == ("active",)
