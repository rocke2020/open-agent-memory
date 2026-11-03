"""A spending limit must not invalidate already produced memory or checkpoints."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from oam.config import ConfigurationError, ModelsConfig, load_config
from oam.indexing.consolidation import _group_inputs
from oam.indexing.identity import index_generation
from oam.indexing.store import input_digest


def test_generation_budget_preserves_l1_and_completed_l2_input_identity():
    base = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    base = base.model_copy(update={"models": ModelsConfig()})
    limited = base.model_copy(
        update={"index": base.index.model_copy(update={"generation_token_budget": 100000})}
    )
    assert base.index.generation_token_budget is None
    assert index_generation(base, {}) == index_generation(limited, {})
    before = SimpleNamespace(config=base, generation_id="existing")
    after = SimpleNamespace(config=limited, generation_id="existing")
    assert input_digest(_group_inputs(before, (), (), (), [])) == input_digest(
        _group_inputs(after, (), (), (), [])
    )


@pytest.mark.parametrize("limit", [0, -1])
def test_generation_budget_rejects_nonpositive_limits(tmp_path, limit):
    path = tmp_path / "budget.yaml"
    path.write_text(f"index:\n  generation_token_budget: {limit}\n")
    with pytest.raises(ConfigurationError):
        load_config(path)
