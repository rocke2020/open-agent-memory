"""Fenced publication and compatible stage checkpoints for one accepted source."""

from __future__ import annotations

import hashlib
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from threading import BoundedSemaphore, Lock
from typing import TYPE_CHECKING

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from oam.auth.credentials import AuthorizedUser
from oam.capacity import CAPACITY_POLL_SECONDS, CapacityUnavailable
from oam.config import OAMConfig
from oam.db.context import bind_user
from oam.workers.claims import RENEW_LEASE_SQL, ClaimedWork

if TYPE_CHECKING:
    from oam.indexing.extraction import InsightResult

MEMORY_REVISION = 1
_INDEXING_CONNECTION_SLOTS: dict[tuple[str, int], BoundedSemaphore] = {}
_INDEXING_CONNECTION_SLOTS_LOCK = Lock()


class StaleIndexWork(RuntimeError):
    """The claim or accepted source no longer permits publication."""


class StaleConsolidationInputs(StaleIndexWork):
    """Consolidation must reload changed inputs before retrying publication."""


class StaleConsolidationPolicy(StaleConsolidationInputs):
    """Consolidation must restart from the current eligible support policy."""


def stable_id(prefix: str, *parts: object) -> str:
    value = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return prefix + "_" + hashlib.sha256(value.encode()).hexdigest()[:32]


def log_insight_indexed(
    store: IndexStore, result: InsightResult, insight_id: str, revision: int, started: float
) -> None:
    print(
        json.dumps(
            {
                "stage": "insight_indexed",
                "operation_id": store.claim.operation_id,
                "generation_id": store.generation_id,
                "insight_id": insight_id,
                "revision": revision,
                "embedding_input": "purpose" if result.retrieval_text is not None else "full_text",
                "support_count": len(result.support_ids),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
            }
        ),
        flush=True,
    )


def input_digest(inputs: object) -> bytes:
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).digest()


@dataclass
class IndexStore:
    dsn: str
    config: OAMConfig
    claim: ClaimedWork
    worker_id: str
    generation_id: str
    pool: ConnectionPool | None = None
    _unsettled_model_stages: set[str] = field(default_factory=set, init=False, repr=False)
    _model_stage_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _inline_semantic: bool = field(default=False, init=False, repr=False)
    _semantic_handoff: bool = field(default=False, init=False, repr=False)

    def __post_init__(self):
        self.authority = AuthorizedUser(
            "worker",
            self.claim.tenant_id,
            "worker",
            self.claim.user_id,
            frozenset({"memory:read", "memory:append"}),
        )
        self.key = (self.claim.tenant_id, self.claim.user_id)
        with self.transaction() as connection:
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT o.source_id,o.source_version,o.accepted_seq,w.input_digest,"
                    "w.generation_id,s.input_role,sv.original_bytes,sv.media_type,q.policy_epoch "
                    "FROM work_units w JOIN operations o USING(tenant_id,user_id,operation_id) "
                    "JOIN users q USING(tenant_id,user_id) "
                    "JOIN sources s ON (s.tenant_id,s.user_id,s.source_id)="
                    "(o.tenant_id,o.user_id,o.source_id) "
                    "JOIN source_versions sv ON "
                    "(sv.tenant_id,sv.user_id,sv.source_id,sv.source_version)="
                    "(o.tenant_id,o.user_id,o.source_id,o.source_version) "
                    "WHERE w.tenant_id=%s AND w.user_id=%s AND w.work_unit_id=%s",
                    (*self.key, self.claim.work_unit_id),
                )
                self.source = cursor.fetchone()
            if self.source is None:
                raise StaleIndexWork("index work is unavailable")
            self.generation_id = self.source["generation_id"]
            connection.execute(
                "UPDATE users SET index_generation_id=%s WHERE tenant_id=%s AND user_id=%s "
                "AND index_generation_id IS NULL",
                (self.generation_id, *self.key),
            )
            self.guard(connection)
            connection.execute(
                "UPDATE operations SET status='running',updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                (*self.key, self.claim.operation_id),
            )

    def resume_source(self):
        """Read previously published interaction passages and their active facts."""
        with self.transaction() as connection:
            self.guard(connection)
            rows = connection.execute(
                "SELECT p.passage_id,p.locator,p.normalized_text,m.memory_id "
                "FROM passages p JOIN evidence e USING(tenant_id,user_id,evidence_id) "
                "LEFT JOIN support sp USING(tenant_id,user_id,passage_id) "
                "LEFT JOIN memories m ON (m.tenant_id,m.user_id,m.memory_id)="
                "(sp.tenant_id,sp.user_id,sp.memory_id) "
                "AND m.generation_id=p.generation_id "
                "AND m.lifecycle_state='active' AND m.visible_to IS NULL "
                "WHERE p.tenant_id=%s AND p.user_id=%s AND e.source_id=%s "
                "AND e.source_version=%s AND p.generation_id=%s AND p.visible_to IS NULL "
                "ORDER BY p.passage_id,m.memory_id",
                (
                    *self.key,
                    self.source["source_id"],
                    self.source["source_version"],
                    self.generation_id,
                ),
            ).fetchall()
            diagnostic_rows = connection.execute(
                "SELECT stage_key,output FROM index_stage_results "
                "WHERE tenant_id=%s AND user_id=%s AND source_id=%s AND source_version=%s "
                "AND generation_id=%s AND starts_with(stage_key,'extract:') "
                "ORDER BY stage_key",
                (
                    *self.key,
                    self.source["source_id"],
                    self.source["source_version"],
                    self.generation_id,
                ),
            ).fetchall()
            removed_rows = connection.execute(
                "SELECT DISTINCT passage_id FROM operations o "
                "CROSS JOIN LATERAL jsonb_array_elements_text("
                "COALESCE(o.stage_state->'removed_passage_ids','[]'::jsonb)) AS passage_id "
                "WHERE o.tenant_id=%s AND o.user_id=%s AND o.kind='remove_memory' "
                "UNION SELECT sp.passage_id FROM operations o JOIN support sp ON "
                "(sp.tenant_id,sp.user_id,sp.memory_id)="
                "(o.tenant_id,o.user_id,o.removal_target_id) "
                "WHERE o.tenant_id=%s AND o.user_id=%s AND o.kind='remove_memory'",
                (*self.key, *self.key),
            ).fetchall()
            state_row = connection.execute(
                "SELECT COALESCE(o.stage_state->'extraction_diagnostics', "
                "parent.stage_state->'extraction_diagnostics', '[]'::jsonb) "
                "FROM operations o LEFT JOIN operations parent ON "
                "(parent.tenant_id,parent.user_id,parent.operation_id)="
                "(o.tenant_id,o.user_id,o.retry_of_operation_id) "
                "WHERE o.tenant_id=%s AND o.user_id=%s AND o.operation_id=%s",
                (*self.key, self.claim.operation_id),
            ).fetchone()
        passages = {}
        facts = {}
        for passage_id, locator, content, memory_id in rows:
            message_index = locator.get("message_index")
            passage_index = locator.get("passage_index")
            if type(message_index) is not int or type(passage_index) is not int:
                raise StaleIndexWork("published passage has no source position")
            passages.setdefault(message_index, {})[passage_index] = (content, passage_id)
            if memory_id:
                facts.setdefault(passage_id, set()).add(memory_id)
        saved_ids = {memory_id for values in facts.values() for memory_id in values}
        saved_passages = {passage_id for passage_id, _, _, _ in rows}
        for (passage_id,) in removed_rows:
            if passage_id in saved_passages:
                facts.setdefault(passage_id, set())
        diagnostics = list(state_row[0]) if state_row else []
        for stage, output in diagnostic_rows:
            group_id = stage.removeprefix("extract:").split(":recipe=", 1)[0]
            output_ids = {
                stable_id(
                    "memory", fact.get("passage_id") or group_id, fact["output_index"], fact["text"]
                )
                for fact in output.get("facts", [])
            }
            members = output.get("_passage_ids", [])
            completed = bool(members) and set(members) <= saved_passages and output_ids <= saved_ids
            if completed:
                for passage_id in members:
                    facts.setdefault(passage_id, set())
            if completed or output_ids & saved_ids:
                for item in output.get("diagnostics", []):
                    diagnostic = {"passage_id": group_id, **item}
                    if diagnostic not in diagnostics:
                        diagnostics.append(diagnostic)
        return (
            {
                message: [(index, *entries[index]) for index in sorted(entries)]
                for message, entries in passages.items()
            },
            facts,
            diagnostics,
        )

    def resource_passages(self):
        """Read published Resource blocks before continuing a partial source."""
        with self.transaction() as connection:
            self.guard(connection)
            rows = connection.execute(
                "SELECT p.locator->>'block',p.normalized_text FROM passages p "
                "JOIN evidence e USING(tenant_id,user_id,evidence_id) "
                "WHERE p.tenant_id=%s AND p.user_id=%s AND e.source_id=%s "
                "AND e.source_version=%s AND p.generation_id=%s AND p.visible_to IS NULL",
                (
                    *self.key,
                    self.source["source_id"],
                    self.source["source_version"],
                    self.generation_id,
                ),
            ).fetchall()
        if any(not locator for locator, _ in rows):
            raise StaleIndexWork("published Resource passage has no locator")
        return dict(rows)

    @contextmanager
    def transaction(self):
        timeout = self.config.model.attempt_timeout_seconds
        deadline = time.monotonic() + timeout
        limit = self.config.database.indexing_concurrency
        with _INDEXING_CONNECTION_SLOTS_LOCK:
            slots = _INDEXING_CONNECTION_SLOTS.setdefault(
                (self.dsn, limit), BoundedSemaphore(limit)
            )
        if not slots.acquire(timeout=timeout):
            raise CapacityUnavailable("indexing database connection wait expired")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CapacityUnavailable("indexing database admission wait expired")
            if self.pool is None:
                connection_context = psycopg.connect(
                    self.dsn, connect_timeout=max(1, math.ceil(remaining))
                )
            else:
                connection_context = self.pool.connection(
                    timeout=min(remaining, self.config.database.pool_acquire_timeout_seconds)
                )
            with connection_context as connection:
                original_timeout = connection.execute("SHOW statement_timeout").fetchone()[0]
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CapacityUnavailable("indexing database admission wait expired")
                    connection.execute(
                        "SELECT set_config('statement_timeout',%s,true)",
                        (str(max(1, int(remaining * 1000))),),
                    )
                    try:
                        slot = connection.execute(
                            "SELECT oam_indexing_db_admit(%s)", (limit,)
                        ).fetchone()[0]
                    except psycopg.errors.QueryCanceled as error:
                        raise CapacityUnavailable(
                            "indexing database admission wait expired"
                        ) from error
                    if time.monotonic() >= deadline:
                        raise CapacityUnavailable("indexing database admission wait expired")
                    if slot is not None:
                        break
                    time.sleep(min(CAPACITY_POLL_SECONDS, max(0, deadline - time.monotonic())))
                connection.execute(
                    "SELECT set_config('statement_timeout',%s,true)", (original_timeout,)
                )
                bind_user(connection.cursor(), self.authority)
                yield connection
                connection.execute(
                    "UPDATE users SET projection_epoch=projection_epoch+1 "
                    "WHERE tenant_id=%s AND user_id=%s",
                    self.key,
                )
        finally:
            slots.release()

    @contextmanager
    def read_only_connection(self):
        """Checkout a content connection for a read-only semantic-link batch."""
        timeout = self.config.database.pool_acquire_timeout_seconds
        if self.pool is None:
            with psycopg.connect(
                self.dsn, connect_timeout=max(1, math.ceil(timeout))
            ) as connection:
                yield connection
        else:
            with self.pool.connection(timeout=timeout) as connection:
                yield connection

    def guard(self, connection, *, allow_policy_change=False):
        if self.claim.stage == "index_semantic_links":
            connection.execute("SET LOCAL jit = off")
        user = connection.execute(
            "SELECT state,policy_epoch,index_generation_id FROM users "
            "WHERE tenant_id=%s AND user_id=%s FOR UPDATE",
            self.key,
        ).fetchone()
        row = connection.execute(
            "SELECT w.owner_id,w.fence,w.status,w.lease_expires_at>clock_timestamp(),"
            "o.cancellation_revision,o.status,s.status,s.active_version,sv.status,"
            "w.generation_id,o.source_id,o.source_version,w.operation_id,w.input_digest "
            "FROM work_units w JOIN operations o USING(tenant_id,user_id,operation_id) "
            "JOIN sources s ON (s.tenant_id,s.user_id,s.source_id)="
            "(o.tenant_id,o.user_id,o.source_id) "
            "JOIN source_versions sv ON (sv.tenant_id,sv.user_id,sv.source_id,sv.source_version)="
            "(o.tenant_id,o.user_id,o.source_id,o.source_version) "
            "WHERE w.tenant_id=%s AND w.user_id=%s AND w.work_unit_id=%s FOR UPDATE OF w,o",
            (*self.key, self.claim.work_unit_id),
        ).fetchone()
        if (
            user is None
            or user[0] != "active"
            or row is None
            or row[:5] != (self.worker_id, self.claim.fence, "running", True, 0)
            or row[5] not in {"accepted", "running"}
            or row[6:9] != ("active", self.source["source_version"], "active")
            or row[9:13]
            != (
                self.generation_id,
                self.source["source_id"],
                self.source["source_version"],
                self.claim.operation_id,
            )
            or bytes(row[13]) != bytes(self.source["input_digest"])
        ):
            raise StaleIndexWork("claim, policy, or source is no longer eligible")
        if (
            not allow_policy_change
            and self.claim.stage == "consolidate"
            and user[1] != self.source["policy_epoch"]
        ):
            raise StaleConsolidationPolicy("consolidation policy changed")

    @property
    def model_lease_seconds(self):
        return max(
            self.config.lease.seconds,
            self.config.model.attempt_timeout_seconds + self.config.lease.heartbeat_seconds,
        )

    def check_retry_claim(self):
        """Recheck cancellation and renew the fenced lease before a split child call."""
        with self.transaction() as connection:
            self.guard(connection)
            connection.execute(
                RENEW_LEASE_SQL,
                (
                    self.model_lease_seconds,
                    *self.key,
                    self.claim.work_unit_id,
                    self.worker_id,
                    self.claim.fence,
                ),
            )

    def mark_inline_semantic(self) -> None:
        """Mark the current source claim as being in its durable semantic phase."""
        self._inline_semantic = True

    @contextmanager
    def model_pipeline(self):
        """Keep one attempt unresolved until all overlapping model stages drain."""
        self._pipeline_owns_attempt = True
        try:
            yield
            with self.transaction() as connection:
                self.guard(connection)
                connection.execute(
                    "UPDATE attempts SET disposition='claimed',request_ref=NULL,response_ref=NULL "
                    "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s",
                    (*self.key, self.claim.work_unit_id, self.claim.attempt_id),
                )
        finally:
            self._pipeline_owns_attempt = False

    def checkpoint(self, stage: str, inputs: object, produce):
        digest = input_digest(inputs)
        identity = (
            *self.key,
            self.source["source_id"],
            self.source["source_version"],
            self.generation_id,
            stage,
            digest,
        )
        with self._model_stage_lock:
            with self.transaction() as connection:
                self.guard(connection)
                existing = connection.execute(
                    "SELECT output FROM index_stage_results WHERE tenant_id=%s AND user_id=%s "
                    "AND source_id=%s AND source_version=%s AND generation_id=%s "
                    "AND (stage_key=%s OR starts_with(stage_key,%s||':recipe=')) "
                    "AND input_digest=%s ORDER BY (stage_key=%s) DESC,stage_key LIMIT 1",
                    (*identity[:6], identity[5], identity[6], identity[5]),
                ).fetchone()
                if existing:
                    return existing[0]
                connection.execute(
                    RENEW_LEASE_SQL,
                    (
                        self.model_lease_seconds,
                        *self.key,
                        self.claim.work_unit_id,
                        self.worker_id,
                        self.claim.fence,
                    ),
                )
                self._record_model_stage_state(connection, stage, dispatched=(stage,))
            self._unsettled_model_stages.add(stage)
        output = produce()
        if hasattr(output, "model_dump"):
            output = output.model_dump(mode="json")
        usage = output.get("usage", {})
        policy_error = None
        with self._model_stage_lock:
            with self.transaction() as connection:
                try:
                    self.guard(connection)
                except StaleConsolidationPolicy as error:
                    # Persist a known completed response before restarting under new policy.
                    self.guard(connection, allow_policy_change=True)
                    policy_error = error
                connection.execute(
                    "INSERT INTO index_stage_results(tenant_id,user_id,source_id,source_version,"
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
                self._record_model_stage_state(connection, stage, completed=(stage,))
            self._unsettled_model_stages.remove(stage)
        if policy_error is not None:
            raise policy_error
        return output

    def checkpoints(self, stages):
        """Run independent model stages together and persist each completed result."""
        if len(stages) == 1:
            stage, inputs, produce = stages[0]
            return [self.checkpoint(stage, inputs, produce)]
        if not stages:
            return []
        outputs = [None] * len(stages)
        pending = []
        with self._model_stage_lock:
            with self.transaction() as connection:
                self.guard(connection)
                for index, (stage, inputs, produce) in enumerate(stages):
                    digest = input_digest(inputs)
                    identity = (
                        *self.key,
                        self.source["source_id"],
                        self.source["source_version"],
                        self.generation_id,
                        stage,
                        digest,
                    )
                    existing = connection.execute(
                        "SELECT output FROM index_stage_results WHERE tenant_id=%s AND user_id=%s "
                        "AND source_id=%s AND source_version=%s AND generation_id=%s "
                        "AND (stage_key=%s OR starts_with(stage_key,%s||':recipe=')) "
                        "AND input_digest=%s ORDER BY (stage_key=%s) DESC,stage_key LIMIT 1",
                        (*identity[:6], identity[5], identity[6], identity[5]),
                    ).fetchone()
                    if existing:
                        outputs[index] = existing[0]
                    else:
                        pending.append((index, stage, identity, produce))
                if pending:
                    connection.execute(
                        RENEW_LEASE_SQL,
                        (
                            self.model_lease_seconds,
                            *self.key,
                            self.claim.work_unit_id,
                            self.worker_id,
                            self.claim.fence,
                        ),
                    )
                    self._record_model_stage_state(
                        connection,
                        pending[0][1],
                        dispatched=tuple(stage for _, stage, _, _ in pending),
                    )
            self._unsettled_model_stages.update(stage for _, stage, _, _ in pending)
        if not pending:
            return outputs
        failures = []
        with ThreadPoolExecutor(max_workers=len(pending)) as executor:
            futures = {
                executor.submit(produce): (index, stage, identity)
                for index, stage, identity, produce in pending
            }
            for future in as_completed(futures):
                index, stage, identity = futures[future]
                try:
                    output = future.result()
                except Exception as error:
                    failures.append((stage, error))
                    outputs[index] = error
                    continue
                if hasattr(output, "model_dump"):
                    output = output.model_dump(mode="json")
                with self._model_stage_lock:
                    with self.transaction() as connection:
                        self.guard(connection)
                        connection.execute(
                            "INSERT INTO index_stage_results(tenant_id,user_id,source_id,"
                            "source_version,generation_id,stage_key,input_digest,output,usage,"
                            "work_unit_id,attempt_id) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                            (
                                *identity,
                                Jsonb(output),
                                Jsonb(output.get("usage", {})),
                                self.claim.work_unit_id,
                                self.claim.attempt_id,
                            ),
                        )
                        self._record_model_stage_state(connection, stage, completed=(stage,))
                    self._unsettled_model_stages.remove(stage)
                outputs[index] = output
        with self._model_stage_lock, self.transaction() as connection:
            self.guard(connection)
            self._record_model_stage_state(
                connection, failures[0][0] if failures else pending[-1][1]
            )
        return outputs

    def _record_model_stage_state(self, connection, stage_ref, *, dispatched=(), completed=()):
        # Local stage state changes after guarded commits under the stage lock.
        # Remote calls run outside both locks; failed calls remain unsettled.
        pending = (self._unsettled_model_stages | set(dispatched)) - set(completed)
        unresolved = bool(pending) or getattr(self, "_pipeline_owns_attempt", False)
        connection.execute(
            "UPDATE attempts SET disposition=%s,request_ref=%s,response_ref=%s "
            "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s",
            (
                "unknown_external_outcome" if unresolved else "claimed",
                min(pending) if pending else stage_ref,
                None if unresolved else stage_ref,
                *self.key,
                self.claim.work_unit_id,
                self.claim.attempt_id,
            ),
        )

    def l2_progress(self, connection) -> tuple[set[str], str]:
        row = connection.execute(
            "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id=%s "
            "AND operation_id=%s",
            (*self.key, self.claim.operation_id),
        ).fetchone()
        state = row[0] if row and row[0] else {}
        state = state if isinstance(state, dict) else {}
        values = state.get("l2_examined_ids", [])
        return (
            {value for value in values if isinstance(value, str)},
            state.get("last_insight_outcome", "no_insight"),
        )

    def mark_l2_group(self, connection, unit_ids, outcome: str) -> None:
        row = connection.execute(
            "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id=%s "
            "AND operation_id=%s FOR UPDATE",
            (*self.key, self.claim.operation_id),
        ).fetchone()
        state = dict(row[0] or {}) if row else {}
        existing = state.get("l2_examined_ids", [])
        state["l2_examined_ids"] = sorted(
            {value for value in existing if isinstance(value, str)} | set(unit_ids)
        )
        state["last_insight_outcome"] = (
            "insight" if state.get("last_insight_outcome") == "insight" else outcome
        )
        connection.execute(
            "UPDATE operations SET stage_state=%s,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
            (Jsonb(state), *self.key, self.claim.operation_id),
        )

    def readiness(self, connection, **stages):
        connection.execute(
            "UPDATE operations SET stage_state=stage_state||%s::jsonb,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
            (Jsonb(stages), *self.key, self.claim.operation_id),
        )

    def mark_deferred_failure_stage(self, stage: str) -> None:
        """Restore an unresolved model call after other passages are published."""
        with self.transaction() as connection:
            self.guard(connection)
            connection.execute(
                "UPDATE attempts SET disposition='unknown_external_outcome',request_ref=%s,"
                "response_ref=NULL,usage=NULL WHERE tenant_id=%s AND user_id=%s "
                "AND work_unit_id=%s AND attempt_id=%s",
                (stage, *self.key, self.claim.work_unit_id, self.claim.attempt_id),
            )

    def resource_metadata(self):
        """Read the accepted upload envelope, not derived document passages."""
        from oam.contracts.resources import EvidenceResourceCreate

        with self.transaction() as connection:
            self.guard(connection)
            rows = connection.execute(
                "SELECT e.content_text FROM evidence e WHERE e.tenant_id=%s AND e.user_id=%s "
                "AND e.source_id=%s AND e.source_version=%s AND e.status='active' "
                "AND e.content_text IS NOT NULL AND NOT EXISTS(SELECT 1 FROM passages p "
                "WHERE (p.tenant_id,p.user_id,p.evidence_id)="
                "(e.tenant_id,e.user_id,e.evidence_id))",
                (*self.key, self.source["source_id"], self.source["source_version"]),
            ).fetchall()
        if len(rows) != 1:
            raise ValueError("resource requires one accepted metadata envelope")
        return EvidenceResourceCreate.model_validate_json(rows[0][0])

    def insert_annotation(
        self, connection, unit_id, annotation, *, origin, ordinal, reference_timezone=None
    ):
        detail = {**annotation.model_dump(mode="json"), "origin": origin}
        bounds = "[]" if annotation.start == annotation.end else "[)"
        if reference_timezone is not None:
            detail.setdefault("reference_timezone", reference_timezone)
        identifier = (
            stable_id("time", unit_id, "caller", ordinal)
            if origin == "caller"
            else stable_id("time", unit_id, ordinal)
        )
        connection.execute(
            "INSERT INTO unit_times(tenant_id,user_id,annotation_id,unit_id,role,"
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

    def finish(
        self,
        *,
        schedule_consolidation: bool = False,
        inline_semantic: bool = False,
        enqueue_semantic: bool = False,
        connection=None,
    ):
        self._semantic_handoff = enqueue_semantic
        with self.transaction() if connection is None else nullcontext(connection) as connection:
            semantic_completion = self.claim.stage == "index_semantic_links" or inline_semantic
            if semantic_completion:
                timeout = min(self.config.lease.seconds, self.config.model.attempt_timeout_seconds)
                connection.execute(
                    "SELECT set_config('statement_timeout',%s,true)", (str(int(timeout * 1000)),)
                )
            self.guard(connection)
            if enqueue_semantic:
                manifest = self.semantic_seed_manifest(connection)
                work_id = stable_id("work_semantic_links", self.claim.operation_id)
                digest = input_digest(manifest)
                existing = connection.execute(
                    "SELECT input_digest,generation_id,dependency_ids FROM work_units "
                    "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s",
                    (*self.key, work_id),
                ).fetchone()
                if existing and (
                    bytes(existing[0]) != digest
                    or existing[1] != self.generation_id
                    or existing[2] != [self.claim.work_unit_id]
                ):
                    raise StaleIndexWork("queued semantic work identity is incompatible")
                connection.execute(
                    "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
                    "input_digest,generation_id,dependency_ids,status,attempts_remaining) "
                    "VALUES (%s,%s,%s,%s,'index_semantic_links',%s,%s,%s,'queued',%s) "
                    "ON CONFLICT DO NOTHING",
                    (
                        *self.key,
                        work_id,
                        self.claim.operation_id,
                        digest,
                        self.generation_id,
                        [self.claim.work_unit_id],
                        self.config.retry.max_attempts,
                    ),
                )
            if self.claim.stage != "index_semantic_links" or inline_semantic:
                self.summarize_usage(connection)
            if semantic_completion:
                state = connection.execute(
                    "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id=%s "
                    "AND operation_id=%s",
                    (*self.key, self.claim.operation_id),
                ).fetchone()[0]
                schedule_consolidation = schedule_consolidation and not state.get(
                    "consolidation_operation_id"
                )
                manifest = self.semantic_seed_manifest(connection)
                count = connection.execute(
                    "SELECT count(*) FROM unit_links WHERE tenant_id=%s AND user_id=%s "
                    "AND kind='semantic' AND source_id=ANY(%s)",
                    (*self.key, manifest["seed_ids"]),
                ).fetchone()[0]
                self.readiness(
                    connection,
                    links_ready=True,
                    semantic_links_status="succeeded",
                    link_coverage={
                        **state.get("link_coverage", {}),
                        "semantic_links": count,
                        "coverage": "complete",
                    },
                )
            if schedule_consolidation:
                consolidation_id = stable_id(
                    "op_consolidate", self.claim.operation_id, self.generation_id
                )
                work_id = stable_id("work_consolidate", consolidation_id)
                connection.execute(
                    "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,"
                    "source_id,source_version,accepted_seq,kind,schema_version,status,"
                    "plan_sealed,stage_state) SELECT tenant_id,user_id,%s,principal_id,"
                    "source_id,source_version,accepted_seq,'consolidate',schema_version,"
                    "'accepted',true,%s::jsonb FROM operations WHERE tenant_id=%s "
                    "AND user_id=%s AND operation_id=%s ON CONFLICT DO NOTHING",
                    (
                        consolidation_id,
                        Jsonb({"source_operation_id": self.claim.operation_id}),
                        *self.key,
                        self.claim.operation_id,
                    ),
                )
                l2_priority_at = connection.execute(
                    "SELECT COALESCE(min(created_at),clock_timestamp()) "
                    "FROM search_units WHERE tenant_id=%s AND user_id=%s "
                    "AND kind='fact' AND source_id=%s AND source_version=%s "
                    "AND generation_id=%s",
                    (
                        *self.key,
                        self.source["source_id"],
                        self.source["source_version"],
                        self.generation_id,
                    ),
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,"
                    "stage,input_digest,generation_id,status,available_at,attempts_remaining,"
                    "l2_priority_at) "
                    "VALUES (%s,%s,%s,%s,'consolidate',%s,%s,'queued',"
                    "clock_timestamp()+(%s*interval '1 hour'),%s,%s) ON CONFLICT DO NOTHING",
                    (
                        *self.key,
                        work_id,
                        consolidation_id,
                        self.source["input_digest"],
                        self.generation_id,
                        self.config.index.insight_consolidation_delay_hours,
                        self.config.retry.max_attempts,
                        l2_priority_at,
                    ),
                )
                self.readiness(
                    connection,
                    insights_ready=False,
                    consolidation_operation_id=consolidation_id,
                    consolidation_status="pending",
                )
            if semantic_completion:
                self.guard(connection)
            connection.execute(
                "UPDATE work_units SET status='succeeded',updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s",
                (*self.key, self.claim.work_unit_id),
            )
            connection.execute(
                "UPDATE attempts SET disposition='succeeded',finished_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s",
                (*self.key, self.claim.work_unit_id, self.claim.attempt_id),
            )
            connection.execute(
                "UPDATE operations SET status=%s,updated_at=clock_timestamp(), "
                "stage_state=stage_state-'error'-'error_detail' "
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                (
                    "running" if enqueue_semantic else "succeeded",
                    *self.key,
                    self.claim.operation_id,
                ),
            )
            if self.claim.stage == "consolidate":
                connection.execute(
                    "UPDATE operations SET stage_state=stage_state||%s::jsonb,"
                    "updated_at=clock_timestamp() WHERE tenant_id=%s AND user_id=%s "
                    "AND operation_id=(SELECT stage_state->>'source_operation_id' "
                    "FROM operations WHERE tenant_id=%s AND user_id=%s AND operation_id=%s)",
                    (
                        Jsonb(
                            {
                                "insights_ready": True,
                                "consolidation_status": "succeeded",
                            }
                        ),
                        *self.key,
                        *self.key,
                        self.claim.operation_id,
                    ),
                )
            connection.execute(
                "UPDATE users SET published_seq=greatest(published_seq,%s) "
                "WHERE tenant_id=%s AND user_id=%s",
                (self.source["accepted_seq"], *self.key),
            )
            if semantic_completion:
                valid = connection.execute(
                    "SELECT lease_expires_at>clock_timestamp() FROM work_units "
                    "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s",
                    (*self.key, self.claim.work_unit_id),
                ).fetchone()
                if valid is None or not valid[0]:
                    raise StaleIndexWork("semantic completion lease expired")

    def semantic_seed_manifest(self, connection):
        from oam.indexing.links import SEMANTIC_LINK_RECIPE

        state = (
            connection.execute(
                "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id=%s "
                "AND operation_id=%s",
                (*self.key, self.claim.operation_id),
            ).fetchone()[0]
            or {}
        )
        manifest = state.get("semantic_link_manifest")
        seeds = manifest.get("seed_ids") if isinstance(manifest, dict) else None
        if (
            not state.get("memory_ready")
            or not state.get("temporal_links_ready")
            or not isinstance(seeds, list)
            or any(not isinstance(seed, str) or not seed for seed in seeds)
            or seeds != sorted(set(seeds))
            or manifest.get("source_id") != self.source["source_id"]
            or manifest.get("source_version") != self.source["source_version"]
            or manifest.get("generation_id") != self.generation_id
            or manifest.get("recipe") != SEMANTIC_LINK_RECIPE
        ):
            raise StaleIndexWork("semantic seed manifest is incompatible")
        return manifest

    def fail(self, error: Exception):
        from oam.indexing.extraction import ExtractionError, ExtractionInputTooLongError
        from oam.models.runtime import (
            ModelEmptyContentError,
            ModelOutputError,
            Usage,
            retryable_model_http_error,
        )

        diagnostic = {"error": type(error).__name__}
        if isinstance(error, (ExtractionError, ModelOutputError)):
            diagnostic["error_detail"] = str(error)
        known_usage = (
            error.usage if isinstance(error, (ExtractionError, ModelOutputError)) else None
        )
        retryable_http = retryable_model_http_error(error)
        not_dispatched = isinstance(
            error,
            (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
                ExtractionInputTooLongError,
            ),
        )
        if retryable_http and not not_dispatched:
            known_usage = Usage(coverage="partial")
        retryable = (
            retryable_http
            or known_usage is not None
            and (
                isinstance(error, ModelEmptyContentError)
                or isinstance(error, ExtractionError)
                and error.retryable
            )
        )
        inputs_changed = (
            isinstance(error, StaleConsolidationInputs) and not self._unsettled_model_stages
        )
        retryable = retryable or inputs_changed
        # The source coordinator drains all producers before failure settlement.
        # One error cannot settle several stages with unsaved outcomes.
        if len(self._unsettled_model_stages) > 1:
            not_dispatched = False
            if retryable_http:
                known_usage = Usage(coverage="partial")
            else:
                known_usage = None
                retryable = False
        semantic = (
            self.claim.stage == "index_semantic_links"
            or self._inline_semantic
            or self._semantic_handoff
        )
        if semantic:
            retryable = retryable or isinstance(error, (psycopg.Error, OSError))
        with self.transaction() as connection:
            owned = connection.execute(
                "UPDATE work_units SET status=CASE WHEN %s AND attempts_remaining>0 "
                "THEN 'queued' ELSE 'failed_terminal' END,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND owner_id=%s "
                "AND fence=%s AND status='running' AND lease_expires_at>clock_timestamp() "
                "RETURNING status",
                (
                    retryable,
                    *self.key,
                    self.claim.work_unit_id,
                    self.worker_id,
                    self.claim.fence,
                ),
            ).fetchone()
            if owned:
                if ((not_dispatched or semantic) and known_usage is None) or inputs_changed:
                    connection.execute(
                        "UPDATE attempts SET disposition='failed',usage=NULL,response_ref=NULL,"
                        "finished_at=clock_timestamp() WHERE tenant_id=%s AND user_id=%s "
                        "AND work_unit_id=%s AND attempt_id=%s",
                        (*self.key, self.claim.work_unit_id, self.claim.attempt_id),
                    )
                elif known_usage is not None:
                    connection.execute(
                        "UPDATE attempts SET disposition='failed',usage=%s,"
                        "response_ref=request_ref,finished_at=clock_timestamp() "
                        "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s",
                        (
                            Jsonb(known_usage.model_dump(mode="json")),
                            *self.key,
                            self.claim.work_unit_id,
                            self.claim.attempt_id,
                        ),
                    )
                retrying = owned[0] == "queued"
                if semantic:
                    diagnostic.update(
                        links_ready=False, semantic_links_status="pending" if retrying else "failed"
                    )
                if retrying:
                    diagnostic["last_retry_error"] = dict(diagnostic)
                if not semantic or self._inline_semantic or self._semantic_handoff:
                    self.summarize_usage(connection)
                connection.execute(
                    "UPDATE operations SET status=%s,"
                    "stage_state=stage_state||%s::jsonb "
                    "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s AND status='running'",
                    (
                        "running" if retrying else "partial_failed",
                        Jsonb(diagnostic),
                        *self.key,
                        self.claim.operation_id,
                    ),
                )
                if self.claim.stage == "consolidate":
                    connection.execute(
                        "UPDATE operations SET stage_state=stage_state||%s::jsonb,"
                        "updated_at=clock_timestamp() WHERE tenant_id=%s AND user_id=%s "
                        "AND operation_id=(SELECT stage_state->>'source_operation_id' "
                        "FROM operations WHERE tenant_id=%s AND user_id=%s AND operation_id=%s)",
                        (
                            Jsonb({"consolidation_status": "pending" if retrying else "failed"}),
                            *self.key,
                            *self.key,
                            self.claim.operation_id,
                        ),
                    )

    def summarize_usage(self, connection):
        work_filter = " AND work_unit_id=%s" if self.claim.stage == "consolidate" else ""
        parameters = (
            *self.key,
            self.source["source_id"],
            self.source["source_version"],
            self.generation_id,
        )
        rows = connection.execute(
            "SELECT stage_key,usage FROM index_stage_results WHERE tenant_id=%s AND user_id=%s "
            "AND source_id=%s AND source_version=%s AND generation_id=%s" + work_filter,
            (*parameters, self.claim.work_unit_id) if work_filter else parameters,
        ).fetchall()
        attempts = connection.execute(
            "SELECT a.disposition,a.request_ref,a.usage FROM attempts a "
            "JOIN work_units w USING(tenant_id,user_id,work_unit_id) "
            "JOIN operations o USING(tenant_id,user_id,operation_id) "
            "WHERE o.tenant_id=%s AND o.user_id=%s AND o.source_id=%s AND o.source_version=%s "
            "AND w.generation_id=%s" + (" AND w.work_unit_id=%s" if work_filter else ""),
            (*parameters, self.claim.work_unit_id) if work_filter else parameters,
        ).fetchall()
        # One failed attempt can include overlapping generation and embedding calls.
        uncertain = any(
            disposition == "unknown_external_outcome"
            or disposition == "failed"
            and usage is not None
            and usage.get("coverage") == "partial"
            for disposition, _, usage in attempts
        )
        generation = []
        embedding = []
        rows.extend(
            (stage, usage)
            for disposition, stage, usage in attempts
            if disposition == "failed" and stage is not None and usage is not None
        )
        for stage, usage in rows:
            if stage.startswith(
                ("extract:", "vision:", "insight:recipe=", "abstract:recipe=")
            ) or stage in {"insight", "abstract"}:
                generation.append(usage)
            elif stage.startswith(("passage:", "facts_embed:", "insight_embed")):
                embedding.append(usage)

        def total(values):
            result = {
                name: sum(value[name] for value in values)
                if all(value.get(name) is not None for value in values)
                else None
                for name in (
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "reasoning_tokens",
                    "cached_input_tokens",
                    "uncached_input_tokens",
                    "cache_creation_input_tokens",
                )
            }
            if uncertain:
                for name in (
                    "cached_input_tokens",
                    "uncached_input_tokens",
                    "cache_creation_input_tokens",
                ):
                    result[name] = None
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
        insight_revision=1,
        mentioned_at=None,
        source_created_at=None,
        source_updated_at=None,
        ordinal=0,
        title=None,
        heading=None,
        caption=None,
    ):
        from oam.retrieval.fts import index_streams

        streams = index_streams(text)
        caption_streams = index_streams(caption or "")
        heading_streams = index_streams(heading or "")
        title_streams = index_streams(title or "")

        connection.execute(
            "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,source_id,"
            "source_version,evidence_id,passage_id,memory_id,insight_id,insight_revision,"
            "generation_id,content_text,locator,embedding,lexical_text,lexical_english,"
            "lexical_simple,lexical_caption_english,lexical_caption_simple,"
            "lexical_heading_english,lexical_heading_simple,lexical_title_english,"
            "lexical_title_simple,mentioned_at,"
            "source_created_at,source_updated_at,ordinal) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,%s,"
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
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
                insight_revision if insight_id else None,
                self.generation_id,
                text,
                Jsonb(locator),
                json.dumps(vector),
                text,
                streams.english,
                streams.simple,
                caption_streams.english,
                caption_streams.simple,
                heading_streams.english,
                heading_streams.simple,
                title_streams.english,
                title_streams.simple,
                mentioned_at,
                source_created_at,
                source_updated_at,
                ordinal,
            ),
        )
