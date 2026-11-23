# 在 OpenClaw 中使用 Open Agent Memory

[中文文档](../../docs/zh/README.md) · [English](README.md)

## 概述

此插件将一个 OAM 用户接入所选 OpenClaw Agent 的本地 WebChat 会话和私聊。本地 WebChat 需要所有者授权，私聊需要逐一明确批准。`targetAgentId` 默认是 `main`，独立于 OAM 的 `userId`。请先在 `api_key` 模式下运行 OAM，并准备用户级密钥。插件提供 Fast 自动召回、采集带标识的已完成用户轮次，以及 `memory_search` 和 `memory_remove` 工具。

## 通过发布归档安装

若要搭配 OAM 0.2.1 使用 Fast、Normal 和 Deep，请安装 [OAM v0.2.1 发布页](https://github.com/rocke2020/open-agent-memory/releases/tag/v0.2.1)上的 OpenClaw 插件 0.0.3。从该发布页下载 `rocke-dong-oam-openclaw-0.0.3.tgz` 和 `SHA256SUMS`。先在下载目录校验归档，再执行安装。

```bash
grep '  rocke-dong-oam-openclaw-0\.0\.3\.tgz$' SHA256SUMS | shasum -a 256 -c &&
openclaw plugins install ./rocke-dong-oam-openclaw-0.0.3.tgz --force --accept-capabilities
```

安装命令会显示插件目录。用该路径配置已有的 OAM 用户。

```bash
node '<installed-plugin-directory>/setup.mjs' --user-id '<oam-user-id>'
```

启动 Gateway 前，请阅读下文的配置和会话准入规则。卸载时运行 `openclaw plugins uninstall oam-openclaw`，然后恢复原先的记忆插件槽位配置。

## 通过 npm 安装

公开 npm 包为 0.0.2，支持 Fast 和 Normal。显式搜索省略模式时使用 Normal。安装无需登录 npm。配置时使用 OpenClaw 显示的安装目录。

```bash
openclaw plugins install npm:@rocke-dong/oam-openclaw@0.0.2 --force --accept-capabilities
node '<installed-plugin-directory>/setup.mjs' --user-id '<oam-user-id>'
```

## 通过 ClawHub 安装

OpenClaw 社区注册表上的包名是 `@rocke2020/oam-openclaw`，宿主插件 ID 是 `oam-openclaw`，npm 包名是 `@rocke-dong/oam-openclaw`。安装前请在社区包页面查看版本和安全扫描状态。本文所述的 0.0.3 包请通过发布归档安装。

```bash
openclaw plugins install clawhub:@rocke2020/oam-openclaw --force --accept-capabilities
node '<installed-plugin-directory>/setup.mjs' --user-id '<oam-user-id>'
```

## 从源码安装

先启动 OAM 并创建用户级密钥，参见[运维指南](../../docs/zh/operations.md)。在当前目录安装插件，然后通过同一配置命令接入既有的 OAM API。

```bash
npm ci
npm run build
openclaw plugins install . --force --accept-capabilities
npm run setup -- --user-id '<oam-user-id>'
```

配置脚本会在终端提示输入缺少的值。默认服务地址是 `http://127.0.0.1:17333`，密钥环境变量是 `OAM_USER_API_KEY`。可以用 `--url` 和 `--key-env` 覆盖这两项，或提供以上三个参数以非交互方式配置。使用 `--agent-id <id>` 可选择 `main` 以外的 Agent。省略时保留已有选择，无已有选择时使用插件默认值 `main`。脚本将经校验的配置一次性写入 OpenClaw，选择 OAM 作为记忆插件，禁用内置的 `memory-core` 条目以避免两个 `memory_search` 工具冲突，并为所选 Agent 启用经所有者授权的本地 WebChat 会话。脚本只保存密钥引用（SecretRef），不会保存凭据值。将密钥环境变量提供给 Gateway，例如放入它的私有环境文件，然后运行 `openclaw gateway restart --safe`。配置脚本不会启动 OAM 或 Gateway，也不会发送模型请求。

如果已有非空的私聊允许列表，脚本会拒绝配置，以免将已有会话悄悄绑定到其他用户。要使用已批准的私聊，请核对用户范围，再手动设置 `plugins.entries.oam-openclaw.config.allowedDirectConversations`，明确指定渠道、聊天 ID 和发送者 ID。脚本也不会覆盖显式禁用的会话钩子。请保存原有记忆插件槽位配置和 `memory-core` 的启用状态，以便回滚。Gateway 启动后，检查插件在运行中的实际状态。离线检查可以在不解析 SecretRef 的情况下注册工具定义，运行中的 Gateway 才使用解析出的密钥。

本地接入支持所选 Agent 的默认 WebChat 会话、Dashboard 和 Responses 会话，以及其他所有者的 WebChat 会话键。OpenClaw 必须识别出调用者是所有者，才会向该本地会话开放两个记忆工具。自动召回需要绑定到当前轮次的工具授权，采集时会再次核对所有者身份和已完成轮次的完整纯文本用户提示。隐私会话、定时、内部、子 Agent、渠道来源、其他 Agent 和非所有者轮次均不走此本地路径。其他渠道必须明确批准一组私聊标识。个人部署时，建议让 Gateway 绑定到环回地址并启用 token 身份验证。若要新建单独的会话，请在每次 Responses 请求中提供 `user` 值。

## Cron 任务与执行记忆

将 `plugins.entries.oam-openclaw.config.captureCronJobs` 设为 `true`，即可采集所选 Agent 的周期任务和已观测到的执行状态。此项默认关闭，独立于会话准入。宿主必须按文档通过 `cron_reconciled` 和 `cron_changed` 事件提供任务快照。插件不会直接读取旧版 JSON 文件或 SQLite。OAM 用户密钥除召回权限外，还需要 `memory:append` 权限。

```bash
openclaw config set plugins.entries.oam-openclaw.config.captureCronJobs true --strict-json
openclaw gateway restart --safe
```

调度器启动或重新加载后，插件会读取已有任务，只学习已启用的周期性用户任务，且任务必须明确设置与 `targetAgentId` 匹配的 `agentId`。Gateway 心跳任务、启动快照中处于禁用状态的任务、未知载荷类型以及名称和描述都无有效信息的任务，都在请求 OAM 前跳过。支持的用户任务载荷类型有 `agentTurn`、`systemEvent`、`command` 和 `script`，按宿主标记的类型分类，与任务名称中的关键词无关。只有 `cron` 和 `every` 计划会作为周期任务定义保存。一次性提醒、进程退出监视器和流触发器不会成为长期任务记忆。任务定义保存任务 ID、名称、可选的人工描述，以及 cron 表达式和时区或执行间隔。任务提示词全文、可执行载荷、会话路由、唤醒策略、间隔起始锚点、随机偏移以及模型和工具设置均不保存。如果缺少描述，只保存名称和时间安排。需要说明任务用途时，请添加任务描述。

在调度器运行期间显式添加、更新或移除任务时，插件以带日期的历史快照记录任务的启用、禁用或已移除状态。禁用任务后仍会保留这次明确的状态变更和已在运行的任务结果。宿主未提供单独的移除时间戳，因此快照只记载已移除状态，不认定移除发生的时间。此过程不删除 OAM 历史，也不控制调度器。过滤仅适用于后续采集，已保存的来源和记忆继续保留。

每个周期任务的宿主运行状态（`ok`、`error` 或 `skipped`）会与任务 ID 和运行时间一并保存。任务 ID 将运行记录与定义关联起来，使任务改名后原有记录仍保持稳定。记录描述的是执行状态，下游投递需要另行确认。生成的报告、原始错误、投递诊断和工具输出均不保存。一次性任务不采集。插件重新核对任务时可以读取最近一次运行，无法重建停止期间错过的所有运行记录。

每次请求都使用由内容派生的稳定幂等键，并校验 OAM 的接收回执。宿主快照取消时，插件停止核对。插件没有本地队列或自动重试。采集和跳过的日志都标识任务及处理结果，不包含凭据或内容。接收回执只确认收件，索引和召回就绪需单独检查。启用采集不会授予定时任务会话访问个人召回或移除工具的权限，也不会删除已保存的报告。

## 召回与上下文上限

自动召回使用 `fast`。当调用 `memory_search` 时省略 `recall_level`，0.0.3 插件会发送 `fast`，npm 包 0.0.2 会发送 `normal`。0.0.3 插件接受 `fast`、`normal` 和 `deep`。Deep 需要 OAM 0.2.1 或其他支持 Deep 的服务版本。OAM 0.2.1 的 Fast、Normal 和 Deep 三档默认上下文上限分别为 8,000、16,000 和 24,000 token。

若 Agent 可以容纳默认 Deep 上限下的完整结果，将 `maxContextTokens` 设为至少 24,000。此宿主上限默认为 1,024，最大值为 32,768。插件用该值检查返回的完整 `context_tokens`，其中包括引用、元数据和原文。超过上限时，自动召回会省略结果，显式搜索则返回错误。宿主在检索结束后验证响应是否满足此上限。

## 响应与证据边界

插件读取响应流时，将解码后的 JSON 响应限制为 1,048,576 个 JavaScript 字符，字节数另设上限，为该值的四倍。响应过大、被拒绝或取消时，会关闭相应的读取器。自动召回和显式搜索共用不可信记忆包裹标记，召回证据中的 `&`、`<` 和 `>` 会先进行转义。这样可防止召回文本闭合该标记。语言模型仍可能受到证据中指令的影响。

## 移除记忆

用户明确要求移除时，调用 `memory_remove`，仅指定一个非空白的 `memory_id` 或 `insight_id`。通过对话或普通搜索确认确切 ID，目标不明确时先与用户核对。用户身份始终由插件配置决定。移除操作使用用户级密钥调用 `DELETE /v1/users/{user_id}/memories/{memory_id}` 或 `DELETE /v1/users/{user_id}/insights/{insight_id}`，需要 OAM 的 `source:delete` 权限。召回和采集继续使用 POST。通过校验的 `202` 回执确认该条目已从服务结果中排除。回执中的清理状态与操作 ID 用于追踪独立的存储清理操作。

如果最近一个已完成的纯文本用户轮次带有 `command: true` 或 `learning_eligible: false` 标记，是 `memory_remove(memory_id=mem_42)` 或 `memory_remove(insight_id="ins_7")` 之类的明确格式请求，或包含移除工具调用或结果（包括失败的移除尝试），采集时将标记为 `learning_eligible: false`。完整用户文本和时间戳会用同一个根据轮次生成的幂等键保存。自动采集排除助手和工具输出，后续普通消息仍可用于学习。Agent 运行失败或中止时不会触发采集。

当已知当前请求是控制操作或符合明确格式的移除请求时，插件会跳过自动召回。仍可用显式 `memory_search` 查找未知目标。只有当前消息文本与历史中最新的用户消息一致时，才会用那条消息的控制元数据决定是否召回。这能避免旧的控制轮次影响新的普通请求。即使省略了本地自动召回，仍需通过所有者准入检查和纯文本提示精确匹配检查。

未调用移除工具的候选项讨论和确认轮次需要宿主提供控制及学习资格元数据。插件不会根据任意自然语言推断移除意图。对于在 Agent 循环之外处理的命令，OpenClaw 的提示钩子契约并不保证提供这些元数据或轮次完成回调，请在已安装的宿主上验证相应路径。真实宿主上的意图分类、命令投递和 OAM 持久化需要单独验证，本地钩子和 HTTP 测试只覆盖本地路径。

## 验证

在本目录运行 `npm test` 和 `npm pack --dry-run`。要验证实际接入路径，请向所选 Agent 发送一条实际的已批准私聊消息，或经所有者授权的本地 WebChat 请求。检查 OAM 的保存回执和来源就绪状态，再启动新会话，确认带引用的上下文确实传给了模型。插件没有本地采集队列，如果 Gateway 在 OAM 接收保存请求前崩溃，这次自动采集可能丢失。
