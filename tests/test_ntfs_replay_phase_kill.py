# SPDX-License-Identifier: MIT
"""Issue #387 / #510, D2: real SIGKILL coverage for NTFS ``$LogFile`` replay phases.

``native_ntfs_recover`` declares three mutation boundaries through the shared
``FAULT_HOOK`` seam: ``mutation:start``, ``mutation:flush`` and
``mutation:done``.  A real POSIX child process is SIGKILLed at each boundary;
afterwards the test proves that

* a kill at ``mutation:start`` leaves the source image byte-for-byte
  identical (sha256 preservation -- nothing has been written yet);
* a kill at ``mutation:flush`` or ``mutation:done`` converges: re-running
  ``native_ntfs_recover`` on the interrupted stage finishes the replay
  idempotently, ``$Volume`` ends clean, and the independent ``ntfsfix -n``
  checker accepts the recovered volume.

This is process-kill evidence only.  It does not claim power-loss durability
or product-level NTFS write support.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from mountfile.native_write import fault_hook_installed
from mountfile.ntfs_reader import parse_ntfs_attribute_stream, probe_ntfs_metadata
from mountfile.ntfs_replay import _attribute_offset, _Mft, native_ntfs_recover

_REPO_ROOT = Path(__file__).resolve().parents[1]

needs_ntfs = pytest.mark.skipif(
    os.name != "posix"
    or shutil.which("mkntfs") is None
    or shutil.which("ntfscp") is None
    or shutil.which("ntfsfix") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp/ntfsfix) are unavailable",
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

from mountfile.ntfs_replay import native_ntfs_recover

native_ntfs_recover(sys.argv[2])
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _volume_dirty_flag(image: Path) -> int:
    """Flags word of the ``$Volume`` ``$VOLUME_INFORMATION`` attribute.

    The previous revision read two bytes at boot-sector offset ``0x78``,
    which is bootstrap code on every real ``mkntfs`` volume -- the constant
    it returned (0x6e20 = 28192) failed the convergence branches regardless
    of the actual dirty state (CI incident #571).  The attribute-offset
    path mirrors ``ntfs_crash_fixture.mark_volume_dirty`` and shares no
    code with the replay executor under test.
    """

    profile = probe_ntfs_metadata(str(image), volume_size=os.path.getsize(image))
    mft_data = [
        attribute
        for attribute in profile.attributes
        if attribute.type_code == 0x80 and not attribute.name
    ][0]
    with open(image, "rb") as handle:
        mft = _Mft(handle, profile.boot, mft_data, base_offset=0)
        volume = mft.read_record(3)
        volume_info = [
            attribute
            for attribute in parse_ntfs_attribute_stream(
                volume, allow_end_marker_padding=True
            )
            if attribute.type_code == 0x70
        ][0]
        offset = _attribute_offset(volume, volume_info.attribute_id)
        fixed = volume.fixed_record
        value = struct.unpack_from("<H", fixed, offset + 20)[0]
        return struct.unpack_from("<H", fixed, offset + value + 10)[0]


def _ntfsfix_no(image: Path) -> subprocess.CompletedProcess:
    """Read-only ntfsfix check; never repairs anything."""

    return subprocess.run(
        ["ntfsfix", "-n", str(image)], capture_output=True, text=True, check=False
    )


def _kill_at(phase: str, image: Path) -> subprocess.CompletedProcess:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(_REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-c", _KILL_CHILD, phase, str(image)],
        cwd=str(_REPO_ROOT),
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


@needs_ntfs
def test_replay_declares_exactly_the_three_mutation_boundaries(
    tmp_path, monkeypatch
) -> None:
    """The kill harness is only complete if the seam emits every declared cell."""

    from tests.ntfs_crash_fixture import make_crash_fixture

    fixture = make_crash_fixture(tmp_path)
    trace: list[str] = []
    monkeypatch.setattr(
        "mountfile.native_write.FAULT_HOOK", trace.append, raising=False
    )
    native_ntfs_recover(str(fixture.image))
    assert trace == ["mutation:start", "mutation:flush", "mutation:done"]


@needs_ntfs
@pytest.mark.parametrize("phase", ("mutation:start", "mutation:flush", "mutation:done"))
def test_sigkill_at_each_replay_phase_preserves_or_converges(tmp_path, phase) -> None:
    """D2: every crash boundary keeps the source intact or recoverable."""

    from tests.ntfs_crash_fixture import make_crash_fixture

    fixture = make_crash_fixture(tmp_path)
    image = fixture.image
    before = _sha256(image)

    result = _kill_at(phase, image)
    assert result.returncode == -signal.SIGKILL, (result.stdout, result.stderr)

    if phase == "mutation:start":
        # Nothing may be written before the first flush boundary.
        assert _sha256(image) == before
        return

    # Recovery must converge on the interrupted stage: a re-run finishes the
    # replay (or finds it already applied) and leaves the volume clean.
    recovered = native_ntfs_recover(str(image))
    assert recovered.volume_flags_after == 0
    assert _volume_dirty_flag(image) == 0

    check = _ntfsfix_no(image)
    assert check.returncode == 0, check.stdout + check.stderr
    assert "processed successfully" in check.stdout
