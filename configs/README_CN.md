# OAM 配置

[English](README.md) | [简体中文](README_CN.md)

## 概述

[oam.yaml](oam.yaml) 是公开行为配置的权威来源。本文说明图扩展、因果索引和召回权重之间的关系。模型绑定、环境变量和服务启动方式见[运维文档](../docs/operations.md)。

## 1. 图扩展模式

检索**通道（arm）**负责生成一个排好序的候选列表。直接向量检索通过语义相似度查找证据；图检索通道则从已匹配的事实出发，通过已存储的实体、语义链接或因果链接扩展到相关事实。

**语义图扩展与直接向量检索是两个不同的步骤。** 直接向量检索比较完整查询的向量与已索引证据的向量。语义链接扩展则从直接向量检索已经找到的事实出发，沿着已存储的链接查找相似事实：

| 路径 | 比较对象 | 候选发现方式 |
|---|---|---|
| 直接向量检索 | 查询与已索引证据 | `查询 → 匹配证据` |
| 语义链接扩展 | 已召回的种子事实与其他事实 | `查询 → 种子事实 → 已存储的语义邻居` |

`fast` 和 `normal` 始终启用基础的直接向量语义检索，不受图扩展设置影响。语义链接扩展是可选的额外检索步骤：它可以找回有用的间接证据，但与种子事实相似的事实不一定回答原始问题，也可能引入相邻话题的噪声。

`search.fast.entity_semantic_graph` 和 `search.normal.entity_semantic_graph` 分别为两种召回模式选择实体图扩展和语义图扩展：

| `entity_semantic_graph` 值 | 实体及别名扩展 | 已存储的语义链接扩展 |
|---|---|---|
| `"off"` | 关闭 | 关闭 |
| `light` | 启用 | 关闭 |
| `full` | 启用 | 启用 |

实体图扩展查找与已召回事实共享同一已存储实体 ID 的其他事实，包括有来源支持的该实体别名。在运维覆盖配置中，将原先每种模式下的 `graph` 键改为 `entity_semantic_graph`；旧键会被拒绝。此次重命名不需要重新索引。

实体图扩展和语义图扩展可按需配置，不必始终同时启用。在 YAML 中必须给 `"off"` 加引号。设置 `entity_semantic_graph: "off"` 后，仍保留直接向量检索、词法检索及其配置的实体名加权，以及适用时间窗口时的时间检索。

仅适用于 fast 的请求字段 `fast_graph` 可以覆盖配置选择：`true` 选择 `light`，`false` 选择 `"off"`，省略或设为 null 则继承 YAML 值。它不覆盖因果遍历。参见[召回 API](../docs/api.md#recall)。

## 2. 独立的因果控制

因果链接表示明确的“结果 → 原因”关系，以 `caused_by` 存储。语义扩展和因果扩展使用不同的关系：

| 方面 | 语义链接扩展 | 因果扩展 |
|---|---|---|
| 关系 | 含义相似，不表达因果判断。 | 来源明确说明某个结果有特定原因。 |
| 链接创建 | 事实之间的向量相似度。 | 模型提取并验证有来源支持的 `caused_by` 字段。 |
| 遍历 | 从种子事实出发，双向各一跳。 | 沿“结果 → 原因”方向，最多两跳。 |
| 召回控制 | 每种模式的 `entity_semantic_graph: full`。 | 每种模式的 `causal_graph: true`，独立于 `entity_semantic_graph`。 |

对于来源中的“因为房租上涨，我推迟了搬家”，因果扩展可以沿着 `推迟搬家 → 房租上涨` 查找。语义扩展则可能把搬家事实连接到另一条关于找公寓的事实。后者是相关信息，不是已经确立的原因。仅有时间先后、共同提及或相似性，不能建立因果链接。

因果开关将链接创建与链接使用分开：

| 设置 | 阶段 | 设为 `true` 时的作用 |
|---|---|---|
| `index.enable_causal_graph` | 索引 | 提取并存储通过验证的因果链接。 |
| `search.fast.causal_graph` | Fast 召回 | 沿现有的出向因果链接查找。 |
| `search.normal.causal_graph` | Normal 召回 | 沿现有的出向因果链接查找。 |

召回沿出向因果路径最多遍历两跳；`search.relation_hops` 仅支持 `2`。每种模式的因果开关也控制时间检索中使用的因果边。关闭它仍会保留普通时间链接和日期处理。

这些开关相互独立。`entity_semantic_graph: "off"` 配合 `causal_graph: true` 可以只启用因果图扩展。启用因果索引不会自动启用因果召回，启用因果召回也不会创建缺失的链接。即使因果索引当前关闭，召回仍可使用先前已存储的因果链接。仅修改召回开关不需要重新索引；修改索引开关会改变提取配方，且不会为旧事实补建链接。

关闭因果索引时，提取仍保留有来源支持的事实、实体、时间标注、文本中的明确原因和状态更新，但会丢弃结构化的 `caused_by` 值。

## 3. 图检索的融合权重

`search.normal_arm_weights.graph` 控制 normal 模式在排名融合时为合并后的图检索通道赋予的权重。已启用的实体、语义和因果邻居共同贡献给这个通道，不会分别获得三个融合票。该权重不负责启用任何扩展路径。

每个已执行的通道为候选贡献 `weight / (search.rrf_k + rank)`，其中排名从 1 开始。当前 normal 模式中，词法、直接向量和时间通道的权重为 `1.0`，图检索通道为 `0.1`。在排名相同时，图检索的贡献是直接向量或词法检索的十分之一。被多个通道找到的候选会累加各通道的贡献。可选的 normal 模式重排在融合之后执行。

Fast 召回为各通道使用相同权重，即使启用了图扩展也是如此。Fast 始终跳过模型重排。图权重 `0.1` 既不是相似度阈值，也不表示图结果占 10% 的配额。

`search.lexical_ranker` 对 fast 和 normal 召回默认使用 `bm25`，以用户范围内的 BM25 得分加上两倍的词项邻近度得分（cover density）对普通全文匹配排序。设置为 `fts` 时仅使用词项邻近度。两种设置都通过 `search.fts_rank_weights` 配置正文、图注、标题层级和文档标题的权重，并沿用相同的排序模式和候选上限。PostgreSQL 在两种设置下都会维护 BM25 索引数据。[索引与检索](../docs/indexing-retrieval.md#6-candidate-retrieval-and-ranking)

`search.lexical_ranking` 对两种召回模式均默认为 `whole_query`，将命中任一所选查询词项的候选统一排序。设为 `per_term` 时，每个词项分别排序后再融合。两种模式也默认启用相邻用户上下文，每段可选相邻原文最多保留 1,000 个字符。升级运维 YAML 时，从 `fast` 和 `normal` 中移除这三个字段，并在 `search` 下设置所需的共享值。单次请求的 `overrides` 对象也可以覆盖这三个字段。

```yaml
search:
  lexical_ranking: whole_query
  adjacent_user_context: true
  adjacent_user_context_max_chars: 1000
```

## 4. 为什么已测试的配置关闭语义图扩展和因果扩展

最新 LME-500 normal、早期 LME-500 fast 和已记录 BEAM-10M 配置均关闭语义链接扩展和因果扩展。BEAM 保留实体及别名扩展，两种 LME 配置则关闭图扩展：

| 评测负载 | 召回模式 | `entity_semantic_graph` | `causal_graph` | 使用的图扩展 |
|---|---|---|---|---|
| 最新 LME-500 | `normal` | `"off"` | `false` | 无。 |
| 早期 LME-500 | `fast` | `"off"` | `false` | 无。 |
| BEAM-10M | `normal` | `light` | `false` | 仅实体及别名扩展。 |

这些配置也都关闭因果索引。它们保留直接向量语义检索、词法检索、适用时的时间检索、原始来源证据，以及事实文本中保留的明确原因。关于原因的问题仍可直接检索这些原因；已存储的因果边是额外的检索路径，不是回答“为什么”的前提。

调优先将图权重从 `1.0` 降至 `0.1`，随后关闭语义链接扩展和因果扩展。BEAM 保留的实体及别名扩展使用 `0.1`；两种 LME 配置均关闭图扩展。

调优观察支持保守扩展：范围较广的图邻居可能加入偏离主题的上下文，并挤占有用证据。尚未对各类扩展分别验证的候选解释是：事实之间的相似性可能偏离查询，而明确记录的原因也可能与当前问题无关。额外候选会竞争有限的检索和上下文容量，因此连接更多证据并不自动意味着回答更好。

已评测的配置能够说明当时选择了哪些路径，但不能分别确定关闭语义链接与关闭因果链接对噪声、上下文 token、准确率和延迟的独立影响。一项图权重实验观察到较广的邻居挤占有用证据，这支持保守赋权，但不等于分别完成了两种扩展的开关对照测试。应区分特定负载下的调优结论与“某类关系普遍有害”这样的判断。

设计优先级是在索引 token 预算内，通过相关且有来源支持的上下文提供准确回答。好的智能体记忆不需要最大限度的图复杂度。保留简单且有效的检索路径；只有在条件一致的对照测试证明额外扩展能够找回所需证据，且不会带来不可接受的噪声、延迟或成本时，才启用它。更丰富的图结构是实现目标的手段，本身不是成功标准。

### 4.1. 由智能体决定何时追查原因

**DNOE（do not overengineer，不过度设计）：** 保持日常召回简单，复用智能体现有的 ReAct 推理与工具调用循环。

- **智能体记忆：** 在上下文和延迟预算内检索相关且有来源支持的证据。
- **ReAct 循环：** 判断任务是否需要因果推理或补充证据，再发起有针对性的后续调用。

1. 通过 `fast` 或 `normal` 获取初始证据；基础的直接向量语义检索始终启用。
2. 如果重要原因缺失或含糊，发起有针对性的后续召回查询，并检查支持它的来源证据。
3. 根据有证据支持的原因组织解释；当来源只能证明时间先后或相关性时，保留不确定性。

当前公开召回请求允许智能体选择 `fast` 或 `normal` 并细化查询。`causal_graph` 仍由运维配置控制，没有逐请求覆盖选项；选择 normal 或询问“为什么”不会自动启用因果图遍历。因此，当前 API 下由智能体主导的调查仍通过额外的普通搜索和基于来源的推理完成。

预期收益是在保护回答准确率和召回延迟的同时，减少无关上下文与不必要的上下文 token。是否进一步追查因果取决于实际任务，由智能体工作流负责。

## 5. 默认值与示例

仓库中的默认配置仅在 normal 召回中启用实体扩展：

| 模式 | `entity_semantic_graph` | `causal_graph` | 已启用的图扩展 |
|---|---|---|---|
| `fast` | `"off"` | `false` | 无，除非请求覆盖配置。 |
| `normal` | `light` | `false` | 仅实体及别名扩展。 |

以下片段展示 [oam.yaml](oam.yaml) 中当前的 normal 图设置和因果索引默认值：

```yaml
index:
  enable_causal_graph: false

search:
  normal_arm_weights:
    graph: 0.1
  normal:
    entity_semantic_graph: light
    causal_graph: false
```

Normal 召回启用实体及别名扩展，图融合权重为 `0.1`。语义链接扩展、因果遍历和因果提取均关闭。两种模式都始终启用基础的直接向量语义检索。要关闭某种模式的全部图扩展，需同时设置该模式的 `entity_semantic_graph: "off"` 和 `causal_graph: false`。

召回诊断将选定的 `entity_semantic_graph` 值记录为 `graph`，并同时报告 `causal_graph` 和 `executed_arms`；检查这些字段可以区分已配置的路径与实际执行的通道。实现入口：[召回服务](../src/oam/retrieval/service.py)、[图检索仓储](../src/oam/retrieval/repository.py)、[语义链接构建](../src/oam/indexing/links.py)、[提取](../src/oam/indexing/extraction.py)和[配置模型](../src/oam/config.py)。

### 5.1. 部分匹配筛选

Fast 的 `search.fast.partial_match_gate` 默认为 `user_fact_exchange`，在组装返回的上下文前筛除相关性证据较弱的对话候选。Normal 的 `search.normal.partial_match_gate` 默认为 `"off"`，保留更广的候选范围供上下文选择。两种模式均支持这两个值。[默认配置](oam.yaml)

假设查询是“我付过研讨会的费用吗？”。一条排名靠后的助手回复讲的是如何报名，也可能因为包含“研讨会”而被召回。如果这条回复仅属于部分词项匹配，并且向量检索没有找到引用同一对话版本中紧邻其前的那条用户消息的事实，筛选就可以将它排除，为付款证据留出空间。筛除一条派生事实，还可能省下该事实所需来源原文占用的空间。

间接匹配的助手原文可通过同一轮问答的证据取得准入资格，条件是限定范围内的向量检索结果同时包含一条由用户消息支持的事实，以及该事实所引用的确切用户原文。直接向量命中、严格词项匹配、通过词项检索直接命中的用户原文，以及词项候选中排名第一且符合条件的对话原文，都可以独立通过筛选。上下文预算和必需支持仍按原有规则处理。[局部证据逻辑](../src/oam/retrieval/local_context.py) · [召回选择](../src/oam/retrieval/service.py)

Fast 默认启用该筛选，以提高上下文预算的使用效率。此前对同一组 13 道题进行 Fast 对照测试时，开启筛选后，渲染后的上下文 token 数减少了 34.1%，保留的跟踪证据段落从 9/16 增至 12/16。平均检索延迟接近，开启时约为 410 毫秒，关闭时约为 412 毫秒。单次请求可查看 `diagnostics.local_context.gate_status` 和 `diagnostics.local_context.filtered_roots`，确认筛选是否执行以及移除了多少候选。

## 6. Fast 来源正文与 Normal 重排

Fast 还支持实验性的 `search.fast.fact_source_bodies` 策略。默认值 `required` 在启用 chunks 时保留每个已选事实的完整来源正文。`ranked` 验证相同的完整持久化来源，保留完整事实、真实引用和来源信息，但完整正文必须在恢复和局部证据筛选后，作为独立且合格的原文候选参与竞争。引用所属来源的扩展、仅来源细节和附属可选邻居不会单独插入正文。完整 Insight 组、normal 和 `include.chunks:false` 保持原有行为。只作为事实支持的原文可能含有有用但未被引用的细节，这些细节可能丢失；采用前应按实际负载验证。去重依据完整且已验证的支持证据。

实验值 `assistant_deduplicated` 先完成普通 required 策略的准入、生命周期检查和 token 修剪，再仅移除由助手来源支持、未截断且冗余的事实：其对应的精确完整原文必须已经选中，时间证据完全一致，且不会丢失别名信息。保留全部原文、用户／混合／未知来源事实及 Insight 必需成员。此策略保留准入和条目顺序，不补入其他候选。若精确 token 数反而增加，则回退到完整 required 表示。Normal、关闭 chunks 和默认 required 的行为不变。被移除事实的 ID 记录于 `diagnostics.context_budget.assistant_deduplicated_fact_ids`；此实验策略没有通用准确率保证。

实验值 `ranked_deduplicated` 使用相同的 ranked 准入，仅当精确来源定位与唯一引用均有效、全部时间证据一致且不丢失别名证据时，用已选中的完整原文替换普通事实。节选和截断原文不能替代事实。原文保留其来源组的相关性位置及完整文本；未匹配事实与 Insight 必需支持保持不变。去重仅释放渲染 token 空间，仍预留原有的 learned/original 池配额。`diagnostics.context_budget.source_deduplicated_fact_ids` 记录替换事实。此策略没有已测得的准确率保证。

Normal 可配置具有 `rerank` 能力的 `models.reranker`，在组装上下文前对融合后的候选文本评分。`search.normal.rerank_candidates` 默认为 48。上限设为 null 时，对融合后的有界候选列表全部评分。模型绑定设为 null 时按融合相关性排序。Fast 跳过模型重排。升级时，从运维 YAML 和 Fast 请求覆盖中移除 `search.fast.rerank_candidates` 和 `search.fast.rerank_min_score` 对应的字段。

## 7. 时间遍历预算与请求覆盖

Fast 和 normal 是可自定义的配置预设。`search.fast.temporal_nodes` 默认为 96，`search.normal.temporal_nodes` 默认为 300。该正整数预算统计时间遍历的种子节点和后续访问节点；直接时间匹配与最终结果池分别控制。`max_results` 不会限制此遍历预算。

要让 fast 使用与 normal 默认值相同的遍历预算，可使用以下配置覆盖：

```yaml
search:
  fast:
    temporal_nodes: 300
```

单次召回可传入 `"recall_level": "fast", "overrides": {"temporal_nodes": 300}`。同一对象也可覆盖所选预设的其他字段；未指定字段继承配置。合并后的值复用现有校验，包括候选上限关系；支持 null 的字段可显式清空。未知字段或无效组合会在检索 I/O 前被拒绝。旧的非 null `fast_graph` 选项优先于 `overrides.entity_semantic_graph`。直接、批量和规划召回均保留覆盖值。响应的 `diagnostics.search_config` 记录生效预设和共享召回设置。

原共享字段 `search.temporal_nodes` 已移除。要保留旧共享值 N 的行为，将其移至 `search.normal.temporal_nodes`，并把 `search.fast.temporal_nodes` 设为 N 与旧 fast `max_results` 的较小值。新配置可独立设置遍历预算和结果限制。本次仅改变召回配置，无需重建索引。
