# SPDX-License-Identifier: MIT
"""Issue #510 acceptance legs that need an independent checker or a refusal proof.

C6: the replayed crash fixture must pass an *independent* checker (``ntfsfix
-n`` from ntfs-3g) — the production gate (``probe_ntfs_log``) alone is not an
oracle, it shares code with the replay it validates.

D3/D4: a source image modified concurrently while a staged write is in
flight must be refused with the original bytes preserved byte-for-byte.
This complements the SIGKILL matrix in
``tests/test_ntfs_writer_fault_coverage.py`` (crash preservation per fault
point) with the *concurrent modification* refusal leg.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from mountfile.native_write import ENABLE_ENV, NativeWriteUnsafe
from mountfile.ntfs_native_writer import (
    NtfsNativeWriter,
    NtfsProfileUnsupported,
    NtfsReplayQuarantined,
)
from mountfile.ntfs_replay import native_ntfs_recover
from tests.ntfs_crash_fixture import make_crash_fixture

needs_ntfs = pytest.mark.skipif(
    shutil.which("mkntfs") is None or shutil.which("ntfscp") is None,
    reason="ntfs-3g tools (mkntfs/ntfscp) are unavailable",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@needs_ntfs
@pytest.mark.skipif(
    shutil.which("ntfsfix") is None,
    reason="ntfsfix (ntfs-3g) is unavailable",
)
def test_replayed_crash_volume_passes_independent_ntfsfix_checker(tmp_path):
    """C6: the dirty fixture fails ntfsfix; the replayed volume passes it.

    ``-n`` keeps the check read-only: ntfsfix must never be the thing that
    repairs the volume inside an acceptance test.
    """
    fixture = make_crash_fixture(tmp_path)
    dirty = subprocess.run(
        ["ntfsfix", "-n", str(fixture.image)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert dirty.returncode != 0, dirty.stdout

    result = native_ntfs_recover(str(fixture.image))
    assert result.changed is True
    assert result.redo_applied == 1
    assert result.volume_flags_after == 0

    recovered = subprocess.run(
        ["ntfsfix", "-n", str(fixture.image)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert "processed successfully" in recovered.stdout


@needs_ntfs
def test_concurrent_source_modification_is_refused_and_source_preserved(
    tmp_path, monkeypatch
):
    """D4 (+ D3): a source changed during staging is never published.

    Mirrors ``test_native_ext2_rejects_concurrent_change_before_commit``:
    the source is mutated right after the disposable copy is made, the
    SHA-256 identity guard must refuse the publish, and the source must stay
    byte-for-byte identical with no staging leftovers behind.
    """
    monkeypatch.setenv(ENABLE_ENV, "1")
    fixture = make_crash_fixture(tmp_path)
    image = fixture.image
    pristine = tmp_path / "pristine-reference.img"
    shutil.copy2(image, pristine)
    before = _sha256(image)

    import mountfile.ntfs_native_writer as writer_module

    original_copy = writer_module._image_copy
    calls = 0

    def mutate_source_after_copy(source):
        nonlocal calls
        calls += 1
        staged = original_copy(source)
        with open(source, "r+b") as stream:
            stream.seek(512)
            value = stream.read(1)
            stream.seek(512)
            stream.write(bytes([value[0] ^ 0x01]))
        return staged

    monkeypatch.setattr(writer_module, "_image_copy", mutate_source_after_copy)
    with pytest.raises(NativeWriteUnsafe, match="changed concurrently"):
        NtfsNativeWriter(recover="native").write_file(str(image), "/target", b"x")
    assert calls == 1
    # The staged write is never published: apart from the single external
    # byte flip at offset 512 itself, the source must equal the pristine
    # reference - no replayed metadata, no staged content, no leftovers.
    after = _sha256(image)
    assert after != before  # the external flip itself happened
    pristine_bytes = pristine.read_bytes()
    source_bytes = image.read_bytes()
    assert len(pristine_bytes) == len(source_bytes)
    differences = [
        offset
        for offset, (was, now) in enumerate(
            zip(pristine_bytes, source_bytes, strict=True)
        )
        if was != now
    ]
    assert differences == [512]
    leftovers = [
        entry.name
        for entry in tmp_path.iterdir()
        if entry is not image and entry is not pristine and ".mountfile-" in entry.name
    ]
    assert leftovers == []


@needs_ntfs
def test_default_writer_is_fail_closed_and_refuses_dirty_image(tmp_path, monkeypatch):
    """C1: ``NtfsNativeWriter()`` with no arguments refuses by default.

    The default profile must be ``recover='refuse'`` with the enable flag
    required: a dirty image gets a typed refusal, is never replayed and the
    source stays byte-for-byte identical.  Quarantine (evidence copy) is the
    explicit ``recover='copy'`` contract, asserted separately below.
    """
    import mountfile.ntfs_native_writer as writer_module

    writer = writer_module.NtfsNativeWriter()
    assert writer.recover == "refuse"
    assert writer.require_flag is True

    monkeypatch.setenv(ENABLE_ENV, "1")
    fixture = make_crash_fixture(tmp_path)
    before = _sha256(fixture.image)
    with pytest.raises(NtfsProfileUnsupported, match="needs log recovery"):
        writer.write_file(str(fixture.image), "/target", b"x")
    assert _sha256(fixture.image) == before
    assert not list(tmp_path.glob("*.mountfile-*"))


@needs_ntfs
def test_recover_copy_quarantines_dirty_image_and_preserves_source(
    tmp_path, monkeypatch
):
    """C1/C7: ``recover='copy'`` quarantines a dirty image, never replays it."""
    monkeypatch.setenv(ENABLE_ENV, "1")
    fixture = make_crash_fixture(tmp_path)
    before = _sha256(fixture.image)
    with pytest.raises(NtfsReplayQuarantined) as excinfo:
        NtfsNativeWriter(recover="copy").write_file(str(fixture.image), "/target", b"x")
    quarantine = excinfo.value.quarantine
    assert Path(quarantine.copy).is_file()
    assert _sha256(Path(quarantine.copy)) == before
    assert _sha256(fixture.image) == before
    marker = Path(quarantine.marker)
    assert marker.is_file()
    assert "not_requested" in marker.read_text(encoding="utf-8")
