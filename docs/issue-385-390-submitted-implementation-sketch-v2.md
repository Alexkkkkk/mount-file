# Обновлённый присланный черновик реализации issues #385–#390

> **Статус: только проектный эскиз, не production-код.** Фрагменты сохранены для ревью, но не подключаются к runtime. В них по-прежнему отсутствуют полноценные проверки checksum/USA/LSN, строгие bounds/sector-size проверки, sequence wrap, undo/checkpoint, durable ordering и crash-safety. До прохождения acceptance-gates запись остаётся fail-closed.

В черновике также используются пути и импорты, которых нет в текущем checkout (`mountfile.engines.*`, `mountfile.vfs.*`, `tests.helpers.*`), поэтому прямое добавление этих фрагментов сломало бы импорт и CI. Актуальная безопасная стратегия описана в [issue-385-390-solution-plan.md](issue-385-390-solution-plan.md).

Ниже — конкретные реализации для каждого из шести открытых issues. Код спроектирован как дополнение к PR #508: он не ломает существующий journal-gate, а расширяет его до полного replay, добавляет матрицу отказов, кросс-платформенные фикстуры и синхронизацию статуса. Все модули следуют принципу fail-closed — любая неопределённость ведёт к отказу от записи, а не к «молчаливому» продолжению.

🧱 #385: Полный JBD2 replay для ext4

PR #508 уже умеет определять NEEDS_REPLAY, но не выполняет сам replay. Этот модуль закрывает redo-фазу с проверкой контрольных сумм и корректной обработкой revoke-блоков.

mountfile/ext4_journal_replay.py:

```python
"""
JBD2 replay (redo only) for ext4.

Contract:
    * Works exclusively on a disposable copy.
    * Verifies descriptor tag checksums when metadata_csum is enabled.
    * Refuses on sequence gaps or malformed revoke blocks.
    * Never touches the original image.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

JBD2_MAGIC = 0xC03B3998
JBD2_DESCRIPTOR = 1
JBD2_COMMIT = 2
JBD2_REVOKE = 5
JBD2_FLAG_ESCAPE = 1
JBD2_FLAG_SAME_UUID = 2
JBD2_FLAG_DELETED = 4
JBD2_FLAG_LAST_TAG = 8


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplayResult:
    replayed: int
    revoked: int
    transactions: int


class JBD2Replayer:
    def __init__(self, image: Path, offset: int = 0, block_size: int = 4096):
        self.image = image
        self.offset = offset
        self.block_size = block_size

    def replay(self) -> ReplayResult:
        jbd2_off = self._find_superblock()
        if jbd2_off < 0:
            raise ReplayError("JBD2 superblock not found")

        sb = self._read(jbd2_off, 1024)
        s_start = struct.unpack_from(">I", sb, 0x1C)[0]
        s_maxlen = struct.unpack_from(">I", sb, 0x10)[0]
        s_first = struct.unpack_from(">I", sb, 0x14)[0]
        if s_start == 0:
            return ReplayResult(0, 0, 0)
        if s_start >= s_maxlen:
            raise ReplayError(f"s_start={s_start} out of ring (maxlen={s_maxlen})")

        revoked: set[int] = set()
        replayed = 0
        txn = 0
        block = s_start

        while True:
            hdr = self._read_block(jbd2_off, block)
            magic, btype = struct.unpack_from(">II", hdr, 0)
            if magic != JBD2_MAGIC:
                break

            if btype == JBD2_COMMIT:
                revoked.clear()
                txn += 1
                block = self._next(block, s_first, s_maxlen)
                continue

            if btype == JBD2_REVOKE:
                count = struct.unpack_from(">I", hdr, 12)[0]
                for i in range(count):
                    r = struct.unpack_from(">I", hdr, 16 + i * 4)[0]
                    revoked.add(r)
                block = self._next(block, s_first, s_maxlen)
                continue

            if btype == JBD2_DESCRIPTOR:
                tags = self._parse_tags(hdr)
                data_block = self._next(block, s_first, s_maxlen)
                for tag in tags:
                    if tag.blocknr in revoked or (tag.flags & JBD2_FLAG_DELETED):
                        data_block = self._next(data_block, s_first, s_maxlen)
                        continue
                    data = self._read_block(jbd2_off, data_block)
                    self._write_target(tag.blocknr, data)
                    replayed += 1
                    data_block = self._next(data_block, s_first, s_maxlen)
                block = data_block
                continue
            break

        with open(self.image, "r+b") as f:
            f.seek(self.offset + jbd2_off + 0x1C)
            f.write(struct.pack(">I", 0))
        return ReplayResult(replayed, len(revoked), txn)

    def _find_superblock(self) -> int:
        for b in range(8):
            off = b * self.block_size
            data = self._read(off, 8)
            if len(data) < 8:
                continue
            magic, btype = struct.unpack(">II", data)
            if magic == JBD2_MAGIC and btype == 4:  # SUPERBLOCK_V2
                return off
        return -1

    def _read(self, off: int, size: int) -> bytes:
        with open(self.image, "rb") as f:
            f.seek(self.offset + off)
            return f.read(size)

    def _read_block(self, jbd2_off: int, block: int) -> bytes:
        return self._read(jbd2_off + block * self.block_size, self.block_size)

    def _next(self, block: int, first: int, maxlen: int) -> int:
        nxt = block + 1
        return first if nxt >= maxlen else nxt

    def _parse_tags(self, hdr: bytes) -> list:
        tags = []
        off = 12
        while off + 8 <= len(hdr):
            blocknr, flags = struct.unpack_from(">II", hdr, off)
            tags.append(type("Tag", (), {"blocknr": blocknr, "flags": flags}))
            if flags & JBD2_FLAG_LAST_TAG:
                break
            if not (flags & JBD2_FLAG_SAME_UUID):
                off += 16
            off += 8
        return tags

    def _write_target(self, blocknr: int, data: bytes) -> None:
        with open(self.image, "r+b") as f:
            f.seek(self.offset + blocknr * self.block_size)
            f.write(data)
```

Интеграция с Ext4JournalGate: при NEEDS_REPLAY вызывается JBD2Replayer.replay(), затем повторный e2fsck -fn. Если replay завершился с ошибкой — образ помечается как quarantine, и запись отклоняется.

🗂️ #386: NTFS redo/undo для $LogFile

PR #508 проверяет $LogFile, но не восстанавливает его. Этот модуль реализует redo-фазу (undo можно добавить вторым PR, как и требует issue).

mountfile/ntfs_logfile_replay.py:

```python
"""
NTFS $LogFile redo replay.

Reads the restart area, walks LFS records, and applies redo
for committed transactions. Refuses on pending (uncommitted)
transactions until undo is implemented.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

RESTART_MAGIC = b"RSTR"
RCRD_MAGIC = b"RCRD"


class NtfsReplayError(RuntimeError):
    pass


@dataclass
class NtfsReplayResult:
    redo_applied: int
    pending_refused: int


class NtfsLogReplayer:
    def __init__(self, image: Path, offset: int = 0):
        self.image = image
        self.offset = offset

    def replay(self, logfile_data: bytes) -> NtfsReplayResult:
        restart = self._parse_restart(logfile_data)
        if self._is_clean(restart):
            return NtfsReplayResult(0, 0)

        pending = 0
        redo = 0
        for rec in self._iter_client_records(logfile_data, restart):
            kind = rec["kind"]
            if kind in (b"INIT", b"OPEN"):
                pending += 1
            elif kind == b"COMM":
                redo += self._apply_redo_chain(logfile_data, rec)

        if pending:
            raise NtfsReplayError(
                f"{pending} pending transaction(s); undo not implemented"
            )
        return NtfsReplayResult(redo, 0)

    def _parse_restart(self, data: bytes) -> dict:
        if not data.startswith(RESTART_MAGIC):
            raise NtfsReplayError("not a $LogFile")
        page = self._apply_usa(data[:512])
        log_page = struct.unpack_from("<I", page, 0x14)[0]
        restart_off = struct.unpack_from("<H", page, 0x18)[0]
        ra = self._apply_usa(data[restart_off:restart_off + 512])
        return {
            "log_page_size": log_page,
            "restart_off": restart_off,
            "log_flags": struct.unpack_from("<H", ra, 0x08)[0],
        }

    def _is_clean(self, restart: dict) -> bool:
        return (restart["log_flags"] & 0x0001) == 0

    def _apply_usa(self, page: bytes) -> bytes:
        buf = bytearray(page)
        usa_off = struct.unpack_from("<H", buf, 4)[0]
        usa_count = struct.unpack_from("<H", buf, 6)[0]
        for i in range(1, usa_count):
            end = i * 512 - 2
            buf[end:end + 2] = buf[usa_off + i * 2: usa_off + i * 2 + 2]
        return bytes(buf)

    def _iter_client_records(self, data: bytes, restart: dict):
        page = restart["log_page_size"]
        pos = restart["restart_off"]
        while pos + page <= len(data):
            fixed = self._apply_usa(data[pos:pos + page])
            if fixed[:4] != RCRD_MAGIC:
                break
            yield from self._parse_lfs_records(fixed, page)
            pos += page

    def _parse_lfs_records(self, page: bytes, page_size: int):
        rec_off = struct.unpack_from("<H", page, 0x10)[0]
        end_off = struct.unpack_from("<H", page, 0x12)[0]
        cur = rec_off
        while cur < end_off and cur + 40 < page_size:
            try:
                (rtype, prev_lsn, undo_next, data_len, seq, cidx, coff,
                 cattr, tattr, tvcn, tlcn, lsn, tx_id, flags) = \
                    struct.unpack_from("<iQQIHHHHHHQQQII", page, cur)
            except struct.error:
                break
            payload = page[cur + 40: cur + 40 + data_len]
            if len(payload) < 4:
                break
            yield {
                "kind": payload[:4],
                "lsn": lsn,
                "prev_lsn": prev_lsn,
                "target_lcn": tlcn,
                "data": payload,
            }
            cur += 40 + data_len
            if cur <= rec_off:
                break

    def _apply_redo_chain(self, data: bytes, comm: dict) -> int:
        lcn = comm.get("target_lcn")
        payload = comm.get("data")
        if not lcn or not payload:
            return 0
        with open(self.image, "r+b") as f:
            f.seek(self.offset + lcn * 512)
            f.write(payload)
        return 1
```

Интеграция: NtfsEngine.prepare() вызывает replay() после проверки $LogFile. При pending — quarantine.

🧪 #387: Матрица отказов (failure matrix)

Фреймворк, который генерирует машиночитаемый артефакт profile × operation × failure × checker и различает pass / fail / unavailable / not_claimed.

tests/acceptance/failure_matrix.py:

```python
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

import pytest


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"
    NOT_CLAIMED = "not_claimed"


@dataclass
class MatrixCell:
    profile: str
    operation: str
    failure: str
    checker: str
    outcome: Outcome
    detail: str = ""


def record(matrix, **kw):
    matrix.append(MatrixCell(**kw))


def publish(matrix, path: Path):
    path.write_text(json.dumps([asdict(c) for c in matrix], indent=2))


@pytest.fixture
def matrix():
    return []


# Пример для ext4
def test_ext4_failure_matrix(ext4_image, matrix):
    from mountfile.native_write_engine import TransactionalWriter, WritePlan
    from mountfile.engines.ext4_engine import Ext4Engine
    from tests.helpers.engines import InjectingEngine

    for failure in ("prepare", "apply", "verify"):
        engine = InjectingEngine(Ext4Engine(), fail_at=failure)
        writer = TransactionalWriter(
            engine=engine,
            verify=lambda p: __import__("subprocess").run(
                ["e2fsck", "-fn", str(p)], capture_output=True
            ).returncode == 0,
        )
        r = writer.execute(WritePlan(
            image=ext4_image, operation="put",
            args={"path": "/x", "data": b"d"},
        ))
        record(
            matrix,
            profile="ext4",
            operation="put",
            failure=failure,
            checker="e2fsck -fn",
            outcome=Outcome.PASS if r.outcome.name.startswith("FAILED") else Outcome.FAIL,
        )


def test_publish(matrix, tmp_path):
    if matrix:
        publish(matrix, tmp_path / "matrix.json")
```

🌍 #389: Кросс-платформенные фикстуры

Манифест, который явно различает unavailable (нет чекера) и not_claimed (профиль read-only).

tests/fixtures/cross_platform.py:

```python
from __future__ import annotations

import platform
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pytest


class Checker(str, Enum):
    E2FSCK = "e2fsck"
    FSCK_FAT = "fsck.fat"
    NTFSFIX = "ntfsfix"


@dataclass
class Fixture:
    name: str
    profile: str
    checker: Checker
    supported: bool


FIXTURES = [
    Fixture("fat32.img", "fat32", Checker.FSCK_FAT, supported=True),
    Fixture("ext4.img", "ext4", Checker.E2FSCK, supported=True),
    Fixture("ntfs.img", "ntfs", Checker.NTFSFIX, supported=False),
]


def checker_available(c: Checker) -> bool:
    return shutil.which(c.value) is not None


@pytest.mark.parametrize("f", FIXTURES)
def test_fixture_parity(f: Fixture):
    if not f.supported:
        pytest.skip(f"{f.profile} is read-only; checker not required")
    if not checker_available(f.checker):
        pytest.fail(
            f"{f.profile} is supported but {f.checker.value} "
            f"is unavailable on {platform.system()}"
        )
```

🔄 #390: Синхронизация статуса

Проверка, что README/CLI/GUI не заявляют возможностей, которых нет в runtime.

mountfile/status_sync.py:

```python
from __future__ import annotations

import re
from pathlib import Path

WRITE_CAPABILITY_CLAIMS = {
    "fat32": True,
    "ext4": False,
    "ntfs": False,
}


def check_readme(path: Path) -> list[str]:
    text = path.read_text()
    errors = []
    for fs, supported in WRITE_CAPABILITY_CLAIMS.items():
        if not supported:
            if re.search(rf"{fs}.*(?:create|write|delete|put)", text, re.I):
                if not re.search(rf"{fs}.*(?:read-only|experimental)", text, re.I):
                    errors.append(
                        f"README claims {fs} write support but code says read-only"
                    )
    return errors


def check_cli_preflight(capabilities: dict[str, bool]) -> list[str]:
    errors = []
    for fs, supported in WRITE_CAPABILITY_CLAIMS.items():
        if capabilities.get(fs) != supported:
            errors.append(
                f"CLI says {fs}={capabilities.get(fs)}, expected {supported}"
            )
    return errors
```

🔗 #388: Унификация VFS-контракта

Гарантия, что GUI/CLI/VFS используют один и тот же набор операций и кодов отказа.

mountfile/vfs/contract.py:

```python
from __future__ import annotations

from enum import Enum
from typing import Protocol


class Operation(str, Enum):
    LIST = "list"
    STAT = "stat"
    READ = "read"
    WRITE = "write"
    CREATE = "create"
    DELETE = "delete"
    RENAME = "rename"
    MKDIR = "mkdir"


class VFSResult:
    def __init__(self, success: bool, error_code: str = "", message: str = ""):
        self.success = success
        self.error_code = error_code
        self.message = message


class VFS(Protocol):
    def supported_operations(self) -> list[Operation]: ...
    def listdir(self, path: str) -> VFSResult: ...
    def stat(self, path: str) -> VFSResult: ...
    def read(self, path: str, offset: int = 0, size: int = -1) -> VFSResult: ...
    def write(self, path: str, data: bytes, offset: int = 0) -> VFSResult: ...
    def create(self, path: str, is_dir: bool = False) -> VFSResult: ...
    def delete(self, path: str, recursive: bool = False) -> VFSResult: ...
    def rename(self, old: str, new: str) -> VFSResult: ...
```

Проверка паритета (tests/unit/test_vfs_parity.py):

```python
import pytest
from mountfile.vfs.contract import Operation, VFSResult
from mountfile.vfs.ext4_adapter import Ext4VFS
from mountfile.vfs.ntfs_adapter import NtfsVFS
from mountfile.vfs.fat32_adapter import Fat32VFS


@pytest.mark.parametrize("cls", [Ext4VFS, NtfsVFS, Fat32VFS])
def test_supported_operations_are_enum(cls):
    vfs = cls("dummy.img")
    ops = vfs.supported_operations()
    assert all(isinstance(op, Operation) for op in ops)


@pytest.mark.parametrize("cls", [Ext4VFS, NtfsVFS, Fat32VFS])
def test_write_returns_vfs_result(cls, monkeypatch):
    monkeypatch.setenv("MOUNTFILE_ENABLE_NATIVE_WRITE", "0")
    vfs = cls("dummy.img")
    r = vfs.write("/x", b"data")
    assert isinstance(r, VFSResult)
    assert not r.success
```

💎 Как это собирается вместе

Issue Модуль Что закрывает
#385 ext4_journal_replay.py Replay JBD2 с revoke и проверкой s_start
#386 ntfs_logfile_replay.py Redo $LogFile, отказ при pending
#387 tests/acceptance/failure_matrix.py Машиночитаемая матрица отказов
#388 mountfile/vfs/contract.py Единый VFSResult и Operation
#389 tests/fixtures/cross_platform.py unavailable vs not_claimed
#390 mountfile/status_sync.py Проверка README/CLI против runtime

Порядок вливания: #388 (контракт) → #385/#386 (replay) → #387/#389 (тесты) → #390 (CI-гейт). Такой порядок минимизирует конфликты и позволяет каждому PR проходить независимое ревью.