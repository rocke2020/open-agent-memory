from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event, Lock

import httpx
import pytest

from oam.workers import index_source, runner

CONFIG_PATH = Path(__file__).parents[2] / "configs" / "oam.yaml"
LIMITS = (
    ("worker", "max_tasks"),
    ("database", "worker_pool_max"),
)


@pytest.fixture(autouse=True)
def local_worker_capacity(monkeypatch):
    class Capacity:
        def __init__(self, config, dsn):
            pass

        def configure(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(runner, "PostgresCapacity", Capacity)


def test_cleanup_claim_runs_without_loading_deleted_original(monkeypatch):
    class QueueConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def cursor(self):
            return object()

    claim = type("Claim", (), {"stage": "cleanup_source"})()
    calls = []
    monkeypatch.setattr(index_source.psycopg, "connect", lambda _: QueueConnection())
    monkeypatch.setattr(index_source, "claim_one", lambda *args, **kwargs: claim)
    monkeypatch.setattr(
        index_source, "IndexStore", lambda *args, **kwargs: pytest.fail("loaded deleted original")
    )
    monkeypatch.setattr(
        index_source,
        "cleanup_claim",
        lambda dsn, active_claim, worker: calls.append((dsn, active_claim, worker)),
        raising=False,
    )

    config = runner.load_config(CONFIG_PATH).config
    assert index_source.process_one(config, "queue", "content", models=object(), worker_id="worker")
    assert calls == [("content", claim, "worker")]


def configure_worker(monkeypatch, *, limiting_field):
    resolved = runner.load_config(CONFIG_PATH)
    config = resolved.config
    for section, field in LIMITS:
        value = 2 if (section, field) == limiting_field else 4
        config = config.model_copy(
            update={section: getattr(config, section).model_copy(update={field: value})}
        )
    monkeypatch.setattr(runner, "load_config", lambda _: replace(resolved, config=config))
    monkeypatch.setenv("OAM_WORKER_QUEUE_DSN", "synthetic-queue")
    monkeypatch.setenv("OAM_WORKER_CONTENT_DSN", "synthetic-content")
    stopped = Event()
    monkeypatch.setattr(runner, "Event", lambda: stopped)
    monkeypatch.setattr(runner.signal, "signal", lambda *_: None)
    return stopped


@pytest.mark.parametrize("limiting_field", LIMITS)
def test_worker_overlaps_sources_within_each_configured_limit_and_drains(
    monkeypatch, limiting_field
):
    stopped = configure_worker(monkeypatch, limiting_field=limiting_field)
    overlapped = Event()
    exceeded = Event()
    release = Event()
    lock = Lock()
    started = []
    completed = []
    closed_early = []
    clients = []

    def process(config, queue, content, *, models, worker_id):
        with lock:
            started.append(
                (
                    queue,
                    content,
                    worker_id,
                    config.worker.max_model_calls,
                )
            )
            clients.append(models._http)
            if len(started) == 2:
                overlapped.set()
            if len(started) > 2:
                exceeded.set()
        release.wait(5)
        with lock:
            closed_early.append(models._http.is_closed)
            completed.append(worker_id)
        return True

    monkeypatch.setattr(runner, "process_one", process)
    with ThreadPoolExecutor(max_workers=1) as supervisor:
        outcome = supervisor.submit(runner.run_worker_from_environment, CONFIG_PATH)
        try:
            assert overlapped.wait(1), "independent source work never overlapped"
            assert not exceeded.wait(0.05), "configured source concurrency was exceeded"
            stopped.set()
            assert not outcome.done(), "shutdown returned before in-flight sources drained"
            assert not completed
        finally:
            stopped.set()
            release.set()
        assert outcome.result(timeout=2) == 0

    assert len(started) == len(completed) == 2
    assert all(item[:2] == ("synthetic-queue", "synthetic-content") for item in started)
    assert all(item[2].startswith("worker_") for item in started)
    assert not any(closed_early)
    assert all(client.is_closed for client in clients)


def test_failed_claim_releases_capacity_and_does_not_log_private_error(monkeypatch, capsys):
    stopped = configure_worker(monkeypatch, limiting_field=("worker", "max_tasks"))
    overlapped = Event()
    release = Event()
    lock = Lock()
    attempts = 0
    active = 0
    completed = 0

    def process(*args, **kwargs):
        nonlocal attempts, active, completed
        with lock:
            attempts += 1
            if attempts == 1:
                raise RuntimeError("private-provider-response")
            active += 1
            if active == 2:
                overlapped.set()
        release.wait(5)
        with lock:
            active -= 1
            completed += 1
        return True

    monkeypatch.setattr(runner, "process_one", process)
    with ThreadPoolExecutor(max_workers=1) as supervisor:
        outcome = supervisor.submit(runner.run_worker_from_environment, CONFIG_PATH)
        try:
            assert overlapped.wait(2), "the failed claim permanently consumed a worker slot"
        finally:
            stopped.set()
            release.set()
        assert outcome.result(timeout=2) == 0

    assert (attempts, active, completed) == (3, 0, 2)
    error_output = capsys.readouterr().err
    assert "RuntimeError" in error_output
    assert "private-provider-response" not in error_output


def test_failed_claim_reports_cause_type_without_private_provider_error(monkeypatch, capsys):
    configure_worker(monkeypatch, limiting_field=("worker", "max_tasks"))

    def process(*args, **kwargs):
        try:
            raise httpx.ReadError("private-provider-response")
        except httpx.ReadError as error:
            raise RuntimeError("multiple extraction stages failed") from error

    monkeypatch.setattr(runner, "process_one", process)
    assert runner.run_worker_from_environment(CONFIG_PATH, once=True) == 1
    error_output = capsys.readouterr().err
    assert "RuntimeError" in error_output
    assert "ReadError" in error_output
    assert "private-provider-response" not in error_output


def test_idle_worker_checks_for_new_work_within_half_second(monkeypatch):
    configure_worker(monkeypatch, limiting_field=("worker", "max_tasks"))

    class StopAfterWait(Event):
        def __init__(self):
            super().__init__()
            self.waits = []

        def wait(self, timeout=None):
            if timeout is not None:
                self.waits.append(timeout)
                self.set()
            return super().wait(timeout)

    stopped = StopAfterWait()
    monkeypatch.setattr(runner, "Event", lambda: stopped)
    monkeypatch.setattr(runner, "process_one", lambda *args, **kwargs: False)
    assert runner.run_worker_from_environment(CONFIG_PATH) == 0
    assert stopped.waits
    assert max(stopped.waits) <= 0.5


def test_source_claim_uses_the_configured_user_concurrency(monkeypatch):
    class QueueConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def cursor(self):
            return object()

    limits = []

    def claim(cursor, *, worker_id, attempt_id, lease_seconds, user_concurrency):
        limits.append(user_concurrency)
        return None

    config = runner.load_config(CONFIG_PATH).config
    config = config.model_copy(update={"user": config.user.model_copy(update={"concurrency": 3})})
    monkeypatch.setattr(index_source.psycopg, "connect", lambda _: QueueConnection())
    monkeypatch.setattr(index_source, "claim_one", claim)
    assert not index_source.process_one(
        config, "queue", "content", models=object(), worker_id="worker"
    )
    assert limits == [3]
