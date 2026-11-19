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


class ProfileGenerateRequest(FrozenContract):
    instruction: str = Field(min_length=1, max_length=4000)

    @field_validator("instruction")
    @classmethod
    def normalize_instruction(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("instruction cannot be blank")
        return value


class ProfileRefreshPolicy(FrozenContract):
    mode: Literal["full", "incremental"] = "incremental"
    refresh_cron: str | None = None
    full_refresh_cron: str | None = None
    refresh_after_consolidation: bool = False
    min_refresh_interval_seconds: int | None = Field(default=None, ge=0)

    @field_validator("refresh_cron", "full_refresh_cron")
    @classmethod
    def valid_cron(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        from croniter import croniter

        value = value.strip()
        if len(value.split()) != 5 or not croniter.is_valid(value):
            raise ValueError("Profile schedules require a valid five-field UTC cron expression")
        return value

    @model_validator(mode="after")
    def exclusive_triggers(self) -> "ProfileRefreshPolicy":
        if self.refresh_cron and self.refresh_after_consolidation:
            raise ValueError("refresh_cron and refresh_after_consolidation are mutually exclusive")
        return self


class ProfilePolicyPatch(FrozenContract):
    mode: Literal["full", "incremental"] | None = None
    refresh_cron: str | None = None
    full_refresh_cron: str | None = None
    refresh_after_consolidation: bool | None = None
    min_refresh_interval_seconds: int | None = Field(default=None, ge=0)

    @field_validator("refresh_cron", "full_refresh_cron")
    @classmethod
    def valid_cron(cls, value: str | None) -> str | None:
        return ProfileRefreshPolicy.valid_cron(value)

    @model_validator(mode="after")
    def validate_patch(self) -> "ProfilePolicyPatch":
        for key in self.model_fields_set - {
            "refresh_cron",
            "full_refresh_cron",
            "min_refresh_interval_seconds",
        }:
            if getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null")
        # Validate supplied values together, leaving omitted settings out of the patch.
        ProfileRefreshPolicy.model_validate(self.model_dump(exclude_unset=True))
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
