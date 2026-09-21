"""Administrator credential rotation requests and secret-free metadata."""

from typing import Literal

from pydantic import AwareDatetime, Field

from oam.contracts.operations import FrozenContract


class CredentialIssueRequest(FrozenContract):
    principal_id: str = Field(min_length=1)
    expires_at: AwareDatetime | None = None


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
