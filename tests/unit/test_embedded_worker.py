import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from types import SimpleNamespace

import httpx
import pytest

from oam.config import ConfigurationError, load_config
from oam.workers import runner

CONFIG_PATH = Path(__file__).parents[2] / "configs/oam.yaml"
ENVIRONMENT = {
    "OAM_WORKER_QUEUE_DSN": "synthetic-queue",
    "OAM_WORKER_CONTENT_DSN": "synthetic-content",
}


@pytest.fixture
def worker_dependencies(monkeypatch):
    capacities = []
    clients = []

    class Capacity:
        def __init__(self, config, dsn):
            self.dsn = dsn
            self.closed = Event()
            capacities.append(self)

        def configure(self):
            pass

        def close(self):
            self.closed.set()

    def models(config, environment, http, *, background, admission, stop_event):
        assert background is True
        assert admission.dsn == "synthetic-queue"
        clients.append(http)
        return SimpleNamespace(_http=http, stop_event=stop_event)

    monkeypatch.setattr(runner, "PostgresCapacity", Capacity, raising=False)
    monkeypatch.setattr(runner, "RuntimeModels", models)
    return capacities, clients


def worker_config(*, shutdown_seconds=2):
    config = load_config(CONFIG_PATH).config
    return config.model_copy(
        update={
            "worker": config.worker.model_copy(update={"max_tasks": 4, "max_model_calls": 1}),
            "user": config.user.model_copy(update={"concurrency": 1}),
            "lease": config.lease.model_copy(update={"shutdown_seconds": shutdown_seconds}),
        }
    )


def test_runner_uses_task_slots_and_drains_before_closing_clients(monkeypatch, worker_dependencies):
    capacities, clients = worker_dependencies
    started = Event()
    release = Event()
    lock = Lock()
    calls = []

    def process(config, queue, content, *, models, worker_id):
        with lock:
            calls.append((queue, content, worker_id))
            if len(calls) == 4:
                started.set()
        assert release.wait(3)
        assert not models._http.is_closed
        return True

    monkeypatch.setattr(runner, "process_one", process)
    worker = runner.WorkerRunner.from_environment(worker_config(), ENVIRONMENT)
    worker.start()
    with ThreadPoolExecutor(1) as supervisor:
        try:
            assert started.wait(1), "user/model limits incorrectly reduced task slots"
            assert worker.healthy
            stopped = supervisor.submit(worker.shutdown)
            assert worker.stop_event.wait(1)
            assert not stopped.done()
            assert not capacities[0].closed.is_set()
        finally:
            release.set()
        assert stopped.result(timeout=2)
    assert len(calls) == 4, "shutdown admitted another claim"
    assert all(call[:2] == ("synthetic-queue", "synthetic-content") for call in calls)
    assert not worker.healthy
    assert capacities[0].closed.is_set()
    assert clients[0].is_closed


def test_shutdown_timeout_keeps_clients_open_until_active_task_finishes(
    monkeypatch, worker_dependencies, capsys
):
    capacities, clients = worker_dependencies
    started = Event()
    release = Event()

    def process(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return True

    monkeypatch.setattr(runner, "process_one", process)
    worker = runner.WorkerRunner.from_environment(worker_config(shutdown_seconds=0.02), ENVIRONMENT)
    worker.start()
    try:
        assert started.wait(1)
        assert not worker.shutdown()
        assert not clients[0].is_closed
        assert not capacities[0].closed.is_set()
        assert not worker.healthy
        assert "incomplete" in capsys.readouterr().err
    finally:
        release.set()
    assert capacities[0].closed.wait(1)
    assert clients[0].is_closed


def test_runner_model_initialization_failure_closes_owned_resources(
    monkeypatch, worker_dependencies
):
    capacities, _ = worker_dependencies
    clients = []
    client_factory = runner.httpx.Client

    def http_client(**kwargs):
        client = client_factory(**kwargs)
        clients.append(client)
        return client

    def broken_models(*args, **kwargs):
        raise ConfigurationError("invalid model configuration")

    monkeypatch.setattr(runner.httpx, "Client", http_client)
    monkeypatch.setattr(runner, "RuntimeModels", broken_models)
    with pytest.raises(ConfigurationError, match="invalid model configuration"):
        runner.WorkerRunner.from_environment(worker_config(), ENVIRONMENT)
    assert clients[0].is_closed
    assert capacities[0].closed.is_set()


def test_startup_failure_stops_submitted_tasks_and_eventually_closes_clients(
    monkeypatch, worker_dependencies
):
    capacities, clients = worker_dependencies

    class FailingExecutor(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            if self._threads:
                raise RuntimeError("thread startup failed")
            return super().submit(*args, **kwargs)

    def process(*args, models, **kwargs):
        assert models.stop_event.wait(2)
        assert not models._http.is_closed
        return True

    monkeypatch.setattr(runner, "ThreadPoolExecutor", FailingExecutor)
    monkeypatch.setattr(runner, "process_one", process)
    worker = runner.WorkerRunner.from_environment(worker_config(), ENVIRONMENT)
    with pytest.raises(RuntimeError, match="thread startup failed"):
        worker.start()
    assert capacities[0].closed.wait(1), "failed startup left an unowned worker"
    assert clients[0].is_closed
    assert not worker.healthy


def test_unexpected_task_exit_makes_runner_unhealthy(monkeypatch, worker_dependencies):
    capacities, clients = worker_dependencies

    def process(*args, **kwargs):
        raise SystemExit("synthetic task termination")

    monkeypatch.setattr(runner, "process_one", process)
    worker = runner.WorkerRunner.from_environment(worker_config(), ENVIRONMENT)
    worker.start()
    assert capacities[0].closed.wait(1)
    assert worker.failed
    assert not worker.healthy
    assert clients[0].is_closed


def test_shutdown_does_not_wait_for_cancelled_pollers(monkeypatch, worker_dependencies):
    capacities, _ = worker_dependencies
    started = Event()

    def process(*args, models, **kwargs):
        started.set()
        assert models.stop_event.wait(1)
        return True

    monkeypatch.setattr(
        runner, "ThreadPoolExecutor", lambda **kwargs: ThreadPoolExecutor(max_workers=1)
    )
    monkeypatch.setattr(runner, "process_one", process)
    worker = runner.WorkerRunner.from_environment(worker_config(shutdown_seconds=0.1), ENVIRONMENT)
    worker.start()
    assert started.wait(1)
    assert worker.shutdown(), "idle queued pollers were left unnotified after cancellation"
    assert capacities[0].closed.is_set()


async def test_embedded_runner_does_not_block_http_and_stopped_runner_is_visible(
    monkeypatch, worker_dependencies, tmp_path
):
    from oam.api import app as app_module

    opened = Event()
    closed = Event()
    started = Event()
    release = Event()

    class Runtime:
        capacity = None

        def open(self):
            opened.set()

        def close(self):
            closed.set()

        def choose_auto_auth_mode(self):
            return "dev"

    monkeypatch.setattr(app_module.DatabaseRuntime, "from_dsn", lambda *args: Runtime())
    monkeypatch.setenv("OAM_API_DSN", "synthetic-api")
    monkeypatch.delenv("OAM_ROOT_API_KEY", raising=False)
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)

    def process(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return True

    monkeypatch.setattr(runner, "process_one", process)
    app = app_module.create_app(CONFIG_PATH)
    async with app.router.lifespan_context(app):
        try:
            assert opened.is_set()
            assert await asyncio.to_thread(started.wait, 1)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await asyncio.wait_for(client.get("/health/live"), timeout=0.5)
                assert response.status_code == 200
                assert (await client.get("/health/ready")).json()["embedded_worker_running"]
                app.state.worker.request_stop()
                readiness = await client.get("/health/ready")
                assert readiness.status_code == 503
                assert readiness.json()["reason"] == "embedded_worker_unavailable"
        finally:
            release.set()
    assert closed.is_set()


async def test_api_disabled_embedded_runner_needs_no_worker_credentials(monkeypatch, tmp_path):
    from oam.api import app as app_module

    path = tmp_path / "api-only.yaml"
    path.write_text("worker:\n  embedded_enabled: false\n")
    monkeypatch.delenv("OAM_API_DSN", raising=False)
    monkeypatch.delenv("OAM_ROOT_API_KEY", raising=False)
    for name in ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    app = app_module.create_app(path)
    async with app.router.lifespan_context(app):
        assert app.state.worker is None


async def test_api_missing_worker_credentials_closes_opened_api_runtime(monkeypatch):
    from oam.api import app as app_module

    closed = Event()

    class Runtime:
        capacity = None

        def open(self):
            pass

        def close(self):
            closed.set()

        def choose_auto_auth_mode(self):
            return "dev"

    monkeypatch.setattr(app_module.DatabaseRuntime, "from_dsn", lambda *args: Runtime())
    monkeypatch.setenv("OAM_API_DSN", "synthetic-api")
    monkeypatch.delenv("OAM_ROOT_API_KEY", raising=False)
    for name in ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    app = app_module.create_app(CONFIG_PATH)
    with pytest.raises(ConfigurationError, match="OAM_WORKER_QUEUE_DSN"):
        async with app.router.lifespan_context(app):
            pass
    assert closed.is_set()


@pytest.mark.parametrize("boundary", ["_content_call", "_operation_call"])
def test_queue_capacity_error_has_retryable_http_response(boundary):
    from fastapi import HTTPException

    from oam.api import app as app_module
    from oam.db.queue_admission import QueueAdmissionFull

    def full():
        raise QueueAdmissionFull("user queue capacity is full")

    with pytest.raises(HTTPException) as caught:
        getattr(app_module, boundary)(full)
    assert caught.value.status_code == 429
    assert caught.value.detail == {"code": "QueueAdmissionFull", "retryable": True}
    assert caught.value.headers == {"Retry-After": "1"}
