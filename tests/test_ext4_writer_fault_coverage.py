# SPDX-License-Identifier: MIT
"""Issue #530 / #387, P2: process-kill coverage for the ext4 native writer.

Mirrors ``tests/test_ntfs_writer_fault_coverage.py`` for the ext4 profile:

* the runtime itself must emit every durable boundary it declares;
* a SIGKILL at any boundary before the atomic publish must leave the source
  image byte-for-byte identical (sha256 preservation);
* only a crash *after* ``publish:done`` may change the image, and then the
  journal gate and ``e2fsck -fn`` must both report a clean volume.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from mountfile.ext4_native_writer import (
    Ext4NativeWriter,
    libext2fs_available,
    probe_journal,
)
from mountfile.journal_safety import JournalState
from mountfile.native_write import ENABLE_ENV, FAULT_POINTS, fault_point_trace

_REPO_ROOT = Path(__file__).resolve().parents[1]

needs_ext = pytest.mark.skipif(
    shutil.which("mkfs.ext4") is None
    or shutil.which("debugfs") is None
    or shutil.which("e2fsck") is None
    or not libext2fs_available(),
    reason="e2fsprogs tools or libext2fs are unavailable",
)

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

from mountfile.ext4_native_writer import Ext4NativeWriter

Ext4NativeWriter().write_file(sys.argv[2], "/target", b"killed ext4 write\n")
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_clean_image(tmp_path: Path) -> Path:
    image = tmp_path / "ext4.img"
    with open(image, "wb") as handle:
        handle.truncate(32 << 20)
    result = subprocess.run(
        [
            "mkfs.ext4",
            "-q",
            "-F",
            "-b",
            "4096",
            "-O",
            "^metadata_csum_seed",
            str(image),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"payload" + b"p" * (4096 - 7))
    result = subprocess.run(
        ["debugfs", "-w", "-R", f"write {payload} /target", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return image


@needs_ext
def test_ext4_writer_emits_declared_fault_boundaries(tmp_path, monkeypatch):
    """The runtime itself traces every boundary; no remembered names (#387)."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_clean_image(tmp_path)
    trace = fault_point_trace(
        Ext4NativeWriter().write_file, str(image), "/target", b"fault trace\n"
    )
    assert set(trace) <= set(FAULT_POINTS)
    assert trace.count("copy:start") == 1
    assert trace.count("mutation:start") == 1
    assert trace.count("mutation:flush") == 1
    assert trace.count("mutation:done") == 1
    assert trace.count("publish:start") == 1
    assert trace.count("publish:done") == 1
    # A successful write ends with the volume clean and checker-verified.
    assert probe_journal(str(image)).state is JournalState.CLEAN


@pytest.mark.skipif(os.name != "posix", reason="SIGKILL process-kill is POSIX-only")
@needs_ext
@pytest.mark.parametrize("point", FAULT_POINTS)
def test_ext4_process_kill_preserves_source_until_atomic_publish(
    tmp_path, monkeypatch, point
):
    """Every crash boundary preserves the source until replace has completed."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_clean_image(tmp_path)
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
        timeout=180,
    )
    if result.returncode != -signal.SIGKILL:
        # ``helper:*`` boundaries belong to the debugfs writer and are never
        # emitted by this profile; the child then simply completed.
        assert result.returncode == 0, (point, result.stdout, result.stderr)
        return
    after = _sha256(image)
    if point == "publish:done":
        assert after != before
        probe = probe_journal(str(image))
        assert probe.state is JournalState.CLEAN
        assert subprocess.run(
            ["e2fsck", "-fn", str(image)], capture_output=True
        ).returncode == 0
    else:
        assert after == before, f"SIGKILL at {point} mutated the source image"
