# Open Agent Memory

## Overview

Open Agent Memory (OAM) is an Apache-2.0 self-hosted memory engine for agent developers. It preserves eligible interaction evidence, derives source-backed memory, keeps HTML/PDF/image Resources separate, retrieves both origins together with citations, and builds the read-only User Profile only when explicitly requested.

The repository is implementing v0.0.1. The user-qualified retain/recall API, source-backed facts, typed Insights, stored User Profile, joint fast/normal retrieval, Resource revision/deletion, recorded inspection, portable export and browser Inspector have isolated PostgreSQL or browser functional coverage. Separate API/worker processes with configured real models have produced and recalled supported L2 plus HTML Evidence through the current API. No complete LongMemEval stage is available. Live User Profile model quality, document-vision quality and release-level recovery remain later gates, so this is not a release candidate.

## Product Model

- **L0 Source evidence:** retained eligible conversations with roles, times, source identities, and learning eligibility.
- **L1 Learned memory:** source-linked facts, preferences, experiences, events, and constraints extracted from eligible interactions.
- **L2 Insights:** persisted synthesis over related eligible L1 records with transitive L0 support and one descriptive subtype: `preference`, `pattern`, `project`, `relationship` or `general`; subtype does not alter ranking.
- **L3 User Profile:** one explicitly refreshed, read-only document built from Profile Data and eligible Learned Memory; unchanged inputs reuse it with zero generation calls.
- **Profile Data:** explicitly supplied structured basic information or preferences, mapped to exact User Profile inputs and excluded from ordinary recall.
- **Resources:** retained text/HTML/PDF/image originals, detailed passages, locators, and abstracts that remain searchable evidence and never automatically become L1–L3.

In `api_key` mode, one credential-derived tenant and one key-bound `user_id` identify a personal memory collection. An administrator creates it with `POST /v1/users` and receives that user's first key once; the host uses the user key for `POST /v1/retain` interactions, `POST /v1/users/{user_id}/resources` document originals, `POST /v1/users/{user_id}/profile-data` structured user inputs, and `POST /v1/recall`. The administrator key cannot read or write personal content directly. Local `dev` mode admits requests without a key and makes no privacy claim; `trusted` mode uses gateway-asserted tenant/user headers, with a root key required on nonlocal requests. Interactions need no caller-managed source/message IDs. Documents use a stable `document_id` with explicit `replace` or text-only `append`; OAM allocates revisions. The [HTTP API](docs/api.md) documents mode selection, JSON and multipart examples, user-key rotation, retry keys and operation readiness.

Recall uses one limit on the complete rendered context: `search.fast.context_max_tokens: 18000` and `search.normal.context_max_tokens: 32768`. Chunks are included by default; `include.chunks: false` omits full original bodies while retaining eligible learned text, required supporting facts and citations. Relevance determines admission across learned evidence and originals, including originals with no extracted facts. Complete evidence groups must fit without clipping; shared source chunks are counted once. If the only eligible L1 fact and its complete support exceed the total, return that bundle alone only when no higher-ranked root has been admitted, stop filling and report the overshoot. Insights have no such exception. `context_tokens` includes citations and metadata under the runtime's reported estimator. Hosts own exact final model-input token accounting.

## Architecture

OAM uses Python, FastAPI, explicit SQL through psycopg, and PostgreSQL with pgvector. API processes are stateless; PostgreSQL owns accepted originals, authorization, source lifecycle, operations, worker claims, leases, fences, and application-replica recovery. Each service uses one configured VLM through OpenAI Chat, with per-role reasoning effort and separate embedding and optional reranker models. There are no provider, model, endpoint, or credential defaults.

```text
trusted application -> user-authorized retain -> PostgreSQL receipt + immutable original
                                      |
                                      v
                               durable workers
                         interactions -> L0 -> L1 -> L2
                         Resources -> passages + abstract
                                      |
                                      v
                         joint fast/normal recall + citations

explicit User Profile refresh -> unchanged: zero calls
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

The canonical configuration defines one `models.vlm` with `protocol: openai_chat` and `capabilities: [text, vision]`, plus a separate `models.embedding`. Set the actual generative model, base URL and credential in your private `.env` as `VLM_MODEL`, `VLM_BASE_URL` and `VLM_API_KEY`; `.env.example` contains placeholders only. The CLI loads the working directory's `.env` without overriding inherited environment variables. YAML owns the protocol: `openai_chat` calls `/chat/completions`; the example's `VLM_URL_TYPE` is helper metadata and is not read by OAM. The Responses protocol is not supported. `models.reasoning_effort` sets each generative role to `none`, `low`, `high` or `max`, with none disabling thinking and null disabling that role. Preflight makes one request for each required capability of every enabled role: extraction checks both text and vision through the same VLM. It rejects empty, malformed, reasoning-only, truncated, non-finite, or dimensionally inconsistent results. Runtime embedding validation additionally requires the configured `index.embedding_dimensions` size. Reranker and planner remain disabled by default. Default tests never read a private `.env` or contact external providers.

Run the API with its embedded durable worker using the operator configuration:

```bash
uv run oam api --config configs/oam.yaml --host 127.0.0.1
```

The default `worker.embedded_enabled: true` requires the API, queue and worker-content DSNs. For separate worker processes, set `worker.embedded_enabled: false` in the operator YAML and run `oam api` and `oam worker` with that file. Each runner has four task slots; deployment tooling controls process count. Shared PostgreSQL admission bounds generation, embeddings and HTTP traffic independently of replica count; see [operations](docs/operations.md#4-limits-and-failure-ownership).

`GET /health/live`, `GET /health/ready`, `GET /v1/capabilities`, `/openapi.json`, and `/ui/` are available. Full-release readiness remains `503 core_only`. Configure distinct API, queue and worker-content DSNs plus extraction, Insight, embedding and abstract model roles before using the core; delegated search also needs an optional planner binding. On a fresh database, `oam migrate` selects rootless local `dev` when no root secret or handoff destination is supplied; set `OAM_ROOT_API_KEY` or `OAM_BOOTSTRAP_OUTPUT` to initialize a keyed deployment. See [operations](docs/operations.md). Batch search and optional one-call delegated search use the existing recall path; Resource/source controls, recorded debug, export validation and the browser Inspector are documented in the [HTTP API](docs/api.md). The stored User Profile uses `/profile`; document Resources use `/resources`, and structured Profile Data uses `/profile-data`; User Profile generation requires its configured model role.

## Release Boundary

v0.0.1 requires all L0–L3 layers, separate Resource ingestion, fast/normal joint recall, source lifecycle, explain/diff/recorded replay, portable export, Memory Inspector, multi-API/multi-worker recovery, conformance evidence, and a public multi-architecture image. Fast and normal are the supported recall modes. The Python SDK, targeted fact controls, supplier importers, full User Profile Studio, hosted service, database HA, and multi-region operation are later work.

No Dockerfile or public image exists yet. Packaging and publication are the final release phase after the complete source-level acceptance matrix passes.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [THIRD_PARTY.md](THIRD_PARTY.md).
