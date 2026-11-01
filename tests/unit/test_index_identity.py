from oam.config import ModelsConfig, load_config
from oam.paths import default_config_path


def test_concise_extraction_invalidates_prior_prompt_checkpoints():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    assert index_generation(config, {}) != (
        "gen_e2621ff78fdee6a06d170b921025404478e75e00bf24f0a2d594ba0d78841207"
    )


def test_event_time_grounding_invalidates_prior_extraction_checkpoints():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    assert index_generation(config, {}) != (
        "gen_f82942ddf971c342c92bf8d7247eee5fe3cb8dbc8125cfbfdf3ae2093e9fdbfb"
    )


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


def test_insight_consolidation_delay_does_not_invalidate_indexed_content(tmp_path):
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    path = tmp_path / "oam.yaml"
    path.write_text("index:\n  insight_consolidation_delay_hours: 2\n")
    tuned = load_config(path).config.model_copy(update={"models": ModelsConfig()})
    assert index_generation(config, {}) == index_generation(tuned, {})


def test_insight_embedding_recipe_changes_only_an_insights_enabled_generation(monkeypatch):
    from oam.indexing import identity

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    enabled = config.model_copy(
        update={"index": config.index.model_copy(update={"insights_enabled": True})}
    )
    disabled = config.model_copy(
        update={"index": config.index.model_copy(update={"insights_enabled": False})}
    )
    enabled_generation = identity.index_generation(enabled, {})
    disabled_generation = identity.index_generation(disabled, {})

    monkeypatch.setattr(
        identity, "INSIGHT_RETRIEVAL_RECIPE", "different-purpose-recipe", raising=False
    )

    assert identity.index_generation(enabled, {}) != enabled_generation
    assert identity.index_generation(disabled, {}) == disabled_generation


def test_resource_completion_budget_invalidates_generated_resource_checkpoints():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    tuned = config.model_copy(
        update={"resource": config.resource.model_copy(update={"max_output_tokens": 8192})}
    )
    assert index_generation(config, {}) != index_generation(tuned, {})


def test_vision_pixel_target_invalidates_resource_projection_generation():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    tuned = config.model_copy(
        update={"resource": config.resource.model_copy(update={"max_pixels_per_image": 20_000_000})}
    )

    assert index_generation(config, {}) != index_generation(tuned, {})


def test_vision_preprocessing_recipe_invalidates_resource_projection_generation(monkeypatch):
    from oam.indexing import identity

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    current = identity.index_generation(config, {})
    monkeypatch.setattr(
        identity, "RESOURCE_VISION_RECIPE", "legacy-original-image-input", raising=False
    )

    assert identity.index_generation(config, {}) != current


def test_recall_tuning_does_not_invalidate_indexed_content_but_model_change_does():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    tuned = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={
                    "normal": config.search.normal.model_copy(
                        update={"max_results": 12, "context_max_tokens": 13}
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


def test_vlm_route_and_model_spelling_do_not_change_index_generation_identity():
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config
    first = {
        "VLM_MODEL": "deepseek-flash",
        "VLM_BASE_URL": "https://api.deepseek.com/v1",
        "VLM_API_KEY": "first-secret",
        "OAM_EMBEDDING_MODEL": "synthetic",
        "OAM_EMBEDDING_URL": "http://embedding.test/v1",
        "OAM_EMBEDDING_KEY": "embedding-secret",
    }
    second = {
        **first,
        "VLM_MODEL": "deepseek/deepseek-flash",
        "VLM_BASE_URL": "https://tcai.example/v1",
        "VLM_API_KEY": "second-secret",
    }

    assert index_generation(config, first) == index_generation(config, second)


def test_extraction_prompt_revision_invalidates_index_generation(monkeypatch):
    from oam.indexing import identity

    config = load_config(default_config_path()).config.model_copy(update={"models": ModelsConfig()})
    current = identity.index_generation(config, {})
    monkeypatch.setattr(identity, "INDEX_RECIPE", "oam-source-fact-insight-v24")
    assert identity.index_generation(config, {}) != current


def test_explicit_legacy_counter_preserves_compatible_prior_generation():
    from oam.config import TokenizerConfig
    from oam.indexing.identity import index_generation

    config = load_config(default_config_path()).config.model_copy(
        update={
            "models": ModelsConfig(),
            "tokenizer": TokenizerConfig(type="oam-unicode-quarter-v1"),
        }
    )
    assert (
        index_generation(config, {})
        == "gen_bcbbe1a13f79116323228293c323d0ec162fe99987a3a54822029f0cb3563c8b"
    )


def test_request_scheduling_limit_does_not_change_source_recipe_above_item_limit():
    from oam.config import EmbeddingModelBinding, ModelCapability, TokenizerConfig
    from oam.indexing.identity import index_generation

    binding = EmbeddingModelBinding(
        protocol="openai_compatible",
        model="synthetic",
        endpoint_env="VECTOR_URL",
        capabilities=frozenset({ModelCapability.EMBEDDINGS}),
        tokenizer=TokenizerConfig(type="cl100k_base"),
        max_input_tokens=8192,
        max_request_tokens=300000,
    )
    config = load_config(default_config_path()).config.model_copy(
        update={"models": ModelsConfig(embedding=binding)}
    )
    smaller = config.model_copy(
        update={
            "models": config.models.model_copy(
                update={"embedding": binding.model_copy(update={"max_request_tokens": 200000})}
            )
        }
    )
    assert index_generation(
        config, {"VECTOR_URL": "https://vector.example/v1"}
    ) == index_generation(smaller, {"VECTOR_URL": "https://vector.example/v1"})
