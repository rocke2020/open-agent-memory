"""Process one requested Profile refresh without a source-bound IndexStore."""

from __future__ import annotations

import json
import os
import time

import httpx
import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.db.context import bind_user
from oam.models.runtime import ModelOutputError
from oam.profiles.document import build_profile_document
from oam.profiles.inputs import MAX_PROMPT_UNITS, load_profile_inputs
from oam.profiles.prompt import PROFILE_SYSTEM
from oam.profiles.selection import PROFILE_PURPOSE, pack_profile_request, rank_profile_candidates
from oam.profiles.service import profile_identity, profile_query_prefix
from oam.tokens import resolve_counter
from oam.workers.claims import ClaimedWork


class StaleProfileWork(RuntimeError):
    """A claimed refresh no longer owns the current target."""


def _guard(connection, claim: ClaimedWork, worker_id: str) -> None:
    row = connection.execute(
        "SELECT w.owner_id,w.fence,w.status,w.lease_expires_at>clock_timestamp(),"
        "w.input_digest,o.status,o.cancellation_revision,u.state,u.index_generation_id "
        "FROM work_units w JOIN operations o USING(tenant_id,user_id,operation_id) "
        "JOIN users u USING(tenant_id,user_id) "
        "WHERE w.tenant_id=%s AND w.user_id=%s AND w.work_unit_id=%s "
        "FOR UPDATE OF w,o,u",
        (claim.tenant_id, claim.user_id, claim.work_unit_id),
    ).fetchone()
    if (
        row is None
        or row[:4] != (worker_id, claim.fence, "running", True)
        or row[5] not in {"accepted", "running"}
        or row[6:8] != (0, "active")
        or row[8] not in {None, claim_generation(connection, claim)}
    ):
        raise StaleProfileWork("Profile lease, policy or user is no longer eligible")


def claim_generation(connection, claim: ClaimedWork) -> str:
    row = connection.execute(
        "SELECT generation_id FROM work_units WHERE tenant_id=%s AND user_id=%s "
        "AND work_unit_id=%s",
        (claim.tenant_id, claim.user_id, claim.work_unit_id),
    ).fetchone()
    if row is None:
        raise StaleProfileWork("Profile work unit is unavailable")
    return row[0]


def _renew_profile_lease(connection, config: OAMConfig, claim: ClaimedWork, worker_id: str) -> None:
    renewed = connection.execute(
        "UPDATE work_units SET lease_expires_at=clock_timestamp() + "
        "(%s * interval '1 second') WHERE tenant_id=%s AND user_id=%s "
        "AND work_unit_id=%s AND owner_id=%s AND fence=%s AND status='running' "
        "AND lease_expires_at>clock_timestamp() RETURNING work_unit_id",
        (
            config.profile.attempt_timeout_seconds + config.lease.heartbeat_seconds,
            claim.tenant_id,
            claim.user_id,
            claim.work_unit_id,
            worker_id,
            claim.fence,
        ),
    ).fetchone()
    if renewed is None:
        raise StaleProfileWork("Profile lease expired before generation")


def process_profile_claim(
    config: OAMConfig,
    content_dsn: str,
    *,
    models,
    pool: ConnectionPool | None = None,
    claim: ClaimedWork,
    worker_id: str,
) -> None:
    authority = AuthorizedUser(
        "worker",
        claim.tenant_id,
        "worker",
        claim.user_id,
        frozenset({"memory:read", "memory:append"}),
    )
    counter = getattr(models, "ingestion_counter", None)
    if counter is None:
        counter = resolve_counter(config.tokenizer, os.environ)
    key = (claim.tenant_id, claim.user_id)
    generation = None
    usage = {}
    embedding_usage = {}
    model_stage = None
    try:
        connection_context = (
            pool.connection(timeout=config.database.pool_acquire_timeout_seconds)
            if pool is not None
            else psycopg.connect(content_dsn)
        )
        with connection_context as connection, connection.transaction():
            bind_user(connection.cursor(), authority)
            _guard(connection, claim, worker_id)
            generation = claim_generation(connection, claim)
            inputs = load_profile_inputs(
                connection, authority, generation, insights_enabled=config.index.insights_enabled
            )
            digest, dependencies, scope_identity, instruction = connection.execute(
                "SELECT w.input_digest,w.dependency_ids,o.stage_state->>'scope_identity',"
                "o.stage_state->>'instruction' "
                "FROM work_units w JOIN operations o "
                "USING(tenant_id,user_id,operation_id) "
                "WHERE w.tenant_id=%s AND w.user_id=%s AND w.work_unit_id=%s",
                (*key, claim.work_unit_id),
            ).fetchone()
            if scope_identity is not None and scope_identity != inputs.scope_identity.hex():
                raise StaleProfileWork("Profile source scope changed before generation")
            target_identity = profile_identity(
                inputs.identity,
                instruction,
                config.profile.max_input_tokens,
                profile_query_prefix(config),
                counter.identity,
                config.profile.max_memory_tokens,
            )
            if dependencies:
                if inputs.waiting_source_ids:
                    raise StaleProfileWork("New Profile inputs are still processing")
                connection.execute(
                    "UPDATE work_units SET input_digest=%s,dependency_ids='{}',"
                    "updated_at=clock_timestamp() WHERE tenant_id=%s AND user_id=%s "
                    "AND work_unit_id=%s AND owner_id=%s AND fence=%s AND status='running'",
                    (target_identity, *key, claim.work_unit_id, worker_id, claim.fence),
                )
            elif target_identity != bytes(digest):
                raise StaleProfileWork("Profile inputs changed before generation")
            connection.execute(
                "UPDATE operations SET status='running',updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                (*key, claim.operation_id),
            )
            needs_model = bool(inputs.prompt_inputs or instruction and inputs.exact_fields)
            if needs_model:
                _renew_profile_lease(connection, config, claim, worker_id)
                connection.execute(
                    "UPDATE attempts SET disposition='unknown_external_outcome',"
                    "request_ref='profile' "
                    "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s",
                    (*key, claim.work_unit_id, claim.attempt_id),
                )
        if needs_model:
            selection_started = time.monotonic()
            eligible_count = len(inputs.supported)
            if eligible_count:
                query = PROFILE_PURPOSE + (f"\n{instruction}" if instruction else "")
                prefix = profile_query_prefix(config)
                model_stage = "embedding"
                embedding = models.embed(
                    (prefix + query,), timeout_seconds=config.profile.attempt_timeout_seconds
                )
                embedding_usage = embedding.usage.model_dump(mode="json")
                model_stage = None
                connection_context = (
                    pool.connection(timeout=config.database.pool_acquire_timeout_seconds)
                    if pool is not None
                    else psycopg.connect(content_dsn)
                )
                with connection_context as connection, connection.transaction():
                    bind_user(connection.cursor(), authority)
                    _guard(connection, claim, worker_id)
                    selected_ids = rank_profile_candidates(
                        connection, embedding.vectors[0], MAX_PROMPT_UNITS
                    )
                    ranked = load_profile_inputs(
                        connection,
                        authority,
                        generation,
                        insights_enabled=config.index.insights_enabled,
                        selected_unit_ids=selected_ids,
                    )
                    if (
                        profile_identity(
                            ranked.identity,
                            instruction,
                            config.profile.max_input_tokens,
                            profile_query_prefix(config),
                            counter.identity,
                            config.profile.max_memory_tokens,
                        )
                        != target_identity
                    ):
                        raise StaleProfileWork("Profile inputs changed during selection")
                    _renew_profile_lease(connection, config, claim, worker_id)
                candidates = ranked.prompt_inputs
            else:
                candidates = []
            packed = pack_profile_request(
                candidates,
                exact_fields=inputs.exact_fields,
                instruction=instruction,
                system=PROFILE_SYSTEM,
                max_input_tokens=config.profile.max_input_tokens,
                max_memory_tokens=config.profile.max_memory_tokens,
                counter=counter,
            )
            selected_inputs = packed.inputs
            uncovered = eligible_count - len(selected_inputs)
            input_coverage = {
                "unexamined": eligible_count - len(candidates),
                "budget_omitted": packed.uncovered,
                "selected": len(selected_inputs),
            }
            print(
                json.dumps(
                    {
                        "stage": "profile_selection",
                        "operation_id": claim.operation_id,
                        "eligible": eligible_count,
                        "candidates": len(candidates),
                        "selected": len(selected_inputs),
                        "unexamined": eligible_count - len(candidates),
                        "budget_omitted": packed.uncovered,
                        "memory_tokens": packed.memory_tokens,
                        "memory_limit": config.profile.max_memory_tokens,
                        "input_tokens": packed.input_tokens,
                        "input_limit": config.profile.max_input_tokens,
                        "duration_ms": round((time.monotonic() - selection_started) * 1000, 2),
                    }
                ),
                flush=True,
            )
            model_stage = "profile"
            result = models.generate(
                "profile",
                system=PROFILE_SYSTEM,
                user=packed.user,
                max_output_tokens=config.profile.max_output_tokens,
                timeout_seconds=config.profile.attempt_timeout_seconds,
            )
            content = result.content
            usage = result.usage.model_dump(mode="json")
            model_stage = None
            generation_calls = 1
        else:
            content, usage, generation_calls = None, {}, 0
            embedding_usage = {}
            selected_inputs, uncovered = [], 0
            input_coverage = {"unexamined": 0, "budget_omitted": 0, "selected": 0}
        document = build_profile_document(
            content,
            selected_inputs,
            inputs.exact_fields,
            instruction=instruction if needs_model else None,
            max_content_bytes=config.profile.max_content_bytes,
        )
        connection_context = (
            pool.connection(timeout=config.database.pool_acquire_timeout_seconds)
            if pool is not None
            else psycopg.connect(content_dsn)
        )
        with connection_context as connection, connection.transaction():
            bind_user(connection.cursor(), authority)
            _guard(connection, claim, worker_id)
            current = load_profile_inputs(
                connection, authority, generation, insights_enabled=config.index.insights_enabled
            )
            if (
                profile_identity(
                    current.identity,
                    instruction,
                    config.profile.max_input_tokens,
                    profile_query_prefix(config),
                    counter.identity,
                    config.profile.max_memory_tokens,
                )
                != target_identity
            ):
                raise StaleProfileWork("Profile inputs changed during generation")
            user = connection.execute(
                "SELECT published_seq FROM users WHERE tenant_id=%s AND user_id=%s FOR UPDATE",
                key,
            ).fetchone()
            revision = connection.execute(
                "SELECT COALESCE(MAX(profile_revision),0)+1 FROM profiles "
                "WHERE tenant_id=%s AND user_id=%s AND profile_id='user'",
                key,
            ).fetchone()[0]
            visible_from = user[0] + 1
            connection.execute(
                "UPDATE profiles SET visible_to=%s WHERE tenant_id=%s AND user_id=%s "
                "AND profile_id='user' AND visible_to IS NULL",
                (visible_from, *key),
            )
            connection.execute(
                "INSERT INTO profiles(tenant_id,user_id,profile_id,profile_revision,sections,"
                'content,instruction,support_ids,"references",input_identity,policy_epoch,'
                "generation_id,visible_from,published_at,input_coverage) "
                "VALUES (%s,%s,'user',%s,'{}'::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,clock_timestamp(),%s)",
                (
                    *key,
                    revision,
                    document["content"],
                    instruction,
                    Jsonb(document["support_ids"]),
                    Jsonb(document["references"]),
                    target_identity,
                    inputs.policy_epoch,
                    generation,
                    visible_from,
                    Jsonb(input_coverage),
                ),
            )
            connection.execute(
                "UPDATE users SET published_seq=%s WHERE tenant_id=%s AND user_id=%s",
                (visible_from, *key),
            )
            connection.execute(
                "UPDATE work_units SET status='succeeded',updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s",
                (*key, claim.work_unit_id),
            )
            connection.execute(
                "UPDATE attempts SET disposition='succeeded',usage=%s,"
                "finished_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s",
                (Jsonb(usage), *key, claim.work_unit_id, claim.attempt_id),
            )
            connection.execute(
                "UPDATE operations SET status='succeeded',stage_state=stage_state||%s,"
                "updated_at=clock_timestamp() WHERE tenant_id=%s AND user_id=%s "
                "AND operation_id=%s",
                (
                    Jsonb(
                        {
                            "profile_current": not inputs.pending_inputs and not uncovered,
                            "pending_inputs": inputs.pending_inputs,
                            "failed_inputs": inputs.failed_inputs,
                            "blocked_inputs": inputs.blocked_inputs,
                            "uncovered_inputs": uncovered,
                            "generation_calls": generation_calls,
                            "usage": usage,
                            "embedding_usage": embedding_usage,
                        }
                    ),
                    *key,
                    claim.operation_id,
                ),
            )
    except Exception as error:
        if isinstance(error, ModelOutputError) and error.usage is not None:
            if model_stage == "embedding":
                embedding_usage = error.usage.model_dump(mode="json")
            elif model_stage == "profile":
                usage = error.usage.model_dump(mode="json")
        attempt_usage: dict[str, object] = dict(usage)
        if embedding_usage:
            attempt_usage["embedding_usage"] = embedding_usage
        connection_context = (
            pool.connection(timeout=config.database.pool_acquire_timeout_seconds)
            if pool is not None
            else psycopg.connect(content_dsn)
        )
        with connection_context as connection, connection.transaction():
            bind_user(connection.cursor(), authority)
            owned = connection.execute(
                "UPDATE work_units SET status='failed_terminal',updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND owner_id=%s "
                "AND fence=%s AND status='running' AND lease_expires_at>clock_timestamp() "
                "RETURNING work_unit_id",
                (*key, claim.work_unit_id, worker_id, claim.fence),
            ).fetchone()
            if not (
                isinstance(error, httpx.RequestError)
                and not isinstance(error, httpx.ConnectTimeout)
                and model_stage != "embedding"
            ):
                connection.execute(
                    "UPDATE attempts SET disposition='failed',usage=COALESCE(%s,usage),"
                    "finished_at=clock_timestamp() "
                    "WHERE tenant_id=%s AND user_id=%s AND work_unit_id=%s AND attempt_id=%s "
                    "AND owner_id=%s AND fence=%s AND finished_at IS NULL",
                    (
                        Jsonb(attempt_usage) if attempt_usage else None,
                        *key,
                        claim.work_unit_id,
                        claim.attempt_id,
                        worker_id,
                        claim.fence,
                    ),
                )
            elif attempt_usage:
                connection.execute(
                    "UPDATE attempts SET usage=%s WHERE tenant_id=%s AND user_id=%s "
                    "AND work_unit_id=%s AND attempt_id=%s AND owner_id=%s AND fence=%s "
                    "AND finished_at IS NULL",
                    (
                        Jsonb(attempt_usage),
                        *key,
                        claim.work_unit_id,
                        claim.attempt_id,
                        worker_id,
                        claim.fence,
                    ),
                )
            if owned:
                failure_state: dict[str, object] = {"error": type(error).__name__}
                if usage:
                    failure_state["usage"] = usage
                if embedding_usage:
                    failure_state["embedding_usage"] = embedding_usage
                connection.execute(
                    "UPDATE operations SET status='partial_failed',stage_state=stage_state||%s,"
                    "updated_at=clock_timestamp() WHERE tenant_id=%s AND user_id=%s "
                    "AND operation_id=%s",
                    (Jsonb(failure_state), *key, claim.operation_id),
                )
        raise
