"""Functional audit regressions for CLI routing, extraction and RW facades."""

from __future__ import annotations

import io
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mountfile import autopick, cli, vfs
from mountfile.extract import ExtractError, extract_file, extract_path
from mountfile.partition import list_partitions
from mountfile.rw import complete
from mountfile.rw.universal import Ext4RW, Mode, NtfsRW, RWError, StrategyUnavailable
from mountfile.rw.v2 import MountFileBridge, create_verified
from mountfile.universal_fs_rw import ImageBackup, UniversalFSRW
from tests.test_ext2_native import _make_ext2
from tests.test_fat32 import make_fat32_image
from tests.test_fat_reader import _make_small_fat


def _partitioned_image(tmp_path, fat_type=12):
    volume = tmp_path / "volume.img"
    payload = _make_small_fat(volume, fat_type)
    content = volume.read_bytes()
    offset = 4096
    prefix = bytearray(b"P" * offset)
    prefix[446:510] = bytes(64)
    prefix[446 + 4] = 0x01 if fat_type == 12 else 0x06
    struct.pack_into("<II", prefix, 446 + 8, offset // 512, len(content) // 512)
    prefix[510:512] = b"\x55\xaa"
    suffix = b"NEIGHBOR-DATA" * 512
    image = tmp_path / "disk.img"
    image.write_bytes(prefix + content + suffix)
    return image, offset, len(content), bytes(prefix), suffix, payload


@pytest.mark.parametrize("fat_type", [12, 16])
def test_real_cli_selected_partition_full_mutation_roundtrip(tmp_path, fat_type):
    image, offset, extent, prefix, suffix, _ = _partitioned_image(tmp_path, fat_type)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"actual cli payload")
    before = image.read_bytes()
    assert (
        cli.main(["put", str(image), str(source), "--volume", "1"]) == cli.NEED_CONFIRM
    )
    assert image.read_bytes() == before
    assert not Path(str(image) + ".bak").exists()
    assert (
        cli.main(["mkdir", str(image), "/DOCS", "--volume", "1", "--backup", "--yes"])
        == 0
    )
    assert Path(str(image) + ".bak").read_bytes() == before
    assert (
        cli.main(
            [
                "put",
                str(image),
                str(source),
                "--dest",
                "/DOCS",
                "--offset",
                str(offset),
                "--yes",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "mv",
                str(image),
                "/DOCS/payload.bin",
                "renamed.bin",
                "--volume",
                "1",
                "--yes",
            ]
        )
        == 0
    )
    out = tmp_path / "out"
    assert (
        cli.main(
            ["get", str(image), "/DOCS/renamed.bin", "--volume", "1", "-o", str(out)]
        )
        == 0
    )
    assert (out / "renamed.bin").read_bytes() == source.read_bytes()
    assert (
        cli.main(["rm", str(image), "/DOCS", "--volume", "1", "--recursive", "--yes"])
        == 0
    )
    result = image.read_bytes()
    assert result[:offset] == prefix
    assert result[offset + extent :] == suffix


def test_module_entrypoint_reads_real_selected_volume(tmp_path):
    image, _, _, _, _, payload = _partitioned_image(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mountfile",
            "read-range",
            str(image),
            "/HELLO.TXT",
            "--volume",
            "1",
            "--length",
            "4",
        ],
        capture_output=True,
        check=False,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == payload[:4]


def test_auto_uses_native_ext2_with_container_extent_for_read_and_write(
    tmp_path, monkeypatch, capsys
):
    if shutil.which("mke2fs") is None:
        pytest.skip("mke2fs is required for a real ext2 image")
    monkeypatch.delenv("MOUNTFILE_ALLOW_LEGACY_TSK", raising=False)
    monkeypatch.setattr(
        vfs, "TskImage", lambda *args, **kwargs: pytest.fail("unexpected legacy parser")
    )
    volume = tmp_path / "ext2.img"
    _make_ext2(volume)
    payload = volume.read_bytes()
    image = tmp_path / "embedded.img"
    prefix = bytes(4096)
    container = bytes(4096) + payload
    suffix = b"untouched neighbor" * 512
    image.write_bytes(prefix + container + suffix)
    udev = tmp_path / "layout.txt"
    udev.write_text(f"[PARTITIONS]\n4096,{len(container)},system\n", encoding="utf-8")
    best, _ = autopick.auto_select(image, udev=udev)
    assert best is not None
    assert best.offset == 8192
    assert best.extent_size == len(payload)
    assert best.backend.startswith("native-ext2")
    target = ["--auto", "--udev", str(udev)]
    assert cli.main(["mkdir", str(image), "/DOCS", *target, "--yes"]) == 0
    source = tmp_path / "hello.txt"
    source.write_bytes(b"bounded ext2")
    assert (
        cli.main(["put", str(image), str(source), "--dest", "/DOCS", *target, "--yes"])
        == 0
    )
    assert cli.main(["stat", str(image), "/DOCS/hello.txt", *target]) == 0
    capsys.readouterr()
    out = tmp_path / "extracted"
    assert cli.main(["get", str(image), "/DOCS", *target, "-o", str(out)]) == 0
    assert (out / "DOCS" / "hello.txt").read_bytes() == b"bounded ext2"
    result = image.read_bytes()
    assert result[:8192] == prefix + bytes(4096)
    assert result[8192 + len(payload) :] == suffix


def test_udev_short_extent_never_authorizes_neighbor_reads(tmp_path):
    volume = tmp_path / "fat.img"
    _make_small_fat(volume, 12)
    image = tmp_path / "embedded.img"
    image.write_bytes(bytes(4096) + volume.read_bytes() + bytes(4096))
    udev = tmp_path / "layout.txt"
    udev.write_text("[PARTITIONS]\n4096,512,system\n", encoding="utf-8")
    best, candidates = autopick.auto_select(image, udev=udev)
    assert best is None
    selected = next(c for c in candidates if c.offset == 4096)
    assert selected.extent_size == 512
    assert selected.backend is None


def test_unopenable_candidates_never_become_success(monkeypatch, tmp_path, capsys):
    image = tmp_path / "bad.img"
    image.write_bytes(bytes(512))
    candidate = autopick.Candidate(0, "ext2/3/4", size=512, extent_size=512)
    monkeypatch.setattr(
        autopick, "scan_candidates", lambda *args, **kwargs: [candidate]
    )
    monkeypatch.setattr(
        autopick, "verify_mount", lambda *args, **kwargs: (None, 0, "dirty volume")
    )
    best, candidates = autopick.auto_select(image)
    assert best is None and candidates == [candidate]
    assert cli.main(["autopick", str(image), "--json"]) == cli.ERROR
    assert json.loads(capsys.readouterr().out)["best"] is None


def test_fat16_autopick_uses_cluster_geometry_not_root_entry_count(tmp_path):
    image = tmp_path / "fat16.img"
    _make_small_fat(image, 16)
    with image.open("r+b") as stream:
        stream.seek(36)
        stream.write(b"\x80\x00\x29\x12")  # standard FAT16 EBPB, not a FAT32 size
    best, _ = autopick.auto_select(image)
    assert best is not None
    assert best.fstype == "FAT16"
    assert best.backend == "native-fat16"


def test_iso_and_hfs_candidate_geometry_is_bounded_and_correct(tmp_path):
    iso = tmp_path / "iso.img"
    raw = bytearray(0x10000)
    raw[0x8000] = 1
    raw[0x8001:0x8006] = b"CD001"
    struct.pack_into("<I", raw, 0x8050, len(raw) // 2048)
    iso.write_bytes(raw)
    with iso.open("rb") as handle:
        assert autopick._validate_at(handle, 0) == ("ISO9660", "", len(raw))
    hfs = tmp_path / "hfs.img"
    raw = bytearray(8192)
    raw[1024:1026] = b"H+"
    struct.pack_into(">II", raw, 1064, 4096, 2)
    hfs.write_bytes(raw)
    with hfs.open("rb") as handle:
        assert autopick._validate_at(handle, 0) == ("HFS+", "", 8192)


@pytest.mark.parametrize(
    "options",
    [["--offset", "-1"], ["--volume", "1", "--auto"], ["--offset", "0", "--auto"]],
)
def test_invalid_or_conflicting_cli_selection_is_refused(tmp_path, options):
    image = tmp_path / "fat.img"
    _make_small_fat(image, 12)
    assert cli.main(["ls", str(image), *options]) == cli.ERROR


def test_explicit_backup_never_overwrites_previous_backup(tmp_path, monkeypatch):
    image = tmp_path / "disk.img"
    image.write_bytes(b"modified image")
    backup = Path(str(image) + ".bak")
    backup.write_bytes(b"original evidence")
    monkeypatch.setattr(
        cli, "UniversalFSRW", lambda *args, **kwargs: pytest.fail("writer opened")
    )
    with pytest.raises(FileExistsError):
        cli._open_write(image, 0, backup=True)
    assert backup.read_bytes() == b"original evidence"
    assert image.read_bytes() == b"modified image"


def test_backup_identity_and_same_second_copies_are_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    first = tmp_path / "one" / "disk.img"
    second = tmp_path / "two" / "disk.img"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"first original")
    second.write_bytes(b"second original")
    a, b = ImageBackup(first), ImageBackup(second)
    original = a.create()
    first.write_bytes(b"first modified")
    newer = a.create()
    other = b.create()
    assert original != newer != other
    assert original.read_bytes() == b"first original"
    assert a.latest() == newer
    assert b.latest() == other
    assert newer.read_bytes() == b"first modified"
    assert other.read_bytes() == b"second original"


def test_facade_stat_exists_restore_and_closed_state(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    image = tmp_path / "fat.img"
    _make_small_fat(image, 12)
    with UniversalFSRW(image) as fs:
        assert fs.stat("/").is_dir
        assert not fs.exists("/NEW.TXT")
        fs.write_file("/NEW.TXT", b"changed")
        assert fs.exists("/NEW.TXT")
        assert fs.restore_from_backup() is True
        assert not fs.exists("/NEW.TXT")
    fs.close()
    with pytest.raises(RuntimeError, match="closed"):
        fs.write_file("/BLOCKED.TXT", b"no")
    with pytest.raises(RuntimeError, match="closed"):
        fs.list()


def test_extraction_respects_case_sensitive_native_paths(tmp_path):
    class Provider:
        def _path_names_equal(self, requested, actual):
            return requested == actual

        def list_dir(self, path):
            return [
                {"name": "FILE", "is_dir": True, "size": 0},
                {"name": "file", "is_dir": False, "size": 7},
            ]

        def read_file(self, path):
            assert path == "/file"
            return b"correct"

    provider = Provider()
    out = tmp_path / "out"
    assert extract_path(provider, "/file", out) == 1
    assert (out / "file").read_bytes() == b"correct"
    with pytest.raises(ExtractError, match="not found"):
        extract_path(provider, "/FiLe", tmp_path / "other")


def test_extraction_cannot_overwrite_its_source_image(tmp_path):
    image = tmp_path / "source.img"
    image.write_bytes(b"original evidence")
    provider = SimpleNamespace(image_path=image, read_file=lambda path: b"replacement")
    with pytest.raises(ExtractError, match="source image"):
        extract_file(provider, "/file", image)
    assert image.read_bytes() == b"original evidence"


def test_bridge_safe_extraction_rejects_destination_symlinks(tmp_path):
    image = tmp_path / "fat.img"
    payload = _make_small_fat(image, 12)
    bridge = MountFileBridge(image)
    assert bridge.stat("/HELLO.TXT")["size"] == len(payload)
    assert bridge.list_files("/")[0]["name"] == "HELLO.TXT"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"host evidence")
    link = tmp_path / "link.bin"
    link.symlink_to(outside)
    with pytest.raises(ExtractError, match="symlink"):
        bridge.extract_file("/HELLO.TXT", link)
    assert outside.read_bytes() == b"host evidence"


def test_verified_fat32_real_operations_and_failed_verify_rollback(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    engine = create_verified(image, mode=Mode.RW)
    engine.open()
    try:
        engine.create_dir("/DOCS")
        engine.create_file("/DOCS/A.TXT", io.BytesIO(b"AAAA-BBBB"))
        engine.write_file("/DOCS/A.TXT", io.BytesIO(b"XXXX"), offset=5)
        engine.rename("/DOCS/A.TXT", "/DOCS/B.TXT")
        assert engine.stat("/DOCS/B.TXT").size == 9
        before = image.read_bytes()
        engine.verifier = SimpleNamespace(
            verify=lambda *args: SimpleNamespace(ok=False, details="injected failure")
        )
        with pytest.raises(RWError, match="injected failure"):
            engine.create_file("/DOCS/C.TXT", io.BytesIO(b"must roll back"))
        assert image.read_bytes() == before
        assert engine.journal.uncommitted() == []
        assert engine.stat("/DOCS/B.TXT").size == 9
        with pytest.raises(FileNotFoundError):
            engine.stat("/DOCS/C.TXT")
    finally:
        engine.close()


def test_native_ext_compatibility_reports_real_metadata_and_roundtrip(tmp_path):
    if shutil.which("mke2fs") is None:
        pytest.skip("mke2fs is required for ext2 fixture")
    image = tmp_path / "ext2.img"
    _make_ext2(image)
    engine = Ext4RW(image, Mode.RW)
    engine.open()
    try:
        engine.create_dir("/DOCS")
        engine.create_file("/DOCS/note.txt", io.BytesIO(b"actual metadata"))
        assert engine.stat("/DOCS/note.txt").size == len(b"actual metadata")
        engine.rename("/DOCS/note.txt", "/DOCS/renamed.txt")
        engine.write_file("/DOCS/renamed.txt", io.BytesIO(b"REAL"), offset=0)
        assert engine._read("/DOCS/renamed.txt") == b"REALal metadata"
        engine.delete("/DOCS/renamed.txt")
        with pytest.raises(FileNotFoundError):
            engine.stat("/DOCS/renamed.txt")
    finally:
        engine.close()


def test_nonfat_dry_run_is_explicitly_refused(tmp_path):
    with pytest.raises(StrategyUnavailable, match="dry-run"):
        complete.create_rw_full(
            tmp_path / "unused.img", fs=complete.FsType.EXT4, mode=Mode.RW, dry_run=True
        )


def test_legacy_restore_cannot_mutate_read_only_image(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    backup = image.with_suffix(image.suffix + ".full-backup")
    backup.write_bytes(b"different")
    engine = complete.create_rw_full(image, mode=Mode.RO)
    before = image.read_bytes()
    try:
        with pytest.raises(RWError, match="RO"):
            engine.restore_from_backup()
        assert image.read_bytes() == before
    finally:
        engine.close()


def test_legacy_ntfs_mounted_paths_refuse_host_escape(tmp_path):
    mount = tmp_path / "mount"
    outside = tmp_path / "outside"
    mount.mkdir()
    outside.mkdir()
    (mount / "escape").symlink_to(outside, target_is_directory=True)
    engine = object.__new__(NtfsRW)
    engine._mnt = mount
    with pytest.raises(ValueError, match="unsafe path"):
        engine._path("/../outside")
    with pytest.raises(PermissionError, match="link/reparse"):
        engine._path("/escape/host.txt")


def test_invalid_mbr_entries_are_not_used_as_volume_bounds(tmp_path):
    image = tmp_path / "invalid-mbr.img"
    raw = bytearray(4096)
    raw[446 + 4] = 0x83
    struct.pack_into("<II", raw, 446 + 8, 7, 10)
    raw[510:512] = b"\x55\xaa"
    image.write_bytes(raw)
    assert list_partitions(image) == []
    assert vfs.volume_size_for_offset(image, 7 * 512) is None
