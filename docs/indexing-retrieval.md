# Open Agent Memory indexing and recall design

## Overview

OAM indexes immutable eligible interactions into L0 passages, L1 facts and L2 Insights, keeps Evidence Resources separate, and retrieves both origins through one PostgreSQL/pgvector pipeline. One credential-derived tenant and one authorized `user_id` identify the personal collection. Each Insight has one of five descriptive subtypes. Recall defaults to mid; the host explicitly selects fast for pre-agent recall. A supplied or query-derived date window adds a temporal ranking arm without excluding otherwise relevant out-of-window evidence. During the ReAct loop the agent can select fast, mid or deep again. Mid and deep have identical options and algorithms; only their configured work budgets differ. A supplied reranker binding automatically enables scoring in both, and an absent binding disables it in both without degradation. Recall never generates text.

Step 5 delivers and evaluates this entire indexing/recall core without L3 Profiles. Step 6 owns L3. This document specifies the target algorithm and public behavior; product source, executed tests and saved evaluation artifacts determine what is implemented. The user-qualified API, document updates, Insight subtypes and temporal-ranking semantics are implemented and functionally tested. No planned score or performance target is a measured result.

## 1. Identity and source-derived decisions

OAM has one personal memory collection per `(tenant_id, user_id)` and no independent caller-selected collection dimension. It combines direct PostgreSQL retrieval, fixed hierarchy paths and two-hop causal expansion with a window-qualified temporal ranking arm and bounded temporal spreading. These are distinct retrieval arms; the temporal arm's repeated expansion does not turn ordinary entity/semantic/hierarchy retrieval into general graph traversal.

1. Eligible interaction messages produce L0 passages and source-backed L1 facts. Related L1 facts produce bounded L2 Insights. An Insight's L1 support already forms the required parent-to-child hierarchy; do not create a second summary system.
2. Evidence Resources produce original bytes, cited passages and Resource abstracts. They participate in direct joint recall but never become L1/L2, graph facts or Profile inputs. A saved document-derived assistant answer stays ineligible when the trusted host labels it so. Profile Resources and L3 generation are outside Step 5.
3. Source-backed generative extraction resolves atomic claims, entities and temporal roles during indexing. L2 synthesis also happens during indexing. Every recall preset uses an explicit time window or runs [deterministic query-time parsing](../src/oam/retrieval/query_time.py) for supported English, Chinese and Japanese expressions. The window steers temporal ranking and does not exclude candidates from other arms. Query-time work consists only of rules, bounded date parsing, embeddings, SQL, graph scoring, optional non-generative reranking and context assembly.
4. Ordinary graph recall uses shared entities, bidirectional semantic links, outgoing effect-to-cause paths of at most two edges, and the fixed L1 child → L2 parent → L1 sibling path. No general recursive graph traversal, Neo4j or separate graph store is needed.
5. Temporal recall adds canonical same-basis proximity edges and actual multi-hop discovery. There is no fixed temporal hop ceiling: continuation scores, visited nodes, eligible-neighbor caps, the total node budget and deadline bound it. Every temporal path stays within the selected window, while other arms may return out-of-window evidence.
6. First prove the public fast path with a positive L2 Insight and HTML evidence. Complete functional coverage of all three modes and graph/temporal cases in Step 5; evaluate fast/mid performance on the three successive LongMemEval sizes. Passing a small fixture does not establish the evaluation performance gates.

The host performs fast recall before invoking its agent. During ReAct, the agent chooses whether to call recall again and selects any preset directly; no forced escalation sequence or OAM-owned sufficiency/planning loop exists. The caller owns the final answer and any full-agent turn budget.

## 2. Data and configuration contracts

PostgreSQL owns originals, projections, support and durable work. Every key, foreign key, query and policy is tenant/user qualified; API and worker content roles use forced non-bypass row-level security. Existing physical `scope_id` columns contain the exact public `user_id`, not a separate identity or mapping. Reuse those tables, policies and collection locks without renaming storage or introducing a third selector.

| Record | Responsibility |
|---|---|
| Source and immutable internal revision | Original bytes, input role, server-assigned revision, caller timestamps and deletion state |
| Message evidence and passage | Exact role/eligibility/time and message/span or document-block locator; normalized text is a derived search representation |
| L1 memory and support | Atomic assertion plus qualified values, supporting passage/span, entities and grounded time annotations |
| L2 Insight and support | Bounded synthesis over at least two distinct eligible L1 records; one descriptive subtype; exact support identities and versions; parent→child membership derives from this support |
| Search representation | Passage/L1/L2 reference, origin/kind, generation, lexical tokens, embedding and stable ordinal; no independent authoritative content |
| Entity posting | One fact-to-normalized-entity membership; no materialized all-pairs entity clique |
| Fact link | Same-user semantic similarity or extracted `caused_by`/`updates`; `updates` never contributes graph activation |
| Time annotation | `event` or `date_reference`, nonempty `tstzrange`, precision/meaning/origin, source span, original time text and timezone/reference interpretation |
| Temporal proximity link | Canonical ordered endpoint pair, one time basis, gap/weight and event/reference annotation witnesses; traversable from either endpoint |
| Stage result and usage | Persisted success/valid no-op/partial/failure, compatible input identity and actual provider usage, reusing existing operations/work/attempt ownership |

The L1 memory unit is one concise, self-contained fact sentence, not a raw passage, conversation summary or bundle of unrelated claims. It preserves the subject and any necessary dates, values, units, negation and uncertainty. For example, “Lin prefers concise work updates.” and “Lin prefers receiving work updates in the morning.” are two independently addressable units. A paragraph may produce several units; source evidence remains unchanged.

A question does not establish its premise as fact, and asking about a topic does not establish a personal trait. Extract explicit assertions or preferences embedded in a question, but return the explicit `no_facts` outcome when the center contains no supported assertion. Its eligible raw passage remains searchable. Empty `facts` paired with the positive `facts` outcome remains invalid structured output, not a valid no-op.

Each accepted unit has its own tenant/user-qualified `memory_id`, exact L0 support, embedding/lexical representation and graph memberships. For L1 search results, `unit_id` is that same `memory_id`; passage and Insight IDs identify different record kinds. Compatible retries preserve the unit ID. Replacing a unit creates a new identity rather than silently changing what an existing ID means. L2 support records refer to these individual IDs, allowing future unit exclusion to invalidate dependent Insights without removing unrelated facts.

Targeted fact forget/remove is outside v0.0.1. Its future design must distinguish excluding one derived unit from deleting its source evidence, suppress all serving representations and dependent synthesis, and prevent retained evidence or rebuilds from silently recreating an excluded claim. Stable unit IDs and explicit support are the preparation required now; no forget endpoint, new deletion workflow or speculative suppression subsystem is added in this version.

Admission uses `POST /v1/retain` with an authorized `user_id` and `Idempotency-Key`; administrator `POST /v1/users` establishes the personal collection and explicit creator grants. JSON contains either an interaction or a text document. Interactions provide message role, exact content, timezone-aware `event_time` and explicit `learning_eligible`, with optional source clocks, timezone and exact-span caller annotations. OAM assigns the source ID and message-order locators. Callers supply no source/message IDs or source version, expected version, upload length or hash. The [HTTP contract](api.md) provides complete request examples.

Documents supply a stable `document_id` and explicit `update_mode`. Text `replace` supplies the complete body; `append` supplies only new text for an existing text document. Under the existing collection lock, OAM checks request idempotency, reads the active text, concatenates exactly without inserted separators, validates the resulting size and allocates the next immutable revision atomically with its receipt. Retries reuse the same logical request rather than appending again. Multipart `metadata` plus `original` on the same endpoint supplies a complete file; HTML/PDF/image files accept replacement only. OAM computes the file length/hash. A newly accepted revision immediately excludes older derivatives while indexing remains pending. No document becomes learned memory.

Time annotations attach to raw passage spans as well as accepted L1 support, so caller-annotated content omitted by extraction remains time-searchable. Document passage annotations remain Evidence and never create Learned Memory. L2 receives a temporal-arm rank only when every required support matches the selected basis/window; it receives no invented aggregate event time and may still appear through a direct arm otherwise. Temporal spreading operates on eligible L1 facts only; raw and document passages remain direct candidates, and L2 participates through verified support.

One embedding generation fixes model identity, dimensions, normalization and lexical/extraction recipes. Generation identity excludes recall-only budgets and private credentials. Never mix equal-dimensional embeddings from different models. The sole dimension owner is `index.embedding_dimensions`. The operator migration command passes this value into dimension-dependent SQL, persists the initialized dimension and rejects a mismatch on later startup/migration. Runtime workers do not invent DDL. A model/dimension change needs an explicitly rebuilt projection rather than silently altering an active index. Store the active generation per tenant/user pair and reject incompatible publication or search until an explicit rebuild changes it.

Required indexes cover tenant/user/source revision, lexical GIN, pgvector cosine HNSW, entity posting directions, fact-link directions ordered by weight, temporal annotation GiST and basis-specific point times, and both temporal-link endpoints. Bounded iterative HNSW scans remain approximate; actual query plans and comparison with exact user-qualified search are acceptance evidence.

`configs/oam.yaml` in the product repository is the only runtime behavior owner. The resolved config supplies `search.fast`, `search.mid`, `search.deep`, per-mode deadlines, graph/intermediate bounds, temporal node/neighbor/seed bounds, chunk/extraction/Insight bounds and indexing-link limits. Fast runs time-qualified spreading within its shorter deadline and caps temporal nodes at `min(search.temporal_nodes, search.fast.max_results)`; it skips ordinary graph, hierarchy and reranking. Mid/deep have the same field set and stage map; only numeric budgets differ. One optional reranker binding controls both modes without a second enable flag.

`POST /v1/recall` requires only authorized `user_id` and nonempty `query`. Optional fields include `recall_level`, paired `time_basis`/`time_window`, timezone-aware `reference_time`, IANA `reference_timezone`, `max_context_tokens` and `allow_partial`. Time basis and window override query parsing for the temporal ranking arm; neither filters other arms or final results. Without an explicit window, one reference instant and timezone (current UTC by default) resolve one supported query expression to an event-basis UTC half-open ranking window. An ambiguous, invalid or unsupported implicit date leaves ordinary recall active with a named diagnostic; invalid explicit windows fail validation. Mode defaults to mid and `allow_partial` to false. Normal callers need not select Profiles, chunks or internal stage budgets; unfinished capabilities cannot be silently included.

## 3. Indexing flow

Indexing publishes independently usable source evidence before higher-level work and never holds a database transaction during remote model calls. A failure preserves completed compatible stages and exposes what remains unfinished. Each bounded stage uses a checkpoint keyed by tenant/user, internal source revision, generation, stage key and input digest. Store successful output and its actual usage atomically under the existing work fence before starting its dependent stage. Retrying the outer indexing unit looks up these exact checkpoints and starts at the first incomplete stage; a failed embedding or Insight stage does not repeat a successful extraction. This is a fixed stage sequence inside existing work ownership, not a new job planner.

```text
trusted retain/upload → authorize/validate → commit originals + operation receipt
  interaction → per-message eligibility → structural passages → embed + lexical index
    → grounded extraction → L1 + support + entities + causal/update links
    → bounded semantic/time links → incremental L2 + support → L2 representation
  Evidence Resource → parse → cited passages → embed + lexical index
    → grounded Resource abstract + separate abstract state
```

1. Validate grants, sizes, types, timestamps and enabled roles. Commit the exact original, per-message eligibility and immutable input identity with the durable operation/work receipt before model dispatch. Mixed eligible/ineligible messages never collapse into one globally eligible record.
2. Split eligible messages at turn/paragraph boundaries with the configured approximate-token target and hard maximum, recursively splitting oversized units. Keep stable source character/byte locators and zero owned overlap. A previous eligible context-only tail can resolve pronouns and relative dates; it cannot own a new fact or introduce ineligible report-derived text. Use the exact named tokenizer to enforce the complete extraction prompt bound.
3. Publish lexical and dense source passages after their stage succeeds. Keep all eligible original passages searchable even if extraction misses a claim. HTML parsing preserves numbers, units, table associations and block locators. Abstract failure leaves passage availability explicitly partial rather than reporting complete success.
4. Submit a bounded extraction envelope with source roles, timestamps, reference timezone, center spans and previous context-only spans. Ask for structured atomic facts, exact quoted supports, categories, normalized entity mentions, temporal annotations and optional explicit `caused_by`/`updates` references. Do not request chain-of-thought or infer a preference merely from one reported action.
5. Validate required schema, output bounds, finite values, allowed types, exact quoted source spans and center ownership. OAM resolves a unique exact quote to its source character span deterministically instead of relying on a model to count characters; valid explicit offsets may disambiguate repeated quotes. Missing or ambiguous matches are rejected, never fuzzily repaired. Unused envelope metadata is not published. Caller-provided spans remain strict and caller time annotations are authoritative for their declared assertion; reject conflicting generated substitutes. Reject invalid derived records while retaining raw evidence and reporting extraction coverage. Refusal, length termination, malformed/empty content and valid zero-fact output are distinct outcomes.
6. Resolve entities conservatively from normalized aliases and explicit identifiers. Exact duplicate claims may share support; near-duplicate wording or contradictory values do not authorize destructive merging. `caused_by` points from an effect to a supported cause; extraction ordering alone is not proof of real-world time. Keep explicit `updates` separate from graph scoring.
7. Embed validated L1 text outside transactions, then publish L1, support, entity postings, extracted links and search representations under the existing claim fence and current-source checks. Persist actual attempt usage including retries; a published output is reused without inventing another paid call.
8. Build semantic links from existing embeddings in a separately recorded bounded stage. Select configured same-generation neighbors once per new fact and record cosine weights. Incoming degree can exceed the outgoing proposal cap, so recall independently caps both directions. Failed enrichment preserves direct L0/L1 availability and reports missing graph coverage.
9. Build temporal links using the same rule for within-batch and existing units, as specified in Section 6. Preserve source timestamps exactly; ordinals and stable IDs break ties without adding artificial seconds.
10. For changed eligible L1 facts, retrieve a bounded related support group, including compatible unchanged facts, and synthesize a source-supported L2 Insight with one predefined subtype. Keep L1 qualifiers and L0 citations; repeated copies of one occurrence do not establish repetition. Evidence Resources, previous generated summaries and Profile text are not independent support. Publish L2 text, subtype, exact L1 support, vector/lexical representation and parent membership together after revalidating every supporting revision.
11. A schema-valid `no_insight` records the examined support group without an Insight or subtype. Missing group coverage, invented support, refusal, empty output or failed generation remain failures. Changed/removed support immediately makes the dependent L2 ineligible. Incremental refresh reuses compatible prior work; it does not scan or resynthesize the entire user's collection. L2 changes advance Profile input freshness without generating L3.

The generation client records model/role, input/output/reasoning/cache usage when available and explicit coverage gaps. Embedding and reranker meters remain separate. Tokens attributable to indexing include extraction, Insight and Resource-abstract generation attempts; answer and judge tokens belong to the evaluator.

### L2 Insight subtypes

Each supported Insight receives exactly one subtype describing its main claim. All subtypes share support validation, persistence and recall; subtype never changes identity, deduplication, candidate selection or ranking. `general` preserves useful synthesis outside the specific labels rather than forcing `no_insight`.

| Subtype | Supported synthesis |
|---|---|
| `preference` | A qualified choice across related facts, not a preference inferred from one action |
| `pattern` | Repeated distinct occurrences or a trend backed by comparable dated facts |
| `project` | Related project goals, decisions, constraints or progress |
| `relationship` | A supported role or connection between people, not inferred closeness |
| `general` | Other useful supported synthesis |

## 4. Shared recall flow and presets

Use one service and one pipeline. Stage eligibility is fixed by the preset and optional model binding; parameter values come from the resolved configuration. No second mid/deep engine or provider-specific branch is required.

| Stage | Fast | Mid | Deep |
|---|---|---|---|
| Deterministic query-time resolution, dense, lexical and exact anchors without date exclusion | Enabled | Enabled | Enabled |
| Entity/semantic/causal/hierarchy expansion | Omitted | Enabled | Same algorithms/options as mid |
| Temporal spreading for a resolved time window | Enabled with fast node/deadline cap | Enabled | Same algorithm with larger budget |
| Scoring-only reranker | Omitted | Automatic when binding supplied | Automatic when binding supplied |
| Candidate, fanout, rerank and time budgets | Small | Everyday balanced budget | Larger budget |
| Generative model call | Never | Never | Never |

```text
query + authorized tenant/user + selected preset + optional ranking window
  → validate → freeze config/read revision/deadline and one query-time reference clock
  → use an explicit window or parse a supported query date into an event window
  → lexical SQL in parallel with one query embedding
  → eligible dense/anchor candidates and graph seeds without date exclusion
  → every mode: window-qualified temporal spreading when a window exists; fast uses its node/deadline cap
  → mid/deep: bounded ordinary graph and hierarchy
  → per-arm stable ranks → RRF → support-aware dedup → bounded hydration
  → configured mid/deep rerank → exact-token context assembly → eligibility recheck
  → typed evidence, citations, context, usage and stage diagnostics
```

Apply tenant/user, current source revision, deletion, learning eligibility and generation before every logical candidate or neighbor cap. Apply the selected basis/window only inside the temporal arm; out-of-window units from lexical, dense or ordinary graph arms remain candidates. Physical ANN scans can still visit rejected rows; never confuse a result cap with a bound on all database work.

Start lexical retrieval while the query embedding is in flight. Read one distance-ordered dense pool large enough for both direct candidates and seeds at their respective thresholds, then derive bounded sets without a second unbounded ANN call. Use PostgreSQL native `ts_rank_cd` as lexical scoring and call it FTS, not BM25. English and Chinese query/document tokenization must share a pinned implementation; do not claim Chinese lexical support from an English stemmer alone. Exact identifiers and quoted anchors are deterministic signals.

Fuse each executed arm with reciprocal rank fusion, `sum(weight / (rrf_k + rank))`, using stable tenant/user-qualified IDs for ties. Graph activation and temporal scores order their own arms only; they are not added to cosine or FTS values. Preserve every candidate's origin, matched annotation and selected support/path evidence across deduplication. L2 gets no reserved rank boost, and Memory/Evidence have no origin quota.

Mid and deep submit at most one bounded reranker request using the selected candidate cap. A missing/empty binding means intentional non-degraded RRF-only output. Configured failures obey the caller's partial policy and are named in diagnostics; never substitute a generative reranker. Reranking can reorder candidates but cannot create new evidence or bypass tenant/user and source eligibility.

Each recall operation computes one absolute deadline consumed by embedding, SQL, graph work, hydration and reranking; no fallback receives a new timeout. Normal pre-agent and ReAct calls have independent recall-operation budgets so agent thinking does not consume a later tool call's entire budget. An explicitly supplied shorter host deadline still bounds that operation and all children. The existing trusted shared-turn key remains an opt-in host constraint: calls carrying that key reuse its deadline, while the normal integration does not automatically attach the pre-agent key to later ReAct calls.

## 5. Graph and hierarchy algorithms

Expand from a fixed set of eligible dense L1 seeds. Ordinary expansion returns narrow IDs, scores and path witnesses, not full payloads. Bound every intermediate branch inside indexed SQL before aggregation, then hydrate survivors once.

1. Entity expansion joins seed → entity posting → eligible fact, with configured per-seed entity and per-entity candidate bounds. An entity shared by many facts remains a posting list; co-occurrence is not causal evidence.
2. Semantic expansion reads outgoing and incoming links independently, each with its own per-seed cap. A new fact's outgoing top-k does not bound an older fact's incoming degree.
3. Causal expansion follows `effect --caused_by--> cause` from original seeds for one edge and then one additional outgoing edge. Use two fixed joins, reject repeated nodes within a path, and reapply eligibility at the intermediate cause. Never reverse edges, follow a third causal edge or mix relation types to extend this ordinary branch.
4. Hierarchy expansion follows `seed L1 child ← support — L2 parent — support → eligible L1 sibling`. Cap parents per seed and children per parent. A parent is usable only when its required support remains eligible. Preserve both stored parent→child witnesses. Do not recursively climb another Insight or turn retrieved siblings into new seeds. Direct L2 hits still carry their own full L1/L0 support.
5. Deduplicate each ordinary-graph signal by candidate. Compute `entity = tanh(distinct_shared_entities * entity_score_scale)`, `semantic = max(link_weight)`, and `causal = max(product(edge_weights along each eligible one/two-edge path))`. Their sum orders only the ordinary graph arm, with stable ID ties. Every stored link weight must be finite and in `(0, 1]`; an explicitly asserted causal link uses 1. Extracted causal weight means an asserted relation, not calibrated truth confidence.
6. Keep hierarchy sibling ranking as a separate arm, ordered by parent rank, sibling query similarity and stable ID. Fuse its rank with dense, lexical and ordinary-graph ranks; do not add its raw score to graph activation.
7. Graph timeout returns completed direct arms only when partial policy permits and final eligibility/hydration can finish inside the deadline. Do not start a fresh fallback graph query after exhausting the graph budget.

The worked acceptance graph uses `MOVE --caused_by--> RENT --caused_by--> JOB --caused_by--> OTHER` and `HOUSING_INSIGHT → MOVE, CHEAPER_OPTION`. With only MOVE seeded, the causal branch finds RENT and JOB, never OTHER or reverse effects. The hierarchy branch finds CHEAPER_OPTION through the two support edges. Isolate other arms when testing those path claims.

## 6. Temporal indexing and recall

An explicit or query-derived window selects and ranks the temporal arm in every mode; it does not exclude out-of-window results found by lexical, dense or ordinary graph arms. Fast follows eligible temporal links within its small node budget and deadline. Mid/deep use the same bounded multi-hop temporal algorithm with larger budgets and additional ordinary graph, hierarchy and reranking stages. Temporal proximity expresses date proximity, never causality.

| Time basis | Temporal-arm match |
|---|---|
| `event` | A source-backed event annotation overlaps the window |
| `referenced` | A date-reference annotation overlaps the window |
| `mentioned` | Explicit source-message mention time lies in `[start, end)` |
| `created` | Caller/source creation time lies in `[start, end)` |
| `updated` | Caller/source update time lies in `[start, end)` |

Index time is operational metadata and never substitutes for any basis. Existing interaction-envelope timestamps describe when the interaction occurred; they become mention provenance, not an invented occurrence time for every extracted fact. Unknown times receive no vote from the selected temporal arm but may be returned by another arm. Caller-local window boundaries must be converted separately to UTC, including daylight-saving transitions.

Store an exact instant as a nonempty singleton `[t,t]`, an actual duration with its declared bounds, and an uncertain day/month/year as a half-open possible period. Preserve `granularity`, `meaning`, `origin`, original text, reference timezone and exact supporting source span. A possible month is not a claim that an event lasted the month. “In 2024 we discussed cancelling the 2025 trip” yields a 2024 event annotation and a distinct 2025 reference; it never becomes a 2024–2025 event interval.

Temporal link construction:

1. For each new eligible fact and available basis, select a bounded number of nearest current same-basis facts within the configured horizon. Interval gap is zero for touching/overlapping ranges and otherwise the distance between nearest bounds; point-time gap is absolute time difference. Do not create duplicate-evidence hops between a raw passage and its own extracted fact.
2. Canonicalize each pair by stable endpoint ID. For event/reference, choose the witness pair by `(gap, left_annotation_id, right_annotation_id)` and store both IDs. For point-time bases, witnesses are null. Store `weight = 1 - gap / horizon`; gaps at or beyond the horizon produce no edge.
3. Use the same proposal cap, horizon and tie-breaking for within-batch and existing neighbors. The union of proposals may give a node more incoming links than its own proposal cap, so recall has a separate eligible-neighbor bound. Concurrent publications need not discover each other; this is bounded approximate adjacency, not an exact continuously repaired nearest-neighbor graph.

Temporal spreading:

1. Apply the chosen basis/window when selecting temporal-arm seeds, leaving dense/lexical candidate caps and ordinary graph seeding unchanged. Select bounded semantic seeds across populated time buckets, round-robin by semantic rank with stable ties. The representative event/reference time is the midpoint of the earliest matching annotation/window intersection; point-time bases use the corresponding timestamp. Coverage selects seeds and adds no separate rank-fusion vote.
2. Initialize seed traversal strength to 1 and mark all seeds visited. For each breadth-first level, read both directions of same-basis temporal links and outgoing stored `caused_by` links. No additional causal relation type or import machinery is required.
3. Before each per-source neighbor cap, enforce tenant/user/source/generation/time eligibility, event/reference edge-witness overlap and a finite query-cosine floor on every target. A hidden, stale, out-of-window or wrong-basis node cannot serve as a bridge. The exact cosine check on a linked target is not another embedding call or ANN query.
4. Compute direct date proximity `D(v) = 1 - min(abs(basis_date(v) - window_midpoint) / window_half_width, 1)`, propagated score `P(u,v) = strength(u) * edge_weight * 0.7 * relation_factor`, and temporal score `T(v) = max(D(v), P(u,v))`. The internal relation factor is 1.1 for `caused_by` and 1 for temporal proximity. No edge amplifies inherited strength: the maximum multiplier is 0.77. Direct date evidence can refresh a descendant's score.
5. Aggregate competing proposals across the entire completed level before admission. Choose highest T, then highest P, then stable seed/parent IDs. Admit unique targets by `(-T, hop_depth, -cosine, stable_id)` until the node budget is full. An admitted node continues only when `T > 0.2`; scores equal to the threshold stop expansion but do not remove the result.
6. Newly admitted continuing nodes form the next frontier. There is no fixed depth or batch-count ceiling. Visited nodes never reopen; retain the best considered path at the earliest discovered depth. Decay alone cannot guarantee termination because date evidence can refresh strength; the finite node budget and absolute deadline remain mandatory.
7. Stop with `frontier_exhausted`, `node_budget` or `deadline`. SQL batches split work within a level without redefining hop depth. Discard unfinished-level proposals on timeout; return only completed, revalidated work with explicit truncation/partial diagnostics. Leave time for final validation and hydration or return a deadline error.
8. Rank unique non-seed discoveries by temporal score, depth, similarity and ID, then contribute that arm's rank to RRF. Merge annotation/path evidence by identity. Revalidate the entire retained seed-to-result path before its graph contribution can affect final rank or inclusion; an invalid bridge removes that contribution and any graph-only result supported solely by it.

The temporal acceptance chain must discover a target missing from the initial dense and lexical pools through A→B→C, then through a longer chain of at least six hops when scores and budgets permit. Fast temporal acceptance uses a natural query such as “last week what I did” without a caller window and proves a time-matched rank plus a graph-only temporal discovery with its path, stop reason and zero generative calls before agent invocation. A separate no-date-refresh chain must yield 0.7, 0.49, 0.343, 0.2401 and 0.16807, admitting but not expanding the final node. Moving B outside the window removes that temporal path but does not exclude B from another arm; excluding its source removes it from every arm. No temporal graph is constructed during recall.

Deterministic query-date parsing is a v0.0.1 recall stage, including fast, and makes no generative call. English and Chinese rules handle common absolute and relative dates, with `dateparser` applied only to bounded date-like spans. Japanese rules cover explicit year/month/day and common relative day/week/month/year expressions. Unsupported, invalid or competing implicit dates leave ordinary recall active with a diagnostic rather than choosing one, and callers use structured windows for non-event time bases. Invalid explicit windows still fail validation. Preserve the original query for relevance and convert local calendar boundaries separately into UTC so daylight-saving days keep their actual duration. Time-only chronological browsing is separate from ranked recall and does not substitute for ANN-plus-spreading acceptance.

## 7. Publication, failures and context assembly

Reuse the existing operation, claim, lease, fence and retry machinery. Persist completed compatible units atomically and retry only unfinished work; no new recovery ledger or queue is required. Remote calls happen outside transactions, while final publication locks and rechecks operation cancellation, claim ownership/fence/lease, input digest, active generation, source revisions, eligibility and policy.

1. A source revision change immediately excludes prior derivatives from serving, even before physical cleanup. A stale worker cannot publish. L2 requires every recorded supporting fact/version to remain eligible, and hierarchy paths obey the same rule.
2. Passage success, L1 success, semantic/time-link coverage, L2 success and Resource abstract state are distinct. A failed later stage preserves earlier eligible content and reports partial coverage. Valid zero-fact/no-insight output never stands in for the positive acceptance fixture.
3. Model failures retain useful diagnostics and actual metered attempts. Unknown remote outcomes follow existing reconciliation rules; never replay a known completed billable unit simply because the outer process restarted.
4. Embedding/lexical/graph/reranker failure can return only completed eligible arms when `allow_partial` permits. Authentication or eligibility failure fails closed. An unavailable encoder must not produce fabricated semantic scores or a false complete-recall claim.
5. Hydrate bounded survivors in a batch with their exact source locators, temporal annotations, L1/L0 support and graph paths. Avoid per-result database round trips. Collapse only duplicate support/span/time identity; retain distinct occurrences and conflicts.
6. Assemble deterministic rendered context under one caller token budget across Memory and Evidence. Include citations and time qualifiers in the exact token count. Truncate only at valid source boundaries with explicit flags; never separate a claim from required provenance. Optional recalled source chunks have their own declared budget and do not silently enter the context-token metric.
7. Recheck current grants, source policy and retained graph/support paths before emission. Return typed items, rendered context and its tokenizer/count, selected mode, executed/omitted stages, generation/read revision, readiness/partial coverage, usage and elapsed stage times. No mode generates L3 or a final answer.

## 8. Implementation and acceptance

The product repository owns executable behavior and evidence. Review this design before implementation, freeze shared schema/configuration/DTO changes centrally, and then use bounded parallel work only for disjoint modules with explicit dependencies. The first real public source-to-fast-search path remains the priority.

| Order | Implemented boundary to prove |
|---|---|
| 1 | User-qualified public retain/upload, real PostgreSQL originals/receipt, model adapter, structural passages, grounded L1 and positive supported L2, then public fast recall with inspectable citations |
| 2 | One shared three-preset service, both configured/absent reranker paths, entity/semantic bounds, two-hop causal paths and L2 sibling witnesses |
| 3 | Five-basis temporal-arm matching, canonical temporal links, second-hop and long-chain discovery, score continuation, no hidden bridges, node/deadline termination |
| 4 | Exact context packing, partial/failure behavior, incremental L2, cross-user/source eligibility and stale-publication rejection |
| 5 | LME-6, then LME-30, then LME-60 with inspectable saved metrics and artifacts |

Meaningful tests precede behavior changes. Deterministic model fixtures prove mechanics without claiming model quality; the safe real path uses an isolated PostgreSQL target and public HTTP. Verify cross-user grants, idempotent interaction/append retries, concurrent exact text appends, file-append rejection, immediate revision exclusion and each Insight subtype with valid support. Query-time cases cover explicit precedence, ambiguous-date diagnostics, out-of-window direct results and fast graph-only discovery with zero generation. Require actual HNSW execution on a representative fixture and compare with exact user-qualified neighbors. Test high-degree entities/reverse semantic links, stale intermediate support, causal direction, temporal witness mismatch and a sixth-hop result with no shortcut edge. Record observed query plans and limitations; index existence is insufficient.

No default test reads private `.env`, contacts paid providers or deletes user data. Live model calls require configured roles and explicit authority. Required verification includes the product's full unit/contract suite, SQL/RLS integration, format/lint checks and the named public-path checks. L3, full PDF/image/Inspector integration and release-image readiness remain separate later gates.

## 9. Progressive LongMemEval evaluation

Evaluate OAM through its public HTTP API in three nested stages: LME-6, LME-30, then LME-60. Use a pinned dataset and balanced one/five/ten questions per category. Ingest chronological histories with source roles and timestamps, and never expose answers or answer-session labels to indexing or recall.

Freeze each stage's models, prompts, mode and context budgets. Evaluate fast and mid against the same indexed histories with Profiles disabled; both modes are required for each LME stage. Deep requires functional tests, but its performance evaluation is deferred to a separate harder dataset, not included in LME-6/30/60. Count unique indexing work once, and meter answer/judge generation separately. Persist each completed question/mode result atomically with its input/config identity; reuse unchanged completed units and rerun unfinished or changed units under fresh isolated user IDs.

Report fast and mid answer accuracy, exact answer-visible context tokens, indexing producer tokens and retrieval latency independently. Count the exact serialized evidence inserted into the answer prompt, including citations/timestamps, using the named tokenizer. Report token coverage gaps and retrieval median/p95, errors, partial outcomes, environment and concurrency. A missing deep result does not make an LME stage incomplete; a missing fast or mid result does.

Complete and inspect each stage before expanding to the next. Numerical acceptance criteria, private comparison inputs and reports are local evaluation configuration/artifacts, never runtime dependencies. No default test reads private credentials, invokes paid models or substitutes synthetic output for a measured score. Evaluation has started, but no complete LME stage or accuracy result is available yet.

## 10. Authority and implementation status

This document is the product indexing/recall target contract. The [architecture](architecture.md) describes the current overall data flow, [acceptance](acceptance.md) defines delivery gates, and [canonical configuration](../configs/oam.yaml) owns executable parameter values. The PostgreSQL retrieval and evidence-backed hierarchy are OAM-owned runtime algorithms; no external memory engine or benchmark runtime is a dependency.

The user-qualified retain/recall interface, server-managed document updates, subtype metadata and temporal ranking are implemented in [HTTP admission](../src/oam/api/app.py), [runtime composition](../src/oam/api/runtime.py), [source admission](../src/oam/db/sources.py), [indexing](../src/oam/workers/index_source.py), the [recall service](../src/oam/retrieval/service.py) and its [SQL repository](../src/oam/retrieval/repository.py). PostgreSQL tests cover temporal-only six-hop fast discovery, direct out-of-window evidence, strict temporal bridges, Insight support and natural HNSW execution. Separate-process configured-model probes have produced and recalled concise L1 units, a supported typed L2 Insight and HTML Evidence; rejected generated time annotations remain explicit partial coverage. These results do not establish benchmark parity or release readiness. Profiles, later Resource/lifecycle integration and the complete deployment matrix remain separate gates.
