"""Conservative per-source admission before each actual generation call."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, cast

import psycopg
from psycopg.types.json import Jsonb

from oam.models.bindings import resolve_model_binding
from oam.models.protocols import generation_request
from oam.models.runtime import (
    UNDISPATCHED_USAGE,
    ModelDispatchUnavailable,
    ModelOutputError,
    ModelResult,
)

if TYPE_CHECKING:
    from oam.indexing.store import IndexStore


class GenerationBudgetRejection(ModelOutputError):
    """A refused call costs zero; split retries may attach earlier paid-call usage."""

    def __init__(self, message: str):
        super().__init__(message, usage=UNDISPATCHED_USAGE)

    @property
    def has_prior_generation_usage(self) -> bool:
        return (
            self.usage is None or self.usage.total_tokens != 0 or self.usage.coverage != "complete"
        )


class GenerationBudgetExceeded(GenerationBudgetRejection):
    """The remaining source allowance cannot cover another complete request."""


class GenerationBudgetUnavailable(GenerationBudgetRejection):
    """Historical usage or the configured protocol has no supported bound."""


class BatchGenerationBudgetRejection(GenerationBudgetRejection):
    """A zero-call batch rejection identifies members to exclude before repacking."""

    def __init__(self, message: str, members: list[tuple[str, int]]):
        super().__init__(message)
        self.members = sorted(set(members))


class BatchGenerationBudgetExceeded(BatchGenerationBudgetRejection):
    """Some source shares exceed their pinned generation ceilings."""


class BatchGenerationBudgetUnavailable(BatchGenerationBudgetRejection):
    """Source ownership, usage or request bounds cannot safely be established."""


def _generation_stage(stage: str) -> bool:
    return "embed" not in stage and stage.startswith(("extract", "insight", "vision", "abstract"))


def _known_tokens(usage: Any) -> int:
    total = usage.get("total_tokens") if isinstance(usage, dict) else None
    if (
        not isinstance(usage, dict)
        or usage.get("coverage") != "complete"
        or type(total) is not int
        or total < 0
    ):
        raise GenerationBudgetUnavailable("generation budget requires complete historical usage")
    return total


class SourceBudgetModels:
    """Keep embeddings unchanged; charge a durable upper allowance for every text call."""

    def __init__(self, store: IndexStore, models: Any):
        self._store = store
        self._models = models

    def __getattr__(self, name: str) -> Any:
        return getattr(self._models, name)

    def generate(self, role: str, **kwargs: Any) -> ModelResult:
        reservation = self._reserve(role, kwargs)
        try:
            return cast(ModelResult, self._models.generate(role, **kwargs))
        except ModelDispatchUnavailable:
            if reservation is not None:
                self._release_undispatched(role, *reservation)
            raise

    def _reserve(self, role: str, kwargs: dict[str, Any]) -> tuple[str, int] | None:
        store = self._store
        rejected = False
        with store.transaction() as connection:
            store.guard(connection)
            owner = connection.execute(
                "SELECT o.operation_id,o.stage_state FROM operations o JOIN work_units w "
                "USING(tenant_id,user_id,operation_id) WHERE o.tenant_id=%s AND o.user_id=%s "
                "AND o.source_id=%s AND o.source_version=%s AND w.generation_id=%s "
                "AND o.kind='index_interaction' AND w.stage='index_source' "
                "ORDER BY o.accepted_seq,o.created_at,o.operation_id LIMIT 1",
                (
                    *store.key,
                    store.source["source_id"],
                    store.source["source_version"],
                    store.generation_id,
                ),
            ).fetchone()
            if owner is None:
                raise GenerationBudgetUnavailable("source generation budget owner is unavailable")
            state = owner[1].get("index_generation_budget")
            if state is None:
                limit = store.config.index.generation_token_budget
                if limit is None:
                    return None
                history = connection.execute(
                    "SELECT stage_key,usage FROM index_stage_results WHERE tenant_id=%s "
                    "AND user_id=%s AND source_id=%s AND source_version=%s AND generation_id=%s",
                    (
                        *store.key,
                        store.source["source_id"],
                        store.source["source_version"],
                        store.generation_id,
                    ),
                ).fetchall()
                tokens = sum(
                    _known_tokens(usage) for stage, usage in history if _generation_stage(stage)
                )
                attempts = connection.execute(
                    "SELECT a.disposition,a.request_ref,a.usage FROM attempts a JOIN work_units w "
                    "USING(tenant_id,user_id,work_unit_id) JOIN operations o "
                    "USING(tenant_id,user_id,operation_id) WHERE o.tenant_id=%s AND o.user_id=%s "
                    "AND o.source_id=%s AND o.source_version=%s AND w.generation_id=%s "
                    "AND a.attempt_id<>%s "
                    "AND a.disposition IN ('failed','unknown_external_outcome')",
                    (
                        *store.key,
                        store.source["source_id"],
                        store.source["source_version"],
                        store.generation_id,
                        store.claim.attempt_id,
                    ),
                ).fetchall()
                for disposition, stage, usage in attempts:
                    if disposition == "unknown_external_outcome" or stage is None:
                        raise GenerationBudgetUnavailable(
                            "source has unresolved historical model usage"
                        )
                    if _generation_stage(stage):
                        tokens += _known_tokens(usage)
                tokens += _historical_batch_tokens(
                    store, connection, (store.source["source_id"], store.source["source_version"])
                )
                state = {"limit": limit, "reserved_upper_tokens": tokens}
            limit, reserved = state.get("limit"), state.get("reserved_upper_tokens")
            if type(limit) is not int or limit <= 0 or type(reserved) is not int or reserved < 0:
                raise GenerationBudgetUnavailable("stored source generation budget is invalid")
            binding = resolve_model_binding(
                store.config.models, role, getattr(self._models, "_environment", os.environ)
            )
            if binding is None or binding.protocol not in {"openai_chat", "anthropic_messages"}:
                raise GenerationBudgetUnavailable(
                    "bounded generation requires a text protocol output ceiling"
                )
            _, _, payload = generation_request(
                binding, kwargs["system"], kwargs["user"], kwargs["max_output_tokens"]
            )
            allowance = (
                len(json.dumps(payload, ensure_ascii=False).encode()) + kwargs["max_output_tokens"]
            )
            rejected = reserved + allowance > limit
            if not rejected:
                state["reserved_upper_tokens"] = reserved + allowance
            connection.execute(
                "UPDATE operations SET stage_state=jsonb_set(stage_state,"
                "'{index_generation_budget}',%s::jsonb),updated_at=clock_timestamp() "
                "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                (Jsonb(state), *store.key, owner[0]),
            )
        print(
            json.dumps(
                {
                    "stage": "index_generation_budget",
                    "operation_id": store.claim.operation_id,
                    "role": role,
                    "limit": limit,
                    "reserved_upper_tokens": state["reserved_upper_tokens"],
                    "request_upper_tokens": allowance,
                    "outcome": "rejected" if rejected else "reserved",
                }
            ),
            flush=True,
        )
        if rejected:
            raise GenerationBudgetExceeded(
                "source generation token budget cannot cover the next call"
            )
        return owner[0], allowance

    def _release_undispatched(self, role: str, owner_id: str, allowance: int) -> None:
        from oam.indexing.store import StaleIndexWork

        store = self._store
        budget = None
        try:
            with store.transaction() as connection:
                store.guard(connection)
                counter = (
                    "(stage_state#>>'{index_generation_budget,reserved_upper_tokens}')::numeric"
                )
                updated = connection.execute(
                    "UPDATE operations SET stage_state=jsonb_set(stage_state,"
                    "'{index_generation_budget,reserved_upper_tokens}',"
                    f"to_jsonb({counter}-%s),false),updated_at=clock_timestamp() "
                    "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s "
                    f"AND {counter}>=%s RETURNING stage_state->'index_generation_budget'",
                    (allowance, *store.key, owner_id, allowance),
                ).fetchone()
            budget = updated[0] if updated is not None else None
        except (StaleIndexWork, psycopg.Error, TimeoutError):
            # A missing claim or uncertain commit must never trigger a second release.
            pass
        print(
            json.dumps(
                {
                    "stage": "index_generation_budget",
                    "operation_id": store.claim.operation_id,
                    "role": role,
                    "request_upper_tokens": allowance,
                    "outcome": "released_without_dispatch" if budget else "release_unconfirmed",
                    **(budget or {}),
                }
            ),
            flush=True,
        )


def _batch_seed_weights(inputs: dict[str, Any]) -> dict[tuple[str, int], int]:
    """Attribute public new-seed bodies; historical context does not own a share."""
    seeds = set(inputs["envelope"]["seed_ids"])
    public = {fact["id"]: fact for fact in inputs["envelope"]["facts"]}
    weights: dict[tuple[str, int], int] = {}
    found = set()
    for fact in inputs["private_facts"]:
        identifier = fact["id"]
        if identifier not in seeds:
            continue
        if identifier in found:
            raise BatchGenerationBudgetUnavailable("duplicate batch seed identity", list(weights))
        found.add(identifier)
        members = set()
        for reference in fact["source_refs"]:
            source, version = reference.rsplit(":", 1)
            if not source or int(version) <= 0:
                raise BatchGenerationBudgetUnavailable(
                    "invalid batch source reference", list(weights)
                )
            members.add((source, int(version)))
        if not members:
            raise BatchGenerationBudgetUnavailable("batch seed has no source owner", list(weights))
        size = len(
            json.dumps(public[identifier], ensure_ascii=False, separators=(",", ":")).encode()
        )
        for member in members:
            weights[member] = weights.get(member, 0) + size
    if found != seeds or not weights:
        raise BatchGenerationBudgetUnavailable("batch seed ownership is incomplete", list(weights))
    return weights


def _batch_shares(total: int, weights: dict[tuple[str, int], int]) -> dict[tuple[str, int], int]:
    """Largest-remainder rounding conserves every token; source identity breaks ties."""
    denominator = sum(weights.values())
    shares = {member: total * weight // denominator for member, weight in weights.items()}
    order = sorted(weights, key=lambda member: (-(total * weights[member] % denominator), member))
    for member in order[: total - sum(shares.values())]:
        shares[member] += 1
    return shares


def _historical_batch_tokens(store: Any, connection: Any, member: tuple[str, int]) -> int:
    """Initialize from existing paid evidence only; pinned balances never reread it."""
    groups = connection.execute(
        "SELECT g.stage_key,g.input_digest,g.inputs,g.usage,g.output IS NOT NULL "
        "FROM l2_batch_members bm JOIN l2_batch_groups g "
        "USING(tenant_id,user_id,batch_id) WHERE bm.tenant_id=%s AND bm.user_id=%s "
        "AND bm.source_id=%s AND bm.source_version=%s AND bm.generation_id=%s",
        (*store.key, *member, store.generation_id),
    ).fetchall()
    weights_by_stage = {}
    tokens = 0
    for stage, digest, inputs, usage, completed in groups:
        if not _generation_stage(stage):
            continue
        weights = _batch_seed_weights(inputs)
        weights_by_stage[stage + ":" + bytes(digest).hex()] = weights
        if completed and member in weights:
            tokens += _batch_shares(_known_tokens(usage), weights).get(member, 0)
    attempts = connection.execute(
        "SELECT a.disposition,a.request_ref,a.usage FROM l2_batch_members bm "
        "JOIN operations o ON (o.tenant_id,o.user_id,o.stage_state->>'l2_batch_id')="
        "(bm.tenant_id,bm.user_id,bm.batch_id) "
        "JOIN work_units w ON (w.tenant_id,w.user_id,w.operation_id)="
        "(o.tenant_id,o.user_id,o.operation_id) "
        "JOIN attempts a ON (a.tenant_id,a.user_id,a.work_unit_id)="
        "(w.tenant_id,w.user_id,w.work_unit_id) "
        "WHERE bm.tenant_id=%s AND bm.user_id=%s AND bm.source_id=%s "
        "AND bm.source_version=%s AND bm.generation_id=%s AND w.generation_id=%s "
        "AND w.stage='consolidate_batch' AND a.attempt_id<>%s "
        "AND a.disposition IN ('failed','unknown_external_outcome')",
        (*store.key, *member, store.generation_id, store.generation_id, store.claim.attempt_id),
    ).fetchall()
    for disposition, stage, usage in attempts:
        if stage in weights_by_stage and member not in weights_by_stage[stage]:
            continue
        if disposition == "unknown_external_outcome":
            raise GenerationBudgetUnavailable("source has unresolved historical batch usage")
        if stage is None:
            if _known_tokens(usage) == 0:
                continue
            raise GenerationBudgetUnavailable("historical batch attribution is unavailable")
        if not _generation_stage(stage):
            continue
        if stage not in weights_by_stage:
            raise GenerationBudgetUnavailable("historical batch attribution is unavailable")
        tokens += _batch_shares(_known_tokens(usage), weights_by_stage[stage]).get(member, 0)
    return tokens


class BatchBudgetModels:
    """Reserve a shared request atomically against the existing source budget owners."""

    def __init__(self, store: Any, models: Any, inputs: dict[str, Any]):
        self._store = store
        self._models = models
        self._inputs = inputs

    def __getattr__(self, name: str) -> Any:
        return getattr(self._models, name)

    def generate(self, role: str, **kwargs: Any) -> ModelResult:
        self._reserve(role, kwargs)
        return cast(ModelResult, self._models.generate(role, **kwargs))

    def _initial_state(self, connection: Any, member: tuple[str, int]) -> dict[str, Any] | None:
        store = self._store
        limit = store.config.index.generation_token_budget
        if limit is None:
            return None
        history = connection.execute(
            "SELECT stage_key,usage FROM index_stage_results WHERE tenant_id=%s "
            "AND user_id=%s AND source_id=%s AND source_version=%s AND generation_id=%s",
            (*store.key, *member, store.generation_id),
        ).fetchall()
        tokens = sum(_known_tokens(usage) for stage, usage in history if _generation_stage(stage))
        attempts = connection.execute(
            "SELECT a.disposition,a.request_ref,a.usage FROM attempts a JOIN work_units w "
            "USING(tenant_id,user_id,work_unit_id) JOIN operations o "
            "USING(tenant_id,user_id,operation_id) WHERE o.tenant_id=%s AND o.user_id=%s "
            "AND o.source_id=%s AND o.source_version=%s AND w.generation_id=%s "
            "AND a.attempt_id<>%s AND a.disposition IN ('failed','unknown_external_outcome')",
            (*store.key, *member, store.generation_id, store.claim.attempt_id),
        ).fetchall()
        for disposition, stage, usage in attempts:
            if disposition == "unknown_external_outcome" or stage is None:
                raise GenerationBudgetUnavailable("source has unresolved historical model usage")
            if _generation_stage(stage):
                tokens += _known_tokens(usage)
        tokens += _historical_batch_tokens(store, connection, member)
        return {"limit": limit, "reserved_upper_tokens": tokens}

    def _reserve(self, role: str, kwargs: dict[str, Any]) -> None:
        store = self._store
        try:
            weights = _batch_seed_weights(self._inputs)
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, BatchGenerationBudgetRejection):
                raise
            raise BatchGenerationBudgetUnavailable(
                "invalid frozen batch seed ownership", []
            ) from error
        unavailable = []
        owners = {}
        with store.transaction() as connection:
            store.guard(connection)
            for member in sorted(weights):
                owner = connection.execute(
                    "SELECT o.operation_id,o.stage_state FROM operations o JOIN work_units w "
                    "USING(tenant_id,user_id,operation_id) JOIN sources s "
                    "ON (s.tenant_id,s.user_id,s.source_id,s.active_version)="
                    "(o.tenant_id,o.user_id,o.source_id,o.source_version) "
                    "JOIN source_versions sv ON "
                    "(sv.tenant_id,sv.user_id,sv.source_id,sv.source_version)="
                    "(o.tenant_id,o.user_id,o.source_id,o.source_version) "
                    "WHERE o.tenant_id=%s AND o.user_id=%s AND o.source_id=%s "
                    "AND o.source_version=%s AND w.generation_id=%s "
                    "AND o.kind='index_interaction' AND w.stage='index_source' "
                    "AND s.status='active' AND sv.status='active' "
                    "ORDER BY o.accepted_seq,o.created_at,o.operation_id LIMIT 1",
                    (*store.key, *member, store.generation_id),
                ).fetchone()
                if owner is None:
                    unavailable.append(member)
                    continue
                try:
                    state = owner[1].get("index_generation_budget")
                    if state is None:
                        state = self._initial_state(connection, member)
                    if state is not None:
                        state = dict(state)
                        limit, reserved = state.get("limit"), state.get("reserved_upper_tokens")
                        if (
                            type(limit) is not int
                            or limit <= 0
                            or type(reserved) is not int
                            or reserved < 0
                        ):
                            raise GenerationBudgetUnavailable(
                                "stored source generation budget is invalid"
                            )
                    owners[member] = (owner[0], state)
                except (GenerationBudgetUnavailable, TypeError, ValueError):
                    unavailable.append(member)
            if unavailable:
                raise BatchGenerationBudgetUnavailable(
                    "batch source budget owner is unavailable", unavailable
                )
            limited_members = [member for member, (_, state) in owners.items() if state is not None]
            allowance = None
            try:
                binding = resolve_model_binding(
                    store.config.models, role, getattr(self._models, "_environment", os.environ)
                )
                if binding is not None and binding.protocol in {
                    "openai_chat",
                    "anthropic_messages",
                }:
                    _, _, payload = generation_request(
                        binding, kwargs["system"], kwargs["user"], kwargs["max_output_tokens"]
                    )
                    allowance = (
                        len(json.dumps(payload, ensure_ascii=False).encode())
                        + kwargs["max_output_tokens"]
                    )
            except ValueError as error:
                if limited_members:
                    raise BatchGenerationBudgetUnavailable(
                        "bounded generation model binding is unavailable", limited_members
                    ) from error
            if allowance is None and limited_members:
                raise BatchGenerationBudgetUnavailable(
                    "bounded generation requires a text protocol output ceiling", limited_members
                )
            shares = _batch_shares(allowance, weights) if allowance is not None else {}
            exhausted = [
                member
                for member, (_, state) in owners.items()
                if state is not None
                and state["reserved_upper_tokens"] + shares[member] > state["limit"]
            ]
            if exhausted:
                raise BatchGenerationBudgetExceeded(
                    "source generation token budget cannot cover its batch share", exhausted
                )
            for member, (operation_id, state) in owners.items():
                if state is None:
                    continue
                state["reserved_upper_tokens"] += shares[member]
                connection.execute(
                    "UPDATE operations SET stage_state=jsonb_set(stage_state,"
                    "'{index_generation_budget}',%s::jsonb),updated_at=clock_timestamp() "
                    "WHERE tenant_id=%s AND user_id=%s AND operation_id=%s",
                    (Jsonb(state), *store.key, operation_id),
                )
            allocation = {
                "attempt_id": store.claim.attempt_id,
                "request_upper_tokens": allowance,
                "source_shares": [
                    {
                        "source_id": member[0],
                        "source_version": member[1],
                        "seed_input_bytes": weights[member],
                        "attributed_upper_tokens": shares.get(member),
                        "budget_enabled": owners[member][1] is not None,
                    }
                    for member in sorted(weights)
                ],
            }
            from oam.indexing.store import input_digest

            saved = connection.execute(
                "UPDATE l2_batch_groups SET allocation=jsonb_set(allocation,'{calls}',"
                "COALESCE(allocation->'calls','[]'::jsonb)||%s::jsonb) "
                "WHERE tenant_id=%s AND user_id=%s AND batch_id=%s AND stage_key='insight' "
                "AND input_digest=%s RETURNING 1",
                (Jsonb([allocation]), *store.key, store.batch_id, input_digest(self._inputs)),
            ).fetchone()
            if saved is None:
                raise BatchGenerationBudgetUnavailable(
                    "frozen batch group accounting is unavailable", list(weights)
                )
        print(
            json.dumps(
                {
                    "stage": "index_generation_budget",
                    "batch_id": store.batch_id,
                    "operation_id": store.claim.operation_id,
                    "role": role,
                    "request_upper_tokens": allowance,
                    "outcome": "reserved",
                    "source_shares": allocation["source_shares"],
                }
            ),
            flush=True,
        )
