# Open Agent Memory 评测结果

[中文文档](README.md) · [English](../benchmarks.md)

## 概述

在 **LongMemEval S** 上，OAM 的 8k Fast 用全文搜索（FTS）与 BM25 获得 **95.2%（476/500）**，已保存上下文平均 **7,989.54 tokens**。完整的 18k Normal FTS 对照获得 **95.6%（478/500）**，平均 **11,375.12 tokens**。8k 的平均上下文少 **29.8%**。Hindsight 公布的结果为 **94.6%（473/500）**，平均 **43,124.55 tokens**。各组分数与上下文平均值涵盖全部 500 题。[Hindsight 固定版本结果](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/outputs/longmemeval/hindsight/rag/s.json.gz)

在 **BEAM-10M** 上，OAM 的平均评分为 **78.23%**，通过率为 **86.0%**，Hindsight 对应为 **64.08%** 与 **73.5%**。OAM 保存的上下文少 **9.85% tokens**。这两项 OAM 结果均用 MiniMax M3.1 Flash 的 adaptive 推理回答和评判。[AMB 固定版本的成绩清单](https://github.com/vectorize-io/agent-memory-benchmark/blob/c963dba91dfe6c459f64bb871c5dafb2f3de1bf1/results-manifest.json)

两项评测沿用 Agent Memory Benchmark（AMB）公布的 Hindsight 评测中的问题、固定参考、评分规则及问答和评判模板。它们比较了各自采用不同模型与记忆流程的完整系统。[OAMB 结果索引](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/eval_results/README.md) · [BEAM-10M 证据](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/beam10m-20261119)

## 1. LongMemEval S 的 8k 与 18k 结果

11 月 22 日评测使用 Fast 召回，完整渲染上限为 8,000 tokens。PostgreSQL FTS 匹配证据，评测适配器使用用户级 BM25 加 **2 × `ts_rank_cd`** 排序普通命中。BM25 按词项在用户索引中的频率及文档长度加权，PostgreSQL 覆盖密度为接近的查询词项加分。严格匹配及精确引用短语和标识符保留优先级，词法列表与向量结果融合。

| 指标 | 11 月 22 日 Fast FTS+BM25 | 完整 Normal FTS 对照 |
| --- | ---: | ---: |
| 通过答案 | **476/500（95.2%）** | **478/500（95.6%）** |
| Hard100，预选的 100 道难题 | 83/100 | 85/100 |
| 其余 400 题 | 393/400 | 393/400 |
| 渲染上下文上限 | **8,000** | 18,000 |
| 500 道题的已保存上下文平均值 | **7,989.54** | 11,375.12 |

按题目 ID 配对，8 题仅在 8k 通过，10 题仅在 18k 通过，14 题均失败。两次完整运行的召回模式、排序器及上下文上限不同。另有同样使用 8k 的纯 FTS 对照，测量检索及证据能否进入上下文。

### 8k 检索与评分

| 已记录设置 | 值 |
| --- | --- |
| 数据集 | 清洗后的 LongMemEval S，修订 `98d7416c24c778c2fee6e6f3006e7a073259d48f`，全部 500 个规范题目 ID |
| 已索引证据 | 保留的 L1 索引，完整问题的已保存 1,024 维向量 |
| 词法排序 | 整体查询 FTS，BM25 的 `k1 = 1.2`、`b = 0.75`，加位置覆盖密度 |
| 候选选择 | 单通道 64、融合 128、来源种子 36、时间遍历 96、每个结果池最多 96 |
| 来源证据 | 原始段落、必需原文、`user_fact_exchange` 部分匹配筛选、每项 1,000 字符的相邻用户上下文 |
| 可选路径 | 关闭实体语义图扩展、因果扩展、洞察、重排。召回不注入画像 |
| 回答与评判 | MiniMax M3.1 Flash，原生 adaptive 思考，推理输出分离，响应为文本 |
| 判定 | 首个可用的显式布尔值，使用原参考答案和固定的 AMB 回答及分类评判模板 |
| 保存的上下文 | 500 份共 3,994,771 tokens，范围 7,693–8,000，`o200k_base` 计数 |

8k 结果复用两条试运行的回答与评判对。一条评判请求在六个不可用响应后通过另一条模型路由取得可用判定。评分记录共 1,029 次模型调用、4,998,133 个已报告 tokens，输出含 325,110 个推理 tokens。完整上下文相关性检查覆盖 400/500 题。此次没有新的索引耗时、索引生成用量或实际账单。[固定 AMB 模板](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/src/memory_bench/dataset/longmemeval.py)

配对检索测试为 FTS+BM25 和纯 FTS 各进行每题三次，使用相同 8k 上限、保留索引和查询向量。四组中，包含所有轮及仅重复轮的最高组内词法 p95 为纯 FTS 的 **0.99 倍**。仅重复轮的最高端到端召回 p95 为 **1.03 倍**。首组经环回 HTTP，其余经进程内 API 路由和 PostgreSQL。耗时未包含实时向量请求。

### 11 月 17 日 Normal FTS 存档与补测

11 月 17 日存档为 **476/500**，其中评判 497 份上下文，另外三个参考答案有争议的案例按错误计入。补测用原 Normal FTS 设置重新获取缺失的三份上下文，沿用原参考答案，三题通过两题。完整对照合并这三题与原先 497 题。下表为原存档数据。[11 月 17 日 OAMB 证据](https://github.com/rocke2020/open-agent-memory-benchmark/tree/e8c92db691defccbb6768bdcb110732ab74e0311/eval_results/lme500-20261117)

| 已记录设置 | 值 |
| --- | --- |
| 数据集 | 清洗后的 LongMemEval S，500 个规范 ID |
| 数据修订 | `98d7416c24c778c2fee6e6f3006e7a073259d48f` |
| 数据 SHA-256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| 回答与评判 | `minimax-m3.1-flash`，均用原生 adaptive |
| 模型请求 | `thinking: {type: adaptive}`、`reasoning_split: true`，未指定 effort 或结构化输出 |
| 提示词 | `7021c59c3497d1dd08d61f12fa4629be983fcfb1` 的原样 AMB 回答与分类评判提示 |
| 判定解析 | 首个可用显式布尔，包括正文中的 `correct=false`，保留有效否定 |
| 召回 | Normal，L1，含段落，禁用图扩展、洞察、画像和重排 |
| 上下文上限 | 固定评测覆盖 18,000 tokens。v0.2.1 的 Normal 默认上限为 16,000 |
| 候选选择 | 整体查询，单通道 64、融合 128、来源种子 36、学习项及原文各最多 96 |
| 原文格式 | 用户事实轮次，启用相邻用户证据，每段限 1,000 字符 |
| 期限 | 5 秒 |
| 已保存上下文 | 共 5,652,671，497 份平均 11,373.58，最高 18,000 tokens |
| 独立计数 | tiktoken `o200k_base.encode_ordinary` |
| 召回 HTTP 样本 | 994 次，平均 332.95 ms，中位数 328.59 ms，p95 428.25 ms，未排除 |
| 运行依据 | 已评测的 OAM 提交和设置见链接的 OAMB 证据 |

### 回答和评判的波动

分数取决于召回、模型生成回答和模型评判。一个相同输入的真实例子是 LongMemEval `e66b632c`：“What was my previous personal best time for the charity 5K run?” 在最终上下文逐字节相同且完整请求设置一致时，`minimax-m3.1-flash` 在已公布尝试中正确回答 **27:45**，后续重试错误地回答 **28:00**。两次都用原生 adaptive 思考、`temperature: 0` 和 `top_p: 1`。上下文包含较早的 27:45、较晚的 26:30 和大致提升，两次回答对证据的理解不同。

[两次尝试存档](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/model-variability-20261118)保留上下文和请求哈希、可见回答、判定和请求参数。这个案例证明曾观察到回答变化。变化频率与基准误差范围需要更大受控样本。评判收到不同回答，独立评判波动未在该例中测量。发布分数描述已记录结果。

### Flash 模型的价格

评测用 MiniMax M3.1 Flash 和 DeepSeek Flash 进行回答及评判。下表为 2026 年 11 月 18 日记录的官方标价，单位为美元每百万 tokens。

| 模型 | 未缓存输入 | 输出 | 来源 |
| --- | ---: | ---: | --- |
| MiniMax M3.1 Flash | **0.15** | **0.60** | [MiniMax 公告](https://www.minimax.io/blog/minimax-m31f) |
| DeepSeek Flash，低峰 | **0.15** | **0.60** | [DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/) |
| DeepSeek Flash，高峰 | **0.30** | **1.20** | [DeepSeek 官方定价](https://api-docs.deepseek.com/quick_start/pricing/) |

缓存折扣及峰谷时段以服务商说明为准。记录中的上下文计数不包括索引、推理、输出、重试和其余模型输入。跨路由实际账单与整场受控成本对照未获得。

### 与 Hindsight 已公布结果比较

| 已记录流程 | 通过答案 | 使用 `o200k_base` 的已保存格式化上下文均值 |
| --- | ---: | ---: |
| OAM Fast FTS+BM25（8k），MiniMax M3.1 Flash 回答和评判 | **476/500（95.2%）** | **7,989.54**，500 份 |
| Hindsight，Gemini 3.1 Pro Preview 回答、Gemini 2.5 Flash Lite 评判 | **473/500（94.6%）** | **43,124.55**，500 份 |

OAM 8k 的评分高 0.6 个百分点，平均已保存上下文少 **81.47%**。复算 Hindsight 时解压固定结果，对每条 `results[].context` 使用 tiktoken 的 `o200k_base.encode_ordinary`，500 份合计 **21,562,274 tokens**。AMB 自己的 `avg_context_tokens` 使用 `cl100k_base`，为 **43,624.5**。[固定 AMB 存档](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/outputs/longmemeval/hindsight/rag/s.json.gz) · [AMB 分词源码](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/src/memory_bench/utils.py#L8-L14)

AMB 固定适配器对 Hindsight 的 LongMemEval 高预算召回分别给记忆和原文设 **32,768 tokens**。回答模板在可用时使用原始召回 JSON，公开存档省略这个字段。上下文差异描述这两套已记录的回答模型、评判模型和检索设置。Hindsight 的完整运行配置和实际模型输入用量在该存档中不可得。[AMB 召回适配器](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/src/memory_bench/memory/hindsight.py#L231-L256) · [AMB 回答模板](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/src/memory_bench/dataset/longmemeval.py#L129-L132)

11 月 17 日存档相比更早的 466/500 多通过 10 题，22 题由错变对，12 题由对变错，含一个沿用的争议答案。检索、格式、提示词和评判模型同时有变化。另一项使用相同已选证据的对照发现，格式调整减少 **332,235 tokens（5.55%）**，保留全部 **4,137,965 证据正文 tokens**。

## 2. 较早的同上下文回答模型比较

| 回答模型 | 推理配置 | 评判 | 通过答案 |
| --- | --- | --- | --- |
| `minimax-m3.1-flash` | 原生 adaptive | DeepSeek Flash，high | **466/500（93.2%）** |
| `deepseek-flash` | Max | DeepSeek Flash，high | **463/500（92.6%）** |

两组使用同样的 500 个问题及逐字节相同的 L1 Fast 上下文，总数 **5,740,548**，平均 **11,481.10 tokens**。两组都通过 448 题，仅 MiniMax 通过 18 题，仅 DeepSeek 通过 15 题，均未通过 19 题。记录的 0.6 个百分点差异为 3 题。原评判以及来源和参考答案有争议的情况可查看[固定配对证据](https://github.com/rocke2020/open-agent-memory-benchmark/tree/308eef998091ec05aa2657b637fc37222e029672/eval_results/lme500)。

分数按固定参考答案由模型判定。L2 洞察、画像及文档资源不在评分范围内。复用已有索引，未测量新索引时间和开销。生成用量排除向量 tokens，推理计入输出。两组记录的时间段、延迟和服务商 token 用量不足以比较受控速度或费用比例。

## 3. BEAM-10M

BEAM-10M 从十段**千万 token 规模对话**中提取 200 个问题，测试十种能力。相较 Hindsight 的公布数据，OAM 平均评分高 **14.15 个百分点**，多通过 **25** 题。提升较大的类别是事件排序、时间推理和信息抽取。[BEAM 论文](https://arxiv.org/abs/2510.27246) · [分类和逐题结果](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/beam10m-20261119)

| 指标 | OAM | Hindsight 公布结果 |
| --- | ---: | ---: |
| 平均评分 | **78.23%** | 64.08% |
| 通过率（单题 ≥ 0.5） | **86.0%（172/200）** | 73.5%（147/200） |
| 已保存上下文均值，双方均用 `o200k_base` 重算 | **24,676.88** | 27,373.25 |
| 回答模型 | MiniMax M3.1 Flash adaptive | Gemini 3.1 Pro Preview |
| 评判模型 | MiniMax M3.1 Flash adaptive | Gemini 2.5 Flash Lite |

对比使用 AMB 存档中的同一问题、参考答案、评分细则、回答与评判模板及计分规则。在代入对应输入后，200 个 OAM 回答和 660 次已接受评判匹配存档的提示词字节。三次评判调用在服务商响应格式包装中加入 JSON 示例，保留评判正文与计分方法。Hindsight 的原始请求体不可取得。[存档的评测实现](https://github.com/vectorize-io/agent-memory-benchmark/blob/5d5e8dbe56f140ad982ae61cbd73f43e100de706/src/memory_bench/dataset/beam.py) · [Hindsight 公开产物](https://github.com/vectorize-io/agent-memory-benchmark/blob/5d5e8dbe56f140ad982ae61cbd73f43e100de706/outputs/beam/hindsight/single-query/10m.json.gz)

平均评分对 200 题的分数取均值。事件排序先匹配事件再衡量排序一致性，其余类别对评分细项取均值。部分存档提示词使用源于参考答案的提示。排除事件排序的 180 题中，OAM 为 **76.48%、152/180 通过**，Hindsight 为 **64.35%、136/180**。这些数字比较完整流程，各自的模型与记忆机制不同。

当期的 180 道 Normal 召回复用 L1 索引，包含原始段落，原生上限为 **24,000 tokens**。加上评测标题后用于回答的上下文平均 **23,974.32 tokens**。第 1 段对话的 20 份此前保存上下文纳入总均值。使用相同分词器重算双方上下文，OAM 少 **9.85%**。Hindsight 用 `cl100k_base` 报告 **27,341.99**。实际账单与可比的索引成本不可得。[OAM 采集设置与配对计数](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/beam10m-20261119)

[更早的 BEAM 已保存上下文重放](https://github.com/rocke2020/open-agent-memory-benchmark/tree/308eef998091ec05aa2657b637fc37222e029672/eval_results/beam10m)使用 DeepSeek Flash 和独立评分设置。

## 4. 检查和复算已保存结果

最新 BEAM-10M CSV 提供题目 ID、分数和上下文计数。11 月 17 日 LME 和更早 BEAM 的 `report.html` 提供保存的上下文及逐题结果。[BEAM-10M 配对结果](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/eval_results/beam10m-20261119/scores.csv) · [OAMB 结果索引](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/eval_results/README.md)

在 OAMB 检出目录执行以下测试，验证归档 HTML 及相邻结果分片。

```bash
git clone https://github.com/rocke2020/open-agent-memory-benchmark.git
cd open-agent-memory-benchmark
uv sync --locked
uv run pytest -q tests/test_published_oam_evidence.py tests/test_published_lme500_20261117.py tests/reporting/test_historical_report.py
```

11 月 17 日活动的 OAMB 清单固定相关 OAM 提交和检索配置。测试验证已有结果，无需新模型调用。

原生 OAMB LME 流程使用 OAM API 建立索引、Fast 召回和自带提示词评分。上述 8k 评测复用已有索引，使用实验排序适配器和固定 AMB 提示词。复现检索需要该适配器、固定运行环境及存档索引状态，复评需要已保存的完整渲染上下文。[原生 OAMB 流程](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/docs/native-oam.md)
