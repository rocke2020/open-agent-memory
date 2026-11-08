# v0.0.1 candidate evidence

## Overview

The November 8, 2026 candidate is prepared for human review. Local application, security, packaging and integration checks are complete. Source visibility, image/package publication, remote CI execution and public-download verification have not been performed. See [candidate identities and measurements](release-candidate.json) and [publication instructions](releasing.md).

The initial preparation snapshot was based on `32f97cca5c5c1fd2546ed0364d2279063afc81b6`, with runtime/build fingerprint `fdf17b14b455fea0aefa887165f724f5236ab0ff81c9a44cd894c5fb0919ceb1`; that preparation run made no commit or push. Its source archive, sanitized evidence and broader matrix below remain preserved under that identity. A subsequent surgical review repaired a stale Inspector deletion refresh and private temporary-file creation. The JSON manifest retains the initial results under `preparation_baseline` and identifies the repaired candidate with fingerprint `bc7518fc1418dfa0656e80b5e31ae9af949fb808656fcb4c75c3d34db9a50c2d`, current image IDs and unchanged plugin checksums.

Fresh review checks passed 2,325 backend cases in one full run and 34 browser cases in a separate serialized run: 2,359 distinct Python cases, with zero skips. The focused affected lane also passed 184 cases. Both plugin suites, both native archive installers, OpenClaw setup validation, lint/format/type checks and both architecture builds/inventories passed. A fresh isolated Compose deployment applied migrations and runtime roles, started two APIs/two workers, exercised Profile Data revisions/conflicts/deletion and exact original/UI bytes, then stopped gracefully. These follow-up checks made zero provider calls; the broader model/host/recovery matrix below describes the retained preparation baseline.

## 1. Preparation verification matrix

| Surface | Measured result | Boundary |
|---|---|---|
| Python product matrix | 2,357 distinct passing cases, combined current evidence; no remaining skips | The backend run had 2,323 passes and one fixture failure; after the credential fix, all 13 cases in the affected file passed. The serialized browser lane passed 33 cases. This is not a claim that one invocation returned 2,357 passes. |
| Static checks | Ruff lint/format, mypy, JavaScript syntax and whitespace checks passed | Mypy retains the declared legacy baseline; two untyped-body notes remain informational. |
| Plugin suites | OpenClaw 60; DSH 11 passing cases | Deterministic admission, failure, context and lifecycle coverage supplements real host paths. |
| Provider preflight | Extraction text/vision, Insights, abstracts, Profile and embedding passed | The configured OAM generation binding uses `openai_chat`; other protocol coverage is deterministic. Planner and reranker were unconfigured. |
| Golden HTTP path | Retain, durable receipts, supported L0/L1, generated persisted L2, cited fast/normal recall, current L3 and unchanged zero-call refresh | Two independent dated synthetic interactions were indexed; an additional accepted source exercised recovery and incremental refresh. Ordinary recall made zero generation calls. |
| Evidence | HTML/PDF/PNG originals independently hash-matched; expected lab values and joint learned/Evidence recall | Original preservation is exact; vision transcription quality is not guaranteed. |
| Lifecycle | Structured Profile Data create/revise/conflict/delete, exact Profile value, portable export/checksums/invalidation, exact L1/L2 removal and unrelated-memory preservation | Deterministic SQL/HTTP cases additionally cover source exclusion, stale work, history, races and cleanup. |
| Inspector | Existing Chrome loaded the exact image; real CRUD, exact removal, safe original text, Profile reuse, sign-out clearing, light/dark and 390px layout passed; no console errors | Original/source content remains plain text. JSON editing applies only to registered owner-supplied Profile Data; generated prose is read-only. |
| Authentication | Full auth/RLS matrix plus actual trusted gateway caller authentication, replaced spoofed headers, denied admin/cross-user access and blocked direct backend listener | Two isolated Mac-local Docker networks; no external identity provider or production gateway certification. |
| Replicas/recovery | Two API/two worker replicas, shared original bytes, ten concurrent reads using both APIs, accepted work retained while workers stopped, survivor processing after worker loss, application restart and rolling replacement | Corrected API kill/restart had 15 successful read samples; the final-image rolling run had 51, both with zero failures. Transparent replay of already-sent writes is not claimed. |
| Containers | Both architectures ran as UID 10001; 139 packaged runtime files matched source bytes; 53 installed runtime distributions inventoried | ARM64 native; AMD64 emulated on the ARM64 Mac. Both use the same non-root API/worker image and existing migrations. |
| Real hosts | OpenClaw 2026.11.1 and DSH 0.2.2-rc.1 installed the exact local archives, captured/indexed completed human turns, recalled citations in fresh sessions and used explicit native search; final-image fresh host checks and exact-ID agent removal preserving the original passed | DSH source revision is recorded in the evidence bundle. Some exclusions use deterministic module/HTTP coverage rather than every physical transport or scheduler. |
| Hygiene | Candidate files and all 130 reachable commits had zero secret findings; a synthetic known-bad control was rejected; notices and licenses inspected | Both plugin archives carry their own license; image dependency notices and system libpq copyright are retained. Public acknowledgments are prepared for the publication boundary. Four initial scanner matches were verified public source-file SHA-256 values; the typed hash representation avoids those false positives without a scanner allowlist. |

The new Inspector Profile Data path reuses existing collection authorization, immutable revisions, original download, exact mappings and durable receipts. It adds no backend route, schema, memory algorithm or automatic Profile generation. Its real HTTP/browser regression test first failed on the missing tab, then passed; broader navigation and removal cases passed after review fixes.

## 2. Commands and configuration

Default checks are documented in the repository instructions. The enabled matrix used an explicitly task-owned PostgreSQL 18/pgvector server, inherited `PGPASSWORD`, isolated fixture scopes, an existing debug Chrome at `127.0.0.1:9222`, and a populated synthetic scope for the read-only readiness-plan test. No persistent personal/evaluation database was selected. Browser checks ran serially because Chrome is shared.

```bash
uv run --with playwright pytest -q -ra tests --ignore=tests/browser --ignore=tests/integration/test_inspector_browser.py
uv run --with playwright pytest -q -ra tests/browser tests/integration/test_inspector_browser.py
uv run --with playwright pytest -q -ra tests/integration/test_vector_indexes.py
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
node --check src/oam/web/app.js
node --check src/oam/web/locales.js
```

The container stack uses separate non-owner API, queue and content logins over shared PostgreSQL. The [deployment guide](../deploy/README.md) gives the startup/migration sequence, replica overlay and exact bounded synthetic probe configuration. Probe reservations were limited to 300,000 generation tokens per source, with explicit 16,384/8,192 output ceilings and a shortened L2 delay. The canonical production delay remains 24 hours. The task-specific operator file and resolved configuration identity accompany the evidence; credentials are excluded.

The account-free archive path was tested through each host's native plugin manager. OpenClaw setup now selects OAM and disables its competing built-in memory entry in one validated configuration batch. Both hosts continued through backend unavailability; whole-context limits were exercised with tiny caps. DSH's independent 381-token control produced zero injected memory messages under its four-token cap. OpenClaw's corresponding control was 764 tokens, and the capped host continued with the automatic-recall warning.

## 3. Usage, failures and limits

The retained November 8 accounting conservatively identifies 74 generation admissions/requests: 44 released OAM generation permits, 15 native OpenClaw model records, ten native DSH calls and five generation preflight requests. OAM's durable operation summaries retain 77,856 reported input and 37,407 output tokens, including a 32,214-token reasoning subset. The native hosts reported 58,300 and 128,767 total tokens respectively. OAM also recorded 103 embedding admissions; preflight used one additional embedding request. Embedding usage is separate from generation performance. A released permit can include a local abort, so the exact runtime transport-dispatch count is not independently measured. Failed/unknown portions, preflight token usage, controller usage and the four native review turns prevent a complete token or monetary total. Earlier historical evidence is excluded from these preparation-run totals.

Failed and intermediate artifacts are retained. The initial conservative Insight reservation exhausted a source budget before its next dispatch; a bounded source-batch fixture then reached full readiness without raising its 300,000-token limit. The first gateway settings produced transient 502s during restart; corrected upstream eligibility and the documented warm replacement/reload sequence passed the later checks. A source test's old operation-count denominator and a subprocess credential fixture were repaired to test the implemented contracts. An overlapping Chrome run was stopped and the browser matrix rerun serially.

The tiny 240×100 PNG's source date is 2026-08-14, but one vision-derived passage read 2028-08-14. Sodium and glucose values were correct and the original bytes were independently preserved. This observed transcription error remains a quality limitation; no dataset-specific correction or OCR-perfect claim was added.

Historical LME-500 and the retained complete BEAM evidence remain labeled development-snapshot evidence. They were not rerun and do not establish a fresh matched provider comparison or current-checkout benchmark score. There is no numerical publication threshold. Physical multi-host failure, database HA, remote CI, published registry pulls and public-download installation remain unverified. The maintainer owns final content review, repository visibility and publication.
