# OAM configuration

[Documentation](../docs/README.md) · English · [简体中文](README_CN.md)

## Overview

[oam.yaml](oam.yaml) is the authoritative public behavior configuration. This guide explains how graph expansion, causal indexing and recall weights interact. See [operations](../docs/operations.md) for model bindings, environment variables and service startup.

Use these settings in v0.2.1. If upgrading an existing operator YAML, see the [configuration upgrade](../docs/operations.md#upgrading-an-operator-yaml).

## 1. Graph expansion modes

A retrieval **arm** produces a ranked candidate list. Direct dense retrieval finds evidence by semantic similarity; the graph arm expands from matching facts to related facts through stored entities, semantic links or causal links.

**Semantic graph expansion differs from direct dense search.** Dense search compares the complete query's embedding with indexed evidence. Semantic-link expansion starts from a fact already retrieved by dense search and follows stored links to similar facts:

| Path | Comparison | Candidate discovery |
|---|---|---|
| Direct dense search | Query to indexed evidence | `query → matching evidence` |
| Semantic-link expansion | Retrieved seed fact to another fact | `query → seed fact → stored semantic neighbor` |

Basic direct dense semantic search is always enabled in `fast`, `normal` and `deep`, independently of the graph expansion settings. Semantic-link expansion is an optional additional retrieval step: it can recover useful indirect evidence, but a fact can resemble a seed without answering the original query and introduce nearby-topic noise.

`search.fast.entity_semantic_graph`, `search.normal.entity_semantic_graph` and `search.deep.entity_semantic_graph` select entity and semantic expansion independently for each recall mode:

| `entity_semantic_graph` value | Entity and alias expansion | Stored semantic-link expansion |
|---|---|---|
| `"off"` | Disabled | Disabled |
| `light` | Enabled | Disabled |
| `full` | Enabled | Enabled |

Entity graph expansion finds other facts sharing a stored entity ID with a retrieved fact, including supported aliases of that entity. Rename the former per-mode `graph` key to `entity_semantic_graph` in operator overrides; the old key is rejected. This rename requires no reindexing.

Entity and semantic expansion are therefore configurable, rather than always required together. Quote `"off"` in YAML. Setting `entity_semantic_graph: "off"` preserves direct dense retrieval, lexical retrieval and its configured entity-name boost, and temporal retrieval when a time window applies.

The fast-only request field `fast_graph` can override the configured selection: `true` selects `light`, `false` selects `"off"`, and omission or null inherits the YAML value. It does not override causal traversal. See the [recall API](../docs/api.md#recall).

## 2. Independent causal controls

Causal links represent explicit effect-to-cause relationships, stored as `caused_by`. Semantic and causal expansion use different relationships:

| Aspect | Semantic-link expansion | Causal expansion |
|---|---|---|
| Relationship | Similar meaning; no causal assertion. | A source explicitly states that an effect has a particular cause. |
| Link creation | Embedding similarity between facts. | Model extraction and validation of source-supported `caused_by` fields. |
| Traversal | Both directions, one hop from a seed fact. | Outgoing effect → cause, up to two hops. |
| Recall control | Per-mode `entity_semantic_graph: full`. | Shared `search.causal_graph: true`, independently of `entity_semantic_graph`. |

For the source statement "I postponed moving because my rent increased," causal expansion can follow `postponed moving → rent increased`. Semantic expansion might connect the moving fact to another fact about looking for an apartment. The latter is related information, not an established cause. Chronology, co-mention and similarity alone cannot create a causal link.

The causal switches separate link creation from link consumption:

| Setting | Stage | Effect when `true` |
|---|---|---|
| `index.enable_causal_graph` | Indexing | Extract and store validated causal links. |
| `search.causal_graph` | Fast, Normal and Deep recall | Follow existing outgoing causal links. |

Recall follows outgoing causal paths for up to two hops. `search.relation_hops` supports only `2`. The shared `search.causal_graph` switch also controls causal edges used in temporal retrieval. Disabling it preserves ordinary temporal links and date handling.

These switches are independent. `entity_semantic_graph: "off"` with `causal_graph: true` enables causal-only graph expansion. Enabling causal indexing does not enable causal recall, and enabling causal recall does not create missing links. Recall can consume previously stored causal links even while causal indexing is disabled. Changing only recall switches requires no reindexing; changing the indexing switch changes the extraction recipe and does not retroactively populate old facts.

With causal indexing disabled, extraction retains supported facts, entities, temporal annotations, explicit reasons in text and state updates, while discarding structured `caused_by` values.

## 3. Graph fusion weight

`search.normal_arm_weights.graph` weights the combined graph arm during Normal and Deep rank fusion. Entity, semantic and causal neighbors that are enabled contribute to this one arm; they do not receive three separate fusion votes. The weight does not enable any expansion path.

Each executed arm contributes `weight / (search.rrf_k + rank)` for a candidate, with ranks starting at 1. The shared Normal and Deep weights are `1.0` for lexical, dense and temporal, and `0.1` for graph. At the same rank, the graph vote is one tenth of a direct dense or lexical vote. A candidate found in multiple arms receives their summed votes. Optional Normal and Deep reranking happens afterward.

Fast recall uses equal arm weights, including when graph expansion is enabled; it does not use `normal_arm_weights`. Fast always skips model reranking. A graph weight of `0.1` is neither a similarity threshold nor a 10% result quota.

`search.lexical_ranker` defaults to `bm25` for all three recall modes. It ranks ordinary full-text matches with owner-local BM25 plus twice the positional cover-density score. Set it to `fts` to use cover density alone. Both settings use `search.fts_rank_weights` for content, caption, heading and title. The same ranking mode and candidate limits apply in either setting, and PostgreSQL maintains the BM25 projection for either setting. [Indexing and retrieval](../docs/indexing-retrieval.md#6-candidate-retrieval-and-ranking)

`search.lexical_ranking` defaults to `whole_query` for all three modes. It ranks one list of lexical candidates matching any selected query term. Set `per_term` to rank a list per term and fuse those lists. All three modes also enable adjacent user context with a cap of 1,000 characters per optional neighboring passage. These three settings are shared under `search`, and the request `overrides` object accepts them.

```yaml
search:
  lexical_ranking: whole_query
  adjacent_user_context: true
  adjacent_user_context_max_chars: 1000
```

## 4. Why the tested presets keep both expansions off

The 8k Fast FTS+BM25 and 18k Normal FTS LME-500 runs disabled graph expansion. The November 19 BEAM-10M run retained entity and alias expansion. All three disabled semantic-link and causal expansion. [November 19 BEAM-10M settings](../docs/benchmarks.md#november-19-beam-10m)

| Evaluated workload | Recall mode | `entity_semantic_graph` | `causal_graph` | Graph expansion used |
|---|---|---|---|---|
| LME-500, 8k FTS+BM25 | `fast` | `"off"` | `false` | None. |
| LME-500, 18k FTS | `normal` | `"off"` | `false` | None. |
| BEAM-10M, November 19 | `normal` | `light` | `false` | Entity and alias only. |

These configurations also disable causal indexing. They retain direct dense semantic search and lexical retrieval, temporal retrieval when applicable, original source evidence and explicit reasons preserved in fact text. Questions about causes can still retrieve those reasons directly; a stored causal edge is an additional retrieval route, not a prerequisite for answering "why."

Tuning progressed from graph weight `1.0` to `0.1`, then to disabling semantic-link and causal expansion. BEAM's retained entity/alias expansion uses `0.1`; the LME presets have graph expansion off.

Our tuning observations favored conservative expansion: broad graph neighbors could add off-topic context and displace useful support. Candidate explanations, unverified separately for each expansion, are that fact-to-fact similarity can drift away from the query and that an explicitly recorded cause can still be irrelevant to the current question. Additional candidates compete for finite retrieval and context capacity, so more connected evidence does not automatically mean better answers.

The evaluated configurations establish which paths were selected; their results do not isolate the independent effects on noise, context tokens, accuracy and latency of disabling semantic links versus causal links. A graph-weight experiment observed useful evidence being displaced by broader neighbors, which supports conservative weighting but is not a separate on/off test of either expansion. Keep the workload-specific tuning conclusion distinct from a universal claim that either relationship type is harmful.

The design priority is accurate answers from relevant, source-backed context within the indexing-token budget. A good agent memory does not require maximum graph complexity. Keep the simpler working retrieval path and enable additional expansion only when matched tests show that it recovers needed evidence without unacceptable noise, latency or cost. Richer graph structure is a means to that outcome, not a success criterion itself.

### 4.1. Let the agent decide when to investigate causes

**DNOE (do not overengineer):** keep routine recall simple and reuse the agent's existing ReAct loop of reasoning and tool calls.

- **Agent memory:** retrieve relevant, source-backed evidence within context and latency budgets.
- **ReAct loop:** decide whether the task needs causal reasoning or additional evidence, then make targeted follow-up calls.

1. Retrieve the initial evidence through `fast`, `normal` or `deep`; basic direct dense semantic search remains enabled.
2. If an important cause is missing or ambiguous, issue a targeted follow-up recall query and inspect the supporting source evidence.
3. Build the explanation from supported reasons, preserving uncertainty where the sources establish only chronology or correlation.

An agent can choose any recall mode and set `overrides.causal_graph: true` on a targeted follow-up request to traverse stored causal links. The shared operator setting supplies the default when the request omits the override.

The intended benefits are less irrelevant context and fewer unnecessary context tokens while protecting answer accuracy and recall latency. Whether the agent needs further causal investigation depends on the actual task and belongs to the agent's workflow.

## 5. Defaults and example

Fast, Normal and Deep use the following numeric presets. For a request that omits `recall_level`, `search.default_recall_level` selects `fast`, `normal` or `deep`. Canonical YAML sets it to `fast`. An explicit `recall_level` takes precedence. Each context ceiling counts the complete rendered evidence. A complete group that exceeds the remaining budget is skipped, and smaller later groups can still fit.

| Mode | Context tokens | Channel candidates | Fused candidates | Temporal nodes | Source seeds | Results per pool |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Fast | 8,000 | 64 | 128 | 64 | 36 | 88 |
| Normal | 16,000 | 64 | 128 | 200 | 36 | 128 |
| Deep | 24,000 | 64 | 154 | 300 | 36 | 154 |

The canonical direct-recall deadlines are 3 seconds for Fast and 5 seconds for Normal and Deep. Batch and planned recall split one selected context total across their child searches. Valid numeric preset overrides in operator YAML take precedence over the defaults. Recall-only preset changes preserve the indexed generation. [Operator configuration](../docs/operations.md#1-configuration)

The November 19 BEAM-10M run used the former Normal 24k recipe with FTS ranking. The November 27 evaluation used a 12,000-token cap and an experimental source recovery step in the evaluation harness. Each score applies to its recorded configuration. [BEAM-10M evaluation settings](../docs/benchmarks.md#3-beam-10m)

Normal and Deep enable entity expansion by default. Fast uses its direct retrieval path.

| Mode | `entity_semantic_graph` | `causal_graph` | Enabled graph expansion |
|---|---|---|---|
| `fast` | `"off"` | `false` | None, unless overridden by the request. |
| `normal` | `light` | `false` | Entity and alias only. |
| `deep` | `light` | `false` | Entity and alias only. |

The defaults configure the recall policies under `search` and the entity graph separately for each mode. [Default configuration](oam.yaml)

```yaml
index:
  enable_causal_graph: false

search:
  default_recall_level: fast
  causal_graph: false
  fact_source_bodies: required
  normal_arm_weights:
    graph: 0.1
  fast:
    entity_semantic_graph: "off"
  normal:
    entity_semantic_graph: light
  deep:
    entity_semantic_graph: light
```

Normal and Deep use entity and alias expansion with a graph fusion weight of `0.1`. Semantic-link expansion, causal traversal and causal extraction are disabled. Basic direct dense semantic search is always enabled in all three modes. To disable graph expansion for a mode, use `entity_semantic_graph: "off"` for that mode and `search.causal_graph: false` for all three modes.

Recall diagnostics report the selected `entity_semantic_graph` value as `graph`, alongside `causal_graph` and `executed_arms`; inspect them to distinguish a configured path from an arm that actually ran. Implementation references: [recall service](../src/oam/retrieval/service.py), [graph repository](../src/oam/retrieval/repository.py), [semantic-link construction](../src/oam/indexing/links.py), [extraction](../src/oam/indexing/extraction.py) and [configuration schema](../src/oam/config.py).

### 5.1. Partial-match gate

Fast, Normal and Deep share `search.partial_match_gate`. The default `user_fact_exchange` filters weaker conversation matches before context assembly. The value `"off"` disables that filter. Both values are available in any recall mode. Use `overrides.partial_match_gate` to select a value for a single request. [Default configuration](oam.yaml)

Suppose the query is “Did I pay for the workshop?” A lower-ranked assistant reply about registration can match the word “workshop.” If the reply has only a partial lexical match and dense retrieval found no fact citing the exact preceding user message in the same conversation revision, the gate can drop it. This leaves room for payment evidence. Filtering a derived fact can also save the space its required source passages would occupy.

An indirectly matched assistant original can qualify through the exchange when the bounded dense results contain both the user-backed fact and the exact user passage it cites. Direct dense and strict lexical hits, directly retrieved lexical user originals, and the first eligible lexical conversation original can pass independently. The context budget and required support rules still apply. [Local evidence logic](../src/oam/retrieval/local_context.py) · [Recall selection](../src/oam/retrieval/service.py)

In an earlier matched 13-question Fast comparison, enabling it reduced rendered context tokens by 34.1% and retained 12 of 16 tracked evidence passages, up from 9 of 16. Mean retrieval latency was roughly the same at 410 ms with the gate enabled and 412 ms with it off. For an individual request, check `diagnostics.local_context.gate_status` and `diagnostics.local_context.filtered_roots` to see whether the gate ran and how many candidates it removed.

## 6. Source bodies and reranking

Fast, Normal and Deep share `search.fact_source_bodies`. Use `overrides.fact_source_bodies` to choose a policy for one request. `required` is the default and includes each admitted fact's complete source bodies when chunks are enabled. `ranked` validates the same complete persisted sources and retains complete facts, genuine citation quotes and provenance, but full bodies must compete as standalone eligible original roots after existing recovery and local selection. Citation-owner expansion, source-only detail and attached optional neighbors do not independently insert bodies. Complete Insight groups retain their required support. With `include.chunks: false`, recall omits full originals regardless of this setting. Deduplication uses complete validated support.

All three modes accept the experimental `fact_source_bodies: assistant_deduplicated` policy. It completes ordinary required admission, lifecycle checks and token trimming first, then removes only nontruncated assistant-supported facts that have an exact complete selected source with identical time evidence and no retrieval path on the fact. Every original, user/mixed/unknown-supported fact and required Insight member remains. It preserves admission and item order, adds no replacement roots, and falls back to the full required rendering if exact tokens grow. Removed fact IDs appear in `diagnostics.context_budget.assistant_deduplicated_fact_ids`; this experimental policy has no general accuracy guarantee.

The additional experimental value `ranked_deduplicated` uses the same ranked admission and replaces an ordinary fact with its complete selected original only when its exact source locator and unique quotes validate, all time evidence matches, and the fact carries no retrieval path. Excerpts and truncated originals cannot replace facts. The original keeps its existing source-group relevance position and complete text; unmatched facts and required Insight support remain. Deduplication frees rendered-token space but reserves the original learned/original pool slots. `diagnostics.context_budget.source_deduplicated_fact_ids` records replaced facts. This policy has no measured accuracy guarantee.

Normal and Deep can use an optional `models.reranker` with the `rerank` capability to score fused candidate texts before context assembly. Their per-preset `rerank_candidates` values both default to 48. A null cap sends the full bounded fused list. Keep the model binding null to use fused relevance order. Fast skips model reranking. Reranker candidate limits belong to Normal and Deep.

In development after v0.2.1, `search.resource_document_guidance` defaults to `false`. When enabled, Normal recall can find additional clauses within selected plain-text Resources, then attach adjacent eligible passages within the context budget. The guided path requires a reranker, requested chunks and no time window. Its candidate list must consist of eligible plain-text Resource passages. This retrieval setting can use an existing index.

The independent `resource.plain_text_embedding_opening` setting defaults to `false`. It selects whether a newly initialized index embeds each plain-text passage with the first 40 words of its document. Existing indexes retain their admitted recipe when the runtime setting changes. Enable the setting on a fresh index after upgrading the API and every worker that processes its sources. Returned passage text and citations continue to use original evidence.

## 7. Temporal traversal budgets and request overrides

Fast, Normal and Deep are configurable presets. `search.fast.temporal_nodes`, `search.normal.temporal_nodes` and `search.deep.temporal_nodes` default to 64, 200 and 300. Each positive traversal budget counts seeds and subsequently visited nodes. Direct temporal matches and final result pools have separate admission. `max_results` does not clamp this budget.

To give fast the same traversal budget as the normal default, use this operator override:

```yaml
search:
  fast:
    temporal_nodes: 200
```

For one recall request, use `"recall_level": "fast", "overrides": {"temporal_nodes": 200}`. The same object accepts other selected-preset fields and shared settings such as `causal_graph` and `fact_source_bodies`. Unspecified fields inherit configuration. Existing validation applies to the merged values, including candidate-cap relationships. Explicit supported nulls clear optional fields. Unknown fields and invalid combinations fail before retrieval I/O. In Fast, the legacy non-null `fast_graph` option takes precedence over `overrides.entity_semantic_graph`. Direct, batch and planned recall preserve overrides; the response reports the effective preset and shared recall settings in `diagnostics.search_config`.

Each preset owns its temporal traversal budget independently of its result limits. Changing these recall limits preserves existing indexed content.

## 8. Personal evidence candidates

Set `personal_evidence_candidates` in `search.fast`, `search.normal` or `search.deep` to a positive count to search user passages backed by active L1 preferences and experiences. The default is zero. The count is capped by `channel_candidates`, and search runs the additional channel when `include.chunks` is true and the query embedding is available. The returned passages compete under the selected mode's ordinary context budget.

For one recall request, set `"include": {"chunks": true}, "overrides": {"personal_evidence_candidates": 16}`. When novel passages enter fusion, the recall trace shows a `personal_evidence` arm. This option reuses the existing embedding, stored support and search indexes. [API reference](../docs/api.md#3-retain-and-recall)
