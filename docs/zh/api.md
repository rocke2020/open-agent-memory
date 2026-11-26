# Open Agent Memory HTTP API

[中文文档](README.md) · [English](../api.md)

## 概述

HTTP/OpenAPI 是 OAM 的核心接入契约。MCP 和宿主插件是可选客户端。服务实现了 `dev`、`api_key`、`trusted` 三种认证模式。在 `api_key` 模式下，用户密钥绑定租户和 `user_id`，管理员密钥用于管理。保存接口接收会话，资源集合接收文档，画像资料集合接收结构化用户信息。普通召回返回带引用的上下文，答案由调用方生成。本参考适用于 v0.2.1。HTTP 请求省略 `recall_level` 时采用 `search.default_recall_level`，默认使用 Fast。对应验证范围见[发布记录（英文）](../releases/0.2.1.md)。

## 1. 全局路由

以下路由提供服务状态、能力和界面资源。

- `GET /health/live` 报告 API 进程存活。
- `GET /health/ready` 执行有界的数据库和模式检查、已启用角色的模型配置检查、模型容量接收检查以及适用的内嵌工作进程检查。通过返回 `200`，失败返回 `503` 及脱敏原因。该检查不调用模型。
- `GET /v1/capabilities` 返回契约、配置和当前索引方案标识，以及实现的功能和有效限制。在 `limits.search.default_recall_level` 和 `limits.search` 中的各档预设查看生效设置。`features.deep_recall` 标明支持 Deep。实际访问能力通过授权的数据操作验证。
- `GET /ui/` 提供同源 Inspector 客户端。网页资源没有内嵌用户内容或持久化凭据。

## 2. 管理

认证模式由运维配置 `auth.mode` 选择。省略且存在活跃管理员密钥时采用 `api_key`。新建的无管理员密钥数据库采用仅环回监听的 `dev`，并保存该选择供重启使用。管理员初始化未完成，或已有密钥模式数据库缺少活跃管理员时，启动失败。`trusted` 必须显式选择。`dev` 没有认证及个人隐私隔离，且禁用密钥管理路由。`trusted` 本地数据请求提供 `X-OAM-Tenant` 和 `X-OAM-User`。非本地请求还须提供 `Authorization: Bearer <root-key>`。断言用户必须匹配路径或请求体的 `user_id`，租户必须已存在且活跃。非本地网关须认证调用方、替换客户端提交的身份请求头，并阻止直接访问后端。仅携带有效管理员密钥的请求可用 `X-OAM-Role` 声明 `user` 或 `admin`。管理路由仍验证真实管理员凭据，断言不能声明 `root`。

`POST /v1/users` 接收租户管理员的 `{"user_id":"lin"}`，原子创建个人集合、绑定用户主体、权限和首个用户密钥。提交后返回 `user_id`、`credential_id` 及仅返回一次的密钥。管理员本身不获该用户授权，旧管理员授权记录也不会通过当前 SQL 权限函数取得个人内容。用户密钥只可保存、召回和查看对应用户的操作。

`GET /v1/users` 列出当前管理员租户中的活跃用户 ID 及最近接收输入时间。可用 `limit`（默认 100，最大 10,000）、从 0 开始的 `offset`（默认 0）、包含边界的 `last_write_at_gte`（带明确 UTC 偏移的 RFC 3339 时间）。先筛选和精确计数，再分页。响应含 `items`、`total`、`limit`、`offset`。每项含 `user_id` 和可为 null 的 `last_write_at`。接收新会话、证据资源或画像资料版本（包括修订）会推进时间，幂等重放、读取、生成画像和后台索引不推进。历史值从已接收来源版本的回执时间回填。同步画像资料的应用应把自己的写入也算入扫描水位。

结果按 `user_id` 字节升序。无已接收输入的用户时间为 null，仅在无时间过滤时匹配。页面与总数共用数据库快照，即使偏移返回空页。不同请求各自读取当前状态，写入可能造成跨页重复或遗漏。`offset` 为非负 PostgreSQL 有符号 64 位整数，最大 9,223,372,036,854,775,807。有效用户凭据返回 `403`，缺失或无效管理员凭据返回 `401`。`trusted` 也需管理员密钥，`dev` 返回 `503`。无效时间或分页返回 `422`，采用 OpenAPI `HTTPValidationError`，`detail` 数组的 `loc` 标识查询参数，另有 `msg` 与 `type`。时间字段标记 `format: date-time`，须携带显式 UTC 偏移。

凭据管理保存 SHA-256 摘要和不可变的租户及主体绑定。管理员可以创建追加的用户密钥并以该用户密钥认证。管理请求本身都直接在 PostgreSQL 验证当前凭据。

| 路由 | 契约 |
| --- | --- |
| `POST /v1/credentials` | 签发时提供同一租户的 `principal_id` 或 `user_id`，恰选一个，可附带有时区的 `expires_at`。使用 `user_id` 可为现有用户追加密钥。提交后只返回一次密钥 |
| `GET /v1/credentials` | 接受 `after` 和正整数 `limit`，上限为 `auth.credential_page_size`。返回凭据元数据和 `next_after` |
| `DELETE /v1/credentials/{credential_id}` | 幂等撤销，返回 `revocation_effective_by`，提交后清除本地缓存。不存在和外租户 ID 返回相同结果 |

## 3. 保存与召回

在 `api_key` 模式下，个人请求使用 `Authorization: Bearer <user-key>`。`trusted` 使用租户及用户断言，非本地监听还要求管理员密钥。`dev` 在环回地址无需密钥。保存需要 `Idempotency-Key` 唯一标识一次逻辑请求，使用同一键及完全相同的输入重试可复用回执。

`dev` 和 `trusted` 新断言的用户具有与新建密钥用户相同的资源、来源控制、调试及导出权限。迁移 0012 为新断言用户提供这些权限。0013 仅升级权限完全匹配旧默认的已有断言用户，保留密钥用户和自定义授权。0014 限制注册函数的所有者和执行权限。运维确认既有用户用途后可补足其权限。

写入先解析模式对应的身份，再读取请求体。`api.max_request_bytes` 限制完整编码体，包含 JSON 转义或 multipart 头与元数据，独立于会话文本及资源原件上限。规范默认值为 128 MiB，以能力响应报告的有效值为准。即使无 `Content-Length` 也计入实际字节，超限在进一步缓冲或解析前返回 `413 RequestTooLarge`，并关闭未完成的 multipart 文件。读取有界请求体确认用户和输入类型后，检查用户绑定、权限和输入内容限制，再接收工作或调用模型。

以下简写的个人子路径均相对于 `/v1/users/{user_id}`。

| 路由 | 契约 |
| --- | --- |
| `POST /v1/retain` | 接收 JSON 会话，返回 `202`、持久操作及服务端分配的来源 |
| `POST /resources` | 接收文本文档、HTTPS URL 或 multipart 原件，要求 `Idempotency-Key`，返回持久 `202` 来源回执 |
| `POST /profile-data` | 接收已注册的 `profile_data_id`、`subtype`、`content`，要求 `Idempotency-Key`，返回 `202` |
| `POST /v1/recall` | 接收 `user_id` 和 `query`，使用显式或配置的预设召回，不生成最终回答 |
| `GET /profile` | 读取存储的用户画像正文、引用、发布时间和处理元数据 |
| `POST /profile/refresh` | 接受 `mode` 与可选 `instruction` 或 `reset_instruction`，返回 `200` 并零调用复用，或返回 `202` 及持久刷新操作 |
| `POST /search-many` | 接收 `queries`，按一个模式共享绝对期限并分配上下文总量，按输入顺序返回每个查询结果 |
| `POST /decompose-and-search` | 接收一个查询。使用一次配置的规划器调用产生有界子查询，或说明回退至原始查询，之后运行相同批量路径 |
| `GET /resources` 或 `/resources/{resource_id}` | 仅列出或检查文档资源、当前版本、段落、摘要状态及操作状态 |
| `GET /profile-data` 或 `/profile-data/{profile_data_id}` | 仅列出或检查结构化画像资料、当前版本和精确映射字段 |
| `GET /resources/{resource_id}/original` 或 `/profile-data/{profile_data_id}/original` | 下载相应集合中活跃版本的原件。原始 HTML 不作为网页执行 |
| `GET /evidence/{source_id}` | 依当前用户策略查看活跃会话原始请求及引用的 L0 段落 |
| `PUT /resources/{resource_id}` 或 `/profile-data/{profile_data_id}` | 使用 `Idempotency-Key` 和正整数 `If-Match` 创建不可变修订版，返回 `202` |
| `DELETE /memories/{memory_id}` 或 `/insights/{insight_id}` | 依据 `source:delete` 精确移除一个带类型的学习记录，提交排除状态并返回 `202`。重复调用返回最初回执 |
| `DELETE /resources/{resource_id}`、`/profile-data/{profile_data_id}` 或 `/evidence/{source_id}` | 立即排除该来源的所有版本，返回持久清理回执。集合与 ID 类型不匹配返回 `404` |
| `GET /memories` | 列出当前 L1 或 L2，可按 `memory_type=basic` 或 `memory_type=insight` 筛选，使用 `limit`、`offset` 或 `after` 分页 |
| `GET /memories/{memory_id}` | 读取一个合格 L1 或当前 L2 版本，要求 `memory_type=basic` 或 `memory_type=insight` |
| `GET /operations/{operation_id}` | 按相同用户权限查看已接收任务、就绪、失败及用量 |
| `POST /operations/{operation_id}/{action}` | 在操作权限和策略检查下使用 `cancel`、`retry` 或 `reconcile-attempt` |
| `POST /operations/{operation_id}/recover` | 使用 `source:delete` 恢复原移除操作中失败的清理或修复，保留原回执、排除状态和依赖 |

重试为失败或取消工作创建关联回执，复用兼容的完成检查点。以同一重试幂等键可查回执。外部尝试结果未核对时返回 `409 RetryRequiresReconciliation`，先凭已验证的结果执行对账。重试无需重新保存已接收来源。

### 会话

宿主向 `/v1/retain` 提交每条消息的角色、原始正文、有时区的 `event_time` 和显式的 `learning_eligible`。OAM 分配会话来源标识，并以消息顺序定位。基于证据文档生成的助手回答可以保存，但须声明为不可学习。

```json
{
  "user_id": "lin",
  "input_type": "interaction",
  "messages": [
    {"role": "user", "content": "Please keep my checkup summaries concise.", "event_time": "2026-09-23T10:00:00+08:00", "learning_eligible": true},
    {"role": "assistant", "content": "The report lists a September checkup.", "event_time": "2026-09-23T10:00:02+08:00", "learning_eligible": false}
  ]
}
```

可选 `temporal_annotations` 包含四个语义字段，分别是 `role`（`event` 或 `date_reference`）、有时区的 `start` 和 `end`、以及原样 `quote`。引用须在所属消息中只出现一次。有重复日期时可提供更长引用。相等边界表示有证据的时间点，分钟、小时、天或其他时间段使用递增边界。精度、不确定性和时长措辞保留于事实和引文。时间点保存为 `[]`，时间段保存为 `[)`，服务确定来源，并保留来源或请求的 `reference_timezone`。各注释没有额外的瞬时、粒度、含义、开闭边界、来源、参考时区或字符坐标字段。资源注释另外指定解析后的 `locator`，引用在该块内须唯一。段落引用标识来源、版本、消息和段落序号，事实引用另有规范段落 ID 和精确原文。检查、导出、洞察和画像使用同一证据标识。

### 文本或文件文档

向 `/v1/users/lin/resources` 提交文档时，在该租户用户下使用稳定的 `document_id`，并指定 `update_mode`。文本 `replace` 提供完整正文，`append` 提供现有文本文档的新增部分。服务逐字拼接并原子分配下一不可变版本，所需空白和分隔符须包含在追加文本中。

```json
{
    "input_type": "document",
  "document_id": "checkup-2026",
  "update_mode": "replace",
  "text": "Checkup date: 2026-09-20."
}
```

文件以 `multipart/form-data` 提交到相同路由。`metadata` 部分包含 `{"input_type":"document","document_id":"scan-2026","update_mode":"replace"}`，`original` 部分包含原始文件、文件名和媒体类型。OAM 校验大小、类型并计算长度与摘要。HTML、PDF、图片需替换整个文件或另建文档 ID。

HTTPS 证据可用同一端点及 JSON `{"input_type":"document_url","document_id":"scan-2026","update_mode":"replace","media_type":"application/pdf","url":"https://reports.example.org/scan.pdf"}`。声明的媒体类型可为 `text/html`、`application/pdf`、`image/png` 或 `image/jpeg`。用户授权后只执行一次有界抓取，拒绝非公共目的地、重定向和与声明不符的字节。索引前提交取回的确切原件。已提交的相同幂等请求复用回执，抓取失败不创建已接收来源。只处理所给链接，无自动刷新。抓取可使用 URL 查询参数，引用和公开错误会省略参数。

保存格式由 OAM 分配 `source_id`、`message_id`、`source_version`，也不接收调用方填入的 `expected_active_version`、`byte_length` 或 `sha256`。接收新文档版本时立即排除旧派生内容，新索引可随后完成。比较并修改使用 `PUT /resources/{resource_id}` 及同形状的 JSON 或 multipart，匹配 `user_id` 和 `document_id`，设置 `update_mode: "replace"`、`Idempotency-Key` 和 `If-Match: <active-version>`。读文件或抓取前先核对当前版本，接收事务中再原子检查。相同键的精确重试复用回执。文档始终属于证据资源。

### 召回

最小请求为 `{"user_id":"lin","query":"How should I summarize the checkup?"}`。请求省略 `recall_level` 时使用 `search.default_recall_level`，默认 `fast`，可设为 `fast`、`normal` 或 `deep`。显式请求模式优先。该规则适用于直接、批量和规划召回。Fast、Normal 和 Deep 各有一个完整渲染上下文上限，默认分别为 8,000、16,000 和 24,000 tokens，由相应 `search.<mode>.context_max_tokens` 控制。预算统计学习正文、完整原文、引用、别名、日期、标签和分隔符，不含外层 JSON。单次可用 `overrides.context_max_tokens` 覆盖。

省略 `include`，发送 `"include": {}` 或 `"include": {"chunks": true}`，都会包含合格原文。`"include": {"chunks": false}` 排除所有完整正文项和正文块，包括洞察的支持原文，同时保留合格事实、洞察、必需事实和真实引用。已保存的支持和别名引用仍保留，即使短原文与引用完全相同。两种选项均校验来源及支持。`include: null`、非布尔 `chunks`、旧对象式 chunks 以及移除的 `max_tokens`、`fact_max_tokens`、`max_context_tokens` 都会被拒绝。`allow_partial` 默认 false，`include_profile` 不受支持。个性化应用可另行读取 `GET /v1/users/{user_id}/profile`，并自行合并输入预算。

`context` 用 `[n]` 标识第 n 个结构化项，用 `S<n>` 标识来源，另外显示版本。完整 ID、精确位置、引用、图见证和时间证据保留于 `items`。事实在核对来源、版本、消息及引用后可引用已选原始段落。完整且非空的时间证据相同才写 `time=source`。等价 UTC 时间可显示为 `Z`。已有格式对照支持完整 ID 形式。实时直接和批量响应报告 `tokenizer: "o200k_base"`，`context_tokens` 统计完整文本，包括每次显示的引用及元数据。批量总值为各子上下文计数之和。回答模型的确切 token 由调用方统计。记录重放沿用原跟踪的分词器和计数规则。

合格事实、洞察和独立原文依最终相关性顺序竞争。即使未抽取出事实、事实无关或完整学习组超限，原文仍可独立入选。学习条目与必需的完整支持一起接收，包括支持事实、引用及启用时的完整正文。共同条目只返回和统计一次。每次尝试加入后的完整渲染都须符合总量，放不下的组跳过，后续较小的组仍可参与。单条 L1 事实也受限制。`max_results` 分别限制学习条目和原文全文，必需事实占学习槽位，禁用全文时不占原文槽位。

三档共用 `search.fact_source_bodies`，可单次覆盖。默认 `required` 在启用 chunks 时纳入已接收事实的支持原文。实验性的 `ranked` 保留完整事实、真实引文、来源及时间，而将完整原文作为合格独立候选参与竞争。引用来源扩展、仅用于来源详情和附属可选邻居不强制带入全文，因此可能省略有用的未引用文字。洞察组和证据资源保留必需表示。所有策略均执行来源校验、基于完整支持去重和预算检查。`diagnostics.context_budget.fact_source_bodies` 报告生效的排名策略。

实验性的 `ranked_deduplicated` 在 ranked 基础上，只有来源和唯一引文校验通过、完整时间证据相同、且事实无检索路径时，用已选择的精确完整原文替换普通事实。原文保留其来源组排序位置及完整文字。不匹配、节选或截断的原文不替换，具有不同时间证据或检索路径的事实及必需洞察支持均保留。槽位在替换前计数，token 计数和最终重建使用投影结果。`diagnostics.context_budget.source_deduplicated_fact_ids` 列出被替换事实。实验性的 `assistant_deduplicated` 在常规 required 接收、生命周期检查和 token 裁减后，只移除有完全一致原文和时间、无检索路径且未截断的助手支持事实，保留所有原文、用户或混合或未知支持的事实和必需洞察，保持顺序，不增加替代候选，若 token 增长则回退完整渲染。`diagnostics.context_budget.assistant_deduplicated_fact_ids` 列出移除项。这两种策略仅在包含正文时适用。

三档共用 `search.lexical_ranking`。默认 `whole_query` 按 OR 全文表达式排名一份候选列表，`per_term` 分别排序再融合。共用的 `search.partial_match_gate` 默认为 `user_fact_exchange`，可设 `"off"`。直接向量、严格全文或主词项、字面锚点和配置的图候选保持独立资格。词法通道短名单中的用户原文直接参与。该短名单中取回正文的首个合格用户或助手原文也可独立参与，包括助手先开口的会话。首个为用户原文时同样占用这项例外。较弱助手原文仍需独立或局部证据。例外不产生图见证或来源扩展权。

其他会话事实或原文需要一个直接向量命中的用户事实，在同一来源版本中准确引用目标用户消息或紧随其后的助手回复。间接助手原文扩展还要求该用户事实和确切用户原文都位于原始向量结果的前 `source_seed_results` 条。普通来源发现另按融合与多样性顺序使用同一限制。开启筛选时，补充发现限于有独立用户事实命中的确切来源版本。全局种子上限选出最接近查询的用户原文，其内容作为附加上下文排在最相关用户事实锚点及其必需支持之后。锚点不入选时保留其稍后位置；该原文不能作为向量见证、独立见证、助手回复或递归发现依据。筛选不改变必需支持，也不筛除证据资源或洞察。允许部分召回且向量不可用时，保留词法召回并在诊断报告跳过筛选。

`search.adjacent_user_context` 默认开启。启用 chunks 时，入选普通事实可附加已发现的前一或后一用户段落，限 `search.adjacent_user_context_max_chars` Unicode 字符，默认每段 1,000。使用前段末尾或后段开头。优先按 Unicode 句末标点及希腊问号保留整句，无完整句才尝试逗号、分号和冒号等分句标点。小数分隔符、紧凑首字母和常见缩写点不结束句子，带上紧随的闭引号。丢弃段落边缘无法确认的片段，两种边界都找不到合适内容则省略可选邻居。标点规则无法识别所有语法情形。取到完整原文时才表示完整。`adjacent_user_context_max_tokens` 已由字符设置取代，需手动改值。节选保留来源并附 `excerpt: {start, end, original_length}`，零基字符坐标且右端不包含，渲染标题也标明范围。它属于可选上下文，引用的必需支持仍保持完整。有独立资格的完整原文或支持组能放入时才可升级节选，仅依靠来源窗口回退无法升级。所有渲染一起计入预算，放不下邻居时仍可接收事实。时间搜索沿用这些规则。

三档复用在完整查询中出现且有当前来源支持的实体名和别名，包括普通 64 词项截断后的名称。完整规范化名称匹配提供候选提示，仍保留同名歧义。名称词项在有限词项列表内获得位置，匹配事实使用 `search.query_entity_boost` 取得有限加分，默认 0.5（50%），范围 0%–100%。严格全文与主词项优先，名称不能单独压过正相关词法命中。`per_term` 同时调整词项列表顺序和既有融合分，无额外通道票。实体提示选中的事实可独立通过部分匹配筛选，保留严格匹配状态和必需引用。编号和引号查询沿用字面路径。诊断 `local_context.query_entity_boost` 和 `query_entity_matches` 报告值及词法命中事实数。设为 0 跳过实体查找。配置仅影响召回，不需再索引或生成。

个人偏好和经历可用 `"include": {"chunks": true}` 与正数 `overrides.personal_evidence_candidates` 启用，检索引用于当前 L1 偏好或经历的合格用户原文。默认 0，受所选模式的 `channel_candidates`、召回期限和上下文预算限制。[个人证据设置和示例](../../configs/README_CN.md#8-个人证据候选)

时间窗口没有直接命中和合格时间种子时，可先在窗口内寻找有相关性的合格事实，再跟随时间关联，保留时间依据和现有候选、截止和预算限制。

直接、批量、规划请求接受 `fast`、`normal`、`deep`，未知值（包括 `mid`）返回 `422`。Fast 默认禁用实体图，Normal 和 Deep 默认 `light`。Deep 与 Normal 共享词法归一化、图权重和可选重排，拥有自己的数字限制。直接请求默认期限 Fast 3 秒、Normal 和 Deep 5 秒。较早的调用方期限优先，`deadline_at` 必须带时区。三档复用已有索引代次并可召回 L2。

每档可选 `entity_semantic_graph: "off"`、`light` 或 `full`，依次禁用实体与语义扩展、按共享实体 ID（含有证据的别名）扩展，或再沿已存语义链接扩展。独立的 `search.causal_graph` 默认 false，启用后普通和时间检索沿有界的出向结果到原因链接遍历。关闭时日期过滤及时间关联仍可用。将实体选项设为 `"off"`（YAML 中加引号）且关闭因果可禁用普通图扩展。配置拒绝非法值与移除的全局 `search.fast_graph`。Fast 请求的旧 `fast_graph` 严格布尔为 true 时选 light，为 false 时选 off，省略或 null 继承设置。Normal 和 Deep 的 true 返回 422，false 不改变其配置。任一模式均可用 `overrides.causal_graph`。诊断及日志报告 `graph`、`causal_graph`、执行通道和降级。禁用分支在 SQL 中跳过。实体、语义和因果共用一张图投票。查询措辞不修改开关。

可选 `overrides` 覆盖选中预设字段和共享的 `lexical_ranking`、`partial_match_gate`、`adjacent_user_context`、`adjacent_user_context_max_chars`、`causal_graph`、`fact_source_bodies`。省略字段继承配置，支持可空的字段可显式清空。例如 `{"user_id":"alice","query":"What happened last month?","recall_level":"fast","overrides":{"temporal_nodes":300}}` 将 Fast 遍历调至 300 节点并沿用 `max_results: 88`。直接和批量及规划的各子查询继承覆盖，覆盖后的上下文总额在子查询间分配。未知字段、非法值和合并后的非法组合在检索前返回 422。模型、凭据、超时及其他共享设置由运维控制。Fast 的非 null `fast_graph` 优先于 `overrides.entity_semantic_graph`。`diagnostics.search_config` 记录有效预设和共享覆盖，子查询最终上限取其预算份额。[配置参考](../../configs/README_CN.md#7-时间遍历预算与请求覆盖)

Normal 和 Deep 可用 `models.reranker` 根据原始查询为已加载正文的候选评分。各自 `rerank_candidates` 默认为 48，可单次覆盖，null 选择完整的有界融合列表。允许部分召回时，可降级保留融合相关性并在 `diagnostics.degraded` 标记 `rerank`。Fast 使用融合相关性。[重排设置](../../configs/README_CN.md#6-来源正文与重排)

配对的可选 `time_basis` 与 `time_window` 显式覆盖查询日期解析。`time_basis` 支持 `event`、`referenced`、`mentioned`、`created`、`updated`，窗口 `start` 与 `end` 必须有时区且开始早于结束。半开窗口只改变时间排序，其他通道和最终接收可使用窗口外证据。若没有显式窗口，使用本次捕获的 UTC 参考时刻，或提供带时区的 `reference_time` 和 IANA `reference_timezone`。受支持的英语、中文和部分日语日期可在任一模式启用时间传播。两个绝对日历端点经 `to`、`from … till/until …`、`between … and`、范围标点、到、至或 から … まで 连接时，半开区间包含最后的日、月或年，本地边界遵循夏令时。独立日期、反转端点、紧邻的 `compare`、`compared` 或 `comparing A to B` 及钟点或相对时间范围视为有歧义。裸 `A to B` 可作为范围，解析器不分类更广的自然语言比较意图。歧义、无效和不支持的隐式日期保留普通召回并报告诊断，非法显式窗口拒绝。权限来自用户绑定，无独立租户或集合选择器。

批量将同一模式和时间选项用于每条调用方查询，无显式窗口时按共享冻结时刻分别解析。开始前校验 `search.max_queries` 和每条查询 token 上限。一个模式总额平均分配，余数分给前几条，未用预算不再分配。两条查询时 Fast、Normal、Deep 每条分别获得 4,000、8,000、12,000 tokens。每条继承 `include.chunks`，独立保留 `items`、完整 `context`、诊断和完整计数，只入选可放入份额的组。结果报告 `ok` 或 `partial`、按序索引、合计 `context_tokens` 以及各失败码。全部失败则请求不可用。期限到后运行中的子查询标为超时，排队查询取消，已运行的只读查询可在自身超时内完成。能力接口提供并发和共享期限。批量编排用 `request.batch_deadline_seconds`。

规划搜索要求可选规划模型已启用。保留完整原始查询为首项，通过至少 1,024 输出 tokens 生成互补证据查询。原始与去重后的扩展共用 `search.max_queries`。只允许一项时直接搜索原始查询，规划状态 `skipped`，调用为零。统一参考时刻及原查询显式或可无歧义推断的窗口适用于所有子项。响应报告已执行计划、实际调用数、模型用量及回退原因。价格无权威绑定，`cost` 为 null，服务商未报的用量为未知。模型失败或计划无效时，在剩余时间内使用原查询，不重试规划。编排经 `request.delegated_deadline_seconds` 限制，子项保留所选模式自身期限，更短的调用方期限优先。未完成规划请求可能在超时返回后继续排空，期限后不会派发子查询。规划角色未配置时能力显示禁用，路由返回能力不可用。子查询仍执行普通召回。

### 画像资料与刷新

画像读响应的 `default_prompt` 来自服务器内置[画像提示词](../../src/oam/profiles/prompt.txt)，无画像或因预览预算省略正文时也可获取。已保存的自定义 `instruction` 补充该提示词。Inspector 使用完整服务预览限额直接打开已保存画像，普通刷新复用已保存指令。自定义面板提供指令输入、应用并刷新、恢复默认，以及可展开的只读默认提示词。

`POST /v1/users/lin/profile-data` 接受 `{"profile_data_id":"basic","subtype":"basic-profile/v1","content":{"date_of_birth":"1990-05-10","interests":["hiking"]}}`，或已注册的 `preferences/v1` 资料及 `example_language`、`domain`。要求 `resource:write` 和 `Idempotency-Key`。修订使用带 `If-Match` 的 `PUT /profile-data/basic`。已知字段确定性地映射至画像，保持在普通召回之外。非法映射在激活前拒绝。保存完整接收的 JSON，活跃新版替代旧精确值。资料和文档通过各自路由管理，无 `resource_type` 选择器。各自集合保留可读的原件。

`GET /v1/users/{user_id}/profile` 返回存储的 `content` 字符串、独立 `references`、可选 `instruction`、最近发布的 `updated_at`、机器可读 `freshness` 与处理计数。合格学习条目未经检查或放不进输入时计入 `uncovered_inputs`。`selection_coverage` 分别报告发布版本已选、未检查和因预算省略的数量，旧版本可为 null。旧字段式版本仅在所有证据仍合格且有当前引用时，在读取时渲染为同样文档格式，不调用生成模型或写库，并保留原输入身份及发布时间。无可读版本时 `content: null`。可选非负 `max_tokens` 受 `profile.max_response_tokens` 限制，默认 18,000。超过预览预算时省略整个文档。默认 Markdown 包括 `## Main content` 与 `## References`，使用选中的 L1、L2 和精确资料值，L0 验证引文，衍生洞察与其事实共用证据。合格 L1 变化影响生成输入身份，可读的已有版本继续使用直到刷新替代。画像概括当前长期状态，使用已解决纠正后的最终证据，略去临时测试标识；未知性别省略，陈述的既往年龄保留来源日期，生成用 “the user”。

`POST /v1/users/{user_id}/profile/refresh` 可提供有限长度的自定义指令，包括 JSON 展现，或以 `reset_instruction:true` 恢复默认。省略则用已保存指令。提示词、输入预算和指令参与输入身份，之后不同指令或重置会取消冲突中的画像任务。除非要求完整重建，合格输入及指令不变返回 `200` 并零生成调用；变化返回 `202`。启用洞察时会推进待处理洞察，仅 L1 时无需等待洞察。刷新关联现有索引及洞察任务，并通过既有约束对每个失败原操作最多重试一次。工作进程继续等待初始索引后新增的洞察依赖，未就绪不会占用生成尝试。兼容检查点可复用。自动重试耗尽、需要重新规划或外部结果需核对时返回 `409 ProfileInputsNotReady`。

默认 Markdown 引用为生成支持列表的位置，重复 ID 去重并重新对应编号，未支持 ID 和越界编号被拒绝。事实或洞察只有省去原段落才能放入预算时，可保留来源标识和日期，而不把段落正文送给模型。没有可容纳的支持或精确字段时提前失败。发布前校验引用支持及来源。还持有尝试所有权时记录已完成向量调用与失败生成用量。引用来源撤回或被替代后旧自由文档整体停止返回，直到重建。生成失败仍保留有资格的旧版本。外部结果不明时报 `409 ProfileRefreshUnresolved`，直至核对。仅写入证据资源不改画像输入身份。

## 4. 来源控制、调试和导出

### 读取各层记忆

用 `GET /v1/users/{user_id}/memories` 直接读取 L1/L2，用 `GET /v1/users/{user_id}/profile` 读取 L3。三者要求认证租户和用户范围内的 `memory:read`，均不调用模型，只返回当前合格内容。资源和画像资料属各自集合。L3 是一份存储文档。画像缺失、支持失效或因预算省略时，`content: null`。检查 `freshness` 与 `omitted`，读取不会刷新画像。

洞察列表项和详情包含非空 `title`，最多 120 字符，完整正文仍在 `text`。标题表示长期主题，通常不会随事件、状态或支持变化而变动，证据改变主题时可修订。可移植导出包含同一标题。旧记录直接使用主题标签或有界预览。

`POST /v1/users/{user_id}/profile/refresh` 的 `instruction` 最多 4,000 字符，例如 `{"instruction":"Return a JSON profile emphasizing ongoing projects and communication preferences."}`。它补充内置系统提示，接口没有 `system_prompt` 字段。刷新同时要求 `memory:append` 和 `memory:read`，可异步生成画像。`GET /profile` 同时返回已有的 `instruction`、内置 `default_prompt` 和正文。`{}` 复用现有指令，`{"reset_instruction":true}` 恢复默认。显式指令和 true 重置不能同时提交。

[画像调度和自定义生成](profiles.md)说明 `GET`、`PATCH /v1/users/{user_id}/profile/policy` 及同步 `POST /v1/users/{user_id}/profile/generate`。更新策略需要读和追加权限。自定义生成要求读取权限，返回带引用的结果和 `Cache-Control: no-store`，无需持久化自定义画像或输出正文。

刷新 `mode` 默认 `incremental`，也可选 `full`。`full` 总会安排重建。增量刷新保留当前受支持的主张，应用新增或变化的合格事实和洞察，并删除支持被撤回的主张。此前完整重建省略且未变化的输入仍显示为未覆盖，需再次完整重建才能重新考虑。待处理增量输入可在后续增量中考虑。旧版不兼容、自定义格式、指令变化或精确画像资料可能要求完整重建。`stage_state` 记录请求模式、实际模式和回退原因。增量更新超过最大存储大小时失败并保留先前版本，可用完整重建压缩。

| 列表参数 | 契约 |
| --- | --- |
| `memory_type` | `basic` 为 L1 事实、偏好和经历，`insight` 为 L2 洞察。省略返回两者 |
| `limit` | 每页数量，默认 50，范围 1–200 |
| `offset` | 从 0 开始，最大 `9223372036854775807`，默认 0。每页 50 条时依次用 0、50、100 |
| `after` | 可选，取自 `next_after` 的 `unit_id`，不能与非零 `offset` 合用。跨页保持 `memory_type` 及 `limit` 不变 |

响应包括 `items`、`total`、`limit`、`offset`、`next_offset`、`next_after`、`updated_at`、`policy_epoch`。`updated_at` 是所有匹配类型的当前合格记录中最新的 `created_at`，与分页无关，集合为空时为 null，描述发布时间。`total` 统计所有匹配的合格记录，即使当前页为空或超出末尾。末页或游标模式的 `next_offset` 为 null，游标模式也返回 `offset: null`。按 `created_at DESC, unit_id DESC` 排序，时间相同时有稳定顺序。同一数据库语句给出计数和本页。后续请求读取即时状态，索引、移除与修订可能使偏移变化。浏览变化中的集合可用游标，需要固定快照时使用导出。已撤回内容不会因游标保留，清理后游标可能失效。

每项包含 `memory_type`（L1 为 `basic`、L2 为 `insight`）、`unit_id`、`kind`（`fact` 或 `insight`）、规范 `memory_id` 或 `insight_id`、完整 `text`、`created_at`、`locator`、`citations`、`supports`、`affected_insight_ids`。L2 另有当前 `insight_revision` 和描述性 `subtype`。详情通过 `GET /memories/{memory_id}?memory_type=basic` 或 `GET /memories/{insight_id}?memory_type=insight` 读取，返回列表项字段和 `policy_epoch`。路径使用规范 ID，区别于检索 `unit_id`。`memory_type` 缺失或未知、分页值非法时返回 `422`。缺失、类型错误或不合格的记录返回 `404 MemoryUnavailable`，无用户权限时返回不可枚举的 `404 UserUnavailable`。读操作完成日志关联响应的 `X-Request-ID`，记录类型、分页、数量、结果和耗时，省略凭据及记忆正文。

类型详情还返回 `entities`、`relationships` 和 `relationships_truncated`。实体含有来源支持的 `entity_id`、规范 `name` 与已观察到的 `mention`。洞察列出其支持事实中的实体。关系包括已保存的 `caused_by`、`updates`、`semantic`、`temporal`、`shared_entity`，带当前合格记录的规范 ID 和可读 `text`。有向关系的 `direction: outgoing` 表示选中记忆是起点，`incoming` 表示终点。时间关联无向，包含 `time_basis`、`gap_seconds`，可用时另有 `time` 中的 `memory_start`、`memory_end`、`related_start`、`related_end`。ID 顺序不代表时间。实体共同出现或身份相同与带类型的实体关系分开。详情最多 50 条关系，依次优先因果、更新、时间、共享实体、语义，由 `relationships_truncated` 标记省略。关系两端均检查来源策略和名称支持。

`GET /v1/users/{user_id}/entities?query=Atlas` 列出最多 50 个当前有支持的实体。可选 `query` 匹配规范名称或别名的大小写不敏感子串。每项含 `entity_id`、`name`、`memory_count`、`first_mentioned_at` 和 `last_mentioned_at`，提及时间缺失时以记忆创建时间回退。响应另有匹配 `total`。分页前按 `memory_count DESC, entity_id ASC` 排序，使用 `after` 传入 `next_after`。

`GET /v1/users/{user_id}/entities/{entity_id}/memories` 返回名称及最多 50 条支持 L1，按 `(mention_time, unit_id)` 升序。提及时间缺失时用创建时间，`after` 使用返回的 `unit_id`。每项的 `source_chunks` 有真实 `text`、`source_id`、`source_version`、`passage_id`、已记录的 `mentions` 和支持 `quote`。原文与当前合格事实、实体归属、段落、代次和活跃版本绑定。两类实体响应均含 `policy_epoch`，缺失或不合格实体返回 `404 MemoryUnavailable`，计数、名称和支持记忆均依据当前来源及名称资格。

新实体以用户范围内唯一的规范化导航名称标识。来源识别为同一对象的提及复用 ID，有原文区分的同名对象使用 `Alex-1`、`Alex-2` 等显示标签，仍保留原写法 `Alex`。未解决身份时可保留事实，省略无法证实的成员关系。生成标签仅为导航。新增迁移保留旧抽取，历史实体不会自动合并。实体详情还返回有界 `original_names`、`aliases`、`original_names_total`、`aliases_total`、`names_next_after`。用 `names_after` 继续名称分页，只提供该游标时返回名称页与空 `items`，无需重新加载记忆页。记忆游标独立使用 `after` 与 `next_after`。可用时 `source_chunks.mention_spans` 含原段落 Unicode 码点坐标 `[start,end]`，右端排除。范围和名称需有当前原文支持，歧义旧记录不会高亮。`memory_count` 统计不同的 L1 记忆项。

`GET /v1/users/{user_id}/entities/graph?query=Atlas` 返回最多 500 个合格 `nodes` 及匹配元数据，以及最多 5,000 条 `edges`，按共享事实计数及实体 ID 排序。每条无向边含 `source`、`target` 和 `weight`，权重是同时提及两实体的不同当前合格事实数。共现图只表达共同提及。`total`、`truncated`、`edges_truncated`、`policy_epoch` 描述覆盖和当前策略。

`GET /v1/users/{user_id}/request-logs` 按时间倒序返回已授权个人请求的 `items`、过滤后的 `total`、`next_after`、`limit`、`offset` 和 `next_offset`。默认 `limit` 为 50，范围 1–500，`offset` 从 0 开始。可选 `operation` 按方法及路由模板不区分大小写匹配子串，`errors_only=true` 选 HTTP 状态至少 400 的记录，`status_code`（100–599）与 `request_id` 精确匹配。可以用 `after` 原值游标分页，或用 `limit`、`offset` 翻页，非零偏移不能同用游标。`summary` 含未筛选的授权 `total_calls` 和 `success_rate`，后者指 HTTP 状态小于 400 的比例，无请求时为 null。`scope` 标识授权租户和用户。摘要、过滤计数及本页共用 SQL 快照。

日志项有 `time`、`request_id`、`operation`、`status`、`duration_ms`、`error_code` 和 `operation_id`。耗时覆盖响应准备，日志持久化与后台工作另计。HTTP `202` 表示接收，完成状态查看关联操作。日志仅记录授权成功后进入个人路径的请求，排除健康检查、静态资源和日志读取，并省去凭据、请求响应正文、查询参数、原始 URL 标识及异常文本。PostgreSQL 在副本间共享记录，重启保留，目前无自动过期。日志在响应后写入并在正常停机时等待完成；日志失败发出关联警告，业务响应继续按原结果返回。

在 `api_key` 模式中，将 `OAM_BASE_URL` 设为服务地址、`OAM_USER_ID` 设为已授权用户，通过安全方式注入 `OAM_USER_KEY`。以下是只读调用。

```bash
curl --fail-with-body --silent --show-error --connect-timeout 5 --max-time 30 \
  "${OAM_BASE_URL}/v1/users/${OAM_USER_ID}/memories?memory_type=basic&limit=50&offset=0" \
  -H "Authorization: Bearer ${OAM_USER_KEY}"
```

沿 `next_offset` 读到 null 可遍历所有 L1 页，改用 `memory_type=insight` 可读取 L2。使用返回的规范 ID 读单项，并单独读取 L3。

```text
GET /v1/users/{user_id}/memories/{memory_id}?memory_type=basic
GET /v1/users/{user_id}/memories/{insight_id}?memory_type=insight
GET /v1/users/{user_id}/profile
```

### 控制来源及检查处理

用规范 `memory_id` 或 `insight_id` 精确移除指定单元。检查和召回中的这两个字段独立于检索 `unit_id` 和洞察版本。路由决定目标类型。未知、其他用户或类型不符的 ID 返回 `404 MemoryUnavailable`，权限不足走现有不可用响应。移除不生成，返回 `target_kind`、相应类型 ID、`operation_id`、已提交的 `policy_epoch`、`cleanup_status` 和 `reused`。重复同一类型及 ID 返原有操作和当前清理状态。通用取消或重试不能撤销或复制已提交的移除。

L1 移除立即排除选中条目及受影响洞察。已存画像在手动或已配置的自动刷新发布新版本前不可读。原来源、段落和其他记录保留。清理先删除所属投影、索引、链接及失效洞察版本，然后依赖整合可开始。证据充分则用相同洞察 ID 和下一版本重新发布，否则可记录有效删除结果。格式错误或拒答仍为失败。直接移除 L2 会清理其各版本、保留事实，并且不会为重建该洞察安排任务。回执在 `stage_state` 列出关联整合操作 ID，分别检查清理与修复。条目相关的向量和词法索引随所属检索单元删除，保留来源及无关条目的向量继续可用。磁盘取证、备份和已下载导出遵循各自存储或保留规则。

恢复维护使用 `POST /v1/users/{user_id}/operations/{original_removal_operation_id}/recover`，返回原操作的 `200` 响应，只在原地重新激活 `failed_terminal` 清理或已登记的关联修复。保留操作及工作 ID、已提交策略代号、排除状态、支持输入、清理依赖、完成结果、兼容检查点和历史尝试。成功清理不再运行，排队或运行工作及余量不变。目标失败项取得配置的尝试额度、新栅栏值并清除旧所有者和租约。按现有整组待处理根任务容量接收，超限返回 `429` 且不更改状态。未知或外用户操作返回 `404 OperationNotFound`。非移除、已取消、来源已失效或外部结果不明时返回 `409 RemovalRecoveryConflict`。不明结果先用受所有权约束的 `reconcile-attempt`。仍标为运行的过期项、已取消工作、已移除内容、重新抽取、画像及备份有各自处理流程。

生命周期、调试和导出采用凭据所绑定的用户范围。默认用户密钥分别获 `resource:write`、`source:delete`、`debug:read` 和 `export:read`，管理员密钥只能管理。资源、画像资料、记忆列表和资源详情或原件读取应用当前来源资格，响应组装期间策略变化时拒绝旧视图。列表含当前条目、用于下一实时页的稳定 ID `next_after` 和 `policy_epoch`。洞察 `subtype` 为 `preferences`、`patterns`、`projects`、`relationships`、`experiences` 或 `general_insights`，事实为 null。资源和画像资料列表的 `updated_at` 是当前活跃来源版本中的最新接收时间，空集合为 null。它跨越所有页，只度量接收。被撤回的内容不能通过游标访问。

删除来源返回 `202` 及 `source_id`、`excluded_versions`、已提交 `policy_epoch`、`cleanup_operation_id`、`cleanup_status`。召回和检查立即排除来源，之后工作进程删除派生物及原始字节。重复删除复用清理回执。修订或删除若在发布结果前改变来源策略，相关的进行中召回可能不可用。新请求仍可召回其他活跃来源。

每次成功直接召回返回 `trace_id`。只有所有引用单元、图路径中间节点、已记录的别名见证均合格时，`GET /v1/users/{user_id}/debug/traces/{trace_id}` 才返回有界的已记录通道、查询摘要及选项、最终 ID、诊断和确切结果。`POST /v1/users/{user_id}/replays` 接受 `{"trace_id":"trace_...","mode":"inspect_recorded"}` 或 `"reassemble_recorded"`，两种模式都不再调用模型，报告一致、不同、过期或因策略遮盖。`GET /v1/users/{user_id}/debug/diff?before=trace_a&after=trace_b` 比较当前仍合格的已记录结果 ID 及上下文。候选或别名支持失效时返回 `policy_redacted`，不输出历史内容。模型重跑和画像快照差异需独立支持。

`POST /v1/users/{user_id}/exports` 捕获当前来源集，返回 `202`、就绪 `export_id` 和操作回执。`GET /exports/{export_id}` 返回格式 1 的清单，含来源版本、策略及代次、投影变更代号、各类数量、部分覆盖、SHA-256 校验和及安全相对成员路径。`GET /exports/{export_id}/records/{index}` 下载有界 JSONL 分片，`GET /exports/{export_id}/originals/{index}` 下载授权原件。记录区分来源、L0 证据与段落、L1 记忆及支持、合格实体及名称与支持和成员关系、L2 洞察及支持、资源摘要和当前合格的 L3 画像及版本与新鲜度。按清单路径将所有成员下载到 `manifest.json` 旁，再离线运行 `oam validate-export <directory>` 验证格式、数量、路径、大小、校验和、来源和原件身份以及 L1 到 L3 支持关系。捕获后新接收、发布索引、修订或删除会使后续服务器下载返回 `409 ExportPolicyChanged`，直至清理移除旧导出。请新建导出。已下载文件由调用方控制。

同源 `/ui/` Inspector 使用公开路由读取来源、画像、资源、就绪、召回跟踪和详情。凭据只在页面内存中，原文和生成文字按安全文本显示，由内容安全策略约束网页。洞察视图通过带类型的关系读取支持条目。记忆详情提供实体筛选、有向关系地图及列表，每页最多六个相邻记忆，节点重新加载对应的类型详情。原始来源在单独列，窄屏在详情下方。召回视图向 `/v1/recall` 提交完整查询、显式模式及 `allow_partial: true`，显示覆盖、耗时、token 数和证据，失败会清除旧结果并显示错误。画像资料在 `/profile-data` API 管理，接收子类型 JSON、可选来源版本及截至时间，修订须带预期活跃版本。画像正文为只读，手动或配置的自动刷新可应用资料变更。

实体视图先显示列表，显示关联记忆数及首次和最近提及日期。关系图可缩放、平移、按时效用色、按共现显示大小，支持悬停、暂停、全屏和本地 SVG 导出。点击节点或列表项打开侧栏，显示实体元数据及按提及时刻排序的原始段落，缺失时用记忆创建时间。原文中已记录的提及和别名在支持引用内以文本节点高亮，重复段落合并，并可跳转到学习记忆。导航和退出关闭图动画。请求日志有调用数、成功比例、全部或错误、状态、API、请求 ID 筛选、重置、可复制 ID 和分页。HTTP 成功比例包含 `202`；完成情况查看操作状态。两个视图均使用登录用户权限，退出即清除页面数据。[Inspector 使用指南](memory-web-ui.md)

## 5. 响应语义

每个响应带请求 ID 和契约版本。异步工作区分 `accepted`、`source_searchable`、`memory_ready`、`insights_ready` 与画像新鲜度，后续阶段各自报告就绪。

召回返回带类型 `items`、完整 `context`、本地 `o200k_base` 的整数 `context_tokens`、`tokenizer`、`trace_id`，以及查询时间、执行或省略阶段、融合通道、部分覆盖、用量和耗时诊断。每条洞察保留子类型与完整已接收支持。事实保留引用、时间证据和图路径。原文保留正文及边界，`include.chunks: false` 时省略全文。带确证共同段落的事实在最早成员排名处与该段落合并显示，原文在事实前。其他会话引用显示来源版本、角色、消息 ID 和准确引文，未知引用形状保留完整 JSON。完整非空时间一致时事实继承来源时间。重复时间列表可以共享 `Time: Tn` 及同一上下文内的完整 `Time Tn: ...` 定义，保留结构化证据以及角色、范围、精度和时区，只有能降低完整计数时使用。重放组装采用同样规则。计数纳入所有块、引用、限定词、定义及分隔符，须符合总预算或子项份额。调用方用自身模型的分词器统计最终输入。

错误详情经过脱敏，外部获取和模型调用发生在授权后。无效凭据返回 `401`，请求体过大为 `413`，过期 `If-Match`、幂等输入变化、未解决画像刷新或导出策略变化为 `409`，显式输入或 URL 内容无效为 `422`，URL 来源不可用为 `502`，能力、必需召回阶段或检查进行时策略不可用为 `503`，召回期限耗尽为 `504`。需要隐藏的权限、来源、跟踪或导出 ID 使用不可枚举的 `404`。

## 6. 当前实现与验证

带用户资格的保存、召回、批量、规划、分开的资源和画像资料接收及生命周期、存储画像、已记录调试、导出和 Inspector 均已实现。隔离 PostgreSQL 验证涵盖资源修订删除、持久清理、活跃来源检查、记录策略遮盖、L3 导出与失效、基于 L2 的刷新、输入不变复用、失败保留以及证据排除。浏览器检查涵盖资源操作、画像状态及刷新、原文呈现安全、窄屏、凭据清除。画像资料接收与生命周期另有 API 检查。日期化的[发布证据（英文）](../release-evidence.md)记录具体服务商、应用多副本恢复和候选产物范围。

`GET /v1/users/{user_id}/profile/diff` 为保留且当前不可用的路由。记录的模型请求当前不能重跑。规划端点需要显式启用的规划角色。检查实际数据操作与[发布记录（英文）](../releases/0.2.1.md)以确认目标版本能力。
