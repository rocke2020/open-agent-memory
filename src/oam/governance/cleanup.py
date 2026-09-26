"""Clean one already excluded source under its durable work claim."""

from __future__ import annotations

import hashlib

import psycopg
from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedUser
from oam.db.context import bind_user
from oam.workers.claims import ClaimedWork


def affected_trace_ids(rows, removed_unit_ids):
    removed = set(removed_unit_ids)
    affected = []
    for trace_id, decisions in rows:
        candidates = set(decisions.get("result_ids", ()))
        for arm_ids in decisions.get("arms", {}).values():
            candidates.update(arm_ids)
        if candidates & removed:
            affected.append(trace_id)
    return affected


def cleanup_claim(dsn: str, claim: ClaimedWork, worker_id: str) -> None:
    key = (claim.tenant_id, claim.user_id)
    authority = AuthorizedUser(
        "worker", claim.tenant_id, "worker", claim.user_id, frozenset({"source:delete"})
    )
    try:
        with psycopg.connect(dsn) as connection:
            bind_user(connection.cursor(), authority)
            connection.execute(
                "SELECT 1 FROM users WHERE tenant_id=%s AND user_id=%s FOR UPDATE", key
            )
            row = connection.execute(
                "SELECT o.source_id,o.status,w.owner_id,w.fence,w.status,"
                "w.lease_expires_at>clock_timestamp(),s.status "
                "FROM work_units w JOIN operations o USING(tenant_id,user_id,operation_id) "
                "JOIN sources s ON (s.tenant_id,s.user_id,s.source_id)="
                "(o.tenant_id,o.user_id,o.source_id) "
                "WHERE w.tenant_id=%s AND w.user_id=%s AND w.work_unit_id=%s "
                "AND w.stage='cleanup_source' FOR UPDATE OF o,w,s",
                (*key, claim.work_unit_id),
            ).fetchone()
            if row is None or (
                row[1] not in {"accepted", "running"}
                or row[2:] != (worker_id, claim.fence, "running", True, "deleting")
            ):
                raise RuntimeError("cleanup claim is no longer eligible")
            source_id = row[0]
            memory_ids = [
                item[0]
                for item in connection.execute(
                    "SELECT DISTINCT sp.memory_id FROM support sp JOIN evidence e "
                    "USING(tenant_id,user_id,evidence_id) WHERE sp.tenant_id=%s "
                    "AND sp.user_id=%s AND e.source_id=%s",
                    (*key, source_id),
                ).fetchall()
            ]
            insight_ids = [
                item[0]
                for item in connection.execute(
                    "SELECT DISTINCT insight_id FROM insight_support WHERE tenant_id=%s "
                    "AND user_id=%s AND memory_id=ANY(%s)",
                    (*key, memory_ids),
                ).fetchall()
            ]
            unit_ids = [
                item[0]
                for item in connection.execute(
                    "SELECT unit_id FROM search_units WHERE tenant_id=%s AND user_id=%s "
                    "AND (source_id=%s OR memory_id=ANY(%s) OR insight_id=ANY(%s))",
                    (*key, source_id, memory_ids, insight_ids),
                ).fetchall()
            ]
            connection.execute(
                "DELETE FROM temporal_links WHERE tenant_id=%s AND user_id=%s "
                "AND (left_id=ANY(%s) OR right_id=ANY(%s))",
                (*key, unit_ids, unit_ids),
            )
            connection.execute(
                "DELETE FROM unit_links WHERE tenant_id=%s AND user_id=%s "
                "AND (source_id=ANY(%s) OR target_id=ANY(%s))",
                (*key, unit_ids, unit_ids),
            )
            for table in ("unit_times", "unit_entities"):
                connection.execute(
                    f"DELETE FROM {table} WHERE tenant_id=%s AND user_id=%s AND unit_id=ANY(%s)",
                    (*key, unit_ids),
                )
            connection.execute(
                "DELETE FROM search_units WHERE tenant_id=%s AND user_id=%s AND unit_id=ANY(%s)",
                (*key, unit_ids),
            )
            connection.execute(
                "DELETE FROM insight_support WHERE tenant_id=%s AND user_id=%s "
                "AND insight_id=ANY(%s)",
                (*key, insight_ids),
            )
            connection.execute(
                "DELETE FROM insights WHERE tenant_id=%s AND user_id=%s AND insight_id=ANY(%s)",
                (*key, insight_ids),
            )
            connection.execute(
                "DELETE FROM support WHERE tenant_id=%s AND user_id=%s AND memory_id=ANY(%s)",
                (*key, memory_ids),
            )
            connection.execute(
                "DELETE FROM memories WHERE tenant_id=%s AND user_id=%s AND memory_id=ANY(%s)",
                (*key, memory_ids),
            )
            connection.execute(
                "DELETE FROM passages WHERE tenant_id=%s AND user_id=%s AND evidence_id IN "
                "(SELECT evidence_id FROM evidence WHERE tenant_id=%s AND user_id=%s "
                "AND source_id=%s)",
                (*key, *key, source_id),
            )
            connection.execute(
                "DELETE FROM evidence WHERE tenant_id=%s AND user_id=%s AND source_id=%s",
                (*key, source_id),
            )
            connection.execute(
                "DELETE FROM index_stage_results WHERE tenant_id=%s AND user_id=%s "
                "AND source_id=%s",
                (*key, source_id),
            )
            connection.execute(
                "UPDATE source_versions SET original_bytes=%s,content_sha256=%s,byte_length=0 "
                "WHERE tenant_id=%s AND user_id=%s AND source_id=%s",
                (b"", hashlib.sha256(b"").digest(), *key, source_id),
            )
            traces = connection.execute(
                "SELECT trace_id,decisions FROM traces WHERE tenant_id=%s AND user_id=%s "
                "AND content IS NOT NULL",
                key,
            ).fetchall()
            redacted = affected_trace_ids(traces, unit_ids)
            if redacted:
                connection.execute(
                    "UPDATE traces SET content=NULL,decisions='{}'::jsonb WHERE tenant_id=%s "
                    "AND user_id=%s AND trace_id=ANY(%s)",
                    (*key, redacted),
                )
            connection.execute(
                "DELETE FROM export_parts WHERE tenant_id=%s AND user_id=%s",
                key,
            )
            connection.execute(
                "UPDATE exports SET state='expired',artifact_bytes=NULL,manifest='{}'::jsonb "
                "WHERE tenant_id=%s AND user_id=%s",
                key,
            )
            connection.execute(
                "UPDATE sources SET status='deleted' WHERE tenant_id=%s AND user_id=%s "
                "AND source_id=%s",
                (*key, source_id),
            )
            connection.execute(
                "UPDATE work_units SET status='succeeded',updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s",
                (*key, claim.work_unit_id),
            )
            connection.execute(
                "UPDATE attempts SET disposition='succeeded',finished_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s",
                (*key, claim.work_unit_id, claim.attempt_id),
            )
            connection.execute(
                "UPDATE operations SET status='succeeded',updated_at=clock_timestamp(),"
                "stage_state=stage_state||%s::jsonb WHERE tenant_id=%s AND user_id=%s "
                "AND operation_id=%s",
                (Jsonb({"active_store_cleanup": "complete"}), *key, claim.operation_id),
            )
    except Exception:
        _mark_failed(dsn, authority, claim, worker_id)
        raise


def _mark_failed(dsn: str, authority: AuthorizedUser, claim: ClaimedWork, worker_id: str) -> None:
    key = (claim.tenant_id, claim.user_id)
    with psycopg.connect(dsn) as connection:
        bind_user(connection.cursor(), authority)
        row = connection.execute(
            "UPDATE work_units SET status=CASE WHEN attempts_remaining>0 THEN 'queued' "
            "ELSE 'failed_terminal' END,updated_at=clock_timestamp() WHERE tenant_id=%s "
            "AND user_id=%s AND work_unit_id=%s AND owner_id=%s AND fence=%s "
            "AND status='running' RETURNING status",
            (*key, claim.work_unit_id, worker_id, claim.fence),
        ).fetchone()
        if row is not None:
            connection.execute(
                "UPDATE attempts SET disposition='failed',finished_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s",
                (*key, claim.work_unit_id, claim.attempt_id),
            )
            connection.execute(
                "UPDATE operations SET status=%s,updated_at=clock_timestamp(),"
                "stage_state=stage_state||%s::jsonb WHERE tenant_id=%s AND user_id=%s "
                "AND operation_id=%s",
                (
                    "running" if row[0] == "queued" else "partial_failed",
                    Jsonb({"active_store_cleanup": "pending" if row[0] == "queued" else "failed"}),
                    *key,
                    claim.operation_id,
                ),
            )
