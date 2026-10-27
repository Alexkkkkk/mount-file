# SPDX-License-Identifier: MIT
"""Transactional, bounded staged exFAT writer.

The writer intentionally supports only operations that fit in an existing
 directory-chain and have fully known allocation metadata.  Every mutation is
made on a complete image copy, read-back validated, and committed only after a
source extent hash check.  Directory growth, recursive deletion and unknown
metadata fail closed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path

from mountfile.exfat_probe import ExfatProbeError, probe_exfat_boot_region
from mountfile.exfat_reader import ExfatReadError, NativeExfatImage


class ExfatWriteError(RuntimeError):
    """The bounded native exFAT write profile rejected the operation."""


_EOC = 0xFFFFFFFF


@contextmanager
def _image_lock(path):
    stream = open(path, "r+b")
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def _hash_extent(path, offset, length):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        stream.seek(offset)
        remaining = length
        while remaining:
            chunk = stream.read(min(1 << 20, remaining))
            if not chunk:
                raise ExfatWriteError("short read while hashing exFAT extent")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.digest()


class NativeExfatWriter:
    """Allocation-aware writer for an explicitly bounded exFAT extent."""

    def __init__(self, image_path, *, offset=0, volume_size=None):
        if volume_size is None:
            raise ExfatWriteError("exFAT writer requires an explicit volume extent")
        self.image = Path(image_path)
        self.offset = int(offset)
        self.volume_size = int(volume_size)
        try:
            self.profile = probe_exfat_boot_region(
                self.image, offset=self.offset, volume_size=self.volume_size
            )
        except (OSError, ExfatProbeError) as exc:
            raise ExfatWriteError(str(exc)) from exc
        if self.profile.number_of_fats != 1:
            raise ExfatWriteError("bounded exFAT writer requires exactly one FAT")
        self.extent_size = self.profile.volume_length * self.profile.bytes_per_sector
        self._source_hash = _hash_extent(self.image, self.offset, self.extent_size)

    def _copy_stage(self):
        handle = tempfile.NamedTemporaryFile(
            prefix=f".{self.image.name}.exfat-",
            suffix=".stage",
            dir=self.image.parent,
            delete=False,
        )
        stage = Path(handle.name)
        handle.close()
        try:
            shutil.copy2(self.image, stage)
            return stage
        except Exception:
            stage.unlink(missing_ok=True)
            raise

    @staticmethod
    def _parts(path):
        if not isinstance(path, str) or not path.startswith("/"):
            raise ExfatWriteError("exFAT paths must be absolute")
        parts = [part for part in path.split("/") if part]
        if any(part in (".", "..") or "\x00" in part for part in parts):
            raise ExfatWriteError("unsafe exFAT path")
        return parts

    def _reader(self, stage):
        return NativeExfatImage(stage, offset=self.offset, volume_size=self.volume_size)

    def _read_rel(self, stream, relative, size):
        absolute = self.offset + relative
        if relative < 0 or size < 0 or absolute + size > self.offset + self.extent_size:
            raise ExfatWriteError("exFAT staged access exceeds selected extent")
        stream.seek(absolute)
        data = stream.read(size)
        if len(data) != size:
            raise ExfatWriteError("short read from staged exFAT image")
        return data

    def _write_rel(self, stream, relative, data):
        absolute = self.offset + relative
        if relative < 0 or absolute + len(data) > self.offset + self.extent_size:
            raise ExfatWriteError("exFAT staged write exceeds selected extent")
        stream.seek(absolute)
        if stream.write(data) != len(data):
            raise ExfatWriteError("short write to staged exFAT image")

    def _directory_records(self, reader, cluster):
        raw = b"".join(
            reader._read_rel(reader._cluster_offset(item), reader.profile.cluster_size)
            for item in reader._chain(cluster, no_fat_chain=False)
        )
        records = []
        pos = 0
        while pos + 32 <= len(raw):
            kind = raw[pos]
            if kind == 0:
                break
            if kind != 0x85:
                pos += 32
                continue
            secondary_count = raw[pos + 1]
            total = (secondary_count + 1) * 32
            if secondary_count < 2 or pos + total > len(raw):
                raise ExfatWriteError("truncated exFAT directory entry set")
            stream = raw[pos + 32 : pos + 64]
            if stream[0] != 0xC0:
                raise ExfatWriteError("invalid exFAT stream extension")
            name_len = stream[3]
            name_raw = bytearray()
            for index in range(2, secondary_count + 1):
                item = raw[pos + index * 32 : pos + (index + 1) * 32]
                if item[0] != 0xC1:
                    raise ExfatWriteError("invalid exFAT filename entry")
                name_raw.extend(item[2:32])
            try:
                name = bytes(name_raw[: name_len * 2]).decode("utf-16le")
            except UnicodeDecodeError as exc:
                raise ExfatWriteError("invalid exFAT filename encoding") from exc
            records.append((name, pos, total, stream, raw[pos : pos + total]))
            pos += total
        return records

    def _locate(self, reader, path):
        parts = self._parts(path)
        if not parts:
            raise ExfatWriteError("root directory cannot be mutated")
        current = reader.profile.root_cluster
        for index, wanted in enumerate(parts):
            record = next(
                (x for x in self._directory_records(reader, current) if x[0] == wanted),
                None,
            )
            if record is None:
                raise FileNotFoundError(path)
            if index == len(parts) - 1:
                return current, record
            entry = reader._find("/" + "/".join(parts[: index + 1]))
            if not entry.is_dir:
                raise NotADirectoryError(path)
            current = entry.first_cluster
        raise FileNotFoundError(path)

    def _parent(self, reader, path):
        parts = self._parts(path)
        if not parts:
            raise ExfatWriteError("root directory cannot be mutated")
        parent = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        entry = reader._find(parent)
        if not entry.is_dir:
            raise NotADirectoryError(parent)
        return entry.first_cluster, parts[-1]

    @staticmethod
    def _name_hash(name):
        checksum = 0
        for value in name.encode("utf-16le"):
            checksum = ((checksum << 15) | (checksum >> 1)) & 0xFFFF
            checksum = (checksum + value) & 0xFFFF
        return checksum

    @staticmethod
    def _entry_checksum(entry_set):
        checksum = 0
        for index, value in enumerate(entry_set):
            if index in (2, 3):
                value = 0
            checksum = ((checksum << 15) | (checksum >> 1)) & 0xFFFF
            checksum = (checksum + value) & 0xFFFF
        return checksum

    def _metadata(self, reader):
        try:
            meta = reader.system_metadata()
        except ExfatReadError as exc:
            raise ExfatWriteError(str(exc)) from exc
        if meta["bitmap_size"] < (reader.profile.cluster_count + 7) // 8:
            raise ExfatWriteError("exFAT allocation bitmap is too small")
        return meta

    def _bitmap(self, stream, reader, meta):
        return bytearray(
            self._read_stream_at(
                stream, reader, meta["bitmap_cluster"], meta["bitmap_size"]
            )
        )

    def _read_stream_at(self, stream, reader, start, size):
        output = bytearray()
        for cluster in reader._chain(start, size, no_fat_chain=False):
            output.extend(
                self._read_rel(
                    stream, reader._cluster_offset(cluster), reader.profile.cluster_size
                )
            )
            if len(output) >= size:
                return bytes(output[:size])
        raise ExfatWriteError("short exFAT metadata stream")

    def _set_allocated(self, bitmap, cluster, allocated):
        index = cluster - 2
        if index < 0 or index >= self.profile.cluster_count:
            raise ExfatWriteError("exFAT cluster is outside allocation bitmap")
        mask = 1 << (index & 7)
        if allocated:
            bitmap[index >> 3] |= mask
        else:
            bitmap[index >> 3] &= ~mask

    def _free_clusters(self, bitmap, reader, needed):
        result = []
        for cluster in range(2, reader.profile.cluster_count + 2):
            if not (bitmap[(cluster - 2) >> 3] & (1 << ((cluster - 2) & 7))):
                result.append(cluster)
                if len(result) == needed:
                    return result
        raise ExfatWriteError("not enough free exFAT clusters")

    def _fat_write(self, stream, reader, cluster, value):
        self._write_rel(
            stream,
            reader.profile.fat_offset * reader.profile.bytes_per_sector + cluster * 4,
            struct.pack("<I", value),
        )

    def _write_bitmap(self, stream, reader, meta, bitmap):
        remaining = bytes(bitmap)
        for cluster in reader._chain(
            meta["bitmap_cluster"], meta["bitmap_size"], no_fat_chain=False
        ):
            chunk = remaining[: reader.profile.cluster_size]
            self._write_rel(
                stream,
                reader._cluster_offset(cluster),
                chunk.ljust(reader.profile.cluster_size, b"\0"),
            )
            remaining = remaining[reader.profile.cluster_size :]
            if not remaining:
                break

    def _free_slot(self, reader, directory_cluster, required):
        if required > reader.profile.cluster_size // 32:
            raise ExfatWriteError(
                "directory entry set is too large for bounded profile"
            )
        raw = b"".join(
            reader._read_rel(reader._cluster_offset(c), reader.profile.cluster_size)
            for c in reader._chain(directory_cluster, no_fat_chain=False)
        )
        for pos in range(0, len(raw) - required * 32 + 1, 32):
            if (
                pos % reader.profile.cluster_size + required * 32
                > reader.profile.cluster_size
            ):
                continue
            if all(raw[pos + n * 32] in (0, 0x05) for n in range(required)):
                return pos
        raise ExfatWriteError("no existing contiguous free directory entry set")

    def _make_entry_set(self, name, *, is_dir, first_cluster, size, no_fat_chain=False):
        encoded = name.encode("utf-16le")
        if not encoded or len(encoded) % 2 or len(encoded) > 3 * 30:
            raise ExfatWriteError("filename exceeds bounded exFAT name-entry profile")
        slots = (len(encoded) + 29) // 30
        result = bytearray((slots + 2) * 32)
        result[0] = 0x85
        result[1] = slots + 1
        struct.pack_into("<H", result, 4, 0x10 if is_dir else 0)
        result[32] = 0xC0
        result[33] = 0x02 if no_fat_chain else 0
        result[35] = len(encoded) // 2
        struct.pack_into("<H", result, 36, self._name_hash(name))
        struct.pack_into("<I", result, 52, first_cluster if size or is_dir else 0)
        struct.pack_into("<Q", result, 56, size)
        padded = encoded.ljust(slots * 30, b"\xff")
        for index in range(slots):
            base = 64 + index * 32
            result[base] = 0xC1
            result[base + 2 : base + 32] = padded[index * 30 : (index + 1) * 30]
        struct.pack_into("<H", result, 2, self._entry_checksum(result))
        return bytes(result)

    def _rewrite_checksum(self, stream, relative, total):
        raw = bytearray(self._read_rel(stream, relative, total))
        struct.pack_into("<H", raw, 2, 0)
        struct.pack_into("<H", raw, 2, self._entry_checksum(raw))
        self._write_rel(stream, relative, raw)

    def _commit_stage(self, stage):
        try:
            if (
                _hash_extent(self.image, self.offset, self.extent_size)
                != self._source_hash
            ):
                raise ExfatWriteError(
                    "source image changed concurrently; refusing commit"
                )
            os.replace(stage, self.image)
            self._source_hash = _hash_extent(self.image, self.offset, self.extent_size)
            stage = None
        finally:
            if stage is not None:
                stage.unlink(missing_ok=True)

    def _mutate(self, operation, validate):
        with _image_lock(self.image):
            stage = self._copy_stage()
            try:
                reader = self._reader(stage)
                try:
                    validate(reader, stage)
                finally:
                    reader.close()
                self._commit_stage(stage)
            except Exception:
                stage.unlink(missing_ok=True)
                raise

    def write_file(self, path, data):
        payload = bytes(data)

        def action(reader, stage):
            meta = self._metadata(reader)
            existing = None
            try:
                _dir, existing = self._locate(reader, path)
            except FileNotFoundError:
                pass
            parts = self._parts(path)
            with stage.open("r+b") as stream:
                bitmap = self._bitmap(stream, reader, meta)
                if existing is None:
                    directory, name = self._parent(reader, path)
                    if any(
                        x[0] == name for x in self._directory_records(reader, directory)
                    ):
                        raise FileExistsError(path)
                    count = (
                        len(payload) + reader.profile.cluster_size - 1
                    ) // reader.profile.cluster_size
                    clusters = (
                        self._free_clusters(bitmap, reader, count) if count else []
                    )
                    for index, cluster in enumerate(clusters):
                        self._set_allocated(bitmap, cluster, True)
                        self._fat_write(
                            stream,
                            reader,
                            cluster,
                            clusters[index + 1] if index + 1 < len(clusters) else _EOC,
                        )
                        chunk = payload[
                            index
                            * reader.profile.cluster_size : (index + 1)
                            * reader.profile.cluster_size
                        ]
                        self._write_rel(
                            stream,
                            reader._cluster_offset(cluster),
                            chunk.ljust(reader.profile.cluster_size, b"\0"),
                        )
                    entry_set = self._make_entry_set(
                        name,
                        is_dir=False,
                        first_cluster=clusters[0] if clusters else 0,
                        size=len(payload),
                    )
                    pos = self._free_slot(reader, directory, len(entry_set) // 32)
                    self._write_rel(
                        stream, reader._cluster_offset(directory) + pos, entry_set
                    )
                    self._write_bitmap(stream, reader, meta, bitmap)
                else:
                    directory, record = self._locate(reader, path)
                    entry = reader._find(path)
                    if entry.is_dir:
                        raise IsADirectoryError(path)
                    old_chain = (
                        reader._chain(
                            entry.first_cluster,
                            entry.size,
                            no_fat_chain=entry.no_fat_chain,
                        )
                        if entry.size
                        else []
                    )
                    needed = (
                        len(payload) + reader.profile.cluster_size - 1
                    ) // reader.profile.cluster_size
                    if needed > len(old_chain):
                        extra = self._free_clusters(
                            bitmap, reader, needed - len(old_chain)
                        )
                        chain = old_chain + extra
                        for cluster in extra:
                            self._set_allocated(bitmap, cluster, True)
                        for index, cluster in enumerate(chain):
                            self._fat_write(
                                stream,
                                reader,
                                cluster,
                                chain[index + 1] if index + 1 < len(chain) else _EOC,
                            )
                        no_fat = False
                    else:
                        chain = old_chain[:needed]
                        no_fat = entry.no_fat_chain
                        for cluster in old_chain[needed:]:
                            self._set_allocated(bitmap, cluster, False)
                            self._fat_write(stream, reader, cluster, 0)
                        if chain:
                            self._fat_write(stream, reader, chain[-1], _EOC)
                    if needed == 0:
                        chain = []
                    for index, cluster in enumerate(chain):
                        chunk = payload[
                            index
                            * reader.profile.cluster_size : (index + 1)
                            * reader.profile.cluster_size
                        ]
                        self._write_rel(
                            stream,
                            reader._cluster_offset(cluster),
                            chunk.ljust(reader.profile.cluster_size, b"\0"),
                        )
                    relative = reader._cluster_offset(directory) + record[1] + 32
                    self._write_rel(
                        stream, relative + 1, bytes([0x02 if no_fat else 0])
                    )
                    self._write_rel(
                        stream, relative + 8, struct.pack("<Q", len(payload))
                    )
                    self._write_rel(
                        stream,
                        relative + 20,
                        struct.pack("<I", chain[0] if chain else 0),
                    )
                    self._write_rel(
                        stream, relative + 24, struct.pack("<Q", len(payload))
                    )
                    self._rewrite_checksum(
                        stream, reader._cluster_offset(directory) + record[1], record[2]
                    )
                    self._write_bitmap(stream, reader, meta, bitmap)
                stream.flush()
                os.fsync(stream.fileno())
            validated = self._reader(stage)
            try:
                if validated.read_file(path) != payload:
                    raise ExfatWriteError("exFAT read-back validation failed")
            finally:
                validated.close()

        self._mutate("write_file", action)

    def rename(self, src, dst):
        src_parts, dst_parts = self._parts(src), self._parts(dst)
        if src_parts[:-1] != dst_parts[:-1]:
            raise ExfatWriteError("bounded exFAT rename supports one directory only")
        new_name = dst_parts[-1]

        def action(reader, stage):
            self._metadata(reader)
            directory, record = self._locate(reader, src)
            if any(
                item[0] == new_name
                for item in self._directory_records(reader, directory)
            ):
                raise FileExistsError(dst)
            slots = (record[2] // 32) - 2
            encoded = new_name.encode("utf-16le")
            if len(encoded) > slots * 30:
                raise ExfatWriteError("rename exceeds allocated name entries")
            relative = reader._cluster_offset(directory) + record[1]
            with stage.open("r+b") as stream:
                self._write_rel(stream, relative + 32 + 3, bytes([len(encoded) // 2]))
                self._write_rel(
                    stream,
                    relative + 32 + 4,
                    struct.pack("<H", self._name_hash(new_name)),
                )
                padded = encoded.ljust(slots * 30, b"\xff")
                for index in range(slots):
                    self._write_rel(
                        stream,
                        relative + 64 + index * 32 + 2,
                        padded[index * 30 : (index + 1) * 30],
                    )
                self._rewrite_checksum(stream, relative, record[2])
                stream.flush()
                os.fsync(stream.fileno())
            validated = self._reader(stage)
            try:
                if validated._find("/" + "/".join(dst_parts)).name != new_name:
                    raise ExfatWriteError("exFAT rename read-back validation failed")
            finally:
                validated.close()

        self._mutate("rename", action)

    def mkdir(self, path):
        def action(reader, stage):
            meta = self._metadata(reader)
            try:
                reader._find(path)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(path)
            directory, name = self._parent(reader, path)
            with stage.open("r+b") as stream:
                bitmap = self._bitmap(stream, reader, meta)
                cluster = self._free_clusters(bitmap, reader, 1)[0]
                self._set_allocated(bitmap, cluster, True)
                self._fat_write(stream, reader, cluster, _EOC)
                self._write_rel(
                    stream,
                    reader._cluster_offset(cluster),
                    b"\0" * reader.profile.cluster_size,
                )
                entry_set = self._make_entry_set(
                    name, is_dir=True, first_cluster=cluster, size=0
                )
                pos = self._free_slot(reader, directory, len(entry_set) // 32)
                self._write_rel(
                    stream, reader._cluster_offset(directory) + pos, entry_set
                )
                self._write_bitmap(stream, reader, meta, bitmap)
                stream.flush()
                os.fsync(stream.fileno())
            validated = self._reader(stage)
            try:
                if not validated._find(path).is_dir:
                    raise ExfatWriteError("exFAT mkdir read-back validation failed")
            finally:
                validated.close()

        self._mutate("mkdir", action)

    def delete(self, path, recursive=False):
        if recursive:
            raise ExfatWriteError("bounded exFAT delete refuses recursive removal")

        def action(reader, stage):
            meta = self._metadata(reader)
            directory, record = self._locate(reader, path)
            entry = reader._find(path)
            if entry.is_dir and self._directory_records(reader, entry.first_cluster):
                raise ExfatWriteError(
                    "bounded exFAT delete refuses non-empty directory"
                )
            if not entry.is_dir and entry.size:
                raise ExfatWriteError("bounded exFAT delete refuses non-empty file")
            with stage.open("r+b") as stream:
                bitmap = self._bitmap(stream, reader, meta)
                chain = []
                if entry.first_cluster:
                    chain = reader._chain(
                        entry.first_cluster,
                        reader.profile.cluster_size if entry.is_dir else entry.size,
                        no_fat_chain=entry.no_fat_chain,
                    )
                for cluster in chain:
                    self._set_allocated(bitmap, cluster, False)
                    self._fat_write(stream, reader, cluster, 0)
                # 0x05 marks the primary entry deleted.  Keep the secondary
                # slots intact: zeroing the whole set would create a 0x00
                # terminator and make valid entries after it unreachable.
                self._write_rel(
                    stream, reader._cluster_offset(directory) + record[1], b"\x05"
                )
                self._write_bitmap(stream, reader, meta, bitmap)
                stream.flush()
                os.fsync(stream.fileno())
            validated = self._reader(stage)
            try:
                try:
                    validated._find(path)
                except FileNotFoundError:
                    pass
                else:
                    raise ExfatWriteError("exFAT delete read-back validation failed")
            finally:
                validated.close()

        self._mutate("delete", action)


__all__ = ["ExfatWriteError", "NativeExfatWriter"]
