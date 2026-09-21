from datetime import datetime
from enum import StrEnum
from typing import Annotated

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
    DEEP = "deep"


class EventTimeRange(FrozenContract):
    from_time: datetime | None = Field(default=None, alias="from")
    to_time: datetime | None = Field(default=None, alias="to")

    @field_validator("from_time", "to_time")
    @classmethod
    def require_aware_event_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("event_time bounds must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "EventTimeRange":
        if self.from_time and self.to_time and self.from_time >= self.to_time:
            raise ValueError("event_time.from must be earlier than event_time.to")
        return self


class RecalledChunkOptions(FrozenContract):
    max_tokens: PositiveInt | None = None


class SearchIncludeOptions(FrozenContract):
    chunks: RecalledChunkOptions | None = None


class SearchOutputOptions(FrozenContract):
    recall_level: RecallLevel = RecallLevel.FAST
    max_context_tokens: PositiveInt | None = None
    include: SearchIncludeOptions = Field(default_factory=SearchIncludeOptions)
    profile_sections: list[str] = Field(default_factory=list)
    allow_partial: bool = False

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
    event_time: EventTimeRange | None = None


class SearchManyRequest(SearchOutputOptions):
    queries: list[NonBlank] = Field(min_length=1)

    def validate_limits(self, config: OAMConfig) -> None:
        super().validate_limits(config)
        if len(self.queries) > config.search.max_queries:
            raise ValueError(f"search batch exceeds {config.search.max_queries} queries")
