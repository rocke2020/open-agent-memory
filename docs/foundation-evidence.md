# Steps 1–4 Foundation Evidence

## Overview

The v0.0.1 Steps 1–4 source foundation is implemented in this product repository as of 2026-09-22. This evidence covers the contract handoff, focused package, shared configuration/database contracts, and the three first-wave implementation lanes; it does not claim the Step 5 source-to-search path or release readiness.

## 1. Included Scope

- The planning handoff points implementation and runtime truth to this repository, while this package has no runtime dependency on the planning or benchmark checkouts.
- The Apache-2.0 Python 0.0.1 distribution provides `oam preflight`, `oam migrate`, `oam api`, `oam worker`, and `oam recover-bootstrap`, plus canonical public documentation and installed config/migration assets.
- `configs/oam.yaml` owns the accepted limits, deadlines, retention, model roles, and concurrency values; environment variables provide only the alternate config path, DSNs, explicitly referenced endpoints/keys, and the operator recovery destination.
- The initial migration defines tenant/Scope authority, immutable PostgreSQL originals, L0–L3 support tables, operations, work units, attempts, usage, deadlines, traces, exports, forced RLS, credential/scope lookup, and recoverable fenced claims.
- Authentication, initial bootstrap/recovery, credential rotation, source/original acceptance, operation state/publication guards, durable claims, model binding/probes, deterministic embedding seams, bounded HTML/PDF/image parsing, medical fixtures, HTTP/OpenAPI routes, response identity, conformance fixtures, and the same-origin Inspector shell have tests.

## 2. Current Verification

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

With `OAM_TEST_POSTGRES_DSN` pointing to an isolated PostgreSQL 18 database and `OAM_TEST_POSTGRES_ISOLATED=1`, the full suite passed: `208 passed in 1.72s`. Ruff reported `All checks passed!` and `88 files already formatted`; source and wheel builds passed. The test server used `pgvector/pgvector:pg18` at digest `sha256:2ba9ca5f2e7daa0f0e7723cba1ee9167bab54efd3640516a44ac1a928dd67e7a`; this is a verification dependency, not an OAM release image.

The database checks exercised real API/queue/content role privileges, denied unscoped reads, original-byte round trips, concurrent idempotent acceptance, claims and uncertain-attempt exclusion, deleted-source rejection, concurrent bootstrap, commit-before-handoff recovery, credential rotation, and local revocation-cache eviction. A fresh `oam migrate` initialized one administrator and a mode-0600 secret destination; repeating it without a destination preserved that identity. A newly launched API passed 62 parsed-response assertions across all 27 declared routes, including operation and credential success/error paths and explicit unavailable responses for later-stage routes.

A clean temporary virtual environment installed the built wheel, resolved the installed canonical config and SQL migration, loaded both Inspector assets, and produced the expected fail-closed preflight result for the intentionally unbound canonical roles. All eight configured model roles also passed the HTTP adapter against a local synthetic provider; this proves the adapter boundary, not an external provider's model quality or capability.

## 3. Explicit Boundary

`[UNVERIFIED ASSUMPTION]` External model-provider capability and multi-replica runtime recovery have not been exercised. No live or paid model endpoint was called. The worker entrypoint remains fail-closed until Step 5 installs stage processors, and indexing, L0–L3 generation, retrieval, complete Resource lifecycle behavior, Profiles, browser integration, Docker, publication, multi-replica recovery, and the golden public path remain later steps.
