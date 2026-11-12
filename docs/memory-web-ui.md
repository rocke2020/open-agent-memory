# Memory Web UI

## Overview

Memory Inspector is the user-memory web UI bundled with the OAM API. Start the API, then open `http://127.0.0.1:17333/ui/` in a browser. It uses the same public API and user permissions as other clients; there is no separate web server or UI account.

## 1. Start the API

Run these commands from the repository root after preparing PostgreSQL with pgvector and separate migration and API database credentials. Set `OAM_MIGRATION_DSN` and `OAM_API_DSN` to real connection strings in your environment or in `./.env`; the CLI loads `./.env` without replacing inherited variables. The values in `.env.example` are placeholders. [Operations](operations.md) explains database credentials and authentication bootstrap.

```bash
uv sync --locked
uv run oam migrate --config configs/oam.yaml
uv run oam api --config configs/oam.yaml --host 127.0.0.1
```

Keep the API command running and open `http://127.0.0.1:17333/ui/`. For a new local `dev` database, leave both `OAM_ROOT_API_KEY` and `OAM_BOOTSTRAP_OUTPUT` unset, including in `./.env`, before migration. An existing keyed database remains keyed; removing a shell variable does not switch its authentication mode. If port 17333 is occupied, choose another `--port` and use that port in the browser URL.

## 2. Connect to your memory

The form asks for the tenant, user ID and optional API credential. Use the values for the same personal memory collection:

| Authentication mode | Tenant and user ID | API credential |
|---|---|---|
| Local `dev` | An active tenant, normally `default`, and the user ID you want to inspect | Leave blank. This loopback-only mode has no privacy boundary. |
| `api_key` | The tenant and user ID bound to your user key | Enter the **user key**. A root administrator key cannot open personal memory. |
| Local `trusted` | The active tenant and asserted user ID | Leave blank; the UI sends the entered identity as local assertion headers. |
| Nonlocal `trusted` | The identity supplied by the authenticated gateway | Open the UI through that gateway; it must replace identity headers and supply the required root-key proof. Do not expose the root key in the browser. |

In `api_key` mode, a tenant administrator creates a user with `POST /v1/users` and receives that user's first key once. Ask the operator for the user key if you do not have it; see [HTTP API administration](api.md#2-administration). Press **Connect** to load that user's current Memories and Resources. The credential stays in page memory and is cleared on sign-out or reload.

## 3. Use the views

The compact header contains the OAM logo, current collection, language selector and GitHub repository link. Use the labeled sidebar to choose a view; on narrow screens it becomes horizontal navigation. Use **Language** to select English, Español, Français, Deutsch, Português, 日本語, 한국어, 简体中文 or 繁體中文. The page initially follows the browser's first supported language and remembers an explicit choice. Switching updates labels, accessible names, status messages and date/time displays without clearing the current connection, selected memory, form entries or retrieved results. Memories, original messages and retrieved content retain their stored language.

**Memories** lists current facts and Insights newest by added time, shows each Insight subtype in the list and details, and lets the user inspect supporting source messages. **User Profile** shows one read-only document: default Markdown displays Main content with headings and bold text rendered safely, while all numbered References, source dates and identifiers stay inside the collapsed Reference details panel; parseable custom JSON is pretty-printed, and other formats display safely as text. The page shows the last recorded User Profile refresh time and keeps detailed source identifiers in a collapsed Reference details panel. An optional custom instruction controls User Profile presentation and is shown with its stored revision; **Use default instructions** restores the default. **Create/Refresh User Profile** reuses unchanged content or returns an operation that runs pending Insights before building the document. Failed Insight or initial processing inputs and eligible items beyond the User Profile input limit remain visible as actionable counts, while routine machine freshness labels stay out of the primary view. **Resources** manages document originals and passages through `/resources`, including create, revise, inspect and delete actions. Deleting a source excludes all its versions and dependent output immediately; a separate cleanup operation reports later removal from active storage.

Memories, Insights and Resources automatically select and open the first available item when entered. Refreshing or loading more keeps the current selection while it remains in the list; an empty collection shows an explicit empty state. **Entities** opens as a list, with **Relationships** available for exploring connections.

Every Insight has a concise title naming its enduring subject, shown in the list and detail heading with the subtype beneath the list title. Titles normally stay stable as the body gains new events or status; synthesis can revise a title when evidence changes or clarifies the subject. The complete Insight text, canonical ID, revision and supporting sources remain in the details. Existing records receive a title from their saved subject label or a bounded content preview. Browsing does not generate or rewrite content.

Select a Memory Unit or Insight to inspect its canonical typed ID, revision, content, provenance and dependent Insights before using **Remove Memory Unit** or **Remove Insight**. The action submits only that selected ID, preserves the source and refreshes the list after exclusion. The receipt exposes cleanup separately from linked consolidation; use operation status to follow maintenance. Removing a learned record withholds affected stored Profile content until explicit refresh.

A new collection can show empty views. The API can serve the UI without a worker. Indexing interactions and Resources, or completing a changed User Profile refresh, requires `uv run oam worker --config configs/oam.yaml` in another terminal, its queue/content DSNs, and the configured model roles. The canonical `configs/oam.yaml` contains no model endpoints or credentials, so model-backed indexing and refresh need operator configuration; see [Operations](operations.md#1-configuration).

The User Profile header shows a compact freshness status and relative update time; hover over the time for its exact date. Older field-based revisions display their existing content when all references remain eligible, without regeneration. An older revision without a publication timestamp says **Updated time not recorded**. If no readable document exists, use **Create/Refresh User Profile** to build one from current sources. If the document exceeds the preview token budget, increase or clear that budget to view it. A processing cutoff is not shown because the current User Profile contract does not record one.

## 4. If the page or connection fails

If `/ui/` does not open, confirm the API process is still running on the chosen host and port; `GET /health/live` checks the process. If **Connect** fails, check the tenant/user pair, user-key type and permissions, and the API database connection. The UI loads Memories and Resources during connection, so a working HTML page alone does not prove that personal data access works. For accepted work that is still pending or missing from a view, inspect its operation status and confirm a worker is running. The current `/health/ready` returns `503 core_only` even when the UI is available; use the actual user operation to check the capability you need.
