import pytest

from oam.indexing.entity_names import entity_name_key


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("  ＮＯＶＡ\t Research  ", "nova research"),
        ("Straße", "strasse"),
        ("LAB-10", "lab-10"),
        ("十层", "十层"),
        ("10 楼", "10 楼"),
    ],
)
def test_entity_lookup_normalization_preserves_meaningful_characters(name, expected):
    assert entity_name_key(name, max_bytes=1024) == expected


@pytest.mark.parametrize("name", ["", " \t", "研究院", "ＡＡＡ"])
def test_entity_lookup_rejects_blank_or_oversized_display_and_keys(name):
    with pytest.raises(ValueError):
        entity_name_key(name, max_bytes=4)


def test_entity_indexing_options_belong_to_index_generation():
    from oam.config import ModelsConfig, load_config
    from oam.indexing.identity import index_generation
    from oam.paths import default_config_path

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    assert config.index.entity_name_max_bytes == 1024
    tuned = config.model_copy(
        update={"index": config.index.model_copy(update={"entity_name_max_bytes": 512})}
    )
    assert index_generation(config, {}) != index_generation(tuned, {})
