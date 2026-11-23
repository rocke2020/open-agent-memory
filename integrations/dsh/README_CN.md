# 在 DeepSeek Harness 中使用 OAM

[中文文档](../../docs/zh/README.md) · [English](README.md)

## 概述

这个个人记忆插件使用 OAM HTTP API 为用户轮次召回带引用的上下文，并在轮次完成后保存用户原文。只在所配置 OAM 用户的所有者可访问的 DSH 配置档（profile）中使用。插件会排除子 Agent。请先启动 OAM，并确保它能够处理操作。

DSH 调度服务可用时，插件会观察有名称的周期任务，保存精简的任务定义、变更、移除事件和最近一次已确认的投递。提示词、生成结果和一次性任务均不保存。投递表示调度器已将消息持久化到会话。任务是否完成需要另行确认。任务目录没有所有者或维护任务分类，因此以 DSH 配置档作为所有权边界。`DSH_HOME`（默认 `~/.dsh`）下的私有文件保存每个任务当前观察到的定义和时间戳，使重启前后未变的快照复用其幂等键。目录只提供最近一次投递回执，插件离线期间错过的中间投递无法恢复。采集失败会写入日志，不加入重试队列。

在 DSH 进程环境中设置 `OAM_DSH_URL`、`OAM_DSH_USER_ID` 和 `OAM_DSH_API_KEY`。请使用 HTTPS 源地址或环回 HTTP 源地址，以及用户级密钥。通过宿主的插件管理器将本包安装到目标 DSH 配置档，然后重启或重新加载该配置档。用户明确要求移除记忆时，模型可以调用 `memory_remove`。

## 召回与上下文上限

自动召回使用 `fast`。当调用 `memory_search` 时省略 `recall_level`，0.0.3 插件会发送 `fast`，npm 包 0.0.2 会发送 `normal`。0.0.3 插件接受 `fast`、`normal` 和 `deep`。Deep 需要 OAM 0.2.1 或其他支持 Deep 的服务版本。OAM 0.2.1 的 Fast、Normal 和 Deep 三档默认上下文上限分别为 8,000、16,000 和 24,000 token。

若宿主可以容纳默认 Deep 上限下的完整响应，将 `maxContextTokens` 设为至少 24,000。服务端返回的完整上下文必须在插件的 `maxContextTokens`（默认 18,000）范围内。超限时，自动召回会记录警告并省略结果，显式搜索会返回错误。插件不会截断上下文。

## 验证

运行 `npm test` 验证本地 HTTP 交互。实际集成还需完成一次独立的用户轮次，核实 OAM 操作已就绪，并在新的 DSH 会话中验证带引用的召回结果。

## 移除记忆

用户明确要求移除时，调用 `memory_remove` 并仅指定 `memory_id` 或 `insight_id` 之一，例如 `{"memory_id":"mem_42"}` 或 `{"insight_id":"ins_7"}`。从对话或 `memory_search` 中确认确切 ID。搜索会把可移除条目的确切 ID 附在其上下文引用编号旁。请求移除时必须使用条目 ID，引用编号只能用来定位条目。目标不明确时先与用户核对。工具始终使用配置中的用户和用户级密钥，需要 OAM 的 `source:delete` 权限，且不向子 Agent 开放。工具发送 `DELETE /v1/users/{user_id}/memories/{memory_id}` 或 `DELETE /v1/users/{user_id}/insights/{insight_id}`。通过校验的 `202` 回执确认该条目已从服务结果中排除。回执中的清理状态与操作 ID 用于追踪独立的存储清理操作。无效请求、请求失败及无效回执都会以工具错误返回。

在已完成的轮次中，如果存在宿主原生的 `memory_remove` 工具调用，或调用了本插件的移除工具，即使移除失败，插件也会保存原始用户文本和时间戳，并标记 `learning_eligible: false`。自动采集排除助手和工具输出，后续普通轮次仍可用于学习。对于没有调用移除工具的候选项讨论和确认轮次，插件不做分类，也不会根据任意自然语言文本推断移除意图。

## 通过发布归档安装

若要搭配 OAM 0.2.1 使用 Fast、Normal 和 Deep，请安装 [OAM v0.2.1 发布页](https://github.com/rocke2020/open-agent-memory/releases/tag/v0.2.1)上的 DSH 插件 0.0.3。从该发布页下载 `rocke-dong-oam-dsh-0.0.3.tgz` 和 `SHA256SUMS`。先在下载目录校验归档，再执行安装。DSH 可能仍需联网获取配置档的依赖。

```bash
grep '  rocke-dong-oam-dsh-0\.0\.3\.tgz$' SHA256SUMS | shasum -a 256 -c &&
dsh plugin --profile headless add /absolute/path/rocke-dong-oam-dsh-0.0.3.tgz
```

选择你要使用的仅所有者可访问的配置档，将上述三个环境变量提供给该 DSH 进程，并重启此配置档。请保留归档文件，因为配置档的包管理器可能会引用其本地路径。使用 `dsh plugin --profile headless remove @rocke-dong/oam-dsh` 可移除该包。包内含插件入口和配置档补丁，OAM 需另行安装和启动。

## 通过 npm 安装

公开 npm 包为 0.0.2，支持 Fast 和 Normal，并可按 ID 移除记忆和洞察。显式搜索省略模式时使用 Normal。请将它安装到仅所有者可访问的 DSH 配置档。

```bash
dsh plugin --profile headless add @rocke-dong/oam-dsh@0.0.2
```

安装公开包无需登录 npm。设置上述 OAM 环境变量，然后重启或重新加载该配置档。如果已经安装过本地的旧版 `@rocke2020/oam-dsh`，请先通过同一配置档的插件管理器将其移除，再安装更名后的包。
