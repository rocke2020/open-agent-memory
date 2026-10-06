"""Deferred semantic reads outside the shared publication lock."""

import psycopg

from oam.db.context import bind_user
from oam.indexing.links import (
    SEMANTIC_LINK_RECIPE,
    SEMANTIC_SEED_BATCH_SIZE,
    propose_semantic_links,
    publish_semantic_links,
)
from oam.indexing.store import StaleIndexWork, input_digest


def _set_claim_timeout(connection, store):
    remaining = connection.execute(
        "SELECT EXTRACT(EPOCH FROM lease_expires_at-clock_timestamp()) "
        "FROM work_units WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s "
        "AND owner_id=%s AND fence=%s AND status='running'",
        (*store.key, store.claim.work_unit_id, store.worker_id, store.claim.fence),
    ).fetchone()
    if remaining is None or remaining[0] <= 0:
        raise StaleIndexWork("semantic claim lease expired")
    timeout_ms = int(min(float(remaining[0]), store.config.model.attempt_timeout_seconds) * 1000)
    if timeout_ms < 1:
        raise StaleIndexWork("semantic claim lease expired")
    connection.execute("SELECT set_config('statement_timeout',%s,true)", (str(timeout_ms),))


def process_semantic_claim(store):
    with store.transaction() as connection:
        store.guard(connection)
        state = connection.execute(
            "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id=%s "
            "AND operation_id=%s",
            (*store.key, store.claim.operation_id),
        ).fetchone()[0]
        manifest = state.get("semantic_link_manifest")
        if (
            not isinstance(manifest, dict)
            or manifest.get("source_id") != store.source["source_id"]
            or manifest.get("source_version") != store.source["source_version"]
            or manifest.get("generation_id") != store.generation_id
            or manifest.get("recipe") != SEMANTIC_LINK_RECIPE
            or input_digest(manifest) != bytes(store.source["input_digest"])
        ):
            raise StaleIndexWork("semantic seed manifest is incompatible")
        seeds = manifest.get("seed_ids")
        if (
            not isinstance(seeds, list)
            or not seeds
            or any(not isinstance(seed, str) or not seed for seed in seeds)
            or seeds != sorted(set(seeds))
        ):
            raise StaleIndexWork("semantic seed manifest is malformed")
        store.readiness(connection, links_ready=False, semantic_links_status="running")
    for offset in range(0, len(seeds), SEMANTIC_SEED_BATCH_SIZE):
        batch = seeds[offset : offset + SEMANTIC_SEED_BATCH_SIZE]
        store.check_retry_claim()
        with psycopg.connect(store.dsn) as connection:
            connection.execute("SET TRANSACTION READ ONLY")
            bind_user(connection.cursor(), store.authority)
            _set_claim_timeout(connection, store)
            proposals = propose_semantic_links(
                connection, store.authority, store.config, store.generation_id, batch
            )
        with store.transaction() as connection:
            _set_claim_timeout(connection, store)
            store.guard(connection)
            publish_semantic_links(
                connection, store.authority, store.config, store.generation_id, batch, proposals
            )
            store.guard(connection)
    store.finish()
