"""Identity of the content-producing recipe, independent of recall tuning."""

import hashlib
import json
from collections.abc import Mapping

from oam.config import OAMConfig
from oam.models.bindings import resolve_model_binding

INDEX_RECIPE = "oam-source-fact-insight-v6"


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
    identity = {
        "recipe": INDEX_RECIPE,
        "chunk": config.chunk.model_dump(mode="json"),
        "extraction": config.extraction.model_dump(mode="json"),
        "index": config.index.model_dump(mode="json"),
        "roles": roles,
    }
    return (
        "gen_"
        + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
