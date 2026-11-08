# Run OAM with Docker Compose

## Overview

OAM uses one non-root image for the API, Memory Inspector, worker and operator commands, with PostgreSQL/pgvector as its only persistent service. The image installs the locked Python dependencies and packaged configuration/migrations. The default topology publishes the API only on loopback. The replica topology adds a local reverse proxy, two APIs and two workers over the same database. This is a local application-recovery simulation, not proof of physical multi-host or database resilience.

## 1. Prepare configuration

Build the candidate from the repository root:

```bash
docker build -t oam:0.0.1-candidate .
cp deploy/.env.example deploy/.env
```

Fill `deploy/.env` with independent random hexadecimal database passwords, a random root API key, and the configured generation/embedding endpoint, credential and model triplets. Generate each secret separately, for example with `openssl rand -hex 24`. Hexadecimal passwords avoid URI-escaping ambiguity in the example DSNs. The file is ignored by Git; keep it private. The root key manages users and cannot directly access their personal memory. Runtime services receive separate non-superuser API, queue and content credentials, not the database administrator password.

`deploy/oam.yaml` selects keyed authentication and dedicated workers; all omitted behavior comes from packaged `configs/oam.yaml`. To use another operator file, change the read-only mount while preserving the same file for all roles. A model endpoint on this Mac must use a container-reachable address such as `host.docker.internal`, rather than container loopback. The model endpoint still needs to accept requests from Docker's network.

## 2. Initialize and start

Run these commands from the repository root:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait postgres
docker compose --env-file deploy/.env -f deploy/compose.yaml run migrate
docker compose --env-file deploy/.env -f deploy/compose.yaml exec -T postgres psql -U postgres -d oam -f /bootstrap/runtime-roles.sql
docker compose --env-file deploy/.env -f deploy/compose.yaml run api preflight --json
docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --wait api worker
```

Migration uses operator authority and initializes the root credential. The role script creates the three runtime logins only when absent and grants the corresponding roles created by migrations. Repeating it does not rotate existing passwords; coordinate any intentional rotation with all replicas. Preflight makes real configured-provider requests; startup/readiness alone do not prove provider capability. Named PostgreSQL storage survives application restarts. The one-off command containers are retained for inspection.

Open `http://127.0.0.1:17333/ui/`. Create a user with the root key, then connect using that user's key, as described in [operations](../docs/operations.md). Change `OAM_HTTP_PORT` before starting if the port is occupied.

## 3. Simulate replicas

Initialize the database as above, then use both Compose files consistently:

```bash
docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.replicas.yaml up -d --wait api worker gateway
```

Only the gateway publishes a loopback host port; neither API replica publishes a separate port. `X-OAM-Replica` identifies the selected upstream for local verification. Caller authentication remains OAM's `api_key` mode; this sample is not an identity-asserting trusted gateway. The gateway forwards the original authorization header. Test the trusted identity boundary separately with authenticated synthetic callers and an isolated backend network.

Keep at least one healthy API and worker available during rolling replacement. Start each replacement API, wait for its readiness, reload the gateway before stopping the old API, then reload again after stopping it. The upstream uses `max_fails=0` so a transient connection failure does not temporarily remove all resolved replica addresses; eligible requests can still retry another replica during connection establishment. This is a read-availability check: already-sent POST/PATCH requests are not transparently replayed, and a response already sent to the client cannot fail over. Clients reuse their idempotency key when resolving an ambiguous accepted write. Reload the gateway with `docker compose --env-file deploy/.env -f deploy/compose.yaml -f deploy/compose.replicas.yaml exec gateway nginx -s reload`. Stop/start only selected task-owned application instances for fault simulation; do not delete persistent volumes. A graceful `stop` preserves the containers and data for inspection.

## 4. Build and verify release artifacts

The Dockerfile pins its multi-architecture Python base by digest. Update that digest deliberately and rerun both architecture checks when changing the base. On a Mac with Docker emulation enabled:

```bash
docker buildx build --platform linux/arm64 --load -t oam:0.0.1-arm64 .
docker buildx build --platform linux/amd64 --load -t oam:0.0.1-amd64 .
```

Record which checks ran natively or under emulation. The manual candidate workflow exports an OCI archive without publishing it. A local image ID, an OCI manifest digest and a published registry digest are different identities; record the one actually tested. Candidate evidence and the opt-in public HTTP probe are described in [release evidence](../docs/release-evidence.md). Source visibility, release uploads, registry publication and release-triggering pushes remain manual release actions after content review.

## Trusted gateway boundary

For `tests/release/verify_candidate.py`, use a separate synthetic deployment with the following explicit operator overlay. The normal 24-hour Insight delay is unsuitable for a four-minute probe. These settings bound the fixture's output reservations within its 300,000 generation-token budget; they are not production tuning advice. Mount the merged operator file into both API and worker roles before retaining the fixture. Set `OAM_RELEASE_BASE_URL`, `OAM_RELEASE_ROOT_KEY` and `OAM_RELEASE_OUTPUT` to that isolated deployment and a private output directory, then run `uv run python tests/release/verify_candidate.py`. Reuse the output directory only with unchanged fixture bytes and index generation.

```yaml
extraction:
  max_output_tokens: 16384
index:
  insight_consolidation_delay_hours: 0.00001
  generation_token_budget: 300000
  insight_seed_policy: source
  insight_max_output_tokens: 8192
  insight_source_batch:
    seed_max: 32
    input_max_tokens: 32768
    target_max: 64
```

The bundled gateway forwards user API keys. To use `auth.mode: trusted`, isolate the API listener on a backend-only network and authenticate callers at your gateway. Replace `X-OAM-Tenant`, `X-OAM-User` and `X-OAM-Role` from authenticated identity; never forward caller-supplied assertions. Nonlocal trusted calls also need the root bearer proof, supplied only by the gateway. Deny `/v1/users`, `/v1/credentials` and its children, and `/v1/users/{user_id}/grants/{principal_id}` on that personal gateway before forwarding root proof. Keep administrative access on a separately authorized path. A caller must not reach the backend directly. The local release simulation verifies these controls with synthetic Basic authentication; it does not certify an external identity provider or production gateway.
