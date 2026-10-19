# SPDX-License-Identifier: MIT
"""Read-only signature scanning over caller-supplied unallocated ranges.

The scanner in this module is deliberately independent from
:mod:`mountfile.unallocated`.  The latter persists hashes and coverage in
SQLite; this module only reads bytes from ranges supplied by the caller and
returns signature observations.  It does not discover allocation state,
carve files, or write an output file.

Ranges are half-open byte intervals, ``[offset, offset + length)``.  A match
is reported only when the complete magic value fits inside one of the supplied
ranges, and its ``offset`` is an absolute image offset.
"""

from __future__ import annotations

import operator
import os
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence


_DEFAULT_CHUNK_SIZE = 1 << 20
_MAX_CHUNK_SIZE = 16 << 20
_MAX_SIGNATURES = 256
_MAX_SIGNATURE_LENGTH = 1 << 20
_MAX_RANGES = 1_000_000


class InvalidRange(ValueError):
    """Raised when a supplied range is malformed or outside the image."""


@dataclass(frozen=True)
class ByteRange:
    """A half-open image range ``[offset, offset + length)``."""

    offset: int
    length: int

    @property
    def end(self) -> int:
        """Return the exclusive absolute end offset."""

        return self.offset + self.length


@dataclass(frozen=True)
class FileSignature:
    """One magic prefix recognised by :func:`scan_unallocated`.

    ``name`` is a stable, lower-case identifier for built-in signatures.
    Custom names are retained as supplied (apart from surrounding whitespace).
    """

    name: str
    magic: bytes
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("signature name must be a non-empty string")
        try:
            magic = memoryview(self.magic).cast("B").tobytes()
        except (TypeError, ValueError) as exc:
            raise TypeError("signature magic must be bytes-like") from exc
        if not magic:
            raise ValueError("signature magic must not be empty")
        if len(magic) > _MAX_SIGNATURE_LENGTH:
            raise ValueError("signature magic is too large")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "magic", magic)
        if not isinstance(self.description, str):
            raise TypeError("signature description must be a string")

    @property
    def length(self) -> int:
        """Length of the magic prefix in bytes."""

        return len(self.magic)

    @property
    def signature(self) -> bytes:
        """Alias for :attr:`magic` for callers using signature terminology."""

        return self.magic


@dataclass(frozen=True)
class SignatureHit:
    """One recognised signature at an absolute image offset."""

    offset: int
    signature: str
    length: int
    magic: bytes = b""
    description: str = ""

    @property
    def absolute_offset(self) -> int:
        """Alias for the absolute image offset."""

        return self.offset

    @property
    def name(self) -> str:
        """Alias for the machine-readable signature name."""

        return self.signature

    @property
    def file_type(self) -> str:
        """Alias useful to callers displaying a detected file type."""

        return self.signature

    @property
    def end(self) -> int:
        """Exclusive offset immediately after the matched magic."""

        return self.offset + self.length

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation without image bytes."""

        return {
            "offset": self.offset,
            "absolute_offset": self.offset,
            "signature": self.signature,
            "name": self.signature,
            "length": self.length,
            "description": self.description,
        }

    def __getitem__(self, key: str) -> Any:
        """Provide lightweight mapping-style access for report builders."""

        if key in ("offset", "absolute_offset"):
            return self.offset
        if key in ("signature", "name", "file_type"):
            return self.signature
        if key == "length":
            return self.length
        if key == "magic":
            return self.magic
        if key == "description":
            return self.description
        raise KeyError(key)


# The identifiers intentionally match the lower-case format keys used by the
# rest of mountfile.  Several container variants share one useful identifier.
COMMON_SIGNATURES = (
    FileSignature("png", b"\x89PNG\r\n\x1a\n", "PNG image"),
    FileSignature("jpeg", b"\xff\xd8\xff", "JPEG image"),
    FileSignature("gif", b"GIF87a", "GIF image"),
    FileSignature("gif", b"GIF89a", "GIF image"),
    FileSignature("pdf", b"%PDF-", "PDF document"),
    FileSignature("zip", b"PK\x03\x04", "ZIP archive"),
    FileSignature("zip", b"PK\x05\x06", "empty ZIP archive"),
    FileSignature("zip", b"PK\x07\x08", "spanned ZIP archive"),
    FileSignature("gzip", b"\x1f\x8b\x08", "GZIP stream"),
    FileSignature("bzip2", b"BZh", "BZip2 stream"),
    FileSignature("xz", b"\xfd7zXZ\x00", "XZ stream"),
    FileSignature("7z", b"7z\xbc\xaf\x27\x1c", "7-Zip archive"),
    FileSignature("rar", b"Rar!\x1a\x07\x00", "RAR archive"),
    FileSignature("rar", b"Rar!\x1a\x07\x01\x00", "RAR archive"),
    FileSignature("ole", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "OLE compound document"),
    FileSignature("rtf", b"{\\rtf", "Rich Text Format document"),
    FileSignature("sqlite", b"SQLite format 3\x00", "SQLite database"),
    FileSignature("elf", b"\x7fELF", "ELF executable"),
    FileSignature("pe", b"MZ", "DOS/PE executable"),
    FileSignature("java", b"\xca\xfe\xba\xbe", "Java class file"),
    FileSignature("mach-o", b"\xfe\xed\xfa\xce", "Mach-O executable"),
    FileSignature("mach-o", b"\xce\xfa\xed\xfe", "Mach-O executable"),
    FileSignature("mach-o-64", b"\xfe\xed\xfa\xcf", "64-bit Mach-O executable"),
    FileSignature("mach-o-64", b"\xcf\xfa\xed\xfe", "64-bit Mach-O executable"),
    FileSignature("bmp", b"BM", "BMP image"),
    FileSignature("tiff", b"II*\x00", "TIFF image"),
    FileSignature("tiff", b"MM\x00*", "TIFF image"),
    FileSignature("riff", b"RIFF", "RIFF container (often WebP/WAV/AVI)"),
    FileSignature("mp3", b"ID3", "MP3 audio"),
    FileSignature("flac", b"fLaC", "FLAC audio"),
    FileSignature("iso9660", b"CD001", "ISO 9660 volume descriptor"),
    FileSignature("android-boot", b"ANDROID!", "Android boot image"),
)

# Readable aliases for callers that prefer one of these names.
DEFAULT_SIGNATURES = COMMON_SIGNATURES
SIGNATURES = COMMON_SIGNATURES


def _as_nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise TypeError("%s must be an integer, not bool" % name)
    try:
        result = operator.index(value)
    except TypeError as exc:
        raise TypeError("%s must be an integer" % name) from exc
    if result < 0:
        raise ValueError("%s must be non-negative" % name)
    return result


def _as_positive_int(value: Any, name: str) -> int:
    result = _as_nonnegative_int(value, name)
    if result == 0:
        raise ValueError("%s must be greater than zero" % name)
    return result


def _iter_range_items(ranges: Any) -> Iterator[Any]:
    """Accept one ``(offset, length)`` pair as a convenience."""

    if isinstance(ranges, ByteRange) or isinstance(ranges, Mapping):
        yield ranges
        return
    if isinstance(ranges, (tuple, list)) and len(ranges) == 2:
        try:
            operator.index(ranges[0])
            operator.index(ranges[1])
        except TypeError:
            pass
        else:
            yield ranges
            return
    try:
        yield from iter(ranges)
    except TypeError as exc:
        raise TypeError("ranges must be an iterable of (offset, length) pairs") from exc


def _coerce_range(item: Any) -> ByteRange:
    if isinstance(item, ByteRange):
        offset, length = item.offset, item.length
    elif isinstance(item, Mapping):
        try:
            offset, length = item["offset"], item["length"]
        except KeyError as exc:
            raise TypeError("range mappings require offset and length") from exc
    else:
        try:
            offset, length = item
        except (TypeError, ValueError) as exc:
            raise TypeError("each range must be a pair (offset, length)") from exc
    try:
        offset = _as_nonnegative_int(offset, "range offset")
        length = _as_positive_int(length, "range length")
    except (TypeError, ValueError) as exc:
        raise InvalidRange(str(exc)) from exc
    return ByteRange(offset, length)


def _normalise_ranges(ranges: Any, image_size: int) -> tuple[ByteRange, ...]:
    found: list[ByteRange] = []
    for number, item in enumerate(_iter_range_items(ranges)):
        if number >= _MAX_RANGES:
            raise InvalidRange("too many ranges")
        current = _coerce_range(item)
        if current.offset > image_size:
            raise InvalidRange("range offset is outside the image")
        if current.length > image_size - current.offset:
            raise InvalidRange("range extends beyond the image")
        found.append(current)

    # Union overlaps and adjacency.  This prevents duplicate hits while still
    # never inventing bytes outside ranges supplied by the caller.
    found.sort(key=lambda item: (item.offset, item.end))
    merged: list[ByteRange] = []
    for current in found:
        if not merged or current.offset > merged[-1].end:
            merged.append(current)
            continue
        previous = merged[-1]
        merged[-1] = ByteRange(previous.offset, max(previous.end, current.end) - previous.offset)
    return tuple(merged)


def _coerce_signature(item: Any) -> FileSignature:
    if isinstance(item, FileSignature):
        return item
    if isinstance(item, Mapping):
        name = item.get("name", item.get("signature"))
        magic = item.get("magic", item.get("pattern"))
        description = item.get("description", "")
        if name is None or magic is None:
            raise TypeError("signature mappings require name and magic")
        return FileSignature(name, magic, description)
    try:
        values = tuple(item)
    except TypeError as exc:
        raise TypeError("each signature must be FileSignature or (name, magic)") from exc
    if len(values) == 2:
        return FileSignature(values[0], values[1])
    if len(values) == 3:
        return FileSignature(values[0], values[1], values[2])
    raise TypeError("each signature must be FileSignature or (name, magic)")


def _normalise_signatures(signatures: Optional[Iterable[Any]]) -> tuple[FileSignature, ...]:
    if signatures is None:
        signatures = COMMON_SIGNATURES
    elif isinstance(signatures, (FileSignature, Mapping)):
        signatures = (signatures,)
    result: list[FileSignature] = []
    seen: set[tuple[str, bytes, str]] = set()
    for number, item in enumerate(signatures):
        if number >= _MAX_SIGNATURES:
            raise ValueError("too many signatures")
        signature = _coerce_signature(item)
        key = (signature.name, signature.magic, signature.description)
        if key not in seen:
            seen.add(key)
            result.append(signature)
    if not result:
        return ()
    # Longest first makes a prefix-specific custom signature win over a broad
    # one at the same offset.  A set below still allows different names to be
    # reported when callers intentionally supply overlapping definitions.
    return tuple(sorted(result, key=lambda item: (-item.length, item.name, item.magic)))


class _ByteSource:
    """Read exact bounded slices from supported read-only source forms."""

    def __init__(self, source: Any, image_size: Optional[int]) -> None:
        self.source = source
        self.path: Optional[os.PathLike[str] | str] = None
        discovered: Optional[int]

        if isinstance(source, (str, bytes, os.PathLike)) and not isinstance(source, (bytearray, memoryview)):
            # A bytes object is an image, while str/path-like values identify a
            # file.  os.fspath keeps pathlib support without opening for write.
            if isinstance(source, (str, os.PathLike)):
                self.path = source
                try:
                    discovered = _as_nonnegative_int(os.stat(source).st_size, "image size")
                except OSError as exc:
                    raise OSError("unable to stat image source") from exc
            else:
                discovered = len(memoryview(source).cast("B"))
        elif isinstance(source, (bytearray, memoryview)):
            discovered = len(memoryview(source).cast("B"))
        elif callable(source) or hasattr(source, "read_at"):
            discovered = None
        else:
            try:
                discovered = _as_nonnegative_int(len(source), "image size")
            except (TypeError, AttributeError):
                discovered = None
            if discovered is None and hasattr(source, "seek") and hasattr(source, "tell"):
                try:
                    position = source.tell()
                    source.seek(0, 2)
                    discovered = _as_nonnegative_int(source.tell(), "image size")
                except (AttributeError, OSError) as exc:
                    raise TypeError("stream source must provide seek and tell") from exc
                finally:
                    try:
                        source.seek(position)
                    except (AttributeError, OSError):
                        pass

        if image_size is None:
            if discovered is None:
                raise ValueError("image_size is required for a callable or unknown source")
            image_size = discovered
        else:
            image_size = _as_nonnegative_int(image_size, "image_size")
            if discovered is not None and discovered != image_size:
                raise ValueError("image_size does not match the byte source")
        self.size = image_size

    def read(self, offset: int, length: int) -> bytes:
        if length <= 0:
            return b""
        source = self.source
        if self.path is not None:
            with open(self.path, "rb") as stream:
                value = stream.read(length) if offset == 0 else self._read_stream(stream, offset, length)
        elif isinstance(source, (bytes, bytearray, memoryview)):
            value = memoryview(source).cast("B")[offset : offset + length].tobytes()
        elif hasattr(source, "read_at"):
            value = source.read_at(offset, length)
        elif callable(source):
            value = source(offset, length)
        elif hasattr(source, "seek") and hasattr(source, "read"):
            value = self._read_stream(source, offset, length)
        else:
            try:
                value = source[offset : offset + length]
            except (TypeError, KeyError, IndexError) as exc:
                raise TypeError("source must be bytes-like, seekable, sliceable, or callable") from exc
        try:
            value = memoryview(value).cast("B").tobytes()
        except (TypeError, ValueError) as exc:
            raise TypeError("byte source must return bytes-like data") from exc
        if len(value) != length:
            raise EOFError(
                "byte source returned a short read at offset %d (wanted %d, got %d)"
                % (offset, length, len(value))
            )
        return value

    @staticmethod
    def _read_stream(stream: Any, offset: int, length: int) -> Any:
        position = stream.tell()
        try:
            stream.seek(offset)
            return stream.read(length)
        finally:
            stream.seek(position)


def _iter_hits(
    source: _ByteSource,
    ranges: Sequence[ByteRange],
    signatures: Sequence[FileSignature],
    chunk_size: int,
    max_hits: Optional[int],
) -> Iterator[SignatureHit]:
    if not ranges or not signatures or max_hits == 0:
        return

    max_magic = max(signature.length for signature in signatures)
    by_first: dict[int, tuple[FileSignature, ...]] = {}
    grouped: dict[int, list[FileSignature]] = {}
    for signature in signatures:
        grouped.setdefault(signature.magic[0], []).append(signature)
    by_first = {key: tuple(value) for key, value in grouped.items()}

    emitted = 0
    for requested in ranges:
        cursor = requested.offset
        range_end = requested.end
        tail = b""
        while cursor < range_end:
            count = min(chunk_size, range_end - cursor)
            chunk = source.read(cursor, count)
            window = tail + chunk
            window_base = cursor - len(tail)
            for local, first in enumerate(window):
                absolute = window_base + local
                if absolute < requested.offset or absolute >= range_end:
                    continue
                for signature in by_first.get(first, ()):
                    # A candidate wholly in the preceding chunk was already
                    # considered.  Candidates crossing the boundary are kept.
                    if absolute < cursor and absolute + signature.length <= cursor:
                        continue
                    if absolute + signature.length > range_end:
                        continue
                    end = local + signature.length
                    if end > len(window) or window[local:end] != signature.magic:
                        continue
                    yield SignatureHit(
                        offset=absolute,
                        signature=signature.name,
                        length=signature.length,
                        magic=signature.magic,
                        description=signature.description,
                    )
                    emitted += 1
                    if max_hits is not None and emitted >= max_hits:
                        return
            tail = window[-(max_magic - 1) :] if max_magic > 1 else b""
            cursor += count


def scan_unallocated(
    image: Any,
    ranges: Any,
    *,
    image_size: Optional[int] = None,
    source_size: Optional[int] = None,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    read_size: Optional[int] = None,
    signatures: Optional[Iterable[Any]] = None,
    max_hits: Optional[int] = None,
) -> Iterator[SignatureHit]:
    """Return a bounded iterator of signatures found in supplied ranges.

    ``image`` may be bytes-like, a path, a seekable binary stream, a
    sliceable/read-at object, or a callable ``(offset, length) -> bytes``.  A
    callable or other source without a discoverable length must be paired with
    ``image_size``.  ``source_size`` and ``read_size`` are compatibility
    aliases for callers that use the indexer's vocabulary.

    The function validates every range before the returned iterator performs a
    read.  It never writes to ``image`` and it does not emit bytes beyond the
    short magic prefix stored in each :class:`SignatureHit`.
    """

    if image_size is not None and source_size is not None:
        if _as_nonnegative_int(image_size, "image_size") != _as_nonnegative_int(
            source_size, "source_size"
        ):
            raise ValueError("image_size and source_size do not match")
    if image_size is None:
        image_size = source_size
    if read_size is not None:
        if chunk_size != _DEFAULT_CHUNK_SIZE and _as_positive_int(chunk_size, "chunk_size") != _as_positive_int(
            read_size, "read_size"
        ):
            raise ValueError("chunk_size and read_size do not match")
        chunk_size = read_size
    chunk_size = _as_positive_int(chunk_size, "chunk_size")
    if chunk_size > _MAX_CHUNK_SIZE:
        raise ValueError("chunk_size is too large")
    if max_hits is not None:
        max_hits = _as_nonnegative_int(max_hits, "max_hits")

    source = _ByteSource(image, image_size)
    normalised_ranges = _normalise_ranges(ranges, source.size)
    normalised_signatures = _normalise_signatures(signatures)
    return _iter_hits(source, normalised_ranges, normalised_signatures, chunk_size, max_hits)


def iter_unallocated_signatures(*args: Any, **kwargs: Any) -> Iterator[SignatureHit]:
    """Composable spelling of :func:`scan_unallocated`."""

    return scan_unallocated(*args, **kwargs)


class UnallocatedScanner:
    """Reusable scanner configuration for several range lists."""

    def __init__(
        self,
        image: Any,
        *,
        image_size: Optional[int] = None,
        source_size: Optional[int] = None,
        chunk_size: int = _DEFAULT_CHUNK_SIZE,
        read_size: Optional[int] = None,
        signatures: Optional[Iterable[Any]] = None,
    ) -> None:
        self.image = image
        self.image_size = image_size
        self.source_size = source_size
        self.chunk_size = chunk_size
        self.read_size = read_size
        self.signatures = None if signatures is None else _normalise_signatures(signatures)

    def scan(self, ranges: Any, *, max_hits: Optional[int] = None) -> Iterator[SignatureHit]:
        """Scan one caller-supplied range collection."""

        return scan_unallocated(
            self.image,
            ranges,
            image_size=self.image_size,
            source_size=self.source_size,
            chunk_size=self.chunk_size,
            read_size=self.read_size,
            signatures=self.signatures,
            max_hits=max_hits,
        )


class UnallocatedExtractor:
    """Compatibility facade for the original image-space extractor.

    The bounded :func:`scan_unallocated` API above is the canonical package
    primitive when allocation ranges are already known. The former
    ``mountfile_dfir_tools.UnallocatedExtractor`` also discovers ranges from
    FAT/NTFS heuristics and writes extracted regions to a separate directory.
    Keep that behaviour available from the package without importing the
    optional legacy module at package-import time.
    """

    def __init__(self, image_path: Any) -> None:
        self.image_path = image_path
        self._legacy = None

    def _implementation(self) -> Any:
        if self._legacy is None:
            try:
                from mountfile_dfir_tools import UnallocatedExtractor as legacy
            except ImportError as exc:
                raise RuntimeError(
                    "legacy allocation discovery is unavailable; use "
                    "scan_unallocated() with explicit ranges"
                ) from exc
            self._legacy = legacy(self.image_path)
        return self._legacy

    def extract(self, out_dir: Any, *, min_region: int = 4096) -> Any:
        """Extract discovered unallocated regions into *out_dir*."""

        return self._implementation().extract(out_dir, min_region=min_region)


# Aliases keep the API easy to discover without creating a second scanner.
scan_unallocated_ranges = scan_unallocated
scan_signatures = scan_unallocated
UnallocatedSignature = SignatureHit
SignatureMatch = SignatureHit


__all__ = [
    "ByteRange",
    "COMMON_SIGNATURES",
    "DEFAULT_SIGNATURES",
    "FileSignature",
    "InvalidRange",
    "SIGNATURES",
    "SignatureHit",
    "SignatureMatch",
    "UnallocatedExtractor",
    "UnallocatedScanner",
    "UnallocatedSignature",
    "iter_unallocated_signatures",
    "scan_signatures",
    "scan_unallocated",
    "scan_unallocated_ranges",
]
