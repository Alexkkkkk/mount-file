# SPDX-License-Identifier: MIT
"""Conservative, bounded read-only exFAT tree provider.

The provider intentionally supports only directory entries and regular-file
reads. It never mutates the image and rejects malformed chains, name sets and
out-of-volume ranges. exFAT writes remain outside this profile.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from pathlib import Path

from mountfile.exfat_probe import ExfatProbeError, probe_exfat_boot_region
from mountfile.safety import RefusalCode, RefusalError, checked_range
from mountfile.vfs_api import ReadOnlyVFSStatMixin


class ExfatReadError(RefusalError):
    """The selected exFAT profile or requested read is invalid."""

    def __init__(self, message: str, code: RefusalCode = RefusalCode.MALFORMED_METADATA):
        super().__init__(message, code)


_EOC = 0xFFFFFFF8
_MAX_CHAIN = 1_000_000
_MAX_DIR_BYTES = 64 * 1024 * 1024
_MAX_READ = 16 * 1024 * 1024


@dataclass(frozen=True)
class _Entry:
    name: str
    is_dir: bool
    first_cluster: int
    size: int
    no_fat_chain: bool


class NativeExfatImage(ReadOnlyVFSStatMixin):
    """Read-only exFAT provider for a validated bounded volume."""

    backend_name = "native-exfat-ro"
    case_insensitive_paths = True
    max_read_range_bytes = _MAX_READ

    def __init__(self, image_path, offset=0, volume_size=None):
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(volume_size, int)
            or isinstance(volume_size, bool)
            or volume_size <= 0
        ):
            raise ExfatReadError("exFAT provider requires an explicit volume extent")
        self.image_path = str(image_path)
        self.offset = offset
        self._stream = None
        try:
            self._stream = Path(self.image_path).open("rb")
            self._image_size = os.fstat(self._stream.fileno()).st_size
            self.profile = probe_exfat_boot_region(
                self.image_path, offset=self.offset, volume_size=volume_size
            )
            if self.profile.volume_flags != 0:
                raise ExfatReadError("native exFAT reader rejects nonzero volume flags")
            if self.profile.number_of_fats != 1:
                raise ExfatReadError(
                    "native exFAT reader supports only single-FAT volumes"
                )
            self.volume_end = (
                self.offset + self.profile.volume_length * self.profile.bytes_per_sector
            )
            if (
                self.volume_end > self.offset + volume_size
                or self.volume_end > self._image_size
            ):
                raise ExfatReadError("exFAT volume exceeds selected bounds")
            self._system_metadata = self.system_metadata()
            self.root_name = self.image_path
        except (OSError, ExfatProbeError, struct.error) as exc:
            self.close()
            raise ExfatReadError(str(exc)) from exc
        except Exception:
            self.close()
            raise

    def close(self):
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.close()

    def reopen(self):
        if self._stream is None or self._stream.closed:
            self._stream = Path(self.image_path).open("rb")
            self._image_size = os.fstat(self._stream.fileno()).st_size

    def _read_rel(self, relative, size):
        if self._stream is None:
            raise ExfatReadError("exFAT image is closed")
        checked_range(
            relative,
            size,
            upper=self.volume_end - self.offset,
            context="exFAT read exceeds selected volume bounds",
            error_type=ExfatReadError,
        )
        absolute = self.offset + relative
        checked_range(
            absolute,
            size,
            upper=self._image_size,
            context="exFAT read exceeds image bounds",
            error_type=ExfatReadError,
        )
        self._stream.seek(absolute)
        data = self._stream.read(size)
        if len(data) != size:
            raise ExfatReadError("short exFAT read")
        return data

    def _fat_entry(self, cluster):
        if not 2 <= cluster <= self.profile.cluster_count + 1:
            raise ExfatReadError("exFAT cluster is outside the data heap")
        relative = self.profile.fat_offset * self.profile.bytes_per_sector + cluster * 4
        return struct.unpack_from("<I", self._read_rel(relative, 4), 0)[0]

    def _cluster_offset(self, cluster):
        if not 2 <= cluster <= self.profile.cluster_count + 1:
            raise ExfatReadError("exFAT cluster is outside the data heap")
        return (
            self.profile.cluster_heap_offset * self.profile.bytes_per_sector
            + (cluster - 2) * self.profile.cluster_size
        )

    def _iter_chain(self, start, size=0, *, no_fat_chain=False):
        if not 2 <= start <= self.profile.cluster_count + 1:
            raise ExfatReadError("exFAT chain starts outside the data heap")
        required = (size + self.profile.cluster_size - 1) // self.profile.cluster_size
        if no_fat_chain:
            if (
                required > self.profile.cluster_count
                or start + required > self.profile.cluster_count + 2
            ):
                raise ExfatReadError("contiguous exFAT stream exceeds cluster capacity")
            yield from range(start, start + required)
            return
        seen = set()
        current = start
        count = 0
        while True:
            if current in seen or count >= _MAX_CHAIN:
                raise ExfatReadError(
                    "exFAT cluster chain contains a cycle or is too long"
                )
            if not 2 <= current <= self.profile.cluster_count + 1:
                raise ExfatReadError("exFAT chain references an invalid cluster")
            seen.add(current)
            count += 1
            if count > self.profile.cluster_count:
                raise ExfatReadError("exFAT chain exceeds volume capacity")
            yield current
            nxt = self._fat_entry(current)
            if nxt >= _EOC:
                break
            if nxt in (0, 1) or nxt > self.profile.cluster_count + 1:
                raise ExfatReadError("exFAT chain contains a free or invalid link")
            current = nxt
        if required and count < required:
            raise ExfatReadError("exFAT chain is shorter than the stream size")

    def _chain(self, start, size=0, *, no_fat_chain=False):
        return list(self._iter_chain(start, size, no_fat_chain=no_fat_chain))

    def _read_directory_bytes(self, cluster):
        """Read a directory only while its cumulative size stays bounded."""
        raw = bytearray()
        for item in self._iter_chain(cluster, no_fat_chain=False):
            if self.profile.cluster_size > _MAX_DIR_BYTES - len(raw):
                raise ExfatReadError("exFAT directory exceeds reader limit")
            raw.extend(
                self._read_rel(self._cluster_offset(item), self.profile.cluster_size)
            )
        return bytes(raw)

    def _read_stream(self, first_cluster, size, no_fat_chain=False):
        if size < 0 or size > self.profile.cluster_count * self.profile.cluster_size:
            raise ExfatReadError("exFAT stream size exceeds volume capacity")
        if size == 0:
            return b""
        output = bytearray()
        for cluster in self._chain(first_cluster, size, no_fat_chain=no_fat_chain):
            output.extend(
                self._read_rel(self._cluster_offset(cluster), self.profile.cluster_size)
            )
            if len(output) >= size:
                break
        if len(output) < size:
            raise ExfatReadError("short exFAT stream")
        return bytes(output[:size])

    def _read_stream_range(
        self, first_cluster, size, offset, length, no_fat_chain=False
    ):
        """Read only clusters intersecting a bounded file range."""
        if size < 0 or size > self.profile.cluster_count * self.profile.cluster_size:
            raise ExfatReadError("exFAT stream size exceeds volume capacity")
        if offset < 0 or length < 0 or offset + length < offset:
            raise ExfatReadError("invalid exFAT stream range")
        if offset >= size or length == 0:
            return b""
        end = min(offset + length, size)
        cluster_size = self.profile.cluster_size
        first_index = offset // cluster_size
        last_index = (end - 1) // cluster_size
        output = bytearray()
        for index, cluster in enumerate(
            self._iter_chain(first_cluster, size, no_fat_chain=no_fat_chain)
        ):
            if index > last_index:
                break
            if index >= first_index:
                cluster_start = index * cluster_size
                read_start = max(offset, cluster_start) - cluster_start
                read_end = min(end, cluster_start + cluster_size) - cluster_start
                output.extend(
                    self._read_rel(
                        self._cluster_offset(cluster) + read_start,
                        read_end - read_start,
                    )
                )
            if index == last_index:
                break
        if len(output) != end - offset:
            raise ExfatReadError("exFAT chain is shorter than requested range")
        return bytes(output)

    @staticmethod
    def _decode_name(raw):
        try:
            name = raw.decode("utf-16le")
        except UnicodeDecodeError as exc:
            raise ExfatReadError("exFAT filename is not valid UTF-16LE") from exc
        if not name or "/" in name or "\\" in name or "\x00" in name:
            raise ExfatReadError("invalid exFAT filename")
        return name

    @staticmethod
    def _entry_set_checksum(raw):
        checksum = 0
        for index, value in enumerate(raw):
            if index in (2, 3):
                value = 0
            checksum = ((checksum << 15) | (checksum >> 1)) & 0xFFFF
            checksum = (checksum + value) & 0xFFFF
        return checksum

    @staticmethod
    def _name_hash(name):
        checksum = 0
        for value in name.encode("utf-16le"):
            checksum = ((checksum << 15) | (checksum >> 1)) & 0xFFFF
            checksum = (checksum + value) & 0xFFFF
        return checksum

    def _directory(self, cluster):
        raw = self._read_directory_bytes(cluster)
        entries = []
        pos = 0
        while pos + 32 <= len(raw):
            kind = raw[pos]
            if kind == 0x00:
                break
            if kind != 0x85:
                pos += 32
                continue
            secondary_count = raw[pos + 1]
            if secondary_count < 2 or secondary_count > 18:
                raise ExfatReadError("exFAT file entry set has invalid secondary count")
            total = (secondary_count + 1) * 32
            if pos + total > len(raw):
                raise ExfatReadError("truncated exFAT file entry set")
            entry_set = raw[pos : pos + total]
            stored_checksum = struct.unpack_from("<H", entry_set, 2)[0]
            if stored_checksum != self._entry_set_checksum(entry_set):
                raise ExfatReadError("exFAT file entry-set checksum mismatch")
            stream = raw[pos + 32 : pos + 64]
            if stream[0] != 0xC0:
                raise ExfatReadError("exFAT file entry lacks stream extension")
            if stream[1] & ~0x03:
                raise ExfatReadError("exFAT stream extension has reserved flags")
            name_length = stream[3]
            if name_length == 0 or name_length > 15 * (secondary_count - 1):
                raise ExfatReadError("exFAT filename length exceeds entry set")
            if (name_length + 14) // 15 != secondary_count - 1:
                raise ExfatReadError("exFAT filename entry count is invalid")
            attributes = struct.unpack_from("<H", raw, pos + 4)[0]
            if attributes & ~0x003F:
                raise ExfatReadError("exFAT file entry has reserved attributes")
            first_cluster = struct.unpack_from("<I", stream, 20)[0]
            size = struct.unpack_from("<Q", stream, 24)[0]
            no_fat_chain = bool(stream[1] & 0x02)
            name_raw = bytearray()
            for index in range(2, secondary_count + 1):
                item = raw[pos + index * 32 : pos + (index + 1) * 32]
                if item[0] != 0xC1:
                    raise ExfatReadError("exFAT file entry has invalid name entry")
                name_raw.extend(item[2:32])
            name = self._decode_name(bytes(name_raw[: name_length * 2]))
            stored_name_hash = struct.unpack_from("<H", stream, 4)[0]
            if stored_name_hash != self._name_hash(name):
                raise ExfatReadError("exFAT filename hash mismatch")
            entries.append(
                _Entry(
                    name,
                    bool(attributes & 0x10),
                    first_cluster,
                    size,
                    no_fat_chain,
                )
            )
            pos += total
        return entries

    def system_metadata(self):
        """Return and validate the mandatory allocation bitmap/upcase entries."""
        raw = self._read_directory_bytes(self.profile.root_cluster)
        bitmap = None
        upcase = None
        for pos in range(0, len(raw) - 31, 32):
            kind = raw[pos]
            if kind == 0:
                break
            if kind == 0x81 and bitmap is None:
                bitmap = (
                    raw[pos + 1],
                    struct.unpack_from("<I", raw, pos + 20)[0],
                    struct.unpack_from("<Q", raw, pos + 24)[0],
                )
            elif kind == 0x82 and upcase is None:
                upcase = (
                    struct.unpack_from("<I", raw, pos + 4)[0],
                    struct.unpack_from("<I", raw, pos + 20)[0],
                    struct.unpack_from("<Q", raw, pos + 24)[0],
                )
        if bitmap is None:
            raise ExfatReadError("exFAT allocation bitmap entry is missing")
        if upcase is None:
            raise ExfatReadError("exFAT upcase table entry is missing")
        bitmap_flags, bitmap_cluster, bitmap_size = bitmap
        if bitmap_flags & 0xFE or bitmap_size < (self.profile.cluster_count + 7) // 8:
            raise ExfatReadError("exFAT allocation bitmap geometry is invalid")
        bitmap_data = self._read_stream(bitmap_cluster, bitmap_size, False)
        for cluster in range(2, self.profile.cluster_count + 2):
            bit = bool(bitmap_data[(cluster - 2) >> 3] & (1 << ((cluster - 2) & 7)))
            fat_value = self._fat_entry(cluster)
            if bit != (fat_value != 0):
                raise ExfatReadError(
                    "exFAT allocation bitmap/FAT metadata is inconsistent"
                )
        upcase_checksum, upcase_cluster, upcase_size = upcase
        if (
            upcase_size == 0
            or upcase_size > self.profile.cluster_count * self.profile.cluster_size
        ):
            raise ExfatReadError("exFAT upcase table size is invalid")
        upcase_data = self._read_stream(upcase_cluster, upcase_size, False)
        checksum = 0
        for value in upcase_data:
            checksum = ((checksum >> 1) | ((checksum & 1) << 31)) & 0xFFFFFFFF
            checksum = (checksum + value) & 0xFFFFFFFF
        if checksum != upcase_checksum:
            raise ExfatReadError("exFAT upcase table checksum mismatch")
        return {
            "bitmap_cluster": bitmap_cluster,
            "bitmap_size": bitmap_size,
            "upcase_cluster": upcase_cluster,
            "upcase_size": upcase_size,
        }

    @staticmethod
    def _parts(path):
        if not isinstance(path, str) or not path.startswith("/"):
            raise ExfatReadError("exFAT paths must be absolute")
        parts = [part for part in path.split("/") if part]
        if any(part in (".", "..") for part in parts):
            raise ExfatReadError("parent traversal is not allowed")
        return parts

    def _find(self, path):
        current = _Entry("/", True, self.profile.root_cluster, 0, False)
        for part in self._parts(path):
            if not current.is_dir:
                raise NotADirectoryError(path)
            match = next(
                (
                    item
                    for item in self._directory(current.first_cluster)
                    if item.name == part or item.name.casefold() == part.casefold()
                ),
                None,
            )
            if match is None:
                raise FileNotFoundError(path)
            current = match
        return current

    def list_dir(self, path="/"):
        entry = self._find(path)
        if not entry.is_dir:
            raise NotADirectoryError(path)
        return [
            {
                "name": item.name,
                "is_dir": item.is_dir,
                "size": 0 if item.is_dir else item.size,
                "date": "",
            }
            for item in sorted(
                self._directory(entry.first_cluster),
                key=lambda item: (not item.is_dir, item.name.casefold()),
            )
        ]

    def file_size(self, path):
        entry = self._find(path)
        if entry.is_dir:
            raise IsADirectoryError(path)
        return entry.size

    def read_file_range(self, path, offset=0, length=1 << 20):
        entry = self._find(path)
        if entry.is_dir:
            raise IsADirectoryError(path)
        if offset < 0 or length < 0 or length > _MAX_READ:
            raise ExfatReadError("invalid or excessive exFAT read range")
        if offset >= entry.size:
            return b""
        length = min(length, entry.size - offset)
        return self._read_stream_range(
            entry.first_cluster,
            entry.size,
            offset,
            length,
            entry.no_fat_chain,
        )

    def read_file(self, path):
        return self.read_file_range(path, 0, self.file_size(path))


__all__ = ["ExfatReadError", "NativeExfatImage"]
