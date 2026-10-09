"""Selected projection cleanup removes cached copies while preserving unrelated data."""

import hashlib

import pytest
from psycopg.types.json import Jsonb
from test_memory_removal import (
    DEPENDENT_INSIGHT,
    INDEPENDENT_INSIGHT,
    INSIGHT,
    OTHER_MEMORIES,
    REMOVED_MEMORY,
    SOURCE,
    SURVIVING_MEMORY,
    USER,
    _client,
    _content,
)
from test_memory_removal import core_database as core_database
from test_memory_removal import removal_database as removal_database

from oam.indexing.store import stable_id

pytestmark = pytest.mark.integration


def _seed_copies(database):
    with _content(database.dsn, database.tenant) as connection:
        key = (database.tenant, USER)
        generation = connection.execute(
            "SELECT index_generation_id FROM users WHERE tenant_id=%s AND user_id=%s", key
        ).fetchone()[0]
        connection.execute(
            "UPDATE memories SET valid_from='2026-01-01Z',valid_to='2026-02-01Z' "
            "WHERE memory_id=ANY(%s)",
            ([REMOVED_MEMORY, SURVIVING_MEMORY],),
        )
        for insight in (INSIGHT, DEPENDENT_INSIGHT, INDEPENDENT_INSIGHT):
            connection.execute(
                "UPDATE insights SET input_digest=%s WHERE insight_id=%s",
                (hashlib.sha256(insight.encode()).digest(), insight),
            )
        connection.execute(
            "INSERT INTO sources(tenant_id,user_id,source_id,input_role,active_version,status) "
            "VALUES (%s,%s,'independent-source','interaction',1,'active')",
            key,
        )
        connection.execute(
            "INSERT INTO source_versions(tenant_id,user_id,source_id,source_version,media_type,"
            "original_bytes,content_sha256,byte_length,status) "
            "VALUES (%s,%s,'independent-source',1,'text/plain',%s,%s,11,'active')",
            (*key, b"independent", hashlib.sha256(b"independent").digest()),
        )
        connection.execute(
            "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,kind,"
            "schema_version,status) VALUES (%s,%s,'copies-op',%s,'index_interaction',1,"
            "'succeeded')",
            (*key, f"user:{USER}"),
        )
        connection.execute(
            "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
            "input_digest,generation_id,status,attempts_remaining) "
            "VALUES (%s,%s,'copies-work','copies-op','index_interaction',%s,%s,'succeeded',1)",
            (*key, hashlib.sha256(b"work").digest(), generation),
        )
        connection.execute(
            "INSERT INTO attempts(tenant_id,user_id,work_unit_id,attempt_id,ordinal,owner_id,"
            "fence,disposition) VALUES (%s,%s,'copies-work','copies-attempt',1,'fixture',1,"
            "'succeeded')",
            key,
        )
        copies = [
            (
                SOURCE,
                "extract:original",
                "extraction",
                {"facts": [{"text": f"Synthetic claim {REMOVED_MEMORY}."}]},
            ),
            (
                SOURCE,
                "facts_embed:" + stable_id("facts_batch", ["original"]),
                "fact-vector",
                {"vectors": [[1.0]]},
            ),
            ("independent-source", "extract:unrelated", "unrelated-extract", {"facts": []}),
            (SOURCE, "passage:keep", "passage", {"vectors": [[1.0]]}),
            (SOURCE, "insight", INSIGHT, {"text": "old selected synthesis"}),
            (SOURCE, "insight", DEPENDENT_INSIGHT, {"text": "old dependent synthesis"}),
            (SOURCE, "insight", INDEPENDENT_INSIGHT, {"text": "independent synthesis"}),
            (SOURCE, f"repair:{INSIGHT}:2:0:synthesis", "old-repair", {"text": "old repair"}),
            (
                SOURCE,
                f"repair:{INDEPENDENT_INSIGHT}:1:0:synthesis",
                "other-repair",
                {"text": "keep"},
            ),
        ]
        for source, stage, identity, output in copies:
            connection.execute(
                "INSERT INTO index_stage_results(tenant_id,user_id,source_id,source_version,"
                "generation_id,stage_key,input_digest,output,work_unit_id,attempt_id) "
                "VALUES (%s,%s,%s,1,%s,%s,%s,%s,'copies-work','copies-attempt')",
                (
                    *key,
                    source,
                    generation,
                    stage,
                    hashlib.sha256(identity.encode()).digest(),
                    Jsonb(output),
                ),
            )
        connection.execute(
            'UPDATE profiles SET sections=%s,"references"=%s WHERE profile_revision=1',
            (Jsonb({"main": ["old selected claim"]}), Jsonb([{"support_id": REMOVED_MEMORY}])),
        )
        connection.execute(
            "INSERT INTO profiles(tenant_id,user_id,profile_id,profile_revision,sections,content,"
            'support_ids,"references",input_identity,policy_epoch,generation_id,visible_from,'
            "visible_to) "
            "VALUES (%s,%s,'user',2,'{}','Independent old Profile',%s,'[]',%s,0,%s,1,2)",
            (*key, Jsonb(list(OTHER_MEMORIES)), hashlib.sha256(b"profile").digest(), generation),
        )


@pytest.mark.parametrize("kind", ["memory", "insight"])
async def test_cleanup_scrubs_only_selected_projection_copies(removal_database, kind):
    database = removal_database
    _seed_copies(database)
    with _content(database.dsn, database.tenant) as connection:
        before_source = connection.execute(
            "SELECT original_bytes FROM source_versions WHERE source_id=%s", (SOURCE,)
        ).fetchone()
        before_survivor = connection.execute(
            "SELECT canonical_claim,valid_from,valid_to FROM memories WHERE memory_id=%s",
            (SURVIVING_MEMORY,),
        ).fetchone()
    async with _client(database.headers) as client:
        endpoint = f"memories/{REMOVED_MEMORY}" if kind == "memory" else f"insights/{INSIGHT}"
        response = await client.delete(f"/v1/users/{USER}/{endpoint}")
        assert response.status_code == 202, response.text
        database.cleanup()
    with _content(database.dsn, database.tenant) as connection:
        if kind == "memory":
            assert connection.execute(
                "SELECT canonical_claim,valid_from,valid_to,lifecycle_state FROM memories "
                "WHERE memory_id=%s",
                (REMOVED_MEMORY,),
            ).fetchone() == ("", None, None, "excluded")
        else:
            assert connection.execute(
                "SELECT canonical_claim,lifecycle_state FROM memories WHERE memory_id=%s",
                (REMOVED_MEMORY,),
            ).fetchone() == (f"Synthetic claim {REMOVED_MEMORY}.", "active")
        remaining = set(
            connection.execute("SELECT source_id,stage_key FROM index_stage_results").fetchall()
        )
        expected = {
            ("independent-source", "extract:unrelated"),
            (SOURCE, "passage:keep"),
            (SOURCE, "insight"),
            (SOURCE, f"repair:{INDEPENDENT_INSIGHT}:1:0:synthesis"),
        }
        if kind == "insight":
            expected.update(
                {
                    (SOURCE, "extract:original"),
                    (SOURCE, "facts_embed:" + stable_id("facts_batch", ["original"])),
                }
            )
        assert remaining == expected
        texts = {
            row[0]
            for row in connection.execute(
                "SELECT output->>'text' FROM index_stage_results WHERE stage_key='insight'"
            ).fetchall()
        }
        assert texts == (
            {"independent synthesis"}
            if kind == "memory"
            else {"independent synthesis", "old dependent synthesis"}
        )
        assert connection.execute(
            'SELECT content,sections,support_ids,"references",visible_to IS NOT NULL FROM profiles '
            "WHERE profile_revision=1"
        ).fetchone() == (None, {}, [], [], True)
        assert connection.execute(
            "SELECT content,support_ids FROM profiles WHERE profile_revision=2"
        ).fetchone() == ("Independent old Profile", list(OTHER_MEMORIES))
        assert (
            connection.execute(
                "SELECT canonical_claim,valid_from,valid_to FROM memories WHERE memory_id=%s",
                (SURVIVING_MEMORY,),
            ).fetchone()
            == before_survivor
        )
        assert (
            connection.execute(
                "SELECT original_bytes FROM source_versions WHERE source_id=%s", (SOURCE,)
            ).fetchone()
            == before_source
        )
        assert connection.execute("SELECT count(*) FROM evidence").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM passages").fetchone() == (1,)
    with _content(database.dsn, database.tenant, "other") as connection:
        assert connection.execute(
            "SELECT canonical_claim FROM memories WHERE memory_id='other-memory-selected'"
        ).fetchone() == ("Synthetic claim other-memory-selected.",)


async def test_older_l1_cleanup_preserves_newer_supported_revision(removal_database):
    database = removal_database
    _seed_copies(database)
    async with _client(database.headers) as client:
        response = await client.delete(f"/v1/users/{USER}/memories/{REMOVED_MEMORY}")
        assert response.status_code == 202, response.text
        with _content(database.dsn, database.tenant) as connection:
            key = (database.tenant, USER)
            generation = connection.execute(
                "SELECT index_generation_id FROM users WHERE tenant_id=%s AND user_id=%s", key
            ).fetchone()[0]
            digest = hashlib.sha256(b"new repair").digest()
            connection.execute(
                "INSERT INTO insights(tenant_id,user_id,insight_id,revision,synthesis_text,"
                "input_digest,recipe_id,generation_id,visible_from) "
                "VALUES (%s,%s,%s,3,'New supported repair',%s,'synthetic',%s,12)",
                (*key, INSIGHT, digest, generation),
            )
            for memory in OTHER_MEMORIES:
                connection.execute(
                    "INSERT INTO insight_support VALUES (%s,%s,%s,3,%s,1)",
                    (*key, INSIGHT, memory),
                )
            connection.execute(
                "INSERT INTO search_units(tenant_id,user_id,unit_id,kind,origin,insight_id,"
                "insight_revision,generation_id,content_text,locator,embedding,lexical_text) "
                "SELECT tenant_id,user_id,%s,kind,origin,insight_id,3,generation_id,"
                "'New supported repair',locator,embedding,'New supported repair' "
                "FROM search_units WHERE unit_id=%s",
                (f"{INSIGHT}:r3", f"{INSIGHT}:r2"),
            )
            for stage in (
                "insight",
                f"repair:{INSIGHT}:3:1:synthesis",
                f"repair:{INSIGHT}:3:1:embed",
            ):
                connection.execute(
                    "INSERT INTO index_stage_results(tenant_id,user_id,source_id,source_version,"
                    "generation_id,stage_key,input_digest,output,work_unit_id,attempt_id) "
                    "VALUES (%s,%s,%s,1,%s,%s,%s,%s,'copies-work','copies-attempt')",
                    (
                        *key,
                        SOURCE,
                        generation,
                        stage,
                        digest,
                        Jsonb({"text": "New supported repair"}),
                    ),
                )
            connection.execute(
                "INSERT INTO profiles(tenant_id,user_id,profile_id,profile_revision,sections,"
                "content,support_ids,input_identity,policy_epoch,generation_id,visible_from) "
                "VALUES (%s,%s,'user',3,'{}','Fresh supported Profile',%s,%s,1,%s,12)",
                (*key, Jsonb([f"{INSIGHT}:r3"]), digest, generation),
            )
        database.cleanup()
    with _content(database.dsn, database.tenant) as connection:
        assert connection.execute(
            "SELECT revision,synthesis_text FROM insights WHERE insight_id=%s", (INSIGHT,)
        ).fetchall() == [(3, "New supported repair")]
        assert connection.execute(
            "SELECT memory_id FROM insight_support WHERE insight_id=%s ORDER BY memory_id",
            (INSIGHT,),
        ).fetchall() == [(value,) for value in OTHER_MEMORIES]
        assert connection.execute(
            "SELECT insight_revision,content_text FROM search_units WHERE insight_id=%s", (INSIGHT,)
        ).fetchall() == [(3, "New supported repair")]
        assert connection.execute(
            "SELECT count(*) FROM index_stage_results WHERE output->>'text'='New supported repair'"
        ).fetchone() == (3,)
        assert connection.execute(
            "SELECT content,visible_to FROM profiles WHERE profile_revision=3"
        ).fetchone() == ("Fresh supported Profile", None)
