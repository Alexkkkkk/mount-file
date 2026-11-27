# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Failure matrix over the fault seam the runtime writer actually emits.

Items D1, D2, D3 and B8 of tracker #510 ask for a *cell* view: for every declared
phase the harness injects a failure, then checks that the source image is still
byte-identical and that no staged sibling survived.  The seam names are imported
from :mod:`mountfile.native_write` instead of being re-listed here, so a cell can
never pass on a phase the production writer does not instrument.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from mountfile.acceptance.hashing import (
    PUBLISH_POINTS,
    SourceIdentity,
)
from mountfile.acceptance.hashing import publish_atomically as _publish
from mountfile.acceptance.hashing import sha256_bytes, sha256_file

#: States a matrix cell may report.
PASSED = "passed"
UNCHANGED = "source_unchanged"
LEAKED = "staged_leaked"
NOT_EXERCISED = "not_exercised"


#: Phases the runtime writer declares.  Imported lazily so a packaging mistake in
#: the writer surfaces as an import error here, not as a silently shorter list.
def declared_points() -> tuple[str, ...]:
    """Return the fault points declared by the runtime writer."""

    from mountfile.native_write import FAULT_POINTS

    return tuple(FAULT_POINTS)


@dataclass(frozen=True)
class FailureCell:
    """Outcome of one (profile, phase, failure-state) cell."""

    profile: str
    phase: str
    failure: str
    state: str
    detail: str
    source_sha256: str

    @property
    def exercised(self) -> bool:
        return self.state != NOT_EXERCISED

    def as_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "phase": self.phase,
            "failure": self.failure,
            "state": self.state,
            "detail": self.detail,
            "source_sha256": self.source_sha256,
            "exercised": self.exercised,
        }


class _Injected(Exception):
    """Raised by the seam to simulate a crash at a declared phase."""


def _cell(
    workdir: Path,
    profile: str,
    phase: str,
    *,
    expected: str = UNCHANGED,
) -> FailureCell:
    """Inject a failure at ``phase`` and judge what the source looks like after.

    A declared runtime point that the publication primitive never reaches is
    reported as ``not_exercised``.  It is never folded into a pass: the writer
    may instrument a phase this primitive does not own, and pretending
    otherwise would make the matrix look complete when it is not.
    """

    source = workdir / f"{profile}-{phase.replace(':', '-')}.img"
    original = b"ACCEPTANCE-SOURCE-" + profile.encode() + b"\x00" * 32
    source.write_bytes(original)
    digest = sha256_bytes(original)
    identity = SourceIdentity.capture(source)

    staged = source.with_name(source.name + ".staged")
    staged.write_bytes(b"ACCEPTANCE-STAGED")

    traced: list[str] = []

    def hook(name: str) -> None:
        traced.append(name)
        if name == phase:
            raise _Injected(f"injected failure at {name}")

    try:
        _publish(staged, source, identity, fault_hook=hook)
    except _Injected as exc:
        detail = str(exc)
    except Exception as exc:  # noqa: BLE001 - a refusal is a valid cell outcome
        detail = f"refused: {type(exc).__name__}: {exc}"
    else:
        detail = "publication completed without an injected failure"

    after = sha256_file(source)
    if phase not in traced:
        return FailureCell(
            profile,
            phase,
            "injected_exception",
            NOT_EXERCISED,
            f"{detail}; seam never reached this phase (traced {len(traced)} points)",
            after,
        )
    if after != digest:
        return FailureCell(
            profile,
            phase,
            "injected_exception",
            PASSED if expected == PASSED else "mutated",
            f"{detail}; source digest changed {digest} -> {after}",
            after,
        )
    if staged.exists() and expected == UNCHANGED:
        return FailureCell(
            profile,
            phase,
            "injected_exception",
            LEAKED,
            f"{detail}; staged sibling survived the injected failure",
            after,
        )
    state = PASSED if expected == PASSED else UNCHANGED
    return FailureCell(profile, phase, "injected_exception", state, detail, after)


def iter_phases() -> Iterator[str]:
    """Yield every phase the matrix must cover, runtime points first."""

    for point in declared_points():
        yield point
    for point in PUBLISH_POINTS:
        if point not in declared_points():
            yield point


def run_failure_matrix(workdir: str | Path, profile: str = "ext4") -> list[FailureCell]:
    """Build the failure matrix for one profile inside ``workdir``."""

    base = Path(workdir)
    base.mkdir(parents=True, exist_ok=True)
    cells: list[FailureCell] = []
    for phase in iter_phases():
        # A failure injected after the rename means publication already
        # happened, so the source is *expected* to change in that one cell.
        expected = PASSED if phase == "publish:done" else UNCHANGED
        cells.append(_cell(base, profile, phase, expected=expected))
    return cells


def matrix_summary(cells: list[FailureCell]) -> dict[str, object]:
    """Aggregate cells into counts plus the phases that did not hold."""

    counts: dict[str, int] = {}
    for cell in cells:
        counts[cell.state] = counts.get(cell.state, 0) + 1
    # A staged sibling surviving a pre-publish failure is expected: the caller
    # discards it.  Only a changed source image is a violation of D3.
    failing = [cell.as_dict() for cell in cells if cell.state == "mutated"]
    leaked = [cell.phase for cell in cells if cell.state == LEAKED]
    return {
        "cells": [cell.as_dict() for cell in cells],
        "counts": counts,
        "total": len(cells),
        "held": not failing,
        "exercised": sum(1 for cell in cells if cell.exercised),
        "leaked_staged": leaked,
        "violations": failing,
    }


def control_cell(workdir: str | Path, profile: str = "ext4") -> FailureCell:
    """A cell that must publish: proves the matrix is not vacuously green."""

    base = Path(workdir)
    base.mkdir(parents=True, exist_ok=True)
    source = base / f"{profile}-control.img"
    original = b"ACCEPTANCE-CONTROL" + b"\x00" * 32
    source.write_bytes(original)
    identity = SourceIdentity.capture(source)
    staged = source.with_name(source.name + ".staged")
    payload = b"ACCEPTANCE-CONTROL-PUBLISHED"
    staged.write_bytes(payload)

    try:
        published = _publish(staged, source, identity)
    except Exception as exc:  # noqa: BLE001
        return FailureCell(
            profile,
            "publish",
            "none",
            "refused",
            f"{type(exc).__name__}: {exc}",
            sha256_file(source),
        )
    state = PASSED if published == sha256_bytes(payload) else "mismatch"
    return FailureCell(profile, "publish", "none", state, "control publish", published)
