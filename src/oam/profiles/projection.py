"""Stored user Profile field composition and request-scoped selection."""

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from oam.tokens import estimate_units, estimated_tokens

SECTIONS = ("main", "preferences", "experiences")
KEYS = {
    "main": {"role", "location", "constraint", "date_of_birth", "sex", "household_size"},
    "preferences": {
        "language",
        "response_style",
        "tool",
        "workflow",
        "example_language",
        "interests",
    },
    "experiences": {"episode"},
}


def _field_id(
    section: str, key: str, qualifier: str, supports: list[dict], *, generated_value: Any = None
) -> str:
    if generated_value is not None:
        suffix = ["generated", generated_value]
    elif section == "experiences":
        suffix = [ref.get("id", ref.get("resource_id")) for ref in supports]
    else:
        suffix = sorted(
            ref["resource_id"] for ref in supports if ref.get("kind") == "profile_resource"
        )
    identity = json.dumps([section, key, qualifier, suffix], sort_keys=True, ensure_ascii=False)
    return "pf_" + hashlib.sha256(identity.encode()).hexdigest()[:24]


def build_sections(
    content: str,
    supported: Mapping[str, dict[str, Any]],
    exact: Iterable[dict[str, Any]],
    *,
    max_fields: int,
    max_value_bytes: int,
) -> dict[str, list[dict[str, Any]]]:
    try:
        generated = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Profile generation is not valid JSON") from exc
    if not isinstance(generated, dict) or set(generated) != set(SECTIONS):
        raise ValueError("Profile generation has invalid sections")
    sections: dict[str, list[dict[str, Any]]] = {section: [] for section in SECTIONS}
    fixed_keys = set()
    for field in exact:
        section, key = field["section"], field["key"]
        if section not in sections or key not in KEYS[section]:
            raise ValueError("Profile Resource mapped an unsupported field")
        value = field["value"]
        if len(json.dumps(value, ensure_ascii=False).encode()) > max_value_bytes:
            raise ValueError("Profile Resource value exceeds limit")
        item = dict(field)
        item["supports"] = list(field["supports"])
        item.setdefault("qualifier", "")
        item.setdefault("assertion_kind", "stated")
        item.setdefault("state", "active")
        item.setdefault("field_id", _field_id(section, key, item["qualifier"], item["supports"]))
        sections[section].append(item)
        fixed_keys.add((section, key, item["qualifier"]))
    for section in SECTIONS:
        fields = generated[section]
        if not isinstance(fields, list):
            raise ValueError("Profile section must be a list")
        for field in fields:
            if not isinstance(field, dict) or not {"key", "value", "support_ids"} <= field.keys():
                raise ValueError("Profile field has invalid shape")
            key, value, support_ids = field["key"], field["value"], field["support_ids"]
            qualifier = field.get("qualifier", "")
            if key not in KEYS[section] or not isinstance(qualifier, str):
                raise ValueError("Profile field uses an unsupported key or qualifier")
            if section == "experiences":
                if (
                    key != "episode"
                    or not isinstance(value, dict)
                    or not {"situation", "action", "observed_outcome", "conditions"} <= value.keys()
                ):
                    raise ValueError("experience requires a supported episode")
            elif not isinstance(value, (str, int, list)):
                raise ValueError("Profile value has invalid type")
            if len(json.dumps(value, ensure_ascii=False).encode()) > max_value_bytes:
                raise ValueError("Profile value exceeds limit")
            if (
                not isinstance(support_ids, list)
                or not support_ids
                or len(set(support_ids)) != len(support_ids)
                or any(identifier not in supported for identifier in support_ids)
            ):
                raise ValueError("Profile field has unsupported source")
            if (section, key, qualifier) in fixed_keys:
                continue
            supports = [supported[identifier] for identifier in support_ids]
            sections[section].append(
                {
                    "field_id": _field_id(section, key, qualifier, supports, generated_value=value),
                    "section": section,
                    "key": key,
                    "value": value,
                    "qualifier": qualifier,
                    "assertion_kind": "inferred"
                    if any(ref["kind"] == "insight" for ref in supports)
                    else "stated",
                    "supports": supports,
                    "state": "active",
                }
            )
        by_claim = {}
        for item in sections[section]:
            claim = (
                item["key"],
                item["qualifier"],
                json.dumps(item["value"], sort_keys=True, ensure_ascii=False),
            )
            if claim in by_claim:
                by_claim[claim]["supports"].extend(item["supports"])
            else:
                by_claim[claim] = item
        sections[section] = list(by_claim.values())
        for item in sections[section]:
            if any(ref["kind"] == "insight" for ref in item["supports"]):
                item["assertion_kind"] = "inferred"
            if section != "experiences" and any(
                other["key"] == item["key"]
                and other["qualifier"] == item["qualifier"]
                and other["value"] != item["value"]
                for other in sections[section]
            ):
                item["state"] = "conflict"
        if len(sections[section]) > max_fields:
            raise ValueError("Profile section exceeds field limit")
        sections[section].sort(key=lambda item: (item["key"], item["qualifier"], item["field_id"]))
    return sections


def select_sections(
    sections: Mapping[str, list[dict[str, Any]]],
    requested: list[str],
    eligible_support_ids: set[str],
    *,
    max_tokens: int,
) -> dict[str, Any]:
    if len(set(requested)) != len(requested) or any(
        section not in SECTIONS for section in requested
    ):
        raise ValueError("invalid Profile section selection")
    if max_tokens < 0:
        raise ValueError("Profile token budget cannot be negative")
    selected = {section: [] for section in SECTIONS if section in requested}
    used_units = 0
    stale = False
    omitted = []
    for section in SECTIONS:
        if section not in selected:
            continue
        for field in sections.get(section, []):
            supports = field.get("supports", [])
            if not supports or any(
                _support_identity(ref) not in eligible_support_ids for ref in supports
            ):
                stale = True
                continue
            encoded = json.dumps(field, ensure_ascii=False, sort_keys=True)
            next_units = used_units + estimate_units(encoded)
            if estimated_tokens(next_units) > max_tokens:
                omitted.append({"field_id": field["field_id"], "reason": "budget"})
                continue
            selected[section].append(field)
            used_units = next_units
    return {
        "sections": selected,
        "freshness": "stale" if stale else "current",
        "omitted": omitted,
        "tokens": estimated_tokens(used_units),
    }


def _support_identity(ref: Mapping[str, Any]) -> str:
    if ref.get("kind") == "profile_resource":
        return f"profile_resource:{ref['resource_id']}:{ref['resource_version']}"
    return str(ref["id"])
