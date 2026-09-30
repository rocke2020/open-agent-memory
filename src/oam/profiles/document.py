"""Validate generated Profile text and attach eligible, readable references."""

from __future__ import annotations

import json
import re
from typing import Any


def _resource_support(field: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    support = field["supports"][0]
    identifier = f"profile_resource:{support['resource_id']}:{support['resource_version']}"
    return identifier, {
        "support_id": identifier,
        "kind": "profile_resource",
        "resource_id": support["resource_id"],
        "resource_version": support["resource_version"],
    }


def build_profile_document(
    generation: str | None,
    prompt_inputs: list[dict[str, Any]],
    exact_fields: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    instruction: str | None,
    max_content_bytes: int,
) -> dict[str, Any]:
    """Build one document while keeping support identities outside generated text."""
    parsed = (
        json.loads(generation) if generation is not None else {"content": "", "support_ids": []}
    )
    if not isinstance(parsed, dict) or set(parsed) != {"content", "support_ids"}:
        raise ValueError("Profile generation must contain content and support_ids")
    body, selected_ids = parsed["content"], parsed["support_ids"]
    if not isinstance(body, str) or not isinstance(selected_ids, list):
        raise ValueError("Profile content or support_ids are invalid")
    selected_ids = list(selected_ids)
    body = body.strip()
    available = {item["id"]: item for item in prompt_inputs}
    resource_refs = dict(_resource_support(field) for field in exact_fields)
    if any(
        not isinstance(identifier, str)
        or identifier not in available
        and identifier not in resource_refs
        for identifier in selected_ids
    ):
        raise ValueError("Profile generation cited unsupported input")
    if len(set(selected_ids)) != len(selected_ids):
        raise ValueError("Profile generation repeated a support")
    if body and not selected_ids:
        raise ValueError("Profile generation has no eligible support")
    references = []
    for identifier in selected_ids:
        if identifier in resource_refs:
            references.append(resource_refs[identifier])
            continue
        item = available[identifier]
        passages = []
        seen_passages = set()
        for passage in item["source_passages"]:
            identity = (passage["source_id"], passage["source_version"], passage["passage_id"])
            if identity in seen_passages:
                continue
            seen_passages.add(identity)
            passages.append(
                {
                    "source_id": passage["source_id"],
                    "source_version": passage["source_version"],
                    "passage_id": passage["passage_id"],
                    "event_time": passage.get("event_time"),
                }
            )
        if not passages:
            raise ValueError("Profile support has no eligible passage")
        references.append(
            {
                "support_id": identifier,
                "kind": item["kind"],
                **({"revision": item["revision"]} if item["kind"] == "insight" else {}),
                **passages[0],
                "passages": passages,
            }
        )
    exact_lines = []
    for field in exact_fields:
        identifier, reference = _resource_support(field)
        if identifier not in selected_ids:
            selected_ids.append(identifier)
            references.append(reference)
        label = field["key"].replace("_", " ").capitalize()
        value = field["value"]
        rendered = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        exact_lines.append(f"- {label}: {rendered} [{selected_ids.index(identifier) + 1}]")
    if instruction is None:
        if "## " in body:
            raise ValueError("Profile main content contains a heading")
        markers = {int(number) for number in re.findall(r"\[(\d+)\]", body)}
        if body and markers != set(range(1, len(parsed["support_ids"]) + 1)):
            raise ValueError("Profile main content has an invalid reference marker")
        main = "\n\n".join(part for part in (body, "\n".join(exact_lines)) if part)
        cited = []
        for number, reference in enumerate(references, start=1):
            if reference["kind"] == "profile_resource":
                location = "Provided profile details"
            else:
                location = "; ".join(
                    f"Conversation on {passage['event_time'][:10]}"
                    if passage.get("event_time")
                    else f"Conversation {passage['source_id']}"
                    for passage in reference["passages"]
                )
            cited.append(f"{number}. {location}")
        content = f"## Main content\n\n{main or 'No supported details yet.'}\n\n## References\n\n"
        content += "\n".join(cited) if cited else "No references yet."
    else:
        if not body:
            raise ValueError("Profile generation has empty custom content")
        content = body
    if len(content.encode("utf-8")) > max_content_bytes:
        raise ValueError("Profile content exceeds the configured size limit")
    return {"content": content, "support_ids": selected_ids, "references": references}
