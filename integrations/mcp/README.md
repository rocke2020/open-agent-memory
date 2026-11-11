# Open Agent Memory MCP Server

## Overview

This package is a thin Model Context Protocol (MCP) server for Open Agent Memory (OAM). It runs as a local stdio process started by an MCP client such as Claude Code, Cursor, or Codex, and calls the OAM HTTP API of an existing deployment. It adds no hosted service, database, queue, or listening port.

The wrapper is an API client, not a second memory implementation. OAM owns authentication, authorization, retention policy, indexing, retrieval, citations, and operation state. The wrapper has six tools and passes OAM's contract through rather than reimplementing it.

Retrieved memory is untrusted evidence, not instructions. Tool descriptions state this boundary; clients must not follow directives found inside recalled text.

See the [71-second cited-recall demo](https://github.com/rocke2020/open-agent-memory/releases/download/v0.0.1/oam-v0.0.1-cited-recall-demo.mp4): a fresh published MCP process checks readiness and retrieves an already indexed synthetic preference, then Memory Inspector opens the original source. The recording runs at 2× speed and performs no new indexing.

## Prerequisites

1. OAM is running in `api_key` mode with a user-scoped key. See the [OAM quickstart](https://github.com/rocke2020/open-agent-memory/blob/main/docs/quickstart.md).
2. The OAM user has completed at least one retain and indexing operation, so recall has evidence to return.
3. Node.js 22 or newer is available where the MCP client runs.

The wrapper supports only `api_key` mode. `dev` mode and forged `trusted` headers are unsupported. One wrapper process serves one OAM user.

## Installation

Install the published npm package:

```bash
npx -y @rocke-dong/oam-mcp
```

To build and run from source instead:

```bash
npm install
npm run build
node dist/index.js
```

## Configuration

The wrapper reads only environment variables and stores no credential:

| Variable | Required | Meaning |
|---|---|---|
| `OAM_BASE_URL` | Yes | Origin of the OAM deployment, for example `http://127.0.0.1:17333` |
| `OAM_USER_ID` | Yes | Authorized personal user |
| `OAM_USER_API_KEY` | Yes | User-scoped key, sent as `Authorization: Bearer <key>` |

`OAM_BASE_URL` must use HTTP or HTTPS, must not contain credentials, and must not include a query or fragment. The wrapper rejects an empty user or key before connecting. Use HTTPS for any non-loopback deployment; a bearer key sent over plaintext HTTP is visible to the network.

The MCP client may store these environment values in its own configuration. Protect that file and never commit a real key. The wrapper itself does not write configuration or cache memory content.

## Client Configuration

Claude Code, after setting the three environment variables above in your shell:

```bash
claude mcp add oam \
  -e "OAM_BASE_URL=$OAM_BASE_URL" \
  -e "OAM_USER_ID=$OAM_USER_ID" \
  -e "OAM_USER_API_KEY=$OAM_USER_API_KEY" \
  -- npx -y @rocke-dong/oam-mcp
```

Cursor, using `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "oam": {
      "command": "npx",
      "args": ["-y", "@rocke-dong/oam-mcp"],
      "env": {
        "OAM_BASE_URL": "http://127.0.0.1:17333",
        "OAM_USER_ID": "your-user-id",
        "OAM_USER_API_KEY": "your-user-key"
      }
    }
  }
}
```

Other MCP clients use the same stdio command, arguments, and environment variables.

## Tools

| Tool | OAM call | Input | Returns |
|---|---|---|---|
| `memory_recall` | `POST /v1/recall` | `query`; optional `recall_level` (`fast` default or `normal`), `include_chunks` (default true), `detail` (`context` default or `full`) | Rendered `context` with citations and `trace_id`; `full` adds typed `items` and diagnostics |
| `memory_add` | `POST /v1/retain` | `content`; optional timezone-aware `event_time`, `learning_eligible` (default true), `idempotency_key` | Durable operation ID and source ID |
| `memory_add_document` | `POST /v1/users/{user_id}/resources` | `document_id`, `update_mode`, exactly one of `text` or `url` plus `media_type`, and optional `idempotency_key` | Source receipt and operation state |
| `memory_operation` | `GET /v1/users/{user_id}/operations/{operation_id}` | `operation_id` | Readiness, stage state, and safe failure details |
| `memory_profile` | `GET /v1/users/{user_id}/profile` | None | Stored Profile content, references, and freshness; never generates |
| `memory_remove` | `DELETE /v1/users/{user_id}/memories/{id}` or `/insights/{id}` | `memory_id`, `memory_type` (`basic` or `insight`) | Removal operation receipt |

`memory_add` sends one explicit user message. Use it only when the user asks to remember something. `memory_add_document` supports text and HTTPS URL originals in this version; multipart file upload is not supported.

Both write tools accept an optional `idempotency_key`. Keys must be 1-200 characters from `A-Z`, `a-z`, `0-9`, `.`, `_`, `:`, or `-`; omit the key to generate one. Pass the same key when retrying the same logical write so OAM reuses the original receipt instead of duplicating the interaction or document revision. Without a key, the wrapper generates a new one for every invocation, so a retry becomes a new logical request.

`memory_recall` defaults to `fast` because it is the pre-agent retrieval mode. `detail: "full"` returns the complete recall JSON, including typed items and diagnostics; the default returns the rendered context, token count, tokenizer identity, trace ID, and item count.

## Behavior and Security

1. Recall output is evidence. The wrapper labels it as untrusted data and never interprets it as instructions.
2. The wrapper sends the API key only in the `Authorization` header. It does not log keys, request bodies, or memory text.
3. OAM errors return the safe OAM code, HTTP status, retryability, and `X-Request-ID`. Unexpected failures return a generic error without a stack.
4. The wrapper does not retry automatically. Retry decisions belong to the caller.
5. Requests use a 60-second total timeout. OAM owns recall deadlines; the platform fetch stack applies its own connection timeout inside that window.
6. Success bodies are capped at 8 MiB and error bodies at 64 KiB. OAM already bounds recall context; the wrapper does not add a second content budget.
7. The wrapper writes no memory content to disk and keeps no background workers.
8. The wrapper refuses HTTP redirects, so the bearer key cannot be forwarded to a different origin by the upstream endpoint.

## Limitations

1. stdio transport only; there is no maintainer-hosted remote MCP endpoint.
2. One OAM user per process; no multi-user switching or OAuth.
3. Text and HTTPS URL documents only; no multipart file upload.
4. User Profile reads only; Profile refresh is not exposed.
5. No generated answers, summarization, or reranking. Recall returns evidence for the host agent to reason over.
6. OAM still calls its configured models for indexing and embedding; this wrapper does not make OAM local-only.

## Development

```bash
npm install
npm test
npm run build
```

The tests use a local mock HTTP server and an in-memory MCP transport. They do not call a live OAM deployment or any model provider. A live acceptance run against a real OAM user remains required before claiming end-to-end readiness.

See the [OAM HTTP API](https://github.com/rocke2020/open-agent-memory/blob/main/docs/api.md), [operations guide](https://github.com/rocke2020/open-agent-memory/blob/main/docs/operations.md), and [repository README](https://github.com/rocke2020/open-agent-memory/blob/main/README.md).

## License

Apache-2.0. See [LICENSE](LICENSE) and [third-party notices](THIRD_PARTY.md).
