from oam.config import ModelsConfig, load_config
from oam.paths import default_config_path


def test_recall_tuning_does_not_invalidate_indexed_content_but_model_change_does():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config
    tuned = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={"mid": config.search.mid.model_copy(update={"max_results": 12})}
            )
        }
    )
    assert index_generation(config, {}) == index_generation(tuned, {})
    changed = config.model_copy(
        update={"index": config.index.model_copy(update={"embedding_dimensions": 768})}
    )
    assert index_generation(config, {}) != index_generation(changed, {})


def test_environment_model_name_is_part_of_index_generation_identity():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config
    models = ModelsConfig.model_validate(
        {
            "roles": {"embedding": "local"},
            "bindings": {
                "local": {
                    "protocol": "openai_compatible",
                    "model_env": "OAM_EMBEDDING_MODEL",
                    "endpoint_env": "OAM_EMBEDDING_URL",
                    "capabilities": ["embeddings"],
                }
            },
        }
    )
    configured = config.model_copy(update={"models": models})
    environment = {
        "OAM_EMBEDDING_MODEL": "embedding-a",
        "OAM_EMBEDDING_URL": "http://model.test/v1",
    }

    first = index_generation(configured, environment)
    environment["OAM_EMBEDDING_MODEL"] = "embedding-b"

    assert first != index_generation(configured, environment)
