# SPDX-License-Identifier: MIT
"""Compare bounded native NTFS replay with a Windows-checker result.

The command never mutates ``--crash-image`` or ``--chkdsk-image``. It copies the
crash image, runs the explicitly bounded native replay on that copy, extracts
four validated metadata streams, and compares them byte-for-byte with the
checker-recovered image. A replay refusal is a typed blocked result, not a pass.

Typed outcomes:

* ``pass`` - every stream is byte-identical with the checker-recovered image;
* ``not_applicable`` - the crash ``$LogFile`` carries no restart page, so the
  bounded replay fabricates no records and byte identity with ``chkdsk /f``
  (which performs its own heavier repair) is not a replay-parity claim. The
  comparator still proves that replay invented nothing and reports the raw
  stream diffs as provenance evidence;
* ``mismatch`` - replay applied records but the streams differ: this is a
  replay-parity bug and stays a hard failure (exit 2);
* ``blocked`` - the replay refused the image with a typed refusal (exit 2).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

# When launched as ``python scripts/compare_ntfs_replay.py`` Python puts only
# ``scripts/`` on sys.path. Add the repository root before importing the
# production package so the documented direct CLI form works from any cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mountfile.ntfs_logfile import NtfsLogFileError
from mountfile.ntfs_replay import NtfsReplayError, native_ntfs_recover

try:  # Works both as ``python -m scripts...`` and as a direct file on Windows.
    from scripts.compare_ntfs_snapshots import compare_file
    from scripts.windows_ntfs_raw_snapshot import snapshot_image
except ModuleNotFoundError:  # pragma: no cover - exercised by direct CLI launch
    from compare_ntfs_snapshots import compare_file
    from windows_ntfs_raw_snapshot import snapshot_image


STREAMS = {
    "mft": "MFT.bin",
    "mftmirr": "MFTMirr.bin",
    "bitmap": "BITMAP.bin",
    "logfile": "LogFile.bin",
}

NOT_APPLICABLE_REASON = (
    "The crash $LogFile carries no restart page: the bounded replay fabricates "
    "no records, so byte identity with chkdsk /f is not a replay-parity claim. "
    "Stream diffs below are provenance evidence only."
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replay_fabricated_nothing(replay: dict[str, object]) -> bool:
    """True when the bounded replay performed no record or stream mutation."""
    counters = ("records_written", "redo_applied", "undo_applied", "stream_writes")
    notes = replay.get("notes") or []
    return (
        replay.get("clean_restart_pages") == 0
        and all(replay.get(key) == 0 for key in counters)
        and any("no restart page" in str(note) for note in notes)
    )


def replay_comparison_status(
    replay: dict[str, object], streams: dict[str, dict[str, object]]
) -> str:
    """Classify the comparison without ever masking a real parity bug."""
    if _replay_fabricated_nothing(replay):
        return "not_applicable"
    if all(item["byte_identical"] for item in streams.values()):
        return "pass"
    return "mismatch"


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
            "not_applicable means the crash $LogFile had no restart page and the bounded replay invented nothing; it is not a parity claim.",
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
            for name, filename in STREAMS.items()
        }
        result["streams"] = streams
        status = replay_comparison_status(replay.as_dict(), streams)
        result["status"] = status
        if status == "not_applicable":
            result["not_applicable_reason"] = NOT_APPLICABLE_REASON
    except (NtfsLogFileError, NtfsReplayError, OSError, ValueError) as exc:
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
    return 0 if result["status"] in ("pass", "not_applicable") else 2


if __name__ == "__main__":
    raise SystemExit(main())
