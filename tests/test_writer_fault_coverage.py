"""Coverage for the runtime fault seam used by issue #387 acceptance tests.

``mountfile.native_write`` declares :data:`FAULT_POINTS` and documents that this
module fails when the runtime stops emitting a declared point.  These tests are
that contract: the declared list is unique, the seam only reports declared
names, and a real ext4 write emits every point in the documented order.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from test_native_journal_writers import make_ext4, needs_ext, needs_libext2fs

from mountfile.ext4_native_writer import probe_journal
from mountfile.journal_safety import JournalState
from mountfile.native_write import (
    ENABLE_ENV,
    FAULT_POINTS,
    _fault,
    fault_hook_installed,
    fault_point_trace,
)


def test_declared_fault_points_are_unique() -> None:
    assert len(FAULT_POINTS) == len(set(FAULT_POINTS))
    assert FAULT_POINTS[0] == "copy:start"
    assert FAULT_POINTS[-1] == "publish:done"


def test_trace_reports_declared_points_only() -> None:
    def work() -> None:
        for name in FAULT_POINTS:
            _fault(name)

    assert tuple(fault_point_trace(work)) == FAULT_POINTS


def test_trace_restores_previous_hook_after_failure() -> None:
    def work() -> None:
        _fault("copy:start")
        raise RuntimeError("injected")

    with pytest.raises(RuntimeError, match="injected"):
        fault_point_trace(work)
    # The failed trace must not stay installed; a later call is a no-op.
    _fault("publish:done")


def test_fault_hook_installed_sets_and_clears_the_seam() -> None:
    seen: list[str] = []
    fault_hook_installed(seen.append)
    try:
        _fault("helper:start")
    finally:
        fault_hook_installed(None)
    assert seen == ["helper:start"]


@needs_ext
@needs_libext2fs
def test_ext4_writer_emits_every_declared_point_in_order(tmp_path, monkeypatch) -> None:
    from mountfile.ext4_native_writer import ext4_write_file_native

    monkeypatch.setenv(ENABLE_ENV, "1")
    image = make_ext4(tmp_path)
    trace = fault_point_trace(
        ext4_write_file_native, str(image), "/f.txt", b"fault coverage\n"
    )
    assert set(trace) <= set(FAULT_POINTS)
    assert set(trace) == set(FAULT_POINTS)
    assert trace[0] == "copy:start"
    assert trace[-1] == "publish:done"
    assert trace.index("mutation:start") < trace.index("mutation:flush")
    assert trace.index("mutation:flush") < trace.index("mutation:done")
    assert trace.index("mutation:done") < trace.index("publish:start")


_REPO_ROOT = Path(__file__).resolve().parents[1]
_KILL_CHILD = """
import os
import signal
import sys

from mountfile.native_write import fault_hook_installed

target = sys.argv[1]


def hook(name):
    if name == target:
        os.kill(os.getpid(), signal.SIGKILL)


fault_hook_installed(hook)
from mountfile.ext4_native_writer import ext4_write_file_native

ext4_write_file_native(sys.argv[2], "/f.txt", b"killed write\\n")
"""


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-kill is POSIX-only")
@needs_ext
@needs_libext2fs
@pytest.mark.parametrize("point", FAULT_POINTS)
def test_ext4_process_kill_preserves_source_until_publish(tmp_path, point) -> None:
    """Kill the writer at every declared boundary and inspect the source image.

    Every point before ``publish:done`` must leave the caller's image
    byte-identical.  ``publish:done`` is the one point where the atomic replace
    has already happened, so the new content is expected.
    """

    image = make_ext4(tmp_path, size="8M")
    before = image.read_bytes()
    environment = os.environ.copy()
    environment[ENABLE_ENV] = "1"
    environment["PYTHONPATH"] = str(_REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", _KILL_CHILD, point, str(image)],
        cwd=str(_REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr
    after = image.read_bytes()
    if point == "publish:done":
        assert after != before
    else:
        assert after == before


@needs_ext
@needs_libext2fs
def test_ext4_writer_finishes_with_a_clean_checkpointed_journal(
    tmp_path, monkeypatch
) -> None:
    from mountfile.ext4_native_writer import ext4_write_file_native

    monkeypatch.setenv(ENABLE_ENV, "1")
    image = make_ext4(tmp_path)
    ext4_write_file_native(str(image), "/f.txt", b"checkpoint evidence\n")
    probe = probe_journal(str(image))
    assert probe.state is JournalState.CLEAN
    # A cleanly unmounted ext4 keeps a nonzero ring head; ``s_sequence == 0``
    # is the actual "no pending transaction" checkpoint marker.
    assert probe.journal_sequence == 0
    from mountfile.native_write import _run

    assert _run(["e2fsck", "-fn", str(image)]).returncode == 0
