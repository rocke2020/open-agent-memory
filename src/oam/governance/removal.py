"""Exact-ID Memory and Insight removal admission and cleanup."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from oam.auth.credentials import AuthorizedUser
from oam.db.context import bind_user
from oam.db.eligibility import ELIGIBLE_UNITS


class RemovalNotFound(LookupError):
    """The typed removal target is not visible to the authorized user."""


@dataclass(frozen=True, slots=True)
class RemovalReceipt:
    target_kind: str
    target_id: str
    operation_id: str
    policy_epoch: int
    cleanup_status: str
    reused: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "target_kind": self.target_kind,
            f"{self.target_kind}_id": self.target_id,
            "status": "excluded",
            "policy_epoch": self.policy_epoch,
            "operation_id": self.operation_id,
            "cleanup_status": self.cleanup_status,
            "reused": self.reused,
        }


class RemovalRepository:
    """Commit immediate serving exclusion and enqueue non-generative cleanup."""

    def remove(
        self,
        connection,
        authority: AuthorizedUser,
        *,
        target_kind: str,
        target_id: str,
        attempts_remaining: int,
    ) -> RemovalReceipt:
        if target_kind not in {"memory", "insight"}:
            raise ValueError("unsupported removal target kind")
        if not target_id.strip():
            raise ValueError("removal target ID cannot be empty")
        if attempts_remaining <= 0:
            raise ValueError("removal attempts must be positive")
        if not authority.allows("source:delete"):
            raise PermissionError("source:delete is required")

        key = (authority.tenant_id, authority.user_id)
        with connection.transaction():
            cursor = connection.cursor()
            bind_user(cursor, authority)
            cursor.execute(
                "SELECT accepted_seq,policy_epoch FROM users "
                "WHERE tenant_id=%s AND user_id=%s AND state='active' FOR UPDATE",
                key,
            )
            owner = cursor.fetchone()
            if owner is None:
                raise RemovalNotFound(target_id)

            cursor.execute(
                "SELECT operation_id,stage_state,status FROM operations "
                "WHERE tenant_id=%s AND user_id=%s AND removal_target_kind=%s "
                "AND removal_target_id=%s ORDER BY created_at LIMIT 1",
                (*key, target_kind, target_id),
            )
            existing = cursor.fetchone()
            if existing is not None:
                state = existing[1] or {}
                return RemovalReceipt(
                    target_kind,
                    target_id,
                    str(existing[0]),
                    int(state.get("policy_epoch", owner[1])),
                    _cleanup_status(state, str(existing[2])),
                    reused=True,
                )

            target = self._target(cursor, key, target_kind, target_id)
            if target is None:
                raise RemovalNotFound(target_id)

            accepted_seq = int(owner[0]) + 1
            policy_epoch = int(owner[1]) + 1
            operation_id = "op_remove_" + secrets.token_hex(12)
            work_unit_id = "work_remove_" + secrets.token_hex(12)
            affected = self._affected_insights(cursor, key, target_kind, target_id)
            repair_inputs = self._repair_inputs(cursor, key, affected) if affected else {}
            state = {
                "policy_epoch": policy_epoch,
                "target_kind": target_kind,
                "target_id": target_id,
                "cleanup_status": "accepted",
                "affected_insight_ids": affected,
                "repair_inputs": repair_inputs,
                "repair_status": "pending" if target_kind == "memory" and affected else "none",
                "model_calls": 0,
            }
            cursor.execute(
                "UPDATE users SET accepted_seq=%s,policy_epoch=%s,projection_epoch="
                "projection_epoch+1 WHERE tenant_id=%s AND user_id=%s",
                (accepted_seq, policy_epoch, *key),
            )
            if target_kind == "memory":
                cursor.execute(
                    "UPDATE memories SET lifecycle_state='excluded',visible_to=%s "
                    "WHERE tenant_id=%s AND user_id=%s AND memory_id=%s",
                    (accepted_seq, *key, target_id),
                )
                if affected:
                    cursor.execute(
                        "UPDATE insights SET visible_to=%s WHERE tenant_id=%s AND user_id=%s "
                        "AND insight_id=ANY(%s) AND visible_to IS NULL",
                        (accepted_seq, *key, affected),
                    )
            else:
                cursor.execute(
                    "UPDATE insights SET visible_to=%s WHERE tenant_id=%s AND user_id=%s "
                    "AND insight_id=%s AND visible_to IS NULL",
                    (accepted_seq, *key, target_id),
                )
            # A removed projection invalidates every published Profile snapshot conservatively.
            cursor.execute(
                "UPDATE profiles SET visible_to=%s WHERE tenant_id=%s AND user_id=%s "
                "AND visible_to IS NULL",
                (accepted_seq, *key),
            )
            cursor.execute(
                "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,accepted_seq,"
                "kind,schema_version,status,plan_sealed,stage_state,removal_target_kind,"
                "removal_target_id) VALUES (%s,%s,%s,%s,%s,%s,1,'accepted',true,%s,%s,%s)",
                (
                    *key,
                    operation_id,
                    authority.principal_id,
                    accepted_seq,
                    f"remove_{target_kind}",
                    Jsonb(state),
                    target_kind,
                    target_id,
                ),
            )
            cursor.execute(
                "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
                "input_digest,generation_id,status,attempts_remaining) VALUES "
                "(%s,%s,%s,%s,%s,%s,'removal','queued',%s)",
                (
                    *key,
                    work_unit_id,
                    operation_id,
                    f"cleanup_{target_kind}",
                    hashlib.sha256(
                        json.dumps([target_kind, target_id], separators=(",", ":")).encode()
                    ).digest(),
                    attempts_remaining,
                ),
            )
            if target_kind == "memory" and affected:
                self._schedule_repairs(
                    cursor,
                    key,
                    target_id,
                    affected,
                    repair_inputs,
                    operation_id,
                    work_unit_id,
                    authority.principal_id,
                    attempts_remaining,
                )
            return RemovalReceipt(target_kind, target_id, operation_id, policy_epoch, "accepted")

    @staticmethod
    def _target(cursor, key, target_kind: str, target_id: str):
        if target_kind == "memory":
            cursor.execute(
                "SELECT memory_id FROM memories WHERE tenant_id=%s AND user_id=%s "
                "AND memory_id=%s FOR UPDATE",
                (*key, target_id),
            )
        else:
            cursor.execute(
                "SELECT insight_id FROM insights WHERE tenant_id=%s AND user_id=%s "
                "AND insight_id=%s LIMIT 1 FOR UPDATE",
                (*key, target_id),
            )
        row = cursor.fetchone()
        if row is None and target_kind == "insight":
            cursor.execute(
                "SELECT operation_id FROM operations WHERE tenant_id=%s AND user_id=%s "
                "AND kind='remove_memory' AND stage_state->'repair_inputs'->'next_revision' ? %s "
                "LIMIT 1",
                (*key, target_id),
            )
            row = cursor.fetchone()
        return row

    @staticmethod
    def _affected_insights(cursor, key, target_kind: str, target_id: str) -> list[str]:
        if target_kind == "insight":
            return []
        cursor.execute(
            "SELECT DISTINCT insight_id FROM insight_support WHERE tenant_id=%s "
            "AND user_id=%s AND memory_id=%s ORDER BY insight_id",
            (*key, target_id),
        )
        found = {str(row[0]) for row in cursor.fetchall()}
        cursor.execute(
            "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id=%s "
            "AND kind='remove_memory'",
            key,
        )
        for (state,) in cursor.fetchall():
            for insight_id, entries in (
                (state or {}).get("repair_inputs", {}).get("support", {}).items()
            ):
                if any(item.get("memory_id") == target_id for item in entries):
                    found.add(str(insight_id))
        return sorted(found)

    @staticmethod
    def _repair_inputs(cursor, key, insight_ids: list[str]) -> dict[str, object]:
        cursor.execute(
            "SELECT insight_id,max(revision) FROM insights WHERE tenant_id=%s AND user_id=%s "
            "AND insight_id=ANY(%s) GROUP BY insight_id ORDER BY insight_id",
            (*key, insight_ids),
        )
        revisions = {str(row[0]): int(row[1]) for row in cursor.fetchall()}
        cursor.execute(
            "SELECT stage_state FROM operations WHERE tenant_id=%s AND user_id=%s "
            "AND kind='remove_memory'",
            key,
        )
        pending: list[tuple[str, dict[str, object]]] = []
        for (state,) in cursor.fetchall():
            inputs = (state or {}).get("repair_inputs", {})
            for insight_id, revision in inputs.get("next_revision", {}).items():
                if insight_id in insight_ids:
                    revisions[insight_id] = max(revisions.get(insight_id, 0), int(revision))
            pending.extend(
                (insight_id, item)
                for insight_id, values in inputs.get("support", {}).items()
                if insight_id in insight_ids
                for item in values
            )
        cursor.execute(
            "SELECT insight_id,insight_revision,memory_id,support_revision "
            "FROM insight_support WHERE tenant_id=%s AND user_id=%s AND insight_id=ANY(%s) "
            "ORDER BY insight_id,insight_revision,memory_id",
            (*key, insight_ids),
        )
        support: dict[str, list[dict[str, object]]] = {insight_id: [] for insight_id in insight_ids}
        rows = list(cursor.fetchall())
        rows.extend(
            (
                insight_id,
                item["revision"],
                item["memory_id"],
                item["support_revision"],
            )
            for insight_id, item in pending
        )
        for insight_id, revision, memory_id, support_revision in rows:
            if int(revision) != revisions[str(insight_id)]:
                continue
            support.setdefault(str(insight_id), []).append(
                {
                    "revision": int(revision),
                    "memory_id": str(memory_id),
                    "support_revision": int(support_revision),
                }
            )
        return {"next_revision": revisions, "support": support}

    @staticmethod
    def _schedule_repairs(
        cursor,
        key,
        removed_id,
        affected,
        repair_inputs,
        removal_operation_id,
        cleanup_work_id,
        principal_id,
        attempts_remaining,
    ):
        cursor.execute(
            f"WITH eligible AS ({ELIGIBLE_UNITS}) "
            "SELECT memory_id,unit_id,source_id,source_version,generation_id FROM eligible "
            "WHERE kind='fact' ORDER BY memory_id,unit_id"
        )
        eligible = {row[0]: row[1:] for row in cursor.fetchall()}
        cursor.execute(
            "SELECT e.source_id,e.source_version,m.generation_id FROM support sp "
            "JOIN evidence e USING(tenant_id,user_id,evidence_id) "
            "JOIN memories m USING(tenant_id,user_id,memory_id) "
            "WHERE sp.tenant_id=%s AND sp.user_id=%s AND sp.memory_id=%s "
            "ORDER BY e.source_id,e.source_version LIMIT 1",
            (*key, removed_id),
        )
        fallback = cursor.fetchone()
        groups = {}
        reset_ids = set()
        for insight_id in affected:
            ids = sorted(
                {
                    value["memory_id"]
                    for value in repair_inputs["support"].get(insight_id, [])
                    if value["memory_id"] in eligible
                }
            )
            reset_ids.update(eligible[memory_id][0] for memory_id in ids)
            source = eligible[ids[0]][1:] if ids else fallback
            if source is None:
                raise RuntimeError("affected Insight has no source for maintenance")
            groups.setdefault(tuple(source), []).append(
                {
                    "insight_id": insight_id,
                    "next_revision": repair_inputs["next_revision"][insight_id] + 1,
                    "support_ids": ids,
                    "removal_operation_id": removal_operation_id,
                }
            )
        # Reset only current progress; completed operations remain historical evidence.
        cursor.execute(
            "SELECT operation_id,source_id,source_version,stage_state,status FROM operations "
            "WHERE tenant_id=%s AND user_id=%s AND kind='consolidate' ORDER BY created_at",
            key,
        )
        prior = cursor.fetchall()
        for operation_id, _, _, state, status in prior:
            if status in {"accepted", "running"} and reset_ids.intersection(
                (state or {}).get("l2_examined_ids", [])
            ):
                state = dict(state or {})
                state["l2_examined_ids"] = sorted(set(state.get("l2_examined_ids", [])) - reset_ids)
                state["insights_ready"] = False
                cursor.execute(
                    "UPDATE operations SET stage_state=%s WHERE tenant_id=%s AND user_id=%s "
                    "AND operation_id=%s",
                    (Jsonb(state), *key, operation_id),
                )
        linked = []
        for (source_id, version, generation), repairs in groups.items():
            operation_id = "op_repair_" + secrets.token_hex(12)
            work_id = "work_repair_" + secrets.token_hex(12)
            coverage = set()
            for _, prior_source, prior_version, state, _ in prior:
                if (prior_source, prior_version) == (source_id, version):
                    coverage.update((state or {}).get("l2_examined_ids", []))
            state = {
                "insight_repairs": repairs,
                "l2_examined_ids": sorted(coverage - reset_ids),
                "insights_ready": False,
            }
            cursor.execute(
                "SELECT operation_id,accepted_seq FROM operations WHERE tenant_id=%s "
                "AND user_id=%s AND source_id=%s AND source_version=%s "
                "AND kind='index_interaction' ORDER BY created_at DESC LIMIT 1",
                (*key, source_id, version),
            )
            base = cursor.fetchone()
            if base:
                state["source_operation_id"] = base[0]
                cursor.execute(
                    "UPDATE operations SET stage_state=stage_state||%s WHERE tenant_id=%s "
                    "AND user_id=%s AND operation_id=%s",
                    (
                        Jsonb(
                            {
                                "insights_ready": False,
                                "consolidation_status": "pending",
                                "consolidation_operation_id": operation_id,
                            }
                        ),
                        *key,
                        base[0],
                    ),
                )
            cursor.execute(
                "INSERT INTO operations(tenant_id,user_id,operation_id,principal_id,source_id,"
                "source_version,accepted_seq,kind,schema_version,status,plan_sealed,stage_state) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'consolidate',1,'accepted',true,%s)",
                (
                    *key,
                    operation_id,
                    principal_id,
                    source_id,
                    version,
                    base[1] if base else 1,
                    Jsonb(state),
                ),
            )
            cursor.execute(
                "INSERT INTO work_units(tenant_id,user_id,work_unit_id,operation_id,stage,"
                "input_digest,generation_id,dependency_ids,status,attempts_remaining) "
                "VALUES (%s,%s,%s,%s,'consolidate',%s,%s,%s,'queued',%s)",
                (
                    *key,
                    work_id,
                    operation_id,
                    hashlib.sha256(json.dumps(repairs, sort_keys=True).encode()).digest(),
                    generation,
                    [cleanup_work_id],
                    attempts_remaining,
                ),
            )
            linked.append(operation_id)
        cursor.execute(
            "UPDATE operations SET stage_state=stage_state||%s WHERE tenant_id=%s "
            "AND user_id=%s AND operation_id=%s",
            (Jsonb({"consolidation_operation_ids": linked}), *key, removal_operation_id),
        )


def _cleanup_status(state: dict, operation_status: str) -> str:
    value = state.get("cleanup_status")
    if isinstance(value, str):
        return value
    return "complete" if operation_status == "succeeded" else operation_status
