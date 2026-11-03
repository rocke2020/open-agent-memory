from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, field_validator, model_validator


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileRefreshRequest(FrozenContract):
    mode: Literal["full", "incremental"] = "incremental"
    instruction: str | None = Field(default=None, min_length=1, max_length=4000)
    reset_instruction: bool = False

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str | None) -> str | None:
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("instruction cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_instruction(self) -> "ProfileRefreshRequest":
        if self.instruction is not None and self.reset_instruction:
            raise ValueError("instruction and reset_instruction cannot be combined")
        return self


class ProfileRefreshUnchanged(FrozenContract):
    status: Literal["unchanged"] = "unchanged"
    profile_revision: PositiveInt
    generation_calls: Literal[0] = 0


class ProfileRefreshAccepted(FrozenContract):
    status: Literal["accepted"] = "accepted"
    operation_id: str = Field(min_length=1)


class ProfileDataRequest(FrozenContract):
    profile_data_id: str = Field(min_length=1, max_length=256)
    subtype: Literal["basic-profile/v1", "preferences/v1"]
    source_revision: str | None = None
    as_of: str | None = None
    content: dict[str, Any]
