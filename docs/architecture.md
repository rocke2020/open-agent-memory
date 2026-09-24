# Open Agent Memory Architecture

## Overview

OAM is one governed data plane: immutable source evidence is authoritative, memories and Profiles are rebuildable projections, and recall assembles bounded context without generating an answer. PostgreSQL owns content, policy, work, and application-replica recovery so API and worker processes can be replaced without losing accepted inputs. The user-qualified retain/recall core, document updates, Insight subtypes and temporal ranking have functional verification; complete Profiles, deployment recovery and benchmark performance remain separate gates.

## 1. Authority and Isolation

A tenant is the organization security boundary. Each `(tenant_id, user_id)` identifies one personal memory collection: credentials derive the tenant, and the caller may address only explicitly granted user IDs. Every content identity, support link, operation, attempt, trace and idempotency key stays within that pair. Existing physical `scope_id` columns store the exact `user_id`; there is no third selector, identity mapping or separate collection namespace.

PostgreSQL row-level security is a mandatory second boundary behind explicit SQL predicates. Migration credentials are separate from non-owner, non-superuser, non-`BYPASSRLS` API and worker roles. A worker claims metadata through a narrow queue role, then binds the authorized tenant/user pair in a separate content transaction. Reuse the existing policies, keys and collection lock; this public terminology needs no storage migration. Administrator `POST /v1/users` creates a collection with explicit creator grants, never wildcard content access.

The operator runs migration and initial administrator bootstrap before starting application replicas. Bootstrap serializes one default tenant, administrator, and credential; it commits their identity before handing the secret to a new operator-only file. Interrupted handoff requires explicit recovery for that same administrator. Runtime credential administration verifies the actor from the supplied secret digest, issues a separate sibling key for rotation, and revokes only same-tenant credentials. API processes never receive migration credentials. [Bootstrap](../src/oam/auth/bootstrap.py), [authentication](../src/oam/auth/repository.py), and [HTTP composition](../src/oam/api/runtime.py) own these boundaries.

The [configuration loader](../src/oam/config.py) validates canonical YAML, overlays an explicitly selected partial operator configuration, and freezes the resolved values. [HTTP admission](../src/oam/api/app.py) uses those same values for request limits and capabilities; request schemas enforce structure without supplying a second set of numeric limits. Full-release readiness remains false while later product gates remain unimplemented.

## 2. Evidence, Projections, and Resources

Eligible interactions enter L0 and may produce L1. Related L1 records may produce supported L2 Insights with one descriptive subtype: `preference`, `pattern`, `project`, `relationship` or `general`. These labels share the same synthesis/support pipeline and do not affect identity or ranking. An explicit Profile refresh freezes eligible L1/L2 and Profile Resource versions and may publish L3. Evidence Resources instead produce detailed passages and a derived abstract; neither output is an input to L1, L2, or L3.

```text
eligible interactions -> L0 -> L1 -> supported L2 --+
eligible L0 detail ----------------------------------+-> requested L3
explicit Profile Resources --------------------------+

text/HTML/PDF/image -> immutable original -> passages + abstract -> joint recall
Evidence Resource ----------------------------------------------X-> L1/L2/L3
```

Every generated claim retains support to immutable source versions and locators. Source revision or deletion changes eligibility before a response or worker publication can use the old derivative.

An L1 memory unit is one concise, independently addressable fact sentence with a stable `memory_id`; raw passages and L2 Insights remain distinct kinds. [HTTP admission](../src/oam/api/app.py) and [runtime composition](../src/oam/api/runtime.py) accept `/v1/retain` JSON interactions/text documents or multipart file metadata and the complete original. Public callers never manage source/message IDs or source revisions. The [source repository](../src/oam/db/sources.py) allocates them internally; existing stored interaction and Resource envelopes remain private worker inputs. The [indexing worker](../src/oam/workers/index_source.py) validates exact caller annotation spans and publishes caller clocks/times on passages and supported facts. Missing extraction does not erase caller-annotated raw evidence. Individual fact forget/remove is not a v0.0.1 API; stable IDs and explicit L2 support prepare that later design without adding a deletion subsystem now.

Retain admission follows credential verification → bounded incoming bytes → JSON/multipart parsing and semantic limits → user grant → atomic source/receipt acceptance. The credential transaction closes before waiting on the upload. The endpoint counts actual received bytes against `api.max_request_bytes`, including chunked bodies; overflow stops parsing with `413` and closes partial upload files. This encoded-body cap is distinct from the accepted content/original limits and has one owner in [canonical configuration](../configs/oam.yaml).

## 3. Durable Work

An accepted write commits its source/original bytes, idempotency identity, root operation, sealed stage manifest, and first runnable work units before returning `202`. Workers claim ready rows with short PostgreSQL transactions, leases, and monotonically increasing fences; remote model I/O happens outside the transaction.

The [source repository](../src/oam/db/sources.py) takes the existing collection lock before checking request idempotency or allocating source/revision identities. A text document's `append` reads the current text, concatenates the exact supplied delta without separators and validates the completed size inside that same transaction, so concurrent appends cannot lose content. Documents use a stable `document_id`; `replace` supplies a complete new text/file revision, and file append is rejected. Source activation rejects deleted sources; originals require a read grant. The [claim function](../migrations/0001_initial.sql) locks work rows without escalating queue privileges and withholds any unit with an unresolved external outcome until explicit [attempt reconciliation](../src/oam/operations/repository.py). The [indexing worker](../src/oam/workers/index_source.py) consumes accepted claims, persists compatible model-stage outputs and publishes supported representations. User-qualified operation routes expose readiness under the same grants. The complete core flow is specified in [indexing and recall](indexing-retrieval.md).

Publication rechecks operation cancellation, lease/fence ownership, source version, learning eligibility, generation, and current policy. A late or stale worker may preserve diagnostic attempt evidence but cannot publish canonical output. Retry reuses compatible completed work and creates a new attempt only for unfinished eligible stages.

A completed model response retains independently validated usage even when its generation is truncated/refused/empty or its embedding/scoring output is invalid. The [model adapter](../src/oam/models/runtime.py) carries that metering to the [existing attempt failure path](../src/oam/indexing/store.py); unavailable or malformed metering is never invented. Retryable extraction/Insight JSON/schema output uses the remaining durable claim budget: finalize the failed attempt and requeue its work atomically, then reuse completed stage checkpoints on the next claim. Model-adapter output rejection and input/output limits are not automatically retried merely because their usage is known. Network timeouts without a received response remain unresolved and are never automatically replayed. Successful-stage and known failed-attempt tokens are counted once each for the same source revision and generation; exhausted retries remain an explicit partial failure.

## 4. Retrieval and Profiles

The [recall service](../src/oam/retrieval/service.py) and [eligible SQL repository](../src/oam/retrieval/repository.py) implement one non-generative fast/mid/deep pipeline. An explicit ranking window wins; otherwise [deterministic query-time parsing](../src/oam/retrieval/query_time.py) resolves one event window from supported English/Chinese or bounded Japanese expressions. Ambiguous, invalid or unsupported implicit dates disable only the temporal arm and produce diagnostics, not request failure. All modes retain lexical, anchor and dense candidates without date exclusion while temporal spreading uses strict basis/window-qualified seeds, neighbors and path witnesses. Ordinary graph/hierarchy retrieval likewise remains free of date exclusion. Tenant/user, source and generation eligibility remain hard constraints everywhere.

Fast uses its shorter deadline and caps temporal nodes at `min(search.temporal_nodes, search.fast.max_results)`, so pre-agent recall can follow temporal links; it skips ordinary graph, hierarchy and reranking. Mid and deep share entity/semantic links, two-hop outgoing causal paths, L2 sibling paths and reranking when bound; only numeric budgets differ. Without a reranker binding, both use reciprocal-rank-fused results without degradation. All modes share citation hydration and exact context packing. Memory and Evidence compete under one result/token budget without origin quotas. The [core acceptance gate](acceptance.md#2-step-5-core-indexing-and-recall-gate) functionally tests all three modes with Profiles disabled before L3 implementation.

The [canonical configuration](../configs/oam.yaml) owns all three presets and deadlines. Public `POST /v1/recall` requires `user_id` and `query`, defaulting to mid when `recall_level` is omitted; the host selects fast explicitly before invoking the agent. Each pre-agent or ReAct recall gets its own operation budget, clamped by an explicit shorter caller deadline. Trusted shared-turn admission is an opt-in internal constraint, not a public body field. No automatic escalation occurs.

The host runs fast recall before invoking the agent and supplies its evidence as initial context. During the ReAct loop, where the agent alternates reasoning and tool calls, it may request fast, mid, or deep again. OAM executes the selected preset; the agent owns subsequent queries, mode choices, and the final answer.

`GET /profile` reads stored eligible fields and freshness only. `POST /profile/refresh` performs the cheap input-identity check: unchanged inputs return `200` and zero generation calls; changed inputs return a durable `202` operation. L2 changes dirty L3 without generating it, and Evidence Resource changes do not dirty L3.

## 5. Runtime Topology

One application image eventually runs either API or worker mode. The API also serves version-matched Inspector assets at `/ui/`. Development uses one API, one worker, and PostgreSQL; the reference application topology uses at least two API and two worker replicas over shared PostgreSQL. This proves application-pod recovery, not PostgreSQL HA or multi-region operation.
