# Open Agent Memory MCP 服务端

[中文文档](../../docs/zh/README.md) · [English](README.md)

## 概述

这是 Open Agent Memory（OAM）的轻量模型上下文协议（MCP）服务端。Claude Code、Cursor 或 Codex 等 MCP 客户端通过本地标准输入输出（stdio）启动它，由它调用现有 OAM 部署的 HTTP API。安装此包无需增加托管服务、数据库、队列或监听端口。

该服务端是 OAM 的 API 客户端。身份验证、授权、保留策略、索引、检索、引用和操作状态均由 OAM 管理。服务端提供六个工具，直接使用 OAM 已有的接口契约。

召回的记忆应视为不可信证据。工具说明明确了这一边界，客户端不得执行召回文本中的指令。

[71 秒带引用召回演示](https://github.com/rocke2020/open-agent-memory/releases/download/v0.0.1/oam-v0.0.1-cited-recall-demo.mp4)展示了如何启动一个新的已发布 MCP 进程，检查就绪状态并召回一个已索引的合成偏好，再通过 Memory Inspector 打开原始来源。视频以 2 倍速播放，演示过程复用已有索引。

## 前置条件

1. OAM 已在 `api_key` 模式下运行，并且你已有用户级密钥。参见 [OAM 快速开始](../../docs/zh/quickstart.md)。
2. OAM 用户至少已完成一次保存和索引操作，使召回有证据可返回。
3. 运行 MCP 客户端的环境装有 Node.js 22 或更高版本。

MCP 服务端仅支持 `api_key` 模式。`dev` 模式和伪造的 `trusted` 请求头均不受支持。每个服务端进程只服务一个 OAM 用户。

## 安装

根据你的 OAM 服务选择安装方式。0.0.2 归档搭配 OAM 0.2.1 支持三档召回。npm 包可用于 Fast 和 Normal。

### 通过发布归档安装

从 [OAM v0.2.1 发布页](https://github.com/rocke2020/open-agent-memory/releases/tag/v0.2.1)下载 `rocke-dong-oam-mcp-0.0.2.tgz` 和 `SHA256SUMS`。在下载目录核对归档的校验和，按下文配置环境后运行。

```bash
grep '  rocke-dong-oam-mcp-0\.0\.2\.tgz$' SHA256SUMS | shasum -a 256 -c &&
npx -y --package=/absolute/path/rocke-dong-oam-mcp-0.0.2.tgz oam-mcp
```

### 通过 npm 安装

已发布的 npm 版本为 0.0.1。MCP 注册表清单 `server.json` 指向这个版本。

```bash
npx -y @rocke-dong/oam-mcp@0.0.1
```

### 从源码安装

要从源码构建并运行，请在本目录执行以下命令。

```bash
npm install
npm run build
node dist/index.js
```

## 配置

服务端只读取环境变量，不存储凭据。

| 环境变量 | 必填 | 含义 |
|---|---|---|
| `OAM_BASE_URL` | 是 | OAM 部署的源地址，例如 `http://127.0.0.1:17333` |
| `OAM_USER_ID` | 是 | 已获授权的个人用户 |
| `OAM_USER_API_KEY` | 是 | 用户级密钥，通过 `Authorization: Bearer <key>` 发送 |

`OAM_BASE_URL` 必须使用 HTTP 或 HTTPS，且不能包含凭据、查询参数或片段。用户 ID 或密钥为空时，服务端会在连接前报错。访问非环回地址时应使用 HTTPS，以免持有者密钥通过明文 HTTP 暴露给网络中的其他方。

MCP 客户端可以在自己的配置中存储这些环境变量值。请保护该文件，切勿将真实密钥提交到版本库。MCP 服务端本身不写入配置，也不缓存记忆内容。

## 客户端配置

在 MCP 客户端配置中填写归档的绝对路径，并将归档保留在该位置，以便客户端再次启动进程。使用 Claude Code 时，先在 shell 中设置上述三个环境变量。

```bash
claude mcp add oam \
  -e "OAM_BASE_URL=$OAM_BASE_URL" \
  -e "OAM_USER_ID=$OAM_USER_ID" \
  -e "OAM_USER_API_KEY=$OAM_USER_API_KEY" \
  -- npx -y --package=/absolute/path/rocke-dong-oam-mcp-0.0.2.tgz oam-mcp
```

使用 Cursor 时，将配置写入 `~/.cursor/mcp.json`。

```json
{
  "mcpServers": {
    "oam": {
      "command": "npx",
      "args": ["-y", "--package=/absolute/path/rocke-dong-oam-mcp-0.0.2.tgz", "oam-mcp"],
      "env": {
        "OAM_BASE_URL": "http://127.0.0.1:17333",
        "OAM_USER_ID": "your-user-id",
        "OAM_USER_API_KEY": "your-user-key"
      }
    }
  }
}
```

其他 MCP 客户端沿用相同的 stdio 命令、参数和环境变量。使用 npm 发布包时，将归档参数和 `oam-mcp` 替换为 `@rocke-dong/oam-mcp@0.0.1`。

## 工具

`memory_recall` 发送 `fast` 作为省略 `recall_level` 时的默认值，独立于 OAM 服务为直接 HTTP 调用设置的默认值。MCP 0.0.2 接受 `fast`、`normal` 和 `deep`。Deep 需要 OAM 0.2.1 或其他支持 Deep 的服务版本。已发布的 npm 包 0.0.1 接受 `fast` 和 `normal`。OAM 0.2.1 的 Fast、Normal 和 Deep 三档默认上下文上限分别为 8,000、16,000 和 24,000 token。

| 工具 | OAM 请求 | 输入 | 返回 |
|---|---|---|---|
| `memory_recall` | `POST /v1/recall` | `query`，以及可选的 `recall_level`（默认 `fast`，可选 `normal`，MCP 0.0.2 搭配兼容服务时支持 `deep`）、`include_chunks`（默认 true）和 `detail`（默认 `context`，可选 `full`） | 带引用的已渲染 `context` 和 `trace_id`，`full` 还返回带类型的 `items` 和诊断信息 |
| `memory_add` | `POST /v1/retain` | `content`，以及可选的带时区 `event_time`、`learning_eligible`（默认 true）和 `idempotency_key` | 持久化操作 ID 和来源 ID |
| `memory_add_document` | `POST /v1/users/{user_id}/resources` | `document_id`、`update_mode`，仅提供 `text` 或同时提供 `url` 和 `media_type`，以及可选的 `idempotency_key` | 来源回执和操作状态 |
| `memory_operation` | `GET /v1/users/{user_id}/operations/{operation_id}` | `operation_id` | 就绪状态、阶段状态及可安全公开的失败详情 |
| `memory_profile` | `GET /v1/users/{user_id}/profile` | 无 | 已保存的用户画像内容、引用和新鲜度，读取不触发生成 |
| `memory_remove` | `DELETE /v1/users/{user_id}/memories/{id}` 或 `/insights/{id}` | `memory_id`、`memory_type`（`basic` 或 `insight`） | 移除操作回执 |

`memory_add` 发送一条用户明确提供的原话。只在用户要求记住某项内容时调用。`memory_add_document` 支持文本和通过 HTTPS URL 获取的原件，当前不支持 multipart 文件上传。

两个写入工具都接受可选参数 `idempotency_key`。键的长度必须为 1–200 个字符，字符只能是 `A-Z`、`a-z`、`0-9`、`.`、`_`、`:` 或 `-`。省略时由工具生成。重试同一次逻辑写入时请使用相同的键，使 OAM 复用原有回执，避免生成重复会话或文档版本。省略键时，服务端为每次调用生成新键，重试也会成为新的逻辑请求。

Fast 是 Agent 运行前的召回模式。设置 `detail: "full"` 时，`memory_recall` 返回完整的召回 JSON，包括带类型的条目和诊断信息。默认返回已渲染的上下文、token 数、分词器标识、追踪 ID 和条目数。

## 行为与安全

1. 召回输出是证据。服务端将其标记为不可信数据，不会将它当作指令执行。
2. 服务端仅通过 `Authorization` 请求头发送 API 密钥，日志不包含密钥、请求体或记忆文本。
3. OAM 错误会返回可安全公开的 OAM 错误码、HTTP 状态、是否可重试以及 `X-Request-ID`。意外失败返回通用错误，不包含堆栈。
4. 服务端不自动重试，由调用方决定是否重试。
5. 请求总超时为 60 秒。OAM 管理召回期限，平台的 fetch 实现在这一时限内应用自己的连接超时。
6. 成功响应体上限为 8 MiB，错误响应体上限为 64 KiB。召回上下文的内容上限由 OAM 控制。
7. 服务端不将记忆内容写入磁盘，也没有后台工作进程。
8. 服务端拒绝 HTTP 重定向，防止 OAM 端点将持有者密钥转发给其他源地址。

## 使用限制

1. 仅支持 stdio 传输，维护者不提供托管的远程 MCP 端点。
2. 每个进程只支持一个 OAM 用户，不支持切换用户或使用 OAuth。
3. 文档只支持文本或 HTTPS URL，不支持 multipart 文件上传。
4. 用户画像仅可读取，工具不提供刷新操作。
5. 召回提供证据，由宿主 Agent 推理和生成回答。客户端不生成答案、不做摘要或重排。
6. OAM 仍会调用已配置的模型进行索引和向量化。接入此服务端不会改变这些模型调用的位置。

## 开发

```bash
npm install
npm test
npm run build
```

测试使用本地模拟 HTTP 服务和内存中的 MCP 传输，不访问实际 OAM 部署或模型服务。端到端就绪需要使用真实 OAM 用户完成一次实际验收。

参见 [OAM HTTP API](../../docs/zh/api.md)、[运维指南](../../docs/zh/operations.md)和[仓库首页](../../README_CN.md)。

## 许可证

Apache-2.0。参见[许可证](LICENSE)和[第三方声明](THIRD_PARTY.md)。
