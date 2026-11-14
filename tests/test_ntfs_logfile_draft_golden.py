"""Golden-тесты драфта redo/undo-разделения $LogFile (патч к #510, блок C).

Синтетическая $LogFile с одной RCRD-страницей, где redo_data != undo_data:
это ловит главное упрощение исходного драфта (#510) на первом же прогоне —
та версия записала бы redo-данные в качестве undo.
"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

import pytest

_DRAFT = (
    Path(__file__).resolve().parent.parent
    / "docs"
    / "ci-drafts"
    / "ntfs_logfile_replay_v2.py"
)
_SPEC = importlib.util.spec_from_file_location("ntfs_logfile_replay_v2", _DRAFT)
mod = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mod  # нужно dataclasses: модуль должен быть в sys.modules
_SPEC.loader.exec_module(mod)

PAGE = 512
CLUSTER = 4096
DATA_OFF = 0x40
LCN = 3  # целевой кластер в образе
REDO = b"REDO-NEW-VALUE"
UNDO = b"UNDO-OLD-VALUE"


def _record(
    lsn: int, prev: int, undo_next: int, txid: int, redo_op: int, undo_op: int
) -> bytes:
    """LFS-запись: 0x30 заголовок + 0x20 LOG_REC_HDR + page_lcns + слайсы."""
    redo_off = 0x20 + 8  # LOG_REC_HDR + один page_lcn
    body_len = redo_off + len(REDO) + len(UNDO)
    hdr = struct.pack("<QQQIHHIIH6x", lsn, prev, undo_next, body_len, 1, 0, 1, txid, 0)
    body = (
        struct.pack(
            "<12HQ",
            redo_op,
            undo_op,
            redo_off,
            len(REDO),  # redo_off / redo_len
            redo_off + len(REDO),
            len(UNDO),  # undo_off / undo_len
            0,  # target_attr
            1,  # lcns_follow
            0,  # record_off
            0,  # attr_off
            0,  # cluster_off
            0,  # reserved
            0,  # target_vcn
        )
        + struct.pack("<Q", LCN)
        + REDO
        + UNDO
    )
    return hdr + body


def _rcrd_page(records: list[bytes]) -> bytes:
    page = bytearray(PAGE)
    page[0:4] = mod.RCRD_MAGIC
    struct.pack_into("<H", page, 4, 0x28)  # usa_off
    struct.pack_into("<H", page, 6, 1)  # usa_count=1: фиксапов нет
    cur = DATA_OFF
    for rec in records:
        page[cur : cur + len(rec)] = rec
        cur = (cur + len(rec) + 7) & ~7
    return bytes(page)


def _restart_page(dirty: bool = True) -> bytes:
    page = bytearray(PAGE)
    page[0:4] = mod.RESTART_MAGIC
    struct.pack_into("<H", page, 4, 0x28)
    struct.pack_into("<H", page, 6, 1)
    struct.pack_into("<II", page, 0x10, PAGE, PAGE)  # sys_page, page_size
    struct.pack_into("<H", page, 0x18, 0x30)  # ra_off
    ra = mod.RESTART_AREA.pack(
        0x40,  # current_lsn
        1,  # log_clients
        0xFFFF,
        0,  # client_idx free/use
        1 if dirty else 0,
        64,  # seq_num_bits
        0x28,  # ra_len
        0x30,  # client_off
        PAGE * 64,  # l_size
        0,  # last_lsn_data_len
        0x30,  # rec_hdr_len
        DATA_OFF,  # data_off
    )
    page[0x30 : 0x30 + len(ra)] = ra
    return bytes(page)


def _logfile(records: list[bytes], dirty: bool = True) -> bytes:
    return _restart_page(dirty) + _restart_page(dirty) + _rcrd_page(records)


@pytest.fixture()
def image(tmp_path):
    path = tmp_path / "image.ntfs"
    path.write_bytes(b"\x00" * 8 * CLUSTER)
    return path


def test_undo_uses_undo_slice_not_redo(image):
    """UNDO-слайс пишется на диск, а не REDO (упрощение из #510 ловится здесь)."""
    rec = _record(
        0x10,
        0,
        0,
        txid=7,
        redo_op=mod.UPDATE_NONRESIDENT_VALUE,
        undo_op=mod.UPDATE_NONRESIDENT_VALUE,
    )
    result = mod.NtfsLogReplayer(image, cluster_size=CLUSTER).replay(_logfile([rec]))
    assert result.redo_applied == 0
    assert result.undo_applied == 1
    assert result.transactions_rolled_back == 1
    data = image.read_bytes()
    target = LCN * CLUSTER  # record_off=0, cluster_off=0 -> начало кластера
    assert data[target : target + len(UNDO)] == UNDO
    assert data[target : target + len(REDO)] != REDO


def test_redo_uses_redo_slice_for_committed_txn(image):
    data_rec = _record(
        0x10,
        0,
        0x30,
        txid=7,
        redo_op=mod.UPDATE_NONRESIDENT_VALUE,
        undo_op=mod.UPDATE_NONRESIDENT_VALUE,
    )
    commit = _record(
        0x30, 0x10, 0, txid=7, redo_op=mod.COMMIT_TRANSACTION, undo_op=mod.NOOP
    )
    result = mod.NtfsLogReplayer(image, cluster_size=CLUSTER).replay(
        _logfile([data_rec, commit])
    )
    assert result.redo_applied == 1
    assert result.transactions_committed == 1
    data = image.read_bytes()
    target = LCN * CLUSTER
    assert data[target : target + len(REDO)] == REDO


def test_broken_undo_chain_refused(image):
    """undo_next указывает на несуществующий LSN -> отказ, на диск ничего не пишется."""
    rec = _record(
        0x10,
        0,
        0x99,
        txid=7,
        redo_op=mod.UPDATE_NONRESIDENT_VALUE,
        undo_op=mod.UPDATE_NONRESIDENT_VALUE,
    )
    with pytest.raises(mod.NtfsReplayError, match="undo chain broken"):
        mod.NtfsLogReplayer(image, cluster_size=CLUSTER).replay(_logfile([rec]))
    assert image.read_bytes() == b"\x00" * 8 * CLUSTER


def test_out_of_body_slice_refused(image):
    """redo-слайс за пределами тела записи -> отказ до всякой записи."""
    rec = _record(0x10, 0, 0, txid=7, redo_op=0xFF, undo_op=mod.NOOP)
    raw = bytearray(_logfile([rec]))
    body_off = 2 * PAGE + DATA_OFF + 0x30
    struct.pack_into("<H", raw, body_off + 6, 0xFFF)  # redo_len -> за тело
    with pytest.raises(mod.NtfsReplayError, match="out of body"):
        mod.NtfsLogReplayer(image, cluster_size=CLUSTER).replay(bytes(raw))


def test_clean_logfile_is_noop(image):
    result = mod.NtfsLogReplayer(image, cluster_size=CLUSTER).replay(
        _logfile([], dirty=False)
    )
    assert result == mod.NtfsReplayResult(0, 0, 0, 0)
    assert image.read_bytes() == b"\x00" * 8 * CLUSTER


def test_multi_page_record_refused(image):
    rec = bytearray(
        _record(
            0x10,
            0,
            0,
            txid=7,
            redo_op=mod.UPDATE_NONRESIDENT_VALUE,
            undo_op=mod.UPDATE_NONRESIDENT_VALUE,
        )
    )
    struct.pack_into("<H", rec, 0x28, mod.LOG_RECORD_MULTI_PAGE)  # flags
    with pytest.raises(mod.NtfsReplayError, match="multi-page"):
        mod.NtfsLogReplayer(image, cluster_size=CLUSTER).replay(_logfile([bytes(rec)]))
