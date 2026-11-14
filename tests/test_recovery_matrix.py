"""Tests for the evidence-bounded writer recovery report."""

import json
from pathlib import Path

from scripts.recovery_matrix import (
    _typed_evidence,
    build_acceptance_report,
    build_report,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_recovery_report_is_fail_closed_and_json_serialisable():
    report = build_report()
    assert report["schema"] == 1
    assert report["capability_matrix_version"] == 6
    assert report["production_ready"] is False
    assert json.loads(json.dumps(report)) == report
    assert {entry["profile_id"] for entry in report["entries"]} == {
        "fat12-16-native-rw",
        "fat32-native-rw",
        "ext2-native-rw",
        "exfat-native-rw",
    }
    assert all(entry["verdict"] == "blocked" for entry in report["entries"])
    assert all(entry["blocking_cells"] for entry in report["entries"])
    assert all(
        entry["evidence"] == "capability_contract" for entry in report["entries"]
    )
    assert all(entry["parity"] == "not_claimed" for entry in report["entries"])


def test_typed_evidence_distinguishes_pass_unavailable_and_not_proven():
    assert _typed_evidence("tested_on_failure") == {
        "status": "pass",
        "source": "tested_on_failure",
    }
    assert _typed_evidence("unsupported") == {
        "status": "unavailable",
        "source": "unsupported",
    }
    assert _typed_evidence("not_proven") == {
        "status": "not_proven",
        "source": "not_proven",
    }
    for entry in build_report()["entries"]:
        assert entry["recovery"]["power_loss"] == "unsupported"
        assert _typed_evidence(entry["recovery"]["power_loss"])["status"] == (
            "unavailable"
        )


def test_ext2_recovery_evidence_is_more_complete_but_not_production_ready():
    report = build_report()
    ext2 = next(
        entry for entry in report["entries"] if entry["profile_id"] == "ext2-native-rw"
    )
    assert ext2["recovery"]["process_kill"] == "tested_with_recovery_sidecar"
    assert ext2["recovery"]["quarantine"] == "tested_on_recovery_failure"
    assert ext2["recovery"]["power_loss"] == "unsupported"
    assert ext2["blocking_cells"] == ["power_loss"]


def test_trusted_recovery_report_matches_the_committed_artifact():
    """The trusted gate only accepts a schema bump for recovery-matrix.json."""
    committed = json.loads(
        (_REPO_ROOT / "docs" / "generated" / "recovery-matrix.json").read_text(
            encoding="utf-8"
        )
    )
    assert committed == build_report()
    assert "acceptance_cells" not in json.dumps(committed)


def test_acceptance_matrix_covers_every_declared_dimension_without_inference():
    report = build_acceptance_report()
    dimensions = report["dimensions"]
    expected_count = len(dimensions["operations"]) * len(dimensions["states"])
    assert dimensions["operations"] == [
        "write-file",
        "mkdir",
        "delete",
        "rename",
        "journal-recovery",
    ]
    assert dimensions["states"] == [
        "clean",
        "dirty",
        "corrupt",
        "concurrent-change",
        "process-killed",
    ]
    assert report["production_ready"] is False
    assert {entry["profile_id"] for entry in report["entries"]} == {
        "fat12-16-native-rw",
        "fat32-native-rw",
        "ext2-native-rw",
        "exfat-native-rw",
        "ext4-native-rw",
        "ntfs-native-rw",
    }
    for entry in report["entries"]:
        cells = entry["cells"]
        assert len(cells) == expected_count
        assert {(cell["operation"], cell["state"]) for cell in cells} == {
            (operation, state)
            for operation in dimensions["operations"]
            for state in dimensions["states"]
        }
        assert all(
            cell["status"] in {"pass", "unavailable", "not_proven"} for cell in cells
        )
        # No generated cell may silently claim success without a tested source.
        assert all(
            cell["status"] != "pass" or cell["source"].startswith("tested_")
            for cell in cells
        )


def test_acceptance_artifact_matches_the_committed_file():
    committed = json.loads(
        (_REPO_ROOT / "docs" / "generated" / "recovery-acceptance.json").read_text(
            encoding="utf-8"
        )
    )
    assert committed == build_acceptance_report()


def test_ntfs_and_ext4_recovery_cells_remain_explicitly_blocked():
    report = build_acceptance_report()
    profiles = {entry["profile_id"]: entry for entry in report["entries"]}
    assert profiles["ext4-native-rw"]["verdict"] == "blocked"
    assert profiles["ntfs-native-rw"]["verdict"] == "blocked"
    assert profiles["ext4-native-rw"]["blocking_cells"] == ["power_loss"]
    assert set(profiles["ntfs-native-rw"]["blocking_cells"]) == {
        "rollback",
        "process_kill",
        "power_loss",
    }
    # The experimental replay profiles never enter the trusted capability set.
    assert {entry["profile_id"] for entry in build_report()["entries"]} == {
        "fat12-16-native-rw",
        "fat32-native-rw",
        "ext2-native-rw",
        "exfat-native-rw",
    }


def test_recovery_evidence_is_not_broadcast_to_uncovered_operations_or_states():
    report = build_acceptance_report()
    ext2 = next(
        entry for entry in report["entries"] if entry["profile_id"] == "ext2-native-rw"
    )
    process_killed = {
        cell["operation"]: cell
        for cell in ext2["cells"]
        if cell["state"] == "process-killed"
    }
    assert process_killed["write-file"]["status"] == "pass"
    assert all(
        process_killed[operation]["status"] == "not_proven"
        for operation in ("mkdir", "delete", "rename", "journal-recovery")
    )
    corrupt = [cell for cell in ext2["cells"] if cell["state"] == "corrupt"]
    assert all(cell["status"] == "not_proven" for cell in corrupt)
