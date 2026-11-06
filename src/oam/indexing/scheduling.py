"""Queue source-owned consolidation without changing completed L1 evidence."""

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
