# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""D5 (#510): the application-route writer must read the staged copy back.

The gated experimental ``NtfsNativeWriter`` already refuses publication when
the staged copy does not read back the payload. This file pins the same
contract on the VFS/application route (``NtfsTransactionalWriter``): before
the atomic replace, the target is read through the independent ``ntfscat``
helper and must equal the requested bytes byte-for-byte; a mismatch refuses
publication and the source image stays untouched.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from mountfile.native_write import ENABLE_ENV, NativeWriteUnsafe

needs_ntfs = pytest.mark.skipif(
    shutil.which("mkntfs") is None
    or shutil.which("ntfscp") is None
    or shutil.which("ntfscat") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp/ntfscat) are unavailable",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ntfs_image_with_target(tmp_path: Path) -> Path:
    image = tmp_path / "ntfs.img"
    with open(image, "wb") as handle:
        handle.truncate(32 << 20)
    result = subprocess.run(
        ["mkntfs", "-Q", "-F", str(image)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    seed = tmp_path / "seed.bin"
    seed.write_bytes(b"seed-content\n")
    result = subprocess.run(
        ["ntfscp", "-f", str(image), str(seed), "/target"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return image


@needs_ntfs
def test_transactional_route_reads_back_staged_bytes(tmp_path, monkeypatch):
    """After write_file the published image holds exactly the payload."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    from mountfile.native_write import NtfsTransactionalWriter

    # The diagnostic ntfsfix gate is a no-op here (repository pattern from
    # tests/test_native_write.py): this file pins the *read-back* contract,
    # not the log gate.
    monkeypatch.setattr(
        "mountfile.native_write.NTFSLogFileManager.require_clean",
        lambda self, image_path, runner: None,
    )
    image = _ntfs_image_with_target(tmp_path)
    payload = b"app-route read-back\n"
    NtfsTransactionalWriter().write_file(str(image), "target", payload)

    read = subprocess.run(
        ["ntfscat", str(image), "/target"],
        capture_output=True,
        check=False,
    )
    assert read.returncode == 0, read.stderr
    assert read.stdout == payload


@needs_ntfs
def test_transactional_route_refuses_readback_mismatch(tmp_path, monkeypatch):
    """A lying read-back refuses publication; the source is preserved."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    import mountfile.native_write as nw

    image = _ntfs_image_with_target(tmp_path)
    before = _sha256(image)

    def lying_readback(image_path, internal_path):
        return b"not-what-was-written"

    monkeypatch.setattr(nw, "_read_ntfs_file_bytes", lying_readback)
    monkeypatch.setattr(
        "mountfile.native_write.NTFSLogFileManager.require_clean",
        lambda self, image_path, runner: None,
    )

    with pytest.raises(NativeWriteUnsafe, match="read-back mismatch"):
        nw.NtfsTransactionalWriter().write_file(
            str(image), "target", b"must-not-publish\n"
        )
    assert _sha256(image) == before
