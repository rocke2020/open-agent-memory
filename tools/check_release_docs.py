"""Prepare or check release notes and a complete documentation review. No provider calls."""

import argparse
import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

DOCUMENT_SUFFIXES = {".md", ".rst", ".adoc"}
REVIEW_ROW = re.compile(r"^- \*\*(updated|no update needed|pending)\*\* `([^`]+)`: (.+)$")
HISTORICAL_PRODUCT_TAG = "v0.0.1"  # Released before this review gate existed.


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def project_version(content: str) -> str:
    version = tomllib.loads(content)["project"]["version"]
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]*", version):
        raise ValueError("Invalid project version")
    return version


def review(base: str, prepare: bool, required: bool) -> None:
    base_sha = git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    previous = project_version(git("show", f"{base_sha}:pyproject.toml"))
    version = project_version(Path("pyproject.toml").read_text())
    if version == previous and not prepare and not required:
        print(f"Product version unchanged ({version}); no release review required.")
        return
    if version == previous:
        raise ValueError("Select a base from the previous product release, before the version bump")

    # A PR's base detects the bump; the release tag defines the complete review scope.
    release_base = git(
        "rev-parse", "--verify", "--end-of-options", f"refs/tags/v{previous}^{{commit}}"
    )
    git("merge-base", "--is-ancestor", release_base, "HEAD")
    report_path = Path(f"docs/releases/{version}.md")
    files = sorted(
        path
        for path in git("ls-files", "-z").split("\0")
        if path and Path(path).is_file() and Path(path) != report_path
    )
    documents = {path for path in files if Path(path).suffix.lower() in DOCUMENT_SUFFIXES}
    scope = hashlib.sha256()
    for path in files:
        scope.update(path.encode() + b"\0" + hashlib.sha256(Path(path).read_bytes()).digest())
    fingerprint = scope.hexdigest()
    changed = {
        path for path in git("diff", "--name-only", "-z", release_base, "--").split("\0") if path
    }

    if prepare:
        print(f"# Open Agent Memory {version}\n")
        print(f"Base: {release_base}\n\nScope: {fingerprint}\n")
        print("## Changes\n\nTODO: describe user-visible changes.\n")
        print("## Documentation review\n")
        for path in sorted(documents):
            print(f"- **pending** `{path}`: TODO: compare with changed code.")
        print("\n## Changed files since the previous release\n")
        for path in sorted(changed - {str(report_path)}):
            print(f"- `{path}`")
        return

    if not report_path.is_file():
        raise ValueError(f"Missing release notes and documentation review: {report_path}")
    report = report_path.read_text()
    errors = []
    if f"Base: {release_base}" not in report.splitlines():
        errors.append("review base does not match the previous product release")
    if f"Scope: {fingerprint}" not in report.splitlines():
        errors.append("stale review: tracked inputs changed; regenerate and review the changes")
    changes = re.search(r"^## Changes\s*\n(.*?)(?=^## |\Z)", report, re.M | re.S)
    if not changes or not changes[1].strip() or "TODO" in changes[1]:
        errors.append("unresolved release notes: describe user-visible changes under '## Changes'")
    section = re.search(r"^## Documentation review\s*\n(.*?)(?=^## |\Z)", report, re.M | re.S)
    rows = {}
    for line in section[1].splitlines() if section else []:
        match = REVIEW_ROW.fullmatch(line)
        if not match:
            continue
        status, path, reason = match.groups()
        if path in rows:
            errors.append(f"duplicate review: {path}")
        rows[path] = status
        if status == "pending" or "TODO" in reason or not reason.strip():
            errors.append(f"unresolved documentation review: {path}")
        if status == "updated" and path not in changed:
            errors.append(f"document marked updated without a change: {path}")
    for path in sorted(documents - rows.keys()):
        errors.append(f"missing documentation review: {path}")
    for path in sorted(rows.keys() - documents):
        errors.append(f"review references a missing document: {path}")
    if errors:
        raise ValueError("\n".join(errors))
    print(f"Release {version}: notes and {len(documents)} documentation reviews complete.")
    print("Coverage checked; the releaser remains responsible for semantic accuracy.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", help="Previous release tag or PR base commit (requires full tags)"
    )
    parser.add_argument("--prepare", action="store_true", help="Print a pending review draft")
    parser.add_argument("--require-review", action="store_true", help="Reject unchanged versions")
    args = parser.parse_args()
    try:
        base = args.base or git("describe", "--tags", "--match", "v[0-9]*", "--abbrev=0")
        if not args.base and base != HISTORICAL_PRODUCT_TAG:
            tagged_commit = git("rev-parse", "--verify", f"refs/tags/{base}^{{commit}}")
            if tagged_commit == git("rev-parse", "HEAD"):
                version = project_version(Path("pyproject.toml").read_text())
                if base != f"v{version}":
                    raise ValueError("Product tag and project version disagree")
                base = git("describe", "--tags", "--match", "v[0-9]*", "--abbrev=0", "HEAD^")
                args.require_review = True
        review(base, args.prepare, args.require_review)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print(
            f"Release documentation gate failed (check base and fetch tags): {detail}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
