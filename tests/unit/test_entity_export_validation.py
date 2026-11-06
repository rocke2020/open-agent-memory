"""Portable entity bindings must retain typed, same-generation source witnesses."""

import json

import pytest
from test_export_validation import _write_records

from oam.exports.validation import ExportValidationError, validate_directory


def entity_records():
    return [
        {"kind": "evidence", "evidence_id": "e", "source_id": "s", "source_version": 1},
        {
            "kind": "passage",
            "passage_id": "p",
            "evidence_id": "e",
            "text": "Nova Research, also called NR, opened a laboratory.",
        },
        {"kind": "memory", "memory_id": "m", "generation_id": "g"},
        {
            "kind": "support",
            "memory_id": "m",
            "evidence_id": "e",
            "passage_id": "p",
            "span": {"quote": "Nova Research, also called NR, opened a laboratory."},
        },
        {
            "kind": "entity",
            "entity_id": "entity",
            "generation_id": "g",
            "canonical_name": "Nova Research",
        },
        {
            "kind": "entity_name",
            "name_id": "canonical",
            "entity_id": "entity",
            "generation_id": "g",
            "name_text": "Nova Research",
            "name_key": "nova research",
            "name_kind": "canonical",
        },
        {
            "kind": "entity_name",
            "name_id": "alias",
            "entity_id": "entity",
            "generation_id": "g",
            "name_text": "NR",
            "name_key": "nr",
            "name_kind": "alias",
        },
        {
            "kind": "entity_name_support",
            "support_id": "canonical-support",
            "name_id": "canonical",
            "entity_id": "entity",
            "generation_id": "g",
            "memory_id": "m",
            "passage_id": "p",
            "quote": "Nova Research, also called NR",
            "support_kind": "canonical",
            "anchor_name_id": None,
        },
        {
            "kind": "entity_name_support",
            "support_id": "alias-support",
            "name_id": "alias",
            "entity_id": "entity",
            "generation_id": "g",
            "memory_id": "m",
            "passage_id": "p",
            "quote": "Nova Research, also called NR",
            "support_kind": "alias",
            "anchor_name_id": "canonical",
        },
        {
            "kind": "entity_membership",
            "unit_id": "unit",
            "memory_id": "m",
            "entity_id": "entity",
            "name_id": "alias",
            "generation_id": "g",
            "observed_mention": "NR",
            "mention_quote": "Nova Research, also called NR",
        },
    ]


def write_export(tmp_path, records):
    _write_records(tmp_path, records)
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["generation_id"] = "g"
    manifest_path.write_text(json.dumps(manifest))


def test_validator_accepts_witnessed_entity_aliases(tmp_path):
    write_export(tmp_path, entity_records())
    counts = validate_directory(tmp_path)
    assert counts["entity"] == 1
    assert counts["entity_name"] == 2
    assert counts["entity_name_support"] == 2
    assert counts["entity_membership"] == 1


def test_validator_accepts_witnessed_entities_from_an_older_generation(tmp_path):
    records = entity_records()
    for record in records:
        if "generation_id" in record:
            record["generation_id"] = "old"
    write_export(tmp_path, records)
    assert validate_directory(tmp_path)["entity"] == 1


@pytest.mark.parametrize(
    ("kind", "field", "bad"),
    [
        ("entity", "generation_id", "old"),
        ("entity_name", "entity_id", "missing"),
        ("entity_name", "generation_id", "old"),
        ("entity_name", "name_kind", "guessed"),
        ("entity_name_support", "memory_id", "missing"),
        ("entity_name_support", "passage_id", "missing"),
        ("entity_name_support", "generation_id", "old"),
        ("entity_name_support", "support_kind", "inferred"),
        ("entity_name_support", "quote", "not present in source"),
        ("entity_membership", "memory_id", "missing"),
        ("entity_membership", "name_id", "missing"),
        ("entity_membership", "entity_id", "missing"),
        ("entity_membership", "generation_id", "old"),
    ],
)
def test_validator_rejects_invalid_entity_binding(tmp_path, kind, field, bad):
    records = entity_records()
    next(r for r in records if r["kind"] == kind)[field] = bad
    write_export(tmp_path, records)
    with pytest.raises(ExportValidationError, match="entity"):
        validate_directory(tmp_path)


def test_validator_rejects_alias_anchor_to_an_alias(tmp_path):
    records = entity_records()
    next(r for r in records if r.get("support_id") == "alias-support")["anchor_name_id"] = "alias"
    write_export(tmp_path, records)
    with pytest.raises(ExportValidationError, match="entity"):
        validate_directory(tmp_path)


def test_validator_rejects_unsupported_exported_name(tmp_path):
    records = [r for r in entity_records() if r.get("support_id") != "alias-support"]
    write_export(tmp_path, records)
    with pytest.raises(ExportValidationError, match="entity"):
        validate_directory(tmp_path)


@pytest.mark.parametrize("witness", ["canonical-support", "alias-support", "membership"])
def test_validator_rejects_entity_quote_outside_owning_fact_support(tmp_path, witness):
    records = entity_records()
    unrelated = "Another team uses NR for North Ridge."
    next(record for record in records if record["kind"] == "passage")["text"] += " " + unrelated
    record = next(
        record
        for record in records
        if record.get("support_id") == witness
        or (witness == "membership" and record["kind"] == "entity_membership")
    )
    record["mention_quote" if witness == "membership" else "quote"] = unrelated
    write_export(tmp_path, records)

    with pytest.raises(ExportValidationError, match="entity"):
        validate_directory(tmp_path)


@pytest.mark.parametrize(
    "change", [{"name_id": "withdrawn"}, {"kind": "canonical"}, {"quote": "invented"}]
)
def test_validator_rejects_invalid_nested_entity_mention(tmp_path, change):
    records = entity_records()
    member = next(r for r in records if r["kind"] == "entity_membership")
    member["source_mentions"] = [
        {
            "name_id": "alias",
            "name": "NR",
            "kind": "alias",
            "quote": "Nova Research, also called NR",
            "span": [26, 28],
            **change,
        }
    ]
    write_export(tmp_path, records)
    with pytest.raises(ExportValidationError, match="entity"):
        validate_directory(tmp_path)
