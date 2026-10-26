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
import sys
import tempfile


class ExtWriteError(RuntimeError):
    """An ext write was rejected or failed before verified completion."""


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.+@=-]+$")
_CHUNK = 1024 * 1024
DEDICATED_WSL_DISTRIBUTION = "MountFileTools"


def normalize_image_path(path, allow_root=False):
    """Normalize a restricted POSIX image path and reject command syntax.

    ``allow_root`` lets the read-only helpers address the filesystem root; a
    write to ``/`` is always rejected.
    """
    if not isinstance(path, str):
        raise ExtWriteError("image path must be text")
    raw = path.replace("\\", "/")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts and allow_root:
        return "/"
    if not parts or any(
        part == ".." or not _SAFE_COMPONENT.match(part) for part in parts
    ):
        raise ExtWriteError(
            "ext write accepts only simple ASCII path names without spaces or shell characters"
        )
    return "/" + "/".join(parts)


def _debugfs_quote(value):
    value = os.fspath(value)
    if any(char in value for char in ("\x00", "\r", "\n", '"')):
        raise ExtWriteError("unsafe path for debugfs command")
    return '"{}"'.format(value.replace("\\", "\\\\"))


def _find_wsl_executable():
    """Find WSL, preferring Sysnative from a 32-bit Python on 64-bit Windows."""
    root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    wow64 = sys.maxsize <= 2**32 and bool(
        os.environ.get("PROCESSOR_ARCHITEW6432")
        or os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "AMD64"
    )
    if root and wow64:
        candidate = os.path.join(root, "Sysnative", "wsl.exe")
        if os.path.isfile(candidate):
            return candidate
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if wsl:
        return wsl
    if root:
        candidate = os.path.join(root, "System32", "wsl.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def _find_debugfs(debugfs="debugfs", windows=None, distribution=None):
    """Find native debugfs, Homebrew's keg-only copy, or WSL's copy.

    ``distribution`` is optional to preserve the default WSL behavior and its
    command shape for existing callers.  When supplied, discovery runs only in
    that named distribution.
    """
    executable = shutil.which(debugfs)
    if executable:
        return executable, None
    if sys.platform == "darwin":
        for candidate in (
            "/opt/homebrew/opt/e2fsprogs/sbin/debugfs",
            "/usr/local/opt/e2fsprogs/sbin/debugfs",
        ):
            if os.path.isfile(candidate):
                return candidate, None
    if windows is None:
        windows = sys.platform.startswith("win")
    if not windows:
        return None, None
    wsl = _find_wsl_executable()
    if not wsl:
        return None, None
    args = [wsl]
    if distribution:
        args.extend(["-d", distribution])
    args.extend(["--exec", "sh", "-lc", "command -v debugfs"])
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    executable = (result.stdout or "").strip().splitlines()
    if result.returncode or not executable or not executable[-1].startswith("/"):
        return None, None
    return executable[-1], wsl


class Ext234ImageWriter:
    """Write files to one ext filesystem inside a raw image.

    `offset` is the filesystem start byte offset in the raw image. The ext
    superblock's own block count bounds the copied extent; bytes after the
    filesystem (for example, following partitions) are never included. A
    conservative clean ext2 feature subset is handled by MountFile's native
    writer; other ext variants retain the legacy debugfs-backed path.
    """

    def __init__(self, image_path, offset=0, debugfs="debugfs", volume_size=None):
        self.image_path = os.path.realpath(os.fspath(image_path))
        self.offset = int(offset)
        self.volume_size = int(volume_size) if volume_size is not None else None
        if self.offset < 0:
            raise ExtWriteError("negative filesystem offset")
        if self.volume_size is not None and self.volume_size <= 0:
            raise ExtWriteError("invalid selected partition size")
        self.wsl_executable = None
        self.wsl_distribution = None
        self._wsl_path_cache = {}
        self.native_backend = None
        self.native_rejection = ""
        self.native_unsafe_error = ""
        if self._try_native_ext2():
            return
        self.debugfs, self.wsl_executable = _find_debugfs(debugfs)
        if not self.debugfs and sys.platform.startswith("win"):
            self.debugfs, self.wsl_executable = _find_debugfs(
                debugfs, distribution=DEDICATED_WSL_DISTRIBUTION
            )
            if self.debugfs:
                self.wsl_distribution = DEDICATED_WSL_DISTRIBUTION
        if not self.debugfs:
            native_detail = (
                " Native ext2 profile rejected: " + self.native_rejection
                if self.native_rejection
                else ""
            )
            raise ExtWriteError(
                "e2fsprogs debugfs is required for ext2/3/4 writes; on Windows, "
                "use Configuration > Install components, or add debugfs.exe to PATH. "
                "On Linux/macOS run python3 install_mountfile.py." + native_detail
            )
        self._read_superblock()
        if self.native_unsafe_error:
            self.write_strategy = "read-only-unsafe"

    def _try_native_ext2(self):
        """Use the in-process backend only for a positively identified ext2."""
        from .ext2_native import NativeExt2ImageWriter, NativeExt2UnsafeError

        try:
            with open(self.image_path, "rb") as image:
                image.seek(self.offset + 1024)
                sb = image.read(1024)
            if len(sb) != 1024 or sb[56:58] != b"\x53\xef":
                return False
            compat = int.from_bytes(sb[92:96], "little")
            incompat = int.from_bytes(sb[96:100], "little")
            if compat & 0x4 or incompat & 0xC0:
                return False
            self.native_backend = NativeExt2ImageWriter(
                self.image_path, offset=self.offset, volume_size=self.volume_size
            )
        except NativeExt2UnsafeError as exc:
            self.native_backend = None
            self.native_rejection = str(exc)
            self.native_unsafe_error = str(exc)
            return False
        except Exception as exc:
            self.native_backend = None
            self.native_rejection = str(exc)
            return False
        self.fs_type = "ext2"
        self.write_strategy = "native-ext2"
        self.filesystem_size = self.native_backend.filesystem_size
        self.block_size = self.native_backend.block_size
        self.free_blocks = self.native_backend._fs.free_blocks
        self.debugfs = None
        self.wsl_executable = None
        return True

    @property
    def writable(self):
        return not bool(self.native_unsafe_error)

    def _require_safe_write(self):
        if self.native_unsafe_error:
            raise ExtWriteError(
                "native ext2 safety gate refused mutation: " + self.native_unsafe_error
            )

    def _wsl_exec_args(self, *command):
        """Build a WSL command pinned to the selected distribution, if any."""
        args = [self.wsl_executable]
        distribution = getattr(self, "wsl_distribution", None)
        if distribution:
            args.extend(["-d", distribution])
        args.extend(["--exec", *command])
        return args

    def _wsl_path(self, path):
        """Translate a host path to the active WSL distribution's path space."""
        host_path = os.path.abspath(os.fspath(path))
        cached = self._wsl_path_cache.get(host_path)
        if cached is not None:
            return cached
        result = subprocess.run(
            self._wsl_exec_args("wslpath", "-a", host_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        converted = (result.stdout or "").strip()
        if result.returncode or not converted.startswith("/"):
            detail = (result.stderr or result.stdout or "no path returned").strip()
            raise ExtWriteError("WSL could not translate a temporary path: %s" % detail)
        self._wsl_path_cache[host_path] = converted
        return converted

    def _host_debugfs_quote(self, path):
        if self.wsl_executable:
            return _debugfs_quote(self._wsl_path(path))
        return _debugfs_quote(path)

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
        self.free_blocks = int.from_bytes(sb[12:16], "little")
        if incompat & 0x80:  # EXT4_FEATURE_INCOMPAT_64BIT
            blocks |= int.from_bytes(sb[0x150:0x154], "little") << 32
            self.free_blocks |= int.from_bytes(sb[0x158:0x15C], "little") << 32
        self.filesystem_size = blocks * self.block_size
        if self.free_blocks > blocks:
            self.free_blocks = 0
        if incompat & 0xC0:
            self.fs_type = "ext4"
        elif compat & 0x4:
            self.fs_type = "ext3"
        else:
            self.fs_type = "ext2"
        self.write_strategy = "debugfs"
        if self.filesystem_size <= 0:
            raise ExtWriteError("invalid ext filesystem size")
        if self.volume_size is not None and self.filesystem_size > self.volume_size:
            raise ExtWriteError(
                "ext filesystem extent exceeds selected partition bounds"
            )
        if self.offset + self.filesystem_size > os.path.getsize(self.image_path):
            raise ExtWriteError("ext filesystem extent exceeds image bounds")

    def _run(self, image_path, command, write=False):
        if self.wsl_executable:
            args = self._wsl_exec_args(self.debugfs)
            image_path = self._wsl_path(image_path)
        else:
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
                    raise ExtWriteError(
                        "unexpected end of image while copying ext extent"
                    )
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
                with (
                    open(self.image_path, "r+b") as image,
                    open(modified_path, "rb") as src,
                ):
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
                        raise ExtWriteError(
                            "committed ext extent failed SHA-256 verification"
                        )
            except Exception:
                try:
                    with (
                        open(self.image_path, "r+b") as image,
                        open(rollback_path, "rb") as original,
                    ):
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
                    raise ExtWriteError(
                        "unexpected end of ext extent during verification"
                    )
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
        if self.native_backend is not None:
            return self.native_backend.read_file(path)
        image_path = normalize_image_path(path, allow_root=True)
        with tempfile.TemporaryDirectory(prefix="mountfile-ext-read-") as work:
            filesystem = os.path.join(work, "filesystem.ext")
            self._copy_extent(self.image_path, filesystem)
            output = os.path.join(work, "out.bin")
            self._run(
                filesystem,
                f"dump {_debugfs_quote(image_path)} {self._host_debugfs_quote(output)}",
            )
            with open(output, "rb") as stream:
                return stream.read()

    def list_dir(self, path="/"):
        if self.native_backend is not None:
            return self.native_backend.list_dir(path)
        image_path = normalize_image_path(path, allow_root=True)
        with tempfile.TemporaryDirectory(prefix="mountfile-ext-list-") as work:
            filesystem = os.path.join(work, "filesystem.ext")
            self._copy_extent(self.image_path, filesystem)
            output = self._run(filesystem, f"ls -l {_debugfs_quote(image_path)}")
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
        if self.native_backend is not None:
            return self.native_backend.write_file(path, data)
        self._require_safe_write()
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
            self._run(working, f"rm {_debugfs_quote(image_path)}", write=True)
            output = self._run(
                working,
                f"write {self._host_debugfs_quote(source)} {_debugfs_quote(image_path)}",
                write=True,
            )
            if "Allocated inode:" not in output:
                raise ExtWriteError("debugfs did not confirm file creation")
            check = os.path.join(work, "verify.bin")
            self._run(
                working,
                f"dump {_debugfs_quote(image_path)} {self._host_debugfs_quote(check)}",
            )
            with open(check, "rb") as stream:
                if stream.read() != data:
                    raise ExtWriteError(
                        "read-back verification failed; image was not changed"
                    )

        self._with_working_extent(apply)

    def mkdir(self, path):
        if self.native_backend is not None:
            return self.native_backend.mkdir(path)
        self._require_safe_write()
        image_path = normalize_image_path(path)

        def apply(working, _work):
            self._run(working, f"mkdir {_debugfs_quote(image_path)}", write=True)

        self._with_working_extent(apply)

    def rename(self, source, destination):
        if self.native_backend is not None:
            return self.native_backend.rename(source, destination)
        self._require_safe_write()
        src = normalize_image_path(source)
        dst = normalize_image_path(destination)

        def apply(working, _work):
            self._run(
                working,
                f"rename {_debugfs_quote(src)} {_debugfs_quote(dst)}",
                write=True,
            )

        self._with_working_extent(apply)

    def delete(self, path, recursive=False):
        if self.native_backend is not None:
            return self.native_backend.delete(path, recursive=recursive)
        self._require_safe_write()
        image_path = normalize_image_path(path, allow_root=True)
        if image_path == "/":
            raise ExtWriteError("refusing to delete the filesystem root")
        entries = (
            {entry["name"]: entry for entry in self.list_dir(image_path)}
            if self._is_dir(image_path)
            else {}
        )
        if entries and not recursive:
            raise ExtWriteError(
                "directory is not empty; recursive delete not confirmed"
            )

        def remove_dir(working, target):
            for name, entry in sorted(entries.items()):
                child = target.rstrip("/") + "/" + name
                if entry["is_dir"]:
                    nested = {item["name"]: item for item in self.list_dir(child)}
                    if nested:
                        raise ExtWriteError(
                            "nested directory delete is not supported by this writer"
                        )
                    self._run(working, f"rmdir {_debugfs_quote(child)}", write=True)
                else:
                    self._run(working, f"rm {_debugfs_quote(child)}", write=True)
            self._run(working, f"rmdir {_debugfs_quote(target)}", write=True)

        def apply(working, _work):
            if self._is_dir(image_path):
                remove_dir(working, image_path)
            else:
                self._run(working, f"rm {_debugfs_quote(image_path)}", write=True)

        self._with_working_extent(apply)

    def _is_dir(self, path):
        """True when `path` is an existing directory in the image."""
        parent = path.rsplit("/", 1)[0] or "/"
        name = path.rsplit("/", 1)[-1]
        return any(
            entry["name"] == name and entry["is_dir"] for entry in self.list_dir(parent)
        )

    def free_space(self):
        """Free bytes reported by the superblock, or None when unknown."""
        if self.native_backend is not None:
            return self.native_backend.free_space()
        if not self.free_blocks:
            return None
        return self.free_blocks * self.block_size

    def close(self):
        """The debugfs writer keeps no handle open between operations."""
        if self.native_backend is not None:
            return self.native_backend.close()
        return None


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
