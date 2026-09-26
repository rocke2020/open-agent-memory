"""Current source and memory inspection under user authority."""

from __future__ import annotations

import json
from dataclasses import asdict

from psycopg.rows import dict_row

from oam.auth.credentials import AuthorizedUser
from oam.db.context import bind_user
from oam.db.eligibility import ELIGIBLE_UNITS
from oam.profiles.resources import map_profile_resource

INSPECTION_PAGE_SIZE = 50
RESOURCE_PASSAGE_LIMIT = 200


class SourcePolicyChanged(RuntimeError):
    """Current source policy changed while an inspection response was assembled."""


class SourceInspector:
    @staticmethod
    def _admit(connection, authority: AuthorizedUser) -> int:
        if not authority.allows("memory:read"):
            raise PermissionError("memory:read is required")
        bind_user(connection.cursor(), authority)
        row = connection.execute(
            "SELECT policy_epoch FROM users WHERE tenant_id=%s AND user_id=%s AND state='active'",
            (authority.tenant_id, authority.user_id),
        ).fetchone()
        if row is None:
            raise PermissionError("user is unavailable")
        return int(row[0])

    @staticmethod
    def _check_policy(connection, authority: AuthorizedUser, admitted_epoch: int) -> None:
        row = connection.execute(
            "SELECT policy_epoch FROM users WHERE tenant_id=%s AND user_id=%s "
            "AND state='active' FOR SHARE",
            (authority.tenant_id, authority.user_id),
        ).fetchone()
        if row is None or row[0] != admitted_epoch:
            raise SourcePolicyChanged("source policy changed during inspection")

    def list_resources(self, connection, authority: AuthorizedUser, *, after: str | None) -> dict:
        with connection.transaction():
            epoch = self._admit(connection, authority)
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT s.source_id AS resource_id,s.resource_type,s.active_version,"
                    "v.media_type,v.byte_length,encode(v.content_sha256,'hex') AS sha256,"
                    "o.operation_id,o.status AS operation_status,o.stage_state "
                    "FROM sources s JOIN source_versions v ON "
                    "(v.tenant_id,v.user_id,v.source_id,v.source_version)="
                    "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
                    "LEFT JOIN LATERAL (SELECT operation_id,status,stage_state FROM operations "
                    "WHERE tenant_id=s.tenant_id AND user_id=s.user_id AND source_id=s.source_id "
                    "AND source_version=s.active_version "
                    "AND kind IN ('index_resource','index_profile_resource') "
                    "ORDER BY created_at DESC,operation_id DESC LIMIT 1) o ON true "
                    "WHERE s.tenant_id=%s AND s.user_id=%s AND s.status='active' "
                    "AND s.input_role IN ('evidence_resource','profile_resource') "
                    "AND v.status='active' "
                    "AND s.source_id>%s ORDER BY s.source_id LIMIT %s",
                    (
                        authority.tenant_id,
                        authority.user_id,
                        after or "",
                        INSPECTION_PAGE_SIZE + 1,
                    ),
                )
                rows = cursor.fetchall()
            items = [self._resource_row(row) for row in rows[:INSPECTION_PAGE_SIZE]]
            self._check_policy(connection, authority, epoch)
            return {
                "items": items,
                "next_after": items[-1]["resource_id"]
                if len(rows) > INSPECTION_PAGE_SIZE
                else None,
                "policy_epoch": epoch,
            }

    def get_resource(self, connection, authority: AuthorizedUser, resource_id: str) -> dict | None:
        with connection.transaction():
            epoch = self._admit(connection, authority)
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT s.source_id AS resource_id,s.resource_type,s.active_version,"
                    "v.media_type,v.byte_length,encode(v.content_sha256,'hex') AS sha256,"
                    "o.operation_id,o.status AS operation_status,o.stage_state "
                    "FROM sources s JOIN source_versions v ON "
                    "(v.tenant_id,v.user_id,v.source_id,v.source_version)="
                    "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
                    "LEFT JOIN LATERAL (SELECT operation_id,status,stage_state FROM operations "
                    "WHERE tenant_id=s.tenant_id AND user_id=s.user_id AND source_id=s.source_id "
                    "AND source_version=s.active_version "
                    "AND kind IN ('index_resource','index_profile_resource') "
                    "ORDER BY created_at DESC,operation_id DESC LIMIT 1) o ON true "
                    "WHERE s.tenant_id=%s AND s.user_id=%s AND s.source_id=%s "
                    "AND s.status='active' "
                    "AND s.input_role IN ('evidence_resource','profile_resource') "
                    "AND v.status='active'",
                    (authority.tenant_id, authority.user_id, resource_id),
                )
                row = cursor.fetchone()
            if row is None:
                self._check_policy(connection, authority, epoch)
                return None
            details = self._resource_row(row)
            version = details["active_version"]
            if details["resource_type"] == "profile":
                original = connection.execute(
                    "SELECT original_bytes FROM source_versions WHERE tenant_id=%s "
                    "AND user_id=%s AND source_id=%s AND source_version=%s "
                    "AND status='active'",
                    (authority.tenant_id, authority.user_id, resource_id, version),
                ).fetchone()
                if original is None:
                    raise SourcePolicyChanged("Profile Resource changed during inspection")
                profile_resource = json.loads(bytes(original[0]))
                details["subtype"] = profile_resource["subtype"]
                details["mapped_fields"] = [
                    asdict(field)
                    for field in map_profile_resource(profile_resource, resource_version=version)
                ]
                self._check_policy(connection, authority, epoch)
                return details
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT p.passage_id,p.locator,p.normalized_text AS text "
                    "FROM passages p JOIN evidence e USING(tenant_id,user_id,evidence_id) "
                    "WHERE e.tenant_id=%s AND e.user_id=%s AND e.source_id=%s "
                    "AND e.source_version=%s AND e.status='active' AND p.visible_to IS NULL "
                    "ORDER BY p.passage_id LIMIT %s",
                    (
                        authority.tenant_id,
                        authority.user_id,
                        resource_id,
                        version,
                        RESOURCE_PASSAGE_LIMIT + 1,
                    ),
                )
                passages = cursor.fetchall()
            details["passages"] = [dict(item) for item in passages[:RESOURCE_PASSAGE_LIMIT]]
            details["passages_truncated"] = len(passages) > RESOURCE_PASSAGE_LIMIT
            metadata = connection.execute(
                "SELECT e.content_text FROM evidence e WHERE e.tenant_id=%s AND e.user_id=%s "
                "AND e.source_id=%s AND e.source_version=%s AND e.status='active' "
                "AND NOT EXISTS (SELECT 1 FROM passages p WHERE p.tenant_id=e.tenant_id "
                "AND p.user_id=e.user_id AND p.evidence_id=e.evidence_id) "
                "ORDER BY e.evidence_id LIMIT 1",
                (authority.tenant_id, authority.user_id, resource_id, version),
            ).fetchone()
            if metadata is not None and metadata[0] is not None:
                try:
                    details["metadata"] = json.loads(metadata[0])
                except (TypeError, ValueError):
                    details["metadata"] = None
            self._check_policy(connection, authority, epoch)
            return details

    def get_original(
        self, connection, authority: AuthorizedUser, resource_id: str
    ) -> tuple[bytes, str] | None:
        with connection.transaction():
            epoch = self._admit(connection, authority)
            row = connection.execute(
                "SELECT v.original_bytes,v.media_type FROM sources s JOIN source_versions v "
                "ON (v.tenant_id,v.user_id,v.source_id,v.source_version)="
                "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
                "WHERE s.tenant_id=%s AND s.user_id=%s AND s.source_id=%s "
                "AND s.status='active' "
                "AND s.input_role IN ('evidence_resource','profile_resource') "
                "AND v.status='active'",
                (authority.tenant_id, authority.user_id, resource_id),
            ).fetchone()
            self._check_policy(connection, authority, epoch)
            return None if row is None else (bytes(row[0]), str(row[1]))

    def get_interaction(self, connection, authority: AuthorizedUser, source_id: str) -> dict | None:
        with connection.transaction():
            epoch = self._admit(connection, authority)
            row = connection.execute(
                "SELECT s.active_version,v.original_bytes,v.byte_length,"
                "encode(v.content_sha256,'hex') FROM sources s JOIN source_versions v ON "
                "(v.tenant_id,v.user_id,v.source_id,v.source_version)="
                "(s.tenant_id,s.user_id,s.source_id,s.active_version) "
                "WHERE s.tenant_id=%s AND s.user_id=%s AND s.source_id=%s "
                "AND s.status='active' AND s.input_role='interaction' AND v.status='active'",
                (authority.tenant_id, authority.user_id, source_id),
            ).fetchone()
            if row is None:
                self._check_policy(connection, authority, epoch)
                return None
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    "SELECT p.passage_id,p.locator,p.normalized_text AS text "
                    "FROM passages p JOIN evidence e USING(tenant_id,user_id,evidence_id) "
                    "WHERE e.tenant_id=%s AND e.user_id=%s AND e.source_id=%s "
                    "AND e.source_version=%s AND e.status='active' "
                    "AND p.visible_to IS NULL ORDER BY p.passage_id LIMIT %s",
                    (
                        authority.tenant_id,
                        authority.user_id,
                        source_id,
                        row[0],
                        RESOURCE_PASSAGE_LIMIT + 1,
                    ),
                )
                passages = cursor.fetchall()
            self._check_policy(connection, authority, epoch)
            return {
                "source_id": source_id,
                "active_version": row[0],
                "byte_length": row[2],
                "sha256": row[3],
                "original": json.loads(bytes(row[1])),
                "passages": [dict(item) for item in passages[:RESOURCE_PASSAGE_LIMIT]],
                "passages_truncated": len(passages) > RESOURCE_PASSAGE_LIMIT,
                "policy_epoch": epoch,
            }

    def list_memories(self, connection, authority: AuthorizedUser, *, after: str | None) -> dict:
        with connection.transaction():
            epoch = self._admit(connection, authority)
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(
                    f"WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS}) "
                    "SELECT u.unit_id,u.kind,u.origin,u.content_text AS text,u.locator,"
                    "COALESCE((SELECT jsonb_agg(jsonb_build_object('source_id',e.source_id,"
                    "'source_version',e.source_version,'evidence_id',sp.evidence_id,"
                    "'passage_id',sp.passage_id,'span',sp.span) "
                    "ORDER BY e.source_id,sp.passage_id) "
                    "FROM support sp JOIN evidence e USING(tenant_id,user_id,evidence_id) "
                    "WHERE sp.tenant_id=u.tenant_id AND sp.user_id=u.user_id "
                    "AND sp.memory_id=ANY(CASE WHEN u.kind='insight' THEN "
                    "ARRAY(SELECT memory_id FROM insight_support isp "
                    "WHERE isp.tenant_id=u.tenant_id "
                    "AND isp.user_id=u.user_id AND isp.insight_id=u.insight_id "
                    "AND isp.insight_revision=u.insight_revision) ELSE ARRAY[u.memory_id] END)),"
                    "'[]'::jsonb) AS citations,"
                    "COALESCE((SELECT jsonb_agg(jsonb_build_object('memory_id',isp.memory_id,"
                    "'support_revision',isp.support_revision) ORDER BY isp.memory_id) "
                    "FROM insight_support isp WHERE isp.tenant_id=u.tenant_id "
                    "AND isp.user_id=u.user_id AND isp.insight_id=u.insight_id "
                    "AND isp.insight_revision=u.insight_revision),'[]'::jsonb) AS supports "
                    "FROM eligible u WHERE u.kind IN ('fact','insight') AND u.unit_id>%s "
                    "ORDER BY u.unit_id LIMIT %s",
                    (after or "", INSPECTION_PAGE_SIZE + 1),
                )
                rows = cursor.fetchall()
            items = [dict(row) for row in rows[:INSPECTION_PAGE_SIZE]]
            self._check_policy(connection, authority, epoch)
            return {
                "items": items,
                "next_after": items[-1]["unit_id"] if len(rows) > INSPECTION_PAGE_SIZE else None,
                "policy_epoch": epoch,
            }

    @staticmethod
    def _resource_row(row) -> dict:
        state = row["stage_state"] or {}
        return {
            "resource_id": row["resource_id"],
            "resource_type": row["resource_type"],
            "active_version": row["active_version"],
            "media_type": row["media_type"],
            "byte_length": row["byte_length"],
            "sha256": row["sha256"],
            "operation_id": row["operation_id"],
            "operation_status": row["operation_status"],
            "source_searchable": state.get("source_searchable", False),
            "abstract_status": state.get("abstract_state", "pending"),
            "abstract": state.get("abstract") if state.get("abstract_state") == "current" else None,
        }
