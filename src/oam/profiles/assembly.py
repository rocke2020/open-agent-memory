"""Attach one complete stored Profile document to non-generative recall."""

from oam.profiles.service import ProfileService
from oam.tokens import estimate_units, estimated_tokens


def search_with_profile(config, pool, recall_service, authority, request, *, deadline_at=None):
    if not request.include_profile:
        return recall_service.search(authority, request, deadline_at=deadline_at)
    budget = request.max_context_tokens or config.context.default_tokens
    profile_limit = max(0, budget // 2 - 4)
    service = ProfileService(config, pool)
    profile = service.get(authority, profile_limit)
    profile_block = "Profile:\n" + profile["content"] if profile["content"] else ""
    profile_tokens = estimated_tokens(estimate_units(profile_block))
    memory_budget = max(1, budget - profile_tokens - (8 if profile_block else 0))
    memory_request = request.model_copy(
        update={"include_profile": False, "max_context_tokens": memory_budget}
    )
    result = recall_service.search(authority, memory_request, deadline_at=deadline_at)
    context = "\n\n".join(part for part in (profile_block, result["context"]) if part)
    total_tokens = estimated_tokens(estimate_units(context))
    if total_tokens > budget:
        raise ValueError("Profile and recall context exceed the shared token budget")
    service.check_policy(authority, profile["policy_epoch"])
    return {**result, "profile": profile, "context": context, "context_tokens": total_tokens}
