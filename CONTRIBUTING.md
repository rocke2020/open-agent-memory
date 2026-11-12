# Contributing to Open Agent Memory

## Overview

Changes should improve one observable product path without weakening evidence, authorization, source lineage, or release claims. Start from a failing behavior test, implement the minimum, and keep public contracts and documentation synchronized.

## Development

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Use isolated synthetic fixtures. Do not read private `.env`, call paid providers, write/delete real service data, or run destructive container cleanup in default tests.

## Change Boundaries

- Public request/response behavior changes update OpenAPI contracts, conformance fixtures, and `docs/api.md` together.
- Configuration changes have one owner in `configs/oam.yaml` and tests proving precedence and fresh-process behavior.
- Schema changes use versioned migrations and preserve non-bypass RLS.
- Cross-cutting worker/retry/publication changes update `docs/architecture.md` and failure tests.
- Generated memories and Profiles remain read-only; source/Resource lifecycle is separate from later targeted fact controls.
- Product version bumps require versioned release notes and a completed review of every documentation file; follow [the release documentation gate](docs/releasing.md#1-review-release-notes-and-documentation).
