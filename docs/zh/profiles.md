# 个人画像与自定义画像

[中文文档](README.md) · [English](../profiles.md)

## 概述

OAM 为每个用户保存一份个人画像，可以显式刷新，也可以配置自动刷新。独立的 API 可按特定任务生成自定义画像，例如工作画像，并直接返回结果。普通召回不调用生成模型，与画像流程分别运行。

## 1. 已保存的个人画像

`GET /v1/users/{user_id}/profile` 读取已保存并有来源支撑的画像。`POST /v1/users/{user_id}/profile/refresh` 接受可选指令和 `mode`，模式为 `incremental`（默认）或 `full`。输入未变化的增量请求返回 `200`，生成调用为零。已接收的处理返回 `202` 及操作 ID。跟踪操作，成功发布后再读取画像。

用 `GET` 或 `PATCH /v1/users/{user_id}/profile/policy` 读取或部分更新自动刷新设置。读取需要 `memory:read`，更新同时需要 `memory:read` 和 `memory:append`。响应包含 `policy`、`paused`、`pending_mode`、`last_success_at` 和 `last_full_at`。所有自动触发条件默认关闭。

| 设置 | 行为 |
| --- | --- |
| `mode` | 默认 `incremental`，也可用 `full`，用于普通自动刷新 |
| `refresh_cron` | 使用五字段 UTC cron 表达式。合格输入未变时复用已有内容 |
| `full_refresh_cron` | 独立的完整重建计划，与普通刷新模式无关 |
| `refresh_after_consolidation` | 所需索引和洞察完成后刷新变化的输入 |
| `min_refresh_interval_seconds` | 非负的自动刷新最短间隔。null 继承 `profile.min_refresh_interval_seconds`，默认 0 |

PATCH 未指定的字段保留当前值。将 cron 字段设为 null 即可清除该计划。`refresh_cron` 和 `refresh_after_consolidation` 互斥。单独启用其中一项会清除另一项，同时显式启用会被拒绝。完整重建计划可与任一个共存。例如每日 UTC 03:00 刷新，并在周日 UTC 04:00 完整重建。

```json
{
  "mode": "incremental",
  "refresh_cron": "0 3 * * *",
  "full_refresh_cron": "0 4 * * 0",
  "min_refresh_interval_seconds": 3600
}
```

完整计划以最近一次成功完整发布为准。如果最近一次完整重建后没有发生刷新，则跳过。两个计划同时到期时，一次完整重建同时满足二者。对于既有画像，若没有以往完整重建的时间记录，首个完整计划可以重建。

工作进程按 `profile.schedule_poll_seconds` 检查策略，默认 30 秒。每批最多发现 `profile.schedule_batch_size` 个用户，默认 100。计划时间意味着有资格开始。延后的触发可合并，完整模式优先。计时器不取消已经提出且运行中的显式任务。成功完整发布满足待处理的完整请求。策略变更时只要仍有触发项启用，待处理项便保留。全部关闭会清除未接收的待处理项，已接收操作继续运行。

自动刷新间隔以最近一次接收或成功发布为基准，显式刷新可跳过该间隔。画像终止失败会暂停自动刷新，成功显式刷新后恢复。一般策略更新不会清除暂停状态或未解决的服务商结果。仍有未就绪或失败输入时，状态会继续显示，自动生成等输入就绪后才开始。显式创建和刷新沿用有限的前置输入恢复流程。刷新失败时，只要旧画像的支持仍然合格，即可继续读取旧内容。关闭画像功能会停止策略扫描并拒绝画像 API 操作。

## 2. 临时生成自定义画像

`POST /v1/users/{user_id}/profile/generate` 需要 `memory:read`，请求体须包含非空的 `instruction`，最长 4,000 字符。同步成功响应为 `200` 并带有 `Cache-Control: no-store`。

```json
{"instruction":"Summarize the user's professional background, work preferences, and current projects."}
```

响应包含 `content`、`references`、`selection_coverage`（`selected`、`unexamined`、`budget_omitted`）、`usage`、`embedding_usage` 和 `generation_calls`。生成用量将服务商报告的推理 tokens 计入输出，向量用量单独报告。服务商未报告则为未知。模型适配器会校验生成的结束状态，响应不暴露 finish reason 字段。

完整指令直接用于选择合格的 L1 事实、L2 洞察和适用的精确画像资料。已有画像和文档证据资源来自独立流程。引用指向支持当前生成内容的事实、洞察及精确画像资料。正文使用数字引用标记时须与返回的引用对应。自定义 JSON 可以只使用单独的引用列表。覆盖字段描述证据选择情况。

OAM 不持久化自定义画像、指令、生成正文、操作结果或内容缓存。已保存的个人画像保持原样。记账可记录请求标识、状态、数量、耗时和用量。服务商侧保留由所用服务和运维策略决定。是否保存返回文本由调用方决定。

证据选择、生成和校验共用 `profile.attempt_timeout_seconds`，并使用既有画像输入与输出限制。远程调用前结束事务。输入缺失或未就绪时在生成前失败，调用方需先处理索引工作。返回前再次核对当前证据、索引代次、策略与凭据授权。服务商或输出失败返回 `502 ProfileGenerationUnavailable`，证据变化返回 `409 ProfileGenerationChanged`，超过模型或请求期限返回 `504 ProfileGenerationDeadlineExceeded`。异常不保存部分正文。调用方断开连接后，已发出的同步模型请求可能运行到限定超时，OAM 不排队继续处理或保存结果。

## 3. 部署

升级时先应用待处理迁移，包括从 v0.1.0 升级需要的 0038 和 0039，再启动新 API 和工作进程。`0039_profile_refresh_policy.sql` 增加用户级策略元数据并启用强制行级安全，通过仅队列角色可用的受限例程发现策略。多个工作进程共用数据库接收条件及现有用户锁来避免重复画像工作。调度随工作进程运行。

[API 参考](api.md) · [索引与检索](indexing-retrieval.md) · [策略接收](../../src/oam/profiles/scheduling.py) · [自定义生成](../../src/oam/profiles/generation.py) · [画像发布](../../src/oam/profiles/worker.py)
