"""Validate a downloaded portable export directory."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

MAX_MANIFEST_BYTES = 8 * 1024 * 1024


class ExportValidationError(ValueError):
    """An export member or manifest is invalid."""


def _member(root: Path, path: str) -> bytes:
    member = root / path
    if not member.resolve().is_relative_to(root.resolve()) or not member.is_file():
        raise ExportValidationError("export member path is unavailable or unsafe")
    return member.read_bytes()


def _verified_member(root: Path, descriptor: dict, expected_path: str, label: str) -> bytes:
    if descriptor.get("path") != expected_path:
        raise ExportValidationError(f"export {label} path does not match its index")
    content = _member(root, expected_path)
    if len(content) != descriptor.get("byte_length"):
        raise ExportValidationError(f"export {label} length mismatch")
    if hashlib.sha256(content).hexdigest() != descriptor.get("sha256"):
        raise ExportValidationError(f"export {label} checksum mismatch")
    return content


def validate_directory(path: str | Path) -> dict[str, int]:
    root = Path(path)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ExportValidationError("export manifest is missing or too large")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["format_version"] != 1:
            raise ExportValidationError("unsupported export format version")
        parts = manifest["parts"]
        originals = manifest["originals"]
        all_records = []
        records_hash = hashlib.sha256()
        for index, descriptor in enumerate(parts):
            if descriptor["index"] != index:
                raise ExportValidationError("export part indexes are not contiguous")
            content = _verified_member(root, descriptor, f"records/{index:06d}.jsonl", "part")
            if content and not content.endswith(b"\n"):
                raise ExportValidationError("export JSONL part is incomplete")
            records_hash.update(content)
            all_records.extend(json.loads(line) for line in content.splitlines())
        if records_hash.hexdigest() != manifest["records_sha256"]:
            raise ExportValidationError("export record checksum mismatch")
        for index, descriptor in enumerate(originals):
            if descriptor["index"] != index:
                raise ExportValidationError("export original indexes are not contiguous")
            _verified_member(root, descriptor, f"originals/{index:06d}.bin", "original")
        counts = dict(sorted(Counter(record["kind"] for record in all_records).items()))
        if counts != manifest["counts"]:
            raise ExportValidationError("export record counts do not match manifest")
        source_ids = {
            (record["source_id"], record["source_version"])
            for record in all_records
            if record["kind"] == "source"
        }
        source_roles = {
            (record["source_id"], record["source_version"]): record.get("input_role")
            for record in all_records
            if record["kind"] == "source"
        }
        evidence_ids = {
            record["evidence_id"] for record in all_records if record["kind"] == "evidence"
        }
        passage_evidence = {
            record["passage_id"]: record["evidence_id"]
            for record in all_records
            if record["kind"] == "passage"
        }
        evidence_sources = {
            record["evidence_id"]: (record["source_id"], record["source_version"])
            for record in all_records
            if record["kind"] == "evidence"
        }
        passage_sources = {
            passage_id: evidence_sources.get(evidence_id)
            for passage_id, evidence_id in passage_evidence.items()
        }
        fact_support_passages = {
            (record["memory_id"], record["passage_id"])
            for record in all_records
            if record["kind"] == "support"
        }
        insight_members = {}
        for record in all_records:
            if record["kind"] == "insight_support":
                insight_members.setdefault(
                    (record["insight_id"], record["insight_revision"]), set()
                ).add(record["memory_id"])
        memory_ids = {record["memory_id"] for record in all_records if record["kind"] == "memory"}
        insight_ids = {
            (record["insight_id"], record["revision"])
            for record in all_records
            if record["kind"] == "insight"
        }
        profiles = [record for record in all_records if record["kind"] == "profile"]
        profile_manifest = manifest.get("profiles", [])
        if len(profiles) != len(profile_manifest) or len(profiles) > 1:
            raise ExportValidationError("Profile manifest does not match records")
        if profiles and profile_manifest[0] != {
            "profile_id": "user",
            "profile_revision": profiles[0]["profile_revision"],
            "freshness": profiles[0]["freshness"],
        }:
            raise ExportValidationError("Profile manifest does not match records")
        memory_support = Counter()
        insight_support = {}
        for record in all_records:
            if record["kind"] == "source":
                index = record["original_index"]
                if not isinstance(index, int) or index < 0 or index >= len(originals):
                    raise ExportValidationError("source has no original member")
                original = originals[index]
                if (record["source_id"], record["source_version"]) != (
                    original["source_id"],
                    original["source_version"],
                ):
                    raise ExportValidationError("source original identity mismatch")
            elif record["kind"] == "evidence":
                if (record["source_id"], record["source_version"]) not in source_ids:
                    raise ExportValidationError("evidence source reference is missing")
            elif record["kind"] == "passage":
                if record["evidence_id"] not in evidence_ids:
                    raise ExportValidationError("passage evidence reference is missing")
            elif record["kind"] == "support":
                if (
                    record["memory_id"] not in memory_ids
                    or record["evidence_id"] not in evidence_ids
                    or passage_evidence.get(record["passage_id"]) != record["evidence_id"]
                ):
                    raise ExportValidationError("support reference is missing")
                memory_support[record["memory_id"]] += 1
            elif record["kind"] == "insight_support":
                if (record["insight_id"], record["insight_revision"]) not in insight_ids or record[
                    "memory_id"
                ] not in memory_ids:
                    raise ExportValidationError("Insight support reference is missing")
                insight_support.setdefault(
                    (record["insight_id"], record["insight_revision"]), set()
                ).add(record["memory_id"])
            elif record["kind"] == "resource_abstract":
                if (record["source_id"], record["source_version"]) not in source_ids:
                    raise ExportValidationError("Resource abstract source reference is missing")
            elif record["kind"] == "profile":
                if "content" in record:
                    if record["policy_epoch"] != manifest["policy_epoch"]:
                        raise ExportValidationError("Profile state does not match manifest")
                    content = record["content"]
                    references = record["references"]
                    support_ids = record["support_ids"]
                    if (
                        (
                            content is not None
                            and (not isinstance(content, str) or not content.strip())
                        )
                        or not isinstance(references, list)
                        or not isinstance(support_ids, list)
                    ):
                        raise ExportValidationError("Profile content is invalid")
                    if content is None and (references or support_ids):
                        raise ExportValidationError("Profile support is missing")
                    if [ref.get("support_id") for ref in references] != support_ids:
                        raise ExportValidationError("Profile support is missing")
                    for ref in references:
                        if ref.get("kind") == "profile_resource":
                            expected_support_id = (
                                f"profile_resource:{ref.get('resource_id')}:"
                                f"{ref.get('resource_version')}"
                            )
                            valid = (
                                source_roles.get(
                                    (ref.get("resource_id"), ref.get("resource_version"))
                                )
                                == "profile_resource"
                                and ref.get("support_id") == expected_support_id
                            )
                        else:
                            passages = ref.get("passages") or [ref]
                            support_id = ref.get("support_id")
                            if ref.get("kind") == "fact":
                                supported = support_id in memory_ids
                                member_ids = {support_id}
                            elif ref.get("kind") == "insight":
                                insight_key = (support_id, ref.get("revision"))
                                supported = insight_key in insight_ids
                                member_ids = insight_members.get(insight_key, set())
                            else:
                                supported = False
                                member_ids = set()
                            valid = (
                                supported
                                and isinstance(passages, list)
                                and bool(passages)
                                and all(
                                    passage_sources.get(passage.get("passage_id"))
                                    == (passage.get("source_id"), passage.get("source_version"))
                                    and any(
                                        (memory_id, passage.get("passage_id"))
                                        in fact_support_passages
                                        for memory_id in member_ids
                                    )
                                    for passage in passages
                                )
                            )
                        if not valid:
                            raise ExportValidationError("Profile support is missing")
                    continue
                if record["policy_epoch"] != manifest["policy_epoch"] or set(
                    record["sections"]
                ) != {"main", "preferences", "experiences"}:
                    raise ExportValidationError("Profile state does not match manifest")
                for fields in record["sections"].values():
                    if not isinstance(fields, list):
                        raise ExportValidationError("Profile fields are invalid")
                    for field in fields:
                        supports = field.get("supports")
                        if not supports:
                            raise ExportValidationError("Profile support is missing")
                        for support in supports:
                            kind = support.get("kind")
                            if kind == "profile_resource":
                                valid = (
                                    source_roles.get(
                                        (
                                            support.get("resource_id"),
                                            support.get("resource_version"),
                                        )
                                    )
                                    == "profile_resource"
                                )
                            elif kind == "fact":
                                valid = support.get("memory_id") in memory_ids
                            elif kind == "insight":
                                valid = (
                                    support.get("insight_id"),
                                    support.get("revision"),
                                ) in insight_ids
                            else:
                                valid = False
                            if not valid:
                                raise ExportValidationError("Profile support is missing")
            elif record["kind"] not in {"memory", "insight"}:
                raise ExportValidationError("unsupported export record kind")
        if any(memory_support[memory_id] == 0 for memory_id in memory_ids):
            raise ExportValidationError("memory support is missing")
        if any(len(insight_support.get(insight_id, ())) < 2 for insight_id in insight_ids):
            raise ExportValidationError("Insight support is incomplete")
        return counts
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, OSError) as exc:
        raise ExportValidationError("export manifest or records are invalid") from exc
