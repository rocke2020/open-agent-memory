# Open Agent Memory Architecture

## Overview

OAM is one governed data plane: immutable source evidence is authoritative, memories and Profiles are rebuildable projections, and recall assembles bounded context without generating an answer. PostgreSQL owns content, policy, work, and application-replica recovery so API and worker processes can be replaced without losing accepted inputs.

## 1. Authority and Isolation

A tenant is the organization security boundary. A Scope is an authorized memory collection inside a tenant. Every content identity, support link, operation, attempt, trace, and idempotency key is tenant-and-Scope qualified; credentials derive the tenant, and callers can address only granted Scope IDs.

PostgreSQL row-level security is a mandatory second boundary behind explicit SQL predicates. Migration credentials are separate from non-owner, non-superuser, non-`BYPASSRLS` API and worker roles. A worker claims metadata through a narrow queue role, then binds the authorized tenant/Scope in a separate content transaction.

The operator runs migration and initial administrator bootstrap before starting application replicas. Bootstrap serializes one default tenant, administrator, and credential; it commits their identity before handing the secret to a new operator-only file. Interrupted handoff requires explicit recovery for that same administrator. Runtime credential administration verifies the actor from the supplied secret digest, issues a separate sibling key for rotation, and revokes only same-tenant credentials. API processes never receive migration credentials. [Bootstrap](../src/oam/auth/bootstrap.py), [authentication](../src/oam/auth/repository.py), and [HTTP composition](../src/oam/api/runtime.py) own these boundaries.

The [configuration loader](../src/oam/config.py) validates canonical YAML, overlays an explicitly selected partial operator configuration, and freezes the resolved values. [HTTP admission](../src/oam/api/app.py) uses those same values for request limits and capabilities; request schemas enforce structure without supplying a second set of numeric limits. Foundation readiness remains false until the indexing and recall path is implemented and verified.

## 2. Evidence, Projections, and Resources

Eligible interactions enter L0 and may produce L1. Related L1 records may produce supported L2 Insights. An explicit Profile refresh freezes eligible L1/L2 and Profile Resource versions and may publish L3. Evidence Resources instead produce detailed passages and a derived abstract; neither output is an input to L1, L2, or L3.

```text
eligible interactions -> L0 -> L1 -> supported L2 --+
eligible L0 detail ----------------------------------+-> requested L3
explicit Profile Resources --------------------------+

HTML/PDF/image -> immutable original -> passages + abstract -> joint recall
Evidence Resource ----------------------------------------------X-> L1/L2/L3
```

Every generated claim retains support to immutable source versions and locators. Source revision or deletion changes eligibility before a response or worker publication can use the old derivative.

## 3. Durable Work

An accepted write commits its source/original bytes, idempotency identity, root operation, sealed stage manifest, and first runnable work units before returning `202`. Workers claim ready rows with short PostgreSQL transactions, leases, and monotonically increasing fences; remote model I/O happens outside the transaction.

The [source repository](../src/oam/db/sources.py) takes the Scope lock before checking idempotency, so overlapping identical writes reuse the committed receipt. Source activation rejects deleted sources and older versions; originals require a read grant. The [claim function](../migrations/0001_initial.sql) locks work rows without escalating queue privileges and withholds any unit with an unresolved external outcome until explicit [attempt reconciliation](../src/oam/operations/repository.py). These foundation repositories are exercised independently of the later ingestion HTTP path.

Publication rechecks operation cancellation, lease/fence ownership, source version, learning eligibility, generation, and current policy. A late or stale worker may preserve diagnostic attempt evidence but cannot publish canonical output. Retry reuses compatible completed work and creates a new attempt only for unfinished eligible stages.

## 4. Retrieval and Profiles

Ordinary search is non-generative. Fast recall uses dense, lexical, exact-anchor, and explicit temporal lanes followed by rank fusion. Deep recall adds bounded PostgreSQL link expansion and an optional configured reranker. Memory and Evidence candidates compete under one result and token budget; neither origin receives a reserved quota.

`GET /profile` reads stored eligible fields and freshness only. `POST /profile/refresh` performs the cheap input-identity check: unchanged inputs return `200` and zero generation calls; changed inputs return a durable `202` operation. L2 changes dirty L3 without generating it, and Evidence Resource changes do not dirty L3.

## 5. Runtime Topology

One application image eventually runs either API or worker mode. The API also serves version-matched Inspector assets at `/ui/`. Development uses one API, one worker, and PostgreSQL; the reference application topology uses at least two API and two worker replicas over shared PostgreSQL. This proves application-pod recovery, not PostgreSQL HA or multi-region operation.
