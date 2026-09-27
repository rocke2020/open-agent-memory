# Open Agent Memory

## Overview

Open Agent Memory (OAM) is an Apache-2.0 self-hosted memory engine for agent developers. It preserves eligible interaction evidence, derives source-backed memory, keeps HTML/PDF/image Resources separate, retrieves both origins together with citations, and builds read-only Profiles only when explicitly requested.

The repository is implementing v0.0.1. The user-qualified retain/recall API, source-backed facts, typed Insights, stored Profiles, joint fast/mid/deep retrieval, Resource revision/deletion, recorded inspection, portable export and browser Inspector have isolated PostgreSQL or browser functional coverage. Separate API/worker processes with configured real models have produced and recalled supported L2 plus HTML Evidence through the current API. No complete LongMemEval stage is available. Live Profile model quality, document-vision quality and release-level recovery remain later gates, so this is not a release candidate.

## Product Model

- **L0 Source evidence:** retained eligible conversations with roles, times, source identities, and learning eligibility.
- **L1 Learned memory:** source-linked facts, preferences, experiences, events, and constraints extracted from eligible interactions.
- **L2 Insights:** persisted synthesis over related eligible L1 records with transitive L0 support and one descriptive subtype: `preference`, `pattern`, `project`, `relationship` or `general`; subtype does not alter ranking.
- **L3 Profile:** explicitly requested, read-only `main`, `preferences`, and `experiences` projections; unchanged inputs reuse the prior Profile with zero generation calls.
- **Evidence Resources:** retained text/HTML/PDF/image originals, detailed passages, locators, and abstracts that remain searchable evidence and never automatically become L1–L3.

In `api_key` mode, one credential-derived tenant and one key-bound `user_id` identify a personal memory collection. An administrator creates it with `POST /v1/users` and receives that user's first key once; the host uses the user key for `POST /v1/retain` interactions, `POST /v1/users/{user_id}/resources` Evidence and Profile originals, and `POST /v1/recall`. The administrator key cannot read or write personal content directly. Local `dev` mode admits requests without a key and makes no privacy claim; `trusted` mode uses gateway-asserted tenant/user headers, with a root key required on nonlocal requests. Interactions need no caller-managed source/message IDs. Documents use a stable `document_id` with explicit `replace` or text-only `append`; OAM allocates revisions. The [HTTP API](docs/api.md) documents mode selection, JSON and multipart examples, user-key rotation, retry keys and operation readiness.

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

See [architecture](docs/architecture.md), [HTTP API](docs/api.md), [operations](docs/operations.md), the [Memory web UI guide](docs/memory-web-ui.md), [acceptance](docs/acceptance.md), and the current [Steps 1–4 foundation evidence](docs/foundation-evidence.md).

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

Run the API with its embedded durable worker using the operator configuration:

```bash
uv run oam api --config configs/oam.yaml --host 127.0.0.1
```

The default `worker.embedded_enabled: true` requires the API, queue and worker-content DSNs. For separate worker processes, set `worker.embedded_enabled: false` in the operator YAML and run `oam api` and `oam worker` with that file. Each runner has four task slots; deployment tooling controls process count. Shared PostgreSQL admission bounds generation, embeddings and HTTP traffic independently of replica count; see [operations](docs/operations.md#4-limits-and-failure-ownership).

`GET /health/live`, `GET /health/ready`, `GET /v1/capabilities`, `/openapi.json`, and `/ui/` are available. Full-release readiness remains `503 core_only`. Configure distinct API, queue and worker-content DSNs plus extraction, Insight, embedding and abstract model roles before using the core; delegated search also needs an optional planner binding. On a fresh database, `oam migrate` selects rootless local `dev` when no root secret or handoff destination is supplied; set `OAM_ROOT_API_KEY` or `OAM_BOOTSTRAP_OUTPUT` to initialize a keyed deployment. See [operations](docs/operations.md). Batch search and optional one-call delegated search use the existing recall path; Resource/source controls, recorded debug, export validation and the browser Inspector are documented in the [HTTP API](docs/api.md). Stored Profiles and typed Evidence/Profile Resource admission are available through the Profile and Resource routes; Profile generation requires its configured model role.

## Release Boundary

v0.0.1 requires all L0–L3 layers, separate Resource ingestion, fast/mid/deep joint recall, source lifecycle, explain/diff/recorded replay, portable export, Memory Inspector, multi-API/multi-worker recovery, conformance evidence, and a public multi-architecture image. Fast and mid are the primary everyday recall modes; deep is an explicit higher-budget option. The Python SDK, targeted fact controls, supplier importers, full Profile Studio, hosted service, database HA, and multi-region operation are later work.

No Dockerfile or public image exists yet. Packaging and publication are the final release phase after the complete source-level acceptance matrix passes.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
