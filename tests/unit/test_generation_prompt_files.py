"""Generation rules must load from editable resources in a fresh process."""

import json
import shutil
import subprocess
import sys
from pathlib import Path


def test_fresh_process_uses_insight_abstract_and_profile_prompt_files(tmp_path):
    package = tmp_path / "oam"
    shutil.copytree(
        Path(__file__).parents[2] / "src/oam", package, ignore=shutil.ignore_patterns("__pycache__")
    )
    for name, content in (
        ("indexing/insight-prompt.txt", "Insight generation rules — 示例.\n"),
        ("resources/abstract-prompt.txt", "Document abstract rules.\n"),
        ("profiles/prompt.txt", "Default Profile rules.\n"),
    ):
        (package / name).write_text(content, encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import json,sys; sys.path[:0]=[sys.argv[1],*json.loads(sys.argv[2])]; "
            "from oam.indexing.extraction import INSIGHT_SYSTEM; "
            "from oam.workers.index_source import ABSTRACT_SYSTEM; "
            "from oam.profiles.prompt import PROFILE_SYSTEM; "
            "print(json.dumps([INSIGHT_SYSTEM,ABSTRACT_SYSTEM,PROFILE_SYSTEM]))",
            str(tmp_path),
            json.dumps([path for path in sys.path if "site-packages" in path]),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "Insight generation rules — 示例.\n",
        "Document abstract rules.",
        "Default Profile rules.",
    ]
