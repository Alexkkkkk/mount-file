# MOUNTFILE_FIX — OLMA core
"""Безопасные обёртки памяти и файлов."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from typing import BinaryIO


class OlmaError(Exception): ...


class OlmaBoundsError(OlmaError): ...


class OlmaIntegrityError(OlmaError): ...


class OlmaStateError(OlmaError): ...


SECTOR = 512
MAX_PARTITIONS = 128
FAT32_MAX_CLUSTER = 0x0FFFFFF5
FAT32_EOC_MIN = 0x0FFFFFF8
FAT32_BAD = 0x0FFFFFF7
DEFAULT_CHUNK = 1 << 20
DEFAULT_MAX_DOWNLOAD = 512 * 1024 * 1024


class OlmaHandle:
    """RAII над файлом."""

    __slots__ = ("_fh", "_path", "_size", "_closed", "_tracer")

    def __init__(self, path, mode="rb", *, tracer=None) -> None:
        self._path = os.fspath(path)
        self._tracer = tracer
        self._fh: BinaryIO = open(self._path, mode)
        self._closed = False
        try:
            self._fh.seek(0, os.SEEK_END)
            self._size = self._fh.tell()
            self._fh.seek(0)
        except OSError:
            self._size = 0
        if tracer:
            tracer.on_open(self._path, mode)

    @property
    def path(self) -> str:
        return self._path

    @property
    def size(self) -> int:
        return self._size

    @property
    def closed(self) -> bool:
        return self._closed

    def read_at(self, offset: int, size: int) -> bytes:
        if self._closed:
            raise OlmaStateError("handle closed")
        if size < 0 or offset < 0:
            raise OlmaBoundsError("negative offset/size")
        if offset + size > self._size:
            raise OlmaBoundsError(f"{offset}+{size} > {self._size}")
        self._fh.seek(offset)
        data = self._fh.read(size)
        if len(data) != size:
            raise OlmaIntegrityError(f"short read: {len(data)} != {size}")
        if self._tracer:
            self._tracer.on_read(self._path, offset, size)
        return data

    def read_upto(self, offset: int, size: int) -> bytes:
        if self._closed:
            raise OlmaStateError("handle closed")
        if size <= 0 or offset >= self._size:
            return b""
        offset = max(0, offset)
        self._fh.seek(offset)
        n = min(size, self._size - offset)
        data = self._fh.read(n)
        if self._tracer:
            self._tracer.on_read(self._path, offset, len(data))
        return data

    def write_at(self, offset: int, data: bytes) -> None:
        if self._closed:
            raise OlmaStateError("handle closed")
        if "r" in self._fh.mode and "+" not in self._fh.mode:
            raise OlmaStateError("read-only")
        if offset < 0:
            raise OlmaBoundsError("negative offset")
        self._fh.seek(offset)
        self._fh.write(data)
        self._fh.flush()
        if self._tracer:
            self._tracer.on_write(self._path, offset, len(data))

    def flush(self) -> None:
        if not self._closed:
            self._fh.flush()
            try:
                os.fsync(self._fh.fileno())
            except OSError:
                pass

    def close(self) -> None:
        if not self._closed:
            try:
                self._fh.close()
            finally:
                self._closed = True
                if self._tracer:
                    self._tracer.on_close(self._path)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


@dataclass(frozen=True)
class OlmaView:
    handle: OlmaHandle
    base_offset: int
    length: int

    def __post_init__(self) -> None:
        if self.base_offset < 0 or self.length < 0:
            raise OlmaBoundsError("negative")
        if self.base_offset + self.length > self.handle.size:
            raise OlmaBoundsError("view out of file")

    @property
    def size(self) -> int:
        return self.length

    def read(self, off: int, size: int) -> bytes:
        if off < 0 or size < 0 or off + size > self.length:
            raise OlmaBoundsError("out of view")
        return self.handle.read_at(self.base_offset + off, size)

    def read_upto(self, off: int, size: int) -> bytes:
        if off < 0 or off >= self.length or size <= 0:
            return b""
        return self.handle.read_at(self.base_offset + off, min(size, self.length - off))

    def subview(self, off: int, length: int) -> OlmaView:
        if off < 0 or length < 0 or off + length > self.length:
            raise OlmaBoundsError("subview")
        return OlmaView(self.handle, self.base_offset + off, length)


class OlmaBuffer:
    __slots__ = ("_data",)

    def __init__(self, data) -> None:
        self._data = bytes(data)

    def __len__(self):
        return len(self._data)

    def __getitem__(self, k):
        if isinstance(k, slice):
            return self._data[k]
        if not isinstance(k, int):
            raise TypeError(type(k).__name__)
        if k < 0 or k >= len(self._data):
            raise OlmaBoundsError(f"index {k} out of [0,{len(self._data)})")
        return self._data[k]

    def slice(self, start: int, size: int) -> bytes:
        if start < 0 or size < 0 or start + size > len(self._data):
            raise OlmaBoundsError(f"{start}+{size}")
        return self._data[start : start + size]

    def u8(self, o):
        return self.slice(o, 1)[0]

    def u16le(self, o):
        return int.from_bytes(self.slice(o, 2), "little")

    def u32le(self, o):
        return int.from_bytes(self.slice(o, 4), "little")

    def u64le(self, o):
        return int.from_bytes(self.slice(o, 8), "little")

    def raw(self):
        return self._data

    def sha256(self):
        return hashlib.sha256(self._data).hexdigest()


@dataclass(frozen=True)
class OlmaCluster:
    value: int
    max_cluster: int = FAT32_MAX_CLUSTER

    def __post_init__(self):
        if not (2 <= self.value < self.max_cluster):
            raise OlmaBoundsError(f"cluster {self.value:#x}")

    @property
    def is_eoc(self):
        return self.value >= FAT32_EOC_MIN

    @property
    def is_bad(self):
        return self.value == FAT32_BAD

    def next_from(self, fat_get):
        n = fat_get(self.value)
        if n == 0 or n >= FAT32_EOC_MIN or n == FAT32_BAD:
            return None
        try:
            return OlmaCluster(n, self.max_cluster)
        except OlmaBoundsError:
            return None


class OlmaChain:
    def __init__(self, start, fat_get, max_cluster=FAT32_MAX_CLUSTER, budget=1_000_000):
        self._start = OlmaCluster(start, max_cluster)
        self._fat_get = fat_get
        self._budget = budget

    def __iter__(self):
        seen, cur, left = set(), self._start, self._budget
        while cur is not None and left > 0:
            if cur.value in seen:
                raise OlmaIntegrityError(f"cycle at {cur.value:#x}")
            seen.add(cur.value)
            yield cur
            left -= 1
            cur = cur.next_from(self._fat_get)
        if left <= 0:
            raise OlmaIntegrityError("chain budget exceeded")


@dataclass(frozen=True)
class OlmaPartition:
    index: int
    start_sector: int
    size_sectors: int
    type_id: int = 0
    label: str = ""

    def __post_init__(self):
        if self.index < 0 or self.start_sector < 0 or self.size_sectors < 0:
            raise OlmaBoundsError("negative")

    def clamp(self, total):
        s = max(0, self.start_sector)
        if s >= total:
            return OlmaPartition(self.index, 0, 0, self.type_id, self.label)
        return OlmaPartition(
            self.index, s, min(self.size_sectors, total - s), self.type_id, self.label
        )

    def view(self, h, total):
        p = self.clamp(total)
        return OlmaView(h, p.start_sector * SECTOR, p.size_sectors * SECTOR)


class OlmaPartitionTable:
    def __init__(self, buf: OlmaBuffer) -> None:
        self._buf = buf
        self._kind = "unknown"
        self._parts: list[OlmaPartition] = []
        self._parse()

    @property
    def kind(self):
        return self._kind

    @property
    def partitions(self):
        return list(self._parts)

    def _parse(self):
        if len(self._buf) < SECTOR:
            raise OlmaIntegrityError("too small")
        if self._buf.slice(510, 2) == b"\x55\xaa":
            if len(self._buf) >= 1024 and self._buf.slice(512, 8) == b"EFI PART":
                self._kind = "gpt"
                self._parse_gpt()
            else:
                self._kind = "mbr"
                self._parse_mbr()
        else:
            raise OlmaIntegrityError("no 0x55AA")

    def _parse_mbr(self):
        for i in range(4):
            e = self._buf.slice(446 + i * 16, 16)
            s = int.from_bytes(e[8:12], "little")
            sz = int.from_bytes(e[12:16], "little")
            if sz == 0:
                continue
            self._parts.append(OlmaPartition(i, s, sz, e[4], ""))
            if len(self._parts) > MAX_PARTITIONS:
                raise OlmaIntegrityError("too many")

    def _parse_gpt(self):
        h = self._buf.slice(512, 92)
        lba = int.from_bytes(h[72:80], "little")
        num = min(int.from_bytes(h[80:84], "little"), MAX_PARTITIONS)
        esz = int.from_bytes(h[84:88], "little")
        if esz == 0 or esz > 512:
            raise OlmaIntegrityError(f"esz={esz}")
        base = lba * SECTOR
        for i in range(num):
            off = base + i * esz
            if off + 16 > len(self._buf):
                break
            if self._buf.slice(off, 16) == b"\x00" * 16:
                continue
            f = int.from_bytes(self._buf.slice(off + 32, 8), "little")
            l = int.from_bytes(self._buf.slice(off + 40, 8), "little")
            if l >= f:
                self._parts.append(OlmaPartition(i, f, l - f + 1, 0, ""))


class OlmaTransaction:
    def __init__(self, target) -> None:
        self._target = os.fspath(target)
        d = os.path.dirname(os.path.abspath(self._target)) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".olma-", suffix=".tmp")
        os.close(fd)
        self._tmp = tmp
        self._committed = False
        self._h = OlmaHandle(tmp, "wb")

    @property
    def handle(self):
        return self._h

    def write_all(self, data):
        self._h.write_at(0, data)

    def commit(self):
        if self._committed:
            raise OlmaStateError("already committed")
        self._h.flush()
        self._h.close()
        os.replace(self._tmp, self._target)
        self._committed = True

    def rollback(self):
        if self._committed:
            return
        try:
            self._h.close()
        except Exception:
            pass
        try:
            os.unlink(self._tmp)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, t, e, tb):
        self.commit() if t is None else self.rollback()


class OlmaXorDecoder:
    def __init__(self, table) -> None:
        self._t = bytes(table)
        if not self._t:
            raise OlmaIntegrityError("empty table")

    @property
    def table_size(self):
        return len(self._t)

    def decode(self, data):
        n = len(self._t)
        return bytes(b ^ self._t[i % n] for i, b in enumerate(data))

    def decode_file(self, src, dst, chunk=DEFAULT_CHUNK):
        total, off, written = src.size, 0, 0
        while off < total:
            n = min(chunk, total - off)
            dst.write_at(off, self.decode(src.read_at(off, n)))
            off += n
            written += n
        return written


def safe_download(url, *, timeout=30.0, max_size=DEFAULT_MAX_DOWNLOAD) -> bytes:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "MountFile/OLMA"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        ln = resp.headers.get("Content-Length")
        if ln and int(ln) > max_size:
            raise OlmaIntegrityError(f"too big: {ln}")
        data = bytearray()
        while True:
            c = resp.read(1 << 16)
            if not c:
                break
            data.extend(c)
            if len(data) > max_size:
                raise OlmaIntegrityError("size exceeded")
    return bytes(data)


def verify_sha256(data, expected_hex) -> bool:
    if not expected_hex:
        return True
    return hashlib.sha256(data).hexdigest().lower() == expected_hex.strip().lower()
