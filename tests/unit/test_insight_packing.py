"""The producer must pack eligible facts before dispatching bounded synthesis."""

from pathlib import Path

from oam.config import load_config
from oam.indexing import extraction


def test_insight_packing_bounds_complete_prompt_and_keeps_priority_order():
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    facts = tuple(
        extraction.InsightInput(
            id=f"fact-{index}", text="long source-supported claim " * 200, source_refs=("source:1",)
        )
        for index in range(64)
    )
    packed = extraction.pack_insight_facts(facts, config)
    assert 2 <= len(packed) < len(facts)
    assert packed == facts[: len(packed)]
    assert (
        extraction.model_input_tokens(
            extraction.INSIGHT_SYSTEM,
            {"facts": [fact.model_dump(mode="json") for fact in packed]},
            config,
        )
        <= config.extraction.max_input_tokens
    )
    assert (
        extraction.model_input_tokens(
            extraction.INSIGHT_SYSTEM,
            {"facts": [fact.model_dump(mode="json") for fact in facts[: len(packed) + 1]]},
            config,
        )
        > config.extraction.max_input_tokens
    )
