"""Operator-only inspection, reconciliation, and deployment capacity configuration."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from oam.capacity import PostgresCapacity, reconcile_model_permit
from oam.config import ConfigurationError, load_config

HTTP_ADMISSION_NAMESPACE = 186865777
OPERATOR_DATABASE_TIMEOUT_SECONDS = 5
PERMIT_METADATA = (
    "permit_id,kind,state,background,created_at,updated_at,expires_at,reconciliation_reason"
)


def _configure(connection, limits: dict[str, int]) -> None:
    # Admission reads capacity_settings before writing permits or owning an HTTP
    # slot. Holding both table locks makes the emptiness check and replacement atomic.
    connection.execute(
        "LOCK TABLE public.capacity_settings, public.model_permits IN ACCESS EXCLUSIVE MODE"
    )
    active = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM public.model_permits "
        "WHERE state IN ('dispatched','unknown') "
        "OR (state='reserved' AND expires_at>clock_timestamp())) AS active"
    ).fetchone()["active"]
    if active:
        raise ValueError("active model permits prevent configuration; stop APIs and workers first")
    active_http = connection.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_locks "
        "WHERE locktype='advisory' AND granted AND classid=%s AND objsubid=2 "
        "AND database=(SELECT oid FROM pg_catalog.pg_database "
        "WHERE datname=current_database())) AS active",
        (HTTP_ADMISSION_NAMESPACE,),
    ).fetchone()["active"]
    if active_http:
        raise ValueError("active HTTP requests prevent configuration; stop APIs and workers first")
    connection.execute(
        "INSERT INTO public.capacity_settings(singleton,limits) VALUES (true,%s) "
        "ON CONFLICT (singleton) DO UPDATE SET limits=EXCLUDED.limits",
        (Jsonb(limits),),
    )


def model_permits_from_environment(
    config_path: str | Path,
    action: str,
    *,
    permit_id: UUID | None = None,
    known_completed: bool = False,
    reason: str = "",
) -> int:
    """Resolve only the configured migration authority; emit metadata as JSON."""
    try:
        config = load_config(config_path).config
    except ConfigurationError:
        print(json.dumps({"error": "invalid OAM configuration"}), file=sys.stderr)
        return 2
    dsn_name = config.database.migration_dsn_env
    dsn = os.environ.get(dsn_name)
    if not dsn:
        print(
            json.dumps({"error": f"missing required environment variable {dsn_name}"}),
            file=sys.stderr,
        )
        return 2
    try:
        with psycopg.connect(
            dsn, row_factory=dict_row, connect_timeout=OPERATOR_DATABASE_TIMEOUT_SECONDS
        ) as connection:
            connection.execute(
                "SELECT set_config('statement_timeout',%s,true)",
                (f"{OPERATOR_DATABASE_TIMEOUT_SECONDS}s",),
            )
            payload: dict[str, object]
            if action == "list":
                permits = connection.execute(
                    f"SELECT {PERMIT_METADATA} FROM public.model_permits "  # noqa: S608
                    "WHERE state IN ('reserved','dispatched','unknown','forced_release') "
                    "ORDER BY created_at,permit_id"
                ).fetchall()
                payload = {"permits": permits}
            elif action == "reconcile":
                if permit_id is None:
                    raise ValueError("reconciliation requires a permit identifier")
                reconcile_model_permit(
                    connection, permit_id, known_completed=known_completed, reason=reason
                )
                permit = connection.execute(
                    f"SELECT {PERMIT_METADATA} FROM public.model_permits WHERE permit_id=%s",  # noqa: S608
                    (permit_id,),
                ).fetchone()
                payload = {"permit": permit}
            elif action == "configure":
                limits = PostgresCapacity(config, dsn).limits
                _configure(connection, limits)
                payload = {"limits": limits}
            else:
                raise ValueError("unknown model permit action")
            verification = connection.execute(
                "SELECT NOT EXISTS (SELECT 1 FROM public.model_permits "
                "WHERE state='forced_release') AS physical_concurrency_verified"
            ).fetchone()
            if verification is None:
                raise ValueError("permit verification state is unavailable")
            payload.update(verification)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    except psycopg.Error:
        print(json.dumps({"error": "operator database action failed"}), file=sys.stderr)
        return 1
    print(json.dumps(payload, default=str, sort_keys=True))
    return 0
