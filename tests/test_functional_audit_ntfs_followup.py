# SPDX-License-Identifier: MIT
"""Real mkfs.ntfs root-list/stat regressions and bounded metadata refusals."""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess

import pytest

from mountfile import vfs
from mountfile.ntfs_reader import (
    NativeNtfsMetadataImage,
    NtfsAttributeSummary,
    NtfsMetadataError,
    NtfsRun,
    _parse_runlist,
    _validate_nonresident_data_stream,
    parse_ntfs_attribute_stream,
)
from mountfile.safety import RefusalCode


@pytest.fixture(scope="module", params=[512, 4096, 8192])
def real_ntfs(request, tmp_path_factory):
    """Format and populate without any MountFile write implementation."""
    tools = [shutil.which(name) for name in ("mkfs.ntfs", "ntfscp")]
    if not all(tools):
        pytest.skip("independent NTFS formatter/copier unavailable")
    directory = tmp_path_factory.mktemp(f"ntfs-followup-{request.param}")
    image = directory / "independent.img"
    with image.open("wb") as stream:
        stream.truncate(32 * 1024**2)

    def run(*args):
        result = subprocess.run(args, capture_output=True, timeout=60)
        assert result.returncode == 0, (result.stdout + result.stderr).decode(
            errors="replace"
        )

    run(tools[0], "-F", "-Q", f"-c{request.param}", str(image))
    payloads = {
        "Small.txt": b"independent resident NTFS data\n",
        "Empty.txt": b"",
        "Large.bin": bytes(range(256)) * 4097,
    }
    source = directory / "source.bin"
    for name, payload in payloads.items():
        source.write_bytes(payload)
        run(tools[1], "-q", str(image), str(source), "/" + name)
    source.write_bytes(b"directory index allocation regression")
    for index in range(48):
        run(tools[1], "-q", str(image), str(source), f"/FILE-{index:02}.txt")
    return image, payloads


def _reader(image):
    return NativeNtfsMetadataImage(image, volume_size=image.stat().st_size)


def _attribute_offsets(record):
    raw = record.fixed_record
    position = record.first_attribute_offset
    while struct.unpack_from("<I", raw, position)[0] != 0xFFFFFFFF:
        type_code, length = struct.unpack_from("<II", raw, position)
        name_length = raw[position + 9]
        name_offset = struct.unpack_from("<H", raw, position + 10)[0]
        name = raw[
            position + name_offset : position + name_offset + name_length * 2
        ].decode("utf-16le")
        yield type_code, name, position
        position += length


def _record_location(reader, number):
    size = reader.profile.boot.file_record_size
    logical = number * size
    cluster_size = reader.profile.boot.cluster_size
    for run in reader._mft_data.runs:
        begin = run.vcn * cluster_size
        end = begin + run.length * cluster_size
        if begin <= logical and logical + size <= end:
            return run.lcn * cluster_size + logical - begin
    pytest.fail("test record does not fit inside one physical MFT run")


def _write_fixed_record(image, position, fixed_record):
    """Serialize a changed fixture record with genuine, regenerated USA tails."""
    raw = bytearray(fixed_record)
    usa_offset, usa_count = struct.unpack_from("<HH", raw, 4)
    sequence = struct.unpack_from("<H", raw, usa_offset)[0]
    for index in range(usa_count - 1):
        trailer = (index + 1) * 512 - 2
        struct.pack_into(
            "<H",
            raw,
            usa_offset + 2 + index * 2,
            struct.unpack_from("<H", raw, trailer)[0],
        )
        struct.pack_into("<H", raw, trailer, sequence)
    with image.open("r+b") as stream:
        stream.seek(position)
        stream.write(raw)


def test_public_root_list_stat_and_read_match_independent_files(real_ntfs):
    image, payloads = real_ntfs
    before = hashlib.sha256(image.read_bytes()).digest()
    reader = vfs.open_image_provider(
        image, volume_size=image.stat().st_size, allow_legacy_fallback=False
    )
    assert isinstance(reader, NativeNtfsMetadataImage)
    try:
        entries = reader.list_dir("/")
        by_name = {entry["name"]: entry for entry in entries}
        assert len(entries) == len(by_name) == 11 + 48 + len(payloads)
        assert "." not in by_name and ".." not in by_name
        assert by_name["$Extend"]["is_dir"] is True
        assert by_name["$BadClus"]["inode"] == 8
        assert by_name["$BadClus"]["size"] == 0
        assert by_name["$BadClus"]["content_supported"] is False
        assert by_name["$BadClus"]["unsupported_streams"] == ["$Bad"]
        assert reader.stat("/")["is_dir"] is True
        assert reader.stat("/$badclus")["unsupported_streams"] == ["$Bad"]
        for name, payload in payloads.items():
            assert by_name[name]["size"] == len(payload)
            assert reader.stat("/" + name.upper())["size"] == len(payload)
            assert reader.read_file("/" + name.upper()) == payload
            assert reader.read_file_range("/" + name, 509, 1111) == payload[509:1620]
            assert reader.read_file_range("/" + name, len(payload), 8) == b""
        for operation in (
            lambda: reader.read_file("/$BadClus"),
            lambda: reader.read_file_range("/$BadClus", 0, 16),
            lambda: reader.read_file_range("/$BadClus", 0, 0),
            lambda: reader.read_file("/$BadClus:$Bad"),
        ):
            with pytest.raises(NtfsMetadataError):
                operation()
        with pytest.raises(NtfsMetadataError, match="sparse") as refusal:
            reader.read_file("/$BadClus")
        assert refusal.value.code == RefusalCode.OPERATION_NOT_SUPPORTED
        metadata = reader._attributes_for_record(8, reader.read_mft_record(8))
        bad = next(a for a in metadata if a.name == "$Bad")
        assert bad.content_supported is False
        assert all(run.lcn is None for run in bad.runs)
        with pytest.raises(NtfsMetadataError, match="sparse"):
            reader._read_nonresident_range(bad, 0, 8)
    finally:
        reader.close()
    assert hashlib.sha256(image.read_bytes()).digest() == before


def test_public_listing_matches_pytsk3_root_metadata(real_ntfs):
    pytsk3 = pytest.importorskip("pytsk3")
    image, payloads = real_ntfs
    oracle = pytsk3.Img_Info(str(image))
    filesystem = pytsk3.FS_Info(oracle)
    expected = {}
    for entry in filesystem.open_dir(path="/"):
        name = entry.info.name.name.decode("utf-8")
        meta = entry.info.meta
        if name in (".", "..") or meta is None:
            continue
        if meta.type in (
            pytsk3.TSK_FS_META_TYPE_VIRT,
            pytsk3.TSK_FS_META_TYPE_VIRT_DIR,
        ):
            continue
        if meta.flags & pytsk3.TSK_FS_META_FLAG_ALLOC:
            expected[name] = (meta.addr, meta.type == pytsk3.TSK_FS_META_TYPE_DIR)
    reader = _reader(image)
    try:
        actual = {
            entry["name"]: (entry["inode"], entry["is_dir"])
            for entry in reader.list_dir("/")
        }
        assert actual == expected
        for name, payload in payloads.items():
            file = filesystem.open(path="/" + name)
            assert file.info.meta.size == len(payload)
            if payload:
                assert file.read_random(0, len(payload)) == reader.read_file("/" + name)
    finally:
        reader.close()
        oracle.close()


@pytest.mark.parametrize(
    "damage,message",
    [
        ("zero_run", "zero-length"),
        ("vcn_coverage", "declared VCN"),
        ("sparse_volume_overrun", "VCN bounds"),
        ("physical_volume_overrun", "outside the volume"),
        ("allocation", "DATA layout"),
        ("initialized", "sizes are inconsistent"),
        ("unknown_flags", "unknown flag"),
        ("compressed", "compressed, encrypted or sparse"),
        ("sparse_flag", "compressed, encrypted or sparse"),
        ("name_overlap", "overlaps its runlist"),
        ("wrong_root_name", "root FILE_NAME"),
        ("wrong_parent", "root FILE_NAME"),
        ("stale_parent", "sequence"),
        ("directory_alias", "directory flag disagrees"),
    ],
)
def test_badclus_exception_rejects_malformed_real_metadata(
    real_ntfs, tmp_path, damage, message
):
    original, _payloads = real_ntfs
    reader = _reader(original)
    try:
        record = reader.read_mft_record(8)
        position = _record_location(reader, 8)
        capacity = reader.profile.boot.cluster_count * reader.profile.boot.cluster_size
        cluster_count = reader.profile.boot.cluster_count
    finally:
        reader.close()
    offsets = {
        (type_code, name): offset
        for type_code, name, offset in _attribute_offsets(record)
    }
    raw = bytearray(record.fixed_record)
    bad = offsets[(0x80, "$Bad")]
    filename = offsets[(0x30, "")]
    run = bad + struct.unpack_from("<H", raw, bad + 32)[0]
    value = filename + struct.unpack_from("<H", raw, filename + 20)[0]
    width = raw[run] & 0xF
    if damage == "zero_run":
        raw[run + 1 : run + 1 + width] = bytes(width)
    elif damage == "vcn_coverage":
        struct.pack_into("<Q", raw, bad + 24, cluster_count)
    elif damage == "sparse_volume_overrun":
        raw[run + 1 : run + 1 + width] = (cluster_count + 1).to_bytes(width, "little")
    elif damage == "physical_volume_overrun":
        raw[run] |= 0x10
        raw[run + 1 + width] = 0xFF
        raw[run + 2 + width] = 0
    elif damage == "allocation":
        struct.pack_into("<Q", raw, bad + 40, capacity * 2)
    elif damage == "initialized":
        struct.pack_into("<Q", raw, bad + 56, capacity + 1)
    elif damage in ("unknown_flags", "compressed", "sparse_flag"):
        struct.pack_into(
            "<H",
            raw,
            bad + 12,
            {"unknown_flags": 4, "compressed": 1, "sparse_flag": 0x8000}[damage],
        )
    elif damage == "name_overlap":
        struct.pack_into("<H", raw, bad + 32, 64)
    elif damage == "wrong_root_name":
        replacement = "NotClus!".encode("utf-16le")
        assert len(replacement) == raw[value + 64] * 2
        raw[value + 66 : value + 66 + len(replacement)] = replacement
    elif damage == "wrong_parent":
        parent = struct.unpack_from("<Q", raw, value)[0]
        struct.pack_into("<Q", raw, value, parent + 1)
    elif damage == "stale_parent":
        parent = struct.unpack_from("<Q", raw, value)[0]
        struct.pack_into("<Q", raw, value, parent + (1 << 48))
    elif damage == "directory_alias":
        struct.pack_into("<H", raw, 0x16, record.flags | 2)
    image = tmp_path / "malformed.img"
    shutil.copyfile(original, image)
    _write_fixed_record(image, position, raw)
    before = hashlib.sha256(image.read_bytes()).digest()
    reader = _reader(image)
    try:
        with pytest.raises(NtfsMetadataError, match=message):
            reader.list_dir("/")
    finally:
        reader.close()
    assert hashlib.sha256(image.read_bytes()).digest() == before


def test_special_stream_is_not_allowed_for_an_ordinary_record(real_ntfs):
    image, _payloads = real_ntfs
    reader = _reader(image)
    try:
        record = reader.read_mft_record(8)
        with pytest.raises(NtfsMetadataError, match="sparse"):
            parse_ntfs_attribute_stream(record, allow_end_marker_padding=True)
        with pytest.raises(NtfsMetadataError, match="sparse"):
            reader._attributes_for_record(64, record)
    finally:
        reader.close()


def test_real_dirty_volume_remains_refused(real_ntfs, tmp_path):
    original, _payloads = real_ntfs
    reader = _reader(original)
    try:
        record = reader.read_mft_record(3)
        position = _record_location(reader, 3)
    finally:
        reader.close()
    volume = next(
        offset
        for type_code, _name, offset in _attribute_offsets(record)
        if type_code == 0x70
    )
    raw = bytearray(record.fixed_record)
    value = volume + struct.unpack_from("<H", raw, volume + 20)[0]
    struct.pack_into("<H", raw, value + 10, 1)
    image = tmp_path / "dirty.img"
    shutil.copyfile(original, image)
    _write_fixed_record(image, position, raw)
    with pytest.raises(NtfsMetadataError, match="dirty") as refusal:
        _reader(image)
    assert refusal.value.code == RefusalCode.DIRTY_OR_RECOVERY_REQUIRED


def test_sparse_descriptors_cannot_be_zero_filled_or_read_as_physical_bytes():
    """Even a manually constructed descriptor must fail before any image access."""
    attribute = NtfsAttributeSummary(
        0x80,
        80,
        True,
        "$Bad",
        0,
        0,
        1,
        (NtfsRun(0, None, 1),),
        data_size=512,
        allocated_size=512,
        initialized_size=0,
    )
    reader = NativeNtfsMetadataImage.__new__(NativeNtfsMetadataImage)
    with pytest.raises(NtfsMetadataError, match="sparse"):
        _validate_nonresident_data_stream(attribute, 512, name="test")
    with pytest.raises(NtfsMetadataError, match="sparse"):
        reader._read_nonresident_range(attribute, 0, 16)
    with pytest.raises(NtfsMetadataError, match="sparse"):
        reader._read_runs(attribute.runs, 0, 16)
    with pytest.raises(NtfsMetadataError, match="sparse"):
        _parse_runlist(bytes([1, 1, 0]), 32, allow_sparse=False)


def test_sparse_metadata_runlist_keeps_signed_lcn_deltas_and_strict_bounds():
    runs = _parse_runlist(
        bytes([0x11, 2, 5, 0x01, 3, 0x11, 1, 0xFF, 0]),
        32,
        highest_vcn=5,
    )
    assert runs == (NtfsRun(0, 5, 2), NtfsRun(2, None, 3), NtfsRun(5, 4, 1))
    for raw, message in (
        (bytes([1, 0, 0]), "zero-length"),
        (bytes([1, 33, 0]), "VCN bounds"),
        (bytes([1, 1]), "unterminated"),
        (bytes([0]), "no extents"),
    ):
        with pytest.raises(NtfsMetadataError, match=message):
            _parse_runlist(raw, 32, allow_sparse=True)
    with pytest.raises(NtfsMetadataError, match="must be a boolean"):
        _parse_runlist(bytes([1, 1, 0]), 32, allow_sparse=1)


def test_new_sparse_metadata_option_requires_explicit_boolean(real_ntfs):
    image, _payloads = real_ntfs
    reader = _reader(image)
    try:
        with pytest.raises(NtfsMetadataError, match="must be a boolean"):
            parse_ntfs_attribute_stream(
                reader.read_mft_record(8), allow_badclus_sparse=1
            )
    finally:
        reader.close()


def test_real_volume_cannot_escape_a_truncated_selected_extent(real_ntfs):
    image, _payloads = real_ntfs
    before = hashlib.sha256(image.read_bytes()).digest()
    with pytest.raises(NtfsMetadataError):
        NativeNtfsMetadataImage(image, volume_size=512)
    assert hashlib.sha256(image.read_bytes()).digest() == before
