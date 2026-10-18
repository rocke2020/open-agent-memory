"""Recorded output cannot outlive the source policy that supports it."""

import time

import pytest
from psycopg.types.json import Jsonb
from test_source_version_admission import command
from test_source_version_admission import source_database as source_database

from oam.config import load_config
from oam.db.sources import SourceRepository
from oam.debug.traces import TraceRepository
from oam.retrieval.repository import RecallRepository

pytestmark = pytest.mark.integration


def test_recorded_trace_redacts_content_after_source_deletion(source_database):
    connection, authority = source_database
    SourceRepository().accept(connection, authority, command())
    connection.execute(
        "UPDATE users SET index_generation_id='generation' WHERE tenant_id=%s AND user_id=%s",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "INSERT INTO passages(tenant_id,user_id,passage_id,evidence_id,parse_revision,"
        "locator,normalized_text,generation_id,visible_from) "
        "VALUES (%s,%s,'passage','evidence1',1,'{}','Private text','generation',1)",
        (authority.tenant_id, authority.user_id),
    )
    connection.execute(
        "INSERT INTO search_units(tenant_id,user_id,unit_id,source_id,source_version,"
        "evidence_id,passage_id,kind,origin,content_text,generation_id,embedding,"
        "lexical_text,locator) VALUES (%s,%s,'passage','source',1,'evidence1','passage',"
        "'passage','memory','Private text','generation',%s::vector,'Private text','{}')",
        (authority.tenant_id, authority.user_id, str([1.0] + [0.0] * 1023)),
    )
    result = {
        "items": [
            {
                "unit_id": "passage",
                "kind": "passage",
                "origin": "memory",
                "text": "Private text",
                "citations": [],
                "time_evidence": [],
                "paths": [],
            }
        ],
        "context": "[1] passage memory\nPrivate text\nSource: ",
        "context_tokens": 10,
        "tokenizer": "oam-unicode-quarter-v1",
        "diagnostics": {"generative_calls": 0},
    }
    connection.execute(
        "INSERT INTO traces(tenant_id,user_id,trace_id,read_seq,generation_id,"
        "policy_epoch,decisions,content,expires_at) "
        "VALUES (%s,%s,'trace',1,'generation',0,%s,%s,clock_timestamp()+interval '1 day')",
        (
            authority.tenant_id,
            authority.user_id,
            Jsonb({"arms": {"dense": ["passage"]}, "query_hash": "hash"}),
            Jsonb(result),
        ),
    )
    config = load_config("configs/oam.yaml").config
    recall = RecallRepository(connection, authority, config, deadline=time.monotonic() + 10)
    assert recall.record_trace(
        "recorded", 0, {"arms": {"dense": ["passage"]}, "query_hash": "hash"}, result
    )
    assert recall.record_trace(
        "empty",
        0,
        {"arms": {}, "query_hash": "other"},
        {**result, "items": [], "context": "", "context_tokens": 0},
    )
    debug_authority = authority.__class__(
        authority.credential_id,
        authority.tenant_id,
        authority.principal_id,
        authority.user_id,
        frozenset({"debug:read"}),
    )
    debug = TraceRepository()

    before = debug.get(connection, debug_authority, "trace")
    assert before["status"] == "recorded"
    assert before["result"]["context"] == result["context"]
    assert (
        debug.replay(connection, debug_authority, "trace", "reassemble_recorded")["status"]
        == "exact"
    )
    assert debug.diff(connection, debug_authority, "trace", "empty")["removed"] == ["passage"]

    SourceRepository().delete(
        connection,
        authority,
        source_id="source",
        resource_only=False,
        operation_id="cleanup",
        work_unit_id="cleanup-work",
        attempts_remaining=2,
    )
    after = debug.get(connection, debug_authority, "trace")
    assert after["status"] == "policy_redacted"
    assert "result" not in after
