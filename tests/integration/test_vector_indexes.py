"""Physical ANN routing and provisioning on an explicitly ephemeral target."""

import json
import os
import re
import select
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from test_index_links import add_fact

from oam.auth.credentials import AuthorizedUser
from oam.cli import main
from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.db.context import bind_user
from oam.indexing.links import propose_semantic_links, publish_semantic_links
from oam.retrieval.repository import RecallRepository

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[2]


@pytest.fixture
def ann_database(monkeypatch):
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly ephemeral PostgreSQL target")
    tenant = "ann-" + uuid4().hex
    with psycopg.connect(dsn) as connection:
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (tenant,))
        for user in ("alice", "bob"):
            connection.execute(
                "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,"
                "index_generation_id) VALUES (%s,%s,'synthetic','test','persistent','generation')",
                (tenant, user),
            )
        authority = AuthorizedUser("key", tenant, "worker", "alice", frozenset({"memory:read"}))
        for index in range(40):
            add_fact(
                connection, authority, f"fact-{index:03}", day=index, vector=(1.0, index / 100)
            )
        connection.execute(
            'UPDATE support SET span=span||\'{"role":"user"}\'::jsonb WHERE tenant_id=%s',
            (tenant,),
        )
        other = AuthorizedUser("key", tenant, "worker", "bob", frozenset({"memory:read"}))
        for index in range(120):
            add_fact(connection, other, f"foreign-{index:03}", vector=(1.0, index / 1000))
        connection.execute("ANALYZE search_units")
    monkeypatch.setenv("OAM_MIGRATION_DSN", dsn)
    return dsn, authority, load_config(ROOT / "configs/oam.yaml").config


def index_names(node):
    found = {node["Index Name"]} if "Index Name" in node else set()
    for child in node.get("Plans", []):
        found.update(index_names(child))
    return found


class PlanRepository(RecallRepository):
    def _execute(self, query, parameters=(), *, configure_ann=False):
        rows = super()._execute(query, parameters, configure_ann=configure_ann)
        values = (
            {
                **self.time_parameters,
                **parameters,
                "ann_tenant": self.authority.tenant_id,
                "ann_user": self.authority.user_id,
            }
            if isinstance(parameters, dict)
            else parameters
        )
        prepared = self.connection.execute(
            "SELECT name FROM pg_prepared_statements ORDER BY prepare_time DESC LIMIT 1"
        ).fetchone()
        if prepared is None:
            self.plan = self.connection.execute(
                "EXPLAIN (ANALYZE,FORMAT JSON) " + query, values
            ).fetchone()[0][0]["Plan"]
            return rows
        name = prepared[0]
        keys = dict.fromkeys(re.findall(r"%\(([^)]+)\)s", query))
        ordered = [values[key] for key in keys]
        self.plan = self.connection.execute(
            sql.SQL("EXPLAIN (ANALYZE,FORMAT JSON) EXECUTE {}({})").format(
                sql.Identifier(name), sql.SQL(",").join(sql.Literal(value) for value in ordered)
            )
        ).fetchone()[0][0]["Plan"]
        return rows


def test_insight_optional_fact_discovery_uses_ann_and_preserves_seeds(ann_database, monkeypatch):
    from oam.indexing import consolidation

    dsn, authority, config = ann_database
    observed = {}
    original = consolidation._rows

    def recording(connection, query, parameters=(), **kwargs):
        result = original(connection, query, parameters, **kwargs)
        prepared = connection.execute(
            "SELECT name FROM pg_prepared_statements ORDER BY prepare_time DESC LIMIT 1"
        ).fetchone()
        if prepared:
            plan = connection.execute(
                sql.SQL("EXPLAIN (ANALYZE,FORMAT JSON) EXECUTE {}({})").format(
                    sql.Identifier(prepared[0]),
                    sql.SQL(",").join(sql.Literal(value) for value in parameters),
                )
            ).fetchone()[0][0]["Plan"]
        else:
            plan = connection.execute(
                "EXPLAIN (ANALYZE,FORMAT JSON) " + query, parameters
            ).fetchone()[0][0]["Plan"]
        observed["plan"] = plan
        return result

    monkeypatch.setattr(consolidation, "_rows", recording)
    with psycopg.connect(dsn) as connection:
        connection.execute("SET LOCAL ROLE oam_content")
        bind_user(connection.cursor(), authority)
        store = SimpleNamespace(config=config, authority=authority, generation_id="generation")
        rows = consolidation._fact_context(store, connection, [{"unit_id": "fact-000"}], [])
        assert rows[0]["unit_id"] == "fact-000"
        assert len(rows) > 1
        assert "search_units_embedding" in index_names(observed["plan"])


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_dense_uses_ann_and_keeps_foreign_rows_out(ann_database, mode):
    dsn, authority, config = ann_database
    with psycopg.connect(dsn) as connection:
        connection.execute("SET LOCAL ROLE oam_content")
        repo = PlanRepository(
            connection,
            authority,
            config,
            deadline=time.monotonic() + 30,
            request=SearchRequest(query="synthetic query", recall_level=mode),
        )
        rows = repo.dense([1.0, *([0.0] * (config.index.embedding_dimensions - 1))], 5)
        assert len(rows) == 5
        assert rows[0]["unit_id"] == "fact-000"
        assert all(row["unit_id"].startswith("fact-") for row in rows)
        assert "search_units_embedding" in index_names(repo.plan)


def test_semantic_proposals_keep_k_while_publisher_enforces_remaining_capacity(ann_database):
    from oam.db.vector_indexes import reconcile_vector_indexes

    dsn, authority, config = ann_database
    config = config.model_copy(
        update={"database": config.database.model_copy(update={"vector_index_mode": "large"})}
    )
    with psycopg.connect(dsn, autocommit=True) as administrator:
        reconcile_vector_indexes(
            administrator, config, tenant_id=authority.tenant_id, user_id=authority.user_id
        )
    with psycopg.connect(dsn) as connection:
        connection.execute("SET LOCAL ROLE oam_content")
        bind_user(connection.cursor(), authority)
        connection.execute(
            "INSERT INTO unit_links(tenant_id,user_id,source_id,target_id,kind,weight) "
            "SELECT %s,%s,'fact-000','fact-'||lpad(i::text,3,'0'),'semantic',1 "
            "FROM generate_series(1,7) i",
            (authority.tenant_id, authority.user_id),
        )
        proposed = propose_semantic_links(connection, authority, config, "generation", ["fact-000"])
        assert len(proposed) == 20
        assert (
            publish_semantic_links(
                connection, authority, config, "generation", ["fact-000"], proposed
            )
            == 13
        )
        assert (
            publish_semantic_links(
                connection, authority, config, "generation", ["fact-000"], proposed
            )
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM unit_links WHERE source_id='fact-000' AND kind='semantic'"
            ).fetchone()[0]
            == 20
        )


def test_dedicated_cli_reuses_vectors_and_is_idempotent(ann_database, tmp_path, capsys):
    dsn, authority, runtime_config = ann_database
    config = tmp_path / "dedicated.yaml"
    config.write_text("database:\n  vector_index_mode: large\n")
    args = [
        "vector-indexes",
        "--config",
        str(config),
        "--once",
        "--tenant-id",
        authority.tenant_id,
        "--user-id",
        authority.user_id,
    ]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["owners"][0]["state"] == "ready"
    index = first["owners"][0]["index"]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["owners"][0]["created"] is False
    with psycopg.connect(dsn) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM search_units WHERE tenant_id=%s AND user_id=%s",
                (authority.tenant_id, authority.user_id),
            ).fetchone()[0]
            == 40
        )
        connection.execute("SET LOCAL ROLE oam_content")
        repo = PlanRepository(
            connection, authority, load_config(config).config, deadline=time.monotonic() + 30
        )
        assert (
            len(repo.dense([1.0, *([0.0] * (runtime_config.index.embedding_dimensions - 1))], 5))
            == 5
        )
        assert index in index_names(repo.plan)
    with psycopg.connect(dsn) as connection:
        connection.execute("SET LOCAL ROLE oam_content")
        shared = PlanRepository(
            connection,
            authority,
            load_config(ROOT / "configs/oam.yaml").config,
            deadline=time.monotonic() + 30,
        )
        assert (
            len(shared.dense([1.0, *([0.0] * (runtime_config.index.embedding_dimensions - 1))], 5))
            == 5
        )
        assert "search_units_embedding" in index_names(shared.plan)
        assert index not in index_names(shared.plan)


def test_growing_mode_promotes_without_changing_stored_rows(ann_database, tmp_path, capsys):
    dsn, authority, _ = ann_database
    path = tmp_path / "growing.yaml"
    path.write_text("database:\n  vector_index_mode: growing\n  vector_index_min_rows: 41\n")
    args = [
        "vector-indexes",
        "--config",
        str(path),
        "--once",
        "--tenant-id",
        authority.tenant_id,
        "--user-id",
        authority.user_id,
    ]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["owners"][0]["state"] == "shared"
    with psycopg.connect(dsn) as connection:
        add_fact(connection, authority, "threshold-crossing", vector=(1.0, 0.0))
    assert main(args) == 0
    owner = json.loads(capsys.readouterr().out)["owners"][0]
    assert owner["state"] == "ready" and owner["created"] is True


@pytest.mark.parametrize("consumer", ["temporal", "related"])
def test_additional_vector_discovery_uses_scoped_ann(ann_database, consumer):
    dsn, authority, config = ann_database
    request = SearchRequest(
        query="synthetic query",
        time_basis="mentioned",
        time_window={"start": "2026-01-01T00:00:00Z", "end": "2026-01-10T00:00:00Z"},
    )
    with psycopg.connect(dsn) as connection:
        connection.execute("SET LOCAL ROLE oam_content")
        repo = PlanRepository(
            connection, authority, config, deadline=time.monotonic() + 30, request=request
        )
        if consumer == "temporal":
            rows = repo.temporal_candidates(
                [1.0, *([0.0] * (config.index.embedding_dimensions - 1))], 5
            )
            assert len(rows) == 5
        else:
            row = repo.related_user_fact(["fact-000"])
            assert row is not None and row["unit_id"] == "fact-001"
        assert "search_units_embedding" in index_names(repo.plan)


@pytest.mark.parametrize("mode", ["small", "large"])
def test_reference_lookup_merges_bounded_ann_probes(ann_database, mode):
    from oam.indexing import references

    lookup = getattr(references, "reference_candidate_ids", None)
    assert callable(lookup)
    dsn, authority, config = ann_database
    config = config.model_copy(
        update={"database": config.database.model_copy(update={"vector_index_mode": mode})}
    )
    with psycopg.connect(dsn) as connection:
        connection.execute("SET LOCAL ROLE oam_content")
        bind_user(connection.cursor(), authority)
        connection.execute("SET LOCAL plan_cache_mode=force_custom_plan")
        settings = (
            "SELECT current_setting('enable_seqscan'),current_setting('enable_sort'),"
            "current_setting('plan_cache_mode')"
        )
        before = connection.execute(settings).fetchone()
        # Eligible historical generations and completed chunks of the same source
        # remain available to extraction references after adopting ANN discovery.
        for table in ("memories", "passages", "search_units"):
            connection.execute(
                sql.SQL(
                    "UPDATE {} SET generation_id='historical' WHERE tenant_id=%s "
                    "AND user_id=%s AND {}=%s"
                ).format(
                    sql.Identifier(table),
                    sql.Identifier(
                        {
                            "memories": "memory_id",
                            "passages": "passage_id",
                            "search_units": "unit_id",
                        }[table]
                    ),
                ),
                (authority.tenant_id, authority.user_id, "fact-039"),
            )
        rows = lookup(
            connection,
            authority,
            config,
            [
                [1.0, *([0.0] * (config.index.embedding_dimensions - 1))],
                [1.0, 0.39, *([0.0] * (config.index.embedding_dimensions - 2))],
            ],
        )
        assert len(rows) == config.extraction.reference_max_facts
        assert "fact-039" in rows and "fact-001" in rows
        assert "fact-000" in rows
        assert all(value.startswith("fact-") for value in rows)
        assert connection.execute(settings).fetchone() == before


def test_index_maintenance_stops_admitting_after_current_build(ann_database):
    from oam.db.vector_indexes import owner_index_name, reconcile_vector_indexes

    dsn, authority, config = ann_database
    stop = Event()
    config = config.model_copy(
        update={"database": config.database.model_copy(update={"vector_index_mode": "large"})}
    )

    class StopAfterBuild:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, query, parameters=()):
            result = self.connection.execute(query, parameters)
            rendered = query.as_string(self.connection) if hasattr(query, "as_string") else query
            if rendered.startswith("CREATE INDEX"):
                stop.set()
            return result

    with psycopg.connect(dsn, autocommit=True) as connection:
        result = reconcile_vector_indexes(
            StopAfterBuild(connection), config, tenant_id=authority.tenant_id, stop=stop
        )
        assert sum(owner["created"] for owner in result["owners"]) == 1
        assert (
            connection.execute(
                "SELECT to_regclass(%s)", (owner_index_name(authority.tenant_id, "bob"),)
            ).fetchone()[0]
            is None
        )
        assert (
            connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('oam:search-unit-vector-indexes',0))"
            ).fetchone()[0]
            is True
        )
        connection.execute(
            "SELECT pg_advisory_unlock(hashtextextended('oam:search-unit-vector-indexes',0))"
        )


def test_maintenance_cli_graceful_shutdown_releases_ownership(ann_database, tmp_path):
    dsn, authority, _ = ann_database
    path = tmp_path / "large.yaml"
    path.write_text("database:\n  vector_index_mode: large\n  vector_index_poll_seconds: 900\n")
    command = [
        sys.executable,
        "-c",
        "import sys; from oam.cli import main; raise SystemExit(main(sys.argv[1:]))",
        "vector-indexes",
        "--config",
        str(path),
        "--tenant-id",
        authority.tenant_id,
        "--user-id",
        authority.user_id,
    ]
    child = subprocess.Popen(
        command,
        cwd=tmp_path,
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(ROOT / "src"),
            "OAM_MIGRATION_DSN": dsn,
            **({"PGPASSWORD": os.environ["PGPASSWORD"]} if os.environ.get("PGPASSWORD") else {}),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 10)[0], "maintenance did not report an outcome"
        assert json.loads(child.stdout.readline())["owners"][0]["state"] == "ready"
        child.terminate()
        child.communicate(timeout=5)
        assert child.returncode == 0
        with psycopg.connect(dsn, autocommit=True) as connection:
            assert connection.execute(
                "SELECT pg_try_advisory_lock(hashtextextended('oam:search-unit-vector-indexes',0))"
            ).fetchone()[0]
            connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended('oam:search-unit-vector-indexes',0))"
            )
    finally:
        if child.poll() is None:
            child.terminate()
            child.communicate(timeout=5)


def test_index_repair_and_name_collision_preserve_content(ann_database):
    from oam.db.vector_indexes import owner_index_name, reconcile_vector_indexes

    dsn, authority, config = ann_database
    config = config.model_copy(
        update={"database": config.database.model_copy(update={"vector_index_mode": "large"})}
    )
    kwargs = {"tenant_id": authority.tenant_id, "user_id": authority.user_id}
    name = owner_index_name(authority.tenant_id, authority.user_id)
    with psycopg.connect(dsn, autocommit=True) as connection:
        reconcile_vector_indexes(connection, config, **kwargs)
        shadow = "shadow_" + uuid4().hex
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(shadow)))
        connection.execute(
            sql.SQL("CREATE TABLE {}.unrelated (id int)").format(sql.Identifier(shadow))
        )
        connection.execute(
            sql.SQL("CREATE INDEX {} ON {}.unrelated(id)").format(
                sql.Identifier(name), sql.Identifier(shadow)
            )
        )
        connection.execute(
            "UPDATE pg_index SET indisvalid=false WHERE indexrelid=%s::regclass",
            ("public." + name,),
        )
        connection.execute(sql.SQL("SET search_path TO {},public").format(sql.Identifier(shadow)))
        assert reconcile_vector_indexes(connection, config, **kwargs)["owners"][0]["created"]
        assert connection.execute("SELECT to_regclass(%s)", (shadow + "." + name,)).fetchone()[0]
        connection.execute("SET ROLE oam_content")
        with pytest.raises(ValueError, match="operator"):
            reconcile_vector_indexes(connection, config, **kwargs)
        connection.execute("RESET ROLE")
        other_name = owner_index_name(authority.tenant_id, "bob")
        connection.execute(
            sql.SQL("CREATE INDEX {} ON search_units(unit_id)").format(sql.Identifier(other_name))
        )
        with pytest.raises(ValueError, match="conflicts"):
            reconcile_vector_indexes(
                connection, config, tenant_id=authority.tenant_id, user_id="bob"
            )
        assert (
            connection.execute("SELECT to_regclass(%s)", (other_name,)).fetchone()[0] == other_name
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM search_units WHERE tenant_id=%s", (authority.tenant_id,)
            ).fetchone()[0]
            == 160
        )
