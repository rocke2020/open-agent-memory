from datetime import datetime
from enum import StrEnum
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from oam.config import OAMConfig

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class RecallLevel(StrEnum):
    FAST = "fast"
    MID = "mid"
    DEEP = "deep"


class TimeBasis(StrEnum):
    EVENT = "event"
    REFERENCED = "referenced"
    MENTIONED = "mentioned"
    CREATED = "created"
    UPDATED = "updated"


class TimeWindow(FrozenContract):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def validate_window(self) -> "TimeWindow":
        for value in (self.start, self.end):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("time_window bounds must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("time_window.start must precede end")
        return self


class RecalledChunkOptions(FrozenContract):
    max_tokens: PositiveInt | None = None


class SearchIncludeOptions(FrozenContract):
    chunks: RecalledChunkOptions | None = None


class SearchOutputOptions(FrozenContract):
    recall_level: RecallLevel = RecallLevel.MID
    max_context_tokens: PositiveInt | None = None
    include: SearchIncludeOptions = Field(default_factory=SearchIncludeOptions)
    profile_sections: list[str] = Field(default_factory=list)
    allow_partial: bool = False
    deadline_at: datetime | None = None

    @field_validator("deadline_at")
    @classmethod
    def require_aware_deadline(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("deadline_at must be timezone-aware")
        return value

    def validate_limits(self, config: OAMConfig) -> None:
        if (
            self.max_context_tokens is not None
            and self.max_context_tokens > config.context.max_tokens
        ):
            raise ValueError(f"max_context_tokens exceeds {config.context.max_tokens}")
        chunks = self.include.chunks
        if (
            chunks is not None
            and chunks.max_tokens is not None
            and chunks.max_tokens > config.chunks.max_tokens
        ):
            raise ValueError(f"include.chunks.max_tokens exceeds {config.chunks.max_tokens}")


class SearchRequest(SearchOutputOptions):
    query: NonBlank
    time_basis: TimeBasis | None = None
    time_window: TimeWindow | None = None
    reference_time: datetime | None = None
    reference_timezone: str | None = None

    @field_validator("reference_time")
    @classmethod
    def require_aware_reference_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("reference_time must be timezone-aware")
        return value

    @field_validator("reference_timezone")
    @classmethod
    def require_iana_reference_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError("reference_timezone must be an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def require_complete_ranking_window(self) -> "SearchRequest":
        if (self.time_basis is None) != (self.time_window is None):
            raise ValueError("time_basis and time_window must be supplied together")
        return self


class SearchManyRequest(SearchOutputOptions):
    queries: list[NonBlank] = Field(min_length=1)

    def validate_limits(self, config: OAMConfig) -> None:
        super().validate_limits(config)
        if len(self.queries) > config.search.max_queries:
            raise ValueError(f"search batch exceeds {config.search.max_queries} queries")
