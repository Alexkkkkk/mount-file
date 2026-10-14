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

EOC = 0x0FFFFFF8
BAD = 0x0FFFFFF7
FREE = 0x00000000
MAX_CHAIN = 0x1000000  # loop guard


class Fat32Error(Exception):
    pass


class DirEntry:
    __slots__ = ("name", "short_raw", "attr", "cluster", "size", "is_dir",
                 "entry_offset")

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
            self.name, "dir " if self.is_dir else "file", self.size)


class Fat32:
    def __init__(self, fileobj, offset=0):
        self.f = fileobj
        self.offset = offset
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
        spf32 = struct.unpack_from("<I", bpb, 36)[0]
        if spf32 == 0:
            raise Fat32Error("FAT12/16 not supported by this module")
        self.spf = spf32
        self.root_cluster = struct.unpack_from("<I", bpb, 44)[0]
        self.fat_start = self.offset + self.reserved * self.bps
        self.fat_size = self.spf * self.bps
        self.data_start = self.offset + (
            self.reserved + self.nfats * self.spf) * self.bps
        self.cluster_size = self.spc * self.bps
        self.entries_per_cluster = self.cluster_size // 32
        total_sectors = struct.unpack_from("<I", bpb, 32)[0] \
            or struct.unpack_from("<H", bpb, 19)[0]
        data_sectors = total_sectors - (
            self.reserved + self.nfats * self.spf)
        cluster_count = data_sectors // self.spc
        if cluster_count < 65525:
            raise Fat32Error("not FAT32 (cluster count %d)" % cluster_count)

    # ------------------------------------------------------------------ IO
    def _read(self, pos, size):
        self.f.seek(pos)
        data = self.f.read(size)
        if len(data) != size:
            raise Fat32Error("short read at 0x%X" % pos)
        return data

    def _write(self, pos, data):
        self.f.seek(pos)
        self.f.write(data)

    def _fat_get(self, n):
        return struct.unpack_from(
            "<I", self._read(self.fat_start + n * 4, 4))[0]

    def _fat_set(self, n, val):
        val &= 0x0FFFFFFF
        for i in range(self.nfats):
            self._write(self.fat_start + i * self.fat_size + n * 4,
                        struct.pack("<I", val))

    def cluster_offset(self, n):
        return self.data_start + (n - 2) * self.cluster_size

    # ----------------------------------------------------------- chains
    def cluster_chain(self, start):
        chain = []
        n = start
        seen = set()
        while FREE < n < BAD:
            if n in seen or len(chain) > MAX_CHAIN:
                raise Fat32Error("FAT chain loop detected at cluster %d" % n)
            seen.add(n)
            chain.append(n)
            nxt = self._fat_get(n)
            if nxt >= EOC:
                break
            n = nxt
        return chain

    def read_cluster(self, n):
        return self._read(self.cluster_offset(n), self.cluster_size)

    def read_chain(self, start, size=None):
        if start < 2:
            return b""
        data = b"".join(self.read_cluster(c) for c in self.cluster_chain(start))
        return data if size is None else data[:size]

    def _alloc_cluster(self):
        total = self.fat_size // 4
        for n in range(2, total):
            if self._fat_get(n) == FREE:
                self._fat_set(n, EOC)
                self._write(self.cluster_offset(n),
                            b"\x00" * self.cluster_size)
                return n
        raise Fat32Error("no free clusters left")

    def _alloc_chain(self, nclusters):
        first = prev = None
        for _ in range(nclusters):
            n = self._alloc_cluster()
            if prev is not None:
                self._fat_set(prev, n)
            else:
                first = n
            prev = n
        return first

    def _free_chain(self, start):
        if start < 2:
            return
        for n in self.cluster_chain(start):
            self._fat_set(n, FREE)

    def _write_chain_data(self, first_cluster, data):
        if first_cluster < 2:
            return
        chain = self.cluster_chain(first_cluster)
        for i, c in enumerate(chain):
            chunk = data[i * self.cluster_size:(i + 1) * self.cluster_size]
            if not chunk:
                break
            if len(chunk) < self.cluster_size:
                chunk = chunk + b"\x00" * (self.cluster_size - len(chunk))
            self._write(self.cluster_offset(c), chunk)

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
            if c in '\\/*?":<>|':
                cleaned.append("_")
            elif ord(c) < 32:
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
        # name fits 8.3 exactly -> use it without a numeric suffix
        if len(base) <= 8 and len(ext) <= 3 and " " not in base + ext:
            candidate = base.ljust(8) + ext.ljust(3)
            enc = candidate.encode("ascii", "replace")
            if enc not in used:
                used.add(enc)
                return enc
        stem = base[:6]
        for i in range(1, 100000):
            suffix = "~%d" % i
            candidate = (stem[: 8 - len(suffix)] + suffix).ljust(8) \
                + ext[:3].ljust(3)
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
        return [chars[i:i + 13] for i in range(0, len(chars), 13)]

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

    def _build_entries(self, long_name, short11, first_cluster, size,
                       attr=0x20):
        entries = []
        chk = self._short_checksum(short11)
        chunks = self._lfn_chunks(long_name) if self._needs_lfn(long_name) \
            else []
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
        t = (12 << 11)
        struct.pack_into("<H", e, 14, t)     # create time
        struct.pack_into("<H", e, 16, date)  # create date
        struct.pack_into("<H", e, 18, date)  # access date
        struct.pack_into("<H", e, 22, t)     # write time
        struct.pack_into("<H", e, 24, date)  # write date
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
            e = data[i:i + 32]
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
                        chars.append(struct.unpack_from("<H", e,
                                                        start + 2 * k)[0])
                part = "".join(chr(c) for c in chars
                               if c not in (0x0000, 0xFFFF))
                pending.append((seq, chk, part))
                continue
            if b0 == 0xE5:
                pending.clear()
                continue
            if attr & 0x08:  # volume label
                pending.clear()
                continue
            short = e[0:11]
            cluster = (struct.unpack_from("<H", e, 20)[0] << 16) \
                | struct.unpack_from("<H", e, 26)[0]
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
            entries.append(DirEntry(name, short, attr, cluster,
                                    0 if is_dir else size, off))
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
            entries.extend(self._parse_dir_data(self.read_cluster(c),
                                                self.cluster_offset(c)))
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
            self._write(off, raw[k:k + 32])

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
            return
        j = i - 1
        while j >= 0 and data[j * 32 + 11] == 0x0F and data[j * 32] != 0xE5:
            j -= 1
        for k in range(j + 1, i + 1):
            self._write_slots(chain, k, b"\xE5" + b"\x00" * 31)

    def write_file(self, dir_path, filename, data):
        """Create or overwrite `filename` in directory `dir_path`."""
        dir_cluster = self.find_dir(dir_path)
        entries_now = self.list_dir(dir_cluster)
        target = next(
            (e for e in entries_now
             if not e.is_dir and e.name.upper() == filename.upper()), None)
        used = {e.short_raw for e in entries_now}
        used |= {self._short_to_name(e.short_raw) for e in entries_now}
        short = self._make_short_name(filename, used)
        nclusters = max(1, math.ceil(len(data) / self.cluster_size)) \
            if data else 0
        if target is not None:
            self._delete_entry(dir_cluster, target.entry_offset)
        first = self._alloc_chain(nclusters) if nclusters else 0
        self._write_chain_data(first, data)
        raw = self._build_entries(filename, short, first, len(data))
        self._insert_entries(dir_cluster, raw)
        if target is not None and target.cluster >= 2:
            self._free_chain(target.cluster)
        self.f.flush()

    def mkdir(self, dir_path, name):
        dir_cluster = self.find_dir(dir_path)
        entries_now = self.list_dir(dir_cluster)
        if any(e.is_dir and e.name.upper() == name.upper()
               for e in entries_now):
            raise Fat32Error("directory already exists: %s" % name)
        used = {e.short_raw for e in entries_now}
        used |= {self._short_to_name(e.short_raw) for e in entries_now}
        short = self._make_short_name(name, used)
        c = self._alloc_cluster()
        dot = bytearray(32)
        dot[0:11] = short[0:1].ljust(8) + b"   "
        dot[11] = 0x10
        struct.pack_into("<H", dot, 20, (c >> 16) & 0xFFFF)
        struct.pack_into("<H", dot, 26, c & 0xFFFF)
        dotdot = bytearray(32)
        dotdot[0:11] = b"..".ljust(8) + b"   "
        dotdot[11] = 0x10
        struct.pack_into("<H", dotdot, 20, (dir_cluster >> 16) & 0xFFFF)
        struct.pack_into("<H", dotdot, 26, dir_cluster & 0xFFFF)
        self._write(self.cluster_offset(c), bytes(dot) + bytes(dotdot)
                    + b"\x00" * (self.cluster_size - 64))
        raw = self._build_entries(name, short, c, 0, attr=0x10)
        self._insert_entries(dir_cluster, raw)
        self.f.flush()
