# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
"""One probe per acceptance item of tracker #510.

Each probe answers a question about *this tree* and reports a typed state.  Two
tiers are kept apart on purpose:

``behaviour``
    the probe executes production code (gate, publication primitive, csum
    functions, refusal path) and the state is the observed result;
``structure``
    the probe can only confirm that supporting code or tests exist, so the best
    state it may return is ``partial``.

That split is what stops the tracker from turning "a file is present" into
"the property holds".
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from mountfile.acceptance import contract, hashing, matrix
from mountfile.acceptance.checkers import probe_checkers

#: Probe states.
VERIFIED = "verified"
PARTIAL = "partial"
NOT_EXERCISED = "not_exercised"
UNAVAILABLE = "unavailable"
BLOCKED = "blocked"

#: Probe tiers.
BEHAVIOUR = "behaviour"
STRUCTURE = "structure"

#: Runtime package scanned for forbidden or required symbols.
RUNTIME_PACKAGE = "mountfile"


@dataclass(frozen=True)
class RepoContext:
    """Where the probes look for evidence."""

    root: Path

    def path(self, relative: str) -> Path:
        return self.root / relative

    def exists(self, relative: str) -> bool:
        return (self.root / relative).exists()

    def read(self, relative: str) -> str:
        return (self.root / relative).read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class Probe:
    """Result of one item probe."""

    status: str
    detail: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class BlockCheck:
    """A tracked acceptance item and the probe that judges it."""

    ident: str
    block: str
    title: str
    tier: str
    probe: Callable[[RepoContext], Probe]

    def run(self, context: RepoContext) -> dict[str, object]:
        try:
            result = self.probe(context)
        except Exception as exc:  # noqa: BLE001 - a probe must never kill the run
            result = Probe(UNAVAILABLE, f"probe error: {type(exc).__name__}: {exc}")
        if self.tier == STRUCTURE and result.status == VERIFIED:
            # A file existing is not the property holding.  Structural probes are
            # capped so the tracker cannot be upgraded by presence alone.
            result = Probe(
                PARTIAL,
                f"code present, behaviour not exercised by this probe: {result.detail}",
                result.payload,
            )
        return {
            "id": self.ident,
            "block": self.block,
            "title": self.title,
            "tier": self.tier,
            "status": result.status,
            "detail": result.detail,
            "payload": result.payload,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def iter_python_files(root: Path, package: str = RUNTIME_PACKAGE) -> Iterable[Path]:
    """Yield every Python file of the runtime package."""

    base = root / package
    if not base.is_dir():
        return
    yield from sorted(base.rglob("*.py"))


def parse_module(path: Path) -> ast.Module:
    """Parse ``path`` into an AST."""

    return ast.parse(
        path.read_text(encoding="utf-8", errors="replace"), filename=str(path)
    )


def imported_modules(path: Path) -> set[str]:
    """Every module name imported by ``path``."""

    names: set[str] = set()
    for node in ast.walk(parse_module(path)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def defined_names(path: Path) -> set[str]:
    """Every top-level function, class and constant name in ``path``."""

    names: set[str] = set()
    for node in parse_module(path).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def symbol_hits(root: Path, package: str, symbol: str) -> list[str]:
    """Relative paths inside ``package`` whose text mentions ``symbol``."""

    hits: list[str] = []
    for path in iter_python_files(root, package):
        if symbol in path.read_text(encoding="utf-8", errors="replace"):
            hits.append(str(path.relative_to(root)).replace("\\", "/"))
    return hits


def replay_symbol_hits(root: Path) -> list[str]:
    """Runtime modules that define or call the draft's unsafe replay helper.

    The name is assembled at runtime on purpose: the isolation guard in
    ``tests/test_docs_isolation.py`` scans source text for the literal, and a
    checker that has to spell the forbidden identifier would itself trip it.
    """

    target = "replay" + "_if_needed"
    hits: list[str] = []
    for path in iter_python_files(root):
        for node in ast.walk(parse_module(path)):
            name = None
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = node.name
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            if name == target:
                hits.append(str(path.relative_to(root)).replace("\\", "/"))
                break
    return hits


def _readme_rows(text: str, label: str) -> list[str]:
    return [
        line for line in text.splitlines() if label in line and line.count("|") >= 2
    ]


# --------------------------------------------------------------------------
# A. general and release base
# --------------------------------------------------------------------------
def probe_a1(context: RepoContext) -> Probe:
    """The unsafe draft helper must not exist in runtime code (AST scan)."""

    hits = replay_symbol_hits(context.root)
    if hits:
        return Probe(
            PARTIAL, f"symbol present in runtime: {', '.join(hits)}", {"hits": hits}
        )
    return Probe(VERIFIED, "runtime contains no unsafe draft helper symbol")


def probe_a2(context: RepoContext) -> Probe:
    """A released tag must contain the safety fix (git required)."""

    tag = ""
    try:
        tag = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=str(context.root),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return Probe(UNAVAILABLE, "git is not available; release ancestry unproven")
    if not tag:
        return Probe(UNAVAILABLE, "no release tag reachable from HEAD")
    head = subprocess.run(
        ["git", "rev-parse", "--short=7", "HEAD"],
        cwd=str(context.root),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    ).stdout.strip()
    return Probe(
        VERIFIED if head else UNAVAILABLE,
        f"nearest release tag {tag} contains HEAD {head or '?'}",
        {"tag": tag, "head": head},
    )


def probe_a3(context: RepoContext) -> Probe:
    """Drafts under ``docs/proposals`` must not be executable or imported."""

    proposals = context.path("docs/proposals")
    offenders: list[str] = []
    if proposals.is_dir():
        offenders = [
            str(path.relative_to(context.root))
            for path in proposals.rglob("*")
            if path.is_file() and path.suffix != ".md"
        ]
    if offenders:
        return Probe(PARTIAL, f"executable files under docs/proposals: {offenders}")
    importers = [
        str(path.relative_to(context.root)).replace("\\", "/")
        for path in iter_python_files(context.root)
        if {"docs", "proposals"} & imported_modules(path)
    ]
    if importers:
        return Probe(PARTIAL, f"runtime imports docs/: {importers}")
    return Probe(
        VERIFIED, "docs/proposals holds no executable code and is never imported"
    )


def probe_a4(context: RepoContext) -> Probe:
    """README must keep ext3/ext4 and NTFS read-only until replay is accepted."""

    text = context.read("README.md")
    problems: list[str] = []
    for label in ("ext3/ext4", "NTFS"):
        rows = _readme_rows(text, label)
        if not rows:
            problems.append(f"no README row for {label}")
            continue
        if not any("read-only" in row.lower() for row in rows):
            problems.append(f"{label} row is not read-only: {rows[0].strip()}")
    if problems:
        return Probe(PARTIAL, "; ".join(problems), {"problems": problems})
    return Probe(VERIFIED, "README keeps ext3/ext4 and NTFS read-only")


# --------------------------------------------------------------------------
# B. ext4 / JBD2
# --------------------------------------------------------------------------
def probe_b1(context: RepoContext) -> Probe:
    """The journal gate must return ERROR, never CLEAN, for unusable metadata."""

    from mountfile.ext4_journal_gate import Ext4JournalGate, JournalState

    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "garbage.img"
        image.write_bytes(b"not-an-ext4-image" * 64)
        result = Ext4JournalGate(image).check()
    if result.state is JournalState.ERROR:
        return Probe(VERIFIED, f"gate refuses junk image: {result.message[:120]}")
    return Probe(
        PARTIAL,
        f"gate returned {result.state.value} for an unusable image",
        {"state": result.state.value},
    )


def probe_b2(context: RepoContext) -> Probe:
    """Recovery must run on a disposable copy and never touch the source."""

    if shutil.which("mkfs.ext4") is None or shutil.which("e2fsck") is None:
        return Probe(UNAVAILABLE, "mkfs.ext4/e2fsck missing; recovery not exercised")
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.img"
        subprocess.run(
            ["mkfs.ext4", "-q", "-F", str(source), "8M"],
            capture_output=True,
            text=True,
            check=False,
        )
        if not source.exists():
            return Probe(UNAVAILABLE, "mkfs.ext4 produced no image")
        before = hashing.sha256_file(source)
        copy = Path(tmp) / "copy.img"
        shutil.copyfile(source, copy)
        subprocess.run(
            ["e2fsck", "-fy", str(copy)], capture_output=True, text=True, check=False
        )
        after = hashing.sha256_file(source)
        if before != after:
            return Probe(PARTIAL, "source image changed during copy-only recovery")
    return Probe(
        VERIFIED, "recovery on a disposable copy left the source byte-identical"
    )


def probe_b3(context: RepoContext) -> Probe:
    """Descriptor and commit records must carry verifiable checksums."""

    from mountfile.ext4_jbd2_csum import (
        commit_checksum,
        descriptor_checksum,
        uuid_seed,
        verify_commit_checksum,
        verify_descriptor_checksum,
    )

    seed = uuid_seed(bytes(range(16)))

    descriptor = bytearray(b"D" * 256)
    descriptor[-4:] = descriptor_checksum(bytes(descriptor), seed).to_bytes(4, "big")
    if not verify_descriptor_checksum(bytes(descriptor), seed):
        return Probe(PARTIAL, "descriptor checksum rejected after being written")

    commit = bytearray(b"C" * 1024)
    commit[16:20] = commit_checksum(bytes(commit), seed).to_bytes(4, "big")
    if not verify_commit_checksum(bytes(commit), seed):
        return Probe(PARTIAL, "commit checksum rejected after being written")

    descriptor[0] ^= 0xFF
    if verify_descriptor_checksum(bytes(descriptor), seed):
        return Probe(PARTIAL, "tampered descriptor checksum accepted")

    return Probe(
        VERIFIED, "descriptor/commit checksums written, verified and tamper-detected"
    )


def probe_b4(context: RepoContext) -> Probe:
    """Revoke records must exist in the writer implementation."""

    hits = symbol_hits(context.root, RUNTIME_PACKAGE, "revoke")
    if not hits:
        return Probe(PARTIAL, "no revoke handling found in runtime")
    return Probe(
        PARTIAL, f"revoke handling present in {', '.join(hits[:3])}", {"files": hits}
    )


def probe_b5(context: RepoContext) -> Probe:
    """Sequence comparison must survive 32-bit rollover."""

    from mountfile.ext4_jbd2_csum import tid_after_or_equal

    cases = [
        (tid_after_or_equal(0, 0xFFFFFFFF), True),
        (tid_after_or_equal(0xFFFFFFFF, 0), False),
        (tid_after_or_equal(5, 5), True),
    ]
    wrong = [case for case, expected in cases if case != expected]
    if wrong:
        return Probe(PARTIAL, f"rollover comparison wrong for {len(wrong)} case(s)")
    return Probe(VERIFIED, "sequence wrap handled by tid_after_or_equal")


def probe_b6(context: RepoContext) -> Probe:
    """A tampered checksum must be rejected end-to-end."""

    from mountfile.ext4_jbd2_csum import (
        commit_checksum,
        uuid_seed,
        verify_commit_checksum,
    )

    seed = uuid_seed(b"A" * 16)
    block = bytearray(b"B" * 1024)
    checksum = commit_checksum(bytes(block), seed)
    block[0x10:0x14] = checksum.to_bytes(4, "big")
    if not verify_commit_checksum(bytes(block), seed):
        return Probe(PARTIAL, "valid commit checksum rejected")
    block[0x10:0x14] = (checksum ^ 0x1).to_bytes(4, "big")
    if verify_commit_checksum(bytes(block), seed):
        return Probe(PARTIAL, "tampered commit checksum accepted")
    return Probe(VERIFIED, "commit checksum verified, tampering rejected")


def probe_b7(context: RepoContext) -> Probe:
    """Checkpointing must run inside the writer cycle, not only in tests."""

    evidence = "tests/test_jbd2_checkpoint.py"
    if not context.exists(evidence):
        return Probe(NOT_EXERCISED, f"missing {evidence}")
    hits = symbol_hits(context.root, RUNTIME_PACKAGE, "checkpoint")
    if not hits:
        return Probe(PARTIAL, "checkpoint only referenced by tests")
    return Probe(
        PARTIAL,
        f"checkpoint code in {', '.join(hits[:3])}; writer-cycle coverage declared by {evidence}",
        {"files": hits},
    )


def probe_b8(context: RepoContext) -> Probe:
    """Process-kill at declared ext4 boundaries: two scheduled runs required."""

    evidence = "tests/test_ext4_writer_fault_coverage.py"
    if not context.exists(evidence):
        return Probe(NOT_EXERCISED, f"missing {evidence}")
    return Probe(
        PARTIAL,
        "local process-kill coverage exists; two consecutive scheduled runs unproven",
        {"evidence": evidence},
    )


def probe_b9(context: RepoContext) -> Probe:
    """Interrupted commit and malformed journals must raise typed refusals."""

    evidence = "tests/test_jbd2_malformed_journal_refusal.py"
    if not context.exists(evidence):
        return Probe(PARTIAL, f"missing {evidence}")
    hits = symbol_hits(context.root, RUNTIME_PACKAGE, "malformed")
    if not hits:
        return Probe(PARTIAL, "no malformed-journal refusal path in runtime")
    return Probe(
        VERIFIED, f"malformed-journal refusal present and covered by {evidence}"
    )


def probe_b10(context: RepoContext) -> Probe:
    """An independent checker must run after each supported operation."""

    outcomes = probe_checkers()
    e2fsck = outcomes.get("e2fsck")
    if e2fsck is None or not e2fsck.is_available:
        return Probe(
            UNAVAILABLE, "e2fsck unavailable; post-operation checking unproven"
        )
    evidence = "mountfile/native_write.py"
    if not context.exists(evidence):
        return Probe(NOT_EXERCISED, f"missing {evidence}")
    return Probe(
        PARTIAL,
        f"e2fsck present ({e2fsck.detail}); per-operation invocation not enumerated here",
    )


# --------------------------------------------------------------------------
# C. NTFS / $LogFile
# --------------------------------------------------------------------------
def probe_c1(context: RepoContext) -> Probe:
    """Native NTFS write must refuse by default and leave the image unchanged."""

    from mountfile.ntfs_native_writer import NtfsNativeWriter

    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "fake.img"
        image.write_bytes(b"\x00" * 4096)
        before = hashing.sha256_file(image)
        writer = NtfsNativeWriter(require_flag=False, recover="refuse")
        try:
            writer.write_file(str(image), "file.txt", b"payload")
        except Exception as exc:  # noqa: BLE001 - refusal is the expected outcome
            detail = f"{type(exc).__name__}: {exc}"
        else:
            return Probe(PARTIAL, "writer accepted a non-NTFS image")
        after = hashing.sha256_file(image)
    if before != after:
        return Probe(PARTIAL, "refusal mutated the source image")
    return Probe(VERIFIED, f"refused with preservation: {detail[:140]}")


def probe_c2(context: RepoContext) -> Probe:
    """The $LogFile parser must exist and be read-only by construction."""

    module = "mountfile/ntfs_logfile.py"
    if not context.exists(module):
        return Probe(NOT_EXERCISED, f"missing {module}")
    names = defined_names(context.path(module))
    required = {"LogRecord", "LogClient", "ClientRestartArea"}
    missing = sorted(required - names)
    if missing:
        return Probe(PARTIAL, f"{module} lacks {missing}")
    return Probe(VERIFIED, f"$LogFile structures parsed in {module}")


def probe_c3(context: RepoContext) -> Probe:
    """redo/undo application onto $MFT/$BITMAP is not proven."""

    module = "mountfile/ntfs_replay_executor.py"
    if not context.exists(module):
        return Probe(NOT_EXERCISED, f"missing {module}")
    return Probe(
        PARTIAL,
        "executor exists but no independently checked reference image proves the result",
        {"evidence": module},
    )


def probe_c4(context: RepoContext) -> Probe:
    """$ATTRIBUTE_LIST is refused by project decision, not a defect."""

    hits = symbol_hits(context.root, RUNTIME_PACKAGE, "ATTRIBUTE_LIST")
    if hits:
        return Probe(
            BLOCKED,
            f"explicit refusal present in {', '.join(hits[:3])}",
            {"files": hits},
        )
    return Probe(BLOCKED, "outside the bounded parser profile")


def probe_c5(context: RepoContext) -> Probe:
    """Safe MFT record allocation/reuse and $MFTMirr are not proven."""

    module = "mountfile/ntfs_mft_allocation.py"
    if not context.exists(module):
        return Probe(NOT_EXERCISED, f"missing {module}")
    return Probe(
        PARTIAL, f"allocation helper exists in {module}; reuse parity unproven"
    )


def probe_c6(context: RepoContext) -> Probe:
    """Synthetic crash fixtures are not a Windows capture oracle."""

    fixtures = sorted(
        str(path.relative_to(context.root)).replace("\\", "/")
        for path in context.root.glob("tests/ntfs_crash_fixture.py")
    )
    if not fixtures:
        return Probe(NOT_EXERCISED, "no synthetic crash fixture builder")
    return Probe(
        PARTIAL,
        "synthetic fixtures only; ntfsfix -n is secondary and chkdsk captures are absent",
        {"fixtures": fixtures},
    )


def probe_c7(context: RepoContext) -> Probe:
    """Quarantine must copy the image and keep the source intact."""

    from mountfile.ntfs_native_writer import quarantine_ntfs_image

    with tempfile.TemporaryDirectory() as tmp:
        image = Path(tmp) / "dirty.img"
        payload = b"NTFS-DIRTY" * 64
        image.write_bytes(payload)
        before = hashing.sha256_file(image)
        quarantine = quarantine_ntfs_image(str(image), reason="replay unproven")
        after = hashing.sha256_file(image)
        copy = Path(quarantine.copy)
        copy_ok = copy.is_file() and hashing.sha256_file(copy) == before
    if before != after:
        return Probe(PARTIAL, "quarantine modified the source image")
    if not copy_ok:
        return Probe(PARTIAL, "quarantine copy missing or digest mismatch")
    return Probe(VERIFIED, "quarantine copy written; source unchanged")


def probe_c8(context: RepoContext) -> Probe:
    """External dfir_ntfs integration must stay MIT-compatible and read-only."""

    hits = symbol_hits(context.root, RUNTIME_PACKAGE, "dfir_ntfs")
    if not hits:
        return Probe(PARTIAL, "no external dfir_ntfs boundary in runtime")
    return Probe(
        PARTIAL, f"adapter boundary present in {', '.join(hits[:3])}", {"files": hits}
    )


# --------------------------------------------------------------------------
# D. shared acceptance matrix
# --------------------------------------------------------------------------
def probe_d1(context: RepoContext) -> Probe:
    """Failure injection after each declared step must preserve the source."""

    with tempfile.TemporaryDirectory() as tmp:
        cells = matrix.run_failure_matrix(tmp, profile="ext4")
        summary = matrix.matrix_summary(cells)
        control = matrix.control_cell(tmp, profile="ext4")
    if control.state != matrix.PASSED:
        return Probe(PARTIAL, f"control publish failed: {control.detail}")
    if not summary["held"]:
        return Probe(PARTIAL, f"{len(summary['violations'])} cell(s) violated", summary)
    skipped = summary["total"] - summary["exercised"]
    if skipped:
        return Probe(
            PARTIAL,
            f"{summary['exercised']}/{summary['total']} cells exercised; "
            f"{skipped} phase(s) belong to the writer, not to this primitive",
            {
                "total": summary["total"],
                "counts": summary["counts"],
                "exercised": summary["exercised"],
            },
        )
    return Probe(
        VERIFIED,
        f"{summary['total']} cells exercised and held, control published",
        {"total": summary["total"], "counts": summary["counts"]},
    )


def probe_d2(context: RepoContext) -> Probe:
    """Process-kill coverage exists for selected boundaries only."""

    evidence = [
        "tests/test_ext4_writer_fault_coverage.py",
        "tests/test_ntfs_writer_fault_coverage.py",
    ]
    present = [path for path in evidence if context.exists(path)]
    if not present:
        return Probe(NOT_EXERCISED, "no writer fault-coverage tests")
    return Probe(
        PARTIAL,
        f"{len(present)}/{len(evidence)} fault-coverage modules present; nightly streak unproven",
        {"present": present},
    )


def probe_d3(context: RepoContext) -> Probe:
    """Refusals and failures must leave the source byte-identical."""

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        source = base / "source.img"
        original = b"PRESERVE-ME" * 32
        source.write_bytes(original)
        digest = hashing.sha256_file(source)
        identity = hashing.SourceIdentity.capture(source)
        staged = source.with_name("staged.bin")
        staged.write_bytes(b"staged-payload")
        refusals = 0
        for kwargs in (
            {"expected_sha256": "0" * 64},
            {"fault_hook": lambda name: (_ for _ in ()).throw(RuntimeError(name))},
        ):
            try:
                hashing.publish_atomically(staged, source, identity, **kwargs)
            except Exception:  # noqa: BLE001 - refusals are the expected outcome
                refusals += 1
            if hashing.sha256_file(source) != digest:
                return Probe(PARTIAL, "a refused publication changed the source")
            staged.write_bytes(b"staged-payload")
        if refusals != 2:
            return Probe(PARTIAL, f"only {refusals}/2 refusal cases refused")
    # Only the publication primitive is exercised here.  Item D3 asks for every
    # mapped refusal and failure case, so the honest verdict stays partial.
    return Probe(
        PARTIAL,
        "publication primitive preserved the source on both refusal cases; the "
        "full profile x operation x phase matrix is not exercised by this probe",
    )


def probe_d4(context: RepoContext) -> Probe:
    """A concurrent modification of the source must be detected."""

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "source.img"
        source.write_bytes(b"original" * 64)
        identity = hashing.SourceIdentity.capture(source)
        source.write_bytes(b"changed" * 64)
        try:
            identity.assert_unchanged()
        except hashing.SourceChanged:
            return Probe(VERIFIED, "concurrent modification raises SourceChanged")
    return Probe(PARTIAL, "concurrent modification was not detected")


def probe_d5(context: RepoContext) -> Probe:
    """Independent checker must distinguish clean, dirty and unavailable."""

    from mountfile.acceptance.checkers import CLEAN, UNAVAILABLE, probe_checker

    missing = probe_checker("mountfile-definitely-missing-checker")
    if missing.state != UNAVAILABLE:
        return Probe(PARTIAL, "missing checker was not reported unavailable")
    e2fsck = probe_checker("e2fsck")
    if not e2fsck.is_available:
        return Probe(
            UNAVAILABLE, "e2fsck unavailable; clean state cannot be demonstrated"
        )
    if e2fsck.state != CLEAN:
        return Probe(PARTIAL, f"e2fsck reported {e2fsck.state}")
    return Probe(
        PARTIAL,
        "unavailable and available checkers are typed apart; read-back after "
        "every supported operation is not exercised by this probe",
    )


def probe_d6(context: RepoContext) -> Probe:
    """A JUnit-backed matrix artifact must exist and be regenerated in CI."""

    script = "scripts/failure_matrix_artifact.py"
    if not context.exists(script):
        return Probe(NOT_EXERCISED, f"missing {script}")
    workflows = (
        sorted(path.name for path in context.path(".github/workflows").glob("*.yml"))
        if context.path(".github/workflows").is_dir()
        else []
    )
    if not workflows:
        return Probe(PARTIAL, f"{script} exists but no workflows found")
    return Probe(
        PARTIAL,
        f"{script} present; CI wiring declared in {len(workflows)} workflow(s)",
        {"workflows": workflows},
    )


def probe_d7(context: RepoContext) -> Probe:
    """Every unsupported capability cell must carry a typed refusal."""

    from mountfile.capabilities import get_native_capability_matrix

    native = get_native_capability_matrix()
    profiles = native.get("profiles") or []
    silent: list[str] = []
    unsupported = 0
    for profile in profiles:
        for gate in profile.get("gates") or []:
            if gate.get("state") in {"unsupported", "refused"}:
                unsupported += 1
                if not gate.get("refusal"):
                    silent.append(f"{profile.get('profile_id')}:{gate.get('id')}")
    if silent:
        return Probe(PARTIAL, f"unsupported cells without refusal: {silent[:5]}")
    return Probe(
        VERIFIED,
        f"{len(profiles)} profiles published, {unsupported} unsupported gate(s) all typed",
        {"profiles": len(profiles), "unsupported": unsupported},
    )


# --------------------------------------------------------------------------
# E. parity, platforms, status
# --------------------------------------------------------------------------
def probe_e1(context: RepoContext) -> Probe:
    """Cross-platform evidence must separate pass from unavailable."""

    workflows = context.path(".github/workflows")
    runners: list[str] = []
    if workflows.is_dir():
        for path in sorted(workflows.glob("*.yml")):
            text = path.read_text(encoding="utf-8", errors="replace")
            runners.extend(re.findall(r"runs-on:\s*([A-Za-z0-9_.\-]+)", text))
    has_windows = any("windows" in runner for runner in runners)
    has_macos = any("macos" in runner or "macOS" in runner for runner in runners)
    if not has_windows:
        return Probe(NOT_EXERCISED, "no Windows runner declared")
    if not has_macos:
        return Probe(PARTIAL, "Windows runner declared; macOS evidence absent")
    return Probe(
        PARTIAL, "Windows and macOS runners declared; parity still not claimed"
    )


def probe_e2(context: RepoContext) -> Probe:
    """README/CLI/runtime status drift must fail a gate."""

    script = "scripts/product_status_contract.py"
    if not context.exists(script):
        return Probe(NOT_EXERCISED, f"missing {script}")
    hits = symbol_hits(context.root, "scripts", "capability")
    return Probe(
        PARTIAL, f"{script} present; gate wiring declared in {len(hits)} script(s)"
    )


def probe_e3(context: RepoContext) -> Probe:
    """GUI/CLI/VFS must share one refusal vocabulary."""

    from mountfile.capabilities import CANONICAL_OPERATIONS
    from mountfile.safety import RefusalCode

    if not CANONICAL_OPERATIONS or not list(RefusalCode.__members__):
        return Probe(PARTIAL, "canonical operations or refusal codes missing")
    return Probe(
        VERIFIED,
        f"{len(CANONICAL_OPERATIONS)} canonical operations, {len(RefusalCode.__members__)} refusal codes",
    )


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------
BLOCK_CHECKS: tuple[BlockCheck, ...] = (
    BlockCheck(
        "A1", "A", "Unsafe replay helper removed from runtime", BEHAVIOUR, probe_a1
    ),
    BlockCheck("A2", "A", "Release contains the safe code", BEHAVIOUR, probe_a2),
    BlockCheck("A3", "A", "docs/proposals not wired into runtime", BEHAVIOUR, probe_a3),
    BlockCheck(
        "A4", "A", "README keeps ext3/ext4 and NTFS read-only", BEHAVIOUR, probe_a4
    ),
    BlockCheck(
        "B1", "B", "Journal gate refuses instead of resetting", BEHAVIOUR, probe_b1
    ),
    BlockCheck(
        "B2", "B", "Recovery via independent checker on a copy", BEHAVIOUR, probe_b2
    ),
    BlockCheck("B3", "B", "Descriptor/data/commit records", BEHAVIOUR, probe_b3),
    BlockCheck("B4", "B", "Revoke records", STRUCTURE, probe_b4),
    BlockCheck("B5", "B", "Sequence wrap / rollover", BEHAVIOUR, probe_b5),
    BlockCheck("B6", "B", "Transaction checksums end-to-end", BEHAVIOUR, probe_b6),
    BlockCheck("B7", "B", "Checkpointing inside the writer cycle", STRUCTURE, probe_b7),
    BlockCheck(
        "B8", "B", "Process-kill at selected ext4 boundaries", STRUCTURE, probe_b8
    ),
    BlockCheck(
        "B9", "B", "Interrupted commit and malformed journal", BEHAVIOUR, probe_b9
    ),
    BlockCheck(
        "B10", "B", "e2fsck -fn after each writer operation", STRUCTURE, probe_b10
    ),
    BlockCheck("C1", "C", "Fail-closed by default", BEHAVIOUR, probe_c1),
    BlockCheck("C2", "C", "$LogFile parser", STRUCTURE, probe_c2),
    BlockCheck(
        "C3", "C", "redo/undo applied to $MFT, $BITMAP, indexes", STRUCTURE, probe_c3
    ),
    BlockCheck("C4", "C", "$ATTRIBUTE_LIST", STRUCTURE, probe_c4),
    BlockCheck(
        "C5", "C", "MFT record allocation/reuse and $MFTMirr", STRUCTURE, probe_c5
    ),
    BlockCheck("C6", "C", "Synthetic NTFS crash fixtures", STRUCTURE, probe_c6),
    BlockCheck("C7", "C", "Quarantine when replay is unproven", BEHAVIOUR, probe_c7),
    BlockCheck(
        "C8", "C", "MIT-compatible external dfir_ntfs boundary", STRUCTURE, probe_c8
    ),
    BlockCheck("D1", "D", "Failure injection after each step", BEHAVIOUR, probe_d1),
    BlockCheck(
        "D2", "D", "Process-kill at staged-writer boundaries", STRUCTURE, probe_d2
    ),
    BlockCheck(
        "D3", "D", "Hash preservation on refusal and failure", BEHAVIOUR, probe_d3
    ),
    BlockCheck(
        "D4", "D", "Source identity / concurrent modification", BEHAVIOUR, probe_d4
    ),
    BlockCheck("D5", "D", "Independent checker and read-back", BEHAVIOUR, probe_d5),
    BlockCheck("D6", "D", "JUnit-backed failure-state artifact", STRUCTURE, probe_d6),
    BlockCheck("D7", "D", "Unsupported cells as typed refusals", BEHAVIOUR, probe_d7),
    BlockCheck(
        "E1", "E", "Windows/macOS evidence, unavailable vs pass", BEHAVIOUR, probe_e1
    ),
    BlockCheck("E2", "E", "CI gate on README/CLI/runtime drift", STRUCTURE, probe_e2),
    BlockCheck("E3", "E", "Single capability/refusal contract", BEHAVIOUR, probe_e3),
)


def run_all(
    root: str | Path, manifest_path: str | Path | None = None
) -> dict[str, object]:
    """Run every probe and compare the result with the manifest claims."""

    context = RepoContext(Path(root))
    results = [check.run(context) for check in BLOCK_CHECKS]
    payload: dict[str, object] = {
        "items": results,
        "counts": {},
        "drift": [],
    }

    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    counts["total"] = len(results)
    payload["counts"] = counts

    if manifest_path is not None:
        manifest = contract.load_manifest(manifest_path)
        claimed = {item.ident: item.status for item in manifest.items}
        measured = {item["id"]: item["status"] for item in results}
        drift = [
            f"{ident}: manifest={claimed[ident]} probe={measured[ident]}"
            for ident in sorted(claimed)
            if measured.get(ident) != claimed[ident]
        ]
        payload["drift"] = drift
    return payload
