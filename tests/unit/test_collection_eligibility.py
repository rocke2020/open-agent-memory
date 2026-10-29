"""Collection visibility preserves historical source-backed records."""

import sqlite3

from oam.db.eligibility import ELIGIBLE_UNITS


class BoolAnd:
    def __init__(self):
        self.value = True

    def step(self, value):
        self.value = self.value and bool(value)

    def finalize(self):
        return self.value


def test_old_and_current_memories_are_visible_but_removed_memory_is_not():
    with sqlite3.connect(":memory:") as db:
        db.create_function("current_setting", 2, lambda name, _: "t" if "tenant" in name else "u")
        db.create_aggregate("bool_and", 1, BoolAnd)
        db.executescript(
            "CREATE TABLE users(tenant_id,user_id,state,index_generation_id);"
            "CREATE TABLE sources(tenant_id,user_id,source_id,status,active_version,input_role);"
            "CREATE TABLE source_versions(tenant_id,user_id,source_id,source_version,status);"
            "CREATE TABLE evidence(tenant_id,user_id,evidence_id,source_id,source_version,"
            "event_time,status,learning_eligible);"
            "CREATE TABLE passages(tenant_id,user_id,passage_id,evidence_id,"
            "generation_id,visible_to);"
            "CREATE TABLE memories(tenant_id,user_id,memory_id,generation_id,"
            "lifecycle_state,visible_to);"
            "CREATE TABLE support(tenant_id,user_id,memory_id,evidence_id,passage_id);"
            "CREATE TABLE insights(tenant_id,user_id,insight_id,revision,generation_id,visible_to);"
            "CREATE TABLE insight_support(tenant_id,user_id,insight_id,insight_revision,memory_id,"
            "support_revision);"
            "CREATE TABLE search_units(tenant_id,user_id,unit_id,kind,origin,source_id,"
            "source_version,passage_id,memory_id,insight_id,insight_revision,generation_id);"
            "INSERT INTO users VALUES('t','u','active','new');"
            "INSERT INTO sources VALUES('t','u','s','active',1,'interaction');"
            "INSERT INTO source_versions VALUES('t','u','s',1,'active');"
            "INSERT INTO evidence VALUES('t','u','e','s',1,NULL,'active',1);"
        )
        for name, generation, state in (
            ("old", "old", "active"),
            ("new", "new", "active"),
            ("removed", "old", "excluded"),
        ):
            db.execute("INSERT INTO passages VALUES('t','u',?,'e',?,NULL)", (name, generation))
            db.execute("INSERT INTO memories VALUES('t','u',?,?,?,NULL)", (name, generation, state))
            db.execute("INSERT INTO support VALUES('t','u',?,'e',?)", (name, name))
            db.execute(
                "INSERT INTO search_units VALUES('t','u',?,'fact','memory','s',1,?,?,NULL,NULL,?)",
                (name, name, name, generation),
            )
        # SQLite needs an explicit LIMIT before OFFSET; the query's policy is unchanged.
        query = ELIGIBLE_UNITS.replace("OFFSET 0", "LIMIT -1 OFFSET 0").replace(
            "SELECT 1 FROM support sp", "SELECT count(*) FROM support sp"
        )
        assert {row[2] for row in db.execute(query)} == {"old", "new"}
