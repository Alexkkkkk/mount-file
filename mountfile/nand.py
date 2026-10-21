# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Safe helpers for raw NAND pages, OOB data, and bad-block tables.

The caller must provide the NAND geometry explicitly.  This module does not
try to infer controller-specific geometry, scrambling, or ECC semantics from a
dump.  Raw pages are represented as ``page data + OOB`` and may optionally be
passed through a caller-supplied decoder implementing :class:`NandPageDecoder`.
No decoder is bundled here because vendor algorithms are not interchangeable.

``block_size`` is the main-area size of one erase block (that is,
``page_size * pages_per_block``); OOB bytes are additional bytes stored for
each page in a raw dump.
"""

from __future__ import annotations

import contextlib
import operator
import os
from dataclasses import dataclass
from typing import BinaryIO, Iterator, Optional, Protocol, Sequence, Tuple, Union


class NandError(ValueError):
    """Base class for invalid NAND geometry, data, or decoder results."""


class GeometryError(NandError):
    """Raised when explicit NAND geometry is missing or inconsistent."""


class NandBoundsError(NandError):
    """Raised when an operation would read outside a complete raw NAND image."""


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    """Return a strict integer, rejecting booleans and lossy coercions."""
    if isinstance(value, bool):
        raise GeometryError("%s must be an integer, not bool" % name)
    try:
        result = operator.index(value)
    except TypeError as exc:
        raise GeometryError("%s must be an integer" % name) from exc
    if positive and result <= 0:
        raise GeometryError("%s must be greater than zero" % name)
    if result < 0:
        raise NandBoundsError("%s must not be negative" % name)
    return result


def _bytes(value: object, name: str) -> bytes:
    """Convert a bytes-like object without accepting text or iterables."""
    if isinstance(value, str):
        raise TypeError("%s must be bytes-like, not str" % name)
    try:
        return memoryview(value).tobytes()  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise TypeError("%s must be bytes-like" % name) from exc


@dataclass(frozen=True)
class NandGeometry:
    """Explicit geometry for a raw NAND dump.

    ``page_size`` and ``block_size`` describe main-area bytes.  ``oob_size``
    describes the extra bytes appended to every page in an interleaved raw
    dump.  A block therefore occupies
    ``pages_per_block * (page_size + oob_size)`` bytes on disk.
    """

    page_size: int
    oob_size: int
    block_size: int

    def __post_init__(self) -> None:
        page_size = _integer(self.page_size, "page_size", positive=True)
        oob_size = _integer(self.oob_size, "oob_size", positive=True)
        block_size = _integer(self.block_size, "block_size", positive=True)
        if block_size % page_size:
            raise GeometryError("block_size must be a multiple of page_size")
        object.__setattr__(self, "page_size", page_size)
        object.__setattr__(self, "oob_size", oob_size)
        object.__setattr__(self, "block_size", block_size)

    @property
    def pages_per_block(self) -> int:
        """Number of main-area pages in one erase block."""
        return self.block_size // self.page_size

    @property
    def raw_page_size(self) -> int:
        """Bytes occupied by one page including its OOB area."""
        return self.page_size + self.oob_size

    @property
    def raw_block_size(self) -> int:
        """Bytes occupied by one block in an interleaved raw dump."""
        return self.pages_per_block * self.raw_page_size

    def page_offset(self, page_index: int) -> int:
        """Return the raw-image offset of ``page_index`` after bounds checks."""
        page_index = _integer(page_index, "page_index")
        return page_index * self.raw_page_size

    def block_offset(self, block_index: int) -> int:
        """Return the raw-image offset of ``block_index`` after bounds checks."""
        block_index = _integer(block_index, "block_index")
        return block_index * self.raw_block_size

    def page_in_block(self, page_index: int) -> int:
        """Return the zero-based page position within its erase block."""
        page_index = _integer(page_index, "page_index")
        return page_index % self.pages_per_block


def parse_geometry(page_size: int, oob_size: int, block_size: int) -> NandGeometry:
    """Validate and construct explicit NAND geometry.

    This named constructor intentionally performs no heuristic detection.
    """
    return NandGeometry(page_size=page_size, oob_size=oob_size, block_size=block_size)


# A descriptive alias for callers that prefer a NAND-specific name.
parse_nand_geometry = parse_geometry


@dataclass(frozen=True)
class NandPage:
    """One raw NAND page split into main data and OOB bytes."""

    page_index: int
    data: bytes
    oob: bytes

    @property
    def raw(self) -> bytes:
        """Return the page in raw-dump order (main data followed by OOB)."""
        return self.data + self.oob

    @property
    def index(self) -> int:
        """Compatibility/readability alias for ``page_index``."""
        return self.page_index


class NandPageDecoder(Protocol):
    """Interface for an optional, caller-provided page decoder.

    Implementations may interpret a controller-specific page, but this package
    deliberately supplies no scrambling or ECC correction implementation.
    A decoder must return a :class:`NandPage` with the same page index and
    geometry-sized ``data`` and ``oob`` fields.
    """

    def decode(self, page: NandPage) -> NandPage:
        """Decode or transform one already-bounds-checked raw page."""


# Short alias useful to applications exposing a generic decoder hook.
PageDecoder = NandPageDecoder


def _validate_page(page: NandPage, geometry: NandGeometry, page_index: int) -> NandPage:
    if not isinstance(page, NandPage):
        raise NandError("decoder must return NandPage")
    if page.page_index != page_index:
        raise NandError("decoder changed page_index")
    data = _bytes(page.data, "decoded data")
    oob = _bytes(page.oob, "decoded OOB")
    if len(data) != geometry.page_size:
        raise NandBoundsError("decoded data has an invalid page size")
    if len(oob) != geometry.oob_size:
        raise NandBoundsError("decoded OOB has an invalid OOB size")
    return NandPage(page_index=page_index, data=data, oob=oob)


def parse_nand_page(
    raw_page: object,
    geometry: NandGeometry,
    page_index: int = 0,
    decoder: Optional[NandPageDecoder] = None,
) -> NandPage:
    """Split one *complete* raw page into data and OOB, optionally via a hook.

    The input must be exactly ``geometry.raw_page_size`` bytes.  Exact-length
    checking avoids silently accepting a truncated page or accidentally
    consuming bytes from the following page.
    """
    if not isinstance(geometry, NandGeometry):
        raise TypeError("geometry must be NandGeometry")
    page_index = _integer(page_index, "page_index")
    raw = _bytes(raw_page, "raw_page")
    if len(raw) != geometry.raw_page_size:
        raise NandBoundsError(
            "raw page has %d bytes; expected exactly %d"
            % (len(raw), geometry.raw_page_size)
        )
    page = NandPage(
        page_index=page_index,
        data=raw[: geometry.page_size],
        oob=raw[geometry.page_size :],
    )
    if decoder is None:
        return page
    decode = getattr(decoder, "decode", None)
    if not callable(decode):
        raise TypeError("decoder must implement decode(page) from NandPageDecoder")
    return _validate_page(decode(page), geometry, page_index)


# Keep the compact name convenient for callers and tests.
parse_page = parse_nand_page


Source = Union[bytes, bytearray, memoryview, BinaryIO, os.PathLike]


@contextlib.contextmanager
def _open_source(source: Source) -> Iterator[Union[memoryview, BinaryIO]]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        yield memoryview(source)
        return
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as handle:
            yield handle
        return
    if not callable(getattr(source, "read", None)) or not callable(
        getattr(source, "seek", None)
    ):
        raise TypeError(
            "source must be bytes-like, a seekable binary stream, or a path"
        )
    yield source  # type: ignore[misc]


def _source_length(source: Union[memoryview, BinaryIO]) -> int:
    if isinstance(source, memoryview):
        return len(source)
    tell = getattr(source, "tell", None)
    if not callable(tell):
        raise NandBoundsError("source must provide tell() for bounds checking")
    try:
        original = tell()
        source.seek(0, os.SEEK_END)
        end = tell()
        source.seek(original, os.SEEK_SET)
    except (OSError, ValueError, TypeError) as exc:
        raise NandBoundsError("source must be seekable for bounds checking") from exc
    if not isinstance(end, int) or end < 0:
        raise NandBoundsError("source reported an invalid size")
    return end


def _read_at(source: Union[memoryview, BinaryIO], offset: int, size: int) -> bytes:
    if offset < 0 or size < 0:
        raise NandBoundsError("negative read range")
    if isinstance(source, memoryview):
        end = offset + size
        if end > len(source):
            raise NandBoundsError("read range exceeds source")
        return source[offset:end].tobytes()

    tell = getattr(source, "tell", None)
    if not callable(tell):
        raise NandBoundsError("source must provide tell() for safe reads")
    try:
        original = tell()
        source.seek(offset, os.SEEK_SET)
        value = source.read(size)
    except (OSError, ValueError, TypeError) as exc:
        raise NandBoundsError("could not read requested source range") from exc
    finally:
        try:
            source.seek(original, os.SEEK_SET)
        except (OSError, ValueError, TypeError, UnboundLocalError):
            # A failed restore cannot make a successful result safe; the
            # length check below still rejects short reads.
            pass
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise NandBoundsError("source read did not return bytes")
    result = _bytes(value, "source read")
    if len(result) != size:
        raise NandBoundsError(
            "short read at offset %d: got %d bytes, expected %d"
            % (offset, len(result), size)
        )
    return result


def read_nand_page(
    source: Source,
    geometry: NandGeometry,
    page_index: int,
    decoder: Optional[NandPageDecoder] = None,
) -> NandPage:
    """Read one complete raw page from bytes, a path, or a seekable stream."""
    if not isinstance(geometry, NandGeometry):
        raise TypeError("geometry must be NandGeometry")
    page_index = _integer(page_index, "page_index")
    with _open_source(source) as handle:
        offset = geometry.page_offset(page_index)
        total = _source_length(handle)
        if offset + geometry.raw_page_size > total:
            raise NandBoundsError("page range exceeds source")
        raw = _read_at(handle, offset, geometry.raw_page_size)
    return parse_nand_page(raw, geometry, page_index=page_index, decoder=decoder)


def iter_nand_pages(
    source: Source,
    geometry: NandGeometry,
    start_page: int = 0,
    page_count: Optional[int] = None,
    decoder: Optional[NandPageDecoder] = None,
) -> Iterator[NandPage]:
    """Yield complete pages after validating image and requested bounds.

    A partial trailing page is rejected instead of being silently discarded.
    """
    if not isinstance(geometry, NandGeometry):
        raise TypeError("geometry must be NandGeometry")
    start_page = _integer(start_page, "start_page")
    if page_count is not None:
        page_count = _integer(page_count, "page_count")

    def _pages() -> Iterator[NandPage]:
        with _open_source(source) as handle:
            total = _source_length(handle)
            if total % geometry.raw_page_size:
                raise NandBoundsError("source size is not a whole number of raw pages")
            available = total // geometry.raw_page_size
            if start_page > available:
                raise NandBoundsError("start_page exceeds source")
            count = available - start_page if page_count is None else page_count
            if start_page + count > available:
                raise NandBoundsError("requested pages exceed source")
            for index in range(start_page, start_page + count):
                raw = _read_at(
                    handle,
                    geometry.page_offset(index),
                    geometry.raw_page_size,
                )
                yield parse_nand_page(raw, geometry, page_index=index, decoder=decoder)

    return _pages()


@dataclass(frozen=True)
class BadBlockMarker:
    """One marker observation used to build a :class:`BadBlockTable`."""

    block_index: int
    page_index: int
    page_in_block: int
    oob_offset: int
    value: bytes

    @property
    def is_bad(self) -> bool:
        """Whether this marker differs from the erased value ``0xFF``."""
        return any(byte != 0xFF for byte in self.value)


@dataclass(frozen=True)
class BlockStatus:
    """Immutable bad/good status and marker evidence for one erase block."""

    block_index: int
    bad: bool
    markers: Tuple[BadBlockMarker, ...]

    @property
    def is_bad(self) -> bool:
        return self.bad


@dataclass(frozen=True)
class BadBlockTable:
    """Immutable bad-block table with marker evidence, not a vendor BBT format."""

    geometry: NandGeometry
    blocks: Tuple[BlockStatus, ...]

    @property
    def entries(self) -> Tuple[BlockStatus, ...]:
        """Alias for callers that use the term ``entries`` for BBT rows."""
        return self.blocks

    @property
    def block_count(self) -> int:
        return len(self.blocks)

    @property
    def bad_blocks(self) -> Tuple[int, ...]:
        return tuple(status.block_index for status in self.blocks if status.bad)

    @property
    def good_blocks(self) -> Tuple[int, ...]:
        return tuple(status.block_index for status in self.blocks if not status.bad)

    def is_bad(self, block_index: int) -> bool:
        """Return status for a scanned block; reject blocks outside this table."""
        block_index = _integer(block_index, "block_index")
        for status in self.blocks:
            if status.block_index == block_index:
                return status.bad
        raise NandBoundsError("block_index was not included in this table")

    def status(self, block_index: int) -> BlockStatus:
        """Return marker evidence for a scanned block."""
        block_index = _integer(block_index, "block_index")
        for status in self.blocks:
            if status.block_index == block_index:
                return status
        raise NandBoundsError("block_index was not included in this table")

    def __contains__(self, block_index: object) -> bool:
        if isinstance(block_index, bool):
            return False
        try:
            index = operator.index(block_index)
        except TypeError:
            return False
        return any(status.block_index == index for status in self.blocks)


def _marker_pages(geometry: NandGeometry, pages: Sequence[int]) -> Tuple[int, ...]:
    try:
        result = tuple(_integer(page, "marker page") for page in pages)
    except TypeError as exc:
        raise GeometryError("marker_pages must be a sequence of page indexes") from exc
    if not result:
        raise GeometryError("marker_pages must not be empty")
    if len(set(result)) != len(result):
        raise GeometryError("marker_pages must not contain duplicates")
    for page in result:
        if page >= geometry.pages_per_block:
            raise GeometryError("marker page exceeds pages_per_block")
    return result


def scan_bad_block_markers(
    source: Source,
    geometry: NandGeometry,
    *,
    start_block: int = 0,
    block_count: Optional[int] = None,
    marker_offset: int = 0,
    marker_length: int = 1,
    marker_pages: Sequence[int] = (0, 1),
) -> BadBlockTable:
    """Scan configurable OOB marker bytes and return an immutable BBT.

    By default the first marker byte on pages 0 and 1 is examined, matching a
    common raw-NAND layout.  This is a scanning policy, not a claim about every
    controller; callers must select different pages/offsets when their device
    specifies another layout.  A marker is considered good only when every
    requested byte is ``0xFF``.

    The source must contain complete interleaved raw blocks.  Partial images,
    invalid marker ranges, and requested blocks outside the source raise
    :class:`NandBoundsError` or :class:`GeometryError` rather than being
    truncated or guessed.
    """
    if not isinstance(geometry, NandGeometry):
        raise TypeError("geometry must be NandGeometry")
    start_block = _integer(start_block, "start_block")
    if block_count is not None:
        block_count = _integer(block_count, "block_count")
    marker_offset = _integer(marker_offset, "marker_offset")
    marker_length = _integer(marker_length, "marker_length", positive=True)
    if marker_offset + marker_length > geometry.oob_size:
        raise GeometryError("marker range exceeds oob_size")
    pages = _marker_pages(geometry, marker_pages)

    with _open_source(source) as handle:
        total = _source_length(handle)
        if total % geometry.raw_block_size:
            raise NandBoundsError("source size is not a whole number of raw blocks")
        available = total // geometry.raw_block_size
        if start_block > available:
            raise NandBoundsError("start_block exceeds source")
        count = available - start_block if block_count is None else block_count
        if start_block + count > available:
            raise NandBoundsError("requested blocks exceed source")

        statuses = []
        for block_index in range(start_block, start_block + count):
            markers = []
            for page_in_block in pages:
                page_index = block_index * geometry.pages_per_block + page_in_block
                offset = (
                    geometry.page_offset(page_index)
                    + geometry.page_size
                    + marker_offset
                )
                value = _read_at(handle, offset, marker_length)
                markers.append(
                    BadBlockMarker(
                        block_index=block_index,
                        page_index=page_index,
                        page_in_block=page_in_block,
                        oob_offset=marker_offset,
                        value=value,
                    )
                )
            statuses.append(
                BlockStatus(
                    block_index=block_index,
                    bad=any(marker.is_bad for marker in markers),
                    markers=tuple(markers),
                )
            )

    return BadBlockTable(geometry=geometry, blocks=tuple(statuses))


# Conventional shorter spelling for applications.
scan_bad_blocks = scan_bad_block_markers


__all__ = [
    "BadBlockMarker",
    "BadBlockTable",
    "BlockStatus",
    "GeometryError",
    "NandBoundsError",
    "NandError",
    "NandGeometry",
    "NandPage",
    "NandPageDecoder",
    "PageDecoder",
    "iter_nand_pages",
    "parse_geometry",
    "parse_nand_geometry",
    "parse_nand_page",
    "parse_page",
    "read_nand_page",
    "scan_bad_block_markers",
    "scan_bad_blocks",
]
