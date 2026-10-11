import json

from oam.config import load_config
from oam.indexing.extraction import pack_reference_facts
from oam.paths import default_config_path
from oam.tokens import UNITS_PER_TOKEN, estimate_units


def test_supported_name_metadata_cannot_escape_the_existing_reference_budget():
    config = load_config(default_config_path()).config
    references = (
        {
            "id": "fact",
            "text": "Nova Research opened a lab.",
            "entities": [
                {"name": "NR", "quote": "Nova Research also called NR " + "support " * 2000}
            ],
        },
    )
    packed = pack_reference_facts(references, {"center": {"text": "I joined NR."}}, config)
    assert (
        estimate_units(json.dumps(list(packed), ensure_ascii=False, separators=(",", ":")))
        <= config.extraction.reference_max_input_tokens * UNITS_PER_TOKEN
    )
