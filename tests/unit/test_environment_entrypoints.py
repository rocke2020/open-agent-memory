from pathlib import Path

import pytest

from oam import startup
from oam.auth import bootstrap
from oam.db import migrations
from oam.workers import runner

CONFIG_PATH = Path(__file__).parents[2] / "configs" / "oam.yaml"


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
    monkeypatch.setattr(startup, "validate_startup_environment", lambda *args, **kwargs: None)


def test_migrate_entrypoint_fails_closed_when_configured_dsn_is_missing(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("OAM_MIGRATION_DSN", raising=False)

    result = migrations.migrate_from_environment(CONFIG_PATH)

    assert result == 2
    assert capsys.readouterr().err == "missing required environment variable OAM_MIGRATION_DSN\n"


def test_worker_entrypoint_requires_both_separate_database_roles(monkeypatch, capsys) -> None:
    monkeypatch.setenv("OAM_WORKER_QUEUE_DSN", "postgresql://queue-secret")
    monkeypatch.delenv("OAM_WORKER_CONTENT_DSN", raising=False)

    result = runner.run_worker_from_environment(CONFIG_PATH)

    assert result == 2
    message = capsys.readouterr().err
    assert message == "missing required environment variable OAM_WORKER_CONTENT_DSN\n"
    assert "queue-secret" not in message


def test_worker_once_processes_one_claim_with_separate_roles(monkeypatch):
    monkeypatch.setenv("OAM_WORKER_QUEUE_DSN", "synthetic-queue")
    monkeypatch.setenv("OAM_WORKER_CONTENT_DSN", "synthetic-content")
    calls = []

    def process(config, queue, content, *, models, worker_id):
        calls.append((queue, content, worker_id, models))
        return True

    monkeypatch.setattr(runner, "process_one", process, raising=False)
    assert runner.run_worker_from_environment(CONFIG_PATH, once=True) == 0
    assert len(calls) == 1
    assert calls[0][:2] == ("synthetic-queue", "synthetic-content")
    assert calls[0][2].startswith("worker_")


def test_worker_once_reports_stage_failure_without_secret_exception(monkeypatch, capsys):
    monkeypatch.setenv("OAM_WORKER_QUEUE_DSN", "synthetic-queue")
    monkeypatch.setenv("OAM_WORKER_CONTENT_DSN", "synthetic-content")

    def process(*args, **kwargs):
        raise RuntimeError("private-secret")

    monkeypatch.setattr(runner, "process_one", process, raising=False)
    assert runner.run_worker_from_environment(CONFIG_PATH, once=True) == 1
    assert "RuntimeError" in capsys.readouterr().err


def test_bootstrap_recovery_requires_operator_dsn_and_output_destination(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("OAM_MIGRATION_DSN", "postgresql://operator-secret")
    monkeypatch.delenv("OAM_BOOTSTRAP_RECOVERY_OUTPUT", raising=False)

    result = bootstrap.recover_bootstrap_from_environment(CONFIG_PATH)

    assert result == 2
    message = capsys.readouterr().err
    assert message == ("missing required environment variable OAM_BOOTSTRAP_RECOVERY_OUTPUT\n")
    assert "operator-secret" not in message
