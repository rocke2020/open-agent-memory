from types import SimpleNamespace

import regex
from test_query_time_recall import CONFIG

from oam.retrieval.repository import RecallRepository


class ReadProbe(RecallRepository):
    def __init__(self, rows=()):
        self.rows = rows
        self.reads = []
        self.request = None
        self.authority = SimpleNamespace(tenant_id="tenant", user_id="user")

    def _execute(self, sql, parameters=(), *, configure_ann=False):
        self.reads.append((sql, parameters))
        return list(self.rows)


def test_empty_search_resources_do_not_read_storage():
    repo = ReadProbe()
    assert repo.resource_document_rank("", "gen", 3) == []
    assert repo.resource_document_rank("retention", "gen", 0) == []
    assert repo.resource_clause_rank("retention", "gen", [], 4) == {}
    assert repo.resource_source_openings([], "gen") == {}
    assert repo.resource_neighbors([], "gen") == {}
    assert repo.reads == []


def test_resource_document_order_and_parameterized_body_terms():
    repo = ReadProbe([{"source_id": "second"}, {"source_id": "first"}])
    assert repo.resource_document_rank("renewal contract", "gen", 2) == ["second", "first"]
    sql, params = repo.reads[0]
    assert "renewal" not in sql
    assert params["generation"] == "gen" and params["limit"] == 2
    assert "bm25_postings" in sql and "bm25_term_stats" in sql
    assert "byte_length" in sql and "text/plain" in sql
    assert "corpus AS MATERIALIZED (SELECT count(*) AS n,avg(byte_length)" in sql


def test_first_passage_reuses_native_literal_predicates_in_current_text_scope():
    query = '"50%_Off" or "Café" or LAB-204'
    repo = ReadProbe([{"matched": True}])
    repo.config = CONFIG
    assert not repo.resource_first_passage_anchor("no literal", "gen")
    assert not repo.reads
    assert repo.resource_first_passage_anchor(query, "gen")
    witness, params = repo.reads[-1]
    repo.anchors(query, 7)
    anchor_sql, anchor_params = repo.reads[-1]
    predicate = (
        "strpos(lower(u.content_text),%(anchor_phrase_0)s)>0 OR "
        "strpos(lower(u.content_text),%(anchor_phrase_1)s)>0 OR "
        "u.content_text ~* ANY(%(identifiers)s::text[])"
    )
    assert predicate in anchor_sql
    assert f"FROM text_units u WHERE u.ordinal=0 AND ({predicate})" in witness
    assert "owner.index_generation_id=%(generation)s" in witness
    assert "v.media_type='text/plain'" in witness and "p.visible_to IS NULL" in witness
    assert params["generation"] == "gen"
    for key in ("identifiers", "anchor_phrase_0", "anchor_phrase_1"):
        assert params[key] == anchor_params[key]
    assert [params["anchor_phrase_0"], params["anchor_phrase_1"]] == ["50%_off", "café"]
    pattern = params["identifiers"][0]
    assert regex.search(pattern, "matching (lab-204).", regex.IGNORECASE)
    assert not regex.search(pattern, "LAB-2045 or prefix_LAB-204", regex.IGNORECASE)


def test_openings_and_neighbors_preserve_seed_and_source_identity():
    repo = ReadProbe([{"unit_id": "middle", "source_id": "contract", "opening": "Preamble"}])
    assert repo.resource_source_openings(["middle"], "gen") == {"middle": ("contract", "Preamble")}
    repo = ReadProbe(
        [
            {"seed_id": "middle", "unit_id": "before"},
            {"seed_id": "middle", "unit_id": "middle"},
            {"seed_id": "middle", "unit_id": "after"},
            {"seed_id": "only", "unit_id": "only"},
        ]
    )
    assert repo.resource_neighbors(["middle", "only", "absent"], "gen") == {
        "middle": ["before", "middle", "after"],
        "only": ["only"],
    }
    assert repo.reads[0][1]["radius"] == 3


def test_clause_ranks_are_separate_for_each_requested_source():
    repo = ReadProbe(
        [
            {"source_id": "first", "unit_id": "clause-b"},
            {"source_id": "first", "unit_id": "clause-a"},
            {"source_id": "second", "unit_id": "clause-c"},
        ]
    )
    assert repo.resource_clause_rank(
        "supplier retention obligations", "gen", ["first", "second"], 2
    ) == {
        "first": ["clause-b", "clause-a"],
        "second": ["clause-c"],
    }
    assert repo.reads[0][1]["generation"] == "gen"
