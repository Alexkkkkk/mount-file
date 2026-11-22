# SPDX-License-Identifier: MIT
"""Contract tests for the E1 draft checker inventory (locally provable)."""

import json

from scripts.windows_checker_inventory import SCHEMA, build_inventory


def test_inventory_on_non_windows_host_is_typed_unavailable():
    inventory = build_inventory()
    # This suite runs in CI on Linux; a Windows-host run is covered by the
    # dedicated Windows acceptance workflow, not here.
    if inventory["host"] == "Windows":
        return
    assert inventory["schema"] == SCHEMA
    assert inventory["status"] == "unavailable"
    assert inventory["reason"] == "no checker binary found on this host"
    assert all(item["present"] is False for item in inventory["checkers"])


def test_inventory_never_reports_pass_without_present_checkers():
    inventory = build_inventory()
    encoded = json.dumps(inventory)
    assert '"status": "pass"' not in encoded
    assert inventory["status"] in {"available", "unavailable"}
    present = [item for item in inventory["checkers"] if item["present"]]
    if not present:
        assert inventory["status"] == "unavailable"


def test_capability_map_separates_what_checkers_do_and_do_not_verify():
    inventory = build_inventory()
    for item in inventory["checkers"]:
        assert item["verifies"] and item["does_not_verify"]
        if item["authority"] == "secondary_only_not_chkdsk_parity":
            assert any("parity" in claim for claim in item["does_not_verify"])
