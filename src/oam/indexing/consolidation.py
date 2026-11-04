"""Bounded incremental Insight lookup and fenced revision publication."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from threading import Lock

from psycopg import sql
from psycopg.rows import dict_row

from oam.db.ann import configure_ann
from oam.db.eligibility import ELIGIBLE_UNITS, ELIGIBLE_UNITS_CANDIDATE
from oam.indexing.extraction import (
    INCREMENTAL_INSIGHT_SYSTEM,
    ConsolidationResult,
    InsightInput,
    consolidate_insights,
    consolidation_envelope,
    insight_annotations,
    model_input_tokens,
)
from oam.indexing.identity import INSIGHT_MODEL_RECIPE
from oam.indexing.store import MEMORY_REVISION, StaleConsolidationInputs, input_digest, stable_id
from oam.retrieval.repository import RecallRepository

INSIGHT_PROTECTED_TARGET_RANKS = 2


class InsightLookup(RecallRepository):
    """Reuse direct recall ranking with Insight eligibility before each limit."""

    def __init__(self, connection, authority, config, *, generation_id, deadline):
        self.generation_predicate = "u.generation_id=" + sql.Literal(generation_id).as_string()
        super().__init__(connection, authority, config, deadline=deadline)

    @property
    def lexical_candidate_predicate(self) -> str:
        return "u.kind='insight' AND " + self.generation_predicate

    @property
    def candidate_units_sql(self) -> str:
        return ELIGIBLE_UNITS_CANDIDATE + " AND u.kind='insight' AND " + self.generation_predicate

    def _eligible(self, *, temporal=False, candidate_facts=False) -> str:
        return (
            "WITH eligible AS NOT MATERIALIZED ("
            + self.candidate_units_sql
            + "), filtered AS NOT MATERIALIZED (SELECT * FROM eligible) "
        )


def _rows(connection, query, parameters=(), *, prepare=None):
    with connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query, parameters, prepare=prepare)
        return cursor.fetchall()


def _vector(value):
    return json.loads(value) if isinstance(value, str) else list(value)


def _set_preparation_timeout(connection, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Insight preparation deadline exhausted")
    connection.execute(
        "SELECT set_config('statement_timeout',%s,true)",
        (str(max(1, int(remaining * 1000))),),
    )


def _pending_group(store, connection, new_fact_ids, examined):
    return _rows(
        connection,
        f"WITH eligible AS ({ELIGIBLE_UNITS}), pending AS MATERIALIZED ("
        "SELECT * FROM eligible WHERE kind='fact' AND source_id=%s AND source_version=%s "
        "AND generation_id=%s AND unit_id=ANY(%s) AND NOT (unit_id=ANY(%s))), "
        "anchor AS (SELECT * FROM pending ORDER BY created_at,unit_id LIMIT 1) "
        "SELECT p.*,p.embedding::text AS vector FROM pending p CROSS JOIN anchor a "
        "WHERE p.unit_id=a.unit_id OR "
        "1-(p.embedding <=> a.embedding)>=%s "
        "ORDER BY (p.unit_id=a.unit_id) DESC,p.embedding <=> a.embedding,p.created_at,p.unit_id "
        "LIMIT %s",
        (
            store.source["source_id"],
            store.source["source_version"],
            store.generation_id,
            new_fact_ids,
            list(examined),
            store.config.index.semantic_link_min_similarity,
            min(store.config.index.insight_seed_max, store.config.extraction.max_facts_per_group),
        ),
    )


def _lookup_targets(store, connection, seeds, *, deadline=None):
    limit = store.config.index.insight_candidate_max
    repository = InsightLookup(
        connection,
        store.authority,
        store.config,
        generation_id=store.generation_id,
        deadline=(
            deadline
            if deadline is not None
            else time.monotonic() + store.config.model.attempt_timeout_seconds
        ),
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
    if deadline is not None:
        _set_preparation_timeout(connection, deadline)
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
        "WHERE u.kind='insight' AND u.generation_id=%s AND u.unit_id=ANY(%s) "
        "ORDER BY array_position(%s,u.unit_id)",
        (store.generation_id, identifiers, identifiers),
    )


def _fact_context(store, connection, seeds, targets, *, seed_only=False):
    seed_ids = [seed["unit_id"] for seed in seeds]
    support_ids = sorted({support["id"] for target in targets for support in target["supports"]})
    eligible = (
        ELIGIBLE_UNITS + " AND u.generation_id=%s" + (" AND u.unit_id=ANY(%s)" if seed_only else "")
    )
    configure_ann(connection, store.config)
    return _rows(
        connection,
        f"WITH eligible AS NOT MATERIALIZED ({eligible}), seeds AS MATERIALIZED ("
        "SELECT embedding FROM eligible WHERE kind='fact' AND unit_id=ANY(%s)), "
        "priority AS MATERIALIZED (SELECT * FROM eligible WHERE kind='fact' "
        "AND (unit_id=ANY(%s) OR memory_id=ANY(%s))), "
        "priority_count AS MATERIALIZED (SELECT count(*) AS total FROM priority), "
        "candidate_ids AS (SELECT unit_id FROM priority UNION "
        "SELECT n.unit_id FROM seeds seed CROSS JOIN LATERAL ("
        "SELECT u.unit_id FROM search_units u WHERE u.tenant_id=%s AND u.user_id=%s "
        "AND u.kind='fact' AND (SELECT total FROM priority_count)<%s "
        "AND (SELECT true FROM eligible qualified WHERE "
        "(qualified.tenant_id,qualified.user_id,qualified.unit_id)="
        "(u.tenant_id,u.user_id,u.unit_id)) IS TRUE "
        "ORDER BY u.embedding <=> seed.embedding LIMIT %s) n), "
        "candidates AS MATERIALIZED (SELECT * FROM eligible WHERE kind='fact' "
        "AND unit_id IN (SELECT unit_id FROM candidate_ids)), "
        "candidate_annotations AS (SELECT candidate.*,"
        "(SELECT jsonb_agg(ut.detail ORDER BY ut.annotation_id) FROM unit_times ut WHERE "
        "(ut.tenant_id,ut.user_id,ut.unit_id)="
        "(candidate.tenant_id,candidate.user_id,candidate.unit_id)) AS annotations "
        "FROM candidates candidate), ranked AS ("
        "SELECT candidate.*, (SELECT min(candidate.embedding <=> seeds.embedding) "
        "FROM seeds) AS distance FROM candidate_annotations candidate) "
        "SELECT * FROM ranked WHERE unit_id=ANY(%s) OR memory_id=ANY(%s) "
        "OR 1-distance>=%s ORDER BY (unit_id=ANY(%s)) DESC,"
        "(memory_id=ANY(%s)) DESC,distance,unit_id LIMIT %s",
        (store.generation_id,)
        + ((seed_ids,) if seed_only else ())
        + (
            seed_ids,
            seed_ids,
            support_ids,
            store.authority.tenant_id,
            store.authority.user_id,
            store.config.extraction.max_facts_per_group,
            store.config.extraction.max_facts_per_group,
            seed_ids,
            support_ids,
            store.config.index.semantic_link_min_similarity,
            seed_ids,
            support_ids,
            store.config.extraction.max_facts_per_group,
        ),
        prepare=True,
    )


def _fact_input(row):
    return InsightInput(
        id=row["memory_id"],
        text=row["content_text"],
        source_refs=(f"{row['source_id']}:{row['source_version']}",),
        mentioned_at=row["mentioned_at"],
        message_role=row["locator"].get("role"),
        temporal_annotations=insight_annotations(row["annotations"]),
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


def _seed_target_candidates(store, connection, seeds, *, deadline=None):
    return [_lookup_targets(store, connection, [seed], deadline=deadline) for seed in seeds]


def _merge_target_candidates(ranked, limit):
    targets, seen = [], set()
    for level in zip_longest(*ranked):
        for target in level:
            if target is not None and target["id"] not in seen:
                seen.add(target["id"])
                targets.append(target)
                if len(targets) == limit:
                    return targets
    return targets


def _freeze_group(store, connection, seeds, *, initial_candidates=None, deadline=None):
    ranked = (
        _seed_target_candidates(store, connection, seeds, deadline=deadline)
        if initial_candidates is None
        else initial_candidates
    )
    history_loads_avoided = 0
    while seeds:
        if deadline is not None:
            _set_preparation_timeout(connection, deadline)
        candidates = ranked[: len(seeds)]
        targets = _merge_target_candidates(candidates, store.config.index.insight_candidate_max)
        required = {rows[0]["id"] for rows in candidates if rows}
        if not required <= {target["id"] for target in targets}:
            seeds = seeds[:-1]
            continue
        candidate_count = len(targets)
        while True:
            public_targets = tuple(
                {key: value for key, value in target.items() if key != "vector"}
                for target in targets
            )
            seed_ids = tuple(seed["memory_id"] for seed in seeds)
            if deadline is not None:
                _set_preparation_timeout(connection, deadline)
            minimum = tuple(
                _fact_input(row)
                for row in _fact_context(store, connection, seeds, targets, seed_only=True)
            )
            packed = _pack_facts(minimum, seed_ids, public_targets, store.config)
            if packed is None:
                history_loads_avoided += 1
            if packed is not None:
                if deadline is not None:
                    _set_preparation_timeout(connection, deadline)
                facts = tuple(
                    _fact_input(row) for row in _fact_context(store, connection, seeds, targets)
                )
                packed = _pack_facts(facts, seed_ids, public_targets, store.config)
            if packed is not None:
                if len(targets) != candidate_count:
                    print(
                        json.dumps(
                            {
                                "stage": "insight_target_admission",
                                "operation_id": store.claim.operation_id,
                                "candidate_count": candidate_count,
                                "admitted_count": len(targets),
                                "max_input_tokens": store.config.extraction.max_input_tokens,
                                "history_loads_avoided": history_loads_avoided,
                                "reason": "complete_input_budget",
                            }
                        ),
                        flush=True,
                    )
                return packed, seed_ids, public_targets, targets
            if len(seeds) > 1 or len(targets) <= 1:
                break
            targets = targets[:-1]
        seeds = seeds[:-1]
    raise ValueError("Insight input budget cannot fit one seed and its complete target context")


def _pending_source_batch(store, connection, new_fact_ids, examined, *, config=None):
    config = config or store.config
    return _rows(
        connection,
        f"WITH eligible AS ({ELIGIBLE_UNITS}) "
        "SELECT *,embedding::text AS vector FROM eligible WHERE kind='fact' "
        "AND source_id=%s AND source_version=%s AND generation_id=%s AND unit_id=ANY(%s) "
        "AND NOT (unit_id=ANY(%s)) ORDER BY created_at,unit_id LIMIT %s",
        (
            store.source["source_id"],
            store.source["source_version"],
            store.generation_id,
            new_fact_ids,
            list(examined),
            min(config.index.insight_seed_max, config.extraction.max_facts_per_group),
        ),
    )


def _source_seed_targets(store, connection, seeds, deadline, blocked_targets):
    def primary_lookup(seed):
        return _lookup_targets(store, connection, [seed], deadline=deadline)

    if (
        blocked_targets
        or store.pool is None
        or store.config.index.insight_generation_concurrency == 1
    ):
        ranked = []
        for seed in seeds:
            targets = primary_lookup(seed)
            if {
                target["id"] for target in targets[:INSIGHT_PROTECTED_TARGET_RANKS]
            } & blocked_targets:
                return None
            ranked.append(targets)
        return ranked

    read_lock = Lock()
    active_reads = peak_reads = 0

    def read(seed):
        nonlocal active_reads, peak_reads
        with store.transaction(nonblocking_read=True, deadline=deadline) as reader:
            if reader is None:
                return None
            with read_lock:
                active_reads += 1
                peak_reads = max(peak_reads, active_reads)
            try:
                return _lookup_targets(store, reader, [seed], deadline=deadline)
            finally:
                with read_lock:
                    active_reads -= 1

    cap = min(store.config.index.insight_generation_concurrency, len(seeds))
    ranked, next_submission, admitted, fallbacks = [], 1, 0, 0
    with ThreadPoolExecutor(max_workers=cap) as executor:
        futures = {}

        def fill():
            nonlocal next_submission
            while len(futures) < cap - 1 and next_submission < len(seeds):
                futures[next_submission] = executor.submit(read, seeds[next_submission])
                next_submission += 1

        fill()
        try:
            for position, seed in enumerate(seeds):
                targets = (
                    primary_lookup(seed)
                    if position == 0 or cap == 1
                    else futures.pop(position).result()
                )
                if targets is None:
                    fallbacks += 1
                    targets = primary_lookup(seed)
                elif position > 0 and cap > 1:
                    admitted += 1
                ranked.append(targets)
                fill()
        finally:
            for future in futures.values():
                future.cancel()
    print(
        json.dumps(
            {
                "stage": "insight_source_lookup",
                "operation_id": store.claim.operation_id,
                "seeds": len(seeds),
                "admitted_reads": admitted,
                "serial_fallbacks": fallbacks,
                "reader_peak": peak_reads,
                "active_after": active_reads,
                "indexing_db_cap": store.config.database.indexing_concurrency,
            }
        ),
        flush=True,
    )
    return ranked


def _freeze_source_batch(
    store, connection, seeds, *, deadline, blocked_targets=frozenset(), config=None
):
    config = config or store.config
    initial_count = len(seeds)
    ranked = _source_seed_targets(store, connection, seeds, deadline, blocked_targets)
    if ranked is None:
        return None
    while seeds:
        targets = []
        for level in zip_longest(*(rows[:INSIGHT_PROTECTED_TARGET_RANKS] for rows in ranked)):
            for target in level:
                if target is not None and target["id"] not in {row["id"] for row in targets}:
                    targets.append(target)

        def public(rows):
            return tuple({k: v for k, v in target.items() if k != "vector"} for target in rows)

        seed_ids = tuple(seed["memory_id"] for seed in seeds)
        _set_preparation_timeout(connection, deadline)
        minimum = tuple(
            _fact_input(row)
            for row in _fact_context(store, connection, seeds, targets, seed_only=True)
        )

        def fits(rows, required=minimum, required_ids=seed_ids):
            return _pack_facts(required, required_ids, public(rows), config) is not None

        if len(targets) <= config.index.insight_candidate_max and fits(targets):
            break
        seeds, ranked = seeds[:-1], ranked[:-1]
    if not seeds:
        raise ValueError("One seed and its complete protected targets exceed input budget")
    protected_count = len(targets)
    for level in zip_longest(*(rows[INSIGHT_PROTECTED_TARGET_RANKS:] for rows in ranked)):
        for target in level:
            if target is None or target["id"] in {row["id"] for row in targets}:
                continue
            if len(targets) < config.index.insight_candidate_max and fits([*targets, target]):
                targets.append(target)
    if {target["id"] for target in targets} & blocked_targets:
        return None
    public_targets = public(targets)
    _set_preparation_timeout(connection, deadline)
    facts = tuple(_fact_input(row) for row in _fact_context(store, connection, seeds, targets))
    packed = _pack_facts(facts, seed_ids, public_targets, config)
    if packed is None:
        raise StaleConsolidationInputs("Protected source batch became ineligible")
    print(
        json.dumps(
            {
                "stage": "insight_source_batch",
                "operation_id": store.claim.operation_id,
                "initial_seeds": initial_count,
                "admitted_seeds": len(seed_ids),
                "protected_targets": protected_count,
                "targets": len(targets),
                "facts": len(packed),
            }
        ),
        flush=True,
    )
    return packed, seed_ids, public_targets, targets


def _source_groups(store, connection, new_fact_ids, examined, *, deadline, config=None):
    selected, target_ids, fact_ids, reserved = [], set(), set(), set(examined)
    for _ in range(store.config.index.insight_generation_concurrency):
        _set_preparation_timeout(connection, deadline)
        seeds = _pending_source_batch(store, connection, new_fact_ids, reserved, config=config)
        if not seeds:
            break
        frozen = _freeze_source_batch(
            store, connection, seeds, deadline=deadline, blocked_targets=target_ids, config=config
        )
        if frozen is None:
            break
        targets, facts = {target["id"] for target in frozen[2]}, {fact.id for fact in frozen[0]}
        if targets & target_ids or facts & fact_ids:
            break
        selected.append(frozen)
        target_ids.update(targets)
        fact_ids.update(facts)
        reserved.update(seed["unit_id"] for seed in seeds if seed["memory_id"] in frozen[1])
    return selected


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
        "WHERE kind='fact' AND generation_id=%s AND memory_id=ANY(%s)",
        (store.generation_id, sorted(required_support)),
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


def _parallel_groups(store, connection, new_fact_ids, examined, *, deadline=None):
    """Reserve a bounded lookahead and select non-overlapping frozen inputs."""
    cap = store.config.index.insight_generation_concurrency
    if deadline is None:
        deadline = time.monotonic() + store.config.model.attempt_timeout_seconds
    reserved, pending = set(examined), []
    selected, target_ids, fact_ids = [], set(), set()
    for _ in range(2 * cap):
        _set_preparation_timeout(connection, deadline)
        group = _pending_group(store, connection, new_fact_ids, reserved)
        if not group:
            break
        reserved.update(row["unit_id"] for row in group)
        pending.append(group)

    def lookup(group):
        with store.transaction(nonblocking_read=True, deadline=deadline) as read:
            return (
                None
                if read is None
                else _seed_target_candidates(store, read, group, deadline=deadline)
            )

    admitted_reads = fallbacks = next_result = 0
    next_submission = 1
    with ThreadPoolExecutor(max_workers=cap) as executor:
        futures = {}

        def fill():
            nonlocal next_submission
            needed = cap - len(selected) - (next_result == 0)
            while len(futures) < needed and next_submission < len(pending):
                futures[next_submission] = executor.submit(lookup, pending[next_submission])
                next_submission += 1

        fill()
        try:
            while next_result < len(pending) and len(selected) < cap:
                group = pending[next_result]
                if next_result == 0:
                    initial_candidates = _seed_target_candidates(
                        store, connection, group, deadline=deadline
                    )
                else:
                    initial_candidates = futures.pop(next_result).result()
                next_result += 1
                if initial_candidates is None:
                    fallbacks += 1
                    initial_candidates = _seed_target_candidates(
                        store, connection, group, deadline=deadline
                    )
                elif next_result > 1:
                    admitted_reads += 1
                initial_targets = _merge_target_candidates(
                    initial_candidates, store.config.index.insight_candidate_max
                )
                if not ({target["id"] for target in initial_targets} & target_ids):
                    frozen = _freeze_group(
                        store,
                        connection,
                        group,
                        initial_candidates=initial_candidates,
                        deadline=deadline,
                    )
                    facts, _, targets, _ = frozen
                    candidate_targets = {target["id"] for target in targets}
                    candidate_facts = {fact.id for fact in facts}
                    if not (candidate_targets & target_ids or candidate_facts & fact_ids):
                        selected.append(frozen)
                        target_ids.update(candidate_targets)
                        fact_ids.update(candidate_facts)
                fill()
        finally:
            for future in futures.values():
                future.cancel()
    print(
        json.dumps(
            {
                "stage": "insight_target_prefetch",
                "operation_id": store.claim.operation_id,
                "admitted_reads": admitted_reads,
                "serial_fallbacks": fallbacks,
                "indexing_db_cap": store.config.database.indexing_concurrency,
                "selected_groups": len(selected),
            }
        ),
        flush=True,
    )
    return selected


def _group_inputs(store, facts, seeds, targets, private_targets, *, config=None):
    config = config or store.config
    return {
        "recipe": INSIGHT_MODEL_RECIPE,
        "system": INCREMENTAL_INSIGHT_SYSTEM,
        "envelope": consolidation_envelope(facts, seeds, targets),
        "private_facts": [fact.model_dump(mode="json") for fact in facts],
        "private_targets": private_targets,
        "generation_id": store.generation_id,
        "config": {
            "index": config.index.model_dump(
                mode="json",
                exclude={
                    "insight_generation_concurrency",
                    "insight_seed_policy",
                    "insight_source_batch",
                    "generation_token_budget",
                },
            ),
            "extraction": config.extraction.model_dump(mode="json"),
        },
    }


def consolidate_source(store, models, new_fact_ids):
    cap = store.config.index.insight_generation_concurrency
    policy = None
    config = store.config
    while True:
        started = time.monotonic()
        with store.transaction() as connection:
            store.guard(connection)
            examined, last_outcome = store.l2_progress(connection)
            if policy is None:
                policy = store.l2_seed_policy(connection)
                if policy == "source":
                    config = store.l2_source_config(connection)
                print(
                    json.dumps(
                        {
                            "stage": "insight_seed_policy",
                            "operation_id": store.claim.operation_id,
                            "policy": policy,
                            "seed_max": config.index.insight_seed_max,
                            "target_max": config.index.insight_candidate_max,
                            "input_max_tokens": config.extraction.max_input_tokens,
                        }
                    ),
                    flush=True,
                )
            deadline = None
            if policy == "source" or cap > 1:
                remaining = connection.execute(
                    "SELECT EXTRACT(EPOCH FROM lease_expires_at-clock_timestamp()) "
                    "FROM work_units WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s",
                    (*store.key, store.claim.work_unit_id),
                ).fetchone()[0]
                deadline = time.monotonic() + min(
                    float(remaining), store.config.model.attempt_timeout_seconds
                )
            if policy == "source":
                groups = _source_groups(
                    store, connection, new_fact_ids, examined, deadline=deadline, config=config
                )
            elif cap == 1:
                group = (
                    _pending_group(store, connection, new_fact_ids, examined)
                    if new_fact_ids
                    else []
                )
                groups = [_freeze_group(store, connection, group)] if group else []
            else:
                groups = _parallel_groups(
                    store, connection, new_fact_ids, examined, deadline=deadline
                )
            if not groups:
                store.readiness(connection, insights_ready=True, insight_outcome=last_outcome)
                return
        stages, ready = [], []
        for facts, seeds, targets, private_targets in groups:
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
            inputs = _group_inputs(store, facts, seeds, targets, private_targets, config=config)
            ready.append((facts, seeds, targets, private_targets, inputs))
            stages.append(
                (
                    "insight",
                    inputs,
                    lambda facts=facts, seeds=seeds, targets=targets, config=config: (
                        consolidate_insights(
                            models,
                            facts=facts,
                            seeds=seeds,
                            targets=targets,
                            config=config,
                            timeout_seconds=store.config.model.attempt_timeout_seconds,
                        )
                    ),
                )
            )
        prepared_at = time.monotonic()
        if len(stages) == 1:
            stage, inputs, produce = stages[0]
            outputs = [store.checkpoint(stage, inputs, produce)]
        else:
            outputs = store.checkpoints(stages) if stages else []
        failures, prepared_groups = [], []
        preparation_failed = False
        for (facts, seeds, targets, private_targets, inputs), output in zip(
            ready, outputs, strict=True
        ):
            if isinstance(output, Exception):
                failures.append(output)
                continue
            try:
                result = ConsolidationResult.model_validate(output)
                prepared = _prepared_actions(store, models, result, private_targets)
            except Exception as error:
                failures.append(error)
                preparation_failed = True
                continue
            prepared_groups.append((facts, seeds, targets, inputs, result, prepared))
        published_groups = []
        if prepared_groups and not preparation_failed:
            try:
                with store.transaction() as connection:
                    for facts, seeds, targets, inputs, result, prepared in prepared_groups:
                        published = _publish_group(
                            store,
                            connection,
                            facts=facts,
                            seeds=seeds,
                            targets=targets,
                            prepared=prepared,
                            digest=input_digest(inputs),
                        )
                        published_groups.append((seeds, result, published))
            except Exception as error:
                published_groups.clear()
                failures.append(error)
        for seeds, result, published in published_groups:
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
                        "model_actions": list(result.model_actions),
                        "usage": result.usage.model_dump(mode="json"),
                        "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                    }
                ),
                flush=True,
            )
        print(
            json.dumps(
                {
                    "stage": "insight_generation_batch",
                    "operation_id": store.claim.operation_id,
                    "selected_groups": len(stages),
                    "generation_cap": cap,
                    "preparation_ms": round((prepared_at - started) * 1000, 2),
                    "completion_ms": round((time.monotonic() - prepared_at) * 1000, 2),
                    "failures": len(failures),
                }
            ),
            flush=True,
        )
        if failures:
            raise failures[0]
