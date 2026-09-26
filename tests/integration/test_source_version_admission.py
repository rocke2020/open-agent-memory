import hashlib
import os
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest

from oam.auth.credentials import AuthorizedUser
from oam.db.context import bind_user
from oam.db.inspection import SourceInspector
from oam.db.sources import AcceptSource, SourceRepository, SourceVersionConflict

pytestmark = pytest.mark.integration


@pytest.fixture
def source_database():
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    with (
        psycopg.connect(dsn, autocommit=True) as connection,
        connection.transaction(force_rollback=True),
    ):
        tenant = f"version-{uuid4().hex}"
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (tenant,))
        connection.execute(
            "INSERT INTO principals(tenant_id,principal_id,role,state) "
            "VALUES (%s,'writer','writer','active')",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime) "
            "VALUES (%s,'scope','synthetic','test','persistent')",
            (tenant,),
        )
        connection.execute("SET LOCAL ROLE oam_api")
        authority = AuthorizedUser(
            "key",
            tenant,
            "writer",
            "scope",
            frozenset({"memory:append", "memory:read", "resource:write", "source:delete"}),
        )
        bind_user(connection.cursor(), authority)
        yield connection, authority


def command(version=1, **changes):
    content = f"source version {version}".encode()
    original = AcceptSource(
        source_id="source",
        source_version=version,
        input_role="interaction",
        resource_type=None,
        media_type="text/plain",
        original_bytes=content,
        evidence_id=f"evidence{version}",
        content_text=None,
        learning_eligible=True,
        operation_id=f"operation{version}",
        operation_kind="index_interaction",
        schema_version=1,
        idempotency_key=f"request{version}",
        request_hash=hashlib.sha256(content).digest(),
        work_unit_id=f"work{version}",
        stage="index_source",
        generation_id="generation",
        attempts_remaining=1,
    )
    return replace(original, **changes)


def test_new_source_cannot_skip_version_one(source_database):
    connection, authority = source_database
    with pytest.raises(SourceVersionConflict):
        SourceRepository().accept(connection, authority, command(2))
    assert (
        connection.execute(
            "SELECT count(*) FROM sources WHERE tenant_id=%s", (authority.tenant_id,)
        ).fetchone()[0]
        == 0
    )


def test_source_replacement_requires_cas_and_preserves_original_bytes(source_database):
    connection, authority = source_database
    repository = SourceRepository()
    first = command()
    repository.accept(connection, authority, first)
    second = command(2, expected_active_version=1)
    receipt = repository.accept(connection, authority, second)
    assert receipt.accepted_seq == 2
    rows = connection.execute(
        "SELECT source_version,status,original_bytes FROM source_versions "
        "WHERE tenant_id=%s ORDER BY source_version",
        (authority.tenant_id,),
    ).fetchall()
    assert [(version, status, bytes(content)) for version, status, content in rows] == [
        (1, "superseded", b"source version 1"),
        (2, "active", b"source version 2"),
    ]
    with pytest.raises(SourceVersionConflict):
        repository.accept(
            connection,
            authority,
            command(
                2,
                expected_active_version=1,
                idempotency_key="stale",
                operation_id="stale",
                evidence_id="stale",
                work_unit_id="stale",
            ),
        )
    assert (
        connection.execute(
            "SELECT accepted_seq FROM users WHERE tenant_id=%s", (authority.tenant_id,)
        ).fetchone()[0]
        == 2
    )
    assert repository.accept(connection, authority, first).reused is True


def test_source_replacement_cannot_omit_expected_version(source_database):
    connection, authority = source_database
    repository = SourceRepository()
    repository.accept(connection, authority, command())
    with pytest.raises(SourceVersionConflict):
        repository.accept(connection, authority, command(2))
    assert (
        connection.execute(
            "SELECT active_version FROM sources WHERE tenant_id=%s", (authority.tenant_id,)
        ).fetchone()[0]
        == 1
    )


def test_server_assigns_document_revisions_and_idempotent_append(source_database):
    connection, authority = source_database
    repository = SourceRepository()
    first = command(source_version=None, input_role="evidence_resource", resource_type="evidence")
    repository.accept(connection, authority, first)
    append = command(
        2,
        source_version=None,
        input_role="evidence_resource",
        resource_type="evidence",
        original_bytes=b"\nSecond line.",
        update_mode="append",
        max_bytes=1000,
    )
    result = repository.accept(connection, authority, append)
    assert result.accepted_seq == 2
    assert repository.accept(connection, authority, append).reused is True
    rows = connection.execute(
        "SELECT source_version,original_bytes,status FROM source_versions "
        "WHERE tenant_id=%s ORDER BY source_version",
        (authority.tenant_id,),
    ).fetchall()
    assert [(v, bytes(b), s) for v, b, s in rows] == [
        (1, b"source version 1", "superseded"),
        (2, b"source version 1\nSecond line.", "active"),
    ]


def test_append_requires_existing_text_and_checks_final_size(source_database):
    connection, authority = source_database
    repository = SourceRepository()
    append = command(
        source_version=None,
        input_role="evidence_resource",
        resource_type="evidence",
        update_mode="append",
        max_bytes=5,
    )
    with pytest.raises(ValueError, match="existing text"):
        repository.accept(connection, authority, append)
    repository.accept(
        connection,
        authority,
        command(source_version=None, input_role="evidence_resource", resource_type="evidence"),
    )
    with pytest.raises(ValueError, match="byte limit"):
        repository.accept(connection, authority, replace(append, idempotency_key="append"))
    assert (
        connection.execute(
            "SELECT active_version FROM sources WHERE tenant_id=%s", (authority.tenant_id,)
        ).fetchone()[0]
        == 1
    )


def test_delete_excludes_every_version_and_reuses_cleanup_receipt(source_database):
    connection, authority = source_database
    repository = SourceRepository()
    repository.accept(connection, authority, command())
    repository.accept(connection, authority, command(2, expected_active_version=1))

    first = repository.delete(
        connection,
        authority,
        source_id="source",
        resource_only=False,
        operation_id="cleanup",
        work_unit_id="cleanup-work",
        attempts_remaining=2,
    )
    again = repository.delete(
        connection,
        authority,
        source_id="source",
        resource_only=False,
        operation_id="ignored",
        work_unit_id="ignored",
        attempts_remaining=2,
    )

    assert first.excluded_versions == (1, 2)
    assert first.cleanup_operation_id == again.cleanup_operation_id == "cleanup"
    assert first.policy_epoch == again.policy_epoch
    assert (
        repository.read_original(connection, authority, source_id="source", source_version=2)
        is None
    )
    assert connection.execute(
        "SELECT status,active_version FROM sources WHERE tenant_id=%s AND user_id=%s "
        "AND source_id='source'",
        (authority.tenant_id, authority.user_id),
    ).fetchone() == ("deleting", None)
    assert [
        row[0]
        for row in connection.execute(
            "SELECT status FROM source_versions WHERE tenant_id=%s AND user_id=%s "
            "AND source_id='source' ORDER BY source_version",
            (authority.tenant_id, authority.user_id),
        )
    ] == ["excluded", "excluded"]


def test_inspector_only_reads_current_resource_version(source_database):
    connection, authority = source_database
    repository = SourceRepository()
    inspector = SourceInspector()
    first = command(source_version=None, input_role="evidence_resource", resource_type="evidence")
    repository.accept(connection, authority, first)
    repository.accept(
        connection,
        authority,
        command(
            2,
            input_role="evidence_resource",
            resource_type="evidence",
            expected_active_version=1,
        ),
    )

    listed = inspector.list_resources(connection, authority, after=None)
    assert [(item["resource_id"], item["active_version"]) for item in listed["items"]] == [
        ("source", 2)
    ]
    assert inspector.get_resource(connection, authority, "source")["active_version"] == 2
    assert inspector.get_original(connection, authority, "source") == (
        b"source version 2",
        "text/plain",
    )
    repository.delete(
        connection,
        authority,
        source_id="source",
        resource_only=True,
        operation_id="cleanup",
        work_unit_id="cleanup-work",
        attempts_remaining=2,
    )
    assert inspector.list_resources(connection, authority, after=None)["items"] == []
    assert inspector.get_resource(connection, authority, "source") is None
    assert inspector.get_original(connection, authority, "source") is None


def test_inspector_lists_supported_memory_until_source_deletion(source_database):
    connection, authority = source_database
    repository = SourceRepository()
    inspector = SourceInspector()
    repository.accept(connection, authority, command())
    connection.execute(
        "UPDATE users SET index_generation_id='generation' WHERE tenant_id=%s AND user_id=%s",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "INSERT INTO passages(tenant_id,user_id,passage_id,evidence_id,parse_revision,"
        "locator,normalized_text,generation_id,visible_from) "
        "VALUES (%s,%s,'passage','evidence1',1,'{}','A supported preference.',"
        "'generation',1)",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "INSERT INTO memories(tenant_id,user_id,memory_id,canonical_claim,"
        "assertion_kind,generation_id,visible_from) "
        "VALUES (%s,%s,'fact','A supported preference.','stated','generation',1)",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "INSERT INTO support(tenant_id,user_id,memory_id,evidence_id,passage_id,"
        "support_kind,span) VALUES (%s,%s,'fact','evidence1','passage','exact','{}')",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "INSERT INTO search_units(tenant_id,user_id,unit_id,source_id,source_version,"
        "evidence_id,memory_id,kind,origin,content_text,generation_id,embedding,"
        "lexical_text,locator) VALUES (%s,%s,'fact','source',1,'evidence1','fact',"
        "'fact','memory','A supported preference.','generation',%s::vector,"
        "'A supported preference.','{}')",
        (authority.tenant_id, authority.user_id, str([1.0] + [0.0] * 1023)),
    )

    before = inspector.list_memories(connection, authority, after=None)
    assert [(item["unit_id"], item["kind"]) for item in before["items"]] == [("fact", "fact")]
    assert before["items"][0]["citations"][0]["source_id"] == "source"
    repository.delete(
        connection,
        authority,
        source_id="source",
        resource_only=False,
        operation_id="cleanup",
        work_unit_id="cleanup-work",
        attempts_remaining=2,
    )
    assert inspector.list_memories(connection, authority, after=None)["items"] == []
