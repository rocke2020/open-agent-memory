# Open Agent Memory documentation

English (default) · [简体中文](zh/README.md)

## Overview

Open Agent Memory (OAM) stores your conversations and documents, learns supported memories, and retrieves cited context for your agent. Start with a working memory example, then use the guides for your own application or deployment. The Compose quickstart uses the v0.2.1 release image. Use the API and configuration references to choose your recall mode and evidence limits.

## 1. Get started

- [Store and recall your first memory](start-here.md) walks through installation, user creation, cited recall, source inspection and a fresh agent session.
- [Retain and recall over HTTP](quickstart.md) gives you a copyable example for an existing service and user key.
- [Memory Inspector](memory-web-ui.md) shows how to connect, browse memories and sources, manage documents and refresh a User Profile.

## 2. Connect an agent or application

- [MCP for Claude Code, Cursor and compatible clients](../integrations/mcp/README.md)
- [OpenClaw plugin](../integrations/openclaw/README.md)
- [DeepSeek Harness plugin](../integrations/dsh/README.md)
- [HTTP API reference](api.md) covers retain, recall, Profiles, Resources and administration.

## 3. Understand memory and recall

- [Indexing and retrieval](indexing-retrieval.md) explains original evidence, learned memory, Insights and the recall pipeline.
- [Personal and custom Profiles](profiles.md) explains stored Profiles, automatic refresh and transient generation.
- [Decision model](decision-model.md) describes the optional client, proposed consumers and measured comparisons.
- [Evaluation results](benchmarks.md) explains the measured accuracy and context usage, with pinned results and reproduction details.

## 4. Deploy and operate

- [Docker Compose](../deploy/README.md) covers installing, configuring and running one instance or local replicas.
- [Operations](operations.md) covers model providers, credentials, database roles, migrations and failure handling.
- [Recall configuration](../configs/README.md) covers the default mode, per-mode presets, shared search settings and request overrides.

## 5. Develop and release

- [Architecture](architecture.md) and [runtime prompts](prompts.md) describe implementation details.
- [Acceptance](acceptance.md), [foundation evidence](foundation-evidence.md) and [candidate evidence](release-evidence.md) describe verification criteria, historical checks and measured limits.
- [Contributing](../CONTRIBUTING.md), [release procedure](releasing.md) and [version history](releases/README.md) cover development and publication.
