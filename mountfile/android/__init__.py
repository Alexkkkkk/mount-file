# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License; see the LICENSE file for details.
"""Safe metadata parsers for Android sparse and boot images.

This module intentionally does not unpack, mount, or execute anything.  It only
validates and reads bounded headers.  Sparse chunks are exposed as metadata
plus a zero-copy :class:`memoryview`; callers that need conversion or logical
image extraction must explicitly invoke a separately installed tool.
"""

from __future__ import annotations

import operator
import shutil
import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple, Union

__all__ = [
    "ANDROID_BOOT_MAGIC",
    "BOOT_IMAGE_HEADER_V1_SIZE",
    "BOOT_IMAGE_HEADER_V2_SIZE",
    "BOOT_IMAGE_HEADER_V3_SIZE",
    "BOOT_IMAGE_HEADER_V4_SIZE",
    "CHUNK_TYPE_CRC32",
    "CHUNK_TYPE_DONT_CARE",
    "CHUNK_TYPE_FILL",
    "CHUNK_TYPE_RAW",
    "SPARSE_CHUNK_HEADER_SIZE",
    "SPARSE_HEADER_SIZE",
    "SPARSE_MAGIC",
    "AndroidFormatError",
    "BootImageHeader",
    "ExternalToolMap",
    "SparseChunk",
    "SparseImageHeader",
    "UnsupportedAndroidFormatError",
    "UnsupportedFormatError",
    "detect_boot_image",
    "detect_sparse_image",
    "discover_external_tools",
    "find_external_tools",
    "is_boot_image",
    "is_sparse_image",
    "iter_sparse_chunks",
    "parse_boot_image_header",
    "parse_sparse_header",
    "parse_sparse_image",
]

# Android sparse image constants (all integer fields are little-endian).
SPARSE_MAGIC = 0xED26FF3A
SPARSE_HEADER_SIZE = 28
SPARSE_CHUNK_HEADER_SIZE = 12
CHUNK_TYPE_RAW = 0xCAC1
CHUNK_TYPE_FILL = 0xCAC2
CHUNK_TYPE_DONT_CARE = 0xCAC3
CHUNK_TYPE_CRC32 = 0xCAC4

_CHUNK_NAMES = {
    CHUNK_TYPE_RAW: "raw",
    CHUNK_TYPE_FILL: "fill",
    CHUNK_TYPE_DONT_CARE: "dont-care",
    CHUNK_TYPE_CRC32: "crc32",
}

# Header sizes defined by AOSP mkbootimg.  Version zero is the packed v0
# structure size (it has no explicit header_size field).
ANDROID_BOOT_MAGIC = b"ANDROID!"
_BOOT_V0_SIZE = 1632
BOOT_IMAGE_HEADER_V1_SIZE = 1648
BOOT_IMAGE_HEADER_V2_SIZE = 1660
BOOT_IMAGE_HEADER_V3_SIZE = 1580
BOOT_IMAGE_HEADER_V4_SIZE = 1584

_EXTERNAL_TOOL_CANDIDATES = {
    "simg2img": ("simg2img",),
    "lpunpack": ("lpunpack",),
    "payload_dumper": (
        "payload_dumper",
        "payload-dumper",
        "payload-dumper-go",
        "payload_dumper.py",
    ),
}
ExternalToolMap = Dict[str, Optional[str]]


class AndroidFormatError(ValueError):
    """Raised when recognized Android bytes are truncated or inconsistent."""


class UnsupportedAndroidFormatError(AndroidFormatError):
    """Raised when bytes identify a format/version this module does not support."""


# A short compatibility name for callers that use the generic terminology.
UnsupportedFormatError = UnsupportedAndroidFormatError


@dataclass(frozen=True)
class SparseImageHeader:
    """Validated Android sparse image header metadata."""

    magic: int
    major_version: int
    minor_version: int
    file_hdr_sz: int
    chunk_hdr_sz: int
    blk_sz: int
    total_blks: int
    total_chunks: int
    image_checksum: int

    @property
    def file_header_size(self) -> int:
        """Descriptive alias for the on-disk ``file_hdr_sz`` field."""

        return self.file_hdr_sz

    @property
    def chunk_header_size(self) -> int:
        """Descriptive alias for the on-disk ``chunk_hdr_sz`` field."""

        return self.chunk_hdr_sz

    @property
    def block_size(self) -> int:
        """Descriptive alias for the on-disk ``blk_sz`` field."""

        return self.blk_sz

    @property
    def total_blocks(self) -> int:
        """Descriptive alias for the on-disk ``total_blks`` field."""

        return self.total_blks


@dataclass(frozen=True)
class SparseChunk:
    """One validated sparse chunk.

    ``data`` is the chunk payload, not the 12-byte (or extended) chunk
    header.  It is a zero-copy view into the bytes passed to
    :func:`iter_sparse_chunks`.
    """

    index: int
    offset: int
    chunk_type: int
    chunk_sz: int
    total_sz: int
    data_offset: int
    data_size: int
    block_start: int
    block_count: int
    data: memoryview

    @property
    def type_name(self) -> str:
        return _CHUNK_NAMES[self.chunk_type]

    @property
    def kind(self) -> str:
        """Alias for :attr:`type_name`."""

        return self.type_name

    @property
    def chunk_size(self) -> int:
        return self.chunk_sz

    @property
    def total_size(self) -> int:
        return self.total_sz

    @property
    def payload(self) -> memoryview:
        """Alias for the payload view in :attr:`data`."""

        return self.data

    @property
    def data_end(self) -> int:
        return self.data_offset + self.data_size


@dataclass(frozen=True)
class BootImageHeader:
    """Validated metadata common to Android boot image header versions 0–4.

    Legacy-only fields are ``None`` for v3/v4, and v3+ fields are ``None`` for
    v0.  ``name`` and ``cmdline`` are NUL-terminated UTF-8 text with invalid
    bytes replaced; the corresponding ``*_bytes`` properties expose the raw
    fixed-width fields.
    """

    version: int
    header_size: int
    kernel_size: int
    ramdisk_size: int
    os_version: int
    page_size: int
    kernel_addr: Optional[int]
    ramdisk_addr: Optional[int]
    second_size: Optional[int]
    second_addr: Optional[int]
    tags_addr: Optional[int]
    name: str
    cmdline: str
    id_words: Tuple[int, ...]
    recovery_dtbo_size: Optional[int]
    recovery_dtbo_offset: Optional[int]
    dtb_size: Optional[int]
    dtb_addr: Optional[int]
    signature_size: Optional[int]
    reserved: Tuple[int, ...]
    _name_bytes: bytes = field(default=b"", repr=False, compare=False)
    _cmdline_bytes: bytes = field(default=b"", repr=False, compare=False)

    @property
    def header_version(self) -> int:
        """Alias for the version encoded in the header."""

        return self.version

    @property
    def name_bytes(self) -> bytes:
        return self._name_bytes

    @property
    def cmdline_bytes(self) -> bytes:
        return self._cmdline_bytes

    @property
    def id_bytes(self) -> bytes:
        return struct.pack("<8I", *self.id_words) if self.id_words else b""


def _as_bytes(data: Union[bytes, bytearray, memoryview]) -> bytes:
    """Return a stable byte string, rejecting objects with surprising I/O."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("image data must be bytes-like")
    try:
        return bytes(data)
    except (TypeError, ValueError) as exc:
        raise TypeError("image data must be a contiguous bytes-like object") from exc


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError("%s must be an integer, not bool" % name)
    try:
        result = operator.index(value)
    except TypeError as exc:
        raise TypeError("%s must be an integer" % name) from exc
    if result < 0:
        raise ValueError("%s must be non-negative" % name)
    return result


def _require(data: bytes, offset: int, length: int, what: str) -> None:
    if offset < 0 or length < 0 or offset > len(data) - length:
        available = max(0, len(data) - offset)
        raise AndroidFormatError(
            "%s is truncated: need %d bytes at offset %d, only %d available"
            % (what, length, offset, available)
        )


def parse_sparse_header(data: Union[bytes, bytearray, memoryview]) -> SparseImageHeader:
    """Parse and validate an Android sparse image file header.

    This function validates only the file header.  Call
    :func:`iter_sparse_chunks` to validate and inspect every chunk.
    """

    blob = _as_bytes(data)
    _require(blob, 0, SPARSE_HEADER_SIZE, "Android sparse image header")
    fields = struct.unpack_from("<IHHHHIIII", blob, 0)
    (
        magic,
        major_version,
        minor_version,
        file_hdr_sz,
        chunk_hdr_sz,
        blk_sz,
        total_blks,
        total_chunks,
        image_checksum,
    ) = fields
    if magic != SPARSE_MAGIC:
        raise UnsupportedAndroidFormatError(
            "unsupported Android sparse image magic 0x%08x (expected 0x%08x)"
            % (magic, SPARSE_MAGIC)
        )
    if major_version != 1:
        raise UnsupportedAndroidFormatError(
            "unsupported Android sparse image major version %d (supported: 1)"
            % major_version
        )
    if file_hdr_sz < SPARSE_HEADER_SIZE:
        raise AndroidFormatError(
            "invalid Android sparse file header size %d (minimum %d)"
            % (file_hdr_sz, SPARSE_HEADER_SIZE)
        )
    if chunk_hdr_sz < SPARSE_CHUNK_HEADER_SIZE:
        raise AndroidFormatError(
            "invalid Android sparse chunk header size %d (minimum %d)"
            % (chunk_hdr_sz, SPARSE_CHUNK_HEADER_SIZE)
        )
    if blk_sz == 0:
        raise AndroidFormatError("invalid Android sparse block size 0")
    _require(blob, 0, file_hdr_sz, "Android sparse image file header")
    return SparseImageHeader(*fields)


def iter_sparse_chunks(
    data: Union[bytes, bytearray, memoryview],
    header: Optional[SparseImageHeader] = None,
) -> Iterator[SparseChunk]:
    """Yield validated sparse chunks without reading beyond ``data``.

    Chunk counts, block counts, payload lengths, and every offset are checked
    before a chunk is yielded.  Unknown chunk types, arithmetic inconsistencies
    and truncated input raise :class:`AndroidFormatError` (or its unsupported
    format subclass).  The generator does not allocate a logical image.
    """

    blob = _as_bytes(data)
    parsed = parse_sparse_header(blob) if header is None else header
    if not isinstance(parsed, SparseImageHeader):
        raise TypeError("header must be a SparseImageHeader")
    offset = parsed.file_hdr_sz
    block_start = 0
    for index in range(parsed.total_chunks):
        _require(blob, offset, parsed.chunk_hdr_sz, "Android sparse chunk header")
        chunk_type, reserved, chunk_sz, total_sz = struct.unpack_from(
            "<HHII", blob, offset
        )
        if reserved != 0:
            raise AndroidFormatError(
                "invalid Android sparse chunk %d: reserved field is non-zero" % index
            )
        if total_sz < parsed.chunk_hdr_sz:
            raise AndroidFormatError(
                "invalid Android sparse chunk %d size %d (header size %d)"
                % (index, total_sz, parsed.chunk_hdr_sz)
            )
        if total_sz > len(blob) - offset:
            raise AndroidFormatError(
                "Android sparse chunk %d is truncated: need %d bytes at offset %d"
                % (index, total_sz, offset)
            )
        if chunk_type not in _CHUNK_NAMES:
            raise UnsupportedAndroidFormatError(
                "unsupported Android sparse chunk type 0x%04x at chunk %d"
                % (chunk_type, index)
            )

        payload_offset = offset + parsed.chunk_hdr_sz
        payload_size = total_sz - parsed.chunk_hdr_sz
        if chunk_type == CHUNK_TYPE_RAW:
            block_count = chunk_sz
            expected_payload = chunk_sz * parsed.blk_sz
            if payload_size != expected_payload:
                raise AndroidFormatError(
                    "invalid raw sparse chunk %d payload size %d (expected %d)"
                    % (index, payload_size, expected_payload)
                )
        elif chunk_type == CHUNK_TYPE_FILL:
            block_count = chunk_sz
            if payload_size != 4:
                raise AndroidFormatError(
                    "invalid fill sparse chunk %d payload size %d (expected 4)"
                    % (index, payload_size)
                )
        elif chunk_type == CHUNK_TYPE_DONT_CARE:
            block_count = chunk_sz
            if payload_size != 0:
                raise AndroidFormatError(
                    "invalid dont-care sparse chunk %d payload size %d (expected 0)"
                    % (index, payload_size)
                )
        else:  # CHUNK_TYPE_CRC32
            block_count = 0
            if chunk_sz != 0:
                raise AndroidFormatError(
                    "invalid crc32 sparse chunk %d block count %d (expected 0)"
                    % (index, chunk_sz)
                )
            if payload_size != 4:
                raise AndroidFormatError(
                    "invalid crc32 sparse chunk %d payload size %d (expected 4)"
                    % (index, payload_size)
                )

        if block_count > parsed.total_blks - block_start:
            raise AndroidFormatError(
                "sparse chunk %d exceeds declared total blocks (%d + %d > %d)"
                % (index, block_start, block_count, parsed.total_blks)
            )
        yield SparseChunk(
            index=index,
            offset=offset,
            chunk_type=chunk_type,
            chunk_sz=chunk_sz,
            total_sz=total_sz,
            data_offset=payload_offset,
            data_size=payload_size,
            block_start=block_start,
            block_count=block_count,
            data=memoryview(blob)[payload_offset : payload_offset + payload_size],
        )
        block_start += block_count
        offset += total_sz

    if block_start != parsed.total_blks:
        raise AndroidFormatError(
            "sparse chunks cover %d blocks, header declares %d"
            % (block_start, parsed.total_blks)
        )


def parse_sparse_image(
    data: Union[bytes, bytearray, memoryview],
) -> SparseImageHeader:
    """Validate the sparse header and all chunks, returning header metadata."""

    header = parse_sparse_header(data)
    for _ in iter_sparse_chunks(data, header):
        pass
    return header


def detect_sparse_image(data: Union[bytes, bytearray, memoryview]) -> bool:
    """Return whether ``data`` starts with a supported, structurally valid header."""

    try:
        parse_sparse_header(data)
    except (AndroidFormatError, TypeError):
        return False
    return True


is_sparse_image = detect_sparse_image


def _cstring(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", errors="replace")


def _legacy_page_size(page_size: int) -> None:
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise AndroidFormatError(
            "invalid legacy Android boot image page size %d (expected a power of two from 512 to 65536)"
            % page_size
        )


def parse_boot_image_header(
    data: Union[bytes, bytearray, memoryview], offset: int = 0
) -> BootImageHeader:
    """Parse Android boot image header metadata for versions 0 through 4.

    Only the header is required; kernel, ramdisk, DTB and signature payloads
    are intentionally not read or verified by this metadata parser.
    """

    blob = _as_bytes(data)
    base = _nonnegative_int(offset, "offset")
    _require(blob, base, 8, "Android boot image magic")
    magic = blob[base : base + 8]
    if magic != ANDROID_BOOT_MAGIC:
        raise UnsupportedAndroidFormatError(
            "unsupported Android boot image magic %r (expected %r)"
            % (magic, ANDROID_BOOT_MAGIC)
        )
    _require(blob, base, 44, "Android boot image version fields")
    version = struct.unpack_from("<I", blob, base + 40)[0]
    if version > 4:
        raise UnsupportedAndroidFormatError(
            "unsupported Android boot image header version %d (supported: 0 through 4)"
            % version
        )

    if version <= 2:
        minimum_size = {
            0: _BOOT_V0_SIZE,
            1: BOOT_IMAGE_HEADER_V1_SIZE,
            2: BOOT_IMAGE_HEADER_V2_SIZE,
        }[version]
        _require(blob, base, minimum_size, "Android boot image v%d header" % version)
        values = struct.unpack_from("<9I", blob, base + 8)
        (
            kernel_size,
            kernel_addr,
            ramdisk_size,
            ramdisk_addr,
            second_size,
            second_addr,
            tags_addr,
            page_size,
            _header_version,
        ) = values
        os_version = struct.unpack_from("<I", blob, base + 44)[0]
        name_bytes = blob[base + 48 : base + 64]
        cmdline_bytes = blob[base + 64 : base + 576]
        id_words = struct.unpack_from("<8I", blob, base + 576)
        recovery_dtbo_size = None
        recovery_dtbo_offset = None
        dtb_size = None
        dtb_addr = None
        declared_header_size = _BOOT_V0_SIZE
        if version >= 1:
            recovery_dtbo_size = struct.unpack_from("<I", blob, base + 1632)[0]
            recovery_dtbo_offset = struct.unpack_from("<Q", blob, base + 1636)[0]
            declared_header_size = struct.unpack_from("<I", blob, base + 1644)[0]
            if declared_header_size < minimum_size:
                raise AndroidFormatError(
                    "invalid Android boot image v%d header size %d (minimum %d)"
                    % (version, declared_header_size, minimum_size)
                )
            _require(
                blob, base, declared_header_size, "declared Android boot image header"
            )
        if version == 2:
            dtb_size = struct.unpack_from("<I", blob, base + 1648)[0]
            dtb_addr = struct.unpack_from("<Q", blob, base + 1652)[0]
        _legacy_page_size(page_size)
        return BootImageHeader(
            version=version,
            header_size=declared_header_size,
            kernel_size=kernel_size,
            ramdisk_size=ramdisk_size,
            os_version=os_version,
            page_size=page_size,
            kernel_addr=kernel_addr,
            ramdisk_addr=ramdisk_addr,
            second_size=second_size,
            second_addr=second_addr,
            tags_addr=tags_addr,
            name=_cstring(name_bytes),
            cmdline=_cstring(cmdline_bytes),
            id_words=tuple(id_words),
            recovery_dtbo_size=recovery_dtbo_size,
            recovery_dtbo_offset=recovery_dtbo_offset,
            dtb_size=dtb_size,
            dtb_addr=dtb_addr,
            signature_size=None,
            reserved=(),
            _name_bytes=name_bytes,
            _cmdline_bytes=cmdline_bytes,
        )

    minimum_size = (
        BOOT_IMAGE_HEADER_V4_SIZE if version == 4 else BOOT_IMAGE_HEADER_V3_SIZE
    )
    _require(blob, base, minimum_size, "Android boot image v%d header" % version)
    kernel_size, ramdisk_size, os_version, declared_header_size = struct.unpack_from(
        "<4I", blob, base + 8
    )
    reserved = struct.unpack_from("<4I", blob, base + 24)
    cmdline_bytes = blob[base + 44 : base + 1580]
    signature_size = (
        struct.unpack_from("<I", blob, base + 1580)[0] if version == 4 else None
    )
    if declared_header_size < minimum_size:
        raise AndroidFormatError(
            "invalid Android boot image v%d header size %d (minimum %d)"
            % (version, declared_header_size, minimum_size)
        )
    _require(blob, base, declared_header_size, "declared Android boot image header")
    return BootImageHeader(
        version=version,
        header_size=declared_header_size,
        kernel_size=kernel_size,
        ramdisk_size=ramdisk_size,
        os_version=os_version,
        page_size=4096,
        kernel_addr=None,
        ramdisk_addr=None,
        second_size=None,
        second_addr=None,
        tags_addr=None,
        name="",
        cmdline=_cstring(cmdline_bytes),
        id_words=(),
        recovery_dtbo_size=None,
        recovery_dtbo_offset=None,
        dtb_size=None,
        dtb_addr=None,
        signature_size=signature_size,
        reserved=tuple(reserved),
        _name_bytes=b"",
        _cmdline_bytes=cmdline_bytes,
    )


def detect_boot_image(
    data: Union[bytes, bytearray, memoryview], offset: int = 0
) -> bool:
    """Return whether ``data`` contains a supported, structurally valid boot header."""

    try:
        parse_boot_image_header(data, offset=offset)
    except (AndroidFormatError, TypeError, ValueError):
        return False
    return True


is_boot_image = detect_boot_image


def discover_external_tools(
    names: Optional[Sequence[str]] = None,
) -> ExternalToolMap:
    """Find optional Android tools on ``PATH`` without executing them.

    The returned mapping contains the first executable path found for each
    requested logical name, or ``None`` when absent.  Discovery is deliberately
    limited to :func:`shutil.which`; it never invokes a tool or imports a
    third-party payload dumper.
    """

    requested = tuple(_EXTERNAL_TOOL_CANDIDATES) if names is None else tuple(names)
    result: ExternalToolMap = {}
    for name in requested:
        if name not in _EXTERNAL_TOOL_CANDIDATES:
            raise ValueError(
                "unknown optional Android tool %r (supported: %s)"
                % (name, ", ".join(_EXTERNAL_TOOL_CANDIDATES))
            )
        result[name] = next(
            (
                path
                for candidate in _EXTERNAL_TOOL_CANDIDATES[name]
                if (path := shutil.which(candidate))
            ),
            None,
        )
    return result


find_external_tools = discover_external_tools
