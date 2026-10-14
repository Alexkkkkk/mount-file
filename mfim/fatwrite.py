"""Optional write backend for FAT12/16/32 implemented through ``mtools``.

This is deliberately a *separate, opt-in* backend:

* it writes only FAT filesystems (mcopy/mmd/mdel/mren), so it can never be
  silently used on an ext4/NTFS/squashfs partition;
* it operates on the raw partition, so the caller must pass the correct
  ``offset`` — the backend verifies the FAT boot signature at that offset
  before running any write command, and refuses otherwise;
* ``mtools`` >= 4.0.19 supports ``offset=`` inside a drive definition, which
  lets us address a partition inside a whole-disk image without mounting.
  If the installed mtools does not support it, the backend transparently
  falls back to extract-modify-merge on a temporary partition copy.

Everything is executed through ``subprocess`` with an explicit argument list
(no shell), and a temporary MTOOLSRC so the user's own mtools config is never
touched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

from .partition import SECTOR_SIZE


class FatWriteError(Exception):
    pass


def mtools_available() -> bool:
    return all(shutil.which(t) for t in ("mcopy", "mdir", "mmd"))


def mtools_version() -> str:
    exe = shutil.which("mdir")
    if not exe:
        return "not installed"
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20)
        first = (out.stdout or out.stderr).strip().splitlines()
        return first[0] if first else "unknown"
    except Exception as exc:  # pragma: no cover
        return f"unknown ({exc})"


class FatWriteBackend:
    """Write access to a FAT partition inside a raw image."""

    DRIVE = "z"

    def __init__(self, image_path: str, offset: int = 0, drive: str = DRIVE):
        if not mtools_available():
            raise FatWriteError(
                "mtools not found (install dosfstools + mtools on Linux, or mtools for Windows)"
            )
        self.image_path = os.path.abspath(image_path)
        self.offset = int(offset)
        self.drive = drive.rstrip(":").lower()
        self._config_path: Optional[str] = None
        self._extract_dir: Optional[str] = None
        self._work_image: Optional[str] = None
        self.mode = "offset"

        self._verify_fat_boot_sector()
        self._probe_offset_support()

    # -- plumbing ----------------------------------------------------------- #
    def _verify_fat_boot_sector(self) -> None:
        with open(self.image_path, "rb") as fp:
            fp.seek(self.offset)
            boot = fp.read(512)
        if len(boot) < 512 or boot[510:512] != b"\x55\xaa":
            raise FatWriteError(
                f"no FAT boot signature at offset {self.offset} — refusing to write"
            )
        is_fat32 = boot[0x52:0x5A].startswith(b"FAT32")
        is_fat1216 = boot[0x36:0x3E].startswith(b"FAT1")
        if not (is_fat32 or is_fat1216):
            raise FatWriteError(
                f"offset {self.offset} does not look like FAT (no FAT12/16/32 label) "
                "— refusing to write"
            )
        self.fat_flavour = "fat32" if is_fat32 else "fat12/16"

    def _write_config(self, image_for_mtools: str, with_offset: int) -> str:
        fd, path = tempfile.mkstemp(prefix="mfim_mtools_", suffix=".rc")
        with os.fdopen(fd, "w") as fp:
            line = f'drive {self.drive}: file="{image_for_mtools}"'
            if with_offset:
                line += f" offset={with_offset}"
            fp.write(line + "\n")
        self._config_path = path
        return path

    def _probe_offset_support(self) -> None:
        """Decide whether this mtools understands ``offset=`` in a drive line."""
        cfg = self._write_config(self.image_path, self.offset)
        code, _, _ = self._run(["mdir", f"{self.drive}:/"], check=False, config=cfg)
        if code == 0:
            self.mode = "offset"
            return
        os.unlink(cfg)
        self._config_path = None
        self.mode = "extract-merge"

    def _prepare_work_image(self) -> str:
        """Extract-merge fallback: work on a private copy of the partition."""
        if self._work_image and os.path.exists(self._work_image):
            return self._work_image
        self._extract_dir = tempfile.mkdtemp(prefix="mfim_part_")
        self._work_image = os.path.join(self._extract_dir, "partition.bin")
        size = self._partition_size()
        with open(self.image_path, "rb") as src, open(self._work_image, "wb") as dst:
            src.seek(self.offset)
            remaining = size
            while remaining > 0:
                block = src.read(min(1 << 20, remaining))
                if not block:
                    break
                dst.write(block)
                remaining -= len(block)
        return self._work_image

    def _partition_size(self) -> int:
        # partition size = distance to the end of the image (mtools does not need
        # more precision than that to parse the FAT itself)
        return os.path.getsize(self.image_path) - self.offset

    def _run(self, args: List[str], check: bool = True,
             config: Optional[str] = None) -> Tuple[int, str, str]:
        env = os.environ.copy()
        cfg = config or self._config_path
        if cfg:
            env["MTOOLSRC"] = cfg
        env.setdefault("MTOOLS_SKIP_CHECK", "1")
        try:
            proc = subprocess.run(args, capture_output=True, text=True, env=env, timeout=300)
        except FileNotFoundError as exc:
            raise FatWriteError(f"{args[0]} not found: {exc}") from exc
        if check and proc.returncode != 0:
            raise FatWriteError(
                f"{args[0]} failed ({proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            )
        return proc.returncode, proc.stdout, proc.stderr

    def _target_config(self) -> str:
        if self.mode == "offset":
            return self._write_config(self.image_path, self.offset)
        return self._write_config(self._prepare_work_image(), 0)

    def _merge_back_if_needed(self) -> None:
        if self.mode != "extract-merge":
            return
        src = self._work_image
        if not src:
            return
        with open(src, "rb") as s, open(self.image_path, "r+b") as d:
            d.seek(self.offset)
            while True:
                block = s.read(1 << 20)
                if not block:
                    break
                d.write(block)
            d.flush()
            os.fsync(d.fileno())

    # -- read helpers ------------------------------------------------------- #
    def listdir(self, path: str = "/") -> str:
        cfg = self._target_config()
        _, out, err = self._run(["mdir", f"{self.drive}:/{path.lstrip('/')}"], config=cfg)
        return out or err

    def read_file_bytes(self, path: str) -> bytes:
        cfg = self._target_config()
        with tempfile.TemporaryDirectory(prefix="mfim_get_") as tmp:
            local = os.path.join(tmp, "out.bin")
            self._run(["mcopy", f"{self.drive}:/{path.lstrip('/')}", local], config=cfg)
            with open(local, "rb") as fp:
                return fp.read()

    # -- write operations --------------------------------------------------- #
    def copy_in(self, local_path: str, dest_path: str) -> None:
        cfg = self._target_config()
        self._run(["mcopy", "-o", local_path, f"{self.drive}:/{dest_path.lstrip('/')}"],
                  config=cfg)
        self._merge_back_if_needed()

    def copy_tree_in(self, local_dir: str, dest_dir: str = "/") -> None:
        cfg = self._target_config()
        self._run(["mcopy", "-s", "-o", local_dir, f"{self.drive}:/{dest_dir.lstrip('/')}"],
                  config=cfg)
        self._merge_back_if_needed()

    def mkdir(self, path: str) -> None:
        cfg = self._target_config()
        self._run(["mmd", f"{self.drive}:/{path.lstrip('/')}"], config=cfg)
        self._merge_back_if_needed()

    def remove(self, path: str) -> None:
        cfg = self._target_config()
        self._run(["mdel", f"{self.drive}:/{path.lstrip('/')}"], config=cfg)
        self._merge_back_if_needed()

    def rename(self, src: str, dst: str) -> None:
        cfg = self._target_config()
        self._run(["mren", f"{self.drive}:/{src.lstrip('/')}",
                   f"{self.drive}:/{dst.lstrip('/')}"], config=cfg)
        self._merge_back_if_needed()

    def write_bytes(self, data: bytes, dest_path: str) -> None:
        with tempfile.TemporaryDirectory(prefix="mfim_put_") as tmp:
            local = os.path.join(tmp, os.path.basename(dest_path) or "blob.bin")
            with open(local, "wb") as fp:
                fp.write(data)
            self.copy_in(local, dest_path)

    # -- lifecycle ---------------------------------------------------------- #
    def describe(self) -> str:
        return (f"FAT write backend: mtools {mtools_version()} | flavour {self.fat_flavour} "
                f"| offset 0x{self.offset:x} | mode={self.mode}")

    def close(self) -> None:
        if self._config_path and os.path.exists(self._config_path):
            os.unlink(self._config_path)
            self._config_path = None
        if self._extract_dir and os.path.isdir(self._extract_dir):
            shutil.rmtree(self._extract_dir, ignore_errors=True)
            self._extract_dir = None
            self._work_image = None

    def __enter__(self) -> "FatWriteBackend":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
