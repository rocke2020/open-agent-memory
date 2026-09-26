"""Deterministic mappings for registered structured Profile Resources."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal


@dataclass(frozen=True)
class ResourceSupport:
    resource_id: str
    resource_version: int
    subtype: str
    source_revision: str | None
    as_of: datetime | None
    content_path: str


@dataclass(frozen=True)
class MappedProfileField:
    section: Literal["main", "preferences"]
    key: str
    value: str | int | list[str]
    qualifier: str | None
    support: ResourceSupport
    assertion_kind: Literal["stated"] = "stated"


def _nonempty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{path} must be a nonempty string without surrounding whitespace")
    return value


def _check_keys(value: Mapping[str, Any], allowed: set[str], required: set[str], path: str) -> None:
    unexpected = value.keys() - allowed
    missing = required - value.keys()
    if unexpected or missing:
        raise ValueError(
            f"{path} has unexpected {sorted(unexpected)} or missing {sorted(missing)} keys"
        )


def _as_of(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("as_of must be a timezone-aware ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("as_of must be a timezone-aware ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as_of must be a timezone-aware ISO datetime")
    return parsed


def map_profile_resource(
    resource: Mapping[str, Any], *, resource_version: int
) -> tuple[MappedProfileField, ...]:
    """Validate one registered Profile Resource and return exact supported fields."""
    if not isinstance(resource, Mapping):
        raise ValueError("resource must be an object")
    _check_keys(
        resource,
        {"resource_id", "resource_type", "subtype", "source_revision", "as_of", "content"},
        {"resource_id", "resource_type", "subtype", "content"},
        "resource",
    )
    if resource["resource_type"] != "profile":
        raise ValueError("resource_type must be profile")
    subtype = resource["subtype"]
    if subtype not in {"basic-profile/v1", "preferences/v1"}:
        raise ValueError("subtype is not registered")
    resource_id = _nonempty_string(resource["resource_id"], "resource_id")
    if type(resource_version) is not int or resource_version < 1:
        raise ValueError("resource_version must be a positive integer")
    source_revision = resource.get("source_revision")
    if source_revision is not None:
        source_revision = _nonempty_string(source_revision, "source_revision")
    as_of = _as_of(resource.get("as_of"))
    content = resource["content"]
    if not isinstance(content, Mapping):
        raise ValueError("content must be an object")

    def field(
        section: Literal["main", "preferences"],
        key: str,
        value: str | int | list[str],
        qualifier: str | None = None,
    ) -> MappedProfileField:
        return MappedProfileField(
            section=section,
            key=key,
            value=value,
            qualifier=qualifier,
            support=ResourceSupport(
                resource_id=resource_id,
                resource_version=resource_version,
                subtype=subtype,
                source_revision=source_revision,
                as_of=as_of,
                content_path=f"content.{key}",
            ),
        )

    if subtype == "preferences/v1":
        _check_keys(
            content, {"example_language", "domain"}, {"example_language", "domain"}, "content"
        )
        language = _nonempty_string(content["example_language"], "content.example_language")
        domain = _nonempty_string(content["domain"], "content.domain")
        return (field("preferences", "example_language", language, domain),)

    _check_keys(
        content,
        {"date_of_birth", "sex", "household_size", "interests"},
        set(),
        "content",
    )
    if not content:
        raise ValueError("content must contain a registered field")
    fields: list[MappedProfileField] = []
    if "date_of_birth" in content:
        value = content["date_of_birth"]
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("content.date_of_birth must be an ISO date")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("content.date_of_birth must be an ISO date") from exc
        fields.append(field("main", "date_of_birth", value))
    if "sex" in content:
        fields.append(field("main", "sex", _nonempty_string(content["sex"], "content.sex")))
    if "household_size" in content:
        value = content["household_size"]
        if type(value) is not int or value < 1:
            raise ValueError("content.household_size must be a positive integer")
        fields.append(field("main", "household_size", value))
    if "interests" in content:
        value = content["interests"]
        if not isinstance(value, list) or not value:
            raise ValueError("content.interests must be a nonempty list of strings")
        interests = [_nonempty_string(item, "content.interests") for item in value]
        fields.append(field("preferences", "interests", interests))
    return tuple(fields)
