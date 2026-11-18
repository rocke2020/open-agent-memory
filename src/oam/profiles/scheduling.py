"""Optional automatic Profile admission through the existing durable worker path."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta

from croniter import croniter
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.contracts.profile import ProfileRefreshPolicy
from oam.db.context import bind_user
from oam.profiles.service import ProfileService

logger = logging.getLogger(__name__)


def merge_policy(current: dict, patch: dict) -> ProfileRefreshPolicy:
    merged = {**current, **patch}
    if patch.get("refresh_cron") and "refresh_after_consolidation" not in patch:
        merged["refresh_after_consolidation"] = False
    if patch.get("refresh_after_consolidation") and "refresh_cron" not in patch:
        merged["refresh_cron"] = None
    return ProfileRefreshPolicy.model_validate(merged)


def policy_enabled(policy: ProfileRefreshPolicy) -> bool:
    return bool(
        policy.refresh_cron or policy.full_refresh_cron or policy.refresh_after_consolidation
    )


def schedule_decision(
    policy: dict, state: dict, now: datetime, *, changed: bool, ready: bool
) -> str | None:
    settings = ProfileRefreshPolicy.model_validate(policy)
    if not policy_enabled(settings):
        return None

    def due(expression, last):
        if not expression:
            return False
        boundary = croniter(expression, now.astimezone(UTC) + timedelta(microseconds=1)).get_prev(
            datetime
        )
        return last is None or boundary > last

    last, full = state.get("last_success_at"), state.get("last_full_at")
    full_due = due(settings.full_refresh_cron, full) and (
        full is None or last is None or last > full
    )
    if full_due or state.get("pending_mode") == "full":
        return "full"
    ordinary_due = (
        due(settings.refresh_cron, last) or settings.refresh_after_consolidation and ready
    )
    if state.get("pending_mode") or changed and ordinary_due:
        return settings.mode
    return None


class _TransactionPool:
    """Let existing admission reuse the user lock and its enclosing transaction."""

    def __init__(self, connection):
        self.connection_value = connection

    def connection(self):
        return nullcontext(self.connection_value)


class ProfilePolicyService:
    def __init__(self, config: OAMConfig, pool):
        self.config, self.pool = config, pool

    def get(self, authority: AuthorizedUser) -> dict:
        if not authority.allows("memory:read"):
            raise PermissionError("Profile policy read is denied")
        with self.pool.connection() as connection, connection.transaction():
            bind_user(connection.cursor(), authority)
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT policy,paused,pending_mode,last_success_at,last_full_at "
                    "FROM profile_refresh_policies WHERE tenant_id=%s AND user_id=%s",
                    (authority.tenant_id, authority.user_id),
                )
                row = cursor.fetchone()
            return {
                "policy": ProfileRefreshPolicy.model_validate(
                    row["policy"] if row else {}
                ).model_dump(),
                "paused": row["paused"] if row else False,
                "pending_mode": row["pending_mode"] if row else None,
                "last_success_at": row["last_success_at"].isoformat()
                if row and row["last_success_at"]
                else None,
                "last_full_at": row["last_full_at"].isoformat()
                if row and row["last_full_at"]
                else None,
            }

    def patch(self, authority: AuthorizedUser, patch: dict) -> dict:
        if not authority.allows("memory:append") or not authority.allows("memory:read"):
            raise PermissionError("Profile policy update is denied")
        key = (authority.tenant_id, authority.user_id)
        with self.pool.connection() as connection, connection.transaction():
            bind_user(connection.cursor(), authority)
            user = connection.execute(
                "SELECT 1 FROM users WHERE tenant_id=%s AND user_id=%s "
                "AND state='active' FOR UPDATE",
                key,
            ).fetchone()
            if user is None:
                raise PermissionError("user is unavailable")
            row = connection.execute(
                "SELECT policy FROM profile_refresh_policies WHERE tenant_id=%s AND user_id=%s", key
            ).fetchone()
            policy = merge_policy(row[0] if row else {}, patch)
            connection.execute(
                "INSERT INTO profile_refresh_policies(tenant_id,user_id,policy) VALUES(%s,%s,%s) "
                "ON CONFLICT(tenant_id,user_id) DO UPDATE SET policy=EXCLUDED.policy,"
                "pending_mode=CASE WHEN %s THEN profile_refresh_policies.pending_mode "
                "ELSE NULL END,"
                "next_check_at=clock_timestamp()",
                (*key, Jsonb(policy.model_dump()), policy_enabled(policy)),
            )
            return ProfilePolicyService(self.config, _TransactionPool(connection)).get(authority)


def admit_scheduled_profile(config: OAMConfig, pool, tenant_id: str, user_id: str) -> bool:
    authority = AuthorizedUser(
        "worker", tenant_id, f"user:{user_id}", user_id, frozenset({"memory:read", "memory:append"})
    )
    key = (tenant_id, user_id)
    with pool.connection() as connection, connection.transaction():
        bind_user(connection.cursor(), authority)
        user = connection.execute(
            "SELECT 1 FROM users WHERE tenant_id=%s AND user_id=%s AND state='active' FOR UPDATE",
            key,
        ).fetchone()
        if user is None:
            return False
        grant = connection.execute(
            "SELECT oam_authorize_user(%s,%s,%s,'memory:read'),"
            "oam_authorize_user(%s,%s,%s,'memory:append')",
            (
                tenant_id,
                authority.principal_id,
                user_id,
                tenant_id,
                authority.principal_id,
                user_id,
            ),
        ).fetchone()
        if not grant or not all(grant):
            return False
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT * FROM profile_refresh_policies "
                "WHERE tenant_id=%s AND user_id=%s FOR UPDATE",
                key,
            )
            state = cursor.fetchone()
        if (
            not state
            or state["paused"]
            or not policy_enabled(ProfileRefreshPolicy.model_validate(state["policy"]))
        ):
            return False
        service = ProfileService(config, _TransactionPool(connection))
        view = service.get(authority, config.profile.max_response_tokens)
        now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
        if state["last_success_at"] is None and view["updated_at"]:
            state["last_success_at"] = datetime.fromisoformat(view["updated_at"])
        ready = not (view["pending_inputs"] or view["failed_inputs"] or view["blocked_inputs"])
        # Uncovered unchanged inputs do not cause repeated generation.
        changed = view["freshness"] in {"missing", "dirty", "stale", "partial"}
        mode = schedule_decision(state["policy"], state, now, changed=changed, ready=ready)
        if mode is None:
            return False
        connection.execute(
            "UPDATE profile_refresh_policies SET pending_mode=%s WHERE tenant_id=%s AND user_id=%s",
            (mode, *key),
        )
        active = connection.execute(
            "SELECT 1 FROM operations WHERE tenant_id=%s AND user_id=%s AND kind='refresh_profile' "
            "AND status IN ('accepted','running') LIMIT 1",
            key,
        ).fetchone()
        if active or not ready:
            return False
        interval = state["policy"].get("min_refresh_interval_seconds")
        if interval is None:
            interval = config.profile.min_refresh_interval_seconds
        floor = max(
            (
                value
                for value in (state["last_admitted_at"], state["last_success_at"])
                if value is not None
            ),
            default=None,
        )
        if floor and now < floor + timedelta(seconds=interval):
            return False
        outcome = service.refresh(authority, mode=mode)
        connection.execute(
            "UPDATE profile_refresh_policies SET pending_mode=NULL,last_admitted_at=%s "
            "WHERE tenant_id=%s AND user_id=%s",
            (now, *key),
        )
        logger.info(
            "profile_auto_admission",
            extra={
                "tenant_id": tenant_id,
                "user_id": user_id,
                "mode": mode,
                "outcome": outcome["status"],
            },
        )
        return outcome["status"] == "accepted"


def run_profile_schedule_tick(
    config: OAMConfig, queue_pool, content_pool, *, stop_event=None
) -> int:
    if not config.profile.enabled:
        return 0
    with queue_pool.connection() as connection, connection.transaction():
        rows = connection.execute(
            "SELECT tenant_id,user_id FROM oam_due_profile_policies(%s,%s)",
            (config.profile.schedule_batch_size, config.profile.schedule_poll_seconds),
        ).fetchall()
    admitted = 0
    for tenant_id, user_id in rows:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            admitted += admit_scheduled_profile(config, content_pool, tenant_id, user_id)
        except Exception as error:
            # One user's temporary capacity/input failure must not starve the rest.
            logger.warning(
                "profile_auto_admission_failed",
                extra={
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "error_type": type(error).__name__,
                },
            )
    return admitted
