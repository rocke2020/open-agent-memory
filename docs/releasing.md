# Preparing and publishing a release

## Overview

Product releases version the Python distribution, CLI, server image and GitHub source tag together. The product version is owned by `pyproject.toml`; `uv.lock` records the resolved distribution. The current product release is 0.0.2. Release preparation verifies the current code and its artifacts; publication requires the maintainer's instruction to release that version.

Integration packages have independent versions: OpenClaw 0.0.2, MCP 0.0.1 and DSH 0.0.1. Reuse their existing published archives when their package code is unchanged; do not republish an existing npm version or advance package versions solely because the product version changes. Preserve earlier product tags, images and release records.

## 1. Review release notes and documentation

Every new product version in `pyproject.toml` requires a release record at `docs/releases/<version>.md`, one file per version. See the [initial release notes](releases/0.0.1.md). Its **Changes** section tells users what was added, changed or fixed, any compatibility or migration actions, and known limitations. Reuse this section as the GitHub Release notes. Keep historical release records unchanged; a version bump alone does not justify changing historical version references or independently versioned plugin packages.

After setting the intended version, stage the intended new files so the scanner includes them, then generate a draft against the previous product tag. For example, when preparing `0.0.2` from `v0.0.1`:

```bash
mkdir -p docs/releases
python tools/check_release_docs.py --base v0.0.1 --prepare > docs/releases/0.0.2.md
git diff v0.0.1 -- src configs migrations deploy integrations pyproject.toml
```

The scanner inventories every tracked Markdown, reStructuredText and AsciiDoc file, including translations, nested integration guides and repository instructions. It lists changed files since the previous product tag. Compare every document's claims, examples, commands and configuration with the changed code and its consumers. Update affected documents, then regenerate the draft after the final code and documentation edits. Fill in the release notes and replace every pending entry with one of these results and a concrete explanation, citing relevant source paths where useful:

```markdown
- **updated** `docs/api.md`: Documented the new request field implemented in src/oam/contracts/.
- **no update needed** `deploy/README.md`: Deployment commands and configuration are unchanged.
```

Run the checker before building or publishing the release:

```bash
python tools/check_release_docs.py --base v0.0.1
```

The checker rejects missing release notes, pending or omitted document reviews, an `updated` claim without a document change, and reviews made stale by changes to tracked file contents. The current release record is excluded from its own fingerprint so completing the notes does not invalidate the review. New files must be staged before drafting; removed documents leave the current inventory. The **Base** field records the previous product tag's commit; **Scope** fingerprints the reviewed tracked inputs. Missing Git history or tags fails the check; fetch full history and tags first. No models or external services are called, and the checker never edits files.

CI checks product version changes on pull requests, `main`, product tags and manual runs. Candidate builds run the same gate before building images. At an exact product tag after the historical `v0.0.1` release, automatic checks use the previous release as the baseline and require a completed review and matching tag. Between tags, unchanged product versions skip the release requirement. The coding agent or releaser owns semantic review: passing this structural check does not certify documentation accuracy. Existing releases do not require retrospective records; plugin-only releases retain their independent versioning and release notes. Local checks cannot prevent a maintainer from manually publishing outside this procedure; keep the **Release documentation / review** status required in repository branch and tag protections where supported.

## 2. Verify the current source and upgrade

Run the repository checks and every integration package's tests:

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
npm --prefix integrations/openclaw ci
npm --prefix integrations/openclaw test
npm --prefix integrations/mcp ci
npm --prefix integrations/mcp test
npm --prefix integrations/dsh test
```

Default pytest can skip database/browser cases when their explicitly isolated infrastructure is absent. Record enabled and skipped checks separately. For 0.0.2, exercise user-list authentication, tenant filtering, accepted-input timestamps and migration 0037, plus Insight-title migration 0036 and its inspection/export/Inspector consumers. Apply all migrations before starting updated API/worker processes. These backfills preserve originals and do not require reindexing or model calls solely to populate titles and activity metadata.

Use a task-owned PostgreSQL/pgvector instance and synthetic users for release checks. Never select persistent personal/evaluation databases. Keep historical benchmark evidence unchanged; a release smoke test is not a fresh LME-500 or BEAM-10M evaluation. [Acceptance](acceptance.md) and the version's release record define the measured scope and outstanding limits.

## 3. Prepare artifacts

Build the Python distribution and both supported container architectures from the verified source:

```bash
uv build
mkdir -p dist/plugins
docker buildx build --platform linux/arm64 --load -t oam:0.0.2-arm64 .
docker buildx build --platform linux/amd64 --load -t oam:0.0.2-amd64 .
```

For unchanged, already-published integrations, download the exact registry archives and compare their runtime contents with the reviewed source:

```bash
npm pack @rocke-dong/oam-openclaw@0.0.2 --pack-destination dist/plugins
npm pack @rocke-dong/oam-mcp@0.0.1 --pack-destination dist/plugins
npm pack @rocke-dong/oam-dsh@0.0.1 --pack-destination dist/plugins
```

Record source commit, archive checksums, native versus emulated checks and immutable image identities. A local image ID does not prove a published registry digest. Verify installed Python/CLI metadata, packaged configuration/migrations and Inspector assets. Against an isolated candidate image, exercise authorized API input, readiness and cited recall, the changed user-list/title surfaces, and the relevant native integration paths. Bound any provider calls and retain actual usage and failures separately from deterministic tests.

The opt-in `tests/release/verify_candidate.py` command uses the bounded synthetic overlay documented in [deployment](../deploy/README.md#trusted-gateway-boundary). Configure its isolated endpoint, root key and private output directory explicitly. Never place credentials, raw model responses or private user content in public artifacts.

## 4. Publish the verified product

After committing the verified source, create the new annotated product tag at that exact commit and run the documentation gate against the previous product release. Do not move an existing tag. Create a source archive from the new tag and checksums for the reviewed distribution, source and plugin archives. Attach those artifacts and sanitized verification evidence to the GitHub Release, using the release record's Changes section as its notes.

Publish the multi-architecture image from that same source:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/rocke2020/open-agent-memory:0.0.2 \
  -t ghcr.io/rocke2020/open-agent-memory:v0.0.2 \
  -t ghcr.io/rocke2020/open-agent-memory:0.0 \
  -t ghcr.io/rocke2020/open-agent-memory:0 \
  -t ghcr.io/rocke2020/open-agent-memory:latest --push .
```

Verify that the version tags and moving aliases resolve to the published reviewed manifest. Existing npm versions are reused rather than published again. GitHub source/image publication and any separate package publication have distinct results and must be reported separately.

## 5. Check public downloads

Pull the published image by digest anonymously on both supported architectures, then verify startup, migrations, authorized reads/writes, cited recall and Inspector assets using an isolated deployment. Download the GitHub source/distribution/plugin archives and checksums without credentials; compare them with the reviewed bytes. Verify the source archive against the tagged tree and integration archives against their published versions. Preserve still-unrun native host or recovery checks as explicit limitations; neither a successful upload nor a local build proves public-download acceptance.
