# Preparing and publishing a release

## Overview

Candidate preparation ends with locally verified images, both plugin archives and checksums, source identity, evidence, and a human review. Publication is a separate explicit action. v0.0.1 requires public npm packages for both the OpenClaw and DSH plugins, alongside versioned GitHub Release archives. Installing public packages or release archives needs no npm account; the maintainer needs npm publishing access.

Plugin packages can be versioned and published independently. For the OpenClaw response-boundary fix, merge the package source into `main` and publish `@rocke-dong/oam-openclaw@0.0.2`; keep the OAM Python/CLI version, product tag, server image and DSH package at `0.0.1`. Do not move the existing `v0.0.1` tag or create a product `v0.0.2` release. The full-product procedure below remains the historical v0.0.1 procedure; package-only publication uses the changed package's own version and reviewed archive.

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

## 2. Review the candidate

Read [release evidence](release-evidence.md), [acceptance](acceptance.md), [deployment](../deploy/README.md), and [third-party notices](../THIRD_PARTY.md). Check the recorded source/configuration hashes and local image IDs against the artifacts being reviewed. A local image ID is not a published registry digest. Review the source changes, the self-contained indexing/recall design and required acknowledgments before making the repository public.

Build and validate the product and both plugins from the reviewed source:

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
npm --prefix integrations/openclaw ci
npm --prefix integrations/openclaw test
npm --prefix integrations/dsh test
```

Default pytest can skip integration/browser checks when their explicit isolated infrastructure is absent. Compare those skips with the candidate's enabled integration evidence; a default green run alone does not certify the release. The opt-in public HTTP probe and its synthetic configuration are documented in the deployment guide.

## 3. Prepare plugin assets

Use a separate output directory and keep both packages at version `0.0.1`: `@rocke-dong/oam-openclaw` and `@rocke-dong/oam-dsh`. The publisher uses the `rocke-dong` npm account. Both package manifests declare public access and the official npm registry through `publishConfig`; they permit publishing. Authenticate the intended publisher before release, and keep credentials outside package contents.

Create `dist/plugins` at the repository root. From `integrations/openclaw`, then from `integrations/dsh`, run:

```bash
npm pack --offline --pack-destination ../../dist/plugins
```

OpenClaw's pack step builds its bundled entrypoint using the installed development tool. Verify each archive's name, version, license, entrypoint and runtime bytes, then create `SHA256SUMS` with `shasum -a 256 *.tgz` in the output directory. Install using the [OpenClaw](../integrations/openclaw/README.md) and [DSH](../integrations/dsh/README.md) native local-package instructions. Local packages have no runtime npm dependencies; the hosts may still need network access for their own dependencies.

Run publish dry-runs for both exact reviewed archives from the repository root. The package manifests select the official registry and public access. A dry-run does not upload the package and does not prove final registry write authorization.

```bash
npm whoami --registry=https://registry.npmjs.org/
npm publish --dry-run ./dist/plugins/rocke-dong-oam-openclaw-0.0.1.tgz
npm publish --dry-run ./dist/plugins/rocke-dong-oam-dsh-0.0.1.tgz
```

## 4. Publish only after human review

The maintainer chooses the final commit/tag and personally changes repository visibility. Keep publication commands out of automatic candidate checks. The manual candidate workflow only builds an OCI artifact; it does not publish images or packages.

After the user confirms the final release, publish both exact reviewed plugin archives at version `0.0.1`. The account's configured publishing authentication still applies.

```bash
npm publish ./dist/plugins/rocke-dong-oam-openclaw-0.0.1.tgz
npm publish ./dist/plugins/rocke-dong-oam-dsh-0.0.1.tgz
```

A maintainer with registry authority can build and publish the reviewed source:

```bash
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/rocke2020/open-agent-memory:0.0.1 \
  -t ghcr.io/rocke2020/open-agent-memory:v0.0.1 \
  -t ghcr.io/rocke2020/open-agent-memory:0.0 \
  -t ghcr.io/rocke2020/open-agent-memory:0 \
  -t ghcr.io/rocke2020/open-agent-memory:latest --push .
```

Record the resulting registry digest and verify that all five tags resolve to that reviewed manifest. Attach both versioned plugin archives, `SHA256SUMS`, sanitized candidate evidence and the final source/configuration identity to the GitHub Release. Preserve dependency notices and add the reviewed public acknowledgments. Container and package publication do not follow automatically from changing source visibility.

## 5. Check downloaded artifacts

After publication, verify that `@rocke-dong/oam-openclaw` and `@rocke-dong/oam-dsh` expose version `0.0.1` on the official registry. Compare each downloaded registry tarball's integrity with its reviewed archive, then install by versioned package name through each host's native package manager in fresh isolated profiles and repeat completed capture, readiness and cited recall. Record package names, versions, registry integrity, host versions and the OAM image identity. npm login, a dry-run or local archive tests do not complete this public-package gate.

After publication, pull the published image by its registry digest and verify startup, migrations, readiness, authorized retain/index/recall, Profile reuse and Inspector assets. Download both plugin archives and checksums from the release, verify them, install into fresh isolated host profiles, and repeat completed capture plus cited recall in a fresh session. Record host versions and receipts. These public-download and registry-pull checks are unverified until the actual artifacts exist; local candidate checks do not substitute for them.
