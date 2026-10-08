"""Prompt-file edits must reach fresh extraction calls without changing Python."""

import json
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path


def test_fresh_process_uses_prompt_files_for_all_extraction_variants(tmp_path):
    # Copy the real package so the process loads edited resources without changing the checkout.
    package = tmp_path / "oam"
    shutil.copytree(
        Path(__file__).parents[2] / "src/oam", package, ignore=shutil.ignore_patterns("__pycache__")
    )
    (tmp_path / "configs").mkdir()
    shutil.copyfile(Path(__file__).parents[2] / "configs/oam.yaml", tmp_path / "configs/oam.yaml")
    (package / "indexing/extraction-prompt.txt").write_text(
        "Base extraction instructions — 示例.\n", encoding="utf-8"
    )
    (package / "indexing/extraction-group-prefix.txt").write_text(
        "\nGrouped passage ownership.\n", encoding="utf-8"
    )
    (package / "indexing/extraction-reference-suffix.txt").write_text(
        "\nReference facts are context.\n", encoding="utf-8"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import json,sys; from pathlib import Path; sys.path[:0]=sys.argv[1:3]; "
            "from oam.config import load_config; "
            "from oam.indexing.extraction import EXTRACTION_SYSTEM,REFERENCE_EXTRACTION_SYSTEM; "
            "from oam.indexing.grouping import group_system; "
            "config=load_config(Path(sys.argv[3])).config; "
            "enabled=config.model_copy(update={'extraction': "
            "config.extraction.model_copy(update={'reference_max_facts':1})}); "
            "disabled=config.model_copy(update={'extraction': "
            "config.extraction.model_copy(update={'reference_max_facts':0})}); "
            "print(json.dumps([EXTRACTION_SYSTEM,REFERENCE_EXTRACTION_SYSTEM,"
            "group_system(disabled),group_system(enabled)]))",
            str(tmp_path),
            sysconfig.get_path("purelib"),
            str(tmp_path / "configs/oam.yaml"),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "Base extraction instructions — 示例.\n",
        "Base extraction instructions — 示例.\n\nReference facts are context.\n",
        "\nGrouped passage ownership.\nBase extraction instructions — 示例.\n",
        "\nGrouped passage ownership.\nBase extraction instructions — 示例.\n"
        "\nReference facts are context.\n",
    ]
