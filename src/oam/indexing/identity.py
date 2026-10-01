"""Identity of the content-producing recipe, independent of recall tuning."""

import hashlib
import json
from collections.abc import Mapping

from oam.config import OAMConfig
from oam.models.bindings import resolve_model_binding
from oam.retrieval.fts import FTS_RECIPE
from oam.tokens import ESTIMATOR

INDEX_RECIPE = "oam-source-fact-insight-v15"


def index_generation(config: OAMConfig, environment: Mapping[str, str]) -> str:
    roles = {}
    for role in ("extraction", "insights", "embedding", "abstract", "document_vision"):
        binding = resolve_model_binding(config.models, role, environment)
        if binding is not None:
            roles[role] = {
                "model": binding.model,
                "endpoint": binding.endpoint,
                "protocol": binding.protocol,
                "reasoning_effort": binding.reasoning_effort,
            }
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
        "index": config.index.model_dump(mode="json"),
        "roles": roles,
    }
    return (
        "gen_"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
