"""Tests for the evidence-bounded writer recovery report."""

from scripts.recovery_matrix import build_report


def test_recovery_report_is_fail_closed_and_json_serialisable():
    import json

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


def test_ext2_recovery_evidence_is_more_complete_but_not_production_ready():
    report = build_report()
    ext2 = next(
        entry for entry in report["entries"] if entry["profile_id"] == "ext2-native-rw"
    )
    assert ext2["recovery"]["process_kill"] == "tested_with_recovery_sidecar"
    assert ext2["recovery"]["quarantine"] == "tested_on_recovery_failure"
    assert ext2["recovery"]["power_loss"] == "unsupported"
    assert ext2["blocking_cells"] == ["power_loss"]
