from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from oam.auth import bootstrap, credentials


@dataclass
class MutableClock:
    elapsed: float = 0.0
    wall: datetime = datetime(2026, 9, 21, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.elapsed

    def utcnow(self) -> datetime:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds
        self.wall += timedelta(seconds=seconds)


def _principal(clock: MutableClock, *, expires_in: float | None = None):
    expires_at = None if expires_in is None else clock.wall + timedelta(seconds=expires_in)
    return credentials.AuthenticatedPrincipal(
        credential_id="key-1",
        tenant_id="tenant-1",
        principal_id="admin-1",
        role="administrator",
        expires_at=expires_at,
    )


def test_opaque_credentials_use_32_random_bytes_and_sha256_digest() -> None:
    issued = credentials.issue_credential(random_bytes=lambda size: b"x" * size)

    assert issued.secret == "oam_eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg"
    assert issued.digest == credentials.digest_secret(issued.secret)
    assert len(issued.digest) == 32
    assert credentials.digest_secret("secret").hex() == (
        "2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b"
    )


def test_positive_cache_uses_a_fixed_non_sliding_ttl() -> None:
    clock = MutableClock()
    cache = credentials.PositiveCredentialCache(
        ttl_seconds=30,
        max_entries=2,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
    )
    digest = credentials.digest_secret("one")
    lookup = cache.begin_lookup(digest)
    assert cache.store(lookup, _principal(clock)) is True

    clock.advance(20)
    assert cache.get(digest) == _principal(clock)
    clock.advance(11)

    assert cache.get(digest) is None


def test_revocation_during_lookup_prevents_stale_cache_repopulation() -> None:
    clock = MutableClock()
    cache = credentials.PositiveCredentialCache(
        ttl_seconds=30,
        max_entries=2,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
    )
    digest = credentials.digest_secret("one")
    lookup = cache.begin_lookup(digest)

    cache.invalidate(digest)

    assert cache.store(lookup, _principal(clock)) is False
    assert cache.get(digest) is None


def test_slow_lookup_cannot_populate_after_its_original_lifetime() -> None:
    clock = MutableClock()
    cache = credentials.PositiveCredentialCache(
        ttl_seconds=30,
        max_entries=2,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
    )
    digest = credentials.digest_secret("one")
    lookup = cache.begin_lookup(digest)

    clock.advance(30)

    assert cache.store(lookup, _principal(clock)) is False
    assert cache.get(digest) is None


def test_cache_expiry_is_capped_by_credential_expiry() -> None:
    clock = MutableClock()
    cache = credentials.PositiveCredentialCache(
        ttl_seconds=30,
        max_entries=2,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
    )
    expiring = credentials.digest_secret("expiring")
    assert cache.store(cache.begin_lookup(expiring), _principal(clock, expires_in=5)) is True
    clock.advance(6)
    assert cache.get(expiring) is None


def test_positive_cache_evicts_the_oldest_entry_at_capacity() -> None:
    clock = MutableClock()
    cache = credentials.PositiveCredentialCache(
        ttl_seconds=30,
        max_entries=2,
        monotonic=clock.monotonic,
        utcnow=clock.utcnow,
    )
    digests = [credentials.digest_secret(value) for value in ("one", "two", "three")]
    for digest in digests:
        assert cache.store(cache.begin_lookup(digest), _principal(clock)) is True

    assert cache.get(digests[0]) is None
    assert cache.get(digests[1]) == _principal(clock)
    assert cache.get(digests[2]) == _principal(clock)


def test_bootstrap_has_one_winner_and_observers_reuse_committed_identity() -> None:
    initial = bootstrap.BootstrapState.initial()

    first = bootstrap.claim_bootstrap(initial, contender_id="pod-1")
    second = bootstrap.claim_bootstrap(first.state, contender_id="pod-2")

    assert first.winner is True
    assert first.state.phase is bootstrap.BootstrapPhase.COMMITTED
    assert first.state.winner_id == "pod-1"
    assert second.winner is False
    assert second.state == first.state


def test_failed_secret_handoff_requires_operator_recovery_not_a_new_winner() -> None:
    claimed = bootstrap.claim_bootstrap(
        bootstrap.BootstrapState.initial(), contender_id="pod-1"
    ).state

    failed = bootstrap.record_handoff_failure(claimed, contender_id="pod-1")
    observer = bootstrap.claim_bootstrap(failed, contender_id="pod-2")

    assert failed.phase is bootstrap.BootstrapPhase.RECOVERY_REQUIRED
    assert observer.winner is False
    assert observer.state == failed
    with pytest.raises(bootstrap.BootstrapTransitionError, match="winner"):
        bootstrap.record_handoff(failed, contender_id="pod-2")


def test_authorized_scope_retains_credential_derived_tenant_identity() -> None:
    principal = credentials.AuthenticatedPrincipal(
        credential_id="key-1",
        tenant_id="tenant-1",
        principal_id="writer-1",
        role="writer",
        expires_at=None,
    )

    scope = credentials.AuthorizedUser.from_database_grant(
        principal,
        user_id="scope-1",
        capabilities={"memory:read", "memory:append"},
    )

    assert scope.tenant_id == "tenant-1"
    assert scope.principal_id == "writer-1"
    assert scope.user_id == "scope-1"
    assert scope.allows("memory:append") is True
    assert scope.allows("source:delete") is False
