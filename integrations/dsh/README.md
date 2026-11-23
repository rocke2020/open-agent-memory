# OAM for DeepSeek Harness

This personal memory plugin uses the OAM HTTP API to recall cited context for human turns and retain human text after completed turns. Use it only in a DSH profile accessible to the owner of the configured OAM user. It excludes subagents. OAM must already be running and processing operations.

When the DSH schedule service is available, the plugin observes named recurring jobs and retains concise definitions, changes, removals, and the latest acknowledged delivery. It excludes prompts, generated output and one-time tasks. A delivery means the scheduler persisted a message to the session; it does not prove the job completed. The profile is the ownership boundary because the catalog has no owner or maintenance classification. A private file under `DSH_HOME` (default `~/.dsh`) preserves each job's current definition observation and timestamp so unchanged snapshots reuse their idempotency key after restart. The catalog exposes only the latest receipt; intermediate deliveries missed while stopped are not recovered. Failed captures are logged, not queued.

Set `OAM_DSH_URL`, `OAM_DSH_USER_ID` and `OAM_DSH_API_KEY` in the DSH process environment. Use an HTTPS origin or a loopback HTTP origin and a user-scoped key. Install this package in the desired DSH profile using the host's plugin manager and restart or reload that profile. Automatic recall uses Fast. The `memory_search` tool defaults to Normal and accepts `fast`, `normal`, and `deep`, whose service context limits default to 8,000, 16,000 and 24,000 tokens. Set `maxContextTokens` to at least 24,000 to consume complete Deep responses when the host can accommodate them. The model can use `memory_remove` for explicit user removal requests. The server's complete context must fit the plugin's `maxContextTokens` configuration (default 18000); oversized results are omitted, with a warning, rather than truncated. Failed capture is logged and is not queued for retry.

Run `npm test` for local HTTP-boundary tests. A live integration requires a separate completed turn, OAM operation readiness, and cited recall in a fresh DSH session; installation or local tests do not prove that path.

## Memory removal

For an explicit user removal request, call `memory_remove` with exactly one `memory_id` or `insight_id`, for example `{"memory_id":"mem_42"}` or `{"insight_id":"ins_7"}`. Resolve the exact ID from dialogue or `memory_search`, which appends each removable item's exact ID alongside its numbered context reference; citation numbers alone are not removal IDs. Clarify ambiguous targets before calling. The tool always uses the configured user and user-scoped key, requires OAM's `source:delete` grant, and is unavailable to subagents. It sends `DELETE /v1/users/{user_id}/memories/{memory_id}` or `DELETE /v1/users/{user_id}/insights/{insight_id}`. A validated `202` receipt confirms exclusion from serving; the reported cleanup status and operation ID describe separate storage cleanup. Invalid requests, failed requests and invalid receipts surface as tool errors.

Completed turns containing a native `memory_remove` tool call or an invocation of this plugin's removal tool retain the original human text and timestamp with `learning_eligible: false`, including failed attempts. Assistant and tool output remain outside capture; later ordinary turns remain eligible. Candidate and confirmation turns without a removal invocation are not classified by this plugin. It does not infer removal intent from arbitrary natural-language text.

## npm installation

Install the independently published 0.0.2 package in the owner-only DSH profile to use exact memory and Insight removal.

```bash
dsh plugin --profile headless add @rocke-dong/oam-dsh@0.0.2
```

No npm login is required for users installing the public package. Supply the OAM environment variables above, then restart or reload that profile. If an earlier local `@rocke2020/oam-dsh` package is installed, remove that package through the same profile's plugin manager before adding the renamed package. Source and archive checks are separate from verification of the actual published package.

## Release archive installation

Download `rocke-dong-oam-dsh-0.0.2.tgz` and `SHA256SUMS` from the same OAM GitHub Release and run `shasum -a 256 -c SHA256SUMS` in that directory. No npm account or registry publication is required. DSH may need network access for its own profile dependencies.

```bash
dsh plugin --profile headless add /absolute/path/rocke-dong-oam-dsh-0.0.2.tgz
```

Select the owner-only profile you intend to use, supply the three environment variables above to that DSH process, and restart the profile. Keep the archive available because the profile's package manager may reference its local path. Use `dsh plugin --profile headless remove @rocke-dong/oam-dsh` to remove it. The package contains the plugin entrypoint and profile patch; it does not include or start OAM.
