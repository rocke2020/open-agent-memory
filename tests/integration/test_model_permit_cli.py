"""Operator CLI over isolated permit metadata, with no external model calls."""

import json

import psycopg
import pytest
import yaml
from psycopg.conninfo import make_conninfo
from test_capacity_admission import capacity_database as capacity_database

from oam.capacity import CapacityUnavailable, PostgresCapacity
from oam.cli import main

pytestmark = pytest.mark.integration


@pytest.fixture
def permit_operator(capacity_database, tmp_path, monkeypatch):
    dsn, config = capacity_database
    config = config.model_copy(
        update={"provider": config.provider.model_copy(update={"llm_concurrency": 1})}
    )
    path = tmp_path / "operator.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    monkeypatch.setenv(config.database.migration_dsn_env, dsn)
    capacity = PostgresCapacity(config, make_conninfo(dsn, options="-c role=oam_api"))
    capacity.configure()
    yield dsn, config, path, capacity
    capacity.close()


def run(path, *arguments):
    return main(["model-permits", "--config", str(path), *arguments])


def test_known_completion_frees_permit_and_listing_excludes_secrets(permit_operator, capsys):
    dsn, _, path, capacity = permit_operator
    permit = capacity.acquire_model("llm", background=False, timeout=1)
    permit.dispatched()
    permit.uncertain()
    reserved = capacity.acquire_model("embedding", background=True, timeout=30)
    dispatched = capacity.acquire_model("embedding", background=False, timeout=30)
    dispatched.dispatched()
    with pytest.raises(CapacityUnavailable):
        capacity.acquire_model("llm", background=False, timeout=0.05)

    assert run(path, "list") == 0
    listing = json.loads(capsys.readouterr().out)
    assert {row["state"] for row in listing["permits"]} == {"reserved", "dispatched", "unknown"}
    row = next(row for row in listing["permits"] if row["permit_id"] == str(permit.permit_id))
    assert row["permit_id"] == str(permit.permit_id)
    assert row["state"] == "unknown"
    assert row["background"] is False
    assert set(row) == {
        "permit_id",
        "kind",
        "state",
        "background",
        "created_at",
        "updated_at",
        "expires_at",
        "reconciliation_reason",
    }
    with psycopg.connect(dsn) as connection:
        connection.execute("SET LOCAL ROLE oam_api")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "UPDATE model_permits SET state='released' WHERE permit_id=%s",
                (permit.permit_id,),
            )
    assert (
        run(
            path,
            "reconcile",
            str(permit.permit_id),
            "--known-completed",
            "--reason",
            "synthetic provider confirmed completion",
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["permit"]["state"] == "released"
    assert result["physical_concurrency_verified"] is True
    capacity.acquire_model("llm", background=False, timeout=1).complete()
    reserved.complete()
    dispatched.complete()


def test_forced_unknown_release_preserves_unverified_state(permit_operator, capsys):
    _, _, path, capacity = permit_operator
    permit = capacity.acquire_model("llm", background=False, timeout=1)
    permit.dispatched()
    permit.uncertain()
    assert (
        run(
            path,
            "reconcile",
            str(permit.permit_id),
            "--force-unknown",
            "--reason",
            "synthetic operator accepts uncertainty",
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["permit"]["state"] == "forced_release"
    assert result["physical_concurrency_verified"] is False
    assert run(path, "list") == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["permits"][0]["state"] == "forced_release"
    assert listing["physical_concurrency_verified"] is False
    capacity.acquire_model("llm", background=False, timeout=1).complete()


@pytest.mark.parametrize("occupied", ["reserved", "dispatched", "unknown", "http", "indexing"])
def test_reconfigure_rejects_live_admission_then_applies_canonical_limits(
    permit_operator, occupied, capsys
):
    dsn, config, path, capacity = permit_operator
    if occupied == "indexing":
        permit = psycopg.connect(dsn)
        assert permit.execute("SELECT oam_indexing_db_admit(4)").fetchone()[0] is not None
    elif occupied == "http":
        permit = capacity.try_http(write=False)
        assert permit is not None
    else:
        permit = capacity.acquire_model("llm", background=False, timeout=30)
        if occupied in {"dispatched", "unknown"}:
            permit.dispatched()
        if occupied == "unknown":
            permit.uncertain()
    updated = config.model_copy(
        update={"provider": config.provider.model_copy(update={"llm_concurrency": 3})}
    )
    path.write_text(yaml.safe_dump(updated.model_dump(mode="json")))
    try:
        assert run(path, "configure") == 2
        assert "active" in json.loads(capsys.readouterr().err)["error"]
    finally:
        permit.close() if occupied in {"http", "indexing"} else permit.complete()
    assert run(path, "configure") == 0
    result = json.loads(capsys.readouterr().out)
    assert result["limits"] == {
        "http": 10,
        "writes": 6,
        "llm": 3,
        "embedding": 8,
        "background_embedding": 8,
        "user_claims": 2,
        "pending_roots": 1000,
        "indexing_db": 4,
    }
    replacement = PostgresCapacity(updated, make_conninfo(dsn, options="-c role=oam_api"))
    replacement.configure()
    permits = [replacement.acquire_model("llm", background=False, timeout=1) for _ in range(3)]
    with pytest.raises(CapacityUnavailable):
        replacement.acquire_model("llm", background=False, timeout=0.05)
    for permit in permits:
        permit.complete()
