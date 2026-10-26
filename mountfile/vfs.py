# MOUNTFILE_FIX — auto-patched + OLMA
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Read-only filesystem access via The Sleuth Kit (pytsk3).

Supports FAT12/16/32, exFAT, NTFS, EXT2/3/4, HFS+, ISO9660 and more
(depending on the libtsk build). Used for browsing and extracting files. The
pure-Python NTFS probe in this module validates boot-sector geometry only; it
does not replace the pytsk3 reader or enable NTFS writes.
"""

from __future__ import annotations

import time

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


# --- /MOUNTFILE_FIX --------------------------------------------------------
try:
    import pytsk3
except ImportError:  # pragma: no cover
    pytsk3 = None


class TskError(Exception):
    pass


def probe_ntfs_profile(image_path, offset=0, volume_size=None):
    """Validate NTFS boot-sector geometry without opening pytsk3 or mounting."""
    from mountfile.ntfs_probe import probe_ntfs_boot_sector

    return probe_ntfs_boot_sector(image_path, offset=offset, volume_size=volume_size)


def parse_ntfs_file_record_header(raw_record):
    """Validate one NTFS FILE record's header and USA, without reading a volume."""
    from mountfile.ntfs_mft import parse_ntfs_file_record_header as parse_record

    return parse_record(raw_record)


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
