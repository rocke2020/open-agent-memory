"""Enabling consolidation reuses completed L1 through the real operator/worker path."""

import json

import httpx
import psycopg
import pytest
from api_test_app import create_api_only_app
from psycopg.conninfo import make_conninfo
from test_core_http import SyntheticIndexModels, process_source, use_created_user_key
from test_core_http import core_database as core_database

from oam.cli import main
from oam.config import load_config
from oam.models.runtime import ModelOutputTooLongError, Usage
from oam.workers.index_source import process_one

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("budget_exhausted", [False, True])
async def test_l2_index_reuses_original_l1_and_does_not_duplicate_jobs(
    core_database, tmp_path, monkeypatch, capsys, budget_exhausted
):
    dsn, tenant, headers = core_database
    off_path = tmp_path / "off.yaml"
    off_path.write_text(
        "index:\n  insights_enabled: false\n"
        + ("" if budget_exhausted else "  generation_token_budget: 200000\n")
    )
    on_path = tmp_path / "on.yaml"
    on_path.write_text("{}\n")
    off = load_config(off_path).config
    on = load_config(on_path).config
    if budget_exhausted:
        on = on.model_copy(
            update={"index": on.index.model_copy(update={"generation_token_budget": 1})}
        )
    models = SyntheticIndexModels()
    queue = make_conninfo(dsn, options="-c role=oam_queue")
    content = make_conninfo(dsn, options="-c role=oam_content")
    app = create_api_only_app(off_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        accepted = await client.post(
            "/v1/retain",
            json={
                "input_type": "interaction",
                "user_id": "scope",
                "messages": [
                    {
                        "role": "user",
                        "content": text,
                        "learning_eligible": True,
                        "event_time": "2026-11-03T00:00:00Z",
                    }
                    for text in (
                        "I prefer jasmine tea after dinner.",
                        "I choose jasmine tea for evening reading.",
                    )
                ],
            },
            headers={"Idempotency-Key": "existing-l1"},
        )
        assert accepted.status_code == 202, accepted.text
        process_source(off, queue, content, models=models, worker_id="l1-only")
        with psycopg.connect(dsn) as connection:
            facts_before = connection.execute(
                "SELECT memory_id,generation_id,canonical_claim FROM memories ORDER BY memory_id"
            ).fetchall()
            checkpoints_before = connection.execute(
                "SELECT stage_key,input_digest,output::text FROM index_stage_results "
                "ORDER BY stage_key,input_digest"
            ).fetchall()
            assert (
                connection.execute(
                    "SELECT count(*) FROM operations WHERE kind='consolidate'"
                ).fetchone()[0]
                == 0
            )
        assert len(facts_before) == 2
        extraction_calls = models.generation_calls.count("extraction")
        monkeypatch.setenv(on.database.migration_dsn_env, dsn)
        monkeypatch.setattr("oam.cli.load_dotenv", lambda *args, **kwargs: False)
        command = [
            "l2-index",
            "--config",
            str(on_path),
            "--tenant-id",
            tenant,
            "--user-id",
            "scope",
            "--limit",
            "1",
        ]

        capsys.readouterr()
        assert main([*command[:-1], "0"]) == 2
        capsys.readouterr()
        monkeypatch.setenv(on.database.migration_dsn_env, content)
        assert main(command) == 2
        capsys.readouterr()
        monkeypatch.setenv(on.database.migration_dsn_env, dsn)
        assert main(command) == 0
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["scheduled_count"] == 1
        assert main(command) == 0
        assert json.loads(capsys.readouterr().out)["scheduled_count"] == 0
        if budget_exhausted:
            with pytest.raises(ValueError, match="source generation token budget"):
                process_one(on, queue, content, models=models, worker_id="l2-only")
        else:
            assert process_one(on, queue, content, models=models, worker_id="l2-only")

        with psycopg.connect(dsn) as connection:
            assert (
                connection.execute(
                    "SELECT memory_id,generation_id,canonical_claim FROM memories "
                    "ORDER BY memory_id"
                ).fetchall()
                == facts_before
            )
            assert (
                connection.execute(
                    "SELECT stage_key,input_digest,output::text FROM index_stage_results "
                    "WHERE stage_key NOT LIKE 'insight%%' ORDER BY stage_key,input_digest"
                ).fetchall()
                == checkpoints_before
            )
            assert connection.execute(
                "SELECT count(*) FROM insights WHERE visible_to IS NULL"
            ).fetchone()[0] == (0 if budget_exhausted else 1)
            operation = connection.execute(
                "SELECT status,stage_state->>'insights_ready' FROM operations "
                "WHERE operation_id=%s",
                (receipt["scheduled"][0],),
            ).fetchone()
            if budget_exhausted:
                assert operation[0] == "partial_failed"
                assert models.generation_calls.count("insights") == 0
                assert (
                    connection.execute(
                        "SELECT stage_state->'l2_examined_ids' FROM operations "
                        "WHERE operation_id=%s",
                        (receipt["scheduled"][0],),
                    ).fetchone()[0]
                    is None
                )
            else:
                assert operation == ("succeeded", "true")
                budget = connection.execute(
                    "SELECT stage_state->'index_generation_budget' FROM operations "
                    "WHERE operation_id=%s",
                    (accepted.json()["operation_id"],),
                ).fetchone()[0]
                assert budget["limit"] == 200000
                assert 30 < budget["reserved_upper_tokens"] <= 200000
        assert models.generation_calls.count("extraction") == extraction_calls


@pytest.mark.parametrize("child_failure", ["budget", "connect"])
async def test_split_retry_reserves_each_call_and_retains_known_failed_usage(
    core_database, tmp_path, child_failure
):
    dsn, _, headers = core_database
    path = tmp_path / "budget.yaml"
    budget = 100000 if child_failure == "budget" else 200000
    path.write_text(f"index:\n  generation_token_budget: {budget}\n")
    config = load_config(path).config

    class OverflowModels(SyntheticIndexModels):
        def generate(self, role, **kwargs):
            self.generation_calls.append(role)
            if child_failure == "connect" and len(self.generation_calls) > 1:
                raise httpx.ConnectTimeout("synthetic child failed before dispatch")
            raise ModelOutputTooLongError(
                "synthetic output overflow",
                usage=Usage(
                    input_tokens=20, output_tokens=10, total_tokens=30, coverage="complete"
                ),
            )

    models = OverflowModels()
    app = create_api_only_app(path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        accepted = await client.post(
            "/v1/retain",
            json={
                "input_type": "interaction",
                "user_id": "scope",
                "messages": [
                    {
                        "role": "user",
                        "content": "I prepare jasmine tea for evening reading. " * 50,
                        "learning_eligible": True,
                        "event_time": "2026-11-03T00:00:00Z",
                    }
                ],
            },
            headers={"Idempotency-Key": "split-budget"},
        )
        assert accepted.status_code == 202, accepted.text
        expected = ValueError if child_failure == "budget" else httpx.ConnectTimeout
        with pytest.raises(expected):
            process_one(
                config,
                make_conninfo(dsn, options="-c role=oam_queue"),
                make_conninfo(dsn, options="-c role=oam_content"),
                models=models,
                worker_id="split-budget",
            )
    assert models.generation_calls == ["extraction"] * (1 if child_failure == "budget" else 2)
    with psycopg.connect(dsn) as connection:
        disposition, usage = connection.execute(
            "SELECT a.disposition,a.usage FROM attempts a JOIN work_units w "
            "USING(tenant_id,user_id,work_unit_id) WHERE w.operation_id=%s",
            (accepted.json()["operation_id"],),
        ).fetchone()
        assert disposition == "failed"
        assert usage["total_tokens"] == 30
        assert usage["coverage"] == ("complete" if child_failure == "budget" else "partial")


async def test_existing_l1_backfill_keeps_each_generation_self_contained(core_database, tmp_path):
    from oam.auth.credentials import AuthorizedUser
    from oam.db.context import bind_user
    from oam.db.eligibility import ELIGIBLE_UNITS
    from oam.workers.l2_index import queue_existing_l1

    dsn, tenant, headers = core_database
    path = tmp_path / "generations.yaml"
    path.write_text("index:\n  insights_enabled: false\n")
    off = load_config(path).config
    on = off.model_copy(
        update={
            "index": off.index.model_copy(
                update={
                    "insights_enabled": True,
                    "insight_consolidation_delay_hours": 0.000001,
                }
            )
        }
    )
    models = SyntheticIndexModels()
    queue = make_conninfo(dsn, options="-c role=oam_queue")
    content = make_conninfo(dsn, options="-c role=oam_content")
    app = create_api_only_app(path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client,
    ):
        await use_created_user_key(client, "scope")
        for generation, config in [("historical-generation", off), ("current-generation", on)]:
            # Model persisted historical namespaces without changing either runtime config.
            with psycopg.connect(dsn) as db:
                db.execute(
                    "UPDATE users SET index_generation_id=%s WHERE tenant_id=%s AND user_id=%s",
                    (generation, tenant, "scope"),
                )
            response = await client.post(
                "/v1/retain",
                json={
                    "input_type": "interaction",
                    "user_id": "scope",
                    "messages": [
                        {
                            "role": "user",
                            "content": text,
                            "learning_eligible": True,
                            "event_time": "2026-11-03T00:00:00Z",
                        }
                        for text in (
                            "I prefer concise morning updates.",
                            "I read concise updates after breakfast.",
                        )
                    ],
                },
                headers={"Idempotency-Key": generation},
            )
            assert response.status_code == 202, response.text
            process_source(config, queue, content, models=models, worker_id=generation)
            if config.index.insights_enabled:
                assert process_one(on, queue, content, models=models, worker_id="current-l2")
        receipt = queue_existing_l1(dsn, on, tenant, "scope", 1)
        assert receipt["scheduled_count"] == 1
        assert process_one(on, queue, content, models=models, worker_id="historical-l2")
        with psycopg.connect(dsn) as db:
            bind_user(
                db.cursor(), AuthorizedUser("operator", tenant, "operator", "scope", frozenset())
            )
            current = db.execute(
                f"WITH eligible AS ({ELIGIBLE_UNITS}) "
                "SELECT generation_id FROM eligible WHERE kind='insight' ORDER BY generation_id"
            ).fetchall()
            assert current == [("current-generation",), ("historical-generation",)]
            assert (
                db.execute(
                    "SELECT count(*) FROM insight_support sp JOIN insights i "
                    "USING(tenant_id,user_id,insight_id) JOIN memories m "
                    "USING(tenant_id,user_id,memory_id) WHERE i.generation_id<>m.generation_id"
                ).fetchone()[0]
                == 0
            )
