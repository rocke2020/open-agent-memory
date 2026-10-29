"""Durable attempt outcomes remain conservative while model stages overlap."""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from test_core_http import core_database as core_database
from test_indexing_retry import accepted_source as accepted_source

from oam.indexing.identity import index_generation
from oam.indexing.store import IndexStore
from oam.workers.claims import claim_one

pytestmark = pytest.mark.integration


@pytest.fixture
def pipeline_store(accepted_source):
    _, dsn, _, config, _ = accepted_source
    with psycopg.connect(make_conninfo(dsn, options="-c role=oam_queue")) as connection:
        claim = claim_one(
            connection.cursor(),
            worker_id="pipeline",
            attempt_id="pipeline-attempt",
            lease_seconds=config.lease.seconds,
            user_concurrency=config.user.concurrency,
        )
    assert claim is not None
    return dsn, IndexStore(
        make_conninfo(dsn, options="-c role=oam_content"),
        config,
        claim,
        "pipeline",
        index_generation(config, os.environ),
    )


def disposition(dsn, store):
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            "SELECT disposition FROM attempts WHERE tenant_id=%s AND user_id=%s AND attempt_id=%s",
            (*store.key, store.claim.attempt_id),
        ).fetchone()[0]


@pytest.mark.parametrize("stage", ["embed:passages", "extract:first", "embed:facts"])
@pytest.mark.parametrize("batched", [False, True])
async def test_successful_sibling_keeps_unknown_attempt_while_another_call_is_in_flight(
    pipeline_store, stage, batched
):
    dsn, store = pipeline_store
    in_flight = Event()
    release = Event()

    def unfinished():
        in_flight.set()
        assert release.wait(5), "test did not drain its owned model stage"
        return {"value": "later", "usage": {}}

    with store.model_pipeline(), ThreadPoolExecutor(max_workers=1) as executor:
        later = executor.submit(store.checkpoint, "extract:later", {"group": 2}, unfinished)
        try:
            assert in_flight.wait(2), "second stage never reached its model boundary"
            assert disposition(dsn, store) == "unknown_external_outcome"
            result = (
                store.checkpoints(
                    [
                        (stage, {"group": 1}, lambda: {"value": "done"}),
                        (stage + ":next", {"group": 3}, lambda: {"value": "next"}),
                    ]
                )[0]
                if batched
                else store.checkpoint(stage, {"group": 1}, lambda: {"value": "done"})
            )
            assert result == {"value": "done"}
            assert not later.done()
            assert disposition(dsn, store) == "unknown_external_outcome"
        finally:
            release.set()
            assert later.result(timeout=2) == {"value": "later", "usage": {}}
        assert disposition(dsn, store) == "unknown_external_outcome"
    assert disposition(dsn, store) == "claimed"


async def test_successful_pipeline_restores_claimed_only_after_its_checkpoints_finish(
    pipeline_store,
):
    dsn, store = pipeline_store
    with store.model_pipeline():
        assert store.checkpoint("embed:passages", ["first"], lambda: {"vectors": [[1, 0]]}) == {
            "vectors": [[1, 0]]
        }
        assert disposition(dsn, store) == "unknown_external_outcome"
    assert disposition(dsn, store) == "claimed"


async def test_failed_pipeline_preserves_unknown_model_outcome(pipeline_store):
    dsn, store = pipeline_store

    def unknown():
        raise httpx.ReadTimeout("synthetic unknown model outcome")

    with (
        pytest.raises(httpx.ReadTimeout, match="synthetic unknown model outcome"),
        store.model_pipeline(),
    ):
        store.checkpoint("extract:first", {"group": 1}, unknown)
    assert disposition(dsn, store) == "unknown_external_outcome"


def test_completed_checkpoint_is_reused_after_recipe_change(pipeline_store):
    dsn, store = pipeline_store
    assert store.checkpoint("example", {"input": "same"}, lambda: {"value": "old"}) == {
        "value": "old"
    }
    changed = store.config.model_copy(
        update={
            "extraction": store.config.extraction.model_copy(
                update={"max_facts_per_group": store.config.extraction.max_facts_per_group - 1}
            )
        }
    )
    updated = IndexStore(
        make_conninfo(dsn, options="-c role=oam_content"),
        changed,
        store.claim,
        "pipeline",
        index_generation(changed, os.environ),
    )
    assert updated.checkpoint("example", {"input": "same"}, lambda: {"value": "new"}) == {
        "value": "old"
    }


def test_recipe_qualified_checkpoint_keeps_plain_input_digest_for_removal(pipeline_store):
    from oam.indexing.store import input_digest

    dsn, store = pipeline_store
    changed = store.config.model_copy(
        update={
            "extraction": store.config.extraction.model_copy(
                update={"max_facts_per_group": store.config.extraction.max_facts_per_group - 1}
            )
        }
    )
    updated = IndexStore(
        make_conninfo(dsn, options="-c role=oam_content"),
        changed,
        store.claim,
        "pipeline",
        index_generation(changed, os.environ),
    )
    updated.checkpoint("insight", {"value": "test"}, lambda: {"value": "complete"})
    with psycopg.connect(dsn) as connection:
        stored = connection.execute(
            "SELECT stage_key,input_digest FROM index_stage_results "
            "WHERE tenant_id=%s AND user_id=%s AND starts_with(stage_key,'insight')",
            store.key,
        ).fetchone()
    assert bytes(stored[1]) == input_digest({"value": "test"})
    assert stored[0].startswith("insight")
