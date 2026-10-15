import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from oam.config import ConfigurationError, load_config

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_fts_rank_weights_reject_values_outside_postgres_range() -> None:
    from pydantic import ValidationError

    from oam.config import SearchConfig

    search = load_config(REPOSITORY_ROOT / "configs" / "oam.yaml").config.search
    for invalid in (-0.1, 1.01, float("inf"), float("nan")):
        values = search.model_dump()
        values["fts_rank_weights"] = [invalid, *values["fts_rank_weights"][1:]]
        with pytest.raises(ValidationError):
            SearchConfig.model_validate(values)


def test_canonical_config_freezes_v001_trial_limits_and_model_role_bindings() -> None:
    resolved = load_config(REPOSITORY_ROOT / "configs" / "oam.yaml")

    assert resolved.config.contract_version == "0.0.1"
    assert resolved.config.resource.max_bytes == 100 * 1024 * 1024
    assert resolved.config.resource.max_pdf_pages == 100
    assert resolved.config.resource.max_images == 20
    assert resolved.config.resource.max_pixels_per_image == 40_000_000
    assert resolved.config.resource.max_input_pixels_per_image == 100_000_000
    assert resolved.config.resource.max_total_pixels == 250_000_000
    assert resolved.config.api.max_request_bytes == 128 * 1024 * 1024
    assert resolved.config.user.max_reports == 100
    assert resolved.config.user.max_chats == 1_000
    assert resolved.config.api.max_concurrent_requests == 10
    assert resolved.config.models.vlm is not None
    assert resolved.config.models.embedding is not None
    assert resolved.config.models.vlm.model_env == "VLM_MODEL"
    assert resolved.config.models.vlm.endpoint_env == "VLM_BASE_URL"
    assert resolved.config.models.vlm.api_key_env == "VLM_API_KEY"
    assert resolved.config.models.embedding.model_env == "OAM_EMBEDDING_MODEL"
    assert "thinking" not in resolved.config.models.model_dump()["vlm"]
    assert resolved.config.models.required_unbound_roles() == set()
    assert len(resolved.sha256) == 64


@pytest.mark.parametrize("capabilities", [["text"], ["vision"]])
def test_extraction_binding_requires_text_and_vision_capabilities(capabilities) -> None:
    from pydantic import ValidationError

    from oam.config import ModelsConfig

    with pytest.raises(ValidationError, match="extraction.*requires capability"):
        ModelsConfig.model_validate(
            {
                "reasoning_effort": {"extraction": "low"},
                "vlm": {
                    "protocol": "openai_chat",
                    "model": "synthetic-vlm",
                    "endpoint_env": "MODEL_URL",
                    "capabilities": capabilities,
                },
            }
        )


def test_role_binding_requires_the_capability_consumed_by_the_role(tmp_path: Path) -> None:
    path = tmp_path / "oam.yaml"
    path.write_text(
        """
models:
  embedding:
      protocol: openai_compatible
      model: example-chat
      model_env: null
      endpoint_env: OAM_SHARED_URL
      api_key_env: OAM_SHARED_KEY
      capabilities: [text]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="embedding.*embeddings"):
        load_config(path)


def test_optional_reranker_and_planner_may_be_unbound(tmp_path: Path) -> None:
    path = tmp_path / "oam.yaml"
    path.write_text("{}\n", encoding="utf-8")

    resolved = load_config(path)

    assert resolved.config.models.reranker is None
    assert resolved.config.models.reasoning_effort.planner is None


@pytest.mark.parametrize("mode", ["dev", "api_key", "trusted"])
def test_first_release_auth_mode_is_selected_from_operator_config(
    tmp_path: Path, mode: str
) -> None:
    path = tmp_path / "oam.yaml"
    path.write_text(f"auth:\n  mode: {mode}\n", encoding="utf-8")

    assert load_config(path).config.auth.mode == mode


@pytest.mark.parametrize("mode", ["oidc", "ldap", "unknown"])
def test_unimplemented_auth_modes_fail_configuration(tmp_path: Path, mode: str) -> None:
    path = tmp_path / "oam.yaml"
    path.write_text(f"auth:\n  mode: {mode}\n", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_config(path)


def test_unknown_configuration_keys_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "oam.yaml"
    path.write_text("models:\n  provider: hidden-default\n", encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_config(path)


def test_canonical_config_has_one_owner_for_database_auth_and_work_limits() -> None:
    config = load_config(REPOSITORY_ROOT / "configs" / "oam.yaml").config

    assert config.database.migration_dsn_env == "OAM_MIGRATION_DSN"
    assert config.database.api_dsn_env == "OAM_API_DSN"
    assert config.database.worker_queue_dsn_env == "OAM_WORKER_QUEUE_DSN"
    assert config.database.worker_content_dsn_env == "OAM_WORKER_CONTENT_DSN"
    assert config.database.api_pool_max == 10
    assert config.database.worker_pool_max == 10
    assert config.database.pool_min_size == 1
    assert config.database.indexing_concurrency == 4
    assert config.auth.cache_ttl_seconds == 30
    assert config.auth.cache_max_entries == 4096
    assert config.ingest.max_bytes == 128 * 1024
    assert config.ingest.max_messages == 128
    assert config.queue.max_pending_per_user == 1000
    assert config.provider.llm_concurrency == 32
    assert config.provider.embedding_concurrency == 8
    assert config.provider.embedding_search_reserved == 0
    assert config.user.concurrency == 2


def test_canonical_config_freezes_every_shared_runtime_limit() -> None:
    config = load_config(REPOSITORY_ROOT / "configs" / "oam.yaml").config

    assert (
        config.chunk.ingestion.target_estimated_tokens,
        config.chunk.ingestion.max_estimated_tokens,
    ) == (
        2048,
        4096,
    )
    assert config.chunk.ingestion.extractable_overlap_estimated_tokens == 0
    assert config.chunk.ingestion.previous_context_estimated_tokens == 128
    assert (config.extraction.max_input_tokens, config.extraction.max_output_tokens) == (
        8192,
        64000,
    )
    assert config.extraction.group_max_estimated_output_tokens == 4096
    assert config.extraction.max_facts_per_group == 64
    index_limits = (
        config.index.semantic_link_min_similarity,
        config.index.semantic_link_max_neighbors,
    )
    assert index_limits == (
        0.7,
        20,
    )
    assert config.search.mid_arm_weights.model_dump() == {
        "lexical": 1.0,
        "dense": 1.0,
        "graph": 0.1,
        "temporal": 1.0,
    }
    assert config.search.graph_seed_min_similarity == 0.3
    assert config.search.graph_seed_max == 12
    assert config.search.mid.source_seed_results == 36
    assert config.search.relation_hops == 2
    assert config.search.relation_candidates == 32
    assert config.search.entity_candidates_per_entity == 200
    assert config.search.ann_iterative_scan == "strict_order"
    assert config.search.ann_scan_max_tuples == 20_000
    assert (
        config.search.mid.channel_candidates,
        config.search.mid.fused_candidates,
        config.search.mid.source_seed_results,
        config.search.mid.max_results,
    ) == (64, 128, 36, 96)
    assert (config.context.default_tokens, config.context.max_tokens) == (2048, 18000)
    assert config.context.source_seed_tokens == 8192
    assert config.context.source_detail_budget_fraction == 0.4
    assert config.context.tokenizer == "oam-unicode-quarter-v1"
    assert (config.chunk.recall.default_tokens, config.chunk.recall.max_tokens) == (8192, 8192)
    assert config.profile.max_content_bytes == 16384
    assert config.profile.max_output_tokens == 2048
    assert config.profile.attempt_timeout_seconds == 180
    assert (config.planner.max_calls, config.planner.max_context_tokens) == (1, 2048)
    assert config.request.batch_deadline_seconds == 10
    assert config.request.delegated_deadline_seconds == 10
    assert config.model.attempt_timeout_seconds == 60
    assert config.retry.max_attempts == 3
    assert (
        config.lease.seconds,
        config.lease.heartbeat_seconds,
        config.lease.shutdown_seconds,
    ) == (90, 20, 90)
    assert (config.trace.content_retention_days, config.trace.metadata_retention_days) == (7, 30)
    assert config.evidence.default_retention_days == 365
    assert config.audit.retention_days == 90
    assert config.export.retention_hours == 24
    assert config.backup.retention_days == 30


def test_config_identity_is_stable_across_fresh_process_hash_seeds(tmp_path: Path) -> None:
    path = tmp_path / "oam.yaml"
    path.write_text(
        """
models:
  vlm:
      protocol: openai_chat
      model: example-model
      model_env: null
      endpoint_env: OAM_SHARED_URL
      capabilities: [vision, text]
""",
        encoding="utf-8",
    )
    command = [
        sys.executable,
        "-c",
        "from oam.config import load_config; import sys; print(load_config(sys.argv[1]).sha256)",
        str(path),
    ]

    identities = {
        subprocess.check_output(
            command,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            text=True,
        ).strip()
        for seed in (1, 2, 3)
    }

    assert len(identities) == 1


def test_partial_config_inherits_canonical_yaml_in_a_fresh_process(tmp_path: Path) -> None:
    base = yaml.safe_load((REPOSITORY_ROOT / "configs" / "oam.yaml").read_text())
    base["resource"]["max_bytes"] = 12345
    base["search"]["fast"]["fused_candidates"] = 128
    base["chunk"]["ingestion"]["target_estimated_tokens"] = 37
    base["chunk"]["recall"]["default_tokens"] = 101
    base["index"]["insight_consolidation_delay_seconds"] = 172800
    config_directory = tmp_path / "configs"
    config_directory.mkdir()
    (config_directory / "oam.yaml").write_text(yaml.safe_dump(base))
    override = tmp_path / "operator.yaml"
    override.write_text(
        "search:\n  fast:\n    max_results: 19\nchunk:\n  recall:\n    max_tokens: 203\n"
    )

    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "from oam.config import load_config; import sys; "
            "c = load_config(sys.argv[1]).config; "
            "print(c.resource.max_bytes, c.search.fast.fused_candidates, "
            "c.search.fast.max_results, c.chunk.ingestion.target_estimated_tokens, "
            "c.chunk.recall.default_tokens, c.chunk.recall.max_tokens, "
            "c.index.insight_consolidation_delay_seconds)",
            str(override),
        ],
        cwd=tmp_path,
        text=True,
    )

    assert output.strip() == "12345 128 19 37 101 203 172800"


def test_missing_canonical_numeric_limit_is_not_filled_by_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from oam import paths

    base = yaml.safe_load((REPOSITORY_ROOT / "configs" / "oam.yaml").read_text())
    del base["resource"]["max_bytes"]
    canonical = tmp_path / "canonical.yaml"
    canonical.write_text(yaml.safe_dump(base))
    monkeypatch.setattr(paths, "default_config_path", lambda: canonical)

    with pytest.raises(ConfigurationError, match="resource.max_bytes"):
        load_config(canonical)


def test_canonical_config_enables_l2_and_profile_model_roles() -> None:
    config = load_config(REPOSITORY_ROOT / "configs" / "oam.yaml").config

    assert config.index.insights_enabled is True
    assert config.profile.enabled is True
    assert {"insights", "profile"} <= config.required_model_roles()
    assert config.required_unbound_roles() == set()


def test_operator_config_can_disable_l2_and_profile_model_roles(tmp_path: Path) -> None:
    path = tmp_path / "oam.yaml"
    path.write_text(
        "index:\n  insights_enabled: false\nprofile:\n  enabled: false\n"
        "models:\n  reasoning_effort:\n    insights: null\n    profile: null\n"
    )

    config = load_config(path).config

    assert config.index.insights_enabled is False
    assert config.profile.enabled is False
    assert config.required_model_roles() == {"abstract", "embedding", "extraction"}
    assert config.required_unbound_roles() == set()


@pytest.mark.parametrize("delay_seconds", [0, -1])
def test_insight_consolidation_rejects_nonpositive_delay(
    tmp_path: Path, delay_seconds: int
) -> None:
    path = tmp_path / "oam.yaml"
    path.write_text(f"index:\n  insight_consolidation_delay_seconds: {delay_seconds}\n")

    with pytest.raises(ConfigurationError, match="greater than 0"):
        load_config(path)


@pytest.mark.parametrize(
    ("insights_enabled", "profile_enabled", "expected_optional_roles"),
    [
        (False, False, set()),
        (True, False, {"insights"}),
        (False, True, {"profile"}),
        (True, True, {"insights", "profile"}),
    ],
)
def test_l2_and_profile_switches_independently_control_required_model_roles(
    insights_enabled: bool, profile_enabled: bool, expected_optional_roles: set[str]
) -> None:
    base = load_config(REPOSITORY_ROOT / "configs" / "oam.yaml").config
    config = base.model_copy(
        update={
            "index": base.index.model_copy(update={"insights_enabled": insights_enabled}),
            "profile": base.profile.model_copy(update={"enabled": profile_enabled}),
        }
    )

    assert (
        config.required_model_roles()
        == {
            "abstract",
            "embedding",
            "extraction",
        }
        | expected_optional_roles
    )


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ("pool_min_size: 11", "pool_min_size cannot exceed api_pool_max"),
        ("pool_min_size: 2\n  worker_pool_max: 1", "pool_min_size cannot exceed worker_pool_max"),
    ],
)
def test_database_pool_sizes_are_validated_at_config_load(tmp_path, settings, message):
    path = tmp_path / "invalid-pool.yaml"
    path.write_text("database:\n  " + settings + "\n")
    with pytest.raises(ConfigurationError, match=message):
        load_config(path)


def test_pool_acquire_timeout_rejects_non_finite_value(tmp_path):
    path = tmp_path / "invalid-pool-timeout.yaml"
    path.write_text("database:\n  pool_acquire_timeout_seconds: .inf\n")
    with pytest.raises(ConfigurationError, match="pool_acquire_timeout_seconds"):
        load_config(path)
