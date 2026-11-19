# SPDX-License-Identifier: MIT
"""Image-level NTFS ``$LogFile`` replay tests.

The fixtures are synthetic crash images built from real ``mkntfs``/``ntfscp``
volumes (see ``tests/ntfs_crash_fixture.py``).  They exercise the production
executor, not a private overlay: the source is a disposable copy, the clean
restart pages are written, ``$Volume`` is cleared and ``ntfscat`` reads the
replayed bytes back.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from mountfile.ntfs_logfile import analyze_image, read_logfile_stream
from mountfile.ntfs_mft import parse_ntfs_file_record_header
from mountfile.ntfs_native_writer import (
    NtfsNativeWriter,
    NtfsProfileUnsupported,
    probe_ntfs_log,
)
from mountfile.ntfs_reader import probe_ntfs_metadata
from mountfile.ntfs_replay import (
    OP_ADD_INDEX_ENTRY_ROOT,
    OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP,
    OP_DEALLOCATE_FILE_RECORD,
    OP_INITIALIZE_FILE_RECORD,
    OP_SET_BITS_IN_NONRESIDENT_BITMAP,
    SUPPORTED_OPERATIONS,
    NtfsReplayError,
    native_ntfs_recover,
    parse_restart_tables,
    plan_ntfs_replay,
)
from tests.ntfs_crash_fixture import (
    corrupt_mft_mirror,
    make_crash_fixture,
    mark_volume_dirty,
)

needs_ntfs = pytest.mark.skipif(
    shutil.which("mkntfs") is None or shutil.which("ntfscp") is None,
    reason="mkntfs/ntfscp are not installed",
)

_SIGKILL_REPLAY_CHILD = r"""
import os
import signal
import sys

from mountfile.native_write import fault_hook_installed
from mountfile.ntfs_replay import native_ntfs_recover

image_path, target_phase = sys.argv[1:]
def kill_at(phase):
    if phase == target_phase:
        os.kill(os.getpid(), signal.SIGKILL)
fault_hook_installed(kill_at)
native_ntfs_recover(image_path)
"""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cat(path: Path, name: str) -> bytes:
    result = subprocess.run(
        ["ntfscat", str(path), name], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


@needs_ntfs
def test_restart_tables_and_plan_are_derived_from_the_checkpoint(tmp_path):
    fixture = make_crash_fixture(tmp_path)
    analysis = analyze_image(str(fixture.image))
    stream, _cluster, _logfile = read_logfile_stream(str(fixture.image))
    tables = parse_restart_tables(stream, analysis)
    assert len(tables.open_attributes) == 1
    assert tables.open_attributes[0].type_code == 0x80
    assert tables.open_attributes[0].record_number == fixture.record_number
    assert len(tables.dirty_pages) == 1
    assert tables.dirty_pages[0].oldest_lsn == fixture.update_lsn
    assert len(tables.transactions) == 1
    plan = plan_ntfs_replay(analysis, tables)
    assert plan.redo_start == fixture.update_lsn
    assert [record.lsn for record in plan.redo] == [fixture.update_lsn]
    assert plan.undo == ()


@needs_ntfs
def test_committed_resident_update_is_replayed_and_read_back(tmp_path):
    fixture = make_crash_fixture(tmp_path)
    before = _sha256(fixture.image)
    result = native_ntfs_recover(str(fixture.image))
    assert result.changed is True
    assert result.redo_applied == 1
    assert result.undo_applied == 0
    assert result.clean_restart_pages == 2
    assert result.volume_flags_before == 1
    assert result.volume_flags_after == 0
    assert _cat(fixture.image, "/target") == fixture.new_data
    gate = probe_ntfs_log(str(fixture.image))
    assert gate.clean is True
    assert _sha256(fixture.image) != before


@needs_ntfs
def test_active_transaction_is_undone_after_redo(tmp_path):
    fixture = make_crash_fixture(tmp_path, active=True)
    result = native_ntfs_recover(str(fixture.image))
    assert result.redo_applied == 1
    assert result.undo_applied == 1
    assert _cat(fixture.image, "/target") == fixture.old_data
    assert probe_ntfs_log(str(fixture.image)).clean is True


@needs_ntfs
def test_committed_nonresident_update_is_replayed(tmp_path):
    fixture = make_crash_fixture(tmp_path, nonresident=True)
    result = native_ntfs_recover(str(fixture.image))
    assert result.redo_applied == 1
    assert result.stream_writes == 1
    assert _cat(fixture.image, "/target")[: len(fixture.new_data)] == fixture.new_data
    assert probe_ntfs_log(str(fixture.image)).clean is True


@needs_ntfs
def test_second_recovery_is_a_clean_noop(tmp_path):
    fixture = make_crash_fixture(tmp_path)
    first = native_ntfs_recover(str(fixture.image))
    assert first.changed is True
    digest = _sha256(fixture.image)
    second = native_ntfs_recover(str(fixture.image))
    assert second.changed is False
    assert second.redo_applied == 0
    assert _sha256(fixture.image) == digest


@needs_ntfs
@pytest.mark.parametrize(
    ("phase", "active"),
    (
        ("replay:redo:start", False),
        ("replay:redo:done", False),
        ("replay:undo:start", True),
        ("replay:undo:done", True),
        ("replay:journal:clean-pages", False),
        ("replay:volume:dirty-flag", False),
        ("replay:mirror:update", False),
        ("replay:flush", False),
    ),
)
def test_native_replay_sigkill_matrix_reaches_each_declared_phase(
    tmp_path, phase, active
):
    """Each declared replay phase is killable on a disposable real NTFS image."""
    fixture = make_crash_fixture(tmp_path, active=active)
    repo_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root)
    result = subprocess.run(
        [sys.executable, "-c", _SIGKILL_REPLAY_CHILD, str(fixture.image), phase],
        cwd=str(repo_root),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == -signal.SIGKILL, (
        phase,
        result.stdout,
        result.stderr,
    )


def test_mft_allocation_and_bitmap_operations_are_not_allowlisted():
    assert (
        not {
            OP_INITIALIZE_FILE_RECORD,
            OP_DEALLOCATE_FILE_RECORD,
            OP_SET_BITS_IN_NONRESIDENT_BITMAP,
            OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP,
        }
        & SUPPORTED_OPERATIONS
    )


@needs_ntfs
@pytest.mark.parametrize(
    "operation",
    [
        OP_ADD_INDEX_ENTRY_ROOT,
        OP_INITIALIZE_FILE_RECORD,
        OP_DEALLOCATE_FILE_RECORD,
        OP_SET_BITS_IN_NONRESIDENT_BITMAP,
        OP_CLEAR_BITS_IN_NONRESIDENT_BITMAP,
    ],
)
def test_unsupported_operations_are_refused_before_writing(tmp_path, operation):
    fixture = make_crash_fixture(tmp_path)
    stream, _cluster, logfile = read_logfile_stream(str(fixture.image))
    analysis = analyze_image(str(fixture.image))
    record = analysis.record_at(fixture.update_lsn)
    assert record is not None
    body = record.byte_offset + analysis.restart.log_record_header_length
    stream = bytearray(stream)
    struct.pack_into("<H", stream, body, operation)
    with open(fixture.image, "r+b") as handle:
        for run in logfile.runs:
            start = run.vcn * _cluster
            length = run.length * _cluster
            handle.seek(run.lcn * _cluster)
            handle.write(stream[start : start + length])
    before = _sha256(fixture.image)
    with pytest.raises(NtfsReplayError, match="unsupported NTFS replay operations"):
        native_ntfs_recover(str(fixture.image))
    assert _sha256(fixture.image) == before


@needs_ntfs
def test_writer_native_mode_replays_then_writes(tmp_path, monkeypatch):
    fixture = make_crash_fixture(tmp_path)
    monkeypatch.setenv("MOUNTFILE_ENABLE_NATIVE_WRITE", "1")
    writer = NtfsNativeWriter(recover="native")
    written = writer.write_file(str(fixture.image), "/target", b"WRITER")
    assert written == 6
    assert _cat(fixture.image, "/target") == b"WRITER"
    assert probe_ntfs_log(str(fixture.image)).clean is True


@needs_ntfs
def test_writer_refuses_dirty_image_without_native_mode(tmp_path, monkeypatch):
    fixture = make_crash_fixture(tmp_path)
    before = _sha256(fixture.image)
    monkeypatch.setenv("MOUNTFILE_ENABLE_NATIVE_WRITE", "1")
    with pytest.raises(NtfsProfileUnsupported, match="needs log recovery"):
        NtfsNativeWriter(recover="refuse").write_file(
            str(fixture.image), "/target", b"WRITER"
        )
    assert _sha256(fixture.image) == before


def _make_clean_ntfs(tmp_path: Path) -> Path:
    image = tmp_path / "clean.img"
    with open(image, "wb") as handle:
        handle.truncate(64 << 20)
    subprocess.run(
        ["mkntfs", "-q", "-F", "-s", "512", "-c", "4096", str(image)],
        check=True,
        capture_output=True,
    )
    return image


@needs_ntfs
def test_dirty_bit_only_volume_is_cleared_without_log_replay(tmp_path):
    image = _make_clean_ntfs(tmp_path)
    mark_volume_dirty(image)
    result = native_ntfs_recover(str(image))
    assert result.changed is True
    assert result.clean_restart_pages == 0
    assert result.redo_applied == 0
    assert result.volume_flags_before == 1
    assert result.volume_flags_after == 0
    assert probe_ntfs_log(str(image)).clean is True


@needs_ntfs
def test_mft_mirror_is_repaired_on_the_volume(tmp_path):
    image = _make_clean_ntfs(tmp_path)
    mark_volume_dirty(image)
    corrupt_mft_mirror(image)
    result = native_ntfs_recover(str(image))
    assert result.mirror_updated is True
    profile = probe_ntfs_metadata(str(image), volume_size=os.path.getsize(image))
    assert profile.mirror_matches_mft is True
    assert probe_ntfs_log(str(image)).clean is True


def _log_record(
    op: int,
    payload: bytes,
    *,
    record_off: int = 0,
    attr_off: int = 0,
    lsn: int = 0x12345678,
):
    from mountfile.ntfs_logfile import LogRecord

    return LogRecord(
        lsn=lsn,
        byte_offset=0,
        page_index=0,
        record_type=1,
        transaction_id=0x18,
        sequence_number=1,
        client_index=0,
        prev_lsn=0,
        undo_next_lsn=0,
        flags=0,
        data_length=len(payload),
        redo_op=op,
        undo_op=op,
        redo_data=payload,
        undo_data=b"",
        target_attr=0,
        lcns_follow=1,
        record_off=record_off,
        attr_off=attr_off,
        cluster_off=0,
        target_vcn=0,
        page_lcns=(0,),
        restart=None,
    )


def _named_resident_data(attribute_id: int) -> bytes:
    name = "test".encode("utf-16-le")
    value = b"12345678"
    size = 0x20 + len(value)
    payload = bytearray(size)
    struct.pack_into("<I", payload, 0, 0x80)
    struct.pack_into("<I", payload, 4, size)
    payload[8] = 0
    payload[9] = 4
    struct.pack_into("<H", payload, 10, 0x18)
    struct.pack_into("<H", payload, 14, attribute_id)
    struct.pack_into("<I", payload, 16, len(value))
    struct.pack_into("<H", payload, 20, 0x20)
    payload[0x18 : 0x18 + len(name)] = name
    payload[0x20 : 0x20 + len(value)] = value
    return bytes(payload)


@needs_ntfs
def test_create_and_delete_attribute_handlers_round_trip(tmp_path):
    from mountfile.ntfs_reader import parse_ntfs_attribute_stream
    from mountfile.ntfs_replay import (
        OP_CREATE_ATTRIBUTE,
        OP_DELETE_ATTRIBUTE,
        _apply_mft_operation,
        _apply_usa,
        _Mft,
    )

    fixture = make_crash_fixture(tmp_path)
    profile = probe_ntfs_metadata(
        str(fixture.image),
        volume_size=os.path.getsize(fixture.image),
        validate_mirror=False,
    )
    mft_data = [a for a in profile.attributes if a.type_code == 0x80 and not a.name][0]
    with open(fixture.image, "r+b") as handle:
        mft = _Mft(handle, profile.boot, mft_data, base_offset=0)
        header = mft.read_record(fixture.record_number)
        end_marker = header.first_attribute_offset
        while end_marker + 4 <= header.bytes_in_use:
            if (
                struct.unpack_from("<I", header.fixed_record, end_marker)[0]
                == 0xFFFFFFFF
            ):
                break
            end_marker += struct.unpack_from("<I", header.fixed_record, end_marker + 4)[
                0
            ]
        assert (
            struct.unpack_from("<I", header.fixed_record, end_marker)[0] == 0xFFFFFFFF
        )
        payload = _named_resident_data(header.next_attribute_id)
        created, _kind = _apply_mft_operation(
            header,
            _log_record(OP_CREATE_ATTRIBUTE, payload, record_off=end_marker),
            payload,
            redo=True,
        )
        created_header = parse_ntfs_file_record_header(_apply_usa(created))
        names = [
            a.name
            for a in parse_ntfs_attribute_stream(
                created_header, allow_end_marker_padding=True
            )
        ]
        assert "test" in names
        assert created_header.bytes_in_use == header.bytes_in_use + len(payload)
        deleted, _kind = _apply_mft_operation(
            created_header,
            _log_record(OP_DELETE_ATTRIBUTE, b"", record_off=end_marker),
            b"",
            redo=True,
        )
        deleted_header = parse_ntfs_file_record_header(_apply_usa(deleted))
        assert "test" not in [
            a.name
            for a in parse_ntfs_attribute_stream(
                deleted_header, allow_end_marker_padding=True
            )
        ]
        assert deleted_header.bytes_in_use == header.bytes_in_use


@needs_ntfs
def test_initialize_and_deallocate_record_handlers(tmp_path):
    from mountfile.ntfs_replay import (
        OP_DEALLOCATE_FILE_RECORD,
        OP_INITIALIZE_FILE_RECORD,
        _apply_mft_operation,
        _Mft,
    )

    fixture = make_crash_fixture(tmp_path)
    profile = probe_ntfs_metadata(
        str(fixture.image),
        volume_size=os.path.getsize(fixture.image),
        validate_mirror=False,
    )
    mft_data = [a for a in profile.attributes if a.type_code == 0x80 and not a.name][0]
    with open(fixture.image, "r+b") as handle:
        mft = _Mft(handle, profile.boot, mft_data, base_offset=0)
        source = mft.read_record(fixture.record_number)
        initialized, _kind = _apply_mft_operation(
            source,
            _log_record(
                OP_INITIALIZE_FILE_RECORD,
                source.fixed_record,
                record_off=0,
                lsn=0xABCDEF,
            ),
            source.fixed_record,
            redo=True,
        )
        mft.write_record(30, initialized)
        read_back = mft.read_record(30)
        assert read_back.fixed_record == initialized
        deallocated, _kind = _apply_mft_operation(
            read_back,
            _log_record(OP_DEALLOCATE_FILE_RECORD, b"", record_off=0),
            b"",
            redo=True,
        )
        mft.write_record(30, deallocated)
        raw = mft.read_record_raw(30)
        assert struct.unpack_from("<H", raw, 0x16)[0] & 0x0001 == 0
        assert (
            struct.unpack_from("<H", raw, 0x10)[0]
            == (read_back.sequence_number + 1) & 0xFFFF
        )
