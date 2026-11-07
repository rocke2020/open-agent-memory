"""Identity of the content-producing recipe, independent of recall tuning."""

import hashlib
import json
from collections.abc import Mapping

from oam.config import OAMConfig
from oam.models.bindings import resolve_model_binding
from oam.models.embedding_inputs import configured_embedding_admission
from oam.retrieval.fts import FTS_RECIPE
from oam.tokens import ESTIMATOR

INDEX_RECIPE = "oam-source-fact-insight-v39"
RESOURCE_VISION_RECIPE = "oam-vision-downscale-v1"
INSIGHT_RETRIEVAL_RECIPE = "oam-incremental-insight-v3"
INSIGHT_MODEL_RECIPE = "oam-insight-model-v1"


def index_generation(config: OAMConfig, environment: Mapping[str, str]) -> str:
    roles = {}
    roles_to_hash: tuple[str, ...] = (
        "extraction",
        "insights",
        "embedding",
        "abstract",
    )
    if not config.index.insights_enabled:
        roles_to_hash = tuple(role for role in roles_to_hash if role != "insights")
    for role in roles_to_hash:
        binding = resolve_model_binding(config.models, role, environment)
        if binding is not None:
            role_identity: dict[str, object] = {
                "protocol": binding.protocol,
                "reasoning_effort": binding.reasoning_effort,
                "thinking": binding.thinking,
            }
            if binding.reasoning_mode != "legacy_chat":
                role_identity["reasoning_mode"] = binding.reasoning_mode
            if binding.reasoning_split is not None:
                role_identity["reasoning_split"] = binding.reasoning_split
            # VLM_BASE_URL, VLM_API_KEY, and VLM_MODEL select the transport at
            # runtime. Route and provider naming are deliberately excluded from
            # content-generation identity so an equivalent binding can resume
            # the same indexed generation.
            if role == "embedding":
                role_identity.update({"model": binding.model, "endpoint": binding.endpoint})
            roles[role] = role_identity
    extraction = config.extraction.model_dump(mode="json")
    references_enabled = bool(config.extraction.reference_max_facts)
    grouping_enabled = config.extraction.group_max_passages > 1
    if not grouping_enabled:
        extraction.pop("group_max_passages")
        extraction.pop("group_max_estimated_output_tokens")
    if not references_enabled:
        extraction.pop("reference_max_facts")
        extraction.pop("reference_max_input_tokens")
    identity = {
        "recipe": INDEX_RECIPE,
        "fts_recipe": FTS_RECIPE,
        "estimator": ESTIMATOR,
        "chunk": config.chunk.ingestion.model_dump(mode="json"),
        "extraction": extraction,
        "resource_max_output_tokens": config.resource.max_output_tokens,
        "resource_vision": {
            "recipe": RESOURCE_VISION_RECIPE,
            "max_pixels_per_image": config.resource.max_pixels_per_image,
        },
        "index": config.index.model_dump(
            mode="json",
            exclude={
                "insight_consolidation_delay_hours",
                "insight_generation_concurrency",
                "insight_seed_policy",
                "insight_source_batch",
                "generation_token_budget",
            }
            | (
                set()
                if config.index.insights_enabled
                else {"insight_seed_max", "insight_candidate_max", "insight_max_output_tokens"}
            ),
        ),
        "roles": roles,
    }
    admission = configured_embedding_admission(config, environment)
    ingestion_counter = admission.counter.identity
    limits = [
        bound
        for bound in (admission.max_input_tokens, admission.max_request_tokens)
        if bound is not None
    ]
    effective_limit = min(limits) if limits else None
    # Preserve the existing recipe identity when the explicit legacy path is unchanged.
    if (
        ingestion_counter != ESTIMATOR
        or admission.counter.identity != ESTIMATOR
        or effective_limit is not None
    ):
        identity["ingestion_counter"] = ingestion_counter
        identity["embedding_input"] = {
            "counter": admission.counter.identity,
            "max_input_tokens": effective_limit,
        }
    if config.index.insights_enabled:
        identity["insight_retrieval_recipe"] = INSIGHT_RETRIEVAL_RECIPE
    return (
        "gen_"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
