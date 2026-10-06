"""Validate service inputs without opening connections or dispatching model calls."""

from collections.abc import Mapping
from typing import Literal

import psycopg
from psycopg.conninfo import conninfo_to_dict

from oam.config import ROLE_CAPABILITY, ConfigurationError, ModelCapability, OAMConfig
from oam.models.bindings import ModelBindingResolutionError, resolve_model_binding


def validate_model_environment(config: OAMConfig, environment: Mapping[str, str]) -> None:
    missing = sorted(config.required_unbound_roles())
    if missing:
        raise ConfigurationError(f"missing required model roles: {', '.join(missing)}")
    roles = set(config.required_model_roles())
    roles.update(
        role for role in ("reranker", "planner") if getattr(config.models.roles, role) is not None
    )
    for role in sorted(roles):
        try:
            binding = resolve_model_binding(config.models, role, environment)
            if binding is not None and ROLE_CAPABILITY[role] in {
                ModelCapability.CHAT,
                ModelCapability.VISION,
            }:
                binding.chat_parameters()
        except ModelBindingResolutionError as error:
            raise ConfigurationError(str(error)) from None


def validate_startup_environment(
    config: OAMConfig,
    environment: Mapping[str, str],
    *,
    command: Literal["api", "worker", "migrate"],
) -> None:
    names = []
    if command == "migrate":
        names.append(config.database.migration_dsn_env)
    if command == "api":
        names.append(config.database.api_dsn_env)
    if command == "worker" or (command == "api" and config.worker.embedded_enabled):
        names.extend((config.database.worker_queue_dsn_env, config.database.worker_content_dsn_env))
    for name in names:
        dsn = environment.get(name, "").strip()
        if not dsn:
            raise ConfigurationError(f"missing required environment variable {name}")
        try:
            conninfo_to_dict(dsn)
        except psycopg.Error:
            raise ConfigurationError(
                f"{name} must be a valid PostgreSQL connection string"
            ) from None
    if command != "migrate":
        validate_model_environment(config, environment)
