"""Pack selected Profile evidence within a complete-request estimate."""

import json
from dataclasses import dataclass
from typing import Any

from oam.db.eligibility import ELIGIBLE_UNITS
from oam.profiles.document import profile_data_support
from oam.tokens import estimate_units, estimated_tokens

PROFILE_PURPOSE = (
    "Current information about the user: identity, background, relationships, "
    "enduring preferences and useful personal context."
)


def rank_profile_candidates(connection, vector: tuple[float, ...], limit: int) -> list[str]:
    rows = connection.execute(
        f"WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS}) "
        "SELECT unit_id FROM eligible WHERE origin='memory' AND kind IN ('fact','insight') "
        "ORDER BY embedding <=> %s::vector, unit_id LIMIT %s",
        (json.dumps(vector), limit),
    ).fetchall()
    return [row[0] for row in rows]


@dataclass(frozen=True)
class PackedProfileRequest:
    inputs: list[dict[str, Any]]
    user: str
    uncovered: int


def pack_profile_request(
    candidates: list[dict[str, Any]],
    *,
    exact_fields: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    instruction: str | None,
    system: str,
    max_input_tokens: int,
) -> PackedProfileRequest:
    selected: list[dict[str, Any]] = []
    model_fields = [
        {**field, "id": profile_data_support(field)[0]} if field.get("supports") else field
        for field in exact_fields
    ]

    def serialize(items: list[dict[str, Any]]) -> str:
        return json.dumps(
            {"inputs": items, "exact_fields": model_fields, "instruction": instruction},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def fits(user: str) -> bool:
        text = system + user
        # The general estimator can substantially undercount identifier-heavy JSON.
        # Use a conservative byte ceiling as well; provider tokenization still varies.
        return (
            max(estimated_tokens(estimate_units(text)), len(text.encode("utf-8")))
            <= max_input_tokens
        )

    user = serialize(selected)
    if not fits(user):
        raise ValueError("Profile required input exceeds request budget")
    for item in candidates:
        proposed = serialize([*selected, item])
        if not fits(proposed) and any(
            "text" in passage for passage in item.get("source_passages", [])
        ):
            item = {
                **item,
                "source_passages": [
                    {key: value for key, value in passage.items() if key != "text"}
                    for passage in item["source_passages"]
                ],
                "source_passage_text_omitted": True,
            }
            proposed = serialize([*selected, item])
        if fits(proposed):
            selected.append(item)
            user = proposed
    if not selected and not model_fields:
        raise ValueError("Profile request has no fitting supported evidence")
    return PackedProfileRequest(selected, user, len(candidates) - len(selected))
