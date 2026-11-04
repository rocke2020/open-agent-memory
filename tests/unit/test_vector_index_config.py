"""Physical vector-index policy must not invalidate content checkpoints."""

from pathlib import Path

import pytest

from oam.config import ConfigurationError, load_config
from oam.indexing.identity import index_generation

ROOT = Path(__file__).parents[2]
ENVIRONMENT = {
    "VLM_MODEL": "synthetic-text",
    "VLM_BASE_URL": "http://example.invalid/v1",
    "VLM_API_KEY": "synthetic",
    "OAM_EMBEDDING_MODEL": "synthetic-vector",
    "OAM_EMBEDDING_URL": "http://example.invalid/v1",
    "OAM_EMBEDDING_KEY": "synthetic",
}


def test_shared_default_and_mode_switch_preserve_content_generation(tmp_path):
    original = load_config(ROOT / "configs/oam.yaml").config
    assert getattr(original.database, "vector_index_mode", None) == "small"
    for mode in ("small", "large", "growing"):
        path = tmp_path / f"{mode}.yaml"
        path.write_text(f"database:\n  vector_index_mode: {mode}\n")
        selected = load_config(path).config
        assert selected.database.vector_index_mode == mode
        assert index_generation(selected, ENVIRONMENT) == index_generation(original, ENVIRONMENT)


@pytest.mark.parametrize(
    "setting",
    ["vector_index_mode: exact", "vector_index_min_rows: 0", "vector_index_poll_seconds: 0"],
)
def test_invalid_physical_index_policy_is_rejected(tmp_path, setting):
    path = tmp_path / "invalid.yaml"
    path.write_text(f"database:\n  {setting}\n")
    with pytest.raises(ConfigurationError):
        load_config(path)
