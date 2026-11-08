# OAM for DeepSeek Harness

This personal memory plugin uses the OAM HTTP API to recall cited context for human turns and retain human text after completed turns. Use it only in a DSH profile accessible to the owner of the configured OAM user. It excludes subagents. OAM must already be running and processing operations.

When the DSH schedule service is available, the plugin observes named recurring jobs and retains concise definitions, changes, removals, and the latest acknowledged delivery. It excludes prompts, generated output and one-time tasks. A delivery means the scheduler persisted a message to the session; it does not prove the job completed. The profile is the ownership boundary because the catalog has no owner or maintenance classification. A private file under `DSH_HOME` (default `~/.dsh`) preserves each job's current definition observation and timestamp so unchanged snapshots reuse their idempotency key after restart. The catalog exposes only the latest receipt; intermediate deliveries missed while stopped are not recovered. Failed captures are logged, not queued.

Set `OAM_DSH_URL`, `OAM_DSH_USER_ID` and `OAM_DSH_API_KEY` in the DSH process environment. Use an HTTPS origin or a loopback HTTP origin and a user-scoped key. Install this package in the desired DSH profile using the host's plugin manager and restart or reload that profile. The model can use `memory_search` for explicit recall. The server's complete context must fit the plugin's `maxContextTokens` configuration (default 18000); oversized results are omitted, with a warning, rather than truncated. Failed capture is logged and is not queued for retry.

Run `npm test` for local HTTP-boundary tests. A live integration requires a separate completed turn, OAM operation readiness, and cited recall in a fresh DSH session; installation or local tests do not prove that path.

## Release archive installation

Download `rocke2020-oam-openclaw-0.0.1.tgz`, `rocke2020-oam-dsh-0.0.1.tgz` and `SHA256SUMS` from the same OAM GitHub Release and run `shasum -a 256 -c SHA256SUMS` in that directory. No npm account or registry publication is required. DSH may need network access for its own profile dependencies.

```bash
dsh plugin --profile headless add /absolute/path/rocke2020-oam-dsh-0.0.1.tgz
```

Select the owner-only profile you intend to use, supply the three environment variables above to that DSH process, and restart the profile. Keep the archive available because the profile's package manager may reference its local path. Use `dsh plugin --profile headless remove @rocke2020/oam-dsh` to remove it. The package contains the plugin entrypoint and profile patch; it does not include or start OAM.
