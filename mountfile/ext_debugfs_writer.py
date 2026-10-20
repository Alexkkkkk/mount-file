# SPDX-License-Identifier: MIT
"""Transactional, opt-in ext2/3/4 file writer backed by e2fsprogs debugfs.

The writer never asks debugfs to mutate the source image directly. It copies the
filesystem extent to a temporary file, applies the operation there, verifies a
file write by reading it back, then commits the extent to the image with a
rollback snapshot. The UI must still create a persistent image backup before
calling it.
"""

import hashlib
import os
import re
import shutil
import subprocess
import tempfile


class ExtWriteError(RuntimeError):
    """An ext write was rejected or failed before verified completion."""


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.+@=-]+$")
_CHUNK = 1024 * 1024


def normalize_image_path(path):
    """Normalize a restricted POSIX image path and reject command syntax."""
    if not isinstance(path, str):
        raise ExtWriteError("image path must be text")
    raw = path.replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." or not _SAFE_COMPONENT.match(part) for part in parts):
        raise ExtWriteError(
            "ext write accepts only simple ASCII path names without spaces or shell characters"
        )
    return "/" + "/".join(parts)


def _debugfs_quote(value):
    value = os.fspath(value)
    if any(char in value for char in ("\x00", "\r", "\n", '"')):
        raise ExtWriteError("unsafe path for debugfs command")
    return '"{}"'.format(value.replace("\\", "\\\\"))


class Ext234ImageWriter:
    """Write files to one ext filesystem inside a raw image.

    `offset` is the filesystem start byte offset in the raw image. The ext
    superblock's own block count bounds the copied extent; bytes after the
    filesystem (for example, following partitions) are never included.
    """

    def __init__(self, image_path, offset=0, debugfs="debugfs", volume_size=None):
        self.image_path = os.path.realpath(os.fspath(image_path))
        self.offset = int(offset)
        self.volume_size = int(volume_size) if volume_size is not None else None
        if self.offset < 0:
            raise ExtWriteError("negative filesystem offset")
        if self.volume_size is not None and self.volume_size <= 0:
            raise ExtWriteError("invalid selected partition size")
        self.debugfs = shutil.which(debugfs)
        if not self.debugfs:
            raise ExtWriteError("e2fsprogs debugfs is required for ext2/3/4 writes")
        self._read_superblock()

    def _read_superblock(self):
        with open(self.image_path, "rb") as image:
            image.seek(self.offset + 1024)
            sb = image.read(1024)
        if len(sb) != 1024 or sb[56:58] != b"\x53\xef":
            raise ExtWriteError("no ext2/3/4 superblock at the selected offset")
        log_block = int.from_bytes(sb[24:28], "little")
        if log_block > 6:
            raise ExtWriteError("unsupported ext block size")
        self.block_size = 1024 << log_block
        blocks = int.from_bytes(sb[4:8], "little")
        incompat = int.from_bytes(sb[96:100], "little")
        compat = int.from_bytes(sb[92:96], "little")
        if incompat & 0x80:  # EXT4_FEATURE_INCOMPAT_64BIT
            blocks |= int.from_bytes(sb[0x150:0x154], "little") << 32
        self.filesystem_size = blocks * self.block_size
        if incompat & 0xC0:
            self.fs_type = "ext4"
        elif compat & 0x4:
            self.fs_type = "ext3"
        else:
            self.fs_type = "ext2"
        if self.filesystem_size <= 0:
            raise ExtWriteError("invalid ext filesystem size")
        if self.volume_size is not None and self.filesystem_size > self.volume_size:
            raise ExtWriteError("ext filesystem extent exceeds selected partition bounds")
        if self.offset + self.filesystem_size > os.path.getsize(self.image_path):
            raise ExtWriteError("ext filesystem extent exceeds image bounds")

    def _run(self, image_path, command, write=False):
        args = [self.debugfs]
        if write:
            args.append("-w")
        args.extend(["-R", command, os.fspath(image_path)])
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        if result.returncode:
            raise ExtWriteError(f"debugfs failed: {output.strip()[-600:]}")
        return output

    def _copy_extent(self, source, destination):
        with open(source, "rb") as src, open(destination, "wb") as dst:
            src.seek(self.offset)
            remaining = self.filesystem_size
            while remaining:
                data = src.read(min(_CHUNK, remaining))
                if not data:
                    raise ExtWriteError("unexpected end of image while copying ext extent")
                dst.write(data)
                remaining -= len(data)
            dst.flush()
            os.fsync(dst.fileno())

    def _commit_extent(self, modified_path):
        """Commit a verified extent; restore original bytes if commit errors."""
        expected = self._hash_extent(modified_path, 0)
        with tempfile.TemporaryDirectory(prefix="mountfile-ext-rollback-") as work:
            rollback_path = os.path.join(work, "original.ext")
            self._copy_extent(self.image_path, rollback_path)
            try:
                with open(self.image_path, "r+b") as image, open(modified_path, "rb") as src:
                    image.seek(self.offset)
                    remaining = self.filesystem_size
                    while remaining:
                        block = src.read(min(_CHUNK, remaining))
                        if not block:
                            raise ExtWriteError("modified ext extent is truncated")
                        image.write(block)
                        remaining -= len(block)
                    image.flush()
                    os.fsync(image.fileno())
                    actual = self._hash_extent(image, self.offset)
                    if actual != expected:
                        raise ExtWriteError("committed ext extent failed SHA-256 verification")
            except Exception:
                try:
                    with open(self.image_path, "r+b") as image, open(
                        rollback_path, "rb"
                    ) as original:
                        image.seek(self.offset)
                        shutil.copyfileobj(original, image, _CHUNK)
                        image.flush()
                        os.fsync(image.fileno())
                except Exception as rollback_error:
                    raise ExtWriteError(
                        "commit failed and automatic rollback also failed; "
                        f"persistent image backup is required: {rollback_error}"
                    ) from rollback_error
                raise

    def _hash_extent(self, path_or_stream, offset):
        digest = hashlib.sha256()
        remaining = self.filesystem_size

        def update_from(stream):
            nonlocal remaining
            stream.seek(offset)
            while remaining:
                chunk = stream.read(min(_CHUNK, remaining))
                if not chunk:
                    raise ExtWriteError("unexpected end of ext extent during verification")
                digest.update(chunk)
                remaining -= len(chunk)

        if hasattr(path_or_stream, "read"):
            update_from(path_or_stream)
        else:
            with open(path_or_stream, "rb") as stream:
                update_from(stream)
        return digest.digest()

    def _with_working_extent(self, operation):
        with tempfile.TemporaryDirectory(prefix="mountfile-ext-work-") as work:
            working = os.path.join(work, "filesystem.ext")
            self._copy_extent(self.image_path, working)
            result = operation(working, work)
            self._commit_extent(working)
            return result

    def read_file(self, path):
        image_path = normalize_image_path(path)
        with tempfile.TemporaryDirectory(prefix="mountfile-ext-read-") as work:
            filesystem = os.path.join(work, "filesystem.ext")
            self._copy_extent(self.image_path, filesystem)
            output = os.path.join(work, "out.bin")
            self._run(
                filesystem,
                f"dump {_debugfs_quote(image_path)} {_debugfs_quote(output)}",
            )
            with open(output, "rb") as stream:
                return stream.read()

    def list_dir(self, path="/"):
        image_path = normalize_image_path(path)
        with tempfile.TemporaryDirectory(prefix="mountfile-ext-list-") as work:
            filesystem = os.path.join(work, "filesystem.ext")
            self._copy_extent(self.image_path, filesystem)
            output = self._run(
                filesystem, f"ls -l {_debugfs_quote(image_path)}"
            )
        entries = []
        for line in output.splitlines():
            parts = line.split(None, 8)
            if len(parts) < 9 or not parts[0].isdigit():
                continue
            try:
                mode = int(parts[1], 8)
                size = int(parts[5])
                name = parts[8].strip()
            except (ValueError, IndexError):
                continue
            if name in (".", ".."):
                continue
            entries.append(
                {
                    "name": name,
                    "is_dir": (mode & 0o170000) == 0o040000,
                    "size": size,
                    "date": "",
                }
            )
        entries.sort(key=lambda item: (not item["is_dir"], item["name"].lower()))
        return entries

    def write_file(self, path, data):
        image_path = normalize_image_path(path)
        if not isinstance(data, bytes):
            data = bytes(data)

        def apply(working, work):
            source = os.path.join(work, "input.bin")
            with open(source, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Remove-if-present is done only on the disposable working extent.
            self._run(
                working, f"rm {_debugfs_quote(image_path)}", write=True
            )
            output = self._run(
                working,
                f"write {_debugfs_quote(source)} {_debugfs_quote(image_path)}",
                write=True,
            )
            if "Allocated inode:" not in output:
                raise ExtWriteError("debugfs did not confirm file creation")
            check = os.path.join(work, "verify.bin")
            self._run(
                working,
                f"dump {_debugfs_quote(image_path)} {_debugfs_quote(check)}",
            )
            with open(check, "rb") as stream:
                if stream.read() != data:
                    raise ExtWriteError("read-back verification failed; image was not changed")

        self._with_working_extent(apply)

    def mkdir(self, path):
        image_path = normalize_image_path(path)

        def apply(working, _work):
            self._run(
                working, f"mkdir {_debugfs_quote(image_path)}", write=True
            )
        self._with_working_extent(apply)

    def rename(self, source, destination):
        src = normalize_image_path(source)
        dst = normalize_image_path(destination)
        def apply(working, _work):
            self._run(
                working,
                f"rename {_debugfs_quote(src)} {_debugfs_quote(dst)}",
                write=True,
            )
        self._with_working_extent(apply)


class Ext234Provider:
    """Minimal read provider backed by debugfs for systems without pytsk3."""

    def __init__(self, image_path, offset=0, volume_size=None):
        self.backend = Ext234ImageWriter(
            image_path, offset=offset, volume_size=volume_size
        )

    def list_dir(self, path="/"):
        return self.backend.list_dir(path)

    def read_file(self, path):
        return self.backend.read_file(path)

    def read_file_range(self, path, offset=0, length=1 << 20):
        data = self.read_file(path)
        return data[offset : offset + length]

    def close(self):
        return None
