"""Read currently eligible, source-backed inputs for an explicit Profile refresh."""

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from psycopg.rows import dict_row

from oam.auth.credentials import AuthorizedUser
from oam.db.context import bind_user
from oam.db.eligibility import ELIGIBLE_SUPPORT, ELIGIBLE_UNITS
from oam.profiles.resources import map_profile_resource

PROFILE_INPUT_SCHEMA_VERSION = 4
MAX_PROMPT_UNITS = 64
MAX_PROMPT_UNITS_PER_KIND = 32
MAX_PASSAGES_PER_MEMORY = 4
MAX_PASSAGES_PER_UNIT = 8
MAX_UNIT_TEXT_CHARS = 2000
MAX_PASSAGE_TEXT_CHARS = 1000

ELIGIBLE_PROFILE_UNITS_SQL = f"""
/* profile:eligible */
WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS})
SELECT u.unit_id,u.kind,u.origin,u.memory_id,u.insight_id,u.insight_revision,
       i.subtype AS insight_subtype,u.content_text
FROM eligible u
LEFT JOIN insights i ON
  (i.tenant_id,i.user_id,i.insight_id,i.revision)=
  (u.tenant_id,u.user_id,u.insight_id,u.insight_revision)
WHERE u.generation_id=%s AND u.origin='memory' AND u.kind IN ('fact','insight')
ORDER BY u.kind,u.unit_id
""".strip()

ACTIVE_PROFILE_RESOURCES_SQL = """
/* profile:resources */
SELECT s.source_id,sv.source_version,sv.media_type,sv.original_bytes
FROM sources s
JOIN source_versions sv ON
  (sv.tenant_id,sv.user_id,sv.source_id,sv.source_version)=
  (s.tenant_id,s.user_id,s.source_id,s.active_version)
WHERE s.tenant_id=%s AND s.user_id=%s
  AND s.input_role='profile_resource' AND s.resource_type='profile'
  AND s.status='active' AND sv.status='active'
ORDER BY s.source_id
""".strip()

PROFILE_SOURCE_SCOPE_SQL = """
/* profile:source_scope */
SELECT input_role,source_id,active_version
FROM sources
WHERE tenant_id=%s AND user_id=%s AND status='active'
  AND input_role IN ('interaction','profile_resource')
ORDER BY input_role,source_id
""".strip()

PENDING_PROFILE_INPUTS_SQL = """
/* profile:pending */
SELECT DISTINCT s.source_id,s.active_version AS source_version,
  CASE
    WHEN EXISTS (
      SELECT 1 FROM operations o JOIN work_units w
        USING(tenant_id,user_id,operation_id)
      WHERE (o.tenant_id,o.user_id,o.source_id,o.source_version)=
            (s.tenant_id,s.user_id,s.source_id,s.active_version)
        AND o.kind IN ('index_interaction','consolidate')
        AND o.status IN ('accepted','running')
        AND w.generation_id=%s AND w.status IN ('queued','running','retry_wait')
    ) THEN 'pending'
    WHEN EXISTS (
      SELECT 1 FROM operations o JOIN work_units w
        USING(tenant_id,user_id,operation_id)
      WHERE (o.tenant_id,o.user_id,o.source_id,o.source_version)=
            (s.tenant_id,s.user_id,s.source_id,s.active_version)
        AND o.kind='consolidate'
        AND w.generation_id=%s AND w.status IN ('failed_terminal','cancelled')
    ) THEN 'failed'
    WHEN EXISTS (
      SELECT 1 FROM operations o JOIN work_units w
        USING(tenant_id,user_id,operation_id)
      WHERE (o.tenant_id,o.user_id,o.source_id,o.source_version)=
            (s.tenant_id,s.user_id,s.source_id,s.active_version)
        AND o.kind='index_interaction'
        AND w.generation_id=%s AND w.status IN ('failed_terminal','cancelled')
    ) THEN 'blocked'
    ELSE 'pending'
  END AS coverage_state
FROM sources s
JOIN source_versions sv ON
  (sv.tenant_id,sv.user_id,sv.source_id,sv.source_version)=
  (s.tenant_id,s.user_id,s.source_id,s.active_version)
LEFT JOIN evidence e ON
  (e.tenant_id,e.user_id,e.source_id,e.source_version)=
  (s.tenant_id,s.user_id,s.source_id,s.active_version)
  AND e.status='active' AND e.learning_eligible
WHERE s.tenant_id=%s AND s.user_id=%s
  AND s.input_role='interaction' AND s.status='active' AND sv.status='active'
  AND (e.evidence_id IS NOT NULL OR EXISTS (
    SELECT 1 FROM operations o JOIN work_units w
      USING(tenant_id,user_id,operation_id)
    WHERE (o.tenant_id,o.user_id,o.source_id,o.source_version)=
          (s.tenant_id,s.user_id,s.source_id,s.active_version)
      AND o.kind='index_interaction' AND w.generation_id=%s
      AND w.status IN ('queued','running','retry_wait','failed_terminal','cancelled')
  ))
  AND NOT EXISTS (
    SELECT 1 FROM operations o
    JOIN work_units w ON
      (w.tenant_id,w.user_id,w.operation_id)=(o.tenant_id,o.user_id,o.operation_id)
    WHERE (o.tenant_id,o.user_id,o.source_id,o.source_version)=
          (s.tenant_id,s.user_id,s.source_id,s.active_version)
      AND o.kind='index_interaction' AND o.status='succeeded'
      AND o.stage_state->>'insights_ready'='true' AND w.generation_id=%s
  )
ORDER BY s.source_id
""".strip()

INSIGHT_SUPPORT_SQL = """
/* profile:insight_support */
SELECT insight_id,insight_revision,memory_id
FROM insight_support
WHERE tenant_id=%s AND user_id=%s AND insight_id=ANY(%s)
ORDER BY insight_id,insight_revision,memory_id
""".strip()

PASSAGE_SUPPORT_SQL = f"""
/* profile:passages */
WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_SUPPORT})
SELECT sp.memory_id,sp.passage_id,sp.source_id,sp.source_version,sp.event_time,
       p.normalized_text,p.locator
FROM eligible sp
JOIN passages p ON
  (p.tenant_id,p.user_id,p.passage_id)=(sp.tenant_id,sp.user_id,sp.passage_id)
WHERE sp.generation_id=%s AND sp.memory_id=ANY(%s)
ORDER BY sp.memory_id,sp.passage_id
""".strip()


@dataclass(frozen=True)
class ProfileInputs:
    identity: bytes
    scope_identity: bytes
    supported: dict[str, dict[str, Any]]
    exact_fields: tuple[dict[str, Any], ...]
    pending_inputs: int
    failed_inputs: int
    blocked_inputs: int
    waiting_source_ids: tuple[str, ...]
    policy_epoch: int
    prompt_inputs: list[dict[str, Any]]
    eligible_support_ids: set[str]
    uncovered_inputs: int


def _identity(payload: dict[str, Any]) -> bytes:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).digest()


def _exact_fields(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    exact: list[dict[str, Any]] = []
    for row in rows:
        if row["media_type"] != "application/json":
            raise ValueError("Profile Resource original must be JSON")
        try:
            resource = json.loads(bytes(row["original_bytes"]))
        except (TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Profile Resource original is invalid JSON") from exc
        if not isinstance(resource, dict) or resource.get("resource_id") != row["source_id"]:
            raise ValueError("Profile Resource identity does not match its source")
        mapped = map_profile_resource(resource, resource_version=row["source_version"])
        for field in mapped:
            support = field.support
            exact.append(
                {
                    "section": field.section,
                    "key": field.key,
                    "value": field.value,
                    "qualifier": field.qualifier or "",
                    "assertion_kind": field.assertion_kind,
                    "supports": [
                        {
                            "kind": "profile_resource",
                            "resource_id": support.resource_id,
                            "resource_version": support.resource_version,
                            "subtype": support.subtype,
                            "source_revision": support.source_revision,
                            "as_of": support.as_of.isoformat() if support.as_of else None,
                            "content_path": support.content_path,
                        }
                    ],
                }
            )
    return tuple(exact)


def _prompt_inputs(cursor, authority: AuthorizedUser, units: list[dict], generation_id: str):
    facts = [row for row in units if row["kind"] == "fact"]
    insights = [row for row in units if row["kind"] == "insight"]
    selected = facts[:MAX_PROMPT_UNITS_PER_KIND] + insights[:MAX_PROMPT_UNITS_PER_KIND]
    remaining = MAX_PROMPT_UNITS - len(selected)
    selected += (facts[MAX_PROMPT_UNITS_PER_KIND:] + insights[MAX_PROMPT_UNITS_PER_KIND:])[
        :remaining
    ]
    insight_keys = {
        (row["insight_id"], row["insight_revision"]) for row in selected if row["kind"] == "insight"
    }
    insight_memory_ids: dict[tuple[str, int], list[str]] = defaultdict(list)
    if insight_keys:
        cursor.execute(
            INSIGHT_SUPPORT_SQL,
            (authority.tenant_id, authority.user_id, sorted({key[0] for key in insight_keys})),
        )
        for row in cursor.fetchall():
            key = (row["insight_id"], row["insight_revision"])
            if key in insight_keys:
                insight_memory_ids[key].append(row["memory_id"])
    memory_ids = {row["memory_id"] for row in selected if row["kind"] == "fact"}
    for identifiers in insight_memory_ids.values():
        memory_ids.update(identifiers)
    passages: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if memory_ids:
        cursor.execute(PASSAGE_SUPPORT_SQL, (generation_id, sorted(memory_ids)))
        for row in cursor.fetchall():
            citations = passages[row["memory_id"]]
            if len(citations) < MAX_PASSAGES_PER_MEMORY:
                text = row["normalized_text"]
                citations.append(
                    {
                        "passage_id": row["passage_id"],
                        "source_id": row["source_id"],
                        "source_version": row["source_version"],
                        "event_time": row["event_time"].isoformat() if row["event_time"] else None,
                        "text": text[:MAX_PASSAGE_TEXT_CHARS],
                        "locator": row["locator"],
                    }
                )
    result = []
    for row in selected:
        if row["kind"] == "fact":
            support_memory_ids = [row["memory_id"]]
        else:
            support_memory_ids = insight_memory_ids[(row["insight_id"], row["insight_revision"])]
        source_passages = [
            passage for memory_id in support_memory_ids for passage in passages[memory_id]
        ]
        if not source_passages:
            raise ValueError("Profile input lost its eligible source passages")
        full_text = row["content_text"]
        item = {
            "kind": row["kind"],
            "id": row["unit_id"],
            "text": full_text[:MAX_UNIT_TEXT_CHARS],
            "text_truncated": len(full_text) > MAX_UNIT_TEXT_CHARS,
            "support_memory_ids": support_memory_ids,
            "source_passages": source_passages[:MAX_PASSAGES_PER_UNIT],
            "source_passages_truncated": len(source_passages) > MAX_PASSAGES_PER_UNIT,
        }
        if row["kind"] == "insight":
            item["subtype"] = row["insight_subtype"]
            item["revision"] = row["insight_revision"]
        result.append(item)
    return result


def load_profile_inputs(connection, authority: AuthorizedUser, generation_id: str) -> ProfileInputs:
    """Load current Profile inputs in one caller-owned PostgreSQL connection."""
    if not authority.allows("memory:read"):
        raise PermissionError("memory:read is required")
    if not generation_id:
        raise ValueError("generation_id is required")
    with connection.transaction():
        cursor = connection.cursor(row_factory=dict_row)
        bind_user(cursor, authority)
        cursor.execute(
            "/* profile:state */ SELECT state,policy_epoch,index_generation_id FROM users "
            "WHERE tenant_id=%s AND user_id=%s",
            (authority.tenant_id, authority.user_id),
        )
        state = cursor.fetchone()
        if state is None or state["state"] != "active":
            raise PermissionError("user is not active")
        if state["index_generation_id"] not in {None, generation_id}:
            raise ValueError("index generation changed")
        cursor.execute(ELIGIBLE_PROFILE_UNITS_SQL, (generation_id,))
        units = cursor.fetchall()
        cursor.execute(ACTIVE_PROFILE_RESOURCES_SQL, (authority.tenant_id, authority.user_id))
        resources = cursor.fetchall()
        exact_fields = _exact_fields(resources)
        cursor.execute(PROFILE_SOURCE_SCOPE_SQL, (authority.tenant_id, authority.user_id))
        source_scope = cursor.fetchall()
        cursor.execute(
            PENDING_PROFILE_INPUTS_SQL,
            (
                generation_id,
                generation_id,
                generation_id,
                authority.tenant_id,
                authority.user_id,
                generation_id,
                generation_id,
            ),
        )
        pending = cursor.fetchall()
        prompt_inputs = _prompt_inputs(cursor, authority, units, generation_id)

    supported: dict[str, dict[str, Any]] = {}
    for row in units:
        identifier = row["unit_id"]
        if row["kind"] == "fact":
            supported[identifier] = {
                "kind": "fact",
                "id": identifier,
                "memory_id": row["memory_id"],
            }
        else:
            supported[identifier] = {
                "kind": "insight",
                "id": identifier,
                "insight_id": row["insight_id"],
                "revision": row["insight_revision"],
            }
    eligible_support_ids = set(supported)
    eligible_support_ids.update(
        f"profile_resource:{row['source_id']}:{row['source_version']}" for row in resources
    )
    identity = _identity(
        {
            "schema_version": PROFILE_INPUT_SCHEMA_VERSION,
            "generation_id": generation_id,
            "policy_epoch": state["policy_epoch"],
            "memory": [
                [
                    row["unit_id"],
                    row["kind"],
                    row["memory_id"],
                    row["insight_id"],
                    row["insight_revision"],
                    row["insight_subtype"],
                    row["content_text"],
                ]
                for row in units
            ],
            "profile_resources": [[row["source_id"], row["source_version"]] for row in resources],
            "pending": [
                [row["source_id"], row["source_version"], row["coverage_state"]] for row in pending
            ],
        }
    )
    return ProfileInputs(
        identity=identity,
        scope_identity=_identity(
            {
                "schema_version": PROFILE_INPUT_SCHEMA_VERSION,
                "generation_id": generation_id,
                "policy_epoch": state["policy_epoch"],
                "sources": [
                    [row["input_role"], row["source_id"], row["active_version"]]
                    for row in source_scope
                ],
            }
        ),
        supported=supported,
        exact_fields=exact_fields,
        pending_inputs=len(pending),
        failed_inputs=sum(row["coverage_state"] == "failed" for row in pending),
        blocked_inputs=sum(row["coverage_state"] == "blocked" for row in pending),
        waiting_source_ids=tuple(
            row["source_id"] for row in pending if row["coverage_state"] in {"pending", "blocked"}
        ),
        policy_epoch=state["policy_epoch"],
        prompt_inputs=prompt_inputs,
        eligible_support_ids=eligible_support_ids,
        uncovered_inputs=len(units) - len(prompt_inputs),
    )
