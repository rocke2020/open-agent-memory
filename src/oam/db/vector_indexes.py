"""Privileged, restartable HNSW provisioning over existing search vectors."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path
from threading import Event

import psycopg
from psycopg import sql
from psycopg.pq import TransactionStatus

from oam.config import ConfigurationError, OAMConfig, load_config

_LOCK_KEY = "oam:search-unit-vector-indexes"


def owner_index_name(tenant_id: str, user_id: str) -> str:
    identity = json.dumps([tenant_id, user_id], ensure_ascii=False, separators=(",", ":"))
    return "su_ann_owner_" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _index_health(connection, name: str, tenant: str, user: str) -> bool | None:
    row = connection.execute(
        "SELECT i.indisvalid AND i.indisready, "
        "i.indrelid='public.search_units'::regclass AND am.amname='hnsw' "
        "AND opc.opcname='vector_cosine_ops' AND i.indnkeyatts=1 "
        "AND i.indkey[0]=(SELECT attnum FROM pg_attribute "
        "WHERE attrelid=i.indrelid AND attname='embedding') "
        "AND pg_get_expr(i.indpred,i.indrelid)=format("
        "'((tenant_id = %%L::text) AND (user_id = %%L::text))',%s::text,%s::text) "
        "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_am am ON am.oid=c.relam JOIN pg_opclass opc ON opc.oid=i.indclass[0] "
        "WHERE n.nspname='public' AND c.relname=%s",
        (tenant, user, name),
    ).fetchone()
    if row is None:
        return None
    if not row[1]:
        raise ValueError("owner index name conflicts with another index definition")
    return bool(row[0])


def reconcile_vector_indexes(
    connection,
    config: OAMConfig,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    stop: Event | None = None,
) -> dict:
    """Catalog is the checkpoint; the direct autocommit operator connection owns DDL."""
    if not connection.autocommit or connection.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError("vector-index maintenance requires an autocommit operator connection")
    if user_id is not None and tenant_id is None:
        raise ValueError("a selected user requires its tenant ID")
    role = connection.execute(
        "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
    ).fetchone()
    if not role or not role[0]:
        raise ValueError("vector-index maintenance requires an unfiltered operator role")
    acquired = connection.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
        (_LOCK_KEY,),
    ).fetchone()[0]
    if not acquired:
        return {"busy": True, "owners": []}
    started = time.monotonic()
    try:
        connection.execute("SET lock_timeout='5s'")
        connection.execute("SET statement_timeout='15min'")
        query = "SELECT tenant_id,user_id FROM users WHERE state='active'"
        parameters: tuple[object, ...] = ()
        if tenant_id is not None:
            query += " AND tenant_id=%s"
            parameters = (tenant_id,)
            if user_id is not None:
                query += " AND user_id=%s"
                parameters += (user_id,)
        owners = connection.execute(query + " ORDER BY tenant_id,user_id", parameters).fetchall()
        if tenant_id is not None and not owners:
            raise ValueError("selected user was not found")
        results = []
        mode = config.database.vector_index_mode
        for tenant, user in owners:
            if stop is not None and stop.is_set():
                break
            name = owner_index_name(tenant, user)
            result = {"tenant_id": tenant, "user_id": user, "index": name, "created": False}
            if mode == "small":
                results.append({**result, "state": "shared"})
                continue
            healthy = _index_health(connection, name, tenant, user)
            if healthy:
                results.append({**result, "state": "ready"})
                continue
            bound = config.database.vector_index_min_rows if mode == "growing" else 1
            count = connection.execute(
                "SELECT count(*) FROM (SELECT 1 FROM search_units "
                "WHERE tenant_id=%s AND user_id=%s AND embedding IS NOT NULL LIMIT %s) bounded",
                (tenant, user, bound),
            ).fetchone()[0]
            if count < bound:
                results.append({**result, "state": "shared"})
                continue
            if healthy is False:
                # Only a catalog-verified index owned by this exact scope can be repaired.
                connection.execute(
                    sql.SQL("DROP INDEX CONCURRENTLY {}").format(sql.Identifier("public", name))
                )
            connection.execute(
                sql.SQL(
                    "CREATE INDEX CONCURRENTLY {} ON public.search_units "
                    "USING hnsw (embedding vector_cosine_ops) "
                    "WHERE tenant_id={} AND user_id={}"
                ).format(
                    sql.Identifier(name),
                    sql.Literal(tenant),
                    sql.Literal(user),
                )
            )
            if _index_health(connection, name, tenant, user) is not True:
                raise ValueError("owner vector index did not become valid and ready")
            results.append({**result, "state": "ready", "created": True})
        return {"busy": False, "owners": results, "duration_seconds": time.monotonic() - started}
    finally:
        connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (_LOCK_KEY,))


def vector_indexes_from_environment(
    config_path: str | Path, *, once: bool, tenant_id: str | None, user_id: str | None
) -> int:
    """Run once or reconcile periodically; shutdown drains the active concurrent build."""
    stop = Event()
    previous = {}
    if not once:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
    try:
        config = load_config(config_path).config
        dsn_name = config.database.migration_dsn_env
        dsn = os.environ.get(dsn_name)
        if not dsn:
            raise ValueError(f"missing required environment variable {dsn_name}")
        while not stop.is_set():
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as connection:
                report = reconcile_vector_indexes(
                    connection, config, tenant_id=tenant_id, user_id=user_id, stop=stop
                )
            print(json.dumps(report), flush=True)
            if once:
                return 0
            stop.wait(config.database.vector_index_poll_seconds)
        return 0
    except (ConfigurationError, ValueError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    except psycopg.Error:
        print(
            json.dumps({"error": "vector-index maintenance failed; inspect index catalog"}),
            file=sys.stderr,
        )
        return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
