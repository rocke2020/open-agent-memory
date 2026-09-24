# Open Agent Memory HTTP API

## Overview

HTTP/OpenAPI is the only required v0.0.1 integration surface. The implemented authentication modes are `dev`, `api_key` and `trusted`; OIDC and LDAP are not available. In `api_key` mode, a user key binds the tenant and `user_id`, and an administrator key cannot select personal content. Retain accepts interactions or documents, and recall returns cited context without generating an answer. No independent collection selector, browser-only policy or Python SDK is required. The core routes below have PostgreSQL-backed functional coverage; complete-release readiness remains separate work.

## 1. Global Routes

Global routes expose service status and configuration without returning retained user content.

- `GET /health/live` reports process liveness only.
- `GET /health/ready` returns `503` with `ready: false` and `reason: core_only` while later release capabilities are incomplete; it also lists unbound required model roles and never performs model calls.
- `GET /v1/capabilities` publishes contract/configuration/index-generation identity, implemented features and effective limits. Feature presence is not a model/database availability probe or a release-readiness claim.
- `GET /ui/` serves the same-origin Inspector shell with no embedded content or persisted credential.

## 2. Administration

Mode selection belongs to `auth.mode` in the operator configuration. When it is omitted, an active root key selects `api_key`; a fresh rootless database selects loopback-only `dev` and records that selection for restarts. An incomplete root-key bootstrap or an existing keyed database without an active root fails startup. `trusted` must be selected explicitly. `dev` has no authentication or personal privacy boundary and disables key-administration routes. In `trusted` mode, a local data request supplies `X-OAM-Tenant` and `X-OAM-User` without a key; a nonlocal data request also supplies a root key as `Authorization: Bearer <root-key>`. The asserted user must match a path or body `user_id`, and an unknown or disabled tenant cannot be created by assertion. The nonlocal gateway must authenticate callers, replace client-supplied identity headers and block direct access to the data listener. `X-OAM-Role` accepts `user` or `admin` only when the request also carries a valid root key; it cannot assert `root` or bypass the root-key requirement on admin routes.

`POST /v1/users` accepts `{"user_id":"lin"}` from a tenant administrator and atomically creates the personal collection, bound user principal, grant and first user credential. It returns `user_id`, `credential_id` and the secret once after commit. The administrator gets no personal-content grant; old administrator grants remain stored but cannot authorize content under the current SQL function. The user's key governs retain, recall and operation inspection for that user only.

Credential administration issues additional opaque credentials, lists credential metadata, and revokes credentials. Issuance returns each secret once; stored records contain only the SHA-256 digest and immutable tenant/principal binding.

Direct root-key denial is not secrecy from the root operator: an operator who issues and receives a user key can authenticate with that user key.

The implemented credential routes are `GET /v1/credentials`, `POST /v1/credentials`, and `DELETE /v1/credentials/{credential_id}`. Issuance accepts exactly one of a same-tenant `principal_id` or `user_id`, plus optional timezone-aware `expires_at`; `user_id` issues a sibling key for that existing user. It returns the new secret only after commit. Listing accepts `after` and a positive `limit` up to the configured `auth.credential_page_size`, returning metadata and `next_after`. Revocation is idempotent, returns `revocation_effective_by`, and evicts the local cache after commit; unknown and foreign credential IDs have the same response. Every administration request verifies the current credential directly in PostgreSQL.

## 3. Retain and Recall

`api_key` content requests use `Authorization: Bearer <user-key>`; `trusted` uses asserted tenant/user headers and adds a root key on nonlocal binds; `dev` needs no key on loopback. Retain requires an `Idempotency-Key` identifying the logical request; retry with the same key and unchanged input to reuse its receipt instead of duplicating an interaction or append.

Retain resolves the mode-specific identity before consuming the request body. `api.max_request_bytes` bounds the complete encoded retain body, including JSON escaping or multipart headers/metadata, separately from the interaction-content and Resource-original limits. Its canonical default is 24 MiB and its effective value is published by capabilities. OAM counts actual incoming bytes even without `Content-Length`; an oversized body returns `413 RequestTooLarge` before further buffering/parsing, and partial multipart files are closed. The identity's user binding, grant and semantic content limits are checked after the bounded body identifies its user and input type, before accepting work or making model calls.

| Route | Contract |
|---|---|
| `POST /v1/retain` | Accept JSON interaction/text document, or multipart document metadata plus the complete `original` file; return `202` with a durable operation and server-assigned source identity. |
| `POST /v1/recall` | Accept `user_id` and `query`; run non-generative `fast`, `mid` or `deep` recall, defaulting to `mid`. |
| `GET /v1/users/{user_id}/operations/{operation_id}` | Inspect accepted work, readiness, failures and usage under the same user grant. |
| `POST /v1/users/{user_id}/operations/{operation_id}/{action}` | Use `cancel`, `retry` or `reconcile-attempt` with the existing operation capability and policy checks. |

### Interaction

The host supplies every message's role, exact content, timezone-aware `event_time` and explicit `learning_eligible` decision. OAM assigns the interaction source identity and uses message order for locators. A saved document-derived assistant answer must remain ineligible for learning.

```json
{
  "user_id": "lin",
  "input_type": "interaction",
  "messages": [
    {"role": "user", "content": "Please keep my checkup summaries concise.", "event_time": "2026-09-23T10:00:00+08:00", "learning_eligible": true},
    {"role": "assistant", "content": "The report lists a September checkup.", "event_time": "2026-09-23T10:00:02+08:00", "learning_eligible": false}
  ]
}
```

### Text or File Document

A document supplies a stable `document_id` within its tenant/user pair and an explicit `update_mode`. Text `replace` supplies the full body; text `append` supplies only the new text for an existing text document. Append concatenates exactly, without inserting whitespace or separators, and allocates the next immutable revision atomically. Include any desired separator in the supplied text.

```json
{
  "user_id": "lin",
  "input_type": "document",
  "document_id": "checkup-2026",
  "update_mode": "replace",
  "text": "Checkup date: 2026-09-20."
}
```

File documents use `multipart/form-data` on the same `/v1/retain` endpoint. The `metadata` part contains `{"user_id":"lin","input_type":"document","document_id":"scan-2026","update_mode":"replace"}`; the `original` part contains the complete file with its filename and media type. OAM validates size/type and computes its length and digest. File append is rejected; replace the complete HTML/PDF/image file or use a separate document ID. PDF/image processing remains a later integration gate.

Neither format accepts caller-managed `source_id`, `message_id`, `source_version`, `expected_active_version`, `byte_length` or `sha256`. OAM owns source/revision allocation; the latest accepted document revision immediately excludes older derivatives while new indexing is pending. Documents remain Evidence and never automatically become learned facts or Insights.

### Recall

The minimal body is `{"user_id":"lin","query":"How should I summarize the checkup?"}`. The host selects `"recall_level":"fast"` explicitly for pre-agent retrieval and may set `max_context_tokens`; later agent calls may select any preset independently. `allow_partial` defaults to false.

Optional paired `time_basis`/`time_window` override query-date parsing. The half-open window affects temporal ranking only, not other retrieval arms or final inclusion. Without a window, OAM uses one captured UTC reference instant, or the supplied timezone-aware `reference_time` and IANA `reference_timezone`. Supported English/Chinese dates and bounded Japanese expressions can enable temporal spreading even in fast mode. Ambiguous, invalid or unsupported implicit dates leave ordinary recall running with a diagnostic; invalid explicit windows fail validation. Recall accepts no independent tenant or collection selector and makes zero generative model calls.

## 4. Response Semantics

Every response carries a request ID and contract version. Accepted asynchronous work distinguishes `accepted`, `source_searchable`, `memory_ready`, `insights_ready`, and Profile freshness; success at one stage never implies later stages completed.

Recall returns typed `items`, exact rendered `context`, `context_tokens`, `tokenizer`, and diagnostics for query-time interpretation, executed/omitted stages, executed fusion arms, partial coverage, usage and timings. Each Insight item includes its assigned subtype and exact supporting facts; empty results do not invent evidence. The context-token count covers the exact returned context, including citations and time qualifiers.

Errors use safe details; validation and authorization complete before external calls. Invalid credentials return `401`; retain wire-body overflow returns `413`; conflicting reuse of an idempotency key returns `409`; invalid explicit input returns `422`; unavailable capability/mandatory recall stages return `503`; exhausted deadlines return `504`. Unavailable authority uses a non-enumerating `404` where required.

## 5. Current Implementation Status

The user-qualified retain/recall surface, server-managed document updates, Insight subtypes and temporal ranking are implemented. Current tests cover atomic append/idempotency, exact source support, temporal ranking without global date exclusion, fast multi-hop spreading and readiness diagnostics. Real-model probes have exercised supported L2 and separate HTML Evidence through the public API; rejected derived annotations remain visible as partial indexing coverage. Profile inclusion, separate source-chunk output, batch/planner, full Resource/lifecycle and Inspector integration remain later gates. OpenAPI presence alone is not an availability or release claim.
