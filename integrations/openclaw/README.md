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

## Memory removal

For an explicit user removal request, call `memory_remove` with exactly one nonblank `memory_id` or `insight_id`. Resolve the exact ID from dialogue or ordinary search; clarify ambiguous targets before calling. Identity always comes from the plugin configuration. Removal uses `DELETE /v1/users/{user_id}/memories/{memory_id}` or `DELETE /v1/users/{user_id}/insights/{insight_id}` with the user-scoped key and requires OAM's `source:delete` grant. Recall and capture continue to use POST. A validated `202` receipt confirms serving exclusion; the reported cleanup status and operation ID describe separate storage cleanup.

Capture retains the latest completed plain-text user turn with `learning_eligible: false` when it is marked `command: true` or `learning_eligible: false`, is an exact typed request such as `memory_remove(memory_id=mem_42)` or `memory_remove(insight_id="ins_7")`, or contains a removal tool call/result, including a failed removal attempt. The complete user text and timestamp are preserved with the same turn-derived idempotency key; assistant/tool output remains outside automatic capture. Ordinary later messages remain eligible. Failed or aborted agent runs still do not capture.

Automatic recall is omitted for a known current control or typed removal request; explicit `memory_search` remains available for resolving an unknown target. Current-message text must match the latest history user message before that message's control metadata affects recall, so an older control turn does not suppress a new ordinary request. Owner admission and exact-prompt checks still apply when local automatic recall is omitted.

The host must supply control/eligibility metadata for removal candidate and confirmation turns without a removal call. The plugin does not infer removal intent from arbitrary natural-language prose. OpenClaw's prompt-hook contract does not guarantee this metadata or a completed-turn callback for commands handled outside the agent loop; verify those paths on the installed host. Local hook/HTTP tests do not prove real-host intent classification, command delivery, or persistence in OAM.

## Verification

Run `npm test` and `npm pack --dry-run` here. Package validation alone does not prove the route is supported: send a real approved direct message or owner-authorized local WebChat request to the selected agent, check an OAM retain receipt and source readiness, then start a fresh session and confirm cited context reaches the model. The plugin has no local capture queue, so a Gateway crash before OAM accepts a retain request can lose that automatic capture.
