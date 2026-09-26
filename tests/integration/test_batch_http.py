"""Step 8.1 public routes over an explicitly isolated PostgreSQL test database."""

from pathlib import Path

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg_pool import ConnectionPool
from test_core_http import core_database as core_database
from test_recall_repository import FixedModels, add_fact

from oam.api.app import create_app
from oam.api.runtime import DatabaseRuntime
from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.models.runtime import ModelResult, Usage

ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.integration


@pytest.mark.parametrize("delegated", [False, True])
async def test_public_batch_and_planned_search_use_current_authorized_index(
    core_database, monkeypatch, tmp_path, delegated
) -> None:
    dsn, tenant, root_headers = core_database
    config_path = ROOT / "configs/oam.yaml"
    if delegated:
        config_path = tmp_path / "planner.yaml"
        config_path.write_text(
            "models:\n"
            "  roles:\n"
            "    planner: local\n"
            "  bindings:\n"
            "    local:\n"
            "      protocol: openai_compatible\n"
            "      model: local-planner\n"
            "      endpoint_env: UNUSED_PLANNER_ENDPOINT\n"
            "      capabilities: [chat]\n",
            encoding="utf-8",
        )
    config = load_config(config_path).config

    class LocalModels(FixedModels):
        def generate(self, role, **kwargs):
            assert role == "planner"
            assert kwargs["max_output_tokens"] >= 1024
            return ModelResult(
                content='{"queries":["apple orchard","pear orchard"]}',
                usage=Usage(
                    input_tokens=12,
                    output_tokens=8,
                    total_tokens=20,
                    coverage="complete",
                ),
            )

    pool = ConnectionPool(
        conninfo=make_conninfo(dsn, options="-c role=oam_api"),
        min_size=1,
        max_size=6,
        open=False,
    )
    runtime = DatabaseRuntime(config, pool, models=LocalModels(config.index.embedding_dimensions))
    monkeypatch.setattr("oam.api.runtime.index_generation", lambda config, env: "generation")
    runtime.open()
    try:
        app = create_app(config_path, runtime=runtime)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            headers=root_headers,
        ) as client:
            created = await client.post("/v1/users", json={"user_id": "lin"})
            assert created.status_code == 201, created.text
            with psycopg.connect(dsn) as connection:
                connection.execute(
                    "UPDATE users SET index_generation_id='generation' "
                    "WHERE tenant_id=%s AND user_id='lin'",
                    (tenant,),
                )
                authority = AuthorizedUser(
                    "key", tenant, "reader", "lin", frozenset({"memory:read"})
                )
                add_fact(connection, authority, "apple", "Lin grows apple trees in an orchard.")
                add_fact(connection, authority, "pear", "Lin grows pear trees in an orchard.")

            client.headers["Authorization"] = f"Bearer {created.json()['secret']}"
            if delegated:
                path = "/v1/users/lin/decompose-and-search"
                body = {
                    "query": "What fruit does Lin grow?",
                    "recall_level": "fast",
                    "profile_sections": [],
                }
            else:
                path = "/v1/users/lin/search-many"
                body = {
                    "queries": ["apple orchard", "pear orchard"],
                    "recall_level": "fast",
                    "profile_sections": [],
                }
            root_response = await client.post(path, json=body, headers=root_headers)
            assert root_response.status_code == 404
            response = await client.post(path, json=body)

        assert response.status_code == 200, response.text
        payload = response.json()
        assert [row["query"] for row in payload["results"]] == [
            "apple orchard",
            "pear orchard",
        ]
        assert all(row["status"] == "ok" for row in payload["results"])
        assert any(item["unit_id"] == "apple" for item in payload["results"][0]["result"]["items"])
        assert any(item["unit_id"] == "pear" for item in payload["results"][1]["result"]["items"])
        assert payload["context_tokens"] <= config.context.default_tokens
        assert all(
            row["result"]["diagnostics"]["generative_calls"] == 0 for row in payload["results"]
        )
        assert payload["diagnostics"]["planner_calls"] == int(delegated)
        if delegated:
            assert payload["planner"]["usage"]["total_tokens"] == 20
    finally:
        runtime.close()
