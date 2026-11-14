"""Real-format and failure regressions for the native filesystem audit."""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess

import pytest

from mountfile.exfat_reader import NativeExfatImage
from mountfile.exfat_writer import ExfatWriteError, NativeExfatWriter
from mountfile.ext2_reader import Ext2ReadError, NativeExt2Image
from mountfile.ext3_reader import NativeExt4Image
from mountfile.fat32 import Fat32, Fat32Error
from mountfile.fat_reader import FatReadError, NativeFatImage
from mountfile.ntfs_reader import NativeNtfsMetadataImage, NtfsMetadataError
from tests.test_fat32 import make_fat32_image
from tests.test_fat_reader import _make_small_fat


def _tool(name):
    executable = shutil.which(name)
    if executable is None:
        pytest.skip(f"independent formatter/checker unavailable: {name}")
    return executable


def _run(*args):
    result = subprocess.run(args, capture_output=True, timeout=60)
    assert result.returncode == 0, (result.stdout + result.stderr).decode(
        errors="replace"
    )
    return result


def _image(path, size):
    with path.open("wb") as stream:
        stream.truncate(size)
    return path


def _legacy_ext2_writer(image, monkeypatch):
    from mountfile.ext_debugfs_writer import Ext234ImageWriter

    _tool("debugfs")
    _run(_tool("mke2fs"), "-F", "-q", "-t", "ext2", str(image))
    monkeypatch.setattr(Ext234ImageWriter, "_try_native_ext2", lambda _self: False)
    return Ext234ImageWriter(image, allow_legacy_debugfs=True)


@pytest.mark.parametrize("fat_type,size", [(12, 1440 * 1024), (16, 32 * 1024**2)])
def test_mkfs_fat12_16_extended_bpb_and_exact_length_lfn(tmp_path, fat_type, size):
    image = _image(tmp_path / "fat.img", size)
    _run(_tool("mkfs.fat"), f"-F{fat_type}", str(image))
    source = tmp_path / "payload.bin"
    payload = bytes(range(256)) * 7
    source.write_bytes(payload)
    name = "abcdefghi.txt"
    assert len(name) == 13
    _run(_tool("mcopy"), "-i", str(image), str(source), f"::{name}")
    _run(_tool("mcopy"), "-i", str(image), str(source), "::hello.txt")
    reader = NativeFatImage(image)
    try:
        assert reader.fat_type == fat_type
        assert reader.read_file("/" + name) == payload
        assert reader.read_file("/HELLO.TXT") == payload
        assert {x["name"] for x in reader.list_dir()} == {name, "hello.txt"}
    finally:
        reader.close()
    _run(_tool("fsck.fat"), "-n", str(image))


@pytest.mark.parametrize("fat_type", [12, 16])
def test_fat_declared_table_capacity_is_checked(tmp_path, fat_type):
    image = tmp_path / "undersized-fat.img"
    _make_small_fat(image, fat_type)
    with image.open("r+b") as stream:
        stream.seek(22)
        stream.write(struct.pack("<H", 1))
    with pytest.raises(FatReadError, match="table is too small"):
        NativeFatImage(image)


def test_fat32_unicode_lfn_spans_noncontiguous_directory_clusters(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    name = "Unicode-\U0001f4be-long-name.txt"
    with image.open("r+b") as stream:
        writer = Fat32(stream)
        for index in range(13):
            writer.write_file("/", f"F{index:02}.TXT", b"")
        writer.write_file("/", "DATA.BIN", b"occupied data cluster")
        writer.write_file("/", name, b"unicode boundary")
        root_chain = writer.cluster_chain(writer.root_cluster)
        assert len(root_chain) > 1
        assert root_chain[1] != root_chain[0] + 1
        assert writer.read_file("/" + name) == b"unicode boundary"
        writer.rename("/" + name, "renamed-\U0001f4be.txt")
        assert writer.read_file("/renamed-\U0001f4be.txt") == b"unicode boundary"
        writer.delete("/renamed-\U0001f4be.txt")
        assert len(writer.list_dir()) == 14
    reader = NativeFatImage(image)
    try:
        assert len(reader.list_dir()) == 14
        assert reader.read_file("/DATA.BIN") == b"occupied data cluster"
    finally:
        reader.close()


@pytest.mark.parametrize("name", ["bad/name", "bad\\name", "a" * 256, "\ud800", "a."])
def test_fat_invalid_names_refused_before_allocation(tmp_path, name):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    before = hashlib.sha256(image.read_bytes()).digest()
    with image.open("r+b") as stream:
        writer = Fat32(stream)
        for operation in (
            lambda: writer.write_file("/", name, b"never publish"),
            lambda: writer.mkdir("/", name),
        ):
            with pytest.raises(Fat32Error):
                operation()
    assert hashlib.sha256(image.read_bytes()).digest() == before


@pytest.mark.parametrize("link", [0, 1, 0x0FFFFFF7, 0x0FFFFFF0])
def test_fat32_malformed_chain_never_returns_truncated_success(tmp_path, link):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    with image.open("r+b") as stream:
        writer = Fat32(stream)
        writer.write_file("/", "LIVE.BIN", b"live" * 300)
        entry = writer.entry("/LIVE.BIN")[1]
        writer._fat_set(entry.cluster, link)
        with pytest.raises(Fat32Error, match="free, bad or reserved"):
            writer.read_file("/LIVE.BIN")


def test_fat32_reserved_bits_and_padding_not_counted_as_free(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    with image.open("r+b") as stream:
        writer = Fat32(stream)
        stream.seek(writer.fat_start + 12)
        stream.write(struct.pack("<I", 0xA0000000))
        assert writer._fat_get(3) == 0
        assert writer.free_space() == writer.free_clusters()[0] * writer.cluster_size
        writer._fat_set(3, 0x0FFFFFF8)
        stream.seek(writer.fat_start + 12)
        assert struct.unpack("<I", stream.read(4))[0] == 0xAFFFFFF8


def test_mkfs_fat32_mirrored_fat_failure_rolls_back(tmp_path, monkeypatch):
    image = _image(tmp_path / "fat32.img", 64 * 1024**2)
    _run(_tool("mkfs.fat"), "-F32", str(image))
    before = hashlib.sha256(image.read_bytes()).digest()
    with image.open("r+b") as stream:
        writer = Fat32(stream)
        assert writer.nfats == 2
        original_write = writer._write
        injected = False

        def fail_second_copy(pos, data):
            nonlocal injected
            if (
                not injected
                and writer.fat_start + writer.fat_size <= pos < writer.data_start
            ):
                injected = True
                raise OSError("injected mirrored FAT failure")
            return original_write(pos, data)

        monkeypatch.setattr(writer, "_write", fail_second_copy)
        with pytest.raises(OSError, match="injected mirrored FAT"):
            writer.write_file("/", "FAIL.TXT", b"preserve image")
    assert hashlib.sha256(image.read_bytes()).digest() == before
    _run(_tool("fsck.fat"), "-n", str(image))


def _exfat(tmp_path, size=16 * 1024**2):
    image = _image(tmp_path / "exfat.img", size)
    _run(_tool("mkfs.exfat"), str(image))
    return image, NativeExfatWriter(image, volume_size=size)


@pytest.mark.parametrize("move", [False, True])
def test_exfat_rename_shorter_name_rebuilds_secondary_count(tmp_path, move):
    image, writer = _exfat(tmp_path)
    writer.write_file("/a-long-name-over-fifteen.txt", b"retained data")
    if move:
        writer.mkdir("/DIR")
    target = "/DIR/a.txt" if move else "/a.txt"
    writer.rename("/a-long-name-over-fifteen.txt", target)
    reader = NativeExfatImage(image, volume_size=image.stat().st_size)
    try:
        assert reader.read_file(target) == b"retained data"
        assert reader._find(target).name == "a.txt"
    finally:
        reader.close()
    _run(_tool("fsck.exfat"), "-n", str(image))


def test_exfat_case_insensitive_delete_preserves_unrelated_files(tmp_path):
    image, writer = _exfat(tmp_path)
    writer.mkdir("/Directory")
    writer.write_file("/Directory/Report.txt", b"remove")
    writer.write_file("/Keep.txt", b"keep")
    writer.delete("/DIRECTORY", recursive=True)
    reader = NativeExfatImage(image, volume_size=image.stat().st_size)
    try:
        assert reader.read_file("/KEEP.TXT") == b"keep"
        assert [x["name"] for x in reader.list_dir()] == ["Keep.txt"]
    finally:
        reader.close()
    _run(_tool("fsck.exfat"), "-n", str(image))


def test_exfat_whole_file_larger_than_range_limit(tmp_path):
    image, writer = _exfat(tmp_path, 32 * 1024**2)
    payload = bytes(range(256)) * (65536 + 1)
    writer.write_file("/large.bin", payload)
    reader = NativeExfatImage(image, volume_size=image.stat().st_size)
    try:
        assert reader.read_file("/LARGE.BIN") == payload
        assert (
            reader.read_file_range("/large.bin", 16 * 1024**2 - 10, 32)
            == payload[-266:-234]
        )
    finally:
        reader.close()
    _run(_tool("fsck.exfat"), "-n", str(image))


def test_real_ntfs_cluster_vcns_and_mft_padding(tmp_path):
    image = _image(tmp_path / "ntfs.img", 32 * 1024**2)
    _run(_tool("mkfs.ntfs"), "-F", "-Q", "-c512", str(image))
    source = tmp_path / "source.bin"
    payload = bytes(range(256)) * 29
    source.write_bytes(payload)
    for index in range(48):
        _run(_tool("ntfscp"), "-q", str(image), str(source), f"/FILE-{index:02}.BIN")
    before = hashlib.sha256(image.read_bytes()).digest()
    reader = NativeNtfsMetadataImage(image, volume_size=image.stat().st_size)
    try:
        entries = reader.list_dir_by_record(5, reader.read_mft_record(5))
        assert "." not in {x.name for x in entries}
        assert next(x for x in entries if x.name == "$Extend").is_directory
        assert reader.read_file("/file-47.bin") == payload
        assert reader.read_file_range("/FILE-47.BIN", 509, 1111) == payload[509:1620]
        # System metadata is visible, but the sparse stream stays unreadable.
        badclus = next(
            item for item in reader.list_dir("/") if item["name"] == "$BadClus"
        )
        assert badclus["content_supported"] is False
        assert badclus["unsupported_streams"] == ["$Bad"]
        assert reader.stat("/$BadClus")["content_supported"] is False
        with pytest.raises(NtfsMetadataError, match="sparse"):
            reader.read_file("/$BadClus")
    finally:
        reader.close()
    assert hashlib.sha256(image.read_bytes()).digest() == before


@pytest.mark.parametrize(
    "seconds,extra,expected",
    [
        (1_700_000_000, 123_456_789 << 2, "2023-11-14T22:13:20.123456789+00:00"),
        (0xFFFFFFFF, 0, "1969-12-31T23:59:59+00:00"),
        (0xFFFFFFFF, 1, "2106-02-07T06:28:15+00:00"),
    ],
)
def test_ext4_timestamp_low_epoch_bits_and_signed_seconds(seconds, extra, expected):
    assert NativeExt2Image._format_ext_timestamp(seconds, extra) == expected
    with pytest.raises(Ext2ReadError, match="nanoseconds"):
        NativeExt2Image._format_ext_timestamp(seconds, 1_000_000_000 << 2)


def test_real_ext4_timestamp_matches_debugfs_encoding(tmp_path):
    image = _image(tmp_path / "ext4.img", 32 * 1024**2)
    _run(_tool("mkfs.ext4"), "-F", "-q", "-O", "^fast_commit,^orphan_file", str(image))
    source = tmp_path / "source.txt"
    source.write_bytes(b"independent ext4 timestamp")
    _run(_tool("debugfs"), "-w", "-R", f"write {source} /stamp.txt", str(image))
    _run(
        _tool("debugfs"),
        "-w",
        "-R",
        "set_inode_field /stamp.txt mtime 1700000000",
        str(image),
    )
    _run(
        _tool("debugfs"),
        "-w",
        "-R",
        f"set_inode_field /stamp.txt mtime_extra {123456789 << 2}",
        str(image),
    )
    reader = NativeExt4Image(image, volume_size=image.stat().st_size)
    try:
        assert reader.read_file("/stamp.txt") == source.read_bytes()
        assert (
            reader.stat("/stamp.txt")["modified"]
            == "2023-11-14T22:13:20.123456789+00:00"
        )
    finally:
        reader.close()
    _run(_tool("e2fsck"), "-fn", str(image))


def test_fat32_cross_linked_files_refused_without_mutation(tmp_path):
    image = tmp_path / "crosslinked.img"
    make_fat32_image(image)
    with image.open("r+b") as stream:
        writer = Fat32(stream)
        writer.write_file("/", "ONE.BIN", b"first")
        writer.write_file("/", "TWO.BIN", b"second")
        first = writer.entry("/ONE.BIN")[1]
        second = writer.entry("/TWO.BIN")[1]
        stream.seek(second.entry_offset + 20)
        stream.write(struct.pack("<H", first.cluster >> 16))
        stream.seek(second.entry_offset + 26)
        stream.write(struct.pack("<H", first.cluster & 0xFFFF))
        stream.flush()
        before = hashlib.sha256(image.read_bytes()).digest()
        for operation in (
            lambda: writer.delete("/ONE.BIN"),
            lambda: writer.write_file("/", "ONE.BIN", b"replacement"),
            lambda: writer.mkdir("/", "NEW"),
        ):
            with pytest.raises(Fat32Error, match="cross-linked"):
                operation()
        assert hashlib.sha256(image.read_bytes()).digest() == before


def test_fat32_short_alias_collision_preserves_requested_long_name(tmp_path):
    image = tmp_path / "alias.img"
    make_fat32_image(image)
    with image.open("r+b") as stream:
        writer = Fat32(stream)
        writer.write_file("/", "A very long name.txt", b"first")
        alias = writer._short_to_name(
            writer.entry("/A very long name.txt")[1].short_raw
        )
        writer.write_file("/", alias, b"second")
        assert writer.read_file("/" + alias) == b"second"
        assert writer.read_file("/A very long name.txt") == b"first"
    reader = NativeFatImage(image)
    try:
        assert reader.read_file("/" + alias) == b"second"
    finally:
        reader.close()


def test_exfat_contiguous_directory_read_and_write_refusal(tmp_path):
    image, writer = _exfat(tmp_path)
    writer.mkdir("/Directory")
    writer.write_file("/Directory/child.txt", b"contiguous directory child")
    reader = NativeExfatImage(image, volume_size=image.stat().st_size)
    try:
        directory = reader._find("/Directory")
        parent_cluster, record = writer._locate(reader, "/Directory")
        position = writer._entry_offset(reader, parent_cluster, record[1])
        with image.open("r+b") as stream:
            entry_set = bytearray(record[4])
            entry_set[33] |= 0x02
            struct.pack_into("<H", entry_set, 2, writer._entry_checksum(entry_set))
            writer._write_rel(stream, position, entry_set)
            writer._fat_write(stream, reader, directory.first_cluster, 0)
    finally:
        reader.close()
    reader = NativeExfatImage(image, volume_size=image.stat().st_size)
    try:
        assert reader.read_file("/directory/child.txt") == b"contiguous directory child"
        assert [x["name"] for x in reader.list_dir("/Directory")] == ["child.txt"]
    finally:
        reader.close()
    writer = NativeExfatWriter(image, volume_size=image.stat().st_size)
    before = hashlib.sha256(image.read_bytes()).digest()
    with pytest.raises(ExfatWriteError, match="contiguous.*bounded profile"):
        writer.delete("/Directory", recursive=True)
    assert hashlib.sha256(image.read_bytes()).digest() == before


def test_legacy_debugfs_regular_file_rename_is_real_and_consistent(
    tmp_path, monkeypatch
):
    image = _image(tmp_path / "ext2.img", 16 * 1024**2)
    writer = _legacy_ext2_writer(image, monkeypatch)
    writer.mkdir("/DIR")
    writer.write_file("/old.txt", b"preserve inode and data")
    writer.rename("/old.txt", "/DIR/new.txt")
    assert writer.read_file("/DIR/new.txt") == b"preserve inode and data"
    assert "old.txt" not in {x["name"] for x in writer.list_dir("/")}
    _run(_tool("e2fsck"), "-fn", str(image))


@pytest.mark.parametrize(
    "operation", ["mkdir-missing-parent", "rename-directory", "delete-missing"]
)
def test_legacy_debugfs_refusals_never_become_dummy_success(
    tmp_path, monkeypatch, operation
):
    from mountfile.ext_debugfs_writer import ExtWriteError

    image = _image(tmp_path / "ext2.img", 16 * 1024**2)
    writer = _legacy_ext2_writer(image, monkeypatch)
    writer.mkdir("/DIR")
    before = hashlib.sha256(image.read_bytes()).digest()
    with pytest.raises((ExtWriteError, FileNotFoundError)):
        if operation == "mkdir-missing-parent":
            writer.mkdir("/absent/child")
        elif operation == "rename-directory":
            writer.rename("/DIR", "/OTHER")
        else:
            writer.delete("/absent")
    assert hashlib.sha256(image.read_bytes()).digest() == before


def test_legacy_debugfs_concurrent_change_is_not_overwritten(tmp_path, monkeypatch):
    from mountfile.ext_debugfs_writer import ExtWriteError

    image = _image(tmp_path / "ext2.img", 16 * 1024**2)
    writer = _legacy_ext2_writer(image, monkeypatch)
    original_run = writer._run
    injected = False

    def concurrent_run(*args, **kwargs):
        nonlocal injected
        result = original_run(*args, **kwargs)
        if kwargs.get("write") and not injected:
            injected = True
            with image.open("r+b") as stream:
                stream.seek(7)
                stream.write(b"external-change")
        return result

    monkeypatch.setattr(writer, "_run", concurrent_run)
    with pytest.raises(ExtWriteError, match="changed concurrently"):
        writer.mkdir("/NEW")
    with image.open("rb") as stream:
        stream.seek(7)
        assert stream.read(len(b"external-change")) == b"external-change"
    assert "NEW" not in {x["name"] for x in writer.list_dir("/")}
