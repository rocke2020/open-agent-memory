"""Exercise the release gate against isolated Git repositories, without providers."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools/check_release_docs.py"


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def release_repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "release@example.test")
    git(tmp_path, "config", "user.name", "Release test")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.0.1"\n')
    (tmp_path / "README.md").write_text("# Product\nOld usage.\n")
    (tmp_path / "README_CN.md").write_text("# Chinese guide\n")
    (tmp_path / "app.py").write_text("LIMIT = 1\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Initial release")
    git(tmp_path, "tag", "v0.0.1")
    return tmp_path


def run_gate(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "v0.0.1", *args],
        cwd=root,
        capture_output=True,
        text=True,
    )


def bump(root):
    (root / "pyproject.toml").write_text('[project]\nversion = "0.0.2"\n')


def finish_review(root):
    draft = run_gate(root, "--prepare")
    assert draft.returncode == 0, draft.stderr
    report = draft.stdout.replace("TODO: describe user-visible changes.", "- Fix startup errors.")
    report = report.replace("**pending**", "**no update needed**")
    report = report.replace(
        "TODO: compare with changed code.", "Startup fix preserves this contract."
    )
    path = root / "docs/releases/0.0.2.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report)
    git(root, "add", ".")
    return path


def test_ordinary_code_changes_do_not_require_release_notes(release_repo):
    (release_repo / "app.py").write_text("LIMIT = 2\n")
    result = run_gate(release_repo)
    assert result.returncode == 0, result.stderr
    assert "unchanged" in result.stdout


def test_version_bump_without_release_notes_is_blocked(release_repo):
    bump(release_repo)
    result = run_gate(release_repo)
    assert result.returncode == 1
    assert "docs/releases/0.0.2.md" in result.stderr


def test_draft_scans_all_docs_and_lists_changes_since_release(release_repo):
    bump(release_repo)
    (release_repo / "app.py").write_text("LIMIT = 2\n")
    result = run_gate(release_repo, "--prepare")
    assert result.returncode == 0, result.stderr
    assert "`README.md`" in result.stdout
    assert "`README_CN.md`" in result.stdout
    assert "`app.py`" in result.stdout
    report = release_repo / "docs/releases/0.0.2.md"
    report.parent.mkdir(parents=True)
    report.write_text(result.stdout)
    rejected = run_gate(release_repo)
    assert rejected.returncode == 1
    assert "unresolved" in rejected.stderr


def test_completed_review_passes_and_new_code_invalidates_it(release_repo):
    bump(release_repo)
    finish_review(release_repo)
    result = run_gate(release_repo)
    assert result.returncode == 0, result.stderr
    (release_repo / "app.py").write_text("LIMIT = 3\n")
    result = run_gate(release_repo)
    assert result.returncode == 1
    assert "stale" in result.stderr


@pytest.mark.parametrize("bad", ["missing document", "pending", "empty changes", "false update"])
def test_incomplete_reviews_are_blocked(release_repo, bad):
    bump(release_repo)
    path = finish_review(release_repo)
    report = path.read_text()
    if bad == "missing document":
        report = "\n".join(line for line in report.splitlines() if "`README_CN.md`" not in line)
    elif bad == "pending":
        report = report.replace("**no update needed**", "**pending**", 1)
    elif bad == "empty changes":
        report = report.replace("- Fix startup errors.", "")
    else:
        report = report.replace("**no update needed**", "**updated**", 1)
    path.write_text(report)
    result = run_gate(release_repo)
    assert result.returncode == 1, result.stdout


def test_new_document_is_included_and_missing_base_fails_closed(release_repo):
    bump(release_repo)
    (release_repo / "new guide.md").write_text("# New guide\n")
    git(release_repo, "add", ".")
    result = run_gate(release_repo, "--prepare")
    assert "`new guide.md`" in result.stdout
    git(release_repo, "tag", "-d", "v0.0.1")
    result = run_gate(release_repo)
    assert result.returncode == 1
    assert "base" in result.stderr.lower()


def test_updated_non_english_document_passes(release_repo):
    bump(release_repo)
    (release_repo / "使用指南.md").write_text("# Usage\nNew instructions.\n")
    git(release_repo, "add", ".")
    path = finish_review(release_repo)
    path.write_text(
        path.read_text().replace("**no update needed** `使用指南.md`", "**updated** `使用指南.md`")
    )
    result = run_gate(release_repo)
    assert result.returncode == 0, result.stderr


def test_pr_base_still_reviews_all_changes_since_the_previous_release(release_repo):
    (release_repo / "app.py").write_text("LIMIT = 2\n")
    git(release_repo, "add", ".")
    git(release_repo, "commit", "-qm", "Earlier code change")
    base = git(release_repo, "rev-parse", "HEAD")
    bump(release_repo)
    result = run_gate(release_repo, "--base", base, "--prepare")
    assert result.returncode == 0, result.stderr
    assert "`app.py`" in result.stdout


def test_tag_checks_require_a_new_version(release_repo):
    result = run_gate(release_repo, "--require-review")
    assert result.returncode == 1
    assert "before the version bump" in result.stderr


@pytest.mark.parametrize("state", ["complete", "missing", "pending", "stale"])
def test_default_candidate_check_validates_a_new_product_tag(release_repo, state):
    bump(release_repo)
    report = finish_review(release_repo)
    git(release_repo, "commit", "-qm", "Prepare release")
    git(release_repo, "tag", "v0.0.2")
    if state == "missing":
        report.unlink()
    elif state == "pending":
        report.write_text(report.read_text().replace("**no update needed**", "**pending**", 1))
    elif state == "stale":
        (release_repo / "app.py").write_text("LIMIT = 3\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=release_repo, capture_output=True, text=True
    )
    assert result.returncode == (0 if state == "complete" else 1), result.stdout
