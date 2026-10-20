"""Fast causal retrieval runs only bounded outgoing fact links in isolated PostgreSQL."""

import time
from pathlib import Path

import pytest
from test_core_http import core_database as core_database
from test_recall_repository import FixedModels, TransactionPool, add_fact, add_insight
from test_recall_repository import recall_database as recall_database

from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.repository import RecallRepository
from oam.retrieval.service import RecallService

pytestmark = pytest.mark.integration


def history(connection, authority):
    for node, text in (
        ("effect", "Anchorword: I moved because my rent increased."),
        ("rent", "My rent increased because my job moved."),
        ("job", "My job moved to a new office."),
        ("third", "The new office opened after a merger."),
        ("reverse", "A reverse effect."),
        ("semantic", "An unrelated semantic neighbor."),
        ("sibling", "An unrelated hierarchy sibling."),
        ("hidden", "An excluded cause."),
    ):
        add_fact(
            connection,
            authority,
            node,
            text,
            eligible=node != "hidden",
            direction=1.0 if node == "effect" else -1.0,
        )
    for source, target, kind in (
        ("effect", "rent", "caused_by"),
        ("rent", "job", "caused_by"),
        ("job", "third", "caused_by"),
        ("reverse", "effect", "caused_by"),
        ("effect", "semantic", "semantic"),
        ("effect", "hidden", "caused_by"),
    ):
        connection.execute(
            "INSERT INTO unit_links VALUES (%s,%s,%s,%s,%s,1)",
            (authority.tenant_id, authority.user_id, source, target, kind),
        )
    add_insight(connection, authority, "plan", ("effect", "sibling"))
    return load_config(Path(__file__).parents[2] / "configs/oam.yaml").config


def test_fast_graph_sql_discovers_two_hops_without_semantic_or_insight_probes(
    recall_database,
):
    connection, authority = recall_database
    config = history(connection, authority)
    config = config.model_copy(
        update={"search": config.search.model_copy(update={"links_per_seed": 1})}
    )
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)
    statements = []
    execute = repository._execute

    def capture(sql, params=()):
        statements.append(sql)
        return execute(sql, params)

    repository._execute = capture
    ranks, paths = repository.fast_graph(("effect",), (1.0,) + (0.0,) * 1023)
    assert set(ranks) == {"rent", "job"}
    assert paths["job"][0]["nodes"] == ["effect", "rent", "job"]
    assert repository.valid_paths(paths, (1.0,) + (0.0,) * 1023) == paths
    assert len(statements) == 3
    assert repository._has_insights is None
    assert "semantic_rows" not in statements[0]
    assert "insight_support" not in statements[0]
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE evidence_id='rent' AND tenant_id=%s",
        (authority.tenant_id,),
    )
    assert repository.fast_graph(("effect",), (1.0,) + (0.0,) * 1023) == ((), {})
    assert repository.valid_paths(paths, (1.0,) + (0.0,) * 1023) == {"rent": [], "job": []}


def test_fast_opt_in_recovers_graph_only_cause_with_cited_final_context(recall_database):
    connection, authority = recall_database
    config = history(connection, authority)
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={"fast": config.search.fast.model_copy(update={"channel_candidates": 1})}
            )
        }
    )
    service = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="generation",
    )
    for enabled in (False, True):
        result = service.search(
            authority,
            SearchRequest(
                query="anchorword",
                recall_level="fast",
                fast_graph=enabled,
                max_context_tokens=2048,
            ),
        )
        ids = {item["unit_id"] for item in result["items"]}
        assert ids == ({"effect", "rent", "job"} if enabled else {"effect"})
        assert ("graph" in result["diagnostics"]["executed_arms"]) == enabled
        if enabled:
            assert "I moved because my rent increased." in result["context"]
            assert "My rent increased because my job moved." in result["context"]
            assert all(item["citations"] for item in result["items"])
            assert result["diagnostics"]["generative_calls"] == 0


@pytest.mark.parametrize("mode", ["fast", "normal"])
@pytest.mark.parametrize(
    ("graph", "expected"),
    [
        ("off", {"effect"}),
        ("light", {"effect", "rent", "job"}),
        ("full", {"effect", "rent", "job", "semantic"}),
    ],
)
def test_configured_graph_selection_reaches_real_sql_context(
    recall_database, tmp_path, mode, graph, expected
):
    connection, authority = recall_database
    history(connection, authority)
    path = tmp_path / "oam.yaml"
    path.write_text(f'search:\n  {mode}:\n    graph: "{graph}"\n    channel_candidates: 1\n')
    config = load_config(path).config
    service = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="generation",
    )
    result = service.search(
        authority, SearchRequest(query="anchorword", recall_level=mode, max_context_tokens=2048)
    )
    assert {item["unit_id"] for item in result["items"]} == expected
    assert ("An unrelated semantic neighbor." in result["context"]) == (graph == "full")
    assert result["diagnostics"]["graph"] == graph
    assert ("graph" in result["diagnostics"]["executed_arms"]) == (graph != "off")
    assert result["diagnostics"]["generative_calls"] == 0
    assert result["context_tokens"] <= 2048
    assert len(result["items"]) <= getattr(config.search, mode).max_results
    assert all(item["citations"] for item in result["items"])


def test_graph_cannot_add_candidates_when_every_fact_is_a_seed(recall_database):
    from test_entity_graph import alias_history

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    connection.execute(
        "INSERT INTO unit_links VALUES (%s,%s,'seed','continuation','caused_by',1)",
        (authority.tenant_id, authority.user_id),
    )
    seeds = ("definition", "seed", "continuation")
    for include_semantic in (False, True):
        assert repository.ordinary(seeds, vector, include_semantic=include_semantic) == ((), {})


def test_fast_alias_only_target_reaches_cited_context_and_revocation_drops_its_path(
    recall_database,
):
    from test_entity_graph import alias_history

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    connection.execute(
        "UPDATE search_units SET embedding=%s::vector WHERE tenant_id=%s AND unit_id<>'seed'",
        (str([-1.0] + [0.0] * 1023), authority.tenant_id),
    )
    config = repository.config.model_copy(
        update={
            "search": repository.config.search.model_copy(
                update={
                    "fast": repository.config.search.fast.model_copy(
                        update={"channel_candidates": 1}
                    )
                }
            )
        }
    )
    service = RecallService(
        config, TransactionPool(connection), FixedModels(1024), generation_id="generation"
    )
    direct = service.search(authority, SearchRequest(query="moved", recall_level="fast"))
    assert {item["unit_id"] for item in direct["items"]} == {"seed"}
    enabled = service.search(
        authority, SearchRequest(query="moved", recall_level="fast", fast_graph=True)
    )
    assert "I joined NR in June" in enabled["context"]
    assert "Nova Research, also called NR" in enabled["context"]
    target = next(item for item in enabled["items"] if item["unit_id"] == "continuation")
    assert target["paths"][0]["alias_witnesses"][0]["citation"]["source_id"] == "definition"
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s "
        "AND evidence_id='definition'",
        (authority.tenant_id,),
    )
    revoked = service.search(
        authority, SearchRequest(query="moved", recall_level="fast", fast_graph=True)
    )
    assert {item["unit_id"] for item in revoked["items"]} == {"seed"}
    assert "continuation" not in repository.fast_graph(("seed",), vector)[0]


async def test_public_http_fast_graph_option_reaches_real_sql_context(core_database):
    import os

    import httpx
    import psycopg
    from api_test_app import create_api_only_app
    from test_core_http import use_created_user_key

    from oam.auth.credentials import AuthorizedUser
    from oam.indexing.identity import index_generation

    dsn, tenant, headers = core_database
    app = create_api_only_app(Path(__file__).parents[2] / "configs/oam.yaml")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        config = app.state.runtime.config
        app.state.runtime.config = config.model_copy(
            update={
                "search": config.search.model_copy(
                    update={"fast": config.search.fast.model_copy(update={"channel_candidates": 1})}
                )
            }
        )
        app.state.runtime.models = FixedModels(config.index.embedding_dimensions)
        generation = index_generation(config, os.environ)
        authority = AuthorizedUser("key", tenant, "reader", "scope", frozenset({"memory:read"}))
        with psycopg.connect(dsn) as connection:
            history(connection, authority)
            assert (
                connection.execute(
                    "SELECT index_generation_id FROM users WHERE tenant_id=%s AND user_id='scope'",
                    (tenant,),
                ).fetchone()[0]
                is None
            )
            connection.execute(
                "UPDATE users SET index_generation_id=%s WHERE tenant_id=%s AND user_id='scope'",
                (generation, tenant),
            )
            for table in ("passages", "memories", "search_units", "insights"):
                connection.execute(
                    psycopg.sql.SQL("UPDATE {} SET generation_id=%s WHERE tenant_id=%s").format(
                        psycopg.sql.Identifier(table)
                    ),
                    (generation, tenant),
                )
        for enabled in (False, True):
            response = await client.post(
                "/v1/recall",
                json={
                    "user_id": "scope",
                    "query": "anchorword",
                    "recall_level": "fast",
                    "fast_graph": enabled,
                },
            )
            assert response.status_code == 200, response.text
            result = response.json()
            assert {item["unit_id"] for item in result["items"]} == (
                {"effect", "rent", "job"} if enabled else {"effect"}
            )
            if enabled:
                assert "My rent increased because my job moved." in result["context"]
                assert all(item["citations"] for item in result["items"])
        batch = await client.post(
            "/v1/users/scope/search-many",
            json={"queries": ["anchorword"], "recall_level": "fast", "fast_graph": True},
        )
        assert batch.status_code == 200, batch.text
        assert {item["unit_id"] for item in batch.json()["results"][0]["result"]["items"]} == {
            "effect",
            "rent",
            "job",
        }
        rejected = await client.post(
            "/v1/recall",
            json={
                "user_id": "scope",
                "query": "anchorword",
                "recall_level": "normal",
                "fast_graph": True,
            },
        )
        assert rejected.status_code == 422


def test_high_degree_entity_respects_eligible_candidate_cap(recall_database):
    from test_entity_graph import alias_history

    from oam.indexing.entities import EntityRepository

    connection, authority = recall_database
    repository, vector, entity_id = alias_history(connection, authority)
    entities = EntityRepository(
        connection,
        repository.config,
        generation_id="generation",
        key=(authority.tenant_id, authority.user_id),
    )
    for index in range(40):
        node = f"member_{index:02}"
        text = f"NR, the organization I mentioned, funded project {index}."
        add_fact(connection, authority, node, text, eligible=index != 0)
        entities.publish_fact(
            node,
            ("NR",),
            text,
            references=({"name": "NR", "reference_id": "definition", "quote": text},),
        )
    config = repository.config.model_copy(
        update={
            "search": repository.config.search.model_copy(
                update={"entity_candidates_per_entity": 3}
            )
        }
    )
    bounded = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)
    ranks, paths = bounded.fast_graph(("seed",), vector)
    assert len(ranks) == 3
    assert "member_00" not in ranks
    assert all(path["entity"] == entity_id for group in paths.values() for path in group)
    assert bounded._has_insights is None


def test_revoked_alias_keeps_independently_supported_fast_causal_path(recall_database):
    from test_entity_graph import alias_history

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    connection.execute(
        "INSERT INTO unit_links VALUES (%s,%s,'seed','continuation','caused_by',1)",
        (authority.tenant_id, authority.user_id),
    )
    _, paths = repository.fast_graph(("seed",), vector)
    selected = {"continuation": paths["continuation"]}
    assert {path["relation"] for path in selected["continuation"]} == {"entity", "caused_by"}
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s "
        "AND evidence_id='definition'",
        (authority.tenant_id,),
    )
    retained = repository.valid_paths(selected, vector)
    assert [path["relation"] for path in retained["continuation"]] == ["caused_by"]
    assert repository._has_insights is None
    assert "continuation" in repository.fast_graph(("seed",), vector)[0]


def test_fast_graph_does_not_read_identity_support_when_every_eligible_fact_is_a_seed(
    recall_database,
):
    from test_entity_graph import alias_history

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    plans = []

    def explain(query, parameters):
        plans.append(
            connection.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + query, parameters).fetchone()[0][
                0
            ]["Plan"]
        )
        return []

    repository._execute = explain
    assert repository.fast_graph(("definition", "seed", "continuation"), vector) == ((), {})

    def visits(node):
        own = (
            node.get("Actual Loops", 0)
            if node.get("Relation Name") in {"entity_names", "entity_name_support", "unit_entities"}
            else 0
        )
        return own + sum(visits(child) for child in node.get("Plans", ()))

    assert sum(visits(plan) for plan in plans) == 0


@pytest.mark.parametrize("include_semantic", (False, True))
def test_graph_checks_fact_support_once_across_identity_projections(
    recall_database, include_semantic
):
    from test_entity_graph import alias_history

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    add_insight(connection, authority, "summary", ("definition", "seed"))
    plans = []
    execute = repository._execute

    def explain(query, parameters):
        plans.append(
            connection.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + query, parameters).fetchone()[0][
                0
            ]["Plan"]
        )
        return execute(query, parameters)

    repository._execute = explain
    ranks, paths = repository.ordinary(("seed",), vector, include_semantic=include_semantic)
    assert "continuation" in ranks
    assert paths["continuation"][0]["alias_witnesses"][0]["quote"] == (
        "Nova Research, also called NR"
    )

    def support_reads(node):
        own = node.get("Actual Loops", 0) if node.get("Relation Name") == "support" else 0
        return own + sum(support_reads(child) for child in node.get("Plans", ()))

    # Identity joins reuse the three fact proofs. Fast separately proves one
    # nonseed fact for its full-history gate before reading stored identities.
    assert sum(support_reads(plan) for plan in plans) == (3 if include_semantic else 4)

    def membership_name_reads(node, in_memberships=False):
        in_memberships |= node.get("Subplan Name") == "CTE memberships"
        own = (
            node.get("Actual Loops", 0)
            if in_memberships
            and node.get("Relation Name") in {"entity_names", "entity_name_support"}
            else 0
        )
        return own + sum(
            membership_name_reads(child, in_memberships) for child in node.get("Plans", ())
        )

    # Memberships must also reuse the validated name bindings, including alias anchors.
    assert sum(membership_name_reads(plan) for plan in plans) == 0


def test_fast_graph_projects_only_seed_entities_with_nonseed_alias_support(recall_database):
    from test_entity_graph import alias_history

    from oam.indexing.entities import EntityRepository

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    repository.config = repository.config.model_copy(
        update={"search": repository.config.search.model_copy(update={"entities_per_seed": 1})}
    )
    expected = repository.fast_graph(("seed",), vector)
    entities = EntityRepository(
        connection,
        repository.config,
        generation_id="generation",
        key=(authority.tenant_id, authority.user_id),
    )
    for index in range(24):
        name = f"Unrelated organization {index}"
        node = f"unrelated_{index}"
        text = f"{name} opened its own laboratory."
        add_fact(connection, authority, node, text)
        entities.publish_fact(node, (name,), text)

    # An unsupported identity sorts before the valid one. Applying the entity
    # cap to raw seed identities would incorrectly hide the valid alias path.
    connection.execute(
        "INSERT INTO entity_units VALUES (%s,%s,'000-invalid','generation','Unsupported')",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "INSERT INTO entity_names(tenant_id,user_id,name_id,entity_id,name_text,name_key,"
        "name_kind,lexical_english,lexical_simple) "
        "VALUES (%s,%s,'invalid-name','000-invalid','Unsupported','unsupported','canonical','','')",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "INSERT INTO unit_entities(tenant_id,user_id,unit_id,entity_key,entity_id,name_id,"
        "observed_mention,mention_quote,generation_id) "
        "VALUES (%s,%s,'seed','unsupported','000-invalid','invalid-name',"
        "'Unsupported','Unsupported','generation')",
        (authority.tenant_id, authority.user_id),
    )

    plans = []
    execute = repository._execute

    def explain(query, parameters):
        plans.append(
            connection.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + query, parameters).fetchone()[0][
                0
            ]["Plan"]
        )
        return execute(query, parameters)

    repository._execute = explain
    ranks, paths = repository.fast_graph(("seed",), vector)
    assert (ranks, paths) == expected
    assert paths["continuation"][0]["alias_witnesses"][0]["citation"]["source_id"] == ("definition")

    def projected_names(node):
        own = (
            node["Actual Rows"] * node["Actual Loops"]
            if node.get("Subplan Name") == "CTE raw_name_support"
            else 0
        )
        return own + sum(projected_names(child) for child in node.get("Plans", ()))

    # Two definition supports and the seed's canonical support; the continuation
    # reuses the definition's alias. Unrelated names must not enter the projection.
    assert sum(projected_names(plan) for plan in plans) == 3


@pytest.mark.parametrize("include_semantic", (False, True))
def test_graph_validates_only_stored_path_candidates_without_capping_raw_neighbors(
    recall_database, include_semantic
):
    from test_entity_graph import alias_history

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    repository.config = repository.config.model_copy(
        update={"search": repository.config.search.model_copy(update={"links_per_seed": 1})}
    )
    for node in ("cause", "cause2", "semantic_out", "semantic_in", "hidden"):
        add_fact(connection, authority, node, node, eligible=node != "hidden")
    for source, target, kind, weight in (
        ("seed", "hidden", "caused_by", 1.0),
        ("seed", "cause", "caused_by", 0.9),
        ("cause", "cause2", "caused_by", 1.0),
        ("seed", "semantic_out", "semantic", 1.0),
        ("semantic_in", "seed", "semantic", 1.0),
    ):
        connection.execute(
            "INSERT INTO unit_links VALUES (%s,%s,%s,%s,%s,%s)",
            (authority.tenant_id, authority.user_id, source, target, kind, weight),
        )
    expected = repository.ordinary(("seed",), vector, include_semantic=include_semantic)
    assert set(expected[0]) == (
        {"definition", "continuation", "cause", "cause2", "semantic_out", "semantic_in"}
        if include_semantic
        else {"definition", "continuation", "cause", "cause2"}
    )
    for index in range(24):
        add_fact(connection, authority, f"unrelated_{index}", "An unrelated supported fact.")

    plans = []
    execute = repository._execute

    def explain(query, parameters):
        plans.append(
            connection.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + query, parameters).fetchone()[0][
                0
            ]["Plan"]
        )
        return execute(query, parameters)

    repository._execute = explain
    assert repository.ordinary(("seed",), vector, include_semantic=include_semantic) == expected

    def support_reads(node):
        own = node.get("Actual Loops", 0) if node.get("Relation Name") == "support" else 0
        return own + sum(support_reads(child) for child in node.get("Plans", ()))

    # The uncapped path closure retains the hidden high-weight neighbor until
    # eligibility filtering, both semantic directions, and the nonseed alias anchor.
    # Unrelated history must not add a current-support proof for every fact.
    assert sum(support_reads(plan) for plan in plans) <= 12


def test_fast_graph_keeps_full_history_gate_when_nonseed_fact_has_no_stored_path(
    recall_database,
):
    from test_entity_graph import alias_history

    from oam.retrieval.repository import _graph_fact_eligibility

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    seeds = ("definition", "seed", "continuation")

    def admitted_seeds():
        return {
            row["unit_id"]
            for row in repository._execute(
                _graph_fact_eligibility(include_semantic=False) + "SELECT unit_id FROM seeds",
                {"seeds": list(seeds)},
            )
        }

    assert admitted_seeds() == set()
    assert repository.fast_graph(seeds, vector) == ((), {})
    add_fact(connection, authority, "unrelated", "A fact without stored graph relations.")
    # The full-history gate admits seeds even when the nonseed fact has no
    # stored path. Existing seeds still cannot become new graph candidates.
    assert admitted_seeds() == set(seeds)
    assert repository.fast_graph(seeds, vector) == ((), {})


def test_raw_graph_guard_keeps_ineligible_nonseed_on_full_proof_path(recall_database):
    from test_entity_graph import alias_history

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    seeds = ("definition", "seed", "continuation")
    add_fact(connection, authority, "hidden_nonseed", "Withdrawn unrelated fact.", eligible=False)
    queries = []
    execute = repository._execute

    def capture(query, parameters):
        queries.append(query)
        return execute(query, parameters)

    repository._execute = capture
    assert repository.fast_graph(seeds, vector) == ((), {})
    assert len(queries) == 2
    assert "raw_seed_entities AS MATERIALIZED" in queries[1]


def test_raw_graph_guard_cannot_use_another_user_or_tenant(recall_database):
    from dataclasses import replace

    from test_entity_graph import alias_history

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    for tenant, user in (
        (authority.tenant_id, "other_user"),
        (authority.tenant_id + "-other", "scope"),
    ):
        if tenant != authority.tenant_id:
            connection.execute(
                "INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (tenant,)
            )
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,index_generation_id) "
            "VALUES (%s,%s,'synthetic','test','persistent','generation')",
            (tenant, user),
        )
        add_fact(
            connection,
            replace(authority, tenant_id=tenant, user_id=user),
            "other_fact",
            "Unrelated scope.",
        )
    queries = []
    execute = repository._execute

    def capture(query, parameters):
        queries.append(query)
        return execute(query, parameters)

    repository._execute = capture
    assert repository.fast_graph(("definition", "seed", "continuation"), vector) == ((), {})
    assert len(queries) == 1


def test_raw_graph_guard_preserves_seed_causal_paths_when_nonseed_exists(recall_database):
    from test_entity_graph import alias_history

    connection, authority = recall_database
    repository, vector, _ = alias_history(connection, authority)
    seeds = ("definition", "seed", "continuation")
    connection.execute(
        "INSERT INTO unit_links VALUES (%s,%s,'seed','continuation','caused_by',1)",
        (authority.tenant_id, authority.user_id),
    )
    add_fact(connection, authority, "unrelated", "Another eligible fact without graph paths.")
    rows = []
    execute = repository._execute

    def capture(query, parameters):
        result = execute(query, parameters)
        rows.extend(row for row in result if "source" in row)
        return result

    repository._execute = capture
    expected = repository.ordinary(seeds, vector, include_semantic=False)
    assert any(row["source"] == "causal" and row["target_id"] == "continuation" for row in rows)
    assert repository.fast_graph(seeds, vector) == expected
