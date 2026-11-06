from pathlib import Path

import pytest
import yaml

from oam.config import ConfigurationError, ModelsConfig, TokenizerConfig, load_config
from oam.indexing.identity import index_generation


def _config(tmp_path, bundle):
    path = tmp_path / "source-batch.yaml"
    path.write_text(yaml.safe_dump({"index": {"insight_source_batch": bundle}}))
    return load_config(path).config


def test_source_batch_defaults_leave_shared_configuration_unchanged():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    assert config.index.insight_source_batch is None
    assert config.for_insight_source_batch() is config


def test_source_batch_effective_configuration_preserves_l1_and_lookup_limits(tmp_path):
    config = _config(tmp_path, {"seed_max": 32, "input_max_tokens": 32768, "target_max": 64})
    effective = config.for_insight_source_batch()
    assert effective.index.insight_seed_max == 32
    assert effective.index.insight_candidate_max == 64
    assert effective.extraction.max_input_tokens == 32768
    assert effective.extraction.max_facts_per_group == 64
    assert config.index.insight_seed_max == 8
    assert config.index.insight_candidate_max == 8
    assert config.extraction.max_input_tokens == 8192
    assert config.extraction.max_facts_per_group == 64
    assert effective.models == config.models
    assert effective.index.insight_max_output_tokens == config.index.insight_max_output_tokens


@pytest.mark.parametrize(
    "bundle",
    [
        {"seed_max": 0, "input_max_tokens": 32768, "target_max": 64},
        {"seed_max": 65, "input_max_tokens": 32768, "target_max": 64},
        {"seed_max": 32, "input_max_tokens": 0, "target_max": 64},
        {"seed_max": 32, "input_max_tokens": 32768, "target_max": 0},
        {"seed_max": 32, "input_max_tokens": 32768, "target_max": 129},
        {"seed_max": 32, "input_max_tokens": 32768},
    ],
)
def test_source_batch_rejects_invalid_or_incomplete_limits(tmp_path, bundle):
    with pytest.raises(ConfigurationError):
        _config(tmp_path, bundle)


def test_source_batch_accepts_upper_bounds(tmp_path):
    config = _config(tmp_path, {"seed_max": 64, "input_max_tokens": 32768, "target_max": 128})
    effective = config.for_insight_source_batch()
    assert effective.index.insight_seed_max == 64
    assert effective.index.insight_candidate_max == 128


@pytest.mark.parametrize(
    "bundle",
    [None, {"seed_max": 32, "input_max_tokens": 32768, "target_max": 64}],
)
def test_source_batch_preserves_captured_legacy_index_generation(tmp_path, bundle, monkeypatch):
    monkeypatch.setattr("oam.indexing.identity.INDEX_RECIPE", "oam-source-fact-insight-v38")
    config = _config(tmp_path, bundle).model_copy(
        update={
            "models": ModelsConfig(),
            "tokenizer": TokenizerConfig(type="oam-unicode-quarter-v1"),
        }
    )
    assert index_generation(config, {}) == (
        "gen_bcbbe1a13f79116323228293c323d0ec162fe99987a3a54822029f0cb3563c8b"
    )
