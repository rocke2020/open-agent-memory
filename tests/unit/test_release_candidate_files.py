"""Candidate credentials stay private even when permission changes fail."""

import os
import runpy
import stat
from contextlib import suppress
from pathlib import Path


def test_private_candidate_save_never_relies_on_post_write_chmod(tmp_path, monkeypatch):
    save = runpy.run_path(str(Path(__file__).parents[1] / "release/verify_candidate.py"))["save"]

    def denied_chmod(*args, **kwargs):
        raise OSError("simulated interruption before permission change")

    monkeypatch.setattr(Path, "chmod", denied_chmod)
    previous = os.umask(0o022)
    try:
        with suppress(OSError):
            save(tmp_path / "user.private.json", {"key": "synthetic-only"}, private=True)
        written = [path for path in tmp_path.iterdir() if "synthetic-only" in path.read_text()]
        assert written
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in written)
    finally:
        os.umask(previous)
