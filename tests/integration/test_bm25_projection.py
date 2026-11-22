"""Transactional lexical projections on an explicitly isolated PostgreSQL target."""

import os
import shutil
import string
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from random import Random
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from test_recall_repository import add_fact

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.db.migrations import apply_migrations
from oam.retrieval.repository import RecallRepository

pytestmark = pytest.mark.integration
pytest_plugins = ["test_core_http", "test_recall_repository"]


def projection(connection, owner):
    key = (owner.tenant_id, owner.user_id)
    return tuple(
        connection.execute(
            f"SELECT {columns} FROM {table} WHERE tenant_id=%s AND user_id=%s ORDER BY 1,2,3",
            key,
        ).fetchall()
        for table, columns in (
            ("bm25_postings", "unit_id,field,lexeme,tf,length"),
            ("bm25_term_stats", "field,lexeme,df"),
            ("bm25_corpus_stats", "field,n,total_length"),
        )
    )


def test_bm25_projection_insert_update_rename_delete_and_rollback(recall_database):
    connection, owner = recall_database
    add_fact(connection, owner, "first", "Cats cats run")
    assert projection(connection, owner) == (
        [("first", 1, "cat", 2, 3), ("first", 1, "run", 1, 3)],
        [(1, "cat", 1), (1, "run", 1)],
        [(1, 1, 3)],
    )
    key = (owner.tenant_id, owner.user_id)
    connection.execute(
        "UPDATE search_units SET lexical_title_english='cats cats',"
        "lexical_english='',lexical_simple='' WHERE tenant_id=%s AND user_id=%s",
        key,
    )
    assert projection(connection, owner) == (
        [("first", 4, "cat", 2, 2)],
        [(4, "cat", 1)],
        [(4, 1, 2)],
    )
    connection.execute(
        "UPDATE search_units SET unit_id='renamed' WHERE tenant_id=%s AND user_id=%s", key
    )
    before = projection(connection, owner)
    assert before[0] == [("renamed", 4, "cat", 2, 2)]
    with connection.transaction(force_rollback=True):
        connection.execute("DELETE FROM search_units WHERE tenant_id=%s AND user_id=%s", key)
        assert projection(connection, owner) == ([], [], [])
    assert projection(connection, owner) == before
    with connection.transaction(force_rollback=True):
        add_fact(connection, owner, "rolled-back", "beta beta")
    assert projection(connection, owner) == before
    connection.execute(
        "UPDATE search_units SET lexical_title_english='' WHERE tenant_id=%s AND user_id=%s", key
    )
    assert projection(connection, owner) == ([], [], [])


def test_bm25_projection_rls_hides_and_rejects_other_owner(recall_database):
    connection, owner = recall_database
    add_fact(connection, owner, "first", "alpha")
    connection.execute("SET LOCAL ROLE oam_api")
    connection.execute("SELECT set_config('oam.tenant_id',%s,true)", (owner.tenant_id,))
    connection.execute("SELECT set_config('oam.user_id',%s,true)", (owner.user_id,))
    add_fact(connection, owner, "second", "alpha beta")
    assert projection(connection, owner)[1] == [(1, "alpha", 2), (1, "beta", 1)]
    connection.execute(
        "DELETE FROM search_units WHERE tenant_id=%s AND user_id=%s AND unit_id='second'",
        (owner.tenant_id, owner.user_id),
    )
    assert projection(connection, owner)[1:] == ([(1, "alpha", 1)], [(1, 1, 1)])
    connection.execute("SELECT set_config('oam.user_id','someone-else',true)")
    assert connection.execute("SELECT * FROM bm25_postings").fetchall() == []
    with pytest.raises(psycopg.errors.InsufficientPrivilege), connection.transaction():
        connection.execute(
            "INSERT INTO bm25_term_stats VALUES (%s,%s,1,oam_bm25_term_key('spoof'),'spoof',1)",
            (owner.tenant_id, owner.user_id),
        )


def test_bm25_projection_counts_unbounded_tf_and_skips_oversized_tokens(recall_database):
    connection, owner = recall_database
    add_fact(connection, owner, "long", " ".join(["cats"] * 300 + ["x" * 2048]))
    assert projection(connection, owner) == (
        [("long", 1, "cat", 300, 300)],
        [(1, "cat", 1)],
        [(1, 1, 300)],
    )
    before = projection(connection, owner)
    connection.execute(
        "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,source_id,source_version,"
        "memory_id,generation_id,content_text,locator,embedding,lexical_text) "
        "SELECT tenant_id,user_id,unit_id,kind,origin,source_id,source_version,"
        "memory_id,generation_id,content_text,locator,embedding,lexical_text FROM search_units "
        "WHERE tenant_id=%s AND user_id=%s ON CONFLICT DO NOTHING",
        (owner.tenant_id, owner.user_id),
    )
    assert projection(connection, owner) == before


def test_bm25_projection_preserves_fts_at_lexeme_byte_boundary(recall_database):
    connection, owner = recall_database
    lexeme = "x" * 2047
    add_fact(connection, owner, "boundary", lexeme)
    fts_terms = connection.execute(
        "SELECT tsvector_to_array(lexical_bilingual) FROM search_units "
        "WHERE tenant_id=%s AND user_id=%s AND unit_id='boundary'",
        (owner.tenant_id, owner.user_id),
    ).fetchone()[0]
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, owner, config, deadline=time.monotonic() + 5)
    matches = repository.lexical(lexeme, 5)
    assert [row["unit_id"] for row in matches] == (["boundary"] if fts_terms else [])
    assert [row[2] for row in projection(connection, owner)[0]] == fts_terms


def test_bm25_concurrent_same_owner_insert_keeps_exact_statistics(core_database):
    dsn, tenant, _ = core_database
    owner = AuthorizedUser("key", tenant, "reader", "scope", frozenset({"memory:read"}))
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime) "
            "VALUES (%s,'scope','synthetic','test','persistent')",
            (tenant,),
        )
    start = Barrier(2, timeout=5)

    def write_unit(unit_id):
        with psycopg.connect(dsn, options="-c statement_timeout=5000") as connection:
            start.wait()
            add_fact(connection, owner, unit_id, "alpha alpha beta")

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(write_unit, ["first", "second"]))
    with psycopg.connect(dsn) as connection:
        assert projection(connection, owner)[1:] == ([(1, "alpha", 2), (1, "beta", 2)], [(1, 2, 6)])


@pytest.mark.parametrize("long_key", [False, True], ids=["typical", "wide-owner-and-term"])
def test_bm25_upgrade_backfills_stored_lexical_fields(
    tmp_path, synthetic_model_environment, long_key
):
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    name = f"oam_bm25_upgrade_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as administrator:
        administrator.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(dsn, dbname=name)
    migrations = Path(__file__).parents[2] / "migrations"
    previous = tmp_path / "previous"
    previous.mkdir()
    for migration in migrations.glob("*.sql"):
        if int(migration.name[:4]) < 40:
            shutil.copy2(migration, previous / migration.name)
    apply_migrations(dsn, previous)
    random = Random(17)
    user_id = (
        "".join(chr(random.randrange(0x20000, 0x2A6E0)) for _ in range(256))
        if long_key
        else "scope"
    )
    text = "".join(random.choices(string.ascii_lowercase, k=2040)) if long_key else "Cats cats"
    owner = AuthorizedUser("key", "upgrade", "reader", user_id, frozenset({"memory:read"}))
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES ('upgrade','active')")
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,index_generation_id) "
            "VALUES ('upgrade',%s,'synthetic','test','persistent','generation')",
            (user_id,),
        )
        add_fact(connection, owner, "old", text)
        connection.execute(
            "UPDATE search_units SET lexical_heading_english='birds',lexical_title_simple='中文'"
        )
    apply_migrations(dsn, migrations)
    with psycopg.connect(dsn) as connection:
        if long_key:
            add_fact(connection, owner, "new", text)
            config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
            connection.execute("SET LOCAL ROLE oam_api")
            repository = RecallRepository(connection, owner, config, deadline=time.monotonic() + 5)
            results = repository.lexical(text, 5)
            assert {row["unit_id"] for row in results} == {"old", "new"}
            assert all(row["strict_match"] and row["score"] > 0.2 for row in results)
            return
        assert projection(connection, owner) == (
            [("old", 1, "cat", 2, 2), ("old", 3, "bird", 1, 1), ("old", 4, "中文", 1, 1)],
            [(1, "cat", 1), (3, "bird", 1), (4, "中文", 1)],
            [(1, 1, 2), (3, 1, 1), (4, 1, 1)],
        )
