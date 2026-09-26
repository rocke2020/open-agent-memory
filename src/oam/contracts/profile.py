from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileSection(StrEnum):
    MAIN = "main"
    PREFERENCES = "preferences"
    EXPERIENCES = "experiences"


class ProfileRefreshRequest(FrozenContract):
    sections: list[ProfileSection] = Field(default_factory=lambda: [ProfileSection.MAIN])


class ProfileRefreshUnchanged(FrozenContract):
    status: Literal["unchanged"] = "unchanged"
    profile_revision: PositiveInt
    generation_calls: Literal[0] = 0


class ProfileRefreshAccepted(FrozenContract):
    status: Literal["accepted"] = "accepted"
    operation_id: str = Field(min_length=1)


class ProfileResourceRequest(FrozenContract):
    resource_id: str = Field(min_length=1, max_length=256)
    resource_type: Literal["profile"]
    subtype: Literal["basic-profile/v1", "preferences/v1"]
    source_revision: str | None = None
    as_of: str | None = None
    content: dict[str, Any]
