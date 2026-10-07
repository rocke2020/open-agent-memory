from oam.config import ModelsConfig, load_config
from oam.paths import default_config_path


def test_overflow_recipe_invalidates_legacy_generation_and_references_change_it():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    config = config.model_copy(
        update={
            "extraction": config.extraction.model_copy(
                update={
                    "group_max_passages": 1,
                    "reference_max_facts": 0,
                    "max_output_tokens": 4096,
                }
            )
        }
    )
    assert index_generation(config, {}) != (
        "gen_f9e12d2e715206e518c73347ad81901d748583d10233a745ebfbabd66f98d758"
    )
    enabled = config.model_copy(
        update={"extraction": config.extraction.model_copy(update={"reference_max_facts": 8})}
    )
    assert index_generation(enabled, {}) != index_generation(config, {})


def test_default_grouping_uses_a_distinct_generation_from_singletons():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    singleton = config.model_copy(
        update={"extraction": config.extraction.model_copy(update={"group_max_passages": 1})}
    )
    assert index_generation(config, {}) != index_generation(singleton, {})


def test_resource_completion_budget_invalidates_generated_resource_checkpoints():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    tuned = config.model_copy(
        update={"resource": config.resource.model_copy(update={"max_output_tokens": 8192})}
    )
    assert index_generation(config, {}) != index_generation(tuned, {})


def test_recall_tuning_does_not_invalidate_indexed_content_but_model_change_does():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    tuned = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={"mid": config.search.mid.model_copy(update={"max_results": 12})}
            ),
            "chunk": config.chunk.model_copy(
                update={
                    "recall": config.chunk.recall.model_copy(
                        update={"default_tokens": 100, "max_tokens": 200}
                    )
                }
            ),
        }
    )
    assert index_generation(config, {}) == index_generation(tuned, {})
    changed = config.model_copy(
        update={"index": config.index.model_copy(update={"embedding_dimensions": 768})}
    )
    assert index_generation(config, {}) != index_generation(changed, {})


def test_environment_model_name_is_part_of_index_generation_identity():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    models = ModelsConfig.model_validate(
        {
            "embedding": {
                "protocol": "openai_compatible",
                "model_env": "OAM_EMBEDDING_MODEL",
                "endpoint_env": "OAM_EMBEDDING_URL",
                "capabilities": ["embeddings"],
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
