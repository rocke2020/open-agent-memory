"""Stored Profile reads and explicit durable refresh admission."""

from __future__ import annotations

import os
import secrets
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.db.context import bind_user
from oam.db.inspection import SourceInspector
from oam.db.queue_admission import require_queue_capacity
from oam.indexing.identity import index_generation
from oam.profiles.inputs import ProfileInputs, load_profile_inputs
from oam.profiles.projection import SECTIONS, build_sections, select_sections

EMPTY_GENERATION = '{"main":[],"preferences":[],"experiences":[]}'


class ProfileRefreshUnresolved(RuntimeError):
    """A prior model request has an ambiguous external outcome."""


def _current_snapshot(connection, authority: AuthorizedUser):
    with connection.cursor(row_factory=dict_row) as cursor:
        bind_user(cursor, authority)
        cursor.execute(
            "SELECT profile_revision,sections,input_identity,policy_epoch,generation_id "
            "FROM profiles WHERE tenant_id=%s AND user_id=%s AND profile_id='user' "
            "AND visible_to IS NULL ORDER BY profile_revision DESC LIMIT 1",
            (authority.tenant_id, authority.user_id),
        )
        return cursor.fetchone()


def _view(
    config: OAMConfig,
    inputs: ProfileInputs,
    snapshot: dict[str, Any] | None,
    sections: list[str],
    max_tokens: int,
) -> dict[str, Any]:
    exact = build_sections(
        EMPTY_GENERATION,
        inputs.supported,
        inputs.exact_fields,
        max_fields=config.profile.max_fields_per_section,
        max_value_bytes=config.profile.max_value_bytes,
    )
    merged = {section: [] for section in SECTIONS}
    for section in SECTIONS:
        exact_keys = {(field["key"], field.get("qualifier", "")) for field in exact[section]}
        from_snapshot = snapshot["sections"].get(section, []) if snapshot else []
        merged[section] = exact[section] + [
            field
            for field in from_snapshot
            if (field["key"], field.get("qualifier", "")) not in exact_keys
            and not any(ref.get("kind") == "profile_resource" for ref in field.get("supports", []))
        ]
    chosen = select_sections(merged, sections, inputs.eligible_support_ids, max_tokens=max_tokens)
    if snapshot is None:
        freshness = "missing"
    elif chosen["freshness"] == "stale":
        freshness = "stale"
    elif inputs.pending_inputs:
        freshness = "partial"
    elif bytes(snapshot["input_identity"]) != inputs.identity:
        freshness = "dirty"
    else:
        freshness = chosen["freshness"]
    return {
        "profile_revision": snapshot["profile_revision"] if snapshot else None,
        "input_identity": inputs.identity.hex(),
        "policy_epoch": inputs.policy_epoch,
        "sections": chosen["sections"],
        "freshness": freshness,
        "pending_inputs": inputs.pending_inputs,
        "omitted": chosen["omitted"],
        "tokens": chosen["tokens"],
        "generation_calls": 0,
    }


class ProfileService:
    def __init__(self, config: OAMConfig, pool):
        self.config = config
        self.pool = pool

    def get(
        self, authority: AuthorizedUser, sections: list[str], max_tokens: int
    ) -> dict[str, Any]:
        if not authority.allows("memory:read"):
            raise PermissionError("Profile read is denied")
        if len(set(sections)) != len(sections) or any(
            section not in SECTIONS for section in sections
        ):
            raise ValueError("invalid Profile section selection")
        generation = index_generation(self.config, os.environ)
        with self.pool.connection() as connection, connection.transaction():
            inputs = load_profile_inputs(connection, authority, generation)
            snapshot = _current_snapshot(connection, authority)
            result = _view(self.config, inputs, snapshot, sections, max_tokens)
            SourceInspector._check_policy(connection, authority, inputs.policy_epoch)
            return result

    def check_policy(self, authority: AuthorizedUser, policy_epoch: int) -> None:
        with self.pool.connection() as connection, connection.transaction():
            bind_user(connection.cursor(), authority)
            SourceInspector._check_policy(connection, authority, policy_epoch)

    def refresh(self, authority: AuthorizedUser, sections: list[str]) -> dict[str, Any]:
        if not authority.allows("memory:append") or not authority.allows("memory:read"):
            raise PermissionError("Profile refresh is denied")
        if len(set(sections)) != len(sections) or any(
            section not in SECTIONS for section in sections
        ):
            raise ValueError("invalid Profile section selection")
        generation = index_generation(self.config, os.environ)
        with self.pool.connection() as connection, connection.transaction():
            with connection.cursor() as cursor:
                bind_user(cursor, authority)
                cursor.execute(
                    "SELECT 1 FROM users WHERE tenant_id=%s AND user_id=%s FOR UPDATE",
                    (authority.tenant_id, authority.user_id),
                )
            inputs = load_profile_inputs(connection, authority, generation)
            snapshot = _current_snapshot(connection, authority)
            if (
                snapshot is not None
                and bytes(snapshot["input_identity"]) == inputs.identity
                and inputs.pending_inputs == 0
            ):
                return {
                    "status": "unchanged",
                    **_view(
                        self.config,
                        inputs,
                        snapshot,
                        sections,
                        self.config.context.default_tokens,
                    ),
                }
            unresolved = connection.execute(
                "SELECT 1 FROM operations o JOIN work_units w "
                "USING(tenant_id,user_id,operation_id) JOIN attempts a "
                "USING(tenant_id,user_id,work_unit_id) "
                "WHERE o.tenant_id=%s AND o.user_id=%s AND o.kind='refresh_profile' "
                "AND o.request_hash=%s AND a.disposition='unknown_external_outcome' "
                "LIMIT 1",
                (authority.tenant_id, authority.user_id, inputs.identity),
            ).fetchone()
            if unresolved:
                raise ProfileRefreshUnresolved("Profile attempt requires reconciliation")
            row = connection.execute(
                "SELECT operation_id FROM operations WHERE tenant_id=%s AND user_id=%s "
                "AND kind='refresh_profile' AND request_hash=%s "
                "AND status IN ('accepted','running') ORDER BY created_at DESC LIMIT 1",
                (authority.tenant_id, authority.user_id, inputs.identity),
            ).fetchone()
            if row:
                return {"status": "accepted", "operation_id": row[0]}
            require_queue_capacity(
                connection.cursor(), authority, self.config.queue.max_pending_per_user
            )
            operation_id = "op_profile_" + secrets.token_hex(12)
            work_id = "work_profile_" + secrets.token_hex(12)
            connection.execute(
                "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,kind,"
                "schema_version,status,request_hash,plan_sealed,stage_state) "
                "VALUES (%s,%s,%s,%s,'refresh_profile',1,'accepted',%s,true,%s)",
                (
                    authority.tenant_id,
                    authority.user_id,
                    operation_id,
                    authority.principal_id,
                    inputs.identity,
                    Jsonb({"pending_inputs": inputs.pending_inputs}),
                ),
            )
            connection.execute(
                "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
                "input_digest,generation_id,status,attempts_remaining) "
                "VALUES (%s,%s,%s,%s,'refresh_profile',%s,%s,'queued',%s)",
                (
                    authority.tenant_id,
                    authority.user_id,
                    work_id,
                    operation_id,
                    inputs.identity,
                    generation,
                    self.config.retry.max_attempts,
                ),
            )
            return {"status": "accepted", "operation_id": operation_id}
