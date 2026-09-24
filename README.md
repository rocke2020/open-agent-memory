# Open Agent Memory

## Overview

Open Agent Memory (OAM) is an Apache-2.0 self-hosted memory engine for agent developers. It preserves eligible interaction evidence, derives source-backed memory, keeps HTML/PDF/image Resources separate, retrieves both origins together with citations, and builds read-only Profiles only when explicitly requested.

The repository is implementing v0.0.1. The user-qualified retain/recall API, server-managed text/document updates, source-backed facts, typed Insights and non-generative fast/mid/deep retrieval have PostgreSQL-backed functional coverage. Separate API/worker processes with configured real models have produced and recalled supported L2 plus HTML Evidence through the current API, including query-date resolution and explicit partial-index diagnostics. No complete LongMemEval stage is available. Profiles, full Resource integration and release-level recovery remain later gates, so this is not a release candidate.

## Product Model

- **L0 Source evidence:** retained eligible conversations with roles, times, source identities, and learning eligibility.
- **L1 Learned memory:** source-linked facts, preferences, experiences, events, and constraints extracted from eligible interactions.
- **L2 Insights:** persisted synthesis over related eligible L1 records with transitive L0 support and one descriptive subtype: `preference`, `pattern`, `project`, `relationship` or `general`; subtype does not alter ranking.
- **L3 Profile:** explicitly requested, read-only `main`, `preferences`, and `experiences` projections; unchanged inputs reuse the prior Profile with zero generation calls.
- **Evidence Resources:** retained text/HTML/PDF/image originals, detailed passages, locators, and abstracts that remain searchable evidence and never automatically become L1–L3.

One credential-derived tenant and one key-bound `user_id` identify a personal memory collection. An administrator creates it with `POST /v1/users` and receives that user's first key once; the host uses the user key for `POST /v1/retain` and `POST /v1/recall`. The administrator key cannot read or write personal content directly. Interactions need no caller-managed source/message IDs. Documents use a stable `document_id` with explicit `replace` or text-only `append`; OAM allocates revisions. The [HTTP API](docs/api.md) documents JSON and multipart examples, user-key rotation, retry keys and operation readiness.

## Architecture

OAM uses Python, FastAPI, explicit SQL through psycopg, and PostgreSQL with pgvector. API processes are stateless; PostgreSQL owns accepted originals, authorization, source lifecycle, operations, worker claims, leases, fences, and application-replica recovery. Model roles use explicitly configured OpenAI-compatible endpoints with no provider, model, endpoint, or credential defaults.

```text
trusted application -> user-authorized retain -> PostgreSQL receipt + immutable original
                                      |
                                      v
                               durable workers
                         interactions -> L0 -> L1 -> L2
                         Resources -> passages + abstract
                                      |
                                      v
                         joint fast/mid/deep recall + citations

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

Run the API and a separate durable worker with the same operator configuration:

```bash
uv run oam api --config configs/oam.yaml --host 127.0.0.1 --port 8080
uv run oam worker --config configs/oam.yaml
```

`GET /health/live`, `GET /health/ready`, `GET /v1/capabilities`, `/openapi.json`, and `/ui/` are available. Full-release readiness remains `503 core_only`. Configure distinct API, queue and worker-content DSNs plus extraction, Insight, embedding and abstract model roles before using the core. Initial `oam migrate` requires a new operator-only file through `OAM_BOOTSTRAP_OUTPUT` for the administrator secret; see [operations](docs/operations.md). The revised user-qualified public flow still needs its named end-to-end verification; do not treat examples or OpenAPI presence as that proof. Profiles, batch/planner, source lifecycle and full Inspector integration remain unavailable. See [architecture](docs/architecture.md) for the current pipeline and [acceptance](docs/acceptance.md) for its remaining gates.

## Release Boundary

v0.0.1 requires all L0–L3 layers, separate Resource ingestion, fast/mid/deep joint recall, source lifecycle, explain/diff/recorded replay, portable export, Memory Inspector, multi-API/multi-worker recovery, conformance evidence, and a public multi-architecture image. Fast and mid are the primary everyday recall modes; deep is an explicit higher-budget option. The Python SDK, targeted fact controls, supplier importers, full Profile Studio, hosted service, database HA, and multi-region operation are later work.

No Dockerfile or public image exists yet. Packaging and publication are the final release phase after the complete source-level acceptance matrix passes.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
