"""Startup rejection and database-backed HTTP readiness without external services."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import psycopg
import pytest
import uvicorn
import yaml

from oam.api.app import create_app
from oam.api.runtime import DatabaseRuntime
from oam.cli import main
from oam.config import load_config
from oam.db.migrations import load_migrations
from oam.startup import validate_startup_environment


@pytest.fixture
def startup_environment(monkeypatch):
    for name in (
        "OAM_API_DSN",
        "OAM_MIGRATION_DSN",
        "OAM_WORKER_QUEUE_DSN",
        "OAM_WORKER_CONTENT_DSN",
        "OAM_ROOT_API_KEY",
        "STARTUP_MODEL_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STARTUP_MODEL_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)


def runtime_config(tmp_path: Path, *, embedded: bool = False) -> Path:
    path = tmp_path / "startup.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "auth": {"mode": "dev"},
                "worker": {"embedded_enabled": embedded},
                "models": {
                    "reasoning_effort": {
                        role: "none" for role in ("extraction", "insights", "abstract", "profile")
                    },
                    "vlm": {
                        "protocol": "openai_chat",
                        "model": "synthetic",
                        "model_env": None,
                        "endpoint_env": "STARTUP_MODEL_URL",
                        "api_key_env": None,
                        "capabilities": ["text", "vision"],
                    },
                    "embedding": {
                        "protocol": "openai_compatible",
                        "model": "synthetic",
                        "model_env": None,
                        "endpoint_env": "STARTUP_MODEL_URL",
                        "api_key_env": None,
                        "capabilities": ["embeddings"],
                    },
                },
            }
        )
    )
    return path


def test_api_command_rejects_missing_database_before_serving(tmp_path, startup_environment, capsys):
    path = runtime_config(tmp_path)

    assert main(["api", "--config", str(path)]) == 2
    assert "OAM_API_DSN" in capsys.readouterr().err


def test_api_command_rejects_malformed_database_without_exposing_credentials(
    tmp_path, startup_environment, monkeypatch, capsys
):
    path = runtime_config(tmp_path)
    monkeypatch.setenv("OAM_API_DSN", "invalid_option=credential-must-stay-private")

    assert main(["api", "--config", str(path)]) == 2
    output = capsys.readouterr()
    assert "OAM_API_DSN" in output.err
    assert "credential-must-stay-private" not in output.out + output.err


def test_api_command_rejects_unresolved_model_before_serving(
    tmp_path, startup_environment, monkeypatch, capsys
):
    path = runtime_config(tmp_path)
    monkeypatch.setenv("OAM_API_DSN", "dbname=synthetic")
    monkeypatch.delenv("STARTUP_MODEL_URL")

    assert main(["api", "--config", str(path)]) == 2
    assert "STARTUP_MODEL_URL" in capsys.readouterr().err


def test_api_command_rejects_missing_embedded_worker_database(
    tmp_path, startup_environment, monkeypatch, capsys
):
    path = runtime_config(tmp_path, embedded=True)
    monkeypatch.setenv("OAM_API_DSN", "dbname=synthetic")

    assert main(["api", "--config", str(path)]) == 2
    assert "OAM_WORKER_QUEUE_DSN" in capsys.readouterr().err


def test_worker_command_rejects_unresolved_model_before_connecting(
    tmp_path, startup_environment, monkeypatch, capsys
):
    path = runtime_config(tmp_path)
    monkeypatch.setenv("OAM_WORKER_QUEUE_DSN", "dbname=synthetic")
    monkeypatch.setenv("OAM_WORKER_CONTENT_DSN", "dbname=synthetic")
    monkeypatch.delenv("STARTUP_MODEL_URL")

    assert main(["worker", "--once", "--config", str(path)]) == 2
    assert "STARTUP_MODEL_URL" in capsys.readouterr().err


def test_migration_command_rejects_malformed_database_without_exposing_credentials(
    tmp_path, startup_environment, monkeypatch, capsys
):
    path = runtime_config(tmp_path)
    monkeypatch.setenv("OAM_MIGRATION_DSN", "invalid_option=credential-must-stay-private")

    assert main(["migrate", "--config", str(path)]) == 2
    output = capsys.readouterr()
    assert "OAM_MIGRATION_DSN" in output.err
    assert "credential-must-stay-private" not in output.out + output.err


def test_disabled_features_do_not_require_their_model_bindings(tmp_path, startup_environment):
    path = runtime_config(tmp_path)
    values = yaml.safe_load(path.read_text())
    values["index"] = {"insights_enabled": False}
    values["profile"] = {"enabled": False}
    values["models"]["reasoning_effort"].update(insights=None, profile=None)
    path.write_text(yaml.safe_dump(values))

    validate_startup_environment(
        load_config(path).config,
        {"OAM_API_DSN": "dbname=synthetic", "STARTUP_MODEL_URL": "http://127.0.0.1:1/v1"},
        command="api",
    )


class ReadinessConnection:
    def __init__(self, *, missing_migration=None):
        self.missing_migration = missing_migration

    @contextmanager
    def transaction(self):
        yield

    def execute(self, query, params=()):
        if query == "SET TRANSACTION READ ONLY" or "set_config('statement_timeout'" in query:
            return SimpleNamespace(fetchone=lambda: (None,))
        if query == "SELECT version FROM schema_migrations":
            versions = [
                migration.version
                for migration in load_migrations(Path(__file__).parents[2] / "migrations")
            ]
            return SimpleNamespace(
                fetchall=lambda: [
                    (version,) for version in versions if version != self.missing_migration
                ]
            )
        raise AssertionError(f"unexpected readiness query: {query}")


class ReadinessPool:
    def __init__(self, *, unavailable=False, missing_migration=None):
        self.unavailable = unavailable
        self.connection_value = ReadinessConnection(missing_migration=missing_migration)

    @contextmanager
    def connection(self, *, timeout):
        if self.unavailable:
            raise psycopg.OperationalError("database unavailable")
        yield self.connection_value

    def close(self):
        pass


@pytest.mark.parametrize(
    (
        "embedded",
        "worker_healthy",
        "worker_database_ready",
        "unavailable",
        "missing_migration",
        "expected_reason",
    ),
    [
        (False, False, False, False, None, None),
        (True, True, True, False, None, None),
        (False, False, False, True, None, "database_unavailable"),
        (False, False, False, False, 1, "schema_outdated"),
        (True, False, False, False, None, "embedded_worker_unavailable"),
        (True, True, False, False, None, "worker_database_unavailable"),
    ],
)
async def test_readiness_requires_database_schema_and_configured_worker(
    tmp_path,
    startup_environment,
    embedded,
    worker_healthy,
    worker_database_ready,
    unavailable,
    missing_migration,
    expected_reason,
):
    path = runtime_config(tmp_path, embedded=embedded)
    config = load_config(path).config
    pool = ReadinessPool(unavailable=unavailable, missing_migration=missing_migration)
    runtime = DatabaseRuntime(config=config, pool=pool, models=object())
    app = create_app(path, runtime=runtime)
    app.state.worker = (
        SimpleNamespace(healthy=worker_healthy, databases_ready=lambda: worker_database_ready)
        if embedded
        else None
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health/ready")
            live = await client.get("/health/live")
        assert live.status_code == 200
        assert response.status_code == (200 if expected_reason is None else 503)
        payload = response.json()
        assert payload["ready"] is (expected_reason is None)
        assert payload["database"] == ("unavailable" if unavailable else "connected")
        assert payload.get("reason") == expected_reason
        if missing_migration is not None:
            assert payload["missing_migrations"] == [1]
    finally:
        runtime.close()
