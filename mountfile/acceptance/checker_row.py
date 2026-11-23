# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Authoritative-checker row for acceptance items C6 and E1.

The row has to answer three separate questions without collapsing them:

* is the authoritative checker (``chkdsk``) actually present;
* did the check run produce a parity verdict, a difference, or an error;
* which checks were never performed at all.

``ntfsfix -n`` stays secondary: it repairs, it does not validate, so it can
never turn a row into ``pass``.  A row is authoritative evidence only through
the ``chkdsk`` column; the secondary column exists to be refused.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping, Sequence
from typing import Callable

SCHEMA = "mountfile.c6-checker-row/v1"

#: Row outcomes.  ``PASS`` is the only state that may back a parity claim.
PASS = "pass"
DIVERGED = "diverged"
UNKNOWN = "unknown"
UNAVAILABLE = "unavailable"
ERROR = "error"
NOT_COMPUTED = "not_computed"

#: The three states a checker probe can report.  Two states are not enough:
#: ``error`` must not fall back to either ``pass`` or ``fail``.
CHECKER_STATES = ("available", "missing", "error")

AUTHORITATIVE_CHECKER = "chkdsk"

#: Tools that may only ever appear as secondary sanity evidence.
SECONDARY_CHECKERS = ("ntfsfix",)

#: Checks the authoritative checker is allowed to be cited for.
SUPPORTED_CHECKS = (
    "volume_dirty_flag",
    "mft_record_bytes",
    "bitmap_bytes",
    "logfile_restart_page",
    "target_content_readback",
)

#: Checks nobody performed.  Keeping this list explicit is what stops ``pass``
#: from silently meaning "everything we happened to look at matched".
UNSUPPORTED_CHECKS = (
    "mft_allocation_invariants",
    "mft_record_reuse",
    "attribute_list_records",
    "index_allocation_indices",
    "security_descriptor_parity",
    "bitmap_vs_mft_cross_invariant",
    "windows_chkdsk_exit_semantics",
)

#: Streams whose byte differences the classifier is able to attribute.
COMPARABLE_STREAMS = ("mft", "mftmirr", "bitmap", "logfile", "target_content")

#: Outcome -> process exit code.  ``unavailable`` is 0 on purpose: an absent
#: checker is missing evidence, not a failing gate.
EXIT_CODES = {
    PASS: 0,
    UNAVAILABLE: 0,
    ERROR: 1,
    DIVERGED: 2,
    UNKNOWN: 3,
}

#: Explicit pytest node IDs for the C6 contract.  The matrix must select these
#: verbatim; prefix/substring matching would silently stop finding a cell when
#: the ``ids=`` format changes.
C6_DECLARED_TEST_IDS = (
    "tests/test_c6_checker_row_contract.py::"
    "test_c6_checker_row_covers_each_checker_state[checker-available]",
    "tests/test_c6_checker_row_contract.py::"
    "test_c6_checker_row_covers_each_checker_state[checker-missing]",
    "tests/test_c6_checker_row_contract.py::"
    "test_c6_checker_row_covers_each_checker_state[checker-error]",
    "tests/test_c6_checker_row_contract.py::"
    "test_c6_negative_parity_case_reports_diverged",
    "tests/test_c6_checker_row_contract.py::"
    "test_c6_unattributed_difference_is_unknown_and_fails_the_job",
)


def inventory() -> dict[str, object]:
    """Return the bounded check inventory and its digest."""

    payload = json.dumps(
        {"supported": list(SUPPORTED_CHECKS), "unsupported": list(UNSUPPORTED_CHECKS)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "supported_checks": list(SUPPORTED_CHECKS),
        "unsupported_checks": list(UNSUPPORTED_CHECKS),
        "inventory_sha256": hashlib.sha256(payload).hexdigest(),
    }


def _env_version_reader(_executable: str) -> str | None:
    """Read a declared checker version from the environment.

    The probe never executes ``chkdsk``: launching a Windows repair tool to ask
    for its version is exactly the kind of side effect this row must not have.
    A run that knows its checker build declares it here.
    """

    value = os.environ.get("MOUNTFILE_CHKDSK_VERSION", "").strip()
    return value or None


def probe_checker(
    state: str = "auto",
    *,
    which: Callable[[str], str | None] = shutil.which,
    version_reader: Callable[[str], str | None] = _env_version_reader,
) -> tuple[str, str | None, str]:
    """Return ``(state, version, detail)`` for the authoritative checker.

    ``state`` may be forced to one of :data:`CHECKER_STATES` so tests and
    evidence runs can exercise every branch without a Windows host.
    """

    if state != "auto":
        if state not in CHECKER_STATES:
            raise ValueError(
                f"unknown checker state {state!r}; expected one of {CHECKER_STATES}"
            )
        return state, None, f"checker state forced to {state!r}"
    path = which(AUTHORITATIVE_CHECKER)
    if path is None:
        return "missing", None, f"{AUTHORITATIVE_CHECKER} is not available on this host"
    return "available", version_reader(path), path


def classify_parity(
    differences: Mapping[str, bool] | None,
    *,
    unattributed: Sequence[str] = (),
) -> str:
    """Turn a byte-difference map into ``pass``, ``diverged`` or ``unknown``.

    A difference the classifier cannot attribute is ``unknown``, never a
    silent ``pass`` and never dumped into an outside-scope bucket.
    """

    if not differences:
        return NOT_COMPUTED
    if unattributed:
        return UNKNOWN
    stray = [
        key
        for key, differs in differences.items()
        if differs and key not in COMPARABLE_STREAMS
    ]
    if stray:
        return UNKNOWN
    return DIVERGED if any(differences.values()) else PASS


def build_row(
    *,
    checker_state: str = "auto",
    check_logfile_state: str | None = None,
    parity: str | None = None,
    unattributed: Sequence[str] = (),
    differences: Mapping[str, bool] | None = None,
    captured_at: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    version_reader: Callable[[str], str | None] = _env_version_reader,
) -> dict[str, object]:
    """Build one C6 checker row.

    ``check_logfile_state == "error"`` models the D5 finding: ``ntfsfix -n``
    ran, and the code still refuses to call that parity.  The row keeps that
    refusal visible instead of smoothing it into a green cell.
    """

    probe_state, version, detail = probe_checker(
        checker_state, which=which, version_reader=version_reader
    )
    if parity is None and differences:
        parity = classify_parity(differences, unattributed=unattributed)

    if probe_state == "missing":
        outcome, parity_value = UNAVAILABLE, NOT_COMPUTED
    elif probe_state == "error" or check_logfile_state == "error":
        outcome, parity_value = ERROR, NOT_COMPUTED
        detail = (
            "authoritative parity could not be established; a secondary tool "
            "does not close the gap"
        )
    elif parity is None:
        outcome, parity_value = ERROR, NOT_COMPUTED
        detail = "checker is available but no parity verdict was computed"
    else:
        parity_value = parity
        outcome = parity

    inv = inventory()
    return {
        "schema": SCHEMA,
        "acceptance_item": "C6",
        "checker": AUTHORITATIVE_CHECKER,
        "authoritative": True,
        "secondary_checkers": [
            {
                "checker": name,
                "authoritative": False,
                "role": "secondary_sanity_only",
                "may_satisfy_parity": False,
            }
            for name in SECONDARY_CHECKERS
        ],
        "checker_state": probe_state,
        "outcome": outcome,
        "parity": parity_value,
        "detail": detail,
        "provenance": {
            "checker_version": version,
            "checker_version_state": "recorded" if version else "unavailable",
            "inventory_sha256": inv["inventory_sha256"],
            "captured_at": captured_at or "unavailable",
        },
        "supported_checks": inv["supported_checks"],
        "unsupported_checks": inv["unsupported_checks"],
        "exit_code": EXIT_CODES[outcome],
    }


def exit_code(row: Mapping[str, object]) -> int:
    """Return the process exit code for ``row``."""

    outcome = str(row.get("outcome"))
    if outcome not in EXIT_CODES:
        raise ValueError(f"unknown row outcome {outcome!r}")
    return EXIT_CODES[outcome]


def select_cases(
    declared_ids: Sequence[str], cases: Sequence[Mapping[str, str]]
) -> dict[str, object]:
    """Match JUnit cases against ``declared_ids`` by exact node ID.

    Exact equality is the point: a reformatted parametrization must show up as
    ``not_proven`` rather than as an accidentally green cell.
    """

    index = {str(case["node_id"]): case for case in cases}
    matched = [node_id for node_id in declared_ids if node_id in index]
    missing = [node_id for node_id in declared_ids if node_id not in index]
    statuses = [str(index[node_id]["status"]) for node_id in matched]
    if "failed" in statuses:
        status = "failed"
    elif not matched:
        status = "not_proven"
    elif missing:
        status = "partial"
    elif any(value != "passed" for value in statuses):
        status = "unavailable"
    else:
        status = "passed"
    return {"status": status, "evidence": matched, "missing": missing}


__all__ = [
    "AUTHORITATIVE_CHECKER",
    "C6_DECLARED_TEST_IDS",
    "CHECKER_STATES",
    "DIVERGED",
    "ERROR",
    "EXIT_CODES",
    "NOT_COMPUTED",
    "PASS",
    "SCHEMA",
    "SECONDARY_CHECKERS",
    "SUPPORTED_CHECKS",
    "UNAVAILABLE",
    "UNKNOWN",
    "UNSUPPORTED_CHECKS",
    "build_row",
    "classify_parity",
    "exit_code",
    "inventory",
    "probe_checker",
    "select_cases",
]
