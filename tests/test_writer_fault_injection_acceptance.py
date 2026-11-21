# SPDX-License-Identifier: MIT
"""Issue #510 / #387: cancellation and injected-failure coverage at every seam.

The existing suites answer two questions well:

* ``test_ext4_writer_fault_coverage`` / ``test_ntfs_writer_fault_coverage``
  and the phase-injection matrices cover what happens when a step *fails*;
* the SIGKILL matrices cover what happens when the machine *dies*.

They do not cover the third state the matrix declares - ``cancellation`` - and
they do not assert that a refused write leaves no staging copy beside the
caller's evidence image.  A cancelled write reaches the writer as a
``BaseException`` (``asyncio.CancelledError``, ``KeyboardInterrupt``), which is
*not* an ``Exception``, so a cleanup guarded by ``except Exception`` silently
leaks ``.mountfile-image-*.tmp`` next to the source image.

Contract under test, for every declared
:data:`mountfile.native_write.FAULT_POINTS` boundary, for ext4/JBD2 and NTFS::

* an injected exception and a cancellation both surface as an error - they are
  never swallowed into a "successful" return value;
* the caller's image is byte-identical for every boundary before every
  ``publish:done``;
* an injection at ``publish:done`` happens after the atomic replace, so the image
  must then hold exactly the requested payload and still be a clean volume;
* no staging artefact is left in the image directory on either path;
* after a refused write the same image is still writable (idempotent retry).

These are test-execution facts about declared writer boundaries.  They are not a
per-metadata-step matrix, not power-loss evidence, and not product write support.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from mountfile.ext4_native_writer import (
    Ext4NativeWriter,
    libext2fs,
    libext2fs_available,
    probe_journal,
)
from mountfile.journal_safety import JournalState
from mountfile.native_write import (
    ENABLE_ENV,
    FAULT_POINTS,
    NativeWriteError,
    fault_hook_installed,
)
from mountfile.ntfs_native_writer import (
    NtfsNativeWriter,
    NtfsProfileUnsupported,
    probe_ntfs_log,
)
from tests.ntfs_crash_fixture import make_crash_fixture

PAYLOAD = b"injected fault acceptance\n"

#: Staging prefix created by ``native_write._image_copy``.
STAGING_GLOB = ".mountfile-image-*.tmp"


class WriteCancelled(BaseException):
    """A cancellation reaching a durable boundary is not an ``Exception``."""


_KINDS = {
    "injected_exception": RuntimeError("injected write failure"),
    "cancellation": WriteCancelled("cancelled at a durable boundary"),
}

_EXT4_READY = (
    shutil.which("mkfs.ext4") is not None
    and shutil.which("debugfs") is not None
    and shutil.which("e2fsck") is not None
    and libext2fs_available()
)
_NTFS_READY = (
    shutil.which("mkntfs") is not None
    and shutil.which("ntfscp") is not None
    and shutil.which("ntfscat") is not None
)

_PROFILES = (
    pytest.param(
        "ext4",
        marks=pytest.mark.skipif(
            not _EXT4_READY, reason="e2fsprogs tools or libext2fs are unavailable"
        ),
        id="ext4",
    ),
    pytest.param(
        "ntfs",
        marks=pytest.mark.skipif(
            not _NTFS_READY,
            reason="ntfs-3g tools (mkntfs/ntfscp/ntfscat) are unavailable",
        ),
        id="ntfs",
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_ext4(tmp_path: Path) -> Path:
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


def _make_image(profile: str, tmp_path: Path) -> Path:
    if profile == "ext4":
        return _make_ext4(tmp_path)
    return make_crash_fixture(tmp_path).image


def _write(profile: str, image: Path) -> None:
    if profile == "ext4":
        Ext4NativeWriter().write_file(str(image), "/target", PAYLOAD)
        return
    NtfsNativeWriter(recover="native").write_file(str(image), "/target", PAYLOAD)


def _assert_published(profile: str, image: Path) -> None:
    """The post-``publish:done`` state must be complete, not merely newer."""

    if profile == "ext4":
        assert probe_journal(str(image)).state is JournalState.CLEAN
        result = subprocess.run(
            ["e2fsck", "-fn", str(image)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr
        dumped = subprocess.run(
            ["debugfs", "-R", "cat /target", str(image)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert dumped.returncode == 0, dumped.stderr
        assert dumped.stdout.encode() == PAYLOAD
        return
    assert probe_ntfs_log(str(image)).clean is True
    read = subprocess.run(
        ["ntfscat", str(image), "/target"], capture_output=True, check=False
    )
    assert read.returncode == 0, read.stderr.decode("utf-8", "replace")
    assert read.stdout == PAYLOAD


@pytest.mark.parametrize("kind", sorted(_KINDS))
@pytest.mark.parametrize("point", FAULT_POINTS)
@pytest.mark.parametrize("profile", _PROFILES)
def test_injected_failure_at_each_boundary_preserves_source_or_publishes_fully(
    tmp_path, monkeypatch, profile, point, kind
) -> None:
    """Every declared boundary is fail-closed before publish and complete after."""

    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_image(profile, tmp_path)
    before = _sha256(image)
    injected = _KINDS[kind]

    def hook(name: str) -> None:
        if name == point:
            raise injected

    fault_hook_installed(hook)
    try:
        with pytest.raises(type(injected)):
            _write(profile, image)
    finally:
        fault_hook_installed(None)

    after = _sha256(image)
    if point == "publish:done":
        assert after != before, "the boundary claims the replace already happened"
        _assert_published(profile, image)
    else:
        assert after == before, f"{kind} at {point} mutated the caller's image"
    assert (
        list(tmp_path.glob(STAGING_GLOB)) == []
    ), f"{kind} at {point} left a staging copy behind"


@pytest.mark.parametrize("kind", sorted(_KINDS))
@pytest.mark.parametrize("profile", _PROFILES)
def test_refused_write_leaves_image_writable(tmp_path, monkeypatch, profile, kind):
    """After a refused write the same image is still writable, unmodified."""

    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_image(profile, tmp_path)
    before = _sha256(image)
    injected = _KINDS[kind]

    def hook(name: str) -> None:
        if name == "mutation:done":
            raise injected

    fault_hook_installed(hook)
    try:
        with pytest.raises(type(injected)):
            _write(profile, image)
    finally:
        fault_hook_installed(None)
    assert _sha256(image) == before
    assert list(tmp_path.glob(STAGING_GLOB)) == []

    _write(profile, image)
    assert _sha256(image) != before
    _assert_published(profile, image)


class _ShortWriteFacade:
    """``ctypes`` facade whose ``ext2fs_file_write`` really writes fewer bytes.

    libext2fs offers no fault injection, so the partial write is produced by the
    binding itself: the call that would write ``nbytes`` is issued with a smaller
    count, which is exactly what a truncated ``write(2)`` looks like to the
    caller - the file receives part of the payload and the out-parameter reports
    the short count.  No root, no loop mount, no privileged helper is involved.
    """

    def __init__(self, real, written: int) -> None:
        self._real = real
        self._written = written

    def __getattr__(self, name):
        return getattr(self._real, name)

    def ext2fs_file_write(self, handle, buffer, nbytes, done):
        return self._real.ext2fs_file_write(handle, buffer, self._written, done)


class _ShortWriteLibrary:
    """``LibExt2fs`` stand-in that injects a genuine partial data write."""

    def __init__(self, real, written: int) -> None:
        self._real = real
        self.lib = _ShortWriteFacade(real.lib, written)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _short_run(real_run, written: int = 1):
    """Make ``ntfscp`` publish a truncated payload, as a short copy would.

    ``ntfscp`` is the NTFS data primitive; truncating the staged payload before
    the helper runs is the same observable event as a helper that stops early.
    """

    def runner(command, **kwargs):
        command = list(command)
        if command and command[0] == "ntfscp":
            payload = Path(command[3])
            payload.write_bytes(payload.read_bytes()[:written])
        return real_run(command, **kwargs)

    return runner


def _short_write_ext4(image: Path, written: int = 1) -> None:
    Ext4NativeWriter(library=_ShortWriteLibrary(libext2fs(), written)).write_file(
        str(image), "/target", PAYLOAD
    )


def _short_write_ntfs(image: Path) -> None:
    NtfsNativeWriter(recover="native").write_file(str(image), "/target", PAYLOAD)


@pytest.mark.parametrize("profile", _PROFILES)
def test_short_write_at_the_data_primitive_refuses_publication(
    tmp_path, monkeypatch, profile
) -> None:
    """A truncated data write is refused; the caller's image is never replaced.

    FAT12/16, FAT32 and exFAT already assert that a short primitive write rolls
    back instead of publishing.  This is the same cell for ext4/JBD2 and NTFS:
    ext4 reports ``short ext4 write: n/m bytes`` from the binding, NTFS is caught
    by the exact staged read-back, and in both cases publication must not happen.
    """

    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_image(profile, tmp_path)
    before = _sha256(image)
    # The NTFS fixture is a deliberate crash image, so its gate is *expected* to
    # be dirty; the contract here is that the refused write leaves that state
    # exactly as it found it.
    gate_before = probe_ntfs_log(str(image)).as_dict() if profile == "ntfs" else None

    if profile == "ext4":
        with pytest.raises(NativeWriteError, match="short ext4 write"):
            _short_write_ext4(image)
    else:
        import mountfile.ntfs_native_writer as ntfs_writer

        with monkeypatch.context() as scope:
            scope.setattr(ntfs_writer, "_run", _short_run(ntfs_writer._run))
            with pytest.raises(NtfsProfileUnsupported, match="read-back mismatch"):
                _short_write_ntfs(image)

    assert _sha256(image) == before, "a short write must never reach the source image"
    assert list(tmp_path.glob(STAGING_GLOB)) == [], "staging copy left behind"
    if profile == "ntfs":
        assert probe_ntfs_log(str(image)).as_dict() == gate_before


@pytest.mark.parametrize("profile", _PROFILES)
def test_short_write_refusal_leaves_image_writable(
    tmp_path, monkeypatch, profile
) -> None:
    """After a refused short write the same image still accepts a full write."""

    monkeypatch.setenv(ENABLE_ENV, "1")
    image = _make_image(profile, tmp_path)
    before = _sha256(image)

    if profile == "ext4":
        with pytest.raises(NativeWriteError, match="short ext4 write"):
            _short_write_ext4(image)
    else:
        import mountfile.ntfs_native_writer as ntfs_writer

        with monkeypatch.context() as scope:
            scope.setattr(ntfs_writer, "_run", _short_run(ntfs_writer._run))
            with pytest.raises(NtfsProfileUnsupported, match="read-back mismatch"):
                _short_write_ntfs(image)

    assert _sha256(image) == before
    assert list(tmp_path.glob(STAGING_GLOB)) == []

    _write(profile, image)
    assert _sha256(image) != before
    _assert_published(profile, image)
