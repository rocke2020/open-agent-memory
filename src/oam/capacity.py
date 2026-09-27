"""Deployment admission over PostgreSQL metadata and request-owned sessions."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from oam.config import ConfigurationError, OAMConfig

CAPACITY_POLL_SECONDS = 0.05


class CapacityUnavailable(TimeoutError):
    """No capacity was acquired before the caller's deadline."""


@dataclass
class HTTPPermit:
    connection: psycopg.Connection
    slot: int

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True)
class ModelPermit:
    capacity: PostgresCapacity
    permit_id: UUID

    def _update(self, state: str) -> None:
        updated = self.capacity._execute(
            "SELECT oam_model_permit_update(%s,%s,%s)",
            (self.permit_id, self.capacity.owner_id, state),
        )
        if not updated:
            raise CapacityUnavailable("model reservation is no longer eligible")

    def dispatched(self) -> None:
        self._update("dispatched")

    def complete(self) -> None:
        self._update("released")

    def uncertain(self) -> None:
        self._update("unknown")


class PostgresCapacity:
    def __init__(self, config: OAMConfig, dsn: str) -> None:
        self.config = config
        self.dsn = dsn
        self.owner_id = uuid4()
        self._closed = Event()
        self.limits = {
            "http": config.api.max_concurrent_requests,
            "writes": config.api.max_concurrent_writes,
            "llm": config.provider.llm_concurrency,
            "embedding": config.provider.embedding_concurrency,
            "background_embedding": (
                config.provider.embedding_concurrency - config.provider.embedding_search_reserved
            ),
            "user_claims": config.user.concurrency,
            "pending_roots": config.queue.max_pending_per_user,
        }

    def _execute(self, query: str, params: tuple = (), *, timeout: float = 5.0):
        with psycopg.connect(self.dsn, connect_timeout=max(1, math.ceil(timeout))) as connection:
            connection.execute(
                "SELECT set_config('statement_timeout',%s,true)",
                (str(max(1, int(timeout * 1000))),),
            )
            row = connection.execute(query, params).fetchone()
            if row is None:
                raise CapacityUnavailable("capacity query returned no result")
            return row[0]

    def configure(self) -> None:
        if not self._execute("SELECT oam_capacity_configure(%s)", (Jsonb(self.limits),)):
            raise ConfigurationError("deployment capacity settings differ from the stored settings")

    def try_http(self, *, write: bool) -> HTTPPermit | None:
        if self._closed.is_set():
            raise CapacityUnavailable("request admission is closed")
        connection = psycopg.connect(self.dsn, autocommit=True, connect_timeout=3)
        try:
            row = connection.execute(
                "SELECT oam_http_admit(%s,%s)", (write, Jsonb(self.limits))
            ).fetchone()
            if row is None:
                raise CapacityUnavailable("request admission returned no result")
            slot = row[0]
            if slot is not None:
                return HTTPPermit(connection, slot)
        except BaseException:
            connection.close()
            raise
        connection.close()
        return None

    def acquire_model(self, kind: str, *, background: bool, timeout: float) -> ModelPermit:
        if kind not in {"llm", "embedding"} or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("invalid model admission request")
        deadline = time.monotonic() + timeout
        while not self._closed.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            permit_id = uuid4()
            try:
                acquired = self._execute(
                    "SELECT oam_model_permit_acquire(%s,%s,%s,%s,%s,%s)",
                    (permit_id, self.owner_id, kind, background, remaining, Jsonb(self.limits)),
                    timeout=remaining,
                )
            except psycopg.Error as exc:
                raise CapacityUnavailable("model admission database is unavailable") from exc
            if acquired:
                return ModelPermit(self, permit_id)
            self._closed.wait(min(CAPACITY_POLL_SECONDS, max(0, deadline - time.monotonic())))
        raise CapacityUnavailable("model capacity wait expired before dispatch")

    def status(self) -> dict:
        return self._execute("SELECT oam_capacity_status()")

    def close(self) -> None:
        self._closed.set()


def reconcile_model_permit(
    connection: psycopg.Connection[Any],
    permit_id: UUID | str,
    *,
    known_completed: bool,
    reason: str,
) -> None:
    """Use operator database authority; application roles cannot update this table."""
    if not reason.strip():
        raise ValueError("reconciliation requires an operator reason")
    row = connection.execute(
        "UPDATE model_permits SET state=%s,reconciliation_reason=%s,"
        "reconciled_by=session_user,updated_at=clock_timestamp() "
        "WHERE permit_id=%s AND state IN ('dispatched','unknown','forced_release') "
        "RETURNING permit_id",
        ("released" if known_completed else "forced_release", reason, UUID(str(permit_id))),
    ).fetchone()
    if row is None:
        raise ValueError("no unresolved model permit matches this identifier")
