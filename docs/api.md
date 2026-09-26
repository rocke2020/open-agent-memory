# Open Agent Memory HTTP API

## Overview

HTTP/OpenAPI is the only required v0.0.1 integration surface. The implemented authentication modes are `dev`, `api_key` and `trusted`; OIDC and LDAP are not available. In `api_key` mode, a user key binds the tenant and `user_id`, and an administrator key cannot select personal content. Retain accepts interactions, the Resource collection accepts Evidence and Profile originals, and recall returns cited context without generating an answer. No independent collection selector, browser-only policy or Python SDK is required. The core routes below have PostgreSQL-backed functional coverage; complete-release readiness remains separate work.

## 1. Global Routes

Global routes expose service status and configuration without returning retained user content.

- `GET /health/live` reports process liveness only.
- `GET /health/ready` returns `503` with `ready: false` and `reason: core_only` while later release capabilities are incomplete; it also lists unbound required model roles and never performs model calls.
- `GET /v1/capabilities` publishes contract/configuration/index-generation identity, implemented features and effective limits. Feature presence is not a model/database availability probe or a release-readiness claim.
- `GET /ui/` serves the same-origin Inspector client with no embedded content or persisted credential.

## 2. Administration

Mode selection belongs to `auth.mode` in the operator configuration. When it is omitted, an active root key selects `api_key`; a fresh rootless database selects loopback-only `dev` and records that selection for restarts. An incomplete root-key bootstrap or an existing keyed database without an active root fails startup. `trusted` must be selected explicitly. `dev` has no authentication or personal privacy boundary and disables key-administration routes. In `trusted` mode, a local data request supplies `X-OAM-Tenant` and `X-OAM-User` without a key; a nonlocal data request also supplies a root key as `Authorization: Bearer <root-key>`. The asserted user must match a path or body `user_id`, and an unknown or disabled tenant cannot be created by assertion. The nonlocal gateway must authenticate callers, replace client-supplied identity headers and block direct access to the data listener. `X-OAM-Role` accepts `user` or `admin` only when the request also carries a valid root key; it cannot assert `root` or bypass the root-key requirement on admin routes.

`POST /v1/users` accepts `{"user_id":"lin"}` from a tenant administrator and atomically creates the personal collection, bound user principal, grant and first user credential. It returns `user_id`, `credential_id` and the secret once after commit. The administrator gets no personal-content grant; old administrator grants remain stored but cannot authorize content under the current SQL function. The user's key governs retain, recall and operation inspection for that user only.

Credential administration issues additional opaque credentials, lists credential metadata, and revokes credentials. Issuance returns each secret once; stored records contain only the SHA-256 digest and immutable tenant/principal binding.

Direct root-key denial is not secrecy from the root operator: an operator who issues and receives a user key can authenticate with that user key.

The implemented credential routes are `GET /v1/credentials`, `POST /v1/credentials`, and `DELETE /v1/credentials/{credential_id}`. Issuance accepts exactly one of a same-tenant `principal_id` or `user_id`, plus optional timezone-aware `expires_at`; `user_id` issues a sibling key for that existing user. It returns the new secret only after commit. Listing accepts `after` and a positive `limit` up to the configured `auth.credential_page_size`, returning metadata and `next_after`. Revocation is idempotent, returns `revocation_effective_by`, and evicts the local cache after commit; unknown and foreign credential IDs have the same response. Every administration request verifies the current credential directly in PostgreSQL.

## 3. Retain and Recall

`api_key` content requests use `Authorization: Bearer <user-key>`; `trusted` uses asserted tenant/user headers and adds a root key on nonlocal binds; `dev` needs no key on loopback. Retain requires an `Idempotency-Key` identifying the logical request; retry with the same key and unchanged input to reuse its receipt instead of duplicating an interaction or append.

Newly asserted personal users in `dev` and `trusted` receive the same Resource, source-control, debug and export grants as newly created keyed personal users. Migration 0012 leaves existing grants unchanged. An operator may add missing capabilities for a verified existing user after reviewing that user's intended access.

Write routes resolve the mode-specific identity before consuming the request body. `api.max_request_bytes` bounds the complete encoded body, including JSON escaping or multipart headers/metadata, separately from the interaction-content and Resource-original limits. Its canonical default is 24 MiB and its effective value is published by capabilities. OAM counts actual incoming bytes even without `Content-Length`; an oversized body returns `413 RequestTooLarge` before further buffering/parsing, and partial multipart files are closed. The identity's user binding, grant and semantic content limits are checked after the bounded body identifies its user and input type, before accepting work or making model calls.

| Route | Contract |
|---|---|
| `POST /v1/retain` | Accept a JSON interaction and return `202` with a durable operation and server-assigned source identity. |
| `POST /v1/users/{user_id}/resources` | Accept an Evidence text document, HTTPS URL or multipart file, or a registered structured Profile Resource, distinguished by `resource_type`; preserve the original and return a durable `202` source receipt. An `Idempotency-Key` is required; Profile revision requires `If-Match`. |
| `POST /v1/recall` | Accept `user_id` and `query`; run non-generative `fast`, `mid` or `deep` recall, defaulting to `mid`. |
| `GET /v1/users/{user_id}/profile` | Read selected stored sections, current exact Resource fields, revision and freshness without generation. |
| `POST /v1/users/{user_id}/profile/refresh` | Return `200` with unchanged stored fields and zero generation calls, or `202` with a durable refresh operation. |
| `POST /v1/users/{user_id}/search-many` | Accept a caller-provided `queries` list; run one selected recall level with a shared deadline and context budget, returning ordered per-query outcomes. |
| `POST /v1/users/{user_id}/decompose-and-search` | Accept one query; use one configured planner call to propose bounded searches, or disclose a direct-query fallback, then run the same batch path. |
| `GET /v1/users/{user_id}/resources` and `/resources/{resource_id}` | List active Evidence and Profile Resources with current version and operation state; detail shows Evidence passages/abstract state or the registered Profile subtype and exact mapped fields. |
| `GET /v1/users/{user_id}/resources/{resource_id}/original` | Download either typed Resource's active original with its media type and attachment disposition; source HTML is never rendered as an API page. |
| `GET /v1/users/{user_id}/evidence/{source_id}` | Inspect an active chat source's retained interaction envelope and cited L0 passages under the current user policy. |
| `PUT /v1/users/{user_id}/resources/{resource_id}` | Revise Evidence text, uploaded HTML/PDF/image or HTTPS URL with `Idempotency-Key` and positive integer `If-Match` active version; return a `202` indexing receipt. Profile Resource revision uses `POST /resources` with `If-Match`. |
| `DELETE /v1/users/{user_id}/resources/{resource_id}` and `/evidence/{source_id}` | Exclude every version of an Evidence Resource, Profile Resource or chat source immediately and return a separate cleanup operation receipt. |
| `GET /v1/users/{user_id}/memories` | List current L1 facts and L2 Insights with support and source citations. |
| `GET /v1/users/{user_id}/operations/{operation_id}` | Inspect accepted work, readiness, failures and usage under the same user grant. |
| `POST /v1/users/{user_id}/operations/{operation_id}/{action}` | Use `cancel`, `retry` or `reconcile-attempt` with the existing operation capability and policy checks. |

### Interaction

The host posts to `/v1/retain` and supplies every message's role, exact content, timezone-aware `event_time` and explicit `learning_eligible` decision. OAM assigns the interaction source identity and uses message order for locators. A saved document-derived assistant answer must remain ineligible for learning.

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

A document posts to `/v1/users/lin/resources` with a stable `document_id` within its tenant/user pair and an explicit `update_mode`. Text `replace` supplies the full body; text `append` supplies only the new text for an existing text document. Append concatenates exactly, without inserting whitespace or separators, and allocates the next immutable revision atomically. Include any desired separator in the supplied text.

```json
{
  "resource_type": "evidence",
  "input_type": "document",
  "document_id": "checkup-2026",
  "update_mode": "replace",
  "text": "Checkup date: 2026-09-20."
}
```

File documents use `multipart/form-data` on `/v1/users/lin/resources`. The `metadata` part contains `{"resource_type":"evidence","input_type":"document","document_id":"scan-2026","update_mode":"replace"}`; the `original` part contains the complete file with its filename and media type. OAM validates size/type and computes its length and digest. File append is rejected; replace the complete HTML/PDF/image file or use a separate document ID.

An HTTPS Evidence URL uses a JSON body such as `{"resource_type":"evidence","input_type":"document_url","document_id":"scan-2026","update_mode":"replace","media_type":"application/pdf","url":"https://reports.example.org/scan.pdf"}` on `/v1/users/lin/resources`. The declared media type is one of `text/html`, `application/pdf`, `image/png` or `image/jpeg`. OAM authorizes the user before a single bounded fetch, rejects nonpublic destinations, redirects and mismatched returned bytes, and commits the exact fetched original before indexing. A committed identical idempotency key returns its receipt without another fetch; a failed fetch creates no accepted source. URL ingestion does not crawl links or refresh automatically. A URL query may be needed for fetching but is never included in citations or public error text.

Neither retain format accepts caller-managed `source_id`, `message_id`, `source_version`, `expected_active_version`, `byte_length` or `sha256`. OAM owns source/revision allocation; the latest accepted document revision immediately excludes older derivatives while new indexing is pending. `PUT /resources/{resource_id}` is the compare-and-set revision route: send the same document JSON or multipart shape with matching `user_id` and `document_id`, `update_mode: "replace"`, `Idempotency-Key`, and `If-Match: <active-version>`. It checks the current version before reading an uploaded body or fetching an HTTPS URL and rechecks it atomically at acceptance. An exact retry with the same key reuses its original receipt. Documents remain Evidence and never automatically become learned facts or Insights.

### Recall

The minimal body is `{"user_id":"lin","query":"How should I summarize the checkup?"}`. The host selects `"recall_level":"fast"` explicitly for pre-agent retrieval and may set `max_context_tokens`; later agent calls may select any preset independently. Omitted `profile_sections` selects stored `main`; `"profile_sections":[]` disables Profile inclusion. Selected sections are read from storage and share the context budget with Memory and Evidence. `allow_partial` defaults to false.

### Profile Resources and Refresh

`POST /v1/users/lin/resources` currently accepts `{"resource_id":"basic","resource_type":"profile","subtype":"basic-profile/v1","content":{"date_of_birth":"1990-05-10","interests":["hiking"]}}` or registered `preferences/v1` content with `example_language` and `domain`. The caller needs `resource:write` and an `Idempotency-Key`; a revision also requires `If-Match` with the current version number. Known fields map deterministically to `main` or `preferences` and remain excluded from ordinary Evidence recall. A mapping error rejects the candidate before source activation. The complete JSON original is retained under the user and new versions immediately supersede prior exact values.

`GET /v1/users/{user_id}/profile` defaults to `main` and accepts repeated `sections` query parameters plus optional non-negative `max_tokens`. It reports `missing`, `partial`, `dirty`, `stale` or `current` freshness and omits fields whose support is no longer eligible. `POST /v1/users/{user_id}/profile/refresh` accepts `sections`, defaulting to `main`; it does no model work on unchanged complete inputs. Changed inputs queue one durable operation, whose worker validates generated fields against current eligible support before publishing a stored revision. A failed refresh leaves eligible prior fields available with non-current freshness. An ambiguous external model outcome blocks another refresh for the same input identity with `409 ProfileRefreshUnresolved` until the attempt is reconciled. Evidence-only writes do not dirty this input identity.

Optional paired `time_basis`/`time_window` override query-date parsing. The half-open window affects temporal ranking only, not other retrieval arms or final inclusion. Without a window, OAM uses one captured UTC reference instant, or the supplied timezone-aware `reference_time` and IANA `reference_timezone`. Supported English/Chinese dates and bounded Japanese expressions can enable temporal spreading even in fast mode. Ambiguous, invalid or unsupported implicit dates leave ordinary recall running with a diagnostic; invalid explicit windows fail validation. Recall accepts no independent tenant or collection selector and makes zero generative model calls.

Batch search uses the same optional mode and time controls for every caller query; without an explicit window, each query is interpreted separately against one frozen request reference instant. The configured `search.max_queries` and per-query token limit are checked before any child search starts. The total `max_context_tokens` defaults to the configured context budget and is reserved across queries in input order as equal shares with the remainder assigned to earlier queries; an unused share is not redistributed. Each result retains its own `items`, `context`, diagnostics and estimated token count. The batch response reports `ok` or `partial` for failed or incomplete children, ordered query indexes, aggregate estimated `context_tokens`, and safe per-query failure codes; if every query fails, the request is unavailable. A child still running at the deadline is reported as timed out, queued work is canceled, and the read-only child may drain after the response under its own timeout. The query concurrency cap and shared deadline are published through capabilities.

Delegated search requires an optional configured planner model role. It requests one JSON query list with at least 1,024 output tokens, validates the list against the same batch limits, and reports the plan, actual planner call count, model-reported usage and any fallback reason. Planner `cost` is null because this release has no authoritative price binding; missing provider usage remains unknown. An invalid or failed planner call uses the original query under the remaining deadline; it never retries the planner. An unfinished planner request may drain after the deadline response, but no child recall starts after expiration. An unconfigured planner is advertised as disabled and the delegated route returns capability unavailable. The planner does not answer the query; each search result still comes from non-generative recall.

## 4. Source Control, Debugging, and Export

Lifecycle, debug and export use the credential-derived user boundary. Default personal user keys receive distinct `resource:write`, `source:delete`, `debug:read` and `export:read` grants; a root key cannot use these personal routes directly. `GET /resources`, `GET /memories` and Resource detail/original reads apply current source eligibility and reject a policy change during response assembly. List responses contain current items, a stable-ID `next_after` for the next live page and `policy_epoch`; a cursor does not preserve access to withdrawn content.

Source deletion returns `202` with `source_id`, `excluded_versions`, the committed `policy_epoch`, `cleanup_operation_id` and `cleanup_status`. Exclusion is immediate across recall and inspection; the worker later removes source derivatives and original bytes. Repeat deletion returns the existing cleanup receipt. Revision or deletion makes an in-flight recall unavailable if its source policy changed before result publication, while a new request can still retrieve unrelated active sources.

Each successful direct recall returns `trace_id`. `GET /v1/users/{user_id}/debug/traces/{trace_id}` exposes the bounded recorded candidate arms, query hash/options, final IDs, diagnostics and exact result when every referenced unit remains eligible. `POST /v1/users/{user_id}/replays` accepts `{"trace_id":"trace_...","mode":"inspect_recorded"}` or `"reassemble_recorded"`; these modes make no new model calls and report exact, divergent, expired or policy-redacted outcomes. `GET /v1/users/{user_id}/debug/diff?before=trace_a&after=trace_b` compares currently eligible recorded result IDs and context. A source change that invalidates a candidate returns `policy_redacted` without old content. Model reruns and Profile snapshot diff wait for their separate capabilities.

`POST /v1/users/{user_id}/exports` captures the current source set and returns `202` with a ready `export_id` and operation receipt. `GET /exports/{export_id}` returns the format-1 manifest: source revision, policy/generation and projection-epoch identity, per-kind counts, partial-coverage state, SHA-256 checksums and safe relative member paths. `GET /exports/{export_id}/records/{index}` downloads one bounded JSONL part; `GET /exports/{export_id}/originals/{index}` downloads an authorized original. Record kinds distinguish source, L0 evidence and passages, L1 memory/support, L2 Insight/support, Resource abstracts and the currently eligible L3 Profile view with its revision and freshness. Download every declared member into its manifest path beside `manifest.json`, then run `oam validate-export <directory>` to verify format, counts, paths, sizes, checksums, source/original identity and L1–L3 support relationships without a server. Source admission, indexing publication, revision or deletion after capture returns `409 ExportPolicyChanged` on later server downloads until cleanup removes the held export; create a fresh export. Already downloaded files remain under caller control.

The bundled `/ui/` Inspector uses these public routes for L1/L2 provenance, selected read-only Profile sections and refresh, both typed Resource originals and controls, operation readiness, direct recall traces and export receipts. It holds a user credential only in page memory, renders source text without interpreting markup and uses same-origin assets and CSP.

## 5. Response Semantics

Every response carries a request ID and contract version. Accepted asynchronous work distinguishes `accepted`, `source_searchable`, `memory_ready`, `insights_ready`, and Profile freshness; success at one stage never implies later stages completed.

Recall returns typed `items`, the complete rendered `context`, an estimated integer `context_tokens`, `tokenizer: "oam-unicode-quarter-v1"`, `trace_id`, and diagnostics for query-time interpretation, executed/omitted stages, executed fusion arms, partial coverage, usage and timings. Each Insight item includes its assigned subtype and exact supporting facts; empty results do not invent evidence. Selected fact and passage records remain intact in `items`, including full per-fact citations, time evidence and graph paths. In `context`, facts with verified support in an already selected passage are displayed together with that passage at the earliest member rank; their `Source: [passage_id]` references point to its displayed original text and citation. Fact text and time annotations remain visible, while graph paths retain their owning fact IDs in `items`. Recorded reassembly uses the same deterministic formatting. The estimate counts every selected block, including citations, time qualifiers and separators, under one caller budget. It is a local Unicode estimate, not an exact model-token limit or provider usage count; callers with a strict model budget should count the returned context using their model's tokenizer.

Errors use safe details; authorization completes before external fetches or model calls. Invalid credentials return `401`; write-body overflow returns `413`; a stale `If-Match`, changed idempotent request unresolved Profile refresh or expired export policy returns `409`; invalid explicit input or URL content returns `422`; an unavailable URL source returns `502`; unavailable capability, mandatory recall stage or in-flight inspection policy returns `503`; exhausted recall deadlines return `504`. Unavailable authority and hidden source/trace/export IDs use a non-enumerating `404` where required.

## 6. Current Implementation Status

The user-qualified retain/recall, batch/planner, typed Evidence/Profile admission and lifecycle, stored Profiles, recorded debug/export and Inspector paths are implemented. Isolated PostgreSQL checks cover Resource revision/deletion, durable cleanup, active-source inspection, recorded trace redaction, L3 export support and invalidation, L2-backed refresh, unchanged reuse, failed-refresh preservation, Evidence exclusion and requested Profile recall. A local browser check covers typed Profile Resource create/revise/delete, Profile freshness and refresh, source-text safety, narrow layout and credential clearing. These checks do not establish live Profile model or document-vision quality, cross-language quality, multi-replica recovery or release readiness. Separate source-chunk output and model reruns remain unavailable. OpenAPI presence alone is not an availability claim.
