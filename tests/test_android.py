import struct

import pytest

from mountfile.android import (ANDROID_BOOT_MAGIC, BOOT_IMAGE_HEADER_V1_SIZE,
                               BOOT_IMAGE_HEADER_V2_SIZE,
                               BOOT_IMAGE_HEADER_V3_SIZE,
                               BOOT_IMAGE_HEADER_V4_SIZE, CHUNK_TYPE_CRC32,
                               CHUNK_TYPE_DONT_CARE, CHUNK_TYPE_FILL,
                               CHUNK_TYPE_RAW, AndroidFormatError,
                               UnsupportedAndroidFormatError,
                               detect_boot_image, detect_sparse_image,
                               discover_external_tools, iter_sparse_chunks,
                               parse_boot_image_header, parse_sparse_header,
                               parse_sparse_image)


def _sparse_image(chunks, *, block_size=4, total_blocks=None, major=1):
    if total_blocks is None:
        total_blocks = sum(chunk[1] for chunk in chunks if chunk[0] != CHUNK_TYPE_CRC32)
    encoded = bytearray(
        struct.pack(
            "<IHHHHIIII",
            0xED26FF3A,
            major,
            0,
            28,
            12,
            block_size,
            total_blocks,
            len(chunks),
            0,
        )
    )
    for chunk_type, block_count, payload in chunks:
        encoded.extend(
            struct.pack("<HHII", chunk_type, 0, block_count, 12 + len(payload))
        )
        encoded.extend(payload)
    return bytes(encoded)


def test_sparse_header_and_bounds_checked_chunk_iteration():
    image = _sparse_image(
        [
            (CHUNK_TYPE_RAW, 2, b"abcdefgh"),
            (CHUNK_TYPE_FILL, 1, b"\x11\x22\x33\x44"),
            (CHUNK_TYPE_DONT_CARE, 1, b""),
            (CHUNK_TYPE_CRC32, 0, b"\0\0\0\0"),
        ]
    )

    header = parse_sparse_header(image)
    chunks = list(iter_sparse_chunks(image, header))

    assert header.block_size == 4
    assert header.total_blocks == 4
    assert [chunk.type_name for chunk in chunks] == [
        "raw",
        "fill",
        "dont-care",
        "crc32",
    ]
    assert chunks[0].data.tobytes() == b"abcdefgh"
    assert chunks[0].block_start == 0
    assert chunks[2].data_size == 0
    assert parse_sparse_image(image) == header
    assert detect_sparse_image(image)


def test_sparse_rejects_truncated_payload_and_unknown_type():
    valid = _sparse_image([(CHUNK_TYPE_RAW, 1, b"abcd")])
    with pytest.raises(AndroidFormatError, match="truncated"):
        list(iter_sparse_chunks(valid[:-1]))

    unknown = bytearray(_sparse_image([(CHUNK_TYPE_DONT_CARE, 1, b"")]))
    struct.pack_into("<H", unknown, 28, 0xCAFE)
    with pytest.raises(
        UnsupportedAndroidFormatError, match="unsupported Android sparse chunk"
    ):
        list(iter_sparse_chunks(bytes(unknown)))


def _legacy_boot(version):
    size = {0: 1632, 1: BOOT_IMAGE_HEADER_V1_SIZE, 2: BOOT_IMAGE_HEADER_V2_SIZE}[
        version
    ]
    image = bytearray(size)
    image[:8] = ANDROID_BOOT_MAGIC
    struct.pack_into(
        "<9I",
        image,
        8,
        0x100,
        0x80008000,
        0x200,
        0x81000000,
        0,
        0,
        0x82000000,
        4096,
        version,
    )
    struct.pack_into("<I", image, 44, 0x12345678)
    image[48:64] = b"product\0" + b"\0" * 8
    image[64:576] = b"console=ttyS0\0" + b"\0" * (512 - len(b"console=ttyS0\0"))
    struct.pack_into("<8I", image, 576, *range(8))
    if version >= 1:
        struct.pack_into("<IQI", image, 1632, 0x40, 0x123456789ABC, size)
    if version == 2:
        struct.pack_into("<IQ", image, 1648, 0x80, 0x84000000)
    return bytes(image)


@pytest.mark.parametrize("version", [0, 1, 2])
def test_boot_legacy_header_metadata(version):
    header = parse_boot_image_header(_legacy_boot(version))

    assert header.version == version
    assert header.kernel_size == 0x100
    assert header.page_size == 4096
    assert header.name == "product"
    assert header.cmdline == "console=ttyS0"
    assert header.id_words == tuple(range(8))
    if version >= 1:
        assert header.recovery_dtbo_size == 0x40
    if version == 2:
        assert header.dtb_size == 0x80
        assert header.dtb_addr == 0x84000000


def _modern_boot(version):
    size = BOOT_IMAGE_HEADER_V4_SIZE if version == 4 else BOOT_IMAGE_HEADER_V3_SIZE
    image = bytearray(size)
    image[:8] = ANDROID_BOOT_MAGIC
    struct.pack_into("<4I", image, 8, 0x1000, 0x2000, 0x1234, size)
    struct.pack_into("<4I", image, 24, 1, 2, 3, 4)
    struct.pack_into("<I", image, 40, version)
    image[44:1580] = b"androidboot.slot_suffix=_a\0" + b"\0" * (
        1536 - len(b"androidboot.slot_suffix=_a\0")
    )
    if version == 4:
        struct.pack_into("<I", image, 1580, 4096)
    return bytes(image)


@pytest.mark.parametrize("version", [3, 4])
def test_boot_modern_header_metadata(version):
    header = parse_boot_image_header(_modern_boot(version))

    assert header.version == version
    assert header.page_size == 4096
    assert header.header_size == (
        BOOT_IMAGE_HEADER_V4_SIZE if version == 4 else BOOT_IMAGE_HEADER_V3_SIZE
    )
    assert header.cmdline == "androidboot.slot_suffix=_a"
    assert header.reserved == (1, 2, 3, 4)
    assert header.signature_size == (4096 if version == 4 else None)
    assert detect_boot_image(_modern_boot(version))


def test_boot_rejects_unsupported_and_truncated_headers():
    unknown = bytearray(_modern_boot(3))
    struct.pack_into("<I", unknown, 40, 5)
    with pytest.raises(UnsupportedAndroidFormatError, match="version 5"):
        parse_boot_image_header(bytes(unknown))
    with pytest.raises(AndroidFormatError, match="truncated"):
        parse_boot_image_header(_modern_boot(3)[:100])
    assert not detect_boot_image(b"ANDROID!")


def test_optional_tool_discovery_only_uses_path_lookup(monkeypatch):
    seen = []

    def fake_which(name):
        seen.append(name)
        return "/opt/android/" + name if name == "simg2img" else None

    monkeypatch.setattr("mountfile.android.shutil.which", fake_which)
    found = discover_external_tools()

    assert found["simg2img"] == "/opt/android/simg2img"
    assert found["lpunpack"] is None
    assert found["payload_dumper"] is None
    assert "simg2img" in seen
    assert "lpunpack" in seen
    assert "payload_dumper" in seen
