# Open Agent Memory Operations

## Overview

OAM uses one canonical behavior file and explicit operator commands. No required role silently falls back to a provider or model; no accepted asynchronous write depends on a client connection remaining open.

## 1. Configuration

`configs/oam.yaml` owns limits, role aliases, retrieval/profile policy, worker concurrency, leases, and retention. An alternate operator YAML may override selected fields; omitted fields come from the canonical YAML, never a second set of Python numeric defaults. Environment variables supply the PostgreSQL DSNs, model endpoint/key values referenced by the selected binding, operator secret-handoff destinations, and an optional alternate config path. An environment variable never overrides a behavioral default invisibly.

Required v0.0.1 roles are extraction, Insights, embedding, document vision, Resource abstract, and Profile generation. Reranker and planner are optional. Each binding declares `protocol: openai_compatible`, model, endpoint environment variable, optional key environment variable, and capabilities; preflight verifies the role-specific behavior rather than trusting a provider name.

## 2. Commands

- `oam preflight` resolves configuration, redacts secrets, and tests enabled role capabilities.
- `oam migrate` applies versioned PostgreSQL migrations with a separate migration credential.
- `oam api` runs the FastAPI role and bundled Inspector.
- `oam worker` validates its distinct queue/content database roles and remains fail-closed until Step 5 installs stage processors; claim and publication ownership primitives are already implemented and tested independently.
- `oam recover-bootstrap` uses separate administrative database authority to replace a committed bootstrap key whose one-time handoff failed.

## 3. Bootstrap and Credentials

The operator initialization step is `oam migrate`, before starting API or worker replicas. On an empty database it requires `OAM_BOOTSTRAP_OUTPUT` naming a new operator-only file and serializes one default tenant, administrator principal, and permanent opaque credential. Exactly one process wins; all others observe the marker. The identity commits before the secret is written once with restrictive permissions, never ordinary logs. A crash after commit but before handoff enters recovery-required state and never generates another key automatically. Completed initialization does not require an output destination on later migration runs; explicit recovery uses a different new file through `OAM_BOOTSTRAP_RECOVERY_OUTPUT`. Application replicas receive only their separate runtime-role DSNs.

Positive credential verification may be cached for a fixed non-sliding 30 seconds under the full supplied-secret digest. Invalid keys are not cached. Revocation commits first, prevents an older lookup from repopulating the cache, and reports the maximum cross-process propagation deadline.

After an interrupted `committed` handoff, rerun `oam migrate` to record `recovery_required`, then explicitly run `oam recover-bootstrap` with a new recovery destination. Recovery commits revocation of the lost key and issuance of its replacement before handoff. A further interruption again requires explicit recovery; it never silently creates another administrator.

## 4. Limits and Failure Ownership

The first supported workload limits each Resource to 20 MiB, 20 PDF pages or images, 40 megapixels per image, and 100 megapixels total; each Scope supports 100 reports and 1,000 chats in the trial profile; the deployment admits 10 simultaneous API requests. Inputs exceeding bounds fail before parsing or model dispatch.

Workers own retry classification and provider permits. Authentication/schema/refusal failures are terminal; bounded transient failures may retry under the original operation budget. Unknown remote outcomes retain their permit and budget uncertainty until an operator reconciles them. Shutdown stops new claims, renews accepted work while draining, and leaves unfinished rows recoverable.

## 5. Current Implementation Status

Steps 1–4 implement checksum-locked migrations, forced-RLS schema contracts, initial administrator bootstrap, credential rotation, scope repositories and cache behavior, lost-handoff recovery, durable claims/fences, model capability probes, bounded Resource parsing, the API runtime, and static Inspector assets. The foundation has passed isolated PostgreSQL 18 and public HTTP checks recorded in [foundation evidence](foundation-evidence.md). First indexing workers, source-to-search behavior, live model-provider capability, and multi-replica recovery remain unverified or intentionally deferred to later gates.
