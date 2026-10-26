# MOUNTFILE_FIX — auto-patched + OLMA
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Read-only filesystem providers for raw images.

``TskImage`` uses The Sleuth Kit (pytsk3) for supported formats. The separate
``NativeExt2Image`` provider reads a strict, clean ext2 subset in pure Python.
Legacy format coverage still depends on the installed libtsk build.

The module also exports a dependency-free ext superblock probe. The probe
reports format features and geometry only; it does not browse files or authorize
writes.
The pure-Python NTFS probe validates boot-sector geometry and FILE-record USA
fixups only; it does not replace the pytsk3 reader or enable NTFS writes.
"""

from __future__ import annotations

import os
import time

from mountfile.ext2_reader import Ext2ReadError, NativeExt2Image
from mountfile.ext_probe import ExtProbeError, probe_ext_superblock

# --- MOUNTFILE_FIX: OLMA safe read -----------------------------------------
try:
    from mountfile.olma.core import OlmaError, OlmaHandle
except Exception:
    OlmaHandle = None


def safe_read(fh, offset, size, *, chunk=1 << 20):
    if OlmaHandle is not None and isinstance(fh, OlmaHandle):
        return fh.read_upto(offset, size)
    if size <= 0 or offset < 0:
        return b""
    out = bytearray()
    fh.seek(offset)
    remaining = size
    while remaining > 0:
        part = fh.read(min(chunk, remaining))
        if not part:
            break
        out.extend(part)
        remaining -= len(part)
    return bytes(out)


def safe_read_exact(fh, offset, size):
    if OlmaHandle is not None and isinstance(fh, OlmaHandle):
        try:
            return fh.read_at(offset, size)
        except OlmaError:
            return None
    d = safe_read(fh, offset, size)
    return d if len(d) == size else None


def probe_ext_profile(image_path, offset=0, volume_size=None):
    """Inspect an ext superblock without requiring pytsk3.

    This reports a preliminary profile only. It does not browse files or
    authorize writes; the selected filesystem backend remains responsible for
    complete metadata validation.
    """
    return probe_ext_superblock(image_path, offset=offset, volume_size=volume_size)


# --- /MOUNTFILE_FIX --------------------------------------------------------
try:
    import pytsk3
except ImportError:  # pragma: no cover
    pytsk3 = None


class TskError(Exception):
    pass


def open_native_ext2(image_path, offset=0, volume_size=None):
    """Open the bounded pure-Python ext2 read-only provider.

    The caller must provide the selected partition size; the provider refuses
    to infer bounds from the remainder of a disk image.
    """
    return NativeExt2Image(image_path, offset=offset, volume_size=volume_size)


def volume_size_for_offset(image_path, offset):
    """Return an exact partition size, or whole-image size for an unpartitioned offset 0."""
    offset = int(offset)
    try:
        from mountfile.partition import list_partitions

        partitions = list_partitions(image_path)
    except Exception:
        return None
    for partition in partitions:
        if partition.offset == offset:
            return partition.size_bytes
    if offset == 0 and not partitions:
        try:
            return os.path.getsize(image_path)
        except OSError:
            return None
    return None


def probe_ntfs_profile(image_path, offset=0, volume_size=None):
    """Validate NTFS boot-sector geometry without opening pytsk3 or mounting."""
    from mountfile.ntfs_probe import probe_ntfs_boot_sector

    return probe_ntfs_boot_sector(image_path, offset=offset, volume_size=volume_size)


def parse_ntfs_file_record_header(raw_record):
    """Validate one NTFS FILE record's header and USA, without reading a volume."""
    from mountfile.ntfs_mft import parse_ntfs_file_record_header as parse_record

    return parse_record(raw_record)


def probe_ntfs_metadata(image_path, offset=0, volume_size=None):
    """Validate bounded NTFS boot, backup boot, MFT and attribute metadata."""
    from mountfile.ntfs_reader import probe_ntfs_metadata as probe_metadata

    return probe_metadata(image_path, offset=offset, volume_size=volume_size)


def probe_exfat_profile(image_path, offset=0, volume_size=None):
    """Validate a bounded exFAT boot region without mounting or writing."""
    from mountfile.exfat_probe import probe_exfat_boot_region

    return probe_exfat_boot_region(image_path, offset=offset, volume_size=volume_size)


def _fmt_mtime(meta):
    """Modification time of a TSK meta record as 'YYYY-MM-DD HH:MM'."""
    try:
        ts = int(meta.mtime)
        if ts <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


class TskImage:
    """Browsable view of one filesystem inside a raw image at `offset`."""

    def __init__(self, image_path, offset=0):
        if pytsk3 is None:
            raise TskError("pytsk3 is not installed. Run: pip install pytsk3")
        self.image_path = str(image_path)
        self.offset = int(offset)
        self.backend_name = "pytsk3"
        try:
            self._open_image()
        except Exception as e:
            raise TskError(str(e)) from e
        self.root_name = image_path

    def _open_image(self):
        self.img = pytsk3.Img_Info(self.image_path)
        self.fs = pytsk3.FS_Info(self.img, offset=self.offset)

    def close(self):
        """Release libtsk's image handle so a native OS writer can mount it."""
        fs, self.fs = getattr(self, "fs", None), None
        img, self.img = getattr(self, "img", None), None
        for handle in (fs, img):
            close = getattr(handle, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def reopen(self):
        """Reopen the image after a writer has detached its temporary mount."""
        if getattr(self, "fs", None) is None:
            self._open_image()

    def list_dir(self, path="/"):
        out = []
        try:
            d = self.fs.open_dir(path)
        except Exception as e:
            raise TskError("cannot open directory %r: %s" % (path, e)) from e
        for entry in d:
            name = entry.info.name.name
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            if name in (".", ".."):
                continue
            meta = entry.info.meta
            if meta is None:
                continue
            is_dir = meta.type == pytsk3.TSK_FS_META_TYPE_DIR
            out.append(
                {
                    "name": name,
                    "is_dir": is_dir,
                    "size": 0 if is_dir else int(meta.size),
                    "date": "" if is_dir else _fmt_mtime(meta),
                }
            )
        out.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return out

    def _open(self, path):
        try:
            return self.fs.open(path)
        except Exception as e:
            raise TskError("cannot open file %r: %s" % (path, e)) from e

    def file_size(self, path):
        return int(self._open(path).info.meta.size)

    def read_file_range(self, path, offset=0, length=1 << 20):
        f = self._open(path)
        size = int(f.info.meta.size)
        if offset >= size:
            return b""
        length = min(length, size - offset)
        chunks = []
        got = 0
        while got < length:
            data = f.read_random(offset + got, min(1 << 20, length - got))
            if not data:
                break
            chunks.append(data)
            got += len(data)
        return b"".join(chunks)

    def read_file(self, path):
        return self.read_file_range(path, 0, self.file_size(path))


def _has_ext_superblock_magic(image_path, offset, volume_size):
    if volume_size is None:
        return False
    try:
        offset = int(offset)
        volume_size = int(volume_size)
        if offset < 0 or volume_size < 0:
            raise TskError("negative filesystem offset or volume size")
        with open(image_path, "rb") as stream:
            image_size = os.fstat(stream.fileno()).st_size
            if offset + volume_size > image_size:
                raise TskError("selected filesystem extent exceeds image bounds")
            if volume_size < 2048:
                return False
            stream.seek(offset + 1024 + 0x38)
            return stream.read(2) == b"\x53\xef"
    except OSError as exc:
        raise TskError(f"cannot inspect image for native ext2: {exc}") from exc


def open_image_provider(image_path, offset=0, volume_size=None):
    """Open a read-only provider, preferring native ext2 when safely bounded.

    Clean ext2 profiles are offered to the pure-Python provider. Profiles not
    supported by that strict subset retain the existing pytsk3 read fallback;
    without an explicit volume size, native ext2 is never selected.
    """
    fallback_reason = None
    if _has_ext_superblock_magic(image_path, offset, volume_size):
        try:
            profile = probe_ext_superblock(
                image_path, offset=offset, volume_size=volume_size
            )
        except ExtProbeError as exc:
            raise TskError(f"invalid ext filesystem profile: {exc}") from exc
        if profile.fs_type == "ext2" and profile.clean_unmounted:
            try:
                return open_native_ext2(image_path, offset, volume_size)
            except Ext2ReadError as exc:
                fallback_reason = f"native ext2 reader declined the profile: {exc}"
        elif not profile.clean_unmounted or profile.journal_recovery_needed:
            fallback_reason = (
                "ext volume is dirty or requires recovery; using read-only fallback"
            )
        else:
            fallback_reason = "native ext3/ext4 reading is not implemented yet"

    try:
        provider = TskImage(image_path, offset=offset)
    except TskError as exc:
        if fallback_reason:
            raise TskError(f"{fallback_reason}; {exc}") from exc
        raise
    if fallback_reason:
        provider.backend_name = "pytsk3-fallback"
        provider.native_fallback_reason = fallback_reason
    return provider
