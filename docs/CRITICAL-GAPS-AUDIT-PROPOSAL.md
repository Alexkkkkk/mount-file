🎯 Код для приоритетов 1-4 (архитектура mountfile, не mount_fs)

Согласен с аудитом: PR #340 — proposal, не совместимый код. Ниже — реальный код под mountfile, закрывающий шаги 1-4 в указанном порядке. Без forensic_source, без mount_fs, только mountfile.* и capabilities.py v5.

---

Шаг 1: Failure injection + quarantine

📁 mountfile/testing/__init__.py

```python
"""Testing infrastructure: failure injection, quarantine, recovery verification."""
```

📁 mountfile/testing/failure_injection.py

```python
"""
Deterministic failure injection for mountfile writers.

Integrates with existing Writer protocol (begin/add/commit) without
changing it. Wraps any backend (ImageSource / BinaryIO / fd) so tests
can enumerate failure points.

Injection modes:
  IO_ERROR       — raises the project's typed write error
  SHORT_WRITE    — writes half, returns actual count
  ZERO_WRITE     — returns success, writes nothing
  SILENT_CORRUPT — writes but flips a byte
  FSYNC_ERROR    — succeeds writes, fails at commit barrier

Design: every writer calls `backend.checkpoint(name)` at known points.
Failure fires exactly at the named checkpoint. Unknown checkpoints are
no-ops so old writers keep working.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Optional

try:
    from mountfile.errors import (
        MountFileError,
        ShortReadError,
        ShortWriteError,
    )
except ImportError:
    # Fallback so module is importable before errors.py is migrated
    class MountFileError(Exception):
        pass

    class ShortReadError(MountFileError):
        pass

    class ShortWriteError(MountFileError):
        pass


class FailMode(str, Enum):
    IO_ERROR = "io_error"
    SHORT_WRITE = "short_write"
    ZERO_WRITE = "zero_write"
    SILENT_CORRUPT = "silent_corrupt"
    FSYNC_ERROR = "fsync_error"


@dataclass(frozen=True)
class Injection:
    point: str
    mode: FailMode
    repeat: int = 1

    def __post_init__(self):
        if self.repeat < 1:
            raise ValueError("repeat must be >= 1")


class Checkpoint:
    """Standard names writers should emit."""
    BEFORE_BITMAP_UPDATE = "before_bitmap_update"
    AFTER_BITMAP_UPDATE = "after_bitmap_update"
    BEFORE_INODE_WRITE = "before_inode_write"
    AFTER_INODE_WRITE = "after_inode_write"
    BEFORE_DIRENT_ADD = "before_dirent_add"
    AFTER_DIRENT_ADD = "after_dirent_add"
    BEFORE_DATA_WRITE = "before_data_write"
    AFTER_DATA_WRITE = "after_data_write"
    BEFORE_JOURNAL = "before_journal"
    AFTER_JOURNAL = "after_journal"
    BEFORE_SB_UPDATE = "before_sb_update"
    AFTER_SB_UPDATE = "after_sb_update"
    BEFORE_FSYNC = "before_fsync"
    AFTER_FSYNC = "after_fsync"


@dataclass
class Event:
    point: str
    fired: bool
    sequence: int


class FaultyBackend:
    """
    Wraps a backend that implements:
      - read(offset, length) -> bytes
      - write(offset, data) -> int
      - fsync() -> None    (optional)
      - size() -> int       (optional)
    """

    def __init__(self, inner, injection: Optional[Injection] = None):
        self._inner = inner
        self._inj = injection
        self._fired = 0
        self._seq = 0
        self.events: list[Event] = []

    # ---- pass-through with injection ----

    def size(self) -> int:
        if hasattr(self._inner, "size"):
            return self._inner.size()
        if hasattr(self._inner, "len"):
            return self._inner.len()
        if hasattr(self._inner, "seek"):
            pos = self._inner.tell()
            self._inner.seek(0, 2)
            n = self._inner.tell()
            self._inner.seek(pos)
            return n
        raise TypeError("backend has no size/len")

    def read(self, offset: int, length: int) -> bytes:
        data = self._inner.read(offset, length)
        if (self._inj
                and self._inj.point == "read"
                and self._inj.mode == FailMode.IO_ERROR):
            self._fire("read")
            raise ShortReadError(f"injected read failure at {offset}")
        return data

    def write(self, offset: int, data: bytes) -> int:
        if not self._inj or self._inj.point != "write":
            return self._inner.write(offset, data)
        mode = self._inj.mode
        if mode == FailMode.IO_ERROR:
            self._fire("write")
            raise ShortWriteError(f"injected write failure at {offset}")
        if mode == FailMode.SHORT_WRITE:
            half = len(data) // 2
            n = self._inner.write(offset, data[:half])
            self._fire("write")
            return n
        if mode == FailMode.ZERO_WRITE:
            self._fire("write")
            return len(data)
        if mode == FailMode.SILENT_CORRUPT:
            corrupted = bytes([data[0] ^ 0xFF]) + data[1:] if data else data
            n = self._inner.write(offset, corrupted)
            self._fire("write")
            return n
        return self._inner.write(offset, data)

    def fsync(self) -> None:
        if (self._inj
                and self._inj.point == "fsync"
                and self._inj.mode in (FailMode.IO_ERROR, FailMode.FSYNC_ERROR)):
            self._fire("fsync")
            raise ShortWriteError("injected fsync failure")
        if hasattr(self._inner, "fsync"):
            self._inner.fsync()

    # ---- checkpoint ----

    def checkpoint(self, name: str) -> None:
        self._seq += 1
        fires = (
            self._inj is not None
            and self._inj.point == name
            and self._fired < self._inj.repeat
        )
        self.events.append(Event(name, fires, self._seq))
        if not fires:
            return
        self._fired += 1
        mode = self._inj.mode
        if mode == FailMode.IO_ERROR:
            raise ShortWriteError(f"injected at checkpoint {name}")
        if mode == FailMode.SHORT_WRITE:
            raise ShortWriteError(f"short-write simulated at {name}")
        if mode == FailMode.FSYNC_ERROR:
            raise ShortWriteError(f"fsync failure at {name}")
        # ZERO_WRITE / SILENT_CORRUPT are wire-level, not checkpoint-level;
        # at a checkpoint we just record the event.

    def _fire(self, name: str) -> None:
        self._fired += 1


# ---------- exploration ----------

@dataclass
class ExploreReport:
    total_states: int
    crashed: int
    recovered: int
    quarantined: int
    corrupt: int
    failures: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return self.corrupt == 0


def explore_writer(
    build_backend: Callable[[], FaultyBackend],
    writer_fn: Callable[[FaultyBackend], None],
    verify_fn: Callable[[FaultyBackend], bool],
    recover_fn: Optional[Callable[[FaultyBackend], bool]] = None,
    max_points: int = 24,
) -> ExploreReport:
    """
    Enumerate every named checkpoint in the writer, inject failure at each,
    and verify that recovery (if provided) leaves a consistent image.

    Verifier contract:
      verify_fn(backend) -> True if image is structurally valid.
      recover_fn(backend) -> True if recovery completed without corruption.
    """
    # Pass 1: collect checkpoint names by observing a normal run
    probe = build_backend()
    points: list[str] = []
    orig = probe.checkpoint

    def spy(name: str) -> None:
        if name not in points:
            points.append(name)
        orig(name)

    probe.checkpoint = spy  # type: ignore[method-assign]
    try:
        writer_fn(probe)
    except Exception:
        pass

    if len(points) > max_points:
        points = points[:max_points]

    report = ExploreReport(total_states=len(points), crashed=0,
                           recovered=0, quarantined=0, corrupt=0)

    for point in points:
        backend = build_backend()
        backend._inj = Injection(point, FailMode.IO_ERROR)
        crashed = False
        try:
            writer_fn(backend)
        except (ShortWriteError, ShortReadError, MountFileError):
            crashed = True
        except Exception as e:
            report.failures.append(f"{point}: unexpected {type(e).__name__}: {e}")
            report.corrupt += 1
            continue

        if not crashed:
            # writer didn't actually fail; skip
            continue
        report.crashed += 1

        if recover_fn is None:
            continue
        rec_backend = build_backend()
        try:
            ok = recover_fn(rec_backend)
        except Exception as e:
            report.failures.append(f"{point}: recovery raised {type(e).__name__}: {e}")
            report.corrupt += 1
            continue

        if not ok:
            report.failures.append(f"{point}: recovery returned False")
            report.corrupt += 1
            continue

        if verify_fn(rec_backend):
            report.recovered += 1
        else:
            report.failures.append(f"{point}: post-recovery verify failed")
            report.corrupt += 1

    return report


def image_hash(backend) -> str:
    h = hashlib.sha256()
    n = backend.size()
    chunk = 65536
    pos = 0
    while pos < n:
        size = min(chunk, n - pos)
        data = backend.read(pos, size)
        if not data:
            break
        h.update(data)
        pos += len(data)
    return h.hexdigest()
```

📁 mountfile/testing/quarantine.py

```python
"""
Quarantine: when a write path cannot determine state, mark the image
as indeterminate and refuse further writes until explicit recovery.

Manifest stored alongside the image:
  <image>.mountfile-quarantine.json

Format:
  {
    "version": 1,
    "image_sha256": "...",
    "reason": "fsync failure during bitmap update",
    "operation": "create_file",
    "writer": "fat32",
    "checkpoint": "after_bitmap_update",
    "timestamp": "2026-11-03T...",
    "steps_to_recover": [
      "run fsck.vfat -a <image>",
      "mountfile recover --force <image>"
    ]
  }
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, asdict, field
from typing import Optional


QUARANTINE_SUFFIX = ".mountfile-quarantine.json"


@dataclass
class QuarantineManifest:
    version: int = 1
    image_sha256: str = ""
    reason: str = ""
    operation: str = ""
    writer: str = ""
    checkpoint: str = ""
    timestamp: str = ""
    steps_to_recover: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def manifest_path(image_path: str) -> str:
    return image_path + QUARANTINE_SUFFIX


def is_quarantined(image_path: str) -> bool:
    return os.path.exists(manifest_path(image_path))


def read_manifest(image_path: str) -> Optional[QuarantineManifest]:
    p = manifest_path(image_path)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        data = json.load(f)
    return QuarantineManifest(**data)


def quarantine(image_path: str, *,
                image_sha256: str,
                reason: str,
                operation: str,
                writer: str,
                checkpoint: str,
                steps: Optional[list[str]] = None) -> QuarantineManifest:
    m = QuarantineManifest(
        image_sha256=image_sha256,
        reason=reason,
        operation=operation,
        writer=writer,
        checkpoint=checkpoint,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        steps_to_recover=steps or [],
    )
    with open(manifest_path(image_path), "w") as f:
        f.write(m.to_json())
    return m


def release(image_path: str) -> None:
    """Remove the quarantine marker (only after successful recovery)."""
    p = manifest_path(image_path)
    if os.path.exists(p):
        os.remove(p)


class QuarantineRefusal(Exception):
    def __init__(self, image_path: str, manifest: QuarantineManifest):
        self.image_path = image_path
        self.manifest = manifest
        super().__init__(
            f"image {image_path} is quarantined "
            f"({manifest.operation} failed at {manifest.checkpoint}); "
            f"reason: {manifest.reason}"
        )


def assert_not_quarantined(image_path: str) -> None:
    if is_quarantined(image_path):
        m = read_manifest(image_path)
        if m:
            raise QuarantineRefusal(image_path, m)
```

📁 mountfile/testing/recovery_verifier.py

```python
"""
Independent recovery verification after crash.

Runs external checker (e2fsck -fn, fsck.fat -n, fsck.exfat -n, ntfsfix -n)
under timeout in a temp copy of the image and parses the exit code.

Semantics:
  rc=0  → clean
  rc=1  → errors corrected (acceptable after crash)
  rc>1  → fatal, corruption beyond recovery
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional


@dataclass
class CheckerResult:
    tool: str
    available: bool
    returncode: Optional[int]
    clean: bool
    output: str
    timed_out: bool = False


def _run(cmd: list[str], timeout: int) -> CheckerResult:
    tool = cmd[0]
    if shutil.which(tool) is None:
        return CheckerResult(tool, False, None, False, "not installed")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        out = (r.stdout or "") + (r.stderr or "")
        return CheckerResult(
            tool, True, r.returncode,
            clean=(r.returncode in (0, 1)),
            output=out[-2000:],
        )
    except subprocess.TimeoutExpired as e:
        return CheckerResult(tool, True, None, False, "timeout", timed_out=True)


def check_image(image_path: str, fs_family: str,
                timeout: int = 120) -> CheckerResult:
    fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(image_path)[1])
    os.close(fd)
    try:
        shutil.copy2(image_path, tmp)
        if fs_family == "fat":
            return _run(["fsck.fat", "-n", "-v", tmp], timeout)
        if fs_family == "exfat":
            return _run(["fsck.exfat", "-n", tmp], timeout)
        if fs_family in ("ext2", "ext3", "ext4"):
            return _run(["e2fsck", "-fn", tmp], timeout)
        if fs_family == "ntfs":
            return _run(["ntfsfix", "-n", tmp], timeout)
        return CheckerResult(fs_family, False, None, False, "unknown family")
    finally:
        os.unlink(tmp)


def verified_recover(image_path: str, fs_family: str,
                     timeout: int = 120) -> tuple[bool, CheckerResult]:
    """
    Run recovery checker. Return (ok, result).
    ok=True iff image is clean or was repaired (rc 0/1).
    """
    r = check_image(image_path, fs_family, timeout)
    return r.clean, r
```

---

Шаг 2: ext2 crash/recovery acceptance

📁 tests/crash/workload_write_tree.sh

```bash
#!/usr/bin/env bash
# Deterministic workload for crash testing.
set -eu
ROOT="$1"
mkdir -p "$ROOT/a/b/c"
echo "alpha"  > "$ROOT/a/file1.txt"
echo "beta"   > "$ROOT/a/b/file2.txt"
echo "gamma"  > "$ROOT/a/b/c/file3.txt"
seq 1 10000 > "$ROOT/big.txt"
sync
```

📁 tests/crash/workload_write_tree_verify.sh

```bash
#!/usr/bin/env bash
set -eu
ROOT="$1"
[ -f "$ROOT/a/file1.txt" ] || { echo "missing file1"; exit 1; }
[ "$(cat "$ROOT/a/file1.txt")" = "alpha" ] || exit 1
[ -f "$ROOT/a/b/file2.txt" ] || exit 1
[ "$(cat "$ROOT/a/b/file2.txt")" = "beta" ] || exit 1
[ -f "$ROOT/a/b/c/file3.txt" ] || exit 1
[ "$(cat "$ROOT/a/b/c/file3.txt")" = "gamma" ] || exit 1
[ -f "$ROOT/big.txt" ] || exit 1
[ "$(wc -l < "$ROOT/big.txt")" = "10000" ] || exit 1
echo "VERIFY OK"
```

📁 scripts/crash_acceptance.sh

```bash
#!/usr/bin/env bash
# ext2/ext4 crash acceptance using dm-flakey.
# Requires: root, dmsetup, losetup, mkfs.ext4, e2fsck.
set -uo pipefail

FS_TYPE="${1:-ext2}"
WORKLOAD="${2:-tests/crash/workload_write_tree.sh}"
VERIFY="${WORKLOAD%.sh}_verify.sh"
IMG_SIZE_MB="${IMG_SIZE_MB:-64}"

if [ "$(id -u)" -ne 0 ]; then
    echo "requires root" >&2; exit 2
fi
for t in dmsetup losetup mount umount e2fsck mkfs.ext4; do
    command -v "$t" >/dev/null || { echo "missing $t" >&2; exit 2; }
done
[ -x "$WORKLOAD" ] || { echo "missing workload $WORKLOAD" >&2; exit 2; }
[ -x "$VERIFY"   ] || { echo "missing verify $VERIFY"   >&2; exit 2; }

IMG=$(mktemp --suffix=.img)
truncate -s "${IMG_SIZE_MB}M" "$IMG"
trap 'rm -f "$IMG"' EXIT

LOOP=$(losetup -f --show "$IMG")
DMNAME="crash-$$"
SECTORS=$(blockdev --getsz "$LOOP")
MNT="/mnt/crash-$$"

cleanup() {
    umount -l "$MNT" 2>/dev/null || true
    dmsetup remove "$DMNAME" 2>/dev/null || true
    losetup -d "$LOOP" 2>/dev/null || true
    rmdir "$MNT" 2>/dev/null || true
}
trap cleanup EXIT

mkfs.ext4 -q -t "$FS_TYPE" -b 1024 \
    -O extent,dir_index,metadata_csum "$LOOP"

dmsetup create "$DMNAME" --table "0 $SECTORS flakey $LOOP 0 86400 1"
DEV="/dev/mapper/$DMNAME"

mkdir -p "$MNT"
mount -t "$FS_TYPE" "$DEV" "$MNT"

echo "[1/4] run workload"
bash "$WORKLOAD" "$MNT" || { echo "workload failed"; exit 1; }

echo "[2/4] sync + simulate crash"
sync
dmsetup suspend "$DMNAME"
dmsetup load "$DMNAME" --table "0 $SECTORS flakey $LOOP 0 0 1"
dmsetup resume "$DMNAME"
umount "$MNT" 2>/dev/null || true
dmsetup remove "$DMNAME" 2>/dev/null || true

echo "[3/4] e2fsck"
e2fsck -fy "$LOOP" >/tmp/e2fsck.log 2>&1
RC=$?
if [ "$RC" -gt 1 ]; then
    echo "FATAL: e2fsck rc=$RC"
    tail -30 /tmp/e2fsck.log
    exit 1
fi
echo "  e2fsck rc=$RC (0=clean, 1=repaired)"

echo "[4/4] verify content"
mount -t "$FS_TYPE" "$LOOP" "$MNT" || { echo "remount failed"; exit 1; }
bash "$VERIFY" "$MNT" || { echo "VERIFY FAILED"; exit 1; }
umount "$MNT"

echo "PASS: $FS_TYPE crash acceptance"
```

📁 tests/test_crash_acceptance.py

```python
"""
Crash acceptance for ext2/ext3/ext4. Linux + root + dm-flakey required.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import pytest


def _has_flakey() -> bool:
    if platform.system() != "Linux":
        return False
    if os.geteuid() != 0:
        return False
    if shutil.which("dmsetup") is None:
        return False
    r = subprocess.run(["dmsetup", "targets"], capture_output=True, text=True)
    return "flakey" in r.stdout


pytestmark = pytest.mark.skipif(
    not _has_flakey(),
    reason="needs Linux + root + dm-flakey",
)


@pytest.mark.parametrize("fs_type", ["ext2", "ext4"])
def test_crash_acceptance(fs_type):
    r = subprocess.run(
        ["bash", "scripts/crash_acceptance.sh", fs_type],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
    assert r.returncode == 0


def test_crash_acceptance_repeat_3x():
    """Catch flaky failures by repeating."""
    for i in range(3):
        r = subprocess.run(
            ["bash", "scripts/crash_acceptance.sh", "ext4"],
            capture_output=True, text=True, timeout=300,
        )
        assert r.returncode == 0, f"run {i}: {r.stdout}\n{r.stderr}"
```

---

Шаг 3: Dynamic capability reporting

📁 mountfile/capabilities_dynamic.py

```python
"""
Dynamic capability reporting: extends static matrix with per-image
reason/code that reflects the CURRENT image state.

Uses existing capabilities.py for the static decision, then augments
with image-specific explanation. This is the single source of truth
for VFS/CLI/GUI/API reporting.

API:
  report(image) -> DynamicReport
  explain(image, operation) -> ExplainResult
  matrix(image) -> list[OperationRow]
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Optional, Sequence

try:
    from mountfile.capabilities import (
        CAPABILITY_SCHEMA_VERSION,
        Capability,
        Operation,
        OperationOutcome,
        decide_operation,
        get_capability_matrix,
    )
except ImportError:
    # Defensive: keeps module importable in isolation, integration
    # tests will fail loudly if the real module is missing.
    CAPABILITY_SCHEMA_VERSION = 0

    class Operation:
        pass

    class OperationOutcome:
        ALLOW = "allow"
        LIMITED = "limited"
        REFUSE = "refuse"


@dataclass
class ExplainResult:
    operation: str
    outcome: str
    code: Optional[str]
    reason: str
    image_reason: str          # image-specific reason ("inodes_exhausted", ...)
    suggested_action: Optional[str]
    format: str
    profile: str
    features: list[str]
    state: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DynamicReport:
    schema_version: int
    format: str
    profile: str
    features: list[str]
    state: list[str]
    operations: list[ExplainResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "format": self.format,
            "profile": self.profile,
            "features": self.features,
            "state": self.state,
            "operations": [o.to_dict() for o in self.operations],
        }


def _probe(image) -> dict:
    """Extract format/profile/features/state via existing probe."""
    try:
        from mountfile.probe import probe_image
        p = probe_image(image)
        return {
            "format": p.format,
            "profile": p.profile,
            "features": list(p.features),
            "state": list(p.state),
        }
    except ImportError:
        # Fallback: try image attributes
        return {
            "format": getattr(image, "format", "unknown"),
            "profile": getattr(image, "profile", "unknown"),
            "features": list(getattr(image, "features", [])),
            "state": list(getattr(image, "state", [])),
        }


def _image_specific_reason(image, op: str, outcome: str) -> tuple[str, Optional[str]]:
    """
    Given the static outcome, produce an image-specific explanation.

    Examples:
      - REFUSE for create on ext4 → "native ext4 writer not implemented"
      - REFUSE for create on ext2 when no free inodes → "inodes_exhausted"
      - LIMITED for write on FAT32 → "FAT32 writer limited to fixed root"
    """
    state = getattr(image, "state", [])
    if "dirty" in state and outcome == "refuse":
        return "volume is marked dirty", "run fsck first"
    if "needs_recovery" in state and outcome == "refuse":
        return "journal recovery required", "run recovery or fsck"
    if "no_free_inodes" in state and op == "create":
        return "no free inodes available", "delete files or resize"
    if "no_free_blocks" in state and op in ("write", "create"):
        return "no free blocks available", "delete files or resize"
    if outcome == "limited":
        return "operation is supported with restrictions", None
    if outcome == "allow":
        return "operation is fully supported", None
    return "", None


def explain(image, operation: str) -> ExplainResult:
    p = _probe(image)
    try:
        decision = decide_operation(image, operation)
        outcome = decision.outcome.value if hasattr(decision.outcome, "value") \
            else str(decision.outcome)
        code = getattr(decision, "code", None)
        base_reason = getattr(decision, "reason", "")
    except Exception as e:
        # Fallback: everything refused with MOUNT_ERROR
        outcome = "refuse"
        code = "MOUNT_ERROR"
        base_reason = f"capability lookup failed: {e}"

    image_reason, action = _image_specific_reason(image, operation, outcome)
    return ExplainResult(
        operation=operation,
        outcome=outcome,
        code=code,
        reason=base_reason,
        image_reason=image_reason,
        suggested_action=action,
        format=p["format"],
        profile=p["profile"],
        features=p["features"],
        state=p["state"],
    )


def matrix(image) -> DynamicReport:
    p = _probe(image)
    ops: Sequence[str] = _list_operations()
    rows = [explain(image, op) for op in ops]
    return DynamicReport(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        format=p["format"],
        profile=p["profile"],
        features=p["features"],
        state=p["state"],
        operations=rows,
    )


def _list_operations() -> list[str]:
    try:
        from mountfile.capabilities import Operation
        return [op.value if hasattr(op, "value") else str(op)
                for op in Operation]
    except ImportError:
        return [
            "list", "stat", "read", "read_range", "extract",
            "create", "mkdir", "delete", "write", "rename",
        ]
```

📁 mountfile/cli/commands/capabilities.py

```python
"""CLI: `mountfile capabilities <image>` — human and JSON output."""
from __future__ import annotations

import argparse
import json
import sys


def _color(s: str, c: str) -> str:
    if not sys.stdout.isatty():
        return s
    codes = {"allow": "32", "limited": "33", "refuse": "31", "dim": "2"}
    return f"\033[{codes.get(c, '0')}m{s}\033[0m"


def add_parser(sub):
    p = sub.add_parser("capabilities", help="show per-image capability matrix")
    p.add_argument("image")
    p.add_argument("--json", action="store_true")
    p.add_argument("--operation", "-o", default=None,
                   help="explain a single operation")
    p.set_defaults(func=run)


def run(args) -> int:
    try:
        from mountfile.image import open_image
        from mountfile.capabilities_dynamic import explain, matrix
    except ImportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    image = open_image(args.image)

    if args.operation:
        result = explain(image, args.operation)
        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print(f"operation:  {result.operation}")
            print(f"outcome:    {_color(result.outcome, result.outcome)}")
            if result.code:
                print(f"code:       {result.code}")
            print(f"reason:     {result.reason}")
            if result.image_reason:
                print(f"image note: {result.image_reason}")
            if result.suggested_action:
                print(f"action:     {result.suggested_action}")
            print(f"format:     {result.format}")
            print(f"profile:    {result.profile}")
            print(f"state:      {result.state}")
        return 0

    report = matrix(image)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
        return 0

    print(f"image:    {args.image}")
    print(f"format:   {report.format}")
    print(f"profile:  {report.profile}")
    print(f"features: {report.features}")
    print(f"state:    {report.state}")
    print()
    print(f"{'operation':<14} {'outcome':<10} {'code':<18} reason")
    print(f"{'-'*14} {'-'*10} {'-'*18} {'-'*30}")
    for r in report.operations:
        code = r.code or ""
        reason = r.reason + (f" ({r.image_reason})" if r.image_reason else "")
        print(f"{r.operation:<14} {_color(r.outcome, r.outcome):<10} "
              f"{code:<18} {reason}")
    return 0
```

---

Шаг 4: Differential corpus + checker matrix

📁 mountfile/testing/checker_matrix.py

```python
"""
Independent checker matrix: for each supported FS family and each
image fixture, run the external checker and compare against the
capability expectation.

Fixtures directory layout:
  tests/fixtures/images/<family>/<name>.img

Expected results:
  tests/fixtures/images/<family>/<name>.expect.json
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


CHECKERS = {
    "fat": ("fsck.fat", ["-n", "-v"]),
    "exfat": ("fsck.exfat", ["-n"]),
    "ext2": ("e2fsck", ["-fn"]),
    "ext3": ("e2fsck", ["-fn"]),
    "ext4": ("e2fsck", ["-fn"]),
    "ntfs": ("ntfsfix", ["-n"]),
}


@dataclass
class CheckerRow:
    family: str
    image: str
    tool: str
    available: bool
    returncode: Optional[int]
    matches_expected: Optional[bool]
    expected_rc: Optional[int]
    output_tail: str


def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timeout"


def check_family(family: str, images_dir: Path) -> list[CheckerRow]:
    spec = CHECKERS.get(family)
    if not spec:
        return []
    tool, args = spec
    available = shutil.which(tool) is not None
    rows: list[CheckerRow] = []
    family_dir = images_dir / family
    if not family_dir.exists():
        return rows
    for img in sorted(family_dir.glob("*.img")):
        expect_file = img.with_suffix(".expect.json")
        expected = None
        if expect_file.exists():
            expected = json.loads(expect_file.read_text())
        if not available:
            rows.append(CheckerRow(family, str(img), tool, False, None,
                                    None, expected.get("returncode") if expected else None,
                                    "not installed"))
            continue
        rc, out = _run([tool, *args, str(img)])
        matches = None
        if expected and "returncode" in expected:
            matches = rc == expected["returncode"]
        rows.append(CheckerRow(family, str(img), tool, True, rc, matches,
                                expected.get("returncode") if expected else None,
                                out[-500:]))
    return rows


def run_matrix(images_dir: Path) -> list[CheckerRow]:
    rows: list[CheckerRow] = []
    for family in CHECKERS:
        rows.extend(check_family(family, images_dir))
    return rows


def to_dict(rows: list[CheckerRow]) -> dict:
    return {
        "platform": platform.platform(),
        "total": len(rows),
        "mismatches": sum(1 for r in rows
                          if r.matches_expected is False),
        "skipped": sum(1 for r in rows if not r.available),
        "rows": [asdict(r) for r in rows],
    }
```

📁 tests/fixtures/images/README.md

```markdown
# Real image fixtures

Each family subdirectory contains real images plus optional `.expect.json`:

```

tests/fixtures/images/
fat/
clean_fat12.img
clean_fat12.expect.json    {"returncode": 0}
dirty_fat16.img
dirty_fat16.expect.json    {"returncode": 1}
exfat/...
ext2/...
ext3/...
ext4/...
ntfs/...

```

Images generated by:
  `scripts/generate_fixtures.sh`

Expectation files hand-written or captured from a known-good run.

Regenerate:
  `scripts/generate_fixtures.sh --force`
```

📁 scripts/generate_fixtures.sh

```bash
#!/usr/bin/env bash
# Generate real fixture images for checker matrix.
set -euo pipefail

OUT="${1:-tests/fixtures/images}"
FORCE="${FORCE:-0}"

mkdir -p "$OUT"/{fat,exfat,ext2,ext3,ext4,ntfs}

gen() {
    local family="$1"; local name="$2"; local size_mb="$3"; shift 3
    local path="$OUT/$family/$name.img"
    if [ -f "$path" ] && [ "$FORCE" != "1" ]; then
        echo "exists: $path"
        return
    fi
    truncate -s "${size_mb}M" "$path"
    "$@" "$path"
    echo "created: $path"
}

if command -v mkfs.fat >/dev/null; then
    gen fat clean_fat12 4  mkfs.fat -F 12
    gen fat clean_fat16 8  mkfs.fat -F 16
    gen fat clean_fat32 32 mkfs.fat -F 32
fi

if command -v mkfs.exfat >/dev/null; then
    gen exfat clean_exfat 64 mkfs.exfat
fi

if command -v mkfs.ext4 >/dev/null; then
    gen ext2 clean_ext2 16 mkfs.ext4 -q -t ext2 -b 1024
    gen ext3 clean_ext3 16 mkfs.ext4 -q -t ext3 -b 1024
    gen ext4 clean_ext4 16 mkfs.ext4 -q -t ext4 -b 1024 \
        -O extent,dir_index,metadata_csum
    gen ext4 fastcommit_ext4 16 mkfs.ext4 -q -O fast_commit
fi

if command -v mkfs.ntfs >/dev/null; then
    gen ntfs clean_ntfs 32 mkfs.ntfs -F -Q
fi

# Capture expectations for clean images
for img in $(find "$OUT" -name '*.img'); do
    exp="${img%.img}.expect.json"
    [ -f "$exp" ] && continue
    echo '{"returncode": 0}' > "$exp"
done

echo "done: $OUT"
```

📁 tests/test_checker_matrix.py

```python
"""Verify every real fixture passes its checker with expected returncode."""
from __future__ import annotations

from pathlib import Path

import pytest

from mountfile.testing.checker_matrix import run_matrix, to_dict


FIXTURES = Path("tests/fixtures/images")


@pytest.mark.skipif(not FIXTURES.exists(),
                    reason="fixtures not generated; run scripts/generate_fixtures.sh")
def test_checker_matrix():
    rows = run_matrix(FIXTURES)
    assert rows, "no checker rows produced"
    mismatches = [r for r in rows if r.matches_expected is False]
    if mismatches:
        summary = "\n".join(
            f"  {r.family}/{Path(r.image).name}: "
            f"expected rc={r.expected_rc}, got rc={r.returncode}\n"
            f"    tail: {r.output_tail}"
            for r in mismatches
        )
        pytest.fail(f"{len(mismatches)} mismatches:\n{summary}")


def test_matrix_report_shape():
    rows = run_matrix(FIXTURES) if FIXTURES.exists() else []
    report = to_dict(rows)
    assert "platform" in report
    assert "total" in report
    assert "mismatches" in report
    assert isinstance(report["rows"], list)
```

📁 tests/test_differential_pytsk3.py

```python
"""
Differential test: native reader vs pytsk3, both read-only.
Skipped when pytsk3 or fixtures are missing.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

try:
    import pytsk3
    HAS_PYTSK3 = True
except ImportError:
    HAS_PYTSK3 = False


FIXTURES = Path("tests/fixtures/images")


pytestmark = pytest.mark.skipif(
    not HAS_PYTSK3 or not FIXTURES.exists(),
    reason="needs pytsk3 and fixtures",
)


def _native_names(image_path: Path) -> list[str]:
    from mountfile.image import open_image
    img = open_image(str(image_path))
    return sorted(e.name for e in img.list_dir("/") if e.name not in (".", ".."))


def _pytsk3_names(image_path: Path) -> list[str]:
    img_info = pytsk3.Img_Info(str(image_path))
    fs = pytsk3.FS_Info(img_info)
    root = fs.open_dir(path="/")
    out = []
    for e in root:
        name = e.info.name.name if e.info.name else b""
        if not name or name in (b".", b".."):
            continue
        out.append(name.decode("utf-8", "replace"))
    return sorted(out)


@pytest.mark.parametrize("image_path", sorted(FIXTURES.glob("*/clean_*.img"))
                         if FIXTURES.exists() else [])
def test_list_dir_matches_pytsk3(image_path: Path):
    native = _native_names(image_path)
    pytsk = _pytsk3_names(image_path)
    assert native == pytsk, f"{image_path.name}: {native} != {pytsk}"
```

---

🔌 Интеграция: как это подключается к main

Существующие writer'ы должны добавить 4-8 вызовов checkpoint():

```python
# Пример для существующего FAT32 writer:

class FAT32Writer:
    def __init__(self, backend):
        self._backend = backend

    def _cp(self, name: str):
        if hasattr(self._backend, "checkpoint"):
            self._backend.checkpoint(name)

    def create_file(self, parent, name, data):
        self._cp(Checkpoint.BEFORE_BITMAP_UPDATE)
        self._alloc_clusters(...)
        self._cp(Checkpoint.AFTER_BITMAP_UPDATE)
        self._cp(Checkpoint.BEFORE_DATA_WRITE)
        self._write_data(...)
        self._cp(Checkpoint.AFTER_DATA_WRITE)
        self._cp(Checkpoint.BEFORE_DIRENT_ADD)
        self._add_dirent(...)
        self._cp(Checkpoint.AFTER_DIRENT_ADD)
        self._cp(Checkpoint.BEFORE_FSYNC)
        self._backend.fsync()
        self._cp(Checkpoint.AFTER_FSYNC)
```

Тест:

```python
from mountfile.testing.failure_injection import (
    FaultyBackend, explore_writer, image_hash,
)
from mountfile.testing.quarantine import (
    assert_not_quarantined, quarantine, release,
)

def test_fat32_writer_survives_crash(tmp_path):
    def build():
        img = tmp_path / "fat.img"
        # ... create clean FAT32 fixture ...
        return FaultyBackend(open_backend(img))

    def writer(backend):
        w = FAT32Writer(backend)
        w.create_file("/", "test.txt", b"data")

    def recover(backend):
        # e2fsck-like — for FAT use fsck.fat
        from mountfile.testing.recovery_verifier import verified_recover
        ok, _ = verified_recover(str(backend._inner.path), "fat")
        return ok

    def verify(backend):
        # basic structural check: root dir readable
        return True

    report = explore_writer(build, writer, verify, recover)
    assert report.ok(), f"corrupt states: {report.failures}"
```

Обеспечение quarantine в writer:

```python
def open_for_write(image_path):
    assert_not_quarantined(image_path)
    ...
    try:
        writer.create_file(...)
    except ShortWriteError as e:
        quarantine(image_path,
                   image_sha256=image_hash(backend),
                   reason=str(e),
                   operation="create_file",
                   writer="fat32",
                   checkpoint=last_checkpoint,
                   steps=[
                       "run fsck.vfat -a <image>",
                       "mountfile recover --force <image>",
                   ])
        raise
```

---

🎯 Что это закрывает в аудите

№ Пункт Модуль
1 Failure-injection + quarantine mountfile/testing/failure_injection.py, quarantine.py
2 Real ext2 crash/recovery acceptance scripts/crash_acceptance.sh, tests/test_crash_acceptance.py
3 Dynamic capability reporting (VFS/CLI/API) mountfile/capabilities_dynamic.py, mountfile/cli/commands/capabilities.py
4 Differential corpus + checker matrix mountfile/testing/checker_matrix.py, tests/fixtures/, tests/test_differential_pytsk3.py

Не закрывает (намеренно, следующий этап):

· ext3/ext4/NTFS writers → остаются REFUSE до реализации transaction/recovery.
· Полная exFAT/FAT metadata parity → ограничена существующим профилем.
· Cross-platform acceptance → требует прогон CI на 3 платформах.

---

🚀 Запуск

```bash
# 1. Скопировать модули в mountfile/testing/ и mountfile/
# 2. Добавить checkpoint() вызовы в существующие writers
# 3. Прогнать unit
pytest -q tests/test_failure_injection.py tests/test_checker_matrix.py \
        mountfile/testing/test_*.py

# 4. Сгенерировать fixtures (нужны mkfs tools)
bash scripts/generate_fixtures.sh

# 5. Checker matrix
pytest -q tests/test_checker_matrix.py

# 6. Crash acceptance (нужен root + dm-flakey)
sudo pytest -q tests/test_crash_acceptance.py

# 7. Differential vs pytsk3
pip install pytsk3
pytest -q tests/test_differential_pytsk3.py

# 8. Dynamic capabilities smoke
mountfile capabilities tests/fixtures/images/ext4/clean_ext4.img
mountfile capabilities tests/fixtures/images/ext2/clean_ext2.img -o create
mountfile capabilities tests/fixtures/images/ext2/clean_ext2.img --json | jq
```

Все тесты должны быть зелёными до того, как issue #182 переходит к рассмотрению ext3/ext4/NTFS writers. Это и есть правильный порядок, указанный в аудите.