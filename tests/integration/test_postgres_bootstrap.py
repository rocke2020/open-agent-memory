"""Bootstrap tests create isolated databases and retain them for diagnosis."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from oam.auth import bootstrap
from oam.auth.credentials import digest_secret
from oam.db.migrations import apply_migrations

pytestmark = pytest.mark.integration


@pytest.fixture
def database():
    dsn = os.environ.get("OAM_TEST_POSTGRES_DSN")
    if not dsn or os.environ.get("OAM_TEST_POSTGRES_ISOLATED") != "1":
        pytest.skip("requires an explicitly isolated PostgreSQL database")
    name = f"oam_bootstrap_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    isolated_dsn = make_conninfo(dsn, dbname=name)
    apply_migrations(isolated_dsn, Path(__file__).parents[2] / "migrations")
    return isolated_dsn


def test_recovery_commits_replacement_before_secret_handoff(database, tmp_path, monkeypatch):
    with psycopg.connect(database) as connection:
        connection.execute("INSERT INTO tenants VALUES ('tenant', 'active', DEFAULT)")
        connection.execute(
            "INSERT INTO principals (tenant_id, principal_id, role, state) "
            "VALUES ('tenant', 'admin', 'administrator', 'active')"
        )
        connection.execute(
            "INSERT INTO credentials "
            "(tenant_id, credential_id, principal_id, secret_digest, state) "
            "VALUES ('tenant', 'lost', 'admin', %s, 'active')",
            (digest_secret("lost"),),
        )
        connection.execute(
            "UPDATE bootstrap_state SET phase = 'recovery_required', winner_id = 'first', "
            "tenant_id = 'tenant', principal_id = 'admin', credential_id = 'lost'"
        )

    original_write = bootstrap.os.write

    def inspect_then_write(descriptor, content):
        with psycopg.connect(database) as check:
            marker = check.execute(
                "SELECT phase, tenant_id, principal_id, credential_id FROM bootstrap_state"
            ).fetchone()
            assert marker[:3] == ("committed", "tenant", "admin")
            assert marker[3] != "lost"
            assert check.execute(
                "SELECT state FROM credentials WHERE credential_id = 'lost'"
            ).fetchone() == ("revoked",)
            assert check.execute(
                "SELECT secret_digest, expires_at FROM credentials WHERE credential_id = %s",
                (marker[3],),
            ).fetchone() == (digest_secret(content.decode("ascii").strip()), None)
        return original_write(descriptor, content)

    monkeypatch.setattr(bootstrap.os, "write", inspect_then_write)
    destination = tmp_path / "replacement"
    bootstrap._recover_bootstrap(database, destination)

    assert destination.stat().st_mode & 0o777 == 0o600
    with psycopg.connect(database) as check:
        assert check.execute("SELECT phase FROM bootstrap_state").fetchone() == ("handed_off",)


def test_initial_bootstrap_has_one_winner_while_handoff_holds_the_lock(
    database, tmp_path, monkeypatch
):
    initialize = bootstrap.initialize_bootstrap
    handoff_entered = threading.Event()
    release_handoff = threading.Event()
    original_handoff = bootstrap._handoff

    def paused_handoff(connection, descriptor, secret):
        with psycopg.connect(database) as check:
            assert check.execute("SELECT phase FROM bootstrap_state").fetchone() == ("committed",)
            assert check.execute(
                "SELECT p.role, c.secret_digest, c.expires_at FROM credentials c "
                "JOIN principals p USING (tenant_id, principal_id)"
            ).fetchone() == ("administrator", digest_secret(secret), None)
        handoff_entered.set()
        assert release_handoff.wait(5), "test must release the pending handoff"
        original_handoff(connection, descriptor, secret)

    monkeypatch.setattr(bootstrap, "_handoff", paused_handoff)
    first_output, second_output = tmp_path / "first", tmp_path / "second"
    with ThreadPoolExecutor(2) as contenders:
        first = contenders.submit(initialize, database, first_output)
        try:
            assert handoff_entered.wait(5), "winner must reach its committed handoff"
            application_name = f"contender-{uuid4().hex}"
            second = contenders.submit(
                initialize,
                make_conninfo(database, application_name=application_name),
                second_output,
            )
            with psycopg.connect(database, autocommit=True) as check:
                deadline = time.monotonic() + 5
                while True:
                    waiting = check.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE application_name = %s "
                        "AND wait_event = 'advisory'",
                        (application_name,),
                    ).fetchone()[0]
                    if waiting == 1:
                        break
                    assert time.monotonic() < deadline, "second contender must wait for winner"
                    threading.Event().wait(0.01)
        finally:
            release_handoff.set()
        assert first.result(timeout=5) is True
        assert second.result(timeout=5) is False

    assert first_output.stat().st_mode & 0o777 == 0o600
    assert not second_output.exists()
    assert initialize(database) is False
    with psycopg.connect(database) as check:
        assert check.execute("SELECT count(*) FROM tenants").fetchone() == (1,)
        assert check.execute("SELECT count(*) FROM principals").fetchone() == (1,)
        assert check.execute("SELECT count(*) FROM credentials").fetchone() == (1,)
        assert check.execute("SELECT phase FROM bootstrap_state").fetchone() == ("handed_off",)


def test_initial_bootstrap_checks_destination_before_creating_identity(database, tmp_path):
    with pytest.raises(bootstrap.BootstrapTransitionError, match="destination"):
        bootstrap.initialize_bootstrap(database)
    destination = tmp_path / "existing"
    destination.write_text("preserve operator file", encoding="ascii")
    with pytest.raises(FileExistsError):
        bootstrap.initialize_bootstrap(database, destination)

    assert destination.read_text(encoding="ascii") == "preserve operator file"
    with psycopg.connect(database) as check:
        assert check.execute("SELECT phase FROM bootstrap_state").fetchone() == ("uninitialized",)
        assert check.execute("SELECT count(*) FROM tenants").fetchone() == (0,)
        assert check.execute("SELECT count(*) FROM credentials").fetchone() == (0,)


def test_crashed_bootstrap_does_not_remint_and_explicit_recovery_replaces_lost_key(
    database, tmp_path, monkeypatch
):
    original_handoff = bootstrap._handoff

    def crash_after_commit(*args):
        raise KeyboardInterrupt("simulated process interruption")

    monkeypatch.setattr(bootstrap, "_handoff", crash_after_commit)
    with pytest.raises(KeyboardInterrupt):
        bootstrap.initialize_bootstrap(database, tmp_path / "lost")
    with psycopg.connect(database) as check:
        original = check.execute(
            "SELECT phase, tenant_id, principal_id, credential_id FROM bootstrap_state"
        ).fetchone()
        assert original[0] == "committed"

    monkeypatch.setattr(bootstrap, "_handoff", original_handoff)
    for _ in range(2):
        with pytest.raises(bootstrap.BootstrapTransitionError, match="recovery"):
            bootstrap.initialize_bootstrap(database, tmp_path / "never-issued")
    assert not (tmp_path / "never-issued").exists()
    with psycopg.connect(database) as check:
        assert check.execute("SELECT phase FROM bootstrap_state").fetchone() == (
            "recovery_required",
        )
        assert check.execute("SELECT count(*) FROM credentials").fetchone() == (1,)

    destination = tmp_path / "recovered"
    bootstrap._recover_bootstrap(database, destination)
    with psycopg.connect(database) as check:
        current = check.execute(
            "SELECT phase, tenant_id, principal_id, credential_id FROM bootstrap_state"
        ).fetchone()
        assert current[:3] == ("handed_off", *original[1:3])
        assert current[3] != original[3]
        assert check.execute(
            "SELECT state FROM credentials WHERE credential_id = %s", (original[3],)
        ).fetchone() == ("revoked",)
        assert check.execute(
            "SELECT secret_digest FROM credentials WHERE credential_id = %s", (current[3],)
        ).fetchone() == (digest_secret(destination.read_text(encoding="ascii").strip()),)
    with pytest.raises(bootstrap.BootstrapTransitionError):
        bootstrap._recover_bootstrap(database, tmp_path / "not-needed")
    assert not (tmp_path / "not-needed").exists()


def test_failed_handoff_remains_recoverable_without_restoring_the_lost_key(
    database, tmp_path, monkeypatch
):
    original_fsync = bootstrap.os.fsync

    def fail_fsync(descriptor):
        raise OSError("simulated destination failure")

    monkeypatch.setattr(bootstrap.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="destination failure"):
        bootstrap.initialize_bootstrap(database, tmp_path / "initial-failed")
    with pytest.raises(OSError, match="destination failure"):
        bootstrap._recover_bootstrap(database, tmp_path / "recovery-failed")
    with psycopg.connect(database) as check:
        assert check.execute("SELECT phase FROM bootstrap_state").fetchone() == (
            "recovery_required",
        )
        assert check.execute(
            "SELECT state, count(*) FROM credentials GROUP BY state ORDER BY state"
        ).fetchall() == [("active", 1), ("revoked", 1)]

    monkeypatch.setattr(bootstrap.os, "fsync", original_fsync)
    bootstrap._recover_bootstrap(database, tmp_path / "recovery-complete")
    with psycopg.connect(database) as check:
        assert check.execute("SELECT phase FROM bootstrap_state").fetchone() == ("handed_off",)
        assert check.execute(
            "SELECT state, count(*) FROM credentials GROUP BY state ORDER BY state"
        ).fetchall() == [("active", 1), ("revoked", 2)]
