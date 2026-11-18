# Personal and custom Profiles

## Overview

OAM stores one personal Profile per user. It can be refreshed explicitly or through an optional automatic-refresh policy. A separate API generates a custom view, such as a work profile, and returns it without saving its instruction or generated content. Ordinary recall remains non-generative and separate from both Profile APIs.

## 1. Stored personal Profile

`GET /v1/users/{user_id}/profile` reads the stored supported document without generating. `POST /v1/users/{user_id}/profile/refresh` accepts the existing optional instruction and `mode` (`incremental`, the default, or `full`). An unchanged incremental request returns `200` with zero generation calls; accepted work returns `202` with an operation ID. Follow that operation and read the Profile after successful publication.

Read and partially update automatic settings through `GET` and `PATCH /v1/users/{user_id}/profile/policy`. Reading requires `memory:read`; updating requires both `memory:read` and `memory:append`. The response contains `policy`, `paused`, `pending_mode`, `last_success_at`, and `last_full_at`. All automatic triggers default off.

| Policy option | Behavior |
| --- | --- |
| `mode` | `incremental` by default or `full`; controls ordinary automatic refreshes |
| `refresh_cron` | Five-field UTC cron expression; unchanged eligible inputs need no generation |
| `full_refresh_cron` | Independent full-rebuild schedule, regardless of ordinary mode |
| `refresh_after_consolidation` | Refresh changed inputs once required indexing/Insight work has settled |
| `min_refresh_interval_seconds` | Non-negative automatic interval; null inherits `profile.min_refresh_interval_seconds`, whose default is zero |

Omitted patch fields retain their values. Null clears a cron schedule. `refresh_cron` and `refresh_after_consolidation` are mutually exclusive: enabling one alone clears the other; explicitly enabling both is rejected. A full-rebuild schedule can accompany either. For example, daily refresh at 03:00 UTC and a Sunday full rebuild at 04:00 UTC:

```json
{
  "mode": "incremental",
  "refresh_cron": "0 3 * * *",
  "full_refresh_cron": "0 4 * * 0",
  "min_refresh_interval_seconds": 3600
}
```

The full schedule uses the last successful full publication rather than the last incremental refresh. It skips when no refresh has occurred since the last full rebuild. When both schedules are due, one full refresh covers both. A first full schedule may rebuild an existing Profile whose historical full clock is unknown.

The worker checks policies at `profile.schedule_poll_seconds` (30 seconds by default), discovering at most `profile.schedule_batch_size` users per batch (100 by default). A schedule is an eligibility time, not a completion deadline. Deferred triggers combine, with full mode taking precedence. Active explicit work is never cancelled by a timer. Successful full publication satisfies a pending full request. Updating settings preserves pending work while a trigger remains enabled; disabling all triggers clears pending work but does not cancel already accepted operations.

The automatic interval uses the most recent admission or successful publication. Explicit refresh bypasses it. A terminal Profile failure pauses automatic refresh; a successful explicit refresh resumes it. Changing ordinary policy settings does not clear this pause or an unresolved provider outcome. Pending/failed prerequisite inputs remain visible and prevent automatic generation until ready; explicit Create/Refresh retains its bounded prerequisite recovery behavior. A failed refresh preserves prior content while its support remains eligible. Disabling the Profile feature stops policy scanning and rejects Profile API actions.

## 2. Custom generation without persistence

`POST /v1/users/{user_id}/profile/generate` requires `memory:read` and a nonblank `instruction` of at most 4,000 characters. It returns a synchronous `200` response with `Cache-Control: no-store`.

```json
{"instruction":"Summarize the user's professional background, work preferences, and current projects."}
```

The response contains `content`, `references`, `selection_coverage` (`selected`, `unexamined`, `budget_omitted`), `usage`, `embedding_usage`, and `generation_calls`. Generation usage includes reported reasoning tokens as a subset of output tokens; embedding usage is separate. Missing provider usage remains unknown. The existing model adapter validates completion termination but does not expose a finish-reason field in this response.

The full instruction selects eligible L1 facts and L2 Insights directly, with exact Profile Data available as grounded input. A custom result is not a rewrite of the stored Profile and does not automatically append unrelated Profile Data. Evidence Resources remain outside generation. References identify selected supporting facts, Insights and exact Profile Data; any numeric inline citation markers must match the returned references. Custom JSON presentation may use the separate reference list without inline markers. Coverage describes evidence selection, not proof that every relevant statement was discovered.

Custom generation does not save a Profile, custom instruction, generated body, operation result or content cache, and does not change the personal Profile. Request/model accounting may retain identifiers, statuses, counts, timing and usage; it does not retain custom content. Provider-side retention follows the configured provider and operator policy. The caller owns storage of the returned result.

One request shares `profile.attempt_timeout_seconds` across selection, generation and validation, and uses the existing Profile input/output limits. Transactions end before remote model calls. Missing/unready inputs fail before generation; the API does not enqueue indexing or retry failed source work. Current evidence, generation and policy are rechecked before returning, followed by credential/grant reauthorization. Provider/output failures return `502 ProfileGenerationUnavailable`, evidence changes return `409 ProfileGenerationChanged`, and exhausted model/request deadlines return `504 ProfileGenerationDeadlineExceeded`. Failure or timeout saves no partial custom output. A disconnected caller can leave an already dispatched synchronous provider request running until its bounded timeout; OAM does not queue a continuation or save its result.

## 3. Deployment

Apply migrations before starting upgraded API and worker processes. Migration `0038_profile_refresh_policy.sql` adds user-scoped policy metadata with forced row-level security and a restricted queue-only discovery routine. Multiple workers use database admission and the existing user lock to prevent duplicate Profile work. No additional scheduler service is required.

See [API reference](api.md), [indexing and retrieval](indexing-retrieval.md), [policy admission](../src/oam/profiles/scheduling.py), [custom generation](../src/oam/profiles/generation.py), and [Profile publication](../src/oam/profiles/worker.py).
