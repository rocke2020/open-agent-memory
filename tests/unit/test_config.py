import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from oam.config import ConfigurationError, load_config

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_canonical_config_freezes_v001_trial_limits_without_provider_defaults() -> None:
    resolved = load_config(REPOSITORY_ROOT / "configs" / "oam.yaml")

    assert resolved.config.contract_version == "0.0.1"
    assert resolved.config.resource.max_bytes == 20 * 1024 * 1024
    assert resolved.config.resource.max_pdf_pages == 20
    assert resolved.config.resource.max_images == 20
    assert resolved.config.resource.max_pixels_per_image == 40_000_000
    assert resolved.config.resource.max_total_pixels == 100_000_000
    assert resolved.config.scope.max_reports == 100
    assert resolved.config.scope.max_chats == 1_000
    assert resolved.config.api.max_concurrent_requests == 10
    assert resolved.config.models.bindings == {}
    assert resolved.config.models.required_unbound_roles() == {
        "abstract",
        "document_vision",
        "embedding",
        "extraction",
        "insights",
        "profile",
    }
    assert len(resolved.sha256) == 64


def test_role_binding_requires_the_capability_consumed_by_the_role(tmp_path: Path) -> None:
    path = tmp_path / "oam.yaml"
    path.write_text(
        """
models:
  roles:
    embedding: shared
  bindings:
    shared:
      protocol: openai_compatible
      model: example-chat
      endpoint_env: OAM_SHARED_URL
      api_key_env: OAM_SHARED_KEY
      capabilities: [chat]
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="embedding.*embeddings"):
        load_config(path)


def test_optional_reranker_and_planner_may_be_unbound(tmp_path: Path) -> None:
    path = tmp_path / "oam.yaml"
    path.write_text("{}\n", encoding="utf-8")

    resolved = load_config(path)

    assert resolved.config.models.roles.reranker is None
    assert resolved.config.models.roles.planner is None


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
    assert config.database.worker_pool_max == 6
    assert config.auth.cache_ttl_seconds == 30
    assert config.auth.cache_max_entries == 4096
    assert config.ingest.max_bytes == 128 * 1024
    assert config.ingest.max_messages == 128
    assert config.queue.max_pending_per_scope == 1000
    assert config.provider.global_concurrency == 4
    assert config.scope.concurrency == 2


def test_canonical_config_freezes_every_shared_runtime_limit() -> None:
    config = load_config(REPOSITORY_ROOT / "configs" / "oam.yaml").config

    assert (config.chunk.target_estimated_tokens, config.chunk.max_estimated_tokens) == (
        2048,
        4096,
    )
    assert config.chunk.extractable_overlap_estimated_tokens == 0
    assert config.chunk.previous_context_estimated_tokens == 128
    assert (config.extraction.max_input_tokens, config.extraction.max_output_tokens) == (
        8192,
        4096,
    )
    assert config.extraction.max_facts_per_group == 64
    index_limits = (
        config.index.semantic_link_min_similarity,
        config.index.semantic_link_max_neighbors,
    )
    assert index_limits == (
        0.7,
        20,
    )
    assert config.search.channel_weights == "equal"
    assert config.search.graph_seed_min_similarity == 0.3
    assert config.search.graph_seed_max == 20
    assert config.search.relation_hops == 2
    assert config.search.relation_candidates == 32
    assert config.search.entity_candidates_per_entity == 200
    assert config.search.entity_score_scale == 0.5
    assert config.search.ann_iterative_scan == "strict_order"
    assert config.search.ann_scan_max_tuples == 20_000
    assert (config.context.default_tokens, config.context.max_tokens) == (2048, 8192)
    assert config.context.tokenizer == "o200k_base"
    assert (config.chunks.default_tokens, config.chunks.max_tokens) == (8192, 8192)
    assert (
        config.profile.main_tokens,
        config.profile.preferences_tokens,
        config.profile.experiences_tokens,
    ) == (256, 512, 512)
    assert config.profile.max_fields_per_section == 32
    assert config.profile.max_value_bytes == 2048
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
  roles:
    extraction: shared
  bindings:
    shared:
      protocol: openai_compatible
      model: example-model
      endpoint_env: OAM_SHARED_URL
      capabilities: [vision, embeddings, chat]
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
    base["search"]["fast"]["fused_candidates"] = 80
    config_directory = tmp_path / "configs"
    config_directory.mkdir()
    (config_directory / "oam.yaml").write_text(yaml.safe_dump(base))
    override = tmp_path / "operator.yaml"
    override.write_text("search:\n  fast:\n    max_results: 19\n")

    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "from oam.config import load_config; import sys; "
            "c = load_config(sys.argv[1]).config; "
            "print(c.resource.max_bytes, c.search.fast.fused_candidates, "
            "c.search.fast.max_results)",
            str(override),
        ],
        cwd=tmp_path,
        text=True,
    )

    assert output.strip() == "12345 80 19"


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
