# Open Agent Memory evaluation results

## Overview

On **LongMemEval S**, OAM's 8k Fast run with full-text search (FTS) and BM25 scored **95.2% (476/500)** with **7,989.54 mean saved context tokens**. The completed 18k Normal FTS run scored **95.6% (478/500)** with **11,375.12 mean tokens**. The 8k run used **29.8% less context on average**. Hindsight's published result was **94.6% (473/500)** with **43,124.55 mean tokens**. Each score and context mean covers all 500 questions. [Hindsight's pinned AMB result](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/outputs/longmemeval/hindsight/rag/s.json.gz)

On **BEAM-10M**, OAM earns **78.23% mean rubric credit** and **86.0% passes**, versus Hindsight's **64.08%** and **73.5%**, with **9.85% fewer saved context tokens**. The headline OAM results on both benchmarks use MiniMax M3.1 Flash with adaptive reasoning for answering and judging. [Pinned AMB results manifest](https://github.com/vectorize-io/agent-memory-benchmark/blob/c963dba91dfe6c459f64bb871c5dafb2f3de1bf1/results-manifest.json)

Both benchmarks use the same questions, fixed references, scoring rules and answer and judge prompt templates as the published Hindsight evaluations in Agent Memory Benchmark (AMB). The results measure end-to-end systems with distinct models and memory pipelines. [OAMB results index](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/eval_results/README.md) · [BEAM-10M evidence](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/beam10m-20261119)

## 1. LongMemEval S at 8k and 18k

The November 22 run used Fast recall with an 8,000-token rendered context cap. PostgreSQL full-text search (FTS) matched evidence, and the benchmark adapter ranked ordinary matches with owner-local BM25 plus **2 × `ts_rank_cd`**. BM25 weights terms using their frequency in the owner's index and adjusts for document length. PostgreSQL cover density favors query terms that occur close together. Strict matches and exact quoted phrases or identifiers retained their existing priority, and the lexical list was fused with dense retrieval.

| Measurement | Fast FTS+BM25, November 22 | Completed Normal FTS comparison |
| --- | ---: | ---: |
| Accepted answers | **476/500 (95.2%)** | **478/500 (95.6%)** |
| Hard100, the preselected 100 hard questions | 83/100 | 85/100 |
| Remaining 400 | 393/400 | 393/400 |
| Rendered context cap | **8,000** | 18,000 |
| Mean saved context tokens across 500 questions | **7,989.54** | 11,375.12 |

Across matching IDs, eight answers passed only at 8k, ten passed only at 18k, and fourteen failed in both runs. The two recorded runs differ in recall mode, ranker and context cap. The paired 8k plain-FTS control measured retrieval and evidence visibility.

### 8k retrieval and scoring

| Recorded setting | Value |
| --- | --- |
| Dataset | Cleaned LongMemEval S at `98d7416c24c778c2fee6e6f3006e7a073259d48f`, all 500 canonical question IDs |
| Indexed evidence | Retained L1 indexes, saved 1,024-dimensional embeddings of the full questions |
| Lexical ranking | Whole-query FTS, BM25 `k1 = 1.2` and `b = 0.75`, plus positional cover density |
| Candidate selection | 64 per channel, 128 fused, 36 source seeds, 96 temporal traversal nodes and up to 96 final results per pool |
| Source evidence | Original chunks, required source bodies, `user_fact_exchange` gate for partial matches, and adjacent user context capped at 1,000 characters per neighbor |
| Optional paths | Entity-semantic and causal graph expansion, Insights and reranking disabled. Profile omitted from recall requests. |
| Answer and judge | MiniMax M3.1 Flash with native adaptive thinking and separate reasoning output, text responses |
| Verdict | First usable explicit boolean, using the original golds and the pinned AMB answer and category-specific judge templates |
| Saved context | 3,994,771 tokens across 500 contexts, range 7,693–8,000, counted with `o200k_base` |

The 8k score includes two reused pilot answer/judge pairs and one judge request that produced a usable verdict on a second model route after six unusable responses. Scoring recorded 1,029 model calls and 4,998,133 reported tokens, with 325,110 reasoning tokens included in output. Whole-context relevance review covers 400 of 500 questions. Fresh indexing time, fresh indexing generation usage and billed cost were unavailable for this run. [Pinned AMB prompt templates](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/src/memory_bench/dataset/longmemeval.py)

The matched retrieval check used the same 8k cap, retained indexes and query vectors in three passes per question for FTS+BM25 and plain FTS. Across the four cohorts and both all-pass and repeat-pass measurements, the highest cohort lexical p95 was **0.99×** plain FTS. The highest repeat-pass end-to-end recall p95 was **1.03×**. The first cohort used loopback HTTP and later cohorts used an in-process API route backed by PostgreSQL. Timings exclude live embedding calls.

<a id="1-latest-longmemeval-s-476500-at-18k"></a>

### November 17 Normal FTS archive and follow-up

The November 17 archive recorded **476/500** with 497 judged contexts and three disputed-reference cases carried as incorrect. The follow-up regenerated the three missing contexts using the archived Normal FTS settings and accepted two of three answers using the original golds. The completed comparison combines those three measurements with the original 497. The settings and counts below describe the original archive. [November 17 OAMB evidence](https://github.com/rocke2020/open-agent-memory-benchmark/tree/e8c92db691defccbb6768bdcb110732ab74e0311/eval_results/lme500-20261117)

| Recorded setting | Value |
| --- | --- |
| Dataset | Cleaned LongMemEval S; 500 canonical IDs |
| Dataset revision | `98d7416c24c778c2fee6e6f3006e7a073259d48f` |
| Dataset SHA-256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| Answer and judge | `minimax-m3.1-flash`; native adaptive for both |
| Model wire settings | `thinking: {type: adaptive}`, `reasoning_split: true`; no explicit effort field or structured-output mode |
| Prompt policy | Exact AMB answer and category-specific judge prompts at `7021c59c3497d1dd08d61f12fa4629be983fcfb1` |
| Judgment parsing | First usable explicit boolean, including inline `correct=false`; valid No judgments are retained |
| Recall | Normal; L1; chunks included; graph, Insights, Profile and reranker disabled |
| Context limit | 18,000 tokens, a pinned evaluation override; the current product Normal default is 16,000 |
| Candidate selection | Whole query; 64 per channel, 128 fused, 36 source seeds, at most 96 learned items and 96 original bodies |
| Source formatting | User-fact exchanges; adjacent user evidence enabled, 1,000-character bound |
| Deadline | 5 seconds |
| Exact saved context tokens | 5,652,671 total; 11,373.58 mean across 497; 18,000 maximum |
| Independent context counter | tiktoken `o200k_base.encode_ordinary` |
| Retrieval HTTP samples | 994; mean 332.95 ms, median 328.59 ms, p95 428.25 ms; no exclusions |
| Runtime provenance | The evaluated OAM commit and evaluation configuration are recorded in the linked OAMB evidence |

### Answer and judge variability

Reported accuracy depends on the generated answers and model-based judgments as well as retrieval. A real same-input example is LongMemEval question `e66b632c`: “What was my previous personal best time for the charity 5K run?” With byte-identical final context and identical complete answer-request settings, `minimax-m3.1-flash` answered **27:45** correctly in the published attempt and **28:00** incorrectly in a later repeat. Both requests used native adaptive thinking, `temperature: 0` and `top_p: 1`. The context contains the explicit earlier 27:45 record alongside a later 26:30 result and an approximate improvement; the two answers interpret that evidence differently.

The [saved two-attempt example](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/model-variability-20261118) preserves context and request hashes, visible answers, judge decisions and request options. It illustrates observed answer variation, not how often it occurs. A selected example or one paired small-cohort rerun does not establish a randomness rate or benchmark error bar. The judges received different answers, so this example does not isolate independent judge variability. The published benchmark scores remain the recorded outcomes, not guaranteed repeat-run scores.

### Low-cost Flash models

The evaluated pipelines use MiniMax M3.1 Flash and DeepSeek Flash for answering and judging. Their vendors publish the following list prices, checked on November 18, 2026, in USD per million tokens:

| Model | Uncached input | Output | Source |
| --- | --- | --- | --- |
| MiniMax M3.1 Flash | **$0.15** | **$0.60** | [MiniMax official announcement](https://www.minimax.io/blog/minimax-m31f) |
| DeepSeek Flash, off-peak | **$0.15** | **$0.60** | [DeepSeek official pricing](https://api-docs.deepseek.com/quick_start/pricing/) |
| DeepSeek Flash, peak | **$0.30** | **$1.20** | [DeepSeek official pricing](https://api-docs.deepseek.com/quick_start/pricing/) |

These prices support describing the models as low-cost; they do not establish a market-wide cheapest model, the Tencent route's reconciled bill or a controlled whole-campaign cost comparison. Cached-input discounts and peak schedules are defined by the vendors. Saved context size alone does not include indexing, reasoning, output, retries or all final model-input tokens.

### Comparison with the recorded Hindsight result

| Recorded pipeline | Accepted answers | Mean saved formatted context, `o200k_base` |
| --- | --- | --- |
| OAM Fast FTS+BM25 (8k), MiniMax M3.1 Flash answer and judge | **476/500 (95.2%)** | **7,989.54** across 500 contexts |
| Hindsight, Gemini 3.1 Pro Preview answer and Gemini 2.5 Flash Lite judge | **473/500 (94.6%)** | **43,124.55** across 500 contexts |

OAM's 8k score is 0.6 percentage points higher and its mean saved formatted context is **81.47% smaller**. To reproduce the Hindsight figure, decompress the pinned archive and count each `results[].context` with tiktoken's `o200k_base.encode_ordinary`. The 500 contexts total **21,562,274 tokens**. AMB's own `avg_context_tokens` field shows **43,624.5** with `cl100k_base`. [Pinned AMB archive](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/outputs/longmemeval/hindsight/rag/s.json.gz) · [AMB tokenizer source](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/src/memory_bench/utils.py#L8-L14)

The pinned AMB adapter requests high-budget Hindsight recall with separate **32,768-token limits** for memories and original chunks on LongMemEval. The answer template uses raw recall JSON when available, and the public archive omits that field. The measured gap describes saved context size in these recorded pipelines, whose answer models, judge models and retrieval settings differ. Hindsight's full runtime configuration and original model-input token usage are unavailable from the archive. [AMB recall adapter](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/src/memory_bench/memory/hindsight.py#L231-L256) · [AMB answer template](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/src/memory_bench/dataset/longmemeval.py#L129-L132)

The November 17 archive gained ten accepted answers over the earlier 466/500 run. Twenty-two previously incorrect cases became correct and twelve previously correct cases became incorrect, including one carried disputed-reference verdict. Retrieval, formatting, prompts and the judge model changed together. A separate comparison using the same selected evidence found that formatting reduced total context by **332,235 tokens (5.55%)** while keeping all **4,137,965 evidence-body tokens**.

## 2. Earlier same-context answer-model comparison

| Answer model | Answer reasoning | Judge | Accepted answers |
| --- | --- | --- | --- |
| `minimax-m3.1-flash` | Native adaptive | DeepSeek Flash, high | **466/500 (93.2%)** |
| `deepseek-flash` | Max | DeepSeek Flash, high | **463/500 (92.6%)** |

These earlier arms use the same 500 questions and byte-identical L1/fast contexts: **5,740,548 tokens total; 11,481.10 mean**. Both arms are accepted on 448 questions, MiniMax alone on 18, DeepSeek alone on 15, and neither on 19. The recorded 0.6 percentage-point difference is three questions. Original judgments, including disputed source/gold cases, are retained in the [immutable paired evidence](https://github.com/rocke2020/open-agent-memory-benchmark/tree/308eef998091ec05aa2657b637fc37222e029672/eval_results/lme500).

These are fixed-reference judge acceptance scores, not independently established true accuracy or a universal model ranking. L2 Insights, User Profiles and document Resources are outside the scored scope. Existing indexes were reused; no new indexing cost or time was measured. Embedding tokens are excluded from generation totals; reasoning belongs in output usage. The two arms' recorded periods, latency and provider token counts do not establish controlled speed or monetary-cost ratios.

## 3. BEAM-10M

BEAM-10M tests ten capabilities across **200 questions from ten conversations at the 10-million-token scale**. OAM gained **14.15 percentage points in mean rubric credit** and **25 passes** over Hindsight's published result. The largest gains were in event ordering, temporal reasoning and information extraction. [BEAM benchmark paper](https://arxiv.org/abs/2510.27246) · [Per-capability and per-question results](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/beam10m-20261119)

| Measurement | OAM | Published Hindsight |
| --- | ---: | ---: |
| Mean rubric credit | **78.23%** | 64.08% |
| Pass rate (question score ≥ 0.5) | **86.0% (172/200)** | 73.5% (147/200) |
| Mean saved answer-context tokens, both recounted with `o200k_base` | **24,676.88** | 27,373.25 |
| Answer model | MiniMax M3.1 Flash adaptive | Gemini 3.1 Pro Preview |
| Judge model | MiniMax M3.1 Flash adaptive | Gemini 2.5 Flash Lite |

The comparison uses the same questions, reference answers, rubrics, answer and judge prompt templates, and scoring rules from AMB's archived evaluator. The replay matched the byte-identical archived prompt text for 200 OAM answers and 660 accepted judge calls with the corresponding inputs inserted. Three judge calls added a JSON example to the provider's response-format wrapper while preserving the judge text and score calculation. Hindsight's original request bodies are unavailable. [Archived evaluator](https://github.com/vectorize-io/agent-memory-benchmark/blob/5d5e8dbe56f140ad982ae61cbd73f43e100de706/src/memory_bench/dataset/beam.py) · [Published Hindsight artifact](https://github.com/vectorize-io/agent-memory-benchmark/blob/5d5e8dbe56f140ad982ae61cbd73f43e100de706/outputs/beam/hindsight/single-query/10m.json.gz)

Mean rubric credit averages the fractional scores of the 200 questions. Event ordering measures rank agreement after matching events, while the other categories average their rubric-item scores. Some archived prompts use reference-derived hints. Across the 180 questions outside event ordering, OAM earned **76.48% and 152/180 passes**, versus **64.35% and 136/180** for Hindsight. Model and memory-pipeline differences make this an end-to-end comparison.

The 180 current normal recalls used existing L1 indexes, included original passages and applied a **24,000-token native context cap**. Their answer contexts averaged **23,974.32 tokens** after adding the benchmark heading. Another 20 previously saved contexts from conversation 1 contribute to the full-run mean. Both systems' saved contexts were recounted with the same tokenizer, giving OAM **9.85% fewer tokens**. Hindsight reported 27,341.99 tokens with `cl100k_base`. Campaign billing and comparable indexing cost are unavailable. [OAM capture settings and paired context counts](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/beam10m-20261119)

The [earlier BEAM saved-context replay](https://github.com/rocke2020/open-agent-memory-benchmark/tree/308eef998091ec05aa2657b637fc37222e029672/eval_results/beam10m) used DeepSeek Flash and a separate scoring setup.

## 4. Inspect and recompute the saved results

The latest BEAM-10M CSV provides question IDs, scores and measured context counts. The November 17 LME and earlier BEAM `report.html` files show saved contexts and individual outcomes. [BEAM-10M paired outcomes](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/eval_results/beam10m-20261119/scores.csv) · [OAMB results index](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/eval_results/README.md)

To validate the archived HTML reports and their adjacent result shards from an OAMB checkout, run the saved-evidence tests.

```bash
git clone https://github.com/rocke2020/open-agent-memory-benchmark.git
cd open-agent-memory-benchmark
uv sync --locked
uv run pytest -q tests/test_published_oam_evidence.py tests/test_published_lme500_20261117.py tests/reporting/test_historical_report.py
```

The November 17 campaign's OAMB manifest pins its evaluated OAM commit and retrieval configuration. These tests validate saved measurements without fresh model calls.

The native OAMB LME workflow prepares the index through the OAM API, uses Fast recall and scores with OAMB's bundled prompts. The reported 8k campaign used retained indexes, an experimental ranking adapter and the pinned AMB prompts. Reproducing that retrieval path requires the campaign's adapter, frozen runtime and retained index state. Repeating its scoring requires the saved rendered contexts. [Native OAMB workflow](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/docs/native-oam.md)
