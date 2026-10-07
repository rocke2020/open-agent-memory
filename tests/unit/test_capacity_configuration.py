from pathlib import Path

import pytest
from pydantic import ValidationError

from oam.config import OAMConfig, load_config
from oam.db.migrations import load_migrations

ROOT = Path(__file__).parents[2]


def test_canonical_capacity_limits_have_separate_scopes():
    config = load_config(ROOT / "configs/oam.yaml").config
    assert config.worker.embedded_enabled is True
    assert config.worker.max_tasks == 10
    assert config.database.indexing_concurrency == 4
    assert config.provider.llm_concurrency == 32
    assert config.provider.embedding_concurrency == 8
    assert config.provider.embedding_search_reserved == 0
    assert config.provider.rerank_concurrency == 4
    assert config.api.max_concurrent_requests == 10
    assert config.api.max_concurrent_writes == 6


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [("api", "max_concurrent_writes", 10), ("provider", "embedding_search_reserved", 8)],
)
def test_capacity_limits_leave_room_for_foreground_and_background(section, field, value):
    data = load_config(ROOT / "configs/oam.yaml").config.model_dump()
    data[section][field] = value
    with pytest.raises(ValidationError):
        OAMConfig.model_validate(data)


def test_repository_migration_sequence_can_initialize_a_fresh_database():
    migrations = load_migrations(ROOT / "migrations")
    assert len({migration.version for migration in migrations}) == len(migrations)
