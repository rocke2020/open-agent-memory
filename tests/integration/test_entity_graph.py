"""Stored identity graph paths require currently eligible name witnesses."""

import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_recall_repository import add_fact
from test_recall_repository import recall_database as _recall_database

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.indexing.entities import EntityRepository
from oam.models.runtime import Usage
from oam.retrieval.context import render_item
from oam.retrieval.repository import RecallRepository
from oam.retrieval.service import RecallService

pytestmark = pytest.mark.integration
recall_database = _recall_database


def alias_history(connection, authority, *, definition_quote=None):
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    texts = {
        "definition": definition_quote or "Nova Research, also called NR, opened a laboratory.",
        "seed": "Nova Research, the same organization I mentioned, moved its laboratory.",
        "continuation": "I joined NR in June, the organization I mentioned before.",
    }
    for memory_id, text in texts.items():
        add_fact(connection, authority, memory_id, text)
    entities = EntityRepository(
        connection,
        config,
        generation_id="generation",
        key=(authority.tenant_id, authority.user_id),
    )
    entity_ids = entities.publish_fact(
        "definition",
        ("Nova Research",),
        texts["definition"],
        aliases=(
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": definition_quote or "Nova Research, also called NR",
            },
        ),
    )
    entities.publish_fact(
        "seed",
        ("Nova Research",),
        texts["seed"],
        references=(
            {"name": "Nova Research", "reference_id": "definition", "quote": texts["seed"]},
        ),
    )
    entities.publish_fact(
        "continuation",
        ("NR",),
        texts["continuation"],
        references=({"name": "NR", "reference_id": "definition", "quote": texts["continuation"]},),
    )
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 10)
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    return repository, vector, entity_ids[0]


def test_alias_graph_reaches_unchanged_target_and_carries_citable_witness(recall_database):
    connection, authority = recall_database
    repository, vector, entity_id = alias_history(connection, authority)

    ranked, paths = repository.ordinary(("seed",), vector)

    assert "continuation" in ranked
    path = paths["continuation"][0]
    assert path["entity"] == entity_id
    assert len(path["name_ids"]) == 2
    witness = path["alias_witnesses"][0]
    assert witness["quote"] == "Nova Research, also called NR"
    assert witness["citation"]["source_id"] == "definition"
    assert witness["citation"]["source_version"] == 1
    assert witness["citation"]["span"]["quote"] == witness["quote"]
    row = repository.hydrate(("continuation",))[0]
    assert row["text"] == "I joined NR in June, the organization I mentioned before."


def test_revoked_alias_invalidates_path_but_keeps_direct_target_evidence(recall_database):
    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    _, paths = repository.ordinary(("seed",), vector)
    selected = {"continuation": paths["continuation"]}
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND user_id=%s "
        "AND evidence_id='definition'",
        (authority.tenant_id, authority.user_id),
    )

    assert repository.valid_paths(selected, vector) == {"continuation": []}
    assert "continuation" not in repository.ordinary(("seed",), vector)[0]
    assert (
        repository.hydrate(("continuation",))[0]["text"]
        == "I joined NR in June, the organization I mentioned before."
    )


def test_independent_alias_support_substitutes_witness_for_same_binding(recall_database):
    connection, authority = recall_database
    repository, vector, entity_id = alias_history(connection, authority)
    text = "Nova Research is also known as NR and serves my research group."
    add_fact(connection, authority, "independent", text)
    entities = EntityRepository(
        connection,
        repository.config,
        generation_id="generation",
        key=(authority.tenant_id, authority.user_id),
    )
    entities.publish_fact(
        "independent",
        ("Nova Research",),
        text,
        aliases=(
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": "Nova Research is also known as NR",
            },
        ),
        references=({"name": "Nova Research", "reference_id": "seed", "quote": text},),
    )
    _, paths = repository.ordinary(("seed",), vector)
    path = paths["continuation"][0]
    chosen = path["alias_witnesses"][0]
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND user_id=%s "
        "AND evidence_id=%s",
        (authority.tenant_id, authority.user_id, chosen["citation"]["source_id"]),
    )

    refreshed = repository.valid_paths({"continuation": [path]}, vector)["continuation"]

    assert len(refreshed) == 1
    replacement = refreshed[0]["alias_witnesses"][0]
    assert replacement["support_id"] != chosen["support_id"]
    assert replacement["name_id"] == chosen["name_id"]
    assert replacement["entity_id"] == entity_id
    assert replacement["citation"]["source_id"] in {"definition", "independent"}


def test_alias_witness_reaches_rendered_context_with_existing_budget_cost(recall_database):
    from oam.retrieval.context import pack_ranked
    from oam.tokens import estimate_units, estimated_tokens

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    _, paths = repository.ordinary(("seed",), vector)
    item = repository.hydrate(("continuation",))[0]
    budget_without_alias = estimated_tokens(estimate_units(render_item(item))) + 1
    item["paths"] = paths["continuation"]

    rendered = render_item(item)

    assert "Nova Research, also called NR" in rendered
    assert "definition" in rendered
    assert "I joined NR in June, the organization I mentioned before." in rendered
    assert pack_ranked([item], budget_without_alias, 1) == []


def test_partial_revalidation_keeps_other_graph_paths_and_independent_direct_hit(recall_database):
    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    config = repository.config
    entities = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    for memory_id, text in (
        ("other_seed", "Vega opened a workshop."),
        ("healthy", "Vega, the project I mentioned, hired me."),
    ):
        add_fact(connection, authority, memory_id, text)
        entities.publish_fact(
            memory_id,
            ("Vega",),
            text,
            references=()
            if memory_id == "other_seed"
            else ({"name": "Vega", "reference_id": "other_seed", "quote": text},),
        )

    class ChangedAliasRepository(RecallRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "continuation", "score": 1.0}]

        def anchors(self, query, limit):
            return []

        def dense(self, query_vector, limit):
            return [
                {"unit_id": node, "kind": "fact", "score": 1.0} for node in ("seed", "other_seed")
            ]

        def ordinary(self, seeds, query_vector, *, include_semantic=True):
            result = super().ordinary(seeds, query_vector, include_semantic=include_semantic)
            connection.execute(
                "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND user_id=%s "
                "AND evidence_id='definition'",
                (authority.tenant_id, authority.user_id),
            )
            return result

        def related_user_fact(self, unit_ids):
            return None

    service = recall_service(connection, config, vector, ChangedAliasRepository)

    result = service.search(
        authority,
        SearchRequest(query="research projects", recall_level="normal", allow_partial=True),
    )

    by_id = {item["unit_id"]: item for item in result["items"]}
    assert "healthy" in by_id
    assert "continuation" in by_id
    assert by_id["continuation"]["paths"] == []
    assert by_id["healthy"]["paths"][0]["relation"] == "entity"
    assert "graph" in result["diagnostics"]["executed_arms"]
    assert "graph_revalidation" in result["diagnostics"]["degraded"]


def recall_service(connection, config, vector, repository_type):
    class Embeddings:
        def embed(self, texts, **kwargs):
            return SimpleNamespace(vectors=(vector,), usage=Usage(coverage="unavailable"))

    class ScopedService(RecallService):
        @contextmanager
        def _repository(self, authority, request, deadline):
            yield repository_type(connection, authority, config, deadline=deadline, request=request)

    return ScopedService(config, None, Embeddings(), generation_id="generation")


def test_replacement_witness_preserves_same_graph_path_under_strict_recall(recall_database):
    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    text = "Nova Research is also known as NR and runs our research group."
    add_fact(connection, authority, "independent", text)
    entities = EntityRepository(
        connection,
        repository.config,
        generation_id="generation",
        key=(authority.tenant_id, authority.user_id),
    )
    entities.publish_fact(
        "independent",
        ("Nova Research",),
        text,
        aliases=({"canonical_name": "Nova Research", "alias": "NR", "quote": text},),
        references=({"name": "Nova Research", "reference_id": "seed", "quote": text},),
    )
    replaced = []

    class ReplacedSupportRepository(RecallRepository):
        def lexical(self, query, limit):
            return []

        def anchors(self, query, limit):
            return []

        def dense(self, query_vector, limit):
            return [{"unit_id": "seed", "kind": "fact", "score": 1.0}]

        def ordinary(self, seeds, query_vector, *, include_semantic=True):
            _, paths = super().ordinary(seeds, query_vector, include_semantic=include_semantic)
            selected = paths["continuation"]
            chosen = selected[0]["alias_witnesses"][0]
            replaced.append(chosen["support_id"])
            connection.execute(
                "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND user_id=%s "
                "AND evidence_id=%s",
                (authority.tenant_id, authority.user_id, chosen["citation"]["source_id"]),
            )
            return ("continuation",), {"continuation": selected}

        def related_user_fact(self, unit_ids):
            return None

    service = recall_service(connection, repository.config, vector, ReplacedSupportRepository)

    result = service.search(
        authority, SearchRequest(query="research membership", recall_level="normal")
    )

    target = next(item for item in result["items"] if item["unit_id"] == "continuation")
    assert target["paths"][0]["alias_witnesses"][0]["support_id"] != replaced[0]
    assert result["diagnostics"]["degraded"] == []
    assert "Nova Research" in result["context"]


@pytest.mark.parametrize(
    "definition",
    [
        None,
        "Nova Research, also called NR, " + "maintained the documented research program; " * 100,
    ],
)
def test_alias_metadata_preserves_graph_vote_under_fact_text_budget(recall_database, definition):
    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority, definition_quote=definition)
    budget = 64

    class DirectTargetRepository(RecallRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "continuation", "score": 1.0}]

        def anchors(self, query, limit):
            return []

        def dense(self, query_vector, limit):
            return [{"unit_id": "seed", "kind": "fact", "score": 1.0}]

        def ordinary(self, seeds, query_vector, *, include_semantic=True):
            _, paths = super().ordinary(seeds, query_vector, include_semantic=include_semantic)
            return ("continuation",), {"continuation": paths["continuation"]}

        def related_user_fact(self, unit_ids):
            return None

    service = recall_service(connection, repository.config, vector, DirectTargetRepository)

    result = service.search(
        authority,
        SearchRequest(query="research membership", recall_level="normal", max_tokens=budget),
    )

    selected = next(item for item in result["items"] if item["unit_id"] == "continuation")
    assert selected["text"] == "I joined NR in June, the organization I mentioned before."
    assert selected["paths"][0]["alias_witnesses"]
    assert "Alias:" in result["context"]
    assert result["diagnostics"]["text_budgets"]["facts"]["used"] <= budget
    trace = connection.execute(
        "SELECT decisions FROM traces WHERE tenant_id=%s AND user_id=%s AND trace_id=%s",
        (authority.tenant_id, authority.user_id, result["trace_id"]),
    ).fetchone()[0]
    contribution = next(row for row in trace["fusion"] if row["unit_id"] == "continuation")
    assert {entry["arm"] for entry in contribution["contributions"]} == {"lexical", "graph"}
    assert "continuation" in trace["arms"]["graph"]


def test_entity_and_semantic_paths_survive_text_only_budget(recall_database, tmp_path):
    from oam.tokens import estimate_units, estimated_tokens

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    add_fact(connection, authority, "healthy", "The research team expanded its laboratory.")
    for target, weight in (("continuation", 0.2), ("healthy", 0.6)):
        connection.execute(
            "INSERT INTO unit_links(tenant_id,user_id,source_id,target_id,kind,weight) "
            "VALUES (%s,%s,'seed',%s,'semantic',%s)",
            (authority.tenant_id, authority.user_id, target, weight),
        )
    budget = max(
        64,
        estimated_tokens(estimate_units(render_item(repository.hydrate(("continuation",))[0]))) + 8,
    )

    class SemanticTargetRepository(RecallRepository):
        def lexical(self, query, limit):
            return [{"unit_id": "continuation", "score": 1.0}]

        def anchors(self, query, limit):
            return []

        def dense(self, query_vector, limit):
            return [{"unit_id": "seed", "kind": "fact", "score": 1.0}]

        def ordinary(self, seeds, query_vector, *, include_semantic=True):
            _, paths = super().ordinary(seeds, query_vector, include_semantic=include_semantic)
            return ("continuation", "healthy"), {
                name: paths[name] for name in ("continuation", "healthy")
            }

        def related_user_fact(self, unit_ids):
            return None

    path = tmp_path / "oam.yaml"
    path.write_text("search:\n  normal:\n    graph: full\n")
    service = recall_service(connection, load_config(path).config, vector, SemanticTargetRepository)

    result = service.search(
        authority,
        SearchRequest(query="research membership", recall_level="normal", max_tokens=budget),
    )

    target = next(item for item in result["items"] if item["unit_id"] == "continuation")
    assert {path["relation"] for path in target["paths"]} >= {"entity", "semantic"}
    trace = connection.execute(
        "SELECT decisions FROM traces WHERE tenant_id=%s AND user_id=%s AND trace_id=%s",
        (authority.tenant_id, authority.user_id, result["trace_id"]),
    ).fetchone()[0]
    assert set(trace["arms"]["graph"]) == {"healthy", "continuation"}


def test_same_alias_endpoints_keep_one_witness_until_definition_is_revoked(recall_database):
    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    text = "NR, the organization I mentioned earlier, opened another workshop."
    add_fact(connection, authority, "alias_seed", text)
    entities = EntityRepository(
        connection,
        repository.config,
        generation_id="generation",
        key=(authority.tenant_id, authority.user_id),
    )
    entities.publish_fact(
        "alias_seed",
        ("NR",),
        text,
        references=({"name": "NR", "reference_id": "definition", "quote": text},),
    )

    ranked, paths = repository.ordinary(("alias_seed",), vector)

    assert "continuation" in ranked
    path = paths["continuation"][0]
    assert len(set(path["name_ids"])) == 1
    assert len(path["alias_witnesses"]) == 1
    assert path["alias_witnesses"][0]["quote"] == "Nova Research, also called NR"
    current = repository.valid_paths({"continuation": [path]}, vector)
    assert len(current["continuation"][0]["alias_witnesses"]) == 1
    item = repository.hydrate(("continuation",))[0]
    item["paths"] = current["continuation"]
    assert render_item(item).count("Alias:") == 1
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND user_id=%s "
        "AND evidence_id='definition'",
        (authority.tenant_id, authority.user_id),
    )

    assert repository.valid_paths(current, vector) == {"continuation": []}
    assert "continuation" not in repository.ordinary(("alias_seed",), vector)[0]
    assert {row["unit_id"] for row in repository.hydrate(("alias_seed", "continuation"))} == {
        "alias_seed",
        "continuation",
    }
