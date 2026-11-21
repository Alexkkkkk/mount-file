"""Real process-kill coverage for the staged NTFS native writer."""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from mountfile.native_write import ENABLE_ENV, FAULT_POINTS, fault_point_trace
from mountfile.ntfs_native_writer import NtfsNativeWriter
from mountfile.ntfs_replay import (
    NTFS_REPLAY_FAULT_POINTS,
    NTFS_REPLAY_WRITE_FAULT_POINTS,
)
from tests.ntfs_crash_fixture import make_crash_fixture

_REPO_ROOT = Path(__file__).resolve().parents[1]

_KILL_CHILD = r"""
import os
import signal
import sys
from mountfile.native_write import fault_hook_installed

target = sys.argv[1]

def hook(name):
    if name == target:
        os.kill(os.getpid(), signal.SIGKILL)

fault_hook_installed(hook)
from mountfile.ntfs_native_writer import NtfsNativeWriter
NtfsNativeWriter(recover="native").write_file(sys.argv[2], "/target", b"killed write\n")
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ntfscat(image: Path, path: str) -> bytes:
    result = subprocess.run(
        ["ntfscat", str(image), path], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-kill is POSIX-only")
@pytest.mark.skipif(
    shutil.which("mkntfs") is None
    or shutil.which("ntfscp") is None
    or shutil.which("ntfscat") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp/ntfscat) are unavailable",
)
@pytest.mark.parametrize(
    ("nonresident", "expected_replay_points"),
    [
        (
            False,
            (
                "ntfs:replay:mft_record:applied",
                "ntfs:replay:restart_pages:applied",
                "ntfs:replay:volume_marker:applied",
                "ntfs:replay:mft_mirror:applied",
            ),
        ),
        (
            True,
            (
                "ntfs:replay:stream_write:applied",
                "ntfs:replay:restart_pages:applied",
                "ntfs:replay:volume_marker:applied",
                "ntfs:replay:mft_mirror:applied",
            ),
        ),
    ],
)
def test_ntfs_writer_emits_declared_and_replay_stage_boundaries(
    tmp_path, monkeypatch, nonresident, expected_replay_points
):
    monkeypatch.setenv(ENABLE_ENV, "1")
    fixture = make_crash_fixture(tmp_path, nonresident=nonresident)
    trace = fault_point_trace(
        NtfsNativeWriter(recover="native").write_file,
        str(fixture.image),
        "/target",
        b"fault trace\n",
    )
    assert set(FAULT_POINTS) <= set(trace)
    assert all(trace.count(point) == 1 for point in FAULT_POINTS)
    assert (
        tuple(point for point in trace if point in NTFS_REPLAY_WRITE_FAULT_POINTS)
        == expected_replay_points
    )
    assert trace.count("mutation:start") == 1
    assert trace.count("mutation:flush") == 1
    assert trace.count("mutation:done") == 1


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-kill is POSIX-only")
@pytest.mark.skipif(
    shutil.which("mkntfs") is None
    or shutil.which("ntfscp") is None
    or shutil.which("ntfscat") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp/ntfscat) are unavailable",
)
@pytest.mark.parametrize(
    "point",
    (*FAULT_POINTS, *NTFS_REPLAY_WRITE_FAULT_POINTS, *NTFS_REPLAY_FAULT_POINTS),
)
def test_ntfs_process_kill_preserves_source_until_atomic_replace(
    tmp_path, point
) -> None:
    """Every crash boundary preserves the source until replace has completed."""

    stream_point = "ntfs:replay:stream_write:applied"
    active = point in {"replay:undo:start", "replay:undo:done"}
    fixture = make_crash_fixture(
        tmp_path, nonresident=point == stream_point, active=active
    )
    image = fixture.image
    before = _sha256(image)
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
    after = _sha256(image)
    if point == "publish:done":
        assert after != before
        from mountfile.ntfs_native_writer import probe_ntfs_log

        assert probe_ntfs_log(str(image)).clean is True
        assert _ntfscat(image, "/target") == b"killed write\n"
    else:
        assert after == before
