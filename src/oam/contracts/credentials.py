"""Administrator credential rotation requests and secret-free metadata."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from oam.contracts.operations import FrozenContract


class CredentialIssueRequest(FrozenContract):
    principal_id: str | None = Field(default=None, min_length=1)
    user_id: str | None = Field(default=None, min_length=1)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_target(self) -> CredentialIssueRequest:
        if (self.principal_id is None) == (self.user_id is None):
            raise ValueError("provide exactly one of principal_id or user_id")
        return self


class CredentialMetadata(FrozenContract):
    credential_id: str
    principal_id: str
    state: Literal["active", "revoked"]
    expires_at: AwareDatetime | None
    revoked_at: AwareDatetime | None
    created_at: AwareDatetime


class CredentialListResponse(FrozenContract):
    items: tuple[CredentialMetadata, ...]
    next_after: str | None


class CredentialIssuedResponse(CredentialMetadata):
    secret: str = Field(repr=False)


class CredentialRevokedResponse(FrozenContract):
    credential_id: str
    revocation_effective_by: AwareDatetime
