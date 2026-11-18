"""Generate a transient, instruction-directed Profile from currently eligible evidence."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

import httpx
from psycopg.errors import QueryCanceled
from psycopg_pool import PoolTimeout

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.db.context import bind_user
from oam.db.inspection import SourceInspector, SourcePolicyChanged
from oam.indexing.identity import index_generation
from oam.models.runtime import ModelOutputError, RuntimeModels
from oam.profiles.document import build_profile_document
from oam.profiles.inputs import MAX_PROMPT_UNITS, ProfileInputs, load_profile_inputs
from oam.profiles.prompt import PROFILE_SYSTEM
from oam.profiles.resources import public_profile_data_support
from oam.profiles.selection import pack_profile_request, rank_profile_candidates
from oam.profiles.service import ProfileInputsNotReady, profile_query_prefix


class _ProfileGenerationFailure(RuntimeError):
    def __init__(self, message: str, *, usage=None, embedding_usage=None, generation_calls=0):
        super().__init__(message)
        self.usage = dict(usage or {})
        self.embedding_usage = dict(embedding_usage or {})
        self.generation_calls = generation_calls


class ProfileGenerationUnavailable(_ProfileGenerationFailure):
    """The provider did not return a usable generated Profile."""


class ProfileGenerationChanged(_ProfileGenerationFailure):
    """Evidence changed while the transient Profile was being generated."""


class ProfileGenerationDeadlineExceeded(_ProfileGenerationFailure, TimeoutError):
    """The total transient generation deadline expired."""


class ProfileGenerator:
    def __init__(self, config: OAMConfig, pool, models: RuntimeModels):
        self.config, self.pool, self.models = config, pool, models

    def generate(
        self,
        authority: AuthorizedUser,
        instruction: str,
        *,
        reauthorize: Callable[[Any], None] | None = None,
    ) -> dict[str, Any]:
        if not authority.allows("memory:read"):
            raise PermissionError("memory:read is required")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("Profile generation requires a nonempty instruction")
        deadline = time.monotonic() + self.config.profile.attempt_timeout_seconds
        usage: dict[str, Any] = {}
        embedding_usage: dict[str, Any] = {}
        generation_calls = 0
        admitted = False

        def failure(kind, message):
            return kind(
                message,
                usage=usage,
                embedding_usage=embedding_usage,
                generation_calls=generation_calls,
            )

        def attach_usage(error):
            for name, value in (
                ("usage", dict(usage)),
                ("embedding_usage", dict(embedding_usage)),
                ("generation_calls", generation_calls),
            ):
                setattr(error, name, value)

        def call_model(method, *args, embedding=False, **kwargs):
            nonlocal usage, embedding_usage, generation_calls
            timeout = remaining()
            if not embedding:
                generation_calls += 1
            try:
                result = method(*args, timeout_seconds=timeout, **kwargs)
            except httpx.TimeoutException:
                raise failure(
                    ProfileGenerationDeadlineExceeded,
                    "Profile generation provider deadline exceeded",
                ) from None
            except ModelOutputError as error:
                if error.usage is not None:
                    reported = error.usage.model_dump(mode="json")
                    if embedding:
                        embedding_usage = reported
                    else:
                        usage = reported
                raise failure(
                    ProfileGenerationUnavailable,
                    "Profile generation provider output is unavailable",
                ) from None
            except httpx.HTTPError:
                raise failure(
                    ProfileGenerationUnavailable, "Profile generation provider is unavailable"
                ) from None
            reported = result.usage.model_dump(mode="json")
            if embedding:
                embedding_usage = reported
            else:
                usage = reported
            return result

        def remaining() -> float:
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise failure(
                    ProfileGenerationDeadlineExceeded, "Profile generation deadline exceeded"
                )
            return budget

        @contextmanager
        def connect():
            try:
                with self.pool.connection(
                    timeout=min(self.config.database.pool_acquire_timeout_seconds, remaining())
                ) as connection:
                    yield connection
            except (QueryCanceled, PoolTimeout):
                raise failure(
                    ProfileGenerationDeadlineExceeded,
                    "Profile generation database deadline exceeded",
                ) from None

        def bound_database(connection):
            connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(max(1, int(remaining() * 1000))),),
            )

        def load(connection, generation, selected=None):
            try:
                return load_profile_inputs(
                    connection,
                    authority,
                    generation,
                    insights_enabled=self.config.index.insights_enabled,
                    selected_unit_ids=selected,
                    include_manifest=True,
                )
            except PermissionError as error:
                attach_usage(error)
                raise
            except ValueError:
                if not admitted:
                    raise
                raise failure(
                    ProfileGenerationChanged, "Profile inputs changed during generation"
                ) from None

        def unchanged(before: ProfileInputs, after: ProfileInputs) -> None:
            if (
                before.identity != after.identity
                or before.scope_identity != after.scope_identity
                or before.manifest != after.manifest
                or before.eligible_support_ids != after.eligible_support_ids
            ):
                raise failure(ProfileGenerationChanged, "Profile inputs changed during generation")

        with connect() as connection, connection.transaction():
            bound_database(connection)
            bind_user(connection.cursor(), authority)
            row = connection.execute(
                "SELECT index_generation_id FROM users WHERE tenant_id=%s AND user_id=%s",
                (authority.tenant_id, authority.user_id),
            ).fetchone()
            admitted_generation = row[0] if row else None
            generation = admitted_generation or index_generation(self.config, os.environ)
            inputs = load(connection, generation)
        admitted = True
        if inputs.pending_inputs or inputs.failed_inputs or inputs.blocked_inputs:
            raise ProfileInputsNotReady("Profile evidence is not ready")
        if not inputs.supported and not inputs.exact_fields:
            raise ValueError("Profile generation has no eligible evidence")
        candidates = []
        if inputs.supported:
            embedding = call_model(
                self.models.embed,
                (profile_query_prefix(self.config) + instruction,),
                embedding=True,
            )
            with connect() as connection, connection.transaction():
                bound_database(connection)
                bind_user(connection.cursor(), authority)
                selected = rank_profile_candidates(
                    connection, embedding.vectors[0], MAX_PROMPT_UNITS
                )
                ranked = load(connection, generation, selected)
                unchanged(inputs, ranked)
                candidates = ranked.prompt_inputs
        packed = pack_profile_request(
            candidates,
            exact_fields=inputs.exact_fields,
            instruction=instruction,
            system=PROFILE_SYSTEM,
            max_input_tokens=self.config.profile.max_input_tokens,
            max_memory_tokens=self.config.profile.max_memory_tokens,
            counter=self.models.ingestion_counter,
        )
        result = call_model(
            self.models.generate,
            "profile",
            system=PROFILE_SYSTEM,
            user=packed.user,
            max_output_tokens=self.config.profile.max_output_tokens,
        )
        remaining()
        try:
            document = build_profile_document(
                result.content,
                packed.inputs,
                inputs.exact_fields,
                instruction=instruction,
                max_content_bytes=self.config.profile.max_content_bytes,
                append_exact_values=False,
            )
            markers = {int(number) for number in re.findall(r"\[(\d+)\]", document["content"])}
            if markers and markers != set(range(1, len(document["support_ids"]) + 1)):
                raise ValueError("inconsistent reference markers")
        except (ValueError, TypeError, KeyError, IndexError):
            raise failure(
                ProfileGenerationUnavailable, "Profile generation returned an invalid document"
            ) from None
        with connect() as connection, connection.transaction():
            bound_database(connection)
            current = load(connection, generation)
            unchanged(inputs, current)
            row = connection.execute(
                "SELECT index_generation_id FROM users WHERE tenant_id=%s AND user_id=%s",
                (authority.tenant_id, authority.user_id),
            ).fetchone()
            if row is None or row[0] != admitted_generation:
                raise failure(
                    ProfileGenerationChanged, "Profile index generation changed during generation"
                )
            if not set(document["support_ids"]) <= current.eligible_support_ids:
                raise failure(ProfileGenerationChanged, "Profile support changed during generation")
            try:
                SourceInspector._check_policy(connection, authority, inputs.policy_epoch)
                if reauthorize is not None:
                    reauthorize(connection)
            except (SourcePolicyChanged, PermissionError) as error:
                attach_usage(error)
                raise
        remaining()
        return {
            "content": document["content"],
            "references": [
                public_profile_data_support(reference)
                if reference.get("kind") == "profile_resource"
                else reference
                for reference in document["references"]
            ],
            "selection_coverage": {
                "selected": len(packed.inputs),
                "unexamined": len(inputs.supported) - len(candidates),
                "budget_omitted": packed.uncovered,
            },
            "usage": result.usage.model_dump(mode="json"),
            "embedding_usage": embedding_usage,
            "generation_calls": 1,
        }
