# Indexing and retrieval

[Documentation](README.md) · [简体中文](zh/indexing-retrieval.md)

## Overview

OAM preserves eligible interaction originals, learns individually supported records, consolidates supported Insights, and returns cited context to a caller that owns the final answer. Documents remain a separate Evidence pool. PostgreSQL stores originals, projections, policy, operations and worker ownership; stateless APIs and durable workers share that authority.

This document describes indexing and recall in v0.2.1. Runtime contracts remain in [API](api.md), operator settings in [configuration](../configs/README.md), and verification boundaries in [acceptance](acceptance.md).

Post-v0.2.1 development adds an optional [decision-model client](../src/oam/models/decision.py) for typed Jev/System One judgments through the shared [runtime](../src/oam/models/runtime.py), [binding resolver](../src/oam/models/bindings.py) and [preflight](../src/oam/models/preflight.py). Its activation, routing and failure behavior are documented in [operations](operations.md#optional-decision-model). Generation still owns semantic decisions and prose in the source → L1 → L2 and explicit Profile-refresh flows. L2 support-only shortcuts and Profile impact gates remain proposed consumers subject to existing publication and budget rules.

## 1. Representations and ownership

| Representation | Meaning | Authority |
|---|---|---|
| L0 | Accepted interaction originals and passages | Immutable source revision and original bytes |
| L1 | Individually identified learned facts, preferences and experiences | Exact quoted support in eligible L0 |
| L2 | Generated, persisted Insights over eligible L1 | Current support IDs and revisions, with L0 provenance |
| L3 | One stored, source-backed User Profile | Explicit refresh over eligible learned inputs and declared Profile Data |
| Evidence Resource | HTML/PDF/image originals, extracted passages and generated abstracts | Current immutable document version |
| Profile Data | Owner-declared structured fields | Registered subtype and current immutable version |

A source version, index generation and tenant/user identity qualify every derived record. Equal embedding dimensions alone do not establish that two embeddings occupy the same semantic space. A Resource abstract is not an Insight, and Evidence Resources never automatically become learned or Profile inputs. Hosts declare learning eligibility for retained messages, including document-assisted assistant output.

## 2. Acceptance and durable work

1. Derive tenant and personal authority from authentication, validate the user, input schema, limits and enabled capabilities, and apply the idempotency contract.
2. Commit accepted original bytes, the source revision and an operation receipt before calling an external model. The receipt distinguishes acceptance from source searchability, L1 readiness and L2 readiness.
3. Let a worker claim bounded work through PostgreSQL with an owner, lease and monotonic fence. Perform remote I/O outside database transactions.
4. Publish a completed stage only after rechecking its source version, generation, eligibility, cancellation and current ownership. Stale owners cannot publish or account work as current.
5. Reuse completed stages only when their input identity matches. Preserve ambiguous external outcomes for explicit reconciliation; do not invent a response or blindly replay uncertain paid work.

The runtime uses separate non-owner API, queue and content roles with forced row-level security. An application restart loses no already accepted original or durable receipt. Automatic plugin capture before the backend accepts a receipt has a different boundary: it can be lost if the host exits, because plugins have no durable capture queue.

Source: [admission/runtime](../src/oam/api/runtime.py), [workers](../src/oam/workers/), [schema and ownership](../migrations/).

## 3. Interaction indexing

Indexing selects learning-eligible messages, preserves L0 passages, chunks deterministically and extracts independently supported L1 records in bounded groups. Extraction can read bounded prior L1 references for continuity; references do not authorize unsupported new claims. Quote/span validation binds learned content to the original message. Entity resolution retains original names, witnessed aliases and source chunks while distinguishing recurring identity from namesakes.

Completed groups are embedded and published incrementally with memberships, directed relations and time evidence. Temporal and semantic link work settles under the same eligibility and publication rules. Progress and partial coverage remain explicit; a failed later stage does not justify relabeling an earlier ready stage as complete end-to-end.

When the optional per-source generation budget is configured, reservations bound extraction and Insight work before dispatch; the default configuration leaves that guard disabled. Input, output, reasoning and failed/retry usage belong to generation accounting; embedding usage is recorded separately. Output ceilings contribute to reservations, not a promise that every call consumes its ceiling. Budgets and stage readiness cannot be inferred from call count alone.

Source: [source worker](../src/oam/workers/index_source.py), [indexing](../src/oam/indexing/), [configuration](../configs/oam.yaml).

## 4. Insight consolidation

L2 is a required supported representation whose purpose is useful memory, not exhaustive rewriting of every L1 fact. Consolidation reads existing L1 and can be enabled after L1 indexing without extracting the same originals again. Ready sources enter a fixed per-user consolidation window with a default duration of 24 hours. An isolated functional fixture can shorten the window without changing the default.

The configured seed policy selects bounded eligible L1 inputs. The lexical part of existing-Insight target lookup uses per-term FTS within the current index generation. The public recall ranker setting does not change this lookup. [Insight target lookup](../src/oam/indexing/consolidation.py) The model returns supported candidate changes or a valid no-Insight result; malformed, empty, refused and failed output remain separate outcomes. Publication validates current support and revisions, preserves stable Insight identity where an update applies, and records its descriptive subtype. Mechanical support IDs are grounding checks; paraphrases of one event do not establish independent recurrence.

Every published Insight has a concise, human-readable title naming its enduring subject. CREATE and UPDATE provide a nonblank title of at most 120 characters; an update normally preserves the previous title, changing it only when supported evidence changes or clarifies the subject. Occurrence dates, delivery histories and temporary status belong in the body unless they identify the subject itself. Ordinary support-only updates preserve the title exactly. A full repair after support removal generates its title from surviving evidence and may rename the subject. The title is stored with the search unit's presentation metadata and returned by memory inspection and export; the separate retrieval label retains its existing embedding role. Existing records reuse their saved subject label, or a bounded content preview when no label exists, without generation or reindexing.

New eligible learned input can change L2 and mark an existing Profile stale. Optional per-user [Profile schedules](profiles.md) can request automatic refresh; all automatic triggers default off. Source exclusion and exact memory removal invalidate affected support before later workers can publish from it.

Source: [Insight implementation](../src/oam/indexing/), [consolidation worker](../src/oam/workers/l2_index.py).

### Automatic consolidation windows

Automatic consolidation collects ready sources for one tenant/user and index generation into a fixed window. Its first admission anchors the configured delay, with a 24-hour default; later sources join only strictly before its immutable cutoff. Arrivals at or after cutoff start the next window even if a worker starts late. With the related seed policy, bounded requests may combine related new facts from several member sources, while unrelated or unpacked facts remain pending. Supporting historical facts provide context and retain their previous completion state.

The window owns frozen group inputs and saved responses, but each source retains separate readiness and generation-budget responsibility. Shared call allowance is apportioned by serialized new-seed bytes with deterministic conserved integer rounding, reserved atomically against each participating source's ceiling and never borrowed across sources. Failed or unfinished groups preserve successful work for recovery; unknown external outcomes retain reconciliation requirements. Completing one member can make that source L2-ready before the entire window finishes.

Existing source-owned consolidation and removal repairs remain supported. Old workers cannot claim the new batch stage; updated workers explicitly support it, and both stages share the per-user L2 claim and operator-hold boundary. Explicit existing-L1 indexing remains immediate, source-count limited and skips already assigned source/version/generation work. Profile refresh waits on shared batch dependencies while retaining failed-input and removal-repair gates. See the [release verification](releases/0.2.0.md) for the measured scope.

Implementation references: [window admission](../src/oam/indexing/scheduling.py), [batch execution and recovery](../src/oam/indexing/batch.py), [generation accounting](../src/oam/indexing/budget.py), [Profile refresh](../src/oam/profiles/service.py).

## 5. Evidence and Profile paths

Evidence accepts declared text or supported HTML/PDF/image originals, preserves their exact bytes in PostgreSQL, and extracts passages with page, region or block locators. Complete parsing generates cited abstract metadata by default. Operators can disable abstract generation for new indexing operations with `resource.generate_abstract`. The operation records `abstract_state=skipped` and completes with searchable passages.

The worker collects native text from every PDF page before it attempts selected pages with vision. After a vision failure, validated native passages can remain searchable, including text from later pages. Strict recall rejects requests while the user has an active PDF with `parse_coverage=partial`. Callers can request available cited passages with `allow_partial=true`. If vision fails before producing a block required to validate a caller annotation, the worker retains the vision error and withholds new passages until their annotations can be validated.

Evidence joins ordinary retrieval without entering L1/L2/L3. Resource revisions use expected-version checks; deletion immediately excludes all versions and schedules storage cleanup. Profile Data have their own registered exact-field mapping and lifecycle.

`GET /profile` reads stored content without generation. An explicit refresh either reuses unchanged eligible input identity with zero calls or creates a durable operation. Create/Refresh Profile prepares unfinished interaction inputs first: it reuses active work, retries each failed original operation at most once through existing retry guards, and advances queued Insight work. The Profile job waits through initial indexing and any subsequently queued Insight stage before generating content; compatible completed checkpoints are reused. Exhausted retries and unresolved external outcomes require operator attention rather than an automatic retry loop. A successful result contains supported content and References; failed refresh preserves prior eligible content. Source changes can invalidate saved support independently of refresh. Exact Profile Data values retain their own cited authority.

Source: [resources](../src/oam/resources/), [Profiles](../src/oam/profiles/).

[Profile scheduling and custom generation](profiles.md) describes the personal Profile policy and the transient `POST /profile/generate` API. Automatic refresh reuses [durable Profile admission](../src/oam/profiles/scheduling.py) and the existing worker. [Custom generation](../src/oam/profiles/generation.py) selects evidence using the complete instruction, returns validated content/references, and stores no custom Profile or output payload. Both paths retain the existing evidence eligibility, authorization and token limits; neither changes ordinary recall.

## 6. Candidate retrieval and ranking

The API resolves the effective recall mode before validating mode-specific overrides, choosing budgets, decomposing a planned request or dispatching retrieval. An explicit `recall_level` wins. If the field is omitted, `search.default_recall_level` selects `fast`, `normal` or `deep`. The canonical YAML and request schema declare Fast as the fallback. Direct, batch and planned recall use the same precedence. The Inspector sends its selected mode explicitly and initially selects Fast. The MCP, OpenClaw and DSH archives for v0.2.1 also send Fast for omitted tool modes. [Recall handlers](../src/oam/api/search.py) · [Request contract](../src/oam/contracts/search.py) · [Default configuration](../configs/oam.yaml)

Recall validates one absolute deadline and keeps the complete original query for semantic embedding. Explicit date windows take precedence over deterministic query-time interpretation. A date window affects temporal ranking; evidence discovered through another eligible arm remains eligible outside that window. Source and tenant/user policy are hard exclusions.

Fast, Normal and Deep share one pipeline with dense candidates, one lexical arm and optional temporal and personal evidence arms. Each mode independently configures `entity_semantic_graph`. The value `"off"` disables entity and semantic graph expansion, `light` expands through shared entity IDs, and `full` also follows stored semantic links. Fast defaults to `"off"`. Normal and Deep default to `light`. All three modes use `search.causal_graph` to control outgoing causal traversal independently of entity expansion. It defaults to `false`. Reciprocal rank fusion combines the executed arms before any configured reranking. [Configuration guide](../configs/README.md#1-graph-expansion-modes) · [Recall service](../src/oam/retrieval/service.py)

In v0.1.0, the lexical arm combines PostgreSQL cover-density ranking (`ts_rank_cd`) with exact quoted-phrase and identifier matches for one fusion vote. Fast and Normal select their lexical ranking strategy and adjacent user context per preset. Fast optionally scores eligible roots after source validation and local evidence selection. Normal scores hydrated candidates earlier in the pipeline when a reranker is configured. [v0.1.0 configuration guide](https://github.com/rocke2020/open-agent-memory/blob/v0.1.0/configs/README.md)

When originals are requested and `personal_evidence_candidates` is positive, recall ranks user passages that support active L1 preferences or experiences using the existing query embedding. It joins each passage to the current source revision and verifies its support. Hits already in the broad dense list receive only their existing dense vote. Other hits enter the usual fusion, reranking and context budget. A qualified original can pass the local evidence gate for its own text. Assistant-reply witnesses and graph seeds still come from the ordinary dense list. The channel reuses the stored evidence and the query embedding. [Personal evidence configuration](../configs/README.md#8-personal-evidence-candidates) · [Eligibility and ranking](../src/oam/retrieval/repository.py)

The `entity_semantic_graph` and `causal_graph` options are independent. Fast's configured local-evidence policy can require an independent user-supported fact witness before admitting indirect conversation originals or assistant replies. Direct dense and strict lexical hits, complete Insight support and Evidence Resources preserve their separate guarantees. User originals found directly in the bounded lexical shortlist remain eligible without a separately retrieved fact. The highest-ranked hydrated memory original in that same shortlist also remains eligible with a stored user or assistant role, including assistant-first histories with no preceding user message. A leading user original consumes this fallback; weaker assistant originals still need existing independent or local evidence. These originals compete in the existing relevance order and context budget. Neither exemption creates witnesses for derived facts, neighboring replies or source expansion. Supplemental user-original discovery stays within independently supported exact source versions; it creates no new fusion vote and cannot recursively authorize more expansion.

Ordinary recall makes zero generative calls. Batch queries share one mode total and deadline. An optional explicitly enabled planner is a separate generative operation; it does not turn ordinary recall into answer generation.

With the local-evidence gate active, supplemental user originals compete in one query-ranked pool bounded by `source_seed_results`, whether their bodies have already been fetched or still need loading. The pool excludes independently ranked original roots and the required originals of each source version's chosen independent user-fact anchor. Other cached originals remain eligible: fetching a late fact's support does not guarantee that its complete bundle will fit. Returned cached bodies are reused, missing bodies are loaded once, and selected originals keep their citations and existing retrieval paths. Supplemental originals attach to the existing anchor without becoming new fact witnesses or expansion seeds; the same rendered-context budget and identity deduplication still govern admission.

Source: [retrieval service](../src/oam/retrieval/service.py), [candidate repository](../src/oam/retrieval/repository.py).

For all three modes, `source_seed_results` owns the source-discovery budget independently of `max_results`, including the dense prefix used to witness indirect replies. The channel cap still bounds that prefix. `max_results` limits the final learned and original pools; reducing it does not shrink source discovery, required-support preparation or the configured reply-witness budget.

Within the selected source revisions, supplemental user-original recovery materializes only eligible unit IDs and stored embeddings before exact cosine ranking and its existing result cap. Unit IDs break equal-distance ties. Source, version, owner, role and lifecycle filters remain inside this candidate boundary; global dense retrieval retains its separate approximate search path and scan budget. The request deadline still bounds recovery.

Fast, Normal and Deep are configurable presets. Each preset owns a `temporal_nodes` traversal budget, including seeds and subsequently visited nodes, with defaults of 64 for Fast, 200 for Normal and 300 for Deep. It is independent of `max_results`, which caps each final result pool; direct temporal matches are admitted separately from traversal. A request-level `overrides` object can replace selected preset fields and shared recall settings. For example, `{"recall_level":"fast","overrides":{"temporal_nodes":300}}` sets Fast's traversal budget to 300. The existing configuration validators check merged values before retrieval I/O. Direct, batch and planned recall preserve these values through SQL retrieval, optional scoring and context assembly; batch context sharing uses the overridden total. `diagnostics.search_config` records the effective preset and shared overrides. See the [configuration guide](../configs/README.md#7-temporal-traversal-budgets-and-request-overrides).

### Owner-local BM25 and shared recall settings

Recall uses PostgreSQL full-text matching and ranks ordinary matches with field-weighted BM25 plus twice the PostgreSQL cover-density score. BM25 weights terms by how uncommon they are in the owner's index and accounts for document length. Cover density favors matches with query terms close together. Strict full and primary matches keep precedence. Protected quoted phrases and identifiers use exact literal matching, and function-word-only queries use the existing FTS fallback. Fast, Normal and Deep default to `search.lexical_ranking: whole_query`, which ranks a single candidate list. Configure `per_term` to rank and fuse separate term lists. [Lexical implementation](../src/oam/retrieval/repository.py)

BM25 statistics count the physically indexed units for one tenant and user. PostgreSQL updates the affected postings and statistics in the same transaction as each lexical insertion, replacement or physical deletion. Scores use current statistics at read time. Withdrawn or superseded sources are immediately excluded by recall eligibility and leave the statistics at physical cleanup. Migration 0040 populates the projection from stored lexical fields without rerunning extraction or embeddings. [Lexical projection migration](../migrations/0040_bm25.sql)

The November 22 LME-500 result used the BM25 plus cover-density formula through an evaluation adapter over retained indexes. It scored 476/500 with an 8,000-token Fast context cap. The current defaults use `search.lexical_ranker: bm25` and `search.fast.context_max_tokens: 8000`. [Evaluation settings](benchmarks.md#1-longmemeval-s-at-8k-and-18k) · [Default configuration](../configs/oam.yaml)

All three modes use `search.partial_match_gate`, which defaults to `user_fact_exchange` and filters weak conversation matches before context assembly. Set it to `"off"` to disable the filter. All three modes can attach already-discovered neighboring user passages to admitted ordinary facts when chunks are requested. `search.adjacent_user_context` is enabled by default and `search.adjacent_user_context_max_chars` defaults to 1,000 characters per optional neighbor.

Fast uses fused relevance for context selection and skips model scoring. Normal and Deep can use the optional `models.reranker` binding to score hydrated candidates before source support and context assembly. Each preset’s `rerank_candidates` caps the candidate batch and defaults to 48. [Configuration guide](../configs/README.md#6-source-bodies-and-reranking)

## 7. Context assembly

Fast, Normal and Deep share `search.fact_source_bodies`. With experimental `ranked` selection, validated citation support and full original bodies qualify independently for packing. The default `required` retains the complete-group behavior below. With `ranked`, an ordinary fact retains its complete text, exact genuine quotes, provenance and time while each full original must qualify separately as an eligible final packing root, including qualified recovered roots. Source-only detail and owner-attached optional bodies are omitted. Complete original texts are never clipped. Complete Insight groups and Evidence Resource roots retain their required support. Deduplication compares complete validated support. Useful unquoted text can be lost when its original qualifies only as fact support, so this policy supplies no universal evidence-coverage guarantee. Diagnostics identify the active ranked policy.

Any mode can use the experimental `fact_source_bodies: assistant_deduplicated` policy. It completes ordinary required admission, lifecycle checks and token trimming first, then removes only nontruncated assistant-supported facts that have an exact complete selected source with identical time evidence and no retrieval path on the fact. Every original, user/mixed/unknown-supported fact and required Insight member remains. It preserves admission and item order, adds no replacement roots, and falls back to the full required rendering if exact tokens grow. The policy applies when chunks are included. Removed fact IDs appear in `diagnostics.context_budget.assistant_deduplicated_fact_ids`; this experimental policy has no general accuracy guarantee.

The experimental `ranked_deduplicated` value additionally replaces an ordinary fact with its already-selected complete original when the exact locator and unique quotes validate, complete time evidence is identical, and the fact carries no retrieval path. It preserves the original's source-group relevance position and text. Facts with unmatched, partial or truncated originals, different times or retrieval paths remain; required Insight support remains complete. Both pool caps count before this projection, so only rendered-token space is reclaimed. Final source-state reconstruction uses the same projection; removing a source-owning root restores any now-unmatched fact and rechecks the rendered budget. Diagnostics list the replaced fact IDs. Neither this policy nor lower token counts establish an accuracy improvement.

Rank roots by relevance before recency and admit complete groups in relevance order. An Insight group includes its required L1 support and original citations. Existing learned/original pool caps bound candidates; one complete rendered-context budget controls final admission. Citation metadata and repeated quotes count toward that budget.

Repeated identical complete temporal metadata may share context-local `Time Tn: ...` definitions and `Time: Tn` references. Source-time inheritance is applied first. Text bodies, citations, temporal roles, event ranges, precision and timezone information remain intact; structured items retain their original metadata. Sharing is used only when the complete rendered string, including definitions, costs fewer context tokens than the existing rendering. The definitions count toward the same context budget.

Canonical Fast, Normal and Deep totals are 8,000, 16,000 and 24,000 tokens. `context_tokens` counts the complete returned rendering with local `o200k_base`. Hosts budget their complete model input separately using their answer model's tokenizer. Plugins reject or omit a response whose complete context exceeds their host cap rather than clipping it.

`include.chunks: false` omits full original bodies while retaining eligible learned content and genuine citations. With chunks enabled, required owner support precedes optional neighboring originals. A bounded supplemental excerpt preserves source identity and does not authorize further expansion. A later complete original replaces an excerpt only when its complete rendering fits.

Oversized groups are omitted as groups. The cap applies to every complete group, including a sole L1 fact and its support. After skipping an oversized group, the selector can admit smaller later groups. Every returned item retains typed origin, canonical identity, citations and applicable time/graph provenance. The caller reasons over this evidence and writes the answer.

Source: [context assembly](../src/oam/retrieval/context.py), [public recall contract](api.md).

## 8. Lifecycle, removal and recovery

Exact L1 removal accepts one canonical memory ID, immediately excludes that unit and affected dependent Insights, and returns a separate cleanup/repair receipt. Exact L2 removal accepts one canonical Insight ID and preserves its underlying facts and originals. Neither removal request generates content; any later authorized consolidation is separately owned work. Removal is not ordinary undo.

Source deletion is broader: it excludes the source and dependent learned, Evidence, Profile, trace and export output under their current eligibility rules. Expected-version Resource/Profile Data revision preserves immutable historical identities while making the new active version authoritative. Response-time and publication-time checks prevent stale workers, snapshots or caches from restoring withdrawn content. Unrelated active sources remain available.

Multiple API and worker replicas share PostgreSQL ownership and originals. Local release evidence exercises application loss/restart, queued work recovery and rolling replacement. It does not establish database high availability, independent physical-host failure tolerance or transparent replay of ambiguous writes.

Source: [governance](../src/oam/governance/), [deployment](../deploy/README.md), [release evidence](release-evidence.md).
