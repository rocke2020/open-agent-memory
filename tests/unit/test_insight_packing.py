"""The producer must pack eligible facts before dispatching bounded synthesis."""

from pathlib import Path

from oam.config import load_config
from oam.indexing import extraction
from oam.tokens import estimate_units, estimated_tokens


def test_model_prompt_bound_counts_the_complete_serialized_messages():
    system = "Use only supplied facts."
    envelope = {"facts": [{"text": "甲😀"}]}
    user = extraction.json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    messages = extraction.json.dumps(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert extraction.model_input_tokens(system, envelope) == estimated_tokens(
        estimate_units(messages)
    )


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
        )
        <= config.extraction.max_input_tokens
    )
    assert (
        extraction.model_input_tokens(
            extraction.INSIGHT_SYSTEM,
            {"facts": [fact.model_dump(mode="json") for fact in facts[: len(packed) + 1]]},
        )
        > config.extraction.max_input_tokens
    )
