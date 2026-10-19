"""Bounded incremental Insight lookup and fenced revision publication."""

from __future__ import annotations

import json
import time
from itertools import zip_longest

from psycopg.rows import dict_row

from oam.db.eligibility import ELIGIBLE_UNITS, ELIGIBLE_UNITS_CANDIDATE
from oam.indexing.extraction import (
    INCREMENTAL_INSIGHT_SYSTEM,
    ConsolidationResult,
    InsightInput,
    consolidate_insights,
    consolidation_envelope,
    model_input_tokens,
)
from oam.indexing.store import MEMORY_REVISION, StaleConsolidationInputs, input_digest, stable_id
from oam.retrieval.repository import RecallRepository


class InsightLookup(RecallRepository):
    """Reuse direct recall ranking with Insight eligibility before each limit."""

    @property
    def candidate_units_sql(self) -> str:
        return ELIGIBLE_UNITS_CANDIDATE + " AND u.kind='insight'"

    def _eligible(self, *, temporal=False, candidate_facts=False) -> str:
        return (
            "WITH eligible AS NOT MATERIALIZED ("
            + self.candidate_units_sql
            + "), filtered AS NOT MATERIALIZED (SELECT * FROM eligible) "
        )


def _rows(connection, query, parameters=()):
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query, parameters)
        return cursor.fetchall()


def _vector(value):
    return json.loads(value) if isinstance(value, str) else list(value)


def _pending_group(store, connection, new_fact_ids, examined):
    return _rows(
        connection,
        f"WITH eligible AS ({ELIGIBLE_UNITS}), pending AS MATERIALIZED ("
        "SELECT * FROM eligible WHERE kind='fact' AND source_id=%s AND source_version=%s "
        "AND unit_id=ANY(%s) AND NOT (unit_id=ANY(%s))), "
        "anchor AS (SELECT * FROM pending ORDER BY created_at,unit_id LIMIT 1) "
        "SELECT p.*,p.embedding::text AS vector FROM pending p CROSS JOIN anchor a "
        "WHERE p.unit_id=a.unit_id OR "
        "1-(p.embedding <=> a.embedding)>=%s "
        "ORDER BY (p.unit_id=a.unit_id) DESC,p.embedding <=> a.embedding,p.created_at,p.unit_id "
        "LIMIT %s",
        (
            store.source["source_id"],
            store.source["source_version"],
            new_fact_ids,
            list(examined),
            store.config.index.semantic_link_min_similarity,
            min(store.config.index.insight_seed_max, store.config.extraction.max_facts_per_group),
        ),
    )


def _lookup_targets(store, connection, seeds):
    limit = store.config.index.insight_candidate_max
    repository = InsightLookup(
        connection,
        store.authority,
        store.config,
        deadline=time.monotonic() + store.config.model.attempt_timeout_seconds,
    )
    channels = []
    for seed in seeds:
        channels.append(repository.dense(_vector(seed["vector"]), limit))
        lexical = repository.lexical(seed["content_text"], limit)
        anchors = repository.anchors(seed["content_text"], limit)
        channels.append([*anchors, *lexical])
    identifiers = []
    for level in zip_longest(*channels):
        for hit in level:
            if hit is not None and hit["unit_id"] not in identifiers:
                identifiers.append(hit["unit_id"])
                if len(identifiers) == limit:
                    break
        if len(identifiers) == limit:
            break
    if not identifiers:
        return []
    return _rows(
        connection,
        f"WITH eligible AS ({ELIGIBLE_UNITS}) "
        "SELECT u.insight_id AS id,u.insight_revision AS revision,u.content_text AS text,"
        "u.locator->>'retrieval_text' AS retrieval_text,i.subtype,u.embedding::text AS vector,"
        "(SELECT jsonb_agg(jsonb_build_object('id',sp.memory_id,'revision',sp.support_revision) "
        "ORDER BY sp.memory_id) FROM insight_support sp WHERE "
        "(sp.tenant_id,sp.user_id,sp.insight_id,sp.insight_revision)="
        "(u.tenant_id,u.user_id,u.insight_id,u.insight_revision)) AS supports "
        "FROM eligible u JOIN insights i ON "
        "(i.tenant_id,i.user_id,i.insight_id,i.revision)="
        "(u.tenant_id,u.user_id,u.insight_id,u.insight_revision) "
        "WHERE u.kind='insight' AND u.unit_id=ANY(%s) "
        "ORDER BY array_position(%s,u.unit_id)",
        (identifiers, identifiers),
    )


def _fact_context(store, connection, seeds, targets):
    seed_ids = [seed["unit_id"] for seed in seeds]
    support_ids = sorted({support["id"] for target in targets for support in target["supports"]})
    return _rows(
        connection,
        f"WITH eligible AS ({ELIGIBLE_UNITS}), seeds AS ("
        "SELECT embedding FROM eligible WHERE kind='fact' AND unit_id=ANY(%s)), "
        "candidate_annotations AS (SELECT candidate.*,"
        "(SELECT jsonb_agg(ut.detail ORDER BY ut.annotation_id) FROM unit_times ut WHERE "
        "(ut.tenant_id,ut.user_id,ut.unit_id)="
        "(candidate.tenant_id,candidate.user_id,candidate.unit_id)) AS annotations "
        "FROM eligible candidate WHERE candidate.kind='fact'), ranked AS ("
        "SELECT candidate.*, (SELECT min(candidate.embedding <=> seeds.embedding) "
        "FROM seeds) AS distance FROM candidate_annotations candidate) "
        "SELECT * FROM ranked WHERE unit_id=ANY(%s) OR memory_id=ANY(%s) "
        "OR 1-distance>=%s ORDER BY (unit_id=ANY(%s)) DESC,"
        "(memory_id=ANY(%s)) DESC,distance,unit_id LIMIT %s",
        (
            seed_ids,
            seed_ids,
            support_ids,
            store.config.index.semantic_link_min_similarity,
            seed_ids,
            support_ids,
            store.config.extraction.max_facts_per_group,
        ),
    )


def _fact_input(row):
    from oam.contracts.evidence import TemporalAnnotation

    return InsightInput(
        id=row["memory_id"],
        text=row["content_text"],
        source_refs=(f"{row['source_id']}:{row['source_version']}",),
        mentioned_at=row["mentioned_at"],
        message_role=row["locator"].get("role"),
        temporal_annotations=tuple(
            TemporalAnnotation.model_validate(
                {key: value[key] for key in TemporalAnnotation.model_fields if key in value}
            )
            for value in row["annotations"] or ()
        ),
    )


def _pack_facts(facts, seeds, targets, config):
    """Keep every seed and complete admitted target; sample historical bodies only."""
    selected = tuple(fact for fact in facts if fact.id in seeds)
    if {fact.id for fact in selected} != set(seeds):
        raise StaleConsolidationInputs("Insight seeds became ineligible")

    def fits(candidate):
        return (
            model_input_tokens(
                INCREMENTAL_INSIGHT_SYSTEM, consolidation_envelope(candidate, seeds, targets)
            )
            <= config.extraction.max_input_tokens
        )

    if not fits(selected):
        return None
    for fact in facts:
        if fact.id not in seeds and fits((*selected, fact)):
            selected = (*selected, fact)
    return selected


def _freeze_group(store, connection, seeds):
    while seeds:
        targets = _lookup_targets(store, connection, seeds)
        public_targets = tuple(
            {key: value for key, value in target.items() if key != "vector"} for target in targets
        )
        facts = tuple(_fact_input(row) for row in _fact_context(store, connection, seeds, targets))
        seed_ids = tuple(seed["memory_id"] for seed in seeds)
        packed = _pack_facts(facts, seed_ids, public_targets, store.config)
        if packed is not None:
            return packed, seed_ids, public_targets, targets
        seeds = seeds[:-1]
    raise ValueError("Insight input budget cannot fit one seed and its complete target context")


def _prepared_actions(store, models, result, targets):
    by_id = {target["id"]: target for target in targets}
    prepared = []
    for action in result.actions:
        if action.action not in {"UPDATE", "CREATE"}:
            prepared.append((action, None))
            continue
        target = by_id.get(action.target_id)
        embedding_text = action.retrieval_text or action.text
        if target and embedding_text == (target["retrieval_text"] or target["text"]):
            vector = _vector(target["vector"])
        else:
            embedded = store.checkpoint(
                "insight_embed",
                embedding_text,
                lambda text=embedding_text: models.embed(
                    (text,), timeout_seconds=store.config.model.attempt_timeout_seconds
                ),
            )
            vector = embedded["vectors"][0]
        prepared.append((action, vector))
    return prepared


def _publish_group(store, connection, *, facts, seeds, targets, prepared, digest):
    """Publish every action and its dispositions in the caller's guarded transaction."""
    store.guard(connection)
    target_by_id = {target["id"]: target for target in targets}
    for target in targets:
        current = connection.execute(
            "SELECT revision FROM insights WHERE tenant_id=%s AND user_id=%s "
            "AND insight_id=%s AND generation_id=%s AND visible_to IS NULL FOR UPDATE",
            (*store.key, target["id"], store.generation_id),
        ).fetchall()
        if current != [(target["revision"],)]:
            raise StaleConsolidationInputs("Insight target revision changed")
        if connection.execute(
            "SELECT 1 FROM operations WHERE tenant_id=%s AND user_id=%s "
            "AND removal_target_kind='insight' AND removal_target_id=%s LIMIT 1",
            (*store.key, target["id"]),
        ).fetchone():
            raise StaleConsolidationInputs("Insight target was directly removed")
    required_support = {fact.id for fact in facts} | {
        support["id"] for target in targets for support in target["supports"]
    }
    eligible = connection.execute(
        f"WITH eligible AS ({ELIGIBLE_UNITS}) SELECT DISTINCT memory_id FROM eligible "
        "WHERE kind='fact' AND memory_id=ANY(%s)",
        (sorted(required_support),),
    ).fetchall()
    if {row[0] for row in eligible} != required_support or any(
        support["revision"] != MEMORY_REVISION
        for target in targets
        for support in target["supports"]
    ):
        raise StaleConsolidationInputs("Insight support became ineligible")

    published = []
    for slot, (action, vector) in enumerate(prepared):
        if action.action not in {"UPDATE", "CREATE"}:
            continue
        target = target_by_id.get(action.target_id)
        identifier = (
            action.target_id
            if target
            else stable_id(
                "insight",
                *store.key,
                store.source["source_id"],
                store.source["source_version"],
                store.generation_id,
                digest.hex(),
                slot,
            )
        )
        revision = target["revision"] + 1 if target else 1
        if connection.execute(
            "SELECT 1 FROM operations WHERE tenant_id=%s AND user_id=%s "
            "AND removal_target_kind='insight' AND removal_target_id=%s LIMIT 1",
            (*store.key, identifier),
        ).fetchone():
            raise StaleConsolidationInputs("Insight identity was directly removed")
        support_ids = set(action.support_ids)
        if target:
            support_ids.update(support["id"] for support in target["supports"])
        if len(support_ids) < 2 or not support_ids <= required_support:
            raise ValueError("Insight publication requires distinct eligible support")
        if target:
            connection.execute(
                "UPDATE insights SET visible_to=%s WHERE tenant_id=%s AND user_id=%s "
                "AND insight_id=%s AND revision=%s AND visible_to IS NULL",
                (store.source["accepted_seq"], *store.key, identifier, target["revision"]),
            )
        connection.execute(
            "INSERT INTO insights(tenant_id,user_id,insight_id,revision,synthesis_text,subtype,"
            "input_digest,recipe_id,generation_id,visible_from) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'oam-insight-incremental-v1',%s,%s)",
            (
                *store.key,
                identifier,
                revision,
                action.text,
                action.subtype,
                digest,
                store.generation_id,
                store.source["accepted_seq"],
            ),
        )
        for memory_id in sorted(support_ids):
            connection.execute(
                "INSERT INTO insight_support(tenant_id,user_id,insight_id,insight_revision,"
                "memory_id,support_revision) VALUES (%s,%s,%s,%s,%s,%s)",
                (*store.key, identifier, revision, memory_id, MEMORY_REVISION),
            )
        store.insert_unit(
            connection,
            unit_id=stable_id("insight_unit", identifier, revision),
            kind="insight",
            text=action.text,
            vector=vector,
            locator={
                "support_ids": sorted(support_ids),
                "seed_ids": list(action.seed_ids),
                "retrieval_text": action.retrieval_text,
            },
            insight_id=identifier,
            insight_revision=revision,
        )
        published.append((identifier, revision))
    outcome = (
        "insight"
        if published or any(action.action == "NO_CHANGE" for action, _ in prepared)
        else "no_insight"
    )
    store.mark_l2_group(connection, seeds, outcome)
    store.readiness(connection, insights_ready=False)
    return published


def consolidate_source(store, models, new_fact_ids):
    while True:
        started = time.monotonic()
        with store.transaction() as connection:
            store.guard(connection)
            examined, last_outcome = store.l2_progress(connection)
            group = (
                _pending_group(store, connection, new_fact_ids, examined) if new_fact_ids else []
            )
            if not group:
                store.readiness(connection, insights_ready=True, insight_outcome=last_outcome)
                return
            facts, seeds, targets, private_targets = _freeze_group(store, connection, group)
        inputs = {
            "system": INCREMENTAL_INSIGHT_SYSTEM,
            "envelope": consolidation_envelope(facts, seeds, targets),
            "generation_id": store.generation_id,
            "config": {
                "index": store.config.index.model_dump(mode="json"),
                "extraction": store.config.extraction.model_dump(mode="json"),
            },
        }
        print(
            json.dumps(
                {
                    "stage": "insight_lookup",
                    "operation_id": store.claim.operation_id,
                    "generation_id": store.generation_id,
                    "seed_count": len(seeds),
                    "fact_count": len(facts),
                    "target_count": len(targets),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                }
            ),
            flush=True,
        )
        if len(facts) < 2 and not targets:
            with store.transaction() as connection:
                store.guard(connection)
                store.mark_l2_group(connection, seeds, "no_insight")
            continue
        result = ConsolidationResult.model_validate(
            store.checkpoint(
                "insight",
                inputs,
                lambda facts=facts, seeds=seeds, targets=targets: consolidate_insights(
                    models,
                    facts=facts,
                    seeds=seeds,
                    targets=targets,
                    config=store.config,
                    timeout_seconds=store.config.model.attempt_timeout_seconds,
                ),
            )
        )
        prepared = _prepared_actions(store, models, result, private_targets)
        with store.transaction() as connection:
            published = _publish_group(
                store,
                connection,
                facts=facts,
                seeds=seeds,
                targets=targets,
                prepared=prepared,
                digest=input_digest(inputs),
            )
        print(
            json.dumps(
                {
                    "stage": "insight_published",
                    "operation_id": store.claim.operation_id,
                    "generation_id": store.generation_id,
                    "published_count": len(published),
                    "published": [
                        {"insight_id": identifier, "revision": revision}
                        for identifier, revision in published
                    ],
                    "seed_count": len(seeds),
                    "actions": [action.action for action in result.actions],
                    "usage": result.usage.model_dump(mode="json"),
                    "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                }
            ),
            flush=True,
        )
