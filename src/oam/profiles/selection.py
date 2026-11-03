"""Pack selected Profile evidence within a complete-request estimate."""

import json
from dataclasses import dataclass
from typing import Any

from oam.db.eligibility import ELIGIBLE_UNITS
from oam.profiles.document import profile_data_support
from oam.tokens import TokenCounter

PROFILE_PURPOSE = (
    "Current information about the user: identity, background, relationships, "
    "enduring preferences and useful personal context."
)


def rank_profile_candidates(
    connection, vector: tuple[float, ...], limit: int, *, candidate_ids: list[str] | None = None
) -> list[str]:
    candidate_filter = " AND unit_id = ANY(%s)" if candidate_ids is not None else ""
    params: tuple[Any, ...] = (json.dumps(vector), limit)
    if candidate_ids is not None:
        params = (candidate_ids, *params)
    rows = connection.execute(
        f"WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS}) "
        "SELECT unit_id FROM eligible WHERE origin='memory' AND kind IN ('fact','insight') "
        f"{candidate_filter} ORDER BY embedding <=> %s::vector, unit_id LIMIT %s",
        params,
    ).fetchall()
    return [row[0] for row in rows]


@dataclass(frozen=True)
class PackedProfileRequest:
    inputs: list[dict[str, Any]]
    user: str
    uncovered: int
    memory_tokens: int
    input_tokens: int


def pack_profile_request(
    candidates: list[dict[str, Any]],
    *,
    exact_fields: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    instruction: str | None,
    system: str,
    max_input_tokens: int,
    max_memory_tokens: int,
    counter: TokenCounter,
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

    def fits(items: list[dict[str, Any]], user: str) -> bool:
        memory = json.dumps(items, ensure_ascii=False, sort_keys=True, default=str)
        return (
            counter.count(memory) <= max_memory_tokens
            and counter.count(system + user) <= max_input_tokens
        )

    user = serialize(selected)
    if not fits(selected, user):
        raise ValueError("Profile required input exceeds request budget")
    for item in candidates:
        proposed = serialize([*selected, item])
        if not fits([*selected, item], proposed) and any(
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
        if fits([*selected, item], proposed):
            selected.append(item)
            user = proposed
    if not selected and not model_fields:
        raise ValueError("Profile request has no fitting supported evidence")
    memory = json.dumps(selected, ensure_ascii=False, sort_keys=True, default=str)
    return PackedProfileRequest(
        selected,
        user,
        len(candidates) - len(selected),
        counter.count(memory),
        counter.count(system + user),
    )
