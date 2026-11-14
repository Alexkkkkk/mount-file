# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""In-process ext4 write path through libext2fs (issue #385, follow-up to #504).

PR #504 shells out to ``debugfs``.  This module adds the library path: the same
``libext2fs`` that ``mke2fs``/``e2fsck`` use is called through ``ctypes``, so the
mutation is validated by the library instead of by a command string.

What this module deliberately does **not** do
---------------------------------------------
It is not a JBD2 transaction writer, and it never "clears" a dirty journal.  The
shortcut from the design note -- writing ``s_start = 0`` and clearing
``EXT4_FEATURE_INCOMPAT_RECOVER`` -- *discards committed transactions* and leaves
the filesystem silently inconsistent.  Recovery is delegated to ``e2fsck -fy`` on
a disposable copy, and the copy is re-gated before anything is published.

Safety model (fail-closed)
--------------------------
1. The caller's image is never opened read-write; a byte copy is mutated instead.
2. The journal gate runs first.  ``RECOVER`` set, or a JBD2 head with
   ``s_start != 0``, means the copy is replayed through ``e2fsck -fy`` (default)
   or the write is refused (``recover="refuse"``).
3. After replay the copy must show a clear ``RECOVER`` bit, an empty JBD2 head and
   ``e2fsck -fn`` exit 0 before the native write starts.
4. After the native write the copy is flushed, re-checked with ``e2fsck -fn`` and
   re-gated; only then is it atomically renamed over the original.

Profiles outside the validated set (external journal, journal checksums,
``meta_bg``, unknown block sizes) are rejected before the first byte is written.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import struct
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional, Sequence

from mountfile.journal_safety import Ext4JournalManager, JournalState
from mountfile.native_write import (
    NativeWriteError,
    NativeWriteUnsafe,
    _atomic_replace,
    _fault,
    _image_copy,
    _require_enabled,
    _require_tools,
    _run,
    _unlink,
    native_write_enabled,
    validate_internal_path,
)

EXT2_ROOT_INO = 2

# Values taken from /usr/include/ext2fs/ext2fs.h (e2fsprogs 1.47).
# NOTE: 0x2000 is EXT2_FLAG_IMAGE_FILE and 0x4 is EXT2_FLAG_FORCE -- neither is
# part of a plain image open, and passing them makes ext2fs_open2 fail.
EXT2_FLAG_RW = 0x0001
EXT2_FLAG_64BITS = 0x20000
EXT2_FILE_WRITE = 0x0001

EXT4_JOURNAL_INO = 8
EXT4_FEATURE_COMPAT_HAS_JOURNAL = 0x0004
EXT4_FEATURE_INCOMPAT_RECOVER = 0x0004
EXT4_FEATURE_INCOMPAT_JOURNAL_DEV = 0x0008
EXT4_FEATURE_INCOMPAT_META_BG = 0x0010
EXT4_FEATURE_INCOMPAT_CSUM_V2 = 0x0800
EXT4_FEATURE_INCOMPAT_CSUM_V3 = 0x1000
EXT4_FEATURE_COMPAT_CSUM_SEED = 0x2000
EXT4_FEATURE_INCOMPAT_CSUM_SEED = 0x2000

SUPPORTED_BLOCK_SIZES = frozenset((1024, 2048, 4096))

_JBD2_MAGIC = 0xC03B3998
_JBD2_SUPERBLOCK_V1 = 3
_JBD2_SUPERBLOCK_V2 = 4


class LibExt2fsUnavailable(NativeWriteError):
    """The native library or one of the required symbols is missing."""


class Ext4ProfileUnsupported(NativeWriteUnsafe):
    """The image layout is outside the validated write profile."""


@dataclass(frozen=True)
class Ext4JournalProbe:
    """Read-only JBD2 probe result; nothing was written."""

    state: JournalState
    message: str
    block_size: int = 0
    journal_start: int = 0
    journal_sequence: int = 0

    @property
    def clean(self) -> bool:
        return self.state is JournalState.CLEAN


def _errname(lib: ctypes.CDLL, code: int) -> str:
    try:
        name = lib.ext2fs_error_name(code)
        if isinstance(name, bytes):
            return name.decode("ascii", "replace")
        return str(name)
    except Exception:  # pragma: no cover - diagnostics only
        return f"error {code}"


def _check(lib: LibExt2fs, ret: int, what: str) -> None:
    if ret:
        raise NativeWriteError(f"{what} failed: {_errname(lib.lib, ret)} ({ret})")


class LibExt2fs:
    """Argument-checked ``ctypes`` binding for the calls this writer uses.

    Two details are easy to get wrong and are handled explicitly here:

    * ``ext2fs_open2`` has no offset parameter -- the offset is passed through the
      ``io_options`` string (``"offset=<bytes>"``), exactly what ``debugfs -o
      offset=`` maps onto.  It also requires an :data:`io_manager`; ``NULL``
      segfaults inside the library, so ``unix_io_manager`` is always resolved.
    * ``ext2fs_file_open2`` takes an **inode number**, not a path, so the path is
      resolved with ``ext2fs_namei`` first (``cwd`` and ``root`` are both
      ``EXT2_ROOT_INO`` for an image-internal absolute path).
    """

    def __init__(self, library: Optional[str] = None) -> None:
        name = library or ctypes.util.find_library("ext2fs")
        if not name:
            raise LibExt2fsUnavailable(
                "libext2fs was not found; install e2fsprogs (libext2fs2)"
            )
        self.lib = ctypes.CDLL(name, use_errno=True)
        self._bind()
        self.io_manager = ctypes.c_void_p.in_dll(self.lib, "unix_io_manager").value

    def _bind(self) -> None:
        lib = self.lib
        required = (
            "ext2fs_open2",
            "ext2fs_namei",
            "ext2fs_file_open2",
            "ext2fs_file_read",
            "ext2fs_file_write",
            "ext2fs_file_set_size2",
            "ext2fs_file_close",
            "ext2fs_flush",
            "ext2fs_close",
        )
        missing = [symbol for symbol in required if not hasattr(lib, symbol)]
        if missing:
            raise LibExt2fsUnavailable(
                "libext2fs is missing symbols: " + ", ".join(missing)
            )

        lib.ext2fs_open2.argtypes = [
            ctypes.c_char_p,  # const char *name
            ctypes.c_char_p,  # const char *io_options
            ctypes.c_int,  # int flags
            ctypes.c_int,  # int superblock
            ctypes.c_uint,  # unsigned int block_size
            ctypes.c_void_p,  # io_manager manager
            ctypes.POINTER(ctypes.c_void_p),  # ext2_filsys *ret_fs
        ]
        lib.ext2fs_open2.restype = ctypes.c_int

        lib.ext2fs_namei.argtypes = [
            ctypes.c_void_p,  # ext2_filsys fs
            ctypes.c_uint,  # ext2_ino_t cwd
            ctypes.c_uint,  # ext2_ino_t root
            ctypes.c_char_p,  # const char *name
            ctypes.POINTER(ctypes.c_uint),  # ext2_ino_t *inode
        ]
        lib.ext2fs_namei.restype = ctypes.c_int

        lib.ext2fs_file_open2.argtypes = [
            ctypes.c_void_p,  # ext2_filsys fs
            ctypes.c_uint,  # ext2_ino_t ino
            ctypes.c_void_p,  # struct ext2_inode *inode
            ctypes.c_int,  # int flags
            ctypes.POINTER(ctypes.c_void_p),  # ext2_file_t *ret
        ]
        lib.ext2fs_file_open2.restype = ctypes.c_int

        for symbol in ("ext2fs_file_read", "ext2fs_file_write"):
            getattr(lib, symbol).argtypes = [
                ctypes.c_void_p,  # ext2_file_t file
                ctypes.c_void_p,  # void *buf
                ctypes.c_uint,  # unsigned int nbytes
                ctypes.POINTER(ctypes.c_uint),  # unsigned int *done
            ]
            getattr(lib, symbol).restype = ctypes.c_int

        lib.ext2fs_file_set_size2.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        lib.ext2fs_file_set_size2.restype = ctypes.c_int

        for symbol in ("ext2fs_file_close", "ext2fs_flush", "ext2fs_close"):
            getattr(lib, symbol).argtypes = [ctypes.c_void_p]
            getattr(lib, symbol).restype = ctypes.c_int

        if hasattr(lib, "ext2fs_error_name"):
            lib.ext2fs_error_name.argtypes = [ctypes.c_int]
            lib.ext2fs_error_name.restype = ctypes.c_char_p


_DEFAULT_LIB: Optional[LibExt2fs] = None


def libext2fs() -> LibExt2fs:
    """Return the process-wide binding, raising a typed error when unusable."""

    global _DEFAULT_LIB
    if _DEFAULT_LIB is None:
        _DEFAULT_LIB = LibExt2fs()
    return _DEFAULT_LIB


def libext2fs_available() -> bool:
    try:
        libext2fs()
    except (LibExt2fsUnavailable, OSError):
        return False
    return True


@contextmanager
def _open_fs(
    lib: LibExt2fs, image_path: str, offset: int, flags: int
) -> Iterator[ctypes.c_void_p]:
    """Open an image; a partition offset is expressed as an io option."""

    io_options = f"offset={offset}".encode() if offset else None
    fs = ctypes.c_void_p()
    ret = lib.lib.ext2fs_open2(
        os.fsencode(image_path),
        io_options,
        flags,
        0,
        0,
        lib.io_manager,
        ctypes.byref(fs),
    )
    _check(lib, ret, "ext2fs_open2")
    try:
        yield fs
    finally:
        lib.lib.ext2fs_close(fs)


def normalize_internal_path(path: str) -> str:
    """Return a traversal-free, image-relative path.

    ``validate_internal_path`` is the project's single authority on path safety;
    it rejects absolute paths, so the leading ``/`` a VFS caller naturally passes
    is stripped here *before* validation.  ``ext2fs_namei`` still receives the
    absolute form, because it resolves against the image root inode.
    """

    if not isinstance(path, str):
        raise NativeWriteUnsafe("filesystem path is not a valid text path")
    return validate_internal_path(path.lstrip("/"))


def _read_superblock(image_path: str, offset: int) -> bytes:
    if offset < 0:
        raise Ext4ProfileUnsupported("filesystem offset cannot be negative")
    try:
        image_size = os.path.getsize(image_path)
    except OSError as exc:
        raise Ext4ProfileUnsupported(f"cannot stat ext4 image: {exc}") from exc
    if offset + 1024 + 1024 > image_size:
        raise Ext4ProfileUnsupported("ext4 superblock is outside the image")
    with open(image_path, "rb") as handle:
        handle.seek(offset + 1024)
        superblock = handle.read(1024)
    if len(superblock) < 1024:
        raise Ext4ProfileUnsupported("ext4 superblock is truncated")
    return superblock


def require_supported_profile(image_path: str, offset: int = 0) -> dict:
    """Validate the on-disk layout before the library is allowed to write.

    Returns ``{"block_size": ..., "journal_inode": ...}`` for logging.  Every
    unknown or unsupported combination raises :class:`Ext4ProfileUnsupported`;
    there is no "best effort" branch.
    """

    superblock = _read_superblock(image_path, offset)
    if struct.unpack_from("<H", superblock, 0x38)[0] != 0xEF53:
        raise Ext4ProfileUnsupported("not an ext2/3/4 filesystem")
    compat = struct.unpack_from("<I", superblock, 0x5C)[0]
    incompat = struct.unpack_from("<I", superblock, 0x60)[0]
    block_size = 1024 << struct.unpack_from("<I", superblock, 0x18)[0]
    journal_inode = struct.unpack_from("<I", superblock, 0xE0)[0]

    if block_size not in SUPPORTED_BLOCK_SIZES:
        raise Ext4ProfileUnsupported(f"unsupported ext4 block size {block_size}")
    if incompat & EXT4_FEATURE_INCOMPAT_JOURNAL_DEV:
        raise Ext4ProfileUnsupported("external journal device is not supported")
    if incompat & EXT4_FEATURE_INCOMPAT_META_BG:
        raise Ext4ProfileUnsupported("meta_bg layout is not supported")
    if incompat & (EXT4_FEATURE_INCOMPAT_CSUM_V2 | EXT4_FEATURE_INCOMPAT_CSUM_V3):
        raise Ext4ProfileUnsupported(
            "journal/metadata checksum features are outside the validated profile"
        )
    if incompat & EXT4_FEATURE_INCOMPAT_CSUM_SEED:
        raise Ext4ProfileUnsupported("checksum seed is not supported")
    if not compat & EXT4_FEATURE_COMPAT_HAS_JOURNAL:
        raise Ext4ProfileUnsupported(
            "image has no internal journal; only journaled ext3/ext4 may be written"
        )
    if journal_inode != EXT4_JOURNAL_INO:
        raise Ext4ProfileUnsupported(
            f"unexpected journal inode {journal_inode} (expected {EXT4_JOURNAL_INO})"
        )
    return {"block_size": block_size, "journal_inode": journal_inode}


def _read_journal_head(image_path: str, offset: int) -> bytes:
    """Read the first bytes of the journal through the journal inode.

    The inode number comes from the superblock (``s_journal_inum``), so no path
    resolution is needed: ``ext2fs_file_open2`` takes an inode number and the
    ``<8>`` pseudo-path only works in ``debugfs``'s own namei mode.
    """

    lib = libext2fs()
    journal_inode = require_supported_profile(image_path, offset)["journal_inode"]
    handle = ctypes.c_void_p()
    with _open_fs(lib, image_path, offset, 0) as fs:
        _check(
            lib,
            lib.lib.ext2fs_file_open2(fs, journal_inode, None, 0, ctypes.byref(handle)),
            f"ext2fs_file_open2(journal inode {journal_inode})",
        )
        try:
            header = ctypes.create_string_buffer(64)
            read = ctypes.c_uint(0)
            _check(
                lib,
                lib.lib.ext2fs_file_read(handle, header, 64, ctypes.byref(read)),
                "ext2fs_file_read(journal)",
            )
        finally:
            lib.lib.ext2fs_file_close(handle)
    return header.raw[: read.value]


def probe_journal(image_path: str, offset: int = 0) -> Ext4JournalProbe:
    """Read-only gate: the ext4 ``RECOVER`` bit validated against the JBD2 head.

    ``RECOVER`` is authoritative for "replay required"; the journal superblock is
    parsed as well so a truncated, unknown-magic or out-of-ring journal is
    reported as :class:`JournalState.ERROR` instead of clean.  Nothing is written.
    """

    base = Ext4JournalManager(str(image_path), offset=offset).check_journal()
    if base.state is JournalState.ERROR:
        return Ext4JournalProbe(base.state, base.message)

    try:
        profile = require_supported_profile(image_path, offset)
        raw = _read_journal_head(image_path, offset)
    except (NativeWriteError, OSError) as exc:
        # A host without libext2fs still gets the conservative superblock answer.
        return Ext4JournalProbe(
            base.state, f"{base.message}; JBD2 head probe skipped: {exc}"
        )

    block_size = profile["block_size"]
    if len(raw) < 12:
        return Ext4JournalProbe(
            JournalState.ERROR, "JBD2 superblock is truncated", block_size
        )
    magic, blocktype = struct.unpack_from(">II", raw, 0)
    if magic != _JBD2_MAGIC or blocktype not in (
        _JBD2_SUPERBLOCK_V1,
        _JBD2_SUPERBLOCK_V2,
    ):
        return Ext4JournalProbe(
            JournalState.ERROR, "JBD2 superblock magic is invalid", block_size
        )
    if len(raw) < 0x20:
        return Ext4JournalProbe(
            JournalState.ERROR, "JBD2 superblock V2 header is truncated", block_size
        )
    start = struct.unpack_from(">I", raw, 0x18)[0]
    sequence = struct.unpack_from(">I", raw, 0x1C)[0]
    max_length = struct.unpack_from(">I", raw, 0x10)[0]
    first = struct.unpack_from(">I", raw, 0x14)[0]
    # ``RECOVER`` in the ext4 superblock is the authoritative "journal must be
    # replayed" marker.  ``s_start`` is NOT: a freshly made filesystem and a
    # cleanly unmounted one both keep a non-zero head (typically 1 or 2) inside
    # the ring.  Treating ``s_start != 0`` as dirty would refuse every healthy
    # image, so it is only reported, and a head outside the ring is corruption.
    if not max_length or first <= 0 or first >= max_length:
        return Ext4JournalProbe(
            JournalState.ERROR,
            f"invalid JBD2 ring geometry (first={first}, max_length={max_length})",
            block_size,
            start,
            sequence,
        )
    if start and not (first <= start < max_length):
        return Ext4JournalProbe(
            JournalState.ERROR,
            f"JBD2 head {start} is outside the ring [{first}, {max_length})",
            block_size,
            start,
            sequence,
        )
    if base.state is JournalState.NEEDS_REPLAY:
        return Ext4JournalProbe(
            JournalState.NEEDS_REPLAY,
            f"ext4 RECOVER flag is set; journal replay required "
            f"(head s_start={start}, s_sequence={sequence})",
            block_size,
            start,
            sequence,
        )
    return Ext4JournalProbe(
        JournalState.CLEAN,
        f"RECOVER flag is clear (journal head s_start={start}, s_sequence={sequence})",
        block_size,
        start,
        sequence,
    )


def _require_clean(image_path: str, offset: int) -> None:
    probe = probe_journal(image_path, offset)
    if probe.state is JournalState.ERROR:
        raise Ext4ProfileUnsupported(f"journal state is unknown: {probe.message}")
    if probe.state is JournalState.NEEDS_REPLAY:
        raise Ext4ProfileUnsupported(
            f"journal is still dirty after replay: {probe.message}"
        )


def _recover_flag_set(image_path: str, offset: int) -> bool:
    """Read the authoritative ``EXT4_FEATURE_INCOMPAT_RECOVER`` bit from disk."""

    superblock = _read_superblock(image_path, offset)
    incompat = struct.unpack_from("<I", superblock, 0x60)[0]
    return bool(incompat & EXT4_FEATURE_INCOMPAT_RECOVER)


def verify_image_after_operation(
    image_path: str, offset: int = 0, *, runner=_run
) -> dict:
    """Run the independent checker after **every** ext4 mutation, not only recovery.

    Issue #510, block B: ``e2fsck -f -n`` used to be attached to the recovery
    path, so an ordinary write was validated by the library's own read-back
    alone.  Three independent invariants are required here:

    * ``e2fsck -fn`` must exit 0 -- any error, warning or repair makes the
      operation fail closed before the copy is published;
    * the JBD2 probe must be :class:`~mountfile.journal_safety.JournalState.CLEAN`;
    * the ``RECOVER`` incompatibility bit must be clear in the on-disk
      superblock, read back without trusting either of the two above.

    Returns machine-readable evidence for the acceptance matrix.
    """

    result = runner(["e2fsck", "-fn", image_path], allowed={0})
    _require_clean(image_path, offset)
    if _recover_flag_set(image_path, offset):
        raise Ext4ProfileUnsupported(
            "ext4 RECOVER flag is still set after the operation"
        )
    probe = probe_journal(image_path, offset)
    return {
        "checker": "e2fsck -fn",
        "exit_code": getattr(result, "returncode", 0),
        "journal_state": probe.state.value,
        "recover_flag": False,
        "block_size": probe.block_size,
    }


def recover_journal_on_copy(image_path: str, offset: int, runner=_run) -> bool:
    """Replay JBD2 with ``e2fsck -fy`` on the disposable copy.

    This is the only supported recovery path: real journal records are applied by
    e2fsprogs itself.  Returns ``True`` when a replay actually ran.
    """

    require_supported_profile(image_path, offset)
    before = probe_journal(image_path, offset)
    if before.state is JournalState.ERROR:
        raise Ext4ProfileUnsupported(before.message)
    if before.clean:
        return False
    runner(["e2fsck", "-fy", image_path], allowed={0, 1, 2, 3})
    # A replay that leaves the copy dirty, uncorrectable or structurally broken
    # (a torn commit record, a bad descriptor, an out-of-ring head) must never be
    # treated as recovery success: the checker is re-run with repair disabled.
    verify_image_after_operation(image_path, offset, runner=runner)
    return True


def ext4_write_file_native(
    image_path: str,
    internal_path: str,
    data: bytes,
    *,
    offset: int = 0,
    library: Optional[LibExt2fs] = None,
) -> int:
    """Overwrite an **existing** file inside a clean ext4 image via libext2fs.

    Journal handling, copy/verify/publication are the caller's responsibility (see
    :class:`Ext4NativeWriter`).  ``ext2fs_namei`` must resolve the path first, so a
    missing path is an error instead of an implicit create.  Returns the number of
    bytes written.
    """

    lib = library or libext2fs()
    require_supported_profile(image_path, offset)
    internal = normalize_internal_path(internal_path)
    if not isinstance(data, (bytes, bytearray)):
        raise NativeWriteUnsafe("ext4 native write requires bytes")
    payload = bytes(data)
    if len(payload) > 0xFFFFFFFF:
        raise NativeWriteUnsafe("payload exceeds the bounded write size")

    _fault("mutation:start")
    with _open_fs(lib, image_path, offset, EXT2_FLAG_RW | EXT2_FLAG_64BITS) as fs:
        inode_number = ctypes.c_uint(0)
        _check(
            lib,
            lib.lib.ext2fs_namei(
                fs,
                EXT2_ROOT_INO,
                EXT2_ROOT_INO,
                os.fsencode("/" + internal),
                ctypes.byref(inode_number),
            ),
            f"ext2fs_namei({internal})",
        )
        handle = ctypes.c_void_p()
        _check(
            lib,
            lib.lib.ext2fs_file_open2(
                fs, inode_number.value, None, EXT2_FILE_WRITE, ctypes.byref(handle)
            ),
            f"ext2fs_file_open2({internal})",
        )
        try:
            _check(
                lib,
                lib.lib.ext2fs_file_set_size2(handle, len(payload)),
                "ext2fs_file_set_size2",
            )
            if payload:
                buffer = ctypes.create_string_buffer(payload, len(payload))
                written = ctypes.c_uint(0)
                _check(
                    lib,
                    lib.lib.ext2fs_file_write(
                        handle, buffer, len(payload), ctypes.byref(written)
                    ),
                    "ext2fs_file_write",
                )
                if written.value != len(payload):
                    raise NativeWriteError(
                        f"short ext4 write: {written.value}/{len(payload)} bytes"
                    )
        finally:
            _check(lib, lib.lib.ext2fs_file_close(handle), "ext2fs_file_close")
        if payload:
            read_handle = ctypes.c_void_p()
            _check(
                lib,
                lib.lib.ext2fs_file_open2(
                    fs,
                    inode_number.value,
                    None,
                    0,
                    ctypes.byref(read_handle),
                ),
                f"ext2fs_file_open2(readback {internal})",
            )
            try:
                read_buffer = ctypes.create_string_buffer(len(payload))
                read_count = ctypes.c_uint(0)
                _check(
                    lib,
                    lib.lib.ext2fs_file_read(
                        read_handle,
                        read_buffer,
                        len(payload),
                        ctypes.byref(read_count),
                    ),
                    f"ext2fs_file_read(readback {internal})",
                )
                if read_count.value != len(payload) or read_buffer.raw != payload:
                    raise NativeWriteError(
                        f"ext4 semantic read-back mismatch for {internal}"
                    )
            finally:
                _check(
                    lib,
                    lib.lib.ext2fs_file_close(read_handle),
                    "ext2fs_file_close(readback)",
                )
        # ext2fs_flush() is what makes the bitmap/inode changes durable.
        _fault("mutation:flush")
        _check(lib, lib.lib.ext2fs_flush(fs), "ext2fs_flush")
    _fault("mutation:done")
    return len(payload)


class Ext4NativeWriter:
    """Copy -> gate -> libext2fs write -> verify -> atomic publish."""

    def __init__(self, *, require_flag: bool = True, recover: str = "copy") -> None:
        if recover not in ("copy", "refuse"):
            raise ValueError("recover must be 'copy' or 'refuse'")
        self.require_flag = require_flag
        self.recover = recover
        self.last_verification: dict | None = None

    def write_file(
        self, image_path: str, path: str, data: bytes, offset: int = 0
    ) -> int:
        if self.require_flag:
            _require_enabled()
        if offset:
            raise NativeWriteUnsafe(
                "nonzero offsets are not supported by the bounded ext4 profile"
            )
        _require_tools("e2fsck")
        internal = normalize_internal_path(path)
        source = os.path.abspath(os.fspath(image_path))
        source_path, temporary = _image_copy(source)
        try:
            require_supported_profile(temporary, 0)
            probe = probe_journal(temporary, 0)
            if probe.state is JournalState.ERROR:
                raise Ext4ProfileUnsupported(
                    f"journal state is unknown: {probe.message}"
                )
            if probe.state is JournalState.NEEDS_REPLAY:
                if self.recover == "refuse":
                    raise Ext4ProfileUnsupported(
                        "journal needs replay; refusing to write a dirty filesystem"
                    )
                recover_journal_on_copy(temporary, 0)
            # A clean journal is a precondition, not a formality: the library below
            # writes metadata outside any JBD2 transaction.
            _require_clean(temporary, 0)
            _run(["e2fsck", "-fn", temporary], allowed={0})
            written = ext4_write_file_native(temporary, internal, data)
            self.last_verification = verify_image_after_operation(temporary, 0)
            _atomic_replace(source_path, temporary)
            temporary = ""
            return written
        finally:
            if temporary:
                _unlink(temporary)

    def create_file(
        self, image_path: str, path: str, data: bytes = b"", offset: int = 0
    ) -> int:
        """Create a regular file through the guarded ext4 write path.

        The in-process libext2fs profile intentionally supports overwriting an
        existing inode only.  Creation requires directory-entry allocation;
        delegate that operation to the project's already validated debugfs
        transactional writer so journal recovery and atomic publication remain
        identical to the other native paths.

        The delegated writer checks its own copy, but the published result is
        verified here as well: every writer operation, whichever backend produced
        it, ends with ``e2fsck -fn`` plus a clean journal gate on the caller's
        image (issue #510, block B).
        """

        from mountfile.native_write import Ext4TransactionalWriter

        if self.require_flag:
            _require_enabled()
        _require_tools("e2fsck")
        source = os.path.abspath(os.fspath(image_path))
        Ext4TransactionalWriter().create_file(image_path, path, data, offset)
        self.last_verification = verify_image_after_operation(source, 0)
        return len(data)


def journal_gate_summary(image_path: str, offset: int = 0) -> dict:
    """Machine-readable gate result used by the VFS adapter and the CLI."""

    probe = probe_journal(image_path, offset)
    enabled = native_write_enabled()
    return {
        "journal_state": probe.state.value,
        "journal_message": probe.message,
        "block_size": probe.block_size,
        "journal_start": probe.journal_start,
        "journal_sequence": probe.journal_sequence,
        "libext2fs": libext2fs_available(),
        "native_write_enabled": enabled,
        "post_operation_checker": "e2fsck -fn",
        "writable": bool(enabled and probe.clean),
    }


def _validate_paths(paths: Sequence[str]) -> list[str]:
    return [validate_internal_path(path) for path in paths]


__all__ = [
    "EXT2_FLAG_64BITS",
    "EXT2_FLAG_RW",
    "EXT2_ROOT_INO",
    "Ext4JournalProbe",
    "Ext4NativeWriter",
    "Ext4ProfileUnsupported",
    "LibExt2fs",
    "LibExt2fsUnavailable",
    "ext4_write_file_native",
    "journal_gate_summary",
    "libext2fs",
    "libext2fs_available",
    "normalize_internal_path",
    "probe_journal",
    "recover_journal_on_copy",
    "require_supported_profile",
    "verify_image_after_operation",
]
