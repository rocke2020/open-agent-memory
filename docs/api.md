# Open Agent Memory HTTP API

## Overview

HTTP/OpenAPI is the only required v0.0.1 integration surface. A credential determines the tenant; an explicitly authorized `user_id` identifies that user's personal memory collection. Retain accepts interactions or documents, and recall returns cited context without generating an answer. No independent collection selector, browser-only policy or Python SDK is required. The core routes below have PostgreSQL-backed functional coverage and a separate-process real-model probe; that does not establish benchmark performance or complete-release readiness.

## 1. Global Routes

Global routes expose service status and configuration without returning retained user content.

- `GET /health/live` reports process liveness only.
- `GET /health/ready` returns `503` with `ready: false` and `reason: core_only` while later release capabilities are incomplete; it also lists unbound required model roles and never performs model calls.
- `GET /v1/capabilities` publishes contract/configuration/index-generation identity, implemented features and effective limits. Feature presence is not a model/database availability probe or a release-readiness claim.
- `GET /ui/` serves the same-origin Inspector shell with no embedded content or persisted credential.

## 2. Administration

`POST /v1/users` accepts `{"user_id":"lin"}` from a tenant administrator and creates the personal collection with explicit creator grants. Administrative authority alone never grants access to every user's content. The same credential-derived tenant and user grant govern retain, recall and operation inspection.

Credential administration issues additional opaque credentials, lists credential metadata, and revokes credentials. Issuance returns each secret once; stored records contain only the SHA-256 digest and immutable tenant/principal binding.

The implemented credential routes are `GET /v1/credentials`, `POST /v1/credentials`, and `DELETE /v1/credentials/{credential_id}`. Issuance accepts an existing same-tenant `principal_id` and optional timezone-aware `expires_at`, and returns the new secret only after commit. Listing accepts `after` and a positive `limit` up to the configured `auth.credential_page_size`, returning metadata and `next_after`. Revocation is idempotent, returns `revocation_effective_by`, and evicts the local cache after commit; unknown and foreign credential IDs have the same response. Every administration request verifies the current credential directly in PostgreSQL.

## 3. Retain and Recall

All content requests use `Authorization: Bearer <opaque-key>`. Retain requires an `Idempotency-Key` identifying the logical request; retry with the same key and unchanged input to reuse its receipt instead of duplicating an interaction or append.

Retain verifies the credential before consuming the request body. `api.max_request_bytes` bounds the complete encoded retain body, including JSON escaping or multipart headers/metadata, separately from the interaction-content and Resource-original limits. Its canonical default is 24 MiB and its effective value is published by capabilities. OAM counts actual incoming bytes even without `Content-Length`; an oversized body returns `413 RequestTooLarge` before further buffering/parsing, and partial multipart files are closed. User-specific grants and semantic content limits are checked after the bounded body identifies its user and input type, before accepting work or making model calls.

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
