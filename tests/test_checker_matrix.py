"""Tests for the reproducible external-checker capability inventory."""

from __future__ import annotations

import json

from scripts.checker_matrix import build_report


def test_checker_matrix_is_json_serialisable_and_never_claims_parity():
    report = build_report()
    assert report["schema"] == 1
    assert report["report"] == "mountfile-external-checker-matrix"
    assert json.loads(json.dumps(report)) == report
    profiles = {entry["profile"]: entry for entry in report["entries"]}
    assert {
        "ext2-native-rw",
        "fat12-16-native-rw",
        "fat32-native-rw",
        "exfat-native-rw",
        "ntfs-native-ro",
        "all-native-readers",
    } == set(profiles)
    assert all(entry["parity"] == "not_claimed" for entry in report["entries"])
    assert all(entry["evidence"] == "fixture_required" for entry in report["entries"])
    assert profiles["ntfs-native-ro"]["checker_authoritative"] is False
    assert profiles["all-native-readers"]["checker_kind"] == "python_module"


def test_checker_matrix_records_unavailable_as_unknown_not_pass(monkeypatch):
    monkeypatch.setattr("scripts.checker_matrix.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "scripts.checker_matrix.importlib.util.find_spec", lambda _name: None
    )
    report = build_report()
    assert all(entry["availability"] == "unavailable" for entry in report["entries"])
    assert all(entry["parity"] == "not_claimed" for entry in report["entries"])
    assert all("unavailable" in entry["reason"] for entry in report["entries"])
    assert all(entry["checker_version"] is None for entry in report["entries"])


def test_checker_matrix_never_conflates_available_tool_with_fixture_evidence(
    monkeypatch,
):
    monkeypatch.setattr(
        "scripts.checker_matrix.shutil.which", lambda name: "/usr/bin/" + name
    )
    monkeypatch.setattr(
        "scripts.checker_matrix.importlib.util.find_spec",
        lambda name: type("Spec", (), {"origin": "/site-packages/" + name + ".so"})(),
    )
    monkeypatch.setattr(
        "scripts.checker_matrix.subprocess.run",
        lambda *args, **kwargs: type(
            "Result", (), {"returncode": 0, "stdout": "tool 1.2\n", "stderr": ""}
        )(),
    )
    report = build_report()
    assert all(entry["availability"] == "available" for entry in report["entries"])
    assert all(
        entry["checker_version"] in {"tool 1.2", None} for entry in report["entries"]
    )
    assert all(entry["evidence"] == "fixture_required" for entry in report["entries"])
    assert all(entry["parity"] == "not_claimed" for entry in report["entries"])
