"""Coverage for the runtime fault seam used by issue #387 acceptance tests.

``mountfile.native_write`` declares :data:`FAULT_POINTS` and documents that this
module fails when the runtime stops emitting a declared point.  These tests are
that contract: the declared list is unique, the seam only reports declared
names, and a real ext4 write emits every point in the documented order.
"""

from __future__ import annotations

import pytest
from test_native_journal_writers import make_ext4, needs_ext, needs_libext2fs

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
