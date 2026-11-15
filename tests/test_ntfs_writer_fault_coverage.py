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


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-kill is POSIX-only")
@pytest.mark.skipif(
    shutil.which("mkntfs") is None or shutil.which("ntfscp") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp) are unavailable",
)
def test_ntfs_writer_emits_each_declared_fault_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv(ENABLE_ENV, "1")
    fixture = make_crash_fixture(tmp_path)
    trace = fault_point_trace(
        NtfsNativeWriter(recover="native").write_file,
        str(fixture.image),
        "/target",
        b"fault trace\n",
    )
    assert len(trace) == len(FAULT_POINTS)
    assert set(trace) == set(FAULT_POINTS)
    assert trace.count("mutation:start") == 1
    assert trace.count("mutation:flush") == 1
    assert trace.count("mutation:done") == 1


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-kill is POSIX-only")
@pytest.mark.skipif(
    shutil.which("mkntfs") is None or shutil.which("ntfscp") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp) are unavailable",
)
@pytest.mark.parametrize("point", FAULT_POINTS)
def test_ntfs_process_kill_preserves_source_until_atomic_publish(
    tmp_path, point
) -> None:
    """Every crash boundary preserves the source until replace has completed."""

    fixture = make_crash_fixture(tmp_path)
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
    else:
        assert after == before
