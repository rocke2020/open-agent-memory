# Open Agent Memory

**Useful memory for everyday AI agents.**

[English](README.md) | [简体中文](README_CN.md) | [日本語](README_JA.md)

[![Open Agent Memory MCP server](https://glama.ai/mcp/servers/rocke2020/open-agent-memory/badges/score.svg)](https://glama.ai/mcp/servers/rocke2020/open-agent-memory)

[Get started](#quick-start) · [Performance](#performance--accuracy) · [Integrations](#connect-your-agent) · [Documentation](docs/README.md) · [Releases](https://github.com/rocke2020/open-agent-memory/releases)

Open Agent Memory (OAM) is a fully open-source memory engine for AI agents. It remembers useful facts, preferences and experiences across conversations, then retrieves relevant evidence with citations when your agent needs it.

Keep your conversations and documents in your own PostgreSQL database, choose your model providers, and inspect what the agent remembers. OAM supplies the memory; your agent writes the answer.

## Performance & Accuracy

**On LongMemEval S (LME-500), OAM scored 476/500 (95.2%) with an 8,000-token context limit and 7,989.54 mean saved context tokens.** Fast recall used PostgreSQL full-text search (FTS) for matching and BM25 plus cover density for ranking. Cover density rewards nearby query terms. Mean context was **29.8% smaller** than in the completed 18k Normal FTS comparison, which scored **478/500 (95.6%)**. [LME-500 method and results](docs/benchmarks.md#1-longmemeval-s-at-8k-and-18k)

| Benchmark | OAM | Published Hindsight | Mean saved context |
| --- | --- | --- | --- |
| **LongMemEval S**, accepted answers | **95.2% (476/500)** | 94.6% (473/500) | **7,989.54** vs 43,124.55 tokens (**81.47% fewer**) |
| **BEAM-10M**, mean rubric credit | **78.23%** | 64.08% | **24,676.88** vs 27,373.25 tokens (**9.85% fewer**) |
| **BEAM-10M**, pass rate (score ≥ 0.5) | **86.0% (172/200)** | 73.5% (147/200) | |

**BEAM-10M tests memory at the 10-million-token scale per conversation.** It covers 200 questions across ten histories and tests event ordering, temporal reasoning and contradiction resolution. OAM gained **14.15 percentage points in mean rubric credit and 25 additional passes**. The full result uses 180 current recalls with a **24,000-token** native context cap and 20 previously saved contexts. [BEAM method and results](docs/benchmarks.md#3-beam-10m)

OAM exceeds the top recorded Hindsight scores on both full datasets using low-cost **MiniMax M3.1 Flash** adaptive for answering and judging. [AMB results manifest](https://github.com/vectorize-io/agent-memory-benchmark/blob/c963dba91dfe6c459f64bb871c5dafb2f3de1bf1/results-manifest.json)

**Both benchmarks use the same questions, reference answers and scoring rules as the published Hindsight runs.** OAM uses the same answer and judge prompt templates from pinned Agent Memory Benchmark (AMB) sources. Three BEAM judge calls added a JSON example in the provider wrapper and kept the judge text and scoring rules. [Pinned Hindsight LME-500 result](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/outputs/longmemeval/hindsight/rag/s.json.gz) · [BEAM protocol](docs/benchmarks.md#3-beam-10m)

The recorded comparisons measure end-to-end results with different models and memory pipelines. For LME-500, both context averages cover 500 saved texts counted with `o200k_base`. The 8k score comes from a benchmark adapter using retained L1 indexes. [LME-500 comparison details](docs/benchmarks.md#comparison-with-the-recorded-hindsight-result)

MiniMax lists USD 0.15 per million uncached input tokens and USD 0.60 per million output tokens. [Official MiniMax pricing](https://platform.minimax.io/docs/pricing/overview#llm)

Scores can vary when answer and judge models are rerun. In a [same-input MiniMax example](docs/benchmarks.md#answer-and-judge-variability), one answer was correct and another was incorrect; this illustrates observed variability, not its frequency.

[Results, model pricing and methodology](docs/benchmarks.md) · [OAMB results index](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/eval_results/README.md) · [BEAM per-question evidence](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/beam10m-20261119)

## Why OAM?

- **Remember across sessions.** Retain useful preferences, decisions and experiences so your agent can use them in a new conversation.
- **Check the evidence.** Recalled memories link back to original source messages. Browse and remove memory through the Memory Inspector.
- **Search conversations and documents together.** Recall relevant learned memory alongside text, HTML, PDF and image Resources; documents stay separate from learned personal memory.
- **Build a useful personal Profile.** Refresh a stored User Profile manually or on a schedule, or generate a custom Profile for a specific purpose.
- **Own your data and models.** Self-host with PostgreSQL/pgvector, configure your own endpoints, and control the recall context budget.

## Quick Start

You need **Docker with Compose**, **Git**, a generation endpoint that supports text and image inputs, and an OpenAI-compatible embedding endpoint. OAM has no default model endpoints or credentials. Model preflight and indexing use your configured providers and may incur charges.

### 1. Prepare your settings

```bash
git clone https://github.com/rocke2020/open-agent-memory.git
cd open-agent-memory
cp deploy/.env.example deploy/.env
```

Edit `deploy/.env`:

- Set `VLM_BASE_URL`, `VLM_API_KEY`, `VLM_MODEL` and `OAM_EMBEDDING_URL`, `OAM_EMBEDDING_KEY`, `OAM_EMBEDDING_MODEL` to your provider settings.
- Set each database password and `OAM_ROOT_API_KEY` to a separate random value; generate each with `openssl rand -hex 24`.
- The default embedding dimension is **1024**. If your endpoint returns a different supported size, set `index.embedding_dimensions` in `deploy/oam.yaml` **before initialization**. See [model configuration](docs/operations.md#1-configuration).

### 2. Start OAM

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait postgres
docker compose --env-file deploy/.env -f deploy/compose.yaml run migrate
docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T postgres psql -U postgres -d oam -f /bootstrap/runtime-roles.sql
docker compose --env-file deploy/.env -f deploy/compose.yaml run api preflight --json
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait api worker
```

Stop if a command fails and resolve the reported error before continuing. The Compose file selects the **v0.2.1** image for Linux AMD64 or ARM64. See the [deployment guide](deploy/README.md) for ports, model servers on your host, source builds and replica setup.

### 3. Store and recall your first memory

Open the **Memory Inspector at <http://127.0.0.1:17333/ui/>**. Follow [Create your personal collection](docs/start-here.md#2-create-your-personal-collection), then [Store and recall a preference](docs/start-here.md#3-store-and-recall-a-preference): create a user key, retain a small preference, wait for indexing, and retrieve it with citations.

Your first success is a recalled preference whose citation leads to the original message. Then connect your agent and ask it to use that preference in a fresh session.

**[Complete first-memory walkthrough →](docs/start-here.md)**

## Connect Your Agent

Start the service and create a user key first. Client packages connect to OAM; they do not start the server.

| Agent or application | Integration |
| --- | --- |
| Claude Code or Cursor | [MCP server and client configuration](integrations/mcp/README.md#release-archive-installation), Node.js 22+ |
| OpenClaw | [Plugin installation](integrations/openclaw/README.md#release-archive-installation), with automatic recall and eligible capture |
| DeepSeek Harness (DSH) | [DSH plugin](integrations/dsh/README.md#release-archive-installation) |
| Your own application | [Copyable HTTP example](docs/quickstart.md) and [API reference](docs/api.md) |

## See What Your Agent Remembers

![Memory Inspector showing recalled evidence and its original source](docs/assets/inspect-memory.png)

The Inspector lets you browse retained sources, inspect recalled memories and citations, manage Resources, and refresh your User Profile. This screenshot uses synthetic demonstration data. [Inspector guide](docs/memory-web-ui.md).

## How Memory Works

OAM preserves original conversations (**L0**), extracts source-backed facts (**L1**), and builds supported Insights across related facts (**L2**). A stored, refreshable User Profile (**L3**) helps the agent personalize its behavior. Text, HTML, PDF and image **Resources** remain separate evidence and can be retrieved alongside learned memory.

Recall defaults to **Fast** and returns cited context for your agent to use. Fast, Normal and Deep have default context limits of **8,000**, **16,000** and **24,000** tokens. Set `search.default_recall_level` in YAML to `fast`, `normal` or `deep` for API requests that omit `recall_level`. An explicit request mode takes precedence.

[Memory and retrieval design](docs/indexing-retrieval.md) · [Architecture](docs/architecture.md) · [Configuration](configs/README.md)

## v0.2.1

v0.2.1 adds the configurable Fast default, three recall presets and native BM25 ranking. The English and Simplified Chinese documentation now covers the same setup, integration and operations paths.

See the [release notes](docs/releases/0.2.1.md) for upgrade instructions, verification scope and downloads. Agent integrations have independent versions.

## Documentation & Development

Browse the [English documentation](docs/README.md) or [Simplified Chinese documentation](docs/zh/README.md).

| Goal | Guide |
| --- | --- |
| Deploy or upgrade OAM | [Docker Compose](deploy/README.md) |
| Store and recall through HTTP | [Quickstart](docs/quickstart.md) · [API reference](docs/api.md) |
| Configure providers and operate the service | [Operations](docs/operations.md) |
| Understand verified behavior and limits | [Acceptance](docs/acceptance.md) · [Release notes](docs/releases/0.2.1.md) |
| Build, test or contribute | [Contributing](CONTRIBUTING.md) · [Release procedure](docs/releasing.md) |

Future work includes smoother onboarding, lower indexing and answer-context token usage, and shared Resource ownership. The Python SDK, hosted service, database high availability and OIDC/LDAP support remain future work. Share setup and retrieval feedback in [Discussions](https://github.com/rocke2020/open-agent-memory/discussions/1).

## Acknowledgments

OAM is informed by open-source work from [Hindsight](https://github.com/vectorize-io/hindsight), [OpenViking](https://github.com/volcengine/OpenViking), [Mem0](https://github.com/mem0ai/mem0), [MemOS](https://github.com/MemTensor/MemOS), [TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) and [gbrain](https://github.com/garrytan/gbrain). We thank the [LongMemEval](https://github.com/xiaowu0162/LongMemEval) and [BEAM](https://github.com/mohammadtavakoli78/BEAM) authors and contributors. See [full acknowledgments](ACKNOWLEDGMENTS.md) and [third-party notices](THIRD_PARTY.md).

## License

[Apache License 2.0](LICENSE).
