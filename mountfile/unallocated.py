# SPDX-License-Identifier: MIT
"""Index caller-supplied unallocated byte ranges safely.

This module deliberately does not discover free space or carve filesystems.  A
caller must provide the ranges that are known (from an allocation map or some
other trusted source).  The index stores SHA-256 metadata and a sparse bitmap
in SQLite; it never stores the raw bytes.
"""

from __future__ import annotations

import hashlib
import operator
import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from os import fspath
from typing import Any, Optional, Tuple

_MAX_SQLITE_INTEGER = (1 << 63) - 1
_BITMAP_PAGE_BYTES = 4096
_BITMAP_PAGE_BITS = _BITMAP_PAGE_BYTES * 8
_DEFAULT_BLOCK_SIZE = 4096
_DEFAULT_READ_SIZE = 1 << 20


class InvalidRange(ValueError):
    """Raised when a requested range is not wholly inside the byte source."""


@dataclass(frozen=True)
class ByteRange:
    """A half-open byte range ``[offset, offset + length)``."""

    offset: int
    length: int


@dataclass(frozen=True)
class UnallocatedChunk:
    """Metadata for one unique chunk in the index.

    ``offset`` is the lowest indexed occurrence of this content.  Use
    :meth:`UnallocatedIndexer.occurrences` when every occurrence is needed.
    """

    offset: int
    length: int
    sha256: str

    @property
    def digest(self) -> str:
        """Alias for :attr:`sha256` for callers using digest terminology."""

        return self.sha256


@dataclass(frozen=True)
class UnallocatedOccurrence:
    """One indexed range, including duplicate-content occurrences."""

    offset: int
    length: int
    sha256: str


class _ByteSource:
    """Normalize supported byte sources and enforce exact reads."""

    def __init__(self, source: Any, size: Optional[int] = None) -> None:
        self.source = source
        discovered = self._discover_size(source)
        if size is None:
            size = discovered
        else:
            size = _as_nonnegative_int(size, "source_size")
            if discovered is not None and discovered != size:
                raise ValueError("source_size does not match the byte source")
        if size is None:
            raise ValueError(
                "source_size is required for a callable or stream without a known length"
            )
        if size > _MAX_SQLITE_INTEGER:
            raise ValueError("source_size is too large for the SQLite index")
        self.size = size

    @staticmethod
    def _discover_size(source: Any) -> Optional[int]:
        if isinstance(source, (bytes, bytearray, memoryview)):
            return len(memoryview(source).cast("B"))
        if callable(source) or hasattr(source, "read_at"):
            # A callable/read_at source has no portable length protocol.
            return None
        try:
            value = len(source)
        except (TypeError, AttributeError):
            value = None
        if value is not None:
            return _as_nonnegative_int(value, "source length")
        if hasattr(source, "seek") and hasattr(source, "tell"):
            position = source.tell()
            try:
                source.seek(0, 2)
                return _as_nonnegative_int(source.tell(), "source length")
            finally:
                source.seek(position)
        return None

    def read(self, offset: int, length: int) -> bytes:
        """Read exactly ``length`` bytes at ``offset`` without short reads."""

        if length == 0:
            return b""
        source = self.source
        if isinstance(source, (bytes, bytearray, memoryview)):
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
                raise TypeError(
                    "source must be bytes-like, seekable, sliceable, or callable"
                ) from exc
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
        try:
            position = stream.tell()
        except (AttributeError, OSError) as exc:
            raise TypeError("a stream source must provide tell() or read_at()") from exc
        try:
            stream.seek(offset)
            return stream.read(length)
        finally:
            stream.seek(position)


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


def _coerce_range(item: Any) -> ByteRange:
    if isinstance(item, ByteRange):
        offset, length = item.offset, item.length
    elif isinstance(item, dict):
        try:
            offset, length = item["offset"], item["length"]
        except KeyError as exc:
            raise TypeError("range mappings require offset and length") from exc
    else:
        try:
            offset, length = item
        except (TypeError, ValueError) as exc:
            raise TypeError("each range must be a pair (offset, length)") from exc
    offset = _as_nonnegative_int(offset, "range offset")
    length = _as_positive_int(length, "range length")
    return ByteRange(offset, length)


def _iter_range_items(ranges: Any) -> Iterator[Any]:
    """Accept one pair as a convenience while retaining iterable semantics."""

    if isinstance(ranges, ByteRange) or isinstance(ranges, dict):
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


class UnallocatedIndexer:
    """Persist hashes and a compact coverage bitmap for supplied raw ranges.

    The normal construction is ``UnallocatedIndexer(database)`` followed by
    ``indexer.index(source, ranges)``.  ``source=...`` may instead be supplied
    to the constructor, in which case ``indexer.index(ranges)`` is accepted.
    ``database`` is a SQLite path or an existing :class:`sqlite3.Connection`.
    No filesystem allocation map is inferred by this class.
    """

    def __init__(
        self,
        database: Any = None,
        source_size: Optional[int] = None,
        block_size: int = _DEFAULT_BLOCK_SIZE,
        read_size: int = _DEFAULT_READ_SIZE,
        *,
        db_path: Any = None,
        source: Any = None,
    ) -> None:
        if database is None:
            database = db_path
        if database is None:
            raise TypeError("database or db_path is required")
        self.block_size = _as_positive_int(block_size, "block_size")
        self.read_size = _as_positive_int(read_size, "read_size")
        if self.block_size > _MAX_SQLITE_INTEGER:
            raise ValueError("block_size is too large")
        self._source = source
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self._connection = (
            sqlite3.connect(fspath(database)) if self._owns_connection else database
        )
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()
        self._set_or_check_metadata("block_size", str(self.block_size))
        if source_size is not None:
            self._set_or_check_metadata(
                "source_size", str(_as_nonnegative_int(source_size, "source_size"))
            )
        self._connection.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        """The underlying SQLite connection, for read-only inspection."""

        return self._connection

    def close(self) -> None:
        if self._owns_connection and self._connection is not None:
            self._connection.close()
            self._connection = None  # type: ignore[assignment]

    def __enter__(self) -> UnallocatedIndexer:
        return self

    def __exit__(self, _exc_type: Any, _exc_value: Any, _traceback: Any) -> None:
        self.close()

    def index(
        self,
        source: Any = None,
        ranges: Optional[Iterable[Any]] = None,
        *,
        source_size: Optional[int] = None,
    ) -> Tuple[UnallocatedChunk, ...]:
        """Hash and persist ``ranges`` in deterministic order.

        Duplicate ranges are ignored.  Different offsets containing identical
        bytes share one ``chunks`` row while retaining each offset in
        ``occurrences``.  All input ranges are validated before any database
        changes are made.
        """

        if ranges is None:
            if self._source is None:
                raise TypeError("index(source, ranges) requires ranges")
            ranges = source
            source = self._source
        elif source is None:
            if self._source is None:
                raise TypeError("index(source, ranges) requires a byte source")
            source = self._source
        expected_size = self._metadata_int("source_size")
        requested_size = source_size if source_size is not None else expected_size
        byte_source = _ByteSource(source, size=requested_size)
        if expected_size is not None and expected_size != byte_source.size:
            raise ValueError("source size does not match this SQLite index")

        validated = set()
        for item in _iter_range_items(ranges):
            current = _coerce_range(item)
            if current.offset > byte_source.size:
                raise InvalidRange("range offset is outside the byte source")
            if current.length > byte_source.size - current.offset:
                raise InvalidRange("range extends beyond the byte source")
            validated.add((current.offset, current.length))
        ordered = sorted(validated)
        if not ordered:
            if expected_size is None:
                self._set_or_check_metadata("source_size", str(byte_source.size))
                self._connection.commit()
            return ()

        hashed = []
        for offset, length in ordered:
            digest = self._hash_range(byte_source, offset, length)
            hashed.append((offset, length, digest))

        self._begin_savepoint()
        try:
            self._set_or_check_metadata("source_size", str(byte_source.size))
            affected = set()
            for offset, length, digest in hashed:
                self._store_occurrence(offset, length, digest)
                self._mark_bitmap(offset, length)
                affected.add((digest, length))
            self._connection.execute(
                "DELETE FROM chunks "
                "WHERE NOT EXISTS (SELECT 1 FROM occurrences o WHERE o.chunk_id = chunks.chunk_id)"
            )
            self._recompute_canonical_offsets()
            self._release_savepoint()
        except Exception:
            self._rollback_savepoint()
            raise

        result = [self._chunk_for_key(digest, length) for digest, length in affected]
        return tuple(
            sorted(result, key=lambda item: (item.offset, item.length, item.sha256))
        )

    def records(self) -> Tuple[UnallocatedChunk, ...]:
        """Return unique chunks sorted by offset, length, and SHA-256."""

        rows = self._connection.execute(
            "SELECT offset, length, sha256 FROM chunks "
            "ORDER BY offset ASC, length ASC, sha256 ASC"
        ).fetchall()
        return tuple(
            UnallocatedChunk(int(row[0]), int(row[1]), str(row[2])) for row in rows
        )

    def occurrences(self) -> Tuple[UnallocatedOccurrence, ...]:
        """Return every indexed range, including duplicate-content ranges."""

        rows = self._connection.execute(
            "SELECT o.offset, o.length, c.sha256 "
            "FROM occurrences o JOIN chunks c ON c.chunk_id = o.chunk_id "
            "ORDER BY o.offset ASC, o.length ASC, c.sha256 ASC"
        ).fetchall()
        return tuple(
            UnallocatedOccurrence(int(row[0]), int(row[1]), str(row[2])) for row in rows
        )

    def is_indexed(self, offset: int, length: int) -> bool:
        """Return whether this exact range has been indexed."""

        offset = _as_nonnegative_int(offset, "range offset")
        length = _as_positive_int(length, "range length")
        row = self._connection.execute(
            "SELECT 1 FROM occurrences WHERE offset = ? AND length = ? LIMIT 1",
            (offset, length),
        ).fetchone()
        return row is not None

    def _hash_range(self, source: _ByteSource, offset: int, length: int) -> str:
        digest = hashlib.sha256()
        remaining = length
        cursor = offset
        while remaining:
            count = min(remaining, self.read_size)
            digest.update(source.read(cursor, count))
            cursor += count
            remaining -= count
        return digest.hexdigest()

    def _create_schema(self) -> None:
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id INTEGER PRIMARY KEY,
                offset INTEGER NOT NULL CHECK (offset >= 0),
                length INTEGER NOT NULL CHECK (length > 0),
                sha256 TEXT NOT NULL,
                UNIQUE (sha256, length)
            );
            CREATE TABLE IF NOT EXISTS occurrences (
                chunk_id INTEGER NOT NULL REFERENCES chunks(chunk_id) ON DELETE CASCADE,
                offset INTEGER NOT NULL CHECK (offset >= 0),
                length INTEGER NOT NULL CHECK (length > 0),
                PRIMARY KEY (chunk_id, offset, length),
                UNIQUE (offset, length)
            );
            CREATE TABLE IF NOT EXISTS bitmap_pages (
                page_no INTEGER PRIMARY KEY CHECK (page_no >= 0),
                bits BLOB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS occurrences_offset_idx ON occurrences(offset, length);
            """)

    def _metadata_int(self, key: str) -> Optional[int]:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            value = int(row[0])
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid %s metadata" % key) from exc
        return value

    def _set_or_check_metadata(self, key: str, value: str) -> None:
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)", (key, value)
            )
        elif row[0] != value:
            raise ValueError("SQLite index %s does not match requested value" % key)

    def _store_occurrence(self, offset: int, length: int, digest: str) -> None:
        existing = self._connection.execute(
            "SELECT o.chunk_id, c.sha256 FROM occurrences o "
            "JOIN chunks c ON c.chunk_id = o.chunk_id "
            "WHERE o.offset = ? AND o.length = ?",
            (offset, length),
        ).fetchone()
        if existing is not None and existing[1] == digest:
            return
        if existing is not None:
            self._connection.execute(
                "DELETE FROM occurrences WHERE chunk_id = ? AND offset = ? AND length = ?",
                (existing[0], offset, length),
            )

        self._connection.execute(
            "INSERT OR IGNORE INTO chunks(offset, length, sha256) VALUES (?, ?, ?)",
            (offset, length, digest),
        )
        row = self._connection.execute(
            "SELECT chunk_id FROM chunks WHERE sha256 = ? AND length = ?",
            (digest, length),
        ).fetchone()
        if (
            row is None
        ):  # pragma: no cover - defensive guard for a broken SQLite connection
            raise sqlite3.DatabaseError("unable to create unallocated chunk")
        self._connection.execute(
            "INSERT INTO occurrences(chunk_id, offset, length) VALUES (?, ?, ?)",
            (row[0], offset, length),
        )

    def _recompute_canonical_offsets(self) -> None:
        self._connection.execute(
            "UPDATE chunks SET offset = ("
            "SELECT MIN(o.offset) FROM occurrences o WHERE o.chunk_id = chunks.chunk_id"
            ")"
        )

    def _chunk_for_key(self, digest: str, length: int) -> UnallocatedChunk:
        row = self._connection.execute(
            "SELECT offset, length, sha256 FROM chunks WHERE sha256 = ? AND length = ?",
            (digest, length),
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError("indexed chunk disappeared")
        return UnallocatedChunk(int(row[0]), int(row[1]), str(row[2]))

    def _mark_bitmap(self, offset: int, length: int) -> None:
        first_block = offset // self.block_size
        last_block = (offset + length - 1) // self.block_size
        first_page = first_block // _BITMAP_PAGE_BITS
        last_page = last_block // _BITMAP_PAGE_BITS
        for page_no in range(first_page, last_page + 1):
            page_first = page_no * _BITMAP_PAGE_BITS
            lo = max(first_block, page_first) - page_first
            hi = min(last_block, page_first + _BITMAP_PAGE_BITS - 1) - page_first
            row = self._connection.execute(
                "SELECT bits FROM bitmap_pages WHERE page_no = ?", (page_no,)
            ).fetchone()
            if row is None:
                bits = bytearray(_BITMAP_PAGE_BYTES)
            else:
                bits = bytearray(row[0])
                if len(bits) != _BITMAP_PAGE_BYTES:
                    raise sqlite3.DatabaseError("invalid bitmap page length")
            if lo == 0 and hi == _BITMAP_PAGE_BITS - 1:
                bits[:] = b"\xff" * _BITMAP_PAGE_BYTES
            else:
                lo_byte, hi_byte = lo // 8, hi // 8
                if lo_byte == hi_byte:
                    bits[lo_byte] |= ((1 << (hi - lo + 1)) - 1) << (lo % 8)
                else:
                    bits[lo_byte] |= 0xFF << (lo % 8)
                    if hi_byte > lo_byte + 1:
                        bits[lo_byte + 1 : hi_byte] = b"\xff" * (hi_byte - lo_byte - 1)
                    bits[hi_byte] |= (1 << ((hi % 8) + 1)) - 1
            if row is None:
                self._connection.execute(
                    "INSERT INTO bitmap_pages(page_no, bits) VALUES (?, ?)",
                    (page_no, bytes(bits)),
                )
            else:
                self._connection.execute(
                    "UPDATE bitmap_pages SET bits = ? WHERE page_no = ?",
                    (bytes(bits), page_no),
                )

    def _begin_savepoint(self) -> None:
        self._connection.execute("SAVEPOINT unallocated_index")

    def _release_savepoint(self) -> None:
        self._connection.execute("RELEASE SAVEPOINT unallocated_index")

    def _rollback_savepoint(self) -> None:
        try:
            self._connection.execute("ROLLBACK TO SAVEPOINT unallocated_index")
            self._connection.execute("RELEASE SAVEPOINT unallocated_index")
        except sqlite3.Error:
            # Preserve the original exception if the connection itself failed.
            pass


class UnallocatedSpaceAnalyzer(UnallocatedIndexer):
    """Convenience form that binds a byte source at construction time."""

    def __init__(
        self,
        source: Any,
        database: Any = None,
        source_size: Optional[int] = None,
        block_size: int = _DEFAULT_BLOCK_SIZE,
        read_size: int = _DEFAULT_READ_SIZE,
        *,
        db_path: Any = None,
    ) -> None:
        if database is None:
            database = db_path
        super().__init__(
            database,
            source_size=source_size,
            block_size=block_size,
            read_size=read_size,
            source=source,
        )


# Readable aliases for callers that prefer the shorter index terminology.
UnallocatedSpaceIndexer = UnallocatedIndexer
UnallocatedIndex = UnallocatedIndexer
UnallocatedAnalyzer = UnallocatedSpaceAnalyzer


def index_unallocated(
    source: Any,
    ranges: Iterable[Any],
    database: Any = None,
    *,
    db_path: Any = None,
    source_size: Optional[int] = None,
    block_size: int = _DEFAULT_BLOCK_SIZE,
    read_size: int = _DEFAULT_READ_SIZE,
) -> Tuple[UnallocatedChunk, ...]:
    """Index caller-supplied ranges and close the SQLite handle afterward.

    This function is intentionally explicit: without ``ranges`` there is no
    filesystem-specific notion of "unallocated" space to infer.
    """

    if database is None:
        database = db_path
    with UnallocatedIndexer(
        database,
        source_size=source_size,
        block_size=block_size,
        read_size=read_size,
    ) as indexer:
        return indexer.index(source, ranges)


analyze_unallocated = index_unallocated


__all__ = [
    "ByteRange",
    "InvalidRange",
    "UnallocatedAnalyzer",
    "UnallocatedChunk",
    "UnallocatedIndex",
    "UnallocatedIndexer",
    "UnallocatedOccurrence",
    "UnallocatedSpaceAnalyzer",
    "UnallocatedSpaceIndexer",
    "analyze_unallocated",
    "index_unallocated",
]
