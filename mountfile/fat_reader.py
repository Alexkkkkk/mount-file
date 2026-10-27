# SPDX-License-Identifier: MIT
"""Bounded, read-only FAT12/16/32 provider.

The writer in :mod:`mountfile.fat32` remains the explicit FAT32 mutation
backend.  This module is deliberately read-only and covers all three classic
FAT variants through the same VFS contract.  It validates the BPB, derives the
volume extent from the filesystem itself, rejects malformed cluster chains and
never follows image-relative paths outside the selected volume.
"""

from __future__ import annotations

import datetime as _dt
import os
import struct
import threading
from dataclasses import dataclass
from pathlib import Path


class FatReadError(ValueError):
    """The selected FAT profile or requested read is invalid or unsupported."""


_MAX_READ_RANGE_BYTES = 16 * 1024 * 1024
_MAX_DIRECTORY_BYTES = 64 * 1024 * 1024
_MAX_DIRECTORY_ENTRIES = 1_000_000
_MAX_LFN_ENTRIES = 20

_EOC12 = 0xFF8
_EOC16 = 0xFFF8
_EOC32 = 0x0FFFFFF8
_BAD12 = 0xFF7
_BAD16 = 0xFFF7
_BAD32 = 0x0FFFFFF7


@dataclass(frozen=True)
class FatProfile:
    fat_type: int
    bytes_per_sector: int
    sectors_per_cluster: int
    reserved_sectors: int
    fat_count: int
    fat_sectors: int
    root_entries: int
    total_sectors: int
    root_dir_sectors: int
    cluster_count: int
    cluster_size: int
    data_start: int
    root_dir_start: int
    root_cluster: int | None
    volume_size: int


@dataclass(frozen=True)
class _Entry:
    name: str
    is_dir: bool
    size: int
    cluster: int
    date: str


class NativeFatImage:
    """Read-only VFS provider for one bounded FAT12/16/32 filesystem."""

    def __init__(self, image_path, offset=0, volume_size=None):
        self.image_path = str(image_path)
        self.offset = int(offset)
        if self.offset < 0:
            raise FatReadError("negative filesystem offset")
        self._stream = None
        self._lock = threading.RLock()
        self._image_size = 0
        try:
            self._stream = Path(self.image_path).open("rb")  # noqa: SIM115
            self._image_size = os.fstat(self._stream.fileno()).st_size
            self.profile = self._parse_profile(volume_size)
            self.fat_type = self.profile.fat_type
            self.block_size = self.profile.bytes_per_sector
            self.cluster_size = self.profile.cluster_size
            self.volume_size = self.profile.volume_size
            self.volume_end = self.offset + self.volume_size
            self.fat_start = (
                self.offset + self.profile.reserved_sectors * self.block_size
            )
            self.fat_size = self.profile.fat_sectors * self.block_size
            self.data_start = self.offset + self.profile.data_start
            self.root_dir_start = self.offset + self.profile.root_dir_start
            self.root_cluster = self.profile.root_cluster
            self.cluster_count = self.profile.cluster_count
            self._max_cluster = self.cluster_count + 1
            self.root_name = self.image_path
            self.backend_name = f"native-fat{self.fat_type}-ro"
            self._validate_metadata_copies()
        except (OSError, struct.error) as exc:
            self.close()
            raise FatReadError(str(exc)) from exc
        except Exception:
            self.close()
            raise

    @staticmethod
    def looks_like_boot_sector(raw: bytes) -> bool:
        """Return whether bytes have enough BPB shape to classify as FAT."""
        if len(raw) < 512 or raw[510:512] != b"\x55\xaa":
            return False
        bps = struct.unpack_from("<H", raw, 11)[0]
        spc = raw[13]
        reserved = struct.unpack_from("<H", raw, 14)[0]
        fats = raw[16]
        total16 = struct.unpack_from("<H", raw, 19)[0]
        total32 = struct.unpack_from("<I", raw, 32)[0]
        fat16 = struct.unpack_from("<H", raw, 22)[0]
        fat32 = struct.unpack_from("<I", raw, 36)[0]
        return (
            bps in (512, 1024, 2048, 4096)
            and spc != 0
            and spc <= 128
            and not (spc & (spc - 1))
            and reserved > 0
            and 1 <= fats <= 2
            and (total16 or total32) > 0
            and (fat16 or fat32) > 0
        )

    def _parse_profile(self, selected_size) -> FatProfile:
        raw = self._read_absolute(self.offset, 512, image_only=True)
        self._boot_sector = raw
        if not self.looks_like_boot_sector(raw):
            raise FatReadError("no valid FAT boot sector at the selected offset")
        bps = struct.unpack_from("<H", raw, 11)[0]
        spc = raw[13]
        reserved = struct.unpack_from("<H", raw, 14)[0]
        fat_count = raw[16]
        root_entries = struct.unpack_from("<H", raw, 17)[0]
        total16 = struct.unpack_from("<H", raw, 19)[0]
        fat16 = struct.unpack_from("<H", raw, 22)[0]
        total32 = struct.unpack_from("<I", raw, 32)[0]
        fat32 = struct.unpack_from("<I", raw, 36)[0]
        total_sectors = total16 or total32
        fat_sectors = fat16 or fat32
        if bps not in (512, 1024, 2048, 4096):
            raise FatReadError("unsupported FAT sector size")
        if spc == 0 or spc > 128 or spc & (spc - 1):
            raise FatReadError("invalid FAT sectors-per-cluster")
        if reserved == 0 or not 1 <= fat_count <= 2:
            raise FatReadError("invalid FAT reserved-sector/FAT count")
        if total_sectors <= 0 or fat_sectors <= 0:
            raise FatReadError("invalid FAT volume geometry")
        root_dir_sectors = ((root_entries * 32) + bps - 1) // bps
        fat_area_sectors = fat_count * fat_sectors
        data_sectors = total_sectors - reserved - fat_area_sectors - root_dir_sectors
        if data_sectors <= 0:
            raise FatReadError("FAT data area is empty")
        cluster_count = data_sectors // spc
        if cluster_count < 4085:
            fat_type = 12
        elif cluster_count < 65525:
            fat_type = 16
        else:
            fat_type = 32
        if fat_type == 32:
            if root_entries or fat16:
                raise FatReadError("FAT32 BPB contains FAT12/16 root geometry")
            root_cluster = struct.unpack_from("<I", raw, 44)[0] & 0x0FFFFFFF
            if not 2 <= root_cluster <= cluster_count + 1:
                raise FatReadError("FAT32 root cluster is outside the data area")
        else:
            if root_entries == 0 or fat32:
                raise FatReadError("FAT12/16 BPB contains FAT32 root geometry")
            root_cluster = None
        volume_bytes = total_sectors * bps
        if volume_bytes < 512 or volume_bytes > self._image_size - self.offset:
            raise FatReadError("FAT volume extends beyond image bounds")
        if selected_size is not None:
            selected_size = int(selected_size)
            if selected_size <= 0 or self.offset + selected_size > self._image_size:
                raise FatReadError("selected FAT extent exceeds image bounds")
            if volume_bytes > selected_size:
                raise FatReadError("FAT volume exceeds selected partition bounds")
        fat_bytes = fat_sectors * bps
        entries_capacity = (
            fat_bytes * (8 if fat_type == 12 else 16 if fat_type == 16 else 32)
        ) // 8
        if entries_capacity < cluster_count + 2:
            raise FatReadError("FAT table is too small for the declared data area")
        data_start = (reserved + fat_area_sectors + root_dir_sectors) * bps
        root_dir_start = (reserved + fat_area_sectors) * bps
        if data_start >= volume_bytes:
            raise FatReadError("FAT data area exceeds volume bounds")
        return FatProfile(
            fat_type=fat_type,
            bytes_per_sector=bps,
            sectors_per_cluster=spc,
            reserved_sectors=reserved,
            fat_count=fat_count,
            fat_sectors=fat_sectors,
            root_entries=root_entries,
            total_sectors=total_sectors,
            root_dir_sectors=root_dir_sectors,
            cluster_count=cluster_count,
            cluster_size=spc * bps,
            data_start=data_start,
            root_dir_start=root_dir_start,
            root_cluster=root_cluster,
            volume_size=volume_bytes,
        )

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    @property
    def f(self):
        """Compatibility alias used by the existing GUI provider lifecycle."""
        return self._stream

    def reopen(self) -> None:
        if self._stream is None or self._stream.closed:
            self._stream = Path(self.image_path).open("rb")
            self._image_size = os.fstat(self._stream.fileno()).st_size
            self.profile = self._parse_profile(self.volume_size)
            self._validate_metadata_copies()

    def _validate_metadata_copies(self) -> None:
        """Reject inconsistent mirrored FAT metadata before any read access."""
        if self.profile.fat_count > 1:
            primary = self._read_at(
                self.profile.reserved_sectors * self.block_size, self.fat_size
            )
            for index in range(1, self.profile.fat_count):
                replica = self._read_at(
                    self.profile.reserved_sectors * self.block_size
                    + index * self.fat_size,
                    self.fat_size,
                )
                if replica != primary:
                    raise FatReadError("mirrored FAT copies are inconsistent")
        if self.fat_type != 32:
            return
        backup_sector = struct.unpack_from("<H", self._boot_sector, 50)[0]
        if not backup_sector:
            return
        backup = self._read_at(backup_sector * self.block_size, self.block_size)
        if backup != self._boot_sector:
            raise FatReadError("FAT32 backup boot sector is inconsistent")

    def _read_absolute(self, absolute: int, size: int, *, image_only=False) -> bytes:
        if self._stream is None:
            raise FatReadError("filesystem image is closed")
        if absolute < 0 or size < 0 or absolute + size < absolute:
            raise FatReadError("negative or overflowing FAT read range")
        if not image_only:
            if absolute < self.offset or absolute + size > self.volume_end:
                raise FatReadError("FAT read exceeds selected volume bounds")
        if absolute + size > self._image_size:
            raise FatReadError("FAT read exceeds image bounds")
        with self._lock:
            self._stream.seek(absolute)
            data = self._stream.read(size)
        if len(data) != size:
            raise FatReadError("short read from FAT image")
        return data

    def _read_at(self, relative: int, size: int) -> bytes:
        if relative < 0 or size < 0 or relative + size < relative:
            raise FatReadError("negative or overflowing FAT read range")
        return self._read_absolute(self.offset + relative, size)

    def _fat_entry(self, cluster: int) -> int:
        if cluster < 0 or cluster >= self.cluster_count + 2:
            raise FatReadError("FAT entry index is outside the declared table")
        if self.fat_type == 12:
            raw = self._read_at(
                self.profile.reserved_sectors * self.block_size + (cluster * 3) // 2, 2
            )
            value = struct.unpack_from("<H", raw, 0)[0]
            return (value >> 4) if cluster & 1 else (value & 0x0FFF)
        if self.fat_type == 16:
            raw = self._read_at(
                self.profile.reserved_sectors * self.block_size + cluster * 2, 2
            )
            return struct.unpack_from("<H", raw, 0)[0]
        raw = self._read_at(
            self.profile.reserved_sectors * self.block_size + cluster * 4, 4
        )
        return struct.unpack_from("<I", raw, 0)[0] & 0x0FFFFFFF

    def _is_eoc(self, value: int) -> bool:
        return value >= {12: _EOC12, 16: _EOC16, 32: _EOC32}[self.fat_type]

    def _is_bad(self, value: int) -> bool:
        return value == {12: _BAD12, 16: _BAD16, 32: _BAD32}[self.fat_type]

    def _chain(self, start: int, size: int = 0) -> list[int]:
        required = (size + self.cluster_size - 1) // self.cluster_size if size else 0
        if start == 0:
            if required:
                raise FatReadError("non-empty FAT file has no start cluster")
            return []
        if not 2 <= start <= self._max_cluster:
            raise FatReadError("FAT chain starts outside the data area")
        result: list[int] = []
        seen: set[int] = set()
        current = start
        while True:
            if current in seen:
                raise FatReadError("FAT cluster chain contains a cycle")
            if not 2 <= current <= self._max_cluster:
                raise FatReadError("FAT chain references an invalid cluster")
            seen.add(current)
            result.append(current)
            if len(result) > self.cluster_count:
                raise FatReadError("FAT cluster chain exceeds volume capacity")
            nxt = self._fat_entry(current)
            if self._is_eoc(nxt):
                break
            if nxt == 0 or self._is_bad(nxt) or nxt == 1:
                raise FatReadError("FAT chain contains a free, bad or reserved link")
            current = nxt
        if required and len(result) < required:
            raise FatReadError("FAT chain is shorter than the file size")
        return result

    def _cluster_bytes(self, cluster: int) -> bytes:
        if not 2 <= cluster <= self._max_cluster:
            raise FatReadError("cluster is outside the data area")
        relative = self.profile.data_start + (cluster - 2) * self.cluster_size
        return self._read_at(relative, self.cluster_size)

    def _read_chain(self, start: int, size: int) -> bytes:
        if size < 0:
            raise FatReadError("negative FAT file size")
        if size == 0:
            if start not in (0, 1):
                self._chain(start, 0)
            return b""
        chain = self._chain(start, size)
        output = bytearray()
        for cluster in chain:
            output.extend(self._cluster_bytes(cluster))
            if len(output) >= size:
                break
        if len(output) < size:
            raise FatReadError("FAT chain produced a short file")
        return bytes(output[:size])

    def _directory_raw(self, cluster: int | None, size: int | None = None) -> bytes:
        if self.fat_type in (12, 16) and cluster is None:
            length = self.profile.root_entries * 32
            return self._read_at(self.profile.root_dir_start, length)
        if cluster is None:
            cluster = self.root_cluster
        if cluster is None:
            raise FatReadError("FAT directory has no start cluster")
        chain = self._chain(cluster, size or 0)
        max_clusters = max(1, _MAX_DIRECTORY_BYTES // self.cluster_size)
        if len(chain) > max_clusters:
            raise FatReadError("FAT directory exceeds the native reader limit")
        raw = b"".join(self._cluster_bytes(item) for item in chain)
        if len(raw) > _MAX_DIRECTORY_BYTES:
            raise FatReadError("FAT directory exceeds the native reader limit")
        return raw

    @staticmethod
    def _short_checksum(raw: bytes) -> int:
        result = 0
        for value in raw[:11]:
            result = (((result & 1) << 7) + (result >> 1) + value) & 0xFF
        return result

    @staticmethod
    def _decode_short(raw: bytes) -> str:
        base = raw[:8].decode("cp437", "replace").rstrip(" ")
        ext = raw[8:11].decode("cp437", "replace").rstrip(" ")
        if not base:
            raise FatReadError("FAT directory contains an empty short name")
        return f"{base}.{ext}" if ext else base

    @staticmethod
    def _decode_lfn(units: list[int]) -> str | None:
        if not units or 0 not in units:
            return None
        end = units.index(0)
        if any(value not in (0xFFFF,) for value in units[end + 1 :]):
            return None
        raw = b"".join(struct.pack("<H", value) for value in units[:end])
        try:
            name = raw.decode("utf-16le")
        except UnicodeDecodeError:
            return None
        if not name or len(name) > 255 or any(c in name for c in ("/", "\\", "\x00")):
            return None
        return name

    def _parse_directory(self, raw: bytes) -> list[_Entry]:
        if len(raw) % 32:
            raise FatReadError("FAT directory is not entry-aligned")
        entries: list[_Entry] = []
        pending: list[tuple[int, int, bytes]] = []
        expected_ord = None
        pending_checksum = None
        for position in range(0, len(raw), 32):
            item = raw[position : position + 32]
            first = item[0]
            if first == 0x00:
                break
            attr = item[11]
            if first == 0xE5:
                pending.clear()
                expected_ord = None
                pending_checksum = None
                continue
            if attr == 0x0F:
                ordinal = item[0]
                sequence = ordinal & 0x1F
                is_last = bool(ordinal & 0x40)
                if not 1 <= sequence <= _MAX_LFN_ENTRIES:
                    pending.clear()
                    expected_ord = None
                    pending_checksum = None
                    continue
                checksum = item[13]
                chunk = item[1:11] + item[14:26] + item[28:32]
                units = [
                    struct.unpack_from("<H", chunk, index)[0]
                    for index in range(0, 26, 2)
                ]
                if is_last:
                    pending = [(sequence, checksum, bytes(chunk))]
                    expected_ord = sequence - 1
                    pending_checksum = checksum
                elif (
                    pending
                    and expected_ord == sequence
                    and pending_checksum == checksum
                ):
                    pending.append((sequence, checksum, bytes(chunk)))
                    expected_ord -= 1
                else:
                    pending.clear()
                    expected_ord = None
                    pending_checksum = None
                continue
            if attr & 0x08:
                pending.clear()
                expected_ord = None
                pending_checksum = None
                continue
            short = bytes(item[:11])
            name = self._decode_short(short)
            if (
                pending
                and expected_ord == 0
                and pending_checksum == self._short_checksum(short)
            ):
                units: list[int] = []
                for _sequence, _checksum, chunk in reversed(pending):
                    units.extend(struct.unpack_from("<13H", chunk, 0))
                name = self._decode_lfn(units) or name
            pending.clear()
            expected_ord = None
            pending_checksum = None
            if name in (".", ".."):
                continue
            is_dir = bool(attr & 0x10)
            cluster = struct.unpack_from("<H", item, 26)[0]
            if self.fat_type == 32:
                cluster |= struct.unpack_from("<H", item, 20)[0] << 16
                cluster &= 0x0FFFFFFF
            size = struct.unpack_from("<I", item, 28)[0]
            if is_dir:
                if not 2 <= cluster <= self._max_cluster:
                    raise FatReadError("FAT directory entry has an invalid cluster")
            elif size and not 2 <= cluster <= self._max_cluster:
                raise FatReadError("non-empty FAT file has an invalid cluster")
            date = ""
            dos_date = struct.unpack_from("<H", item, 24)[0]
            dos_time = struct.unpack_from("<H", item, 22)[0]
            if dos_date:
                try:
                    stamp = _dt.datetime(
                        1980 + ((dos_date >> 9) & 0x7F),
                        (dos_date >> 5) & 0x0F,
                        dos_date & 0x1F,
                        (dos_time >> 11) & 0x1F,
                        (dos_time >> 5) & 0x3F,
                        (dos_time & 0x1F) * 2,
                    )
                    date = stamp.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    date = ""
            entries.append(_Entry(name, is_dir, size, cluster, date))
            if len(entries) > _MAX_DIRECTORY_ENTRIES:
                raise FatReadError("FAT directory exceeds the entry-count limit")
        return entries

    @staticmethod
    def _parts(path: str) -> list[str]:
        if not isinstance(path, str) or "\x00" in path or "\\" in path:
            raise FatReadError("FAT path contains an unsafe separator")
        parts = [part for part in path.split("/") if part]
        if any(part in (".", "..") for part in parts):
            raise FatReadError("dot components are not supported in FAT paths")
        return parts

    def _root_context(self) -> tuple[int | None, str]:
        return (self.root_cluster, "/") if self.fat_type == 32 else (None, "/")

    def _resolve_dir(self, path: str) -> tuple[int | None, list[_Entry]]:
        parts = self._parts(path)
        cluster, _ = self._root_context()
        entries = self._parse_directory(self._directory_raw(cluster))
        current = "/"
        for part in parts:
            match = next(
                (
                    entry
                    for entry in entries
                    if entry.name.casefold() == part.casefold()
                ),
                None,
            )
            if match is None or not match.is_dir:
                raise FatReadError(f"FAT directory does not exist: {path}")
            cluster = match.cluster
            current = current.rstrip("/") + "/" + match.name
            entries = self._parse_directory(self._directory_raw(cluster))
        return cluster, entries

    def _resolve_file(self, path: str) -> _Entry:
        parts = self._parts(path)
        if not parts:
            raise FatReadError("FAT root is a directory")
        parent = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        _cluster, entries = self._resolve_dir(parent)
        match = next(
            (
                entry
                for entry in entries
                if entry.name.casefold() == parts[-1].casefold()
            ),
            None,
        )
        if match is None:
            raise FileNotFoundError(path)
        return match

    def list_dir(self, path="/") -> list[dict]:
        _cluster, entries = self._resolve_dir(path or "/")
        return [
            {
                "name": entry.name,
                "is_dir": entry.is_dir,
                "size": 0 if entry.is_dir else entry.size,
                "date": entry.date,
            }
            for entry in sorted(
                entries, key=lambda item: (not item.is_dir, item.name.casefold())
            )
        ]

    def file_size(self, path) -> int:
        entry = self._resolve_file(path)
        if entry.is_dir:
            raise IsADirectoryError(path)
        return entry.size

    def read_file_range(self, path, offset=0, length=1 << 20) -> bytes:
        if not isinstance(offset, int) or not isinstance(length, int):
            raise FatReadError("FAT read offset and length must be integers")
        if offset < 0 or length < 0:
            raise FatReadError("negative FAT file read range")
        entry = self._resolve_file(path)
        if entry.is_dir:
            raise IsADirectoryError(path)
        if offset >= entry.size or length == 0:
            return b""
        amount = min(length, entry.size - offset)
        if amount > _MAX_READ_RANGE_BYTES:
            raise FatReadError("requested FAT read exceeds the 16 MiB native limit")
        chain = self._chain(entry.cluster, entry.size)
        first = offset // self.cluster_size
        last = (offset + amount - 1) // self.cluster_size
        output = bytearray()
        for cluster in chain[first : last + 1]:
            output.extend(self._cluster_bytes(cluster))
        start = offset % self.cluster_size
        return bytes(output[start : start + amount])

    def read_file(self, path) -> bytes:
        size = self.file_size(path)
        if size == 0:
            return b""
        output = bytearray()
        offset = 0
        while offset < size:
            part = self.read_file_range(
                path, offset, min(_MAX_READ_RANGE_BYTES, size - offset)
            )
            if not part:
                raise FatReadError("FAT file read returned a short range")
            output.extend(part)
            offset += len(part)
        return bytes(output)


__all__ = ["FatProfile", "FatReadError", "NativeFatImage"]
