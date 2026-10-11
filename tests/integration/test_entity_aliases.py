# ruff: noqa: F811
import json
from pathlib import Path

import psycopg
import pytest
from test_extraction_references import (  # noqa: F401
    ReferenceModels,
    core_database,
    reference_client,
    retain,
    run_source,
)
from test_recall_repository import (
    add_fact,
    recall_database,  # noqa: F401
)

from oam.config import load_config
from oam.indexing.entities import EntityRepository
from oam.models.runtime import EmbeddingResult, ModelResult, Usage

pytestmark = pytest.mark.integration


def test_reference_entity_projection_reuses_current_fact_support(recall_database):
    from test_entity_graph import alias_history

    connection, authority = recall_database
    recall, _, _ = alias_history(connection, authority)
    repository = EntityRepository(
        connection,
        recall.config,
        generation_id="generation",
        key=(authority.tenant_id, authority.user_id),
    )
    plans = []
    read = repository._rows

    def explain(query, parameters):
        plans.append(
            connection.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + query, parameters).fetchone()[0][
                0
            ]["Plan"]
        )
        return read(query, parameters)

    repository._rows = explain
    names = repository.reference_entities(["seed", "continuation"])
    assert {item["name"] for item in names["seed"]} == {"Nova Research", "NR"}
    assert names["seed"] == names["continuation"]

    def support_reads(node):
        own = node.get("Actual Loops", 0) if node.get("Relation Name") == "support" else 0
        return own + sum(support_reads(child) for child in node.get("Plans", ()))

    assert sum(support_reads(plan) for plan in plans) == 3


class AliasModels(ReferenceModels):
    def generate(self, role, **kwargs):
        if role != "extraction":
            return super().generate(role, **kwargs)
        envelope = json.loads(kwargs["user"])
        self.inputs.append(envelope)
        text = envelope["center"]["text"]
        fact = {"text": text, "quote": text}
        if "also called" in text:
            fact.update(
                entities=["Nova Research", "NR"],
                entity_aliases=[
                    {
                        "canonical_name": "Nova Research",
                        "alias": "NR",
                        "quote": "Nova Research, also called NR",
                    }
                ],
            )
        else:
            offered = envelope["reference_facts"][0]
            assert any(name["name"] == "NR" for name in offered["entities"])
            fact.update(
                entities=["NR"],
                entity_references=[{"name": "NR", "reference_id": offered["id"], "quote": text}],
            )
        return ModelResult(
            content=json.dumps({"outcome": "facts", "facts": [fact]}),
            usage=Usage(
                input_tokens=20,
                output_tokens=20,
                total_tokens=40,
                reasoning_tokens=0,
                coverage="complete",
            ),
        )


@pytest.mark.asyncio
async def test_aliases_reach_public_worker_and_cited_recall(reference_client):
    client, dsn, tenant, config, app = reference_client
    app.state.runtime.models = AliasModels()
    await retain(
        client, "alias-definition", ["Nova Research, also called NR, opened a laboratory."]
    )
    assert run_source(dsn, config, app.state.runtime.models)
    await retain(client, "alias-continuation", ["I joined NR in June."])
    assert run_source(dsn, config, app.state.runtime.models)
    response = await client.post(
        "/v1/recall",
        json={
            "user_id": "lin",
            "query": "When did I join Nova Research?",
            "recall_level": "mid",
            "max_context_tokens": 2048,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert "I joined NR in June." in result["context"]
    with psycopg.connect(dsn) as connection:
        rows = connection.execute(
            "SELECT DISTINCT entity_id FROM unit_entities WHERE tenant_id=%s "
            "AND user_id='lin' AND entity_id IS NOT NULL",
            (tenant,),
        ).fetchall()
    assert len(rows) == 1


def test_explicit_alias_and_grounded_continuation_share_an_identity(recall_database):
    connection, authority = recall_database
    definition = "Nova Research, also called NR, opened a laboratory."
    continuation = "I joined NR in June."
    add_fact(connection, authority, "definition", definition)
    add_fact(connection, authority, "continuation", continuation)
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    first = repository.publish_fact(
        "definition",
        ("Nova Research",),
        definition,
        aliases=(
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": "Nova Research, also called NR",
            },
        ),
    )
    assert len(first) == 1
    second = repository.publish_fact(
        "continuation",
        ("NR",),
        continuation,
        references=({"name": "NR", "reference_id": "definition", "quote": continuation},),
    )
    assert second == first
    stored = connection.execute(
        "SELECT observed_mention FROM unit_entities WHERE unit_id='continuation'"
    ).fetchone()
    assert stored == ("NR",)


def test_one_fact_naming_canonical_and_alias_publishes_one_membership(recall_database):
    connection, authority = recall_database
    quote = "Nova Research, also called NR, opened a laboratory."
    add_fact(connection, authority, "definition", quote)
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    result = repository.publish_fact(
        "definition",
        ("Nova Research", "NR"),
        quote,
        aliases=(
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": "Nova Research, also called NR",
            },
        ),
    )
    assert len(result) == 1


def test_distinct_objects_can_independently_declare_the_same_alias(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    identities = []
    for node, name in (("marine", "Atlas Marine Lab"), ("materials", "Aurora Materials Lab")):
        quote = name + ", also called AML, has its own laboratory."
        add_fact(connection, authority, node, quote)
        identities.append(
            repository.publish_fact(
                node,
                (name, "AML"),
                quote,
                aliases=(
                    {"canonical_name": name, "alias": "AML", "quote": name + ", also called AML"},
                ),
            )
        )
    assert len(identities[0]) == len(identities[1]) == 1
    assert identities[0] != identities[1]
    names = connection.execute(
        "SELECT DISTINCT entity_id FROM entity_names WHERE name_key='aml'"
    ).fetchall()
    assert len(names) == 2


def test_late_alias_can_use_the_other_spelling_as_canonical_anchor(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    add_fact(connection, authority, "early", "NR opened a laboratory.")
    original = repository.publish_fact("early", ("NR",), "NR opened a laboratory.")
    quote = "Nova Research, also called NR, opened a laboratory."
    add_fact(connection, authority, "late", quote)
    resolved = repository.publish_fact(
        "late",
        ("Nova Research", "NR"),
        quote,
        aliases=(
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": "Nova Research, also called NR",
            },
        ),
        references=({"name": "NR", "reference_id": "early", "quote": quote},),
    )
    assert resolved == original


def test_alias_declaration_does_not_merge_two_referenced_identities(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    originals = []
    for node, name in (("early-a", "Nova Research"), ("early-b", "NR")):
        quote = name + " opened a laboratory."
        add_fact(connection, authority, node, quote)
        originals.extend(repository.publish_fact(node, (name,), quote))
    quote = "Nova Research, also called NR, opened a laboratory."
    add_fact(connection, authority, "late", quote)
    resolved = repository.publish_fact(
        "late",
        ("Nova Research", "NR"),
        quote,
        aliases=(
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": "Nova Research, also called NR",
            },
        ),
        references=(
            {"name": "Nova Research", "reference_id": "early-a", "quote": quote},
            {"name": "NR", "reference_id": "early-b", "quote": quote},
        ),
    )
    assert set(resolved) == set(originals)
    assert not connection.execute(
        "SELECT 1 FROM entity_name_support WHERE support_kind='alias'"
    ).fetchall()


def test_oversized_entity_does_not_prevent_fact_publication(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    name = "x" * (config.index.entity_name_max_bytes + 1)
    add_fact(connection, authority, "fact", name)
    assert repository.publish_fact("fact", (name,), name) == ()


def test_grounded_alias_only_fact_has_a_membership_for_continuation(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    quote = "Nova Research, also called NR, opened a laboratory."
    add_fact(connection, authority, "definition", quote)
    definition = repository.publish_fact(
        "definition",
        (),
        quote,
        aliases=(
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": "Nova Research, also called NR",
            },
        ),
    )
    assert len(definition) == 1
    later_quote = "I joined NR in June."
    add_fact(connection, authority, "continuation", later_quote)
    continuation = repository.publish_fact(
        "continuation",
        ("NR",),
        later_quote,
        references=({"name": "NR", "reference_id": "definition", "quote": later_quote},),
    )
    assert continuation == definition


def test_recall_tuning_does_not_change_extraction_entity_metadata(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    names = tuple(f"Laboratory{i}" for i in range(10))
    quote = ", ".join(names) + " are separate laboratories."
    add_fact(connection, authority, "fact", quote)
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    repository.publish_fact("fact", names, quote)
    expected = repository.reference_entities(["fact"])
    tuned = config.model_copy(
        update={"search": config.search.model_copy(update={"entities_per_seed": 1})}
    )
    other = EntityRepository(
        connection, tuned, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    assert other.reference_entities(["fact"]) == expected


class FocusedAliasModels(AliasModels):
    def generate(self, role, **kwargs):
        envelope = json.loads(kwargs["user"])
        text = envelope.get("center", {}).get("text", "")
        if role == "extraction" and "cloud" in text:
            self.inputs.append(envelope)
            return ModelResult(
                content=json.dumps({"outcome": "facts", "facts": [{"text": text, "quote": text}]}),
                usage=Usage(
                    input_tokens=20, output_tokens=20, total_tokens=40, coverage="complete"
                ),
            )
        return super().generate(role, **kwargs)

    def embed(self, texts, **kwargs):
        return EmbeddingResult(
            vectors=tuple(
                (
                    (0.0, 1.0)
                    if "I joined NR" in text
                    else ((0.8, 0.6) if "cloud" in text else (1.0, 0.0))
                )
                + (0.0,) * 1022
                for text in texts
            ),
            usage=Usage(
                input_tokens=len(texts),
                output_tokens=0,
                total_tokens=len(texts),
                coverage="complete",
            ),
        )


@pytest.mark.asyncio
async def test_public_mid_discovers_alias_target_absent_from_direct_arms(
    reference_client, tmp_path
):
    client, dsn, tenant, config, app = reference_client
    search = config.search.model_copy(
        update={
            "graph_seed_max": 2,
            "fast": config.search.fast.model_copy(update={"channel_candidates": 1}),
            "mid": config.search.mid.model_copy(update={"channel_candidates": 1}),
        }
    )
    config = config.model_copy(update={"search": search})
    app.state.runtime.config = config
    app.state.runtime.models = FocusedAliasModels()
    await retain(
        client, "discovery-definition", ["Nova Research, also called NR, opened a laboratory."]
    )
    assert run_source(dsn, config, app.state.runtime.models)
    await retain(client, "discovery-continuation", ["I joined NR in June."])
    assert run_source(dsn, config, app.state.runtime.models)
    await retain(client, "discovery-distractor", ["I observed a cloud over the city."])
    assert run_source(dsn, config, app.state.runtime.models)
    observed = {}
    for mode in ("fast", "mid"):
        response = await client.post(
            "/v1/recall",
            json={
                "user_id": "lin",
                "query": "Nova Research laboratory",
                "recall_level": mode,
                "max_context_tokens": 2048,
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()
        observed[mode] = result
        if mode == "fast":
            assert "I joined NR in June." not in result["context"]
            continue
        target = next(
            item
            for item in result["items"]
            if item["text"] == "I joined NR in June." and item["kind"] == "fact"
        )
        assert any(path.get("alias_witnesses") for path in target["paths"])
        assert "Alias: 'Nova Research, also called NR'" in result["context"]
        with psycopg.connect(dsn) as connection:
            decisions = connection.execute(
                "SELECT decisions FROM traces WHERE tenant_id=%s AND trace_id=%s",
                (tenant, result["trace_id"]),
            ).fetchone()[0]
        assert target["unit_id"] not in decisions["arms"]["dense"]
        assert target["unit_id"] not in decisions["arms"]["lexical"]
    assert len(app.state.runtime.models.inputs) == 3
    (tmp_path / "public-alias-discovery.json").write_text(
        json.dumps(
            {"provider": "deterministic_fake", "modes": observed}, ensure_ascii=False, indent=2
        )
        + "\n"
    )


def test_reference_metadata_keeps_matched_name_inside_the_name_cap(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={"index": config.index.model_copy(update={"entity_search_names_per_entity": 1})}
    )
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    quote = "Nova Research, also called NR, opened a laboratory."
    add_fact(connection, authority, "definition", quote)
    repository.publish_fact(
        "definition",
        ("Nova Research",),
        quote,
        aliases=(
            {
                "canonical_name": "Nova Research",
                "alias": "NR",
                "quote": "Nova Research, also called NR",
            },
        ),
    )
    names = connection.execute(
        "SELECT name_id,name_text FROM entity_names ORDER BY name_id"
    ).fetchall()
    preferred_id, preferred_text = names[-1]
    result = repository.reference_entities(["definition"], preferred_name_ids=(preferred_id,))
    assert len(result["definition"]) == 1
    assert result["definition"][0]["name"] == preferred_text


def test_repeated_declaration_witnessed_only_through_alias_reuses_observed_canonical_anchor(
    recall_database,
):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    first = "Nova Research, also called NR, opened a laboratory."
    add_fact(connection, authority, "first", first)
    original = repository.publish_fact(
        "first",
        ("Nova Research", "NR"),
        first,
        aliases=({"canonical_name": "Nova Research", "alias": "NR", "quote": first},),
    )
    quote = "NR is the Nova Research organization I mentioned before."
    add_fact(connection, authority, "again", quote)
    aliases = ({"canonical_name": "Nova Research", "alias": "NR", "quote": quote},)
    references = ({"name": "NR", "reference_id": "first", "quote": quote},)
    prepared = repository.prepare_fact(("Nova Research", "NR"), aliases, references)
    resolved = repository.publish_fact(
        "again",
        ("Nova Research", "NR"),
        quote,
        aliases=aliases,
        references=references,
        prepared=prepared,
    )
    assert resolved == original
    assert repository.diagnostics == []
    assert (
        connection.execute(
            "SELECT COUNT(DISTINCT entity_id) FROM entity_names WHERE tenant_id=%s AND user_id=%s",
            (authority.tenant_id, authority.user_id),
        ).fetchone()[0]
        == 1
    )
    assert not connection.execute(
        "SELECT 1 FROM entity_name_support WHERE tenant_id=%s AND user_id=%s "
        "AND name_id=anchor_name_id",
        (authority.tenant_id, authority.user_id),
    ).fetchall()
    assert connection.execute(
        "SELECT support_kind FROM entity_name_support s JOIN entity_names n "
        "USING(tenant_id,user_id,name_id) WHERE s.tenant_id=%s AND s.user_id=%s "
        "AND s.memory_id='again' AND n.name_key='nova research'",
        (authority.tenant_id, authority.user_id),
    ).fetchall() == [("canonical",)]
    # This complete declaration independently supports the same observed binding.
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND evidence_id='first'",
        (authority.tenant_id,),
    )
    assert (
        repository._reference("NR", ({"name": "NR", "reference_id": "again", "quote": quote},))[
            "entity_id"
        ]
        == original[0]
    )


def test_alias_only_reference_does_not_create_an_unseen_canonical_chain(recall_database):
    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    first = "Horizon Studio, also called HST, opened a workshop."
    add_fact(connection, authority, "first", first)
    original = repository.publish_fact(
        "first",
        ("Horizon Studio", "HST"),
        first,
        aliases=({"canonical_name": "Horizon Studio", "alias": "HST", "quote": first},),
    )
    quote = "HST, the studio I mentioned, is also called Workshop Seven."
    add_fact(connection, authority, "third", quote)
    repository.publish_fact(
        "third",
        ("HST", "Workshop Seven"),
        quote,
        aliases=({"canonical_name": "HST", "alias": "Workshop Seven", "quote": quote},),
        references=({"name": "HST", "reference_id": "first", "quote": quote},),
    )
    assert "dependent_alias_anchor" in repository.diagnostics
    assert not connection.execute(
        "SELECT 1 FROM entity_names WHERE tenant_id=%s AND entity_id=%s "
        "AND name_key='workshop seven'",
        (authority.tenant_id, original[0]),
    ).fetchall()
