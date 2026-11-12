"""Administrator-facing user directory contracts."""

import re
from datetime import UTC, datetime

from pydantic import AwareDatetime, Field, field_serializer

from oam.contracts.operations import FrozenContract

USER_LIST_DEFAULT_LIMIT = 100
USER_LIST_MAX_LIMIT = 10_000
USER_LIST_MAX_OFFSET = 2**63 - 1  # PostgreSQL OFFSET uses a signed bigint.
_RFC3339 = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)


def parse_last_write_at_gte(value: str | None) -> datetime | None:
    if value is None:
        return None
    if _RFC3339.fullmatch(value) is None:
        raise ValueError("last_write_at_gte must be an RFC 3339 timestamp with UTC offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ValueError("last_write_at_gte must be an RFC 3339 timestamp") from exc


class UserListItem(FrozenContract):
    user_id: str
    last_write_at: AwareDatetime | None

    @field_serializer("last_write_at")
    def serialize_last_write_at(self, value: datetime | None) -> str | None:
        return None if value is None else value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class UserListResponse(FrozenContract):
    items: tuple[UserListItem, ...]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=USER_LIST_MAX_LIMIT)
    offset: int = Field(ge=0, le=USER_LIST_MAX_OFFSET)
