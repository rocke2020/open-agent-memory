"""Non-generative orchestration for L1-caused Insight repair."""

from __future__ import annotations

import time

from psycopg.types.json import Jsonb

from oam.db.eligibility import ELIGIBLE_UNITS
from oam.indexing.extraction import (
    INSIGHT_SYSTEM,
    InsightInput,
    InsightResult,
    insight_annotations,
    insight_fact_projection,
    pack_insight_facts,
    synthesize_insight,
)
from oam.indexing.identity import INSIGHT_MODEL_RECIPE
from oam.indexing.store import MEMORY_REVISION, input_digest, log_insight_indexed, stable_id


def repair_insights(store, models) -> bool:
    """Repair captured Insight identities after their L1 support was removed.

    Cleanup has already removed the old revisions. This function never treats a
    model failure as a valid deletion outcome and never revives a directly removed
    Insight identity.
    """
    with store.transaction() as connection:
        store.guard(connection)
        row = connection.execute(
            "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id=%s "
            "AND operation_id=%s FOR UPDATE",
            (*store.key, store.claim.operation_id),
        ).fetchone()
        state = dict(row[0] or {}) if row else {}
    repairs = state.get("insight_repairs", [])
    if not isinstance(repairs, list) or not repairs:
        return False
    outcomes = dict(state.get("repair_outcomes", {}))
    policy_epoch = int(store.source["policy_epoch"])

    for repair in repairs:
        if not isinstance(repair, dict):
            raise ValueError("invalid Insight repair metadata")
        insight_id = str(repair["insight_id"])
        if insight_id in outcomes:
            continue
        revision = int(repair["next_revision"])
        support_ids = tuple(str(value) for value in repair.get("support_ids", ()))

        with store.transaction() as connection:
            store.guard(connection)
            rows = connection.execute(
                f"WITH eligible AS ({ELIGIBLE_UNITS}) "
                "SELECT u.unit_id,u.memory_id,u.content_text,u.source_id,u.source_version,"
                "u.mentioned_at,"
                "u.locator->>'role' AS message_role,"
                "(SELECT jsonb_agg(ut.detail ORDER BY ut.annotation_id) FROM unit_times ut WHERE "
                "(ut.tenant_id,ut.user_id,ut.unit_id)="
                "(u.tenant_id,u.user_id,u.unit_id)) AS annotations "
                "FROM eligible u WHERE kind='fact' AND memory_id=ANY(%s) "
                "ORDER BY memory_id,unit_id",
                (list(support_ids),),
            ).fetchall()
            directly_removed = connection.execute(
                "SELECT 1 FROM operations WHERE tenant_id=%s AND user_id=%s "
                "AND removal_target_kind='insight' AND removal_target_id=%s LIMIT 1",
                (*store.key, insight_id),
            ).fetchone()
            if directly_removed:
                outcomes[insight_id] = "directly_removed"
                _save_outcome(store, connection, outcomes)
                continue
        if len(rows) < 2:
            with store.transaction() as connection:
                store.guard(connection)
                _save_outcome_if_current(
                    store, connection, insight_id, revision, outcomes, "deleted", rows
                )
            continue
        with store.transaction() as connection:
            store.guard(connection)
            max_revision = connection.execute(
                "SELECT COALESCE(MAX(revision),0) FROM insights WHERE tenant_id=%s "
                "AND user_id=%s AND insight_id=%s",
                (*store.key, insight_id),
            ).fetchone()[0]
            if int(max_revision) >= revision:
                outcomes[insight_id] = "superseded"
                _save_outcome(store, connection, outcomes)
                continue

        facts = pack_insight_facts(
            tuple(
                InsightInput(
                    id=row[1],
                    text=row[2],
                    source_refs=(f"{row[3]}:{row[4]}",),
                    mentioned_at=row[5],
                    message_role=row[6],
                    temporal_annotations=insight_annotations(row[7]),
                )
                for row in rows
            ),
            store.config,
        )
        captured_support = {row[1] for row in rows}
        if {fact.id for fact in facts} != captured_support:
            raise ValueError("Insight repair budget cannot fit the complete eligible support set")

        input_values = {
            "recipe": INSIGHT_MODEL_RECIPE,
            "system": INSIGHT_SYSTEM,
            "envelope": {"facts": [insight_fact_projection(fact) for fact in facts]},
            "private_facts": [fact.model_dump(mode="json") for fact in facts],
            "repair": repair,
            "generation_id": store.generation_id,
            "config": {
                "index": store.config.index.model_dump(
                    mode="json",
                    exclude={
                        "insight_generation_concurrency",
                        "insight_seed_policy",
                        "insight_source_batch",
                    },
                ),
                "extraction": store.config.extraction.model_dump(mode="json"),
            },
        }
        insight_started = time.monotonic()
        result = InsightResult.model_validate(
            store.checkpoint(
                f"repair:{insight_id}:{revision}:{policy_epoch}:synthesis",
                input_values,
                lambda facts=facts: synthesize_insight(
                    models,
                    facts=facts,
                    config=store.config,
                    timeout_seconds=store.config.model.attempt_timeout_seconds,
                ),
            )
        )
        if result.outcome == "no_insight":
            with store.transaction() as connection:
                store.guard(connection)
                _save_outcome_if_current(
                    store, connection, insight_id, revision, outcomes, "deleted", rows
                )
            continue
        if not result.text:
            raise ValueError("validated Insight repair has no text")
        if set(result.support_ids) != captured_support:
            raise ValueError("Insight repair must preserve the complete eligible support set")

        embedding_text = result.embedding_text
        assert embedding_text is not None
        vector = store.checkpoint(
            f"repair:{insight_id}:{revision}:{policy_epoch}:embed",
            embedding_text,
            lambda text=embedding_text: models.embed(
                (text,), timeout_seconds=store.config.model.attempt_timeout_seconds
            ),
        )
        digest = input_digest(input_values)
        with store.transaction() as connection:
            store.guard(connection)
            direct = connection.execute(
                "SELECT 1 FROM operations WHERE tenant_id=%s AND user_id=%s "
                "AND removal_target_kind='insight' AND removal_target_id=%s LIMIT 1",
                (*store.key, insight_id),
            ).fetchone()
            if direct:
                outcomes[insight_id] = "directly_removed"
                _save_outcome(store, connection, outcomes)
                continue
            eligible = connection.execute(
                f"WITH eligible AS ({ELIGIBLE_UNITS}) SELECT memory_id FROM eligible "
                "WHERE kind='fact' AND memory_id=ANY(%s)",
                (list(result.support_ids),),
            ).fetchall()
            if {row[0] for row in eligible} != captured_support:
                raise ValueError("Insight repair support became ineligible")
            max_revision = connection.execute(
                "SELECT COALESCE(MAX(revision),0) FROM insights WHERE tenant_id=%s "
                "AND user_id=%s AND insight_id=%s",
                (*store.key, insight_id),
            ).fetchone()[0]
            if int(max_revision) >= revision:
                outcomes[insight_id] = "superseded"
                _save_outcome(store, connection, outcomes)
                continue
            connection.execute(
                "INSERT INTO insights(tenant_id,user_id,insight_id,revision,synthesis_text,"
                "subtype,input_digest,recipe_id,generation_id,visible_from) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,'oam-insight-repair-v1',%s,%s)",
                (
                    *store.key,
                    insight_id,
                    revision,
                    result.text,
                    result.subtype,
                    digest,
                    store.generation_id,
                    store.source["accepted_seq"],
                ),
            )
            for memory_id in result.support_ids:
                connection.execute(
                    "INSERT INTO insight_support(tenant_id,user_id,insight_id,"
                    "insight_revision,memory_id,support_revision) VALUES (%s,%s,%s,%s,%s,%s)",
                    (*store.key, insight_id, revision, memory_id, MEMORY_REVISION),
                )
            store.insert_unit(
                connection,
                unit_id=stable_id("insight", insight_id, revision, digest.hex()),
                kind="insight",
                text=result.text,
                vector=vector["vectors"][0],
                locator={
                    "support_ids": list(result.support_ids),
                    "repair": True,
                    "retrieval_text": result.retrieval_text,
                },
                insight_id=insight_id,
                insight_revision=revision,
            )
            outcomes[insight_id] = "updated"
            store.mark_l2_group(connection, [row[0] for row in rows], "insight")
            _save_outcome(store, connection, outcomes)
        log_insight_indexed(store, result, insight_id, revision, insight_started)
    return True


def _save_outcome(store, connection, outcomes: dict[str, str]) -> None:
    connection.execute(
        "UPDATE operations SET stage_state=stage_state||%s::jsonb,updated_at=clock_timestamp() "
        "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
        (Jsonb({"repair_outcomes": outcomes}), *store.key, store.claim.operation_id),
    )


def _save_outcome_if_current(
    store, connection, insight_id, revision, outcomes, outcome, rows
) -> None:
    direct = connection.execute(
        "SELECT 1 FROM operations WHERE tenant_id=%s AND user_id=%s "
        "AND removal_target_kind='insight' AND removal_target_id=%s LIMIT 1",
        (*store.key, insight_id),
    ).fetchone()
    max_revision = connection.execute(
        "SELECT COALESCE(MAX(revision),0) FROM insights WHERE tenant_id=%s "
        "AND user_id=%s AND insight_id=%s",
        (*store.key, insight_id),
    ).fetchone()[0]
    value = (
        "directly_removed" if direct else "superseded" if int(max_revision) >= revision else outcome
    )
    outcomes[insight_id] = value
    if value != "superseded":
        store.mark_l2_group(connection, [row[0] for row in rows], value)
    _save_outcome(store, connection, outcomes)
