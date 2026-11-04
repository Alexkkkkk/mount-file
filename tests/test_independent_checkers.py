"""Focused tests for test-only, non-repair independent checker wrappers."""

from __future__ import annotations

import shutil
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from mountfile.ext2_native import NativeExt2ImageWriter
from tests import _independent_checkers as checkers


def _digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _blank_image(path: Path, size: int = 32 * 1024 * 1024) -> None:
    with path.open("wb") as stream:
        stream.truncate(size)


def _require_tools(*tools: str) -> None:
    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"independent real-image check requires: {', '.join(missing)}")


def _format_ext2(image: Path) -> None:
    _require_tools("mke2fs")
    subprocess.run(
        [
            shutil.which("mke2fs"),
            "-q",
            "-F",
            "-t",
            "ext2",
            "-b",
            "1024",
            "-O",
            "none,filetype,sparse_super,large_file",
            str(image),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _format_fat32(image: Path) -> None:
    _require_tools("mkfs.fat")
    subprocess.run(
        [shutil.which("mkfs.fat"), "-F", "32", str(image)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _format_exfat(image: Path) -> None:
    _require_tools("mkfs.exfat")
    subprocess.run(
        [shutil.which("mkfs.exfat"), str(image)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _format_ntfs(image: Path) -> None:
    _require_tools("mkfs.ntfs")
    subprocess.run(
        [shutil.which("mkfs.ntfs"), "-F", "-Q", str(image)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_unavailable_checker_is_explicit_and_does_not_touch_source(
    tmp_path, monkeypatch
):
    image = tmp_path / "synthetic.ext2"
    image.write_bytes(b"synthetic image")
    before = _digest(image)
    monkeypatch.setattr(checkers.shutil, "which", lambda _tool: None)

    result = checkers.check_ext_image(image)

    assert result.verdict is checkers.CheckerVerdict.UNAVAILABLE
    assert not result.available
    assert not result.is_clean
    assert result.source_unchanged
    assert result.copy_unchanged is None
    assert _digest(image) == before


def test_e2fsck_code_one_is_not_clean_and_checker_receives_copy(tmp_path, monkeypatch):
    image = tmp_path / "synthetic.ext2"
    image.write_bytes(b"source remains private")
    before = _digest(image)
    seen: dict[str, Path] = {}
    monkeypatch.setattr(checkers.shutil, "which", lambda _tool: "/fake/e2fsck")

    def fake_run(command, **kwargs):
        disposable = Path(command[-1])
        seen["copy"] = disposable
        assert disposable != image
        assert disposable.read_bytes() == image.read_bytes()
        assert kwargs["timeout"] == 7
        assert kwargs["check"] is False
        return subprocess.CompletedProcess(
            command, 1, stdout="would correct", stderr=""
        )

    monkeypatch.setattr(checkers.subprocess, "run", fake_run)
    result = checkers.check_ext_image(image, timeout=7)

    assert result.returncode == 1
    assert result.exit_flags == ("errors-corrected",)
    assert result.verdict is checkers.CheckerVerdict.NOT_VERIFIED_CLEAN
    assert not result.is_clean
    assert result.source_unchanged
    assert result.copy_unchanged
    assert _digest(image) == before
    assert not seen["copy"].exists(), "disposable checker copy must be cleaned up"


def test_successful_checker_that_changes_copy_is_not_accepted(tmp_path, monkeypatch):
    image = tmp_path / "synthetic.fat"
    image.write_bytes(b"unchanged source")
    before = _digest(image)
    monkeypatch.setattr(checkers.shutil, "which", lambda _tool: "/fake/fsck.fat")

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"checker changed only the copy")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(checkers.subprocess, "run", fake_run)
    result = checkers.check_fat_image(image)

    assert result.returncode == 0
    assert result.verdict is checkers.CheckerVerdict.NOT_VERIFIED_CLEAN
    assert result.source_unchanged
    assert not result.copy_unchanged
    assert _digest(image) == before


def test_timeout_is_reported_without_touching_source(tmp_path, monkeypatch):
    image = tmp_path / "synthetic.exfat"
    image.write_bytes(b"timeout source")
    before = _digest(image)
    monkeypatch.setattr(checkers.shutil, "which", lambda _tool: "/fake/fsck.exfat")

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command,
            kwargs["timeout"],
            output=b"partial checker output",
            stderr=b"checker timeout",
        )

    monkeypatch.setattr(checkers.subprocess, "run", fake_run)
    result = checkers.check_exfat_image(image, timeout=3)

    assert result.verdict is checkers.CheckerVerdict.TIMED_OUT
    assert result.timed_out
    assert result.returncode is None
    assert result.stdout == "partial checker output"
    assert result.stderr == "checker timeout"
    assert result.source_unchanged
    assert result.copy_unchanged
    assert _digest(image) == before


def test_ntfsfix_success_is_inconclusive_not_clean(tmp_path, monkeypatch):
    image = tmp_path / "synthetic.ntfs"
    image.write_bytes(b"ntfs source")
    monkeypatch.setattr(checkers.shutil, "which", lambda _tool: "/fake/ntfsfix")
    monkeypatch.setattr(
        checkers.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout="metadata checked", stderr=""
        ),
    )

    result = checkers.check_ntfsfix_image(image)

    assert result.returncode == 0
    assert result.verdict is checkers.CheckerVerdict.INCONCLUSIVE
    assert not result.is_clean
    assert result.source_unchanged
    assert result.copy_unchanged


@pytest.mark.parametrize(
    ("name", "required_tools", "formatter", "checker"),
    [
        ("ext2", ("mke2fs", "e2fsck"), _format_ext2, checkers.check_ext_image),
        ("fat32", ("mkfs.fat", "fsck.fat"), _format_fat32, checkers.check_fat_image),
        (
            "exfat",
            ("mkfs.exfat", "fsck.exfat"),
            _format_exfat,
            checkers.check_exfat_image,
        ),
    ],
)
def test_real_mkfs_image_passes_nonrepair_checker_on_disposable_copy(
    tmp_path, name, required_tools, formatter, checker
):
    _require_tools(*required_tools)
    image = tmp_path / f"{name}.img"
    _blank_image(image)
    formatter(image)
    before = _digest(image)

    result = checker(image, timeout=60)

    assert result.available
    assert not result.timed_out
    assert result.command[-1] != str(image)
    assert result.verdict is checkers.CheckerVerdict.CLEAN, result.diagnostics()
    assert result.source_unchanged
    assert result.copy_unchanged
    assert _digest(image) == before


def test_native_ext2_mutation_passes_e2fsck_on_disposable_copy(tmp_path):
    _require_tools("mke2fs", "e2fsck")
    image = tmp_path / "native-write.ext2"
    _blank_image(image)
    _format_ext2(image)
    writer = NativeExt2ImageWriter(image)
    writer.mkdir("/evidence")
    writer.write_file("/evidence/checker.bin", b"native writer output")
    before = _digest(image)

    result = checkers.check_ext_image(image, timeout=60)

    assert result.verdict is checkers.CheckerVerdict.CLEAN, result.diagnostics()
    assert result.source_unchanged
    assert result.copy_unchanged
    assert _digest(image) == before


def test_real_mkfs_ntfs_is_never_declared_clean_by_ntfsfix(tmp_path):
    _require_tools("mkfs.ntfs", "ntfsfix")
    image = tmp_path / "fresh.ntfs"
    _blank_image(image)
    _format_ntfs(image)
    before = _digest(image)

    result = checkers.check_ntfsfix_image(image, timeout=60)

    assert result.available
    assert not result.timed_out
    assert not result.is_clean
    assert result.verdict is checkers.CheckerVerdict.INCONCLUSIVE, result.diagnostics()
    assert result.source_unchanged
    assert result.copy_unchanged
    assert _digest(image) == before
