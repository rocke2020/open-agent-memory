# AGENTS.md
This file provides context for AI coding assistants (Claude Code, Codex, etc.)

Always reply only in English.

## Current Mission

Build, verify, and release Open Agent Memory v0.0.1. This repository is the self-contained product implementation and the post-handoff source of truth for public contracts, runtime code, tests, deployment assets, and release evidence.

The release must preserve L0 source evidence, derive source-backed L1 memory, generate and retrieve supported L2 Insights, build L3 Profiles only on explicit refresh, keep HTML/PDF/image Evidence Resources separate from Learned Memory, retrieve both origins together, and run with multiple stateless API and durable worker replicas over PostgreSQL/pgvector.

## Codes Architecture

- `src/oam/api/` owns FastAPI handlers, authentication admission, response envelopes, and static Inspector serving.
- `src/oam/contracts/` owns public Pydantic request/response types shared by HTTP and conformance fixtures.
- `src/oam/evidence/`, `indexing/`, `insights/`, `profiles/`, `resources/`, and `retrieval/` own the Evidence-to-Projections-to-Context data path.
- `src/oam/auth/`, `governance/`, and `workers/` own tenant/scope authority, source lifecycle, durable operations, leases, fences, and publication.
- `migrations/` owns versioned PostgreSQL schema and RLS. Runtime code must not invent schema outside migrations.
- `configs/oam.yaml` is the sole public behavior owner. Environment variables provide secrets, endpoints, model identifiers selected through `model_env`, DSNs, and an alternate config path only. The CLI loads `./.env` without overriding inherited process variables.
- `conformance/` and `tests/` own public fixtures and fail-capable unit, integration, contract, browser, and end-to-end proof.
- `src/oam/web/` contains the bundled same-origin Memory Inspector. It is an API client, never a second policy engine.
- `deploy/` and the root `Dockerfile` own the supported container topologies and are added only in the final release phase.

## Product Boundaries

- The HTTP/OpenAPI contract is the only required v0.0.1 client surface. Do not add a Python SDK without a later demand-backed decision.
- The product must not import a benchmark, legacy supplier spike, or external planning checkout as a runtime dependency.
- Ordinary search is non-generative. The caller owns final reasoning and answers.
- Evidence Resources never become L1/L2/L3 inputs. Explicit Profile Resources are the only Resource path into exact Profile fields.
- `GET /profile` never generates. `POST /profile/refresh` returns an unchanged zero-call result or a durable asynchronous operation receipt.
- PostgreSQL owns authoritative content, original bytes, operations, work claims, policy, and application-replica recovery. Do not add Redis, a queue service, an object store, or a storage plugin before measured need.

## Engineering Rules

- Use test-driven development for behavior changes: add one failing test, confirm the expected failure, implement the minimum, then run the full suite.
- Keep tenant and scope in every content identity and foreign key. API and worker content roles must be non-owner, non-superuser, and non-`BYPASSRLS` with forced RLS.
- Commit accepted source/original bytes and the operation receipt before external model work. Never hold a database transaction open during remote I/O.
- Validate authorization, size, schema, deadlines, and enabled model capabilities before dispatch. Visible LLM smoke responses require at least 1,024 output tokens and must reject length termination, empty/malformed content, and reasoning-only output.
- Preserve prior eligible content on failed refresh; never publish from an expired lease, stale fence, superseded source, deleted source, changed generation, or cancelled operation.
- Default tests use deterministic local fakes and isolated ephemeral databases. Never read private `.env`, call paid providers, delete real data, or run destructive container cleanup.
- Keep documentation self-contained and current. README is for public users; agent-only policy belongs here.
- During private development, tracked code and documentation use OAM terminology and never name external memory providers, their abbreviations, research references, or comparisons, including in comments, docstrings, examples, and links. Keep comparison inputs/results, research and attribution inventories local and ignored. Inspect staged code and documentation for this boundary before every commit. Immediately before publicizing, add public acknowledgments thanking all referenced open-source memory providers and include any required licenses/notices.

## Default Verification

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
```

Mypy checks modules outside the declared legacy baseline in `pyproject.toml`; remove baseline entries as those modules are corrected.

Passing these commands does not prove live providers, PostgreSQL recovery, browser behavior, multi-node operation, or release readiness; run the named integration and release gates before making those claims.
