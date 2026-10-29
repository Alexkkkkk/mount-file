# SPDX-License-Identifier: MIT
"""Synthetic regression tests for the bounded pure-Python ext2 reader."""

import hashlib
import struct

import pytest

from mountfile import cli, vfs
from mountfile.ext2_reader import Ext2ReadError, NativeExt2Image

BLOCK_SIZE = 1024
BLOCKS_COUNT = 64
INODES_COUNT = 32
INODE_TABLE_BLOCK = 5
ROOT_DIR_BLOCK = 20


def _put_inode(image, number, *, mode, size, pointers, flags=0, mtime=0):
    offset = INODE_TABLE_BLOCK * BLOCK_SIZE + (number - 1) * 128
    struct.pack_into("<H", image, offset, mode)
    struct.pack_into("<I", image, offset + 4, size & 0xFFFFFFFF)
    struct.pack_into("<I", image, offset + 16, mtime)
    struct.pack_into("<I", image, offset + 32, flags)
    for slot, block in enumerate(pointers):
        struct.pack_into("<I", image, offset + 40 + slot * 4, block)


def _dir_block(entries):
    result = bytearray(BLOCK_SIZE)
    position = 0
    for index, (name, inode, file_type) in enumerate(entries):
        raw_name = name.encode("utf-8")
        minimum = (8 + len(raw_name) + 3) & ~3
        record_length = BLOCK_SIZE - position if index == len(entries) - 1 else minimum
        struct.pack_into("<IHB", result, position, inode, record_length, len(raw_name))
        result[position + 7] = file_type
        result[position + 8 : position + 8 + len(raw_name)] = raw_name
        position += record_length
    assert position == BLOCK_SIZE
    return bytes(result)


def _make_image(path, *, state=1, compat=0x0008, incompat=0x0002, ro_compat=0x0003):
    image = bytearray(BLOCKS_COUNT * BLOCK_SIZE)
    sb = memoryview(image)[1024:2048]
    struct.pack_into("<I", sb, 0x00, INODES_COUNT)
    struct.pack_into("<I", sb, 0x04, BLOCKS_COUNT)
    struct.pack_into("<I", sb, 0x14, 1)
    struct.pack_into("<I", sb, 0x18, 0)
    struct.pack_into("<I", sb, 0x20, BLOCKS_COUNT - 1)
    struct.pack_into("<I", sb, 0x28, INODES_COUNT)
    struct.pack_into("<H", sb, 0x38, 0xEF53)
    struct.pack_into("<H", sb, 0x3A, state)
    struct.pack_into("<I", sb, 0x4C, 1)
    struct.pack_into("<H", sb, 0x58, 128)
    struct.pack_into("<I", sb, 0x5C, compat)
    struct.pack_into("<I", sb, 0x60, incompat)
    struct.pack_into("<I", sb, 0x64, ro_compat)
    sb[0x78:0x7C] = b"TEST"

    # Primary group descriptor: bitmaps at 3/4 and inode table at blocks 5-8.
    struct.pack_into("<III", image, 2 * BLOCK_SIZE, 3, 4, INODE_TABLE_BLOCK)
    image[3 * BLOCK_SIZE : 4 * BLOCK_SIZE] = b"\xff" * BLOCK_SIZE
    image[4 * BLOCK_SIZE : 5 * BLOCK_SIZE] = b"\xff" * BLOCK_SIZE

    _put_inode(
        image,
        2,
        mode=0x4000 | 0o755,
        size=BLOCK_SIZE,
        pointers=(ROOT_DIR_BLOCK,) + (0,) * 14,
    )
    _put_inode(
        image, 11, mode=0x8000 | 0o644, size=11, pointers=(21,) + (0,) * 14, mtime=1
    )

    single_pointers = [0] * 15
    single_pointers[12] = 23
    _put_inode(
        image,
        12,
        mode=0x8000 | 0o644,
        size=13 * BLOCK_SIZE,
        pointers=single_pointers,
    )

    double_pointers = [0] * 15
    double_pointers[13] = 25
    double_logical = 12 + BLOCK_SIZE // 4
    _put_inode(
        image,
        13,
        mode=0x8000 | 0o644,
        size=(double_logical + 1) * BLOCK_SIZE,
        pointers=double_pointers,
    )

    triple_pointers = [0] * 15
    triple_pointers[14] = 28
    triple_logical = 12 + BLOCK_SIZE // 4 + (BLOCK_SIZE // 4) ** 2
    _put_inode(
        image,
        14,
        mode=0x8000 | 0o644,
        size=(triple_logical + 1) * BLOCK_SIZE,
        pointers=triple_pointers,
    )

    entries = [
        (".", 2, 2),
        ("..", 2, 2),
        ("hello.txt", 11, 1),
        ("single.bin", 12, 1),
        ("double.bin", 13, 1),
        ("triple.bin", 14, 1),
    ]
    image[ROOT_DIR_BLOCK * BLOCK_SIZE : (ROOT_DIR_BLOCK + 1) * BLOCK_SIZE] = _dir_block(
        entries
    )
    image[21 * BLOCK_SIZE : 21 * BLOCK_SIZE + 11] = b"hello ext2!"

    # Single-indirect: inode 12 -> 23 -> data block 24.
    struct.pack_into("<I", image, 23 * BLOCK_SIZE, 24)
    image[24 * BLOCK_SIZE : 25 * BLOCK_SIZE] = b"S" * BLOCK_SIZE
    # Double-indirect: inode 13 -> 25 -> 26 -> data block 27.
    struct.pack_into("<I", image, 25 * BLOCK_SIZE, 26)
    struct.pack_into("<I", image, 26 * BLOCK_SIZE, 27)
    image[27 * BLOCK_SIZE : 28 * BLOCK_SIZE] = b"D" * BLOCK_SIZE
    # Triple-indirect: inode 14 -> 28 -> 29 -> 30 -> data block 31.
    struct.pack_into("<I", image, 28 * BLOCK_SIZE, 29)
    struct.pack_into("<I", image, 29 * BLOCK_SIZE, 30)
    struct.pack_into("<I", image, 30 * BLOCK_SIZE, 31)
    image[31 * BLOCK_SIZE : 32 * BLOCK_SIZE] = b"T" * BLOCK_SIZE

    path.write_bytes(image)
    return bytes(image)


def _dir_block_for(entries, block_size, filetype):
    result = bytearray(block_size)
    position = 0
    for index, (name, inode, kind) in enumerate(entries):
        raw_name = name.encode("utf-8")
        minimum = (8 + len(raw_name) + 3) & ~3
        record_length = block_size - position if index == len(entries) - 1 else minimum
        struct.pack_into("<IH", result, position, inode, record_length)
        if filetype:
            result[position + 6] = len(raw_name)
            result[position + 7] = kind
        else:
            struct.pack_into("<H", result, position + 6, len(raw_name))
        result[position + 8 : position + 8 + len(raw_name)] = raw_name
        position += record_length
    assert position == block_size
    return bytes(result)


def _make_small_block_image(path, block_size, *, revision=1, filetype=True):
    blocks_count = 32
    inodes_count = 16
    first_data = 1 if block_size == 1024 else 0
    blocks_per_group = blocks_count - first_data
    inode_table = first_data + 4
    inode_table_blocks = (inodes_count * 128 + block_size - 1) // block_size
    directory_block = inode_table + inode_table_blocks + 1
    file_block = directory_block + 1
    image = bytearray(blocks_count * block_size)
    sb = memoryview(image)[1024:2048]
    struct.pack_into("<I", sb, 0x00, inodes_count)
    struct.pack_into("<I", sb, 0x04, blocks_count)
    struct.pack_into("<I", sb, 0x14, first_data)
    struct.pack_into("<I", sb, 0x18, block_size.bit_length() - 11)
    struct.pack_into("<I", sb, 0x20, blocks_per_group)
    struct.pack_into("<I", sb, 0x28, inodes_count)
    struct.pack_into("<H", sb, 0x38, 0xEF53)
    struct.pack_into("<H", sb, 0x3A, 1)
    struct.pack_into("<I", sb, 0x4C, revision)
    struct.pack_into("<H", sb, 0x58, 128)
    struct.pack_into("<I", sb, 0x60, 0x0002 if filetype else 0)
    gdt_block = first_data + 1
    block_bitmap = first_data + 2
    inode_bitmap = first_data + 3
    struct.pack_into(
        "<III", image, gdt_block * block_size, block_bitmap, inode_bitmap, inode_table
    )
    image[block_bitmap * block_size : (block_bitmap + 1) * block_size] = (
        b"\xff" * block_size
    )
    image[inode_bitmap * block_size : (inode_bitmap + 1) * block_size] = (
        b"\xff" * block_size
    )

    def put_inode(number, mode, size, block):
        inode_offset = inode_table * block_size + (number - 1) * 128
        struct.pack_into("<H", image, inode_offset, mode)
        struct.pack_into("<I", image, inode_offset + 4, size)
        struct.pack_into("<I", image, inode_offset + 40, block)

    put_inode(2, 0x4000 | 0o755, block_size, directory_block)
    put_inode(3, 0x8000 | 0o644, 5, file_block)
    entries = [(".", 2, 2), ("..", 2, 2), ("x", 3, 1)]
    directory = _dir_block_for(entries, block_size, filetype)
    image[directory_block * block_size : (directory_block + 1) * block_size] = directory
    image[file_block * block_size : file_block * block_size + 5] = b"small"
    path.write_bytes(image)
    return len(image)


@pytest.mark.parametrize("block_size", [2048, 4096])
def test_reads_supported_larger_block_sizes(tmp_path, block_size):
    image_path = tmp_path / f"ext2-{block_size}.img"
    extent_size = _make_small_block_image(image_path, block_size)
    provider = NativeExt2Image(image_path, volume_size=extent_size)
    try:
        assert provider.block_size == block_size
        assert provider.read_file("/x") == b"small"
    finally:
        provider.close()


def test_reads_revision_zero_classic_directory_records(tmp_path):
    image_path = tmp_path / "revision-zero.img"
    extent_size = _make_small_block_image(image_path, 1024, revision=0, filetype=False)
    provider = NativeExt2Image(image_path, volume_size=extent_size)
    try:
        assert provider.read_file("/x") == b"small"
    finally:
        provider.close()


def test_reads_directory_and_direct_file_without_external_tools(tmp_path):
    image_path = tmp_path / "ext2.img"
    original = _make_image(image_path)
    provider = NativeExt2Image(image_path, offset=0, volume_size=len(original))
    try:
        assert provider.backend_name == "native-ext2"
        assert provider._stream.writable() is False
        assert provider.file_size("/hello.txt") == 11
        assert provider.read_file_range("/hello.txt", 6, 32) == b"ext2!"
        assert [item["name"] for item in provider.list_dir("/")] == [
            "double.bin",
            "hello.txt",
            "single.bin",
            "triple.bin",
        ]
    finally:
        provider.close()


def test_vfs_native_provider_does_not_require_pytsk3(tmp_path, monkeypatch):
    image_path = tmp_path / "vfs-ext2.img"
    _make_image(image_path)
    monkeypatch.setattr(vfs, "pytsk3", None)
    assert vfs.volume_size_for_offset(image_path, 0) == BLOCKS_COUNT * BLOCK_SIZE
    provider = vfs.open_image_provider(
        image_path, offset=0, volume_size=BLOCKS_COUNT * BLOCK_SIZE
    )
    try:
        assert provider.backend_name == "native-ext2"
        assert provider.read_file("/hello.txt") == b"hello ext2!"
    finally:
        provider.close()
    monkeypatch.setattr(
        vfs,
        "TskImage",
        lambda *args, **kwargs: pytest.fail("unbounded ext2 reached pytsk3"),
    )
    with pytest.raises(vfs.RecognizedFilesystemError, match="explicit bounded extent"):
        vfs.open_image_provider(image_path, offset=0)


def test_dirty_ext2_is_rejected_without_pytsk3_or_fat32_fallback(tmp_path, monkeypatch):
    image_path = tmp_path / "dirty-ext2-routing.img"
    _make_image(image_path, state=2)

    monkeypatch.setattr(
        vfs,
        "TskImage",
        lambda *args, **kwargs: pytest.fail("dirty ext2 reached pytsk3 fallback"),
    )
    monkeypatch.setattr(
        cli,
        "Fat32Provider",
        lambda *args, **kwargs: pytest.fail("dirty ext2 reached FAT32 fallback"),
    )

    with pytest.raises(vfs.RecognizedFilesystemError, match="ext2 volume is dirty"):
        vfs.open_image_provider(
            image_path, offset=0, volume_size=BLOCKS_COUNT * BLOCK_SIZE
        )
    with pytest.raises(vfs.RecognizedFilesystemError, match="ext2 volume is dirty"):
        cli._provider(str(image_path), 0)


def test_cli_selects_native_ext2_for_bounded_whole_image(tmp_path, monkeypatch):
    image_path = tmp_path / "cli-ext2.img"
    _make_image(image_path)
    monkeypatch.setattr(vfs, "pytsk3", None)
    provider, backend = cli._provider(str(image_path), 0)
    try:
        assert backend == "native-ext2"
        assert provider.read_file("/hello.txt") == b"hello ext2!"
    finally:
        provider.close()


def test_native_ext2_provider_can_close_and_reopen(tmp_path):
    image_path = tmp_path / "reopen-ext2.img"
    _make_image(image_path)
    provider = NativeExt2Image(image_path, volume_size=BLOCKS_COUNT * BLOCK_SIZE)
    try:
        provider.close()
        with pytest.raises(Ext2ReadError, match="image is closed"):
            provider.read_file("/hello.txt")
        provider.reopen()
        assert provider.read_file("/hello.txt") == b"hello ext2!"
    finally:
        provider.close()


def test_native_ext2_failed_reopen_after_truncation_stays_closed(tmp_path):
    image_path = tmp_path / "truncated-reopen-ext2.img"
    _make_image(image_path)
    provider = NativeExt2Image(image_path, volume_size=BLOCKS_COUNT * BLOCK_SIZE)
    provider.close()
    image_path.write_bytes(b"truncated")

    with pytest.raises(Ext2ReadError):
        provider.reopen()
    assert provider._stream is None
    with pytest.raises(Ext2ReadError, match="image is closed"):
        provider.read_file("/hello.txt")


def test_reads_single_double_and_triple_indirect_blocks(tmp_path):
    image_path = tmp_path / "indirect.img"
    _make_image(image_path)
    provider = NativeExt2Image(image_path, volume_size=BLOCKS_COUNT * BLOCK_SIZE)
    try:
        single_start = 12 * BLOCK_SIZE
        double_start = (12 + BLOCK_SIZE // 4) * BLOCK_SIZE
        triple_start = (12 + BLOCK_SIZE // 4 + (BLOCK_SIZE // 4) ** 2) * BLOCK_SIZE
        assert provider.read_file_range("/single.bin", 0, 1) == b"\x00"
        assert provider.read_file_range("/single.bin", single_start, 4) == b"SSSS"
        assert provider.read_file_range("/double.bin", double_start, 4) == b"DDDD"
        assert provider.read_file_range("/triple.bin", triple_start, 4) == b"TTTT"
    finally:
        provider.close()


def test_range_reads_batch_contiguous_blocks_and_cache_indirect_tables(tmp_path):
    image_path = tmp_path / "batched-range.img"
    image = bytearray(_make_image(image_path))
    inode_offset = INODE_TABLE_BLOCK * BLOCK_SIZE + (12 - 1) * 128
    struct.pack_into("<I", image, inode_offset + 4, 15 * BLOCK_SIZE)
    struct.pack_into("<III", image, 23 * BLOCK_SIZE, 24, 25, 26)
    image[24 * BLOCK_SIZE : 25 * BLOCK_SIZE] = b"A" * BLOCK_SIZE
    image[25 * BLOCK_SIZE : 26 * BLOCK_SIZE] = b"B" * BLOCK_SIZE
    image[26 * BLOCK_SIZE : 27 * BLOCK_SIZE] = b"C" * BLOCK_SIZE
    image_path.write_bytes(image)

    provider = NativeExt2Image(image_path, volume_size=len(image))
    pointer_reads = []
    data_reads = []
    read_at = provider._read_at

    def counted_read_at(relative_offset, size):
        if relative_offset == 23 * BLOCK_SIZE:
            pointer_reads.append((relative_offset, size))
        if 24 * BLOCK_SIZE <= relative_offset < 27 * BLOCK_SIZE:
            data_reads.append((relative_offset, size))
        return read_at(relative_offset, size)

    provider._read_at = counted_read_at
    try:
        assert provider.read_file_range(
            "/single.bin", 12 * BLOCK_SIZE, 3 * BLOCK_SIZE
        ) == (b"A" * BLOCK_SIZE + b"B" * BLOCK_SIZE + b"C" * BLOCK_SIZE)
        assert pointer_reads == [(23 * BLOCK_SIZE, BLOCK_SIZE)]
        assert data_reads == [(24 * BLOCK_SIZE, 3 * BLOCK_SIZE)]
    finally:
        provider.close()


def test_range_read_keeps_noncontiguous_blocks_and_sparse_holes_separate(tmp_path):
    image_path = tmp_path / "noncontiguous-range.img"
    image = bytearray(_make_image(image_path))
    inode_offset = INODE_TABLE_BLOCK * BLOCK_SIZE + (12 - 1) * 128
    struct.pack_into("<I", image, inode_offset + 4, 15 * BLOCK_SIZE)
    struct.pack_into("<III", image, 23 * BLOCK_SIZE, 24, 26, 0)
    image[24 * BLOCK_SIZE : 25 * BLOCK_SIZE] = b"A" * BLOCK_SIZE
    image[26 * BLOCK_SIZE : 27 * BLOCK_SIZE] = b"B" * BLOCK_SIZE
    image_path.write_bytes(image)

    provider = NativeExt2Image(image_path, volume_size=len(image))
    try:
        assert (
            provider.read_file_range("/single.bin", 12 * BLOCK_SIZE, 3 * BLOCK_SIZE)
            == b"A" * BLOCK_SIZE + b"B" * BLOCK_SIZE + b"\x00" * BLOCK_SIZE
        )
    finally:
        provider.close()


def test_large_file_reads_are_limited_before_allocating(tmp_path):
    image_path = tmp_path / "large-file.img"
    _make_image(image_path)
    provider = NativeExt2Image(image_path, volume_size=BLOCKS_COUNT * BLOCK_SIZE)
    try:
        with pytest.raises(Ext2ReadError, match="16 MiB"):
            provider.read_file_range("/triple.bin", 0, 17 * 1024 * 1024)
        with pytest.raises(Ext2ReadError, match="16 MiB"):
            provider.read_file("/triple.bin")
        assert (
            provider.read_file_range(
                "/triple.bin", provider.file_size("/triple.bin") - 4, 1 << 40
            )
            == b"TTTT"
        )
    finally:
        provider.close()


def test_partition_offset_is_bounded_and_image_is_unchanged(tmp_path):
    payload = _make_image(tmp_path / "payload.img")
    prefix = b"P" * 73
    suffix = b"SENTINEL" * 16
    image_path = tmp_path / "partitioned.img"
    image_path.write_bytes(prefix + payload + suffix)
    assert vfs.volume_size_for_offset(image_path, len(prefix)) is None
    before = hashlib.sha256(image_path.read_bytes()).digest()
    provider = NativeExt2Image(image_path, offset=len(prefix), volume_size=len(payload))
    try:
        assert provider.read_file("/hello.txt") == b"hello ext2!"
        assert provider._read_at(provider.filesystem_size - 1, 1) == payload[-1:]
        with pytest.raises(Ext2ReadError, match="partition bounds"):
            provider._read_at(provider.filesystem_size, 1)
    finally:
        provider.close()
    assert hashlib.sha256(image_path.read_bytes()).digest() == before


def test_dirty_journaled_and_unknown_feature_profiles_fail_closed(tmp_path):
    dirty = tmp_path / "dirty.img"
    _make_image(dirty, state=2)
    with pytest.raises(Ext2ReadError, match="dirty"):
        NativeExt2Image(dirty, volume_size=BLOCKS_COUNT * BLOCK_SIZE)

    journaled = tmp_path / "journaled.img"
    _make_image(journaled, compat=0x000C)
    with pytest.raises(Ext2ReadError, match="ext2 profiles only|journal"):
        NativeExt2Image(journaled, volume_size=BLOCKS_COUNT * BLOCK_SIZE)

    unknown = tmp_path / "unknown-feature.img"
    _make_image(unknown, compat=0x0028)
    with pytest.raises(Ext2ReadError, match="indexed|feature"):
        NativeExt2Image(unknown, volume_size=BLOCKS_COUNT * BLOCK_SIZE)


def test_malformed_directory_and_out_of_range_data_pointer_fail(tmp_path):
    image_path = tmp_path / "malformed-dir.img"
    _make_image(image_path)
    with image_path.open("r+b") as stream:
        stream.seek(ROOT_DIR_BLOCK * BLOCK_SIZE + 4)
        stream.write(b"\x00\x00")
    provider = NativeExt2Image(image_path, volume_size=BLOCKS_COUNT * BLOCK_SIZE)
    try:
        with pytest.raises(Ext2ReadError, match="record length"):
            provider.list_dir("/")
    finally:
        provider.close()

    pointer_path = tmp_path / "bad-pointer.img"
    _make_image(pointer_path)
    inode_offset = INODE_TABLE_BLOCK * BLOCK_SIZE + (11 - 1) * 128
    with pointer_path.open("r+b") as stream:
        stream.seek(inode_offset + 40)
        stream.write(struct.pack("<I", BLOCKS_COUNT + 1))
    provider = NativeExt2Image(pointer_path, volume_size=BLOCKS_COUNT * BLOCK_SIZE)
    try:
        with pytest.raises(Ext2ReadError, match="allocatable filesystem bounds"):
            provider.read_file_range("/hello.txt", 0, 1)
    finally:
        provider.close()


def test_path_traversal_is_rejected(tmp_path):
    image_path = tmp_path / "ext2.img"
    _make_image(image_path)
    provider = NativeExt2Image(image_path, volume_size=BLOCKS_COUNT * BLOCK_SIZE)
    try:
        with pytest.raises(Ext2ReadError, match="dot components"):
            provider.file_size("/../hello.txt")
    finally:
        provider.close()
