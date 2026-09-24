# Open Agent Memory Operations

## Overview

OAM uses one canonical behavior file and explicit operator commands. No required role silently falls back to a provider or model; no accepted asynchronous write depends on a client connection remaining open.

## 1. Configuration

`configs/oam.yaml` owns authentication mode, limits, role aliases, retrieval/profile policy, worker concurrency, leases, and retention. An alternate operator YAML may override selected fields; omitted fields come from the canonical YAML, never a second set of Python numeric defaults. At CLI startup, inherited process variables take precedence and `./.env` fills only missing variables. Environment variables supply the PostgreSQL DSNs, the private `OAM_ROOT_API_KEY` value when operator-supplied, model identifiers selected through `model_env`, model endpoint/key values referenced by the selected binding, operator secret-handoff destinations, and an optional alternate config path. An environment variable never overrides a behavioral default invisibly.

Required v0.0.1 roles are extraction, Insights, embedding, document vision, Resource abstract, and Profile generation. Reranker and planner are optional. Each binding declares `protocol: openai_compatible`, exactly one of `model` or `model_env`, an endpoint environment variable, an optional key environment variable, and capabilities; preflight verifies the role-specific behavior rather than trusting a provider name.

## 2. Commands

- `oam preflight` resolves configuration, redacts secrets, and tests enabled role capabilities.
- `oam migrate` applies versioned PostgreSQL migrations with a separate migration credential.
- `oam api` runs the FastAPI role and bundled Inspector.
- `oam worker` runs source indexing with separate queue/content database roles. `--once` processes at most one queued source and exits. The long-running process bounds concurrent sources by the minimum configured task, model, provider, database-pool and Scope limits; each source's stages remain serial.
- `oam recover-bootstrap` uses separate administrative database authority to replace a committed bootstrap key whose one-time handoff failed.

## 3. Bootstrap and Credentials

The operator initialization step is `oam migrate`, before starting API or worker replicas. With `auth.mode` omitted and neither `OAM_ROOT_API_KEY` nor `OAM_BOOTSTRAP_OUTPUT` set, a fresh database gets the active `default` tenant without a root credential; `oam api` selects `dev` and must bind to loopback. Explicit rootless `trusted` mode also creates that default tenant and accepts local gateway identity headers; nonlocal trusted startup requires an active root key. `OAM_ROOT_API_KEY` stores an operator-supplied root secret as a database digest without writing a handoff file. Alternatively, `OAM_BOOTSTRAP_OUTPUT` names a new operator-only file for a generated root secret; the identity commits before the secret is written once with restrictive permissions, never ordinary logs. Exactly one bootstrap process wins, and interrupted generated-secret handoff requires explicit recovery. An empty root-secret value or simultaneous supplied secret and handoff destination fails. Existing keyed databases keep `api_key` mode when mode is omitted and never silently downgrade to `dev`; incomplete bootstrap fails startup. Application replicas receive only their separate runtime-role DSNs. The administrator creates a user through `POST /v1/users`, receives its first user key once, and uses that key for personal data calls; the root credential itself cannot use personal routes in `api_key` mode.

Positive credential verification may be cached for a fixed non-sliding 30 seconds under the full supplied-secret digest. Invalid keys are not cached. Revocation commits first, prevents an older lookup from repopulating the cache, and reports the maximum cross-process propagation deadline.

After an interrupted `committed` handoff, rerun `oam migrate` to record `recovery_required`, then explicitly run `oam recover-bootstrap` with a new recovery destination. Recovery commits revocation of the lost key and issuance of its replacement before handoff. A further interruption again requires explicit recovery; it never silently creates another administrator.

## 4. Limits and Failure Ownership

The first supported workload limits each Resource to 20 MiB, 20 PDF pages or images, 40 megapixels per image, and 100 megapixels total; each tenant/user pair supports 100 reports and 1,000 chats in the trial profile; the deployment admits 10 simultaneous API requests. Inputs exceeding bounds fail before parsing or model dispatch.

The current worker records compatible stage outputs and usage before dependent work and preserves completed stages after failure. Unknown model outcomes remain explicit. Shutdown stops new admission and drains complete active sources before closing the model client. The current concurrency cap is per process and whole-source draining has no fixed shutdown bound; cluster-wide permits, bounded shutdown and replica recovery remain release gates, not current guarantees.

## 5. Current Implementation Status

The core adds eligible interaction and HTML indexing, supported L1/L2 publication and fast/mid/deep direct recall to the [foundation](foundation-evidence.md). A separate API/worker probe with configured real models has exercised that core path; progressive LongMemEval evaluation is underway and has not passed. L3, full PDF/image ingestion, lifecycle/Inspector integration, deployment permits and multi-replica recovery remain later gates. [Architecture](architecture.md) describes the current pipeline, and [acceptance](acceptance.md) defines the remaining gates.
