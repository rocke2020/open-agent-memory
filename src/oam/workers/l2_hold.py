"""Operator control of durable, per-user consolidation claim holds."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg

from oam.config import ConfigurationError, load_config

_DATABASE_TIMEOUT_SECONDS = 5


def _require_unfiltered_operator(connection: psycopg.Connection) -> None:
    role = connection.execute(
        "SELECT rolsuper,rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=current_user"
    ).fetchone()
    if role is None or not (role[0] or role[1]):
        raise ValueError("L2 hold requires an operator role with BYPASSRLS or superuser access")


def set_l2_hold(dsn: str, tenant_id: str, user_id: str, held: bool) -> None:
    """Serialize with claim admission; refuse to hold while L2 work is running."""
    with psycopg.connect(dsn, connect_timeout=_DATABASE_TIMEOUT_SECONDS) as connection:
        connection.execute("SELECT set_config('statement_timeout',%s,true)", ("5s",))
        _require_unfiltered_operator(connection)
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended("
            "'oam:user-claims:' || jsonb_build_array(%s::text,%s::text)::text,0))",
            (tenant_id, user_id),
        )
        if held:
            active = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM work_units WHERE tenant_id=%s AND user_id=%s "
                "AND stage='consolidate' AND status='running')",
                (tenant_id, user_id),
            ).fetchone()
            if active is None:
                raise ValueError("L2 work state is unavailable")
            if active[0]:
                raise ValueError("L2 work is running or requires recovery; drain it before holding")
        connection.execute(
            "INSERT INTO l2_claim_holds(tenant_id,user_id,held) VALUES (%s,%s,%s) "
            "ON CONFLICT (tenant_id,user_id) DO UPDATE SET held=EXCLUDED.held",
            (tenant_id, user_id, held),
        )


def l2_hold_from_environment(
    config_path: str | Path, action: str, tenant_id: str, user_id: str
) -> int:
    """Use only the selected operator DSN, without exposing connection errors."""
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
    if not tenant_id.strip() or not user_id.strip():
        print(json.dumps({"error": "tenant and user IDs must be nonempty"}), file=sys.stderr)
        return 2
    try:
        if action in {"hold", "release"}:
            set_l2_hold(dsn, tenant_id, user_id, action == "hold")
        elif action != "status":
            raise ValueError("unknown L2 hold action")
        with psycopg.connect(dsn, connect_timeout=_DATABASE_TIMEOUT_SECONDS) as connection:
            connection.execute("SELECT set_config('statement_timeout',%s,true)", ("5s",))
            _require_unfiltered_operator(connection)
            row = connection.execute(
                "SELECT coalesce(h.held,false), "
                "(SELECT count(*) FROM work_units w WHERE w.tenant_id=u.tenant_id "
                "AND w.user_id=u.user_id AND w.stage='consolidate' AND w.status='running') "
                "FROM users u LEFT JOIN l2_claim_holds h USING(tenant_id,user_id) "
                "WHERE u.tenant_id=%s AND u.user_id=%s",
                (tenant_id, user_id),
            ).fetchone()
            if row is None:
                raise ValueError("user not found")
        print(
            json.dumps(
                {"tenant_id": tenant_id, "user_id": user_id, "held": row[0], "running": row[1]}
            )
        )
        return 0
    except ValueError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    except psycopg.Error:
        print(json.dumps({"error": "operator database action failed"}), file=sys.stderr)
        return 1
