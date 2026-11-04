"""Regressions for RWBackend extraction through the hardened writer."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mountfile.exfat_reader import ExfatReadError, NativeExfatImage
from mountfile.extract import ExtractError, ExtractionLimits
from mountfile.universal_fs_rw import (
    FSEntry,
    FSInfo,
    NativeExfatRW,
    RWBackend,
    UniversalFSRW,
)


class _MemoryBackend(RWBackend):
    def __init__(self, payload=b"image payload", *, size=None, read_error=None):
        super().__init__(Path("unused.img"))
        self.payload = payload
        self.size = len(payload) if size is None else size
        self.read_error = read_error
        self.read_calls = 0

    def info(self):
        return FSInfo(fs_type="test", offset=0, backend="memory")

    def list(self, path="/"):
        if path == "/":
            return [
                FSEntry(
                    name="evidence.bin",
                    path="/evidence.bin",
                    size=self.size,
                    is_dir=False,
                )
            ]
        return []

    def read(self, path):
        assert path == "/evidence.bin"
        self.read_calls += 1
        if self.read_error is not None:
            raise self.read_error
        return self.payload

    def write_file(self, path, data):
        raise AssertionError("not used by extraction")

    def mkdir(self, path):
        raise AssertionError("not used by extraction")

    def delete(self, path, recursive=False):
        raise AssertionError("not used by extraction")

    def rename(self, src, dst):
        raise AssertionError("not used by extraction")


def _facade(backend):
    facade = UniversalFSRW.__new__(UniversalFSRW)
    facade.backend = backend
    return facade


def test_rwbackend_extract_uses_hardened_writer_and_preserves_public_result(tmp_path):
    backend = _MemoryBackend(b"verified evidence")
    destination = tmp_path / "output" / "evidence.bin"

    result = backend.extract("/evidence.bin", destination)

    assert result == destination
    assert result.read_bytes() == b"verified evidence"
    assert backend.read_calls == 1


def test_universal_fsrw_extract_delegates_to_safe_rwbackend_path(tmp_path):
    backend = _MemoryBackend(b"facade evidence")
    destination = tmp_path / "evidence.bin"

    result = _facade(backend).extract("/evidence.bin", destination)

    assert result == destination
    assert destination.read_bytes() == b"facade evidence"


def test_rwbackend_extract_uses_bounded_reader_api_and_resource_limit(tmp_path):
    class RangeReader:
        max_read_range_bytes = 3

        def __init__(self):
            self.calls = []

        def file_size(self, path):
            assert path == "/EVIDENCE.BIN"
            return 7

        def read_file_range(self, path, offset, length):
            self.calls.append((path, offset, length))
            return b"payload"[offset : offset + length]

    backend = _MemoryBackend(b"unused payload")
    reader = RangeReader()
    backend._reader = reader
    destination = tmp_path / "evidence.bin"
    destination.write_bytes(b"old evidence")

    with pytest.raises(ExtractError, match="size limit"):
        backend.extract(
            "/EVIDENCE.BIN",
            destination,
            limits=ExtractionLimits(max_file_size=6),
        )

    assert reader.calls == []
    assert backend.read_calls == 0
    assert destination.read_bytes() == b"old evidence"

    backend.extract(
        "/EVIDENCE.BIN",
        destination,
        limits=ExtractionLimits(max_file_size=7, max_total_size=7),
    )
    assert destination.read_bytes() == b"payload"
    assert reader.calls == [
        ("/EVIDENCE.BIN", 0, 3),
        ("/EVIDENCE.BIN", 3, 3),
        ("/EVIDENCE.BIN", 6, 1),
    ]


def test_rwbackend_extract_refuses_destination_symlink_before_reading_source(tmp_path):
    backend = _MemoryBackend()
    protected = tmp_path / "protected.bin"
    protected.write_bytes(b"must remain unchanged")
    destination = tmp_path / "evidence.bin"
    try:
        destination.symlink_to(protected)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable in test environment")

    with pytest.raises(ExtractError, match="symlink|safely write"):
        backend.extract("/evidence.bin", destination)

    assert protected.read_bytes() == b"must remain unchanged"
    assert destination.is_symlink()
    assert backend.read_calls == 0


def test_rwbackend_extract_does_not_require_case_sensitive_stat(tmp_path):
    class CaseInsensitiveBackend(_MemoryBackend):
        def read(self, path):
            if path.casefold() != "/evidence.bin":
                raise FileNotFoundError(path)
            return self.payload

    backend = CaseInsensitiveBackend(b"case-insensitive source")
    destination = tmp_path / "evidence.bin"
    assert backend.stat("/EVIDENCE.BIN") is None  # existing exact-case stat contract
    assert backend.extract("/EVIDENCE.BIN", destination) == destination
    assert destination.read_bytes() == b"case-insensitive source"


def test_rwbackend_extract_exfat_case_insensitive_real_image(tmp_path):
    mkfs = shutil.which("mkfs.exfat")
    if not mkfs:
        pytest.skip("mkfs.exfat required for this real-image test")
    image = tmp_path / "exfat.img"
    size = 16 * 1024 * 1024
    with image.open("wb") as stream:
        stream.truncate(size)
    subprocess.run([mkfs, str(image)], check=True, capture_output=True, timeout=60)
    backend = NativeExfatRW(image, volume_size=size)
    try:
        backend.write_file("/Report-é.txt", b"real exFAT data")
        destination = tmp_path / "result.bin"
        assert backend.extract("/report-É.TXT", destination) == destination
        assert destination.read_bytes() == b"real exFAT data"
        backend.write_file("/second.bin", b"second allocation")
        assert backend.read("/REPORT-é.txt") == b"real exFAT data"
        backend.delete("/Report-é.txt")
        assert backend.read("/second.bin") == b"second allocation"
    finally:
        backend.close()


def test_exfat_reopen_revalidates_dirty_volume(tmp_path):
    mkfs = shutil.which("mkfs.exfat")
    if not mkfs:
        pytest.skip("mkfs.exfat required for this real-image test")
    image = tmp_path / "dirty.img"
    size = 16 * 1024 * 1024
    with image.open("wb") as stream:
        stream.truncate(size)
    subprocess.run([mkfs, str(image)], check=True, capture_output=True, timeout=60)
    reader = NativeExfatImage(image, volume_size=size)
    reader.close()
    # VolumeFlags are excluded from the exFAT boot checksum. Update both boot
    # copies to model a volume marked dirty while the provider is closed.
    with image.open("r+b") as stream:
        for offset in (0x6A, 12 * 512 + 0x6A):
            stream.seek(offset)
            stream.write((2).to_bytes(2, "little"))
    with pytest.raises(ExfatReadError, match="dirty"):
        reader.reopen()
    assert reader._stream is None


def test_rwbackend_extract_replaces_hardlink_without_modifying_external_file(tmp_path):
    backend = _MemoryBackend()
    protected = tmp_path / "protected.bin"
    protected.write_bytes(b"must remain unchanged")
    destination = tmp_path / "evidence.bin"
    try:
        os.link(protected, destination)
    except OSError:
        pytest.skip("hard links unavailable in test environment")
    backend.extract("/evidence.bin", destination)
    assert protected.read_bytes() == b"must remain unchanged"
    assert destination.read_bytes() == backend.payload


def test_rwbackend_extract_refuses_symlinked_parent(tmp_path):
    backend = _MemoryBackend()
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "output"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks unavailable in test environment")
    with pytest.raises(ExtractError, match="symlink|safely"):
        backend.extract("/evidence.bin", root / "link" / "evidence.bin")
    assert list(outside.iterdir()) == []


def test_rwbackend_extract_preserves_non_os_source_errors_and_destination(tmp_path):
    source_error = ValueError("simulated source read failure")
    backend = _MemoryBackend(read_error=source_error)
    destination = tmp_path / "evidence.bin"
    destination.write_bytes(b"previous evidence")

    with pytest.raises(ValueError, match="simulated source read failure") as caught:
        backend.extract("/evidence.bin", destination)

    assert caught.value is source_error
    assert destination.read_bytes() == b"previous evidence"
    assert list(tmp_path.glob(".mountfile-*.tmp")) == []
