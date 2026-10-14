"""Virtual File System facade over one filesystem inside a disk image.

Design notes
------------
* pytsk3 (The Sleuth Kit) is a **read** engine here. Its read path is excellent
  (FAT12/16/32, NTFS, EXT2/3/4, exFAT, ISO9660, HFS+, YAFFS...) and its write
  path either does not exist or is not usable for ordinary files. Anything in
  this module that claims to write is not pytsk3 — writes live in
  ``fatwrite.py`` (mtools) and ``rawio.py`` (transactional sector patcher).
* ``pytsk3.TskFile.read()`` does **not exist**. TSK reads are random-access:
  ``read_random(offset, size)``. Chunked sequential reads are therefore
  implemented by looping ``read_random`` up to ``meta.size`` — and that loop is
  unit-tested, including the sparse-hole case where TSK returns ``None``.
* ``entry.info.meta`` can legitimately be ``None`` (some special/deleted
  entries); every accessor tolerates that.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Iterator, List, Optional

try:  # pragma: no cover - import guard
    import pytsk3
except ImportError as exc:  # pragma: no cover
    pytsk3 = None  # type: ignore
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

DEFAULT_CHUNK = 1 << 20

META_DIR = getattr(pytsk3, "TSK_FS_META_TYPE_DIR", 2) if pytsk3 else 2
META_REG = getattr(pytsk3, "TSK_FS_META_TYPE_REG", 1) if pytsk3 else 1
# TSK meta types that are NOT real on-disk directory entries:
#    10 = TSK_FS_META_TYPE_VIRT ($MBR, $FAT1, $FAT2, ... synthesised by TSK)
#    11 = TSK_FS_META_TYPE_STR  ($OrphanFiles and other virtual streams)
META_VIRT = getattr(pytsk3, "TSK_FS_META_TYPE_VIRT", 10) if pytsk3 else 10
META_STR = getattr(pytsk3, "TSK_FS_META_TYPE_STR", 11) if pytsk3 else 11
PSEUDO_META_TYPES = {META_VIRT, META_STR}
# TSK also injects a synthetic entry that carries the FAT/NTFS volume label
_TSK_PSEUDO_SUFFIX = "(Volume Label Entry)"

# TSK type codes as seen in meta.type
META_TYPE_NAMES = {
    0: "unknown",
    1: "file",
    2: "dir",
    3: "char",
    4: "block",
    5: "fifo",
    6: "symlink",
    7: "shadow",
    8: "socket",
    9: "whiteout",
    10: "virtual",
    11: "stream",
}


class VfsError(Exception):
    """Any error talking to the filesystem (open failure, missing path, ...)."""


def _require_pytsk3() -> None:
    if pytsk3 is None:  # pragma: no cover
        raise VfsError(
            "pytsk3 is not installed. Install it with: pip install pytsk3 "
            f"({_IMPORT_ERROR})"
        )


@dataclasses.dataclass
class VfsEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    inode: int = 0
    type_name: str = "unknown"
    mtime: Optional[int] = None
    mode: Optional[int] = None
    allocated: bool = True
    virtual: bool = False          # TSK pseudo file ($MBR, $FAT1, $OrphanFiles, ...)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _meta_of(tsk_entry) -> Optional[object]:
    info = getattr(tsk_entry, "info", None)
    if info is None:
        return None
    try:
        return info.meta
    except Exception:
        return None


def entry_from_tsk(tsk_entry, dir_path: str) -> Optional[VfsEntry]:
    """Convert one pytsk3 directory entry into a :class:`VfsEntry`.

    Returns ``None`` for the ``.`` / ``..`` entries. Handles ``meta is None``
    and non-UTF-8 names. Kept module-level on purpose so it can be unit-tested
    with a fake TSK object.
    """
    info = getattr(tsk_entry, "info", None)
    if info is None:
        return None
    raw_name = b""
    try:
        raw_name = info.name.name
    except Exception:
        return None
    from .util import decode_name, join_fs_path, normalize_path

    name = decode_name(raw_name)
    if name in (".", ".."):
        return None

    meta = _meta_of(tsk_entry)
    mtype = getattr(meta, "type", None) if meta is not None else None
    is_dir = mtype == META_DIR
    size = getattr(meta, "size", None) if meta is not None else None
    inode = getattr(meta, "addr", 0) if meta is not None else 0
    mtime = getattr(meta, "mtime", None) if meta is not None else None
    mode = getattr(meta, "mode", None) if meta is not None else None
    flags = getattr(meta, "flags", None) if meta is not None else None
    allocated = True
    if flags is not None and pytsk3 is not None:
        allocated = bool(flags & getattr(pytsk3, "TSK_FS_META_FLAG_ALLOC", 1))
    virtual = mtype in PSEUDO_META_TYPES or name.endswith(_TSK_PSEUDO_SUFFIX)

    return VfsEntry(
        name=name,
        path=join_fs_path(normalize_path(dir_path), name),
        is_dir=is_dir,
        size=int(size or 0),
        inode=int(inode or 0),
        type_name=META_TYPE_NAMES.get(mtype, "unknown" if mtype is None else str(mtype)),
        mtime=int(mtime) if mtime else None,
        mode=mode,
        allocated=allocated,
        virtual=virtual,
    )


class ImageVFS:
    """Read-oriented view of one filesystem at ``offset`` inside ``image_path``."""

    def __init__(self, image_path: str, offset: int = 0, chunk: int = DEFAULT_CHUNK):
        _require_pytsk3()
        if not os.path.exists(image_path):
            raise VfsError(f"image not found: {image_path}")
        self.image_path = image_path
        self.offset = int(offset)
        self.chunk = int(chunk)
        try:
            self.img = pytsk3.Img_Info(image_path)
            self.fs = pytsk3.FS_Info(self.img, offset=self.offset)
        except Exception as exc:  # pytsk3 raises IOError subclasses
            raise VfsError(
                f"cannot open filesystem at offset {self.offset} in {image_path}: {exc}"
            ) from exc
        try:
            self.fs_type = self.fs.info.ftype
        except Exception:
            self.fs_type = None

    # -- listing ----------------------------------------------------------- #
    def list_dir(self, path: str = "/", include_virtual: bool = False) -> List[VfsEntry]:
        """List a directory.

        TSK injects its own pseudo files into every FAT/NTFS listing (`$MBR`,
        `$FAT1`, `$FAT2`, `$OrphanFiles`, plus a synthetic volume-label entry).
        A user-facing file manager must not show them, so they are filtered out
        by default; ``include_virtual=True`` returns the raw TSK view (useful for
        forensic work, where `$MFT`/`$OrphanFiles` are interesting).
        """
        from .util import normalize_path

        path = normalize_path(path)
        try:
            directory = self.fs.open_dir(path)
        except Exception as exc:
            raise VfsError(f"cannot open directory {path!r}: {exc}") from exc

        out: List[VfsEntry] = []
        try:
            for entry in directory:
                item = entry_from_tsk(entry, path)
                if item is None:
                    continue
                if item.virtual and not include_virtual:
                    continue
                out.append(item)
        except Exception as exc:  # a corrupt entry must not kill the listing
            if not out:
                raise VfsError(f"error while reading {path!r}: {exc}") from exc
        out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return out

    def walk(self, path: str = "/", max_entries: Optional[int] = None,
             include_virtual: bool = False) -> Iterator[VfsEntry]:
        stack = [path]
        seen = 0
        while stack:
            current = stack.pop()
            for entry in self.list_dir(current, include_virtual=include_virtual):
                yield entry
                seen += 1
                if max_entries is not None and seen >= max_entries:
                    return
                if entry.is_dir:
                    stack.append(entry.path)

    # -- stat / open -------------------------------------------------------- #
    def stat(self, path: str) -> VfsEntry:
        from .util import decode_name, normalize_path

        path = normalize_path(path)
        if path == "/":
            return VfsEntry(name="/", path="/", is_dir=True, size=0, type_name="dir")
        parent, _, name = path.rpartition("/")
        for entry in self.list_dir(parent or "/"):
            if entry.name == name:
                return entry
        raise VfsError(f"path not found: {path}")

    def open_file(self, path: str):
        from .util import normalize_path

        path = normalize_path(path)
        try:
            return self.fs.open(path)
        except Exception as exc:
            raise VfsError(f"cannot open file {path!r}: {exc}") from exc

    @staticmethod
    def size_of(tsk_file) -> int:
        meta = getattr(getattr(tsk_file, "info", None), "meta", None)
        return int(getattr(meta, "size", 0) or 0)

    # -- reading ------------------------------------------------------------ #
    def read_range(self, path: str, offset: int = 0, length: int = 4096,
                   zero_on_hole: bool = True) -> bytes:
        f = self.open_file(path)
        data = f.read_random(offset, length)
        if data is None:
            if not zero_on_hole:
                return b""
            return b"\x00" * length
        return bytes(data)

    def iter_file(self, path: str, chunk: Optional[int] = None) -> Iterator[bytes]:
        """Stream a file. Handles sparse holes (read_random -> None) safely."""
        chunk = chunk or self.chunk
        f = self.open_file(path)
        size = self.size_of(f)
        offset = 0
        guard = 0
        while offset < size:
            want = min(chunk, size - offset)
            data = f.read_random(offset, want)
            if data is None:
                # sparse hole: TSK reports "no data here"
                yield b"\x00" * want
                offset += want
            elif not data:
                break
            else:
                yield bytes(data)
                offset += len(data)
            guard += 1
            if guard > 1_000_000:  # pragma: no cover - paranoia
                raise VfsError("read loop guard tripped while streaming a file")

    def read_file(self, path: str, chunk: Optional[int] = None) -> bytes:
        return b"".join(self.iter_file(path, chunk))

    def extract(self, src_path: str, dst_path: str, chunk: Optional[int] = None,
                verify: bool = False) -> dict:
        """Write ``src_path`` from the image to ``dst_path`` on the host."""
        stat = self.stat(src_path)
        if stat.is_dir:
            raise VfsError(f"{src_path!r} is a directory; extraction handles files only")
        written = 0
        parent = os.path.dirname(os.path.abspath(dst_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dst_path, "wb") as out:
            for block in self.iter_file(src_path, chunk):
                out.write(block)
                written += len(block)
            out.flush()
            os.fsync(out.fileno())
        result = {"path": dst_path, "bytes": written, "expected": stat.size,
                  "match": written == stat.size}
        if verify:
            from .util import sha256_file

            result["sha256"] = sha256_file(dst_path)
        return result

    # -- hex ---------------------------------------------------------------- #
    def hexdump(self, path: str, offset: int = 0, length: int = 256,
                width: int = 16) -> str:
        from .hexview import hexdump

        data = self.read_range(path, offset, length)
        return hexdump(data, base=offset, width=width)

    # -- introspection ------------------------------------------------------ #
    def describe(self) -> str:
        name = META_TYPE_NAMES.get(self.fs_type, self.fs_type)
        return (f"{self.image_path} @ 0x{self.offset:x} "
                f"(fs_type={name}, tsk={pytsk3.TSK_VERSION_STR})")
