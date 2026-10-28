"""Acceptance matrix for issue #182's bounded native VFS contract.

These tests intentionally use the existing synthetic ext fixtures and native
writer helper.  They verify the public behavior at the routing boundary rather
than reaching into provider internals, and keep all negative cases fail-closed.
"""

import struct

import pytest

from mountfile import vfs
from mountfile.ext2_native import NativeExt2ImageWriter
from mountfile.ext2_reader import NativeExt2Image
from mountfile.extract import extract_path
from tests.test_ext2_native import _make_ext2
from tests.test_ext2_reader import BLOCK_SIZE, BLOCKS_COUNT, _make_image
from tests.test_ext3_reader import _make_clean_journal_image


@pytest.mark.parametrize("backend_flag", [None, False])
def test_bounded_native_provider_contract_without_pytsk3(
    tmp_path, monkeypatch, backend_flag
):
    """list/stat/read-range/extract work through the offline native provider."""
    image = tmp_path / "contract.ext2"
    _make_image(image)
    monkeypatch.setattr(vfs, "pytsk3", None)
    provider = vfs.open_image_provider(
        image,
        offset=0,
        volume_size=BLOCKS_COUNT * BLOCK_SIZE,
        allow_legacy_fallback=backend_flag,
    )
    try:
        assert provider.backend_name == "native-ext2"
        assert provider.read_capabilities == ("list", "stat", "read-range", "extract")
        assert provider.write_capabilities == ()
        assert {entry["name"] for entry in provider.list_dir("/")} == {
            "hello.txt",
            "single.bin",
            "double.bin",
            "triple.bin",
        }
        assert provider.stat("/hello.txt")["size"] == 11
        assert provider.read_file_range("/hello.txt", 6, 5) == b"ext2!"
        destination = tmp_path / "extracted"
        assert extract_path(provider, "/hello.txt", destination) == 1
        assert (destination / "hello.txt").read_bytes() == b"hello ext2!"
    finally:
        provider.close()


def test_partition_extent_bounds_preserve_neighbor_sentinels(tmp_path, monkeypatch):
    """Routing honors the selected partition extent and never reads neighbors."""
    partition = tmp_path / "partition.ext2"
    extent = _make_image(partition)
    offset = 2048 * 512
    prefix = b"prefix-sentinel" * 31
    suffix = b"suffix-sentinel" * 31
    image = tmp_path / "disk.img"
    with image.open("wb") as stream:
        stream.write(b"\0" * (offset - len(prefix)))
        stream.write(prefix)
        stream.write(extent)
        stream.write(suffix)
    # The synthetic MBR describes exactly the ext2 extent, leaving suffix data
    # outside the selected partition.  This also exercises list_partitions' EOF
    # and partition-size calculations rather than inferring size from remainder.
    mbr = bytearray(512)
    mbr[446 + 4] = 0x83
    struct.pack_into("<II", mbr, 446 + 8, 2048, len(extent) // 512)
    mbr[510:512] = b"\x55\xaa"
    with image.open("r+b") as stream:
        stream.seek(0)
        stream.write(mbr)
    parts = vfs.volume_size_for_offset(image, offset)
    assert parts == len(extent)
    monkeypatch.setattr(vfs, "pytsk3", None)
    provider = vfs.open_image_provider(image, offset=offset, volume_size=parts)
    try:
        assert provider.read_file("/hello.txt") == b"hello ext2!"
    finally:
        provider.close()
    raw = image.read_bytes()
    assert raw[offset - len(prefix) : offset] == prefix
    assert raw[offset + len(extent) :] == suffix
    with pytest.raises(vfs.TskError, match="exceeds image bounds"):
        vfs.open_image_provider(
            image, offset=offset, volume_size=len(extent) + len(suffix) + 1
        )


@pytest.mark.parametrize(
    ("name", "mutate", "message"),
    [
        (
            "malformed",
            lambda data: data.__setitem__(1024 + 0x38, 0),
            "legacy pytsk3 fallback is disabled",
        ),
        (
            "dirty",
            lambda data: data.__setitem__(1024 + 0x3A, 2),
            "dirty or requires journal recovery",
        ),
    ],
)
def test_malformed_and_dirty_profiles_default_deny_without_fallback(
    tmp_path, monkeypatch, name, mutate, message
):
    image = tmp_path / f"{name}.img"
    _make_image(image)
    data = bytearray(image.read_bytes())
    mutate(data)
    image.write_bytes(data)
    monkeypatch.setattr(
        vfs,
        "TskImage",
        lambda *_args, **_kwargs: pytest.fail("rejected native profile used pytsk3"),
    )
    with pytest.raises(vfs.RecognizedFilesystemError, match=message):
        vfs.open_image_provider(image, volume_size=len(data))


def test_recovery_required_profile_is_default_deny_and_read_only(tmp_path, monkeypatch):
    image = tmp_path / "recovery-required.img"
    _make_clean_journal_image(image)
    data = bytearray(image.read_bytes())
    struct.pack_into(">I", data, 40 * 1024 + 24, 1)
    image.write_bytes(data)
    before = image.read_bytes()
    monkeypatch.setattr(
        vfs,
        "TskImage",
        lambda *_args, **_kwargs: pytest.fail("recovery-required ext used pytsk3"),
    )
    with pytest.raises(
        vfs.RecognizedFilesystemError,
        match="native ext3/ext4 reader declined.*uncheckpointed transaction",
    ):
        vfs.open_image_provider(image, volume_size=len(data))
    assert image.read_bytes() == before


@pytest.mark.skipif(
    not __import__("shutil").which("mke2fs"), reason="mke2fs is required"
)
def test_native_write_readback_and_injected_commit_failure_preserve_original(
    tmp_path, monkeypatch
):
    image = tmp_path / "writable.ext2"
    _make_ext2(image, size=8 * 1024 * 1024)
    writer = NativeExt2ImageWriter(image)
    writer.write_file("/acceptance.txt", b"native write read-back")
    reader = NativeExt2Image(image, volume_size=image.stat().st_size)
    try:
        assert reader.read_file("/acceptance.txt") == b"native write read-back"
    finally:
        reader.close()

    before = image.read_bytes()
    failing = NativeExt2ImageWriter(image)
    monkeypatch.setattr(
        failing,
        "_commit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected acceptance commit failure")
        ),
    )
    with pytest.raises(OSError, match="injected acceptance commit failure"):
        failing.write_file("/must-not-commit.txt", b"blocked")
    assert image.read_bytes() == before
