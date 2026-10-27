# MOUNTFILE_FIX — auto-patched + OLMA
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Minimal FAT32 reader/writer for raw disk images.

Read : directory listing, file read.
Write: file create / overwrite inside existing directories, mkdir.

WARNING: write operations modify the image in place. Always keep a backup.
"""

from __future__ import annotations

import math
import struct
from contextlib import contextmanager

# --- MOUNTFILE_FIX: OLMA FAT32 helpers -------------------------------------
try:
    from mountfile.olma.core import (
        FAT32_BAD,
        FAT32_EOC_MIN,
        FAT32_MAX_CLUSTER,
        OlmaChain,
        OlmaCluster,
    )
except Exception:
    OlmaChain = None


def _check_cluster_range(c, mc=FAT32_MAX_CLUSTER):
    try:
        OlmaCluster(c, mc)
        return True
    except Exception:
        return False


def _is_eoc(v):
    return v >= FAT32_EOC_MIN


def _is_bad(v):
    return v == FAT32_BAD


def _iter_chain(start, fat_get, mc=FAT32_MAX_CLUSTER):
    if OlmaChain is None:
        cur = start
        while _check_cluster_range(cur, mc):
            yield cur
            cur = fat_get(cur)
            if _is_eoc(cur) or _is_bad(cur) or cur == 0:
                break
        return
    for c in OlmaChain(start, fat_get, mc):
        yield c.value


def _safe_free_chain(fat_get, fat_set, start, mc=FAT32_MAX_CLUSTER):
    freed = 0
    try:
        for v in _iter_chain(start, fat_get, mc):
            fat_set(v, 0)
            freed += 1
    except Exception:
        pass
    return freed


# --- /MOUNTFILE_FIX --------------------------------------------------------
EOC = 0x0FFFFFF8
BAD = 0x0FFFFFF7
FREE = 0x00000000
MAX_CHAIN = 0x1000000  # loop guard


class Fat32Error(Exception):
    pass


class _MutationRollbackError(Fat32Error):
    """A metadata write failed and its best-effort restoration also failed."""

    rollback_failed = True


class DirEntry:
    __slots__ = (
        "attr",
        "cluster",
        "entry_offset",
        "is_dir",
        "name",
        "short_raw",
        "size",
    )

    def __init__(self, name, short_raw, attr, cluster, size, entry_offset):
        self.name = name
        self.short_raw = short_raw
        self.attr = attr
        self.cluster = cluster
        self.size = size
        self.is_dir = bool(attr & 0x10)
        self.entry_offset = entry_offset

    def __repr__(self):
        return "<DirEntry %r %s %d>" % (
            self.name,
            "dir " if self.is_dir else "file",
            self.size,
        )


class Fat32:
    def __init__(self, fileobj, offset=0):
        self.f = fileobj
        self.offset = offset
        if offset < 0:
            raise Fat32Error("negative volume offset")
        self.f.seek(offset)
        bpb = self.f.read(64)
        if len(bpb) < 64:
            raise Fat32Error("image too small")
        bps = struct.unpack_from("<H", bpb, 11)[0]
        if bps not in (512, 1024, 2048, 4096):
            raise Fat32Error("invalid bytes-per-sector (not a FAT BPB?)")
        self.bps = bps
        self.spc = bpb[13]
        if self.spc == 0 or (self.spc & (self.spc - 1)):
            raise Fat32Error("invalid sectors-per-cluster")
        self.reserved = struct.unpack_from("<H", bpb, 14)[0]
        self.nfats = bpb[16]
        self.ext_flags = struct.unpack_from("<H", bpb, 40)[0]
        if self.ext_flags & 0x0080:
            raise Fat32Error("non-mirrored FAT32 mode is unsupported")
        spf32 = struct.unpack_from("<I", bpb, 36)[0]
        if spf32 == 0:
            raise Fat32Error("FAT12/16 not supported by this module")
        if self.reserved == 0 or self.nfats == 0:
            raise Fat32Error("invalid FAT32 reserved-sector/FAT count")
        self.spf = spf32
        self.root_cluster = struct.unpack_from("<I", bpb, 44)[0]
        # FSINFO is only a cache of the allocator state; the FAT stays the
        # source of truth.  The pointer is remembered here so the writer paths
        # can refresh that cache - a stale free-cluster count is the exact
        # condition `fsck.fat` reports as "Free cluster summary wrong".
        self.fsinfo_sector = struct.unpack_from("<H", bpb, 48)[0]
        self.backup_boot_sector = struct.unpack_from("<H", bpb, 50)[0]
        self.fat_start = self.offset + self.reserved * self.bps
        self.fat_size = self.spf * self.bps
        self.data_start = (
            self.offset + (self.reserved + self.nfats * self.spf) * self.bps
        )
        self.cluster_size = self.spc * self.bps
        self.entries_per_cluster = self.cluster_size // 32
        total_sectors = (
            struct.unpack_from("<I", bpb, 32)[0] or struct.unpack_from("<H", bpb, 19)[0]
        )
        data_sectors = total_sectors - (self.reserved + self.nfats * self.spf)
        if total_sectors <= 0 or data_sectors <= 0:
            raise Fat32Error("invalid FAT32 volume size")
        self.f.seek(0, 2)
        physical_size = self.f.tell()
        if self.offset + total_sectors * self.bps > physical_size:
            raise Fat32Error("FAT32 BPB volume extends beyond image EOF")
        self.volume_end = self.offset + total_sectors * self.bps
        self._validate_backup_boot_sector()
        self._validate_fat_copies()
        cluster_count = data_sectors // self.spc
        if cluster_count < 65525:
            raise Fat32Error("not FAT32 (cluster count %d)" % cluster_count)
        if cluster_count + 2 > self.fat_size // 4:
            raise Fat32Error("FAT32 FAT is too small for the declared volume")
        if not 2 <= self.root_cluster < cluster_count + 2:
            raise Fat32Error("invalid FAT32 root cluster")
        self.cluster_count = cluster_count
        self._alloc_hint = 2

    def _validate_fat_copies(self):
        """Require identical mirrored FATs before exposing mutation APIs."""
        if self.nfats <= 1:
            return
        primary = self._read(self.fat_start, self.fat_size)
        for index in range(1, self.nfats):
            replica = self._read(
                self.fat_start + index * self.fat_size,
                self.fat_size,
            )
            if replica != primary:
                raise Fat32Error("mirrored FAT32 copies are inconsistent")

    def _validate_backup_boot_sector(self):
        """Require a declared FAT32 backup boot sector to match the primary."""
        if not self.backup_boot_sector:
            return
        backup_offset = self.offset + self.backup_boot_sector * self.bps
        primary = self._read(self.offset, self.bps)
        backup = self._read(backup_offset, self.bps)
        if backup != primary:
            raise Fat32Error("FAT32 backup boot sector is inconsistent")

    # ------------------------------------------------------------------ IO
    def _read(self, pos, size):
        if pos < self.offset or size < 0 or pos + size > self.volume_end:
            raise Fat32Error("read exceeds FAT32 volume bounds at 0x%X" % pos)
        self.f.seek(pos)
        data = self.f.read(size)
        if len(data) != size:
            raise Fat32Error("short read at 0x%X" % pos)
        return data

    @contextmanager
    def _atomic_metadata(self):
        """Restore every written metadata range if an operation raises."""
        if getattr(self, "_mutation_log", None) is not None:
            yield
            return

        changes = []
        self._mutation_log = changes
        try:
            yield
        except BaseException as original_error:
            self._mutation_log = None
            failures = []
            for pos, original in reversed(changes):
                try:
                    self.f.seek(pos)
                    written = self.f.write(original)
                    if written is not None and written != len(original):
                        raise Fat32Error("short write while restoring metadata")
                except Exception as rollback_error:
                    failures.append(rollback_error)
            try:
                self.f.flush()
            except Exception as rollback_error:
                failures.append(rollback_error)
            if failures:
                raise _MutationRollbackError(
                    "FAT32 metadata rollback failed; the image may be inconsistent"
                ) from original_error
            raise
        else:
            self._mutation_log = None

    def _write(self, pos, data):
        if pos < self.offset or pos + len(data) > self.volume_end:
            raise Fat32Error("write exceeds FAT32 volume bounds at 0x%X" % pos)
        changes = getattr(self, "_mutation_log", None)
        if changes is not None and data:
            self.f.seek(pos)
            original = self.f.read(len(data))
            if len(original) != len(data):
                raise Fat32Error("short read before metadata write at 0x%X" % pos)
            changes.append((pos, original))
        self.f.seek(pos)
        written = self.f.write(data)
        if written is not None and written != len(data):
            raise Fat32Error("short write at 0x%X" % pos)

    def _fat_get(self, n):
        return struct.unpack_from("<I", self._read(self.fat_start + n * 4, 4))[0]

    def _fat_set(self, n, val):
        val &= 0x0FFFFFFF
        for i in range(self.nfats):
            self._write(
                self.fat_start + i * self.fat_size + n * 4, struct.pack("<I", val)
            )

    def cluster_offset(self, n):
        return self.data_start + (n - 2) * self.cluster_size

    # ----------------------------------------------------------- chains
    def cluster_chain(self, start):
        chain = []
        n = start
        seen = set()
        while FREE < n < BAD:
            if n >= self.cluster_count + 2 or n >= self.fat_size // 4:
                raise Fat32Error("FAT chain references out-of-range cluster %d" % n)
            if n in seen or len(chain) > MAX_CHAIN:
                raise Fat32Error("FAT chain loop detected at cluster %d" % n)
            seen.add(n)
            chain.append(n)
            nxt = self._fat_get(n)
            if nxt >= EOC:
                break
            n = nxt
        return chain

    @property
    def total_bytes(self):
        """Size of the data area (all clusters) in bytes."""
        return self.cluster_count * self.cluster_size

    def free_space(self, max_fat_bytes=64 << 20):
        """Free bytes on the volume, or None when the FAT is too big to scan.

        The whole FAT is read in one call and zeroed entries are counted, so
        this stays fast on the small volumes (firmware dumps, SD cards) the
        utility is used on and simply gives up on multi-gigabyte FATs.
        """
        if self.fat_size > max_fat_bytes:
            return None
        data = self._read(self.fat_start, self.fat_size)
        free = 0
        for n in range(2, self.fat_size // 4):
            if struct.unpack_from("<I", data, n * 4)[0] == 0:
                free += 1
        return free * self.cluster_size

    def read_cluster(self, n):
        return self._read(self.cluster_offset(n), self.cluster_size)

    def read_chain(self, start, size=None):
        if start < 2:
            if size is not None and size > 0:
                raise Fat32Error("non-empty file has invalid start cluster")
            return b""
        data = b"".join(self.read_cluster(c) for c in self.cluster_chain(start))
        return data if size is None else data[:size]

    def _alloc_cluster(self):
        # Only clusters in the data region are allocatable; FAT padding entries
        # must never be used, even when the FAT is larger than it needs to be.
        total = min(self.cluster_count + 2, self.fat_size // 4)
        start = self._alloc_hint
        if start < 2 or start >= total:
            start = 2

        # Continue from the last allocation instead of rescanning the occupied
        # prefix for every cluster. Without this hint, writing N clusters costs
        # O(N²) tiny seek/read operations on the image file.
        for first, last in ((start, total), (2, start)):
            for n in range(first, last):
                if self._fat_get(n) == FREE:
                    self._fat_set(n, EOC)
                    try:
                        self._write(self.cluster_offset(n), b"\x00" * self.cluster_size)
                    except Exception as original_error:
                        try:
                            self._fat_set(n, FREE)
                        except Exception as rollback_error:
                            raise _MutationRollbackError(
                                "cluster initialization failed and its allocation "
                                "could not be reclaimed: %s" % rollback_error
                            ) from original_error
                        self._alloc_hint = min(self._alloc_hint, n)
                        raise
                    self._alloc_hint = n + 1 if n + 1 < total else 2
                    return n
        raise Fat32Error("no free clusters left")

    def _alloc_chain(self, nclusters):
        first = prev = None
        allocated = []
        try:
            for _ in range(nclusters):
                n = self._alloc_cluster()
                allocated.append(n)
                if prev is not None:
                    self._fat_set(prev, n)
                else:
                    first = n
                prev = n
        except Exception as original_error:
            # Allocation may run out of space partway through a request. Free
            # every cluster allocated so far, including one not yet linked if
            # updating the previous FAT entry itself failed.
            rollback_errors = []
            for n in allocated:
                try:
                    self._fat_set(n, FREE)
                except Exception as rollback_error:
                    rollback_errors.append(rollback_error)
                self._alloc_hint = min(self._alloc_hint, n)
            if rollback_errors:
                raise _MutationRollbackError(
                    "cluster chain allocation failed and cleanup was incomplete"
                ) from original_error
            raise
        return first

    def _free_chain(self, start):
        if start < 2:
            return
        for n in self.cluster_chain(start):
            self._fat_set(n, FREE)
            self._alloc_hint = min(self._alloc_hint, n)

    def _write_chain_data(self, first_cluster, data):
        if first_cluster < 2:
            return
        chain = self.cluster_chain(first_cluster)
        for i, c in enumerate(chain):
            chunk = data[i * self.cluster_size : (i + 1) * self.cluster_size]
            if not chunk:
                break
            if len(chunk) < self.cluster_size:
                chunk = chunk + b"\x00" * (self.cluster_size - len(chunk))
            self._write(self.cluster_offset(c), chunk)

    # ------------------------------------------------------------ FSINFO
    def _fsinfo_location(self):
        """Byte offset of a *valid* FSINFO structure, or ``None``.

        A volume that does not declare an FSINFO sector, points one at a
        nonsensical sector, or carries the wrong signatures is left untouched:
        writing the counters into an unrelated sector would corrupt the volume
        while "fixing" a cache.
        """
        if not self.fsinfo_sector:
            return None
        off = self.offset + self.fsinfo_sector * self.bps
        try:
            raw = self._read(off, self.bps)
        except Fat32Error:
            return None
        if struct.unpack_from("<I", raw, 0)[0] != 0x41615252:  # lead signature
            return None
        if struct.unpack_from("<I", raw, 484)[0] != 0x61417272:  # structure
            return None
        if struct.unpack_from("<I", raw, 508)[0] != 0xAA550000:  # trail
            return None
        return off

    def free_clusters(self):
        """Exact number of free clusters, counted from the FAT itself."""
        total = min(self.cluster_count + 2, self.fat_size // 4)
        fat = self._read(self.fat_start, self.fat_size)
        free = 0
        first = 0xFFFFFFFF
        for n in range(2, total):
            if struct.unpack_from("<I", fat, n * 4)[0] == 0:
                free += 1
                if first == 0xFFFFFFFF:
                    first = n
        return free, first

    def sync_fsinfo(self):
        """Refresh the cached free-cluster count/next-free hint.

        Counted from the FAT (the source of truth), never incremented, so the
        cache cannot drift after a rollback or a partially applied operation.
        Returns ``True`` when an FSINFO structure was found and updated.
        """
        off = self._fsinfo_location()
        if off is None:
            return False
        free, first = self.free_clusters()
        raw = bytearray(self._read(off, self.bps))
        struct.pack_into("<I", raw, 488, free)
        struct.pack_into("<I", raw, 492, first)
        self._write(off, bytes(raw))
        return True

    # ----------------------------------------------------- 8.3 / LFN
    @staticmethod
    def _short_checksum(short11):
        s = 0
        for b in short11:
            s = (((s & 1) << 7) + (s >> 1) + b) & 0xFF
        return s

    @staticmethod
    def _short_to_name(short11):
        base = short11[0:8].decode("ascii", "replace").rstrip()
        ext = short11[8:11].decode("ascii", "replace").rstrip()
        return "%s.%s" % (base, ext) if ext else base

    @staticmethod
    def _sanitize(name):
        cleaned = []
        for c in name.strip():
            if c in '\\/*?":<>|' or ord(c) < 32:
                cleaned.append("_")
            else:
                cleaned.append(c)
        out = "".join(cleaned).strip(" .")
        return out or "FILE"

    def _make_short_name(self, name, used):
        name = self._sanitize(name)
        base, dot, ext = name.rpartition(".")
        if not dot:
            base, ext = name, ""
        base = base.upper()
        ext = ext.upper()
        if len(base) <= 8 and len(ext) <= 3 and " " not in base + ext:
            candidate = base.ljust(8) + ext.ljust(3)
            enc = candidate.encode("ascii", "replace")
            if enc not in used:
                used.add(enc)
                return enc
        stem = base[:6]
        for i in range(1, 100000):
            suffix = "~%d" % i
            candidate = (stem[: 8 - len(suffix)] + suffix).ljust(8) + ext[:3].ljust(3)
            enc = candidate.encode("ascii", "replace")
            if enc not in used:
                used.add(enc)
                return enc
        raise Fat32Error("cannot generate a unique 8.3 name")

    @staticmethod
    def _lfn_chunks(name):
        chars = [ord(c) for c in name]
        chars.append(0x0000)
        while len(chars) % 13:
            chars.append(0xFFFF)
        return [chars[i : i + 13] for i in range(0, len(chars), 13)]

    def _build_lfn_entry(self, seq, chunk, checksum):
        e = bytearray(32)
        e[0] = seq
        idx = 0
        for start, count in ((1, 5), (14, 6), (28, 2)):
            for k in range(count):
                struct.pack_into("<H", e, start + 2 * k, chunk[idx] & 0xFFFF)
                idx += 1
        e[11] = 0x0F
        e[13] = checksum
        return bytes(e)

    @staticmethod
    def _needs_lfn(name):
        if name != name.upper():
            return True
        base, dot, ext = name.partition(".")
        if not dot:
            ext = ""
        if len(base) > 8 or len(ext) > 3:
            return True
        if any(c in name for c in " +,;=[]&@"):
            return True
        return False

    def _build_entries(self, long_name, short11, first_cluster, size, attr=0x20):
        entries = []
        chk = self._short_checksum(short11)
        chunks = self._lfn_chunks(long_name) if self._needs_lfn(long_name) else []
        n = len(chunks)
        for i in range(n - 1, -1, -1):
            seq = i + 1
            if i == n - 1:
                seq |= 0x40
            entries.append(self._build_lfn_entry(seq, chunks[i], chk))
        e = bytearray(32)
        e[0:11] = short11
        e[11] = attr
        date = ((2026 - 1980) << 9) | (1 << 5) | 1
        t = 12 << 11
        struct.pack_into("<H", e, 14, t)
        struct.pack_into("<H", e, 16, date)
        struct.pack_into("<H", e, 18, date)
        struct.pack_into("<H", e, 22, t)
        struct.pack_into("<H", e, 24, date)
        struct.pack_into("<H", e, 20, (first_cluster >> 16) & 0xFFFF)
        struct.pack_into("<H", e, 26, first_cluster & 0xFFFF)
        struct.pack_into("<I", e, 28, size)
        entries.append(bytes(e))
        return entries

    # ------------------------------------------------------ directories
    def _parse_dir_data(self, data, base_offset):
        entries = []
        pending = []
        i = 0
        while i + 32 <= len(data):
            e = data[i : i + 32]
            b0 = e[0]
            attr = e[11]
            if b0 == 0x00:
                break
            off = base_offset + i
            i += 32
            if attr == 0x0F and b0 != 0xE5:
                seq = b0 & 0x1F
                chk = e[13]
                chars = []
                for start, count in ((1, 5), (14, 6), (28, 2)):
                    for k in range(count):
                        chars.append(struct.unpack_from("<H", e, start + 2 * k)[0])
                part = "".join(chr(c) for c in chars if c not in (0x0000, 0xFFFF))
                pending.append((seq, chk, part))
                continue
            if b0 == 0xE5:
                pending.clear()
                continue
            if attr & 0x08:  # volume label
                pending.clear()
                continue
            short = e[0:11]
            cluster = (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from(
                "<H", e, 26
            )[0]
            size = struct.unpack_from("<I", e, 28)[0]
            is_dir = bool(attr & 0x10)
            if is_dir and short[0:1] == b".":
                pending.clear()
                continue
            name = None
            if pending:
                chk = self._short_checksum(short)
                parts = [p for s, c, p in sorted(pending) if c == chk]
                if parts:
                    name = "".join(parts)
                pending.clear()
            if name is None:
                name = self._short_to_name(short)
            entries.append(
                DirEntry(name, short, attr, cluster, 0 if is_dir else size, off)
            )
        return entries

    def _dir_stream(self, cluster):
        chain = self.cluster_chain(cluster)
        data = b"".join(self.read_cluster(c) for c in chain)
        return chain, data

    def list_dir(self, cluster=None):
        if cluster is None:
            cluster = self.root_cluster
        entries = []
        for c in self.cluster_chain(cluster):
            entries.extend(
                self._parse_dir_data(self.read_cluster(c), self.cluster_offset(c))
            )
        return entries

    def find_dir(self, path):
        cluster = self.root_cluster
        for part in [p for p in path.split("/") if p]:
            found = None
            for e in self.list_dir(cluster):
                if e.is_dir and e.name.upper() == part.upper():
                    found = e
                    break
            if found is None:
                raise Fat32Error("directory not found: %s" % part)
            cluster = found.cluster
        return cluster

    def read_file(self, path):
        parts = [p for p in path.split("/") if p]
        if not parts:
            raise Fat32Error("not a file")
        d = self.find_dir("/" + "/".join(parts[:-1]))
        name = parts[-1].upper()
        for e in self.list_dir(d):
            if not e.is_dir and e.name.upper() == name:
                return self.read_chain(e.cluster, e.size)
        raise Fat32Error("file not found: %s" % path)

    def entry(self, path):
        """Return ``(parent_cluster, DirEntry)`` for a path (file or dir)."""
        parts = [p for p in path.replace("\\", "/").split("/") if p]
        if not parts:
            raise Fat32Error("the root directory has no directory entry")
        parent = self.find_dir("/" + "/".join(parts[:-1]))
        name = parts[-1].upper()
        for e in self.list_dir(parent):
            if e.name.upper() == name:
                return parent, e
        raise Fat32Error("not found: %s" % path)

    def dir_is_empty(self, entry):
        """True when a directory holds nothing besides "." and ".."."""
        if entry.cluster < 2:
            return True
        chain, data = self._dir_stream(entry.cluster)
        return not self._parse_dir_data(data, 0)

    def delete(self, path, recursive=False):
        """Delete a file, or a directory (recursively with `recursive=True`).

        Directory and FAT metadata are restored if any step fails, so a partial
        recursive delete cannot expose clusters still referenced by live entries.
        """
        parent, entry = self.entry(path)
        if entry.is_dir:
            if not recursive and not self.dir_is_empty(entry):
                raise Fat32Error("directory is not empty: %s" % path)
        with self._atomic_metadata():
            if entry.is_dir and recursive:
                self._delete_tree(entry)
            self._delete_entry(parent, entry.entry_offset)
            if entry.cluster >= 2:
                self._free_chain(entry.cluster)
            self.sync_fsinfo()
            self.f.flush()
        return entry

    def _delete_tree(self, directory):
        """Remove the contents of `directory` (its own entry stays)."""
        for child in self.list_dir(directory.cluster):
            if child.is_dir:
                self._delete_tree(child)
            self._delete_entry(directory.cluster, child.entry_offset)
            self.f.flush()
            if child.cluster >= 2:
                self._free_chain(child.cluster)

    def rename(self, path, new_name):
        """Rename a file or directory in place (LFN entries are rebuilt)."""
        new_name = self._sanitize(new_name)
        parent, entry = self.entry(path)
        if entry.name.upper() == new_name.upper():
            return entry
        siblings = self.list_dir(parent)
        if any(e.name.upper() == new_name.upper() for e in siblings):
            raise Fat32Error("already exists: %s" % new_name)
        used = {e.short_raw for e in siblings}
        used |= {
            self._short_to_name(e.short_raw).encode("ascii", "replace")
            for e in siblings
        }
        short = self._make_short_name(new_name, used)
        attr = 0x10 if entry.is_dir else 0x20
        raw = self._build_entries(new_name, short, entry.cluster, entry.size, attr=attr)
        with self._atomic_metadata():
            self._insert_entries(parent, raw)
            self._delete_entry(parent, entry.entry_offset)
            self.sync_fsinfo()
            self.f.flush()
        return entry

    # -------------------------------------------------------- writing
    def _find_slots(self, data, needed):
        free_run = 0
        run_start = None
        for i in range(len(data) // 32):
            if data[i * 32] in (0x00, 0xE5):
                if free_run == 0:
                    run_start = i
                free_run += 1
                if free_run >= needed:
                    return run_start
            else:
                free_run = 0
        return None

    def _write_slots(self, chain, index, raw):
        epc = self.entries_per_cluster
        for k in range(0, len(raw), 32):
            i = index + k // 32
            c = chain[i // epc]
            off = self.cluster_offset(c) + (i % epc) * 32
            self._write(off, raw[k : k + 32])

    def _append_dir_cluster(self, chain):
        newc = self._alloc_cluster()
        self._fat_set(chain[-1], newc)
        return chain + [newc]

    def _insert_entries(self, dir_cluster, raw_entries):
        chain, data = self._dir_stream(dir_cluster)
        needed = len(raw_entries)
        idx = self._find_slots(data, needed)
        if idx is None:
            tail_free = 0
            for i in range(len(data) // 32 - 1, -1, -1):
                if data[i * 32] in (0x00, 0xE5):
                    tail_free += 1
                else:
                    break
            add = math.ceil((needed - tail_free) / self.entries_per_cluster)
            for _ in range(max(1, add)):
                chain = self._append_dir_cluster(chain)
            idx = len(data) // 32
        self._write_slots(chain, idx, b"".join(raw_entries))

    def _delete_entry(self, dir_cluster, abs_offset):
        chain, data = self._dir_stream(dir_cluster)
        epc = self.entries_per_cluster
        i = None
        for ci, c in enumerate(chain):
            base = self.cluster_offset(c)
            if base <= abs_offset < base + self.cluster_size:
                i = ci * epc + (abs_offset - base) // 32
                break
        if i is None:
            raise Fat32Error("directory entry to tombstone was not found")
        j = i - 1
        while j >= 0 and data[j * 32 + 11] == 0x0F and data[j * 32] != 0xE5:
            j -= 1
        for k in range(j + 1, i + 1):
            self._write_slots(chain, k, b"\xe5" + b"\x00" * 31)
        return True

    def _directory_references_cluster(self, dir_cluster, cluster):
        """Check raw short entries, including an entry from a partial insert."""
        _, data = self._dir_stream(dir_cluster)
        for i in range(0, len(data) - 31, 32):
            entry = data[i : i + 32]
            if entry[0] in (0x00, 0xE5) or entry[11] == 0x0F:
                continue
            first = (struct.unpack_from("<H", entry, 20)[0] << 16) | struct.unpack_from(
                "<H", entry, 26
            )[0]
            if first == cluster:
                return True
        return False

    def write_file(self, dir_path, filename, data):
        """Create or overwrite `filename` in directory `dir_path`."""
        dir_cluster = self.find_dir(dir_path)
        entries_now = self.list_dir(dir_cluster)
        target = next(
            (
                e
                for e in entries_now
                if not e.is_dir and e.name.upper() == filename.upper()
            ),
            None,
        )
        used = {e.short_raw for e in entries_now}
        used |= {self._short_to_name(e.short_raw) for e in entries_now}
        short = (
            target.short_raw
            if target is not None
            else self._make_short_name(filename, used)
        )
        nclusters = max(1, math.ceil(len(data) / self.cluster_size)) if data else 0
        first = self._alloc_chain(nclusters) if nclusters else 0
        try:
            self._write_chain_data(first, data)
        except Exception as original_error:
            try:
                if first >= 2:
                    self._free_chain(first)
            except Exception as rollback_error:
                raise _MutationRollbackError(
                    "file data write failed and its allocation could not be freed"
                ) from original_error
            raise
        raw = self._build_entries(filename, short, first, len(data))
        # Keep the old entry and its chain until the replacement is fully
        # published. Directory/FAT metadata writes are reversible as a unit.
        try:
            with self._atomic_metadata():
                self._insert_entries(dir_cluster, raw)
                if target is not None:
                    self._delete_entry(dir_cluster, target.entry_offset)
                    if target.cluster >= 2:
                        self._free_chain(target.cluster)
                self.sync_fsinfo()
                self.f.flush()
        except _MutationRollbackError:
            # Do not free the new chain when directory restoration is uncertain.
            raise
        except BaseException as original_error:
            try:
                if first >= 2:
                    self._free_chain(first)
                self.sync_fsinfo()
                self.f.flush()
            except Exception as rollback_error:
                raise _MutationRollbackError(
                    "replacement failed and its new cluster chain could not be "
                    "reclaimed: %s" % rollback_error
                ) from original_error
            raise

    def mkdir(self, dir_path, name):
        dir_cluster = self.find_dir(dir_path)
        entries_now = self.list_dir(dir_cluster)
        if any(e.is_dir and e.name.upper() == name.upper() for e in entries_now):
            raise Fat32Error("directory already exists: %s" % name)
        used = {e.short_raw for e in entries_now}
        used |= {self._short_to_name(e.short_raw) for e in entries_now}
        short = self._make_short_name(name, used)
        with self._atomic_metadata():
            c = self._alloc_cluster()
            dot = bytearray(32)
            # The self/parent entries are spelled "." and ".." padded with
            # spaces - writing the short name there made list_dir() report a
            # phantom entry inside every freshly created directory.
            dot[0:11] = b"." + b" " * 10
            dot[11] = 0x10
            struct.pack_into("<H", dot, 20, (c >> 16) & 0xFFFF)
            struct.pack_into("<H", dot, 26, c & 0xFFFF)
            dotdot = bytearray(32)
            dotdot[0:11] = b"..".ljust(8) + b"   "
            dotdot[11] = 0x10
            # Root-level directories store 0 in the ".." cluster field.
            parent_cluster = 0 if dir_cluster == self.root_cluster else dir_cluster
            struct.pack_into("<H", dotdot, 20, (parent_cluster >> 16) & 0xFFFF)
            struct.pack_into("<H", dotdot, 26, parent_cluster & 0xFFFF)
            self._write(
                self.cluster_offset(c),
                bytes(dot) + bytes(dotdot) + b"\x00" * (self.cluster_size - 64),
            )
            raw = self._build_entries(name, short, c, 0, attr=0x10)
            self._insert_entries(dir_cluster, raw)
            self.sync_fsinfo()
            self.f.flush()


class Fat32Provider:
    """Read-only provider over a FAT32 volume.

    Exposes the same tiny interface as the pytsk3 provider in
    :mod:`mountfile.vfs` (`list_dir`, `read_file`, `read_file_range`,
    `file_size`, `root_name`), so the GUI and the CLI can browse and extract
    FAT32 images even when The Sleuth Kit is not installed.
    """

    def __init__(self, image_path, offset=0, writable=False, fileobj=None):
        self.root_name = image_path
        self._owns = fileobj is None
        self.f = fileobj or open(image_path, "r+b" if writable else "rb")
        try:
            self.fs = Fat32(self.f, offset)
        except Exception:
            self.close()
            raise

    # -- lifecycle -----------------------------------------------------
    def close(self):
        if self._owns and self.f is not None:
            try:
                self.f.close()
            except OSError:
                pass
            self.f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- provider interface --------------------------------------------
    def list_dir(self, path="/"):
        entries = self.fs.list_dir(self.fs.find_dir(path))
        out = [{"name": e.name, "is_dir": e.is_dir, "size": e.size} for e in entries]
        out.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return out

    def _entry(self, path):
        return self.fs.entry(path)[1]

    def file_size(self, path):
        entry = self._entry(path)
        return 0 if entry.is_dir else int(entry.size)

    def read_file_range(self, path, offset=0, length=1 << 20):
        entry = self._entry(path)
        if entry.is_dir:
            raise Fat32Error("is a directory: %s" % path)
        size = int(entry.size)
        if offset >= size or offset < 0:
            return b""
        length = min(length, size - offset)
        if entry.cluster < 2:
            raise Fat32Error("non-empty file has invalid start cluster")
        out = bytearray()
        pos = 0
        csize = self.fs.cluster_size
        for c in self.fs.cluster_chain(entry.cluster):
            if pos + csize <= offset:
                pos += csize
                continue
            data = self.fs.read_cluster(c)
            start = max(0, offset - pos)
            out += data[start : start + (length - len(out))]
            pos += csize
            if len(out) >= length:
                break
        return bytes(out)

    def read_file(self, path):
        return self.fs.read_file(path)
