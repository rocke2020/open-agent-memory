# Open Agent Memory evaluation results

[Documentation](README.md) · [简体中文](zh/benchmarks.md)

## Overview

On **LongMemEval S**, OAM's 8k Fast run with full-text search (FTS) and BM25 scored **95.2% (476/500)** with **7,989.54 mean saved context tokens**. The completed 18k Normal FTS run scored **95.6% (478/500)** with **11,375.12 mean tokens**. The 8k run used **29.8% less context on average**. Hindsight's published result was **94.6% (473/500)** with **43,124.55 mean tokens**. Each score and context mean covers all 500 questions. [Hindsight's pinned AMB result](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/outputs/longmemeval/hindsight/rag/s.json.gz)

On **BEAM-10M**, OAM's November 27 experimental Normal run scored **85.24% mean rubric credit (179/200 passes)** with **11,972.66 mean context tokens** under a **12,000-token cap**. Past.dev published **85.03%** and Hindsight published **64.08%** using their own evaluators. In a separate replay with the same evaluator and cap, OAM scored **85.49%** versus **80.54%** on Past.dev's saved retrieval. [BEAM-10M results](#3-beam-10m)

On **LegalBench-RAG mini**, OAM recovered **88.45% of annotated evidence characters** in the top five results, compared with Hindsight's published **87.22%**. Agent Memory Benchmark formatted context averaged **3,702** tokens for OAM and **3,821** for Hindsight across **776 queries**. [LegalBench-RAG mini results](#legalbench-rag-mini)

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
| Context limit | 18,000 tokens, a pinned evaluation override; the v0.2.1 Normal default is 16,000 |
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

BEAM-10M tests ten memory capabilities across **200 questions from ten conversations at the 10-million-token scale**. Mean rubric credit averages the fractional scores across all questions. A score of at least 0.50 counts as a pass. The November 27, 2026 OAM result exceeded Past.dev's published score by **0.21 percentage points** with the same pass count. It exceeded Hindsight's published score by **21.16 points and 32 passes**, using **56.26% fewer mean context tokens**. [BEAM benchmark paper](https://arxiv.org/abs/2510.27246) · [Past.dev leaderboard](https://past.dev/benchmarks/beam)

### Full split and published baselines

| Run | Mean rubric credit | Passes | Mean answer-context tokens (`o200k_base`) |
| --- | ---: | ---: | ---: |
| OAM experimental Normal, 12,000-token cap | **85.24%** | **179/200** | **11,972.66** |
| Past.dev published, GPT-5.6 Luna | 85.03% | 179/200 | 10,467.45 |
| Hindsight published, Gemini 3.1 Pro Preview answer and Gemini 2.5 Flash Lite judge | 64.08% | 147/200 | 27,373.25 |

OAM used Normal retrieval over L1 facts and original passages, with L2 Insights and Profile disabled. DeepSeek Flash indexed the source material with low reasoning effort. The evaluation used the benchmark cutoff for all 20 knowledge-update questions and an experimental source recovery step in the evaluation harness. Exact rendered evidence ranged from **11,220 to 12,000 tokens**. Ordinary Normal retrieval in the same campaign scored **84.49% (178/200)**. The experimental step added one pass. Removing a separate disputed half-credit judgment would put the experimental score at **84.99% (179/200)**.

OAM used Past.dev's pinned answer and judge prompts, rubrics and score reducer, with adaptive MiniMax M3.1 Flash in both roles. Past.dev's published run used GPT-5.6 Luna and strict JSON-schema judge requests. Hindsight used the archived Agent Memory Benchmark (AMB) protocol. The published rows compare complete systems and evaluation setups. [Pinned evaluator](https://github.com/pastdotdev/benchmarks/blob/b3971de539aac109448f00ca124b023f2a89a7cb/beam/scoring.py) · [Past.dev result](https://github.com/pastdotdev/benchmarks/blob/b3971de539aac109448f00ca124b023f2a89a7cb/beam/results/10m/summary.json) · [Hindsight result](https://github.com/vectorize-io/agent-memory-benchmark/blob/5d5e8dbe56f140ad982ae61cbd73f43e100de706/outputs/beam/hindsight/single-query/10m.json.gz)

Context counts cover retrieved evidence shown to the answer model. Past.dev's published mean was recounted from the compact JSON `recall` value for each of its 200 questions. Those contexts ranged from **8,086 to 16,318 tokens**. [Past.dev per-question archive](https://github.com/pastdotdev/benchmarks/blob/b3971de539aac109448f00ca124b023f2a89a7cb/beam/results/10m/results.jsonl)

<a id="beam-10m-matched-replay"></a>

### Matched Past.dev replay

Both arms used the same 200 questions, prompts, scoring rules, adaptive MiniMax M3.1 Flash answer and judge settings, and a **12,000-token context cap**. The Past.dev arm replayed its saved retrieval, keeping the longest prefix of complete ranked records that fit the cap. OAM led by **4.95 percentage points and eight passes**, using **1,831.12 more mean context tokens** under the shared cap.

| Replayed answer context | Mean rubric credit | Passes | Mean answer-context tokens (`o200k_base`) |
| --- | ---: | ---: | ---: |
| OAM experimental Normal | **85.49%** | **180/200** | 11,972.66 |
| Past.dev saved retrieval, capped | 80.54% | 172/200 | 10,141.54 |

The replay judge awarded half credit to an OAM answer that had scored zero in the original run, moving it over the pass threshold. The original score remains 85.24%. The pinned event-ordering prompt contains the reference order. Across the other 180 matched questions, OAM scored **84.34% (160/180)** and capped Past.dev scored **78.63% (152/180)**. [Pinned event-ordering prompt](https://github.com/pastdotdev/benchmarks/blob/b3971de539aac109448f00ca124b023f2a89a7cb/beam/exabase_prompts.py#L37-L46)

<a id="november-19-beam-10m"></a>

### November 19 OAM and Hindsight comparison

In the earlier run, OAM gained **14.15 percentage points** and **25 passes** over Hindsight's published result. The largest gains were in event ordering, temporal reasoning and information extraction. [Archived per-capability and per-question results](https://github.com/rocke2020/open-agent-memory-benchmark/tree/b24c8037adcb97666be63944841b7581d50141bf/eval_results/beam10m-20261119)

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

<a id="legalbench-rag-mini"></a>

## 4. LegalBench-RAG mini

LegalBench-RAG mini measures retrieval from legal documents. OAM and Hindsight searched the same **714 plain-text contracts and policies** under neutral document IDs and scored all **776 queries** with the pinned Agent Memory Benchmark (AMB) scorer. Character Recall@5 is the share of marked evidence characters recovered in the first five hits, averaged across queries. Character Precision@5 is the share of located returned characters that overlap marked evidence. The protocol scored retrieval directly, with zero answer-generation or judge calls. [Benchmark paper](https://arxiv.org/abs/2408.10343) · [Pinned scorer](https://github.com/vectorize-io/agent-memory-benchmark/blob/01bd83f3fa5245d6be3986cfab81fdd828f26acd/src/memory_bench/dataset/knowledge.py) · [Published Hindsight archive](https://raw.githubusercontent.com/vectorize-io/agent-memory-benchmark/01bd83f3fa5245d6be3986cfab81fdd828f26acd/outputs/legalbench-rag/hindsight-no-filenames/retrieval/mini.json.gz)

| Measurement | OAM, document-guided Normal | Hindsight, published neutral-filename run |
| --- | ---: | ---: |
| Character Recall@5 | **88.45%** | 87.22% |
| Character Precision@5 | **4.05%** | 3.83% |
| Mean AMB-formatted context tokens | **3,702** | 3,821 |
| Retrieval p50 | 3.388 s | 3.251 s |
| Retrieval p95 | **5.693 s** | 7.984 s |
| Queries with zero recall | **72** | 84 |

The November 25, 2026 UTC OAM run used a fresh index, BGE small English embeddings with the document's first 40 words prepended to each passage, and DeepSeek Flash reranking. The document-guided Normal path considered up to 144 candidates and returned at most five hits. OAM's native context includes citation metadata and averaged **3,993 tokens** under its **4,096-token cap**. The table uses the AMB-formatted count for both systems. Retrieval latencies come from the two recorded environments.

The corresponding passage-body embedding run scored **86.41% Recall@5**. With source openings, MAUD and ContractNLI gained recall and PrivacyQA regressed. Document guidance and source-opening embeddings were benchmark overrides. Both are off in the default product configuration. An offline check reproduced all 776 OAM scores from the saved native replies. [Product defaults](../configs/oam.yaml)

## 5. Inspect and recompute the saved results

The public November 19 BEAM-10M CSV provides question IDs, scores and measured context counts. The November 27 OAM results above are reported in aggregate. The November 17 LME and earlier BEAM `report.html` files show saved contexts and individual outcomes. [November 19 BEAM-10M paired outcomes](https://github.com/rocke2020/open-agent-memory-benchmark/blob/b24c8037adcb97666be63944841b7581d50141bf/eval_results/beam10m-20261119/scores.csv) · [OAMB results index](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/eval_results/README.md)

To validate the archived HTML reports and their adjacent result shards from an OAMB checkout, run the saved-evidence tests.

```bash
git clone https://github.com/rocke2020/open-agent-memory-benchmark.git
cd open-agent-memory-benchmark
uv sync --locked
uv run pytest -q tests/test_published_oam_evidence.py tests/test_published_lme500_20261117.py tests/reporting/test_historical_report.py
```

The November 17 campaign's OAMB manifest pins its evaluated OAM commit and retrieval configuration. These tests validate saved measurements without fresh model calls.

The native OAMB LME workflow prepares the index through the OAM API, uses Fast recall and scores with OAMB's bundled prompts. The reported 8k campaign used retained indexes, an experimental ranking adapter and the pinned AMB prompts. Reproducing that retrieval path requires the campaign's adapter, frozen runtime and retained index state. Repeating its scoring requires the saved rendered contexts. [Native OAMB workflow](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/docs/native-oam.md)
