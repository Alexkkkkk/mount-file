# SPDX-License-Identifier: MIT
"""Quarantined, read-only adapter for an externally installed ``dfir_ntfs``.

The dependency is not bundled: upstream dfir_ntfs is GPL-3.0 and this MIT
project must not silently vendor or link it as a normal dependency. This module
is never imported by production code. It only normalizes parser output for
experiments and refuses missing or incompatible APIs explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


class ExperimentalAdapterUnavailable(RuntimeError):
    """The optional external parser is unavailable or its API is unsupported."""


@dataclass(frozen=True)
class ExternalLogRecord:
    transaction_id: int
    lsn: int
    redo_data: bytes
    undo_data: bytes
    redo_operation: int | None = None
    undo_operation: int | None = None


class DfirNtfsAdapter:
    """Read-only compatibility wrapper; it never opens an image for writing."""

    def __init__(self, logfile_path: str | Path):
        try:
            from dfir_ntfs.LogFile import LogFileParser  # type: ignore
        except ImportError as exc:
            raise ExperimentalAdapterUnavailable(
                "optional GPL-3.0 dfir_ntfs is not installed; experimental adapter unavailable"
            ) from exc
        self.logfile_path = Path(logfile_path)
        try:
            self._stream = self.logfile_path.open("rb")
            self._parser = LogFileParser(self._stream)
        except Exception as exc:
            self.close()
            raise ExperimentalAdapterUnavailable(
                f"dfir_ntfs could not parse $LogFile: {exc}"
            ) from exc

    def close(self) -> None:
        stream = getattr(self, "_stream", None)
        if stream is not None:
            stream.close()
            self._stream = None

    def __enter__(self) -> DfirNtfsAdapter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def records(self) -> Iterator[ExternalLogRecord]:
        parser = self._parser
        method = getattr(parser, "parse_ntfs_records", None)
        if not callable(method):
            raise ExperimentalAdapterUnavailable(
                "dfir_ntfs API has no parse_ntfs_records()"
            )
        try:
            records = method(recover_log_data=True)
        except TypeError:
            records = method()
        if records is None:
            raise ExperimentalAdapterUnavailable("dfir_ntfs returned no record stream")
        for record in records:
            yield self._normalize(record)

    @staticmethod
    def _value(
        record: object, names: tuple[str, ...], default: object = None
    ) -> object:
        for name in names:
            method = getattr(record, name, None)
            if callable(method):
                return method()
            if hasattr(record, name):
                return getattr(record, name)
        return default

    @classmethod
    def _normalize(cls, record: object) -> ExternalLogRecord:
        redo = cls._value(record, ("get_redo_data", "redo_data"), b"")
        undo = cls._value(record, ("get_undo_data", "undo_data"), b"")
        if not isinstance(redo, bytes | bytearray) or not isinstance(
            undo, bytes | bytearray
        ):
            raise ExperimentalAdapterUnavailable("dfir_ntfs returned non-byte payload")
        txid = cls._value(record, ("transaction_id", "get_transaction_id"), 0)
        lsn = cls._value(record, ("lsn", "get_this_lsn"), 0)
        try:
            return ExternalLogRecord(
                transaction_id=int(txid),
                lsn=int(lsn),
                redo_data=bytes(redo),
                undo_data=bytes(undo),
                redo_operation=cls._optional_int(record, ("get_redo_operation",)),
                undo_operation=cls._optional_int(record, ("get_undo_operation",)),
            )
        except (TypeError, ValueError) as exc:
            raise ExperimentalAdapterUnavailable(
                f"dfir_ntfs returned a malformed record: {exc}"
            ) from exc

    @classmethod
    def _optional_int(cls, record: object, names: tuple[str, ...]) -> int | None:
        value = cls._value(record, names)
        return None if value is None else int(value)


__all__ = [
    "DfirNtfsAdapter",
    "ExperimentalAdapterUnavailable",
    "ExternalLogRecord",
]
