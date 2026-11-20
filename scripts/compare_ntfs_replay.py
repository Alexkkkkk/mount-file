# SPDX-License-Identifier: MIT
"""Compare bounded native NTFS replay with a Windows-checker result.

The command never mutates ``--crash-image`` or ``--chkdsk-image``. It copies the
crash image, runs the explicitly bounded native replay on that copy, extracts
four validated metadata streams, and compares them byte-for-byte with the
checker-recovered image. A replay refusal is a typed blocked result, not a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

from mountfile.ntfs_replay import NtfsReplayError, native_ntfs_recover
from scripts.compare_ntfs_snapshots import compare_file
from scripts.windows_ntfs_raw_snapshot import snapshot_image


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compare_replay(
    crash_image: Path, chkdsk_image: Path, native_image: Path, output: Path
) -> dict[str, object]:
    if not crash_image.is_file() or not chkdsk_image.is_file():
        raise FileNotFoundError("crash and chkdsk raw images are required")
    native_image.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(crash_image, native_image)
    result: dict[str, object] = {
        "schema": "mountfile.windows-ntfs-replay-comparison/v1",
        "crash_image": {"path": str(crash_image), "sha256": _sha256(crash_image)},
        "chkdsk_image": {"path": str(chkdsk_image), "sha256": _sha256(chkdsk_image)},
        "native_image": {"path": str(native_image)},
        "status": "blocked",
        "claims": [
            "The native replay is run only on a disposable copy of the crash image.",
            "A pass requires byte-identical MFT, MFTMirr, BITMAP and LogFile streams.",
            "This does not claim product NTFS write support or power-loss durability.",
        ],
    }
    try:
        replay = native_ntfs_recover(str(native_image))
        result["replay"] = replay.as_dict()
        native_dir = output.with_name(output.name + ".native-snapshot")
        checker_dir = output.with_name(output.name + ".chkdsk-snapshot")
        snapshot_image(native_image, native_dir)
        snapshot_image(chkdsk_image, checker_dir)
        streams = {
            name: compare_file(native_dir / filename, checker_dir / filename)
            for name, filename in {
                "mft": "MFT.bin",
                "mftmirr": "MFTMirr.bin",
                "bitmap": "BITMAP.bin",
                "logfile": "LogFile.bin",
            }.items()
        }
        result["streams"] = streams
        result["status"] = (
            "pass"
            if all(item["byte_identical"] for item in streams.values())
            else "mismatch"
        )
    except (NtfsReplayError, OSError, ValueError) as exc:
        result["error_type"] = type(exc).__name__
        result["error"] = str(exc)
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crash-image", type=Path, required=True)
    parser.add_argument("--chkdsk-image", type=Path, required=True)
    parser.add_argument("--native-image", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = compare_replay(
        args.crash_image, args.chkdsk_image, args.native_image, args.out
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
