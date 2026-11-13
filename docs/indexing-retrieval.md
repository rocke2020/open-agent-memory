# Indexing and retrieval

## Overview

OAM preserves eligible interaction originals, learns individually supported records, consolidates supported Insights, and returns cited context to a caller that owns the final answer. Documents remain a separate Evidence pool. PostgreSQL stores originals, projections, policy, operations and worker ownership; stateless APIs and durable workers share that authority.

This document describes the release design for indexing and recall. Runtime contracts remain in [API](api.md), operator settings in [configuration](../configs/README.md), and verification boundaries in [acceptance](acceptance.md).

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

L2 is a required supported representation whose purpose is useful memory, not exhaustive rewriting of every L1 fact. Consolidation reads existing L1 and can be enabled after L1 indexing without extracting the same originals again. The canonical configuration schedules consolidation after a 24-hour delay. An isolated functional fixture can shorten that delay explicitly without changing the production default.

The configured seed policy selects bounded eligible L1 inputs. The model returns supported candidate changes or a valid no-Insight result; malformed, empty, refused and failed output remain separate outcomes. Publication validates current support and revisions, preserves stable Insight identity where an update applies, and records its descriptive subtype. Mechanical support IDs are grounding checks; paraphrases of one event do not establish independent recurrence.

Every published Insight has a concise, human-readable title naming its enduring subject. CREATE and UPDATE provide a nonblank title of at most 120 characters; an update normally preserves the previous title, changing it only when supported evidence changes or clarifies the subject. Occurrence dates, delivery histories and temporary status belong in the body unless they identify the subject itself. Ordinary support-only updates preserve the title exactly. A full repair after support removal generates its title from surviving evidence and may rename the subject. The title is stored with the search unit's presentation metadata and returned by memory inspection and export; the separate retrieval label retains its existing embedding role. Existing records reuse their saved subject label, or a bounded content preview when no label exists, without generation or reindexing.

New eligible learned input can change L2 and mark an existing Profile stale. The worker never generates a new Profile automatically. Source exclusion and exact memory removal invalidate affected support before later workers can publish from it.

Source: [Insight implementation](../src/oam/indexing/), [consolidation worker](../src/oam/workers/l2_index.py).

## 5. Evidence and Profile paths

Evidence accepts declared text or supported HTML/PDF/image originals, preserves their exact bytes in PostgreSQL, extracts passages with page/region/block locators, and generates an eligible cited abstract. Native PDF extraction and document vision are distinct paths with visible readiness and failures. Original-byte preservation does not imply perfect OCR or abstract quality.

Evidence joins ordinary retrieval without entering L1/L2/L3. Resource revisions use expected-version checks; deletion immediately excludes all versions and schedules storage cleanup. Profile Data have their own registered exact-field mapping and lifecycle.

`GET /profile` reads stored content without generation. An explicit refresh either reuses unchanged eligible input identity with zero calls or creates a durable operation. A successful result contains supported content and References; failed refresh preserves prior eligible content. Source changes can invalidate saved support independently of refresh. Exact Profile Data values retain their own cited authority.

Source: [resources](../src/oam/resources/), [Profiles](../src/oam/profiles/).

## 6. Candidate retrieval and ranking

Recall validates one absolute deadline and keeps the complete original query for semantic embedding. Explicit date windows take precedence over deterministic query-time interpretation. A date window affects temporal ranking; evidence discovered through another eligible arm remains eligible outside that window. Source and tenant/user policy are hard exclusions.

Fast and normal share one pipeline with dense candidates, one lexical arm and an optional temporal arm. Each mode independently configures `entity_semantic_graph`: `"off"` disables entity and semantic graph expansion, `light` expands through shared entity IDs, and `full` also follows stored semantic links. The separate `causal_graph` boolean controls outgoing causal traversal independently, including when `entity_semantic_graph` is `"off"`. These controls preserve direct dense search and lexical entity-name matching. Fast defaults to `"off"`/`false`; normal defaults to `light`/`false` and uses a configured reranker when present. The lexical arm combines PostgreSQL `ts_rank_cd` full-text search with narrow literal identifier and quoted-phrase matching. A literal hit and a full-text hit receive one lexical fusion vote. Reciprocal rank fusion combines at most four executed arms; reranking is a later stage. See the [configuration guide](../configs/README.md#1-graph-expansion-modes) and [recall service](../src/oam/retrieval/service.py).

The `entity_semantic_graph` and `causal_graph` options are independent. Fast's configured local-evidence policy can require an independent user-supported fact witness before admitting indirect conversation originals or assistant replies. Direct dense and strict lexical hits, complete Insight support and Evidence Resources preserve their separate guarantees. User originals found directly in the bounded lexical shortlist remain eligible without a separately retrieved fact. The highest-ranked hydrated memory original in that same shortlist also remains eligible with a stored user or assistant role, including assistant-first histories with no preceding user message. A leading user original consumes this fallback; weaker assistant originals still need existing independent or local evidence. These originals compete in the existing relevance order and context budget. Neither exemption creates witnesses for derived facts, neighboring replies or source expansion. Supplemental user-original discovery stays within independently supported exact source versions; it creates no new fusion vote and cannot recursively authorize more expansion.

Ordinary recall makes zero generative calls. Batch queries share one mode total and deadline. An optional explicitly enabled planner is a separate generative operation; it does not turn ordinary recall into answer generation.

Source: [retrieval service](../src/oam/retrieval/service.py), [candidate repository](../src/oam/retrieval/repository.py).

## 7. Context assembly

Rank roots by relevance before recency and admit complete groups in relevance order. An Insight group includes its required L1 support and original citations. Existing learned/original pool caps bound candidates; one complete rendered-context budget controls final admission. Citation metadata and repeated quotes count toward that budget.

Canonical fast and normal totals are 18,000 and 32,768 tokens. `context_tokens` counts the complete returned rendering with local `o200k_base`; it is not provider-billed usage or an exact count for every answer model. Hosts budget their complete model input separately. Plugins reject or omit a response whose complete context exceeds their host cap rather than clipping it.

`include.chunks: false` omits full original bodies while retaining eligible learned content and genuine citations. With chunks enabled, required owner support precedes optional neighboring originals. A bounded supplemental excerpt preserves source identity and does not authorize further expansion. A later complete original replaces an excerpt only when its complete rendering fits.

Oversized groups are omitted as groups. The documented singleton L1 complete-bundle fallback can return one complete oversized root only when no higher-ranked root was admitted; it reports overshoot and admits no later filling. Insights have no exemption. Every returned item retains typed origin, canonical identity, citations and applicable time/graph provenance. The caller reasons over this evidence and writes the answer.

Source: [context assembly](../src/oam/retrieval/context.py), [public recall contract](api.md).

## 8. Lifecycle, removal and recovery

Exact L1 removal accepts one canonical memory ID, immediately excludes that unit and affected dependent Insights, and returns a separate cleanup/repair receipt. Exact L2 removal accepts one canonical Insight ID and preserves its underlying facts and originals. Neither removal request generates content; any later authorized consolidation is separately owned work. Removal is not ordinary undo.

Source deletion is broader: it excludes the source and dependent learned, Evidence, Profile, trace and export output under their current eligibility rules. Expected-version Resource/Profile Data revision preserves immutable historical identities while making the new active version authoritative. Response-time and publication-time checks prevent stale workers, snapshots or caches from restoring withdrawn content. Unrelated active sources remain available.

Multiple API and worker replicas share PostgreSQL ownership and originals. Local release evidence exercises application loss/restart, queued work recovery and rolling replacement. It does not establish database high availability, independent physical-host failure tolerance or transparent replay of ambiguous writes.

Source: [governance](../src/oam/governance/), [deployment](../deploy/README.md), [release evidence](release-evidence.md).
