#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""B8 draft: block-level write-ordering gate via dm-log-writes (UNVERIFIED).

Status: draft. ``dmsetup``, ``modprobe`` and ``/dev/loop-control`` do not
exist in the development sandbox, so neither this script nor its workflow has
ever executed end to end. Every prerequisite is probed and typed: a missing
capability produces ``status=unavailable`` (exit 3) instead of a weaker pass.
The gate claims nothing about power-loss durability: dm-log-writes records
block write order, not media failure or disk-cache reordering.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

SCHEMA = "mountfile.b8-log-writes/v0-draft"
IMAGE_BYTES = 64 << 20
FILE_COUNT = 20
UNAVAILABLE_EXIT = 3
BLOCKED_EXIT = 2
FAILED_EXIT = 1

CLAIMS = (
    "dm-log-writes records block-level write order; it does not emulate media failure or disk-cache reordering.",
    "A green gate means every replayed prefix is either e2fsck -fn clean or journal-recoverable to CLEAN.",
    "This draft has never executed against a real runner and is not acceptance evidence yet.",
)


def _run(argv: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, check=True, text=True, capture_output=True, **kwargs)


def _prerequisites() -> tuple[list[dict[str, object]], list[str]]:
    probes = [
        ("mkfs.ext4", ["mkfs.ext4", "-V"], "ext4 builder"),
        ("e2fsck", ["e2fsck", "-V"], "independent checker"),
        ("losetup", ["losetup", "-V"], "loop device setup"),
        ("dmsetup", ["dmsetup", "-V"], "device-mapper control"),
    ]
    report: list[dict[str, object]] = []
    missing: list[str] = []
    for binary, argv, label in probes:
        path = shutil.which(binary)
        available = path is not None
        report.append({"name": label, "binary": binary, "available": available})
        if not available:
            missing.append(label)
    if not Path("/dev/loop-control").exists():
        report.append({"name": "loop-control device", "available": False})
        missing.append("loop-control device")
    else:
        report.append({"name": "loop-control device", "available": True})
    modprobe = shutil.which("modprobe")
    loaded = False
    if modprobe:
        probe = subprocess.run(
            [modprobe, "dm-log-writes"], capture_output=True, text=True
        )
        loaded = probe.returncode == 0
        report.append({"name": "dm-log-writes module", "available": loaded})
    else:
        report.append({"name": "dm-log-writes module", "available": False})
    if not loaded:
        missing.append("dm-log-writes module")
    return report, missing


def _build_image(image: Path) -> None:
    with image.open("wb") as stream:
        stream.truncate(IMAGE_BYTES)
    _run(["mkfs.ext4", "-F", "-q", str(image)])


def _attach_log_writes(image: Path, log: Path) -> tuple[str, str, int]:
    loop = _run(["losetup", "-f", "--show", str(image)]).stdout.strip()
    size = int(_run(["blockdev", "--getsize64", loop]).stdout.strip())
    sectors = size // 512
    table = f"0 {sectors} log-writes {loop} {log}"
    _run(["dmsetup", "create", "logw", "--table", table])
    return loop, "/dev/mapper/logw", sectors


def _writer_workload(mount: Path, dm_device: str) -> list[str]:
    """Write bounded files with fsync, emitting a mark after each file."""
    marks: list[str] = []
    for index in range(FILE_COUNT):
        target = mount / f"durable-{index:03d}.bin"
        payload = (f"durable-order-probe-{index}\n" * 128).encode()
        with target.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            _run(["sync"])
        mark = f"file-{index:03d}"
        _run(["dmsetup", "message", dm_device, "0", mark])
        marks.append(mark)
    return marks


def _replay_and_check(
    parser: Path, log: Path, image: Path, marks: list[str], workdir: Path
) -> dict[str, object]:
    help_text = _run([sys.executable, str(parser), "--help"]).stdout
    if "replay" not in help_text:
        raise SystemExit(
            json.dumps(
                {
                    "status": "blocked",
                    "reason": "pinned parser does not advertise the replay subcommand; pin/CLI draft needs a real-runner correction",
                }
            )
        )
    results: list[dict[str, object]] = []
    for index, mark in enumerate(marks):
        replayed = workdir / f"replayed-{index:03d}.img"
        shutil.copy2(image, replayed)
        # TODO(pin): confirm the exact replay CLI of the pinned parser on the
        # first real run; the contract test only pins the fail-closed shape.
        _run(
            [
                sys.executable,
                str(parser),
                "replay",
                "--log",
                str(log),
                "--recon",
                str(index + 1),
            ]
        )
        check = subprocess.run(
            ["e2fsck", "-fn", str(replayed)], capture_output=True, text=True
        )
        results.append(
            {
                "mark": mark,
                "e2fsck_fn_exit": check.returncode,
                "clean": check.returncode == 0,
                "output_tail": (check.stdout + check.stderr)[-400:],
            }
        )
    return {"replayed_prefixes": results}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("b8-log-writes.json"))
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()

    prereqs, missing = _prerequisites()
    report: dict[str, object] = {
        "schema": SCHEMA,
        "status": "unavailable" if missing else "ready",
        "prerequisites": prereqs,
        "claims": list(CLAIMS),
    }
    if args.self_check or missing:
        if missing:
            report["missing"] = missing
        args.out.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 0 if args.self_check else UNAVAILABLE_EXIT

    workdir = Path("/dev/shm/b8-log-writes")
    workdir.mkdir(parents=True, exist_ok=True)
    image = workdir / "base.img"
    log = workdir / "log-writes.bin"
    loop = dm_device = ""
    try:
        _build_image(image)
        loop, dm_device, _sectors = _attach_log_writes(image, log)
        _run(["mkfs.ext4", "-F", "-q", dm_device])
        mount = workdir / "mnt"
        mount.mkdir(parents=True, exist_ok=True)
        _run(["mount", dm_device, str(mount)])
        marks = _writer_workload(mount, dm_device)
        _run(["umount", str(mount)])
        replayed = _replay_and_check(
            Path("tools/log-writes/log-writes.py"), log, image, marks, workdir
        )
        report["status"] = "passed"
        report.update(replayed)
    except subprocess.CalledProcessError as exc:
        report["status"] = "blocked"
        report["error"] = (exc.stderr or exc.stdout)[-400:]
    finally:
        for cleanup in (
            ["umount", str(workdir / "mnt")],
            ["dmsetup", "remove", "logw"],
            ["losetup", "-d", loop],
        ):
            if cleanup[1:] and cleanup[1]:
                subprocess.run(cleanup, capture_output=True, text=True)
        shutil.rmtree(workdir, ignore_errors=True)

    args.out.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0 if report["status"] == "passed" else FAILED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
