from pathlib import Path

from oam.paths import default_config_path, default_migration_directory

REPOSITORY_ROOT = Path(__file__).parents[2]


def test_source_checkout_resolves_canonical_packaged_runtime_assets() -> None:
    assert default_config_path() == REPOSITORY_ROOT / "configs" / "oam.yaml"
    assert default_migration_directory() == REPOSITORY_ROOT / "migrations"
