"""Serialized first-credential bootstrap and explicit operator recovery."""

from __future__ import annotations

import os
import secrets
import sys
from contextlib import contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

import psycopg

from oam.auth.credentials import issue_credential
from oam.config import ConfigurationError, load_config


class BootstrapTransitionError(RuntimeError):
    """The requested bootstrap transition is not allowed."""


class BootstrapPhase(StrEnum):
    UNINITIALIZED = "uninitialized"
    COMMITTED = "committed"
    HANDED_OFF = "handed_off"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class BootstrapState:
    phase: BootstrapPhase
    winner_id: str | None

    @classmethod
    def initial(cls) -> BootstrapState:
        return cls(phase=BootstrapPhase.UNINITIALIZED, winner_id=None)


@dataclass(frozen=True, slots=True)
class BootstrapDecision:
    winner: bool
    state: BootstrapState


def claim_bootstrap(state: BootstrapState, *, contender_id: str) -> BootstrapDecision:
    """Choose exactly one winner; committed observers never mint another credential."""
    if not contender_id.strip():
        raise ValueError("contender_id cannot be empty")
    if state.phase is not BootstrapPhase.UNINITIALIZED:
        return BootstrapDecision(winner=False, state=state)
    return BootstrapDecision(
        winner=True,
        state=BootstrapState(phase=BootstrapPhase.COMMITTED, winner_id=contender_id),
    )


def record_handoff(state: BootstrapState, *, contender_id: str) -> BootstrapState:
    _require_winner(state, contender_id)
    return replace(state, phase=BootstrapPhase.HANDED_OFF)


def record_handoff_failure(state: BootstrapState, *, contender_id: str) -> BootstrapState:
    _require_winner(state, contender_id)
    return replace(state, phase=BootstrapPhase.RECOVERY_REQUIRED)


def _require_winner(state: BootstrapState, contender_id: str) -> None:
    if state.phase is not BootstrapPhase.COMMITTED or state.winner_id != contender_id:
        raise BootstrapTransitionError("only the committed bootstrap winner may hand off")


def _operator_dsn(config_path: str | Path) -> str:
    try:
        resolved = load_config(config_path)
    except ConfigurationError:
        raise ConfigurationError("invalid OAM configuration") from None
    dsn_environment_name = resolved.config.database.migration_dsn_env
    dsn = os.environ.get(dsn_environment_name)
    if not dsn:
        raise ConfigurationError(f"missing required environment variable {dsn_environment_name}")
    return dsn


def bootstrap_from_environment(config_path: str | Path) -> int:
    """Initialize after schema migration using the explicit operator destination."""
    try:
        dsn = _operator_dsn(config_path)
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2
    output = os.environ.get("OAM_BOOTSTRAP_OUTPUT")
    try:
        created = initialize_bootstrap(dsn, Path(output) if output else None)
    except (OSError, psycopg.Error, BootstrapTransitionError):
        print(
            "bootstrap failed; inspect OAM_BOOTSTRAP_OUTPUT and bootstrap recovery state",
            file=sys.stderr,
        )
        return 1
    if created:
        print(f"bootstrap credential written to {output}")
    return 0


def initialize_bootstrap(dsn: str, output_path: Path | None = None) -> bool:
    """Commit one default administrator, then hand off its secret exactly once."""
    with _locked_connection(dsn) as connection:
        row = connection.execute(
            "SELECT phase FROM bootstrap_state WHERE singleton = true"
        ).fetchone()
        if row is None:
            raise BootstrapTransitionError("bootstrap state is missing")
        phase = BootstrapPhase(row[0])
        if phase is BootstrapPhase.HANDED_OFF:
            return False
        if phase is BootstrapPhase.COMMITTED:
            _require_recovery(connection)
            raise BootstrapTransitionError("interrupted bootstrap requires operator recovery")
        if phase is BootstrapPhase.RECOVERY_REQUIRED:
            raise BootstrapTransitionError("bootstrap requires operator recovery")
        if output_path is None:
            raise BootstrapTransitionError("bootstrap requires an explicit operator destination")
        descriptor = _reserve_destination(output_path)
        try:
            issued = issue_credential()
            tenant_id = f"tenant_{secrets.token_hex(12)}"
            principal_id = f"principal_{secrets.token_hex(12)}"
            credential_id = f"key_{secrets.token_hex(12)}"
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO tenants (tenant_id, state) VALUES (%s, 'active')", (tenant_id,)
                )
                cursor.execute(
                    "INSERT INTO principals (tenant_id, principal_id, role, state) "
                    "VALUES (%s, %s, 'administrator', 'active')",
                    (tenant_id, principal_id),
                )
                cursor.execute(
                    "INSERT INTO credentials "
                    "(tenant_id, credential_id, principal_id, secret_digest, state) "
                    "VALUES (%s, %s, %s, %s, 'active')",
                    (tenant_id, credential_id, principal_id, issued.digest),
                )
                cursor.execute(
                    "UPDATE bootstrap_state SET phase = 'committed', winner_id = %s, "
                    "tenant_id = %s, principal_id = %s, credential_id = %s, "
                    "committed_at = clock_timestamp() WHERE singleton = true",
                    (secrets.token_hex(16), tenant_id, principal_id, credential_id),
                )
            _handoff(connection, descriptor, issued.secret)
        finally:
            os.close(descriptor)
    return True


def recover_bootstrap_from_environment(config_path: str | Path) -> int:
    """Issue one replacement for the exact default administrator after failed handoff."""
    try:
        dsn = _operator_dsn(config_path)
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 2
    output_environment_name = "OAM_BOOTSTRAP_RECOVERY_OUTPUT"
    output = os.environ.get(output_environment_name)
    if not output:
        print(
            f"missing required environment variable {output_environment_name}",
            file=sys.stderr,
        )
        return 2
    try:
        _recover_bootstrap(dsn, Path(output))
    except (OSError, psycopg.Error, BootstrapTransitionError):
        print("bootstrap recovery failed; inspect the operator destination", file=sys.stderr)
        return 1
    print(f"replacement bootstrap credential written to {output}")
    return 0


def _recover_bootstrap(dsn: str, output_path: Path) -> None:
    with _locked_connection(dsn) as connection:
        row = connection.execute(
            "SELECT phase, tenant_id, principal_id, credential_id FROM bootstrap_state "
            "WHERE singleton = true"
        ).fetchone()
        if row is None or row[0] != BootstrapPhase.RECOVERY_REQUIRED.value:
            raise BootstrapTransitionError("bootstrap recovery requires recovery_required state")
        tenant_id, principal_id, previous_credential_id = row[1], row[2], row[3]
        if not tenant_id or not principal_id or not previous_credential_id:
            raise BootstrapTransitionError("bootstrap administrator identity is missing")
        administrator = connection.execute(
            "SELECT 1 FROM principals p JOIN tenants t ON t.tenant_id = p.tenant_id "
            "WHERE p.tenant_id = %s AND p.principal_id = %s "
            "AND p.role = 'administrator' AND p.state = 'active' AND t.state = 'active'",
            (tenant_id, principal_id),
        ).fetchone()
        if administrator is None:
            raise BootstrapTransitionError("bootstrap administrator is not active")
        descriptor = _reserve_destination(output_path)
        try:
            issued = issue_credential()
            credential_id = f"key_{secrets.token_hex(12)}"
            with connection.transaction(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE credentials SET state = 'revoked', revoked_at = clock_timestamp() "
                    "WHERE tenant_id = %s AND principal_id = %s AND credential_id = %s "
                    "AND state = 'active' RETURNING credential_id",
                    (tenant_id, principal_id, previous_credential_id),
                )
                if cursor.fetchone() is None:
                    raise BootstrapTransitionError("lost bootstrap credential is not active")
                cursor.execute(
                    "INSERT INTO credentials "
                    "(tenant_id, credential_id, principal_id, secret_digest, state) "
                    "VALUES (%s, %s, %s, %s, 'active')",
                    (tenant_id, credential_id, principal_id, issued.digest),
                )
                cursor.execute(
                    "UPDATE bootstrap_state SET phase = 'committed', credential_id = %s, "
                    "winner_id = %s, committed_at = clock_timestamp(), handed_off_at = NULL "
                    "WHERE singleton = true",
                    (credential_id, secrets.token_hex(16)),
                )
            _handoff(connection, descriptor, issued.secret)
        finally:
            os.close(descriptor)


@contextmanager
def _locked_connection(dsn: str):
    # The dedicated session owns the lock across transactions and file handoff.
    # Closing it also releases the lock after a crash or any exception.
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(hashtext('open-agent-memory:bootstrap'))")
        yield connection


def _reserve_destination(output_path: Path) -> int:
    return os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)


def _handoff(connection: psycopg.Connection, descriptor: int, secret: str) -> None:
    try:
        remaining = f"{secret}\n".encode("ascii")
        while remaining:
            written = os.write(descriptor, remaining)
            if written == 0:
                raise OSError("bootstrap destination accepted no bytes")
            remaining = remaining[written:]
        os.fsync(descriptor)
        with connection.transaction():
            connection.execute(
                "UPDATE bootstrap_state SET phase = 'handed_off', "
                "handed_off_at = clock_timestamp() WHERE singleton = true"
            )
    except Exception:
        _require_recovery(connection)
        raise


def _require_recovery(connection: psycopg.Connection) -> None:
    with connection.transaction():
        connection.execute(
            "UPDATE bootstrap_state SET phase = 'recovery_required' WHERE singleton = true"
        )
