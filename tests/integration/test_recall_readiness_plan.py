"""Read-only readiness plan gate for an explicitly supplied populated scope."""

import os
import time

import psycopg
import pytest

from oam.auth.credentials import AuthorizedUser
from oam.config import load_config
from oam.paths import default_config_path
from oam.retrieval.repository import RecallRepository


def test_readiness_does_not_repeat_operation_lookup_for_each_source():
    dsn = os.environ.get("OAM_READ_ONLY_TEST_DSN")
    if not dsn:
        pytest.skip("requires an explicit read-only populated-scope DSN")
    tenant = os.environ["OAM_READ_ONLY_TEST_TENANT"]
    user = os.environ["OAM_READ_ONLY_TEST_USER"]
    authority = AuthorizedUser("plan-check", tenant, "plan-check", user, frozenset({"memory:read"}))
    with psycopg.connect(dsn) as connection, connection.transaction():
        connection.execute("SET TRANSACTION READ ONLY")
        assert connection.execute(
            "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False,)
        repository = RecallRepository(
            connection,
            authority,
            load_config(default_config_path()).config,
            deadline=time.monotonic() + 30,
        )
        captured = []
        execute = repository._execute

        def record(query, parameters=(), **kwargs):
            captured.append((query, parameters))
            return execute(query, parameters, **kwargs)

        repository._execute = record
        state = repository.user_state()
        generation = connection.execute(
            "SELECT index_generation_id FROM users WHERE tenant_id=%s AND user_id=%s",
            (tenant, user),
        ).fetchone()[0]
        assert state["index_generation_id"] == generation
        sources = connection.execute(
            "SELECT count(*) FROM sources WHERE tenant_id=%s AND user_id=%s",
            (tenant, user),
        ).fetchone()[0]
        work_units = connection.execute(
            "SELECT count(*) FROM operations o "
            "JOIN work_units w USING (tenant_id,user_id,operation_id) "
            "WHERE o.tenant_id=%s AND o.user_id=%s AND o.kind<>'consolidate'",
            (tenant, user),
        ).fetchone()[0]
        if min(sources, work_units) < 3:
            pytest.skip("scope needs multiple sources and operations to expose repeated lookups")
        query, parameters = captured[0]
        plan = connection.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + query, parameters).fetchone()[
            0
        ][0]["Plan"]

        def operation_nodes(node):
            relation = node.get("Relation Name")
            children = [child for plan in node.get("Plans", []) for child in operation_nodes(plan)]
            return ([node] if relation == "operations" else []) + children

        nodes = operation_nodes(plan)
        assert nodes, "readiness plan must inspect operations"
        operation_loops = sum(node.get("Actual Loops", 1) for node in nodes)
        operation_rows = sum(
            node.get("Actual Rows", 0) * node.get("Actual Loops", 1) for node in nodes
        )
        # Readiness examines all source receipts in the user namespace, including
        # cleanup work outside the active indexing generation. A receipt may own
        # multiple work units; one lookup per unit is linear, per-source repetition is not.
        assert operation_loops <= work_units and operation_rows <= work_units, (
            "readiness repeats operation lookups across current operations",
            operation_loops,
            operation_rows,
            sources,
            work_units,
        )
