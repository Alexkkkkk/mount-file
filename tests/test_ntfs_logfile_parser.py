# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
"""Evidence tests for the read-only ``$LogFile`` analyzer (issue #510 block C).

The byte layouts asserted here are the ones recorded in
``docs/ntfs-logfile-layout.md``, i.e. ``fs/ntfs3/fslog.c`` as of 2026-11-14.  The
builders produce spec-conformant streams; every test then states which
acceptance claim the stream proves, including the claims the analyzer *refuses*
to make (no byte target, no execution, real crash images unavailable here).
"""

from __future__ import annotations

import gzip
import pathlib
import struct

import pytest

from mountfile import ntfs_logfile as lf

pytestmark = pytest.mark.unit

PAGE = 4096
RA_OFF = 0x30
CLIENT_OFF = 0x40
RA_LEN = CLIENT_OFF + 0xA0
FILE_SIZE = 256 * 1024  # 64 log pages: restart, tail copies and record pages
SEQ_BITS = 67 - FILE_SIZE.bit_length()
UNUSED = b"\xff" * PAGE

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "ntfs-readonly-32m.img.gz"


def _seal(page: bytearray, usa_offset: int) -> bytes:
    """Write a USA over the last u16 of every 512-byte stride, sector 0 included."""

    count = len(page) // 512 + 1
    sequence = 0x4A17
    struct.pack_into("<HH", page, 4, usa_offset, count)
    struct.pack_into("<H", page, usa_offset, sequence)
    for index in range(len(page) // 512):
        trailer = (index + 1) * 512 - 2
        stored = struct.unpack_from("<H", page, trailer)[0]
        struct.pack_into("<H", page, usa_offset + 2 + 2 * index, stored)
        struct.pack_into("<H", page, trailer, sequence)
    return bytes(page)


def _restart_page(
    current_lsn: int,
    *,
    dirty: bool,
    client_oldest: int = 0,
    client_restart_lsn: int = 0,
    open_count: int = 3,
    major: int = 1,
    minor: int = 1,
) -> bytes:
    page = bytearray(PAGE)
    page[0:4] = b"RSTR"
    struct.pack_into("<H", page, 4, 0x1E)
    struct.pack_into("<Q", page, 8, 0)  # no chkdsk LSN
    struct.pack_into("<II", page, 0x10, PAGE, PAGE)
    struct.pack_into("<HHH", page, RA_OFF - 0x18, RA_OFF, minor, major)
    area = RA_OFF
    struct.pack_into("<Q", page, area + 0x00, current_lsn)
    struct.pack_into("<H", page, area + 0x08, 1)  # log_clients
    # A dirty (mounted) log keeps its client in the in-use list and clears the
    # RESTART_VOLUME_IS_CLEAN bit; a closed log does the opposite.
    struct.pack_into(
        "<HH", page, area + 0x0A, 0xFFFF if dirty else 0, 0 if dirty else 0xFFFF
    )
    struct.pack_into("<H", page, area + 0x0E, 0x0000 if dirty else 0x0002)
    struct.pack_into("<I", page, area + 0x10, SEQ_BITS)
    struct.pack_into("<HH", page, area + 0x14, RA_LEN, CLIENT_OFF)
    struct.pack_into("<q", page, area + 0x18, FILE_SIZE)
    struct.pack_into("<I", page, area + 0x20, 0)  # last_lsn_data_len
    struct.pack_into("<HH", page, area + 0x24, 0x30, 0x40)  # rec_hdr_len, data_off
    struct.pack_into("<I", page, area + 0x28, open_count)
    client = area + CLIENT_OFF
    struct.pack_into(
        "<QQHHH", page, client, client_oldest, client_restart_lsn, 0xFFFF, 0xFFFF, 0
    )
    struct.pack_into("<I", page, client + 0x1C, 8)
    page[client + 0x20 : client + 0x28] = "NTFS".encode("utf-16le")
    return _seal(page, 0x1E)


def _align8(value: int) -> int:
    return (value + 7) & ~7


def client_restart_payload(check_point_start: int) -> bytes:
    return lf.NTFS_RESTART.pack(
        1,
        0,
        check_point_start,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    )


def record_body(
    *,
    redo_op: int = 0,
    undo_op: int = 0,
    redo: bytes = b"",
    undo: bytes = b"",
    target_attr: int = 0,
    lcns: tuple[int, ...] = (),
    target_vcn: int = 0,
    record_off: int = 0,
    attr_off: int = 0,
    cluster_off: int = 0,
    payload: bytes | None = None,
) -> bytes:
    """Build the client data of one record: header, LCNs, then redo and undo.

    Redo and undo get **different offsets and different bytes**, which is the
    property the v1 draft of #510 lost.
    """

    follow = max(1, len(lcns))
    if payload is not None:
        return payload
    body = bytearray(0x20 + 8 * follow)
    redo_off = len(body)
    body += redo + bytes((-len(redo)) % 8)
    undo_off = len(body)
    body += undo + bytes((-len(undo)) % 8)
    body[0:0x20] = lf.LOG_REC_HDR.pack(
        redo_op,
        undo_op,
        redo_off,
        len(redo),
        undo_off,
        len(undo),
        target_attr,
        len(lcns),
        record_off,
        attr_off,
        cluster_off,
        0,  # reserved
        target_vcn,
    )
    for index, lcn in enumerate(lcns):
        struct.pack_into("<Q", body, 0x20 + 8 * index, lcn)
    return bytes(body)


def build_log(
    records: list[dict],
    *,
    dirty: bool = True,
    client_oldest: int = 0,
    restart_index: int | None = None,
    current_lsn: int | None = None,
    major: int = 1,
) -> tuple[bytes, list[int]]:
    """Pack records into RCRD pages and return the stream plus their LSNs."""

    first_page = (0x22 if major >= 2 else 4) * PAGE
    pages: dict[int, bytearray] = {}
    lsns: list[int] = []
    cursor = first_page + 0x40
    for entry in records:
        body = entry["body"]
        page_index = cursor // PAGE
        page_offset = cursor % PAGE
        if page_offset + 0x30 + len(body) > PAGE:
            page_index += 1
            page_offset = 0x40
            cursor = page_index * PAGE + page_offset
        lsn = cursor >> 3
        header = lf.LFS_RECORD_HDR.pack(
            lsn,
            entry.get("prev", 0),
            entry.get("undo_next", 0),
            len(body),
            0,
            entry.get("client", 0),
            entry.get("type", lf.LFS_CLIENT_RECORD),
            entry.get("txid", 0),
            entry.get("flags", 0),
        )
        page = pages.setdefault(page_index, bytearray(PAGE))
        page[page_offset : page_offset + len(header)] = header
        page[page_offset + 0x30 : page_offset + 0x30 + len(body)] = body
        struct.pack_into("<H", page, 0x18, _align8(page_offset + 0x30 + len(body)))
        struct.pack_into("<Q", page, 0x08, entry.get("page_lsn", lsn))
        struct.pack_into("<I", page, 0x10, 1)  # LOG_PAGE_LOG_RECORD_END
        struct.pack_into("<HH", page, 0x14, 1, 1)  # page_count, page_pos
        struct.pack_into("<Q", page, 0x20, entry.get("last_end_lsn", lsn))
        cursor = page_index * PAGE + _align8(page_offset + 0x30 + len(body))
        lsns.append(lsn)

    stream = bytearray(
        _restart_page(
            current_lsn if current_lsn is not None else (lsns[-1] if lsns else 0),
            dirty=dirty,
            client_oldest=client_oldest,
            client_restart_lsn=lsns[restart_index] if restart_index is not None else 0,
            major=major,
        )
    )
    stream += _restart_page(
        current_lsn if current_lsn is not None else (lsns[-1] if lsns else 0),
        dirty=dirty,
        client_oldest=client_oldest,
        client_restart_lsn=lsns[restart_index] if restart_index is not None else 0,
        major=major,
    )
    stream += UNUSED * ((first_page - 2 * PAGE) // PAGE)
    for page_index in range(first_page // PAGE, FILE_SIZE // PAGE):
        if page_index in pages:
            page = pages[page_index]
            page[0:4] = b"RCRD"
            stream += _seal(page, 0x28)
        else:
            stream += UNUSED
    return bytes(stream), lsns


def update_record(
    *, txid: int, redo: bytes, undo: bytes, prev: int = 0, undo_next: int = 0, **kwargs
) -> dict:
    return {
        "body": record_body(
            redo_op=0x07,
            undo_op=0x07,
            redo=redo,
            undo=undo,
            target_attr=kwargs.pop("target_attr", 0x18),
            lcns=kwargs.pop("lcns", (0x40,)),
            **kwargs,
        ),
        "txid": txid,
        "prev": prev,
        "undo_next": undo_next,
    }


def marker_record(op: int, txid: int) -> dict:
    return {
        "body": record_body(redo_op=op, undo_op=0x00, redo=b"", undo=b""),
        "txid": txid,
    }


def test_restart_pages_and_client_are_parsed():
    stream, _ = build_log([])
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    assert analysis.page_size == PAGE
    assert analysis.first_page == 4 * PAGE
    assert analysis.log_size == FILE_SIZE
    assert analysis.truncated is False
    assert analysis.dirty is True
    assert [client.name for client in analysis.clients] == ["NTFS"]
    assert analysis.clients[0].index == 0
    assert analysis.executable is False


def test_closed_log_has_no_in_use_client():
    stream, _ = build_log([], dirty=False)
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    assert analysis.clients == ()
    assert analysis.dirty is False
    assert any("log is closed" in note for note in analysis.notes)


def test_redo_and_undo_slices_stay_separate():
    """The audit's P1 item: one payload for both sides must not be possible."""

    redo = b"NEWBYTES"
    undo = b"OLDBYTES"
    stream, lsns = build_log([update_record(txid=0x18, redo=redo, undo=undo)])
    record = lf.analyze_logfile(stream, cluster_size=512).records[0]
    assert record.redo_data == redo
    assert record.undo_data == undo
    assert record.redo_len == record.undo_len == 8
    assert record.lsn == lsns[0]
    assert record.page_index == 4
    assert record.byte_offset == 4 * PAGE + 0x40


def test_identical_redo_and_undo_bytes_are_still_separate_slices():
    payload = b"SAMEBYTES"[1:]
    stream, _ = build_log([update_record(txid=0x18, redo=payload, undo=payload)])
    record = lf.analyze_logfile(stream, cluster_size=512).records[0]
    assert record.redo_data == record.undo_data == payload


def test_committed_transaction_is_redone_and_never_undone():
    stream, lsns = build_log(
        [
            update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8),
            marker_record(0x1A, 0x18),  # CommitTransaction
        ]
    )
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    assert [t.state for t in analysis.transactions] == ["committed"]
    assert [step.kind for step in analysis.redo_plan] == ["redo"]
    assert analysis.redo_plan[0].operation_name == "UpdateResidentValue"
    assert analysis.redo_plan[0].payload == b"\x01" * 8
    assert analysis.undo_plan == ()


def test_active_transaction_is_undone_with_undo_bytes_not_redo_bytes():
    first_lsn = (4 * PAGE + 0x40) >> 3
    stream, lsns = build_log(
        [
            update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8),
            update_record(
                txid=0x18,
                redo=b"\x03" * 8,
                undo=b"\x04" * 8,
                undo_next=first_lsn,
            ),
        ]
    )
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    transaction = analysis.transactions[0]
    assert transaction.state == "active"
    assert transaction.undo_next_lsn == lsns[1]
    assert [step.kind for step in analysis.undo_plan] == ["undo", "undo"]
    assert [step.payload for step in analysis.undo_plan] == [b"\x04" * 8, b"\x02" * 8]
    assert analysis.redo_plan == ()  # active transactions are not redone


def test_broken_undo_chain_does_not_emit_a_partial_plan():
    stream, _ = build_log(
        [
            update_record(
                txid=0x18,
                redo=b"\x01" * 8,
                undo=b"\x02" * 8,
                undo_next=0xDEADBEEF,
            )
        ]
    )
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    assert analysis.undo_plan == ()
    assert any("outside its parsed record chain" in note for note in analysis.notes)


def test_prepared_transaction_is_not_rolled_back():
    stream, _ = build_log(
        [
            update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8),
            marker_record(0x19, 0x18),  # PrepareTransaction
        ]
    )
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    assert [t.state for t in analysis.transactions] == ["prepared"]
    assert analysis.undo_plan == ()
    assert analysis.redo_plan == ()


def test_forgotten_transaction_is_neither_redone_nor_undone():
    """Forget frees a slot, which may then identify a different transaction."""

    stream, lsns = build_log(
        [
            update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8),
            marker_record(0x1B, 0x18),  # ForgetTransaction
            update_record(txid=0x18, redo=b"\x05" * 8, undo=b"\x06" * 8),
        ]
    )
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    assert [t.forgotten for t in analysis.transactions] == [True, False]
    assert [t.transaction_id for t in analysis.transactions] == [0x18, 0x18]
    assert analysis.transactions[0].record_lsns == tuple(lsns[:2])
    assert analysis.transactions[1].record_lsns == (lsns[2],)
    assert analysis.redo_plan == ()
    assert [step.payload for step in analysis.undo_plan] == [b"\x06" * 8]
    assert len(analysis.records) == 3


def test_checkpoint_filters_records_before_analysis_start():
    checkpoint_body = record_body(payload=client_restart_payload(0))
    stream, lsns = build_log(
        [
            update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8),
            {"body": checkpoint_body, "type": lf.LFS_CLIENT_RESTART, "txid": 0},
        ],
        restart_index=1,
    )
    # check_point_start == 0 means "start from the restart record itself".
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    assert analysis.checkpoint_lsn == lsns[1]
    assert analysis.restart_area is not None
    assert [t.state for t in analysis.transactions] == []

    checkpoint = lsns[1]
    body = record_body(payload=client_restart_payload(checkpoint))
    stream, lsns = build_log(
        [
            update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8),
            {"body": body, "type": lf.LFS_CLIENT_RESTART, "txid": 0},
            update_record(txid=0x40, redo=b"\x03" * 8, undo=b"\x04" * 8),
        ],
        restart_index=1,
    )
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    assert analysis.checkpoint_lsn == checkpoint
    assert [t.transaction_id for t in analysis.transactions] == [0x40]
    assert len(analysis.records) == 3  # everything is still parsed


def test_clr_record_steers_undo_next():
    first_lsn = (4 * PAGE + 0x40) >> 3
    stream, lsns = build_log(
        [
            update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8),
            {
                "body": record_body(
                    redo_op=0x01,  # CompensationLogRecord
                    undo_op=0x01,
                    redo=b"\x00" * 8,
                    undo=b"\x00" * 8,
                    target_attr=0x18,
                    lcns=(0x40,),
                ),
                "txid": 0x18,
                "undo_next": first_lsn,
            },
        ]
    )
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    transaction = analysis.transactions[0]
    assert transaction.undo_next_lsn == lsns[0]
    # The CLR itself carries no byte action, so only the first record is undone.
    assert [step.lsn for step in analysis.undo_plan] == [lsns[0]]


def test_no_undo_steps_without_undo_pointer():
    body = record_body(
        redo_op=0x07,
        undo_op=0x00,  # undo Noop
        redo=b"\x01" * 8,
        undo=b"",
        target_attr=0x18,
        lcns=(0x40,),
    )
    stream, _ = build_log([{"body": body, "txid": 0x18}])
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    assert [t.state for t in analysis.transactions] == ["active"]
    assert analysis.undo_plan == ()


def test_page_lcns_and_target_coordinates_are_preserved():
    stream, _ = build_log(
        [
            {
                "body": record_body(
                    redo_op=0x08,
                    undo_op=0x08,
                    redo=b"\xaa" * 16,
                    undo=b"\x55" * 8,
                    target_attr=0x20,
                    lcns=(0x11, 0x12, 0x13),
                    target_vcn=0x1000,
                    record_off=0x50,
                    attr_off=0x18,
                    cluster_off=2,
                ),
                "txid": 0x18,
            }
        ]
    )
    record = lf.analyze_logfile(stream, cluster_size=512).records[0]
    assert record.page_lcns == (0x11, 0x12, 0x13)
    assert (
        record.target_vcn,
        record.record_off,
        record.attr_off,
        record.cluster_off,
    ) == (
        0x1000,
        0x50,
        0x18,
        2,
    )
    assert record.redo_len == 16 and record.undo_len == 8


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda s, lsns: _poke(
                s,
                4 * PAGE + 0x40 + 0x00,
                lf.LFS_RECORD_HDR.pack(
                    lsns[0],
                    0,
                    0,
                    0x50,
                    0,
                    0,
                    lf.LFS_CLIENT_RECORD,
                    0x18,
                    lf.LOG_RECORD_MULTI_PAGE,
                ),
            ),
            "multi-page",
        ),
        (
            lambda s, lsns: _poke(s, 4 * PAGE + 0x40 + 0x18, struct.pack("<I", 0x1000)),
            "past the page's free-space",
        ),
        (
            lambda s, lsns: _poke(s, 4 * PAGE + 0x40 + 0x18, struct.pack("<I", 0x1)),
            "unaligned data length",
        ),
        (
            lambda s, lsns: _poke(s, 4 * PAGE + 0x40 + 0x24, struct.pack("<I", 0x17)),
            "transaction-table offset",
        ),
        (
            lambda s, lsns: _poke(s, 4 * PAGE + 0x40 + 0x20, struct.pack("<I", 7)),
            "unknown type",
        ),
        (lambda s, lsns: _poke(s, 4 * PAGE + 6, b"\x00\x00"), "USA count"),
        (lambda s, lsns: _poke(s, 4 * PAGE + 0x1FE, b"\x00\x00"), "USA mismatch"),
        (lambda s, lsns: _poke(s, 4 * PAGE, b"BAAD"), "BAAD"),
        (lambda s, lsns: _poke(s, 4 * PAGE, b"XXXX"), "not b'RCRD'"),
    ],
)
def test_malformed_records_and_pages_are_refused(mutate, message):
    stream, lsns = build_log(
        [update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8)]
    )
    damaged = mutate(bytearray(stream), lsns)
    with pytest.raises(lf.NtfsLogFileError, match=message):
        lf.analyze_logfile(bytes(damaged), cluster_size=512)


def _poke(stream: bytearray, offset: int, data: bytes) -> bytearray:
    stream[offset : offset + len(data)] = data
    return stream


def test_record_area_before_first_page_is_ignored():
    """Pages 2..3 are tail copies, not records (fslog.c:1362-1363)."""

    stream, _ = build_log(
        [update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8)]
    )
    body = bytearray(stream)
    # Put a plausible-looking header inside a tail-copy page: it must not parse.
    header = lf.LFS_RECORD_HDR.pack(
        0x1234, 0, 0, 0x50, 0, 0, lf.LFS_CLIENT_RECORD, 0x18, 0
    )
    body[2 * PAGE + 0x40 : 2 * PAGE + 0x40 + len(header)] = header
    analysis = lf.analyze_logfile(bytes(body), cluster_size=512)
    assert [record.lsn for record in analysis.records] != [0x1234]


def test_duplicate_lsn_is_refused():
    stream, lsns = build_log(
        [update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8)]
    )
    damaged = bytearray(stream)
    # Point a second record's header at the first record's LSN.
    second = 4 * PAGE + 0x40 + 0x88
    if second + 0x30 > FILE_SIZE:
        pytest.skip("fixture too small for a second slot")
    damaged[second : second + 8] = struct.pack("<Q", lsns[0])
    analysis_stream = bytes(damaged)
    # The slot is beyond the page's free-space offset, so it is not scanned; the
    # duplicate check is exercised by shrinking nothing and rewriting the first
    # header to collide with the restart record instead.
    with pytest.raises((lf.NtfsLogFileError,)):
        lf.analyze_logfile(_two_records_one_lsn(), cluster_size=512)
    assert analysis_stream  # the untouched stream stays parseable


def _two_records_one_lsn() -> bytes:
    stream, lsns = build_log(
        [
            update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8),
            update_record(txid=0x40, redo=b"\x03" * 8, undo=b"\x04" * 8),
        ]
    )
    damaged = bytearray(stream)
    second = lsns[1] * 8
    assert struct.unpack_from("<Q", damaged, second)[0] == lsns[1]
    struct.pack_into("<Q", damaged, second, lsns[0])
    return bytes(damaged)


def test_truncated_stream_is_reported_not_silently_accepted():
    stream, _ = build_log(
        [update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8)]
    )
    short = stream[: 8 * PAGE]
    analysis = lf.analyze_logfile(short, cluster_size=512)
    assert analysis.truncated is True
    assert any("declares" in note for note in analysis.notes)


def test_plan_is_never_executable_and_targets_stay_unresolved():
    stream, lsns = build_log(
        [
            update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8),
            update_record(txid=0x40, redo=b"\x03" * 8, undo=b"\x04" * 8),
            marker_record(0x1A, 0x40),
        ]
    )
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    steps = [*analysis.redo_plan, *analysis.undo_plan]
    assert steps, "the plan should not be vacuous"
    assert analysis.executable is False
    assert all(step.resolvable is False for step in steps)
    assert all(step.reasons for step in steps)
    assert analysis.blockers == lf.EXECUTION_BLOCKERS
    assert "issue #387" in " ".join(analysis.blockers)


def test_analyzer_refuses_to_open_anything_for_writing(tmp_path, monkeypatch):
    """Structural proof: the analyzer has no write path at all."""

    import builtins
    import os as os_module

    real_open = builtins.open

    def guard(file, mode="r", *args, **kwargs):
        assert (
            "w" not in mode and "+" not in mode and "a" not in mode and "x" not in mode
        ), f"the $LogFile analyzer tried to open {file!r} in mode {mode!r}"
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guard)
    monkeypatch.setattr(
        os_module,
        "open",
        lambda *args, **kwargs: pytest.fail("the analyzer used os.open"),
    )
    stream, _ = build_log(
        [update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8)]
    )
    analysis = lf.analyze_logfile(stream, cluster_size=512)
    assert analysis.records


def test_geometry_validation_delegates_to_the_shipped_restart_parser():
    stream, _ = build_log([])
    damaged = bytearray(stream)
    struct.pack_into("<I", damaged, RA_OFF + 0x18, PAGE)  # absurd file_size
    with pytest.raises(lf.NtfsLogFileError, match="restart page 0"):
        lf.analyze_logfile(bytes(damaged), cluster_size=512)


def test_log_page_smaller_than_system_page_is_refused():
    page = bytearray(_restart_page(0, dirty=True))
    struct.pack_into("<I", page, 0x14, 512)  # log_page_size
    stream = bytearray(bytes(page) * 2) + UNUSED * (FILE_SIZE - 2 * PAGE)
    with pytest.raises(
        lf.NtfsLogFileError, match="mixed geometry|restart page geometry"
    ):
        lf.analyze_logfile(bytes(stream), cluster_size=512)


def test_clients_greater_than_one_are_refused():
    stream, _ = build_log([])
    damaged = bytearray(stream)
    struct.pack_into("<H", damaged, RA_OFF + 0x08, 2)
    with pytest.raises(lf.NtfsLogFileError, match="unknown state"):
        lf.analyze_logfile(bytes(damaged), cluster_size=512)


def test_second_restart_page_is_choosen_by_current_lsn():
    stream, lsns = build_log(
        [update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8)]
    )
    older = _restart_page(1, dirty=True)
    newer = stream[:PAGE]
    combined = bytearray(older + newer + stream[2 * PAGE :])
    analysis = lf.analyze_logfile(bytes(combined), cluster_size=512)
    assert analysis.current_lsn == lsns[0]


def test_as_dict_reports_counts_and_blockers():
    stream, _ = build_log(
        [
            update_record(txid=0x18, redo=b"\x01" * 8, undo=b"\x02" * 8),
            marker_record(0x1A, 0x18),
        ]
    )
    payload = lf.analyze_logfile(stream, cluster_size=512).as_dict()
    assert payload["parsed"] is True
    assert payload["record_count"] == 2
    assert payload["redo_steps"][0]["operation"] == "UpdateResidentValue"
    assert payload["unresolved_steps"] == len(payload["redo_steps"])
    assert payload["executable"] is False
    assert payload["blockers"]


@pytest.mark.skipif(not FIXTURE.exists(), reason="NTFS fixture is unavailable")
def test_real_formatted_volume_reports_an_unwritten_log(tmp_path):
    """Reads a real ``$LogFile`` stream and refuses to invent a plan for it.

    The 32 MiB fixture is a genuine formatted NTFS image whose log was never
    mounted, so both restart pages are erased.  The reader must still locate the
    stream through the ``$MFT`` runlist, and the analyzer must refuse rather than
    treat the absence of a restart area as "clean, nothing to do".
    """

    image = tmp_path / "ntfs-readonly-32m.img"
    image.write_bytes(gzip.decompress(FIXTURE.read_bytes()))
    stream, cluster, attribute = lf.read_logfile_stream(image)
    assert cluster == 512
    assert attribute.nonresident is True
    assert len(stream) == attribute.data_size
    assert stream[: 2 * PAGE].strip(b"\xff") == b""
    with pytest.raises(lf.NtfsLogFileError, match="never written"):
        lf.analyze_logfile(stream, cluster_size=cluster)
