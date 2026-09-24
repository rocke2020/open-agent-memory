# Open Agent Memory Acceptance

## Overview

Release claims require fail-capable evidence through the real public path. Unit tests prove deterministic policy, integration tests prove PostgreSQL/provider boundaries, and a small authorized end-to-end run proves the inspectable artifact; one layer never substitutes for another.

## 1. Steps 1–4 Foundation Gate

The foundation gate requires a clean focused package, self-contained public documents, closed configuration, versioned migrations/RLS, credential/bootstrap behavior, durable operation/work claims, bounded local parsers, model capability probes, OpenAPI DTOs, operation routes, conformance fixtures, and a same-origin Inspector shell. It explicitly does not claim indexing or retrieval.

The repository records deterministic unit/contract coverage and PostgreSQL integration coverage for this foundation. Integration tests are skipped unless both `OAM_TEST_POSTGRES_DSN` and `OAM_TEST_POSTGRES_ISOLATED=1` identify an explicitly isolated database. Bootstrap tests create separate databases on that isolated server and retain them for diagnosis; the test operator needs database-creation authority. Current foundation evidence includes a real PostgreSQL 18 run, but does not establish production deployment or replica recovery.

Authentication acceptance requires an isolated API/SQL matrix for `api_key` root denial, same-user and cross-user keys, rootless `dev` loopback startup and keyed-database downgrade rejection, plus `trusted` local assertions, first-seen user registration, tenant/user mismatches, nonlocal root-key proof and root-only administration. A candidate nonlocal trusted deployment must separately prove that its gateway authenticates callers, replaces spoofed identity headers and blocks direct listener access; application tests alone cannot pass that deployment gate. OIDC and LDAP are outside v0.0.1.

The dated commands, artifact probe, and explicit exclusions are recorded in [Steps 1–4 foundation evidence](foundation-evidence.md).

## 2. Step 5 Core Indexing and Recall Gate

This gate evaluates the indexing and recall core independently of L3 user Profiles. It uses invented medical data: eligible conversations plus a separate HTML report with dates, numbers, units, and table relationships. The public path must commit originals and receipts, produce L0/L1/L2 and cited Resource passages/abstract state, and return Memory and Evidence through direct fast/mid/deep recall. Prove the public fast path first, then complete mid and deep within the same gate. The report and its assistant-derived answer must remain excluded from Learned Memory and eligible Profile inputs.

All three modes use one pipeline and resolve supported query dates before candidate retrieval when no explicit ranking window is supplied. Fast fuses dense, one FTS-plus-narrow-literal lexical arm and optional temporal; mid and deep add ordinary graph and L2 hierarchy for at most five fusion arms, with only parameter budgets/limits differing. Verify through public HTTP that `LAB-204` does not match `LAB-2045` and an intentional `"morning shift"` match is contiguous; inspect fusion inputs to prove that a unit found by FTS and literal matching receives one lexical vote and no `anchors` arm reaches RRF. Verify bounded entity/semantic links, two-hop causal paths, L2 sibling paths and automatic reranking when its model binding is supplied. Verify English, Chinese and supported Japanese absolute/relative expressions, explicit-window precedence, ambiguous-date diagnostics that preserve ordinary recall, timezone boundaries, and out-of-window direct evidence remaining eligible plus a graph-only temporal discovery through public HTTP. Verify configured reranker calls and non-degraded RRF-only behavior when absent for both mid and deep; fast always skips reranking. Require a generated, persisted, retrievable L2 Insight with one validated descriptive subtype and exact L1-to-L0 support, incremental synthesis, and distinct valid `no_insight` versus malformed, empty, refused, or failed outcomes. No recall mode may generate content; each must enforce its request/shared-turn deadlines and disclose failure or partial coverage. All three modes must be verified through the public API before this gate passes. Progressive LME-6, LME-30 and LME-60 evaluations remain required; the small functional probe is not a benchmark gate.

Index the fixtures once, then compare fast, mid, and deep on the same eligible index, query set, and caller context-token budget with L3 disabled and `profile_sections: []`. Expected facts/passages/support citations, including evidence reached by the graph arm, make the checks fail-capable. Retain requests/responses and fixture/config identity; report indexing usage once, per-mode retrieval quality against expected evidence, context tokens, recall usage, latency, and failures. Prioritize fast/mid quality and latency as the common real-use modes; deep results cannot substitute for their acceptance. Deep need not outperform the other modes on every query. Final-answer accuracy requires a separate external evaluator.

Step 6 implements only L3 user Profiles on this evaluated core: stored `main`, `preferences`, and `experiences`, eligible L0/L1/L2 and explicit Profile Resource inputs, dirty-but-not-automatically-generated behavior, read-only `GET`, asynchronous changed-input refresh, and unchanged `200` reuse with zero generation calls. Verify Profile exclusion of report-derived content and preservation of still-eligible prior fields after failed refresh.

The agent-integration scenario starts with host-run fast recall before the agent invocation. During the ReAct loop, the agent can request any of fast, mid, or deep again. The HTTP contract must preserve the chosen mode and never perform an automatic escalation or impose a fixed sequence.

LongMemEval performance acceptance covers fast and mid only, in order LME-6, LME-30 and LME-60. Both modes must meet the declared four-metric gates on the same indexed histories. Deep's functional conformance stays in the core gate, while its performance evaluation is deferred to a harder dataset.

## 3. Complete Release Gate

v0.0.1 extends the completed core and L3 gates to PDF/image, batch/planner integration over the fast/mid/deep core, source revision/deletion, explain/diff/recorded replay, portable export, browser/API parity, multi-API/multi-worker recovery, and external OAMB screening. The final candidate image is built for Linux AMD64 and ARM64 and the complete matrix reruns against its immutable digest before source/image publication.

## 4. Evidence Rules

LLM probes expecting visible content allow at least 1,024 output tokens and reject length termination, empty/malformed content, and reasoning-only output. Embeddings must be finite and dimensionally consistent. Browser/static presence, health, nonempty output, schema compatibility, and passing legacy tests prove only their named boundaries.

Default tests use deterministic fakes and isolated ephemeral databases. Paid endpoints, sensitive data, destructive fault injection, external deployment, repository visibility changes, and image publication require explicit authority and verified targets.
