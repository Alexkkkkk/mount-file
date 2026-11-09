"""Byte-range read/write, append and truncate across the native backends."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_fat32 import make_fat32_image  # noqa: E402

from mountfile.universal_fs_rw import Fat32RW, UniversalFSRW  # noqa: E402


def _fat(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    return image, Fat32RW(image, volume_size=image.stat().st_size)


# ------------------------------------------------------------------ read_range
def test_read_range_returns_requested_window(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        payload = bytes(range(256)) * 20
        fs.write_file("/window.bin", payload)
        assert fs.read_range("/window.bin", 0, 10) == payload[:10]
        assert fs.read_range("/window.bin", 100, 50) == payload[100:150]
        assert fs.read_range("/window.bin", len(payload) - 4) == payload[-4:]
    finally:
        fs.close()


def test_read_range_clamps_at_end_of_file(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/small.txt", b"12345")
        assert fs.read_range("/small.txt", 3, 999) == b"45"
        assert fs.read_range("/small.txt", 5, 10) == b""
        assert fs.read_range("/small.txt", 99, 10) == b""
        assert fs.read_range("/small.txt", 0, 0) == b""
    finally:
        fs.close()


def test_read_range_spans_cluster_boundary(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        payload = bytes((i * 7) % 256 for i in range(fs.cluster_size * 3))
        fs.write_file("/multi.bin", payload)
        offset = fs.cluster_size - 10
        assert fs.read_range("/multi.bin", offset, 40) == payload[offset : offset + 40]
    finally:
        fs.close()


def test_read_range_rejects_bad_arguments(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/a.txt", b"abc")
        with pytest.raises(ValueError):
            fs.read_range("/a.txt", -1, 1)
        with pytest.raises(ValueError):
            fs.read_range("/a.txt", 0, -2)
        with pytest.raises(FileNotFoundError):
            fs.read_range("/missing.txt")
        with pytest.raises(IsADirectoryError):
            fs.mkdir("/DIR")
            fs.read_range("/DIR")
    finally:
        fs.close()


# ----------------------------------------------------------------- write_range
def test_write_range_overwrites_in_place(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/note.txt", b"AAAAAAAAAA")
        fs.write_range("/note.txt", 2, b"bb")
        assert fs.read("/note.txt") == b"AAbbAAAAAA"
    finally:
        fs.close()


def test_write_range_past_end_grows_and_zero_fills(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/grow.bin", b"AB")
        fs.write_range("/grow.bin", 5, b"ZZ")
        data = fs.read("/grow.bin")
        assert data == b"AB" + b"\x00" * 3 + b"ZZ"
        assert fs.stat("/grow.bin").size == 7
    finally:
        fs.close()


def test_write_range_zeroes_stale_slack_before_extension(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/grow.bin", b"AB")
        _parent, entry, _raw, _path = fs._entry_for("/grow.bin")
        cluster = bytearray(fs._read_cluster(entry.inode))
        cluster[2:5] = b"OLD"
        fs._write_cluster(entry.inode, bytes(cluster))

        fs.write_range("/grow.bin", 5, b"ZZ")

        assert fs.read("/grow.bin") == b"AB\x00\x00\x00ZZ"
    finally:
        fs.close()


def test_write_range_grows_across_clusters(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/big.bin", b"A")
        offset = fs.cluster_size * 2 + 5
        fs.write_range("/big.bin", offset, b"payload")
        data = fs.read("/big.bin")
        assert len(data) == offset + 7
        assert data[offset : offset + 7] == b"payload"
        assert data[0:1] == b"A"
        assert data[1:offset] == b"\x00" * (offset - 1)
    finally:
        fs.close()


def test_write_range_full_cluster_overwrite(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/full.bin", b"\x00" * (fs.cluster_size * 2))
        block = b"\xff" * fs.cluster_size
        fs.write_range("/full.bin", 0, block)
        assert fs.read("/full.bin")[: fs.cluster_size] == block
        assert fs.read("/full.bin")[fs.cluster_size :] == b"\x00" * fs.cluster_size
    finally:
        fs.close()


def test_write_range_requires_existing_file(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        with pytest.raises(FileNotFoundError):
            fs.write_range("/absent.txt", 0, b"x")
    finally:
        fs.close()


def test_write_range_empty_payload_is_a_presence_check(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/keep.txt", b"keep")
        fs.write_range("/keep.txt", 0, b"")
        assert fs.read("/keep.txt") == b"keep"
        with pytest.raises(FileNotFoundError):
            fs.write_range("/nope.txt", 0, b"")
    finally:
        fs.close()


# ------------------------------------------------------------ append / truncate
def test_append_extends_file(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/log.txt", b"line1\n")
        fs.append("/log.txt", b"line2\n")
        assert fs.read("/log.txt") == b"line1\nline2\n"
        assert fs.stat("/log.txt").size == 12
    finally:
        fs.close()


def test_append_to_empty_file(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/empty.txt", b"")
        fs.append("/empty.txt", b"first")
        assert fs.read("/empty.txt") == b"first"
    finally:
        fs.close()


def test_truncate_shrinks_and_grows(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/t.bin", b"0123456789")
        fs.truncate("/t.bin", 4)
        assert fs.read("/t.bin") == b"0123"
        fs.truncate("/t.bin", 6)
        assert fs.read("/t.bin") == b"0123\x00\x00"
        fs.truncate("/t.bin", 6)
        assert fs.read("/t.bin") == b"0123\x00\x00"
    finally:
        fs.close()


def test_truncate_to_zero_leaves_readable_empty_file(tmp_path):
    _, fs = _fat(tmp_path)
    try:
        fs.write_file("/z.bin", b"abc")
        fs.truncate("/z.bin", 0)
        assert fs.read("/z.bin") == b""
        assert fs.stat("/z.bin").size == 0
    finally:
        fs.close()


# ------------------------------------------------------------- facade + fsck
def test_facade_write_flow_survives_fsck(tmp_path):
    if shutil.which("fsck.fat") is None:
        pytest.skip("dosfstools is required")
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    fs = UniversalFSRW(image, offset=0, auto_backup=False, allow_legacy_mount=False)
    try:
        fs.write_file("/config.ini", b"[main]\nkey=1\n")
        fs.write_range("/config.ini", 7, b"key=2")
        fs.append("/config.ini", b"extra=yes\n")
        assert fs.read("/config.ini") == b"[main]\nkey=2\nextra=yes\n"
        assert fs.read_range("/config.ini", 0, 6) == b"[main]"
        fs.truncate("/config.ini", 12)
        assert fs.read("/config.ini") == b"[main]\nkey=2"
    finally:
        fs.close()
    result = subprocess.run(
        ["fsck.fat", "-n", str(image)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ------------------------------------------------------------------- exFAT/ext
def test_ext2_read_range_uses_generic_fallback(tmp_path):
    if shutil.which("mke2fs") is None:
        pytest.skip("mke2fs is required")
    from test_ext2_native import _make_ext2
    from mountfile.universal_fs_rw import Ext234RW

    image = tmp_path / "data.ext2"
    _make_ext2(image)
    fs = Ext234RW(image, volume_size=image.stat().st_size)
    try:
        payload = b"0123456789abcdef" * 100
        fs.write_file("/range.bin", payload)
        assert fs.read_range("/range.bin", 4, 8) == payload[4:12]
        assert fs.read_range("/range.bin", 1500) == payload[1500:]
    finally:
        fs.close()


def test_exfat_read_range(tmp_path):
    if shutil.which("mkfs.exfat") is None:
        pytest.skip("exfatprogs is required")
    from mountfile.universal_fs_rw import NativeExfatRW

    image = tmp_path / "data.exfat"
    with image.open("wb") as stream:
        stream.truncate(128 * 1024 * 1024)
    subprocess.run(
        ["mkfs.exfat", str(image)], check=True, capture_output=True
    )
    fs = NativeExfatRW(image, offset=0, volume_size=image.stat().st_size)
    try:
        payload = b"exfat-range-payload" * 50
        fs.write_file("/r.bin", payload)
        assert fs.read_range("/r.bin", 6, 5) == payload[6:11]
    finally:
        fs.close()


def test_large_file_offset_writes_stay_consistent(tmp_path):
    """A multi-megabyte file keeps every offset write and read correct."""
    _, fs = _fat(tmp_path)
    try:
        cluster = fs.cluster_size
        size = cluster * 32
        payload = bytes((i * 13) % 251 for i in range(size))
        fs.write_file("/large.bin", payload)

        assert fs.stat("/large.bin").size == size
        assert fs.read_range("/large.bin", 0, 16) == payload[:16]
        assert fs.read_range("/large.bin", size - 16, 16) == payload[-16:]
        middle = cluster * 16 + 3
        assert fs.read_range("/large.bin", middle, 64) == payload[middle : middle + 64]

        for offset in (0, cluster - 1, cluster * 7, size - 4):
            fs.write_range("/large.bin", offset, b"MARK")
            assert fs.read_range("/large.bin", offset, 4) == b"MARK"
            assert fs.stat("/large.bin").size == size

        assert fs.read_range("/large.bin", 0, 4) == b"MARK"
        assert fs.read_range("/large.bin", cluster * 7, 4) == b"MARK"
        assert fs.read_range("/large.bin", size - 4, 4) == b"MARK"
    finally:
        fs.close()
