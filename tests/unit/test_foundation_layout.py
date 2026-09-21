from importlib.util import find_spec
from pathlib import Path


def test_persistence_foundation_modules_and_initial_migration_exist() -> None:
    root = Path(__file__).parents[2]

    assert (root / "migrations" / "0001_initial.sql").is_file()
    for module_name in (
        "oam.db.context",
        "oam.db.migrations",
        "oam.db.sources",
        "oam.auth.credentials",
        "oam.auth.bootstrap",
        "oam.auth.repository",
        "oam.operations.state",
        "oam.operations.publication",
        "oam.operations.repository",
        "oam.workers.claims",
    ):
        assert find_spec(module_name) is not None, module_name
