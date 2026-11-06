"""Operator admission of L2 work over already completed L1 indexes."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from oam.auth.credentials import AuthorizedUser
from oam.config import ConfigurationError, OAMConfig, load_config
from oam.db.context import bind_user
from oam.db.eligibility import ELIGIBLE_UNITS
from oam.db.queue_admission import QueueAdmissionFull, require_queue_capacity
from oam.indexing.scheduling import enqueue_consolidation
from oam.workers.l2_hold import _DATABASE_TIMEOUT_SECONDS, _require_unfiltered_operator


def queue_existing_l1(
    dsn: str, config: OAMConfig, tenant_id: str, user_id: str, limit: int
) -> dict[str, Any]:
    """Atomically enqueue a bounded set; existing jobs retain their retry ownership."""
    if not tenant_id.strip() or not user_id.strip() or limit <= 0:
        raise ValueError("tenant/user must be nonempty and limit must be positive")
    if not config.index.insights_enabled:
        raise ValueError("enable L2 indexing in the selected configuration first")
    started = time.monotonic()
    authority = AuthorizedUser("operator", tenant_id, "operator", user_id, frozenset())
    scheduled: list[str] = []
    queue_full = False
    with psycopg.connect(dsn, connect_timeout=_DATABASE_TIMEOUT_SECONDS) as connection:
        _require_unfiltered_operator(connection)
        connection.execute("SELECT set_config('statement_timeout',%s,true)", ("5s",))
        bind_user(connection.cursor(), authority)
        user = connection.execute(
            "SELECT state FROM users WHERE tenant_id=%s AND user_id=%s FOR UPDATE",
            (tenant_id, user_id),
        ).fetchone()
        if user is None or user[0] != "active":
            raise ValueError("active user not found")
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                f"WITH eligible AS ({ELIGIBLE_UNITS}) "
                "SELECT DISTINCT ON (o.source_id,o.source_version,w.generation_id) "
                "o.operation_id,o.source_id,o.source_version,w.generation_id,w.input_digest "
                "FROM operations o JOIN work_units w USING(tenant_id,user_id,operation_id) "
                "JOIN sources s ON (s.tenant_id,s.user_id,s.source_id)="
                "(o.tenant_id,o.user_id,o.source_id) JOIN source_versions sv ON "
                "(sv.tenant_id,sv.user_id,sv.source_id,sv.source_version)="
                "(o.tenant_id,o.user_id,o.source_id,o.source_version) "
                "WHERE o.tenant_id=%s AND o.user_id=%s AND o.kind='index_interaction' "
                "AND o.status='succeeded' AND w.stage='index_source' AND w.status='succeeded' "
                "AND o.stage_state->>'memory_ready'='true' "
                "AND o.stage_state->>'links_ready'='true' "
                "AND s.status='active' AND s.input_role='interaction' "
                "AND s.active_version=o.source_version AND sv.status='active' "
                "AND EXISTS(SELECT 1 FROM eligible u WHERE u.kind='fact' "
                "AND (u.source_id,u.source_version,u.generation_id)="
                "(o.source_id,o.source_version,w.generation_id)) "
                "AND NOT EXISTS(SELECT 1 FROM operations prior JOIN work_units pw "
                "USING(tenant_id,user_id,operation_id) WHERE prior.tenant_id=o.tenant_id "
                "AND prior.user_id=o.user_id AND prior.source_id=o.source_id "
                "AND prior.source_version=o.source_version AND prior.kind='consolidate' "
                "AND pw.generation_id=w.generation_id) "
                "ORDER BY o.source_id,o.source_version,w.generation_id,"
                "o.accepted_seq,o.operation_id "
                "LIMIT %s",
                (tenant_id, user_id, limit),
            )
            sources = cursor.fetchall()
        for source in sources:
            try:
                require_queue_capacity(
                    connection.cursor(), authority, config.queue.max_pending_per_user
                )
            except QueueAdmissionFull:
                queue_full = True
                break
            scheduled.append(
                enqueue_consolidation(
                    connection,
                    config,
                    key=(tenant_id, user_id),
                    source_operation_id=source["operation_id"],
                    source=source,
                    generation_id=source["generation_id"],
                    delay_hours=0,
                )
            )
    return {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "scheduled": scheduled,
        "scheduled_count": len(scheduled),
        "queue_full": queue_full,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
    }


def l2_index_from_environment(
    config_path: str | Path, tenant_id: str, user_id: str, limit: int
) -> int:
    try:
        config = load_config(config_path).config
    except ConfigurationError:
        print(json.dumps({"error": "invalid OAM configuration"}), file=sys.stderr)
        return 2
    name = config.database.migration_dsn_env
    dsn = os.environ.get(name)
    if not dsn:
        print(
            json.dumps({"error": f"missing required environment variable {name}"}), file=sys.stderr
        )
        return 2
    try:
        print(json.dumps(queue_existing_l1(dsn, config, tenant_id, user_id, limit)))
        return 0
    except ValueError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    except psycopg.Error:
        print(json.dumps({"error": "operator database action failed"}), file=sys.stderr)
        return 1
