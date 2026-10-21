# Open Agent Memory for OpenClaw

## Overview

This plugin connects one OAM user to owner-authorized local WebChat sessions of one selected OpenClaw agent and explicitly approved direct conversations for that agent. `targetAgentId` defaults to `main`; it is separate from OAM's `userId`. OAM must already be running in `api_key` mode with a user-scoped key. The plugin adds fast automatic recall, capture of a completed keyed human turn, and the `memory_search` and `memory_remove` tools.

## Local installation

Start OAM and create a user-scoped key first; see [operations](../../docs/operations.md). From this directory, install the plugin, then run the one-path setup for an existing OAM API:

```bash
npm ci
npm run build
openclaw plugins install . --force --accept-capabilities
npm run setup -- --user-id '<oam-user-id>'
```

The setup prompts for missing values in a terminal. Its defaults are `http://127.0.0.1:17333` and the key environment variable `OAM_USER_API_KEY`; pass `--url` and `--key-env` to override them or all three flags for non-interactive use. Pass `--agent-id <id>` to select an agent other than `main`; omitting it preserves an existing selection or uses the plugin's `main` default. It writes one validated OpenClaw config batch, selecting OAM as the memory plugin and enabling owner-authorized local WebChat sessions for the selected agent. It stores a SecretRef, never the credential value. Supply the variable to the Gateway (for example through its private environment file), then run `openclaw gateway restart --safe`. The setup does not start OAM or the Gateway and does not send model requests.

Setup refuses an existing nonempty direct-conversation allowlist rather than silently rebind those chats to another user. To use approved direct conversations, review the user scope and set `plugins.entries.oam-openclaw.config.allowedDirectConversations` manually with an explicit channel, chat ID and sender ID. The script also refuses to override an explicitly disabled conversation hook. Keep a copy of the previous memory slot for rollback. Check the live Gateway plugin state after startup; offline inspection can register schemas without resolving a SecretRef, but only the running Gateway uses the resolved key.

The local route accepts the selected agent's default WebChat session, Dashboard and Responses sessions, and other owner WebChat session keys. OpenClaw must identify the caller as an owner before exposing either memory tool; automatic recall requires the turn-bound tool grant, and capture rechecks owner admission and the exact completed plain-text user prompt. Incognito, scheduled, internal, subagent, channel-origin, other-agent, and nonowner turns are excluded from this local route. Other channels require an explicitly approved direct-conversation tuple. Keep the Gateway loopback-bound with token authentication for a personal installation, and include a `user` value in each Responses request to create a separate session.

## Cron task and action memory

Set `plugins.entries.oam-openclaw.config.captureCronJobs` to `true` to capture the selected agent's recurring routines and successful report content. This option defaults to disabled and is separate from conversational route admission. It requires a host that dispatches `cron_reconciled` and `cron_changed` with the documented job snapshots; it does not read legacy JSON files or SQLite directly. Your OAM user key needs `memory:append` in addition to its recall permissions.

```bash
openclaw config set plugins.entries.oam-openclaw.config.captureCronJobs true --strict-json
openclaw gateway restart --safe
```

After scheduler startup or reload, the plugin reads existing jobs and learns only enabled, recurring user tasks with an explicit `agentId` matching `targetAgentId`. Gateway heartbeat jobs, disabled startup snapshots, unknown payload types and jobs without a meaningful name or description are skipped before any OAM request. Supported user-task payload types are `agentTurn`, `systemEvent`, `command` and `script`; classification uses the host type, not task-name keywords. Only `cron` and `every` schedules qualify as routine definitions. One-time reminders, process-exit watchers and stream triggers do not become lasting routine memories. Definitions retain the name, optional human description, cron expression/time zone or interval. Full task prompts, executable payloads, internal IDs, session routes, wake policy, interval anchors, jitter and model/tool settings are excluded. Without a description, only the name and timing are retained; add a task description when its purpose needs more detail.

Explicit live additions, updates and removals preserve the user task's enabled, disabled or removed state as dated historical snapshots. Disabling a task does not discard its explicit state change or an already running task's result. The host supplies no separate removal timestamp, so a removed snapshot records the removal without claiming when it occurred; it does not delete OAM history or control the scheduler. Filtering is prospective and leaves previously stored sources and memories unchanged.

Routine run status, duration, delivery acknowledgements and execution/delivery diagnostics create no OAM write. A successful execution's nonblank report summary enters ordinary interaction learning as one explicitly eligible `assistant` message containing exactly the original report text. The collector adds no title, status or narrative that could itself become a memory. Run time and assistant provenance remain in the structured message fields; correlated capture logs retain the job/source association. The existing extractor selects durable source-backed claims and can return no facts for filler; report passages remain indexed. Generated claims are not user statements or independently verified facts. These eligible interactions can inform later Insights and an explicitly refreshed Profile under OAM's ordinary relevance and grounding rules; there is no separate hard Profile exclusion for reports.

Failed/skipped executions, reported execution errors, host-suppressed output and standalone `HEARTBEAT_OK`/`NO_REPLY` control replies are omitted. Substantive text mentioning control tokens remains eligible. Delivery failure alone does not discard a successfully generated report: the host's overall completion status also includes delivery, so execution status determines report eligibility. A successful report can be retained even when its task was one-time or was disabled while running. The host summary is the supported report surface; full transcripts, tool outputs and automatic historical backfill are not imported.

Each request has a stable content-derived idempotency key and validates its OAM acceptance receipt. Runtime-only metadata does not change report content or identity. Reconciliation stops when its host snapshot is cancelled. There is no local queue or automatic retry. `cron definition` and `cron report` logs correlate the job, OAM operation/source and request duration; `cron skipped` reports the filtering reason, while warnings report capture failures without credentials or content. An accepted receipt means OAM owns processing; verify source readiness, extracted claims and cited recall separately. Enabling capture does not grant cron sessions access to personal recall or removal tools. Filtering itself leaves stored history unchanged; explicitly authorized cleanup uses OAM's normal source/removal APIs.

The plugin sends `maxContextTokens` as the learned-text `max_tokens` request budget and separately checks the complete returned `context_tokens` against the same host limit. Citations, metadata and required originals can take a valid server result above that limit; automatic recall then omits the result and explicit search reports an error instead of injecting oversized context.

## Memory removal

For an explicit user removal request, call `memory_remove` with exactly one nonblank `memory_id` or `insight_id`. Resolve the exact ID from dialogue or ordinary search; clarify ambiguous targets before calling. Identity always comes from the plugin configuration. Removal uses `DELETE /v1/users/{user_id}/memories/{memory_id}` or `DELETE /v1/users/{user_id}/insights/{insight_id}` with the user-scoped key and requires OAM's `source:delete` grant. Recall and capture continue to use POST. A validated `202` receipt confirms serving exclusion; the reported cleanup status and operation ID describe separate storage cleanup.

Capture retains the latest completed plain-text user turn with `learning_eligible: false` when it is marked `command: true` or `learning_eligible: false`, is an exact typed request such as `memory_remove(memory_id=mem_42)` or `memory_remove(insight_id="ins_7")`, or contains a removal tool call/result, including a failed removal attempt. The complete user text and timestamp are preserved with the same turn-derived idempotency key; assistant/tool output remains outside automatic capture. Ordinary later messages remain eligible. Failed or aborted agent runs still do not capture.

Automatic recall is omitted for a known current control or typed removal request; explicit `memory_search` remains available for resolving an unknown target. Current-message text must match the latest history user message before that message's control metadata affects recall, so an older control turn does not suppress a new ordinary request. Owner admission and exact-prompt checks still apply when local automatic recall is omitted.

The host must supply control/eligibility metadata for removal candidate and confirmation turns without a removal call. The plugin does not infer removal intent from arbitrary natural-language prose. OpenClaw's prompt-hook contract does not guarantee this metadata or a completed-turn callback for commands handled outside the agent loop; verify those paths on the installed host. Local hook/HTTP tests do not prove real-host intent classification, command delivery, or persistence in OAM.

## Verification

Run `npm test` and `npm pack --dry-run` here. Package validation alone does not prove the route is supported: send a real approved direct message or owner-authorized local WebChat request to the selected agent, check an OAM retain receipt and source readiness, then start a fresh session and confirm cited context reaches the model. The plugin has no local capture queue, so a Gateway crash before OAM accepts a retain request can lose that automatic capture.
