"""NTFS $LogFile redo/undo draft v2 — патч к черновику #510 (блок C).

Отменяет упрощение «один payload на redo и undo»: redo и undo — два
независимых слайса (op-код, смещение, длина) внутри одной LFS-записи, а
undo-проход идёт по цепочке client_undo_next_lsn, а не по обратному списку
записей транзакции. Все смещения сверены с fs/ntfs3/fslog.c
(torvalds/linux): LFS_RECORD_HDR == 0x30, LOG_REC_HDR == 0x20 — там же
стоят static_assert'ы.

Draft: живёт в docs/ci-drafts/ до решения по #386. В runtime не
подключается — изоляцию документов закрепляет CI-гейт #511; единственный
потребитель — golden-тест tests/test_ntfs_logfile_draft_golden.py.

Fail-closed: любое нарушение layout/chain/bounds -> NtfsReplayError, и на
диск не попадает ни один байт.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

SECTOR = 512
RESTART_MAGIC = b"RSTR"
RCRD_MAGIC = b"RCRD"
BAAD_MAGIC = b"BAAD"

LFS_CLIENT_RECORD = 1
LOG_RECORD_MULTI_PAGE = 0x0001

# NTFS_LOG_OPERATION (enum из fslog.c, строки 299-338).
NOOP = 0x00
COMPENSATION_LOG_RECORD = 0x01
UPDATE_NONRESIDENT_VALUE = 0x08
PREPARE_TRANSACTION = 0x19
COMMIT_TRANSACTION = 0x1A
FORGET_TRANSACTION = 0x1B

# LFS_RECORD_HDR (fslog.c:245): this_lsn, client_prev_lsn,
# client_undo_next_lsn, client_data_len, client.seq_num, client.client_idx,
# record_type, transact_id, flags; 6 байт выравнивания. Ровно 0x30.
LFS_REC_HDR = struct.Struct("<QQQIHHIIH6x")
assert LFS_REC_HDR.size == 0x30

# LOG_REC_HDR (fslog.c:73): redo_op, undo_op, redo_off, redo_len, undo_off,
# undo_len, target_attr, lcns_follow, record_off, attr_off, cluster_off,
# reserved, target_vcn; хвост — page_lcns[lcns_follow]. Ровно 0x20.
LOG_REC_HDR = struct.Struct("<12HQ")
assert LOG_REC_HDR.size == 0x20

# RESTART_AREA (fslog.c:56): current_lsn, log_clients, client_idx[2], flags,
# seq_num_bits, ra_len, client_off, l_size, last_lsn_data_len, rec_hdr_len,
# data_off.
RESTART_AREA = struct.Struct("<QHHHHIHHQIHH")
assert RESTART_AREA.size == 0x28


class NtfsReplayError(RuntimeError):
    pass


@dataclass
class NtfsReplayResult:
    redo_applied: int
    undo_applied: int
    transactions_committed: int
    transactions_rolled_back: int


@dataclass
class LogRecord:
    lsn: int
    prev_lsn: int
    undo_next: int
    txid: int
    redo_op: int
    redo_data: bytes
    undo_op: int
    undo_data: bytes
    lcns_follow: int
    record_off: int
    cluster_off: int
    target_vcn: int
    page_lcns: tuple = field(default_factory=tuple)
    body_len: int = 0


def _apply_usa(page: bytes, page_size: int) -> bytes:
    """Fix-up protection (USA): восстановить значения по секторам страницы."""
    if len(page) < page_size:
        raise NtfsReplayError("short page for USA fixup")
    usa_off = struct.unpack_from("<H", page, 4)[0]
    usa_count = struct.unpack_from("<H", page, 6)[0]
    if usa_off < 8 or usa_off + usa_count * 2 > len(page):
        raise NtfsReplayError(f"invalid USA (off={usa_off}, count={usa_count})")
    buf = bytearray(page)
    for i in range(1, usa_count):
        end = i * SECTOR - 2
        if end + 2 > len(buf):
            break
        stored = bytes(buf[usa_off + i * 2 : usa_off + i * 2 + 2])
        if bytes(buf[end : end + 2]) != stored:
            raise NtfsReplayError(f"USA mismatch at sector {i}")
        buf[end : end + 2] = stored
    return bytes(buf)


class NtfsLogReplayer:
    """Redo/undo по $LogFile. Только для disposable-копии образа."""

    def __init__(self, image_path, offset: int = 0, cluster_size: int = 4096):
        self.image_path = image_path
        self.offset = offset
        self.cluster_size = cluster_size

    # ------------------------------------------------------------------ API

    def replay(self, logfile_data: bytes) -> NtfsReplayResult:
        ra, page_size = self._parse_restart(logfile_data)
        if not ra["flags"] & 1:
            return NtfsReplayResult(0, 0, 0, 0)
        records = self._parse_records(logfile_data, ra, page_size)

        redo = undo = committed = rolled = 0
        by_txn: dict[int, list[LogRecord]] = {}
        for rec in records.values():
            by_txn.setdefault(rec.txid, []).append(rec)

        for txn_records in by_txn.values():
            if any(
                r.redo_op in (COMMIT_TRANSACTION, FORGET_TRANSACTION)
                for r in txn_records
            ):
                # Применяем только рабочие записи; COMM/FORG сами по себе
                # данных не несут и на диск не пишутся.
                for rec in sorted(txn_records, key=lambda r: r.lsn):
                    if rec.redo_op in (
                        COMMIT_TRANSACTION,
                        PREPARE_TRANSACTION,
                        FORGET_TRANSACTION,
                    ):
                        continue
                    redo += self._apply(rec, use_redo=True)
                committed += 1
            else:
                # Undo: по цепочке client_undo_next_lsn от хвостовой записи.
                # Цепочка материализуется целиком ДО первого применения:
                # разрыв в середине не должен оставлять частично
                # откаченную транзакцию (all-or-nothing, fail-closed).
                start = max(txn_records, key=lambda r: r.lsn)
                chain = list(self._undo_chain(records, start))
                for rec in chain:
                    undo += self._apply(rec, use_redo=False)
                rolled += 1
        return NtfsReplayResult(redo, undo, committed, rolled)

    # ------------------------------------------------------------- parsing

    def _parse_restart(self, data: bytes) -> tuple[dict, int]:
        if data[:4] not in (RESTART_MAGIC, BAAD_MAGIC):
            raise NtfsReplayError("not a $LogFile (missing RSTR)")
        if len(data) < SECTOR:
            raise NtfsReplayError("$LogFile shorter than one sector")
        page = _apply_usa(data[:SECTOR], SECTOR)
        sys_page, page_size = struct.unpack_from("<II", page, 0x10)
        ra_off = struct.unpack_from("<H", page, 0x18)[0]
        for name, value in (("sys_page", sys_page), ("page_size", page_size)):
            if value < SECTOR or value & (value - 1):
                raise NtfsReplayError(f"implausible {name}={value}")
        if ra_off + RESTART_AREA.size > SECTOR - 2:
            raise NtfsReplayError(f"restart area out of first sector (ra_off={ra_off})")
        ra_page = (
            _apply_usa(data[:page_size], page_size) if page_size > SECTOR else page
        )
        area = dict(
            zip(
                (
                    "current_lsn",
                    "log_clients",
                    "client_idx0",
                    "client_idx1",
                    "flags",
                    "seq_num_bits",
                    "ra_len",
                    "client_off",
                    "l_size",
                    "last_lsn_data_len",
                    "rec_hdr_len",
                    "data_off",
                ),
                RESTART_AREA.unpack_from(ra_page, ra_off),
            )
        )
        if area["log_clients"] > 1:
            raise NtfsReplayError(
                "multi-client $LogFile is not supported (fail-closed)"
            )
        if area["rec_hdr_len"] != LFS_REC_HDR.size:
            raise NtfsReplayError(f"unexpected rec_hdr_len={area['rec_hdr_len']}")
        return area, page_size

    def _parse_records(
        self, data: bytes, ra: dict, page_size: int
    ) -> dict[int, LogRecord]:
        """RCRD-страницы идут после двух restart-копий; записи — от data_off."""
        records: dict[int, LogRecord] = {}
        pos = 2 * page_size
        while pos + page_size <= len(data):
            page = data[pos : pos + page_size]
            pos += page_size
            if page[:4] == RESTART_MAGIC or page[:4] == BAAD_MAGIC or not any(page[:4]):
                continue
            if page[:4] != RCRD_MAGIC:
                break
            fixed = _apply_usa(page, page_size)
            cur = ra["data_off"]
            while cur + LFS_REC_HDR.size <= page_size:
                rec = self._parse_record(fixed, cur)
                if rec is None:
                    break
                if rec.lsn in records:
                    raise NtfsReplayError(f"duplicate LSN {rec.lsn:#x}")
                records[rec.lsn] = rec
                cur = (cur + LFS_REC_HDR.size + rec.body_len + 7) & ~7
        return records

    def _parse_record(self, page: bytes, cur: int) -> LogRecord | None:
        (
            this_lsn,
            prev_lsn,
            undo_next,
            data_len,
            _seq,
            _cidx,
            rec_type,
            txid,
            flags,
        ) = LFS_REC_HDR.unpack_from(page, cur)
        if rec_type == 0 and not any(page[cur : cur + LFS_REC_HDR.size]):
            return None  # хвост страницы
        if rec_type != LFS_CLIENT_RECORD:
            return None  # restart-записи в клиентском проходе не применяются
        if flags & LOG_RECORD_MULTI_PAGE:
            raise NtfsReplayError(
                f"multi-page log record at LSN {this_lsn:#x} (fail-closed, bounded profile)"
            )
        body_off = cur + LFS_REC_HDR.size
        if data_len < LOG_REC_HDR.size or body_off + data_len > len(page):
            raise NtfsReplayError(f"bad record body at LSN {this_lsn:#x}")
        (
            redo_op,
            undo_op,
            redo_off,
            redo_len,
            undo_off,
            undo_len,
            _target_attr,
            lcns_follow,
            record_off,
            _attr_off,
            cluster_off,
            _rsv,
            target_vcn,
        ) = LOG_REC_HDR.unpack_from(page, body_off)

        def _slice(off: int, ln: int, which: str) -> bytes:
            if off + ln > data_len:
                raise NtfsReplayError(
                    f"{which} slice out of body at LSN {this_lsn:#x} "
                    f"(off={off}, len={ln}, body={data_len})"
                )
            return bytes(page[body_off + off : body_off + off + ln])

        redo_data = _slice(redo_off, redo_len, "redo") if redo_len else b""
        undo_data = _slice(undo_off, undo_len, "undo") if undo_len else b""
        lcns: tuple = ()
        if lcns_follow:
            if LOG_REC_HDR.size + 8 * lcns_follow > data_len:
                raise NtfsReplayError(f"page_lcns out of body at LSN {this_lsn:#x}")
            lcns = struct.unpack_from(
                f"<{lcns_follow}Q", page, body_off + LOG_REC_HDR.size
            )
        return LogRecord(
            lsn=this_lsn,
            prev_lsn=prev_lsn,
            undo_next=undo_next,
            txid=txid,
            redo_op=redo_op,
            redo_data=redo_data,
            undo_op=undo_op,
            undo_data=undo_data,
            lcns_follow=lcns_follow,
            record_off=record_off,
            cluster_off=cluster_off,
            target_vcn=target_vcn,
            page_lcns=lcns,
            body_len=data_len,
        )

    # ------------------------------------------------------------ undo chain

    def _undo_chain(
        self, records: dict[int, LogRecord], start: LogRecord
    ) -> Iterator[LogRecord]:
        """Обход client_undo_next_lsn: Noop/CLR рулят цепочку, разрыв — отказ."""
        lsn, seen = start.lsn, set()
        while lsn:
            if lsn in seen:
                raise NtfsReplayError(f"undo chain loop at LSN {lsn:#x}")
            seen.add(lsn)
            rec = records.get(lsn)
            if rec is None:
                raise NtfsReplayError(f"undo chain broken at LSN {lsn:#x}")
            if rec.undo_op not in (NOOP, COMPENSATION_LOG_RECORD):
                yield rec
            lsn = rec.undo_next

    # ---------------------------------------------------------------- apply

    def _apply(self, rec: LogRecord, use_redo: bool) -> int:
        op = rec.redo_op if use_redo else rec.undo_op
        data = rec.redo_data if use_redo else rec.undo_data
        if op in (NOOP, COMPENSATION_LOG_RECORD) or not data:
            return 0
        if op in (COMMIT_TRANSACTION, PREPARE_TRANSACTION) or not rec.page_lcns:
            raise NtfsReplayError(
                f"unsupported log operation {op:#x} at LSN {rec.lsn:#x} (fail-closed)"
            )
        intra = (rec.cluster_off << 9) + rec.record_off
        page_idx = intra // self.cluster_size
        if page_idx >= rec.lcns_follow:
            raise NtfsReplayError(
                f"page index {page_idx} beyond page_lcns at LSN {rec.lsn:#x}"
            )
        if intra + len(data) > rec.lcns_follow * self.cluster_size:
            raise NtfsReplayError(
                f"data span beyond mapped clusters at LSN {rec.lsn:#x}"
            )
        target = (
            self.offset
            + rec.page_lcns[page_idx] * self.cluster_size
            + intra % self.cluster_size
        )
        self._write_at(target, data)
        return 1

    def _write_at(self, target: int, data: bytes) -> None:
        with open(self.image_path, "r+b") as f:
            f.seek(0, 2)
            size = f.tell()
            if target + len(data) > size:
                raise NtfsReplayError(f"target {target:#x} beyond volume (size={size})")
            f.seek(target)
            f.write(data)
