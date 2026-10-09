"""Clean one already excluded source under its durable work claim."""

from __future__ import annotations

import hashlib

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from oam.auth.credentials import AuthorizedUser
from oam.db.context import bind_user
from oam.indexing.store import input_digest
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


def cleanup_claim(
    dsn: str, claim: ClaimedWork, worker_id: str, *, pool: ConnectionPool | None = None
) -> None:
    key = (claim.tenant_id, claim.user_id)
    authority = AuthorizedUser(
        "worker", claim.tenant_id, "worker", claim.user_id, frozenset({"source:delete"})
    )
    try:
        connection_context = pool.connection() if pool is not None else psycopg.connect(dsn)
        with connection_context as connection:
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
        _mark_failed(dsn, authority, claim, worker_id, pool=pool)
        raise


def cleanup_removal_claim(
    dsn: str, claim: ClaimedWork, worker_id: str, *, pool: ConnectionPool | None = None
) -> None:
    """Remove projection copies for one already-excluded typed target.

    This path deliberately does not call a model and never deletes source, evidence,
    passage, or original-byte rows. The operation row remains the removal tombstone.
    """
    key = (claim.tenant_id, claim.user_id)
    authority = AuthorizedUser(
        "worker", claim.tenant_id, "worker", claim.user_id, frozenset({"source:delete"})
    )
    connection_context = pool.connection() if pool is not None else psycopg.connect(dsn)
    try:
        with connection_context as connection:
            bind_user(connection.cursor(), authority)
            connection.execute(
                "SELECT 1 FROM users WHERE tenant_id=%s AND user_id=%s FOR UPDATE", key
            )
            row = connection.execute(
                "SELECT o.removal_target_kind,o.removal_target_id,o.stage_state,o.status,"
                "w.owner_id,w.fence,w.status,w.lease_expires_at>clock_timestamp() "
                "FROM work_units w JOIN operations o USING(tenant_id,user_id,operation_id) "
                "WHERE w.tenant_id=%s AND w.user_id=%s AND w.work_unit_id=%s "
                "AND w.stage IN ('cleanup_memory','cleanup_insight') "
                "FOR UPDATE OF o,w",
                (*key, claim.work_unit_id),
            ).fetchone()
            if (
                row is None
                or row[3] not in {"accepted", "running"}
                or row[4:]
                != (
                    worker_id,
                    claim.fence,
                    "running",
                    True,
                )
            ):
                raise RuntimeError("removal cleanup claim is no longer eligible")
            target_kind, target_id = str(row[0]), str(row[1])
            state = dict(row[2] or {})
            affected = [str(value) for value in state.get("affected_insight_ids", [])]
            memory_target = target_id if target_kind == "memory" else ""
            insight_target = target_id if target_kind == "insight" else ""
            revision_limits = (
                {
                    identifier: int(state["repair_inputs"]["next_revision"][identifier])
                    for identifier in affected
                }
                if target_kind == "memory"
                else {}
            )
            limits = Jsonb(revision_limits)
            old_insights = connection.execute(
                "SELECT insight_id,revision,input_digest,synthesis_text FROM insights "
                "WHERE tenant_id=%s AND user_id=%s AND (insight_id=%s OR "
                "revision <= (%s::jsonb->>insight_id)::integer)",
                (*key, insight_target, limits),
            ).fetchall()
            _remove_projection_checkpoints(
                connection, key, memory_target, insight_target, revision_limits, old_insights
            )
            unit_ids = [
                item[0]
                for item in connection.execute(
                    "SELECT unit_id FROM search_units WHERE tenant_id=%s AND user_id=%s "
                    "AND ((kind='fact' AND memory_id=%s) OR "
                    "(kind='insight' AND (insight_id=%s OR "
                    "insight_revision <= (%s::jsonb->>insight_id)::integer)))",
                    (*key, memory_target, insight_target, limits),
                ).fetchall()
            ]
            # Admission closes Profiles conservatively. Scrub only those closed copies;
            # a supported explicit refresh published afterwards must survive old cleanup.
            connection.execute(
                "UPDATE profiles SET content=NULL,sections='{}'::jsonb,support_ids='[]'::jsonb,"
                "\"references\"='[]'::jsonb WHERE tenant_id=%s AND user_id=%s "
                'AND visible_to IS NOT NULL AND (support_ids ?| %s OR "references" ?| %s)',
                (*key, [memory_target, *unit_ids], [insight_target, *unit_ids]),
            )
            if unit_ids:
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
                        f"DELETE FROM {table} WHERE tenant_id=%s AND user_id=%s "
                        "AND unit_id=ANY(%s)",
                        (*key, unit_ids),
                    )
                connection.execute(
                    "DELETE FROM search_units WHERE tenant_id=%s AND user_id=%s "
                    "AND unit_id=ANY(%s)",
                    (*key, unit_ids),
                )
            if target_kind == "memory":
                if affected:
                    connection.execute(
                        "DELETE FROM insight_support WHERE tenant_id=%s AND user_id=%s "
                        "AND insight_revision <= (%s::jsonb->>insight_id)::integer",
                        (*key, limits),
                    )
                    connection.execute(
                        "DELETE FROM insights WHERE tenant_id=%s AND user_id=%s "
                        "AND revision <= (%s::jsonb->>insight_id)::integer",
                        (*key, limits),
                    )
                connection.execute(
                    "DELETE FROM support WHERE tenant_id=%s AND user_id=%s AND memory_id=%s",
                    (*key, target_id),
                )
                connection.execute(
                    "UPDATE memories SET canonical_claim='',valid_from=NULL,valid_to=NULL "
                    "WHERE tenant_id=%s AND user_id=%s AND memory_id=%s "
                    "AND lifecycle_state='excluded'",
                    (*key, target_id),
                )
            else:
                connection.execute(
                    "DELETE FROM insight_support WHERE tenant_id=%s AND user_id=%s "
                    "AND insight_id=%s",
                    (*key, target_id),
                )
                connection.execute(
                    "DELETE FROM insights WHERE tenant_id=%s AND user_id=%s AND insight_id=%s",
                    (*key, target_id),
                )
            traces = connection.execute(
                "SELECT trace_id,decisions FROM traces WHERE tenant_id=%s AND user_id=%s",
                key,
            ).fetchall()
            redacted = affected_trace_ids(traces, unit_ids)
            if redacted:
                connection.execute(
                    "UPDATE traces SET content=NULL,decisions='{}'::jsonb WHERE tenant_id=%s "
                    "AND user_id=%s AND trace_id=ANY(%s)",
                    (*key, redacted),
                )
            connection.execute("DELETE FROM export_parts WHERE tenant_id=%s AND user_id=%s", key)
            connection.execute(
                "UPDATE exports SET state='expired',artifact_bytes=NULL,manifest='{}'::jsonb "
                "WHERE tenant_id=%s AND user_id=%s",
                key,
            )
            connection.execute(
                "UPDATE users SET projection_epoch=projection_epoch+1 "
                "WHERE tenant_id=%s AND user_id=%s",
                key,
            )
            state["cleanup_status"] = "complete"
            state["cleanup_model_calls"] = 0
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
                "UPDATE operations SET status='succeeded',stage_state=%s,"
                "updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                (Jsonb(state), *key, claim.operation_id),
            )
    except Exception:
        _mark_removal_failed(dsn, authority, claim, worker_id, pool=pool)
        raise


def _remove_projection_checkpoints(
    connection, key, memory_target, insight_target, revision_limits, old_insights
):
    if memory_target:
        # Extraction checkpoints contain original claims, not their eventual memory IDs.
        # Invalidate the extraction families for the supported source revisions only.
        connection.execute(
            "DELETE FROM index_stage_results cached WHERE tenant_id=%s AND user_id=%s "
            "AND (starts_with(stage_key,'extract:') OR starts_with(stage_key,'facts_embed:') "
            "OR starts_with(stage_key,'references:')) AND EXISTS ("
            "SELECT 1 FROM support sp JOIN evidence e USING(tenant_id,user_id,evidence_id) "
            "WHERE sp.tenant_id=cached.tenant_id AND sp.user_id=cached.user_id "
            "AND sp.memory_id=%s AND (e.source_id,e.source_version)="
            "(cached.source_id,cached.source_version))",
            (*key, memory_target),
        )
    digests = [bytes(row[2]) for row in old_insights]
    digests.extend(input_digest(row[3]) for row in old_insights)
    connection.execute(
        "DELETE FROM index_stage_results WHERE tenant_id=%s AND user_id=%s AND ("
        "((stage_key='insight' OR starts_with(stage_key,'insight_embed')) "
        "AND input_digest=ANY(%s)) OR "
        "(%s<>'' AND starts_with(stage_key,'repair:'||%s||':')) OR EXISTS ("
        "SELECT 1 FROM jsonb_each_text(%s::jsonb) bound WHERE "
        "starts_with(stage_key,'repair:'||bound.key||':') AND "
        "CASE WHEN split_part(substr(stage_key,length(bound.key)+9),':',1) ~ '^[0-9]+$' "
        "THEN split_part(substr(stage_key,length(bound.key)+9),':',1)::numeric "
        "<= bound.value::numeric ELSE false END))",
        (*key, digests, insight_target, insight_target, Jsonb(revision_limits)),
    )


def _mark_removal_failed(
    dsn: str,
    authority: AuthorizedUser,
    claim: ClaimedWork,
    worker_id: str,
    *,
    pool: ConnectionPool | None = None,
) -> None:
    key = (claim.tenant_id, claim.user_id)
    connection_context = pool.connection() if pool is not None else psycopg.connect(dsn)
    with connection_context as connection:
        bind_user(connection.cursor(), authority)
        row = connection.execute(
            "UPDATE work_units SET status=CASE WHEN attempts_remaining>0 THEN 'queued' "
            "ELSE 'failed_terminal' END,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND owner_id=%s "
            "AND fence=%s AND status='running' RETURNING status",
            (*key, claim.work_unit_id, worker_id, claim.fence),
        ).fetchone()
        if row is not None:
            connection.execute(
                "UPDATE attempts SET disposition='failed',finished_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s",
                (*key, claim.work_unit_id, claim.attempt_id),
            )
            connection.execute(
                "UPDATE operations SET status=%s,stage_state=stage_state||%s::jsonb,"
                "updated_at=clock_timestamp() WHERE tenant_id=%s AND user_id=%s "
                "AND operation_id=%s",
                (
                    "running" if row[0] == "queued" else "partial_failed",
                    Jsonb({"cleanup_status": "pending" if row[0] == "queued" else "failed"}),
                    *key,
                    claim.operation_id,
                ),
            )


def _mark_failed(
    dsn: str,
    authority: AuthorizedUser,
    claim: ClaimedWork,
    worker_id: str,
    *,
    pool: ConnectionPool | None = None,
) -> None:
    key = (claim.tenant_id, claim.user_id)
    connection_context = pool.connection() if pool is not None else psycopg.connect(dsn)
    with connection_context as connection:
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
