# SPDX-License-Identifier: MIT
r"""Extract bounded raw NTFS $MFT and $BITMAP streams from a Windows volume.

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
from pathlib import Path

from mountfile.ntfs_reader import NativeNtfsMetadataImage, NtfsMetadataError


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream(image: NativeNtfsMetadataImage, record_number: int, name: str) -> bytes:
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
    return image._read_nonresident_range(
        attribute, 0, attribute.data_size
    )  # noqa: SLF001


def snapshot(device: str, offset: int, volume_size: int, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    image = NativeNtfsMetadataImage(device, offset=offset, volume_size=volume_size)
    try:
        mft = _stream(image, 0, "")
        bitmap = _stream(image, 6, "")
    finally:
        image.close()
    paths = {"mft": output / "MFT.bin", "bitmap": output / "BITMAP.bin"}
    paths["mft"].write_bytes(mft)
    paths["bitmap"].write_bytes(bitmap)
    manifest = {
        "schema": "mountfile.windows-ntfs-raw-snapshot/v1",
        "device": device,
        "offset": offset,
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
            "These are exact bytes read from validated NTFS nonresident streams.",
            "This is metadata evidence, not proof of replay parity by itself.",
        ],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--offset", type=int, required=True)
    parser.add_argument("--volume-size", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            snapshot(args.device, args.offset, args.volume_size, args.out), indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
