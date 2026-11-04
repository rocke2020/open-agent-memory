from __future__ import annotations

import hashlib
import json
import math
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
    StrictBool,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from oam import paths


class ConfigurationError(ValueError):
    """The public OAM behavior configuration is invalid."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceConfig(FrozenModel):
    max_output_tokens: PositiveInt
    max_bytes: PositiveInt
    url_fetch_timeout_seconds: PositiveInt
    pdf_child_timeout_seconds: PositiveInt
    max_pdf_pages: PositiveInt
    max_images: PositiveInt
    max_input_pixels_per_image: PositiveInt
    max_pixels_per_image: PositiveInt
    max_total_pixels: PositiveInt


class APIConfig(FrozenModel):
    max_concurrent_requests: PositiveInt
    max_concurrent_writes: PositiveInt
    max_request_bytes: PositiveInt

    @model_validator(mode="after")
    def validate_write_capacity(self) -> APIConfig:
        if self.max_concurrent_writes >= self.max_concurrent_requests:
            raise ValueError("write capacity must leave capacity for reads")
        return self


class IngestionChunkConfig(FrozenModel):
    target_estimated_tokens: PositiveInt
    max_estimated_tokens: PositiveInt
    extractable_overlap_estimated_tokens: NonNegativeInt
    previous_context_estimated_tokens: PositiveInt

    @model_validator(mode="after")
    def validate_target(self) -> IngestionChunkConfig:
        if self.target_estimated_tokens > self.max_estimated_tokens:
            raise ValueError("target_estimated_tokens cannot exceed max_estimated_tokens")
        return self


class ExtractionConfig(FrozenModel):
    max_input_tokens: PositiveInt
    group_max_estimated_output_tokens: PositiveInt
    max_output_tokens: PositiveInt
    max_facts_per_group: PositiveInt
    group_max_passages: PositiveInt
    reference_max_facts: NonNegativeInt
    reference_max_input_tokens: PositiveInt


class SearchTier(FrozenModel):
    context_max_tokens: PositiveInt
    graph: Literal["off", "light", "full"]
    causal_graph: bool
    lexical_ranking: Literal["per_term", "whole_query"]
    partial_match_gate: Literal["off", "user_fact_exchange"]
    adjacent_user_context: bool
    adjacent_user_context_max_chars: int = Field(gt=0, strict=True)
    channel_candidates: PositiveInt
    fused_candidates: PositiveInt
    source_seed_results: PositiveInt
    max_results: PositiveInt
    rerank_candidates: PositiveInt | None = None

    @model_validator(mode="after")
    def validate_caps(self) -> SearchTier:
        if self.max_results > self.fused_candidates:
            raise ValueError("max_results cannot exceed fused_candidates")
        if self.rerank_candidates and self.rerank_candidates > self.fused_candidates:
            raise ValueError("rerank_candidates cannot exceed fused_candidates")
        return self


class NormalArmWeights(FrozenModel):
    lexical: PositiveFloat
    dense: PositiveFloat
    graph: PositiveFloat
    temporal: PositiveFloat


class SearchConfig(FrozenModel):
    max_query_tokens: PositiveInt
    max_queries: PositiveInt
    query_concurrency: PositiveInt
    rrf_k: PositiveInt
    fts_rank_weights: tuple[float, float, float, float]
    query_entity_boost: float = Field(ge=0, le=1)
    normal_arm_weights: NormalArmWeights
    graph_seed_min_similarity: float = Field(ge=0, le=1)
    graph_seed_max: PositiveInt
    relation_hops: Literal[2]
    relation_candidates: PositiveInt
    entity_candidates_per_entity: PositiveInt
    entities_per_seed: PositiveInt
    links_per_seed: PositiveInt
    temporal_seed_max: PositiveInt
    temporal_coverage_buckets: PositiveInt
    temporal_neighbors: PositiveInt
    temporal_nodes: PositiveInt
    temporal_semantic_floor: float = Field(ge=0, le=1)
    ann_iterative_scan: Literal["strict_order"]
    ann_scan_max_tuples: PositiveInt
    fast: SearchTier
    normal: SearchTier

    @field_validator("fts_rank_weights")
    @classmethod
    def validate_fts_rank_weights(cls, weights: tuple[float, float, float, float]):
        if any(not math.isfinite(weight) or not 0 <= weight <= 1 for weight in weights):
            raise ValueError("FTS rank weights must be finite values between 0 and 1")
        return weights


class RecallConfig(FrozenModel):
    turn_deadline_seconds: PositiveFloat
    fast_deadline_seconds: PositiveFloat
    normal_deadline_seconds: PositiveFloat


class ChunkConfig(FrozenModel):
    ingestion: IngestionChunkConfig


class ProfileConfig(FrozenModel):
    enabled: bool = True
    max_response_tokens: PositiveInt
    max_content_bytes: PositiveInt
    max_output_tokens: PositiveInt
    attempt_timeout_seconds: PositiveFloat


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
    embedded_enabled: bool
    max_tasks: PositiveInt
    claim_semantic_links: bool = True


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
    worker_pool_max: PositiveInt = 10
    pool_min_size: PositiveInt = 1
    pool_acquire_timeout_seconds: PositiveFloat = 5.0
    indexing_concurrency: PositiveInt
    vector_index_mode: Literal["small", "large", "growing"]
    vector_index_min_rows: PositiveInt
    vector_index_poll_seconds: PositiveInt

    @model_validator(mode="after")
    def validate_pool_sizes(self) -> DatabaseConfig:
        if not math.isfinite(self.pool_acquire_timeout_seconds):
            raise ValueError("pool_acquire_timeout_seconds must be finite")
        if self.pool_min_size > self.api_pool_max:
            raise ValueError("pool_min_size cannot exceed api_pool_max")
        if self.pool_min_size > self.worker_pool_max:
            raise ValueError("pool_min_size cannot exceed worker_pool_max")
        return self


class AuthConfig(FrozenModel):
    mode: Literal["dev", "api_key", "trusted"] | None
    root_key_env: str = Field(min_length=1)
    cache_ttl_seconds: PositiveInt
    cache_max_entries: PositiveInt
    credential_page_size: PositiveInt


class IngestConfig(FrozenModel):
    max_bytes: PositiveInt
    max_messages: PositiveInt


class QueueConfig(FrozenModel):
    max_pending_per_user: PositiveInt


class ProviderConfig(FrozenModel):
    llm_concurrency: PositiveInt
    embedding_concurrency: PositiveInt
    embedding_search_reserved: NonNegativeInt
    rerank_concurrency: PositiveInt

    @model_validator(mode="after")
    def validate_embedding_capacity(self) -> ProviderConfig:
        if self.embedding_search_reserved >= self.embedding_concurrency:
            raise ValueError("embedding reservation must leave capacity for indexing")
        return self


class UserConfig(FrozenModel):
    max_reports: PositiveInt
    max_chats: PositiveInt
    concurrency: PositiveInt


class InsightSourceBatchConfig(FrozenModel):
    seed_max: PositiveInt = Field(le=64)
    input_max_tokens: PositiveInt
    target_max: PositiveInt = Field(le=128)


class IndexConfig(FrozenModel):
    enable_causal_graph: bool
    insights_enabled: bool = True
    insight_consolidation_delay_hours: PositiveFloat
    insight_generation_concurrency: PositiveInt
    insight_seed_policy: Literal["related", "source"]
    insight_source_batch: InsightSourceBatchConfig | None = None
    insight_seed_max: PositiveInt = Field(le=64)
    insight_candidate_max: PositiveInt = Field(le=64)
    insight_max_output_tokens: int = Field(ge=1024)
    embedding_dimensions: PositiveInt = Field(le=2000)
    semantic_link_min_similarity: float = Field(ge=0, le=1)
    semantic_link_max_neighbors: PositiveInt
    semantic_link_ann_iterative_scan: Literal["strict_order"]
    semantic_link_ann_scan_max_tuples: PositiveInt
    temporal_link_max_neighbors: PositiveInt
    temporal_link_horizon_seconds: PositiveFloat
    entity_name_max_bytes: PositiveInt
    entity_resolution_candidates: PositiveInt
    entity_search_names_per_entity: PositiveInt
    entity_search_rank_normalization: Literal[0, 1, 2]


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
    TEXT = "text"
    VISION = "vision"
    EMBEDDINGS = "embeddings"
    RERANK = "rerank"


class ModelBinding(FrozenModel):
    protocol: Literal["openai_chat", "openai_responses", "anthropic_messages", "openai_compatible"]
    reasoning_split: StrictBool | None = None
    reasoning_mode: Literal["legacy_chat", "effort", "provider_default"] = "legacy_chat"
    model: str | None = Field(default=None, min_length=1)
    model_env: str | None = Field(default=None, min_length=1)
    endpoint_env: str = Field(min_length=1)
    api_key_env: str | None = Field(default=None, min_length=1)
    capabilities: frozenset[ModelCapability]

    @model_validator(mode="after")
    def validate_model_source(self) -> ModelBinding:
        if (self.model is None) == (self.model_env is None):
            raise ValueError("exactly one of model or model_env is required")
        return self

    @field_serializer("capabilities")
    def serialize_capabilities(self, capabilities: frozenset[ModelCapability]) -> list[str]:
        return sorted(capability.value for capability in capabilities)


class EmbeddingModelBinding(ModelBinding):
    query_prefix: str = ""


ReasoningEffort = Literal["none", "low", "high", "max"]


class ReasoningEfforts(FrozenModel):
    extraction: ReasoningEffort | None = None
    insights: ReasoningEffort | None = None
    abstract: ReasoningEffort | None = None
    profile: ReasoningEffort | None = None
    planner: ReasoningEffort | None = None


ROLE_CAPABILITIES: dict[str, tuple[ModelCapability, ...]] = {
    "extraction": (ModelCapability.TEXT, ModelCapability.VISION),
    "insights": (ModelCapability.TEXT,),
    "embedding": (ModelCapability.EMBEDDINGS,),
    "abstract": (ModelCapability.TEXT,),
    "profile": (ModelCapability.TEXT,),
    "reranker": (ModelCapability.RERANK,),
    "planner": (ModelCapability.TEXT,),
}
REQUIRED_MODEL_ROLES = frozenset({"extraction", "insights", "embedding", "abstract", "profile"})


class ModelsConfig(FrozenModel):
    vlm: ModelBinding | None = None
    reasoning_effort: ReasoningEfforts = Field(default_factory=ReasoningEfforts)
    embedding: EmbeddingModelBinding | None = None
    reranker: ModelBinding | None = None

    @model_validator(mode="after")
    def validate_roles(self) -> ModelsConfig:
        if self.vlm is not None and self.vlm.protocol not in {
            "openai_chat",
            "openai_responses",
            "anthropic_messages",
        }:
            raise ValueError("models.vlm.protocol must be a generative protocol")
        if self.vlm is not None and self.vlm.protocol != "openai_chat":
            if self.vlm.reasoning_split is not None:
                raise ValueError("reasoning_split requires openai_chat")
            if self.vlm.reasoning_mode == "legacy_chat":
                raise ValueError("legacy_chat reasoning requires openai_chat")
        if (
            self.vlm is not None
            and self.vlm.protocol == "anthropic_messages"
            and self.vlm.reasoning_mode == "effort"
            and any(value == "none" for value in self.reasoning_effort.model_dump().values())
        ):
            raise ValueError("anthropic_messages does not support effort none")
        for role in ("embedding", "reranker"):
            binding = getattr(self, role)
            if binding is not None and binding.protocol != "openai_compatible":
                raise ValueError(f"models.{role}.protocol must be openai_compatible")
            if binding is not None and (
                binding.reasoning_split is not None or binding.reasoning_mode != "legacy_chat"
            ):
                raise ValueError(f"models.{role} does not support generation reasoning options")
        for role, capabilities in ROLE_CAPABILITIES.items():
            binding = self.binding_for(role)
            if binding is None:
                if (
                    role in ReasoningEfforts.model_fields
                    and getattr(self.reasoning_effort, role) is not None
                ):
                    raise ValueError(f"models.reasoning_effort.{role} requires models.vlm")
                continue
            for capability in capabilities:
                if capability not in binding.capabilities:
                    raise ValueError(f"model role {role} requires capability {capability.value!r}")
        return self

    def required_unbound_roles(self) -> set[str]:
        return {role for role in REQUIRED_MODEL_ROLES if self.binding_for(role) is None}

    def binding_for(self, role: str) -> ModelBinding | None:
        if role in {"embedding", "reranker"}:
            return getattr(self, role)
        if role not in ReasoningEfforts.model_fields:
            raise ValueError(f"unknown model role {role!r}")
        return None if getattr(self.reasoning_effort, role) is None else self.vlm


class OAMConfig(FrozenModel):
    contract_version: Literal["0.0.1"]
    resource: ResourceConfig
    api: APIConfig
    chunk: ChunkConfig
    extraction: ExtractionConfig
    search: SearchConfig
    index: IndexConfig
    recall: RecallConfig
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
    user: UserConfig
    trace: TraceConfig
    evidence: EvidenceConfig
    audit: AuditConfig
    export: ExportConfig
    backup: BackupConfig
    models: ModelsConfig = Field(default_factory=ModelsConfig)

    def for_insight_source_batch(self) -> OAMConfig:
        """Apply source-batch packing limits without changing L1 or target lookup limits."""
        batch = self.index.insight_source_batch
        if batch is None:
            return self
        return self.model_copy(
            update={
                "extraction": self.extraction.model_copy(
                    update={"max_input_tokens": batch.input_max_tokens}
                ),
                "index": self.index.model_copy(
                    update={
                        "insight_seed_max": batch.seed_max,
                        "insight_candidate_max": batch.target_max,
                    }
                ),
            }
        )

    def required_model_roles(self) -> frozenset[str]:
        roles = set(REQUIRED_MODEL_ROLES)
        if not self.index.insights_enabled:
            roles.discard("insights")
        if not self.profile.enabled:
            roles.discard("profile")
        return frozenset(roles)

    def required_unbound_roles(self) -> set[str]:
        return {
            role for role in self.required_model_roles() if self.models.binding_for(role) is None
        }


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
