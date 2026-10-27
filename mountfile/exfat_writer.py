# SPDX-License-Identifier: MIT
"""Transactional, deliberately limited exFAT writer.

The phase-one profile only replaces an existing regular file without growing
its allocated stream and renames an existing same-directory entry. Both
operations are staged on a complete image copy, validated through the native
read-only provider, then atomically committed after a source-hash check.
Allocation, mkdir and delete are intentionally rejected until bitmap and full
entry-set transactions are implemented.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path

from mountfile.exfat_reader import ExfatReadError, NativeExfatImage
from mountfile.exfat_probe import ExfatProbeError, probe_exfat_boot_region


class ExfatWriteError(RuntimeError):
    """The limited native exFAT write profile rejected the operation."""


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
    """Phase-one transactional writer for an explicitly bounded exFAT extent."""

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
        if any(part in (".", "..") for part in parts):
            raise ExfatWriteError("parent traversal is not allowed")
        return parts

    def _reader(self, stage):
        return NativeExfatImage(stage, offset=self.offset, volume_size=self.volume_size)

    def _read_rel(self, stream, relative, size):
        absolute = self.offset + relative
        if relative < 0 or size < 0 or absolute + size > self.offset + self.extent_size:
            raise ExfatWriteError("exFAT staged write exceeds selected extent")
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
            if raw[pos] == 0:
                break
            if raw[pos] != 0x85:
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
        current_cluster = reader.profile.root_cluster
        parts = self._parts(path)
        if not parts:
            raise ExfatWriteError("root directory cannot be mutated")
        for index, wanted in enumerate(parts):
            records = self._directory_records(reader, current_cluster)
            record = next((item for item in records if item[0] == wanted), None)
            if record is None:
                raise FileNotFoundError(path)
            if index == len(parts) - 1:
                return current_cluster, record
            entry = reader._find("/" + "/".join(parts[: index + 1]))
            if not entry.is_dir:
                raise NotADirectoryError(path)
            current_cluster = entry.first_cluster

    @staticmethod
    def _name_hash(name):
        checksum = 0
        encoded = name.encode("utf-16le")
        for value in encoded:
            checksum = ((checksum << 15) | (checksum >> 1)) & 0xFFFF
            checksum = (checksum + value) & 0xFFFF
        return checksum

    def _commit_stage(self, stage, operation):
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

    def write_file(self, path, data):
        payload = bytes(data)
        with _image_lock(self.image):
            stage = self._copy_stage()
            try:
                reader = self._reader(stage)
                try:
                    reader.system_metadata()
                except ExfatReadError as exc:
                    raise ExfatWriteError(str(exc)) from exc
                _cluster, (_name, entry_pos, _total, stream_entry, _raw) = self._locate(
                    reader, path
                )
                entry = reader._find(path)
                if entry.is_dir:
                    raise IsADirectoryError(path)
                capacity = (
                    len(
                        reader._chain(
                            entry.first_cluster,
                            entry.size,
                            no_fat_chain=entry.no_fat_chain,
                        )
                    )
                    * reader.profile.cluster_size
                )
                if len(payload) > capacity:
                    raise ExfatWriteError(
                        "phase-one exFAT writer refuses stream growth"
                    )
                with stage.open("r+b") as stream:
                    for index, cluster in enumerate(
                        reader._chain(
                            entry.first_cluster,
                            capacity,
                            no_fat_chain=entry.no_fat_chain,
                        )
                    ):
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
                        if len(chunk) < reader.profile.cluster_size:
                            break
                    absolute_stream = self.offset + reader._cluster_offset(
                        reader.profile.root_cluster
                    )  # replaced below
                    del absolute_stream
                    # Locate the stream extension relative to its containing directory.
                    directory_cluster, record = self._locate(reader, path)
                    directory_raw_offset = (
                        reader._cluster_offset(directory_cluster) + record[1]
                    )
                    stream_relative = directory_raw_offset + 32
                    self._write_rel(
                        stream, stream_relative + 8, struct.pack("<Q", len(payload))
                    )
                    self._write_rel(
                        stream, stream_relative + 24, struct.pack("<Q", len(payload))
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
                validated = self._reader(stage)
                try:
                    if validated.read_file(path) != payload:
                        raise ExfatWriteError("exFAT read-back validation failed")
                finally:
                    validated.close()
                self._commit_stage(stage, "write_file")
            except Exception:
                stage.unlink(missing_ok=True)
                raise

    def rename(self, src, dst):
        src_parts = self._parts(src)
        dst_parts = self._parts(dst)
        if src_parts[:-1] != dst_parts[:-1]:
            raise ExfatWriteError("phase-one exFAT rename supports one directory only")
        new_name = dst_parts[-1]
        encoded = new_name.encode("utf-16le")
        with _image_lock(self.image):
            stage = self._copy_stage()
            try:
                reader = self._reader(stage)
                try:
                    reader.system_metadata()
                except ExfatReadError as exc:
                    raise ExfatWriteError(str(exc)) from exc
                _cluster, record = self._locate(reader, src)
                if any(
                    item[0] == new_name
                    for item in self._directory_records(reader, _cluster)
                ):
                    raise FileExistsError(dst)
                name_slots = (record[2] // 32) - 2
                if len(encoded) > name_slots * 30:
                    raise ExfatWriteError(
                        "phase-one exFAT rename exceeds allocated name entries"
                    )
                directory_relative = reader._cluster_offset(_cluster) + record[1]
                with stage.open("r+b") as stream:
                    self._write_rel(
                        stream, directory_relative + 32 + 3, bytes([len(encoded) // 2])
                    )
                    self._write_rel(
                        stream,
                        directory_relative + 32 + 4,
                        struct.pack("<H", self._name_hash(new_name)),
                    )
                    padded = encoded.ljust(name_slots * 30, b"\xff")
                    for index in range(name_slots):
                        self._write_rel(
                            stream,
                            directory_relative + 64 + index * 32 + 2,
                            padded[index * 30 : (index + 1) * 30],
                        )
                    stream.flush()
                    os.fsync(stream.fileno())
                validated = self._reader(stage)
                try:
                    if validated.file_size("/" + "/".join(dst_parts)) < 0:
                        raise ExfatWriteError(
                            "exFAT rename read-back validation failed"
                        )
                finally:
                    validated.close()
                self._commit_stage(stage, "rename")
            except Exception:
                stage.unlink(missing_ok=True)
                raise

    def mkdir(self, path):
        raise ExfatWriteError(
            "phase-one exFAT writer does not allocate directory entries"
        )

    def delete(self, path, recursive=False):
        raise ExfatWriteError(
            "phase-one exFAT writer does not update allocation bitmap"
        )


__all__ = ["ExfatWriteError", "NativeExfatWriter"]
