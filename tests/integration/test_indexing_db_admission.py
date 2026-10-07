"""Indexing database work shares capacity without holding it during model calls."""

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from queue import Empty, Queue
from threading import Barrier, Event, Lock
from time import monotonic, sleep

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app as create_app
from psycopg.conninfo import make_conninfo
from test_core_http import ROOT, use_created_user_key
from test_core_http import core_database as core_database

from oam.capacity import CapacityUnavailable
from oam.config import load_config
from oam.indexing.identity import index_generation
from oam.indexing.store import IndexStore
from oam.workers.claims import claim_one

pytestmark = pytest.mark.integration


@pytest.fixture
async def indexing_stores(core_database):
    dsn, _tenant, headers = core_database
    config = load_config(ROOT / "configs/oam.yaml").config
    app = create_app(ROOT / "configs/oam.yaml")
    stores = []
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        for index in range(6):
            client.headers.update(headers)
            user_id = f"indexing-{index}"
            await use_created_user_key(client, user_id)
            response = await client.post(
                "/v1/retain",
                headers={"Idempotency-Key": user_id},
                json={
                    "user_id": user_id,
                    "input_type": "interaction",
                    "messages": [
                        {
                            "role": "user",
                            "content": "I prefer concise updates.",
                            "event_time": "2026-10-06T09:00:00Z",
                            "learning_eligible": True,
                        }
                    ],
                },
            )
            assert response.status_code == 202, response.text
            worker_id = f"indexing-worker-{index}"
            with psycopg.connect(make_conninfo(dsn, options="-c role=oam_queue")) as connection:
                claim = claim_one(
                    connection.cursor(),
                    worker_id=worker_id,
                    attempt_id=f"indexing-attempt-{index}",
                    lease_seconds=config.lease.seconds,
                    user_concurrency=config.user.concurrency,
                )
            assert claim is not None
            stores.append(
                IndexStore(
                    make_conninfo(
                        dsn,
                        options="-c role=oam_content",
                        application_name=f"indexing-replica-{index % 2}",
                    ),
                    config,
                    claim,
                    worker_id,
                    index_generation(config, os.environ),
                )
            )
    return dsn, stores


def test_indexing_transactions_overlap_but_never_exceed_shared_cap(indexing_stores):
    _dsn, stores = indexing_stores
    entered: Queue[tuple[int, int]] = Queue()
    release = [Event() for _ in stores]
    lock = Lock()
    active = maximum = 0

    def hold(index):
        nonlocal active, maximum
        with stores[index].transaction() as connection:
            stores[index].guard(connection)
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                entered.put((index, connection.info.backend_pid))
                assert release[index].wait(5), "test did not release indexing transaction"
            finally:
                with lock:
                    active -= 1

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(hold, index) for index in range(6)]
        try:
            initial = [entered.get(timeout=3) for _ in range(4)]
            assert len({pid for _, pid in initial}) == 4
            with pytest.raises(Empty):
                entered.get(timeout=0.15)
            released, _ = initial[0]
            release[released].set()
            futures[released].result(timeout=2)
            entered.get(timeout=2)
        finally:
            for event in release:
                event.set()
        for future in futures:
            future.result(timeout=3)
    assert maximum == 4
    assert active == 0


def test_admission_deadline_and_rollback_release(indexing_stores):
    _dsn, stores = indexing_stores
    waiting = stores[4]
    waiting.config = waiting.config.model_copy(
        update={"model": waiting.config.model.model_copy(update={"attempt_timeout_seconds": 0.15})}
    )
    with pytest.raises(RuntimeError, match="rollback held transactions"), ExitStack() as held:
        for store in stores[:4]:
            connection = held.enter_context(store.transaction())
            store.guard(connection)
        started = monotonic()
        with pytest.raises(CapacityUnavailable), waiting.transaction():
            pytest.fail("saturated indexing capacity admitted content work")
        assert monotonic() - started < 1
        raise RuntimeError("rollback held transactions")
    with ExitStack() as held:
        for store in stores[:4]:
            connection = held.enter_context(store.transaction())
            store.guard(connection)


def test_local_waiters_do_not_open_more_database_connections(indexing_stores):
    dsn, stores = indexing_stores
    local_dsn = make_conninfo(
        dsn, options="-c role=oam_content", application_name="indexing-local-connections"
    )
    for store in stores:
        store.dsn = local_dsn
    started = Barrier(6, timeout=3)
    entered: Queue[None] = Queue()
    release = Event()

    def hold(store):
        started.wait()
        with store.transaction() as connection:
            store.guard(connection)
            entered.put(None)
            assert release.wait(5), "test did not release local indexing connections"

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = [executor.submit(hold, store) for store in stores]
        try:
            for _ in range(4):
                entered.get(timeout=3)
            with pytest.raises(Empty):
                entered.get(timeout=0.15)
            with psycopg.connect(dsn) as inspector:
                sessions = inspector.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                    "AND application_name='indexing-local-connections'"
                ).fetchone()[0]
            assert sessions == 4, "local admission waiters opened extra PostgreSQL sessions"
        finally:
            release.set()
        for future in futures:
            future.result(timeout=3)


@pytest.mark.parametrize("release", ["commit", "rollback", "close"])
def test_database_slot_releases_with_its_transaction(indexing_stores, release):
    _dsn, stores = indexing_stores
    with ExitStack() as held:
        connections = [held.enter_context(psycopg.connect(store.dsn)) for store in stores[:4]]
        for connection in connections:
            assert connection.execute("SELECT oam_indexing_db_admit(4)").fetchone()[0] is not None
        waiting = held.enter_context(psycopg.connect(stores[4].dsn))
        assert waiting.execute("SELECT oam_indexing_db_admit(4)").fetchone() == (None,)
        getattr(connections[0], release)()
        slot = waiting.execute("SELECT oam_indexing_db_admit(4)").fetchone()[0]
        if release == "close":
            deadline = monotonic() + 1
            while slot is None:
                remaining = deadline - monotonic()
                assert remaining > 0, "closed backend did not release its indexing slot"
                sleep(min(0.01, remaining))
                slot = waiting.execute("SELECT oam_indexing_db_admit(4)").fetchone()[0]
        assert slot is not None


def test_database_admission_rejects_a_different_deployment_limit(indexing_stores):
    _dsn, stores = indexing_stores
    with (
        psycopg.connect(stores[0].dsn) as connection,
        pytest.raises(psycopg.errors.RaiseException, match="capacity configuration mismatch"),
    ):
        connection.execute("SELECT oam_indexing_db_admit(5)")


def test_checkpoint_releases_all_database_slots_before_produce(indexing_stores):
    _dsn, stores = indexing_stores
    store = stores[0]

    def produce():
        # Four other real content transactions can enter while output is produced.
        with ExitStack() as held:
            for other in stores[1:5]:
                connection = held.enter_context(other.transaction())
                other.guard(connection)
        return {"result": "completed"}

    output = store.checkpoint("db-slot-release", {"input": "synthetic"}, produce)
    assert output == {"result": "completed"}
    with store.transaction() as connection:
        saved = connection.execute(
            "SELECT output FROM index_stage_results WHERE tenant_id=%s AND user_id=%s "
            "AND stage_key='db-slot-release'",
            store.key,
        ).fetchone()
        assert saved == ({"result": "completed"},)
