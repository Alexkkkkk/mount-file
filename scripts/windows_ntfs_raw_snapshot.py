# SPDX-License-Identifier: MIT
r"""Extract bounded raw NTFS metadata streams from a Windows volume.

The command is intended for an elevated Windows CI runner with a physical
volume handle (for example ``\\.\PhysicalDrive7``). It uses MountFile's
validated boot/MFT/runlist parser, then writes the exact stream bytes and a
JSON manifest. It refuses sparse/ambiguous metadata and never mutates the
volume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

from mountfile.image_source import WindowsRawDeviceImageSource
from mountfile.ntfs_reader import NativeNtfsMetadataImage, NtfsMetadataError

_MAX_RAW_VOLUME_BYTES = 128 << 30
_MAX_SIGNED_OFFSET = (1 << 63) - 1
_SNAPSHOT_CHUNK_BYTES = 1 << 20


def _validate_device_window(offset: int, volume_size: int) -> int:
    if (
        not isinstance(offset, int)
        or isinstance(offset, bool)
        or offset < 0
        or not isinstance(volume_size, int)
        or isinstance(volume_size, bool)
        or volume_size <= 0
        or volume_size > _MAX_RAW_VOLUME_BYTES
    ):
        raise NtfsMetadataError("raw snapshot offset or volume size is invalid")
    if offset > _MAX_SIGNED_OFFSET - volume_size:
        raise NtfsMetadataError("raw snapshot device window overflows signed offsets")
    return offset + volume_size


def _validate_sector_size(sector_size: int) -> int:
    if (
        type(sector_size) is not int
        or sector_size < 512
        or sector_size > (1 << 20)
        or sector_size & (sector_size - 1)
    ):
        raise NtfsMetadataError("raw snapshot physical sector size is invalid")
    return sector_size


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_stream(
    image: NativeNtfsMetadataImage, record_number: int, name: str, output: Path
) -> int:
    record = image.read_mft_record(record_number)
    attributes = image._attributes_for_record(record_number, record)  # noqa: SLF001
    streams = [
        item for item in attributes if item.type_code == 0x80 and item.name == name
    ]
    if len(streams) != 1:
        raise NtfsMetadataError(
            f"expected exactly one unnamed DATA stream on MFT record {record_number}"
        )
    attribute = streams[0]
    if not attribute.nonresident or attribute.data_size <= 0:
        raise NtfsMetadataError(
            "metadata DATA stream is not a readable nonresident stream"
        )
    temporary = output.with_name(output.name + ".tmp")
    written = 0
    try:
        with temporary.open("wb") as stream:
            while written < attribute.data_size:
                length = min(_SNAPSHOT_CHUNK_BYTES, attribute.data_size - written)
                chunk = image._read_nonresident_range(  # noqa: SLF001
                    attribute, written, length
                )
                if len(chunk) != length:
                    raise NtfsMetadataError(
                        "short read while extracting NTFS metadata stream"
                    )
                if stream.write(chunk) != length:
                    raise OSError("short write while saving NTFS metadata snapshot")
                written += length
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return written


def _copy_volume(source, offset: int, volume_size: int, output: Path) -> int:
    """Copy the bounded volume window without reading outside its partition."""
    temporary = output.with_name(output.name + ".tmp")
    written = 0
    try:
        with temporary.open("wb") as stream:
            while written < volume_size:
                length = min(_SNAPSHOT_CHUNK_BYTES, volume_size - written)
                chunk = source.read_at(offset + written, length)
                if len(chunk) != length:
                    raise NtfsMetadataError("short read while saving raw volume image")
                stream.write(chunk)
                written += length
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return written


def snapshot(
    device: str,
    offset: int,
    volume_size: int,
    output: Path,
    sector_size: int = 512,
    raw_image: Path | None = None,
) -> dict:
    source_size = _validate_device_window(offset, volume_size)
    sector_size = _validate_sector_size(sector_size)
    output.mkdir(parents=True, exist_ok=True)

    def source_factory():
        return WindowsRawDeviceImageSource(
            device,
            size=source_size,
            lower_bound=offset,
            sector_size=sector_size,
        )

    image = NativeNtfsMetadataImage(
        device,
        offset=offset,
        volume_size=volume_size,
        source_factory=source_factory,
        allow_dirty_read=True,
    )
    try:
        paths = {
            "mft": output / "MFT.bin",
            "mftmirr": output / "MFTMirr.bin",
            "bitmap": output / "BITMAP.bin",
            "logfile": output / "LogFile.bin",
        }
        _write_stream(image, 0, "", paths["mft"])
        _write_stream(image, 1, "", paths["mftmirr"])
        _write_stream(image, 6, "", paths["bitmap"])
        _write_stream(image, 2, "", paths["logfile"])
    finally:
        image.close()
    if raw_image is not None:
        raw_image.parent.mkdir(parents=True, exist_ok=True)
        source = source_factory()
        try:
            _copy_volume(source, offset, volume_size, raw_image)
        finally:
            source.close()
    manifest = {
        "schema": "mountfile.windows-ntfs-raw-snapshot/v1",
        "device": device,
        "offset": offset,
        "volume_size": volume_size,
        "physical_sector_size": sector_size,
        "streams": {
            key: {
                "path": path.name,
                "size": path.stat().st_size,
                "sha256": _digest(path),
            }
            for key, path in paths.items()
        },
        "raw_image": (
            {
                "path": raw_image.name,
                "size": raw_image.stat().st_size,
                "sha256": _digest(raw_image),
            }
            if raw_image is not None
            else None
        ),
        "claims": [
            "These are exact bytes read from validated NTFS nonresident streams.",
            "This is metadata evidence, not proof of replay parity by itself.",
            "$MFTMirr is captured independently so mirror identity can be compared.",
            "$LogFile is captured to preserve replay provenance; it is not replayed by this snapshot tool.",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def snapshot_image(image_path: str | Path, output: Path) -> dict:
    """Extract the same streams from a detached raw volume image."""
    image_path = Path(image_path)
    volume_size = image_path.stat().st_size
    output.mkdir(parents=True, exist_ok=True)
    image = NativeNtfsMetadataImage(
        image_path,
        offset=0,
        volume_size=volume_size,
        allow_dirty_read=True,
    )
    try:
        paths = {
            "mft": output / "MFT.bin",
            "mftmirr": output / "MFTMirr.bin",
            "bitmap": output / "BITMAP.bin",
            "logfile": output / "LogFile.bin",
        }
        _write_stream(image, 0, "", paths["mft"])
        _write_stream(image, 1, "", paths["mftmirr"])
        _write_stream(image, 6, "", paths["bitmap"])
        _write_stream(image, 2, "", paths["logfile"])
    finally:
        image.close()
    return {
        "schema": "mountfile.windows-ntfs-raw-snapshot/v1",
        "source": str(image_path),
        "volume_size": volume_size,
        "streams": {
            key: {
                "path": path.name,
                "size": path.stat().st_size,
                "sha256": _digest(path),
            }
            for key, path in paths.items()
        },
        "claims": [
            "These are exact bytes read from a detached raw NTFS volume image.",
            "This is metadata evidence, not proof of replay parity by itself.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--offset", type=int, required=True)
    parser.add_argument("--volume-size", type=int, required=True)
    parser.add_argument("--sector-size", type=int, default=512)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--raw-image", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            snapshot(
                args.device,
                args.offset,
                args.volume_size,
                args.out,
                args.sector_size,
                args.raw_image,
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
