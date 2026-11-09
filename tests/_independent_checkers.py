"""Test-only wrappers for non-repair filesystem checker invocations.

The wrappers deliberately keep external processes out of production code.  Each
checker receives a freshly copied image in a temporary directory, so a test can
use a trusted checker without giving it the source image.  A successful
``ntfsfix`` invocation is intentionally inconclusive: ntfsfix is not a full
NTFS consistency checker or a replacement for chkdsk.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path


class CheckerVerdict(str, Enum):
    """Conservative result classes for test assertions."""

    CLEAN = "clean"
    NOT_VERIFIED_CLEAN = "not-verified-clean"
    INCONCLUSIVE = "inconclusive"
    UNAVAILABLE = "unavailable"
    TIMED_OUT = "timed-out"


@dataclass(frozen=True)
class CheckerResult:
    """Result of running one checker against a disposable image copy."""

    tool: str
    available: bool
    command: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    verdict: CheckerVerdict
    timed_out: bool
    source_unchanged: bool
    copy_unchanged: bool | None
    exit_flags: tuple[str, ...] = ()
    source_sha256_before: str | None = None
    source_sha256_after: str | None = None
    copy_sha256_before: str | None = None
    copy_sha256_after: str | None = None

    @property
    def is_clean(self) -> bool:
        """Whether this checker established a clean image under its own contract."""

        return self.verdict is CheckerVerdict.CLEAN

    def diagnostics(self) -> str:
        """Return useful failure context without treating output as a clean verdict."""

        details = [
            f"tool={self.tool}",
            f"verdict={self.verdict.value}",
            f"returncode={self.returncode}",
            f"source_unchanged={self.source_unchanged}",
            f"copy_unchanged={self.copy_unchanged}",
        ]
        if self.exit_flags:
            details.append(f"exit_flags={','.join(self.exit_flags)}")
        output = (self.stdout + self.stderr).strip()
        if output:
            details.append(output)
        return "\n".join(details)


_E2FSCK_EXIT_BITS = (
    (1, "errors-corrected"),
    (2, "reboot-required"),
    (4, "uncorrected-filesystem-errors"),
    (8, "operational-error"),
    (16, "usage-or-syntax-error"),
    (32, "cancelled"),
    (128, "shared-library-error"),
)


def e2fsck_exit_flags(returncode: int) -> tuple[str, ...]:
    """Decode e2fsck's documented bitmask without assigning a clean result.

    In particular, bit 1 means that e2fsck reports corrected errors.  With
    ``-n`` no repair is requested, but a return code containing that bit is
    still non-zero and must never be treated as evidence of a clean image.
    """

    if returncode < 0:
        return (f"signal-{abs(returncode)}",)

    flags = [name for bit, name in _E2FSCK_EXIT_BITS if returncode & bit]
    unknown_bits = returncode & ~sum(bit for bit, _ in _E2FSCK_EXIT_BITS)
    if unknown_bits:
        flags.append(f"unknown-0x{unknown_bits:x}")
    return tuple(flags)


def check_ext_image(image: str | Path, *, timeout: float = 60.0) -> CheckerResult:
    """Run ``e2fsck -f -n`` on a disposable copy of an ext image.

    Only an exact zero exit status, with neither source nor disposable copy
    modified, is considered clean.  e2fsck uses a bitmask result, so code 1 is
    deliberately *not* accepted as clean.
    """

    return _run_on_copy(
        image,
        tool="e2fsck",
        arguments=("-f", "-n"),
        timeout=timeout,
        exit_flags=e2fsck_exit_flags,
    )


def check_fat_image(image: str | Path, *, timeout: float = 60.0) -> CheckerResult:
    """Run the non-repair FAT checker on a disposable copy when available."""

    return _run_on_copy(
        image,
        tool="fsck.fat",
        arguments=("-n",),
        timeout=timeout,
    )


def check_exfat_image(image: str | Path, *, timeout: float = 60.0) -> CheckerResult:
    """Run the non-repair exFAT checker on a disposable copy when available."""

    return _run_on_copy(
        image,
        tool="fsck.exfat",
        arguments=("-n",),
        timeout=timeout,
    )


def check_ntfsfix_image(image: str | Path, *, timeout: float = 60.0) -> CheckerResult:
    """Run ``ntfsfix -n`` but never use it to declare an NTFS image clean.

    ntfsfix can perform limited repairs and its dry-run result is not equivalent
    to a complete NTFS check.  Thus even return code zero is ``INCONCLUSIVE``.
    """

    return _run_on_copy(
        image,
        tool="ntfsfix",
        arguments=("-n",),
        timeout=timeout,
        successful_verdict=CheckerVerdict.INCONCLUSIVE,
    )


def _run_on_copy(
    image: str | Path,
    *,
    tool: str,
    arguments: Sequence[str],
    timeout: float,
    successful_verdict: CheckerVerdict = CheckerVerdict.CLEAN,
    exit_flags: Callable[[int], tuple[str, ...]] | None = None,
) -> CheckerResult:
    """Run a non-repair checker against a short-lived copy of ``image``.

    Only a preflight ``shutil.which`` miss is represented as ``UNAVAILABLE``.
    Process launch errors deliberately propagate rather than turning a broken
    checker invocation or a project problem into a skipped test.
    """

    source = Path(image)
    if not source.is_file():
        raise FileNotFoundError(f"checker source image is not a file: {source}")
    if timeout <= 0:
        raise ValueError("checker timeout must be positive")

    executable = shutil.which(tool)
    if executable is None:
        return CheckerResult(
            tool=tool,
            available=False,
            command=(),
            returncode=None,
            stdout="",
            stderr=f"{tool} not found on PATH",
            verdict=CheckerVerdict.UNAVAILABLE,
            timed_out=False,
            source_unchanged=True,
            copy_unchanged=None,
            source_sha256_before=None,
            source_sha256_after=None,
            copy_sha256_before=None,
            copy_sha256_after=None,
        )

    source_digest = _sha256(source)
    with tempfile.TemporaryDirectory(prefix="mountfile-checker-") as directory:
        disposable = Path(directory) / source.name
        shutil.copy2(source, disposable)
        copy_digest = _sha256(disposable)
        command = (str(executable), *arguments, str(disposable))
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            source_after = _sha256(source)
            copy_after = _sha256(disposable)
            source_unchanged = source_after == source_digest
            copy_unchanged = copy_after == copy_digest
            return CheckerResult(
                tool=tool,
                available=True,
                command=command,
                returncode=None,
                stdout=_as_text(error.stdout),
                stderr=_as_text(error.stderr),
                verdict=CheckerVerdict.TIMED_OUT,
                timed_out=True,
                source_unchanged=source_unchanged,
                copy_unchanged=copy_unchanged,
                source_sha256_before=source_digest,
                source_sha256_after=source_after,
                copy_sha256_before=copy_digest,
                copy_sha256_after=copy_after,
            )

        source_after = _sha256(source)
        copy_after = _sha256(disposable)
        source_unchanged = source_after == source_digest
        copy_unchanged = copy_after == copy_digest
        flags = exit_flags(completed.returncode) if exit_flags else ()
        if completed.returncode == 0 and source_unchanged and copy_unchanged:
            verdict = successful_verdict
        else:
            verdict = CheckerVerdict.NOT_VERIFIED_CLEAN
        return CheckerResult(
            tool=tool,
            available=True,
            command=command,
            returncode=completed.returncode,
            stdout=_as_text(completed.stdout),
            stderr=_as_text(completed.stderr),
            verdict=verdict,
            timed_out=False,
            source_unchanged=source_unchanged,
            copy_unchanged=copy_unchanged,
            exit_flags=flags,
            source_sha256_before=source_digest,
            source_sha256_after=source_after,
            copy_sha256_before=copy_digest,
            copy_sha256_after=copy_after,
        )


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_checker_fixture(
    record_property,
    *,
    profile: str,
    image: str | Path,
    result: CheckerResult,
) -> None:
    """Attach content-addressed source/copy evidence to a pytest JUnit case."""
    source = Path(image)
    record_fixture_evidence(
        record_property,
        profile=profile,
        image_size_bytes=source.stat().st_size,
        source_sha256_before=result.source_sha256_before,
        source_sha256_after=result.source_sha256_after,
        checker_tool=result.tool,
        checker_verdict=result.verdict.value,
        copy_sha256_before=result.copy_sha256_before,
        copy_sha256_after=result.copy_sha256_after,
        source_unchanged=result.source_unchanged,
        copy_unchanged=result.copy_unchanged,
    )


def record_fixture_evidence(
    record_property,
    *,
    profile: str,
    image_size_bytes: int,
    source_sha256_before: str | None,
    source_sha256_after: str | None,
    checker_tool: str,
    checker_verdict: str,
    copy_sha256_before: str | None,
    copy_sha256_after: str | None,
    source_unchanged: bool,
    copy_unchanged: bool | None,
) -> None:
    """Attach stable fields for either external-copy checks or direct readers."""
    properties = {
        "mountfile.fixture.profile": profile,
        "mountfile.fixture.image_size_bytes": str(image_size_bytes),
        "mountfile.fixture.sha256_before_checker": source_sha256_before or "",
        "mountfile.fixture.sha256_after_checker": source_sha256_after or "",
        "mountfile.checker.tool": checker_tool,
        "mountfile.checker.verdict": checker_verdict,
        "mountfile.checker.copy_sha256_before": copy_sha256_before or "",
        "mountfile.checker.copy_sha256_after": copy_sha256_after or "",
        "mountfile.checker.source_unchanged": str(source_unchanged).lower(),
        "mountfile.checker.copy_unchanged": (
            "not_applicable" if copy_unchanged is None else str(copy_unchanged).lower()
        ),
    }
    for name, value in properties.items():
        record_property(name, value)


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
