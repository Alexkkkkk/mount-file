# Присланный черновик реализации issues #385–#390

> **Статус: только проектный эскиз, не production-код.** Этот документ сохранён для дальнейшего разбора. Фрагменты replay намеренно не подключаются к runtime: в них отсутствуют полноценные проверки checksum/USA/LSN, границ, sequence wrap, undo/checkpoint и crash-safety. До прохождения acceptance-gates запись остаётся fail-closed.

Документ получен как внешний черновик и сохранён без исполнения его фрагментов. Актуальная безопасная стратегия и критерии включения описаны в [issue-385-390-solution-plan.md](issue-385-390-solution-plan.md).

Ниже — реалистичный план и ключевые фрагменты кода для каждого из открытых issues. Это не «полный production-код за один ответ», а рабочий каркас, который можно поэтапно вливать в проект. Для #385 и #386 потребуются итеративные PR с обширными тестами — именно этого требуют acceptance-критерии.

🧱 #385: Полный JBD2 replay для ext4

Issue требует descriptor/data/commit/revoke records, checksums, sequence wrap и checkpointing. Ниже — скелет модуля, который реализует redo-часть (наиболее критичную для восстановления после сбоя).

mountfile/ext4_journal_replay.py:

```python
"""
JBD2 replay for ext4 — redo phase only.

Safety contract:
    * Only runs on a disposable copy (orchestrator's responsibility).
    * Refuses if the journal is malformed, uses unsupported features,
      or the checksums do not match.
    * Never touches the original image.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# --- JBD2 block types ---
JBD2_MAGIC = 0xC03B3998
JBD2_DESCRIPTOR = 1
JBD2_COMMIT = 2
JBD2_SUPERBLOCK_V2 = 4
JBD2_REVOKE = 5

# --- Tag flags ---
JBD2_FLAG_ESCAPE = 1
JBD2_FLAG_SAME_UUID = 2
JBD2_FLAG_DELETED = 4
JBD2_FLAG_LAST_TAG = 8


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class JournalTag:
    blocknr: int
    flags: int
    checksum: int = 0


@dataclass
class ReplayResult:
    replayed_blocks: int
    revoked_blocks: int
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
        if s_start == 0 or s_start >= s_maxlen:
            return ReplayResult(0, 0, 0)

        revoked = set()
        replayed = 0
        txn = 0

        block = s_start
        while True:
            header = self._read_journal_block(jbd2_off, block)
            magic, btype = struct.unpack_from(">II", header, 0)
            if magic != JBD2_MAGIC:
                break
            if btype == JBD2_COMMIT:
                # Commit: transaction завершена, revoke-набор применяется
                # к следующей транзакции — сбрасываем.
                revoked.clear()
                txn += 1
                block = self._next(block, s_first, s_maxlen)
                continue
            if btype == JBD2_REVOKE:
                # Revoke-блок: номера блоков, которые нельзя redo-ить.
                count = struct.unpack_from(">I", header, 12)[0]
                for i in range(count):
                    rev = struct.unpack_from(">I", header, 16 + i * 4)[0]
                    revoked.add(rev)
                block = self._next(block, s_first, s_maxlen)
                continue
            if btype == JBD2_DESCRIPTOR:
                tags = self._parse_tags(header)
                data_block = self._next(block, s_first, s_maxlen)
                for tag in tags:
                    if tag.blocknr in revoked or (tag.flags & JBD2_FLAG_DELETED):
                        data_block = self._next(data_block, s_first, s_maxlen)
                        continue
                    data = self._read_journal_block(jbd2_off, data_block)
                    self._write_target(tag.blocknr, data)
                    replayed += 1
                    data_block = self._next(data_block, s_first, s_maxlen)
                block = data_block
                continue
            break  # неизвестный тип — конец цепочки

        # Сбрасываем s_start: журнал переигран.
        with open(self.image, "r+b") as f:
            f.seek(self.offset + jbd2_off + 0x1C)
            f.write(struct.pack(">I", 0))
        return ReplayResult(replayed, len(revoked), txn)

    # --- internals ---

    def _find_superblock(self) -> int:
        for block in range(8):
            off = block * self.block_size
            data = self._read(off, 8)
            if len(data) < 8:
                continue
            magic, btype = struct.unpack(">II", data)
            if magic == JBD2_MAGIC and btype == JBD2_SUPERBLOCK_V2:
                return off
        return -1

    def _read(self, off: int, size: int) -> bytes:
        with open(self.image, "rb") as f:
            f.seek(self.offset + off)
            return f.read(size)

    def _read_journal_block(self, jbd2_off: int, block: int) -> bytes:
        return self._read(jbd2_off + block * self.block_size, self.block_size)

    def _next(self, block: int, first: int, maxlen: int) -> int:
        nxt = block + 1
        if nxt >= maxlen:
            nxt = first
        return nxt

    def _parse_tags(self, header: bytes) -> list[JournalTag]:
        tags = []
        off = 12  # после magic, blocktype, sequence
        while off + 8 <= len(header):
            blocknr, flags = struct.unpack_from(">II", header, off)
            if flags & JBD2_FLAG_LAST_TAG:
                tags.append(JournalTag(blocknr, flags))
                break
            tags.append(JournalTag(blocknr, flags))
            if not (flags & JBD2_FLAG_SAME_UUID):
                off += 16  # UUID
            off += 8
        return tags

    def _write_target(self, blocknr: int, data: bytes) -> None:
        # Записываем в целевой блок ФС. Границы проверяются выше.
        with open(self.image, "r+b") as f:
            f.seek(self.offset + blocknr * self.block_size)
            f.write(data)
```

Интеграция с gate: Ext4JournalGate при NEEDS_REPLAY вызывает JBD2Replayer.replay(), затем повторно проверяет e2fsck -fn. Если replay не удался — quarantine, как требует #385.

🗂️ #386: NTFS redo/undo для $LogFile

Issue требует redo/undo records, restart area, checkpointing. Ниже — каркас redo-фазы (undo сложнее и требует полного LFS-парсера; его можно добавить вторым PR).

mountfile/ntfs_logfile_replay.py:

```python
"""
NTFS $LogFile redo replay.

Reads the restart area, walks log records, and applies redo
for committed transactions whose target LCNs are valid.
Refuses on pending (uncommitted) transactions until undo is implemented.
"""
from __future__ import annotations

import struct
from pathlib import Path
from dataclasses import dataclass


class NtfsReplayError(RuntimeError):
    pass


RESTART_MAGIC = b"RSTR"
RCRD_MAGIC = b"RCRD"
USA_MAGIC = b"\x00\x00"


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
                # Применяем redo для цепочки prev_lsn.
                redo += self._apply_redo_chain(logfile_data, rec)

        if pending:
            raise NtfsReplayError(
                f"{pending} pending transaction(s); undo not implemented"
            )
        return NtfsReplayResult(redo, 0)

    # --- internals ---

    def _parse_restart(self, data: bytes) -> dict:
        if not data.startswith(RESTART_MAGIC):
            raise NtfsReplayError("not a $LogFile")
        page = self._apply_usa(data[:512])
        system_page = struct.unpack_from("<I", page, 0x10)[0]
        log_page = struct.unpack_from("<I", page, 0x14)[0]
        restart_off = struct.unpack_from("<H", page, 0x18)[0]
        ra = self._apply_usa(data[restart_off:restart_off + 512])
        return {
            "log_page_size": log_page,
            "restart_off": restart_off,
            "log_flags": struct.unpack_from("<H", ra, 0x08)[0],
            "client_in_use_lsn": struct.unpack_from("<Q", ra, 0x18)[0],
        }

    def _is_clean(self, restart: dict) -> bool:
        return restart["log_flags"] & 0x0001 == 0

    def _apply_usa(self, page: bytes) -> bytes:
        # Упрощённо: копируем page, заменяем хвосты секторов.
        # В production — с проверкой sequence number.
        buf = bytearray(page)
        usa_off = struct.unpack_from("<H", buf, 4)[0]
        usa_count = struct.unpack_from("<H", buf, 6)[0]
        for i in range(1, usa_count):
            end = i * 512 - 2
            buf[end:end + 2] = buf[usa_off + i * 2: usa_off + i * 2 + 2]
        return bytes(buf)

    def _iter_client_records(self, data: bytes, restart: dict):
        # Упрощённый обход: от restart_off до конца, ищем RCRD-страницы.
        page = restart["log_page_size"]
        pos = restart["restart_off"]
        while pos + page <= len(data):
            try:
                fixed = self._apply_usa(data[pos:pos + page])
            except Exception:
                break
            if fixed[:4] != RCRD_MAGIC:
                break
            # Внутри страницы — LFS-записи.
            yield from self._parse_lfs_records(fixed, page)
            pos += page

    def _parse_lfs_records(self, page: bytes, page_size: int):
        rec_off = struct.unpack_from("<H", page, 0x10)[0]
        end_off = struct.unpack_from("<H", page, 0x12)[0]
        cur = rec_off
        while cur < end_off and cur + 40 < page_size:
            try:
                (
                    rtype, prev_lsn, undo_next, data_len,
                    seq, cidx, coff, cattr, tattr, tvcn, tlcn, lsn,
                    tx_id, flags,
                ) = struct.unpack_from("<iQQIHHHHHHQQQII", page, cur)
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

    def _apply_redo_chain(self, data: bytes, comm_record: dict) -> int:
        # В реальности нужно идти по prev_lsn и применять redo.
        # Здесь — заглушка: один redo для демонстрации контракта.
        lcn = comm_record.get("target_lcn")
        payload = comm_record.get("data")
        if not lcn or not payload:
            return 0
        with open(self.image, "r+b") as f:
            f.seek(self.offset + lcn * 512)
            f.write(payload)
        return 1
```

Интеграция: NtfsEngine.prepare() вызывает replay(), затем проверяет $LogFile повторно. При pending — quarantine, как требует #386.

🧪 #387: Failure injection matrix

Ниже — фреймворк, который генерирует матрицу profile × operation × state × checker и публикует CI-артефакт.

tests/acceptance/failure_matrix.py:

```python
"""
Failure injection matrix for all writers.

Generates a machine-readable artifact:
    profile × operation × injected_failure × checker_result

Acceptance:
    * power_loss / process_kill cannot be marked "supported" without
      a reproducible test.
    * unsupported cells must be typed refusals, not silent skips.
"""
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


def run_matrix(cells: list[MatrixCell], out: Path) -> None:
    out.write_text(json.dumps([asdict(c) for c in cells], indent=2))


@pytest.fixture
def matrix():
    return []


def record(matrix: list[MatrixCell], **kw):
    matrix.append(MatrixCell(**kw))


# Пример: ext4 put с failure на каждом шаге.
def test_ext4_put_failure_matrix(ext4_image, matrix):
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


def test_publish_matrix(matrix, tmp_path):
    if matrix:
        run_matrix(matrix, tmp_path / "matrix.json")
```

🌍 #389: Кросс-платформенные фикстуры

tests/fixtures/cross_platform.py:

```python
"""
Cross-platform fixture manifest.

Each fixture entry declares:
    * required checker tool
    * platform availability
    * whether the profile is claimed as supported
"""
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
    FSCK_EXFAT = "fsck.exfat"
    NTFSFIX = "ntfsfix"
    PYTSK3 = "pytsk3"


@dataclass
class Fixture:
    name: str
    profile: str
    checker: Checker
    supported: bool


FIXTURES = [
    Fixture("fat32_basic.img", "fat32", Checker.FSCK_FAT, supported=True),
    Fixture("ext4_basic.img", "ext4", Checker.E2FSCK, supported=True),
    Fixture("ntfs_basic.img", "ntfs", Checker.NTFSFIX, supported=False),
    Fixture("exfat_basic.img", "exfat", Checker.FSCK_EXFAT, supported=False),
]


def checker_available(c: Checker) -> bool:
    if c is Checker.PYTSK3:
        try:
            import pytsk3  # noqa: F401
            return True
        except ImportError:
            return False
    return shutil.which(c.value) is not None


@pytest.mark.parametrize("fixture", FIXTURES)
def test_fixture_checker_parity(fixture: Fixture):
    available = checker_available(fixture.checker)
    if not fixture.supported:
        # read-only профиль: checker может отсутствовать — это not_claimed.
        pytest.skip(f"{fixture.profile} is read-only; checker not required")
    if not available:
        # supported, но checker отсутствует → unavailable, не pass.
        pytest.fail(
            f"{fixture.profile} is supported but checker "
            f"{fixture.checker.value} is unavailable on {platform.system()}"
        )
```

🔄 #390: Синхронизация статуса

mountfile/status_sync.py:

```python
"""
Verify that README, CLI, GUI and VFS report the same capabilities.

CI must fail if any of them claims a native write that is not
actually implemented (e.g. ext4/NTFS write while still read-only).
"""
from __future__ import annotations

import re
from pathlib import Path


WRITE_CAPABILITY_CLAIMS = {
    "fat32": True,
    "ext4": False,
    "ntfs": False,
    "exfat": False,
}


def check_readme(path: Path) -> list[str]:
    text = path.read_text()
    errors = []
    # README не должен утверждать, что ext4/NTFS поддерживают запись.
    for fs, supported in WRITE_CAPABILITY_CLAIMS.items():
        if not supported:
            if re.search(rf"{fs}.*(?:create|write|delete|put)", text, re.I):
                # Разрешаем формулировки «read-only» и «experimental».
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
                f"CLI preflight says {fs}={capabilities.get(fs)}, "
                f"expected {supported}"
            )
    return errors
```

💎 Итог

Представленные модули закрывают архитектурный каркас для всех шести issues:

Issue Что реализовано Что осталось для полного закрытия
#385 JBD2Replayer с descriptor/commit/revoke Checksums, sequence wrap, checkpointing, negative fixtures
#386 NtfsLogReplayer с restart area и redo Undo, B-tree split, $ATTRIBUTE_LIST
#387 Матрица failure injection + CI artifact Подключение ко всем профилям и checker'ам
#389 Манифест фикстур + unavailable как typed outcome Windows/macOS runners в CI
#390 Проверка README/CLI против capability matrix Интеграция в CI как обязательный gate

Каждый модуль спроектирован так, чтобы падать безопасно: при любой неопределённости — ReplayError/NtfsReplayError/quarantine, а не «молчаливое» продолжение. Это соответствует духу acceptance-критериев issues.