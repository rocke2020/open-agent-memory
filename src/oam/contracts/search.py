from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    PrivateAttr,
    StringConstraints,
    field_validator,
    model_validator,
)

from oam.config import OAMConfig, SearchConfig, SearchTier

NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_SHARED_SEARCH_OVERRIDES = frozenset(
    {
        "lexical_ranking",
        "partial_match_gate",
        "adjacent_user_context",
        "adjacent_user_context_max_chars",
        "causal_graph",
        "fact_source_bodies",
    }
)


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class RecallLevel(StrEnum):
    FAST = "fast"
    NORMAL = "normal"
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


class SearchIncludeOptions(FrozenContract):
    chunks: bool = Field(default=True, strict=True)


class SearchOutputOptions(FrozenContract):
    recall_level: RecallLevel = Field(
        default=RecallLevel.FAST,
        description=(
            "Recall preset. When omitted, uses search.default_recall_level from configuration "
            "(fast by default). An explicit value takes precedence."
        ),
    )
    overrides: dict[str, JsonValue] = Field(
        default_factory=dict,
        description=(
            "Request-only overrides for fields of the selected fast, normal, or deep search "
            "preset and the shared lexical_ranking, partial_match_gate, adjacent_user_context, "
            "adjacent_user_context_max_chars, causal_graph, and fact_source_bodies settings. "
            "Omitted fields inherit configuration; "
            "merged values use the same validators. "
            'For example, {"temporal_nodes": 300} changes traversal without changing max_results.'
        ),
    )
    fast_graph: bool | None = Field(
        default=None,
        strict=True,
        description=(
            "Override fast entity/semantic graph selection: true selects light, false selects off. "
            "Omitted or null inherits search.fast.entity_semantic_graph from configuration."
        ),
    )
    _context_max_tokens: int | None = PrivateAttr(default=None)
    include: SearchIncludeOptions = Field(default_factory=SearchIncludeOptions)
    allow_partial: bool = False
    deadline_at: datetime | None = None

    @model_validator(mode="after")
    def require_fast_for_graph_option(self) -> "SearchOutputOptions":
        self._validate_fast_graph()
        return self

    def _validate_fast_graph(self) -> None:
        if self.fast_graph and self.recall_level != RecallLevel.FAST:
            raise ValueError("fast_graph requires recall_level=fast")

    @field_validator("deadline_at")
    @classmethod
    def require_aware_deadline(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("deadline_at must be timezone-aware")
        return value

    def resolve_defaults(self, config: OAMConfig) -> Self:
        if "recall_level" in self.model_fields_set:
            return self
        return self.model_copy(
            update={"recall_level": RecallLevel(config.search.default_recall_level)}
        )

    def validate_limits(self, config: OAMConfig) -> None:
        resolved = self.resolve_defaults(config)
        resolved._validate_fast_graph()
        resolved.search_settings(config)
        resolved.search_tier(config)

    def search_settings(self, config: OAMConfig) -> SearchConfig:
        shared_overrides = {
            key: value for key, value in self.overrides.items() if key in _SHARED_SEARCH_OVERRIDES
        }
        if not shared_overrides:
            return config.search
        return SearchConfig.model_validate({**config.search.model_dump(), **shared_overrides})

    def search_tier(self, config: OAMConfig) -> SearchTier:
        mode = self.resolve_defaults(config).recall_level.value
        tier = getattr(config.search, mode)
        tier_overrides = {
            key: value
            for key, value in self.overrides.items()
            if key not in _SHARED_SEARCH_OVERRIDES
        }
        if tier_overrides:
            values = config.model_dump(mode="python")
            values["search"][mode].update(tier_overrides)
            tier = getattr(OAMConfig.model_validate(values).search, mode)
        if mode == "fast" and self.fast_graph is not None:
            tier = tier.model_copy(
                update={"entity_semantic_graph": "light" if self.fast_graph else "off"}
            )
        return tier

    def context_limit(self, config: OAMConfig) -> int:
        if self._context_max_tokens is not None:
            return self._context_max_tokens
        return self.search_tier(config).context_max_tokens


class SearchQueryOptions(SearchOutputOptions):
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
    def require_complete_ranking_window(self) -> "SearchQueryOptions":
        if (self.time_basis is None) != (self.time_window is None):
            raise ValueError("time_basis and time_window must be supplied together")
        return self


class SearchRequest(SearchQueryOptions):
    query: NonBlank


class SearchManyRequest(SearchQueryOptions):
    queries: list[NonBlank] = Field(min_length=1)

    def validate_limits(self, config: OAMConfig) -> None:
        super().validate_limits(config)
        if len(self.queries) > config.search.max_queries:
            raise ValueError(f"search batch exceeds {config.search.max_queries} queries")
