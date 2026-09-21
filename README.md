# Open Agent Memory

## Overview

Open Agent Memory (OAM) is an Apache-2.0 self-hosted memory engine for agent developers. It preserves eligible interaction evidence, derives source-backed memory, keeps HTML/PDF/image Resources separate, retrieves both origins together with citations, and builds read-only Profiles only when explicitly requested.

The repository is implementing v0.0.1. The current Step 1–4 foundation includes closed configuration, versioned PostgreSQL schema/RLS, authentication and durable-operation repositories, real model capability preflight, bounded Resource parsing, HTTP/OpenAPI contracts, database-backed operation routes, conformance fixtures, and the browser Inspector shell. It does not yet provide the Step 5 indexing and retrieval path, so it must not be treated as a usable memory server or release candidate.

## Product Model

- **L0 Source evidence:** retained eligible conversations with roles, times, source identities, and learning eligibility.
- **L1 Learned memory:** source-linked facts, preferences, experiences, events, and constraints extracted from eligible interactions.
- **L2 Insights:** persisted synthesis over related eligible L1 records with transitive L0 support.
- **L3 Profile:** explicitly requested, read-only `main`, `preferences`, and `experiences` projections; unchanged inputs reuse the prior Profile with zero generation calls.
- **Evidence Resources:** retained HTML/PDF/image originals, detailed passages, locators, and abstracts that remain searchable evidence and never automatically become L1–L3.

## Architecture

OAM uses Python, FastAPI, explicit SQL through psycopg, and PostgreSQL with pgvector. API processes are stateless; PostgreSQL owns accepted originals, authorization, source lifecycle, operations, worker claims, leases, fences, and application-replica recovery. Model roles use explicitly configured OpenAI-compatible endpoints with no provider, model, endpoint, or credential defaults.

```text
trusted application -> HTTP API -> PostgreSQL receipt + immutable original
                                      |
                                      v
                               durable workers
                         interactions -> L0 -> L1 -> L2
                         Resources -> passages + abstract
                                      |
                                      v
                         joint fast/deep recall + citations

explicit Profile refresh -> unchanged: zero calls
                         -> changed: durable operation -> L3
```

See [architecture](docs/architecture.md), [HTTP API](docs/api.md), [operations](docs/operations.md), [acceptance](docs/acceptance.md), and the current [Steps 1–4 foundation evidence](docs/foundation-evidence.md).

## Development Setup

Python 3.12–3.14 and `uv` are required.

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run oam preflight --config configs/oam.yaml --json
```

The canonical configuration intentionally has no model bindings, so preflight exits `2` and lists every missing required role. When bindings are present, preflight makes one role-appropriate request per enabled role and rejects empty, malformed, reasoning-only, truncated, non-finite, or dimensionally inconsistent results. Supply endpoints and secrets through an explicit operator configuration before expecting readiness. Default tests never read a private `.env` or contact external providers.

The current API shell can be inspected locally:

```bash
uv run oam api --config configs/oam.yaml --host 127.0.0.1 --port 8080
```

`GET /health/live`, `GET /health/ready`, `GET /v1/capabilities`, `/openapi.json`, and `/ui/` are available. Readiness remains `503 foundation_only`. Operation read/cancel/retry/reconcile and credential list/issue/revoke routes use the configured API database role when `OAM_API_DSN` is set. Initial `oam migrate` requires a new operator-only file through `OAM_BOOTSTRAP_OUTPUT` for the administrator secret; see [operations](docs/operations.md). Ingestion, Resource, memory, search, Profile, debug, export, and Scope administration remain fail-closed until their later implementation steps.

## Release Boundary

v0.0.1 requires all L0–L3 layers, separate Resource ingestion, fast and deep joint recall, source lifecycle, explain/diff/recorded replay, portable export, Memory Inspector, multi-API/multi-worker recovery, conformance evidence, and a public multi-architecture image. The Python SDK, targeted fact controls, supplier importers, full Profile Studio, hosted service, database HA, and multi-region operation are later work.

No Dockerfile or public image exists yet. Packaging and publication are the final release phase after the complete source-level acceptance matrix passes.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
