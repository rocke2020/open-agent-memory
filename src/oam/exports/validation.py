"""Validate a downloaded portable export directory."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
ENTITY_RECORD_KINDS = frozenset(
    {"entity", "entity_name", "entity_name_support", "entity_membership"}
)


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


def _validate_entity_records(records: list[dict], generation: str | None) -> None:
    selected = [record for record in records if record["kind"] in ENTITY_RECORD_KINDS]
    if not selected:
        return
    if not isinstance(generation, str) or not generation.strip():
        raise ExportValidationError("entity generation is missing")

    def indexed(kind: str, field: str) -> dict[str, dict]:
        result = {}
        for record in selected:
            if record["kind"] != kind:
                continue
            identifier = record[field]
            if not isinstance(identifier, str) or not identifier.strip() or identifier in result:
                raise ExportValidationError("entity record identity is invalid or duplicated")
            result[identifier] = record
        return result

    entities = indexed("entity", "entity_id")
    names = indexed("entity_name", "name_id")
    indexed("entity_name_support", "support_id")
    memberships = [record for record in selected if record["kind"] == "entity_membership"]
    memories = {record["memory_id"]: record for record in records if record["kind"] == "memory"}
    passages = {record["passage_id"]: record for record in records if record["kind"] == "passage"}
    fact_support = {
        (record["memory_id"], record["passage_id"]): record.get("span")
        for record in records
        if record["kind"] == "support"
    }
    for record in selected:
        if not isinstance(record["generation_id"], str) or not record["generation_id"].strip():
            raise ExportValidationError("entity generation is missing")
    for entity in entities.values():
        if not isinstance(entity["canonical_name"], str) or not entity["canonical_name"].strip():
            raise ExportValidationError("entity canonical name is invalid")
    for name_record in names.values():
        if (
            name_record["entity_id"] not in entities
            or name_record["generation_id"] != entities[name_record["entity_id"]]["generation_id"]
            or name_record["name_kind"] not in {"canonical", "alias"}
            or any(
                not isinstance(name_record[key], str) or not name_record[key].strip()
                for key in ("name_text", "name_key")
            )
        ):
            raise ExportValidationError("entity name identity or kind is invalid")

    supported_names = set()
    for support in selected:
        if support["kind"] != "entity_name_support":
            continue
        name = names.get(support["name_id"])
        memory = memories.get(support["memory_id"])
        passage = passages.get(support["passage_id"])
        quote = support["quote"]
        span = fact_support.get((support["memory_id"], support["passage_id"]))
        if (
            name is None
            or name["entity_id"] != support["entity_id"]
            or name["generation_id"] != support["generation_id"]
            or name["name_kind"] != support["support_kind"]
            or memory is None
            or memory.get("generation_id") != support["generation_id"]
            or (support["memory_id"], support["passage_id"]) not in fact_support
            or passage is None
            or not isinstance(passage.get("text"), str)
            or not isinstance(quote, str)
            or not quote.strip()
            or quote not in passage["text"]
            or not isinstance(span, dict)
            or not isinstance(span.get("quote"), str)
            or quote not in span["quote"]
        ):
            raise ExportValidationError("entity name support reference or quote is invalid")
        anchor = support["anchor_name_id"]
        if support["support_kind"] == "canonical":
            if anchor is not None:
                raise ExportValidationError("entity canonical support cannot have an alias anchor")
        elif (
            anchor not in names
            or names[anchor]["name_kind"] != "canonical"
            or names[anchor]["entity_id"] != support["entity_id"]
        ):
            raise ExportValidationError("entity alias anchor is invalid")
        supported_names.add(support["name_id"])
    if set(names) != supported_names or set(entities) != {
        name["entity_id"] for name in names.values()
    }:
        raise ExportValidationError("entity name or identity lacks source support")

    seen_memberships = set()
    for member in memberships:
        name = names.get(member["name_id"])
        memory = memories.get(member["memory_id"])
        identity = (member["unit_id"], member["entity_id"])
        quote = member["mention_quote"]
        if (
            identity in seen_memberships
            or not isinstance(member["unit_id"], str)
            or not member["unit_id"].strip()
            or name is None
            or name["entity_id"] != member["entity_id"]
            or name["generation_id"] != member["generation_id"]
            or memory is None
            or memory.get("generation_id") != member["generation_id"]
            or not isinstance(member["observed_mention"], str)
            or not member["observed_mention"].strip()
            or not isinstance(quote, str)
            or not quote.strip()
            or not any(
                quote in passages[passage_id].get("text", "")
                and isinstance(span, dict)
                and isinstance(span.get("quote"), str)
                and quote in span["quote"]
                for (memory_id, passage_id), span in fact_support.items()
                if memory_id == member["memory_id"] and passage_id in passages
            )
        ):
            raise ExportValidationError("entity membership reference or quote is invalid")
        seen_memberships.add(identity)


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
        all_records: list[dict] = []
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
        insight_members: dict[tuple[str, int], set[str]] = {}
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
        memory_support: Counter[str] = Counter()
        insight_support: dict[tuple[str, int], set[str]] = {}
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
                                insight_key = (
                                    ref.get("insight_id", support_id),
                                    ref.get("revision"),
                                )
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
            elif record["kind"] not in {"memory", "insight"} | ENTITY_RECORD_KINDS:
                raise ExportValidationError("unsupported export record kind")
        if any(memory_support[memory_id] == 0 for memory_id in memory_ids):
            raise ExportValidationError("memory support is missing")
        if any(len(insight_support.get(insight_id, ())) < 2 for insight_id in insight_ids):
            raise ExportValidationError("Insight support is incomplete")
        _validate_entity_records(all_records, manifest.get("generation_id"))
        return counts
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, OSError) as exc:
        raise ExportValidationError("export manifest or records are invalid") from exc
