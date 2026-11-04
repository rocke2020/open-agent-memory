import math
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg.types.range import Range

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.db.context import bind_user
from oam.db.eligibility import ELIGIBLE_UNITS
from oam.models.runtime import EmbeddingResult, Usage
from oam.retrieval.fts import index_streams

pytestmark = pytest.mark.integration
pytest_plugins = ["test_core_http"]


@pytest.fixture
def recall_database(core_database):
    # ANN and planner assertions need physical isolation, including index and table statistics.
    dsn, _, _ = core_database
    with (
        psycopg.connect(dsn, autocommit=True) as connection,
        connection.transaction(force_rollback=True),
    ):
        tenant = f"recall-{uuid4().hex}"
        connection.execute("INSERT INTO tenants(tenant_id,state) VALUES (%s,'active')", (tenant,))
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,index_generation_id) "
            "VALUES (%s,'scope','synthetic','test','persistent','generation')",
            (tenant,),
        )
        authority = AuthorizedUser("key", tenant, "reader", "scope", frozenset({"memory:read"}))
        yield connection, authority


def add_fact(
    connection, authority, node_id, text, *, eligible=True, direction=1.0, with_original=False
):
    tenant, scope = authority.tenant_id, authority.user_id
    original = text.encode()
    connection.execute(
        "INSERT INTO sources(tenant_id,user_id,source_id,input_role,active_version,status) "
        "VALUES (%s,%s,%s,'interaction',1,'active')",
        (tenant, scope, node_id),
    )
    connection.execute(
        "INSERT INTO source_versions(tenant_id,user_id,source_id,source_version,media_type,"
        "original_bytes,content_sha256,byte_length,status) "
        "VALUES (%s,%s,%s,1,'text/plain',%s,%s,%s,'active')",
        (tenant, scope, node_id, original, b"s" * 32, len(original)),
    )
    connection.execute(
        "INSERT INTO evidence(tenant_id,user_id,evidence_id,source_id,source_version,accepted_seq,"
        "content_text,learning_eligible,status) VALUES (%s,%s,%s,%s,1,1,%s,%s,'active')",
        (tenant, scope, node_id, node_id, text, eligible),
    )
    connection.execute(
        "INSERT INTO passages(tenant_id,user_id,passage_id,evidence_id,parse_revision,locator,"
        "normalized_text,generation_id,visible_from) VALUES (%s,%s,%s,%s,1,'{}',%s,'generation',1)",
        (tenant, scope, node_id, node_id, text),
    )
    connection.execute(
        "INSERT INTO memories(tenant_id,user_id,memory_id,canonical_claim,assertion_kind,"
        "generation_id,visible_from) VALUES (%s,%s,%s,%s,'stated','generation',1)",
        (tenant, scope, node_id, text),
    )
    connection.execute(
        "INSERT INTO support(tenant_id,user_id,memory_id,evidence_id,passage_id,"
        "support_kind,span) "
        "VALUES (%s,%s,%s,%s,%s,'quote',%s)",
        (tenant, scope, node_id, node_id, node_id, Jsonb({"quote": text})),
    )
    dimensions = connection.execute("SELECT embedding_dimensions FROM index_settings").fetchone()[0]
    vector = str([direction, math.sqrt(1 - direction * direction)] + [0.0] * (dimensions - 2))
    streams = index_streams(text)
    connection.execute(
        "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,source_id,source_version,"
        "evidence_id,memory_id,generation_id,content_text,locator,embedding,lexical_text,"
        "lexical_english,lexical_simple) "
        "VALUES (%s,%s,%s,'fact','memory',%s,1,%s,%s,'generation',%s,%s,%s::vector,%s,%s,%s)",
        (
            tenant,
            scope,
            node_id,
            node_id,
            node_id,
            node_id,
            text,
            Jsonb({"source_id": node_id, "passage_id": node_id, "quote": text}),
            vector,
            text.lower(),
            streams.english,
            streams.simple,
        ),
    )

    if with_original:
        connection.execute(
            "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,source_id,"
            "source_version,evidence_id,passage_id,generation_id,content_text,locator,"
            "embedding,lexical_text,lexical_english,lexical_simple) "
            "SELECT tenant_id,user_id,unit_id || '_passage','passage',origin,source_id,"
            "source_version,evidence_id,%s,generation_id,content_text,%s,%s::vector,'',''::tsvector,"
            "''::tsvector FROM search_units WHERE tenant_id=%s AND user_id=%s AND unit_id=%s",
            (
                node_id,
                Jsonb({"source_id": node_id, "source_version": 1}),
                str([-1.0] + [0.0] * (dimensions - 1)),
                tenant,
                scope,
                node_id,
            ),
        )


def test_bilingual_fts_keeps_partial_candidates_and_ranks_primary_and_modifiers(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "a_partial", "我喜欢苹果")
    add_fact(connection, authority, "b_full", "我不喜欢苹果")
    add_fact(connection, authority, "c_english", "She planted apple trees")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert [row["unit_id"] for row in repository.lexical("我不喜欢苹果", 3)][:2] == [
        "b_full",
        "a_partial",
    ]
    assert [row["unit_id"] for row in repository.lexical("apples orchard", 3)] == ["c_english"]


def add_user_passage(connection, authority, node_id, text, mentioned_at):
    add_fact(connection, authority, node_id, text)
    connection.execute(
        "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,source_id,"
        "source_version,evidence_id,passage_id,generation_id,content_text,locator,"
        "embedding,lexical_text,lexical_english,lexical_simple,mentioned_at) "
        "SELECT tenant_id,user_id,unit_id || '_passage','passage',origin,source_id,"
        "source_version,evidence_id,%s,generation_id,content_text,%s,embedding,"
        "lexical_text,lexical_english,lexical_simple,%s FROM search_units "
        "WHERE tenant_id=%s AND user_id=%s AND unit_id=%s",
        (
            node_id,
            Jsonb({"role": "user", "source_id": node_id}),
            mentioned_at,
            authority.tenant_id,
            authority.user_id,
            node_id,
        ),
    )


def test_source_user_originals_filter_role_revision_and_eligibility_before_limit(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    for identifier in ("a_assistant", "b_ineligible", "z_user"):
        add_user_passage(connection, authority, identifier, "Workshop progress update.", None)
    connection.execute(
        'UPDATE search_units SET locator=locator || \'{"role":"assistant"}\'::jsonb '
        "WHERE tenant_id=%s AND user_id=%s AND unit_id='a_assistant_passage'",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND user_id=%s "
        "AND evidence_id='b_ineligible'",
        (authority.tenant_id, authority.user_id),
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    dimensions = config.index.embedding_dimensions
    vector = [1.0] + [0.0] * (dimensions - 1)
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)
    keys = (("a_assistant", 1), ("b_ineligible", 1), ("z_user", 1))

    assert [row["unit_id"] for row in repository.source_user_passage_ids(keys, vector, 1, ())] == [
        "z_user_passage"
    ]
    assert repository.source_user_passage_ids((("z_user", 2),), vector, 1, ()) == []
    assert repository.source_user_passage_ids(keys, vector, 1, ("z_user_passage",)) == []


def test_direct_retrieval_filters_ineligible_rows_before_its_limit(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "a_hidden", "orchard apple apple", eligible=False)
    add_fact(connection, authority, "b_visible", "orchard apple")
    add_fact(connection, authority, "c_other", "unrelated pears", direction=-1.0)
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert repository.user_generation() == "generation"
    assert [row["unit_id"] for row in repository.lexical("orchard", 1)] == ["b_visible"]
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    assert [row["unit_id"] for row in repository.dense(vector, 1)] == ["b_visible"]
    hydrated = repository.hydrate(("a_hidden", "b_visible"))
    assert [row["unit_id"] for row in hydrated] == ["b_visible"]
    assert hydrated[0]["citations"][0]["source_id"] == "b_visible"


def test_related_user_fact_excludes_seed_and_ineligible_neighbors_before_limit(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "seed", "I attended the science fair.", direction=1.0)
    add_fact(
        connection, authority, "hidden", "I joined another event.", eligible=False, direction=0.99
    )
    add_fact(connection, authority, "related", "I visited the art fair.", direction=0.9)
    connection.execute(
        'UPDATE support SET span=span || \'{"role":"user"}\'::jsonb '
        "WHERE tenant_id=%s AND user_id=%s",
        (authority.tenant_id, authority.user_id),
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert repository.related_user_fact(("seed",))["unit_id"] == "related"
    assert repository.related_user_fact(("seed", "related"))["unit_id"] == "related"


def test_related_user_fact_checks_support_in_rank_order_until_first_eligible_match(
    recall_database,
):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    for node, direction, eligible in (
        ("assistant_seed", 1.0, True),
        ("revoked_seed", 1.0, False),
        ("seed", 1.0, True),
        ("hidden_neighbor", 0.99, False),
        ("assistant_neighbor", 0.98, True),
        ("a_match", 0.97, True),
        ("b_match", 0.97, True),
    ):
        add_fact(connection, authority, node, node, direction=direction, eligible=eligible)
    for index in range(24):
        add_fact(connection, authority, f"distant-{index:02}", "A distant event", direction=0.2)
    connection.execute(
        "UPDATE support SET span=span || jsonb_build_object('role',CASE "
        "WHEN memory_id IN ('assistant_seed','assistant_neighbor') "
        "THEN 'assistant' ELSE 'user' END) "
        "WHERE tenant_id=%s AND user_id=%s",
        (authority.tenant_id, authority.user_id),
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)
    plans = []
    execute = repository._execute

    def explain(query, parameters, **kwargs):
        result = execute(query, parameters, **kwargs)
        parameters = {
            **repository.time_parameters,
            **parameters,
            "ann_tenant": authority.tenant_id,
            "ann_user": authority.user_id,
        }
        plans.append(
            connection.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + query, parameters).fetchone()[0][
                0
            ]["Plan"]
        )
        return result

    repository._execute = explain
    result = repository.related_user_fact(("missing", "assistant_seed", "revoked_seed", "seed"))
    assert result is not None and result["unit_id"] in {"a_match", "b_match"}
    assert result == {
        "seed_id": "seed",
        "unit_id": result["unit_id"],
        "source_id": result["unit_id"],
        "source_version": 1,
    }

    def memory_checks(node):
        own = node.get("Actual Loops", 0) if node.get("Relation Name") == "memories" else 0
        return own + sum(memory_checks(child) for child in node.get("Plans", ()))

    # Two seed checks and at most five nearby candidates. The ANN filter can
    # validate eligibility before the user-role check; distant facts stay unvisited.
    assert sum(memory_checks(plan) for plan in plans) <= 7


def test_supported_fact_ids_selects_next_eligible_user_fact(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    source_text = "I went to a local community meeting."
    add_user_passage(connection, authority, "source", source_text, "2023-05-20T00:00:00Z")
    add_fact(connection, authority, "excluded", "A closer but excluded fact.", direction=1.0)
    add_fact(connection, authority, "eligible", "A supported user fact.", direction=0.9)
    for memory_id in ("excluded", "eligible"):
        connection.execute(
            "INSERT INTO support(tenant_id,user_id,memory_id,evidence_id,passage_id,"
            "support_kind,span) VALUES (%s,%s,%s,'source','source','quote',%s)",
            (
                authority.tenant_id,
                authority.user_id,
                memory_id,
                Jsonb({"role": "user", "quote": source_text}),
            ),
        )
    connection.execute(
        "UPDATE memories SET lifecycle_state='excluded' "
        "WHERE tenant_id=%s AND user_id=%s AND memory_id='excluded'",
        (authority.tenant_id, authority.user_id),
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert repository.supported_fact_ids(("source_passage",), vector, 1, ("source",)) == [
        {"passage_id": "source_passage", "unit_id": "eligible"}
    ]


def test_source_passage_locators_preserves_passage_and_unit_ids(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_user_passage(
        connection, authority, "source", "The user joined a meeting.", "2023-05-20T00:00:00Z"
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    rows = repository.source_passage_locators(("source",))
    assert [(row["passage_id"], row["unit_id"]) for row in rows] == [("source", "source_passage")]


def test_candidate_eligibility_matches_full_source_and_support_policy(recall_database):
    from oam.db.eligibility import (
        ELIGIBLE_UNITS_CANDIDATE,
        ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS,
    )

    connection, authority = recall_database
    add_fact(connection, authority, "visible", "Visible source")
    add_fact(connection, authority, "hidden", "Hidden source", eligible=False)
    add_fact(connection, authority, "mixed", "One active and one inactive support")
    add_fact(connection, authority, "superseded", "Superseded source")
    connection.execute(
        "INSERT INTO support(tenant_id,user_id,memory_id,evidence_id,passage_id,"
        "support_kind,span) VALUES (%s,%s,'mixed','hidden','hidden','quote',%s)",
        (authority.tenant_id, authority.user_id, Jsonb({"quote": "source"})),
    )
    connection.execute(
        "UPDATE source_versions SET status='superseded' WHERE tenant_id=%s AND user_id=%s "
        "AND source_id='superseded'",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute("SET LOCAL ROLE oam_api")
    bind_user(connection.cursor(), authority)
    full = connection.execute("SELECT unit_id FROM (" + ELIGIBLE_UNITS + ") u").fetchall()
    candidate = connection.execute(
        "SELECT unit_id FROM (" + ELIGIBLE_UNITS_CANDIDATE + ") u"
    ).fetchall()
    no_insights = connection.execute(
        "SELECT unit_id FROM (" + ELIGIBLE_UNITS_CANDIDATE_NO_INSIGHTS + ") u"
    ).fetchall()

    assert full == candidate == no_insights == [("visible",)]


def test_lexical_recall_falls_back_for_conversational_recommendation(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "old_book", "Recommend a book to read.")
    add_fact(
        connection,
        authority,
        "recent_special",
        "Recommend Netflix stand-up comedy specials.",
    )
    connection.execute(
        "UPDATE search_units SET mentioned_at='2023-05-20T00:00:00Z' WHERE unit_id='old_book'"
    )
    connection.execute(
        "UPDATE search_units SET mentioned_at='2023-05-29T00:00:00Z' WHERE unit_id='recent_special'"
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert {
        row["unit_id"]
        for row in repository.lexical(
            "Can you recommend a show or movie for me to watch tonight?", 2
        )
    } == {"old_book", "recent_special"}


def test_lexical_eligibility_avoids_second_unit_lookup_for_each_hit(recall_database, monkeypatch):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    for index in range(256):
        add_fact(
            connection,
            authority,
            f"orchard{index:03d}",
            f"Shared orchard note {index}.",
            direction=0.0,
        )
    for table in ("search_units", "memories", "support", "passages", "evidence"):
        connection.execute(f"ANALYZE {table}")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 10)
    captured = []
    execute = repository._execute

    def capture(sql, parameters):
        captured.append((sql, parameters))
        return execute(sql, parameters)

    monkeypatch.setattr(repository, "_execute", capture)
    assert len(repository.lexical("shared orchard", 10)) == 10
    sql, parameters = captured[-1]
    plan = connection.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + sql, parameters).fetchone()[0][0][
        "Plan"
    ]

    def nodes(node):
        yield node
        for child in node.get("Plans", ()):
            yield from nodes(child)

    assert (
        sum(
            node.get("Actual Loops", 0)
            for node in nodes(plan)
            if node.get("Index Name") == "search_units_pkey"
        )
        == 0
    )
    # Ranking a common term should validate only enough supported candidates to
    # fill each term's cap, rather than every matching memory in the history.
    assert (
        sum(
            node.get("Actual Loops", 0) * node.get("Actual Rows", 0)
            for node in nodes(plan)
            if node.get("Relation Name") == "memories"
        )
        <= 20
    )


def test_imperative_show_does_not_promote_movie_preferences(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_user_passage(
        connection,
        authority,
        "old_movie",
        "I prefer Netflix comedy specials on my laptop.",
        "2023-05-20T00:00:00Z",
    )
    add_user_passage(
        connection,
        authority,
        "recent_laptop",
        "I want a laptop with 16 GB of RAM.",
        "2023-05-30T00:00:00Z",
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert (
        repository.lexical("Show me your recommendations for a laptop.", 1)[0]["unit_id"]
        == "recent_laptop_passage"
    )
    assert (
        repository.lexical("Recommend a laptop. Show me more.", 1)[0]["unit_id"]
        == "recent_laptop_passage"
    )
    assert (
        repository.lexical("Recommend a laptop and show me more.", 1)[0]["unit_id"]
        == "recent_laptop_passage"
    )


def test_literal_identifier_does_not_match_a_longer_identifier(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "exact", "The result is LAB-204.")
    add_fact(connection, authority, "longer", "The result is LAB-2045.")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert repository.lexical("LAB-204", 10) == []
    assert [row["unit_id"] for row in repository.anchors("LAB-204", 10)] == ["exact"]
    assert "longer" not in {row["unit_id"] for row in repository.lexical("LAB-204", 10)}


def test_mixed_identifier_query_ranks_remaining_words_before_literal_cap(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "a_unrelated", "LAB-204 catering is ready.")
    add_fact(connection, authority, "z_target", "LAB-204 deployment status is green.")
    add_fact(connection, authority, "near_identifier", "LAB-2045 deployment status is green.")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert [row["unit_id"] for row in repository.anchors("LAB-204 deployment status", 1)] == [
        "z_target"
    ]


def test_function_word_only_query_keeps_strict_lexical_match(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "band", "The Who will perform.")
    add_fact(connection, authority, "the_only", "The rehearsal starts now.")
    add_fact(connection, authority, "who_only", "Who will perform next?")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert [row["unit_id"] for row in repository.lexical("The Who", 3)] == ["band"]


@pytest.mark.parametrize(
    ("query", "target", "other"),
    [
        ("LAB-204 apples", "LAB-204 apple tree", "LAB-204 orchard"),
        ("LAB-204 我不喜欢苹果", "LAB-204 我不喜欢苹果", "LAB-204 我喜欢苹果"),
    ],
)
def test_anchor_remainder_preserves_bilingual_ranking(recall_database, query, target, other):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "a_other", other)
    add_fact(connection, authority, "z_target", target)
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert repository.lexical(query, 1) == []
    assert [row["unit_id"] for row in repository.anchors(query, 1)] == ["z_target"]


def test_common_query_term_cannot_exhaust_partial_match_cap(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "a_target", "Zephyr Zephyr initiative.")
    add_fact(connection, authority, "b_other", "Outcome of another effort.")
    for index in range(8):
        add_fact(connection, authority, f"filler_{index}", "Good good good good good.")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert [row["unit_id"] for row in repository.lexical("Zephyr good outcome", 1)] == ["a_target"]


def test_common_term_remains_a_bounded_candidate_source(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "only_first", "Orchid project update.")
    for index in range(7):
        add_fact(connection, authority, f"first_{index}", "Orchid initiative.")
    add_fact(connection, authority, "second", "Quality checks passed.")
    add_fact(connection, authority, "third", "Status was reported.")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert "only_first" in {
        row["unit_id"] for row in repository.lexical("Orchid quality status", 10)
    }


@pytest.mark.parametrize(
    ("query", "partial", "terms"),
    [
        ("kiln fan pressure", "Fan pressure rises during drying.", ("kiln", "fan", "pressure")),
        ("风扇 压力 湿度", "风扇压力升高。", ("风扇", "压力", "湿度")),
    ],
)
def test_partial_query_coverage_survives_repeated_single_term_competitors(
    recall_database, query, partial, terms
):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "z_partial", partial)
    for term_index, term in enumerate(terms):
        for index in range(3):
            add_fact(
                connection,
                authority,
                f"a_repeat_{term_index}_{index}",
                (term + " ") * 15 + "unrelated explanation " * 100,
            )
    # A stronger but ineligible match must not consume the candidate budget.
    add_fact(connection, authority, "a_hidden", query, eligible=False)
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert "z_partial" in {row["unit_id"] for row in repository.lexical(query, 2)}


def test_quoted_phrase_does_not_admit_separated_words_through_fts(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "exact", "The morning shift starts at eight.")
    add_fact(connection, authority, "separated", "Morning planning follows the night shift.")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert [row["unit_id"] for row in repository.anchors('"morning shift"', 10)] == ["exact"]
    assert "separated" not in {row["unit_id"] for row in repository.lexical('"morning shift"', 10)}


def test_quoted_phrase_ranks_remaining_words_before_literal_cap(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "a_unrelated", "The morning shift coffee rota changed.")
    add_fact(connection, authority, "z_target", "The morning shift staffing is short.")
    add_fact(
        connection,
        authority,
        "separated",
        "Morning planning follows the night shift staffing review.",
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert [row["unit_id"] for row in repository.anchors('"morning shift" staffing', 1)] == [
        "z_target"
    ]


def test_natural_hyphenated_query_keeps_partial_fts(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "balance", "I seek work and life balance.")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert [row["unit_id"] for row in repository.lexical("work-life balance", 10)] == ["balance"]


def test_underscore_identifier_uses_exact_anchor_instead_of_partial_fts(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "exact", "The patient_42 record is current.")
    add_fact(connection, authority, "split", "The patient 42 record is current.")
    add_fact(connection, authority, "longer", "The patient_420 record is current.")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    assert repository.lexical("patient_42", 10) == []
    assert [row["unit_id"] for row in repository.anchors("patient_42", 10)] == ["exact"]


def test_fast_service_returns_cited_context_with_mode_total(recall_database):
    from oam.retrieval.service import RecallService

    connection, authority = recall_database
    add_fact(
        connection,
        authority,
        "apple",
        "I plant apple trees in my orchard.",
        with_original=True,
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    models = FixedModels(config.index.embedding_dimensions)
    connection.execute("SET LOCAL ROLE oam_api")
    service = RecallService(config, TransactionPool(connection), models, generation_id="generation")

    result = service.search(
        authority, SearchRequest(query="apple", recall_level="fast", include={"chunks": False})
    )

    assert result["items"][0]["unit_id"] == "apple"
    assert "apple trees" in result["context"]
    assert "source_id" in result["context"]
    assert result["context_tokens"] > 0
    assert result["context_tokens"] <= config.search.fast.context_max_tokens
    assert result["diagnostics"]["executed_stages"] == [
        "fts",
        "anchors",
        "embedding",
        "dense",
        "related_user_fact",
        "local_context_selection",
    ]
    assert result["diagnostics"]["generative_calls"] == 0
    assert result["diagnostics"]["executed_arms"] == ["lexical", "dense"]
    zero_share = SearchRequest(
        query="apple",
        include={"chunks": False},
    )
    zero_share._context_max_tokens = 0
    empty = service.search(authority, zero_share)
    assert empty["context"] == ""
    assert empty["items"] == []


def test_recall_reports_pending_partial_and_ready_from_current_index_operation(recall_database):
    from oam.retrieval.service import RecallService

    connection, authority = recall_database
    add_fact(
        connection,
        authority,
        "apple",
        "Apple orchard",
        with_original=True,
    )
    connection.execute(
        "INSERT INTO principals(tenant_id,principal_id,role,state) "
        "VALUES (%s,'reader','service','active')",
        (authority.tenant_id,),
    )
    connection.execute(
        "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,source_id,"
        "source_version,kind,schema_version,status) "
        "VALUES (%s,%s,'index','reader','apple',1,'retain',1,'running')",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,input_digest,"
        "generation_id,status,attempts_remaining) VALUES (%s,%s,'work','index','index',%s,"
        "'generation','running',1)",
        (authority.tenant_id, authority.user_id, b"x" * 32),
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    service = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="generation",
    )
    request = SearchRequest(
        query="apple",
        recall_level="fast",
        include={"chunks": False},
    )
    pending = service.search(authority, request)
    assert pending["items"][0]["unit_id"] == "apple"
    assert pending["diagnostics"]["readiness"] == "pending"
    assert pending["diagnostics"]["partial"] is True
    connection.execute(
        "UPDATE operations SET status='succeeded',stage_state=%s WHERE tenant_id=%s",
        (Jsonb({"extraction_coverage": "partial"}), authority.tenant_id),
    )
    partial = service.search(authority, request)
    assert partial["diagnostics"]["readiness"] == "partial"
    assert partial["diagnostics"]["partial"] is True
    connection.execute(
        "UPDATE operations SET stage_state=%s WHERE tenant_id=%s",
        (Jsonb({"extraction_coverage": "complete"}), authority.tenant_id),
    )
    ready = service.search(authority, request)
    assert ready["diagnostics"]["readiness"] == "ready"
    assert ready["diagnostics"]["partial"] is False


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_existing_memory_remains_recallable_after_recipe_change(recall_database, mode):
    from oam.retrieval.service import RecallService

    connection, authority = recall_database
    add_fact(
        connection, authority, "apple", "I plant apple trees in my orchard.", with_original=True
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    models = FixedModels(config.index.embedding_dimensions)
    connection.execute("SET LOCAL ROLE oam_api")
    service = RecallService(config, TransactionPool(connection), models, generation_id="new-recipe")

    result = service.search(
        authority, SearchRequest(query="apple", recall_level=mode, include={"chunks": False})
    )
    assert any(item["unit_id"] == "apple" for item in result["items"])
    assert "apple trees" in result["context"]


def test_readiness_selects_current_operations_without_rescanning_each_history(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    connection.execute(
        "INSERT INTO principals(tenant_id,principal_id,role,state) "
        "VALUES (%s,'reader','service','active')",
        (authority.tenant_id,),
    )
    source_count = 24
    for index in range(source_count):
        source_id = f"session-{index:02}"
        add_fact(connection, authority, source_id, f"History session {index}")
        connection.execute(
            "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,source_id,"
            "source_version,kind,schema_version,status,created_at) "
            "VALUES (%s,%s,%s,'reader',%s,1,'retain',1,'succeeded','2026-01-01')",
            (authority.tenant_id, authority.user_id, source_id, source_id),
        )
        connection.execute(
            "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
            "input_digest,generation_id,status,attempts_remaining) "
            "VALUES (%s,%s,%s,%s,'index',%s,'generation','succeeded',1)",
            (authority.tenant_id, authority.user_id, source_id, source_id, b"x" * 32),
        )
    # The latest base operation counts across generations; consolidation does not.
    for operation_id, kind, generation in (
        ("wrong-generation", "retain", "other"),
        ("consolidation", "consolidate", "generation"),
    ):
        connection.execute(
            "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,source_id,"
            "source_version,kind,schema_version,status,stage_state,created_at) "
            "VALUES (%s,%s,%s,'reader','session-00',1,%s,1,'failed_terminal',%s,'2026-02-01')",
            (
                authority.tenant_id,
                authority.user_id,
                operation_id,
                kind,
                Jsonb({"parse_coverage": "partial", "error": "failed"}),
            ),
        )
        connection.execute(
            "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
            "input_digest,generation_id,status,attempts_remaining) "
            "VALUES (%s,%s,%s,%s,'index',%s,%s,'failed_terminal',1)",
            (
                authority.tenant_id,
                authority.user_id,
                operation_id,
                operation_id,
                b"x" * 32,
                generation,
            ),
        )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)
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
    assert repository.user_state() == {
        "index_generation_id": "generation",
        "policy_epoch": 0,
        "parse_partial": True,
        "readiness": "partial",
    }
    connection.execute(
        "UPDATE operations SET status='succeeded',stage_state='{}'::jsonb "
        "WHERE tenant_id=%s AND user_id=%s AND operation_id='wrong-generation'",
        (authority.tenant_id, authority.user_id),
    )
    assert repository.user_state()["readiness"] == "ready"

    def work_reads(node):
        own = (
            (
                node.get("Actual Rows", 0)
                + node.get("Rows Removed by Filter", 0)
                + node.get("Rows Removed by Index Recheck", 0)
            )
            * node.get("Actual Loops", 0)
            if node.get("Relation Name") == "work_units"
            else 0
        )
        return own + sum(work_reads(child) for child in node.get("Plans", ()))

    # Work proportional to the history; repeating all work per source is quadratic.
    assert work_reads(plans[0]) <= source_count * 4


def test_event_window_only_qualifies_temporal_candidates(recall_database):
    from datetime import UTC, datetime

    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    for node_id in ("a_hidden", "a_reference", "b_boundary", "c_event"):
        add_fact(connection, authority, node_id, "apple planting")
        date = datetime(2026, 2 if node_id == "b_boundary" else 1, 1, tzinfo=UTC)
        connection.execute(
            "INSERT INTO unit_times(tenant_id,user_id,annotation_id,unit_id,role,"
            "time_span,detail) "
            "VALUES (%s,%s,%s,%s,%s,%s,'{}')",
            (
                authority.tenant_id,
                authority.user_id,
                node_id,
                node_id,
                "date_reference" if node_id == "a_reference" else "event",
                Range(date, date, "[]"),
            ),
        )
    connection.execute(
        "UPDATE sources SET status='deleted' WHERE tenant_id=%s AND source_id='a_hidden'",
        (authority.tenant_id,),
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    request = SearchRequest(
        query="apple",
        time_basis="event",
        time_window={"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
    )
    repository = RecallRepository(
        connection, authority, config, deadline=time.monotonic() + 5, request=request
    )

    assert [row["unit_id"] for row in repository.lexical("apple", 1)] == ["a_reference"]
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    assert {row["unit_id"] for row in repository.dense(vector, 3)} == {
        "a_reference",
        "b_boundary",
        "c_event",
    }
    assert [row["unit_id"] for row in repository.hydrate(("b_boundary",))] == ["b_boundary"]
    assert [
        node.node_id
        for node in repository.temporal_nodes(("a_reference", "b_boundary", "c_event"), vector)
    ] == ["c_event"]
    assert [node.node_id for node in repository.temporal_candidates(vector, 1)] == ["c_event"]
    connection.execute(
        "UPDATE sources SET status='deleted' WHERE tenant_id=%s AND source_id='c_event'",
        (authority.tenant_id,),
    )
    assert repository.hydrate(("c_event",)) == []
    assert repository.temporal_candidates(vector, 1) == ()


def add_insight(connection, authority, unit_id, support_ids):
    values = (authority.tenant_id, authority.user_id, unit_id)
    connection.execute(
        "INSERT INTO insights(tenant_id,user_id,insight_id,revision,synthesis_text,input_digest,"
        "recipe_id,generation_id,visible_from) "
        "VALUES (%s,%s,%s,1,'Shared plan',%s,'test','generation',1)",
        (*values, b"i" * 32),
    )
    for support_id in support_ids:
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,%s,1,%s,1)", (*values, support_id)
        )
    dimensions = connection.execute("SELECT embedding_dimensions FROM index_settings").fetchone()[0]
    connection.execute(
        "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,"
        "insight_id,insight_revision,"
        "generation_id,content_text,locator,embedding,lexical_text) "
        "VALUES (%s,%s,%s,'insight','memory',%s,1,'generation',"
        "'Shared plan','{}',%s::vector,'shared plan')",
        (*values, unit_id, str([-1.0] + [0.0] * (dimensions - 1))),
    )


def test_a_fact_with_withdrawn_support_does_not_survive_on_its_other_support(recall_database):
    connection, authority = recall_database
    bind_user(connection.cursor(), authority)
    add_fact(connection, authority, "shared", "Shared wording")
    add_fact(connection, authority, "withdrawn", "Withdrawn wording")
    add_fact(connection, authority, "independent", "Independent wording")
    connection.execute(
        "INSERT INTO support(tenant_id,user_id,memory_id,evidence_id,passage_id,"
        "support_kind,span) VALUES (%s,%s,'shared','withdrawn','withdrawn','quote','{}')",
        (authority.tenant_id, authority.user_id),
    )

    before = connection.execute(
        f"WITH eligible AS ({ELIGIBLE_UNITS}) SELECT unit_id FROM eligible "
        "WHERE kind='fact' ORDER BY unit_id"
    ).fetchall()
    assert [row[0] for row in before] == ["independent", "shared", "withdrawn"]
    connection.execute(
        "UPDATE sources SET status='deleting',active_version=NULL WHERE tenant_id=%s "
        "AND user_id=%s AND source_id='withdrawn'",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "UPDATE source_versions SET status='excluded' WHERE tenant_id=%s AND user_id=%s "
        "AND source_id='withdrawn'",
        (authority.tenant_id, authority.user_id),
    )

    after = connection.execute(
        f"WITH eligible AS ({ELIGIBLE_UNITS}) SELECT unit_id FROM eligible "
        "WHERE kind='fact' ORDER BY unit_id"
    ).fetchall()
    assert [row[0] for row in after] == ["independent"]


def test_insight_requires_current_immutable_support_revision(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "a", "First support")
    add_fact(connection, authority, "b", "Second support")
    add_insight(connection, authority, "parent", ("a", "b"))
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)
    hydrated = repository.hydrate(("parent",))
    assert [row["unit_id"] for row in hydrated] == ["parent"]
    assert hydrated[0]["subtype"] == "general_insights"
    assert hydrated[0]["supports"] == [
        {"memory_id": "a", "support_revision": 1},
        {"memory_id": "b", "support_revision": 1},
    ]
    connection.execute(
        "UPDATE insight_support SET support_revision=2 WHERE tenant_id=%s AND memory_id='a'",
        (authority.tenant_id,),
    )
    assert repository.hydrate(("parent",)) == []


def test_hydrated_insight_orders_shared_passage_support_by_memory_id(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "z", "z claim")
    add_fact(connection, authority, "a", "a claim")
    connection.execute(
        "INSERT INTO support(tenant_id,user_id,memory_id,evidence_id,passage_id,"
        "support_kind,span) VALUES (%s,%s,'a','z','z','quote',%s)",
        (authority.tenant_id, authority.user_id, Jsonb({"quote": " c"})),
    )
    add_insight(connection, authority, "parent", ("z", "a"))
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    citations = repository.hydrate(("parent",))[0]["citations"]
    assert [(item["source_id"], item["passage_id"], item["span"]) for item in citations] == [
        ("a", "a", {"quote": "a claim"}),
        ("z", "z", {"quote": " c"}),
        ("z", "z", {"quote": "z claim"}),
    ]


def test_recall_planning_settings_are_transaction_local(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL jit=on")
    connection.execute("SET LOCAL plan_cache_mode=force_generic_plan")
    with connection.transaction(force_rollback=True):
        RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)
        assert connection.execute("SHOW jit").fetchone()[0] == "off"
        assert connection.execute("SHOW plan_cache_mode").fetchone()[0] == "force_custom_plan"
    assert connection.execute("SHOW jit").fetchone()[0] == "on"
    assert connection.execute("SHOW plan_cache_mode").fetchone()[0] == "force_generic_plan"


@pytest.mark.parametrize("mode", ["normal"])
def test_normal_follows_causal_direction_without_expanding_l2_siblings(recall_database, mode):
    from oam.retrieval.service import RecallService

    connection, authority = recall_database
    for node in ("move", "rent", "job", "other", "reverse", "cheap"):
        add_fact(
            connection,
            authority,
            node,
            "anchorword" if node == "move" else node,
            direction=1.0 if node == "move" else -1.0,
            with_original=True,
        )
    for source, target in (
        ("move", "rent"),
        ("rent", "job"),
        ("job", "other"),
        ("reverse", "move"),
    ):
        connection.execute(
            "INSERT INTO unit_links VALUES (%s,%s,%s,%s,'caused_by',1)",
            (authority.tenant_id, authority.user_id, source, target),
        )
    add_insight(connection, authority, "plan", ("move", "cheap"))
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    tier = getattr(config.search, mode).model_copy(
        update={"channel_candidates": 1, "causal_graph": True}
    )
    config = config.model_copy(update={"search": config.search.model_copy(update={mode: tier})})
    service = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="generation",
    )

    result = service.search(
        authority,
        SearchRequest(
            query="anchorword",
            recall_level=mode,
            include={"chunks": False},
        ),
    )

    ids = {item["unit_id"] for item in result["items"]}
    assert ids == {"move", "rent", "job"}
    assert "graph" in result["diagnostics"]["executed_stages"]
    assert "rerank" in result["diagnostics"]["omitted_stages"]
    assert result["diagnostics"]["degraded"] == []


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_temporal_sql_discovers_six_hops_and_rejects_hidden_bridge(recall_database, mode):
    from datetime import UTC, datetime

    from oam.retrieval.service import RecallService

    connection, authority = recall_database
    instant = datetime(2026, 1, 6, tzinfo=UTC)
    for index in range(7):
        node = f"node{index}"
        add_fact(
            connection,
            authority,
            node,
            "anchorword" if index == 0 else f"Linked event {index}",
            direction=1 if index == 0 else 0.2,
            with_original=True,
        )
        connection.execute(
            "INSERT INTO unit_times VALUES (%s,%s,%s,%s,'event',%s,'{}')",
            (authority.tenant_id, authority.user_id, node, node, Range(instant, instant, "[]")),
        )
        if index:
            previous = f"node{index - 1}"
            connection.execute(
                "INSERT INTO temporal_links VALUES (%s,%s,'event',%s,%s,%s,%s,0,1)",
                (authority.tenant_id, authority.user_id, previous, node, previous, node),
            )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    # Isolate temporal traversal from the separately tested exchange-selection gate.
    tier = getattr(config.search, mode).model_copy(
        update={"channel_candidates": 1, "partial_match_gate": "off"}
    )
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={mode: tier, "graph_seed_max": 1, "temporal_seed_max": 1}
            )
        }
    )
    service = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="generation",
    )
    request = (
        SearchRequest(
            query="last week what I did",
            recall_level="fast",
            reference_time="2026-01-14T12:00:00Z",
            include={"chunks": False},
        )
        if mode == "fast"
        else SearchRequest(
            query="anchorword",
            recall_level="normal",
            time_basis="event",
            time_window={"start": "2026-01-01T00:00:00Z", "end": "2026-01-11T00:00:00Z"},
            include={"chunks": False},
        )
    )

    result = service.search(authority, request)

    assert {item["unit_id"] for item in result["items"]} == {f"node{i}" for i in range(7)}
    sixth = next(item for item in result["items"] if item["unit_id"] == "node6")
    assert sixth["paths"][0]["nodes"] == [f"node{i}" for i in range(7)]
    assert len(sixth["paths"][0]["edge_ids"]) == 6
    if mode == "fast":
        assert result["diagnostics"]["query_time"]["status"] == "inferred"
        assert "temporal" in result["diagnostics"]["executed_stages"]
        assert result["diagnostics"]["generative_calls"] == 0
    connection.execute(
        "UPDATE evidence SET learning_eligible=false WHERE tenant_id=%s AND evidence_id='node2'",
        (authority.tenant_id,),
    )
    hidden = service.search(authority, request)
    assert {item["unit_id"] for item in hidden["items"]} == {"node0", "node1"}


@pytest.mark.parametrize("allow_partial", [False, True])
def test_configured_reranker_failure_obeys_partial_policy(recall_database, allow_partial):
    from oam.retrieval.service import RecallService, RecallUnavailable

    connection, authority = recall_database
    add_fact(
        connection,
        authority,
        "apple",
        "Apple orchard",
        with_original=True,
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    from oam.config import ModelsConfig

    models_config = ModelsConfig.model_validate(
        {
            "reranker": {
                "protocol": "openai_compatible",
                "model": "synthetic-ranker",
                "endpoint_env": "SYNTHETIC_URL",
                "capabilities": ["rerank"],
            },
        }
    )
    config = config.model_copy(update={"models": models_config})

    class FailingReranker(FixedModels):
        def rerank(self, query, documents, *, timeout_seconds):
            raise RuntimeError("synthetic scoring failure")

    service = RecallService(
        config,
        TransactionPool(connection),
        FailingReranker(config.index.embedding_dimensions),
        generation_id="generation",
    )
    request = SearchRequest(
        query="apple",
        recall_level="normal",
        allow_partial=allow_partial,
        include={"chunks": False},
    )
    if not allow_partial:
        with pytest.raises(RecallUnavailable, match="rerank"):
            service.search(authority, request)
    else:
        result = service.search(authority, request)
        assert result["items"][0]["unit_id"] == "apple"
        assert result["diagnostics"]["degraded"] == ["rerank"]


@pytest.mark.parametrize("mode", ["normal"])
def test_configured_reranker_scores_once_and_controls_final_order(recall_database, mode):
    from oam.config import ModelsConfig
    from oam.models.runtime import RerankResult
    from oam.retrieval.service import RecallService

    connection, authority = recall_database
    add_fact(
        connection,
        authority,
        "a",
        "Apple orchard A",
        with_original=True,
    )
    add_fact(
        connection,
        authority,
        "b",
        "Apple orchard B",
        with_original=True,
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={
            "models": ModelsConfig.model_validate(
                {
                    "reranker": {
                        "protocol": "openai_compatible",
                        "model": "synthetic-ranker",
                        "endpoint_env": "SYNTHETIC_URL",
                        "capabilities": ["rerank"],
                    },
                }
            )
        }
    )

    class ScoringModels(FixedModels):
        calls = 0

        def rerank(self, query, documents, *, timeout_seconds):
            self.calls += 1
            assert query == "apple" and documents == (
                "Apple orchard A",
                "Apple orchard B",
                "Apple orchard A",
                "Apple orchard B",
            )
            return RerankResult(scores=(0.2, 0.9, 0.1, 0.1), usage=Usage())

    models = ScoringModels(config.index.embedding_dimensions)
    service = RecallService(config, TransactionPool(connection), models, generation_id="generation")
    result = service.search(
        authority,
        SearchRequest(
            query="apple",
            recall_level=mode,
            include={"chunks": False},
        ),
    )
    assert [item["unit_id"] for item in result["items"]] == ["b", "a"]
    assert models.calls == 1
    assert "rerank" in result["diagnostics"]["executed_stages"]


def test_failed_embedding_preserves_completed_text_arm_when_partial_allowed(recall_database):
    from oam.retrieval.service import RecallService, RecallUnavailable

    connection, authority = recall_database
    add_fact(
        connection,
        authority,
        "apple",
        "Apple orchard",
        with_original=True,
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config

    class FailedEmbedding:
        def embed(self, *args, **kwargs):
            raise RuntimeError("synthetic embedding unavailable")

    service = RecallService(
        config, TransactionPool(connection), FailedEmbedding(), generation_id="generation"
    )

    result = service.search(
        authority,
        SearchRequest(
            query="apple",
            allow_partial=True,
            include={"chunks": False},
        ),
    )

    assert result["items"][0]["unit_id"] == "apple"
    assert result["diagnostics"]["degraded"] == ["embedding"]
    with pytest.raises(RecallUnavailable, match="embedding"):
        service.search(
            authority,
            SearchRequest(
                query="apple",
                include={"chunks": False},
            ),
        )


def test_embedding_overlaps_real_lexical_read_and_both_drain(recall_database, monkeypatch):
    from threading import Event

    from oam.retrieval.repository import RecallRepository
    from oam.retrieval.service import RecallService

    connection, authority = recall_database
    add_fact(
        connection,
        authority,
        "apple",
        "Apple orchard",
        with_original=True,
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    text_started, embedding_started, text_finished = Event(), Event(), Event()
    original = RecallRepository.lexical

    def lexical(repository, query, limit):
        text_started.set()
        assert embedding_started.wait(1), "embedding did not overlap lexical retrieval"
        result = original(repository, query, limit)
        text_finished.set()
        return result

    monkeypatch.setattr(RecallRepository, "lexical", lexical)

    class OverlappingEmbedding(FixedModels):
        def embed(self, texts, *, timeout_seconds):
            embedding_started.set()
            assert text_started.wait(1), "lexical retrieval never started"
            assert text_finished.wait(1), "lexical SQL did not complete during embedding"
            return super().embed(texts, timeout_seconds=timeout_seconds)

    service = RecallService(
        config,
        TransactionPool(connection),
        OverlappingEmbedding(config.index.embedding_dimensions),
        generation_id="generation",
    )

    result = service.search(
        authority,
        SearchRequest(
            query="apple",
            include={"chunks": False},
        ),
    )

    assert result["items"][0]["unit_id"] == "apple"
    assert text_finished.is_set()


def test_graph_sql_filters_hidden_neighbors_before_each_direction_cap(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    for node, eligible in (
        ("seed", True),
        ("a_hidden", False),
        ("z_visible", True),
        ("incoming", True),
    ):
        add_fact(connection, authority, node, node + " garden", eligible=eligible)
    from oam.indexing.entities import EntityRepository

    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    entities = EntityRepository(
        connection, config, generation_id="generation", key=(authority.tenant_id, authority.user_id)
    )
    for node in ("seed", "a_hidden", "z_visible"):
        entities.publish_fact(
            node,
            ("garden",),
            node + " garden",
            references=()
            if node == "seed"
            else ({"name": "garden", "reference_id": "seed", "quote": node + " garden"},),
        )
    for source, target, weight in (("a_hidden", "seed", 1.0), ("incoming", "seed", 0.9)):
        connection.execute(
            "INSERT INTO unit_links VALUES (%s,%s,%s,%s,'semantic',%s)",
            (authority.tenant_id, authority.user_id, source, target, weight),
        )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={"links_per_seed": 1, "entity_candidates_per_entity": 1}
            )
        }
    )
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    statements = []
    execute = repository._execute

    def record_statement(sql, parameters=()):
        statements.append(sql)
        return execute(sql, parameters)

    repository._execute = record_statement

    ranks, paths = repository.ordinary(("seed",), vector)

    assert len(statements) == 1
    assert set(ranks) == {"incoming", "z_visible"}
    assert paths["incoming"][0]["relation"] == "semantic"
    assert (
        paths["z_visible"][0]["entity"]
        == connection.execute(
            "SELECT entity_id FROM unit_entities WHERE unit_id='seed'"
        ).fetchone()[0]
    )


def test_temporal_witness_role_is_checked_before_neighbor_cap(recall_database):
    from datetime import UTC, datetime

    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    instant = datetime(2026, 1, 6, tzinfo=UTC)
    for node in ("a_seed", "b_wrong", "c_valid", "d_valid", "aa_seed"):
        add_fact(connection, authority, node, node)
        connection.execute(
            "INSERT INTO unit_times VALUES (%s,%s,%s,%s,'event',%s,'{}')",
            (authority.tenant_id, authority.user_id, node, node, Range(instant, instant, "[]")),
        )
    connection.execute(
        "INSERT INTO unit_times VALUES "
        "(%s,%s,'wrong-reference','b_wrong','date_reference',%s,'{}')",
        (authority.tenant_id, authority.user_id, Range(instant, instant, "[]")),
    )
    for source, target, witness, weight in (
        ("a_seed", "b_wrong", "wrong-reference", 1.0),
        ("a_seed", "c_valid", "c_valid", 0.9),
        ("aa_seed", "b_wrong", "wrong-reference", 0.2),
        ("aa_seed", "d_valid", "d_valid", 0.1),
    ):
        connection.execute(
            "INSERT INTO temporal_links VALUES (%s,%s,'event',%s,%s,%s,%s,0,%s)",
            (authority.tenant_id, authority.user_id, source, target, source, witness, weight),
        )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    request = SearchRequest(
        query="seed",
        time_basis="event",
        time_window={"start": "2026-01-01T00:00:00Z", "end": "2026-01-11T00:00:00Z"},
    )
    repository = RecallRepository(
        connection, authority, config, request=request, deadline=time.monotonic() + 5
    )

    neighbors = repository.temporal_neighbors(
        "a_seed", (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1), 1
    )

    assert [edge.target.node_id for edge in neighbors] == ["c_valid"]
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    sources = ("a_seed", "aa_seed")
    batched = repository.temporal_neighbors_batch(sources, vector, 1)
    assert batched == {
        source: repository.temporal_neighbors(source, vector, 1) for source in sources
    }
    assert {
        source: [edge.target.node_id for edge in edges] for source, edges in batched.items()
    } == {"a_seed": ["c_valid"], "aa_seed": ["d_valid"]}


def test_strict_window_requires_every_insight_support_to_match(recall_database):
    from datetime import UTC, datetime

    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    for node, month in (("a", 1), ("b", 2)):
        add_fact(connection, authority, node, node)
        date = datetime(2026, month, 6, tzinfo=UTC)
        connection.execute(
            "INSERT INTO unit_times VALUES (%s,%s,%s,%s,'event',%s,'{}')",
            (authority.tenant_id, authority.user_id, node, node, Range(date, date, "[]")),
        )
    add_insight(connection, authority, "plan", ("a", "b"))
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    request = SearchRequest(
        query="plan",
        time_basis="event",
        time_window={"start": "2026-01-01T00:00:00Z", "end": "2026-02-01T00:00:00Z"},
    )
    repository = RecallRepository(
        connection, authority, config, request=request, deadline=time.monotonic() + 5
    )

    assert [row["unit_id"] for row in repository.hydrate(("plan",))] == ["plan"]
    assert [row["unit_id"] for row in repository.hydrate(("a", "b"))] == ["a", "b"]
    assert repository.temporal_matches(("a", "b", "plan")) == ("a",)
    connection.execute(
        "UPDATE unit_times SET time_span=tstzrange('2026-01-06','2026-01-06','[]') "
        "WHERE tenant_id=%s AND unit_id='b'",
        (authority.tenant_id,),
    )
    assert repository.temporal_matches(("a", "b", "plan")) == ("a", "b", "plan")


def test_expired_host_deadline_dispatches_no_model_request(recall_database):
    from datetime import UTC, datetime, timedelta

    from oam.retrieval.service import RecallService

    connection, authority = recall_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    service = RecallService(
        config, TransactionPool(connection), object(), generation_id="generation"
    )
    with pytest.raises(TimeoutError, match="deadline"):
        service.search(
            authority,
            SearchRequest(
                query="apple",
                include={"chunks": False},
            ),
            deadline_at=datetime.now(UTC) - timedelta(seconds=1),
        )


def test_point_time_basis_is_visible_in_returned_evidence(recall_database):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "apple", "Apple orchard")
    connection.execute(
        "UPDATE search_units SET mentioned_at='2026-01-03T00:00:00Z' WHERE tenant_id=%s",
        (authority.tenant_id,),
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)

    row = repository.hydrate(("apple",))[0]

    assert {"role": "mentioned", "at": "2026-01-03T00:00:00+00:00"} in row["time_evidence"]


@pytest.mark.parametrize(("mode", "limit"), [("fast", 64), ("normal", 1)])
def test_exact_dense_ties_use_stable_identity_after_similarity(recall_database, mode, limit):
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    add_fact(connection, authority, "z_first_inserted", "Orchard maintenance report.")
    add_fact(connection, authority, "a_later_inserted", "A separate orchard report.")
    add_fact(connection, authority, "0_less_relevant", "A related orchard topic.", direction=0.8)
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    repository = RecallRepository(
        connection,
        authority,
        config,
        deadline=time.monotonic() + 5,
        request=SearchRequest(query="orchard", recall_level=mode),
    )
    expected = ["a_later_inserted", "z_first_inserted", "0_less_relevant"]
    assert [row["unit_id"] for row in repository.dense(vector, limit)] == expected[:limit]


def test_hnsw_executes_and_matches_exact_scoped_neighbors(recall_database, monkeypatch):
    from oam.db.context import bind_user
    from oam.retrieval.repository import RecallRepository

    connection, authority = recall_database
    count = 512
    for index in range(count):
        add_fact(
            connection,
            authority,
            f"candidate{index:04d}",
            f"Synthetic point {index}",
            direction=0.2 + index / count * 0.7,
        )
    for table in (
        "search_units",
        "memories",
        "support",
        "passages",
        "evidence",
        "sources",
        "source_versions",
    ):
        connection.execute(f"ANALYZE {table}")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    connection.execute("SET LOCAL ROLE oam_api")
    bind_user(connection.cursor(), authority)
    connection.execute("SET LOCAL hnsw.iterative_scan='strict_order'")
    repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 10)
    calls = []
    original = repository._execute

    def capture(sql, parameters, **options):
        calls.append((sql, parameters))
        return original(sql, parameters, **options)

    monkeypatch.setattr(repository, "_execute", capture)
    approximate = repository.dense(vector, 5)
    query, parameters = calls[-1]
    plan = connection.execute("EXPLAIN (ANALYZE,FORMAT JSON) " + query, parameters).fetchone()[0][
        0
    ]["Plan"]

    def nodes(node):
        yield node
        for child in node.get("Plans", ()):
            yield from nodes(child)

    executed = [
        node
        for node in nodes(plan)
        if node.get("Index Name") == "search_units_embedding" and node["Actual Loops"] > 0
    ]
    assert executed, [
        (node["Node Type"], node.get("Index Name"), node.get("Actual Rows")) for node in nodes(plan)
    ]
    connection.execute("SET LOCAL enable_seqscan=on")
    connection.execute("SET LOCAL enable_indexscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    exact = repository.dense(vector, 5)
    assert (
        [row["unit_id"] for row in approximate]
        == [row["unit_id"] for row in exact]
        == [f"candidate{index:04d}" for index in range(count - 1, count - 6, -1)]
    )


@pytest.mark.parametrize("pipeline_supported", (False, True))
@pytest.mark.parametrize(
    ("query", "error"),
    (
        ("SELECT 1/0", psycopg.errors.DivisionByZero),
        ("SELECT pg_sleep(0.25)", psycopg.errors.QueryCanceled),
    ),
)
def test_recall_query_failure_and_deadline_leave_connection_usable(
    recall_database, monkeypatch, pipeline_supported, query, error
):
    from oam.retrieval.repository import RecallRepository

    if pipeline_supported and not psycopg.Pipeline.is_supported():
        pytest.skip("pipeline transport requires a supported client library")
    monkeypatch.setattr(psycopg.Pipeline, "is_supported", staticmethod(lambda: pipeline_supported))
    connection, authority = recall_database
    add_fact(connection, authority, "visible", "A supported current fact.")
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SELECT set_config('oam.user_id','outer-scope',true)")
    before = connection.execute(
        "SELECT current_setting('statement_timeout'),current_setting('oam.user_id',true)"
    ).fetchone()
    with pytest.raises(error), connection.transaction():
        repository = RecallRepository(
            connection, authority, config, deadline=time.monotonic() + 0.05
        )
        timeout = repository._execute("SELECT current_setting('statement_timeout') AS value")[0]
        assert timeout["value"] != "0"
        repository._execute(query)
    assert connection.pgconn.pipeline_status == psycopg.pq.PipelineStatus.OFF
    assert (
        connection.execute(
            "SELECT current_setting('statement_timeout'),current_setting('oam.user_id',true)"
        ).fetchone()
        == before
    )
    with connection.transaction():
        repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 5)
        assert [row["unit_id"] for row in repository.hydrate(("visible",))] == ["visible"]


class TransactionPool:
    def __init__(self, connection):
        self._connection = connection

    @contextmanager
    def connection(self, *, timeout):
        assert timeout > 0
        yield self._connection


class FixedModels:
    def __init__(self, dimensions):
        self.dimensions = dimensions

    def embed(self, texts, *, timeout_seconds):
        assert timeout_seconds > 0
        return EmbeddingResult(
            vectors=((1.0,) + (0.0,) * (self.dimensions - 1),),
            usage=Usage(input_tokens=1, output_tokens=0, total_tokens=1, coverage="complete"),
        )


def test_empty_user_recall_returns_empty_result(recall_database):
    from oam.retrieval.service import RecallService

    connection, authority = recall_database
    connection.execute(
        "UPDATE users SET index_generation_id=NULL WHERE tenant_id=%s AND user_id=%s",
        (authority.tenant_id, authority.user_id),
    )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    connection.execute("SET LOCAL ROLE oam_api")
    result = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="current",
    ).search(authority, SearchRequest(query="apple", recall_level="fast"))
    assert result["items"] == []
    assert result["context"] == ""
