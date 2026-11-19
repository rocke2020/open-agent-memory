"""Admit user-level L2 windows while retaining existing source-owned jobs."""

from collections.abc import Mapping
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from oam.config import OAMConfig
from oam.indexing.store import stable_id


def enqueue_consolidation(
    connection: psycopg.Connection,
    config: OAMConfig,
    *,
    key: tuple[str, str],
    source_operation_id: str,
    source: Mapping[str, Any],
    generation_id: str,
    delay_hours: float | None = None,
) -> str:
    """Admit once under the caller's user lock; explicit manual work stays immediate."""
    if delay_hours is not None:
        return _enqueue_source_consolidation(
            connection,
            config,
            key=key,
            source_operation_id=source_operation_id,
            source=source,
            generation_id=generation_id,
            delay_hours=delay_hours,
        )
    member = connection.execute(
        "SELECT batch_id FROM l2_batch_members WHERE tenant_id=%s AND user_id=%s "
        "AND source_id=%s AND source_version=%s AND generation_id=%s",
        (*key, source["source_id"], source["source_version"], generation_id),
    ).fetchone()
    if member:
        return member[0]
    legacy_id = stable_id("op_consolidate", source_operation_id, generation_id)
    if connection.execute(
        "SELECT 1 FROM operations WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
        (*key, legacy_id),
    ).fetchone():
        return legacy_id
    clock = connection.execute("SELECT clock_timestamp()").fetchone()
    if clock is None:
        raise RuntimeError("consolidation admission time is unavailable")
    admitted_at = clock[0]
    delay = config.index.insight_consolidation_delay_hours
    window = (
        connection.execute(
            "SELECT o.operation_id FROM operations o JOIN work_units w "
            "USING(tenant_id,user_id,operation_id) WHERE o.tenant_id=%s AND o.user_id=%s "
            "AND w.generation_id=%s AND w.stage='consolidate_batch' "
            "AND o.retry_of_operation_id IS NULL AND o.status='accepted' "
            "AND o.cancellation_revision=0 AND w.status='queued' "
            "AND (o.stage_state->>'l2_due_at')::timestamptz>%s "
            "ORDER BY o.created_at,o.operation_id LIMIT 1",
            (*key, generation_id, admitted_at),
        ).fetchone()
        if delay > 0
        else None
    )
    operation_id = (
        window[0] if window else stable_id("op_l2_batch", *key, generation_id, source_operation_id)
    )
    if not window:
        from datetime import timedelta

        due_at = admitted_at + timedelta(hours=delay)
        state = {
            "l2_batch_id": operation_id,
            "l2_due_at": due_at.isoformat(),
            "l2_opened_at": admitted_at.isoformat(),
            "l2_seed_policy": config.index.insight_seed_policy,
            "l2_source_batch": config.index.insight_source_batch.model_dump(mode="json")
            if config.index.insight_source_batch
            else None,
        }
        connection.execute(
            "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,"
            "accepted_seq,kind,schema_version,status,plan_sealed,stage_state) "
            "SELECT tenant_id,user_id,%s,principal_id,accepted_seq,'consolidate',"
            "schema_version,'accepted',true,%s FROM operations "
            "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
            (operation_id, Jsonb(state), *key, source_operation_id),
        )
        connection.execute(
            "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
            "input_digest,generation_id,status,available_at,attempts_remaining,l2_priority_at) "
            "VALUES(%s,%s,%s,%s,'consolidate_batch',%s,%s,'queued',%s,%s,%s)",
            (
                *key,
                stable_id("work_l2_batch", operation_id),
                operation_id,
                source["input_digest"],
                generation_id,
                due_at,
                config.retry.max_attempts,
                admitted_at,
            ),
        )
    connection.execute(
        "INSERT INTO l2_batch_members(tenant_id,user_id,source_id,source_version,"
        "generation_id,batch_id,source_operation_id,admitted_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            *key,
            source["source_id"],
            source["source_version"],
            generation_id,
            operation_id,
            source_operation_id,
            admitted_at,
        ),
    )
    connection.execute(
        "UPDATE operations SET stage_state=stage_state||%s,updated_at=clock_timestamp() "
        "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
        (
            Jsonb(
                {
                    "insights_ready": False,
                    "consolidation_operation_id": operation_id,
                    "consolidation_status": "pending",
                }
            ),
            *key,
            source_operation_id,
        ),
    )
    return operation_id


def _enqueue_source_consolidation(
    connection: psycopg.Connection,
    config: OAMConfig,
    *,
    key: tuple[str, str],
    source_operation_id: str,
    source: Mapping[str, Any],
    generation_id: str,
    delay_hours: float | None = None,
) -> str:
    """Caller holds the user lock and owns the transaction and admission check."""
    operation_id = stable_id("op_consolidate", source_operation_id, generation_id)
    work_id = stable_id("work_consolidate", operation_id)
    connection.execute(
        "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,"
        "source_id,source_version,accepted_seq,kind,schema_version,status,"
        "plan_sealed,stage_state) SELECT tenant_id,user_id,%s,principal_id,"
        "source_id,source_version,accepted_seq,'consolidate',schema_version,"
        "'accepted',true,%s::jsonb FROM operations WHERE tenant_id=%s "
        "AND user_id=%s AND operation_id=%s ON CONFLICT DO NOTHING",
        (
            operation_id,
            Jsonb({"source_operation_id": source_operation_id}),
            *key,
            source_operation_id,
        ),
    )
    priority_row = connection.execute(
        "SELECT COALESCE(min(created_at),clock_timestamp()) "
        "FROM search_units WHERE tenant_id=%s AND user_id=%s "
        "AND kind='fact' AND source_id=%s AND source_version=%s AND generation_id=%s",
        (*key, source["source_id"], source["source_version"], generation_id),
    ).fetchone()
    if priority_row is None:
        raise RuntimeError("consolidation priority could not be read")
    priority = priority_row[0]
    connection.execute(
        "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,"
        "stage,input_digest,generation_id,status,available_at,attempts_remaining,"
        "l2_priority_at) VALUES (%s,%s,%s,%s,'consolidate',%s,%s,'queued',"
        "clock_timestamp()+(%s*interval '1 hour'),%s,%s) ON CONFLICT DO NOTHING",
        (
            *key,
            work_id,
            operation_id,
            source["input_digest"],
            generation_id,
            config.index.insight_consolidation_delay_hours if delay_hours is None else delay_hours,
            config.retry.max_attempts,
            priority,
        ),
    )
    connection.execute(
        "UPDATE operations SET stage_state=stage_state||%s::jsonb,updated_at=clock_timestamp() "
        "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
        (
            Jsonb(
                {
                    "insights_ready": False,
                    "consolidation_operation_id": operation_id,
                    "consolidation_status": "pending",
                }
            ),
            *key,
            source_operation_id,
        ),
    )
    return operation_id
