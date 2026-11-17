# Open Agent Memory evaluation results

## Overview

OAM records **95.2% (476/500)** on LongMemEval S with MiniMax M3.1 Flash native-adaptive answering and judging, normal recall and an **18,000-token retrieval-context limit**. The 497 scored cases average **11,373.58 saved context tokens**; three disputed-gold cases remain counted as incorrect in the unchanged 500-question denominator. The frozen retrieval source matches the published main source; the v0.0.3 image was not rerun.

The [latest LME-500 evidence](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/lme500-20261117) publishes saved contexts, visible answers, judgments, the exact main source commit and runtime file hashes. The earlier [466/463 paired comparison](https://github.com/rocke2020/open-agent-memory-benchmark/tree/308eef998091ec05aa2657b637fc37222e029672/eval_results/lme500) and [BEAM-10M evidence](https://github.com/rocke2020/open-agent-memory-benchmark/tree/308eef998091ec05aa2657b637fc37222e029672/eval_results/beam10m) remain separate recorded evaluations.

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
| Runtime provenance | 116 frozen runtime file hashes match final main; exact source commit and evaluation override are published in OAMB |

MiniMax M3.1 Flash is a low-priced model: its vendor lists **USD 0.15 per million input tokens and USD 0.60 per million output tokens**, with cached input at USD 0.015, checked on November 17, 2026. These are vendor list prices, not a reconciled bill for this Tencent Cloud AI run. [MiniMax official pricing](https://platform.minimax.io/docs/pricing/overview).

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

| Measurement | OAM | Hindsight saved-context replay |
| --- | --- | --- |
| Coverage | 200 questions; 20 per complete history | Same 200 questions |
| Mean rubric score | **61.56%** | **52.16%** |
| OAM selected answers scoring at least 0.5 | 142/200 (71.0%) | Not the mean-rubric comparison |
| OAM mean answer-visible context | 32,106.88 `cl100k_base` tokens | OAM uses 17.43% more context |
| OAM retrieval timing | 2,906.5 ms mean; 2,617.9 ms median; 5,527.6 ms p95 | Engine not rerun |

The BEAM results summarize recorded selected answers with DeepSeek Flash judging and preserve each answer's actual model settings in the evidence. They are separate from the current DeepSeek max answer profile. Hindsight's saved contexts were replayed without rerunning its indexing or retrieval.

BEAM assigns each question the mean of its rubric-item scores; its mean rubric score differs from its threshold pass rate. The retrieval timings come from 540 probes on histories 2–10, with none excluded. Complete comparable indexing-generation usage, full-campaign cost and indexing wall time are unavailable. The dataset revision is `9b2096193fe74e2837e4713e483351e19817773c`, with SHA-256 `c16643a7cc7d93c1ed5509568cdd9e4c387dcd60414e68dde740b54064e68ba8`. Recall is normal, using L1; L2 and Profile are excluded from scored contexts.

The [BEAM manifest](https://github.com/rocke2020/open-agent-memory-benchmark/blob/308eef998091ec05aa2657b637fc37222e029672/eval_results/beam10m/manifest.json) records per-history configuration, exact model bindings and available runtime revisions. Unknown inputs remain unknown. Context size, model settings and saved-context replay prevent attributing the score difference to the memory engine alone.

## 4. Inspect and recompute the saved results

The self-contained `report.html` files show full saved contexts and individual outcomes without model calls. Adjacent gzip JSON shards retain visible answers and judgments; the latest campaign marks carried outcomes explicitly. Recompute coverage, scores, context totals and evidence integrity from an OAMB checkout:

```bash
git clone https://github.com/rocke2020/open-agent-memory-benchmark.git
cd open-agent-memory-benchmark
uv sync --locked
uv run pytest -q tests/test_published_oam_evidence.py tests/reporting/test_historical_report.py
```

The latest campaign's manifest pins the final OAM main commit and the 116 matching frozen runtime files. Use that exact commit for a source build, and the adjacent evaluation configuration for the recorded retrieval settings. These commands validate saved measurements; no fresh model calls are needed.

The current [native OAM workflow](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/docs/native-oam.md) remains a separate LME fast-recall protocol. Changing only its model names does not reproduce the latest normal-recall/AMB-prompt campaign. New model output can vary; a successful run is not proof of score parity. Provider and infrastructure costs depend on the chosen configuration.
