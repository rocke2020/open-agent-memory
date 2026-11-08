# OAM configuration

## Overview

[oam.yaml](oam.yaml) is the authoritative public behavior configuration. This guide explains how graph expansion, causal indexing and recall weights interact. See [operations](../docs/operations.md) for model bindings, environment variables and service startup.

## 1. Graph expansion modes

A retrieval **arm** produces a ranked candidate list. Direct dense retrieval finds evidence by semantic similarity; the graph arm expands from matching facts to related facts through stored entities, semantic links or causal links.

**Semantic graph expansion differs from direct dense search.** Dense search compares the complete query's embedding with indexed evidence. Semantic-link expansion starts from a fact already retrieved by dense search and follows stored links to similar facts:

| Path | Comparison | Candidate discovery |
|---|---|---|
| Direct dense search | Query to indexed evidence | `query → matching evidence` |
| Semantic-link expansion | Retrieved seed fact to another fact | `query → seed fact → stored semantic neighbor` |

Basic direct dense semantic search is always enabled in both `fast` and `normal`, independently of the graph expansion settings. Semantic-link expansion is an optional additional retrieval step: it can recover useful indirect evidence, but a fact can resemble a seed without answering the original query and introduce nearby-topic noise.

`search.fast.graph` and `search.normal.graph` select entity and semantic expansion independently for each recall mode:

| `graph` value | Entity and alias expansion | Stored semantic-link expansion |
|---|---|---|
| `"off"` | Disabled | Disabled |
| `light` | Enabled | Disabled |
| `full` | Enabled | Enabled |

Entity and semantic expansion are therefore configurable, rather than always required together. Quote `"off"` in YAML. Setting `graph: "off"` preserves direct dense retrieval, lexical retrieval and its configured entity-name boost, and temporal retrieval when a time window applies.

The fast-only request field `fast_graph` can override the configured selection: `true` selects `light`, `false` selects `"off"`, and omission or null inherits the YAML value. It does not override causal traversal. See the [recall API](../docs/api.md#recall).

## 2. Independent causal controls

Causal links represent explicit effect-to-cause relationships, stored as `caused_by`. Semantic and causal expansion use different relationships:

| Aspect | Semantic-link expansion | Causal expansion |
|---|---|---|
| Relationship | Similar meaning; no causal assertion. | A source explicitly states that an effect has a particular cause. |
| Link creation | Embedding similarity between facts. | Model extraction and validation of source-supported `caused_by` fields. |
| Traversal | Both directions, one hop from a seed fact. | Outgoing effect → cause, up to two hops. |
| Recall control | Per-mode `graph: full`. | Per-mode `causal_graph: true`, independently of `graph`. |

For the source statement "I postponed moving because my rent increased," causal expansion can follow `postponed moving → rent increased`. Semantic expansion might connect the moving fact to another fact about looking for an apartment. The latter is related information, not an established cause. Chronology, co-mention and similarity alone cannot create a causal link.

The causal switches separate link creation from link consumption:

| Setting | Stage | Effect when `true` |
|---|---|---|
| `index.enable_causal_graph` | Indexing | Extract and store validated causal links. |
| `search.fast.causal_graph` | Fast recall | Follow existing outgoing causal links. |
| `search.normal.causal_graph` | Normal recall | Follow existing outgoing causal links. |

Recall follows outgoing causal paths for up to two hops; `search.relation_hops` supports only `2`. The per-mode causal switch also controls causal edges used in temporal retrieval. Disabling it preserves ordinary temporal links and date handling.

These switches are independent. `graph: "off"` with `causal_graph: true` enables causal-only graph expansion. Enabling causal indexing does not enable causal recall, and enabling causal recall does not create missing links. Recall can consume previously stored causal links even while causal indexing is disabled. Changing only recall switches requires no reindexing; changing the indexing switch changes the extraction recipe and does not retroactively populate old facts.

With causal indexing disabled, extraction retains supported facts, entities, temporal annotations, explicit reasons in text and state updates, while discarding structured `caused_by` values.

## 3. Graph fusion weight

`search.normal_arm_weights.graph` weights the combined graph arm during normal-mode rank fusion. Entity, semantic and causal neighbors that are enabled contribute to this one arm; they do not receive three separate fusion votes. The weight does not enable any expansion path.

Each executed arm contributes `weight / (search.rrf_k + rank)` for a candidate, with ranks starting at 1. The current normal weights are `1.0` for lexical, dense and temporal, and `0.1` for graph. At the same rank, the graph vote is one tenth of a direct dense or lexical vote. A candidate found in multiple arms receives their summed votes. Optional normal-mode reranking happens afterward.

Fast recall uses equal arm weights, including when graph expansion is enabled; it does not use `normal_arm_weights` and never reranks. A graph weight of `0.1` is neither a similarity threshold nor a 10% result quota.

## 4. Why the tested presets keep both expansions off

The selected LME-500 fast and BEAM-10M normal configurations keep semantic-link and causal expansion disabled. Their graph settings differ because normal retains entity and alias expansion:

| Evaluated workload | Recall mode | `graph` | `causal_graph` | Graph expansion used |
|---|---|---|---|---|
| LME-500 | `fast` | `"off"` | `false` | None. |
| BEAM-10M | `normal` | `light` | `false` | Entity and alias only. |

Both configurations also disable causal indexing. They retain direct dense semantic search and lexical retrieval, temporal retrieval when applicable, original source evidence and explicit reasons preserved in fact text. Questions about causes can still retrieve those reasons directly; a stored causal edge is an additional retrieval route, not a prerequisite for answering "why."

Tuning progressed from graph weight `1.0` to `0.1`, then to disabling semantic-link and causal expansion. Normal's retained entity/alias expansion still uses `0.1`; fast has graph expansion off.

Our tuning observations favored conservative expansion: broad graph neighbors could add off-topic context and displace useful support. Candidate explanations, unverified separately for each expansion, are that fact-to-fact similarity can drift away from the query and that an explicitly recorded cause can still be irrelevant to the current question. Additional candidates compete for finite retrieval and context capacity, so more connected evidence does not automatically mean better answers.

The evaluated configurations establish which paths were selected; their results do not isolate the independent effects on noise, context tokens, accuracy and latency of disabling semantic links versus causal links. A graph-weight experiment observed useful evidence being displaced by broader neighbors, which supports conservative weighting but is not a separate on/off test of either expansion. Keep the workload-specific tuning conclusion distinct from a universal claim that either relationship type is harmful.

The design priority is accurate answers from relevant, source-backed context within the indexing-token budget. A good agent memory does not require maximum graph complexity. Keep the simpler working retrieval path and enable additional expansion only when matched tests show that it recovers needed evidence without unacceptable noise, latency or cost. Richer graph structure is a means to that outcome, not a success criterion itself.

### 4.1. Let the agent decide when to investigate causes

**DNOE (do not overengineer):** keep routine recall simple and reuse the agent's existing ReAct loop of reasoning and tool calls.

- **Agent memory:** retrieve relevant, source-backed evidence within context and latency budgets.
- **ReAct loop:** decide whether the task needs causal reasoning or additional evidence, then make targeted follow-up calls.

1. Retrieve the initial evidence through `fast` or `normal`; basic direct dense semantic search remains enabled.
2. If an important cause is missing or ambiguous, issue a targeted follow-up recall query and inspect the supporting source evidence.
3. Build the explanation from supported reasons, preserving uncertainty where the sources establish only chronology or correlation.

Today the public recall request lets the agent choose `fast` or `normal` and refine its query. `causal_graph` remains an operator configuration setting with no per-request override; selecting normal or asking a "why" question does not enable causal graph traversal. Agent-directed investigation therefore uses additional ordinary searches and source-based reasoning under the current API.

The intended benefits are less irrelevant context and fewer unnecessary context tokens while protecting answer accuracy and recall latency. Whether the agent needs further causal investigation depends on the actual task and belongs to the agent's workflow.

## 5. Defaults and example

The checked-in defaults enable entity expansion only in normal recall:

| Mode | `graph` | `causal_graph` | Enabled graph expansion |
|---|---|---|---|
| `fast` | `"off"` | `false` | None, unless overridden by the request. |
| `normal` | `light` | `false` | Entity and alias only. |

The following fragment shows the current normal-mode graph settings and causal indexing default from [oam.yaml](oam.yaml):

```yaml
index:
  enable_causal_graph: false

search:
  normal_arm_weights:
    graph: 0.1
  normal:
    graph: light
    causal_graph: false
```

Normal recall uses entity and alias expansion with a graph fusion weight of `0.1`. Semantic-link expansion, causal traversal and causal extraction are disabled. Basic direct dense semantic search is always enabled in both modes. To disable all graph expansion in either mode, set both its `graph: "off"` and `causal_graph: false`.

Recall diagnostics report `graph`, `causal_graph` and `executed_arms`; inspect them to distinguish a configured path from an arm that actually ran. Implementation references: [recall service](../src/oam/retrieval/service.py), [graph repository](../src/oam/retrieval/repository.py), [semantic-link construction](../src/oam/indexing/links.py), [extraction](../src/oam/indexing/extraction.py) and [configuration schema](../src/oam/config.py).
