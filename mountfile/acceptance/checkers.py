# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""Independent checker probing for acceptance items D5, B10, C6 and E1.

The tracker cannot treat "the helper is installed" as "the image is verified",
and it cannot treat a missing helper as a pass.  Every probe therefore returns
one of three typed outcomes, and ``unavailable`` is never collapsed into
``clean``.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

#: Checker outcome states.  ``CLEAN`` is the only state that may back a
#: ``verified`` acceptance claim about a specific image.
CLEAN = "clean"
DIRTY = "dirty"
ERROR = "error"
UNAVAILABLE = "unavailable"


class CheckerState(str, Enum):
    CLEAN = CLEAN
    DIRTY = DIRTY
    ERROR = ERROR
    UNAVAILABLE = UNAVAILABLE


@dataclass(frozen=True)
class CheckerOutcome:
    """Result of one checker probe."""

    tool: str
    state: str
    detail: str
    returncode: int | None = None

    @property
    def is_clean(self) -> bool:
        return self.state == CLEAN

    @property
    def is_available(self) -> bool:
        return self.state != UNAVAILABLE

    def as_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "state": self.state,
            "detail": self.detail,
            "returncode": self.returncode,
        }


#: Tools the acceptance matrix is allowed to cite as *secondary* evidence.
SECONDARY_TOOLS = ("e2fsck", "ntfsfix", "ntfscat", "debugfs", "mkfs.ext4", "mkfs.ntfs")

#: Tools that are authoritative for one filesystem each.  ``ntfsfix`` is
#: deliberately absent: it repairs, it does not validate, and item C6 says so.
AUTHORITATIVE_TOOLS = {"e2fsck": "ext2/ext3/ext4"}


def tool_version(tool: str, timeout: float = 20.0) -> str:
    """Return the first version line of ``tool``, or ``""`` when unreadable."""

    try:
        completed = subprocess.run(
            [tool, "-V"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    output = (completed.stdout or completed.stderr or "").strip().splitlines()
    return output[0].strip() if output else ""


def probe_checker(tool: str) -> CheckerOutcome:
    """Report whether ``tool`` exists, without pretending to have run it."""

    path = shutil.which(tool)
    if path is None:
        return CheckerOutcome(tool, UNAVAILABLE, f"{tool} not found on PATH")
    version = tool_version(tool) or "version unreadable"
    return CheckerOutcome(tool, CLEAN, f"{path} ({version})")


def probe_checkers() -> dict[str, CheckerOutcome]:
    """Probe every declared tool once."""

    return {tool: probe_checker(tool) for tool in SECONDARY_TOOLS}


def run_checker(
    tool: str,
    image: str | Path,
    args: tuple[str, ...] = ("-fn",),
    timeout: float = 120.0,
) -> CheckerOutcome:
    """Run a read-only checker on ``image``.

    ``-n`` (no-write) is mandatory for every call this harness makes, so a
    probe can never repair the image it is supposed to judge.
    """

    if tool not in AUTHORITATIVE_TOOLS:
        return CheckerOutcome(
            tool,
            UNAVAILABLE,
            f"{tool} is not an authoritative checker for this harness",
        )
    if not args or not any(flag.startswith("-n") for flag in args):
        return CheckerOutcome(tool, ERROR, "read-only flag missing; refusing to run")
    executable = shutil.which(tool)
    if executable is None:
        return CheckerOutcome(tool, UNAVAILABLE, f"{tool} not found on PATH")
    target = Path(image)
    if not target.is_file():
        return CheckerOutcome(tool, UNAVAILABLE, f"image missing: {target}")

    try:
        completed = subprocess.run(
            [executable, *args, str(target)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckerOutcome(tool, ERROR, f"{tool} could not run: {exc}")

    detail = ((completed.stdout or "") + (completed.stderr or "")).strip()[-400:]
    if completed.returncode == 0:
        return CheckerOutcome(tool, CLEAN, detail or "clean", completed.returncode)
    if completed.returncode == 1:
        return CheckerOutcome(
            tool, DIRTY, detail or "differences found", completed.returncode
        )
    return CheckerOutcome(tool, ERROR, detail or "checker error", completed.returncode)


def summarise(outcomes: dict[str, CheckerOutcome]) -> dict[str, int]:
    """Count probe states so a report can state coverage without adjectives."""

    counts = {state: 0 for state in (CLEAN, DIRTY, ERROR, UNAVAILABLE)}
    for outcome in outcomes.values():
        counts[outcome.state] = counts.get(outcome.state, 0) + 1
    counts["total"] = len(outcomes)
    return counts
