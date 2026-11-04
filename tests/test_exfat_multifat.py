"""TexFAT is never enabled by a successful geometric exFAT probe."""

import struct
from types import SimpleNamespace

import pytest
from test_exfat_probe import VOLUME_SIZE, _tree_image

from mountfile import vfs
from mountfile.exfat_probe import (
    ExfatProbeError,
    exfat_boot_checksum,
    parse_exfat_boot_region,
    probe_exfat_boot_region,
)
from mountfile.exfat_reader import ExfatReadError, NativeExfatImage
from mountfile.exfat_writer import ExfatWriteError, NativeExfatWriter
from mountfile.safety import RefusalCode
from mountfile.universal_fs_rw import NativeExfatRW, UniversalFSRW
from mountfile.write_engine import WriteEngine, WriterUnavailableError


def _boot(number_of_fats=2, flags=1, backup_number_of_fats=None):
    sector = 512
    region = bytearray(24 * sector)
    for start in (0, 12 * sector):
        boot = memoryview(region)[start : start + sector]
        boot[3:11] = b"EXFAT   "
        struct.pack_into("<Q", boot, 0x48, 4096)
        struct.pack_into("<I", boot, 0x50, 24)
        struct.pack_into("<I", boot, 0x54, 8)
        struct.pack_into("<I", boot, 0x58, 40 if number_of_fats == 2 else 32)
        struct.pack_into("<I", boot, 0x5C, 100)
        struct.pack_into("<I", boot, 0x60, 2)
        struct.pack_into("<H", boot, 0x68, 0x0100)
        struct.pack_into("<H", boot, 0x6A, flags)
        boot[0x6C] = 9
        boot[0x6E] = number_of_fats
        boot[510:512] = b"\x55\xaa"
    if backup_number_of_fats is not None:
        region[12 * sector + 0x6E] = backup_number_of_fats
    for start in (0, 12 * sector):
        checksum = exfat_boot_checksum(
            bytes(region[start : start + 11 * sector]), sector_size=sector
        )
        for offset in range(0, sector, 4):
            struct.pack_into("<I", region, start + 11 * sector + offset, checksum)
    return bytes(region)


def _set_boot_jump(raw):
    # The independent firmware detector requires a boot jump in addition to the
    # OEM magic. Recompute both checksums after making synthetic sectors realistic.
    for start in (0, 12 * 512):
        raw[start : start + 3] = b"\xeb\x76\x90"
        checksum = exfat_boot_checksum(raw[start : start + 11 * 512])
        for offset in range(0, 512, 4):
            struct.pack_into("<I", raw, start + 11 * 512 + offset, checksum)


def _two_fat_image(path, active, bitmap_kind):
    """Create synthetic cases, not a claim of complete/valid TexFAT metadata."""
    _tree_image(path, number_of_fats=2, volume_flags=active)
    raw = bytearray(path.read_bytes())
    _set_boot_jump(raw)
    root = 40 * 512
    if bitmap_kind == "none":
        raw[root] = 0
    elif bitmap_kind == "id1":
        raw[root + 1] = 1
    elif bitmap_kind == "invalid":
        raw[root + 1] = 0x80
    elif bitmap_kind == "pair":
        # Two distinct bitmap entries and streams, IDs 0 and 1. FAT contents
        # remain deliberately different; no TexFAT recovery is inferred here.
        raw[root + 160 : root + 192] = raw[root : root + 32]
        raw[root + 161] = 1
        struct.pack_into("<I", raw, root + 180, 6)
        struct.pack_into("<I", raw, (24 + active * 8) * 512 + 6 * 4, 0xFFFFFFFF)
        raw[42 * 512] |= 0x10  # bitmap 0 allocates cluster 6
        raw[44 * 512 : 45 * 512] = raw[42 * 512 : 43 * 512]
    elif bitmap_kind != "id0":
        raise AssertionError(bitmap_kind)
    path.write_bytes(raw)


@pytest.mark.parametrize("flags, active", [(0, 0), (1, 1), (2, 0), (0x8, 0), (0xA, 0)])
def test_probe_decodes_active_fat_but_does_not_authorize_texfat(flags, active):
    profile = parse_exfat_boot_region(_boot(flags=flags), volume_size=4096 * 512)
    assert profile.number_of_fats == 2
    assert profile.active_fat == active
    assert profile.volume_flags == flags


def test_one_fat_cannot_select_fat_copy_two():
    with pytest.raises(ExfatProbeError, match="active FAT"):
        parse_exfat_boot_region(
            _boot(number_of_fats=1, flags=1), volume_size=4096 * 512
        )


def test_backup_number_of_fats_is_geometry_and_must_reconcile():
    with pytest.raises(ExfatProbeError, match="geometry mismatch"):
        parse_exfat_boot_region(
            _boot(number_of_fats=2, flags=1, backup_number_of_fats=1),
            volume_size=4096 * 512,
        )


def test_reserved_high_volume_flags_are_rejected():
    with pytest.raises(ExfatProbeError, match="reserved bits"):
        parse_exfat_boot_region(_boot(flags=0x10), volume_size=4096 * 512)


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        (0x0002, "volume is dirty"),
        (0x0004, "media failure"),
        (0x0008, "ClearToZero"),
        (0x0010, "reserved bits"),
    ],
)
def test_one_fat_writer_refuses_unsafe_volume_flags_before_staging(
    tmp_path, monkeypatch, flags, message
):
    image = tmp_path / "volume.img"
    image.write_bytes(b"source")
    profile = SimpleNamespace(
        number_of_fats=1,
        volume_flags=flags,
        volume_length=1,
        bytes_per_sector=512,
    )
    monkeypatch.setattr(
        "mountfile.exfat_writer._image_identity",
        lambda *_args, **_kwargs: (6, b"x" * 32),
    )
    monkeypatch.setattr(
        "mountfile.exfat_writer.probe_exfat_boot_region",
        lambda *_args, **_kwargs: profile,
    )

    with pytest.raises(ExfatWriteError, match=message):
        NativeExfatWriter(image, volume_size=512)

    assert image.read_bytes() == b"source"
    assert list(tmp_path.glob("*.stage")) == []


@pytest.mark.parametrize("active", [0, 1])
@pytest.mark.parametrize("bitmap_kind", ["none", "id0", "id1", "pair", "invalid"])
@pytest.mark.parametrize("prefix_size", [512, 666])
def test_two_fat_refused_before_metadata_or_staging_across_native_routes(
    tmp_path, monkeypatch, active, bitmap_kind, prefix_size
):
    volume = tmp_path / "volume.img"
    _two_fat_image(volume, active, bitmap_kind)
    prefix, suffix = b"P" * prefix_size, b"suffix outside partition"
    image = tmp_path / "bounded.img"
    image.write_bytes(prefix + volume.read_bytes() + suffix)
    before = image.read_bytes()
    offset = len(prefix)
    profile = probe_exfat_boot_region(image, offset=offset, volume_size=VOLUME_SIZE)
    assert (profile.number_of_fats, profile.active_fat) == (2, active)

    monkeypatch.setattr(
        NativeExfatImage,
        "system_metadata",
        lambda *_: pytest.fail("read system metadata"),
    )
    monkeypatch.setattr(
        NativeExfatWriter, "_copy_stage", lambda *_: pytest.fail("created stage")
    )
    monkeypatch.setattr(vfs, "TskImage", lambda *_a, **_k: pytest.fail("TSK fallback"))
    monkeypatch.setattr(
        "mountfile.universal_fs_rw.LoopMountRW",
        lambda *_a, **_k: pytest.fail("mount fallback"),
    )

    for open_reader in (
        lambda: NativeExfatImage(image, offset=offset, volume_size=VOLUME_SIZE),
        lambda: NativeExfatRW(image, offset=offset, volume_size=VOLUME_SIZE),
    ):
        with pytest.raises(ExfatReadError, match="NumberOfFats=2") as refusal:
            open_reader()
        assert refusal.value.code is RefusalCode.UNSUPPORTED_PROFILE
        assert refusal.value.category.value == "unsupported"
    with pytest.raises(ExfatWriteError, match="NumberOfFats=2") as refusal:
        NativeExfatWriter(image, offset=offset, volume_size=VOLUME_SIZE)
    assert refusal.value.code is RefusalCode.UNSUPPORTED_PROFILE
    for allow in (False, True):
        with pytest.raises(
            vfs.RecognizedFilesystemError, match="NumberOfFats=2"
        ) as refusal:
            vfs.open_image_provider(
                image,
                offset=offset,
                volume_size=VOLUME_SIZE,
                allow_legacy_fallback=allow,
            )
        assert refusal.value.code is RefusalCode.UNSUPPORTED_PROFILE
        assert refusal.value.category.value == "unsupported"
        with pytest.raises(RuntimeError, match="NumberOfFats=2") as refusal:
            UniversalFSRW(
                image,
                offset=offset,
                volume_size=VOLUME_SIZE,
                auto_backup=False,
                allow_legacy_mount=allow,
            )
        assert isinstance(refusal.value.__cause__, ExfatReadError)
        assert refusal.value.__cause__.code is RefusalCode.UNSUPPORTED_PROFILE
    assert image.read_bytes() == before
    assert list(tmp_path.glob("*.stage")) == []


@pytest.mark.parametrize("active", [0, 1])
@pytest.mark.parametrize("damage", ["jump", "signature"])
def test_exfat_marker_never_falls_back_when_boot_is_damaged(
    tmp_path, monkeypatch, active, damage
):
    image = tmp_path / "bad-boot.img"
    _two_fat_image(image, active, "id0")
    raw = bytearray(image.read_bytes())
    if damage == "jump":
        raw[0:3] = bytes(3)
    else:
        raw[510:512] = bytes(2)
    checksum = exfat_boot_checksum(raw[: 11 * 512])
    for offset in range(0, 512, 4):
        struct.pack_into("<I", raw, 11 * 512 + offset, checksum)
    image.write_bytes(raw)
    before = image.read_bytes()
    monkeypatch.setattr(
        "mountfile.universal_fs_rw.LoopMountRW",
        lambda *_a, **_k: pytest.fail("mount fallback"),
    )
    monkeypatch.setattr(vfs, "TskImage", lambda *_a, **_k: pytest.fail("TSK fallback"))

    with pytest.raises(vfs.RecognizedFilesystemError):
        vfs.open_image_provider(
            image, volume_size=VOLUME_SIZE, allow_legacy_fallback=True
        )
    with pytest.raises(RuntimeError):
        UniversalFSRW(image, offset=0, volume_size=VOLUME_SIZE, allow_legacy_mount=True)
    assert image.read_bytes() == before


@pytest.mark.parametrize("active", [0, 1])
def test_write_engine_refuses_two_fat_without_changing_image(tmp_path, active):
    image = tmp_path / "engine.img"
    _two_fat_image(image, active, "pair")
    before = image.read_bytes()
    # The facade's separate damage report does not currently classify exFAT;
    # even an explicit damaged-image override cannot bypass the native refusal.
    with pytest.raises(WriterUnavailableError, match="NumberOfFats=2"):
        WriteEngine(
            image,
            offset=0,
            read_only=False,
            allow_damaged=True,
            snapshot_dir=tmp_path / "snapshots",
            journal_path=tmp_path / "journal.jsonl",
        )
    assert image.read_bytes() == before


@pytest.mark.parametrize("flags", [0, 1, 2, 3, 4, 5, 8, 9])
def test_two_fat_refusal_precedes_volume_state_and_bitmap_checks(tmp_path, flags):
    image = tmp_path / "flags.img"
    _two_fat_image(image, flags & 1, "pair")
    raw = bytearray(image.read_bytes())
    # VolumeFlags are excluded from boot-region checksums.
    struct.pack_into("<H", raw, 0x6A, flags)
    image.write_bytes(raw)
    before = image.read_bytes()
    for opener, error in (
        (NativeExfatImage, ExfatReadError),
        (NativeExfatWriter, ExfatWriteError),
    ):
        with pytest.raises(error, match="NumberOfFats=2") as refusal:
            opener(image, volume_size=VOLUME_SIZE)
        assert refusal.value.code is RefusalCode.UNSUPPORTED_PROFILE
    assert image.read_bytes() == before


def test_writer_revalidates_stage_when_opened_one_fat_image_changes_to_two(tmp_path):
    image = tmp_path / "image.img"
    _tree_image(image)
    writer = NativeExfatWriter(image, volume_size=VOLUME_SIZE)
    _two_fat_image(image, 1, "id0")
    before = image.read_bytes()
    with pytest.raises(ExfatWriteError, match="NumberOfFats=2") as refusal:
        writer.write_file("/HELLO.TXT", b"must not be written")
    assert refusal.value.code is RefusalCode.UNSUPPORTED_PROFILE
    assert image.read_bytes() == before
    assert list(tmp_path.glob("*.stage")) == []


def test_fat_write_cannot_mirror_a_forged_two_fat_reader():
    writer = NativeExfatWriter.__new__(NativeExfatWriter)
    writes = []
    writer._write_rel = lambda _stream, offset, data: writes.append((offset, data))
    reader = SimpleNamespace(
        profile=SimpleNamespace(number_of_fats=2, fat_offset=24, bytes_per_sector=512)
    )
    with pytest.raises(ExfatWriteError, match="NumberOfFats=2") as refusal:
        writer._fat_write(object(), reader, 3, 0x12345678)
    assert refusal.value.code is RefusalCode.UNSUPPORTED_PROFILE
    assert writes == []
    reader.profile.number_of_fats = 1
    writer._fat_write(object(), reader, 3, 0x12345678)
    assert writes == [(24 * 512 + 3 * 4, struct.pack("<I", 0x12345678))]


def test_one_fat_status_and_mutations_remain_available(tmp_path):
    image = tmp_path / "one-fat.img"
    _tree_image(image)
    raw = bytearray(image.read_bytes())
    _set_boot_jump(raw)
    image.write_bytes(raw)
    reader = vfs.open_image_provider(image, volume_size=VOLUME_SIZE)
    try:
        status = reader.backend_status
        assert status["profile_id"] == "exfat-native-ro"
        assert status["operations"]["read-file"]["state"] == "available"
        assert status["operations"]["write-file"]["state"] == "refused"
        assert "single-exfat-fat" in status["constraint_ids"]
    finally:
        reader.close()
    with UniversalFSRW(image, volume_size=VOLUME_SIZE, auto_backup=False) as fs:
        status = fs.capability_status()
        assert status["profile_id"] == "exfat-native-rw"
        assert status["write_enabled"] is True
        assert "single-exfat-fat" in status["constraint_ids"]
        fs.write_file("/HELLO.TXT", b"single FAT still works")
        fs.mkdir("/dir")
        fs.rename("/dir", "/moved")
        fs.delete("/moved")
        assert fs.read("/HELLO.TXT") == b"single FAT still works"
