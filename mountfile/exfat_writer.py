# SPDX-License-Identifier: MIT
"""Transactional, bounded staged exFAT writer.

The writer intentionally supports only operations with fully known allocation
metadata. Every mutation is made on a complete image copy, read-back validated,
and committed only after a source extent hash check. Unsupported profiles fail
closed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path

from mountfile.exfat_probe import ExfatProbeError, probe_exfat_boot_region
from mountfile.exfat_reader import ExfatReadError, NativeExfatImage
from mountfile.safety import RefusalCode, RefusalError, checked_range


class ExfatWriteError(RuntimeError):
    """The bounded native exFAT write profile rejected the operation."""

    def __init__(self, message: str, code: RefusalCode | None = None):
        super().__init__(message)
        self.code = code


class _NoFreeDirectorySlot(ExfatWriteError):
    """No contiguous free entry-set range exists in the current directory."""


_EOC = 0xFFFFFFFF


@contextmanager
def _image_lock(path):
    image = Path(path).resolve()
    identity = hashlib.sha256(os.fsencode(str(image))).hexdigest()[:32]
    lock_path = image.parent / f".mountfile-{identity}.lock"
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    stream = None
    acquired = False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("exFAT lock path is not a regular file")
        stream = os.fdopen(fd, "r+b", buffering=0)
        fd = None
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
            stream.flush()
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        acquired = True
        yield
    finally:
        try:
            if acquired and stream is not None:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            if stream is not None:
                stream.close()
            elif fd is not None:
                os.close(fd)


def _image_identity(path):
    """Return a stable full-image identity or fail if it changes while hashing."""
    path = Path(path)
    try:
        link_info = path.lstat()
    except OSError as exc:
        raise ExfatWriteError("cannot inspect exFAT image path") from exc
    reparse_point = bool(
        getattr(link_info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )
    if stat.S_ISLNK(link_info.st_mode) or reparse_point:
        raise ExfatWriteError(
            "exFAT image target must not be a symlink or reparse point"
        )
    if not stat.S_ISREG(link_info.st_mode):
        raise ExfatWriteError("exFAT image target is not a regular file")
    if getattr(link_info, "st_nlink", 1) != 1:
        raise ExfatWriteError("exFAT image target must not have multiple hard links")

    digest = hashlib.sha256()
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            same_path_object = os.name == "nt" or (
                (link_info.st_dev, link_info.st_ino) == (before.st_dev, before.st_ino)
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or getattr(before, "st_nlink", 1) != 1
                or not same_path_object
            ):
                raise ExfatWriteError("exFAT image path changed during validation")
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(stream.fileno())
    except OSError as exc:
        raise ExfatWriteError("cannot establish full exFAT image identity") from exc

    def signature(metadata):
        return (
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    # Windows may report different st_dev/st_ino values for a path stat and
    # its open handle. Compare metadata on the same descriptor; a concurrent
    # path replacement is detected when the next identity is computed at
    # commit, while in-place changes during hashing alter these timestamps.
    if signature(before) != signature(after):
        raise ExfatWriteError("exFAT image changed while establishing identity")
    return after.st_size, digest.digest()


class NativeExfatWriter:
    """Allocation-aware writer for an explicitly bounded exFAT extent."""

    def __init__(self, image_path, *, offset=0, volume_size=None):
        if volume_size is None:
            raise ExfatWriteError("exFAT writer requires an explicit volume extent")
        self.image = Path(os.path.abspath(os.fspath(image_path)))
        _image_identity(self.image)
        self.offset = int(offset)
        self.volume_size = int(volume_size)
        try:
            self.profile = probe_exfat_boot_region(
                self.image, offset=self.offset, volume_size=self.volume_size
            )
        except (OSError, ExfatProbeError) as exc:
            raise ExfatWriteError(str(exc)) from exc
        if self.profile.volume_flags != 0:
            raise ExfatWriteError("exFAT writer rejects nonzero volume flags")
        if self.profile.number_of_fats != 1:
            raise ExfatWriteError("bounded exFAT writer requires exactly one FAT")
        self.extent_size = self.profile.volume_length * self.profile.bytes_per_sector
        self._source_image_identity = _image_identity(self.image)

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
        try:
            return NativeExfatImage(
                stage, offset=self.offset, volume_size=self.volume_size
            )
        except ExfatReadError as exc:
            raise ExfatWriteError(str(exc)) from exc

    def _read_rel(self, stream, relative, size):
        try:
            start, _ = checked_range(relative, size, upper=self.extent_size)
        except RefusalError as exc:
            raise ExfatWriteError(
                "exFAT staged access exceeds selected extent", exc.code
            ) from exc
        absolute = self.offset + start
        stream.seek(absolute)
        data = stream.read(size)
        if len(data) != size:
            raise ExfatWriteError("short read from staged exFAT image")
        return data

    def _write_rel(self, stream, relative, data):
        try:
            start, _ = checked_range(relative, len(data), upper=self.extent_size)
        except RefusalError as exc:
            raise ExfatWriteError(
                "exFAT staged write exceeds selected extent", exc.code
            ) from exc
        absolute = self.offset + start
        stream.seek(absolute)
        if stream.write(data) != len(data):
            raise ExfatWriteError("short write to staged exFAT image")

    def _directory_records(self, reader, cluster):
        raw = reader._read_directory_bytes(cluster)
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
            entry_set = raw[pos : pos + total]
            stored_checksum = struct.unpack_from("<H", entry_set, 2)[0]
            if stored_checksum != NativeExfatImage._entry_set_checksum(entry_set):
                raise ExfatWriteError("exFAT file entry-set checksum mismatch")
            stream = raw[pos + 32 : pos + 64]
            if stream[0] != 0xC0:
                raise ExfatWriteError("invalid exFAT stream extension")
            if stream[1] & ~0x03:
                raise ExfatWriteError("exFAT stream extension has reserved flags")
            name_len = stream[3]
            if name_len == 0 or name_len > 15 * (secondary_count - 1):
                raise ExfatWriteError("exFAT filename length exceeds entry set")
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
            if struct.unpack_from("<H", stream, 4)[0] != self._name_hash(name):
                raise ExfatWriteError("exFAT filename hash mismatch")
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

    def _entry_offset(self, reader, directory_cluster, logical_offset):
        chain = reader._chain(directory_cluster, no_fat_chain=False)
        cluster_index, cluster_offset = divmod(
            logical_offset, reader.profile.cluster_size
        )
        if cluster_index >= len(chain):
            raise ExfatWriteError("directory entry offset exceeds its cluster chain")
        return reader._cluster_offset(chain[cluster_index]) + cluster_offset

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

    def _allocation_owners(self, reader, meta):
        owners = {}

        def claim(chain, owner):
            for cluster in chain:
                previous = owners.get(cluster)
                if previous is not None:
                    raise ExfatWriteError(
                        f"exFAT cluster {cluster} is shared by {previous} and {owner}"
                    )
                owners[cluster] = owner

        def checked_chain(first_cluster, size, no_fat_chain):
            try:
                return reader._chain(first_cluster, size, no_fat_chain=no_fat_chain)
            except ExfatReadError as exc:
                raise ExfatWriteError(str(exc)) from exc

        claim(checked_chain(reader.profile.root_cluster, 0, False), "root directory")
        claim(
            checked_chain(meta["bitmap_cluster"], meta["bitmap_size"], False),
            "allocation bitmap",
        )
        claim(
            checked_chain(meta["upcase_cluster"], meta["upcase_size"], False),
            "up-case table",
        )

        pending = [("/", reader.profile.root_cluster, 0)]
        visited_directories = {reader.profile.root_cluster}
        scanned_entries = 0
        cluster_size = reader.profile.cluster_size
        while pending:
            directory_path, directory_cluster, depth = pending.pop()
            if depth > 128:
                raise ExfatWriteError("exFAT directory tree exceeds depth limit")
            try:
                records = self._directory_records(reader, directory_cluster)
            except ExfatReadError as exc:
                raise ExfatWriteError(str(exc)) from exc
            names = set()
            for record in records:
                scanned_entries += 1
                if scanned_entries > 100_000:
                    raise ExfatWriteError("exFAT allocation scan exceeds entry limit")
                name, _position, _total, stream, entry_set = record
                if name in names or name in (".", ".."):
                    raise ExfatWriteError(
                        "exFAT directory has an unsafe duplicate name"
                    )
                names.add(name)
                if "/" in name or "\\" in name:
                    raise ExfatWriteError("exFAT directory has an unsafe filename")
                path = (
                    directory_path.rstrip("/") + "/" + name
                    if directory_path != "/"
                    else "/" + name
                )
                attributes = struct.unpack_from("<H", entry_set, 4)[0]
                is_directory = bool(attributes & 0x10)
                no_fat_chain = bool(stream[1] & 0x02)
                valid_data_length = struct.unpack_from("<Q", stream, 8)[0]
                first_cluster = struct.unpack_from("<I", stream, 20)[0]
                size = struct.unpack_from("<Q", stream, 24)[0]
                if valid_data_length > size:
                    raise ExfatWriteError("exFAT ValidDataLength exceeds DataLength")
                if is_directory:
                    if (
                        not first_cluster
                        or not size
                        or size % cluster_size
                        or valid_data_length != size
                    ):
                        raise ExfatWriteError(
                            "exFAT directory stream lengths are inconsistent"
                        )
                    chain = checked_chain(first_cluster, size, no_fat_chain)
                    if len(chain) * cluster_size != size:
                        raise ExfatWriteError(
                            "exFAT directory length disagrees with its cluster chain"
                        )
                    claim(chain, path)
                    if first_cluster in visited_directories:
                        raise ExfatWriteError("exFAT directory tree contains a cycle")
                    visited_directories.add(first_cluster)
                    pending.append((path, first_cluster, depth + 1))
                elif size:
                    chain = checked_chain(first_cluster, size, no_fat_chain)
                    claim(chain, path)
                elif first_cluster or valid_data_length:
                    raise ExfatWriteError(
                        "empty exFAT file has a non-empty stream allocation"
                    )
        return owners

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

    def _free_clusters(self, bitmap, reader, needed, stream):
        result = []
        for cluster in range(2, reader.profile.cluster_count + 2):
            if not (bitmap[(cluster - 2) >> 3] & (1 << ((cluster - 2) & 7))):
                fat_relative = (
                    reader.profile.fat_offset * reader.profile.bytes_per_sector
                    + cluster * 4
                )
                fat_value = struct.unpack(
                    "<I", self._read_rel(stream, fat_relative, 4)
                )[0]
                if fat_value != 0:
                    raise ExfatWriteError(
                        "exFAT allocation bitmap/FAT metadata is inconsistent"
                    )
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
        for cluster in reader._chain(directory_cluster, no_fat_chain=False):
            raw = reader._read_rel(
                reader._cluster_offset(cluster), reader.profile.cluster_size
            )
            for pos in range(0, len(raw) - required * 32 + 1, 32):
                if all(raw[pos + n * 32] in (0, 0x05) for n in range(required)):
                    return cluster, pos
        raise _NoFreeDirectorySlot("no existing contiguous free directory entry set")

    def _append_directory_cluster(
        self, reader, stream, bitmap, directory_cluster, directory_path
    ):
        chain = reader._chain(directory_cluster, no_fat_chain=False)
        if not chain or len(chain) >= reader.profile.cluster_count:
            raise ExfatWriteError("exFAT directory has reached cluster capacity")
        cluster_size = reader.profile.cluster_size
        current_size = len(chain) * cluster_size
        parent_cluster = None
        parent_record = None
        if directory_path == "/":
            if directory_cluster != reader.profile.root_cluster:
                raise ExfatWriteError("root directory cluster identity mismatch")
        else:
            parent_cluster, parent_record = self._locate(reader, directory_path)
            entry = reader._find(directory_path)
            valid_data_length = struct.unpack_from("<Q", parent_record[3], 8)[0]
            if (
                not entry.is_dir
                or entry.first_cluster != directory_cluster
                or entry.size != current_size
                or valid_data_length != current_size
            ):
                raise ExfatWriteError(
                    "exFAT directory stream length disagrees with its cluster chain"
                )
        terminator = None
        for index, existing_cluster in enumerate(chain):
            raw = reader._read_rel(
                reader._cluster_offset(existing_cluster), cluster_size
            )
            terminator_offset = next(
                (offset for offset in range(0, len(raw), 32) if raw[offset] == 0),
                None,
            )
            if terminator_offset is not None:
                terminator = (index, terminator_offset)
                break
        if terminator is not None:
            start_index, start_offset = terminator
            for index in range(start_index, len(chain)):
                first_offset = start_offset if index == start_index else 0
                for offset in range(first_offset, cluster_size, 32):
                    self._write_rel(
                        stream,
                        reader._cluster_offset(chain[index]) + offset,
                        b"\x05",
                    )
        cluster = self._free_clusters(bitmap, reader, 1, stream)[0]
        self._fat_write(stream, reader, chain[-1], cluster)
        self._fat_write(stream, reader, cluster, _EOC)
        self._set_allocated(bitmap, cluster, True)
        self._write_rel(
            stream,
            reader._cluster_offset(cluster),
            b"\0" * cluster_size,
        )
        if parent_record is not None:
            entry_offset = self._entry_offset(reader, parent_cluster, parent_record[1])
            new_size = current_size + cluster_size
            self._write_rel(stream, entry_offset + 32 + 8, struct.pack("<Q", new_size))
            self._write_rel(stream, entry_offset + 32 + 24, struct.pack("<Q", new_size))
            self._rewrite_checksum(stream, entry_offset, parent_record[2])
        return cluster

    def _directory_slot(
        self, reader, stream, bitmap, directory_cluster, directory_path, required
    ):
        try:
            return self._free_slot(reader, directory_cluster, required)
        except _NoFreeDirectorySlot:
            cluster = self._append_directory_cluster(
                reader, stream, bitmap, directory_cluster, directory_path
            )
            return cluster, 0

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
        struct.pack_into("<Q", result, 40, size)
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
            if _image_identity(self.image) != self._source_image_identity:
                raise ExfatWriteError(
                    "source image changed concurrently; refusing commit"
                )
            staged_identity = _image_identity(stage)
            os.replace(stage, self.image)
            self._source_image_identity = staged_identity
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
                verified = self._reader(stage)
                try:
                    self._metadata(verified)
                finally:
                    verified.close()
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
            directory_path = "/" + "/".join(parts[:-1])
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
                        self._free_clusters(bitmap, reader, count, stream)
                        if count
                        else []
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
                    slot_cluster, slot_offset = self._directory_slot(
                        reader,
                        stream,
                        bitmap,
                        directory,
                        directory_path,
                        len(entry_set) // 32,
                    )
                    self._write_rel(
                        stream,
                        reader._cluster_offset(slot_cluster) + slot_offset,
                        entry_set,
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
                            bitmap, reader, needed - len(old_chain), stream
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
                    relative = self._entry_offset(reader, directory, record[1]) + 32
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
                        stream,
                        self._entry_offset(reader, directory, record[1]),
                        record[2],
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
        if not src_parts or not dst_parts:
            raise ExfatWriteError("root directory cannot be renamed")
        src_path = "/" + "/".join(src_parts)
        dst_path = "/" + "/".join(dst_parts)
        src_parent_path = "/" + "/".join(src_parts[:-1])
        dst_parent_path = "/" + "/".join(dst_parts[:-1])
        new_name = dst_parts[-1]

        def action(reader, stage):
            meta = self._metadata(reader)
            if src_path == dst_path:
                return
            directory, record = self._locate(reader, src_path)
            destination = reader._find(dst_parent_path)
            if not destination.is_dir:
                raise NotADirectoryError(dst_parent_path)
            destination_directory, _ = self._parent(reader, dst_path)
            source_entry = reader._find(src_path)
            same_directory = src_parent_path == dst_parent_path
            if source_entry.is_dir and (
                dst_parent_path == src_path
                or dst_parent_path.startswith(src_path.rstrip("/") + "/")
            ):
                raise ExfatWriteError("cannot move an exFAT directory into itself")
            if any(
                item[0] == new_name
                for item in self._directory_records(reader, destination_directory)
                if not (same_directory and item[1] == record[1])
            ):
                raise FileExistsError(dst)
            slots = (record[2] // 32) - 2
            encoded = new_name.encode("utf-16le")
            if len(encoded) > slots * 30:
                raise ExfatWriteError("rename exceeds allocated name entries")
            with stage.open("r+b") as stream:
                if same_directory:
                    relative = self._entry_offset(reader, directory, record[1])
                    self._write_rel(
                        stream, relative + 32 + 3, bytes([len(encoded) // 2])
                    )
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
                else:
                    entry_set = bytearray(record[4])
                    entry_set[32 + 3] = len(encoded) // 2
                    struct.pack_into("<H", entry_set, 32 + 4, self._name_hash(new_name))
                    padded = encoded.ljust(slots * 30, b"\xff")
                    for index in range(slots):
                        base = 64 + index * 32
                        entry_set[base + 2 : base + 32] = padded[
                            index * 30 : (index + 1) * 30
                        ]
                    struct.pack_into("<H", entry_set, 2, 0)
                    struct.pack_into(
                        "<H", entry_set, 2, self._entry_checksum(entry_set)
                    )
                    bitmap = self._bitmap(stream, reader, meta)
                    slot_cluster, slot_offset = self._directory_slot(
                        reader,
                        stream,
                        bitmap,
                        destination_directory,
                        dst_parent_path,
                        len(entry_set) // 32,
                    )
                    destination_offset = (
                        reader._cluster_offset(slot_cluster) + slot_offset
                    )
                    self._write_rel(stream, destination_offset, entry_set)
                    source_offset = self._entry_offset(reader, directory, record[1])
                    for offset in range(0, record[2], 32):
                        entry_type = record[4][offset]
                        self._write_rel(
                            stream,
                            source_offset + offset,
                            bytes([entry_type & 0x7F]),
                        )
                    self._write_bitmap(stream, reader, meta, bitmap)
                stream.flush()
                os.fsync(stream.fileno())
            validated = self._reader(stage)
            try:
                moved_entry = validated._find(dst_path)
                if (
                    moved_entry.name != new_name
                    or moved_entry.is_dir != source_entry.is_dir
                    or moved_entry.first_cluster != source_entry.first_cluster
                    or moved_entry.size != source_entry.size
                ):
                    raise ExfatWriteError("exFAT rename read-back validation failed")
                if not same_directory:
                    try:
                        validated._find(src_path)
                    except FileNotFoundError:
                        pass
                    else:
                        raise ExfatWriteError(
                            "exFAT move left the source directory entry active"
                        )
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
            parts = self._parts(path)
            directory_path = "/" + "/".join(parts[:-1])
            directory, name = self._parent(reader, path)
            with stage.open("r+b") as stream:
                bitmap = self._bitmap(stream, reader, meta)
                cluster = self._free_clusters(bitmap, reader, 1, stream)[0]
                self._set_allocated(bitmap, cluster, True)
                self._fat_write(stream, reader, cluster, _EOC)
                self._write_rel(
                    stream,
                    reader._cluster_offset(cluster),
                    b"\0" * reader.profile.cluster_size,
                )
                entry_set = self._make_entry_set(
                    name,
                    is_dir=True,
                    first_cluster=cluster,
                    size=reader.profile.cluster_size,
                )
                slot_cluster, slot_offset = self._directory_slot(
                    reader,
                    stream,
                    bitmap,
                    directory,
                    directory_path,
                    len(entry_set) // 32,
                )
                self._write_rel(
                    stream,
                    reader._cluster_offset(slot_cluster) + slot_offset,
                    entry_set,
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
        parts = self._parts(path)
        canonical_path = "/" + "/".join(parts)

        def action(reader, stage):
            meta = self._metadata(reader)
            owners = self._allocation_owners(reader, meta)
            with stage.open("r+b") as stream:
                bitmap = self._bitmap(stream, reader, meta)

                def remove_entry(target, depth=0):
                    if depth > 128:
                        raise ExfatWriteError(
                            "recursive exFAT delete exceeds depth limit"
                        )
                    directory, record = self._locate(reader, target)
                    entry = reader._find(target)
                    if entry.is_dir:
                        children = self._directory_records(reader, entry.first_cluster)
                        if children and not recursive:
                            raise ExfatWriteError(
                                "bounded exFAT delete refuses non-empty directory"
                            )
                        valid_data_length = struct.unpack_from("<Q", record[3], 8)[0]
                        chain = reader._chain(
                            entry.first_cluster,
                            entry.size,
                            no_fat_chain=entry.no_fat_chain,
                        )
                        if (
                            not entry.size
                            or entry.size % reader.profile.cluster_size
                            or valid_data_length != entry.size
                            or len(chain) * reader.profile.cluster_size != entry.size
                        ):
                            raise ExfatWriteError(
                                "exFAT directory stream lengths are inconsistent"
                            )
                        for child in children:
                            child_path = target.rstrip("/") + "/" + child[0]
                            remove_entry(child_path, depth + 1)
                    elif entry.size:
                        chain = reader._chain(
                            entry.first_cluster,
                            entry.size,
                            no_fat_chain=entry.no_fat_chain,
                        )
                    else:
                        if entry.first_cluster:
                            raise ExfatWriteError(
                                "empty exFAT file has a cluster allocation"
                            )
                        chain = []

                    for cluster in chain:
                        if owners.get(cluster) != target:
                            raise ExfatWriteError(
                                "refusing to free an unowned exFAT cluster"
                            )
                        self._set_allocated(bitmap, cluster, False)
                        self._fat_write(stream, reader, cluster, 0)

                    relative = self._entry_offset(reader, directory, record[1])
                    for offset in range(0, record[2], 32):
                        entry_type = record[4][offset]
                        if not entry_type & 0x80:
                            raise ExfatWriteError(
                                "exFAT entry set contains an inactive secondary"
                            )
                        self._write_rel(
                            stream,
                            relative + offset,
                            bytes([entry_type & 0x7F]),
                        )

                remove_entry(canonical_path)
                self._write_bitmap(stream, reader, meta, bitmap)
                stream.flush()
                os.fsync(stream.fileno())
            validated = self._reader(stage)
            try:
                try:
                    validated._find(canonical_path)
                except FileNotFoundError:
                    pass
                else:
                    raise ExfatWriteError("exFAT delete read-back validation failed")
            finally:
                validated.close()

        self._mutate("delete", action)


__all__ = ["ExfatWriteError", "NativeExfatWriter"]
