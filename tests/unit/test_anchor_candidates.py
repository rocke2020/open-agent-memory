"""Literal recall bounds eligibility work by exact physical matches."""

import time

from test_query_time_recall import CONFIG
from test_recall_repository_setup import RecordingConnection

from oam.auth.credentials import AuthorizedUser
from oam.retrieval.repository import RecallRepository


def test_exact_anchor_scan_precedes_eligibility_and_only_eligible_rows_are_limited():
    connection = RecordingConnection()
    authority = AuthorizedUser("key", "tenant", "reader", "user", frozenset({"memory:read"}))
    repo = RecallRepository(connection, authority, CONFIG, deadline=time.monotonic() + 5)

    repo.anchors('"50%_Off" or "Café" LAB-204 delivery', 2)

    query = connection.statements[-1]
    assert "physical_matches AS MATERIALIZED" in query
    matches, eligibility = query.split("FROM physical_matches", 1)
    physical = matches.split("physical_matches AS MATERIALIZED", 1)[1]
    assert "FROM search_units" in physical
    assert "%(ann_tenant)s" in physical and "%(ann_user)s" in physical
    assert "LIMIT" not in physical
    assert "strpos(lower(u.content_text)" in physical and "~* ANY" in physical
    assert "FROM filtered u" in eligibility
    assert "ts_rank_cd" in eligibility and eligibility.endswith("LIMIT %(limit)s")
    parameters = connection.parameters[-1]
    assert "50%_off" in parameters.values() and "café" in parameters.values()
    assert parameters["identifiers"] == ["(^|[^[:alnum:]_-])LAB\\-204($|[^[:alnum:]_-])"]
