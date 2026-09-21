from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    ValidationError,
    field_serializer,
    model_validator,
)

from oam import paths


class ConfigurationError(ValueError):
    """The public OAM behavior configuration is invalid."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceConfig(FrozenModel):
    max_bytes: PositiveInt
    max_pdf_pages: PositiveInt
    max_images: PositiveInt
    max_pixels_per_image: PositiveInt
    max_total_pixels: PositiveInt


class APIConfig(FrozenModel):
    max_concurrent_requests: PositiveInt


class ChunkConfig(FrozenModel):
    target_estimated_tokens: PositiveInt
    max_estimated_tokens: PositiveInt
    extractable_overlap_estimated_tokens: NonNegativeInt
    previous_context_estimated_tokens: PositiveInt

    @model_validator(mode="after")
    def validate_target(self) -> ChunkConfig:
        if self.target_estimated_tokens > self.max_estimated_tokens:
            raise ValueError("target_estimated_tokens cannot exceed max_estimated_tokens")
        return self


class ExtractionConfig(FrozenModel):
    max_input_tokens: PositiveInt
    max_output_tokens: PositiveInt
    max_facts_per_group: PositiveInt


class SearchTier(FrozenModel):
    channel_candidates: PositiveInt
    fused_candidates: PositiveInt
    max_results: PositiveInt
    rerank_candidates: PositiveInt | None = None

    @model_validator(mode="after")
    def validate_caps(self) -> SearchTier:
        if self.max_results > self.fused_candidates:
            raise ValueError("max_results cannot exceed fused_candidates")
        if self.rerank_candidates and self.rerank_candidates > self.fused_candidates:
            raise ValueError("rerank_candidates cannot exceed fused_candidates")
        return self


class SearchConfig(FrozenModel):
    max_query_tokens: PositiveInt
    max_queries: PositiveInt
    query_concurrency: PositiveInt
    rrf_k: PositiveInt
    channel_weights: Literal["equal"]
    graph_seed_min_similarity: float = Field(ge=0, le=1)
    graph_seed_max: PositiveInt
    relation_hops: Literal[1]
    relation_candidates: PositiveInt
    entity_candidates_per_entity: PositiveInt
    entity_score_scale: PositiveFloat
    ann_iterative_scan: Literal["strict_order"]
    ann_scan_max_tuples: PositiveInt
    fast: SearchTier
    deep: SearchTier


class RecallConfig(FrozenModel):
    turn_deadline_seconds: PositiveFloat
    fast_deadline_seconds: PositiveFloat
    deep_deadline_seconds: PositiveFloat


class ContextConfig(FrozenModel):
    default_tokens: PositiveInt
    max_tokens: PositiveInt
    tokenizer: Literal["o200k_base"]

    @model_validator(mode="after")
    def validate_budget(self) -> ContextConfig:
        if self.default_tokens > self.max_tokens:
            raise ValueError("default_tokens cannot exceed max_tokens")
        return self


class RecalledChunksConfig(FrozenModel):
    default_tokens: PositiveInt
    max_tokens: PositiveInt

    @model_validator(mode="after")
    def validate_budget(self) -> RecalledChunksConfig:
        if self.default_tokens > self.max_tokens:
            raise ValueError("default_tokens cannot exceed max_tokens")
        return self


class ProfileConfig(FrozenModel):
    main_tokens: PositiveInt
    preferences_tokens: PositiveInt
    experiences_tokens: PositiveInt
    max_fields_per_section: PositiveInt
    max_value_bytes: PositiveInt


class PlannerConfig(FrozenModel):
    max_calls: Literal[1]
    max_context_tokens: PositiveInt


class RequestConfig(FrozenModel):
    batch_deadline_seconds: PositiveFloat
    delegated_deadline_seconds: PositiveFloat


class ModelRuntimeConfig(FrozenModel):
    attempt_timeout_seconds: PositiveFloat


class RetryConfig(FrozenModel):
    max_attempts: PositiveInt


class WorkerConfig(FrozenModel):
    max_tasks: PositiveInt
    max_model_calls: PositiveInt


class LeaseConfig(FrozenModel):
    seconds: PositiveInt
    heartbeat_seconds: PositiveInt
    shutdown_seconds: PositiveInt


class DatabaseConfig(FrozenModel):
    migration_dsn_env: str = Field(min_length=1)
    api_dsn_env: str = Field(min_length=1)
    worker_queue_dsn_env: str = Field(min_length=1)
    worker_content_dsn_env: str = Field(min_length=1)
    api_pool_max: PositiveInt
    worker_pool_max: PositiveInt


class AuthConfig(FrozenModel):
    cache_ttl_seconds: PositiveInt
    cache_max_entries: PositiveInt
    credential_page_size: PositiveInt


class IngestConfig(FrozenModel):
    max_bytes: PositiveInt
    max_messages: PositiveInt


class QueueConfig(FrozenModel):
    max_pending_per_scope: PositiveInt


class ProviderConfig(FrozenModel):
    global_concurrency: PositiveInt


class ScopeConfig(FrozenModel):
    max_reports: PositiveInt
    max_chats: PositiveInt
    concurrency: PositiveInt


class IndexConfig(FrozenModel):
    semantic_link_min_similarity: float = Field(ge=0, le=1)
    semantic_link_max_neighbors: PositiveInt


class TraceConfig(FrozenModel):
    content_retention_days: PositiveInt
    metadata_retention_days: PositiveInt


class EvidenceConfig(FrozenModel):
    default_retention_days: PositiveInt


class AuditConfig(FrozenModel):
    retention_days: PositiveInt


class ExportConfig(FrozenModel):
    retention_hours: PositiveInt


class BackupConfig(FrozenModel):
    retention_days: PositiveInt


class ModelCapability(StrEnum):
    CHAT = "chat"
    VISION = "vision"
    EMBEDDINGS = "embeddings"
    RERANK = "rerank"


class ModelBinding(FrozenModel):
    protocol: Literal["openai_compatible"]
    model: str = Field(min_length=1)
    endpoint_env: str = Field(min_length=1)
    api_key_env: str | None = Field(default=None, min_length=1)
    capabilities: frozenset[ModelCapability]

    @field_serializer("capabilities")
    def serialize_capabilities(self, capabilities: frozenset[ModelCapability]) -> list[str]:
        return sorted(capability.value for capability in capabilities)


class ModelRoles(FrozenModel):
    extraction: str | None = None
    insights: str | None = None
    embedding: str | None = None
    document_vision: str | None = None
    abstract: str | None = None
    profile: str | None = None
    reranker: str | None = None
    planner: str | None = None


ROLE_CAPABILITY: dict[str, ModelCapability] = {
    "extraction": ModelCapability.CHAT,
    "insights": ModelCapability.CHAT,
    "embedding": ModelCapability.EMBEDDINGS,
    "document_vision": ModelCapability.VISION,
    "abstract": ModelCapability.CHAT,
    "profile": ModelCapability.CHAT,
    "reranker": ModelCapability.RERANK,
    "planner": ModelCapability.CHAT,
}
REQUIRED_MODEL_ROLES = frozenset(
    {"extraction", "insights", "embedding", "document_vision", "abstract", "profile"}
)


class ModelsConfig(FrozenModel):
    roles: ModelRoles = Field(default_factory=ModelRoles)
    bindings: dict[str, ModelBinding] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_roles(self) -> ModelsConfig:
        for role, capability in ROLE_CAPABILITY.items():
            alias = getattr(self.roles, role)
            if alias is None:
                continue
            binding = self.bindings.get(alias)
            if binding is None:
                raise ValueError(f"models.roles.{role} references unknown binding {alias!r}")
            if capability not in binding.capabilities:
                raise ValueError(f"models.roles.{role} requires capability {capability.value!r}")
        return self

    def required_unbound_roles(self) -> set[str]:
        return {role for role in REQUIRED_MODEL_ROLES if getattr(self.roles, role) is None}

    def binding_for(self, role: str) -> ModelBinding | None:
        alias = getattr(self.roles, role)
        return None if alias is None else self.bindings[alias]


class OAMConfig(FrozenModel):
    contract_version: Literal["0.0.1"]
    resource: ResourceConfig
    api: APIConfig
    chunk: ChunkConfig
    extraction: ExtractionConfig
    search: SearchConfig
    index: IndexConfig
    recall: RecallConfig
    context: ContextConfig
    chunks: RecalledChunksConfig
    profile: ProfileConfig
    planner: PlannerConfig
    request: RequestConfig
    model: ModelRuntimeConfig
    retry: RetryConfig
    worker: WorkerConfig
    lease: LeaseConfig
    database: DatabaseConfig
    auth: AuthConfig
    ingest: IngestConfig
    queue: QueueConfig
    provider: ProviderConfig
    scope: ScopeConfig
    trace: TraceConfig
    evidence: EvidenceConfig
    audit: AuditConfig
    export: ExportConfig
    backup: BackupConfig
    models: ModelsConfig = Field(default_factory=ModelsConfig)


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    config: OAMConfig
    source_path: Path
    sha256: str


def load_config(path: str | Path) -> ResolvedConfig:
    """Load canonical YAML defaults, then apply an optional operator override."""
    source_path = Path(path).expanduser().resolve()
    canonical_path = paths.default_config_path()
    canonical_values = _read_config_mapping(canonical_path)
    try:
        config = OAMConfig.model_validate(canonical_values)
        if source_path != canonical_path.resolve():
            selected_values = _read_config_mapping(source_path)
            config = OAMConfig.model_validate(_overlay_config(canonical_values, selected_values))
    except ValidationError as exc:
        raise ConfigurationError(str(exc)) from exc
    canonical = json.dumps(
        config.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return ResolvedConfig(
        config=config,
        source_path=source_path,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _read_config_mapping(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load OAM config: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigurationError("OAM config must be a YAML mapping")
    return raw


def _overlay_config(base: dict, overrides: dict) -> dict:
    merged = base.copy()
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _overlay_config(merged[key], value)
        else:
            merged[key] = value
    return merged
