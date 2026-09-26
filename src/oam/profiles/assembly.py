"""Attach requested stored Profile fields to non-generative recall."""

import json

from oam.profiles.service import ProfileService
from oam.tokens import estimate_units, estimated_tokens


def search_with_profile(config, pool, recall_service, authority, request, *, deadline_at=None):
    if not request.profile_sections:
        return recall_service.search(authority, request, deadline_at=deadline_at)
    budget = request.max_context_tokens or config.context.default_tokens
    profile_limit = min(
        budget // 2,
        sum(
            getattr(config.profile, f"{section}_tokens")
            for section in request.profile_sections
            if section in {"main", "preferences", "experiences"}
        ),
    )
    service = ProfileService(config, pool)
    profile = service.get(authority, request.profile_sections, profile_limit)
    has_fields = any(profile["sections"].values())
    profile_block = (
        "Profile: "
        + json.dumps(profile["sections"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if has_fields
        else ""
    )
    profile_tokens = estimated_tokens(estimate_units(profile_block))
    memory_budget = max(1, budget - profile_tokens - (8 if profile_block else 0))
    memory_request = request.model_copy(
        update={"profile_sections": [], "max_context_tokens": memory_budget}
    )
    result = recall_service.search(authority, memory_request, deadline_at=deadline_at)
    context = "\n\n".join(part for part in (profile_block, result["context"]) if part)
    total_tokens = estimated_tokens(estimate_units(context))
    if total_tokens > budget:
        raise ValueError("Profile and recall context exceed the shared token budget")
    service.check_policy(authority, profile["policy_epoch"])
    return {**result, "profile": profile, "context": context, "context_tokens": total_tokens}
