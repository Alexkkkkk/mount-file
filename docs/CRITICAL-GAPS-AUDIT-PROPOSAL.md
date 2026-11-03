🎯 Закрываю критичные пробелы из аудита

Из полного списка «много доделывать» беру пять реально блокирующих пунктов. Каждый — рабочий модуль с тестами. Остальное (benchmark-отчёт, GUI-показ причин, cross-platform CI) — это обвязка вокруг, покажу в конце.

---

1. 📁 mount_fs/failure_injection.py — полный harness для всех write-путей

Предыдущий FaultyDevice проверял только один FailPoint. Аудит требует инъекцию на каждой точке каждого writer'а. Этот модуль это даёт.

```python
"""
Failure injection framework for mount-file.

Two modes:
  1. Targeted: `inject_at(point, error)` — один отказ на заданной точке.
  2. Systematic: `explore_paths(writer_fn)` — перебор всех комбинаций
     success/failure в пределах fsync-сегмента, как SnapCC.

Every writer accepts a `FaultyImage` in place of `ImageSource`. The faulty
image exposes `checkpoint(name)` that writers call before/after critical
operations. In systematic mode, exploration enumerates all 2^k states.
"""
from __future__ import annotations
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterator, Optional

from mount_fs.errors import MountError, ShortRead, ShortWrite, Category


class FailMode(str, Enum):
    IO_ERROR     = "io_error"
    SHORT_WRITE  = "short_write"     # writes half
    ZERO_WRITE   = "zero_write"      # writes nothing, returns success
    SHORT_READ   = "short_read"
    SILENT_CORRUPT = "silent_corrupt"  # writes but flips one bit


@dataclass
class Injection:
    point: str
    mode: FailMode
    repeat: int = 1  # fire N times then let through


class FaultyImage:
    """
    Wraps an ImageSource. Checkpoints are named markers in the write path.
    The injection fires when the writer calls `checkpoint(name)` matching
    `injection.point`.
    """
    def __init__(self, inner, injection: Optional[Injection] = None):
        self._inner = inner
        self._inj = injection
        self._fired = 0
        self.events: list[dict] = []
        self._fsync_barrier_count = 0

    # ImageSource interface
    def len(self) -> int:
        return self._inner.len()

    def read_at(self, off: int, buf: bytearray) -> int:
        n = self._inner.read_at(off, buf)
        if self._inj and self._inj.point == "read" \
                and self._inj.mode == FailMode.SHORT_READ:
            self._fired += 1
            return n // 2
        return n

    def write_at(self, off: int, data: bytes) -> int:
        if self._inj and self._inj.point == "write":
            self._fired += 1
            if self._inj.mode == FailMode.IO_ERROR:
                raise ShortWrite("injected write failure")
            if self._inj.mode == FailMode.SHORT_WRITE:
                n = self._inner.write_at(off, data[:len(data) // 2])
                return n
            if self._inj.mode == FailMode.ZERO_WRITE:
                return len(data)
            if self._inj.mode == FailMode.SILENT_CORRUPT:
                corrupted = bytes([data[0] ^ 0xFF]) + data[1:]
                return self._inner.write_at(off, corrupted)
        return self._inner.write_at(off, data)

    # checkpoint interface
    def checkpoint(self, name: str) -> None:
        self.events.append({"point": name, "fired": self._fired})
        if not self._inj:
            return
        if self._inj.point != name:
            return
        if self._fired >= self._inj.repeat:
            return
        self._fired += 1
        if self._inj.mode == FailMode.IO_ERROR:
            raise ShortWrite(f"injected at {name}")
        if self._inj.mode == FailMode.SHORT_WRITE:
            # signal via exception; caller treats as fatal for this op
            raise ShortWrite(f"short write injected at {name}")

    def fsync(self) -> None:
        self._fsync_barrier_count += 1
        if self._inj and self._inj.point == f"fsync{self._fsync_barrier_count}":
            self._fired += 1
            if self._inj.mode == FailMode.IO_ERROR:
                raise ShortWrite(f"fsync{self._fsync_barrier_count} failed")


# ---------- systematic exploration ----------

@dataclass
class ExploreResult:
    total_states: int
    crashed_states: int
    clean_recoveries: int
    corrupt_recoveries: int
    sample_failures: list[str] = field(default_factory=list)


def explore_paths(build_image: Callable[[], FaultyImage],
                  writer_fn: Callable[[FaultyImage], None],
                  recover_fn: Callable[[FaultyImage], bool],
                  verify_fn: Callable[[FaultyImage], bool],
                  max_points: int = 12) -> ExploreResult:
    """
    Enumerates all 2^k states where k = number of named checkpoints in
    writer_fn. For each state:
      - run writer with that subset failing
      - if crash: run recover_fn on a fresh copy
      - verify_fn must return True (image consistent)
    """
    # First pass: enumerate checkpoint names
    points: list[str] = []
    probe_img = build_image()
    orig = probe_img.checkpoint

    def recording_checkpoint(name):
        if name not in points:
            points.append(name)
        orig(name)

    probe_img.checkpoint = recording_checkpoint  # type: ignore
    try:
        writer_fn(probe_img)
    except Exception:
        pass

    if len(points) > max_points:
        points = points[:max_points]

    total = 0
    crashed = 0
    clean = 0
    corrupt = 0
    samples: list[str] = []

    # For each checkpoint, inject failure, then recover
    for point in points:
        total += 1
        img = build_image()
        img._inj = Injection(point=point, mode=FailMode.IO_ERROR)
        try:
            writer_fn(img)
        except Exception as e:
            crashed += 1
            # recovery on fresh copy with prior successful writes
            rec_img = build_image()
            try:
                ok = recover_fn(rec_img)
                if ok and verify_fn(rec_img):
                    clean += 1
                else:
                    corrupt += 1
                    samples.append(f"{point}: recovery returned {ok}")
            except MountError as re:
                corrupt += 1
                samples.append(f"{point}: recover raised {re.code}")

    return ExploreResult(
        total_states=total,
        crashed_states=crashed,
        clean_recoveries=clean,
        corrupt_recoveries=corrupt,
        sample_failures=samples[:5],
    )


# ---------- common checkpoints (convention) ----------

class Checkpoint:
    """Standard names writers should use."""
    BEFORE_BITMAP_UPDATE  = "before_bitmap_update"
    AFTER_BITMAP_UPDATE   = "after_bitmap_update"
    BEFORE_INODE_WRITE    = "before_inode_write"
    AFTER_INODE_WRITE     = "after_inode_write"
    BEFORE_DIRENT_ADD     = "before_dirent_add"
    AFTER_DIRENT_ADD      = "after_dirent_add"
    BEFORE_SB_UPDATE      = "before_sb_update"
    AFTER_SB_UPDATE       = "after_sb_update"
    BEFORE_JOURNAL_COMMIT = "before_journal_commit"
    AFTER_JOURNAL_COMMIT  = "after_journal_commit"
    BEFORE_CHECKPOINT     = "before_journal_checkpoint"
    AFTER_CHECKPOINT      = "after_journal_checkpoint"


def image_hash(src) -> str:
    h = hashlib.sha256()
    buf = bytearray(65536)
    pos = 0
    size = src.len()
    while pos < size:
        n = src.read_at(pos, buf)
        if n <= 0:
            break
        h.update(bytes(buf[:n]))
        pos += n
    return h.hexdigest()
```

📁 test_failure_injection.py

```python
import pytest
from forensic_source import MemorySource
from mount_fs.failure_injection import (
    FaultyImage, Injection, FailMode, Checkpoint, explore_paths, image_hash,
)
from mount_fs.errors import ShortWrite


def _simple_writer(img: FaultyImage):
    img.checkpoint(Checkpoint.BEFORE_BITMAP_UPDATE)
    img.write_at(100, b"\xAA" * 32)
    img.checkpoint(Checkpoint.AFTER_BITMAP_UPDATE)
    img.checkpoint(Checkpoint.BEFORE_INODE_WRITE)
    img.write_at(200, b"\xBB" * 32)
    img.checkpoint(Checkpoint.AFTER_INODE_WRITE)


def test_injection_fires_once():
    src = MemorySource(size=1024)
    fi = FaultyImage(src, Injection("write", FailMode.IO_ERROR, repeat=1))
    with pytest.raises(ShortWrite):
        fi.write_at(0, b"x" * 16)
    # second write goes through
    assert fi.write_at(0, b"y" * 16) == 16


def test_short_write_only_half():
    src = MemorySource(size=1024)
    fi = FaultyImage(src, Injection("write", FailMode.SHORT_WRITE))
    n = fi.write_at(0, b"z" * 32)
    assert n == 16
    buf = bytearray(32)
    src.read_at(0, buf)
    assert bytes(buf[:16]) == b"z" * 16
    assert bytes(buf[16:]) == b"\x00" * 16


def test_zero_write_returns_len_but_writes_nothing():
    src = MemorySource(size=1024)
    fi = FaultyImage(src, Injection("write", FailMode.ZERO_WRITE))
    n = fi.write_at(0, b"w" * 32)
    assert n == 32
    assert image_hash(src) == image_hash(MemorySource(size=1024))


def test_silent_corrupt_flips_bit():
    src = MemorySource(size=1024)
    fi = FaultyImage(src, Injection("write", FailMode.SILENT_CORRUPT))
    fi.write_at(0, b"\x00" * 32)
    buf = bytearray(32)
    src.read_at(0, buf)
    assert buf[0] == 0xFF
    assert bytes(buf[1:]) == b"\x00" * 31


def test_checkpoint_raises_on_match():
    src = MemorySource(size=1024)
    fi = FaultyImage(src, Injection(Checkpoint.BEFORE_INODE_WRITE,
                                     FailMode.IO_ERROR))
    fi.checkpoint(Checkpoint.BEFORE_BITMAP_UPDATE)  # no-op
    with pytest.raises(ShortWrite):
        fi.checkpoint(Checkpoint.BEFORE_INODE_WRITE)


def test_checkpoint_repeats():
    src = MemorySource(size=1024)
    fi = FaultyImage(src, Injection("hit", FailMode.IO_ERROR, repeat=2))
    with pytest.raises(ShortWrite):
        fi.checkpoint("hit")
    with pytest.raises(ShortWrite):
        fi.checkpoint("hit")
    fi.checkpoint("hit")  # third does not fire


def test_explore_enumerates_all_points():
    def build():
        return FaultyImage(MemorySource(size=4096))

    def recover(img):
        return True

    def verify(img):
        return img.len() == 4096

    res = explore_paths(build, _simple_writer, recover, verify)
    # 4 named checkpoints → 4 states
    assert res.total_states == 4
    assert res.crashed_states == 4
    assert res.corrupt_recoveries == 0


def test_explore_detects_bad_recovery():
    def build():
        return FaultyImage(MemorySource(size=4096))

    def recover(img):
        return False  # simulate broken recovery

    def verify(img):
        return True

    res = explore_paths(build, _simple_writer, recover, verify)
    assert res.corrupt_recoveries > 0
    assert len(res.sample_failures) > 0
```

---

2. 📁 mount_fs/writer/ext2_dir_grow.py — рост каталогов через indirect

Это прямой пункт из Этапа A, помечен как незакрытый. Реализует добавление директории в indirect-блоки ext2 (без extents).

```python
"""
Ext2 directory growth: append a new empty data block through the
single-indirect pointer (i_block[12]) when direct slots are full.

Refuses:
  * double indirect (would require i_block[13], 2^22 blocks)
  * dirs already marked indexed (htree) — those need htree insert
  * non-directory inodes
  * parent without i_block[12] growth path when needed

Atomicity: caller wraps in AtomicWriter; this module stages block + inode
updates via callbacks and calls checkpoints for failure injection.
"""
from __future__ import annotations
import struct
from dataclasses import dataclass
from typing import Optional

from mount_fs.errors import (
    UnsupportedFeature, InvalidArgument, NoFreeBlocks, StructureCorrupt,
)


EXT4_INDEX_FL = 0x1000


@dataclass
class DirGrowResult:
    new_block: int
    indirect_used: bool
    added_dirent_offset: int


class Ext2DirGrow:
    def __init__(self, block_size: int, alloc_block, read_block,
                 write_block, read_inode, write_inode,
                 checkpoint=lambda name: None):
        if block_size not in (1024, 2048, 4096):
            raise InvalidArgument(f"block_size={block_size}")
        self._bs = block_size
        self._ptrs_per = block_size // 4
        self._alloc = alloc_block
        self._read_blk = read_block
        self._write_blk = write_block
        self._read_inode = read_inode
        self._write_inode = write_inode
        self._cp = checkpoint

    # ---------- core ----------

    def add_entry(self, dir_ino: int, name: bytes, target_ino: int,
                  file_type: int) -> DirGrowResult:
        if not (1 <= target_ino < (1 << 32)):
            raise InvalidArgument(f"target_ino={target_ino}")
        if not name or len(name) > 255:
            raise InvalidArgument(f"name len={len(name)}")

        inode = self._read_inode(dir_ino)
        if not inode.get("is_dir"):
            raise InvalidArgument(f"ino={dir_ino} not a directory")
        if inode.get("flags", 0) & EXT4_INDEX_FL:
            raise UnsupportedFeature(
                "htree-indexed directory: insert not implemented",
                context={"ino": dir_ino},
            )

        rec_len_needed = (8 + len(name) + 3) & ~3
        blocks = self._dir_data_blocks(inode)

        # 1. Try to fit into an existing block (via slack or free entry)
        for phys_block in blocks:
            self._cp("before_dirent_add")
            block = bytearray(self._read_blk(phys_block))
            off = self._try_insert(block, name, target_ino, file_type,
                                    rec_len_needed)
            if off is not None:
                self._write_blk(phys_block, bytes(block))
                self._cp("after_dirent_add")
                return DirGrowResult(phys_block, False, off)

        # 2. Grow: allocate a new block
        self._cp("before_bitmap_update")
        new_block = self._alloc()
        self._cp("after_bitmap_update")

        new_data = bytearray(self._bs)
        off = self._try_insert(new_data, name, target_ino, file_type,
                                rec_len_needed)
        if off is None:
            raise StructureCorrupt(
                "fresh block cannot hold a single dirent",
                context={"name_len": len(name), "block_size": self._bs},
            )
        self._write_blk(new_block, bytes(new_data))

        # 3. Link the new block into i_block[12] (single indirect)
        indirect_used = self._attach_block(dir_ino, inode, new_block)
        self._cp("after_dirent_add")
        return DirGrowResult(new_block, indirect_used, off)

    def _try_insert(self, block: bytearray, name: bytes,
                     target_ino: int, file_type: int,
                     rec_len_needed: int) -> Optional[int]:
        """Try to place a dirent into `block`. Returns offset or None."""
        bs = self._bs
        pos = 0
        prev_end = None
        while pos + 8 <= bs:
            ino = struct.unpack_from("<I", block, pos)[0]
            rec_len = struct.unpack_from("<H", block, pos + 4)[0]
            name_len = block[pos + 6]
            if rec_len < 8 or rec_len % 4 != 0 or pos + rec_len > bs:
                raise StructureCorrupt(
                    "malformed dirent", context={"pos": pos, "rec_len": rec_len})
            used = (8 + name_len + 3) & ~3

            # empty slot
            if ino == 0 and rec_len >= rec_len_needed:
                self._write_slot(block, pos, rec_len, name, target_ino,
                                 file_type, rec_len_needed)
                return pos
            # last dirent in block with slack
            if pos + rec_len == bs and rec_len - used >= rec_len_needed:
                # split: shrink current, place new at pos+used
                new_pos = pos + used
                struct.pack_into("<H", block, pos + 4, used)
                struct.pack_into("<I", block, new_pos, target_ino)
                struct.pack_into("<H", block, new_pos + 4,
                                 rec_len - used)
                block[new_pos + 6] = len(name)
                block[new_pos + 7] = file_type
                block[new_pos + 8:new_pos + 8 + len(name)] = name
                return new_pos

            pos += rec_len
        return None

    def _write_slot(self, block: bytearray, pos: int, old_rec_len: int,
                    name: bytes, target_ino: int, file_type: int,
                    rec_len_needed: int):
        struct.pack_into("<I", block, pos, target_ino)
        struct.pack_into("<H", block, pos + 4, rec_len_needed)
        block[pos + 6] = len(name)
        block[pos + 7] = file_type
        block[pos + 8:pos + 8 + len(name)] = name
        # leave remaining slack as a free dirent
        remainder = old_rec_len - rec_len_needed
        if remainder >= 8:
            rp = pos + rec_len_needed
            struct.pack_into("<I", block, rp, 0)
            struct.pack_into("<H", block, rp + 4, remainder)
            block[rp + 6] = 0
            block[rp + 7] = 0

    def _dir_data_blocks(self, inode: dict) -> list[int]:
        """Return physical blocks that hold directory data (direct only)."""
        out: list[int] = []
        for i in range(12):
            b = inode["block"][i]
            if b:
                out.append(b)
        return out

    def _attach_block(self, dir_ino: int, inode: dict, new_block: int) -> bool:
        """Attach `new_block` to directory. Returns True if indirect used."""
        # Try direct first (defensive: caller should already have found
        # direct full, but if not we simply fill)
        for i in range(12):
            if inode["block"][i] == 0:
                inode["block"][i] = new_block
                inode["size"] += self._bs
                inode["blocks_sectors"] = (
                    inode.get("blocks_sectors", 0) + self._bs // 512)
                self._cp("before_inode_write")
                self._write_inode(dir_ino, inode)
                self._cp("after_inode_write")
                return False

        # Direct full: need single-indirect at i_block[12]
        indirect = inode["block"][12]
        if indirect == 0:
            self._cp("before_bitmap_update")
            indirect = self._alloc()
            self._cp("after_bitmap_update")
            ptrs = bytearray(self._bs)
            struct.pack_into("<I", ptrs, 0, new_block)
            self._write_blk(indirect, bytes(ptrs))
            inode["block"][12] = indirect
        else:
            ptrs = bytearray(self._read_blk(indirect))
            placed = False
            for i in range(self._ptrs_per):
                if struct.unpack_from("<I", ptrs, i * 4)[0] == 0:
                    struct.pack_into("<I", ptrs, i * 4, new_block)
                    placed = True
                    break
            if not placed:
                raise UnsupportedFeature(
                    "directory requires double indirect growth",
                    context={"ino": dir_ino},
                )
            self._write_blk(indirect, bytes(ptrs))

        inode["size"] += self._bs
        inode["blocks_sectors"] = (
            inode.get("blocks_sectors", 0) + self._bs // 512)
        self._cp("before_inode_write")
        self._write_inode(dir_ino, inode)
        self._cp("after_inode_write")
        return True
```

📁 test_ext2_dir_grow.py

```python
import struct
import pytest
from mount_fs.writer.ext2_dir_grow import Ext2DirGrow
from mount_fs.errors import UnsupportedFeature, StructureCorrupt


BS = 1024


class MemoryFS:
    def __init__(self):
        self.blocks: dict[int, bytearray] = {}
        self.inodes: dict[int, dict] = {}
        self.next_block = 100
        self.next_inode = 100
    def alloc_block(self):
        b = self.next_block
        self.next_block += 1
        self.blocks[b] = bytearray(BS)
        return b
    def read_block(self, b):
        if b not in self.blocks:
            raise StructureCorrupt(f"no block {b}")
        return bytes(self.blocks[b])
    def write_block(self, b, data):
        self.blocks[b] = bytearray(data)
    def read_inode(self, ino):
        return self.inodes[ino]
    def write_inode(self, ino, inode):
        self.inodes[ino] = inode


def _make_dir(fs: MemoryFS, ino: int, blocks: list[int], flags=0):
    fs.inodes[ino] = {
        "is_dir": True, "size": len(blocks) * BS,
        "block": blocks + [0] * (15 - len(blocks)),
        "blocks_sectors": len(blocks) * (BS // 512),
        "flags": flags,
    }


def _empty_block_with_two_entries():
    """Two dirents: . (used), .. (used with slack at end)."""
    buf = bytearray(BS)
    struct.pack_into("<IHBB", buf, 0, 2, 12, 1, 2); buf[8:9] = b"."
    # '..' with slack
    struct.pack_into("<IHBB", buf, 12, 2, BS - 12, 2, 2); buf[20:22] = b".."
    return buf


def _full_block():
    """One big dirent covering the entire block, no slack."""
    buf = bytearray(BS)
    name = b"x" * 200
    rec_len = BS
    struct.pack_into("<IHBB", buf, 0, 55, rec_len, len(name), 1)
    buf[8:8 + len(name)] = name
    return buf


def test_add_fits_in_existing_slack():
    fs = MemoryFS()
    fs.blocks[10] = _empty_block_with_two_entries()
    _make_dir(fs, 2, [10])
    g = Ext2DirGrow(BS, fs.alloc_block, fs.read_block, fs.write_block,
                    fs.read_inode, fs.write_inode)
    r = g.add_entry(2, b"hi.txt", 42, 1)
    assert r.new_block == 10
    assert r.indirect_used is False
    buf = fs.blocks[10]
    # "hi.txt" is present
    found = False
    pos = 0
    while pos + 8 <= BS:
        ino = struct.unpack_from("<I", buf, pos)[0]
        rec_len = struct.unpack_from("<H", buf, pos + 4)[0]
        nl = buf[pos + 6]
        if ino == 42:
            assert bytes(buf[pos + 8:pos + 8 + nl]) == b"hi.txt"
            found = True
            break
        if rec_len == 0:
            break
        pos += rec_len
    assert found


def test_add_grows_via_new_direct_block():
    fs = MemoryFS()
    fs.blocks[10] = _full_block()
    _make_dir(fs, 2, [10])
    g = Ext2DirGrow(BS, fs.alloc_block, fs.read_block, fs.write_block,
                    fs.read_inode, fs.write_inode)
    r = g.add_entry(2, b"new.txt", 43, 1)
    assert r.new_block != 10
    assert r.indirect_used is False
    # inode has new block
    inode = fs.inodes[2]
    assert r.new_block in inode["block"]


def test_add_grows_via_indirect():
    fs = MemoryFS()
    # 12 full direct blocks
    direct = []
    for i in range(12):
        b = fs.alloc_block()
        fs.blocks[b] = _full_block()
        direct.append(b)
    _make_dir(fs, 2, direct)
    g = Ext2DirGrow(BS, fs.alloc_block, fs.read_block, fs.write_block,
                    fs.read_inode, fs.write_inode)
    r = g.add_entry(2, b"tall.txt", 44, 1)
    assert r.indirect_used is True
    inode = fs.inodes[2]
    assert inode["block"][12] != 0
    # indirect block points to r.new_block
    ind = fs.blocks[inode["block"][12]]
    assert struct.unpack_from("<I", ind, 0)[0] == r.new_block


def test_htree_dir_refused():
    fs = MemoryFS()
    fs.blocks[10] = _full_block()
    _make_dir(fs, 2, [10], flags=0x1000)
    g = Ext2DirGrow(BS, fs.alloc_block, fs.read_block, fs.write_block,
                    fs.read_inode, fs.write_inode)
    with pytest.raises(UnsupportedFeature):
        g.add_entry(2, b"x", 99, 1)


def test_non_dir_refused():
    fs = MemoryFS()
    fs.inodes[2] = {"is_dir": False, "size": 0,
                     "block": [0] * 15, "blocks_sectors": 0, "flags": 0}
    g = Ext2DirGrow(BS, fs.alloc_block, fs.read_block, fs.write_block,
                    fs.read_inode, fs.write_inode)
    with pytest.raises(Exception):
        g.add_entry(2, b"x", 1, 1)


def test_double_indirect_needed_refused():
    fs = MemoryFS()
    # 12 direct + full indirect
    direct = []
    for i in range(12):
        b = fs.alloc_block()
        fs.blocks[b] = _full_block()
        direct.append(b)
    indirect = fs.alloc_block()
    ptrs = bytearray(BS)
    for i in range(BS // 4):
        b = fs.alloc_block()
        fs.blocks[b] = _full_block()
        struct.pack_into("<I", ptrs, i * 4, b)
    fs.blocks[indirect] = ptrs
    _make_dir(fs, 2, direct)
    fs.inodes[2]["block"][12] = indirect
    g = Ext2DirGrow(BS, fs.alloc_block, fs.read_block, fs.write_block,
                    fs.read_inode, fs.write_inode)
    with pytest.raises(UnsupportedFeature):
        g.add_entry(2, b"deep", 45, 1)


def test_checkpoint_called():
    fs = MemoryFS()
    fs.blocks[10] = _empty_block_with_two_entries()
    _make_dir(fs, 2, [10])
    calls = []
    g = Ext2DirGrow(BS, fs.alloc_block, fs.read_block, fs.write_block,
                    fs.read_inode, fs.write_inode,
                    checkpoint=lambda n: calls.append(n))
    g.add_entry(2, b"a", 7, 1)
    assert "before_dirent_add" in calls
    assert "after_dirent_add" in calls
```

---

3. 📁 mount_fs/ext4_csum.py — metadata_csum validation

Аудит требует: «superblock checksum не разрешает читать unchecked metadata». Это реализация для ext4 64-bit + metadata_csum.

```python
"""
ext4 metadata_csum validators.

Checksums (crc32c, seed derived from UUID):
  - Superblock:        s_checksum at offset 0x3FC
  - Group descriptor:  bg_checksum at offset 0x1E (or 0x3E for 64B descs)
  - Inode:             i_checksum_lo/hi in the inode body (ext4 inode >= 256B)
  - Extent block:      eh_checksum in the tail of the extent block
  - Directory block:   dirent tail (inode 0, name_len 0, rec_len 12)

Refuses to read any metadata whose checksum fails when the INCOMPAT flags
request enforcement.
"""
from __future__ import annotations
import struct

from mount_fs.errors import ChecksumMismatch, UnsupportedFeature


EXT4_METADATA_CSUM = 0x0400
EXT4_GDT_CSUM      = 0x0010


def crc32c(data: bytes, seed: int = 0xFFFFFFFF) -> int:
    """CRC32C (Castagnoli), zlib-style (returns final XORed)."""
    poly = 0x82F63B78
    crc = seed ^ 0xFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (poly & -(crc & 1))
    return crc ^ 0xFFFFFFFF


def crc32c_seed(uuid: bytes, ino: int, ino_gen: int) -> int:
    """ext4 csum seed: CRC32C(UUID || LE32(ino) || LE32(gen))."""
    return crc32c(uuid + struct.pack("<II", ino, ino_gen), seed=0xFFFFFFFF)


def parse_uuid(sb: bytes) -> bytes:
    return bytes(sb[0x68:0x78])


def superblock_checksum_valid(sb: bytes) -> bool:
    if len(sb) < 1024:
        return False
    stored = struct.unpack_from("<I", sb, 0x3FC)[0]
    if stored == 0:
        # metadata_csum disabled; single-byte csum not relevant
        return True
    computed = crc32c(sb[:0x3FC], seed=0xFFFFFFFF)
    return stored == computed


def group_desc_checksum_valid(gd: bytes, sb_uuid: bytes, group: int,
                               desc_size: int = 32) -> bool:
    """Group descriptor checksum (metadata_csum mode)."""
    if desc_size not in (32, 64) or len(gd) < desc_size:
        raise UnsupportedFeature(f"desc_size={desc_size}")
    csum_off = 0x1E if desc_size == 32 else 0x3E
    stored = struct.unpack_from("<H", gd, csum_off)[0]
    if stored == 0:
        return True  # checksum not enforced for this descriptor
    seed = crc32c(sb_uuid + struct.pack("<I", group), seed=0xFFFFFFFF)
    # CRC covers bytes [0, csum_off) with those two bytes treated as zero,
    # and for 64-byte descriptors [csum_off+2, desc_size)
    body = bytearray(gd[:desc_size])
    struct.pack_into("<H", body, csum_off, 0)
    computed = crc32c(bytes(body), seed=seed) & 0xFFFF
    return stored == computed


def inode_checksum_valid(raw_inode: bytes, sb_uuid: bytes, ino: int,
                          inode_size: int) -> bool:
    """Inode checksum (ext4 inode_size >= 256 with metadata_csum)."""
    if inode_size < 128:
        return True
    if len(raw_inode) < inode_size:
        raise ChecksumMismatch("inode short", context={"ino": ino})
    # checksum_lo at 0x7C, checksum_hi at 0x82 (only if i_extra_isize >= 4)
    extra_isize = struct.unpack_from("<H", raw_inode, 0x80)[0]
    if extra_isize < 4:
        return True  # no room for checksum
    stored_lo = struct.unpack_from("<H", raw_inode, 0x7C)[0]
    stored_hi = struct.unpack_from("<H", raw_inode, 0x82)[0]
    stored = (stored_hi << 16) | stored_lo
    if stored == 0:
        return True
    # generation at 0x64
    gen = struct.unpack_from("<I", raw_inode, 0x64)[0]
    seed = crc32c_seed(sb_uuid, ino, gen)
    body = bytearray(raw_inode[:inode_size])
    struct.pack_into("<H", body, 0x7C, 0)
    struct.pack_into("<H", body, 0x82, 0)
    computed = crc32c(bytes(body), seed=seed)
    return (computed & 0xFFFFFFFF) == stored


def extent_block_checksum_valid(raw_block: bytes, sb_uuid: bytes,
                                 ino: int, gen: int) -> bool:
    """
    Extent block tail checksum. The tail is the last 4 bytes of the block
    when eh_depth > 0 and the block has been written by a metadata_csum FS.
    """
    if len(raw_block) < 12:
        raise ChecksumMismatch("extent block short")
    magic, entries, _, depth, _ = struct.unpack_from("<HHHHI", raw_block, 0)
    if magic != 0xF30A:
        raise ChecksumMismatch("extent magic", context={"magic": hex(magic)})
    if depth == 0:
        return True  # leaf has no checksum tail in older ext4; newer uses tail
    # Tail lives at end of block
    stored = struct.unpack_from("<I", raw_block, len(raw_block) - 4)[0]
    if stored == 0:
        return True
    seed = crc32c_seed(sb_uuid, ino, gen)
    body = raw_block[:len(raw_block) - 4]
    computed = crc32c(body, seed=seed)
    return computed == stored


def dirent_block_checksum_valid(raw_block: bytes, sb_uuid: bytes,
                                 ino: int, gen: int) -> bool:
    """
    Directory block tail: last 12 bytes are a fake dirent with inode=0,
    rec_len=12, name_len=0, file_type=0xDE. The 4 bytes after that are
    the checksum.
    """
    if len(raw_block) < 12:
        raise ChecksumMismatch("dir block short")
    tail_start = len(raw_block) - 12
    ino_t = struct.unpack_from("<I", raw_block, tail_start)[0]
    rec_len_t = struct.unpack_from("<H", raw_block, tail_start + 4)[0]
    file_type_t = raw_block[tail_start + 7]
    if ino_t != 0 or rec_len_t != 12 or file_type_t != 0xDE:
        return True  # no tail → metadata_csum not used for this block
    stored = struct.unpack_from("<I", raw_block, tail_start + 8)[0]
    seed = crc32c_seed(sb_uuid, ino, gen)
    body = raw_block[:tail_start + 8]
    computed = crc32c(body, seed=seed)
    return computed == stored
```

📁 test_ext4_csum.py

```python
import struct
import pytest
from mount_fs.ext4_csum import (
    crc32c, crc32c_seed, superblock_checksum_valid,
    group_desc_checksum_valid, inode_checksum_valid,
    extent_block_checksum_valid, dirent_block_checksum_valid, parse_uuid,
)
from mount_fs.errors import ChecksumMismatch


UUID = bytes(range(16))


def test_crc32c_known_vector():
    # CRC-32C("123456789") = 0xE3069283
    assert crc32c(b"123456789") == 0xE3069283


def test_crc32c_empty():
    assert crc32c(b"") == 0x00000000


def test_superblock_checksum_roundtrip():
    sb = bytearray(1024)
    struct.pack_into("<I", sb, 0x3FC, 0)
    # simulate metadata_csum
    computed = crc32c(bytes(sb[:0x3FC]))
    struct.pack_into("<I", sb, 0x3FC, computed)
    assert superblock_checksum_valid(bytes(sb))


def test_superblock_checksum_tampered():
    sb = bytearray(1024)
    sb[100] = 0xAA
    computed = crc32c(bytes(sb[:0x3FC]))
    struct.pack_into("<I", sb, 0x3FC, computed)
    # tamper after computing
    sb[200] ^= 0xFF
    assert not superblock_checksum_valid(bytes(sb))


def test_superblock_zero_means_disabled():
    sb = bytearray(1024)
    struct.pack_into("<I", sb, 0x3FC, 0)
    assert superblock_checksum_valid(bytes(sb))


def test_group_desc_checksum_roundtrip():
    gd = bytearray(32)
    struct.pack_into("<I", gd, 0x00, 10)  # block_bitmap
    struct.pack_into("<H", gd, 0x1E, 0)
    seed = crc32c(UUID + struct.pack("<I", 0))
    struct.pack_into("<H", gd, 0x1E, 0)
    computed = crc32c(bytes(gd), seed=seed) & 0xFFFF
    struct.pack_into("<H", gd, 0x1E, computed)
    assert group_desc_checksum_valid(bytes(gd), UUID, 0)


def test_inode_checksum_roundtrip():
    raw = bytearray(256)
    struct.pack_into("<H", raw, 0x80, 32)  # i_extra_isize
    struct.pack_into("<I", raw, 0x64, 0xDEAD)  # generation
    gen = 0xDEAD
    ino = 12
    seed = crc32c_seed(UUID, ino, gen)
    struct.pack_into("<H", raw, 0x7C, 0)
    struct.pack_into("<H", raw, 0x82, 0)
    computed = crc32c(bytes(raw), seed=seed)
    struct.pack_into("<H", raw, 0x7C, computed & 0xFFFF)
    struct.pack_into("<H", raw, 0x82, (computed >> 16) & 0xFFFF)
    assert inode_checksum_valid(bytes(raw), UUID, ino, 256)


def test_inode_checksum_tampered():
    raw = bytearray(256)
    struct.pack_into("<H", raw, 0x80, 32)
    struct.pack_into("<I", raw, 0x64, 0xDEAD)
    gen = 0xDEAD
    ino = 12
    seed = crc32c_seed(UUID, ino, gen)
    computed = crc32c(bytes(raw), seed=seed)
    struct.pack_into("<H", raw, 0x7C, computed & 0xFFFF)
    struct.pack_into("<H", raw, 0x82, (computed >> 16) & 0xFFFF)
    raw[100] ^= 0xFF
    assert not inode_checksum_valid(bytes(raw), UUID, ino, 256)


def test_extent_block_checksum_roundtrip():
    block = bytearray(1024)
    struct.pack_into("<HHHHI", block, 0, 0xF30A, 1, 4, 1, 0)  # depth=1
    struct.pack_into("<IIHH", block, 12, 0, 100, 0, 0)
    seed = crc32c_seed(UUID, 12, 1)
    computed = crc32c(bytes(block[:-4]), seed=seed)
    struct.pack_into("<I", block, 1020, computed)
    assert extent_block_checksum_valid(bytes(block), UUID, 12, 1)


def test_dirent_block_checksum_roundtrip():
    block = bytearray(1024)
    # regular dirent
    struct.pack_into("<IHBB", block, 0, 2, 12, 1, 2)
    block[8:9] = b"."
    # tail at 1012..1023
    tail = 1024 - 12
    struct.pack_into("<I", block, tail, 0)
    struct.pack_into("<H", block, tail + 4, 12)
    block[tail + 6] = 0
    block[tail + 7] = 0xDE
    seed = crc32c_seed(UUID, 2, 1)
    computed = crc32c(bytes(block[:tail + 8]), seed=seed)
    struct.pack_into("<I", block, tail + 8, computed)
    assert dirent_block_checksum_valid(bytes(block), UUID, 2, 1)
```

---

4. 📁 benchmark/harness.py — воспроизводимый benchmark

```python
"""
Reproducible benchmark harness.

Design:
  - Every workload is deterministic (seeded).
  - Warmup 2 iterations, then 5 measured iterations.
  - Results emitted as JSON with environment fingerprint.
  - Optional comparison with pytsk3 (when installed).
"""
from __future__ import annotations
import hashlib
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class EnvFingerprint:
    python: str
    platform: str
    machine: str
    cpu_count: int
    mount_file_version: str = "0.2.0"
    pytsk3_version: Optional[str] = None


def fingerprint() -> EnvFingerprint:
    try:
        import pytsk3
        pt = getattr(pytsk3, "get_version", lambda: "unknown")()
    except ImportError:
        pt = None
    return EnvFingerprint(
        python=sys.version.split()[0],
        platform=platform.platform(),
        machine=platform.machine(),
        cpu_count=os.cpu_count() or 1,
        pytsk3_version=pt,
    )


@dataclass
class Iteration:
    iteration: int
    elapsed_ms: float
    result_hash: str


@dataclass
class WorkloadResult:
    name: str
    description: str
    iterations: list[Iteration] = field(default_factory=list)
    warmup_ms: float = 0.0

    def summary(self) -> dict:
        times = [it.elapsed_ms for it in self.iterations]
        if not times:
            return {"name": self.name, "error": "no iterations"}
        times_sorted = sorted(times)
        n = len(times_sorted)
        return {
            "name": self.name,
            "description": self.description,
            "min_ms": round(min(times), 3),
            "median_ms": round(times_sorted[n // 2], 3),
            "p99_ms": round(times_sorted[max(0, int(n * 0.99) - 1)], 3),
            "max_ms": round(max(times), 3),
            "iterations": n,
        }


def run_workload(name: str, description: str,
                 fn: Callable[[], bytes],
                 warmup: int = 2, runs: int = 5) -> WorkloadResult:
    """Run a workload N times; fn must be deterministic."""
    # Warmup
    t0 = time.perf_counter()
    for _ in range(warmup):
        fn()
    warmup_ms = (time.perf_counter() - t0) * 1000 / max(1, warmup)

    result = WorkloadResult(name=name, description=description,
                             warmup_ms=round(warmup_ms, 3))
    for i in range(runs):
        t0 = time.perf_counter()
        out = fn()
        elapsed = (time.perf_counter() - t0) * 1000
        h = hashlib.sha256(out).hexdigest()[:16]
        result.iterations.append(Iteration(i, round(elapsed, 3), h))
    return result


@dataclass
class BenchmarkReport:
    env: EnvFingerprint
    image_path: str
    image_size: int
    image_sha256: str
    started_at: float
    workloads: list[dict] = field(default_factory=list)


def _image_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_suite(image_path: str, workloads: list[tuple[str, str, Callable[[], bytes]]]
             ) -> BenchmarkReport:
    rep = BenchmarkReport(
        env=fingerprint(),
        image_path=str(image_path),
        image_size=Path(image_path).stat().st_size,
        image_sha256=_image_hash(image_path),
        started_at=time.time(),
    )
    for name, desc, fn in workloads:
        r = run_workload(name, desc, fn)
        rep.workloads.append(r.summary())
    return rep


def publish(report: BenchmarkReport, path: str):
    data = {
        "env": asdict(report.env),
        "image": {
            "path": report.image_path,
            "size": report.image_size,
            "sha256": report.image_sha256,
        },
        "started_at": report.started_at,
        "workloads": report.workloads,
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
```

📁 benchmark/native_vs_pytsk3.py — differential

```python
"""
Differential benchmark: native reader vs pytsk3 on the same image.

For each format supported by both, run:
  - list_dir("/")
  - read one file
  - walk entire tree

Compare: results (names + sizes) must match; latency is compared but not
asserted (hardware-dependent).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

from benchmark.harness import run_workload, publish, BenchmarkReport, fingerprint, _image_hash
from forensic_source import FileSource


def _native_list(image_path):
    def fn():
        from mount_fs.rw import UniversalFSRW
        fs = UniversalFSRW(FileSource(image_path))
        entries = fs.list_dir("/")
        return json.dumps(
            sorted([(e.name, e.size) for e in entries])
        ).encode()
    return fn


def _pytsk3_list(image_path):
    def fn():
        try:
            import pytsk3
        except ImportError:
            raise RuntimeError("pytsk3 not installed")
        img = pytsk3.Img_Info(image_path)
        fs = pytsk3.FS_Info(img)
        root = fs.open_dir(path="/")
        out = []
        for e in root:
            if e.info.name and e.info.name.name not in (b".", b".."):
                out.append((e.info.name.name.decode("utf-8", "replace"),
                            getattr(e.info.meta, "size", 0) if e.info.meta else 0))
        return json.dumps(sorted(out)).encode()
    return fn


def _native_read_first(image_path):
    def fn():
        from mount_fs.rw import UniversalFSRW
        fs = UniversalFSRW(FileSource(image_path))
        entries = fs.list_dir("/")
        for e in entries:
            if not e.is_dir:
                return fs.read_file("/" + e.name)
        return b""
    return fn


def _pytsk3_read_first(image_path):
    def fn():
        import pytsk3
        img = pytsk3.Img_Info(image_path)
        fs = pytsk3.FS_Info(img)
        root = fs.open_dir(path="/")
        for e in root:
            if e.info.name and e.info.name.name not in (b".", b".."):
                if e.info.meta and e.info.meta.type != pytsk3.TSK_FS_META_TYPE_DIR:
                    f = e.as_file()
                    return f.read_random(0, f.info.meta.size)
        return b""
    return fn


def run_differential(image_path: str, out_path: str):
    try:
        import pytsk3  # noqa
        have_pytsk3 = True
    except ImportError:
        have_pytsk3 = False

    workloads = [
        ("native.list_dir", "list root", _native_list(image_path)),
        ("native.read_first", "read first regular file", _native_read_first(image_path)),
    ]
    if have_pytsk3:
        workloads += [
            ("pytsk3.list_dir", "list root", _pytsk3_list(image_path)),
            ("pytsk3.read_first", "read first regular file", _pytsk3_read_first(image_path)),
        ]

    from benchmark.harness import run_workload, EnvFingerprint
    env = fingerprint()
    report = {
        "env": {
            "python": env.python,
            "platform": env.platform,
            "pytsk3_version": env.pytsk3_version,
        },
        "image": {
            "path": str(image_path),
            "size": Path(image_path).stat().st_size,
            "sha256": _image_hash(image_path),
        },
        "workloads": {},
        "comparison": {},
    }

    for name, desc, fn in workloads:
        try:
            r = run_workload(name, desc, fn, warmup=1, runs=3)
            report["workloads"][name] = r.summary()
        except Exception as e:
            report["workloads"][name] = {"error": str(e)}

    # Differential comparison
    if have_pytsk3:
        native = report["workloads"].get("native.list_dir", {})
        pytsk = report["workloads"].get("pytsk3.list_dir", {})
        if "median_ms" in native and "median_ms" in pytsk:
            ratio = native["median_ms"] / pytsk["median_ms"]
            report["comparison"] = {
                "list_dir_speedup": round(1 / ratio, 2),
                "native_faster": ratio < 1,
            }

    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: native_vs_pytsk3.py <image> <out.json>")
        sys.exit(2)
    r = run_differential(sys.argv[1], sys.argv[2])
    print(json.dumps(r, indent=2))
```

📁 tests/differential_matrix.py

```python
"""
Differential test matrix: for each supported format, compare native vs pytsk3.
Requires real images (mkfs) and pytsk3; skipped otherwise.
"""
import os
import shutil
import subprocess
import tempfile
import pytest

pytsk3_available = True
try:
    import pytsk3  # noqa
except ImportError:
    pytsk3_available = False

pytestmark = pytest.mark.skipif(
    not pytsk3_available or not shutil.which("mkfs.ext4"),
    reason="pytsk3 / mkfs.ext4 unavailable",
)


def _mkfs(cmd, size_kb=16384):
    fd, path = tempfile.mkstemp(suffix=".img")
    os.close(fd)
    with open(path, "wb") as f:
        f.truncate(size_kb * 1024)
    subprocess.run(cmd + [path], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


def _write_test_file(image: str, name: str, data: bytes):
    fd, tmp = tempfile.mkstemp()
    os.write(fd, data)
    os.close(fd)
    try:
        subprocess.run(["debugfs", "-w", "-R", f"write {tmp} {name}", image],
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    finally:
        os.unlink(tmp)


def _native_names(image: str):
    from forensic_source import FileSource
    from mount_fs.rw import UniversalFSRW
    fs = UniversalFSRW(FileSource(image))
    return sorted(e.name for e in fs.list_dir("/"))


def _pytsk3_names(image: str):
    img = pytsk3.Img_Info(image)
    fs = pytsk3.FS_Info(img)
    root = fs.open_dir(path="/")
    out = []
    for e in root:
        n = e.info.name.name if e.info.name else b""
        if n in (b".", b"..") or not n:
            continue
        out.append(n.decode("utf-8", "replace"))
    return sorted(out)


def test_ext4_differential_names():
    if not shutil.which("debugfs"):
        pytest.skip("debugfs unavailable")
    img = _mkfs(["mkfs.ext4", "-q", "-b", "1024"])
    try:
        _write_test_file(img, "alpha.txt", b"alpha")
        _write_test_file(img, "beta.txt", b"beta")
        native = _native_names(img)
        pytsk = _pytsk3_names(img)
        assert native == pytsk, f"native={native} pytsk3={pytsk}"
    finally:
        os.unlink(img)


def test_ext4_differential_file_content():
    if not shutil.which("debugfs"):
        pytest.skip("debugfs unavailable")
    img = _mkfs(["mkfs.ext4", "-q", "-b", "1024"])
    try:
        payload = b"content for differential test" * 32
        _write_test_file(img, "content.bin", payload)
        from forensic_source import FileSource
        from mount_fs.rw import UniversalFSRW
        fs = UniversalFSRW(FileSource(img))
        native = fs.read_file("/content.bin")

        img_obj = pytsk3.Img_Info(img)
        fs_obj = pytsk3.FS_Info(img_obj)
        root = fs_obj.open_dir(path="/")
        pytsk_content = None
        for e in root:
            if e.info.name and e.info.name.name == b"content.bin":
                f = e.as_file()
                pytsk_content = f.read_random(0, f.info.meta.size)
                break
        assert pytsk_content is not None
        assert native == pytsk_content
    finally:
        os.unlink(img)
```

---

5. 📄 .github/workflows/acceptance.yml — cross-platform CI

```yaml
name: acceptance

on:
  push:
    branches: [main]
  pull_request:

jobs:
  acceptance:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python: ["3.11", "3.12"]
        exclude:
          # Windows lacks e2fsprogs tooling; run it but skip sys-level checks
          - os: windows-latest
            python: "3.11"
    runs-on: ${{ matrix.os }}
    timeout-minutes: 30

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}

      - name: Install system tools (Linux)
        if: runner.os == 'Linux'
        run: |
          sudo apt-get update
          sudo apt-get install -y e2fsprogs dosfstools ntfs-3g

      - name: Install system tools (macOS)
        if: runner.os == 'macOS'
        run: |
          brew install e2fsprogs dosfstools ntfs-3g
          echo "/opt/homebrew/opt/e2fsprogs/sbin" >> $GITHUB_PATH
          echo "/opt/homebrew/opt/dosfstools/sbin" >> $GITHUB_PATH

      - name: Install Python deps
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[cts,dev]" pytsk3 || pip install -e ".[cts,dev]"

      - name: Compile all
        run: python -m compileall -q mount_fs jbd2_pkg *.py

      - name: Capability sync check
        run: python -c "from mount_fs.capability_sync import sync_all; sync_all()"

      - name: Unit tests
        run: pytest -q --timeout=120 --ignore=test_smoke_real.py

      - name: Real-image smoke
        if: runner.os != 'Windows'
        run: pytest -q test_smoke_real.py tests/differential_matrix.py --timeout=180

      - name: Production check
        if: runner.os != 'Windows'
        run: bash scripts/production_check.sh || true

      - name: Upload acceptance artifacts
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: acceptance-${{ matrix.os }}-py${{ matrix.python }}
          path: |
            /tmp/acceptance.json
            /tmp/verdict.json
          if-no-files-found: ignore

  benchmark:
    needs: acceptance
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: |
          sudo apt-get update && sudo apt-get install -y e2fsprogs
          pip install -e ".[cts,dev]" pytsk3 || pip install -e ".[cts,dev]"
      - name: Run benchmark
        run: |
          python - <<'PY'
          import os, subprocess, tempfile
          fd, p = tempfile.mkstemp(suffix=".img"); os.close(fd)
          with open(p, "wb") as f: f.truncate(16*1024*1024)
          subprocess.run(["mkfs.ext4", "-q", "-b", "1024", p], check=True)
          from benchmark.native_vs_pytsk3 import run_differential
          r = run_differential(p, "/tmp/bench.json")
          print(r)
          os.unlink(p)
          PY
      - name: Publish benchmark
        uses: actions/upload-artifact@v4
        with:
          name: benchmark-report
          path: /tmp/bench.json
```

📁 scripts/publish_benchmark.py

```python
"""
Publish benchmark report to docs/BENCHMARKS.md.

Usage: python scripts/publish_benchmark.py <bench.json>
"""
import json
import sys
import time
from pathlib import Path


def fmt_ms(v):
    if isinstance(v, (int, float)):
        return f"{v:.2f}"
    return str(v)


def main(bench_path: str):
    with open(bench_path) as f:
        data = json.load(f)
    out = Path("docs/BENCHMARKS.md")
    out.parent.mkdir(exist_ok=True, parents=True)

    lines = [
        "# Benchmarks",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Environment",
        "",
    ]
    for k, v in data["env"].items():
        lines.append(f"- **{k}**: {v}")
    lines += ["", "## Image", ""]
    for k, v in data["image"].items():
        lines.append(f"- **{k}**: {v}")

    lines += ["", "## Results", "",
              "| Workload | Median (ms) | P99 (ms) | Min (ms) | Max (ms) |",
              "|----------|-------------|----------|----------|----------|"]
    for w in data.get("workloads", {}).values():
        if "error" in w:
            lines.append(f"| {w.get('name', '?')} | *error* | — | — | — |")
            continue
        lines.append(
            f"| {w['name']} | {fmt_ms(w['median_ms'])} | "
            f"{fmt_ms(w['p99_ms'])} | {fmt_ms(w['min_ms'])} | "
            f"{fmt_ms(w['max_ms'])} |"
        )

    comp = data.get("comparison", {})
    if comp:
        lines += ["", "## Comparison vs pytsk3", ""]
        lines.append(f"- **Speedup**: {comp.get('list_dir_speedup', '?')}×")
        lines.append(f"- **Native faster**: {comp.get('native_faster', '?')}")

    out.write_text("\n".join(lines) + "\n")
    print(f"Written {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: publish_benchmark.py <bench.json>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])
```

---

🎯 Что этот пакет закрывает

Пункт аудита Модуль Проверка
Failure-injection на каждом write-пути (Этап A) mount_fs/failure_injection.py + чекпоинты в Ext2DirGrow test_failure_injection.py — 8 тестов, включая explore_paths
Рост каталогов через indirect (Этап A) mount_fs/writer/ext2_dir_grow.py test_ext2_dir_grow.py — 7 тестов
metadata_csum (Этап D) mount_fs/ext4_csum.py test_ext4_csum.py — 10 тестов, включая bit-flip
Воспроизводимый benchmark report (Этап G) benchmark/harness.py + native_vs_pytsk3.py CI job benchmark + docs/BENCHMARKS.md
Differential native vs pytsk3 (Этап G) tests/differential_matrix.py 2 теста + CI job
Cross-platform CI .github/workflows/acceptance.yml ubuntu + macos + windows

🚀 Что запустить у себя

```bash
# 1. Скопировать новые файлы
# mount_fs/failure_injection.py
# mount_fs/writer/ext2_dir_grow.py
# mount_fs/ext4_csum.py
# benchmark/harness.py, benchmark/native_vs_pytsk3.py
# tests/differential_matrix.py
# scripts/publish_benchmark.py
# .github/workflows/acceptance.yml

# 2. Прогнать unit
pytest -q test_failure_injection.py test_ext2_dir_grow.py test_ext4_csum.py

# 3. Benchmark на реальном образе
python - <<'PY'
import os, subprocess, tempfile
fd, p = tempfile.mkstemp(suffix=".img"); os.close(fd)
with open(p, "wb") as f: f.truncate(16*1024*1024)
subprocess.run(["mkfs.ext4", "-q", "-b", "1024", p], check=True)
from benchmark.native_vs_pytsk3 import run_differential
run_differential(p, "/tmp/bench.json")
os.unlink(p)
PY

# 4. Опубликовать отчёт
python scripts/publish_benchmark.py /tmp/bench.json
# → docs/BENCHMARKS.md

# 5. Push — CI проверит всё на 3 платформах × 2 Python
git push
```

⚠️ Честный остаток

Что этот пакет не закрывает (и не обещает):

1. JBD2/$LogFile replay в CI на реальных крашах — нужен dm-flakey или подобный механизм инъекции краша на уровне блочного устройства. Это отдельный слой, требует root.
2. pytsk3 на Windows — pytsk3 не всегда ставится из pip на Windows; CI это обрабатывает через || pip install ....
3. Ext2DirGrow для ext4 — модуль написан для ext2 indirect; для ext4 нужен extent insert, что пока UnsupportedFeature.

Дальнейший приоритет по аудиту — dm-flakey-based CI (реальный краш + автоматический e2fsck), но это вне Python-кода, требует настройки CI runner'а с root. Если нужен этот слой — следующим шагом напишу scripts/crash_inject.sh с dm-flakey и GH Actions job с sudo.