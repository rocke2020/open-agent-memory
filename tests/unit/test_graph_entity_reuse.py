"""Each stored entity identity supplies at most one shared-entity signal."""

from pathlib import Path

from oam.config import load_config
from oam.retrieval.repository import RecallRepository


def test_repeated_alias_paths_do_not_outvote_two_distinct_shared_entity_ids():
    repository = object.__new__(RecallRepository)
    repository.config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository.request = None
    repository._has_insights = False
    repository._eligible = lambda **kwargs: "WITH filtered AS (SELECT 1) "
    graph_rows = []
    for seed, target, entity, names in (
        ("seed_a", "one_entity", "entity_1", ["full_name", "abbreviation"]),
        ("seed_b", "one_entity", "entity_1", ["abbreviation", "abbreviation"]),
        ("seed_a", "two_entities", "entity_2", ["name_2", "name_2"]),
        ("seed_b", "two_entities", "entity_3", ["name_3", "name_3"]),
    ):
        graph_rows.append(
            {
                "source": "entity",
                "seed_id": seed,
                "unit_id": target,
                "entity_key": entity,
                "entity_evidence": {"name_ids": names, "alias_witnesses": []},
            }
        )
    repository._execute = lambda sql, parameters: graph_rows

    ranked, paths = repository.ordinary(("seed_a", "seed_b"), (1.0,))

    assert ranked == ("two_entities", "one_entity")
    assert {path["entity"] for path in paths["one_entity"]} == {"entity_1"}
    assert len(paths["one_entity"]) == 2
