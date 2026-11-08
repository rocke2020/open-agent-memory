# Open Agent Memory for OpenClaw

## Overview

This plugin connects one OAM user to owner-authorized local WebChat sessions of one selected OpenClaw agent and explicitly approved direct conversations for that agent. `targetAgentId` defaults to `main`; it is separate from OAM's `userId`. OAM must already be running in `api_key` mode with a user-scoped key. The plugin adds fast automatic recall, capture of a completed keyed human turn, and the `memory_search` and `memory_remove` tools.

## npm installation

The v0.0.1 release publishes `@rocke-dong/oam-openclaw` on the public npm registry. After publication, install the versioned package with OpenClaw and configure the existing OAM user:

```bash
openclaw plugins install npm:@rocke-dong/oam-openclaw@0.0.1 --force --accept-capabilities
node '<installed-plugin-directory>/setup.mjs' --user-id '<oam-user-id>'
```

The install command reports the plugin directory. No npm login is required for users installing the public package. Source and archive checks are separate from verification of the actual published package.

## Release archive installation

Download `rocke-dong-oam-openclaw-0.0.1.tgz`, `rocke-dong-oam-dsh-0.0.1.tgz` and `SHA256SUMS` from the same OAM GitHub Release. Verify the checksums in that download directory with `shasum -a 256 -c SHA256SUMS`. No npm account or registry publication is required. The host may still need network access for its own dependencies.

```bash
openclaw plugins install ./rocke-dong-oam-openclaw-0.0.1.tgz --force --accept-capabilities
node '<installed-plugin-directory>/setup.mjs' --user-id '<oam-user-id>'
```

The install command reports the plugin directory; substitute that path above. Read the setup and admission rules below before starting the Gateway. To remove the package, use `openclaw plugins uninstall oam-openclaw` and restore the previous memory slot.

## Source installation

Start OAM and create a user-scoped key first; see [operations](../../docs/operations.md). From this directory, install the plugin, then run the one-path setup for an existing OAM API:

```bash
npm ci
npm run build
openclaw plugins install . --force --accept-capabilities
npm run setup -- --user-id '<oam-user-id>'
```

The setup prompts for missing values in a terminal. Its defaults are `http://127.0.0.1:17333` and the key environment variable `OAM_USER_API_KEY`; pass `--url` and `--key-env` to override them or all three flags for non-interactive use. Pass `--agent-id <id>` to select an agent other than `main`; omitting it preserves an existing selection or uses the plugin's `main` default. It writes one validated OpenClaw config batch, selecting OAM as the memory plugin, disabling the built-in `memory-core` entry to prevent a competing `memory_search` tool, and enabling owner-authorized local WebChat sessions for the selected agent. It stores a SecretRef, never the credential value. Supply the variable to the Gateway (for example through its private environment file), then run `openclaw gateway restart --safe`. The setup does not start OAM or the Gateway and does not send model requests.

Setup refuses an existing nonempty direct-conversation allowlist rather than silently rebind those chats to another user. To use approved direct conversations, review the user scope and set `plugins.entries.oam-openclaw.config.allowedDirectConversations` manually with an explicit channel, chat ID and sender ID. The script also refuses to override an explicitly disabled conversation hook. Keep a copy of the previous memory slot and `memory-core` enablement for rollback. Check the live Gateway plugin state after startup; offline inspection can register schemas without resolving a SecretRef, but only the running Gateway uses the resolved key.

The local route accepts the selected agent's default WebChat session, Dashboard and Responses sessions, and other owner WebChat session keys. OpenClaw must identify the caller as an owner before exposing either memory tool; automatic recall requires the turn-bound tool grant, and capture rechecks owner admission and the exact completed plain-text user prompt. Incognito, scheduled, internal, subagent, channel-origin, other-agent, and nonowner turns are excluded from this local route. Other channels require an explicitly approved direct-conversation tuple. Keep the Gateway loopback-bound with token authentication for a personal installation, and include a `user` value in each Responses request to create a separate session.

## Cron task and action memory

Set `plugins.entries.oam-openclaw.config.captureCronJobs` to `true` to capture the selected agent's recurring routines and observed execution status. This option defaults to disabled and is separate from conversational route admission. It requires a host that dispatches `cron_reconciled` and `cron_changed` with the documented job snapshots; it does not read legacy JSON files or SQLite directly. Your OAM user key needs `memory:append` in addition to its recall permissions.

```bash
openclaw config set plugins.entries.oam-openclaw.config.captureCronJobs true --strict-json
openclaw gateway restart --safe
```

After scheduler startup or reload, the plugin reads existing jobs and learns only enabled, recurring user tasks with an explicit `agentId` matching `targetAgentId`. Gateway heartbeat jobs, disabled startup snapshots, unknown payload types and jobs without a meaningful name or description are skipped before any OAM request. Supported user-task payload types are `agentTurn`, `systemEvent`, `command` and `script`; classification uses the host type, not task-name keywords. Only `cron` and `every` schedules qualify as routine definitions. One-time reminders, process-exit watchers and stream triggers do not become lasting routine memories. Definitions retain the job ID, name, optional human description, cron expression/time zone or interval. Full task prompts, executable payloads, session routes, wake policy, interval anchors, jitter and model/tool settings are excluded. Without a description, only the name and timing are retained; add a task description when its purpose needs more detail.

Explicit live additions, updates and removals preserve the user task's enabled, disabled or removed state as dated historical snapshots. Disabling a task does not discard its explicit state change or an already running task's result. The host supplies no separate removal timestamp, so a removed snapshot records the removal without claiming when it occurred; it does not delete OAM history or control the scheduler. Filtering is prospective and leaves previously stored sources and memories unchanged.

For each recurring job, a host-reported run status (`ok`, `error`, or `skipped`) is retained with the job ID and run time. The ID joins it to the definition and keeps an old run stable when the job is renamed. This reports execution, not downstream delivery. Generated reports, raw errors, delivery diagnostics and tool output are excluded. One-time tasks are excluded. Reconciliation can observe the most recent run but does not reconstruct every run missed while the plugin was stopped.

Each request has a stable content-derived idempotency key and validates its OAM acceptance receipt. Reconciliation stops when its host snapshot is cancelled. There is no local queue or automatic retry. Capture and skip logs identify the job and outcome without credentials or content. Acceptance does not prove indexing or recall readiness. Enabling capture does not grant cron sessions access to personal recall or removal tools, and it does not delete previously stored reports.

Set `maxContextTokens` to the service’s largest recall context limit when the agent can accommodate it; the default is 1024 and the maximum is 32768. The plugin checks the complete returned `context_tokens` against its `maxContextTokens` host limit. Citations, metadata and required originals can take a valid server result above that limit; automatic recall then omits the result and explicit search reports an error instead of injecting oversized context.

## Memory removal

For an explicit user removal request, call `memory_remove` with exactly one nonblank `memory_id` or `insight_id`. Resolve the exact ID from dialogue or ordinary search; clarify ambiguous targets before calling. Identity always comes from the plugin configuration. Removal uses `DELETE /v1/users/{user_id}/memories/{memory_id}` or `DELETE /v1/users/{user_id}/insights/{insight_id}` with the user-scoped key and requires OAM's `source:delete` grant. Recall and capture continue to use POST. A validated `202` receipt confirms serving exclusion; the reported cleanup status and operation ID describe separate storage cleanup.

Capture retains the latest completed plain-text user turn with `learning_eligible: false` when it is marked `command: true` or `learning_eligible: false`, is an exact typed request such as `memory_remove(memory_id=mem_42)` or `memory_remove(insight_id="ins_7")`, or contains a removal tool call/result, including a failed removal attempt. The complete user text and timestamp are preserved with the same turn-derived idempotency key; assistant/tool output remains outside automatic capture. Ordinary later messages remain eligible. Failed or aborted agent runs still do not capture.

Automatic recall is omitted for a known current control or typed removal request; explicit `memory_search` remains available for resolving an unknown target. Current-message text must match the latest history user message before that message's control metadata affects recall, so an older control turn does not suppress a new ordinary request. Owner admission and exact-prompt checks still apply when local automatic recall is omitted.

The host must supply control/eligibility metadata for removal candidate and confirmation turns without a removal call. The plugin does not infer removal intent from arbitrary natural-language prose. OpenClaw's prompt-hook contract does not guarantee this metadata or a completed-turn callback for commands handled outside the agent loop; verify those paths on the installed host. Local hook/HTTP tests do not prove real-host intent classification, command delivery, or persistence in OAM.

## Verification

Run `npm test` and `npm pack --dry-run` here. Package validation alone does not prove the route is supported: send a real approved direct message or owner-authorized local WebChat request to the selected agent, check an OAM retain receipt and source readiness, then start a fresh session and confirm cited context reaches the model. The plugin has no local capture queue, so a Gateway crash before OAM accepts a retain request can lose that automatic capture.
