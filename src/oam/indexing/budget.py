"""Conservative per-source admission before each actual generation call."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, cast

from psycopg.types.json import Jsonb

from oam.models.bindings import resolve_model_binding
from oam.models.protocols import generation_request
from oam.models.runtime import ModelOutputError, ModelResult, Usage

if TYPE_CHECKING:
    from oam.indexing.store import IndexStore


class GenerationBudgetRejection(ModelOutputError):
    """A refused call costs zero; split retries may attach earlier paid-call usage."""

    def __init__(self, message: str):
        super().__init__(
            message,
            usage=Usage(
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                reasoning_tokens=0,
                coverage="complete",
            ),
        )

    @property
    def has_prior_generation_usage(self) -> bool:
        return (
            self.usage is None or self.usage.total_tokens != 0 or self.usage.coverage != "complete"
        )


class GenerationBudgetExceeded(GenerationBudgetRejection):
    """The remaining source allowance cannot cover another complete request."""


class GenerationBudgetUnavailable(GenerationBudgetRejection):
    """Historical usage or the configured protocol has no supported bound."""


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
        self._reserve(role, kwargs)
        return cast(ModelResult, self._models.generate(role, **kwargs))

    def _reserve(self, role: str, kwargs: dict[str, Any]) -> None:
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
                    return
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
