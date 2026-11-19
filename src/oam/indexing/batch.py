"""User-owned L2 claims; source extraction and legacy checkpoints stay source-owned.

ready sources -> fixed window -> frozen bounded group -> reserved call
             -> durable response -> fenced publication + member progress
Retries reference the original batch and reuse its frozen, compatible groups.
"""

from __future__ import annotations

import json
import time

from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.db.eligibility import ELIGIBLE_UNITS
from oam.indexing.budget import BatchBudgetModels, BatchGenerationBudgetRejection
from oam.indexing.extraction import ConsolidationResult, InsightInput, consolidate_insights
from oam.indexing.store import IndexStore, StaleConsolidationInputs, StaleIndexWork, input_digest
from oam.models.embedding_inputs import EmbeddingInputLimitError
from oam.workers.claims import RENEW_LEASE_SQL


class BatchStore(IndexStore):
    """Reuse transaction/attempt mechanics, never a proxy source or source checkpoint."""

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
            row = connection.execute(
                "SELECT o.stage_state,w.generation_id FROM operations o JOIN work_units w "
                "USING(tenant_id,user_id,operation_id) WHERE w.tenant_id=%s AND w.user_id=%s "
                "AND w.work_unit_id=%s",
                (*self.key, self.claim.work_unit_id),
            ).fetchone()
            if row is None or not row[0].get("l2_batch_id"):
                raise StaleIndexWork("batch identity is unavailable")
            self.batch_id, self.generation_id = row[0]["l2_batch_id"], row[1]
            connection.execute(
                "UPDATE operations SET status='running',updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s "
                "AND status IN ('accepted','running') AND cancellation_revision=0 "
                "AND EXISTS(SELECT 1 FROM work_units w WHERE w.tenant_id=%s AND w.user_id=%s "
                "AND w.work_unit_id=%s AND w.owner_id=%s AND w.fence=%s "
                "AND w.status='running' AND w.lease_expires_at>clock_timestamp())",
                (
                    *self.key,
                    self.claim.operation_id,
                    *self.key,
                    self.claim.work_unit_id,
                    self.worker_id,
                    self.claim.fence,
                ),
            )

    def guard(self, connection, *, allow_policy_change=False):
        user = connection.execute(
            "SELECT state,index_generation_id FROM users WHERE tenant_id=%s AND user_id=%s "
            "FOR UPDATE",
            self.key,
        ).fetchone()
        row = connection.execute(
            "SELECT w.owner_id,w.fence,w.status,w.lease_expires_at>clock_timestamp(),"
            "o.cancellation_revision,o.status,w.generation_id,o.stage_state->>'l2_batch_id' "
            "FROM work_units w JOIN operations o USING(tenant_id,user_id,operation_id) "
            "WHERE w.tenant_id=%s AND w.user_id=%s AND w.work_unit_id=%s "
            "AND o.operation_id=%s FOR UPDATE OF w,o",
            (*self.key, self.claim.work_unit_id, self.claim.operation_id),
        ).fetchone()
        if (
            user is None
            or user[0] != "active"
            or user[1] != self.generation_id
            or row is None
            or row[:5] != (self.worker_id, self.claim.fence, "running", True, 0)
            or row[5] not in {"accepted", "running"}
            or row[6:] != (self.generation_id, self.batch_id)
        ):
            raise StaleIndexWork("batch claim, generation, or user is no longer eligible")

    def l2_progress(self, connection):
        state = connection.execute(
            "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id=%s "
            "AND operation_id=%s",
            (*self.key, self.batch_id),
        ).fetchone()[0]
        return set(state.get("l2_examined_ids", [])), state.get(
            "last_insight_outcome", "no_insight"
        )

    def mark_l2_group(self, connection, unit_ids, outcome):
        examined, previous = self.l2_progress(connection)
        connection.execute(
            "UPDATE operations SET stage_state=stage_state||%s,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
            (
                Jsonb(
                    {
                        "l2_examined_ids": sorted(examined | set(unit_ids)),
                        "last_insight_outcome": "insight" if previous == "insight" else outcome,
                    }
                ),
                *self.key,
                self.batch_id,
            ),
        )
        self.settle_members(connection)

    def settle_members(self, connection):
        examined, outcome = self.l2_progress(connection)
        connection.execute(
            "UPDATE l2_batch_members bm SET status='excluded',error='source_ineligible' "
            "WHERE bm.tenant_id=%s AND bm.user_id=%s AND bm.batch_id=%s "
            "AND bm.status='pending' AND NOT EXISTS(SELECT 1 FROM sources s "
            "JOIN source_versions sv ON (sv.tenant_id,sv.user_id,sv.source_id,sv.source_version)="
            "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
            "WHERE (s.tenant_id,s.user_id,s.source_id,s.active_version)="
            "(bm.tenant_id,bm.user_id,bm.source_id,bm.source_version) "
            "AND s.status='active' AND sv.status='active')",
            (*self.key, self.batch_id),
        )
        connection.execute(
            f"WITH eligible AS ({ELIGIBLE_UNITS}) UPDATE l2_batch_members bm "
            "SET status='succeeded',error=NULL WHERE bm.tenant_id=%s AND bm.user_id=%s "
            "AND bm.batch_id=%s AND bm.status='pending' AND NOT EXISTS(SELECT 1 "
            "FROM eligible u WHERE u.kind='fact' "
            "AND (u.source_id,u.source_version,u.generation_id)="
            "(bm.source_id,bm.source_version,bm.generation_id) AND NOT(u.unit_id=ANY(%s)))",
            (*self.key, self.batch_id, list(examined)),
        )
        connection.execute(
            "UPDATE operations o SET stage_state=o.stage_state||jsonb_build_object("
            "'insights_ready',bm.status='succeeded','consolidation_status',"
            "CASE WHEN bm.status='succeeded' THEN 'succeeded' WHEN bm.status='pending' "
            "THEN 'pending' ELSE 'failed' END,'insight_outcome',%s::text,"
            "'consolidation_error',bm.error),"
            "updated_at=clock_timestamp() FROM l2_batch_members bm "
            "WHERE (o.tenant_id,o.user_id,o.operation_id)="
            "(bm.tenant_id,bm.user_id,bm.source_operation_id) AND bm.tenant_id=%s "
            "AND bm.user_id=%s AND bm.batch_id=%s",
            (outcome, *self.key, self.batch_id),
        )
        members = connection.execute(
            "SELECT status,source_id,source_version,error FROM l2_batch_members "
            "WHERE tenant_id=%s AND user_id=%s AND batch_id=%s ORDER BY source_id,source_version",
            (*self.key, self.batch_id),
        ).fetchall()
        counts = dict.fromkeys(("pending", "succeeded", "excluded", "failed"), 0)
        failed = []
        for status, source_id, version, error in members:
            counts[status] += 1
            if status == "failed":
                failed.append({"source_id": source_id, "source_version": version, "error": error})
        connection.execute(
            "UPDATE operations SET stage_state=stage_state||%s,updated_at=clock_timestamp() "
            "WHERE tenant_id=%s AND user_id=%s AND operation_id=ANY(%s)",
            (
                Jsonb({"l2_member_counts": counts, "failed_sources": failed}),
                *self.key,
                [self.batch_id, self.claim.operation_id],
            ),
        )
        return counts

    def checkpoint(self, stage, inputs, produce):
        digest = input_digest(inputs)
        stage_ref = stage + ":" + digest.hex()
        with self.transaction() as connection:
            self.guard(connection)
            row = connection.execute(
                "SELECT output FROM l2_batch_groups WHERE tenant_id=%s AND user_id=%s "
                "AND batch_id=%s AND stage_key=%s AND input_digest=%s",
                (*self.key, self.batch_id, stage, digest),
            ).fetchone()
            if row and row[0] is not None:
                return row[0]
            connection.execute(
                "INSERT INTO l2_batch_groups(tenant_id,user_id,batch_id,stage_key,"
                "input_digest,inputs,work_unit_id,attempt_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (
                    *self.key,
                    self.batch_id,
                    stage,
                    digest,
                    Jsonb(inputs),
                    self.claim.work_unit_id,
                    self.claim.attempt_id,
                ),
            )
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
            self._record_model_stage_state(connection, stage_ref, dispatched=(stage_ref,))
        self._unsettled_model_stages.add(stage_ref)
        try:
            output = produce()
        except Exception as error:
            if isinstance(error, EmbeddingInputLimitError) or (
                isinstance(error, BatchGenerationBudgetRejection)
                and not error.has_prior_generation_usage
            ):
                self._settle_local_rejection(stage_ref)
                raise
            self._model_stage_failures[stage_ref] = error
            raise
        if hasattr(output, "model_dump"):
            output = output.model_dump(mode="json")
        try:
            with self.transaction() as connection:
                self.guard(connection)
                connection.execute(
                    "UPDATE l2_batch_groups SET output=%s,usage=%s,work_unit_id=%s,attempt_id=%s "
                    "WHERE tenant_id=%s AND user_id=%s AND batch_id=%s "
                    "AND stage_key=%s AND input_digest=%s AND output IS NULL",
                    (
                        Jsonb(output),
                        Jsonb(output.get("usage", {})),
                        self.claim.work_unit_id,
                        self.claim.attempt_id,
                        *self.key,
                        self.batch_id,
                        stage,
                        digest,
                    ),
                )
                self._record_model_stage_state(connection, stage_ref, completed=(stage_ref,))
        except Exception:
            self._uncached_model_stages.add(stage_ref)
            raise
        self._unsettled_model_stages.remove(stage_ref)
        return output

    def publication_sequence(self, connection, digest):
        row = connection.execute(
            "SELECT publication_seq FROM l2_batch_groups WHERE tenant_id=%s AND user_id=%s "
            "AND batch_id=%s AND stage_key='insight' AND input_digest=%s FOR UPDATE",
            (*self.key, self.batch_id, digest),
        ).fetchone()
        if row is None:
            raise StaleConsolidationInputs("frozen batch group is unavailable")
        if row[0] is not None:
            return row[0]
        sequence = connection.execute(
            "UPDATE users SET accepted_seq=accepted_seq+1,published_seq=accepted_seq+1 "
            "WHERE tenant_id=%s AND user_id=%s RETURNING accepted_seq",
            self.key,
        ).fetchone()[0]
        connection.execute(
            "UPDATE l2_batch_groups SET publication_seq=%s WHERE tenant_id=%s AND user_id=%s "
            "AND batch_id=%s AND stage_key='insight' AND input_digest=%s",
            (sequence, *self.key, self.batch_id, digest),
        )
        return sequence

    def summarize_usage(self, connection):
        from oam.indexing.extraction import _sum_extraction_usage
        from oam.models.runtime import Usage

        rows = connection.execute(
            "SELECT stage_key,usage FROM l2_batch_groups WHERE tenant_id=%s AND user_id=%s "
            "AND batch_id=%s AND output IS NOT NULL",
            (*self.key, self.batch_id),
        ).fetchall()
        attempts = connection.execute(
            "SELECT a.disposition,a.request_ref,a.usage FROM attempts a JOIN work_units w "
            "USING(tenant_id,user_id,work_unit_id) JOIN operations o "
            "USING(tenant_id,user_id,operation_id) WHERE o.tenant_id=%s AND o.user_id=%s "
            "AND o.stage_state->>'l2_batch_id'=%s",
            (*self.key, self.batch_id),
        ).fetchall()
        generation = [Usage.model_validate(usage) for stage, usage in rows if "embed" not in stage]
        embedding = [Usage.model_validate(usage) for stage, usage in rows if "embed" in stage]
        generation.extend(
            Usage.model_validate(usage) if usage else None
            for disposition, stage, usage in attempts
            if disposition in {"failed", "unknown_external_outcome"}
            and (stage is None or "embed" not in stage)
        )
        self.readiness(
            connection,
            usage=_sum_extraction_usage(generation).model_dump(mode="json"),
            embedding_usage=_sum_extraction_usage(embedding).model_dump(mode="json"),
            index_attempts=len(attempts),
        )

    def finish(self):
        with self.transaction() as connection:
            self.guard(connection)
            counts = self.settle_members(connection)
            failed = counts["pending"] + counts["failed"]
            self.summarize_usage(connection)
            self.readiness(connection, insights_ready=not failed)
            connection.execute(
                "UPDATE work_units SET status=%s,updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s",
                ("failed_terminal" if failed else "succeeded", *self.key, self.claim.work_unit_id),
            )
            connection.execute(
                "UPDATE attempts SET disposition='succeeded',finished_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s",
                (*self.key, self.claim.work_unit_id, self.claim.attempt_id),
            )
            connection.execute(
                "UPDATE operations SET status=%s,updated_at=clock_timestamp(),"
                "stage_state=CASE WHEN %s THEN stage_state||"
                '\'{"error":"BatchMembersFailed"}\'::jsonb '
                "ELSE stage_state-'error'-'error_detail' END "
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                (
                    "partial_failed" if failed else "succeeded",
                    bool(failed),
                    *self.key,
                    self.claim.operation_id,
                ),
            )

    def failure_settled(self, connection, error, retrying):
        """Settle members before the same commit can admit an explicit retry."""
        if not retrying:
            connection.execute(
                "UPDATE l2_batch_members SET status='failed',error=%s WHERE tenant_id=%s "
                "AND user_id=%s AND batch_id=%s AND status='pending'",
                (type(error).__name__, *self.key, self.batch_id),
            )
        self.settle_members(connection)


def run_batch(store, models):
    """Drain frozen work before selecting more seeds; paid results survive a restart."""
    from oam.indexing.consolidation import (
        _freeze_group,
        _group_inputs,
        _pending_group,
        _prepared_actions,
        _publish_group,
        _source_groups,
    )

    started = time.monotonic()
    while True:
        with store.transaction() as connection:
            store.guard(connection)
            store.settle_members(connection)
            saved = connection.execute(
                "SELECT inputs FROM l2_batch_groups WHERE tenant_id=%s AND user_id=%s "
                "AND batch_id=%s AND stage_key='insight' AND NOT published AND NOT invalidated "
                "ORDER BY created_at,input_digest LIMIT 1",
                (*store.key, store.batch_id),
            ).fetchone()
            if saved:
                inputs = saved[0]
                config = OAMConfig.model_validate(inputs["batch_config"])
                facts = tuple(InsightInput.model_validate(fact) for fact in inputs["private_facts"])
                seeds = tuple(inputs["envelope"]["seed_ids"])
                targets = tuple(inputs["envelope"]["targets"])
                private_targets = inputs["private_targets"]
            else:
                examined, _ = store.l2_progress(connection)
                config = store.config
                state = connection.execute(
                    "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id=%s "
                    "AND operation_id=%s",
                    (*store.key, store.batch_id),
                ).fetchone()[0]
                if state["l2_seed_policy"] == "source":
                    from oam.config import InsightSourceBatchConfig

                    batch = state.get("l2_source_batch")
                    config = config.model_copy(
                        update={
                            "index": config.index.model_copy(
                                update={
                                    "insight_source_batch": InsightSourceBatchConfig.model_validate(
                                        batch
                                    )
                                    if batch
                                    else None,
                                }
                            )
                        }
                    ).for_insight_source_batch()
                    groups = _source_groups(
                        store,
                        connection,
                        None,
                        examined,
                        deadline=time.monotonic() + config.model.attempt_timeout_seconds,
                        config=config,
                    )
                    frozen = groups[0] if groups else None
                else:
                    group = _pending_group(store, connection, None, examined)
                    frozen = _freeze_group(store, connection, group) if group else None
                if frozen is None:
                    break
                facts, seeds, targets, private_targets = frozen
                if len(facts) < 2 and not targets:
                    store.mark_l2_group(connection, seeds, "no_insight")
                    continue
                inputs = _group_inputs(store, facts, seeds, targets, private_targets, config=config)
                inputs["batch_config"] = config.model_dump(mode="json")
        try:
            output = store.checkpoint(
                "insight",
                inputs,
                lambda facts=facts, seeds=seeds, targets=targets, config=config, inputs=inputs: (
                    consolidate_insights(
                        BatchBudgetModels(store, models, inputs),
                        facts=facts,
                        seeds=seeds,
                        targets=targets,
                        config=config,
                        timeout_seconds=config.model.attempt_timeout_seconds,
                    )
                ),
            )
        except BatchGenerationBudgetRejection as error:
            if error.has_prior_generation_usage or not error.members:
                raise
            with store.transaction() as connection:
                store.guard(connection)
                store.settle_members(connection)
                for source_id, version in error.members:
                    connection.execute(
                        "UPDATE l2_batch_members SET status='failed',error=%s "
                        "WHERE tenant_id=%s AND user_id=%s AND batch_id=%s "
                        "AND source_id=%s AND source_version=%s AND status='pending'",
                        (type(error).__name__, *store.key, store.batch_id, source_id, version),
                    )
                connection.execute(
                    "UPDATE l2_batch_groups SET invalidated=true WHERE tenant_id=%s AND user_id=%s "
                    "AND batch_id=%s AND stage_key='insight' AND input_digest=%s",
                    (*store.key, store.batch_id, input_digest(inputs)),
                )
                store.settle_members(connection)
            print(
                json.dumps(
                    {
                        "stage": "insight_batch_member_rejected",
                        "batch_id": store.batch_id,
                        "error": type(error).__name__,
                        "member_count": len(error.members),
                    }
                ),
                flush=True,
            )
            continue
        result = ConsolidationResult.model_validate(output)
        prepared = _prepared_actions(store, models, result, private_targets)
        digest = input_digest(inputs)
        try:
            with store.transaction() as connection:
                _publish_group(
                    store,
                    connection,
                    facts=facts,
                    seeds=seeds,
                    targets=targets,
                    prepared=prepared,
                    digest=digest,
                )
                connection.execute(
                    "UPDATE l2_batch_groups SET published=true WHERE tenant_id=%s AND user_id=%s "
                    "AND batch_id=%s AND stage_key='insight' AND input_digest=%s",
                    (*store.key, store.batch_id, digest),
                )
        except StaleConsolidationInputs:
            with store.transaction() as connection:
                store.guard(connection)
                connection.execute(
                    "UPDATE l2_batch_groups SET invalidated=true WHERE tenant_id=%s AND user_id=%s "
                    "AND batch_id=%s AND stage_key='insight' AND input_digest=%s",
                    (*store.key, store.batch_id, digest),
                )
            raise
        print(
            json.dumps(
                {
                    "stage": "insight_batch_published",
                    "operation_id": store.claim.operation_id,
                    "batch_id": store.batch_id,
                    "seed_count": len(seeds),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                    "usage": result.usage.model_dump(mode="json"),
                }
            ),
            flush=True,
        )
    store.finish()
