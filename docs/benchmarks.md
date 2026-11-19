# Open Agent Memory evaluation results

## Overview

OAM exceeds the highest recorded Hindsight scores in AMB's published results for **LongMemEval S** and **BEAM-10M**. On BEAM-10M, OAM earns **78.23% mean rubric credit** and **86.0% passes**, versus **64.08%** and **73.5%**, with **9.85% fewer saved context tokens**. Both OAM runs use low-cost MiniMax M3.1 Flash adaptive for answering and judging. [Pinned AMB results manifest](https://github.com/vectorize-io/agent-memory-benchmark/blob/c963dba91dfe6c459f64bb871c5dafb2f3de1bf1/results-manifest.json)

OAM records **95.2% (476/500)** on LongMemEval S with MiniMax M3.1 Flash native-adaptive answering and judging, normal recall and an **18,000-token retrieval-context limit**. The 497 scored cases average **11,373.58 saved context tokens**; three disputed-gold cases remain counted as incorrect in the unchanged 500-question denominator.

Both comparisons use the same questions, references and scoring rules as the published Hindsight runs, with byte-identical answer and judge prompt templates from pinned AMB sources. The evaluations measure recorded end-to-end systems with distinct models and memory pipelines. [LongMemEval evidence](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/lme500-20261117) · [BEAM-10M evidence](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/beam10m-20261119)

## 1. Latest LongMemEval S: 476/500 at 18k

The hard 100 contribute **83/100** and the remaining 400 contribute **393/400**, totaling **476/500**. All 500 canonical question IDs and fixed gold answers remain unchanged. There are 497 current answer/judge pairs and three explicitly carried incorrect outcomes; the latter have no newly generated context, answer or judgment. Existing completed indexes were reused without indexing.

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
| Context limit | 18,000 tokens, an evaluation override; the product normal default is 24,000 |
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
| OAM, MiniMax M3.1 Flash answer and judge | **476/500 (95.2%)** | **11,373.58** across 497 contexts |
| Hindsight, Gemini 3.1 Pro Preview answer and Gemini 2.5 Flash Lite judge | **473/500 (94.6%)** | **43,124.55** across 500 contexts |

OAM's recorded score is 0.6 percentage points higher and its saved formatted contexts are **73.63% smaller**. The Hindsight result is the [AMB artifact at revision `7021c59`](https://github.com/vectorize-io/agent-memory-benchmark/blob/7021c59c3497d1dd08d61f12fa4629be983fcfb1/outputs/longmemeval/hindsight/rag/s.json.gz). Different answer and judge models, context populations and full pipelines prevent attributing the difference to retrieval alone. Hindsight's actual server revision and complete resolved settings are not established by that artifact. Saved formatted context counts are not exact model-input or billing counts; the Hindsight answer path can use raw recall JSON instead.

The change from the earlier 466/500 is a net gain of ten accepted answers: 22 previously incorrect cases become correct and 12 previously correct cases become incorrect, including one carried disputed-gold No. Retrieval, formatting, prompts and judge model changed together. This is evidence of the resulting pipeline, not an isolated ten-question causal gain from retrieval. A same-selected-evidence formatting comparison reduces total context tokens by **332,235 (5.55%)**, while keeping all **4,137,965 evidence-body tokens** unchanged.

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

The [latest BEAM-10M paired outcomes](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/eval_results/beam10m-20261119/scores.csv) provide question IDs, scores and measured context counts. The LME and earlier BEAM `report.html` files show saved contexts and individual outcomes. Validate those HTML reports and their adjacent result shards from an OAMB checkout:

```bash
git clone https://github.com/rocke2020/open-agent-memory-benchmark.git
cd open-agent-memory-benchmark
uv sync --locked
uv run pytest -q tests/test_published_oam_evidence.py tests/test_published_lme500_20261117.py tests/reporting/test_historical_report.py
```

The latest campaign's OAMB manifest records the evaluated OAM commit. Use that exact commit for a source build, and the adjacent evaluation configuration for the recorded retrieval settings. These commands validate saved measurements; no fresh model calls are needed.

The current [native OAM workflow](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/docs/native-oam.md) remains a separate LME fast-recall protocol. Changing only its model names does not reproduce the latest normal-recall/AMB-prompt campaign. New model output can vary; a successful run is not proof of score parity. Provider and infrastructure costs depend on the chosen configuration.
