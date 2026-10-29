# OAM for DeepSeek Harness

This personal memory plugin uses the OAM HTTP API to recall cited context for human turns and retain human text after completed turns. Use it only in a DSH profile accessible to the owner of the configured OAM user. It excludes subagents. OAM must already be running and processing operations.

Set `OAM_DSH_URL`, `OAM_DSH_USER_ID` and `OAM_DSH_API_KEY` in the DSH process environment. Use an HTTPS origin or a loopback HTTP origin and a user-scoped key. Install this package in the desired DSH profile using the host's plugin manager and restart or reload that profile. The model can use `memory_search` for explicit recall. The server's complete context must fit the plugin's `maxContextTokens` configuration (default 18000); oversized results are omitted, with a warning, rather than truncated. Failed capture is logged and is not queued for retry.

Run `npm test` for local HTTP-boundary tests. A live integration requires a separate completed turn, OAM operation readiness, and cited recall in a fresh DSH session; installation or local tests do not prove that path.
