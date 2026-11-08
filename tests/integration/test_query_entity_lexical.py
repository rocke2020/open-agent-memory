"""Stored entity hints improve one lexical arm without replacing query relevance."""

import time
from pathlib import Path

import pytest
from test_core_http import core_database as core_database
from test_recall_repository import FixedModels, TransactionPool, add_fact, add_insight
from test_recall_repository import recall_database as recall_database

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.indexing.entities import EntityRepository
from oam.retrieval.repository import RecallRepository
from oam.retrieval.service import RecallService

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("ranking", ["per_term", "whole_query"])
@pytest.mark.parametrize("query", ["Nova Research warranty", "NR warranty", "IT", "After"])
def test_insight_candidate_pushdown_preserves_fact_backed_entity_names(
    recall_database, ranking, query
):
    from oam.indexing.consolidation import InsightLookup

    class UnfilteredLookup(InsightLookup):
        @property
        def lexical_candidate_predicate(self):
            return "true"

    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={
                    "normal": config.search.normal.model_copy(update={"lexical_ranking": ranking})
                }
            )
        }
    )
    alias = query if query in {"IT", "After"} else "NR"
    name = "Information Technology" if alias in {"IT", "After"} else "Nova Research"
    definition = f"{name} is also called {alias}."
    target = f"{name} approved the laptop warranty."
    entities = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    add_fact(connection, authority, "definition", definition)
    entities.publish_fact(
        "definition",
        (name,),
        definition,
        aliases=({"canonical_name": name, "alias": alias, "quote": definition},),
    )
    add_fact(connection, authority, "target", target)
    entities.publish_fact(
        "target",
        (name,),
        target,
        references=({"name": name, "reference_id": "definition", "quote": target},),
    )
    add_insight(connection, authority, "insight", ("definition", "target"))
    text = f"{name} ({alias}) approved the laptop warranty."
    connection.execute(
        "UPDATE search_units SET content_text=%s,lexical_text=%s WHERE unit_id='insight'",
        (text, text),
    )
    connection.execute("UPDATE insights SET synthesis_text=%s WHERE insight_id='insight'", (text,))
    current = InsightLookup(
        connection, authority, config, generation_id="generation", deadline=time.monotonic() + 10
    )
    original = UnfilteredLookup(
        connection, authority, config, generation_id="generation", deadline=time.monotonic() + 10
    )
    names = current._query_entity_names(query)
    assert names == original._query_entity_names(query)
    assert names
    assert current.lexical(query, 8) == original.lexical(query, 8)
    assert [row["unit_id"] for row in current.lexical(query, 8)] == ["insight"]
    connection.execute("UPDATE evidence SET learning_eligible=false WHERE evidence_id='definition'")
    assert current.lexical(query, 8) == original.lexical(query, 8) == []


def alias_history(connection, authority):
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    entities = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    definition = "Nova Research is also called NR."
    add_fact(connection, authority, "definition", definition, with_original=True)
    identity = entities.publish_fact(
        "definition",
        ("Nova Research",),
        definition,
        aliases=({"canonical_name": "Nova Research", "alias": "NR", "quote": definition},),
    )[0]
    for node, text in (
        ("z_target", "Nova Research battery warranty."),
        ("picnic", "Nova Research organized a picnic."),
    ):
        add_fact(connection, authority, node, text, with_original=True)
        entities.publish_fact(
            node,
            ("Nova Research",),
            text,
            references=({"name": "Nova Research", "reference_id": "definition", "quote": text},),
        )
    add_fact(connection, authority, "a_other", "Battery warranty.", with_original=True)
    return config, identity


def lexical(connection, authority, config, query, mode="fast", limit=4):
    repository = RecallRepository(
        connection,
        authority,
        config,
        deadline=time.monotonic() + 5,
        request=SearchRequest(query=query, recall_level=mode),
    )
    return repository.lexical(query, limit)


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_supported_alias_boosts_relevant_fact_without_promoting_unrelated_entity_fact(
    recall_database, mode
):
    connection, authority = recall_database
    config, _ = alias_history(connection, authority)
    connection.execute("SET LOCAL ROLE oam_api")

    rows = lexical(connection, authority, config, "NR battery warranty", mode)

    assert rows[0]["unit_id"] == "z_target"
    assert rows[0]["entity_match"] is True
    assert rows[0]["strict_match"] is False
    assert next(row for row in rows if row["unit_id"] == "a_other")["entity_match"] is False
    assert [row["unit_id"] for row in rows].index("picnic") > 1


def test_entity_hint_does_not_override_stronger_full_query_relevance(recall_database):
    connection, authority = recall_database
    config, _ = alias_history(connection, authority)
    add_fact(connection, authority, "exact", "NR battery warranty policy.")
    connection.execute("SET LOCAL ROLE oam_api")

    rows = lexical(connection, authority, config, "NR battery warranty policy")

    assert rows[0]["unit_id"] == "exact"
    assert rows[0]["strict_match"] is True


def test_revoked_alias_support_no_longer_grants_entity_priority(recall_database):
    connection, authority = recall_database
    config, _ = alias_history(connection, authority)
    connection.execute(
        "UPDATE evidence SET learning_eligible=false "
        "WHERE tenant_id=%s AND evidence_id='definition'",
        (authority.tenant_id,),
    )
    connection.execute("SET LOCAL ROLE oam_api")

    rows = lexical(connection, authority, config, "NR battery warranty")

    assert rows[0]["unit_id"] == "a_other"
    assert not any(row.get("entity_match") for row in rows)


def test_same_spelling_keeps_both_supported_entity_identities(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    entities = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    for node, text in (("colleague", "Alex teaches physics."), ("coach", "Alex teaches swimming.")):
        add_fact(connection, authority, node, text)
        entities.publish_fact(node, ("Alex",), text)
    connection.execute("SET LOCAL ROLE oam_api")

    rows = lexical(connection, authority, config, "Alex teaches", limit=2)

    assert {row["unit_id"] for row in rows if row.get("entity_match")} == {"colleague", "coach"}


def test_entity_match_requires_whole_name_not_stemming_equivalence(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    text = "Lab provides a warranty."
    add_fact(connection, authority, "target", text)
    EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    ).publish_fact("target", ("Lab",), text)
    connection.execute("SET LOCAL ROLE oam_api")

    rows = lexical(connection, authority, config, "Labs warranty")

    assert not any(row.get("entity_match") for row in rows)


def test_multiword_stopword_name_is_recognized_inside_query(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    text = "The Who announced tour dates."
    add_fact(connection, authority, "band", text)
    EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    ).publish_fact("band", ("The Who",), text)
    connection.execute("SET LOCAL ROLE oam_api")

    rows = lexical(connection, authority, config, "The Who tour dates")

    assert rows[0]["unit_id"] == "band"
    assert rows[0].get("entity_match") is True


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize("alias", ["IT", "After"])
def test_stopword_alias_alone_keeps_entity_membership_candidates(recall_database, mode, alias):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    entities = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    definition = f"Information Technology is also called {alias}."
    add_fact(connection, authority, "definition", definition)
    entities.publish_fact(
        "definition",
        ("Information Technology",),
        definition,
        aliases=(
            {"canonical_name": "Information Technology", "alias": alias, "quote": definition},
        ),
    )
    text = "Information Technology approved the laptop warranty."
    add_fact(connection, authority, "target", text)
    entities.publish_fact(
        "target",
        ("Information Technology",),
        text,
        references=(
            {"name": "Information Technology", "reference_id": "definition", "quote": text},
        ),
    )
    connection.execute("SET LOCAL ROLE oam_api")

    rows = lexical(connection, authority, config, alias, mode)

    assert "target" in {row["unit_id"] for row in rows if row.get("entity_match")}


def test_entity_fallback_keeps_original_query_modifiers(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    text = "Information Technology is also called IT."
    add_fact(connection, authority, "definition", text)
    EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    ).publish_fact(
        "definition",
        ("Information Technology",),
        text,
        aliases=({"canonical_name": "Information Technology", "alias": "IT", "quote": text},),
    )
    connection.execute("SET LOCAL ROLE oam_api")

    rows = lexical(connection, authority, config, "IT before")

    assert not any(row["strict_match"] for row in rows)


@pytest.mark.parametrize("name", ["Nova Research", "Will", "星河工作室", "Acme (Labs)", "Straße"])
def test_entity_at_end_of_long_query_survives_lexical_term_limit(recall_database, name):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    text = f"{name} supplies a battery warranty."
    add_fact(connection, authority, "entity_fact", text)
    EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    ).publish_fact("entity_fact", (name,), text)
    add_fact(connection, authority, "other", "filler0 filler1 filler2")
    query = " ".join(f"filler{index}" for index in range(70)) + f" warranty {name}"
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(
        connection,
        authority,
        config,
        deadline=time.monotonic() + 5,
        request=SearchRequest(query=query, recall_level="fast"),
    )

    rows = repository.lexical(query, 2)

    assert "entity_fact" in {row["unit_id"] for row in rows}


def test_fast_context_retains_query_entity_fact_outside_dense_pool(recall_database, caplog):
    connection, authority = recall_database
    config, _ = alias_history(connection, authority)
    add_fact(connection, authority, "b_other", "Unrelated semantic hit.", with_original=True)
    connection.execute(
        "UPDATE search_units SET embedding=%s::vector WHERE tenant_id=%s "
        "AND unit_id IN ('z_target','definition','picnic')",
        (str([-1.0] + [0.0] * (config.index.embedding_dimensions - 1)), authority.tenant_id),
    )
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={"fast": config.search.fast.model_copy(update={"channel_candidates": 2})}
            )
        }
    )
    service = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="generation",
    )
    connection.execute("SET LOCAL ROLE oam_api")
    with caplog.at_level("INFO", logger="oam.retrieval.service"):
        result = service.search(
            authority, SearchRequest(query="NR battery warranty", recall_level="fast")
        )

    assert "z_target" in {item["unit_id"] for item in result["items"]}
    assert "Nova Research battery warranty." in result["context"]
    assert result["diagnostics"]["local_context"]["query_entity_matches"] > 0
    assert result["diagnostics"]["generative_calls"] == 0
    assert "entity_matches=" in caplog.text
