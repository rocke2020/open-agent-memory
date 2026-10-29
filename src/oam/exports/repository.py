"""Portable current-policy export."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import replace

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedUser
from oam.config import OAMConfig
from oam.db.context import bind_user
from oam.db.eligibility import ELIGIBLE_UNITS
from oam.db.entity_eligibility import (
    ELIGIBLE_ENTITY_MEMBERSHIPS,
    ELIGIBLE_ENTITY_NAME_SUPPORT,
)
from oam.indexing.identity import index_generation
from oam.profiles.inputs import load_profile_inputs
from oam.profiles.service import _current_snapshot, _view, stored_profile_support_ids

EXPORT_PART_BYTES = 1024 * 1024
EXPORT_FORMAT_VERSION = 1
EXPORT_OWNER_SQL = (
    "SELECT u.accepted_seq,u.published_seq,u.policy_epoch,u.index_generation_id,"
    "u.projection_epoch "
    "FROM users u WHERE u.tenant_id=%s AND u.user_id=%s AND u.state='active'"
)


class ExportPolicyChanged(RuntimeError):
    """The export snapshot no longer matches current source policy."""


class ExportNotFound(LookupError):
    """No current export is visible for the authorized user."""


class ExportRepository:
    def __init__(self, config: OAMConfig):
        self.config = config

    @staticmethod
    def _admit(connection, authority: AuthorizedUser):
        if not authority.allows("export:read"):
            raise PermissionError("export:read is required")
        bind_user(connection.cursor(), authority)
        owner = connection.execute(
            EXPORT_OWNER_SQL + " FOR SHARE OF u", (authority.tenant_id, authority.user_id)
        ).fetchone()
        if owner is None:
            raise ExportNotFound("user unavailable")
        return owner

    @staticmethod
    def _check_owner(connection, authority: AuthorizedUser, frozen: tuple) -> None:
        owner = connection.execute(
            EXPORT_OWNER_SQL + " FOR SHARE OF u", (authority.tenant_id, authority.user_id)
        ).fetchone()
        if owner != frozen:
            raise ExportPolicyChanged("export input or policy changed")

    def create(
        self, connection, authority: AuthorizedUser, *, export_id: str, retention_hours: int
    ) -> dict:
        if retention_hours <= 0:
            raise ValueError("export retention must be positive")
        key = (authority.tenant_id, authority.user_id)
        with connection.transaction():
            owner = self._admit(connection, authority)
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT s.source_id,s.input_role,s.resource_type,s.status,s.active_version,"
                    "v.media_type,v.byte_length,encode(v.content_sha256,'hex') AS sha256 "
                    "FROM sources s JOIN source_versions v ON "
                    "(v.tenant_id,v.user_id,v.source_id,v.source_version)="
                    "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
                    "WHERE s.tenant_id=%s AND s.user_id=%s AND s.status='active' "
                    "AND v.status='active' ORDER BY s.source_id",
                    key,
                )
                sources = cursor.fetchall()
            originals = []
            records = []
            for index, source in enumerate(sources):
                originals.append(
                    {
                        "index": index,
                        "path": f"originals/{index:06d}.bin",
                        "source_id": source["source_id"],
                        "source_version": source["active_version"],
                        "media_type": source["media_type"],
                        "byte_length": source["byte_length"],
                        "sha256": source["sha256"],
                    }
                )
                records.append(
                    {
                        "kind": "source",
                        "source_id": source["source_id"],
                        "source_version": source["active_version"],
                        "input_role": source["input_role"],
                        "resource_type": source["resource_type"],
                        "status": source["status"],
                        "original_index": index,
                    }
                )
            records.extend(self._projection_records(connection, key))
            profile_record = self._profile_record(connection, authority, owner[3])
            if profile_record is not None:
                records.append(profile_record)
            pending_sources = [
                row[0]
                for row in connection.execute(
                    "SELECT s.source_id FROM sources s LEFT JOIN LATERAL "
                    "(SELECT status,stage_state FROM operations o WHERE o.tenant_id=s.tenant_id "
                    "AND o.user_id=s.user_id AND o.source_id=s.source_id "
                    "AND o.source_version=s.active_version "
                    "AND o.kind IN ('index_interaction','index_resource','index_profile_resource') "
                    "ORDER BY o.created_at DESC,o.operation_id DESC LIMIT 1) latest ON true "
                    "WHERE s.tenant_id=%s AND s.user_id=%s AND s.status='active' "
                    "AND (latest.status IS DISTINCT FROM 'succeeded' OR "
                    "(s.input_role='interaction' AND "
                    "latest.stage_state->>'insights_ready' IS DISTINCT FROM 'true')) "
                    "ORDER BY s.source_id",
                    key,
                ).fetchall()
            ]
            self._check_owner(connection, authority, owner)
            part_contents, part_descriptors, records_sha = _pack_records(records)
            counts = dict(sorted(Counter(record["kind"] for record in records).items()))
            manifest = {
                "format_version": EXPORT_FORMAT_VERSION,
                "tenant_id": authority.tenant_id,
                "user_id": authority.user_id,
                "accepted_seq": owner[0],
                "published_seq": owner[1],
                "policy_epoch": owner[2],
                "generation_id": owner[3],
                "projection_epoch": owner[4],
                "counts": counts,
                "coverage": {
                    "complete": not pending_sources,
                    "pending_sources": pending_sources,
                },
                "records_sha256": records_sha,
                "parts": part_descriptors,
                "originals": originals,
                "profiles": [
                    {
                        "profile_id": "user",
                        "profile_revision": profile_record["profile_revision"],
                        "freshness": profile_record["freshness"],
                    }
                ]
                if profile_record is not None
                else [],
            }
            connection.execute(
                "INSERT INTO exports(tenant_id,user_id,export_id,policy_epoch,state,manifest,"
                "expires_at) VALUES (%s,%s,%s,%s,'ready',%s,"
                "clock_timestamp()+(%s*interval '1 hour'))",
                (*key, export_id, owner[2], Jsonb(manifest), retention_hours),
            )
            for index, content in enumerate(part_contents):
                connection.execute(
                    "INSERT INTO export_parts(tenant_id,user_id,export_id,part_index,content,"
                    "content_sha256,byte_length) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        *key,
                        export_id,
                        index,
                        content,
                        hashlib.sha256(content).digest(),
                        len(content),
                    ),
                )
            connection.execute(
                "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,accepted_seq,"
                "kind,schema_version,status,plan_sealed,stage_state) "
                "VALUES (%s,%s,%s,%s,%s,'export',1,'succeeded',true,%s)",
                (
                    *key,
                    export_id,
                    authority.principal_id,
                    owner[0],
                    Jsonb({"export_ready": True}),
                ),
            )
            return {"export_id": export_id, "operation_id": export_id, "state": "ready"}

    def _profile_record(
        self, connection, authority: AuthorizedUser, generation_id: str | None
    ) -> dict | None:
        has_profile = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM profiles WHERE tenant_id=%s AND user_id=%s "
            "AND profile_id='user' AND visible_to IS NULL) OR "
            "EXISTS(SELECT 1 FROM sources WHERE tenant_id=%s AND user_id=%s "
            "AND input_role='profile_resource' AND status='active')",
            (authority.tenant_id, authority.user_id) * 2,
        ).fetchone()[0]
        if not has_profile:
            return None
        generation = generation_id or index_generation(self.config, os.environ)
        profile_authority = replace(
            authority, capabilities=authority.capabilities | {"memory:read"}
        )
        snapshot = _current_snapshot(connection, authority)
        inputs = load_profile_inputs(
            connection,
            profile_authority,
            generation,
            stored_support_ids=stored_profile_support_ids(snapshot),
        )
        view = _view(self.config, inputs, snapshot, sys.maxsize)
        return {
            "kind": "profile",
            "profile_revision": view["profile_revision"],
            "input_identity": view["input_identity"],
            "policy_epoch": view["policy_epoch"],
            "freshness": view["freshness"],
            "pending_inputs": view["pending_inputs"],
            "content": view["content"],
            "instruction": view["instruction"],
            "references": view["references"],
            "support_ids": view["support_ids"],
        }

    @staticmethod
    def _projection_records(connection, key: tuple[str, str]) -> list[dict]:
        records = []
        with connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                "SELECT e.evidence_id,e.source_id,e.source_version,e.content_text,"
                "e.learning_eligible,e.event_time,s.input_role FROM evidence e JOIN sources s "
                "USING(tenant_id,user_id,source_id) WHERE e.tenant_id=%s AND e.user_id=%s "
                "AND e.status='active' AND s.status='active' "
                "AND s.active_version=e.source_version ORDER BY e.evidence_id",
                key,
            )
            for row in cursor.fetchall():
                records.append(
                    {
                        "kind": "evidence",
                        "evidence_id": row["evidence_id"],
                        "source_id": row["source_id"],
                        "source_version": row["source_version"],
                        "content_text": row["content_text"]
                        if row["input_role"] == "interaction"
                        or row["evidence_id"].startswith("envelope_")
                        else None,
                        "learning_eligible": row["learning_eligible"],
                        "event_time": row["event_time"].isoformat() if row["event_time"] else None,
                    }
                )
            cursor.execute(
                "SELECT DISTINCT ON (o.source_id) o.source_id,o.source_version,o.stage_state "
                "FROM operations o JOIN sources s USING(tenant_id,user_id,source_id) "
                "WHERE o.tenant_id=%s AND o.user_id=%s AND o.kind='index_resource' "
                "AND s.status='active' AND s.active_version=o.source_version "
                "AND o.stage_state->>'abstract_state'='current' "
                "ORDER BY o.source_id,o.created_at DESC,o.operation_id DESC",
                key,
            )
            for row in cursor.fetchall():
                records.append(
                    {
                        "kind": "resource_abstract",
                        "source_id": row["source_id"],
                        "source_version": row["source_version"],
                        "text": row["stage_state"]["abstract"],
                        "coverage": row["stage_state"].get("abstract_coverage", "unknown"),
                    }
                )
            cursor.execute(
                "SELECT p.passage_id,s.input_role,p.locator,p.normalized_text,p.evidence_id "
                "FROM passages p JOIN evidence e USING(tenant_id,user_id,evidence_id) "
                "JOIN sources s USING(tenant_id,user_id,source_id) "
                "JOIN source_versions v USING(tenant_id,user_id,source_id,source_version) "
                "JOIN users owner ON (owner.tenant_id,owner.user_id)=(p.tenant_id,p.user_id) "
                "WHERE p.tenant_id=%s AND p.user_id=%s AND p.visible_to IS NULL "
                "AND e.status='active' "
                "AND s.status='active' AND v.status='active' "
                "AND s.active_version=e.source_version ORDER BY p.passage_id",
                key,
            )
            for row in cursor.fetchall():
                records.append(
                    {
                        "kind": "passage",
                        "passage_id": row["passage_id"],
                        "origin": "evidence"
                        if row["input_role"] == "evidence_resource"
                        else "memory",
                        "evidence_id": row["evidence_id"],
                        "locator": row["locator"],
                        "text": row["normalized_text"],
                    }
                )
            cursor.execute(
                f"WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS}) "
                "SELECT m.memory_id,m.canonical_claim,m.assertion_kind,m.category,"
                "m.valid_from,m.valid_to,m.generation_id FROM eligible u JOIN memories m "
                "ON (m.tenant_id,m.user_id,m.memory_id)=(u.tenant_id,u.user_id,u.memory_id) "
                "WHERE u.kind='fact' ORDER BY m.memory_id"
            )
            memories = cursor.fetchall()
            for row in memories:
                records.append(
                    {
                        "kind": "memory",
                        "memory_id": row["memory_id"],
                        "text": row["canonical_claim"],
                        "assertion_kind": row["assertion_kind"],
                        "category": row["category"],
                        "valid_from": row["valid_from"].isoformat() if row["valid_from"] else None,
                        "valid_to": row["valid_to"].isoformat() if row["valid_to"] else None,
                        "generation_id": row["generation_id"],
                    }
                )
            memory_ids = [row["memory_id"] for row in memories]
            cursor.execute(
                "SELECT memory_id,evidence_id,passage_id,support_kind,span FROM support "
                "WHERE tenant_id=%s AND user_id=%s AND memory_id=ANY(%s) "
                "ORDER BY memory_id,evidence_id,passage_id",
                (*key, memory_ids),
            )
            for row in cursor.fetchall():
                records.append({"kind": "support", **dict(row)})
            records.extend(ExportRepository._entity_records(cursor))
            cursor.execute(
                f"WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS}) "
                "SELECT i.insight_id,i.revision,i.synthesis_text,i.subtype,i.recipe_id,"
                "i.generation_id FROM eligible u JOIN insights i ON "
                "(i.tenant_id,i.user_id,i.insight_id,i.revision)="
                "(u.tenant_id,u.user_id,u.insight_id,u.insight_revision) "
                "WHERE u.kind='insight' ORDER BY i.insight_id,i.revision"
            )
            insights = cursor.fetchall()
            for row in insights:
                records.append(
                    {
                        "kind": "insight",
                        "insight_id": row["insight_id"],
                        "revision": row["revision"],
                        "text": row["synthesis_text"],
                        "subtype": row["subtype"],
                        "recipe_id": row["recipe_id"],
                        "generation_id": row["generation_id"],
                    }
                )
            insight_keys = {(row["insight_id"], row["revision"]) for row in insights}
            cursor.execute(
                "SELECT insight_id,insight_revision,memory_id,support_revision "
                "FROM insight_support WHERE tenant_id=%s AND user_id=%s "
                "AND insight_id=ANY(%s) ORDER BY insight_id,insight_revision,memory_id",
                (*key, [insight_id for insight_id, _ in insight_keys]),
            )
            for row in cursor.fetchall():
                if (row["insight_id"], row["insight_revision"]) in insight_keys:
                    records.append({"kind": "insight_support", **dict(row)})
        return records

    @staticmethod
    def _entity_records(cursor) -> list[dict]:
        cursor.execute(
            f"WITH bindings AS NOT MATERIALIZED ({ELIGIBLE_ENTITY_NAME_SUPPORT}) "
            "SELECT DISTINCT b.entity_id,b.generation_id,e.canonical_name,b.name_id,"
            "b.name_text,b.name_key,b.name_kind,b.support_id,b.memory_id,b.passage_id,"
            "b.quote,b.support_kind,b.anchor_name_id FROM bindings b JOIN entity_units e "
            "USING(tenant_id,user_id,entity_id,generation_id) "
            "ORDER BY b.entity_id,b.name_id,b.support_id"
        )
        records = []
        entities, names = set(), set()
        for row in cursor.fetchall():
            if row["entity_id"] not in entities:
                entities.add(row["entity_id"])
                records.append(
                    {
                        "kind": "entity",
                        **{
                            key: row[key]
                            for key in ("entity_id", "generation_id", "canonical_name")
                        },
                    }
                )
            if row["name_id"] not in names:
                names.add(row["name_id"])
                records.append(
                    {
                        "kind": "entity_name",
                        **{
                            key: row[key]
                            for key in (
                                "name_id",
                                "entity_id",
                                "generation_id",
                                "name_text",
                                "name_key",
                                "name_kind",
                            )
                        },
                    }
                )
            records.append(
                {
                    "kind": "entity_name_support",
                    **{
                        key: row[key]
                        for key in (
                            "support_id",
                            "name_id",
                            "entity_id",
                            "generation_id",
                            "memory_id",
                            "passage_id",
                            "quote",
                            "support_kind",
                            "anchor_name_id",
                        )
                    },
                }
            )
        cursor.execute(
            f"WITH members AS NOT MATERIALIZED ({ELIGIBLE_ENTITY_MEMBERSHIPS}) "
            "SELECT m.unit_id,f.memory_id,m.entity_id,m.name_id,m.generation_id,"
            "m.observed_mention,m.mention_quote FROM members m JOIN search_units f "
            "USING(tenant_id,user_id,unit_id,generation_id) ORDER BY m.unit_id,m.entity_id"
        )
        records.extend({"kind": "entity_membership", **dict(row)} for row in cursor.fetchall())
        return records

    def _current_manifest(self, connection, authority: AuthorizedUser, export_id: str):
        owner = self._admit(connection, authority)
        row = connection.execute(
            "SELECT state,manifest FROM exports WHERE tenant_id=%s AND user_id=%s "
            "AND export_id=%s AND expires_at>clock_timestamp()",
            (authority.tenant_id, authority.user_id, export_id),
        ).fetchone()
        if row is None or row[0] != "ready":
            raise ExportNotFound(export_id)
        manifest = row[1]
        frozen = (
            manifest["accepted_seq"],
            manifest["published_seq"],
            manifest["policy_epoch"],
            manifest["generation_id"],
            manifest["projection_epoch"],
        )
        if tuple(owner) != frozen:
            raise ExportPolicyChanged("export input or policy changed")
        return manifest, frozen

    def get_manifest(self, connection, authority: AuthorizedUser, export_id: str) -> dict:
        with connection.transaction():
            manifest, frozen = self._current_manifest(connection, authority, export_id)
            self._check_owner(connection, authority, frozen)
            return manifest

    def get_part(self, connection, authority: AuthorizedUser, export_id: str, index: int) -> bytes:
        with connection.transaction():
            manifest, frozen = self._current_manifest(connection, authority, export_id)
            if index < 0 or index >= len(manifest["parts"]):
                raise ExportNotFound(export_id)
            row = connection.execute(
                "SELECT content FROM export_parts WHERE tenant_id=%s AND user_id=%s "
                "AND export_id=%s AND part_index=%s",
                (authority.tenant_id, authority.user_id, export_id, index),
            ).fetchone()
            if row is None:
                raise ExportNotFound(export_id)
            content = bytes(row[0])
            if hashlib.sha256(content).hexdigest() != manifest["parts"][index]["sha256"]:
                raise ValueError("export part checksum mismatch")
            self._check_owner(connection, authority, frozen)
            return content

    def get_original(
        self, connection, authority: AuthorizedUser, export_id: str, index: int
    ) -> tuple[bytes, str]:
        with connection.transaction():
            manifest, frozen = self._current_manifest(connection, authority, export_id)
            if index < 0 or index >= len(manifest["originals"]):
                raise ExportNotFound(export_id)
            item = manifest["originals"][index]
            row = connection.execute(
                "SELECT v.original_bytes,v.media_type FROM sources s JOIN source_versions v ON "
                "(v.tenant_id,v.user_id,v.source_id,v.source_version)="
                "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
                "WHERE s.tenant_id=%s AND s.user_id=%s AND s.source_id=%s "
                "AND s.active_version=%s AND s.status='active' AND v.status='active'",
                (
                    authority.tenant_id,
                    authority.user_id,
                    item["source_id"],
                    item["source_version"],
                ),
            ).fetchone()
            if row is None:
                raise ExportPolicyChanged("original source changed")
            content = bytes(row[0])
            if (
                len(content) != item["byte_length"]
                or hashlib.sha256(content).hexdigest() != item["sha256"]
            ):
                raise ValueError("export original checksum mismatch")
            self._check_owner(connection, authority, frozen)
            return content, str(row[1])


def _pack_records(records: list[dict]) -> tuple[list[bytes], list[dict], str]:
    contents = []
    buffer = bytearray()
    total = hashlib.sha256()
    for record in records:
        line = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        line += b"\n"
        if len(line) > EXPORT_PART_BYTES:
            raise ValueError("one export record exceeds the bounded part size")
        if buffer and len(buffer) + len(line) > EXPORT_PART_BYTES:
            contents.append(bytes(buffer))
            buffer.clear()
        buffer.extend(line)
        total.update(line)
    if buffer:
        contents.append(bytes(buffer))
    descriptors = [
        {
            "index": index,
            "path": f"records/{index:06d}.jsonl",
            "byte_length": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for index, content in enumerate(contents)
    ]
    return contents, descriptors, total.hexdigest()
