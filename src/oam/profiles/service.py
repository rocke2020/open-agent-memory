"""Stored Profile reads and explicit durable refresh admission."""

from __future__ import annotations

import hashlib
import json
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
from oam.operations.repository import OperationRepository
from oam.profiles.document import render_stored_profile_fields
from oam.profiles.inputs import ProfileInputs, load_profile_inputs
from oam.profiles.prompt import PROFILE_SYSTEM
from oam.profiles.resources import public_profile_data_support
from oam.tokens import estimate_units, estimated_tokens


class ProfileRefreshUnresolved(RuntimeError):
    """A prior model request has an ambiguous external outcome."""


class ProfileInputsNotReady(RuntimeError):
    """A requested Profile source has not reached the Insight stage."""


def _current_snapshot(connection, authority: AuthorizedUser):
    with connection.cursor(row_factory=dict_row) as cursor:
        bind_user(cursor, authority)
        cursor.execute(
            'SELECT profile_revision,sections,content,instruction,support_ids,"references",'
            "input_identity,policy_epoch,generation_id,published_at "
            "FROM profiles WHERE tenant_id=%s AND user_id=%s AND profile_id='user' "
            "AND visible_to IS NULL ORDER BY profile_revision DESC LIMIT 1",
            (authority.tenant_id, authority.user_id),
        )
        return cursor.fetchone()


def profile_identity(input_identity: bytes, instruction: str | None) -> bytes:
    recipe = json.dumps([input_identity.hex(), PROFILE_SYSTEM, instruction], ensure_ascii=False)
    return hashlib.sha256(recipe.encode("utf-8")).digest()


def stored_profile_support_ids(snapshot: dict[str, Any] | None) -> set[str]:
    if snapshot is None or snapshot["content"] is not None:
        return set()
    return {
        support["id"]
        for fields in snapshot.get("sections", {}).values()
        for field in fields
        for support in field.get("supports", [])
        if "id" in support
    }


def _view(
    config: OAMConfig,
    inputs: ProfileInputs,
    snapshot: dict[str, Any] | None,
    max_tokens: int,
) -> dict[str, Any]:
    legacy_unavailable = False
    if snapshot and snapshot["content"] is None and any(snapshot.get("sections", {}).values()):
        try:
            document = render_stored_profile_fields(
                snapshot["sections"],
                inputs.stored_support_inputs,
                inputs.eligible_support_ids,
                max_content_bytes=config.profile.max_content_bytes,
            )
            snapshot = {**snapshot, **document}
        except ValueError:
            legacy_unavailable = True
    supported = (
        snapshot is not None
        and not legacy_unavailable
        and set(snapshot["support_ids"]) <= inputs.eligible_support_ids
    )
    content = snapshot["content"] if snapshot and supported else None
    budget_omitted = content is not None and estimated_tokens(estimate_units(content)) > max_tokens
    if budget_omitted:
        content = None
    if snapshot is None or snapshot["content"] is None and not legacy_unavailable:
        freshness = "missing"
    elif not supported:
        freshness = "stale"
    elif inputs.waiting_source_ids:
        freshness = "dirty"
    elif inputs.failed_inputs:
        freshness = "partial"
    elif bytes(snapshot["input_identity"]) != profile_identity(
        inputs.identity, snapshot["instruction"]
    ):
        freshness = "dirty"
    elif inputs.uncovered_inputs:
        freshness = "uncovered"
    else:
        freshness = "current"
    return {
        "profile_revision": snapshot["profile_revision"] if snapshot else None,
        "updated_at": snapshot["published_at"].isoformat()
        if snapshot and snapshot["published_at"]
        else None,
        "input_identity": inputs.identity.hex(),
        "policy_epoch": inputs.policy_epoch,
        "content": content,
        "instruction": snapshot["instruction"] if snapshot else None,
        "default_prompt": PROFILE_SYSTEM,
        "references": snapshot["references"] if content is not None else [],
        "support_ids": snapshot["support_ids"] if content is not None else [],
        "freshness": freshness,
        "pending_inputs": inputs.pending_inputs,
        "failed_inputs": inputs.failed_inputs,
        "blocked_inputs": inputs.blocked_inputs,
        "uncovered_inputs": inputs.uncovered_inputs,
        "omitted": [{"reason": "budget"}] if budget_omitted else [],
        "tokens": estimated_tokens(estimate_units(content)) if content else 0,
        "generation_calls": 0,
    }


def _public_view(config, inputs, snapshot, max_tokens) -> dict[str, Any]:
    result = _view(config, inputs, snapshot, max_tokens)
    result["references"] = [
        public_profile_data_support(reference)
        if reference.get("kind") == "profile_resource"
        else reference
        for reference in result["references"]
    ]
    return result


class ProfileService:
    def __init__(self, config: OAMConfig, pool):
        self.config = config
        self.pool = pool

    def get(self, authority: AuthorizedUser, max_tokens: int) -> dict[str, Any]:
        if not authority.allows("memory:read"):
            raise PermissionError("Profile read is denied")
        with self.pool.connection() as connection, connection.transaction():
            bind_user(connection.cursor(), authority)
            row = connection.execute(
                "SELECT index_generation_id FROM users WHERE tenant_id=%s AND user_id=%s FOR SHARE",
                (authority.tenant_id, authority.user_id),
            ).fetchone()
            snapshot = _current_snapshot(connection, authority)
            generation = row[0] if row else None
            if generation is None and snapshot:
                generation = snapshot["generation_id"]
            if generation is None:
                generation = index_generation(self.config, os.environ)
            inputs = load_profile_inputs(
                connection,
                authority,
                generation,
                stored_support_ids=stored_profile_support_ids(snapshot),
            )
            result = _public_view(self.config, inputs, snapshot, max_tokens)
            SourceInspector._check_policy(connection, authority, inputs.policy_epoch)
            return result

    def check_policy(self, authority: AuthorizedUser, policy_epoch: int) -> None:
        with self.pool.connection() as connection, connection.transaction():
            bind_user(connection.cursor(), authority)
            SourceInspector._check_policy(connection, authority, policy_epoch)

    def refresh(
        self,
        authority: AuthorizedUser,
        instruction: str | None = None,
        *,
        reset_instruction: bool = False,
    ) -> dict[str, Any]:
        if not authority.allows("memory:append") or not authority.allows("memory:read"):
            raise PermissionError("Profile refresh is denied")
        if instruction is not None and reset_instruction:
            raise ValueError("instruction and reset_instruction cannot be combined")
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
            if reset_instruction:
                selected_instruction = None
            elif instruction is not None:
                selected_instruction = instruction
            else:
                selected_instruction = snapshot["instruction"] if snapshot else None
            target_identity = profile_identity(inputs.identity, selected_instruction)
            active_rows = connection.execute(
                "SELECT o.operation_id,o.request_hash,o.stage_state,w.dependency_ids,"
                "w.input_digest "
                "FROM operations o JOIN work_units w "
                "USING(tenant_id,user_id,operation_id) "
                "WHERE o.tenant_id=%s AND o.user_id=%s AND o.kind='refresh_profile' "
                "AND o.status IN ('accepted','running') AND w.generation_id=%s "
                "ORDER BY o.created_at DESC,o.operation_id DESC",
                (authority.tenant_id, authority.user_id, generation),
            ).fetchall()
            for active in active_rows:
                if active[2].get("instruction") != selected_instruction:
                    OperationRepository().cancel(connection, authority, active[0])
            row = next(
                (
                    active
                    for active in active_rows
                    if active[2].get("instruction") == selected_instruction
                ),
                None,
            )
            if (
                snapshot is not None
                and snapshot["content"] is not None
                and set(snapshot["support_ids"]) <= inputs.eligible_support_ids
                and bytes(snapshot["input_identity"]) == target_identity
                and not inputs.waiting_source_ids
            ):
                return {
                    "status": "unchanged",
                    **_public_view(
                        self.config,
                        inputs,
                        snapshot,
                        self.config.profile.max_response_tokens,
                    ),
                }
            if (
                row
                and row[2].get("scope_identity") == inputs.scope_identity.hex()
                and row[2].get("instruction") == selected_instruction
                and (bytes(row[1]) == target_identity or row[3] or bytes(row[4]) == target_identity)
            ):
                return {"status": "accepted", "operation_id": row[0]}
            unresolved = connection.execute(
                "SELECT 1 FROM operations o JOIN work_units w "
                "USING(tenant_id,user_id,operation_id) JOIN attempts a "
                "USING(tenant_id,user_id,work_unit_id) "
                "WHERE o.tenant_id=%s AND o.user_id=%s AND o.kind='refresh_profile' "
                "AND a.disposition='unknown_external_outcome' AND "
                "((o.stage_state->>'scope_identity'=%s AND "
                "o.stage_state->>'instruction' IS NOT DISTINCT FROM %s) OR "
                "(NOT (o.stage_state ? 'scope_identity') AND o.request_hash=%s)) "
                "LIMIT 1",
                (
                    authority.tenant_id,
                    authority.user_id,
                    inputs.scope_identity.hex(),
                    selected_instruction,
                    target_identity,
                ),
            ).fetchone()
            if unresolved:
                raise ProfileRefreshUnresolved("Profile attempt requires reconciliation")
            require_queue_capacity(
                connection.cursor(), authority, self.config.queue.max_pending_per_user
            )
            dependencies: list[str] = []
            if inputs.waiting_source_ids:
                rows = connection.execute(
                    "SELECT o.source_id,w.work_unit_id FROM operations o JOIN work_units w "
                    "USING(tenant_id,user_id,operation_id) JOIN sources s "
                    "ON (s.tenant_id,s.user_id,s.source_id)="
                    "(o.tenant_id,o.user_id,o.source_id) "
                    "WHERE o.tenant_id=%s AND o.user_id=%s AND o.kind='consolidate' "
                    "AND o.source_id=ANY(%s) AND o.source_version=s.active_version "
                    "AND s.status='active' AND o.status IN ('accepted','running') "
                    "AND w.stage='consolidate' AND w.generation_id=%s "
                    "AND w.status IN ('queued','running','retry_wait') "
                    "ORDER BY o.source_id,w.work_unit_id",
                    (
                        authority.tenant_id,
                        authority.user_id,
                        list(inputs.waiting_source_ids),
                        generation,
                    ),
                ).fetchall()
                if {row[0] for row in rows} != set(inputs.waiting_source_ids):
                    raise ProfileInputsNotReady("Profile inputs are still indexing")
                dependencies = [row[1] for row in rows]
                connection.execute(
                    "UPDATE work_units SET available_at=clock_timestamp(),"
                    "updated_at=clock_timestamp() WHERE tenant_id=%s AND user_id=%s "
                    "AND work_unit_id=ANY(%s) AND status IN ('queued','retry_wait') "
                    "AND available_at>clock_timestamp()",
                    (authority.tenant_id, authority.user_id, dependencies),
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
                    target_identity,
                    Jsonb(
                        {
                            "pending_inputs": inputs.pending_inputs,
                            "failed_inputs": inputs.failed_inputs,
                            "blocked_inputs": inputs.blocked_inputs,
                            "uncovered_inputs": inputs.uncovered_inputs,
                            "scope_identity": inputs.scope_identity.hex(),
                            "instruction": selected_instruction,
                        }
                    ),
                ),
            )
            connection.execute(
                "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
                "input_digest,generation_id,status,dependency_ids,attempts_remaining) "
                "VALUES (%s,%s,%s,%s,'refresh_profile',%s,%s,'queued',%s,%s)",
                (
                    authority.tenant_id,
                    authority.user_id,
                    work_id,
                    operation_id,
                    target_identity,
                    generation,
                    dependencies,
                    self.config.retry.max_attempts,
                ),
            )
            return {"status": "accepted", "operation_id": operation_id}
