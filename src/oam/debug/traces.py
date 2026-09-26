"""Recorded recall traces under current source policy."""

from __future__ import annotations

import hashlib
import json

from oam.auth.credentials import AuthorizedUser
from oam.db.context import bind_user
from oam.db.eligibility import ELIGIBLE_UNITS
from oam.tokens import ESTIMATOR, estimate_units, estimated_tokens


class TraceNotFound(LookupError):
    """No retained trace is available to this user."""


class TraceRepository:
    def __init__(self, *, content_retention_days: int | None = None):
        self.content_retention_days = content_retention_days

    def get(self, connection, authority: AuthorizedUser, trace_id: str) -> dict:
        if not authority.allows("debug:read"):
            raise PermissionError("debug:read is required")
        with connection.transaction():
            bind_user(connection.cursor(), authority)
            owner = connection.execute(
                "SELECT policy_epoch FROM users WHERE tenant_id=%s AND user_id=%s "
                "AND state='active'",
                (authority.tenant_id, authority.user_id),
            ).fetchone()
            if owner is None:
                raise TraceNotFound(trace_id)
            row = connection.execute(
                "SELECT decisions,content,created_at,generation_id,read_seq "
                "FROM traces WHERE tenant_id=%s AND user_id=%s AND trace_id=%s "
                "AND expires_at>clock_timestamp()",
                (authority.tenant_id, authority.user_id, trace_id),
            ).fetchone()
            if row is None:
                raise TraceNotFound(trace_id)
            decisions, result, created_at, generation_id, read_seq = row
            content_expired = (
                self.content_retention_days is not None
                and connection.execute(
                    "SELECT %s < clock_timestamp() - (%s * interval '1 day')",
                    (created_at, self.content_retention_days),
                ).fetchone()[0]
            )
            if content_expired:
                return {"trace_id": trace_id, "status": "artifacts_expired"}
            if not decisions or result is None:
                return {"trace_id": trace_id, "status": "policy_redacted"}
            candidate_ids = {
                item_id for ids in decisions.get("arms", {}).values() for item_id in ids
            }
            candidate_ids.update(item["unit_id"] for item in result.get("items", []))
            if candidate_ids:
                rows = connection.execute(
                    f"WITH eligible AS NOT MATERIALIZED ({ELIGIBLE_UNITS}) "
                    "SELECT unit_id FROM eligible WHERE unit_id=ANY(%s)",
                    (list(candidate_ids),),
                ).fetchall()
                if {item[0] for item in rows} != candidate_ids:
                    return {"trace_id": trace_id, "status": "policy_redacted"}
            final = connection.execute(
                "SELECT policy_epoch FROM users WHERE tenant_id=%s AND user_id=%s "
                "AND state='active' FOR SHARE",
                (authority.tenant_id, authority.user_id),
            ).fetchone()
            if final is None or final[0] != owner[0]:
                return {"trace_id": trace_id, "status": "policy_redacted"}
            return {
                "trace_id": trace_id,
                "status": "recorded",
                "read_seq": read_seq,
                "generation_id": generation_id,
                "decisions": decisions,
                "result": result,
            }

    def replay(self, connection, authority: AuthorizedUser, trace_id: str, mode: str) -> dict:
        recorded = self.get(connection, authority, trace_id)
        if recorded["status"] != "recorded":
            return recorded
        result = recorded["result"]
        if mode == "inspect_recorded":
            return {"trace_id": trace_id, "status": "exact", "result": result}
        if mode != "reassemble_recorded":
            raise ValueError("unsupported recorded replay mode")
        blocks = []
        for item in result["items"]:
            citations = json.dumps(item["citations"], ensure_ascii=False, separators=(",", ":"))
            time_evidence = json.dumps(
                item["time_evidence"], ensure_ascii=False, separators=(",", ":")
            )
            blocks.append(
                f"[{item['unit_id']}] {item['kind']} {item['origin']}\n{item['text']}\n"
                f"Citations: {citations}\nTime: {time_evidence}"
            )
        context = "\n\n".join(blocks)
        units = sum(estimate_units(block) for block in blocks) + max(0, len(blocks) - 1) * 2
        tokens = estimated_tokens(units)
        exact = (
            context == result["context"]
            and tokens == result["context_tokens"]
            and result["tokenizer"] == ESTIMATOR
        )
        return {
            "trace_id": trace_id,
            "status": "exact" if exact else "divergent",
            "comparison": {
                "recorded_sha256": hashlib.sha256(result["context"].encode()).hexdigest(),
                "reassembled_sha256": hashlib.sha256(context.encode()).hexdigest(),
                "recorded_tokens": result["context_tokens"],
                "reassembled_tokens": tokens,
            },
            "result": result if exact else None,
        }

    def diff(self, connection, authority: AuthorizedUser, before: str, after: str) -> dict:
        earlier = self.get(connection, authority, before)
        later = self.get(connection, authority, after)
        if earlier["status"] != "recorded" or later["status"] != "recorded":
            return {"status": "policy_redacted", "before": before, "after": after}
        old_ids = {item["unit_id"] for item in earlier["result"]["items"]}
        new_ids = {item["unit_id"] for item in later["result"]["items"]}
        return {
            "status": "comparable",
            "before": before,
            "after": after,
            "added": sorted(new_ids - old_ids),
            "removed": sorted(old_ids - new_ids),
            "context_changed": earlier["result"]["context"] != later["result"]["context"],
        }
