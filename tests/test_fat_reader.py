# SPDX-License-Identifier: MIT
"""Regression tests for the bounded pure-Python FAT reader."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from mountfile import cli, vfs
from mountfile.fat32 import Fat32
from mountfile.fat_reader import FatReadError, NativeFatImage
from tests.test_fat32 import make_fat32_image


def _set_fat12_entry(fat: bytearray, cluster: int, value: int) -> None:
    offset = (cluster * 3) // 2
    current = struct.unpack_from("<H", fat, offset)[0]
    if cluster & 1:
        current = (current & 0x000F) | ((value & 0x0FFF) << 4)
    else:
        current = (current & 0xF000) | (value & 0x0FFF)
    struct.pack_into("<H", fat, offset, current)


def _make_small_fat(path, fat_type: int) -> bytes:
    bps = 512
    if fat_type == 12:
        total_sectors, reserved, fat_sectors, root_entries = 3000, 1, 10, 224
    elif fat_type == 16:
        total_sectors, reserved, fat_sectors, root_entries = 7000, 1, 32, 512
    else:  # pragma: no cover - callers use the existing complete FAT32 fixture
        raise AssertionError(fat_type)
    root_sectors = (root_entries * 32 + bps - 1) // bps
    data_sectors = total_sectors - reserved - fat_sectors - root_sectors
    with path.open("wb") as stream:
        stream.truncate(total_sectors * bps)
        boot = bytearray(bps)
        boot[:3] = b"\xeb\x3c\x90"
        boot[3:11] = b"MSDOS5.0"
        struct.pack_into("<H", boot, 11, bps)
        boot[13] = 1
        struct.pack_into("<H", boot, 14, reserved)
        boot[16] = 1
        struct.pack_into("<H", boot, 17, root_entries)
        struct.pack_into("<H", boot, 19, total_sectors)
        boot[21] = 0xF8
        struct.pack_into("<H", boot, 22, fat_sectors)
        boot[510:512] = b"\x55\xaa"
        label_offset = 54 if fat_type in (12, 16) else 82
        boot[label_offset : label_offset + 8] = f"FAT{fat_type}   ".encode()
        stream.write(boot)
        fat = bytearray(fat_sectors * bps)
        if fat_type == 12:
            _set_fat12_entry(fat, 0, 0xFF8)
            _set_fat12_entry(fat, 1, 0xFFF)
            _set_fat12_entry(fat, 2, 0xFFF)
        else:
            struct.pack_into("<HHH", fat, 0, 0xFFF8, 0xFFFF, 0xFFFF)
        stream.seek(reserved * bps)
        stream.write(fat)
        entry = bytearray(32)
        entry[:11] = b"HELLO   TXT"
        entry[11] = 0x20
        struct.pack_into("<H", entry, 26, 2)
        payload = f"hello FAT{fat_type}\n".encode()
        struct.pack_into("<I", entry, 28, len(payload))
        stream.seek((reserved + fat_sectors) * bps)
        stream.write(entry)
        data_start = (reserved + fat_sectors + root_sectors) * bps
        stream.seek(data_start)
        stream.write(payload.ljust(bps, b"\x00"))
    return payload


@pytest.mark.parametrize("fat_type", [12, 16])
def test_native_fat12_and_fat16_read_file(tmp_path, fat_type):
    image = tmp_path / f"fat{fat_type}.img"
    payload = _make_small_fat(image, fat_type)
    provider = NativeFatImage(image)
    try:
        assert provider.fat_type == fat_type
        assert provider.backend_name == f"native-fat{fat_type}-ro"
        assert provider.read_file("/hello.txt") == payload
        assert provider.read_file_range("/HELLO.TXT", 2, 5) == payload[2:7]
        assert provider.list_dir("/")[0]["name"] == "HELLO.TXT"
    finally:
        provider.close()


def test_native_fat32_reads_lfn_and_uses_native_backend(tmp_path):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    payload = b"native FAT32\n"
    with image.open("r+b") as stream:
        writer = Fat32(stream)
        writer.write_file("/", "A long native name.txt", payload)
    provider = NativeFatImage(image)
    try:
        assert provider.backend_name == "native-fat32-ro"
        assert provider.read_file("/A long native name.txt") == payload
    finally:
        provider.close()


def test_native_fat32_rejects_inconsistent_backup_boot_sector(tmp_path):
    image = tmp_path / "fat32-bad-backup.img"
    make_fat32_image(image)
    raw = bytearray(image.read_bytes())
    raw[6 * 512 + 3] ^= 1
    image.write_bytes(raw)

    with pytest.raises(FatReadError, match="backup boot sector"):
        NativeFatImage(image, volume_size=image.stat().st_size)


def test_native_fat32_rejects_inconsistent_mirrored_fat(tmp_path):
    image = tmp_path / "fat32-bad-mirror.img"
    make_fat32_image(image)
    raw = bytearray(image.read_bytes())
    raw[16] = 2
    raw[6 * 512 + 16] = 2
    raw[(32 + 600) * 512 + 8] ^= 1
    image.write_bytes(raw)

    with pytest.raises(FatReadError, match="mirrored FAT copies"):
        NativeFatImage(image, volume_size=image.stat().st_size)


def test_native_fat_rejects_short_chain_and_never_falls_back(tmp_path):
    image = tmp_path / "fat12.img"
    _make_small_fat(image, 12)
    with image.open("r+b") as stream:
        stream.seek(1 * 512 + 3)  # FAT12 entry for cluster 2
        raw = bytearray(stream.read(2))
        raw[0] &= 0x0F
        raw[1] = 0
        stream.seek(1 * 512 + 3)
        stream.write(raw)
    provider = NativeFatImage(image)
    try:
        with pytest.raises(FatReadError, match="shorter|free|invalid"):
            provider.read_file("/HELLO.TXT")
    finally:
        provider.close()


def test_vfs_selects_native_fat_without_pytsk3(tmp_path, monkeypatch):
    image = tmp_path / "fat32.img"
    make_fat32_image(image)
    monkeypatch.setattr(vfs, "pytsk3", None)
    provider = vfs.open_image_provider(str(image), volume_size=image.stat().st_size)
    try:
        assert provider.backend_name == "native-fat32-ro"
        assert provider.list_dir("/") == []
    finally:
        provider.close()


@pytest.mark.parametrize(
    ("marker_offset", "marker"),
    [(54, b"FAT12   "), (54, b"FAT16   "), (82, b"FAT32   ")],
)
def test_vfs_rejects_malformed_fat_marker_without_legacy_fallback(
    tmp_path, monkeypatch, marker_offset, marker
):
    image = tmp_path / f"malformed-{marker[:5].decode().lower()}.img"
    boot = bytearray(512)
    boot[marker_offset : marker_offset + len(marker)] = marker
    boot[510:512] = b"\x55\xaa"
    image.write_bytes(boot)
    opened_tsk = []

    def tsk_sentinel(*_args, **_kwargs):
        opened_tsk.append(True)
        raise AssertionError("TskImage must not open a recognized FAT image")

    monkeypatch.setattr(vfs, "TskImage", tsk_sentinel)
    with pytest.raises(vfs.RecognizedFilesystemError, match="native FAT reader"):
        vfs.open_image_provider(image, allow_legacy_fallback=True)
    assert opened_tsk == []


def test_native_fat_requires_explicit_extent_at_nonzero_offset(tmp_path):
    volume = tmp_path / "fat32-volume.img"
    make_fat32_image(volume)
    prefix = b"P" * 4096
    image = tmp_path / "disk.img"
    image.write_bytes(prefix + volume.read_bytes())

    with pytest.raises(FatReadError, match="explicit FAT volume_size"):
        NativeFatImage(image, offset=len(prefix))

    with pytest.raises(vfs.RecognizedFilesystemError, match="explicit FAT volume_size"):
        vfs.open_image_provider(image, offset=len(prefix), allow_legacy_fallback=True)


def test_vfs_rejects_inflated_fat_bpb_outside_nonzero_extent(tmp_path):
    volume = tmp_path / "fat32-volume.img"
    make_fat32_image(volume)
    raw_volume = bytearray(volume.read_bytes())
    declared_sectors = struct.unpack_from("<I", raw_volume, 32)[0]
    struct.pack_into("<I", raw_volume, 32, declared_sectors + 8)
    prefix = b"P" * 4096
    suffix = b"S" * (16 * 512)
    image = tmp_path / "disk.img"
    image.write_bytes(prefix + raw_volume + suffix)

    with pytest.raises(
        vfs.RecognizedFilesystemError, match="selected partition bounds"
    ):
        vfs.open_image_provider(
            image,
            offset=len(prefix),
            volume_size=len(raw_volume),
            allow_legacy_fallback=True,
        )


# ---------------------------------------------------------------------------
# Differential matrix (issue #182): native FAT12/16/32 readers vs an
# independent reference oracle.
#
# Neither pytsk3 nor mkfs.vfat is available in the test environment, so the
# oracle is a deliberately separate minimal FAT parser implemented below
# (_ReferenceFat).  It shares no code with mountfile.fat_reader: it parses the
# raw image bytes with its own BPB/FAT/LFN decoding.
# ---------------------------------------------------------------------------

_CLUSTER_SIZE = 512
_HELLO = b"Hello FAT reader!\n"
_LONG_NAME = "A long native name.txt"
_LONG_SHORT = b"LNGNAME TXT"
_LONG_PAYLOAD = b"long name payload\n"
_NOTE = b"nested note\n"
_BIG = bytes((i * 13 + 7) % 256 for i in range(3 * _CLUSTER_SIZE + 40))
_FILES = {
    "/HELLO.TXT": _HELLO,
    _LONG_NAME: _LONG_PAYLOAD,
    "/EMPTY.BIN": b"",
    "/BIG.BIN": _BIG,
    "/DOCS/NOTE.TXT": _NOTE,
}
_FAT_TYPES = [12, 16, 32]
_FAT_OFFSET = {12: 1 * 512, 16: 1 * 512, 32: 32 * 512}
_GEOMETRY = {
    12: dict(total_sectors=3000, reserved=1, fat_sectors=10, root_entries=224),
    16: dict(total_sectors=7000, reserved=1, fat_sectors=32, root_entries=512),
}
_EOC = {12: 0xFFF, 16: 0xFFFF, 32: 0x0FFFFFF8}


def _pack_short_entry(short_raw: bytes, attr: int, size: int, cluster: int) -> bytes:
    entry = bytearray(32)
    entry[0:11] = short_raw
    entry[11] = attr
    struct.pack_into("<H", entry, 26, cluster & 0xFFFF)
    struct.pack_into("<I", entry, 28, size)
    return bytes(entry)


def _lfn_dir_entries(long_name: str, short_raw: bytes) -> list[bytes]:
    """Build on-disk LFN entries for one long name (independent of the reader)."""
    checksum = 0
    for value in short_raw:
        checksum = (((checksum & 1) << 7) + (checksum >> 1) + value) & 0xFF
    units = [ord(char) for char in long_name] + [0x0000]
    total = (len(units) + 12) // 13
    units += [0xFFFF] * (total * 13 - len(units))
    offsets = (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30)
    entries = []
    for sequence in range(total, 0, -1):
        entry = bytearray(32)
        entry[0] = sequence | (0x40 if sequence == total else 0)
        entry[11] = 0x0F
        entry[13] = checksum
        for position, unit in zip(offsets, units[(sequence - 1) * 13 : sequence * 13]):
            struct.pack_into("<H", entry, position, unit)
        entries.append(bytes(entry))
    return entries


def _set_fat_entry_in_image(
    raw: bytearray, fat_type: int, fat_offset: int, cluster: int, value: int
) -> None:
    if fat_type == 12:
        offset = fat_offset + (cluster * 3) // 2
        current = struct.unpack_from("<H", raw, offset)[0]
        if cluster & 1:
            current = (current & 0x000F) | ((value & 0x0FFF) << 4)
        else:
            current = (current & 0xF000) | (value & 0x0FFF)
        struct.pack_into("<H", raw, offset, current)
    elif fat_type == 16:
        struct.pack_into("<H", raw, fat_offset + cluster * 2, value)
    else:
        struct.pack_into("<I", raw, fat_offset + cluster * 4, value & 0x0FFFFFFF)


def _make_fat1216_image(path, fat_type: int) -> None:
    """Build a structurally complete FAT12/16 image with the shared content set."""
    geo = _GEOMETRY[fat_type]
    bps, spc = 512, 1
    total_sectors, reserved = geo["total_sectors"], geo["reserved"]
    fat_sectors, root_entries = geo["fat_sectors"], geo["root_entries"]
    root_sectors = (root_entries * 32) // bps
    data_start = (reserved + fat_sectors + root_sectors) * bps
    image = bytearray(total_sectors * bps)

    boot = bytearray(bps)
    boot[:3] = b"\xeb\x3c\x90"
    boot[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", boot, 11, bps)
    boot[13] = spc
    struct.pack_into("<H", boot, 14, reserved)
    boot[16] = 1
    struct.pack_into("<H", boot, 17, root_entries)
    struct.pack_into("<H", boot, 19, total_sectors)
    boot[21] = 0xF8
    struct.pack_into("<H", boot, 22, fat_sectors)
    boot[54:62] = f"FAT{fat_type}   ".encode()
    boot[510:512] = b"\x55\xaa"
    image[:bps] = boot

    fat = bytearray(fat_sectors * bps)

    def set_fat(cluster: int, value: int) -> None:
        _set_fat_entry_in_image(fat, fat_type, 0, cluster, value)

    if fat_type == 12:
        set_fat(0, 0xFF8)  # media descriptor word
        set_fat(1, 0xFFF)
    else:
        set_fat(0, 0xFFF8)
        set_fat(1, 0xFFFF)

    next_cluster = 2

    def alloc(content: bytes) -> int:
        nonlocal next_cluster
        count = (len(content) + _CLUSTER_SIZE - 1) // _CLUSTER_SIZE
        if not count:
            return 0
        start = next_cluster
        for index in range(count):
            if index == count - 1:
                set_fat(start + index, _EOC[fat_type])
            else:
                set_fat(start + index, start + index + 1)
        offset = data_start + (start - 2) * _CLUSTER_SIZE
        image[offset : offset + count * _CLUSTER_SIZE] = content.ljust(
            count * _CLUSTER_SIZE, b"\x00"
        )
        next_cluster += count
        return start

    hello_cluster = alloc(_HELLO)
    long_cluster = alloc(_LONG_PAYLOAD)
    big_cluster = alloc(_BIG)
    note_cluster = alloc(_NOTE)
    docs_cluster = next_cluster  # peek: alloc() below takes exactly this slot
    sub = bytearray(_CLUSTER_SIZE)
    sub[0:32] = _pack_short_entry(b".          ", 0x10, 0, docs_cluster)
    sub[32:64] = _pack_short_entry(b"..         ", 0x10, 0, 0)
    sub[64:96] = _pack_short_entry(b"NOTE    TXT", 0x20, len(_NOTE), note_cluster)
    docs_cluster = alloc(bytes(sub))

    root = bytearray(root_entries * 32)
    position = 0
    root[position : position + 32] = _pack_short_entry(
        b"HELLO   TXT", 0x20, len(_HELLO), hello_cluster
    )
    position += 32
    for entry in _lfn_dir_entries(_LONG_NAME, _LONG_SHORT):
        root[position : position + 32] = entry
        position += 32
    root[position : position + 32] = _pack_short_entry(
        _LONG_SHORT, 0x20, len(_LONG_PAYLOAD), long_cluster
    )
    position += 32
    root[position : position + 32] = _pack_short_entry(b"EMPTY   BIN", 0x20, 0, 0)
    position += 32
    root[position : position + 32] = _pack_short_entry(
        b"BIG     BIN", 0x20, len(_BIG), big_cluster
    )
    position += 32
    root[position : position + 32] = _pack_short_entry(
        b"DOCS       ", 0x10, 0, docs_cluster
    )
    root_offset = (reserved + fat_sectors) * bps
    image[root_offset : root_offset + len(root)] = root
    image[reserved * bps : reserved * bps + len(fat)] = fat
    Path(path).write_bytes(bytes(image))


def _build_fat_image(path, fat_type: int) -> dict:
    """Build a valid image for any FAT variant and return its cluster map."""
    if fat_type == 32:
        make_fat32_image(path)
        with path.open("r+b") as stream:
            writer = Fat32(stream)
            writer.write_file("/", "HELLO.TXT", _HELLO)
            writer.write_file("/", _LONG_NAME, _LONG_PAYLOAD)
            writer.write_file("/", "EMPTY.BIN", b"")
            writer.write_file("/", "BIG.BIN", _BIG)
            writer.mkdir("/", "DOCS")
            writer.write_file("/DOCS", "NOTE.TXT", _NOTE)
    else:
        _make_fat1216_image(path, fat_type)
    provider = NativeFatImage(str(path))
    try:
        return {
            "HELLO.TXT": provider._resolve_file("/HELLO.TXT").cluster,
            "BIG.BIN": provider._resolve_file("/BIG.BIN").cluster,
        }
    finally:
        provider.close()


def _apply_damage(path, fat_type: int, spec: dict, kind: str) -> None:
    raw = bytearray(Path(path).read_bytes())
    if kind == "boot-signature":
        raw[510:512] = b"\x00\x00"
    elif kind == "truncated":
        raw = raw[:-512]
    elif kind == "chain-cycle":
        _set_fat_entry_in_image(
            raw, fat_type, _FAT_OFFSET[fat_type], spec["BIG.BIN"], spec["BIG.BIN"]
        )
    elif kind == "free-link":
        _set_fat_entry_in_image(
            raw, fat_type, _FAT_OFFSET[fat_type], spec["BIG.BIN"], 0
        )
    else:  # pragma: no cover - guard against matrix typos
        raise AssertionError(kind)
    Path(path).write_bytes(bytes(raw))


class _ReferenceFat:
    """Minimal independent FAT12/16/32 oracle used only by the matrix tests."""

    _EOC = {12: 0xFF8, 16: 0xFFF8, 32: 0x0FFFFFF8}

    def __init__(self, path):
        self.raw = Path(path).read_bytes()
        r = self.raw
        self.bps = int.from_bytes(r[11:13], "little")
        self.spc = r[13]
        self.reserved = int.from_bytes(r[14:16], "little")
        self.nfats = r[16]
        self.root_entries = int.from_bytes(r[17:19], "little")
        self.total = int.from_bytes(r[19:21], "little") or int.from_bytes(
            r[32:36], "little"
        )
        fat16 = int.from_bytes(r[22:24], "little")
        fat32 = int.from_bytes(r[36:40], "little")
        self.fat_sectors = fat16 or fat32
        self.fat_start = self.reserved * self.bps
        self.root_start = self.fat_start + self.nfats * self.fat_sectors * self.bps
        self.data_start = self.root_start + self.root_entries * 32
        self.cluster_size = self.spc * self.bps
        self.cluster_count = (
            self.total * self.bps - self.data_start
        ) // self.cluster_size
        self.fat_type = (
            32
            if self.cluster_count >= 65525
            else 16 if self.cluster_count >= 4085 else 12
        )
        self.root_cluster = (
            int.from_bytes(r[44:48], "little") & 0x0FFFFFFF
            if self.fat_type == 32
            else None
        )

    def _fat(self, cluster: int) -> int:
        r, base = self.raw, self.fat_start
        if self.fat_type == 12:
            value = int.from_bytes(r[base + (cluster * 3) // 2 :][:2], "little")
            return (value >> 4) if cluster & 1 else (value & 0xFFF)
        if self.fat_type == 16:
            return int.from_bytes(r[base + cluster * 2 :][:2], "little")
        return int.from_bytes(r[base + cluster * 4 :][:4], "little") & 0x0FFFFFFF

    def _chain(self, start: int) -> list[int]:
        chain = []
        while True:
            chain.append(start)
            value = self._fat(start)
            if value >= self._EOC[self.fat_type]:
                return chain
            start = value

    def _cluster_data(self, cluster: int) -> bytes:
        offset = self.data_start + (cluster - 2) * self.cluster_size
        return self.raw[offset : offset + self.cluster_size]

    def _dir_entries(self, cluster: int | None) -> list[dict]:
        if cluster is None:
            raw = self.raw[self.root_start : self.root_start + self.root_entries * 32]
        else:
            raw = b"".join(self._cluster_data(item) for item in self._chain(cluster))
        out: list[dict] = []
        lfn: list[bytes] = []
        for position in range(0, len(raw), 32):
            entry = raw[position : position + 32]
            if entry[0] == 0x00:
                break
            if entry[0] == 0xE5:
                lfn = []
                continue
            if entry[11] == 0x0F:
                lfn.append(entry)
                continue
            if entry[11] & 0x08:
                lfn = []
                continue
            base = entry[:8].decode("cp437", "replace").rstrip(" ")
            ext = entry[8:11].decode("cp437", "replace").rstrip(" ")
            name = f"{base}.{ext}" if ext else base
            if lfn:
                units = []
                for unit_entry in reversed(lfn):
                    units += [
                        int.from_bytes(unit_entry[spot : spot + 2], "little")
                        for spot in (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30)
                    ]
                name = "".join(chr(u) for u in units if u not in (0x0000, 0xFFFF))
                lfn = []
            start = int.from_bytes(entry[26:28], "little")
            if self.fat_type == 32:
                start |= int.from_bytes(entry[20:22], "little") << 16
            out.append(
                {
                    "name": name,
                    "is_dir": bool(entry[11] & 0x10),
                    "size": int.from_bytes(entry[28:32], "little"),
                    "cluster": start,
                }
            )
        return [item for item in out if item["name"] not in (".", "..")]

    def _resolve(self, path: str) -> dict:
        parts = [part for part in path.split("/") if part]
        cluster = self.root_cluster
        entries = self._dir_entries(cluster)
        for index, part in enumerate(parts):
            match = next(
                item for item in entries if item["name"].lower() == part.lower()
            )
            if index == len(parts) - 1 and not match["is_dir"]:
                return match
            cluster = match["cluster"]
            entries = self._dir_entries(cluster)
        raise FileNotFoundError(path)

    def read(self, path: str) -> bytes:
        entry = self._resolve(path)
        if not entry["size"]:
            return b""
        data = b"".join(
            self._cluster_data(item) for item in self._chain(entry["cluster"])
        )
        return data[: entry["size"]]

    def list_dir(self, path: str = "/") -> list[dict]:
        cluster = self.root_cluster
        for part in (item for item in path.split("/") if item):
            match = next(
                item
                for item in self._dir_entries(cluster)
                if item["name"].lower() == part.lower()
            )
            cluster = match["cluster"]
        return self._dir_entries(cluster)


@pytest.mark.parametrize("fat_type", _FAT_TYPES)
def test_differential_matrix_native_matches_reference(tmp_path, fat_type):
    image = tmp_path / f"fat{fat_type}-parity.img"
    _build_fat_image(image, fat_type)
    reference = _ReferenceFat(image)
    assert reference.fat_type == fat_type
    provider = NativeFatImage(str(image))
    try:
        assert provider.fat_type == fat_type
        assert provider.backend_name == f"native-fat{fat_type}-ro"
        native_root = {
            (item["name"], item["is_dir"], item["size"])
            for item in provider.list_dir("/")
        }
        reference_root = {
            (item["name"], item["is_dir"], item["size"])
            for item in reference.list_dir("/")
        }
        assert native_root == reference_root
        for path, payload in _FILES.items():
            assert provider.file_size(path) == len(payload)
            assert provider.stat(path)["size"] == len(payload)
            assert reference.read(path) == payload
            assert provider.read_file(path) == payload
            for offset in (
                0,
                3,
                provider.cluster_size - 1,
                provider.cluster_size,
                2 * provider.cluster_size + 5,
            ):
                expected = (
                    payload[offset : offset + 13] if offset < len(payload) else b""
                )
                assert provider.read_file_range(path, offset, 13) == expected
        native_docs = {
            (item["name"], item["is_dir"], item["size"])
            for item in provider.list_dir("/DOCS")
        }
        reference_docs = {
            (item["name"], item["is_dir"], item["size"])
            for item in reference.list_dir("/DOCS")
        }
        assert native_docs == reference_docs
    finally:
        provider.close()


def _forbid_legacy_fallback(monkeypatch) -> None:
    def _forbidden(*args, **kwargs):
        raise AssertionError("legacy pytsk3 fallback must not run for FAT profiles")

    monkeypatch.setattr(vfs, "TskImage", _forbidden)
    monkeypatch.setattr(vfs, "legacy_tsk_enabled", lambda: True)


@pytest.mark.parametrize("fat_type", _FAT_TYPES)
def test_routing_bounded_fat_profiles_go_native_without_fallback(
    tmp_path, monkeypatch, fat_type
):
    image = tmp_path / f"fat{fat_type}-route.img"
    _build_fat_image(image, fat_type)
    _forbid_legacy_fallback(monkeypatch)
    for volume_size in (None, image.stat().st_size):
        provider = vfs.open_image_provider(str(image), volume_size=volume_size)
        try:
            assert provider.backend_name == f"native-fat{fat_type}-ro"
            assert provider.read_file("/HELLO.TXT") == _HELLO
        finally:
            provider.close()


@pytest.mark.parametrize("fat_type", _FAT_TYPES)
def test_cli_get_extracts_native_fat12_16_32_without_legacy_fallback(
    tmp_path, monkeypatch, fat_type
):
    image = tmp_path / f"fat{fat_type}-cli-get.img"
    _build_fat_image(image, fat_type)
    _forbid_legacy_fallback(monkeypatch)
    output = tmp_path / f"fat{fat_type}-extracted"

    assert (
        cli.main(["get", str(image), "/HELLO.TXT", "--output", str(output)]) == cli.OK
    )
    assert (output / "HELLO.TXT").read_bytes() == _HELLO


@pytest.mark.parametrize("fat_type", _FAT_TYPES)
@pytest.mark.parametrize("kind", ["boot-signature", "truncated"])
def test_routing_refuses_damaged_fat_profiles(tmp_path, fat_type, kind):
    image = tmp_path / f"fat{fat_type}-{kind}.img"
    spec = _build_fat_image(image, fat_type)
    _apply_damage(image, fat_type, spec, kind)
    from mountfile.vfs import RecognizedFilesystemError

    with pytest.raises(RecognizedFilesystemError):
        vfs.open_image_provider(str(image))


@pytest.mark.parametrize("fat_type", _FAT_TYPES)
@pytest.mark.parametrize("kind", ["chain-cycle", "free-link"])
def test_damaged_chains_fail_closed_without_fallback(
    tmp_path, monkeypatch, fat_type, kind
):
    image = tmp_path / f"fat{fat_type}-{kind}.img"
    spec = _build_fat_image(image, fat_type)
    _apply_damage(image, fat_type, spec, kind)
    _forbid_legacy_fallback(monkeypatch)
    provider = vfs.open_image_provider(str(image))
    try:
        with pytest.raises(FatReadError, match="cycle|free, bad or reserved"):
            provider.read_file("/BIG.BIN")
        with pytest.raises(FatReadError, match="cycle|free, bad or reserved"):
            provider.read_file_range("/BIG.BIN", 0, 16)
    finally:
        provider.close()
