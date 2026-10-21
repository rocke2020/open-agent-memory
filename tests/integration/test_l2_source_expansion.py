"""Eligible L2 supports restore exact originals through PostgreSQL recall."""

import time
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb
from test_core_http import core_database as core_database
from test_recall_repository import (
    FixedModels,
    TransactionPool,
    add_fact,
    add_insight,
    add_user_passage,
)

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.contracts.search import SearchRequest
from oam.retrieval.repository import RecallRepository
from oam.retrieval.service import RecallService

pytestmark = pytest.mark.integration
ORIGINAL = (
    "Batch duration improved from 40 to 32 ms.\n```python\n"
    "from joblib import Parallel, delayed\n"
    "results = Parallel(n_jobs=4)(delayed(run)(x) for x in batches)\n```"
)


@pytest.fixture
def l2_database(core_database):
    dsn, tenant, _ = core_database
    authority = AuthorizedUser("key", tenant, "reader", "scope", frozenset({"memory:read"}))
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("BEGIN")
        connection.execute(
            "INSERT INTO users(tenant_id,user_id,subject,namespace,lifetime,index_generation_id) "
            "VALUES (%s,'scope','synthetic','test','persistent','generation')",
            (tenant,),
        )
        add_user_passage(connection, authority, "code", ORIGINAL, "2026-09-03T12:00:00Z")
        add_user_passage(connection, authority, "review", "The implementation awaits review.", None)
        for name, text in (
            ("code", "Batch duration improved from 40 to 32 ms."),
            ("review", "The implementation awaits review."),
        ):
            locator = {
                "source_id": name,
                "source_version": 1,
                "message_id": name + "-message",
                "role": "user",
                "passage_index": 3,
            }
            connection.execute(
                "UPDATE passages SET locator=%s WHERE tenant_id=%s AND passage_id=%s",
                (Jsonb(locator), tenant, name),
            )
            connection.execute(
                "UPDATE support SET span=%s WHERE tenant_id=%s AND memory_id=%s",
                (Jsonb({**locator, "quote": text}), tenant, name),
            )
            connection.execute(
                "UPDATE search_units SET locator=%s WHERE tenant_id=%s AND unit_id=%s",
                (Jsonb(locator), tenant, name + "_passage"),
            )
            connection.execute(
                "UPDATE search_units SET content_text=%s WHERE tenant_id=%s AND unit_id=%s",
                (text, tenant, name),
            )
        # A sibling chunk in the same original message must remain outside L2 direct expansion.
        connection.execute(
            "INSERT INTO passages(tenant_id,user_id,passage_id,evidence_id,parse_revision,locator,"
            "normalized_text,generation_id,visible_from) SELECT tenant_id,user_id,'sibling',"
            "evidence_id,parse_revision,locator,'UNRELATED SIBLING','generation',1 FROM passages "
            "WHERE tenant_id=%s AND passage_id='code'",
            (tenant,),
        )
        connection.execute(
            "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,"
            "source_id,source_version,"
            "evidence_id,passage_id,generation_id,content_text,locator,embedding,lexical_text) "
            "SELECT tenant_id,user_id,'sibling','passage',origin,source_id,source_version,"
            "evidence_id,'sibling',generation_id,'UNRELATED SIBLING',locator,embedding,'' "
            "FROM search_units WHERE tenant_id=%s AND unit_id='code_passage'",
            (tenant,),
        )
        add_insight(connection, authority, "insight", ("code", "review"))
        dimensions = connection.execute(
            "SELECT embedding_dimensions FROM index_settings"
        ).fetchone()[0]
        vector = str([-1.0] + [0.0] * (dimensions - 1))
        connection.execute(
            "UPDATE search_units SET embedding=%s::vector WHERE tenant_id=%s", (vector, tenant)
        )
        # Only the Insight faces the query; direct arms cannot independently admit originals.
        connection.execute(
            "UPDATE search_units SET embedding=%s::vector WHERE tenant_id=%s AND kind='insight'",
            (str([1.0] + [0.0] * (dimensions - 1)), tenant),
        )
        connection.execute("COMMIT")
        yield connection, authority


@pytest.mark.parametrize("mode", ["fast", "normal"])
def test_l2_only_direct_candidate_restores_full_code_in_actual_context(l2_database, mode):
    connection, authority = l2_database
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    config = config.model_copy(
        update={
            "search": config.search.model_copy(
                update={
                    mode: getattr(config.search, mode).model_copy(update={"channel_candidates": 1})
                }
            )
        }
    )
    connection.execute("SET ROLE oam_api")
    service = RecallService(
        config,
        TransactionPool(connection),
        FixedModels(config.index.embedding_dimensions),
        generation_id="generation",
    )
    result = service.search(authority, SearchRequest(query="shared plan", recall_level=mode))
    assert ORIGINAL in result["context"]
    assert "UNRELATED SIBLING" not in result["context"]
    assert {item["unit_id"] for item in result["items"]} == {
        "insight",
        "code",
        "review",
        "code_passage",
        "review_passage",
    }
    assert "joblib" not in next(
        item["text"] for item in result["items"] if item["unit_id"] == "code"
    )
    assert "message:code-message chunk:code index:3" in result["context"]
    assert "2026-09-03T12:00:00Z" in result["context"]
    connection.execute("RESET ROLE")
    decisions = connection.execute(
        "SELECT decisions FROM traces WHERE trace_id=%s", (result["trace_id"],)
    ).fetchone()[0]
    assert decisions["arms"]["dense"] == ["insight"]
    assert all(node == "insight" for nodes in decisions["arms"].values() for node in nodes)
    assert decisions["l2_source_expansion"][0]["status"] == "included"


@pytest.mark.parametrize("invalid", ["source", "revision", "generation"])
def test_support_lookup_enforces_whole_insight_eligibility(l2_database, invalid):
    connection, authority = l2_database
    if invalid == "source":
        connection.execute(
            "UPDATE evidence SET learning_eligible=false "
            "WHERE tenant_id=%s AND evidence_id='review'",
            (authority.tenant_id,),
        )
    elif invalid == "revision":
        connection.execute(
            "UPDATE insight_support SET support_revision=2 "
            "WHERE tenant_id=%s AND memory_id='review'",
            (authority.tenant_id,),
        )
    else:
        connection.execute(
            "UPDATE users SET index_generation_id='new' WHERE tenant_id=%s", (authority.tenant_id,)
        )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    with connection.transaction():
        repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 10)
        assert repository.insight_support_units(("insight",), vector, 1) == []


def test_support_lookup_ranks_inside_support_set_and_obeys_existing_cap(l2_database):
    connection, authority = l2_database
    with connection.transaction():
        add_fact(connection, authority, "relevant", "Related implementation", direction=0.8)
        connection.execute(
            "INSERT INTO insight_support VALUES (%s,%s,'insight',1,'relevant',1)",
            (authority.tenant_id, authority.user_id),
        )
    config = load_config(Path(__file__).parents[2] / "configs/oam.yaml").config
    vector = (1.0,) + (0.0,) * (config.index.embedding_dimensions - 1)
    with connection.transaction():
        repository = RecallRepository(connection, authority, config, deadline=time.monotonic() + 10)
        selected = repository.insight_support_units(("insight",), vector, 1)
    assert [row["memory_id"] for row in selected] == ["relevant"]
