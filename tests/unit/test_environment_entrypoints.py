from pathlib import Path

from oam.auth import bootstrap
from oam.db import migrations
from oam.workers import runner

CONFIG_PATH = Path(__file__).parents[2] / "configs" / "oam.yaml"


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
