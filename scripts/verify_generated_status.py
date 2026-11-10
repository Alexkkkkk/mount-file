#!/usr/bin/env python3
"""Verify generated status artifacts, including forward schema upgrades.

The PR-target workflow runs this script from the trusted default branch. It
reads generated files as JSON data only; it never imports or executes code from
a pull-request checkout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ARTIFACTS = (
    "capability-matrix.json",
    "checker-matrix.json",
    "recovery-matrix.json",
    "product-status.json",
)
_PROFILE_IDENTITY_FIELDS = (
    "profile_id",
    "filesystem",
    "backend",
    "interface",
    "mode",
    "write_strategy",
)
_VOLATILE_CHECKER_FIELDS = frozenset(
    {
        "availability",
        "checker_path",
        "checker_version",
        "platform",
        "platform_release",
        "python",
        "reason",
    }
)


class StatusValidationError(ValueError):
    """Raised when generated status artifacts are stale or inconsistent."""


def _json(path: Path) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise StatusValidationError(
                    f"duplicate JSON key {key!r} in {path.name}"
                )
            value[key] = item
        return value

    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=unique_object
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StatusValidationError(f"cannot read valid JSON {path}: {exc}") from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StatusValidationError(message)


def _stable_checker_matrix(value: Any) -> Any:
    """Remove runner-specific inventory fields before checker comparison."""
    if isinstance(value, dict):
        return {
            key: _stable_checker_matrix(item)
            for key, item in value.items()
            if key not in _VOLATILE_CHECKER_FIELDS
        }
    if isinstance(value, list):
        return [_stable_checker_matrix(item) for item in value]
    return value


def _validate_profile_operations(
    expected_matrix: dict[str, Any],
    candidate_matrix: dict[str, Any],
    old_ops: list[str],
    new_ops: list[str],
) -> None:
    expected_profiles = expected_matrix.get("profiles")
    candidate_profiles = candidate_matrix.get("profiles")
    _require(isinstance(expected_profiles, list), "trusted profiles must be a list")
    _require(isinstance(candidate_profiles, list), "candidate profiles must be a list")
    expected_by_id = {
        p.get("profile_id"): p for p in expected_profiles if isinstance(p, dict)
    }
    candidate_by_id = {
        p.get("profile_id"): p for p in candidate_profiles if isinstance(p, dict)
    }
    _require(
        None not in expected_by_id and None not in candidate_by_id,
        "profile_id is required",
    )
    _require(
        set(candidate_by_id) == set(expected_by_id),
        "schema upgrade must preserve the set of capability profiles",
    )

    for profile_id, before in expected_by_id.items():
        after = candidate_by_id[profile_id]
        for field in _PROFILE_IDENTITY_FIELDS:
            _require(
                after.get(field) == before.get(field),
                f"profile {profile_id!r} changed immutable field {field!r}",
            )
        _require(
            after.get("read_capabilities") == before.get("read_capabilities"),
            f"profile {profile_id!r} changed existing read capabilities",
        )
        before_ops = before.get("operations")
        after_ops = after.get("operations")
        _require(
            isinstance(before_ops, dict),
            f"profile {profile_id!r} has invalid trusted operations",
        )
        _require(
            isinstance(after_ops, dict),
            f"profile {profile_id!r} has invalid candidate operations",
        )
        _require(
            set(before_ops) == set(old_ops),
            f"profile {profile_id!r} has incomplete trusted operations",
        )
        _require(
            set(after_ops) == set(old_ops + new_ops),
            f"profile {profile_id!r} has incomplete candidate operations",
        )
        for operation in old_ops:
            _require(
                after_ops[operation] == before_ops[operation],
                f"profile {profile_id!r} changed existing operation {operation!r}",
            )

        newly_available_api: list[str] = []
        for operation in new_ops:
            cell = after_ops[operation]
            _require(
                isinstance(cell, dict),
                f"profile {profile_id!r} operation {operation!r} must be an object",
            )
            state = cell.get("state")
            _require(
                state in {"available", "refused"},
                f"profile {profile_id!r} operation {operation!r} has invalid state",
            )
            if state == "available":
                api_operation = cell.get("api_operation")
                _require(
                    isinstance(api_operation, str) and api_operation,
                    f"available {operation!r} must name its API operation",
                )
                _require(
                    cell.get("refusal") is None,
                    f"available {operation!r} cannot include a refusal",
                )
                newly_available_api.append(api_operation)
            else:
                _require(
                    cell.get("api_operation") is None,
                    f"refused {operation!r} cannot expose an API operation",
                )
                refusal = cell.get("refusal")
                _require(
                    isinstance(refusal, dict)
                    and refusal.get("code") == "operation_not_supported"
                    and refusal.get("category") == "unsupported"
                    and isinstance(refusal.get("message"), str),
                    f"refused {operation!r} must include a typed refusal",
                )

        before_write = before.get("write_capabilities")
        after_write = after.get("write_capabilities")
        _require(
            isinstance(before_write, list) and isinstance(after_write, list),
            f"profile {profile_id!r} write capabilities must be lists",
        )
        _require(
            after_write[: len(before_write)] == before_write,
            f"profile {profile_id!r} removed or reordered existing write capabilities",
        )
        added_write = after_write[len(before_write) :]
        _require(
            len(added_write) == len(set(added_write))
            and len(newly_available_api) == len(set(newly_available_api))
            and set(added_write) == set(newly_available_api),
            f"profile {profile_id!r} new write capabilities do not match newly available operations",
        )

        before_normalized = dict(before)
        after_normalized = dict(after)
        before_normalized["operations"] = before_ops
        after_normalized["operations"] = {
            operation: after_ops[operation] for operation in old_ops
        }
        after_normalized["write_capabilities"] = before_write
        _require(
            after_normalized == before_normalized,
            f"profile {profile_id!r} changed fields beyond the new operations",
        )


def validate_upgrade(expected_dir: Path, committed_dir: Path) -> str:
    """Validate artifacts; return the outcome description or raise on failure."""
    expected_raw = {name: (expected_dir / name).read_bytes() for name in ARTIFACTS}
    candidate_raw = {name: (committed_dir / name).read_bytes() for name in ARTIFACTS}
    if all(expected_raw[name] == candidate_raw[name] for name in ARTIFACTS):
        return "Canonical status artifacts match the trusted generator."

    expected = {name: _json(expected_dir / name) for name in ARTIFACTS}
    candidate = {name: _json(committed_dir / name) for name in ARTIFACTS}
    _require(
        _stable_checker_matrix(expected["checker-matrix.json"])
        == _stable_checker_matrix(candidate["checker-matrix.json"]),
        "checker-matrix.json changed its stable claims",
    )
    if all(
        expected_raw[name] == candidate_raw[name]
        for name in (
            "capability-matrix.json",
            "recovery-matrix.json",
            "product-status.json",
        )
    ):
        return "Canonical status artifacts match; checker inventory differs only by runner."

    old_matrix = expected["capability-matrix.json"]
    new_matrix = candidate["capability-matrix.json"]
    _require(
        isinstance(old_matrix, dict) and isinstance(new_matrix, dict),
        "capability matrices must be JSON objects",
    )
    old_version = old_matrix.get("version")
    new_version = new_matrix.get("version")
    _require(
        isinstance(old_version, int)
        and not isinstance(old_version, bool)
        and isinstance(new_version, int)
        and not isinstance(new_version, bool)
        and new_version == old_version + 1,
        "only a one-step capability schema version upgrade can use this validation path",
    )
    old_ops = old_matrix.get("operations")
    new_ops_all = new_matrix.get("operations")
    _require(
        isinstance(old_ops, list) and isinstance(new_ops_all, list),
        "operation lists are required",
    )
    _require(
        new_ops_all[: len(old_ops)] == old_ops
        and len(new_ops_all) > len(old_ops)
        and len(new_ops_all) == len(set(new_ops_all))
        and all(isinstance(op, str) and op for op in new_ops_all),
        "schema upgrade must append unique canonical operations without changing existing order",
    )
    new_ops = new_ops_all[len(old_ops) :]
    old_matrix_normalized = dict(old_matrix)
    new_matrix_normalized = dict(new_matrix)
    old_matrix_normalized.pop("version", None)
    new_matrix_normalized.pop("version", None)
    old_matrix_normalized.pop("operations", None)
    new_matrix_normalized.pop("operations", None)
    _require(
        old_matrix_normalized.keys() == new_matrix_normalized.keys(),
        "schema upgrade changed top-level capability matrix fields",
    )
    _require(
        [p.get("profile_id") for p in old_matrix.get("profiles", [])]
        == [p.get("profile_id") for p in new_matrix.get("profiles", [])],
        "schema upgrade reordered capability profiles",
    )
    _validate_profile_operations(old_matrix, new_matrix, old_ops, new_ops)

    recovery = candidate["recovery-matrix.json"]
    product = candidate["product-status.json"]
    _require(
        isinstance(recovery, dict) and isinstance(product, dict),
        "recovery and product status must be JSON objects",
    )
    _require(
        recovery.get("capability_matrix_version") == new_version,
        "recovery matrix version does not match capability matrix",
    )
    _require(
        product.get("capability_matrix_version") == new_version,
        "product status version does not match capability matrix",
    )
    _require(
        product.get("verdict") == "pass" and product.get("blockers") == [],
        "product status must pass with no blockers",
    )

    # During a capability schema bump, the only permitted recovery/product data
    # difference is the matching matrix version. All their other claims remain
    # byte-for-byte equivalent in semantic JSON form to trusted generation.
    old_recovery = expected["recovery-matrix.json"]
    old_product = expected["product-status.json"]
    _require(
        isinstance(old_recovery, dict) and isinstance(old_product, dict),
        "trusted recovery/product status must be JSON objects",
    )
    recovery_normalized = dict(recovery)
    product_normalized = dict(product)
    recovery_normalized["capability_matrix_version"] = old_version
    product_normalized["capability_matrix_version"] = old_version
    _require(
        recovery_normalized == old_recovery,
        "recovery claims changed beyond the schema version",
    )
    _require(
        product_normalized == old_product,
        "product status changed beyond the schema version",
    )

    return f"Validated capability schema upgrade {old_version} -> {new_version} using JSON-only checks."


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-dir", required=True, type=Path)
    parser.add_argument("--committed-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        print(validate_upgrade(args.expected_dir, args.committed_dir))
    except (OSError, StatusValidationError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
