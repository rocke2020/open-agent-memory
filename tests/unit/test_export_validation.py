"""Downloaded export members are verified without a running OAM service."""

import hashlib
import json
from collections import Counter

import pytest

from oam.exports.validation import ExportValidationError, validate_directory


def test_validator_checks_part_and_original_bytes(tmp_path):
    records = b'{"kind":"source","source_id":"one","source_version":1,"original_index":0}\n'
    original = b"original bytes"
    (tmp_path / "records").mkdir()
    (tmp_path / "originals").mkdir()
    (tmp_path / "records/000000.jsonl").write_bytes(records)
    (tmp_path / "originals/000000.bin").write_bytes(original)
    manifest = {
        "format_version": 1,
        "counts": {"source": 1},
        "records_sha256": hashlib.sha256(records).hexdigest(),
        "parts": [
            {
                "index": 0,
                "path": "records/000000.jsonl",
                "byte_length": len(records),
                "sha256": hashlib.sha256(records).hexdigest(),
            }
        ],
        "originals": [
            {
                "index": 0,
                "path": "originals/000000.bin",
                "source_id": "one",
                "source_version": 1,
                "byte_length": len(original),
                "sha256": hashlib.sha256(original).hexdigest(),
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert validate_directory(tmp_path) == {"source": 1}

    (tmp_path / "originals/000000.bin").write_bytes(b"changed")
    with pytest.raises(ExportValidationError, match="original"):
        validate_directory(tmp_path)


def test_validator_rejects_missing_support_target(tmp_path):
    records = b'{"kind":"support","memory_id":"absent","evidence_id":"none","passage_id":"none"}\n'
    (tmp_path / "records").mkdir()
    (tmp_path / "records/000000.jsonl").write_bytes(records)
    manifest = {
        "format_version": 1,
        "counts": {"support": 1},
        "records_sha256": hashlib.sha256(records).hexdigest(),
        "parts": [
            {
                "index": 0,
                "path": "records/000000.jsonl",
                "byte_length": len(records),
                "sha256": hashlib.sha256(records).hexdigest(),
            }
        ],
        "originals": [],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ExportValidationError, match="support"):
        validate_directory(tmp_path)


def _write_records(tmp_path, records):
    records = [
        {"kind": "source", "source_id": "s", "source_version": 1, "original_index": 0},
        *records,
    ]
    data = b"".join(json.dumps(record).encode() + b"\n" for record in records)
    (tmp_path / "records").mkdir()
    (tmp_path / "originals").mkdir()
    (tmp_path / "records/000000.jsonl").write_bytes(data)
    (tmp_path / "originals/000000.bin").write_bytes(b"")
    manifest = {
        "format_version": 1,
        "counts": dict(Counter(record["kind"] for record in records)),
        "records_sha256": hashlib.sha256(data).hexdigest(),
        "parts": [
            {
                "index": 0,
                "path": "records/000000.jsonl",
                "byte_length": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        ],
        "originals": [
            {
                "index": 0,
                "path": "originals/000000.bin",
                "source_id": "s",
                "source_version": 1,
                "byte_length": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_validator_rejects_memory_without_support(tmp_path):
    _write_records(tmp_path, [{"kind": "memory", "memory_id": "orphan"}])
    with pytest.raises(ExportValidationError, match="support"):
        validate_directory(tmp_path)


def test_validator_rejects_support_with_wrong_evidence_passage_pair(tmp_path):
    _write_records(
        tmp_path,
        [
            {"kind": "evidence", "evidence_id": "first", "source_id": "s", "source_version": 1},
            {"kind": "evidence", "evidence_id": "second", "source_id": "s", "source_version": 1},
            {"kind": "passage", "passage_id": "p", "evidence_id": "first"},
            {"kind": "memory", "memory_id": "m"},
            {"kind": "support", "memory_id": "m", "evidence_id": "second", "passage_id": "p"},
        ],
    )
    with pytest.raises(ExportValidationError, match="support"):
        validate_directory(tmp_path)


def test_validator_rejects_insight_without_two_supported_memories(tmp_path):
    _write_records(tmp_path, [{"kind": "insight", "insight_id": "i", "revision": 1}])
    with pytest.raises(ExportValidationError, match="Insight support"):
        validate_directory(tmp_path)


def test_validator_rejects_profile_field_with_missing_resource_support(tmp_path):
    _write_records(
        tmp_path,
        [
            {
                "kind": "profile",
                "profile_revision": None,
                "freshness": "missing",
                "policy_epoch": 1,
                "sections": {
                    "main": [
                        {
                            "field_id": "pf_test",
                            "supports": [
                                {
                                    "kind": "profile_resource",
                                    "resource_id": "missing",
                                    "resource_version": 1,
                                }
                            ],
                        }
                    ],
                    "preferences": [],
                    "experiences": [],
                },
            }
        ],
    )
    manifest_path = tmp_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["policy_epoch"] = 1
    manifest["profiles"] = [
        {"profile_id": "user", "profile_revision": None, "freshness": "missing"}
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ExportValidationError, match="Profile support"):
        validate_directory(tmp_path)
