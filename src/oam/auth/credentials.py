"""Opaque credential helpers and the bounded positive verification cache."""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime

SECRET_BYTES = 32
SECRET_PREFIX = "oam_"


@dataclass(frozen=True, slots=True)
class IssuedCredential:
    secret: str
    digest: bytes


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    credential_id: str
    tenant_id: str
    principal_id: str
    role: str
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class AuthorizedScope:
    credential_id: str
    tenant_id: str
    principal_id: str
    scope_id: str
    capabilities: frozenset[str]

    @classmethod
    def from_database_grant(
        cls,
        principal: AuthenticatedPrincipal,
        *,
        scope_id: str,
        capabilities: Collection[str],
    ) -> AuthorizedScope:
        """Create authority only from a credential-derived principal and checked grant."""
        if not scope_id.strip():
            raise ValueError("scope_id cannot be empty")
        frozen_capabilities = frozenset(capabilities)
        if not frozen_capabilities or any(
            not capability.strip() for capability in frozen_capabilities
        ):
            raise ValueError("capabilities must contain non-empty values")
        return cls(
            credential_id=principal.credential_id,
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            scope_id=scope_id,
            capabilities=frozen_capabilities,
        )

    def allows(self, capability: str) -> bool:
        return capability in self.capabilities


def digest_secret(secret: str) -> bytes:
    """Return the database lookup digest without retaining the supplied secret."""
    return hashlib.sha256(secret.encode("utf-8")).digest()


def issue_credential(
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> IssuedCredential:
    """Issue one opaque secret; callers persist only the returned digest."""
    entropy = random_bytes(SECRET_BYTES)
    if len(entropy) != SECRET_BYTES:
        raise RuntimeError(f"credential entropy must contain exactly {SECRET_BYTES} bytes")
    encoded = base64.urlsafe_b64encode(entropy).rstrip(b"=").decode("ascii")
    secret = f"{SECRET_PREFIX}{encoded}"
    return IssuedCredential(secret=secret, digest=digest_secret(secret))


@dataclass(frozen=True, slots=True)
class CredentialLookup:
    digest: bytes
    invalidation_generation: int
    cache_deadline: float


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    principal: AuthenticatedPrincipal
    cache_deadline: float


class PositiveCredentialCache:
    """Thread-safe positive cache with fixed lifetimes and revocation fencing."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._monotonic = monotonic
        self._utcnow = utcnow
        self._entries: OrderedDict[bytes, _CacheEntry] = OrderedDict()
        self._invalidation_generation: dict[bytes, int] = {}
        self._lock = threading.RLock()

    def begin_lookup(self, digest: bytes) -> CredentialLookup:
        self._validate_digest(digest)
        with self._lock:
            return CredentialLookup(
                digest=digest,
                invalidation_generation=self._invalidation_generation.get(digest, 0),
                cache_deadline=self._monotonic() + self._ttl_seconds,
            )

    def store(
        self,
        lookup: CredentialLookup,
        principal: AuthenticatedPrincipal,
    ) -> bool:
        """Cache an authoritative positive result if its original lifetime remains valid."""
        now_monotonic = self._monotonic()
        now_wall = self._utcnow()
        with self._lock:
            if self._invalidation_generation.get(lookup.digest, 0) != (
                lookup.invalidation_generation
            ):
                return False
            if now_monotonic >= lookup.cache_deadline:
                return False
            if principal.expires_at is not None and principal.expires_at <= now_wall:
                return False
            self._entries.pop(lookup.digest, None)
            while len(self._entries) >= self._max_entries:
                self._entries.popitem(last=False)
            self._entries[lookup.digest] = _CacheEntry(
                principal=principal,
                cache_deadline=lookup.cache_deadline,
            )
            return True

    def get(self, digest: bytes) -> AuthenticatedPrincipal | None:
        self._validate_digest(digest)
        now_monotonic = self._monotonic()
        now_wall = self._utcnow()
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                return None
            if entry.cache_deadline <= now_monotonic or (
                entry.principal.expires_at is not None and entry.principal.expires_at <= now_wall
            ):
                self._entries.pop(digest, None)
                return None
            return entry.principal

    def invalidate(self, digest: bytes) -> None:
        """Fence concurrent older lookups and evict this process's positive snapshot."""
        self._validate_digest(digest)
        with self._lock:
            self._invalidation_generation[digest] = self._invalidation_generation.get(digest, 0) + 1
            self._entries.pop(digest, None)

    @staticmethod
    def _validate_digest(digest: bytes) -> None:
        if len(digest) != hashlib.sha256().digest_size:
            raise ValueError("credential digest must be a SHA-256 digest")
