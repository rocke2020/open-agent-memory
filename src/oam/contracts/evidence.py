from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from oam.config import OAMConfig
from oam.quotes import unique_quote

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SourceText = Annotated[str, StringConstraints(min_length=1)]
SourceVersion = Annotated[int, Field(strict=True, ge=1)]


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _reference_timezone(value: str | None) -> str | None:
    if value is not None:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("reference_timezone must name an IANA timezone") from exc
    return value


class SourceMetadata(FrozenContract):
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None
    reference_timezone: NonBlank | None = None

    @field_validator("source_created_at", "source_updated_at")
    @classmethod
    def require_aware_source_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("source timestamps must be timezone-aware")
        return value

    @field_validator("reference_timezone")
    @classmethod
    def validate_reference_timezone(cls, value: str | None) -> str | None:
        return _reference_timezone(value)


class SourceInputMetadata(SourceMetadata):
    """Stored indexing envelope; version fields are assigned by source admission."""

    source_version: SourceVersion = 1
    expected_active_version: SourceVersion | None = None

    @model_validator(mode="after")
    def require_next_version(self) -> "SourceInputMetadata":
        if self.source_version != (self.expected_active_version or 0) + 1:
            raise ValueError(
                "source_version must follow expected_active_version; "
                "a new source starts at version 1"
            )
        return self


class TemporalAnnotation(FrozenContract):
    role: Literal["event", "date_reference"]
    start: datetime
    end: datetime
    quote: SourceText

    @field_validator("start", "end", mode="before")
    @classmethod
    def require_date_representation(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("annotation time must be an explicit timezone-aware date")
        return value

    @model_validator(mode="after")
    def validate_annotation(self) -> "TemporalAnnotation":
        if self.start.utcoffset() is None or self.end.utcoffset() is None:
            raise ValueError("annotation times must be timezone-aware")
        if self.start > self.end:
            raise ValueError("annotation start must not exceed its end")
        return self

    def validate_quote(self, content: str) -> None:
        if not unique_quote(content, self.quote):
            raise ValueError("annotation quote must occur exactly once in its owning source")


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class EvidenceMessage(FrozenContract):
    message_id: NonBlank
    role: MessageRole
    content: SourceText
    event_time: datetime
    learning_eligible: bool = False
    temporal_annotations: list[TemporalAnnotation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_quotes(self) -> "EvidenceMessage":
        if not self.content.strip():
            raise ValueError("message content must not be blank")
        for annotation in self.temporal_annotations:
            annotation.validate_quote(self.content)
        return self

    @field_validator("event_time")
    @classmethod
    def require_aware_event_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_time must be timezone-aware")
        return value


class EvidenceAppendRequest(SourceInputMetadata):
    input_role: Literal["interaction"] = "interaction"
    source_id: NonBlank
    messages: list[EvidenceMessage] = Field(min_length=1)

    def validate_limits(self, config: OAMConfig) -> None:
        if len(self.messages) > config.ingest.max_messages:
            raise ValueError(f"interaction exceeds {config.ingest.max_messages} messages")
        size = sum(len(message.content.encode("utf-8")) for message in self.messages)
        if size > config.ingest.max_bytes:
            raise ValueError(f"interaction content exceeds {config.ingest.max_bytes} bytes")
