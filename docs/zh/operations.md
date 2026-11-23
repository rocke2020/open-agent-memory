# Open Agent Memory 运维

[中文文档](README.md) · [English](../operations.md)

## 概述

OAM 使用一份权威行为配置文件和显式运维命令。必需的模型角色不会静默回退到其他服务商或模型；已经接收的异步写入不依赖客户端保持连接。

来源分段和最终向量输入接收共用 `models.embedding.tokenizer`，默认为 `auto`。精确匹配的模型名称会选择 OpenAI `cl100k_base` 计数器或内置的官方 Qwen3-Embedding 分词器。未知名称使用近似的 `o200k_base`。标量预设 `openai` 选择 `cl100k_base`，并将自动单条输入上限设为 8192；`qwen` 选择对应的已识别 Qwen3-Embedding 资源，无法识别时使用近似的 0.6B 资源，自动单条输入上限为 32768。对于名称不含已公布尺寸标签的在线 Qwen 模型，可设置 `tokenizer: qwen` 使用该近似预设。显式数值 `max_input_tokens` 始终优先，显式 null 表示不设本地上限。请求总量和条数限制仍由模型规则或显式覆盖决定。分词器对象选择配置好的本地计数器；显式 `null` 保留通用 `tokenizer.type`。抽取估算、画像计数和实时召回继续使用各自独立的计数策略。

向量绑定的 `max_input_tokens`、`max_request_tokens` 和 `max_request_items` 各自默认为 `auto`，可以覆盖为严格正整数，或使用兼容的 `null` 表示不设本地上限。解析器使用配置中的模型字面值，或从传入进程环境读取其 `model_env`，不会根据端点或凭据推断。运行时在构造时保存环境快照，因此更换模型后需要重启进程。未配置向量绑定时，保留通用计数器及无本地上限的旧行为。选择某个计数器不能证明其与远端服务分词一致；当前所有本地向量 token 计数均标记为估算。

| 精确模型规则（不区分大小写） | 计数器 | 单条输入 tokens | 每次请求总 tokens | 每次请求条数 |
| --- | --- | --- | --- | --- |
| `text-embedding-ada-002`、`text-embedding-3-small`、`text-embedding-3-large` | `cl100k_base` | 8192 | 300000 | 2048 |
| `Qwen/Qwen3-Embedding-0.6B`、`-4B`、`-8B`；不带命名空间的 `Qwen3-Embedding-<size>`；本地 `qwen3-embedding:<size>` | 对应的内置分词器，包含 EOS | 32768 | 未知 | 未知 |
| `kinfra-text-embedding-0.6b`、`kinfra-text-embedding-4b` | 近似 `o200k_base` | 32768 | 未知 | 128 |
| 其他模型标识 | 近似 `o200k_base` | 8192 | 未知 | 未知 |

表中列出 `tokenizer: auto`、分词器对象及兼容 null 设置使用的模型默认值。标量 `openai` 和 `qwen` 仅按上述规则覆盖自动单条输入上限，不改变请求总量或条数上限。只有这些精确名称会被识别；其他命名空间、后缀、生成、重排及多模态模型名称均按未知处理。请求上限未知表示没有额外的本地限制，不代表服务商容量无限。OpenAI 规则依据其[向量 API 限制](https://developers.openai.com/api/reference/resources/embeddings/methods/create)。内置 Qwen 文件离线加载、验证固定哈希，并保留精确的内容计数器身份；来源记录见[第三方声明（英文）](../../THIRD_PARTY.md)。运维配置的别名只声明本地模型身份，不能证明远端服务与之相同。

发起 HTTP 请求前，运行时和预检会将超长向量输入裁为满足 `max_input_tokens` 的原始 Unicode 前缀，计数包含查询前缀和分词器特殊 token 等完整最终输入。告警记录条目下标、原始和保留的 token 数、上限及分词器身份，不记录来源正文。存储的来源、事实和原始查询保持完整，仅向量表示保留的前缀。若非空前缀也无法放入，则在 HTTP 前失败。显式 `max_input_tokens: null` 关闭该裁减。准备完成后再检查请求总量和条数，超限仍拒绝整个批次；OAM 不会为满足请求总上限继续裁减、重组批次或仅发送部分输入。来源分段在遵守有效单条上限的同时，仍保留完整来源覆盖。

托管的 `text-embedding-v4` 不属于原始 Qwen 规则，按未知模型处理。其[托管 API](https://www.alibabacloud.com/help/en/model-studio/embedding-interfaces-compatible-with-openai)记录上限为 8192 tokens 和 10 条；当前规则不直接支持此托管配置，需要兼容的显式设置及调用方批次大小。OAM 不会重组请求。腾讯[模型列表](https://cloud.tencent.com/document/product/1823/130051#vec-model-list-section)为已识别的 KinFRA 文本模型标注 32K 上下文，OAM 使用 32768-token 输入上限。其[向量 API](https://cloud.tencent.com/document/product/1823/133515)建议每次最多 128 条。OAM 将 128 作为默认产品限制，不将其宣称为服务端硬上限。KinFRA 4B 固定返回 2560 维向量，超过 OAM 当前 2000 维上限，因此识别 token 规则不代表支持完整接入流程。

实时召回使用缓存的本地 `o200k_base`，统计完整候选及返回上下文，与向量绑定无关。计数包括引用、引文、别名、日期、格式和分隔符。现有渲染字节以及紧凑或完整格式选项保持与记录回放兼容。旧轨迹按其记录的 Unicode 估算器重新计数，原生轨迹按 `o200k_base` 计数；不改写历史上下文和计数。该词表下的精确计数不等于回答模型的 token 用量。

使用本地分词器资源时，在 `models.embedding.tokenizer` 下设置 `{type: tokenizer_json, file_env: OAM_EMBEDDING_TOKENIZER_FILE, add_special_tokens: false}`，再用该环境变量指向本地 `tokenizer.json`。特殊 token 策略应匹配已验证的实际服务。OAM 仅加载指定文件，关闭其中保存的截断和填充，并对文件字节及特殊 token 策略计算哈希，不下载资源或静默更换词表。资源缺失或无效时，运行时和预检均失败。已发布的证据及用户命名空间继续合格。恢复工作保留已经发布的段落边界，输入匹配时复用检查点；保存的输入超限时，仍可能在向量接收阶段明确失败。

启动升级后的工作进程前应用所有迁移。会话索引先发布抽取事实和时间链接，再原子保存与来源、版本、索引代次对应的语义种子清单，完成直接工作单元，并在同一来源操作下排队依赖它的 `index_semantic_links` 工作单元。直接记忆此时已经可检索，`memory_ready=true`、`temporal_links_ready=true`，但 `links_ready=false`，来源操作仍为 `processing`。语义工作进程在共享用户行锁之外读取有界候选，重新检查来源资格、代次、租约和栅栏后受控分批发布。成功后设置 `links_ready=true`，完成来源操作，并在启用洞察时按配置延迟安排独立整合。空种子集合仍经过语义任务认领并完成，无需读取邻居。`worker.claim_semantic_links` 可暂停队列中的语义任务而不消耗认领次数。既有内联语义阶段及队列语义单元仍可恢复，兼容的事实、向量和检查点会复用。语义失败不会清除已经发布的直接记忆；应检查操作的就绪状态和失败详情，不能将直接记忆就绪等同于链接全部就绪。需要完整图覆盖时，应等待 `links_ready=true`。

内容索引中，已完成且未拒绝的模型响应若正文为空、仅空白或 null，可以使用剩余 `retry.max_attempts` 工作重试额度；默认包括首次在内共三次。复用成功检查点，保留失败响应的用量；额度耗尽后报告失败，不会按成功的 `no_facts` 处理。显式 `{"outcome":"no_facts","facts":[]}` 仍是无需重试的成功结果。拒绝、仅推理内容、工具调用及其他格式错误继续使用原有失败行为。不新增模型客户端重试循环，也不改变语义链接重试策略。

发布运行时需要在安装 OAM 前提供系统 PostgreSQL 客户端库 `libpq`。本地开发依赖组安装自带依赖的 Psycopg 二进制扩展，发布安装不包括该组。发布镜像必须保留系统库许可证声明。

Fast、Normal 和 Deep 在 `search` 下共用 `lexical_ranking: whole_query`、`partial_match_gate: user_fact_exchange`、`adjacent_user_context: true` 及 `adjacent_user_context_max_chars: 1000`。将 `search.partial_match_gate` 设为 `"off"` 可在三个模式中关闭筛选。来源种子上限也限制用于间接助手回复交叉验证的原始向量候选前缀，但不会将可选用户上下文限制为最早放入的事实。这些流程复用已有向量、词法和来源数据。Fast 始终跳过模型重排。直接词法用户原文，以及词法记忆候选中排名第一、已加载且保存为用户或助手角色的合格原文，可以无需另一个召回事实而通过局部证据检查。排名第一的用户原文会占用此例外；较弱的助手原文仍需既有的独立或局部证据。此资格不改变相关性顺序和预算，也不赋予见证或来源发现权限。启用筛选时，补充用户原文在独立用户事实的来源版本中发现，并共用全局来源种子上限；这些原文在所属事实之后竞争，不得为助手回复或进一步恢复提供资格。检查 `diagnostics.local_context`、`local_context_selection` 耗时及对应召回日志，可确认配置的筛选实际执行。`recovery_sources`、`recovered_user_originals`、`selected_recovered_user_originals` 和 `source_user_context` 分别标识合格来源、发现的完整原文、最终完整原文及所属事实；节选不计入完整原文数。向量检索不可用的部分召回报告 `dense_unavailable` 并保留词法结果。可选相邻用户上下文使用权威配置中的 `search.adjacent_user_context_max_chars` 字符上限，取前一用户段落的尾部及后一用户段落的头部。句子边界优先于分句，并防止错误处理数字或缩写；没有适合且可信的边界时省略可选内容。必需的被引用正文保持完整。节选携带字符范围元数据和可见上下文标记，共用已有条目和 token 上限；关闭 chunks 或节选放不下时跳过。诊断和关联召回日志报告生效字符上限。更改召回配置后应重启 API。修改这些检索字段不会重建或改写已有索引。[权威配置](../../configs/oam.yaml)

每个模式独立选择 `entity_semantic_graph`。Fast 默认为 `"off"`，跳过实体图扩展。Normal 和 Deep 默认为 `light`，通过共享的已存储实体 ID 扩展。任一模式均可选 `full`，额外沿已保存的语义链接扩展。共享的 `search.causal_graph` 默认为 `false`，控制普通及时间检索的出向因果链接。关闭两类图控制后，时间日期筛选和时间链接仍然有效。单请求可通过 `overrides.causal_graph` 更改因果遍历。旧的 Fast 专用 `fast_graph` 请求字段控制实体和语义扩展。比较配置时，检查 `diagnostics.graph`（选中的 `entity_semantic_graph` 值）、`diagnostics.causal_graph` 及关联召回日志。

## 1. 配置

在运维 YAML 中将 `search.default_recall_level` 设为 `fast`、`normal` 或 `deep`，供省略 `recall_level` 的直接、批量和规划请求使用。默认是 `fast`，显式指定的模式优先。修改后重启 API 进程以加载配置，既有索引代次可继续使用。[召回配置](../../configs/README_CN.md#5-默认值与示例)

### 向量索引策略

`database.vector_index_mode` 选择 `small`（默认，共享 HNSW）、`large`（每个租户和用户一份部分 HNSW），或 `growing`（达到 `database.vector_index_min_rows` 前共享，初始阈值为 10,000 个带向量的搜索单元）。这些物理策略与召回模式无关。修改策略保留事实、向量、内容生成身份及已完成模型检查点。向量发现使用近似最近邻搜索（ANN）；图或支持候选的有界集合仍可能用精确分数排序。在严格的归属者筛选下，共享图可能返回较少匹配，应按预期用户分布评估候选覆盖和延迟。

运行 `oam vector-indexes --config <operator.yaml> --once --tenant-id <tenant> --user-id <user>` 为一个归属者创建或验证索引。省略 `--user-id` 选择该租户，两个都省略则选择数据库中的活跃用户。命令使用配置的迁移 DSN，需要不受行级筛选的运维权限；内容角色仍不能创建索引。维护通过数据库会话锁串行执行，因此需要直连或会话池运维连接。系统目录检查验证精确的表、归属谓词、余弦运算符类和 ready/valid 状态；名称冲突时安全失败，中断留下的无效索引可在不重新抽取的情况下修复。

自动创建时，将 `oam vector-indexes --config <operator.yaml>` 作为独立特权维护进程监管。它每隔 `database.vector_index_poll_seconds` 检查一次，默认 900 秒，用 `CREATE INDEX CONCURRENTLY` 创建索引，集合缩小时保留有效专用索引。计数到所需阈值即停止，不在召回路径中执行。SIGINT/SIGTERM 阻止下一个归属者的创建，并等待当前操作结束；维护 SQL 有 15 分钟语句超时。创建后须验证实际使用了共享或归属者索引；系统目录中存在索引不能证明查询计划选择了它。自动切换和运维失败不会解除内容任务暂停或重置模型尝试次数。

### 洞察调度

`index.insights_enabled` 启用自动 L2 洞察合成。`index.insight_consolidation_delay_hours: 24` 为每个租户、用户和索引代次定义自动整合窗口。首个完成必需索引阶段的来源开启窗口，截止前就绪的后续来源共享截止时间。时长必须为正数，如 `0.5` 表示 30 分钟。旧 `insight_consolidation_delay_seconds` 除以 3,600 后改用小时设置。更改后重启 API 和工作进程。已经排队的窗口及旧任务保留保存的到期时间，既有索引仍可用。

默认关闭的用户级整合认领暂停仅供受控评测使用，普通 L2 索引无需运维操作。显式开启后，暂停跨工作进程重启保留，阻止排队的 L2 任务开始。它不停止正在运行的任务，不改变到期时间或消耗尝试次数；应在任务开始前设置，或先等待现有任务结束。解除后，已到期任务可以被普通工作进程接收。暂停是数据库状态，不是索引或召回配置项。

用 `oam l2-hold --config <operator.yaml> hold --tenant-id <tenant> --user-id <user>` 暂停，将 `hold` 替换为 `status` 或 `release` 可查看或解除。命令只使用运维配置选择的迁移 DSN，要求其有效 PostgreSQL 角色具有 `BYPASSRLS` 或超级用户权限，以免运行中的任务被行级安全隐藏。应用凭据不具备此权限。状态报告持久暂停及正在运行的整合任务数。只要存在运行中的整合，包括仍需恢复的过期认领，就拒绝设置暂停。命令不会取消任务或让未来到期的任务立即到期。

在已完成 L1 的集合上启用洞察后，运行 `oam l2-index --config <operator.yaml> --tenant-id <tenant> --user-id <user> --limit <source-count>`。此仅供运维使用的命令，最多为指定数量的活跃当前会话来源排队任务；这些来源须具有合格的已完成 L1，并保留原始代次和已有整合身份。命令不调用模型，也不修改 L1。新任务立即到期，遵守用户暂停及普通工作进程限制，并报告 `scheduled`、`scheduled_count` 和 `queue_full`。已有整合任务会跳过，包括失败任务；失败任务应使用原有重试路径，不能另建替代任务。工作进程处理队列后可再次运行命令，接收更多来源。

受预算约束的会话索引开始前，将 `index.generation_token_budget` 设为正整数；null 关闭新来源的预算接收限制。预算统计单位是一个会话的来源、版本和代次，包括 L1、L2 文本生成及失败和重试调用；画像、向量和文档资源独立计算。原始来源操作固定其预算。每次实际生成调用，包括抽取拆分重试，都在 HTTP 前、现有用户锁内，预留序列化文本请求的 UTF-8 字节数加完整输出 token 上限。这是保守的文本 token 额度，不是服务商报告的实测用量。预留不退还，失败及结果未知仍计入额度；修改配置后原预算继续生效。在已有 L1 上启用预算时，既有完整生成用量用于初始化计数；历史含义不明时阻止新调用。Chat 和 Messages 支持输出上限；受预算限制的模式拒绝 Responses 生成，因为其当前请求不携带该上限。检查 `index_generation_budget` 日志及来源操作的 `index_generation_budget` 状态，将预留额度与实际生成用量分别报告。预算拒绝保留未完成任务及已完成 L1 和检查点，不会缩小输出预算强行发起调用。

`chunk.ingestion` 将每条消息或文档切为段落，目标为估算 2,048 tokens，硬上限 4,096，归属内容不重叠，并保留前置 128 tokens 仅供上下文使用。单段和分组抽取均遵守这些限制。空行定义段落，普通换行不会强制切分。能够容纳时保留完整段落，否则依次按多语言句子标点、分句、空白及有界字符切分。贪心组装可以为完成一个单元超过软目标，但不能超过硬上限。分段器以连续、不重叠的区间覆盖每个输入字符；纯空白片段不进入模型输入，已接收的原文不变。溢出重试使用同一标点检测器在中点附近拆分，保留两个精确来源子串。修改分段设置不影响已有合格记录的读取。会话、纯文本资源及原生 PDF 的重试保留已发布的分段边界，并处理余下来源文本。资源重试拒绝与既有发布结果冲突的解析。每个召回模式共用一份完整渲染上限，`search.fast.context_max_tokens`、`search.normal.context_max_tokens`、`search.deep.context_max_tokens` 默认分别为 8,000、16,000、24,000。默认包含 chunks，严格布尔值 `include.chunks: false` 排除所有完整来源正文，但保留合格学习条目、必需支持事实和引用。两种表示均校验来源资格及完整支持。共享 chunks 只计一次，放不下的完整组直接省略，不裁剪。只有一个合格 L1 事实时，其完整支持也受该上限约束。省略过大的组后，后续较小的组仍可入选。`search.<mode>.max_results` 分别限制学习条目池和完整原文池。召回专用设置不影响索引。应移除旧 `context` 配置及 `chunk.recall` 设置；没有独立的召回分词器选项或文本预算默认值。能力接口公布各模式上下文上限，运行时在召回响应中标识计数实现，精确模型输入计数由调用方负责。画像预览使用独立的 `profile.max_response_tokens: 18000` 默认值及上限。`profile.max_memory_tokens: 32768` 独立限制序列化后的所选记忆及来源信息，不含系统提示词、精确字段和指令。`profile.max_input_tokens: 49152` 另用配置的分词器限制完整系统提示词及序列化请求。适用限制中更严格的一项决定候选能否放入；工作进程同时记录两种 token 数及上限。该计数未必与服务商模型相同，也不保证实际上下文一定容纳；运维方应选择适合的分词器和预算，并为输出和推理留出空间。

消息分组通过 `extraction.group_max_passages: 64` 默认开启。短来源的完整序列化输入及估计输出在限制内时，一次调用完成抽取；较长来源按来源顺序分组，不跨越不合格消息。设为 1 可逐段抽取。`extraction.group_max_estimated_output_tokens: 4096` 限制分组规划估算，`extraction.max_output_tokens: 64000` 提供抽取的 Chat 完成预算，洞察预算独立配置。Messages 要求完成预算，Responses 则不发送该参数，依靠请求截止时间。提高完成预算不会扩大分组，因为规划使用独立预算。长度截断响应会触发递归拆分重试，先在段落边界，再在单个来源区间的句子边界附近拆分。被截断正文绝不发布。不可继续拆分的溢出仍失败；错误 JSON 和其他服务商失败不触发拆分。每个子调用重新检查取消、来源资格，并在发出前续租带栅栏的认领。资源视觉及摘要请求使用 `resource.max_output_tokens: 4096` 作为 Chat/Messages 完成预算，Responses 不发送请求输出上限。子调用按来源顺序在现有外层检查点内执行，用量与溢出尝试汇总；不完整拆分树会整体重试。更改抽取设置会改变新任务的生成方案，既有完成投影仍可读。默认设置减少短来源调用，但真实模型覆盖、生成 tokens 及第一阶段延迟仍需评测。

抽取提示词选择重要且可复用的陈述，同时保留有实际影响的一次性事件、实体、状态更新链接、限定条件及日期证据，排除套话、通用建议和仅表示知悉的承诺。来源支持采用稳定的段落、来源和消息身份，以及一处未改动的唯一引文。分组抽取中，仅当未改动、非空的引文在所有提供段落正文中恰好出现一次时，才恢复缺失或无效的段落标识。仅供上下文的文本及既有事实引用不能确立归属；歧义匹配和与有效给定归属矛盾的引文仍拒绝。恢复保留输出下标并记录 `recovered_passage` 诊断，来源继续标记为部分完成，普通事实及元数据校验仍适用。抽取封装、调用方或模型产生的时间标注，以及存储的段落和事实定位符均不含字符坐标；引文有歧义时需要更长的精确上下文。时间标注共用角色、含时区的起止时间及精确引文。点或区间的范围包含规则及来源由服务端确定，参考时区由来源或请求元数据提供。精度、不确定性和持续时长保留在事实文字及引文中。版本化索引方案见[索引身份实现](../../src/oam/indexing/identity.py)。已有完成投影仍可读，启动不会改写或删除存储数据。升级前应按原契约处理完已接收的待处理任务。含已移除字符或时间分类、来源字段的原始标注封装不能由新严格契约回放；生成方案不会改写已接收的原文。既有数据不会自动迁移或删除。提示词示例和确定性测试不能证明完整历史质量或索引延迟一致。

`index.enable_causal_graph` 默认为 `false`，抽取使用共享实体、时间、更新提示词，不包含结构化因果链接。设为 `true` 后附加因果指令及共享示例的链接字段，保存通过校验的结果指向原因的链接。关闭时丢弃模型自行返回的 `caused_by`，但不会丢弃其他有效事实。此索引选择会改变生成代次。任何模式均可用 `search.causal_graph: true` 遍历已有因果链接，包括时间传播。只启用因果扩展时，配合 `entity_semantic_graph: "off"`。这些召回设置无需重新索引。

既有事实上下文和消息分组默认同时启用：`extraction.reference_max_facts: 10` 最多允许十条事实，`reference_max_input_tokens: 2048` 是可配置的估算 token 软目标。工作进程固定完整合格候选，为抽取请求派生同比例文本预览。计入序列化元数据后，所有长度的文本使用相同比例，不预留短文本或重新分配额度。预览在附近句子或逗号边界结束，因此总量可能超过目标。缩短的预览带 `text_truncated: true`，保留 ID、发言角色及时间元数据，其缺失限定条件仍未知。存储事实和完整新来源不变。`max_input_tokens` 仍对完整序列化模型输入施加硬上限；引用接收保留相关性顺序的前缀，不跳过较大的高排名记录填入低排名记录。将事实条数设为 0 可关闭引用上下文。选择复用段落向量，不增加生成调用。更改引用设置或索引方案不要求重建已经完成的集合。质量提升和第一阶段耗时需同条件真实评测，向量 tokens 应单独报告。

`configs/oam.yaml` 管理认证模式、限制、角色别名、检索和画像策略、工作并发、租约及保留策略。运维方可用其他 YAML 覆盖选定字段，省略字段从权威 YAML 继承，不存在另一套 Python 数值默认值。CLI 启动时，继承的进程变量优先，`./.env` 只补充缺失变量。环境变量提供 PostgreSQL DSN、运维方提供的私有 `OAM_ROOT_API_KEY`、通过 `model_env` 选择的模型名、选定绑定引用的端点及密钥、运维密钥交付目的地和可选配置路径。环境变量不会隐式覆盖行为默认值。

API 和工作进程启动服务前，拒绝缺失或无效的必需 PostgreSQL 连接串，以及无法解析的已启用模型绑定。迁移只检查迁移连接，不要求模型凭据。启动检查不调用模型，应独立运行 `oam preflight` 验证实际服务商能力。

必需角色包括抽取、洞察、向量、资源摘要及画像生成。生成角色共享 `models.vlm`，向量及可选重排各有独立绑定。VLM `protocol` 可选 `openai_chat`、`openai_responses`、`anthropic_messages`：Chat 在基地址后加 `/chat/completions`，Responses 加 `/responses`，Messages 加 `/v1/messages`。应配置与协议匹配的基地址；Messages 使用 `x-api-key` 和 `anthropic-version`，OpenAI 协议使用 Bearer。`models.reasoning_effort` 中 `null` 表示角色关闭，其他值启用角色。`reasoning_mode: legacy_chat` 保留既有 Chat thinking 和 effort 字段，`effort` 发送协议专用 effort，`provider_default` 显式省略推理控制。新的非 Chat 绑定必须显式选择非 legacy 模式。`reasoning_split` 仅支持 Chat：`null` 不发送，`false` 和 `true` 原样发送。各服务商对这些选项、结构化输出和视觉的支持不同，应预检真实端点。抽取预检同时要求文本和视觉。Chat 响应开头完整的推理块会在 JSON 校验前分离，不完整或仅推理内容失败。Messages 响应报告的缓存和未缓存输入 tokens 均计入总量。服务商报告时，用量及日志提供缓存、未缓存及创建缓存的输入数；未知明细为 null，总输入始终包含这些部分。OAM 不保存价格或计算费用。

`models.embedding` 独立声明模型、端点、可选密钥及 `embeddings` 能力，使用 `protocol: openai_compatible`。`index.embedding_dimensions: 1024` 决定数据库初始化及运行校验的向量维数，端点必须返回相同维数。无认证的向量端点可在运维 YAML 中设置 `models.embedding.api_key_env: null`。`models.reranker` 配置可选 Normal 和 Deep 评分，默认 null，设置后必须具备 `rerank` 能力。通过 `models.reasoning_effort.planner` 选择 effort 可启用可选规划器，它复用同一 VLM。预检验证各角色实际行为，不以模型名称或配置存在为依据。

旧 `models.roles` 和 `models.bindings` 别名映射已不接受。应将共享生成定义移到 `models.vlm`，角色 effort 移到 `models.reasoning_effort`，向量和重排定义移到对应命名字段，并移除每个绑定下的 `thinking`、`reasoning_effort`。配置的向量维数必须匹配数据库 schema。维数相同不保证不同向量模型兼容；更换模型时，运维方应按需要安排重新生成向量。模型及生成方案变更不会自动重建已完成集合。

### 升级运维 YAML

沿用 v0.2.0 的运维 YAML 启动 v0.2.1 前，从各模式下移除原有的 `causal_graph`、`fact_source_bodies`、`lexical_ranking`、`partial_match_gate`、`adjacent_user_context` 和 `adjacent_user_context_max_chars`，然后在 `search` 下设置所需共享值。各进程应加载同一份运维配置。旧预设值不同时，可在需要不同共享值的请求中使用 `overrides`。`entity_semantic_graph` 和数值限制仍按模式设置。Fast 跳过模型评分，运维 YAML 和 Fast 请求覆盖不再接受 `search.fast.rerank_candidates`、`search.fast.rerank_min_score` 所对应的字段。Normal 和 Deep 分别接受 `rerank_candidates`，配置了 `models.reranker` 时可调用评分模型。[权威配置](../../configs/oam.yaml)

Fast、Normal、Deep 的标准上下文上限分别为 8,000、16,000、24,000 tokens。显式 Normal `max_results` 或 `rerank_candidates` 超过默认融合候选上限 128 时，提高 `search.normal.fused_candidates` 或降低相应覆盖值。若要保留以往省略模式时的选择，设置 `search.default_recall_level: normal`，再按需分别配置上下文与候选限制。Deep 默认为 24,000 tokens 和 154 个融合候选，排序与证据策略使用 v0.2.1 的共享设置。

## 2. 命令

- `oam preflight` 解析配置、隐藏密钥，并测试已启用角色的能力。
- `oam migrate` 使用独立迁移凭据应用版本化 PostgreSQL 迁移。
- `oam api` 运行 FastAPI、内置 Inspector，以及默认启用的嵌入式后台执行器。嵌入模式除 API DSN 外，还需要队列和工作内容 DSN。纯 API 进程应设置 `worker.embedded_enabled: false`。
- `oam worker` 使用独立队列和内容数据库角色运行同一组后台处理器。`--once` 最多处理一个队列工作单元后退出。每个执行器最多有 `worker.max_tasks` 个并发任务，用户级认领、生成、向量和索引数据库事务各有独立上限。嵌入开关不会关闭显式启动的工作进程。
- `oam model-permits --config <operator.yaml> list` 使用迁移 DSN 的运维权限检查结果未确认的模型调用元数据。API 和工作凭据不能直接修改许可记录。
- `oam model-permits --config <operator.yaml> reconcile <permit-id> --reason <text> --known-completed` 在独立确认远端已完成后释放许可。`--force-unknown` 则明确接受远端结果未确认，实际物理并发仍未验证。
- `oam model-permits --config <operator.yaml> configure` 在停止 API 和工作进程后应用 YAML 容量变更。活跃 HTTP 请求及运行中或结果不明的模型许可会阻止更改。
- `oam recover-bootstrap` 使用独立数据库管理权限，替换已经提交但一次性交付失败的初始化密钥。
- `oam validate-export <directory>` 无需数据库或模型，校验下载的格式 1 清单、每份声明的 JSONL 和原文、条数、哈希及来源和支持身份。

`GET /health/live` 报告进程存活。`GET /health/ready` 仅在以下有界检查通过时返回 `200`：API 数据库连接可读取所有必需迁移版本、已启用模型配置能解析、嵌入工作进程健康且其配置的队列和内容数据库可连接，以及模型接收可用。否则返回 `503`、原因及数据库和 schema 状态。就绪检查不调用模型，也不能证明服务商可达。部署此检查前应用 `0025_readiness_metadata.sql`，授予运行角色对迁移注册表的只读权限。

## 3. 初始化与凭据

运维初始化命令为 `oam migrate`，应在启动 API 或工作副本前执行。应用 0017–0021 前，先处理完并停止旧 API 和工作进程，运行 `oam migrate` 后再启动新版。旧工作进程无法处理 0018 中具有依赖的画像任务，也无法写入 0020 要求的复数洞察子类型；新 API 需要 0019 的发布时间及 0021 的画像文档列。省略 `auth.mode` 且未设置 `OAM_ROOT_API_KEY` 和 `OAM_BOOTSTRAP_OUTPUT` 时，全新数据库创建活跃 `default` 租户但无管理员凭据，`oam api` 选择 `dev` 且必须监听环回地址。显式无管理员密钥的 `trusted` 模式也创建该默认租户并接受本地网关身份头，非本地 trusted 启动需要活跃管理员密钥。`OAM_ROOT_API_KEY` 将运维提供的密钥以摘要存入数据库，不写交付文件。另一方式是通过 `OAM_BOOTSTRAP_OUTPUT` 指定新的仅运维可读文件，接收生成的管理员密钥：身份先提交，再按严格权限仅写一次密钥，绝不进入普通日志。仅一个初始化进程获胜，生成密钥交付中断后需要显式恢复。空密钥或同时配置给定密钥和交付路径均失败。既有密钥数据库在省略模式时保留 `api_key`，不会静默降为 `dev`；未完成初始化会导致启动失败。应用副本仅获得各自运行角色的 DSN。管理员通过 `POST /v1/users` 创建用户，一次性收到首个用户密钥，再使用该密钥访问个人数据；`api_key` 模式下，管理员凭据本身不能访问个人路由。

成功的凭据验证可按完整密钥摘要缓存固定的 30 秒，命中不会延长时间。无效密钥不缓存。撤销先提交，阻止旧查询重新填充缓存，并报告跨进程生效的最晚时间。

在 `committed` 交付中断后，重新运行 `oam migrate` 记录 `recovery_required`，再用新的恢复交付路径显式运行 `oam recover-bootstrap`。恢复先提交旧密钥撤销及替代密钥，再交付；再次中断仍需要显式恢复，不会静默创建另一位管理员。

迁移 0040 需要能绕过强制行级安全的迁移凭据。它读取所有归属者的存储词法字段，并在一个事务内回填 BM25 投影。回填期间阻塞 `search_units` 写入，应在升级副本启动前安排写维护窗口。输入来自现有内容、事实和向量，模型调用为零。可设 `search.lexical_ranker: fts`，在已迁移的数据库上使用覆盖密度排序。[词法排序配置](../../configs/README_CN.md#3-图检索的融合权重)

## 4. 限制与失败责任

画像调用在 `profile.attempt_timeout_seconds` 下有独立 180 秒超时，工作进程仅为该调用延长画像认领租约；刷新在生成前返回持久回执。每项资源最多接受 100 MiB 原始字节，以及 100 页 PDF 或 20 张原图；当前公开图片路由一次仅接受一张。`resource.max_input_pixels_per_image` 在完整解码前拒绝超过 100 百万像素的原图。`resource.max_pixels_per_image` 将视觉副本及 PDF 渲染图缩到每张最多 40 百万像素，保留原图。`resource.max_total_pixels` 对原图或渲染 PDF 页施加 250 百万像素总硬上限，可容纳普通渲染比例下 100 页 Legal 尺寸页面，但不保证任意 100 页文档均能放入。报告及会话集合条数设置仍属保留配置，未执行；部署允许 10 个同时 API 请求。HTTPS 证据 URL 也有 100 MiB 内容上限，以及 `resource.url_fetch_timeout_seconds` 管理的 30 秒总抓取期限，DNS、连接及正文传输共用。PDF 本地解析和渲染具有 `resource.pdf_child_timeout_seconds` 管理的独立 30 秒子进程期限，结果传输另有界限。模型发出前仍执行各项硬限制。

权威容量默认值如下。

| 设置 | 默认值 | 范围 |
| --- | ---: | --- |
| `api.max_concurrent_requests` | 10 | 跨副本共享的已接收业务 HTTP 请求 |
| `api.max_concurrent_writes` | 6 | 共享变更请求；其余四个总槽位留给读取 |
| `worker.max_tasks` | 10 | 每个执行器的任务 |
| `database.indexing_concurrency` | 4 | 跨副本共享索引事务；模型 I/O 不占数据库槽位 |
| `user.concurrency` | 2 | 所有执行器中每个租户和用户的有效认领 |
| `queue.max_pending_per_user` | 1,000 | 未完成的根接收请求，含延迟的必需子任务 |
| `provider.llm_concurrency` | 32 | 共享文本和视觉生成调用 |
| `provider.embedding_concurrency` | 8 | 共享向量调用 |
| `provider.embedding_search_reserved` | 0 | 可选的向量预留容量，不供后台使用；检索可以使用任意空闲槽位 |
| `provider.rerank_concurrency` | 4 | 每个前台模型客户端的本地重排调用 |

HTTP 接收限制覆盖 `/v1/` 路由，存活、就绪和静态资源不占业务槽位。POST 搜索按读取处理。超载时在读取请求体或接收来源前返回可重试 `429` 及 `Retry-After`。每个已接收 HTTP 请求为接收槽位持有独立 PostgreSQL 会话；规划连接时，除内容连接池外，还须计入整个部署最多十个此类会话及短模型接收事务。运行 DSN 必须直连或使用会话池，事务池无法维持会话级 advisory lock 的归属。

首个 API 或工作进程启动时将共享容量固定到 PostgreSQL，其他副本的共享限制冲突时启动失败。运维 YAML 对生成、向量、工作任务及索引事务分别限流，旧 `worker.max_model_calls`、`database.worker_pool_max`、`provider.global_concurrency` 必须移除。升级到迁移 0024 或修改共享限制时，先停止 API 和工作进程、处理结果不明的许可，运行 `oam migrate`，再运行 `oam model-permits --config <operator.yaml> configure`，最后以一致配置重启全部副本。配置器拒绝活跃模型许可、HTTP 接收及索引事务槽位。数值默认值是初始容量，不代表吞吐量或延迟保证。

接收先检查幂等性，再检查根请求数。运行任务、自动重试及洞察窗口中每个尚未结束的来源继续占据根槽位；显式重试需要新槽位。到达接收上限后，精确回执复用及未变化画像刷新仍可执行。来源删除清理豁免，避免队列满时无法删除来源。URL 路径在抓取前进行轻量队列预检，接收字节时重复权威检查。

后台模型请求在 `model.attempt_timeout_seconds` 下默认超时 600 秒，包含等待容量。索引延长带栅栏的租约，以覆盖超时及续租余量。被捕获的传输错误，包括读写超时和断开连接、HTTP 429 及服务端错误，在 `retry.max_attempts` 内重试未完成工作，默认共三次工作尝试，不额外增加模型客户端循环。兼容的完成检查点会复用，失败调用用量未知时保持部分已知，不按零计数。捕获到传输失败时释放本地模型容量，但服务商仍可能完成并收费，因此该限制约束本地运行中调用，不是不可见的远端计算。

模型许可区分尚未使用的预留和已经提交的调用意图。未使用的预留可过期；未在本地结清的中断调用，包括进程丢失，保留结果未确认的许可。每个许可 ID 通过 `X-Request-ID` 发送给服务商，便于关联。应通过运维命令检查这些许可，确认远端完成后再释放；也可明确接受不确定性并强制释放。全部许可均不确定时，就绪状态显示阻塞。强制释放保留不确定性记录，不能证明远端物理并发上限。

工作进程在后续依赖任务前保留兼容检查点和用量。关闭时停止认领和新模型调用，最多等待 `lease.shutdown_seconds`；同步任务仍未完成时报告未完全结束。客户端继续由任务持有直至最终完成，限时等待不会强行终止 Python 线程。工作租约、栅栏和未知结果记账仍是恢复依据。

API 在返回操作回执前提交资源修订或来源全版本排除。删除取消未完成索引认领，并排队独立 `cleanup_source` 工作单元；工作进程移除活跃存储中受影响的派生内容、原始字节及保留的导出部分，再完成清理操作。清理失败持续显示待处理或部分失败，绝不恢复资格。新的召回、检查、轨迹和导出读取会重新检查当前策略；运行中读取跨越来源变更时返回具名的不可用或已隐藏结果。备份到期和恢复验证属于后续发布运维验收，因此清理成功只描述当前 PostgreSQL 活跃存储。

## 5. 当前实现状态

已实现的核心包括合格会话及文档索引、有支持的 L1/L2 发布、fast/normal/deep 带引用召回、手动和可选自动刷新用户画像、临时自定义画像、带类型的记忆和来源生命周期、诊断记录、可移植导出及 Memory Inspector。从 v0.1.0 升级时，启动新版 API 和工作进程前应用包括 0038、0039 在内的全部待处理迁移。[评测证据](benchmarks.md)记录 LongMemEval S 和 BEAM-10M 所评估的版本。当前验证见 [v0.2.1 发布记录（英文）](../releases/0.2.1.md)，早期服务商检查见 [v0.0.1 候选证据（英文）](../release-evidence.md)。物理多机故障、数据库高可用、备份恢复，以及达到采用规模时的用户列表查询成本，均是独立且尚未验证的边界。
