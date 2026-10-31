# SPDX-License-Identifier: MIT
"""Synthetic tests for the bounded exFAT boot-region probe."""

import os
import struct
from types import SimpleNamespace

import pytest

from mountfile import vfs
from mountfile.exfat_probe import (
    ExfatProbeError,
    exfat_boot_checksum,
    parse_exfat_boot_region,
)
from mountfile.exfat_reader import ExfatReadError, NativeExfatImage
from mountfile.exfat_writer import ExfatWriteError, NativeExfatWriter
from mountfile.extract import extract_file
from mountfile.safety import RefusalCode
from mountfile.universal_fs_rw import NativeExfatRW

VOLUME_SIZE = 2 * 1024 * 1024


def _entry_set_checksum(entry_set):
    checksum = 0
    for index, value in enumerate(entry_set):
        if index in (2, 3):
            value = 0
        checksum = ((checksum << 15) | (checksum >> 1)) & 0xFFFF
        checksum = (checksum + value) & 0xFFFF
    return checksum


def _name_hash(name):
    checksum = 0
    for value in name.encode("utf-16le"):
        checksum = ((checksum << 15) | (checksum >> 1)) & 0xFFFF
        checksum = (checksum + value) & 0xFFFF
    return checksum


def _compressed_upcase_table():
    # Identity mappings 0..0x60, ASCII a-z -> A-Z, identity through 0xE3,
    # ä -> Ä, then identity through U+FFFF.
    units = [0xFFFF, 97]
    units.extend(range(ord("A"), ord("Z") + 1))
    units.extend((0xFFFF, 105))
    units.append(0x00C4)
    units.extend((0xFFFF, 0x10000 - 0x00E5))
    return struct.pack(f"<{len(units)}H", *units)


def _upcase_checksum(data):
    checksum = 0
    for value in data:
        checksum = ((checksum >> 1) | ((checksum & 1) << 31)) & 0xFFFFFFFF
        checksum = (checksum + value) & 0xFFFFFFFF
    return checksum


def _region(sector_size=512):
    region = bytearray(24 * sector_size)
    boot = memoryview(region)[:sector_size]
    boot[3:11] = b"EXFAT   "
    struct.pack_into("<Q", boot, 0x40, 0)
    struct.pack_into("<Q", boot, 0x48, VOLUME_SIZE // sector_size)
    struct.pack_into("<I", boot, 0x50, 24)
    struct.pack_into("<I", boot, 0x54, 8)
    struct.pack_into("<I", boot, 0x58, 32)
    struct.pack_into("<I", boot, 0x5C, 100)
    struct.pack_into("<I", boot, 0x60, 2)
    struct.pack_into("<I", boot, 0x64, 0x12345678)
    struct.pack_into("<H", boot, 0x6A, 0)
    boot[0x6C] = {512: 9, 1024: 10, 2048: 11, 4096: 12}[sector_size]
    boot[0x6D] = 0
    boot[0x6E] = 1
    boot[0x70] = 10
    boot[510:512] = b"\x55\xaa"
    region[12 * sector_size : 23 * sector_size] = region[: 11 * sector_size]
    for start in (0, 12 * sector_size):
        checksum = exfat_boot_checksum(
            bytes(region[start : start + 11 * sector_size]),
            sector_size=sector_size,
        )
        for offset in range(0, sector_size, 4):
            struct.pack_into("<I", region, start + 11 * sector_size + offset, checksum)
    return bytes(region)


def _image_with_volume_flags(path, flags):
    region = bytearray(_region())
    struct.pack_into("<H", region, 0x6A, flags)
    checksum = exfat_boot_checksum(bytes(region[: 11 * 512]), sector_size=512)
    for offset in range(0, 512, 4):
        struct.pack_into("<I", region, 11 * 512 + offset, checksum)
    path.write_bytes(region + bytes(VOLUME_SIZE - len(region)))


def test_parses_exfat_geometry_and_checksum():
    profile = parse_exfat_boot_region(_region(), volume_size=VOLUME_SIZE)
    assert profile.bytes_per_sector == 512
    assert profile.cluster_size == 512
    assert profile.fat_offset == 24
    assert profile.cluster_heap_offset == 32
    assert profile.volume_length == VOLUME_SIZE // 512
    assert profile.number_of_fats == 1
    assert profile.root_cluster == 2
    assert profile.percent_in_use == 10


@pytest.mark.parametrize("sector_size", [1024, 2048, 4096])
def test_parses_larger_exfat_sector_sizes(sector_size):
    profile = parse_exfat_boot_region(_region(sector_size), volume_size=VOLUME_SIZE)
    assert profile.bytes_per_sector == sector_size


def test_vfs_probe_uses_partition_offset(tmp_path):
    prefix = b"P" * 73
    path = tmp_path / "exfat.img"
    path.write_bytes(prefix + _region() + bytes(VOLUME_SIZE - len(_region())))
    profile = vfs.probe_exfat_profile(path, offset=len(prefix), volume_size=VOLUME_SIZE)
    assert profile.partition_offset == len(prefix)


def test_vfs_opens_validated_exfat_with_native_provider(tmp_path):
    path = tmp_path / "exfat.img"
    _tree_image(path)
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    assert provider.backend_name == "native-exfat-ro"
    provider.close()


def test_native_exfat_reads_a_bounded_file_entry_set(tmp_path):
    path = tmp_path / "exfat-tree.img"
    payload = _tree_image(path)
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.list_dir("/")[0]["name"] == "HELLO.TXT"
        assert provider.file_size("/HELLO.TXT") == len(payload)
        assert provider.read_file_range("/HELLO.TXT", 2, 5) == payload[2:7]
    finally:
        provider.close()


def test_native_exfat_uses_upcase_table_for_name_hash_and_path_lookup(tmp_path):
    path = tmp_path / "exfat-upcase-name.img"
    payload = _tree_image(path)
    raw = bytearray(path.read_bytes())
    root = 32 * 512
    # The table leaves sharp-s as one UTF-16 code unit. Python casefold()
    # expands it to "ss", which is not exFAT's table-based name matching.
    name = "ß.txt"
    raw[root + 99] = len(name)
    struct.pack_into("<H", raw, root + 100, _name_hash("ß.TXT"))
    raw[root + 130 : root + 160] = name.encode("utf-16le").ljust(30, b"\0")
    struct.pack_into(
        "<H", raw, root + 66, _entry_set_checksum(raw[root + 64 : root + 160])
    )
    path.write_bytes(raw)

    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.read_file("/ß.TXT") == payload
        with pytest.raises(FileNotFoundError):
            provider.file_size("/SS.TXT")
    finally:
        provider.close()


def test_native_exfat_reads_nofatchain_when_fat_entry_is_invalid(tmp_path):
    path = tmp_path / "exfat-nofat-chain.img"
    payload = _tree_image(path)
    raw = bytearray(path.read_bytes())
    root = 32 * 512
    raw[root + 97] = 0x02
    struct.pack_into(
        "<H", raw, root + 66, _entry_set_checksum(raw[root + 64 : root + 160])
    )
    struct.pack_into("<I", raw, 24 * 512 + 3 * 4, 0)
    path.write_bytes(raw)

    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.read_file("/HELLO.TXT") == payload
    finally:
        provider.close()


def test_native_exfat_writer_uses_volume_upcase_table_for_new_names(tmp_path):
    path = tmp_path / "exfat-writer-upcase.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.write_file("/ß.txt", b"unicode payload")
    writer.mkdir("/ä-dir")

    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.read_file("/ß.TXT") == b"unicode payload"
        assert provider.list_dir("/Ä-DIR") == []
        with pytest.raises(FileNotFoundError):
            provider.file_size("/SS.TXT")
    finally:
        provider.close()


def test_native_exfat_rejects_file_cluster_marked_free(tmp_path):
    path = tmp_path / "exfat-free-file-cluster.img"
    _tree_image(path)
    raw = bytearray(path.read_bytes())
    raw[34 * 512] &= ~0x02  # cluster 3 is allocation bitmap bit 1
    path.write_bytes(raw)

    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        with pytest.raises(ExfatReadError, match="cluster marked free"):
            provider.read_file("/HELLO.TXT")
    finally:
        provider.close()


def test_exfat_upcase_table_accepts_compressed_and_uncompressed_forms():
    table = NativeExfatImage._parse_upcase_table(_compressed_upcase_table())
    assert table[ord("a")] == ord("A")
    assert table[ord("ä")] == ord("Ä")
    assert table[ord("ß")] == ord("ß")

    uncompressed = list(range(0x10000))
    uncompressed[ord("a") : ord("z") + 1] = range(ord("A"), ord("Z") + 1)
    raw = struct.pack("<65536H", *uncompressed)
    assert NativeExfatImage._parse_upcase_table(raw) == tuple(uncompressed)


@pytest.mark.parametrize("raw", [b"\xff\xff", b"\xff\xff\x00\x00", b"\x41\x00"])
def test_exfat_upcase_table_rejects_truncated_or_incomplete_data(raw):
    with pytest.raises(ExfatReadError, match="up-case"):
        NativeExfatImage._parse_upcase_table(raw)


def test_native_exfat_traverses_nested_paths_and_case_insensitive_stat(tmp_path):
    path = tmp_path / "exfat-nested-read.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.mkdir("/Nested")
    writer.write_file("/Nested/Report.TXT", b"nested report")
    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.stat("/nested/report.txt")["size"] == len(b"nested report")
        assert provider.read_file_range("/NESTED/report.txt", 7, 6) == b"report"
        assert provider.read_file("/NESTED/report.txt") == b"nested report"
        assert provider.list_dir("/nested") == [
            {"name": "Report.TXT", "is_dir": False, "size": 13, "date": ""}
        ]
    finally:
        provider.close()


def test_native_exfat_rejects_reserved_file_attributes(tmp_path):
    path = tmp_path / "exfat-reserved-attributes.img"
    _tree_image(path)
    raw = bytearray(path.read_bytes())
    entry_start = 32 * 512 + 64
    struct.pack_into("<H", raw, entry_start + 4, 0x0040)
    entry_set = bytearray(raw[entry_start : entry_start + 96])
    struct.pack_into("<H", entry_set, 2, _entry_set_checksum(entry_set))
    raw[entry_start : entry_start + 96] = entry_set
    path.write_bytes(raw)
    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        with pytest.raises(ExfatReadError, match="reserved attributes"):
            provider.list_dir("/")
    finally:
        provider.close()


def test_exfat_range_read_fetches_only_clusters_and_bytes_requested():
    reader = NativeExfatImage.__new__(NativeExfatImage)
    reader.profile = SimpleNamespace(cluster_size=512, cluster_count=10)
    entry = SimpleNamespace(
        is_dir=False,
        size=4096,
        first_cluster=2,
        no_fat_chain=False,
        valid_data_length=4096,
    )
    reader._find = lambda _path: entry
    yielded = []

    def chain(*_args, **_kwargs):
        for cluster in (2, 3, 4, 5):
            yielded.append(cluster)
            yield cluster

    reader._iter_chain = chain
    reader._cluster_offset = lambda cluster: cluster * 512
    reads = []

    def read_relative(relative, size):
        reads.append((relative, size))
        return bytes([relative // 512]) * size

    reader._read_rel = read_relative

    assert reader.read_file_range("/FILE", 500, 40) == b"\x02" * 12 + b"\x03" * 28
    assert yielded == [2, 3]
    assert reads == [(2 * 512 + 500, 12), (3 * 512, 28)]


def test_exfat_range_read_rejects_chain_shorter_than_requested_range():
    reader = NativeExfatImage.__new__(NativeExfatImage)
    reader.profile = SimpleNamespace(cluster_size=512, cluster_count=10)
    entry = SimpleNamespace(
        is_dir=False,
        size=4096,
        first_cluster=2,
        no_fat_chain=False,
        valid_data_length=4096,
    )
    reader._find = lambda _path: entry
    reader._iter_chain = lambda *_args, **_kwargs: iter((2, 3))
    reader._cluster_offset = lambda cluster: cluster * 512
    reader._read_rel = lambda _relative, size: bytes(size)

    with pytest.raises(ExfatReadError, match="shorter than requested range"):
        reader.read_file_range("/FILE", 1024, 512)


def test_native_exfat_reader_rejects_unvalidated_multiple_fats(tmp_path):
    region = bytearray(_region())
    for start in (0, 12 * 512):
        region[start + 0x6E] = 2
        struct.pack_into("<I", region, start + 0x58, 40)
        checksum = exfat_boot_checksum(
            bytes(region[start : start + 11 * 512]), sector_size=512
        )
        for offset in range(0, 512, 4):
            struct.pack_into("<I", region, start + 11 * 512 + offset, checksum)
    path = tmp_path / "exfat-two-fats.img"
    path.write_bytes(region + bytes(VOLUME_SIZE - len(region)))

    with pytest.raises(ExfatReadError, match="only single-FAT"):
        NativeExfatImage(path, volume_size=VOLUME_SIZE)


@pytest.mark.parametrize("flags", [0x0001, 0x0002, 0x0004, 0x0008, 0x8000])
def test_native_exfat_reader_rejects_nonzero_volume_flags(tmp_path, flags):
    path = tmp_path / f"exfat-flags-{flags:04x}.img"
    _image_with_volume_flags(path, flags)

    with pytest.raises(ExfatReadError, match="nonzero volume flags"):
        NativeExfatImage(path, volume_size=VOLUME_SIZE)


@pytest.mark.parametrize("flags", [0x0001, 0x0002, 0x0004, 0x0008, 0x8000])
def test_native_exfat_writer_rejects_nonzero_volume_flags_without_mutation(
    tmp_path, flags
):
    path = tmp_path / f"exfat-writer-flags-{flags:04x}.img"
    _image_with_volume_flags(path, flags)
    before = path.read_bytes()

    with pytest.raises(ExfatWriteError, match="nonzero volume flags"):
        NativeExfatWriter(path, volume_size=VOLUME_SIZE)

    assert path.read_bytes() == before


@pytest.mark.parametrize("operation", ["read", "write"])
def test_native_exfat_writer_range_refusal_preserves_image(tmp_path, operation):
    path = tmp_path / f"exfat-bounds-{operation}.img"
    _image_with_volume_flags(path, 0)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    before = path.read_bytes()

    with path.open("r+b") as stream:
        with pytest.raises(ExfatWriteError) as caught:
            if operation == "read":
                writer._read_rel(stream, writer.extent_size - 1, 2)
            else:
                writer._write_rel(stream, writer.extent_size - 1, b"xy")

    assert caught.value.code is RefusalCode.RANGE_OUT_OF_BOUNDS
    assert path.read_bytes() == before


@pytest.mark.parametrize("directory_reader", ["_directory", "system_metadata"])
def test_directory_limit_is_checked_before_reading_next_cluster(
    monkeypatch, directory_reader
):
    import mountfile.exfat_reader as exfat_reader

    monkeypatch.setattr(exfat_reader, "_MAX_DIR_BYTES", 512)
    reader = NativeExfatImage.__new__(NativeExfatImage)
    reader.profile = SimpleNamespace(cluster_size=512, root_cluster=2)
    reader._iter_chain = lambda *_args, **_kwargs: iter((2, 3))
    reader._cluster_offset = lambda cluster: cluster * 512
    reads = []

    def read_relative(relative, size):
        reads.append((relative, size))
        return bytes(size)

    reader._read_rel = read_relative
    with pytest.raises(ExfatReadError, match="directory exceeds reader limit"):
        if directory_reader == "_directory":
            reader._directory(2)
        else:
            reader.system_metadata()

    assert reads == [(1024, 512)]


def test_exfat_writer_directory_records_use_bounded_reader_limit(monkeypatch):
    import mountfile.exfat_reader as exfat_reader

    monkeypatch.setattr(exfat_reader, "_MAX_DIR_BYTES", 512)
    reader = NativeExfatImage.__new__(NativeExfatImage)
    reader.profile = SimpleNamespace(cluster_size=512)
    reader._iter_chain = lambda *_args, **_kwargs: iter((2, 3))
    reader._cluster_offset = lambda cluster: cluster * 512
    reads = []

    def read_relative(relative, size):
        reads.append((relative, size))
        return bytes(size)

    reader._read_rel = read_relative
    writer = NativeExfatWriter.__new__(NativeExfatWriter)
    with pytest.raises(ExfatReadError, match="directory exceeds reader limit"):
        writer._directory_records(reader, 2)

    assert reads == [(1024, 512)]


def _tree_image(path, *, valid_data_length=None):
    region = bytearray(VOLUME_SIZE)
    region[: 24 * 512] = _region()
    struct.pack_into("<I", region, 24 * 512 + 2 * 4, 0xFFFFFFFF)
    struct.pack_into("<I", region, 24 * 512 + 3 * 4, 0xFFFFFFFF)
    struct.pack_into("<I", region, 24 * 512 + 4 * 4, 0xFFFFFFFF)
    struct.pack_into("<I", region, 24 * 512 + 5 * 4, 0xFFFFFFFF)
    root = bytearray(512)
    root[0] = 0x81
    struct.pack_into("<I", root, 20, 4)
    struct.pack_into("<Q", root, 24, 512)
    upcase_data = _compressed_upcase_table()
    root[32] = 0x82
    struct.pack_into("<I", root, 36, _upcase_checksum(upcase_data))
    struct.pack_into("<I", root, 52, 5)
    struct.pack_into("<Q", root, 56, len(upcase_data))
    root[64] = 0x85
    root[65] = 2
    payload = b"hello exFAT"
    root[96] = 0xC0
    root[99] = 9
    struct.pack_into("<H", root, 100, _name_hash("HELLO.TXT"))
    if valid_data_length is None:
        valid_data_length = len(payload)
    struct.pack_into("<Q", root, 104, valid_data_length)
    struct.pack_into("<I", root, 116, 3)
    struct.pack_into("<Q", root, 120, len(payload))
    root[128] = 0xC1
    root[130:148] = "HELLO.TXT".encode("utf-16le")
    struct.pack_into("<H", root, 66, _entry_set_checksum(root[64:160]))
    region[32 * 512 : 33 * 512] = root
    region[33 * 512 : 33 * 512 + len(payload)] = payload
    region[34 * 512 : 35 * 512] = b"\x0f" + bytes(511)
    region[35 * 512 : 35 * 512 + len(upcase_data)] = upcase_data
    path.write_bytes(region)
    return payload


@pytest.mark.parametrize("valid_data_length", [0, 5, 11])
def test_native_exfat_zero_fills_invalid_file_tail_on_read_and_extract(
    tmp_path, valid_data_length
):
    path = tmp_path / f"exfat-vdl-{valid_data_length}.img"
    payload = _tree_image(path, valid_data_length=valid_data_length)
    expected = payload[:valid_data_length] + bytes(len(payload) - valid_data_length)
    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.file_size("/HELLO.TXT") == len(payload)
        assert provider.read_file("/HELLO.TXT") == expected
        destination = tmp_path / f"extracted-{valid_data_length}.bin"
        assert extract_file(provider, "/HELLO.TXT", destination) == len(payload)
        assert destination.read_bytes() == expected
    finally:
        provider.close()


def test_native_exfat_range_crossing_vdl_returns_zeroes_and_respects_eof(tmp_path):
    path = tmp_path / "exfat-vdl-range.img"
    _tree_image(path, valid_data_length=5)
    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.read_file_range("/HELLO.TXT", 3, 5) == b"lo" + bytes(3)
        assert provider.read_file_range("/HELLO.TXT", 5, 3) == bytes(3)
        assert provider.read_file_range("/HELLO.TXT", 10, 8) == bytes(1)
        assert provider.read_file_range("/HELLO.TXT", 11, 1) == b""
    finally:
        provider.close()


def test_native_exfat_rejects_valid_data_length_larger_than_data_length(tmp_path):
    path = tmp_path / "exfat-vdl-too-large.img"
    _tree_image(path, valid_data_length=12)
    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        with pytest.raises(ExfatReadError, match="ValidDataLength exceeds DataLength"):
            provider.list_dir("/")
    finally:
        provider.close()


def test_native_exfat_rejects_directory_vdl_different_from_data_length(tmp_path):
    path = tmp_path / "exfat-directory-vdl-mismatch.img"
    _tree_image(path, valid_data_length=5)
    raw = bytearray(path.read_bytes())
    entry_start = 32 * 512 + 64
    struct.pack_into("<H", raw, entry_start + 4, 0x10)
    entry_set = bytearray(raw[entry_start : entry_start + 96])
    struct.pack_into("<H", entry_set, 2, _entry_set_checksum(entry_set))
    raw[entry_start : entry_start + 96] = entry_set
    path.write_bytes(raw)

    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        with pytest.raises(
            ExfatReadError,
            match="directory ValidDataLength differs from DataLength",
        ):
            provider.list_dir("/")
    finally:
        provider.close()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        ("missing_bitmap", "allocation bitmap entry is missing"),
        ("missing_upcase", "upcase table entry is missing"),
        ("bitmap_system_cluster_free", "cluster marked free"),
        ("bitmap_flags", "allocation bitmap geometry is invalid"),
        ("duplicate_bitmap", "allocation bitmap entry is duplicated"),
        ("upcase_checksum", "upcase table checksum mismatch"),
    ],
)
def test_native_exfat_reader_requires_valid_system_metadata(tmp_path, mutate, message):
    path = tmp_path / f"exfat-metadata-{mutate}.img"
    _tree_image(path)
    raw = bytearray(path.read_bytes())
    root = 32 * 512
    if mutate == "missing_bitmap":
        raw[root] = 0
    elif mutate == "missing_upcase":
        raw[root + 32] = 0
    elif mutate == "bitmap_system_cluster_free":
        raw[34 * 512] ^= 0x04
    elif mutate == "bitmap_flags":
        raw[root + 1] = 1
    elif mutate == "duplicate_bitmap":
        raw[root + 160 : root + 192] = raw[root : root + 32]
    else:
        raw[35 * 512] = 1
    path.write_bytes(raw)

    with pytest.raises(ExfatReadError, match=message):
        NativeExfatImage(path, volume_size=VOLUME_SIZE)


def test_native_exfat_writer_replaces_and_renames_with_staging(tmp_path):
    path = tmp_path / "exfat-writer.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.write_file("/HELLO.TXT", b"updated")
    with pytest.raises(ExfatWriteError, match="root directory"):
        writer.rename("/HELLO.TXT", "/")
    writer.rename("/HELLO.TXT", "/RENAMED.TXT")
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.read_file("/RENAMED.TXT") == b"updated"
    finally:
        provider.close()


def test_native_exfat_writer_rejects_symlink_target_without_mutation(tmp_path):
    target = tmp_path / "exfat-target.img"
    alias = tmp_path / "exfat-symlink.img"
    _tree_image(target)
    original = target.read_bytes()
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ExfatWriteError, match="must not be a symlink"):
        NativeExfatWriter(alias, volume_size=VOLUME_SIZE)

    assert alias.is_symlink()
    assert target.read_bytes()[:VOLUME_SIZE] == original


def test_native_exfat_writer_rejects_hardlinked_target_without_mutation(tmp_path):
    target = tmp_path / "exfat-target.img"
    alias = tmp_path / "exfat-hardlink.img"
    _tree_image(target)
    original = target.read_bytes()
    try:
        os.link(target, alias)
    except OSError as exc:
        pytest.skip(f"hard link creation is unavailable: {exc}")

    with pytest.raises(ExfatWriteError, match="multiple hard links"):
        NativeExfatWriter(target, volume_size=VOLUME_SIZE)

    assert target.read_bytes()[:VOLUME_SIZE] == original
    assert alias.read_bytes()[:VOLUME_SIZE] == original


def test_exfat_writer_refuses_to_overwrite_concurrent_outside_extent_change(
    tmp_path, monkeypatch
):
    path = tmp_path / "exfat-outside-change.img"
    original_payload = _tree_image(path)
    original_suffix = b"outside-image-sentinel-001"
    changed_suffix = b"outside-image-sentinel-002"
    with path.open("ab") as stream:
        stream.write(original_suffix)

    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    copy_stage = writer._copy_stage

    def copy_then_change_outside_extent():
        stage = copy_stage()
        with path.open("r+b") as stream:
            stream.seek(VOLUME_SIZE)
            stream.write(changed_suffix)
            stream.flush()
        return stage

    monkeypatch.setattr(writer, "_copy_stage", copy_then_change_outside_extent)
    with pytest.raises(ExfatWriteError, match="changed concurrently"):
        writer.write_file("/HELLO.TXT", b"must not commit")

    assert path.read_bytes()[VOLUME_SIZE:] == changed_suffix
    provider = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.read_file("/HELLO.TXT") == original_payload
    finally:
        provider.close()


def test_native_exfat_move_grows_destination_directory(tmp_path):
    path = tmp_path / "exfat-cross-directory-move.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.mkdir("/SRC")
    writer.mkdir("/DST")
    for index in range(5):
        writer.write_file(f"/DST/{index}.TXT", b"")
    writer.write_file("/SRC/FILE.TXT", b"moved file contents")
    writer.rename("/SRC/FILE.TXT", "/DST/RENAMED.TXT")

    reader = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        destination = reader._find("/DST")
        assert destination.size == 2 * reader.profile.cluster_size
        assert len(reader._chain(destination.first_cluster)) == 2
        reader.system_metadata()
        assert reader.read_file("/DST/RENAMED.TXT") == b"moved file contents"
        with pytest.raises(FileNotFoundError):
            reader._find("/SRC/FILE.TXT")
    finally:
        reader.close()


def test_native_exfat_moves_directories_and_rejects_cycles(tmp_path):
    path = tmp_path / "exfat-directory-move.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.mkdir("/PARENT")
    writer.mkdir("/PARENT/CHILD")
    writer.write_file("/PARENT/CHILD/ITEM.TXT", b"nested contents")
    writer.mkdir("/OUT")
    with pytest.raises(ExfatWriteError, match="into itself"):
        writer.rename("/PARENT", "/PARENT/CHILD/CYCLE")
    writer.rename("/PARENT/CHILD", "/OUT/MOVED")

    reader = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        reader.system_metadata()
        assert reader._find("/OUT/MOVED").is_dir
        assert reader.read_file("/OUT/MOVED/ITEM.TXT") == b"nested contents"
        with pytest.raises(FileNotFoundError):
            reader._find("/PARENT/CHILD")
    finally:
        reader.close()


def test_native_exfat_writer_deletes_nonempty_files(tmp_path):
    path = tmp_path / "exfat-writer-limits.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.write_file("/HELLO.TXT", b"x" * 513)
    writer.write_file("/HELLO.TXT", b"shrunk")
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    try:
        assert provider.read_file("/HELLO.TXT") == b"shrunk"
    finally:
        provider.close()
    writer.delete("/HELLO.TXT")
    reader = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        reader.system_metadata()
        assert reader.list_dir("/") == []
    finally:
        reader.close()


def test_native_exfat_writer_recursively_deletes_directory_tree(tmp_path):
    path = tmp_path / "exfat-recursive-delete.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.mkdir("/DIR")
    writer.write_file("/DIR/FILE.TXT", b"x" * 700)
    writer.mkdir("/DIR/SUB")
    writer.write_file("/DIR/SUB/DEEP.TXT", b"nested data")
    before = path.read_bytes()

    with pytest.raises(ExfatWriteError, match="non-empty directory"):
        writer.delete("/DIR")
    assert path.read_bytes() == before

    writer.delete("/DIR", recursive=True)
    reader = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        reader.system_metadata()
        assert [item["name"] for item in reader.list_dir("/")] == ["HELLO.TXT"]
        with pytest.raises(FileNotFoundError):
            reader._find("/DIR")
    finally:
        reader.close()


def test_native_exfat_delete_refuses_cross_linked_cluster(tmp_path):
    path = tmp_path / "exfat-cross-linked-delete.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.write_file("/OTHER.TXT", b"separate allocation")
    reader = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    record = next(
        item
        for item in writer._directory_records(reader, reader.profile.root_cluster)
        if item[0] == "OTHER.TXT"
    )
    relative = writer._entry_offset(reader, reader.profile.root_cluster, record[1])
    reader.close()

    entry_set = bytearray(record[4])
    struct.pack_into("<I", entry_set, 52, 3)
    struct.pack_into("<H", entry_set, 2, 0)
    struct.pack_into("<H", entry_set, 2, writer._entry_checksum(entry_set))
    with path.open("r+b") as stream:
        stream.seek(relative)
        stream.write(entry_set)
    before = path.read_bytes()

    with pytest.raises(ExfatWriteError, match="shared by"):
        writer.delete("/OTHER.TXT")
    assert path.read_bytes() == before


def test_native_exfat_writer_creates_mkdirs_and_deletes_empty_entries(tmp_path):
    path = tmp_path / "exfat-writer-allocation.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.write_file("/NEW.TXT", b"")
    writer.mkdir("/EMPTY")
    writer.delete("/NEW.TXT")
    writer.delete("/EMPTY")
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    try:
        assert [item["name"] for item in provider.list_dir("/")] == ["HELLO.TXT"]
    finally:
        provider.close()


def test_native_exfat_writer_grows_full_root_directory_by_one_cluster(tmp_path):
    path = tmp_path / "exfat-root-growth.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    for name in ("A.TXT", "B.TXT", "C.TXT"):
        writer.write_file("/" + name, b"")
    writer.write_file("/GROWN.TXT", b"after root growth")

    reader = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        root_chain = reader._chain(reader.profile.root_cluster, no_fat_chain=False)
        assert len(root_chain) == 2
        reader.system_metadata()
        names = {entry.name for entry in reader._directory(reader.profile.root_cluster)}
        assert {"HELLO.TXT", "A.TXT", "B.TXT", "C.TXT", "GROWN.TXT"} <= names
        assert reader.read_file("/GROWN.TXT") == b"after root growth"
    finally:
        reader.close()


def test_native_exfat_mutates_entries_in_fragmented_root_directory(tmp_path):
    path = tmp_path / "exfat-fragmented-root-mutations.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    for name in ("A.TXT", "B.TXT", "C.TXT"):
        writer.write_file("/" + name, b"")
    writer.write_file("/GROWN.TXT", b"initial")
    writer.write_file("/GROWN.TXT", b"updated")
    writer.rename("/GROWN.TXT", "/MOVED.TXT")
    writer.write_file("/MOVED.TXT", b"")
    writer.delete("/MOVED.TXT")

    reader = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        assert len(reader._chain(reader.profile.root_cluster)) == 2
        reader.system_metadata()
        names = {entry.name for entry in reader._directory(reader.profile.root_cluster)}
        assert {"HELLO.TXT", "A.TXT", "B.TXT", "C.TXT"} <= names
        assert "GROWN.TXT" not in names
        assert "MOVED.TXT" not in names
    finally:
        reader.close()


def test_native_exfat_mkdir_grows_full_root_directory(tmp_path):
    path = tmp_path / "exfat-root-mkdir-growth.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    for name in ("A.TXT", "B.TXT", "C.TXT"):
        writer.write_file("/" + name, b"")
    writer.mkdir("/GROWN")

    reader = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        assert len(reader._chain(reader.profile.root_cluster)) == 2
        reader.system_metadata()
        assert next(item for item in reader.list_dir("/") if item["name"] == "GROWN")[
            "is_dir"
        ]
    finally:
        reader.close()


@pytest.mark.parametrize("create_directory", [False, True])
def test_native_exfat_nested_directory_growth_updates_stream_lengths(
    tmp_path, create_directory
):
    path = tmp_path / "exfat-nested-growth.img"
    _tree_image(path)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    writer.mkdir("/NESTED")
    for index in range(5):
        writer.write_file(f"/NESTED/{index}.TXT", b"")
    if create_directory:
        writer.mkdir("/NESTED/OVERFLOW")
    else:
        writer.write_file("/NESTED/OVERFLOW.TXT", b"nested growth")

    reader = NativeExfatImage(path, volume_size=VOLUME_SIZE)
    try:
        nested = reader._find("/NESTED")
        assert nested.size == 2 * reader.profile.cluster_size
        assert len(reader._chain(nested.first_cluster)) == 2
        record = next(
            item
            for item in writer._directory_records(reader, reader.profile.root_cluster)
            if item[0] == "NESTED"
        )
        assert struct.unpack_from("<Q", record[3], 8)[0] == nested.size
        reader.system_metadata()
        names = {item.name for item in reader._directory(nested.first_cluster)}
        assert "OVERFLOW" in names if create_directory else "OVERFLOW.TXT" in names
        if not create_directory:
            assert reader.read_file("/NESTED/OVERFLOW.TXT") == b"nested growth"
    finally:
        reader.close()

    if not create_directory:
        writer.write_file("/NESTED/OVERFLOW.TXT", b"updated")
        writer.rename("/NESTED/OVERFLOW.TXT", "/NESTED/MOVED.TXT")
        writer.write_file("/NESTED/MOVED.TXT", b"")
        writer.delete("/NESTED/MOVED.TXT")
        reader = NativeExfatImage(path, volume_size=VOLUME_SIZE)
        try:
            nested = reader._find("/NESTED")
            assert nested.size == 2 * reader.profile.cluster_size
            reader.system_metadata()
            names = {item.name for item in reader._directory(nested.first_cluster)}
            assert "MOVED.TXT" not in names
        finally:
            reader.close()


def test_universal_rw_exposes_limited_exfat_strategy(tmp_path, monkeypatch):
    path = tmp_path / "exfat-rw.img"
    _tree_image(path)
    backend = NativeExfatRW(path, volume_size=VOLUME_SIZE)
    try:
        assert backend.info().write_strategy == "native-exfat-phase1"

        import mountfile.exfat_writer as exfat_writer

        replace = exfat_writer.os.replace

        def replace_with_closed_source(src, dst):
            assert backend._reader._stream is None
            return replace(src, dst)

        monkeypatch.setattr(exfat_writer.os, "replace", replace_with_closed_source)
        backend.write_file("/HELLO.TXT", b"adapter")
        assert backend.read("/HELLO.TXT") == b"adapter"
        backend.mkdir("/NEW")
        assert "NEW" in {entry.name for entry in backend.list("/")}
        backend.rename("/NEW", "/RENAMED")
        assert "RENAMED" in {entry.name for entry in backend.list("/")}
        backend.delete("/RENAMED")
        assert "RENAMED" not in {entry.name for entry in backend.list("/")}
    finally:
        backend.close()


def test_exfat_writer_fails_closed_without_system_metadata(tmp_path):
    path = tmp_path / "exfat-no-metadata.img"
    _tree_image(path)
    raw = bytearray(path.read_bytes())
    raw[32 * 512] = 0
    path.write_bytes(raw)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    with pytest.raises(ExfatWriteError, match="allocation bitmap entry is missing"):
        writer.write_file("/HELLO.TXT", b"blocked")


def test_native_exfat_reader_rejects_entry_set_checksum_corruption(tmp_path):
    path = tmp_path / "exfat-entry-checksum.img"
    _tree_image(path)
    raw = bytearray(path.read_bytes())
    raw[32 * 512 + 130] ^= 0x01
    path.write_bytes(raw)
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    try:
        with pytest.raises(ExfatReadError, match="entry-set checksum"):
            provider.list_dir("/")
    finally:
        provider.close()


def test_native_exfat_writer_rejects_entry_checksum_before_mutation(tmp_path):
    path = tmp_path / "exfat-writer-entry-checksum.img"
    _tree_image(path)
    raw = bytearray(path.read_bytes())
    raw[32 * 512 + 130] ^= 0x01
    path.write_bytes(raw)
    before = path.read_bytes()
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    with pytest.raises(ExfatWriteError, match="entry-set checksum"):
        writer.write_file("/HELLO.TXT", b"must not commit")
    assert path.read_bytes() == before


def test_native_exfat_reader_rejects_filename_hash_corruption(tmp_path):
    path = tmp_path / "exfat-name-hash.img"
    _tree_image(path)
    raw = bytearray(path.read_bytes())
    struct.pack_into("<H", raw, 32 * 512 + 100, 0x1234)
    entry_start = 32 * 512 + 64
    entry_set = bytearray(raw[entry_start : entry_start + 96])
    struct.pack_into("<H", entry_set, 2, _entry_set_checksum(entry_set))
    raw[entry_start : entry_start + 96] = entry_set
    path.write_bytes(raw)
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    try:
        with pytest.raises(ExfatReadError, match="filename hash"):
            provider.list_dir("/")
    finally:
        provider.close()


@pytest.mark.parametrize(
    ("relative", "value", "message"),
    [
        (1, 0x80, "reserved flags"),
        (3, 16, "filename length"),
    ],
)
def test_native_exfat_reader_rejects_malformed_stream_extension(
    tmp_path, relative, value, message
):
    path = tmp_path / "exfat-malformed-stream.img"
    _tree_image(path)
    raw = bytearray(path.read_bytes())
    raw[32 * 512 + 96 + relative] = value
    entry_start = 32 * 512 + 64
    entry_set = bytearray(raw[entry_start : entry_start + 96])
    struct.pack_into("<H", entry_set, 2, _entry_set_checksum(entry_set))
    raw[entry_start : entry_start + 96] = entry_set
    path.write_bytes(raw)
    provider = vfs.open_image_provider(path, volume_size=VOLUME_SIZE)
    try:
        with pytest.raises(ExfatReadError, match=message):
            provider.list_dir("/")
    finally:
        provider.close()


def test_native_exfat_writer_rejects_bitmap_fat_mismatch(tmp_path):
    path = tmp_path / "exfat-bitmap-fat-mismatch.img"
    _tree_image(path)
    raw = bytearray(path.read_bytes())
    struct.pack_into("<I", raw, 24 * 512 + 6 * 4, 0xFFFFFFFF)
    path.write_bytes(raw)
    writer = NativeExfatWriter(path, volume_size=VOLUME_SIZE)
    with pytest.raises(ExfatWriteError, match="bitmap/FAT metadata is inconsistent"):
        writer.write_file("/NEW.TXT", b"blocked")


@pytest.mark.parametrize("mutate", ["signature", "checksum", "heap"])
def test_rejects_corrupt_or_unsafe_boot_region(mutate):
    region = bytearray(_region())
    if mutate == "signature":
        region[3:11] = b"NOTEXFAT"
    elif mutate == "checksum":
        region[-1] ^= 1
    else:
        struct.pack_into("<I", region, 0x58, 10)
    with pytest.raises(ExfatProbeError):
        parse_exfat_boot_region(bytes(region), volume_size=VOLUME_SIZE)


def test_rejects_corrupt_exfat_backup_boot_checksum():
    region = bytearray(_region())
    region[23 * 512] ^= 1
    with pytest.raises(ExfatProbeError, match="backup boot-region checksum"):
        parse_exfat_boot_region(bytes(region), volume_size=VOLUME_SIZE)


def test_backup_boot_flags_and_percent_in_use_may_be_stale():
    region = bytearray(_region())
    backup = 12 * 512
    struct.pack_into("<H", region, backup + 0x6A, 3)
    region[backup + 0x70] = 70
    profile = parse_exfat_boot_region(bytes(region), volume_size=VOLUME_SIZE)
    assert profile.volume_flags == 0
    assert profile.percent_in_use == 10


@pytest.mark.parametrize("mutate", ["volume_length", "fat_count", "fat_size"])
def test_rejects_exfat_geometry_that_only_fit_under_wrong_units(mutate):
    region = bytearray(_region())
    if mutate == "volume_length":
        struct.pack_into("<Q", region, 0x48, VOLUME_SIZE)
    elif mutate == "fat_count":
        region[0x6E] = 0
    else:
        struct.pack_into("<I", region, 0x54, 1)
        struct.pack_into("<I", region, 0x5C, 200)
    checksum = exfat_boot_checksum(bytes(region[: 11 * 512]), sector_size=512)
    for offset in range(0, 512, 4):
        struct.pack_into("<I", region, 11 * 512 + offset, checksum)
    with pytest.raises(ExfatProbeError):
        parse_exfat_boot_region(bytes(region), volume_size=VOLUME_SIZE)
