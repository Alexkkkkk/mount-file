# SPDX-License-Identifier: MIT
"""Smoke-test the shared read-only VFS contract across native providers.

Each case supplies its own synthetic image: different filesystems cannot share
one on-disk image. The runner deliberately disables the legacy pytsk3 fallback
and validates listing, stat, full reads, bounded range reads, and safe extraction.
Fixture construction belongs in tests/tools rather than this runtime module.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from mountfile import vfs
from mountfile.extract import extract_file


@dataclass(frozen=True)
class ParityCase:
    """One bounded filesystem image and its expected regular-file contents."""

    fs_name: str
    image_path: Path
    expected_files: Mapping[str, bytes]
    offset: int = 0
    volume_size: int | None = None


@dataclass
class ParityReport:
    """Aggregate result for a native-reader smoke matrix."""

    ran: list[str] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)
    checks: int = 0

    @property
    def ok(self) -> bool:
        return not self.failures and bool(self.ran)


def _normalized_file_path(path: str) -> PurePosixPath:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "\x00" in path
        or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/")[1:])
    ):
        raise ValueError(f"expected a normalized absolute VFS file path: {path!r}")
    normalized = PurePosixPath(path)
    if normalized.as_posix() != path:
        raise ValueError(f"expected a normalized absolute VFS file path: {path!r}")
    return normalized


def _run_case(case: ParityCase) -> int:
    image = Path(case.image_path).expanduser().resolve()
    if not image.is_file():
        raise ValueError(f"image is not a regular file: {image}")
    offset = case.offset
    volume_size = case.volume_size
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("filesystem offset must be a non-negative integer")
    if volume_size is None:
        volume_size = image.stat().st_size - offset
    if (
        isinstance(volume_size, bool)
        or not isinstance(volume_size, int)
        or volume_size <= 0
        or offset + volume_size > image.stat().st_size
    ):
        raise ValueError("filesystem extent is invalid or exceeds the image")
    if not case.expected_files:
        raise ValueError("parity case must contain at least one expected file")

    provider = vfs.open_image_provider(
        image,
        offset=offset,
        volume_size=volume_size,
        allow_legacy_fallback=False,
    )
    checks = 0
    try:
        backend_name = str(getattr(provider, "backend_name", ""))
        if not backend_name.startswith("native-"):
            raise AssertionError(
                f"expected a native backend, got {backend_name or type(provider).__name__}"
            )

        listed_by_parent: dict[str, set[str]] = {}
        with tempfile.TemporaryDirectory(prefix="mountfile-native-parity-") as tmp:
            extraction_root = Path(tmp)
            for index, (file_path, expected) in enumerate(case.expected_files.items()):
                normalized = _normalized_file_path(file_path)
                if not isinstance(expected, bytes):
                    raise TypeError(f"expected contents for {file_path!r} must be bytes")
                parent = normalized.parent.as_posix()
                if parent not in listed_by_parent:
                    entries = provider.list_dir(parent)
                    listed_by_parent[parent] = {
                        str(entry.get("name", "")).casefold() for entry in entries
                    }
                if normalized.name.casefold() not in listed_by_parent[parent]:
                    raise AssertionError(
                        f"list_dir({parent!r}) did not contain {normalized.name!r}"
                    )
                checks += 1

                metadata = provider.stat(file_path)
                if metadata.get("is_dir") or int(metadata.get("size", -1)) != len(expected):
                    raise AssertionError(
                        f"stat({file_path!r}) returned unexpected type/size: {metadata!r}"
                    )
                checks += 1

                actual = provider.read_file(file_path)
                if actual != expected:
                    raise AssertionError(f"read_file({file_path!r}) content differs")
                checks += 1

                range_offset = min(1, len(expected))
                range_length = min(17, len(expected) - range_offset)
                ranged = provider.read_file_range(
                    file_path, range_offset, range_length
                )
                if ranged != expected[range_offset : range_offset + range_length]:
                    raise AssertionError(
                        f"read_file_range({file_path!r}) content differs"
                    )
                checks += 1

                destination = extraction_root / f"file-{index:04d}.bin"
                written = extract_file(provider, file_path, destination)
                if written != len(expected) or destination.read_bytes() != expected:
                    raise AssertionError(f"extract_file({file_path!r}) content differs")
                checks += 1
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    return checks


def run_parity_matrix(cases: Iterable[ParityCase]) -> ParityReport:
    """Run the common read-only contract against each supplied native image.

    A failed case is recorded while later cases continue. The report is
    successful only when at least one case ran and every case passed.
    """
    report = ParityReport()
    seen: set[str] = set()
    materialized = list(cases)
    if not materialized:
        raise ValueError("parity matrix must contain at least one case")

    for case in materialized:
        if not case.fs_name or case.fs_name in seen:
            raise ValueError("parity case names must be non-empty and unique")
        seen.add(case.fs_name)
        try:
            report.checks += _run_case(case)
        except Exception as exc:
            report.failures[case.fs_name] = f"{type(exc).__name__}: {exc}"
        else:
            report.ran.append(case.fs_name)
    return report


__all__ = ["ParityCase", "ParityReport", "run_parity_matrix"]
