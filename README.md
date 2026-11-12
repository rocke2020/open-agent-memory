# Open Agent Memory

English (canonical) | [简体中文](README_CN.md) | [日本語](README_JA.md)

Open Agent Memory (OAM) is a fully open source memory engine for AI agents. It turns conversations eligible for long-term memory into source-backed facts and Insights, keeps documents as separate Resources, and retrieves both with citations. An explicitly refreshed User Profile helps agents carry useful personal context across sessions.

## Performance & Accuracy

We chose **DeepSeek Flash** to control evaluation costs and show memory performance with a model used for everyday agent work. The completed v0.0.1 development evaluations use **high-effort first answers and at most one eligible max-effort retry**, with high-effort judging, and cover **500 LongMemEval S questions** and **200 BEAM-10M questions**.

### Recorded DeepSeek-based results

| Evaluation | OAM | Hindsight with DeepSeek | Observed difference |
|---|---|---|---|
| [LME-500: answer accuracy](https://github.com/rocke2020/open-agent-memory-benchmark/tree/32bf1921f0db7c8f0008428b06b637c1f93ff385/eval_results/lme500) | **94% (470/500)** | 89.6% (448/500) | **+4.4 percentage points** |
| [BEAM-10M: mean rubric score](https://github.com/rocke2020/open-agent-memory-benchmark/tree/32bf1921f0db7c8f0008428b06b637c1f93ff385/eval_results/beam10m) | **61.56%** | 52.16% (saved-context replay) | **+9.40 percentage points** |

**LME comparison:** OAM used DeepSeek Flash with the high-first/max-retry policy above; the [full Hindsight reproduction](https://github.com/rocke2020/agent-memory-benchmark/tree/b6eac3dbd41de03fb6ac31486b342f4d1b556dde) used DeepSeek V4 Flash 0731 for extraction and judging and V4 Pro 0813 for answers. The model versions and answer-generation settings differ.

**BEAM comparison:** both arms used the same DeepSeek Flash answer/judge policy on the same 200 questions. Hindsight's saved contexts were replayed; its engine was not rerun. OAM used 17.43% more answer context. See the [comparison methodology and per-question evidence](https://github.com/rocke2020/open-agent-memory-benchmark/blob/32bf1921f0db7c8f0008428b06b637c1f93ff385/eval_results/beam10m/README.md).

LME-500 used fast recall with **12.4K answer-context tokens on average**. BEAM-10M tested **10 complete, untruncated conversation histories**, with 20 questions per history. The linked reports record the model bindings used in each run.

These historical development runs measure retrieval, answering and judging with L1 memory; L2 Insights, User Profiles and document Resources were not scored.

See [benchmark results and reproduction](docs/benchmarks.md) for the evaluation methodology, model settings, context counters, evaluated revisions, limitations and every question's saved evidence.

[First cited recall](docs/quickstart.md) · [MCP setup](integrations/mcp/README.md) · [Benchmark results and reproduction](docs/benchmarks.md)

[![Watch OAM recall preferences with source citations](docs/assets/cited-recall-demo.gif)](https://github.com/rocke2020/open-agent-memory/releases/download/v0.0.1/oam-v0.0.1-cited-recall-demo.mp4)

[Watch the 71-second demo](https://github.com/rocke2020/open-agent-memory/releases/download/v0.0.1/oam-v0.0.1-cited-recall-demo.mp4): an existing synthetic memory, readiness and recall from a fresh MCP process, then the original source in Memory Inspector. The recording runs at 2× speed and reuses the completed index.

Connect an existing OAM deployment to your MCP client with Node.js 22+:

```json
{
  "mcpServers": {
    "oam": {
      "command": "npx",
      "args": ["-y", "@rocke-dong/oam-mcp@0.0.1"],
      "env": {
        "OAM_BASE_URL": "http://127.0.0.1:17333",
        "OAM_USER_ID": "your-user-id",
        "OAM_USER_API_KEY": "your-user-key"
      }
    }
  }
}
```

Use your own user-scoped key and keep the client configuration private. [Set up OAM first](deploy/README.md), then follow the [Claude Code and Cursor instructions](integrations/mcp/README.md#client-configuration).

## What Is Open Agent Memory?

“Open” means the complete memory system: indexing, retrieval, Profiles, user controls and native multi-node operation are available for developers to inspect, run, modify and improve.

Integrate through the HTTP API, the local [MCP server](integrations/mcp/README.md), or the OpenClaw and DeepSeek Harness plugins. The bundled Memory Inspector lets users browse memory, follow its sources, search Resources and manage what is retained.

## Why Open Agent Memory?

- **Fully open source, with native multi-node deployment.** Run stateless API replicas and durable workers across nodes over shared PostgreSQL/pgvector. Coordination, recovery and deployment code are included.
- **Recall with evidence.** Retrieve facts and Insights together with supporting original passages and citations, so the agent can reason from the source.
- **Search conversations and documents together.** HTML, PDF and image Resources remain separate from learned personal memory while sharing one retrieval path.
- **Personalize when you choose.** Build a stored User Profile from eligible memory and explicit Profile Data on request; unchanged inputs reuse it without generation.
- **Inspect and control memory.** Browse sources in the Inspector, remove an individual Memory Unit or Insight, and manage document Resources through the same authorized API.
- **Control your data and models.** Self-host your memory in PostgreSQL/pgvector and configure your own model endpoints.

See the [acceptance requirements](docs/acceptance.md), [release evidence](docs/release-evidence.md) and [published release](https://github.com/rocke2020/open-agent-memory/releases/tag/v0.0.1) for verified behavior, deployment limits and artifacts.

## Get Started

Clone the repository and pull the published v0.0.1 image for Linux AMD64 or ARM64:

```bash
git clone https://github.com/rocke2020/open-agent-memory.git
cd open-agent-memory
docker pull ghcr.io/rocke2020/open-agent-memory:0.0.1
```

Follow the [Docker Compose guide](deploy/README.md) to configure PostgreSQL and your model endpoints, initialize OAM and open the Memory Inspector. Set `OAM_IMAGE=ghcr.io/rocke2020/open-agent-memory:0.0.1` in `deploy/.env` to use the published image.

Integrate through the [HTTP API](docs/api.md), the [MCP server](integrations/mcp/README.md), or the [OpenClaw](integrations/openclaw/README.md#npm-installation) or [DeepSeek Harness](integrations/dsh/README.md) plugin. Start with the [copyable retain → readiness → recall example](docs/quickstart.md) to store a conversation, wait for indexing and inspect the returned source citations.

## Product Model

- **L0 Source evidence:** retained eligible conversations with roles, times, source identities, and learning eligibility.
- **L1 Learned memory:** source-linked facts, preferences, experiences, events, and constraints extracted from interactions eligible for long-term memory.
- **L2 Insights:** persisted synthesis over related eligible L1 records with transitive L0 support and one descriptive subtype: `preferences`, `patterns`, `projects`, `relationships`, `experiences` or `general_insights`; subtype does not alter ranking.
- **L3 User Profile:** one explicitly refreshed, read-only document built from Profile Data and eligible Learned Memory; unchanged inputs reuse it with zero generation calls.
- **Profile Data:** explicitly supplied structured basic information or preferences, mapped to exact User Profile inputs and excluded from ordinary recall.
- **Resources:** retained text/HTML/PDF/image originals, detailed passages, locators, and abstracts that remain searchable evidence and never automatically become L1–L3.

In `api_key` mode, one credential-derived tenant and one key-bound `user_id` identify a personal memory collection. An administrator creates it with `POST /v1/users` and receives that user's first key once; the host uses the user key for `POST /v1/retain` interactions, `POST /v1/users/{user_id}/resources` document originals, `POST /v1/users/{user_id}/profile-data` structured user inputs, and `POST /v1/recall`. The administrator key cannot read or write personal content directly. Local `dev` mode admits requests without a key and makes no privacy claim; `trusted` mode uses gateway-asserted tenant/user headers, with a root key required on nonlocal requests. Interactions need no caller-managed source/message IDs. Documents use a stable `document_id` with explicit `replace` or text-only `append`; OAM allocates revisions. The [HTTP API](docs/api.md) documents mode selection, JSON and multipart examples, user-key rotation, retry keys and operation readiness.

Recall uses one limit on the complete rendered context: `search.fast.context_max_tokens: 18000` and `search.normal.context_max_tokens: 32768`. Chunks are included by default; `include.chunks: false` omits full original bodies while retaining eligible learned text, required supporting facts and citations. Relevance determines admission across learned evidence and originals, including originals with no extracted facts. Complete evidence groups must fit without clipping; shared source chunks are counted once. If the only eligible L1 fact and its complete support exceed the total, return that bundle alone only when no higher-ranked root has been admitted, stop filling and report the overshoot. Insights have no such exception. `context_tokens` counts the complete returned text, including citations and metadata, with the local `o200k_base` tokenizer. It is not provider usage; hosts count their final composed model input separately.

## Architecture

OAM uses Python, FastAPI, explicit SQL through psycopg, and PostgreSQL with pgvector. API processes are stateless; PostgreSQL owns accepted originals, authorization, source lifecycle, operations, worker claims, leases, fences, and application-replica recovery. Each service uses one configured VLM through OpenAI Chat, OpenAI Responses or Anthropic Messages, with per-role reasoning effort and separate embedding and optional reranker models. There are no provider, model, endpoint, or credential defaults.

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

See [architecture](docs/architecture.md), [HTTP API](docs/api.md), [configuration guide](configs/README.md), [operations](docs/operations.md), the [Memory web UI guide](docs/memory-web-ui.md), [acceptance](docs/acceptance.md), and the current [Steps 1–4 foundation evidence](docs/foundation-evidence.md).

## Development Setup

Python 3.12–3.14 and `uv` are required.

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run oam preflight --config configs/oam.yaml --json
```

The canonical configuration defines one shared `models.vlm`, plus a separate `models.embedding`. Select `openai_chat`, `openai_responses`, or `anthropic_messages` as the generation protocol, and configure the model, endpoint and credential through private environment variables. `models.reasoning_effort` enables each role with `none`, `low`, `high` or `max`; YAML null disables that role. The VLM reasoning mode determines which settings are sent. Preflight exercises enabled text and vision roles with the selected protocol; actual model capabilities depend on the endpoint. See [model configuration](docs/operations.md) for protocol paths and reasoning options. Default tests never read private `.env` or contact external providers.

Run the API with its embedded durable worker using the operator configuration:

```bash
uv run oam api --config configs/oam.yaml --host 127.0.0.1
```

The default `worker.embedded_enabled: true` requires the API, queue and worker-content DSNs. For separate worker processes, set `worker.embedded_enabled: false` in the operator YAML and run `oam api` and `oam worker` with that file. Each runner uses the canonical `worker.max_tasks` limit, currently ten task slots; deployment tooling controls process count. Shared PostgreSQL admission bounds generation, embeddings and HTTP traffic independently of replica count; see [operations](docs/operations.md#4-limits-and-failure-ownership).

`GET /health/live`, `GET /health/ready`, `GET /v1/capabilities`, `/openapi.json`, and `/ui/` are available. Readiness returns `200` when the bounded database/schema, enabled-model configuration, admission and applicable worker checks succeed, or `503` with a reason. It makes no model calls and does not certify model quality or release readiness. Configure distinct API, queue and worker-content DSNs plus extraction, Insight, embedding and abstract model roles before using the core; delegated search also needs an optional planner binding. On a fresh database, `oam migrate` selects rootless local `dev` when no root secret or handoff destination is supplied; set `OAM_ROOT_API_KEY` or `OAM_BOOTSTRAP_OUTPUT` to initialize a keyed deployment. See [operations](docs/operations.md). Batch search and optional one-call delegated search use the existing recall path; Resource/source controls, recorded debug, export validation and the browser Inspector are documented in the [HTTP API](docs/api.md). The stored User Profile uses `/profile`; document Resources use `/resources`, and structured Profile Data uses `/profile-data`; User Profile generation requires its configured model role.

## Release Boundary

v0.0.1 requires all L0–L3 layers, separate Resource ingestion, fast/normal joint recall, source lifecycle, explain/diff/recorded replay, portable export, Memory Inspector, multi-API/multi-worker recovery, conformance evidence, and a public multi-architecture image. Fast and normal are the supported recall modes. The Python SDK, supplier importers, full User Profile Studio, hosted service, database HA, and multi-region operation are later work.

The [Docker/Compose guide](deploy/README.md) uses one non-root API/worker image, bundled Inspector assets and PostgreSQL/pgvector. The replica example simulates two APIs and two workers locally; it does not establish physical multi-host resilience. [OpenClaw](integrations/openclaw/README.md) and [DSH](integrations/dsh/README.md) install from npm, with OpenClaw also available on ClawHub and versioned plugin tarballs retained for reproducibility. Public release and image availability are separate from local candidate verification.

## Roadmap

v0.0.1 is released. The following product releases are planned; scope may change with validated user feedback, and no release dates are committed.

| Release | Planned work |
|---|---|
| v0.0.2 | Add a user-listing API, with contract, authorization rules and acceptance criteria pending design completion. Include the implemented tokenizer optimization: native token counting by default, embedding-aware source sizing and admission, and complete rendered recall-context counting. |
| v0.1.0 | Improve general retrieval quality and rerun retrieval, answering and judging against the existing completed LME-500 and BEAM-10M indexes, without repeating ingestion, extraction or indexing. The LME-500 target is at least 473/500 (94.6%); the published v0.0.1 result remains 470/500 (94.0%). |

The OpenClaw plugin's already-published 0.0.2 is an independently versioned package release; product v0.0.2 is still planned. Further work includes smoother onboarding, reducing unnecessary indexing and answer-context tokens, evaluating keyword-ranking approaches, and defining shared Resource ownership and OIDC/LDAP support. These are proposed improvements, not delivered capabilities.

Share setup and retrieval feedback in [Discussions](https://github.com/rocke2020/open-agent-memory/discussions/1). The current release's supported behavior and limits remain documented above and in the [release evidence](docs/release-evidence.md).

## Acknowledgments

We thank the contributors to [Hindsight](https://github.com/vectorize-io/hindsight), [OpenViking](https://github.com/volcengine/OpenViking), [Mem0](https://github.com/mem0ai/mem0), [MemOS](https://github.com/MemTensor/MemOS), [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) and [gbrain](https://github.com/garrytan/gbrain) for their open-source work, which informed OAM. We also thank the authors and contributors of [LongMemEval](https://github.com/xiaowu0162/LongMemEval) and [BEAM](https://github.com/mohammadtavakoli78/BEAM) for the benchmarks used in our evaluations. See the [full acknowledgments](ACKNOWLEDGMENTS.md) and [third-party notices](THIRD_PARTY.md) for project, benchmark and license credits.
