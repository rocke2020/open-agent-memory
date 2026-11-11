# Open Agent Memory v0.0.1 evaluation results

## Overview

OAM v0.0.1 reports the completed development evaluations covering 500 LongMemEval S questions and 200 BEAM-10M questions. LME-500 reached **94.0% (470/500)** with `deepseek-v4.1-flash` and **12.4K average answer-context tokens**. BEAM-10M reached a **61.56% mean rubric score** with DeepSeek Flash over ten complete histories, each exceeding ten million tokens; its saved answer/judge bindings use `deepseek-flash`. These are historical pipeline results, including a recorded single-retry policy, rather than fresh measurements of the released image or a ranking against other systems.

We deliberately chose DeepSeek Flash to control evaluation costs and represent everyday agent use with memory, rather than maximize the headline score with a top model. These scores measure retrieval together with answer generation and judging. Complex questions also depend on the answer model's reasoning, so comparisons need the model bindings and scoring policy alongside the memory results. The archived runs do not isolate how much a stronger answer model would improve either score.

The public evidence includes every question's saved context, answer, judgment and selected retry. The [LME-500 report](https://github.com/rocke2020/open-agent-memory-benchmark/tree/32bf1921f0db7c8f0008428b06b637c1f93ff385/eval_results/lme500) and [BEAM-10M report](https://github.com/rocke2020/open-agent-memory-benchmark/tree/32bf1921f0db7c8f0008428b06b637c1f93ff385/eval_results/beam10m) provide the full evidence and limitations.

## 1. Results

“First pass” uses the original answer. “Policy final” uses the recorded retry when present, even if its score is lower. An eligible failure received at most one max-effort answer retry on unchanged context and prompt, followed by high-effort judging. No best-of-attempt selection is applied.

| Measurement | LME-500 | BEAM-10M |
| --- | --- | --- |
| Coverage | 500 unique questions | 200 unique questions; 20 per complete history |
| First-pass quality | 90.6% accuracy (453/500) | 59.89% mean rubric score |
| Policy-final quality | **94.0% accuracy (470/500)** | **61.56% mean rubric score** |
| Policy-final questions scoring at least 0.5 | Not the LME scoring rule | 142/200 (71.0%) |
| Mean saved answer-visible context | 12,402.62 tokens | 32,106.88 tokens |
| Context counter | `o200k_base` | `cl100k_base` |
| Retrieval timing | Approximately 748.25 ms weighted raw mean | 2,906.5 ms mean; 2,617.9 ms median; 5,527.6 ms p95, from 540 probes on histories 2–10; none excluded |
| Selected-history indexing generation usage | 618,864,325 tokens; excludes embeddings | No complete comparable full-campaign subtotal |
| Complete campaign cost and indexing wall time | Unavailable | Unavailable |

BEAM assigns each question the mean of its rubric-item scores; its mean rubric score is different from its threshold pass rate. Context tokens, provider generation usage and monetary cost are separate measurements. Reasoning is included in output usage. Timing samples came from different runtime conditions and do not establish a controlled speed comparison. Neither evaluation measures L2 Insights, User Profiles or document Resources.

## 2. Models and configuration identity

| Setting | LME-500 | BEAM-10M |
| --- | --- | --- |
| Dataset revision | `98d7416c24c778c2fee6e6f3006e7a073259d48f` | `9b2096193fe74e2837e4713e483351e19817773c` |
| Dataset SHA-256 | `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442` | `c16643a7cc7d93c1ed5509568cdd9e4c387dcd60414e68dde740b54064e68ba8` |
| Recall | `fast` | `normal` |
| Learned representation | L1; L2 and Profile disabled | L1; L2 excluded from scored contexts; Profile disabled |
| Generation model | `deepseek-v4.1-flash` | DeepSeek Flash; saved answer/judge bindings use `deepseek-flash` |
| Reasoning | Extraction low; first answer and judge high; eligible answer retry max | Extraction low; first answer and judge high; eligible answer retry max |
| Embeddings | Qwen3, 32K input limit | Qwen3, 32K input limit |
| Runtime identity | Revision `1d8554b3e9fcaf8b169b7c542146dfd6e074f361` for the first 290 questions; final 210 revision unavailable | Histories 2–10: API `5f34c5d`, worker `74f84ed24a28199fbb317ed94f29997bae33194c`; history 1 revision unavailable |

The [LME manifest](https://github.com/rocke2020/open-agent-memory-benchmark/blob/32bf1921f0db7c8f0008428b06b637c1f93ff385/eval_results/lme500/manifest.json) records cohort identities and unavailable metadata. The [BEAM manifest](https://github.com/rocke2020/open-agent-memory-benchmark/blob/32bf1921f0db7c8f0008428b06b637c1f93ff385/eval_results/beam10m/manifest.json) records per-history configuration and evaluator SHA-256 values, exact answer/judge bindings and source receipts. Its two configuration hashes are `aa8884438e01509f617027ffe9ba9853c092e22fe277be0d677a218ead05b1ac` and `51fd56aef68c7ee5913d17da75b5d998c67c0f3d8e5407f0604208a13f0d8a3c`. Unknown historical inputs remain unknown; no single configuration or released-image identity is inferred.

## 3. Reproduce the published measurements

Recompute question coverage, first-pass and policy-final quality, retry selection and exact context counts from the public archive without running OAM or calling model providers. Python and `uv` are required; dependency installation needs network access.

```bash
git clone https://github.com/rocke2020/open-agent-memory-benchmark.git
cd open-agent-memory-benchmark
git checkout 32bf1921f0db7c8f0008428b06b637c1f93ff385
uv sync --locked
uv run pytest -q tests/test_published_oam_evidence.py
```

Download either report's `report.html` and open it locally to inspect full contexts and individual outcomes. Python's standard `gzip` and `json` modules can also read the adjacent evidence shards.

These commands validate saved measurements. They do not rerun historical indexing or reproduce unavailable settings. For a new evaluation against your deployment, follow the benchmark repository's [native OAM workflow](https://github.com/rocke2020/open-agent-memory-benchmark/blob/32bf1921f0db7c8f0008428b06b637c1f93ff385/docs/native-oam.md); record its actual revision, settings, usage and results separately. Provider and infrastructure costs for a new run depend on your configuration.
