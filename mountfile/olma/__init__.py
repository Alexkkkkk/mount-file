# MOUNTFILE_FIX — OLMA package
"""OLMA — Object-Level Memory Analysis & Access + self-learning.

Единая точка входа:
    from mountfile.olma import olma
    olma.install()                # включить трассировку
    olma.analyze()                # статический анализ проекта
    olma.fix(dry_run=True)        # автоисправление
    olma.report(fmt="html")       # отчёт
    olma.trace_stats()            # статистика runtime-трассировки
    olma.learn()                  # цикл самообучения
"""

from __future__ import annotations

from .analyzer import Issue, OlmaAnalyzer
from .cli import olma_main
from .core import (FAT32_BAD, FAT32_EOC_MIN, FAT32_MAX_CLUSTER, SECTOR,
                   OlmaBoundsError, OlmaBuffer, OlmaChain, OlmaCluster,
                   OlmaError, OlmaHandle, OlmaIntegrityError, OlmaPartition,
                   OlmaPartitionTable, OlmaStateError, OlmaTransaction,
                   OlmaView, OlmaXorDecoder, safe_download, verify_sha256)
from .fixer import FixResult, OlmaFixer
from .github import GhResult, OlmaGitHub, OlmaGitHubError, from_env
from .learn import OlmaKnowledgeBase, OlmaLearner
from .reporter import OlmaReporter
from .rules import RULES, Rule, Severity
from .runtime import OlmaRuntime, RuntimeEvent


class Olma:
    """Фасад для всех операций OLMA."""

    def __init__(self) -> None:
        self._runtime: OlmaRuntime | None = None
        self._analyzer: OlmaAnalyzer | None = None
        self._learner: OlmaLearner | None = None

    # --- runtime ---
    def install(self, *, trace_files: bool = True, trace_memory: bool = False) -> None:
        if self._runtime is not None:
            return
        self._runtime = OlmaRuntime(
            trace_files=trace_files,
            trace_memory=trace_memory,
        )
        self._runtime.install()

    def uninstall(self) -> None:
        if self._runtime:
            self._runtime.uninstall()
            self._runtime = None

    def trace_stats(self) -> dict:
        return self._runtime.stats() if self._runtime else {}

    def trace_events(self, limit: int = 100) -> list:
        return self._runtime.events(limit) if self._runtime else []

    # --- analysis ---
    def analyzer(self, root: str | None = None) -> OlmaAnalyzer:
        if self._analyzer is None:
            from pathlib import Path

            base = Path(root) if root else Path(__file__).resolve().parents[1]
            self._analyzer = OlmaAnalyzer(base)
        return self._analyzer

    def analyze(self, root: str | None = None) -> list:
        return self.analyzer(root).scan()

    # --- fix ---
    def fix(self, *, dry_run: bool = True, root: str | None = None) -> list:
        from .fixer import OlmaFixer

        issues = self.analyze(root)
        fixer = OlmaFixer(self.analyzer(root).root, dry_run=dry_run)
        return fixer.apply(issues)

    # --- report ---
    def report(
        self, *, fmt: str = "html", out: str | None = None, root: str | None = None
    ) -> str:
        from .reporter import OlmaReporter

        issues = self.analyze(root)
        rep = OlmaReporter(issues, self.trace_stats())
        return rep.render(fmt=fmt, out=out)

    # --- self-learning ---
    def learner(self, root: str | None = None) -> OlmaLearner:
        if self._learner is None:
            from pathlib import Path

            base = Path(root) if root else Path(__file__).resolve().parents[1]
            self._learner = OlmaLearner(base)
        return self._learner

    def learn(self, root: str | None = None, *, smoke_ok: bool = True) -> None:
        issues = self.analyze(root)
        fixes = self.fix(dry_run=True, root=root)
        self.learner(root).learn(issues, fixes, smoke_ok=smoke_ok)

    def suggest(self, issue, root: str | None = None) -> list:
        return self.learner(root).suggest(issue)

    # --- github publishing ---
    def publish_issue(
        self, *, repo: str | None = None, dry_run: bool = True, root: str | None = None
    ) -> GhResult:
        from .github import from_env

        issues = self.analyze(root)
        gh = from_env(repo, dry_run=dry_run)
        return gh.publish_analysis(issues, self.trace_stats())

    def publish_pr(
        self, *, repo: str | None = None, dry_run: bool = True, root: str | None = None
    ) -> GhResult:
        from pathlib import Path

        from .github import from_env

        fixes = [r for r in self.fix(dry_run=False, root=root) if r.applied]
        base = Path(root) if root else Path(__file__).resolve().parents[1]
        changes: dict[str, bytes] = {}
        for r in fixes:
            p = base / r.file
            if p.exists() and r.file not in changes:
                changes[r.file] = p.read_bytes()
        gh = from_env(repo, dry_run=dry_run)
        return gh.publish_fixes_pr(fixes, changes)


olma = Olma()

__all__ = [
    "olma",
    "Olma",
    "OlmaError",
    "OlmaBoundsError",
    "OlmaIntegrityError",
    "OlmaStateError",
    "OlmaHandle",
    "OlmaView",
    "OlmaBuffer",
    "OlmaCluster",
    "OlmaChain",
    "OlmaPartition",
    "OlmaPartitionTable",
    "OlmaTransaction",
    "OlmaXorDecoder",
    "OlmaRuntime",
    "RuntimeEvent",
    "OlmaAnalyzer",
    "OlmaFixer",
    "OlmaReporter",
    "OlmaLearner",
    "OlmaKnowledgeBase",
    "OlmaGitHub",
    "OlmaGitHubError",
    "GhResult",
    "from_env",
    "Issue",
    "Rule",
    "Severity",
    "RULES",
    "FixResult",
    "safe_download",
    "verify_sha256",
    "olma_main",
    "SECTOR",
    "FAT32_MAX_CLUSTER",
    "FAT32_EOC_MIN",
    "FAT32_BAD",
]
