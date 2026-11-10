from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.verify_generated_status import StatusValidationError, validate_upgrade

OLD_OPERATIONS = ["list", "write-file"]


def _cell(state: str, api: str | None = None) -> dict:
    if state == "available":
        return {"state": state, "api_operation": api, "refusal": None}
    return {
        "state": state,
        "api_operation": None,
        "refusal": {
            "code": "operation_not_supported",
            "category": "unsupported",
            "message": "unsupported",
        },
    }


def _profile(profile_id: str, writable: bool, include_link: bool = False) -> dict:
    write = ["write_file"] if writable else []
    operations = {"list": _cell("available", "list")}
    operations["write-file"] = (
        _cell("available", "write_file") if writable else _cell("refused")
    )
    operations["hard-link"] = (
        _cell("available", "link") if include_link and writable else _cell("refused")
    )
    if include_link and writable:
        write.append("link")
    return {
        "profile_id": profile_id,
        "filesystem": "ext2",
        "backend": "native-ext2",
        "interface": "universal-fs-rw" if writable else "vfs",
        "mode": "read-write" if writable else "read-only",
        "write_strategy": "native-ext2" if writable else "read-only",
        "read_capabilities": ["list"],
        "write_capabilities": write,
        "operations": operations,
        "constraints": ["bounded"],
    }


def _artifact_set(version: int, candidate: bool) -> dict[str, dict]:
    operations = OLD_OPERATIONS + (["hard-link"] if candidate else [])
    profiles = [
        _profile("ext2-ro", writable=False, include_link=candidate),
        _profile("ext2-rw", writable=True, include_link=candidate),
    ]
    if not candidate:
        for profile in profiles:
            profile["operations"].pop("hard-link")
    return {
        "capability-matrix.json": {
            "version": version,
            "operations": operations,
            "profiles": profiles,
        },
        "checker-matrix.json": {"schema": 1, "entries": []},
        "recovery-matrix.json": {
            "schema": 1,
            "capability_matrix_version": version,
            "production_ready": False,
            "entries": [],
        },
        "product-status.json": {
            "schema": 1,
            "capability_matrix_version": version,
            "verdict": "pass",
            "blockers": [],
        },
    }


def _write_set(path: Path, artifacts: dict[str, dict]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for name, data in artifacts.items():
        (path / name).write_text(
            json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )


def test_identical_generated_artifacts_pass_without_migration(tmp_path):
    artifacts = _artifact_set(5, candidate=False)
    expected = tmp_path / "expected"
    committed = tmp_path / "committed"
    _write_set(expected, artifacts)
    _write_set(committed, artifacts)

    assert validate_upgrade(expected, committed) == (
        "Canonical status artifacts match the trusted generator."
    )


def test_checker_inventory_may_differ_by_runner_without_changing_claims(tmp_path):
    expected = _artifact_set(5, candidate=False)
    committed = _artifact_set(5, candidate=False)
    expected["checker-matrix.json"] = {
        "schema": 1,
        "platform": "linux",
        "python": "3.12.0",
        "entries": [
            {
                "checker": "fsck.fat",
                "availability": "available",
                "checker_path": "/usr/sbin/fsck.fat",
                "checker_version": "1.0",
                "reason": "fixture-specific test exists but was not executed",
                "fixture_status": "fixture_declared",
            }
        ],
    }
    committed["checker-matrix.json"] = {
        "schema": 1,
        "platform": "windows",
        "python": "3.11.9",
        "entries": [
            {
                "checker": "fsck.fat",
                "availability": "unavailable",
                "checker_path": None,
                "checker_version": None,
                "reason": "required checker/runtime is unavailable on this runner",
                "fixture_status": "fixture_declared",
            }
        ],
    }
    expected_dir = tmp_path / "expected"
    committed_dir = tmp_path / "committed"
    _write_set(expected_dir, expected)
    _write_set(committed_dir, committed)

    assert validate_upgrade(expected_dir, committed_dir) == (
        "Canonical status artifacts match; checker inventory differs only by runner."
    )


def test_valid_one_step_schema_upgrade_passes_as_data_only(tmp_path):
    expected = tmp_path / "expected"
    committed = tmp_path / "committed"
    _write_set(expected, _artifact_set(5, candidate=False))
    _write_set(committed, _artifact_set(6, candidate=True))

    assert "5 -> 6" in validate_upgrade(expected, committed)


def test_schema_upgrade_rejects_changes_to_existing_operation(tmp_path):
    expected = tmp_path / "expected"
    committed = tmp_path / "committed"
    _write_set(expected, _artifact_set(5, candidate=False))
    candidate = _artifact_set(6, candidate=True)
    candidate["capability-matrix.json"]["profiles"][0]["operations"]["list"] = _cell(
        "refused"
    )
    _write_set(committed, candidate)

    with pytest.raises(StatusValidationError, match="changed existing operation"):
        validate_upgrade(expected, committed)


def test_schema_upgrade_rejects_product_version_mismatch(tmp_path):
    expected = tmp_path / "expected"
    committed = tmp_path / "committed"
    _write_set(expected, _artifact_set(5, candidate=False))
    candidate = _artifact_set(6, candidate=True)
    candidate["product-status.json"]["capability_matrix_version"] = 5
    _write_set(committed, candidate)

    with pytest.raises(StatusValidationError, match="product status version"):
        validate_upgrade(expected, committed)


def test_schema_upgrade_rejects_changes_to_product_claims(tmp_path):
    expected = tmp_path / "expected"
    committed = tmp_path / "committed"
    _write_set(expected, _artifact_set(5, candidate=False))
    candidate = _artifact_set(6, candidate=True)
    candidate["product-status.json"]["blockers"] = ["new claim"]
    candidate["product-status.json"]["verdict"] = "blocked"
    _write_set(committed, candidate)

    with pytest.raises(StatusValidationError, match="must pass with no blockers"):
        validate_upgrade(expected, committed)
