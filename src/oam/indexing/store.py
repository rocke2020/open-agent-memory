"""Fenced publication and compatible stage checkpoints for one accepted source."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedScope
from oam.config import OAMConfig
from oam.db.context import bind_scope
from oam.workers.claims import RENEW_LEASE_SQL, ClaimedWork

MEMORY_REVISION = 1


class StaleIndexWork(RuntimeError):
    """The claim or accepted source no longer permits publication."""


def stable_id(prefix: str, *parts: object) -> str:
    value = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return prefix + "_" + hashlib.sha256(value.encode()).hexdigest()[:32]


@dataclass
class IndexStore:
    dsn: str
    config: OAMConfig
    claim: ClaimedWork
    worker_id: str
    generation_id: str

    def __post_init__(self):
        self.authority = AuthorizedScope(
            "worker",
            self.claim.tenant_id,
            "worker",
            self.claim.scope_id,
            frozenset({"memory:read", "memory:append"}),
        )
        self.key = (self.claim.tenant_id, self.claim.scope_id)
        with self.transaction() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT o.source_id,o.source_version,o.accepted_seq,w.input_digest,"
                    "w.generation_id,s.input_role,sv.original_bytes,sv.media_type,q.policy_epoch "
                    "FROM work_units w JOIN operations o USING(tenant_id,scope_id,operation_id) "
                    "JOIN scopes q USING(tenant_id,scope_id) "
                    "JOIN sources s ON (s.tenant_id,s.scope_id,s.source_id)="
                    "(o.tenant_id,o.scope_id,o.source_id) "
                    "JOIN source_versions sv ON "
                    "(sv.tenant_id,sv.scope_id,sv.source_id,sv.source_version)="
                    "(o.tenant_id,o.scope_id,o.source_id,o.source_version) "
                    "WHERE w.tenant_id=%s AND w.scope_id=%s AND w.work_unit_id=%s",
                    (*self.key, self.claim.work_unit_id),
                )
                self.source = cursor.fetchone()
            if self.source is None or self.source["generation_id"] != self.generation_id:
                raise StaleIndexWork("index generation changed")
            connection.execute(
                "UPDATE scopes SET index_generation_id=%s WHERE tenant_id=%s AND scope_id=%s "
                "AND index_generation_id IS NULL",
                (self.generation_id, *self.key),
            )
            self.guard(connection)
            connection.execute(
                "UPDATE operations SET status='running',updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND scope_id=%s AND operation_id=%s",
                (*self.key, self.claim.operation_id),
            )

    @contextmanager
    def transaction(self):
        with psycopg.connect(self.dsn) as connection:
            bind_scope(connection.cursor(), self.authority)
            yield connection

    def guard(self, connection):
        scope = connection.execute(
            "SELECT state,policy_epoch,index_generation_id FROM scopes "
            "WHERE tenant_id=%s AND scope_id=%s FOR UPDATE",
            self.key,
        ).fetchone()
        row = connection.execute(
            "SELECT w.owner_id,w.fence,w.status,w.lease_expires_at>clock_timestamp(),"
            "o.cancellation_revision,o.status,s.status,s.active_version,sv.status "
            "FROM work_units w JOIN operations o USING(tenant_id,scope_id,operation_id) "
            "JOIN sources s ON (s.tenant_id,s.scope_id,s.source_id)="
            "(o.tenant_id,o.scope_id,o.source_id) "
            "JOIN source_versions sv ON (sv.tenant_id,sv.scope_id,sv.source_id,sv.source_version)="
            "(o.tenant_id,o.scope_id,o.source_id,o.source_version) "
            "WHERE w.tenant_id=%s AND w.scope_id=%s AND w.work_unit_id=%s FOR UPDATE OF w,o",
            (*self.key, self.claim.work_unit_id),
        ).fetchone()
        if (
            scope is None
            or scope != ("active", self.source["policy_epoch"], self.generation_id)
            or row is None
            or row[:5] != (self.worker_id, self.claim.fence, "running", True, 0)
            or row[5] not in {"accepted", "running"}
            or row[6:] != ("active", self.source["source_version"], "active")
        ):
            raise StaleIndexWork("claim, policy, or source is no longer eligible")

    def checkpoint(self, stage: str, inputs: object, produce):
        digest = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).digest()
        identity = (
            *self.key,
            self.source["source_id"],
            self.source["source_version"],
            self.generation_id,
            stage,
            digest,
        )
        with self.transaction() as connection:
            self.guard(connection)
            existing = connection.execute(
                "SELECT output FROM index_stage_results WHERE tenant_id=%s AND scope_id=%s "
                "AND source_id=%s AND source_version=%s AND generation_id=%s "
                "AND stage_key=%s AND input_digest=%s",
                identity,
            ).fetchone()
            if existing:
                return existing[0]
            connection.execute(
                RENEW_LEASE_SQL,
                (
                    self.config.lease.seconds,
                    *self.key,
                    self.claim.work_unit_id,
                    self.worker_id,
                    self.claim.fence,
                ),
            )
            connection.execute(
                "UPDATE attempts SET disposition='unknown_external_outcome',request_ref=%s "
                "WHERE tenant_id=%s AND scope_id=%s AND work_unit_id=%s AND attempt_id=%s",
                (stage, *self.key, self.claim.work_unit_id, self.claim.attempt_id),
            )
        output = produce()
        if hasattr(output, "model_dump"):
            output = output.model_dump(mode="json")
        usage = output.get("usage", {})
        with self.transaction() as connection:
            self.guard(connection)
            connection.execute(
                "INSERT INTO index_stage_results(tenant_id,scope_id,source_id,source_version,"
                "generation_id,stage_key,input_digest,output,usage,work_unit_id,attempt_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    *identity,
                    Jsonb(output),
                    Jsonb(usage),
                    self.claim.work_unit_id,
                    self.claim.attempt_id,
                ),
            )
            connection.execute(
                "UPDATE attempts SET disposition='claimed',response_ref=%s "
                "WHERE tenant_id=%s AND scope_id=%s AND work_unit_id=%s AND attempt_id=%s",
                (stage, *self.key, self.claim.work_unit_id, self.claim.attempt_id),
            )
        return output

    def readiness(self, connection, **stages):
        connection.execute(
            "UPDATE operations SET stage_state=stage_state||%s::jsonb,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND scope_id=%s AND operation_id=%s",
            (Jsonb(stages), *self.key, self.claim.operation_id),
        )

    def resource_metadata(self):
        """Read the accepted upload envelope, not derived document passages."""
        from oam.contracts.resources import EvidenceResourceCreate

        with self.transaction() as connection:
            self.guard(connection)
            rows = connection.execute(
                "SELECT e.content_text FROM evidence e WHERE e.tenant_id=%s AND e.scope_id=%s "
                "AND e.source_id=%s AND e.source_version=%s AND e.status='active' "
                "AND e.content_text IS NOT NULL AND NOT EXISTS(SELECT 1 FROM passages p "
                "WHERE (p.tenant_id,p.scope_id,p.evidence_id)="
                "(e.tenant_id,e.scope_id,e.evidence_id))",
                (*self.key, self.source["source_id"], self.source["source_version"]),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError("resource requires one accepted metadata envelope")
        return EvidenceResourceCreate.model_validate_json(rows[0][0])

    def insert_annotation(
        self, connection, unit_id, annotation, *, origin, ordinal, reference_timezone=None
    ):
        detail = {**annotation.model_dump(mode="json"), "origin": origin}
        bounds = detail.get("bounds") or ("[]" if annotation.instant else "[)")
        if reference_timezone is not None:
            detail.setdefault("reference_timezone", reference_timezone)
        identifier = (
            stable_id("time", unit_id, "caller", ordinal)
            if origin == "caller"
            else stable_id("time", unit_id, ordinal)
        )
        connection.execute(
            "INSERT INTO unit_times(tenant_id,scope_id,annotation_id,unit_id,role,"
            "time_span,detail) "
            "VALUES (%s,%s,%s,%s,%s,tstzrange(%s,%s,%s),%s) ON CONFLICT DO NOTHING",
            (
                *self.key,
                identifier,
                unit_id,
                annotation.role,
                annotation.start,
                annotation.end,
                bounds,
                Jsonb(detail),
            ),
        )

    def finish(self):
        with self.transaction() as connection:
            self.guard(connection)
            self.summarize_usage(connection)
            connection.execute(
                "UPDATE work_units SET status='succeeded',updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND scope_id=%s AND work_unit_id=%s",
                (*self.key, self.claim.work_unit_id),
            )
            connection.execute(
                "UPDATE attempts SET disposition='succeeded',finished_at=clock_timestamp() "
                "WHERE tenant_id=%s AND scope_id=%s AND work_unit_id=%s AND attempt_id=%s",
                (*self.key, self.claim.work_unit_id, self.claim.attempt_id),
            )
            connection.execute(
                "UPDATE operations SET status='succeeded',updated_at=clock_timestamp(), "
                "stage_state=stage_state-'error'-'error_detail' "
                "WHERE tenant_id=%s AND scope_id=%s AND operation_id=%s",
                (*self.key, self.claim.operation_id),
            )
            connection.execute(
                "UPDATE scopes SET published_seq=greatest(published_seq,%s) "
                "WHERE tenant_id=%s AND scope_id=%s",
                (self.source["accepted_seq"], *self.key),
            )

    def fail(self, error: Exception):
        from oam.indexing.extraction import ExtractionError
        from oam.models.runtime import ModelOutputError

        diagnostic = {"error": type(error).__name__}
        if isinstance(error, (ExtractionError, ModelOutputError)):
            diagnostic["error_detail"] = str(error)
        known_usage = (
            error.usage if isinstance(error, (ExtractionError, ModelOutputError)) else None
        )
        retryable = (
            isinstance(error, ExtractionError) and known_usage is not None and error.retryable
        )
        with self.transaction() as connection:
            owned = connection.execute(
                "UPDATE work_units SET status=CASE WHEN %s AND attempts_remaining>0 "
                "THEN 'queued' ELSE 'failed_terminal' END,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND scope_id=%s AND work_unit_id=%s AND owner_id=%s "
                "AND fence=%s AND status='running' AND lease_expires_at>clock_timestamp() "
                "RETURNING status",
                (retryable, *self.key, self.claim.work_unit_id, self.worker_id, self.claim.fence),
            ).fetchone()
            if owned:
                if known_usage is not None:
                    connection.execute(
                        "UPDATE attempts SET disposition='failed',usage=%s,"
                        "response_ref=request_ref,finished_at=clock_timestamp() "
                        "WHERE tenant_id=%s AND scope_id=%s AND work_unit_id=%s AND attempt_id=%s",
                        (
                            Jsonb(known_usage.model_dump(mode="json")),
                            *self.key,
                            self.claim.work_unit_id,
                            self.claim.attempt_id,
                        ),
                    )
                retrying = owned[0] == "queued"
                if retrying:
                    diagnostic["last_retry_error"] = dict(diagnostic)
                self.summarize_usage(connection)
                connection.execute(
                    "UPDATE operations SET status=%s,"
                    "stage_state=stage_state||%s::jsonb "
                    "WHERE tenant_id=%s AND scope_id=%s AND operation_id=%s AND status='running'",
                    (
                        "running" if retrying else "partial_failed",
                        Jsonb(diagnostic),
                        *self.key,
                        self.claim.operation_id,
                    ),
                )

    def summarize_usage(self, connection):
        rows = connection.execute(
            "SELECT stage_key,usage FROM index_stage_results WHERE tenant_id=%s AND scope_id=%s "
            "AND source_id=%s AND source_version=%s AND generation_id=%s",
            (
                *self.key,
                self.source["source_id"],
                self.source["source_version"],
                self.generation_id,
            ),
        ).fetchall()
        attempts = connection.execute(
            "SELECT a.disposition,a.request_ref,a.usage FROM attempts a "
            "JOIN work_units w USING(tenant_id,scope_id,work_unit_id) "
            "JOIN operations o USING(tenant_id,scope_id,operation_id) "
            "WHERE o.tenant_id=%s AND o.scope_id=%s AND o.source_id=%s AND o.source_version=%s "
            "AND w.generation_id=%s",
            (
                *self.key,
                self.source["source_id"],
                self.source["source_version"],
                self.generation_id,
            ),
        ).fetchall()
        uncertain = any(disposition == "unknown_external_outcome" for disposition, _, _ in attempts)
        generation = []
        embedding = []
        rows.extend(
            (stage, usage)
            for disposition, stage, usage in attempts
            if disposition == "failed" and stage is not None and usage is not None
        )
        for stage, usage in rows:
            if stage.startswith("extract:") or stage in {"insight", "abstract"}:
                generation.append(usage)
            elif stage.startswith(("passage:", "facts_embed:", "insight_embed")):
                embedding.append(usage)

        def total(values):
            result = {
                name: sum(value[name] for value in values)
                if all(value.get(name) is not None for value in values)
                else None
                for name in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens")
            }
            result["coverage"] = (
                "complete"
                if not uncertain and all(value.get("coverage") == "complete" for value in values)
                else "partial"
            )
            return result

        self.readiness(
            connection,
            usage=total(generation),
            embedding_usage=total(embedding),
            index_attempts=len(attempts),
        )

    def insert_unit(
        self,
        connection,
        *,
        unit_id,
        kind,
        text,
        vector,
        locator,
        evidence_id=None,
        passage_id=None,
        memory_id=None,
        insight_id=None,
        mentioned_at=None,
        source_created_at=None,
        source_updated_at=None,
        ordinal=0,
    ):
        from oam.retrieval.repository import lexicalize

        connection.execute(
            "INSERT INTO search_units(tenant_id,scope_id,unit_id,kind,origin,source_id,"
            "source_version,evidence_id,passage_id,memory_id,insight_id,insight_revision,"
            "generation_id,content_text,locator,embedding,lexical_text,mentioned_at,"
            "source_created_at,source_updated_at,ordinal) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,%s,%s) "
            "ON CONFLICT DO NOTHING",
            (
                *self.key,
                unit_id,
                kind,
                "evidence" if kind == "resource_passage" else "memory",
                None if insight_id else self.source["source_id"],
                None if insight_id else self.source["source_version"],
                evidence_id,
                passage_id,
                memory_id,
                insight_id,
                1 if insight_id else None,
                self.generation_id,
                text,
                Jsonb(locator),
                json.dumps(vector),
                lexicalize(text),
                mentioned_at,
                source_created_at,
                source_updated_at,
                ordinal,
            ),
        )
