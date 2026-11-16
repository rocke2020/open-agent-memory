# Open Agent Memory evaluation results

## Overview

OAM reaches **93.2% (466/500)** on LongMemEval S with MiniMax M3.1 Flash native-adaptive answers and DeepSeek Flash high-effort judging. DeepSeek Flash max-effort answers reach **92.6% (463/500)** on the same 500 questions and byte-identical saved OAM contexts. These are recorded L1/fast-recall pipeline measurements; publishing OAM 0.0.3 does not make them fresh measurements of its release image.

The [paired LME-500 evidence](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/lme500) contains the questions, full contexts, both answers and original judgments. The separate [BEAM-10M evidence](https://github.com/rocke2020/open-agent-memory-benchmark/tree/main/eval_results/beam10m) records a 61.56% mean rubric score with DeepSeek Flash over ten complete histories. OAMB is identified by its Git commit; OAM keeps its product version.

## 1. LongMemEval S: all 500 questions

| Answer model | Answer reasoning | Judge | Accepted answers |
| --- | --- | --- | --- |
| `minimax-m3.1-flash` | Native adaptive | DeepSeek Flash, high | **466/500 (93.2%)** |
| `deepseek-flash` | Max | DeepSeek Flash, high | **463/500 (92.6%)** |

Every canonical question ID occurs once in each arm. MiniMax M3.1 Flash uses native adaptive answer reasoning; DeepSeek Flash uses max answer reasoning. The saved responses retain their original model settings and fixed-reference judgments.

Both arms are accepted on 448 questions, MiniMax alone on 18, DeepSeek alone on 15, and neither on 19. The 0.6 percentage-point difference is three questions in this recorded comparison. Fixed references and original verdicts remain unchanged, including disputed source/gold cases. These are judge acceptance scores, not independently established true accuracy or a universal model ranking.

| Recorded setting | Value |
| --- | --- |
| Dataset | Cleaned LongMemEval S, 500 canonical IDs |
| Dataset revision | `98d7416c24c778c2fee6e6f3006e7a073259d48f` |
| Dataset SHA-256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` |
| Recall and representation | Fast; L1; graph, Insights and Profile disabled |
| Exact saved context tokens | 5,740,548 total; 11,481.10 mean |
| Independent context counter | tiktoken 0.14.0, `o200k_base.encode_ordinary` |
| Query embeddings | Qwen3-Embedding-0.6B, 1,024 dimensions, bundled Qwen tokenizer |
| Indexing | Existing completed indexes reused; no new indexing time or cost measured |
| Runtime provenance | Saved context/source identities and known revisions are recorded in the LME manifest; no single extraction or released-image identity is inferred |

L2 Insights, User Profiles and document Resources are outside the scored scope. The two models' answers and judgments were collected in different periods; recorded latency and provider token counts do not establish controlled speed or monetary-cost ratios. Embedding tokens are excluded from generation totals; reasoning is included in output usage.

## 2. BEAM-10M

| Measurement | OAM | Hindsight saved-context replay |
| --- | --- | --- |
| Coverage | 200 questions; 20 per complete history | Same 200 questions |
| Mean rubric score | **61.56%** | **52.16%** |
| OAM selected answers scoring at least 0.5 | 142/200 (71.0%) | Not the mean-rubric comparison |
| OAM mean answer-visible context | 32,106.88 `cl100k_base` tokens | OAM uses 17.43% more context |
| OAM retrieval timing | 2,906.5 ms mean; 2,617.9 ms median; 5,527.6 ms p95 | Engine not rerun |

The BEAM results summarize recorded selected answers with DeepSeek Flash judging and preserve each answer's actual model settings in the evidence. They are separate from the current DeepSeek max answer profile. Hindsight's saved contexts were replayed without rerunning its indexing or retrieval.

BEAM assigns each question the mean of its rubric-item scores; its mean rubric score differs from its threshold pass rate. The retrieval timings come from 540 probes on histories 2–10, with none excluded. Complete comparable indexing-generation usage, full-campaign cost and indexing wall time are unavailable. The dataset revision is `9b2096193fe74e2837e4713e483351e19817773c`, with SHA-256 `c16643a7cc7d93c1ed5509568cdd9e4c387dcd60414e68dde740b54064e68ba8`. Recall is normal, using L1; L2 and Profile are excluded from scored contexts.

The [BEAM manifest](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/eval_results/beam10m/manifest.json) records per-history configuration, exact model bindings and available runtime revisions. Unknown inputs remain unknown. Context size, model settings and saved-context replay prevent attributing the score difference to the memory engine alone.

## 3. Inspect and recompute the saved results

The self-contained `report.html` files show full contexts and individual outcomes without model calls. The adjacent gzip JSON shards retain both LME arms' full context text and scoring evidence. Recompute coverage, scores, context totals and evidence integrity from an OAMB checkout:

```bash
git clone https://github.com/rocke2020/open-agent-memory-benchmark.git
cd open-agent-memory-benchmark
git rev-parse HEAD  # Record the benchmark source revision.
uv sync --locked
uv run pytest -q tests/test_published_oam_evidence.py tests/reporting/test_historical_report.py
```

These commands validate saved measurements. For a new run against your deployment, follow the [native OAM workflow](https://github.com/rocke2020/open-agent-memory-benchmark/blob/main/docs/native-oam.md), choose the DeepSeek max or MiniMax native-adaptive LME profile, and record actual OAM revision, settings, usage and results. New model output can vary; a successful run is not proof of score parity. Provider and infrastructure costs depend on your configuration.
