"""Process one requested Profile refresh without a source-bound IndexStore."""

from __future__ import annotations

import hashlib
import json
import os
import time

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.db.context import bind_user
from oam.models.runtime import ModelOutputError
from oam.profiles.delta import (
    INCREMENTAL_SYSTEM,
    build_incremental_document,
    incremental_candidates,
    incremental_system,
    parse_profile_blocks,
    prune_profile_baseline,
)
from oam.profiles.document import build_profile_document
from oam.profiles.inputs import MAX_PROMPT_UNITS, load_profile_inputs
from oam.profiles.prompt import PROFILE_SYSTEM
from oam.profiles.selection import PROFILE_PURPOSE, pack_profile_request, rank_profile_candidates
from oam.profiles.service import profile_identity, profile_query_prefix
from oam.tokens import resolve_counter
from oam.workers.claims import ClaimedWork


class StaleProfileWork(RuntimeError):
    """A claimed refresh no longer owns the current target."""


def _latest_snapshot(connection, authority: AuthorizedUser):
    """Worker-only baseline, including a closed copy sanitized by removal cleanup."""
    with connection.cursor(row_factory=dict_row) as cursor:
        bind_user(cursor, authority)
        cursor.execute(
            'SELECT profile_revision,content,instruction,support_ids,"references",input_manifest '
            "FROM profiles WHERE tenant_id=%s AND user_id=%s AND profile_id='user' "
            "ORDER BY profile_revision DESC LIMIT 1",
            (authority.tenant_id, authority.user_id),
        )
        return cursor.fetchone()


def _snapshot_fingerprint(snapshot) -> str:
    return json.dumps(snapshot, sort_keys=True, default=str)


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
    started = time.monotonic()
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
            baseline = _latest_snapshot(connection, authority)
            baseline_fingerprint = _snapshot_fingerprint(baseline)
            inputs = load_profile_inputs(
                connection,
                authority,
                generation,
                insights_enabled=config.index.insights_enabled,
                include_manifest=True,
                stored_support_ids=set(baseline["support_ids"]) if baseline else None,
            )
            digest, dependencies, scope_identity, instruction, requested_mode = connection.execute(
                "SELECT w.input_digest,w.dependency_ids,o.stage_state->>'scope_identity',"
                "o.stage_state->>'instruction',o.stage_state->>'mode' "
                "FROM work_units w JOIN operations o "
                "USING(tenant_id,user_id,operation_id) "
                "WHERE w.tenant_id=%s AND w.user_id=%s AND w.work_unit_id=%s",
                (*key, claim.work_unit_id),
            ).fetchone()
            requested_mode = "incremental" if requested_mode in {None, "auto"} else requested_mode
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
            manifest = dict(inputs.manifest)
            recipe = profile_identity(
                bytes(32),
                instruction,
                config.profile.max_input_tokens,
                profile_query_prefix(config),
                counter.identity,
                config.profile.max_memory_tokens,
            )
            manifest["recipe"] = hashlib.sha256(recipe + INCREMENTAL_SYSTEM.encode()).hexdigest()
            candidate_ids = None
            candidate_keys: set[str] = set()
            pruned_blocks = 0
            actual_mode, fallback_reason = "full", None
            if requested_mode != "full":
                fallback_reason = "incompatible_baseline"
                if (
                    baseline
                    and baseline["content"]
                    and instruction is None
                    and not inputs.exact_fields
                ):
                    remaining = incremental_candidates(baseline.get("input_manifest"), manifest)
                    if remaining is not None:
                        try:
                            baseline, pruned_blocks = prune_profile_baseline(
                                baseline,
                                manifest,
                                inputs.stored_support_inputs,
                                max_content_bytes=max(
                                    config.profile.max_content_bytes,
                                    len(baseline["content"].encode()),
                                ),
                            )
                            blocks = parse_profile_blocks(
                                baseline["content"], baseline["support_ids"]
                            )
                        except ValueError:
                            pass
                        else:
                            candidate_keys = remaining
                            candidate_ids = [
                                identifier
                                for identifier in inputs.supported
                                if hashlib.sha256(identifier.encode()).hexdigest() in remaining
                            ]
                            actual_mode, fallback_reason = "incremental", None
            system = incremental_system(blocks) if actual_mode == "incremental" else PROFILE_SYSTEM
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
            needs_model = (
                bool(candidate_ids)
                if actual_mode == "incremental"
                else bool(inputs.prompt_inputs or instruction and inputs.exact_fields)
            )
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

                def select_candidates(candidate_scope):
                    connection_context = (
                        pool.connection(timeout=config.database.pool_acquire_timeout_seconds)
                        if pool is not None
                        else psycopg.connect(content_dsn)
                    )
                    with connection_context as connection, connection.transaction():
                        bind_user(connection.cursor(), authority)
                        _guard(connection, claim, worker_id)
                        selected_ids = rank_profile_candidates(
                            connection,
                            embedding.vectors[0],
                            MAX_PROMPT_UNITS,
                            **(
                                {"candidate_ids": candidate_scope}
                                if candidate_scope is not None
                                else {}
                            ),
                        )
                        ranked = load_profile_inputs(
                            connection,
                            authority,
                            generation,
                            insights_enabled=config.index.insights_enabled,
                            selected_unit_ids=selected_ids,
                            include_manifest=True,
                        )
                        if ranked.manifest != inputs.manifest:
                            raise StaleProfileWork("Profile support changed during selection")
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
                    return ranked.prompt_inputs

                candidates = select_candidates(candidate_ids)
            else:
                candidates = []

            def pack_candidates():
                return pack_profile_request(
                    candidates,
                    exact_fields=inputs.exact_fields,
                    instruction=instruction,
                    system=system,
                    max_input_tokens=config.profile.max_input_tokens,
                    max_memory_tokens=config.profile.max_memory_tokens,
                    counter=counter,
                )

            try:
                packed = pack_candidates()
            except ValueError:
                if actual_mode != "incremental":
                    raise
                actual_mode, fallback_reason = "full", "incremental_input_budget"
                system = PROFILE_SYSTEM
                candidates = select_candidates(None)
                packed = pack_candidates()
            selected_inputs = packed.inputs
            examined_before = (
                {
                    key
                    for key in baseline["input_manifest"]["examined"]
                    if manifest["units"].get(key) == baseline["input_manifest"]["units"].get(key)
                }
                if actual_mode == "incremental"
                else set()
            )
            examined = examined_before | {
                hashlib.sha256(item["id"].encode()).hexdigest() for item in selected_inputs
            }
            uncovered = eligible_count - len(examined)
            input_coverage = {
                "unexamined": uncovered - packed.uncovered,
                "budget_omitted": packed.uncovered,
                "selected": len(examined),
            }
            print(
                json.dumps(
                    {
                        "stage": "profile_selection",
                        "operation_id": claim.operation_id,
                        "eligible": eligible_count,
                        "candidates": len(candidates),
                        "selected": len(selected_inputs),
                        "unexamined": input_coverage["unexamined"],
                        "mode": actual_mode,
                        "fallback_reason": fallback_reason,
                        "previously_examined": len(examined_before),
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
                system=system,
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
            examined = (
                {
                    key
                    for key in baseline["input_manifest"]["examined"]
                    if manifest["units"].get(key) == baseline["input_manifest"]["units"].get(key)
                }
                if actual_mode == "incremental"
                else set()
            )
            uncovered = len(inputs.supported) - len(examined)
            input_coverage = {
                "unexamined": uncovered,
                "budget_omitted": 0,
                "selected": len(examined),
            }
        manifest["examined"] = sorted(examined)
        manifest["pending"] = sorted(candidate_keys - examined)
        if fallback_reason == "incremental_input_budget":
            manifest["fallback_reason"] = fallback_reason
        if actual_mode == "incremental":
            document = build_incremental_document(
                content if needs_model else '{"no_change":true,"edits":[]}',
                baseline,
                selected_inputs,
                max_content_bytes=config.profile.max_content_bytes,
            )
        else:
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
                connection,
                authority,
                generation,
                insights_enabled=config.index.insights_enabled,
                include_manifest=True,
            )
            current_baseline = _latest_snapshot(connection, authority)
            if _snapshot_fingerprint(current_baseline) != baseline_fingerprint:
                raise StaleProfileWork("Profile baseline changed during generation")
            if current.manifest != inputs.manifest:
                raise StaleProfileWork("Profile support changed during generation")
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
                "generation_id,visible_from,published_at,input_manifest,input_coverage) "
                "VALUES (%s,%s,'user',%s,'{}'::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,"
                "clock_timestamp(),%s,%s)",
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
                    Jsonb(manifest),
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
                            "mode": actual_mode,
                            "requested_mode": requested_mode or "incremental",
                            "fallback_reason": fallback_reason,
                            "pruned_blocks": pruned_blocks,
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
        print(
            json.dumps(
                {
                    "stage": "profile_refresh_published",
                    "operation_id": claim.operation_id,
                    "mode": actual_mode,
                    "fallback_reason": fallback_reason,
                    "pruned_blocks": pruned_blocks,
                    "generation_calls": generation_calls,
                    "uncovered_inputs": uncovered,
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                }
            ),
            flush=True,
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
