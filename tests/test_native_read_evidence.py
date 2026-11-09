# SPDX-License-Identifier: MIT
"""Tests for portable, content-addressed native read-path evidence."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from scripts.native_read_evidence import build_evidence_report
from tests.native_parity_fixtures import (
    NTFS_FIXTURE_PATH,
    NTFS_FIXTURE_SHA256,
    NTFS_IMAGE_SHA256,
    NTFS_PAYLOAD,
)

_EXPECTED_READ_PROFILES = {
    "fat12-native-ro",
    "fat16-native-ro",
    "fat32-native-ro",
    "exfat-native-ro",
    "ext2-native-ro",
    "ext3-native-ro",
    "ext4-native-ro",
    "ntfs-native-ro",
}
_EXPECTED_OPERATIONS = {"list", "stat", "read-file", "read-range", "extract"}


def test_ntfs_fixture_matches_content_addressed_manifest():
    manifest_path = Path("tests/fixtures/ntfs-readonly-32m.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    compressed = NTFS_FIXTURE_PATH.read_bytes()
    raw_image = gzip.decompress(compressed)

    assert hashlib.sha256(compressed).hexdigest() == NTFS_FIXTURE_SHA256
    assert manifest["compressed_sha256"] == NTFS_FIXTURE_SHA256
    assert manifest["image_sha256"] == NTFS_IMAGE_SHA256
    assert hashlib.sha256(raw_image).hexdigest() == NTFS_IMAGE_SHA256
    assert len(raw_image) == manifest["image_size_bytes"]
    assert manifest["expected_files"] == [
        {
            "path": "/HELLO.TXT",
            "size_bytes": len(NTFS_PAYLOAD),
            "sha256": hashlib.sha256(NTFS_PAYLOAD).hexdigest(),
        }
    ]


def test_native_read_evidence_reports_fixture_hash_and_each_read_operation():
    report = build_evidence_report()

    assert report["schema"] == "mountfile.native-read-evidence/v1"
    assert report["summary"]["verdict"] == "pass"
    assert report["gate"]["verdict"] == "blocked"
    assert report["platform_adapter_evidence"]["status"] == "not_collected"
    assert report["scope"]["legacy_fallback"] == "disabled"
    assert report["scope"]["independent_parity"] == "not_claimed_by_this_report"
    profiles = {item["profile_id"]: item for item in report["profiles"]}
    assert set(profiles) == _EXPECTED_READ_PROFILES

    for profile in profiles.values():
        assert profile["execution_status"] == "pass"
        assert set(profile["operations"]) == _EXPECTED_OPERATIONS
        assert all(
            result["status"] == "pass" for result in profile["operations"].values()
        )
        assert profile["fixture"]["source_unchanged"] is True
        assert profile["fixture"]["sha256"] == profile["fixture"]["sha256_after"]
        assert len(profile["fixture"]["sha256"]) == 64
        assert profile["independent_oracle"]["status"] == "not_claimed"

    ntfs = profiles["ntfs-native-ro"]
    assert ntfs["fixture"]["kind"] == "real-prebuilt"
    assert ntfs["fixture"]["sha256"] == NTFS_IMAGE_SHA256
    assert ntfs["fixture"]["source_sha256"] == NTFS_FIXTURE_SHA256
    assert json.loads(json.dumps(report)) == report
    assert all(
        item["parity"] == "not_claimed" for item in report["external_checker_inventory"]
    )
