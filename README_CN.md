# Open Agent Memory

[English](README.md) | 简体中文 | [日本語](README_JA.md)

本文是[英文 README](README.md) 的中文译文，英文版为权威版本。链接指向的详细技术文档目前以英文为主。

## 什么是 Open Agent Memory？

Open Agent Memory（OAM）是采用 Apache-2.0 许可证、支持自托管的 AI Agent 记忆引擎。它将符合学习条件的会话转化为有来源支撑的事实与洞察（Insights），将文档作为独立资源（Resources）保存，并在统一检索中返回来源引用。显式刷新的用户画像（User Profile）帮助 Agent 在不同会话之间保留有用的个人上下文。

“Open”意味着完整的记忆系统：索引、检索、用户画像、用户控制以及原生多节点运行能力都包含在 Apache-2.0 开源核心中，开发者可以检查、运行、修改和改进整个系统。

通过 HTTP API、[本地 MCP 服务](integrations/mcp/README.md)，或 OpenClaw 与 DeepSeek Harness 插件接入。内置的 Memory Inspector 可让用户浏览记忆、追溯来源、搜索文档资源，并管理保留的内容。

## 为什么选择 Open Agent Memory？

- **完整开源，原生支持多节点部署。** 基于共享 PostgreSQL/pgvector，跨节点运行无状态 API 副本和持久化 Worker。协调、恢复和部署代码均属于 Apache-2.0 开源核心。
- **有证据支撑的召回。** 同时检索事实、洞察及其原始支持片段和引用，让 Agent 能基于来源进行推理。
- **统一搜索会话与文档。** HTML、PDF 和图片资源与学习得到的个人记忆保持分离，同时使用统一的检索路径。
- **按需个性化。** 显式请求后，根据符合条件的记忆和明确提供的画像数据生成并保存用户画像；输入不变时直接复用，无需生成。
- **检查和控制记忆。** 在 Inspector 中浏览来源，删除单条记忆单元或洞察，并通过同一套经过授权的 API 管理文档资源。
- **掌控数据和模型。** 将记忆自托管于 PostgreSQL/pgvector，并配置自己的模型端点。

已验证的行为、部署限制和交付物见[验收要求](docs/acceptance.md)、[发布证据](docs/release-evidence.md)和[已发布版本](https://github.com/rocke2020/open-agent-memory/releases/tag/v0.0.1)。

## 快速开始

克隆仓库并拉取适用于 Linux AMD64 或 ARM64 的 v0.0.1 已发布镜像：

```bash
git clone https://github.com/rocke2020/open-agent-memory.git
cd open-agent-memory
docker pull ghcr.io/rocke2020/open-agent-memory:0.0.1
```

按照 [Docker Compose 指南](deploy/README.md)配置 PostgreSQL 和模型端点，初始化 OAM 并打开 Memory Inspector。在 `deploy/.env` 中设置 `OAM_IMAGE=ghcr.io/rocke2020/open-agent-memory:0.0.1`，即可使用已发布镜像。

通过 [HTTP API](docs/api.md)、[本地 MCP 服务](integrations/mcp/README.md)接入，或安装 [OpenClaw](integrations/openclaw/README.md#npm-installation) 或 [DeepSeek Harness](integrations/dsh/README.md) 插件。先运行[可复制的写入 → 等待就绪 → 召回示例](docs/quickstart.md)，保存会话、等待索引完成并检查返回的来源引用。

MCP 接入需要 Node.js 22 或更高版本、已部署的 OAM 实例，以及对应用户的 API 密钥。MCP 客户端在本机启动适配器，通过 stdio 通信；适配器再连接你的 OAM 实例。安装和客户端配置见 [MCP 接入指南](integrations/mcp/README.md)。

## 性能与准确率

**OAM 在 LongMemEval S（LME-500）上取得了 94.0% 的准确率（470/500）**，抽取、回答和评判均使用 `deepseek-v4.1-flash`，面向日常 Agent 工作负载。Fast 召回提供的**回答上下文平均为 12.4K tokens**。该结果包含已记录的单次重试策略。详见 [LME-500 结果与逐题证据](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/lme500)。

对于大规模记忆工作负载，我们采用 [BEAM-10M](https://github.com/mohammadtavakoli78/BEAM)，即该基准的一千万 token 会话规模。[本次评估](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/beam10m)使用 **10 段完整、未截断的会话历史，每段均超过 10 million tokens**，通过 **200 道问题**检验长历史下的召回质量和检索性能。

这些结果来自已记录的 v0.0.1 开发快照。链接中的报告提供了被评估的版本、设置、重试策略和完整回答上下文。

## 产品模型

- **L0 来源证据：** 保留符合条件的会话，包括角色、时间、来源标识和学习资格。
- **L1 学习记忆：** 从符合学习条件的交互中抽取、与来源关联的事实、偏好、经历、事件和约束。
- **L2 洞察（Insights）：** 对相关且符合条件的 L1 记录进行综合并持久化，通过支持链追溯至 L0；每条洞察具有一个描述性子类型：`preferences`、`patterns`、`projects`、`relationships`、`experiences` 或 `general_insights`，子类型不影响排名。
- **L3 用户画像（User Profile）：** 根据画像数据和符合条件的学习记忆构建、显式刷新且只读的单一文档；输入不变时直接复用，生成模型调用次数为零。
- **画像数据（Profile Data）：** 明确提供的结构化基本信息或偏好，映射为精确的用户画像输入，不参与普通召回。
- **文档资源（Resources）：** 保留的文本、HTML、PDF 或图片原件，以及详细片段、定位信息和摘要；它们始终是可检索证据，不会自动成为 L1–L3。

在 `api_key` 模式下，由凭据确定的租户与用户密钥绑定的 `user_id` 共同标识一个个人记忆集合。管理员通过 `POST /v1/users` 创建用户，并仅在创建时收到该用户的首个密钥；宿主应用使用用户密钥调用 `POST /v1/retain` 写入交互、调用 `POST /v1/users/{user_id}/resources` 写入文档原件、调用 `POST /v1/users/{user_id}/profile-data` 写入结构化用户输入，并调用 `POST /v1/recall` 召回。管理员密钥不能直接读写个人内容。本地 `dev` 模式允许不带密钥的请求，不提供隐私隔离保证；`trusted` 模式使用网关声明的租户和用户身份，非本地请求需要根密钥。交互写入不要求调用方管理来源或消息 ID。文档使用稳定的 `document_id`，并显式选择 `replace` 或仅适用于文本的 `append`；修订版本由 OAM 分配。[HTTP API](docs/api.md) 说明了模式选择、JSON 和 multipart 示例、用户密钥轮换、重试键和操作就绪状态。

召回对完整渲染后的上下文使用单一上限：`search.fast.context_max_tokens: 18000` 和 `search.normal.context_max_tokens: 32768`。默认包含原文片段；设置 `include.chunks: false` 会省略完整原文正文，但保留符合条件的学习文本、必需的支持事实和引用。学习证据与原文均按相关性接纳，包括没有抽取出事实的原文。完整证据组必须在预算内整体放入，不能截断；共享来源片段只计数一次。如果唯一符合条件的 L1 事实及其完整支持材料超出总上限，且此前尚未接纳排名更高的主结果，则仅返回这一证据组、停止继续填充，并报告超出量。洞察不适用这一例外。`context_tokens` 使用本地 `o200k_base` tokenizer 统计完整返回文本，包括引用和元数据。它不等于供应商用量；宿主应用应另行统计最终组合后的模型输入。

## 架构

OAM 使用 Python、FastAPI、通过 psycopg 编写的显式 SQL，以及带 pgvector 的 PostgreSQL。API 进程无状态；PostgreSQL 负责已接受的原件、授权、来源生命周期、操作、Worker 任务认领、租约、隔离令牌（fence）以及应用副本恢复。每个服务通过 OpenAI Chat、OpenAI Responses 或 Anthropic Messages 使用一个配置的 VLM，可按角色设置推理强度，并单独配置 embedding 模型和可选的 reranker 模型。系统不提供默认供应商、模型、端点或凭据。

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

详见[架构](docs/architecture.md)、[HTTP API](docs/api.md)、[配置指南](configs/README.md)、[运维](docs/operations.md)、[Memory Web UI 指南](docs/memory-web-ui.md)、[验收](docs/acceptance.md)以及当前的[步骤 1–4 基础证据](docs/foundation-evidence.md)。

## 开发环境

需要 Python 3.12–3.14 和 `uv`。

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run oam preflight --config configs/oam.yaml --json
```

权威配置定义共享的 `models.vlm` 和独立的 `models.embedding`。选择 `openai_chat`、`openai_responses` 或 `anthropic_messages` 作为生成协议，并通过私有环境变量配置模型、端点和凭据。`models.reasoning_effort` 可用 `none`、`low`、`high` 或 `max` 为各角色启用相应设置；YAML null 会禁用该角色。VLM 的推理模式决定实际发送哪些参数。Preflight 使用所选协议检查已启用的文本和视觉角色；实际模型能力取决于端点。协议路径和推理选项见[模型配置](docs/operations.md)。默认测试不会读取私有 `.env`，也不会访问外部供应商。

使用运维配置运行 API 及其内嵌的持久化 Worker：

```bash
uv run oam api --config configs/oam.yaml --host 127.0.0.1
```

默认的 `worker.embedded_enabled: true` 要求配置 API、队列和 Worker 内容访问所用的 DSN。要使用独立 Worker 进程，在运维 YAML 中设置 `worker.embedded_enabled: false`，并使用该文件分别运行 `oam api` 和 `oam worker`。每个运行器都使用权威配置中的 `worker.max_tasks` 限制，当前为十个任务槽；进程数量由部署工具控制。共享的 PostgreSQL 准入机制对生成、embedding 和 HTTP 流量施加限制，不随副本数量增加而放大，详见[运维](docs/operations.md#4-limits-and-failure-ownership)。

系统提供 `GET /health/live`、`GET /health/ready`、`GET /v1/capabilities`、`/openapi.json` 和 `/ui/`。就绪检查在有界的数据库与 schema、已启用模型配置、准入机制以及适用的 Worker 检查成功时返回 `200`，否则返回 `503` 并说明原因。它不调用模型，也不证明模型质量或发布就绪。使用核心功能前，需要配置独立的 API、队列和 Worker 内容 DSN，以及抽取、洞察、embedding 和摘要模型角色；委托搜索还需要可选的 planner 绑定。在新数据库上，如果既未提供根密钥也未提供密钥交接目标，`oam migrate` 会选择无根密钥的本地 `dev` 模式；设置 `OAM_ROOT_API_KEY` 或 `OAM_BOOTSTRAP_OUTPUT` 可初始化带密钥的部署。详见[运维](docs/operations.md)。批量搜索和可选的单次调用委托搜索复用已有召回路径；文档资源与来源控制、已记录的调试信息、导出校验以及浏览器 Inspector 见 [HTTP API](docs/api.md)。保存的用户画像使用 `/profile`，文档资源使用 `/resources`，结构化画像数据使用 `/profile-data`；生成用户画像需要配置对应模型角色。

## 发布范围

v0.0.1 要求包含全部 L0–L3 层、独立的文档资源摄入、fast/normal 统一召回、来源生命周期、解释与差异比较及已记录结果的重放、可移植导出、Memory Inspector、多 API/多 Worker 恢复、一致性验收证据以及公开的多架构镜像。支持的召回模式为 fast 和 normal。Python SDK、供应商导入器、完整的用户画像工作台、托管服务、数据库高可用和多地域运行属于后续工作。

[Docker/Compose 指南](deploy/README.md)使用一个非 root 的 API/Worker 镜像，包含 Inspector 静态资源，并使用 PostgreSQL/pgvector。副本示例在本地模拟两个 API 和两个 Worker，并不证明跨物理主机的故障恢复能力。[OpenClaw](integrations/openclaw/README.md) 和 [DSH](integrations/dsh/README.md) 使用带版本的插件归档包，安装者无需 npm 发布账号。公开版本与镜像的可用性，和本地候选版验证是不同的验收事项。

## 致谢

感谢 [Hindsight](https://github.com/vectorize-io/hindsight)、[OpenViking](https://github.com/volcengine/OpenViking)、[Mem0](https://github.com/mem0ai/mem0)、[MemOS](https://github.com/MemTensor/MemOS)、[TencentDB Agent Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory) 和 [gbrain](https://github.com/garrytan/gbrain) 的贡献者。他们的开源工作为 OAM 提供了参考。也感谢 [LongMemEval](https://github.com/xiaowu0162/LongMemEval) 和 [BEAM](https://github.com/mohammadtavakoli78/BEAM) 的作者与贡献者，提供了我们评估所用的基准。项目、评测与许可证致谢详见[完整致谢](ACKNOWLEDGMENTS.md)和[第三方声明](THIRD_PARTY.md)。

## 许可证

Apache License 2.0。详见 [LICENSE](LICENSE) 和 [THIRD_PARTY.md](THIRD_PARTY.md)。
