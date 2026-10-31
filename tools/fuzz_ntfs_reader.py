#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Coverage-guided fuzz target for bounded NTFS metadata parsers.

Run from the repository root with:
    uv run --extra fuzz python tools/fuzz_ntfs_reader.py /tmp/mountfile-ntfs-corpus -runs=10000

The selected corpus directory is created if needed and receives a small set of
seed inputs. Existing corpus entries are never overwritten.
"""

from __future__ import annotations

import struct
import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

_RECORD_SIZE = 1024
_FIRST_ATTRIBUTE = 0x38
_MAX_INPUT_SIZE = 1 << 20
_CLUSTER_COUNT = 1 << 16


def _attribute_list_seed(type_code: int) -> bytes:
    entry = struct.pack("<IHBBQQH", type_code, 32, 0, 0, 0, (1 << 48) | 1, 1)
    return entry + bytes(32 - len(entry))


def _file_record_seed(attribute_payload: bytes = b"") -> bytes:
    raw = bytearray(_RECORD_SIZE)
    raw[:4] = b"FILE"
    usa_offset = 0x30
    struct.pack_into("<HH", raw, 4, usa_offset, 3)
    struct.pack_into("<H", raw, 0x10, 1)
    struct.pack_into("<H", raw, 0x14, _FIRST_ATTRIBUTE)
    struct.pack_into("<H", raw, 0x16, 1)
    struct.pack_into("<H", raw, usa_offset, 0xA55A)
    struct.pack_into("<HH", raw, usa_offset + 2, 0, 0)

    payload = attribute_payload[: _RECORD_SIZE - _FIRST_ATTRIBUTE - 4]
    end = _FIRST_ATTRIBUTE + len(payload)
    raw[_FIRST_ATTRIBUTE:end] = payload
    raw[end : end + 4] = b"\xff\xff\xff\xff"
    struct.pack_into("<I", raw, 0x18, end + 4)
    struct.pack_into("<I", raw, 0x1C, _RECORD_SIZE)
    raw[510:512] = b"\x5a\xa5"
    raw[_RECORD_SIZE - 2 : _RECORD_SIZE] = b"\x5a\xa5"
    return bytes(raw)


def _seed_corpus(corpus_dir: Path) -> None:
    corpus_dir.mkdir(parents=True, exist_ok=True)
    seeds = {
        "attribute-list-valid.bin": _attribute_list_seed(0x10),
        "attribute-list-self-reference.bin": _attribute_list_seed(0x20),
        "runlist-valid.bin": bytes((0x11, 1, 1, 0)),
        "runlist-sparse.bin": bytes((0x01, 1, 0)),
        "file-record-empty.bin": _file_record_seed(),
        "logfile-restart-page.bin": b"RSTR" + bytes(4092),
        "index-block.bin": b"INDX" + bytes(4092),
    }
    for name, data in seeds.items():
        destination = corpus_dir / name
        if not destination.exists():
            destination.write_bytes(data)


def _build_target(
    *,
    metadata_error: type[Exception],
    record_error: type[Exception],
    parse_attribute_list: Callable,
    parse_attribute_stream: Callable,
    parse_file_name: Callable,
    parse_file_record: Callable,
    parse_index_block: Callable,
    parse_logfile_restart_area: Callable,
    parse_standard_information: Callable,
    parse_runlist: Callable,
):
    def test_one_input(data: bytes) -> None:
        raw = bytes(data)
        if len(raw) > _MAX_INPUT_SIZE:
            return

        for parser in (
            parse_attribute_list,
            parse_file_name,
            parse_logfile_restart_area,
            parse_standard_information,
        ):
            with suppress(metadata_error):
                parser(raw)

        try:
            runs = parse_runlist(raw, _CLUSTER_COUNT)
        except metadata_error:
            pass
        else:
            if not runs:
                raise AssertionError("successful NTFS runlist parse returned no runs")
            expected_vcn = 0
            for run in runs:
                if run.vcn != expected_vcn or run.length <= 0 or run.lcn is None:
                    raise AssertionError(
                        "successful NTFS runlist violated VCN/LCN bounds"
                    )
                if run.lcn < 0 or run.lcn + run.length > _CLUSTER_COUNT:
                    raise AssertionError("successful NTFS runlist escaped the volume")
                expected_vcn += run.length

        with suppress(metadata_error):
            parse_index_block(
                raw,
                parent_reference=(1 << 48) | 1,
                expected_vcn=0,
            )

        candidates = (raw, _file_record_seed(raw))
        for candidate in candidates:
            try:
                record = parse_file_record(candidate)
            except record_error:
                continue
            with suppress(metadata_error):
                parse_attribute_stream(
                    record,
                    cluster_count=_CLUSTER_COUNT,
                    allow_attribute_list=True,
                )

    return test_one_input


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1].startswith("-"):
        print(
            "usage: fuzz_ntfs_reader.py CORPUS_DIR [libFuzzer options, e.g. -runs=10000]",
            file=sys.stderr,
        )
        return 2

    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    corpus_dir = Path(sys.argv[1]).expanduser().resolve()
    _seed_corpus(corpus_dir)

    try:
        import atheris
    except ImportError as exc:
        print(
            "Install the optional fuzz extra with: uv sync --extra fuzz",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    with atheris.instrument_imports():
        from mountfile.ntfs_mft import NtfsRecordError, parse_ntfs_file_record_header
        from mountfile.ntfs_reader import (
            NtfsMetadataError,
            _parse_runlist,
            parse_ntfs_attribute_list,
            parse_ntfs_attribute_stream,
            parse_ntfs_file_name,
            parse_ntfs_index_block,
            parse_ntfs_logfile_restart_area,
            parse_ntfs_standard_information,
        )

    target = _build_target(
        metadata_error=NtfsMetadataError,
        record_error=NtfsRecordError,
        parse_attribute_list=parse_ntfs_attribute_list,
        parse_attribute_stream=parse_ntfs_attribute_stream,
        parse_file_name=parse_ntfs_file_name,
        parse_file_record=parse_ntfs_file_record_header,
        parse_index_block=parse_ntfs_index_block,
        parse_logfile_restart_area=parse_ntfs_logfile_restart_area,
        parse_standard_information=parse_ntfs_standard_information,
        parse_runlist=_parse_runlist,
    )
    atheris.Setup(sys.argv, target)
    atheris.Fuzz()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
