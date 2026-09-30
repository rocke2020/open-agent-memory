import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from oam.config import load_config
from oam.db.migrations import apply_migrations

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.integration


@pytest.fixture
def capacity_database():
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires explicitly isolated PostgreSQL")
    name = "oam_capacity_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(dsn, dbname=name)
    apply_migrations(dsn, ROOT / "migrations")
    config = load_config(ROOT / "configs/oam.yaml").config
    return dsn, config


def test_http_write_reservation_is_shared_and_released(capacity_database):
    from oam.capacity import PostgresCapacity

    dsn, config = capacity_database
    first = PostgresCapacity(config, make_conninfo(dsn, options="-c role=oam_api"))
    second = PostgresCapacity(config, make_conninfo(dsn, options="-c role=oam_api"))
    first.configure()
    second.configure()
    held = []
    try:
        for _ in range(6):
            permit = first.try_http(write=True)
            assert permit is not None
            held.append(permit)
        assert second.try_http(write=True) is None
        for _ in range(4):
            permit = second.try_http(write=False)
            assert permit is not None
            held.append(permit)
        assert second.try_http(write=False) is None
        held.pop(0).close()
        replacement = second.try_http(write=True)
        assert replacement is not None
        held.append(replacement)
    finally:
        for permit in held:
            permit.close()
        first.close()
        second.close()


def test_api_model_permits_reuse_its_open_connection_pool(capacity_database, monkeypatch):
    from oam.api.runtime import DatabaseRuntime

    dsn, config = capacity_database
    runtime = DatabaseRuntime.from_dsn(config, make_conninfo(dsn, options="-c role=oam_api"))
    runtime.open()
    try:
        assert runtime.capacity is not None
        assert runtime.capacity.pool is runtime.pool

        def unexpected_connect(*args, **kwargs):
            pytest.fail("model permit opened a fresh PostgreSQL connection")

        monkeypatch.setattr("oam.capacity.psycopg.connect", unexpected_connect)
        permit = runtime.capacity.acquire_model("embedding", background=False, timeout=1)
        permit.dispatched()
        permit.complete()
        assert runtime.capacity.status()
    finally:
        runtime.close()


async def test_two_http_replicas_reject_overload_before_application_acceptance(capacity_database):
    import asyncio
    from contextlib import AsyncExitStack
    from types import SimpleNamespace

    import httpx

    from oam.api.admission import RequestAdmissionMiddleware
    from oam.capacity import PostgresCapacity

    dsn, config = capacity_database
    entered = asyncio.Queue()
    releases = {}
    accepted = []

    async def application(scope, receive, send):
        identifier = scope["query_string"].decode()
        accepted.append(identifier)
        releases[identifier] = asyncio.Event()
        await entered.put(identifier)
        await releases[identifier].wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"accepted"})

    tasks = []
    async with AsyncExitStack() as stack:
        clients = []
        for _ in range(2):
            capacity = PostgresCapacity(config, make_conninfo(dsn, options="-c role=oam_api"))
            capacity.configure()
            stack.callback(capacity.close)
            app = RequestAdmissionMiddleware(
                application, runtime=SimpleNamespace(capacity=capacity)
            )
            clients.append(
                await stack.enter_async_context(
                    httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test")
                )
            )
        try:
            for index in range(6):
                tasks.append(asyncio.create_task(clients[index % 2].post(f"/v1/retain?{index}")))
            for _ in range(6):
                await asyncio.wait_for(entered.get(), 2)
            blocked_write = await clients[1].post("/v1/retain?blocked-write")
            assert blocked_write.status_code == 429
            assert blocked_write.json()["detail"]["retryable"] is True
            for index in range(6, 10):
                tasks.append(
                    asyncio.create_task(clients[index % 2].get(f"/v1/capabilities?{index}"))
                )
            for _ in range(4):
                await asyncio.wait_for(entered.get(), 2)
            eleventh = await clients[0].get("/v1/capabilities?eleventh")
            assert eleventh.status_code == 429
            assert eleventh.headers["retry-after"] == "1"
            assert len(accepted) == 10
            assert "eleventh" not in accepted and "blocked-write" not in accepted
            releases["0"].set()
            assert (await tasks[0]).status_code == 200
            tasks.append(asyncio.create_task(clients[1].post("/v1/retain?replacement")))
            assert await asyncio.wait_for(entered.get(), 2) == "replacement"
        finally:
            for release in releases.values():
                release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def test_background_embeddings_leave_search_capacity(capacity_database):
    from oam.capacity import CapacityUnavailable, PostgresCapacity

    dsn, config = capacity_database
    worker = PostgresCapacity(config, make_conninfo(dsn, options="-c role=oam_queue"))
    api = PostgresCapacity(config, make_conninfo(dsn, options="-c role=oam_api"))
    worker.configure()
    api.configure()
    held = []
    try:
        for _ in range(4):
            held.append(worker.acquire_model("embedding", background=True, timeout=1))
        with pytest.raises(CapacityUnavailable):
            worker.acquire_model("embedding", background=True, timeout=0.05)
        for _ in range(4):
            held.append(api.acquire_model("embedding", background=False, timeout=1))
        with pytest.raises(CapacityUnavailable):
            api.acquire_model("embedding", background=False, timeout=0.05)
    finally:
        for permit in held:
            permit.complete()


def test_unknown_generation_permit_survives_owner_and_lease(capacity_database):
    from oam.capacity import CapacityUnavailable, PostgresCapacity

    dsn, config = capacity_database
    config = config.model_copy(
        update={"provider": config.provider.model_copy(update={"llm_concurrency": 1})}
    )
    owner = PostgresCapacity(config, make_conninfo(dsn, options="-c role=oam_queue"))
    owner.configure()
    permit = owner.acquire_model("llm", background=True, timeout=1)
    permit.dispatched()
    permit.uncertain()
    owner.close()
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE model_permits SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE permit_id=%s",
            (permit.permit_id,),
        )
    replacement = PostgresCapacity(config, make_conninfo(dsn, options="-c role=oam_queue"))
    replacement.configure()
    with pytest.raises(CapacityUnavailable):
        replacement.acquire_model("llm", background=True, timeout=0.05)
    with psycopg.connect(dsn) as administrator:
        from oam.capacity import reconcile_model_permit

        reconcile_model_permit(
            administrator,
            permit.permit_id,
            known_completed=True,
            reason="synthetic provider confirmed completion",
        )
    replacement.acquire_model("llm", background=True, timeout=1).complete()


def test_expired_unused_reservation_cannot_dispatch(capacity_database):
    from oam.capacity import CapacityUnavailable, PostgresCapacity

    dsn, config = capacity_database
    capacity = PostgresCapacity(config, make_conninfo(dsn, options="-c role=oam_api"))
    capacity.configure()
    permit = capacity.acquire_model("llm", background=False, timeout=1)
    with psycopg.connect(dsn) as connection:
        connection.execute(
            "UPDATE model_permits SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE permit_id=%s",
            (permit.permit_id,),
        )
    with pytest.raises(CapacityUnavailable):
        permit.dispatched()


def test_late_known_completion_resolves_a_forced_uncertain_release(capacity_database):
    from oam.capacity import PostgresCapacity, reconcile_model_permit

    dsn, config = capacity_database
    capacity = PostgresCapacity(config, dsn)
    capacity.configure()
    permit = capacity.acquire_model("llm", background=False, timeout=1)
    permit.dispatched()
    with psycopg.connect(dsn) as connection:
        reconcile_model_permit(
            connection,
            permit.permit_id,
            known_completed=False,
            reason="synthetic operator acknowledgement",
        )
    assert capacity.status()["physical_concurrency_verified"] is False
    permit.complete()
    assert capacity.status()["physical_concurrency_verified"] is True


def test_shared_limit_bounds_actual_generation_dispatch_and_reuses_capacity(capacity_database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock

    import httpx

    from oam.capacity import PostgresCapacity
    from oam.config import ModelsConfig
    from oam.models.runtime import RuntimeModels

    dsn, config = capacity_database
    config = config.model_copy(
        update={
            "models": ModelsConfig.model_validate(
                {
                    "roles": {"extraction": "synthetic"},
                    "bindings": {
                        "synthetic": {
                            "protocol": "openai_compatible",
                            "model": "synthetic",
                            "endpoint_env": "SYNTHETIC_ENDPOINT",
                            "capabilities": ["chat"],
                        }
                    },
                }
            )
        }
    )
    release = Event()
    saturated = Event()
    exceeded = Event()
    lock = Lock()
    active = maximum = 0

    def respond(request):
        nonlocal active, maximum
        assert request.headers["x-request-id"]
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 32:
                saturated.set()
            if active > 32:
                exceeded.set()
        try:
            assert release.wait(5), "test did not release held model responses"
            return httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}
            )
        finally:
            with lock:
                active -= 1

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        models = []
        for _ in range(5):
            admission = PostgresCapacity(config, make_conninfo(dsn, options="-c role=oam_queue"))
            admission.configure()
            models.append(
                RuntimeModels(
                    config,
                    {"SYNTHETIC_ENDPOINT": "http://synthetic.test"},
                    http,
                    admission=admission,
                    background=True,
                )
            )
        with ThreadPoolExecutor(max_workers=40) as callers:
            futures = [
                callers.submit(
                    models[index % 5].generate,
                    "extraction",
                    system="s",
                    user="u",
                    max_output_tokens=1024,
                    timeout_seconds=8,
                )
                for index in range(40)
            ]
            try:
                assert saturated.wait(4), "independent callers did not reach the global limit"
                assert not exceeded.wait(0.1), "the shared limit allowed a 33rd dispatch"
                with psycopg.connect(dsn) as check:
                    assert (
                        check.execute(
                            "SELECT count(*) FROM model_permits WHERE state='dispatched'"
                        ).fetchone()[0]
                        == 32
                    )
            finally:
                release.set()
            assert all(future.result(timeout=5).content == "{}" for future in futures)
    assert maximum == 32
    assert active == 0
    assert models[0]._admission.status()["llm_occupied"] == 0
