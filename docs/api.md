# Open Agent Memory HTTP API

## Overview

HTTP/OpenAPI is the only required v0.0.1 integration surface. Every content route is scoped, credential-derived, versioned, fail-closed, and shared by raw clients and the Memory Inspector; no browser-only policy or Python SDK is required.

## 1. Global Routes

- `GET /health/live` reports process liveness only.
- `GET /health/ready` returns `503` with `ready: false` and `reason: foundation_only` while the indexing and recall path is unavailable; it also lists unbound required model roles and never performs model calls.
- `GET /v1/capabilities` publishes contract version, enabled features, effective limits, artifact versions, and meter coverage.
- `GET /ui/` serves the same-origin Inspector shell with no embedded content or persisted credential.

## 2. Administration

Tenant-administrator routes create Scopes, replace same-tenant principal grants, issue additional opaque credentials, list credential metadata, and revoke credentials. Issuance returns each secret once; stored records contain only the SHA-256 digest and immutable tenant/principal binding. Administrators still need explicit content grants.

The implemented credential routes are `GET /v1/credentials`, `POST /v1/credentials`, and `DELETE /v1/credentials/{credential_id}`. Issuance accepts an existing same-tenant `principal_id` and optional timezone-aware `expires_at`, and returns the new secret only after commit. Listing accepts `after` and a positive `limit` up to the configured `auth.credential_page_size`, returning metadata and `next_after`. Revocation is idempotent, returns `revocation_effective_by`, and evicts the local cache after commit; unknown and foreign credential IDs have the same response. Every administration request verifies the current credential directly in PostgreSQL.

## 3. Scoped Content Routes

All routes below use `/v1/scopes/{scope_id}` and derive tenant/principal identity from `Authorization: Bearer <opaque-key>`.

| Route | Contract |
|---|---|
| `POST /evidence` | Append trusted eligible/ineligible interaction spans and return a durable operation receipt. |
| `GET/DELETE /evidence/{id}` | Inspect retained source or commit source exclusion plus asynchronous cleanup. |
| `POST/GET/PUT/DELETE /resources` | Create, inspect, revise, and delete immutable typed Profile/Evidence Resources. |
| `GET /memories` | List distinct L1 records and L2 Insights with support. |
| `POST /search` | Run one direct fast/deep query with zero generative calls. |
| `POST /search-many` | Run a validated query batch under one recall level, budget, and deadline. |
| `POST /decompose-and-search` | Optionally perform at most one planner call, then bounded retrieval or original-query fallback. |
| `GET /profile` | Read selected stored fields and freshness; never generate. |
| `POST /profile/refresh` | Return `200 unchanged` with zero calls or `202` with a durable operation. |
| `GET/POST /operations/{id}` | Inspect, cancel, retry, or explicitly reconcile one uncertain attempt. |
| `GET /debug/traces/{id}` | Explain authorized retained candidate, ranking, packing, and exclusion decisions. |
| `GET /profile/diff`, `POST /replays`, `POST /exports` | Compare snapshots, reconstruct recorded work, or create a validated portable export. |

## 4. Response Semantics

Every response carries a request ID and contract version. Accepted asynchronous work distinguishes `accepted`, `source_searchable`, `memory_ready`, `insights_ready`, and Profile freshness; success at one stage never implies later stages completed.

Errors carry a stable code, stage, retryability, operation ID when accepted, and safe details. Validation and authorization complete before external calls. Unknown and unauthorized object IDs use a non-enumerating response where the contract requires it.

## 5. Current Implementation Status

The Step 4 foundation publishes OpenAPI paths, request IDs, contract-version headers, fail-closed authentication admission, effective limits, database-backed operation and credential handlers, and the same-origin Inspector shell. Scope administration and content routes remain unavailable until their owning implementation steps; an OpenAPI path, operation receipt, or HTTP 200 health response is not proof that indexing or recall works.
