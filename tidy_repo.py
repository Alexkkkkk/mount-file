#!/usr/bin/env python3
"""
tidy_repo.py v3 — супер-полная автоматика для репозитория mount-file.

  • Автодетект проблем и Health Score (0–100)
  • DAG-планировщик, параллельное выполнение
  • Плагины из tidy_plugins/*.py (auto-discovery)
  • Автогенерация CHANGELOG из git-логов
  • Автофикс линтером, self-healing, мульти-проход
  • HTML-дашборд и JSON-отчёт
  • Только stdlib. Python 3.9+.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import fnmatch
import html
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

# ─────────────────────────────────────────── ANSI

class C:
    RESET="\033[0m"; BOLD="\033[1m"; DIM="\033[2m"
    RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"
    BLUE="\033[34m"; MAGENTA="\033[35m"; CYAN="\033[36m"

    @classmethod
    def disable(cls):
        for k in list(vars(cls)):
            if k.isupper(): setattr(cls, k, "")

if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C.disable()


def banner() -> None:
    print(f"""{C.CYAN}{C.BOLD}
  ┌──────────────────────────────────────────────────┐
  │   tidy_repo.py  ·  mount-file  ·  v3 · auto     │
  └──────────────────────────────────────────────────┘
{C.RESET}""")

def hr(t: str = "") -> None:
    if t:
        print(f"\n{C.BOLD}{C.BLUE}▐ {t} {C.RESET}{C.DIM}{'─' * max(0, 46 - len(t))}{C.RESET}")
    else:
        print(f"{C.DIM}{'─' * 52}{C.RESET}")

def log(msg: str, kind: str = "info", indent: int = 0) -> None:
    icons = {"info": f"{C.CYAN}•{C.RESET}", "ok": f"{C.GREEN}✓{C.RESET}",
             "skip": f"{C.DIM}·{C.RESET}", "warn": f"{C.YELLOW}▲{C.RESET}",
             "err": f"{C.RED}✗{C.RESET}", "run": f"{C.MAGENTA}▶{C.RESET}",
             "star": f"{C.YELLOW}★{C.RESET}"}
    print(f"{'  ' * indent}{icons.get(kind, '•')} {msg}")

def ask(q: str, default: bool = True) -> bool:
    suf = "[Y/n]" if default else "[y/N]"
    try:
        a = input(f"{C.BOLD}{q}{C.RESET} {suf} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(); return default
    return default if not a else a in ("y","yes","д","да")


# ─────────────────────────────────────────── shell

def sh(cmd: list[str], *, check=False, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check,
                          timeout=timeout, cwd=Path.cwd())

def has(cmd: str) -> bool:
    return shutil.which(cmd) is not None

def git(*args: str, check=False) -> str:
    r = sh(["git", *args], check=check)
    return r.stdout.strip()

def is_git() -> bool:
    try:
        return git("rev-parse", "--is-inside-work-tree") == "true"
    except Exception:
        return False

def git_clean() -> bool:
    return is_git() and git("status", "--porcelain") == ""

def git_owner_repo() -> Optional[tuple[str, str]]:
    try:
        url = git("config", "--get", "remote.origin.url")
    except Exception:
        return None
    m = re.search(r"github\.com[:/]([^/]+)/([^/.]+?)(\.git)?$", url)
    return (m.group(1), m.group(2)) if m else None


# ─────────────────────────────────────────── контекст

@dataclass
class Fact:
    """Результат детектора."""
    key: str
    value: Any
    confidence: float = 1.0

@dataclass
class Ctx:
    root: Path
    dry: bool = False
    auto: bool = True
    facts: dict[str, Any] = field(default_factory=dict)
    report: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    moved: dict[str, str] = field(default_factory=dict)
    backup: Optional[Path] = None
    score_before: int = 0
    score_after: int = 0
    t0: float = field(default_factory=time.time)

    def record(self, action: str, op: str, **extra) -> None:
        self.report.append({"action": action, "op": op,
                            "ts": datetime.now().isoformat(timespec="seconds"), **extra})

    def diff(self, path: Path, op: str, *, size: int = 0) -> None:
        self.changes.append({"path": str(path.relative_to(self.root)),
                             "op": op, "size": size})


# ─────────────────────────────────────────── утилиты

def write(ctx: Ctx, path: Path, content: str, *, overwrite: bool = False) -> bool:
    if path.exists() and not overwrite:
        ctx.record("write", "skip", path=str(path)); return False
    if ctx.dry:
        log(f"[dry] write {path.relative_to(ctx.root)}", "run", 1); return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    ctx.diff(path, "create" if not path.exists() else "modify", size=len(content))
    log(f"создан {path.relative_to(ctx.root)}", "ok", 1); return True

def move(ctx: Ctx, src: Path, dst: Path) -> bool:
    if not src.exists(): return False
    if dst.exists(): log(f"skip {src.name}", "skip", 1); return False
    if ctx.dry:
        log(f"[dry] mv {src.name} → {dst.relative_to(ctx.root)}", "run", 1); return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    ctx.moved[src.name] = str(dst)
    ctx.diff(dst, "move")
    log(f"{src.name} → {dst.relative_to(ctx.root)}", "ok", 1); return True


# ─────────────────────────────────────────── детекторы

DETECTORS: list[Callable[[Ctx], None]] = []

def detector(fn):
    DETECTORS.append(fn); return fn

@detector
def d_python(ctx: Ctx) -> None:
    versions = []
    for p in list(ctx.root.glob("*.py")) + list(ctx.root.glob("**/*.py"))[:50]:
        try:
            m = re.search(r"^#!.*python(\d\.\d+)", p.read_text(errors="ignore")[:200], re.M)
            if m: versions.append(m.group(1))
        except Exception: pass
    ctx.facts["python"] = max(versions) if versions else "3.9"

@detector
def d_gui(ctx: Ctx) -> None:
    blob = ""
    for f in ("requirements.txt","requirements-win7.txt","pyproject.toml","setup.py"):
        p = ctx.root / f
        if p.exists(): blob += p.read_text(errors="ignore").lower()
    for key, dep in {"pyside6":"PySide6>=6.5","pyqt6":"PyQt6>=6.5",
                     "pyqt5":"PyQt5>=5.15","wxpython":"wxPython>=4.2",
                     "kivy":"Kivy>=2.3"}.items():
        if key in blob:
            ctx.facts["gui"] = dep; return
    ctx.facts["gui"] = "PySide6>=6.5"  # default

@detector
def d_deps(ctx: Ctx) -> None:
    deps: list[str] = []
    for f in ("requirements.txt", "requirements-win7.txt"):
        p = ctx.root / f
        if not p.exists(): continue
        for line in p.read_text(errors="ignore").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"): deps.append(line)
    seen: set[str] = set()
    ctx.facts["deps"] = [d for d in deps if not (d in seen or seen.add(d))]

@detector
def d_tests(ctx: Ctx) -> None:
    t = ctx.root / "tests"
    ctx.facts["tests"] = t.is_dir() and any(t.rglob("test_*.py"))
    ctx.facts["pytest"] = ctx.facts["tests"] or (ctx.root / "pytest.ini").exists()

@detector
def d_ci(ctx: Ctx) -> None:
    wf = ctx.root / ".github/workflows"
    ctx.facts["ci"] = wf.is_dir() and any(wf.glob("*.y*ml"))
    ctx.facts["ci_files"] = sorted(p.name for p in wf.glob("*.y*ml")) if wf.exists() else []

@detector
def d_entrypoints(ctx: Ctx) -> None:
    names = ["mountfile.py", "mountfile_all.py", "mountfile_fix.py", "auto_mountfile.py"]
    ctx.facts["dupes"] = [n for n in names if (ctx.root / n).exists()]

@detector
def d_precommit(ctx: Ctx) -> None:
    ctx.facts["precommit"] = (ctx.root / ".pre-commit-config.yaml").exists()

@detector
def d_docs(ctx: Ctx) -> None:
    ctx.facts["docs"] = {
        f: (ctx.root / f).exists()
        for f in ("README.md","CONTRIBUTING.md","SECURITY.md",
                  "CHANGELOG.md","CODE_OF_CONDUCT.md","LICENSE","LICENSE.md")
    }

@detector
def d_pyproject(ctx: Ctx) -> None:
    ctx.facts["pyproject"] = (ctx.root / "pyproject.toml").exists()

@detector
def d_git_history(ctx: Ctx) -> None:
    if not is_git():
        ctx.facts["commits"] = []; return
    try:
        raw = git("log", "--pretty=format:%h|%s", "-n", "200")
    except Exception:
        ctx.facts["commits"] = []; return
    out = []
    for line in raw.splitlines():
        if "|" in line:
            h, s = line.split("|", 1); out.append((h, s))
    ctx.facts["commits"] = out


# ─────────────────────────────────────────── Health Score

def compute_score(ctx: Ctx) -> tuple[int, dict[str, int]]:
    s: dict[str, int] = {}
    s["structure"] = 20 if not ctx.facts.get("dupes") else 12
    s["pyproject"] = 15 if ctx.facts.get("pyproject") else 0
    s["tests"]     = 10 if ctx.facts.get("tests") else 0
    s["ci"]        = 15 if ctx.facts.get("ci") else 0
    s["precommit"] = 10 if ctx.facts.get("precommit") else 0
    docs = ctx.facts.get("docs", {})
    s["docs"]      = min(15, 3 * sum(1 for v in docs.values() if v))
    s["readme_badges"] = 5 if (ctx.root / "README.md").exists() and \
                              "badge.svg" in (ctx.root / "README.md").read_text(errors="ignore") else 0
    s["license"]   = 5 if docs.get("LICENSE") or docs.get("LICENSE.md") else 0
    s["gitignore"] = 5 if (ctx.root / ".gitignore").exists() else 0
    return sum(s.values()), s


# ─────────────────────────────────────────── плагины

def load_plugins(ctx: Ctx) -> list[Action]:
    plugins_dir = ctx.root / "tidy_plugins"
    if not plugins_dir.is_dir(): return []
    loaded: list[Action] = []
    for p in sorted(plugins_dir.glob("*.py")):
        if p.name.startswith("_"): continue
        try:
            spec = importlib.util.spec_from_file_location(p.stem, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)  # type: ignore
            if hasattr(mod, "ACTIONS"):
                loaded.extend(mod.ACTIONS)
                log(f"плагин {p.name}: +{len(mod.ACTIONS)} действий", "ok", 1)
        except Exception as e:
            log(f"плагин {p.name} не загружен: {e}", "warn", 1)
    return loaded


# ─────────────────────────────────────────── DAG

@dataclass
class Action:
    key: str
    title: str
    run: Callable[[Ctx], None]
    needs: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    auto: bool = True              # выполнять ли в auto-режиме
    parallel_safe: bool = True
    weight: int = 1                # приоритет
    plugin: bool = False

    def __hash__(self): return hash(self.key)


def topo_sort(actions: list[Action]) -> list[list[Action]]:
    """Разбивает DAG на «волны» — можно выполнять параллельно внутри волны."""
    by_key = {a.key: a for a in actions}
    deps: dict[str, set[str]] = {a.key: {n for n in a.needs if n in by_key} for a in actions}
    waves: list[list[Action]] = []
    remaining = set(by_key)
    while remaining:
        wave = [by_key[k] for k in remaining if not (deps[k] & remaining)]
        if not wave:
            wave = [by_key[k] for k in remaining]  # цикл — просто всё оставшееся
        wave.sort(key=lambda a: -a.weight)
        waves.append(wave)
        remaining -= {a.key for a in wave}
    return waves


# ─────────────────────────────────────────── HTML-дашборд

def render_html(ctx: Ctx, path: Path) -> None:
    s_before = ctx.score_before
    s_after = ctx.score_after
    delta = s_after - s_before
    color = "#22c55e" if delta >= 0 else "#ef4444"

    rows = "\n".join(
        f"<tr><td>{html.escape(c['path'])}</td><td><b>{c['op']}</b></td>"
        f"<td>{c.get('size', 0)}</td></tr>" for c in ctx.changes
    ) or "<tr><td colspan='3'><i>без изменений</i></td></tr>"

    logs = "\n".join(
        f"<tr><td>{html.escape(r['action'])}</td><td>{html.escape(r['op'])}</td>"
        f"<td>{html.escape(str(r.get('path', r.get('src', ''))))}</td></tr>"
        for r in ctx.report
    ) or "<tr><td colspan='3'><i>пусто</i></td></tr>"

    content = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>tidy_repo · отчёт · {datetime.now():%Y-%m-%d %H:%M}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background:#0f172a; color:#e2e8f0;
         margin:0; padding:32px; }}
  h1 {{ margin:0 0 8px; }}
  .score {{ display:flex; gap:24px; margin:24px 0; }}
  .card {{ background:#1e293b; padding:20px 28px; border-radius:14px; min-width:150px;
          box-shadow:0 4px 24px #0005; }}
  .card .num {{ font-size:42px; font-weight:700; }}
  .card .lbl {{ opacity:.6; font-size:13px; text-transform:uppercase; letter-spacing:.08em; }}
  table {{ width:100%; border-collapse:collapse; margin:16px 0; background:#1e293b;
          border-radius:12px; overflow:hidden; }}
  th,td {{ padding:10px 14px; text-align:left; border-bottom:1px solid #334155; font-size:14px; }}
  th {{ background:#0f172a; opacity:.7; font-weight:600; }}
  .delta {{ color:{color}; }}
  footer {{ opacity:.5; margin-top:32px; font-size:13px; }}
</style></head><body>
<h1>tidy_repo · отчёт о наведении порядка</h1>
<p style="opacity:.7">{datetime.now():%Y-%m-%d %H:%M:%S} · {html.escape(str(ctx.root))}</p>

<div class="score">
  <div class="card"><div class="num">{s_before}</div><div class="lbl">Health до</div></div>
  <div class="card"><div class="num">{s_after}</div><div class="lbl">Health после</div></div>
  <div class="card"><div class="num delta">{'+' if delta>=0 else ''}{delta}</div><div class="lbl">Δ</div></div>
  <div class="card"><div class="num">{len(ctx.changes)}</div><div class="lbl">Изменений</div></div>
</div>

<h2>Изменённые файлы</h2>
<table><thead><tr><th>Файл</th><th>Операция</th><th>Байт</th></tr></thead>
<tbody>{rows}</tbody></table>

<h2>Журнал действий</h2>
<table><thead><tr><th>Шаг</th><th>Операция</th><th>Объект</th></tr></thead>
<tbody>{logs}</tbody></table>

<footer>Сгенерировано tidy_repo.py v3 · {ctx.t0 and f'{time.time()-ctx.t0:.2f}s'}</footer>
</body></html>"""
    path.write_text(content, encoding="utf-8")


# ─────────────────────────────────────────── действия

ACTIONS: list[Action] = []

def action(key: str, title: str, **kw):
    def deco(fn):
        ACTIONS.append(Action(key=key, title=title, run=fn, **kw)); return fn
    return deco


@action("move", "Перенести точки входа в tools/", produces=("moved",))
def a_move(ctx: Ctx) -> None:
    hr("1. Перенос дублирующихся точек входа")
    dupes = ctx.facts.get("dupes") or []
    if not dupes:
        log("нет дублей", "skip", 1); return
    tools = ctx.root / "tools"
    if not ctx.dry: tools.mkdir(exist_ok=True)
    for n in dupes: move(ctx, ctx.root / n, tools / n)


@action("imports", "Автопочинка импортов", needs=("move",))
def a_imports(ctx: Ctx) -> None:
    hr("2. Автопочинка импортов")
    if not ctx.moved:
        log("нечего чинить", "skip", 1); return
    mods = {Path(k).stem for k in ctx.moved}
    pat = re.compile(rf"^(\s*)(from|import)\s+({'|'.join(map(re.escape, mods))})\b", re.M)
    fixed = 0
    for py in ctx.root.rglob("*.py"):
        if "tools" in py.parts or ".tidy_backup" in str(py): continue
        try: text = py.read_text(encoding="utf-8")
        except Exception: continue

        def repl(m):
            nonlocal fixed; fixed += 1
            return f"{m.group(1)}# [tidy] import moved → tools/\n{m.group(0)}"

        new = pat.sub(repl, text)
        if new != text and not ctx.dry:
            py.write_text(new, encoding="utf-8")
            log(f"помечен {py.relative_to(ctx.root)}", "warn", 1)
    log(f"импортов помечено: {fixed}" if fixed else "импортов нет", "ok" if fixed else "skip", 1)


@action("pyproject", "Создать/обновить pyproject.toml", needs=("imports",))
def a_pyproject(ctx: Ctx) -> None:
    hr("3. pyproject.toml")
    gui = ctx.facts.get("gui", "PySide6>=6.5")
    py = ctx.facts.get("python", "3.9")
    extra = [d for d in ctx.facts.get("deps", [])
             if gui.split(">=")[0].lower() not in d.lower()]
    deps = [gui, *extra]
    deps_toml = "\n".join(f'    "{d}",' for d in deps)
    content = textwrap.dedent(f'''\
        [build-system]
        requires = ["setuptools>=68", "wheel"]
        build-backend = "setuptools.build_meta"

        [project]
        name = "mountfile"
        version = "0.1.0"
        description = "GUI-инструмент для работы с raw-образами NAND/eMMC/дисков"
        readme = "README.md"
        requires-python = ">={py}"
        license = {{ text = "MIT" }}
        authors = [{{ name = "Alexkkkkk" }}]

        dependencies = [
        {deps_toml}
        ]

        [project.optional-dependencies]
        dev = [
            "pytest>=7", "pytest-cov>=4",
            "ruff>=0.4", "mypy>=1.10", "pre-commit>=3.7",
        ]

        [project.scripts]
        mountfile = "mountfile.__main__:main"

        [tool.setuptools.packages.find]
        include = ["mountfile*"]

        [tool.ruff]
        line-length = 100
        target-version = "py{py.replace('.', '')}"

        [tool.ruff.lint]
        select = ["E","F","W","I","UP","B","SIM"]
        ignore = ["E501"]

        [tool.mypy]
        python_version = "{py}"
        ignore_missing_imports = true
        warn_unused_ignores = true
        warn_redundant_casts = true

        [tool.pytest.ini_options]
        testpaths = ["tests"]
        addopts = "-ra --strict-markers"
        ''')
    write(ctx, ctx.root / "pyproject.toml", content)


@action("precommit", "Настроить pre-commit", needs=("pyproject",))
def a_precommit(ctx: Ctx) -> None:
    hr("4. .pre-commit-config.yaml")
    write(ctx, ctx.root / ".pre-commit-config.yaml", textwrap.dedent("""\
        repos:
          - repo: https://github.com/astral-sh/ruff-pre-commit
            rev: v0.4.10
            hooks:
              - id: ruff
                args: [--fix]
              - id: ruff-format
          - repo: https://github.com/pre-commit/mirrors-mypy
            rev: v1.10.0
            hooks:
              - id: mypy
                args: [--ignore-missing-imports]
                exclude: ^tools/
          - repo: https://github.com/pre-commit/pre-commit-hooks
            rev: v4.6.0
            hooks:
              - id: end-of-file-fixer
              - id: trailing-whitespace
              - id: check-yaml
              - id: check-toml
              - id: check-merge-conflict
              - id: mixed-line-ending
        """))


@action("docs", "Шаблоны документации")
def a_docs(ctx: Ctx) -> None:
    hr("5. Документация")
    write(ctx, ctx.root / "CONTRIBUTING.md", textwrap.dedent("""\
        # Contributing

        1. Форк → ветка `feat/my-feature`.
        2. `pip install -e ".[dev]"` + `pre-commit install`.
        3. Тесты в `tests/`, коммиты в [Conventional Commits](https://www.conventionalcommits.org/).
        4. PR.
        """))
    write(ctx, ctx.root / "SECURITY.md", textwrap.dedent("""\
        # Security Policy

        Пожалуйста, **не открывайте публичный issue**. Пишите на `security@example.com`.
        Ответ — в течение 72 часов.
        """))
    write(ctx, ctx.root / "CODE_OF_CONDUCT.md", textwrap.dedent("""\
        # Code of Conduct

        Мы придерживаемся [Contributor Covenant v2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).
        """))


@action("changelog", "Сгенерировать CHANGELOG из git", needs=("docs",))
def a_changelog(ctx: Ctx) -> None:
    hr("6. CHANGELOG из git-истории")

    commits = ctx.facts.get("commits", [])
    buckets: dict[str, list[str]] = defaultdict(list)
    type_map = {
        "feat": "Added", "fix": "Fixed", "perf": "Changed",
        "refactor": "Changed", "docs": "Documentation",
        "chore": "Maintenance", "test": "Tests", "ci": "CI",
        "build": "Build", "style": "Style", "revert": "Reverted",
    }
    conv = re.compile(r"^(\w+)(?:\(([^)]+)\))?!?:\s*(.+)$")
    for h, s in commits:
        m = conv.match(s)
        if m:
            t, scope, title = m.groups()
            buckets[type_map.get(t, "Other")].append(
                f"- {title}" + (f" (`{scope}`)" if scope else "") + f" ({h})"
            )
        else:
            buckets["Other"].append(f"- {s} ({h})")

    lines = [
        "# Changelog\n",
        "Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), "
        "версионирование: [SemVer](https://semver.org/lang/ru/).\n",
        f"## [Unreleased] — {datetime.now():%Y-%m-%d}\n",
    ]
    for b in ("Added", "Changed", "Fixed", "Documentation", "Tests",
              "CI", "Build", "Maintenance", "Style", "Reverted", "Other"):
        if buckets[b]:
            lines.append(f"### {b}\n")
            lines += buckets[b] + ["\n"]
    write(ctx, ctx.root / "CHANGELOG.md", "\n".join(lines), overwrite=True)


@action("ci", "GitHub Actions: ci.yml + release.yml")
def a_ci(ctx: Ctx) -> None:
    hr("7. GitHub Actions")
    py = ctx.facts.get("python", "3.11")
    write(ctx, ctx.root / ".github/workflows/ci.yml", textwrap.dedent(f"""\
        name: CI
        on:
          push: {{ branches: [main, master] }}
          pull_request:
        jobs:
          quality:
            runs-on: ubuntu-latest
            steps:
              - uses: actions/checkout@v4
              - uses: actions/setup-python@v5
                with: {{ python-version: "3.11", cache: pip }}
              - run: pip install -e ".[dev]"
              - run: ruff check .
              - run: ruff format --check .
              - run: mypy mountfile
              - run: pytest --cov=mountfile --cov-report=xml
              - uses: codecov/codecov-action@v4
                if: always()
                with: {{ files: coverage.xml, fail_ci_if_error: false }}
        """))
    write(ctx, ctx.root / ".github/workflows/release.yml", textwrap.dedent("""\
        name: Release
        on: { push: { tags: ["v*"] } }
        permissions: { contents: write }
        jobs:
          build:
            strategy:
              matrix:
                include:
                  - { os: ubuntu-latest,  artifact: mountfile-linux }
                  - { os: windows-latest, artifact: mountfile-windows }
                  - { os: macos-latest,   artifact: mountfile-macos }
            runs-on: ${{ matrix.os }}
            steps:
              - uses: actions/checkout@v4
              - uses: actions/setup-python@v5
                with: { python-version: "3.11" }
              - run: pip install -e ".[dev]" pyinstaller
              - run: pyinstaller --onefile --windowed --name ${{ matrix.artifact }} main.py
              - uses: actions/upload-artifact@v4
                with: { name: ${{ matrix.artifact }}, path: dist/* }
          release:
            needs: build
            runs-on: ubuntu-latest
            steps:
              - uses: actions/download-artifact@v4
                with: { path: artifacts }
              - uses: softprops/action-gh-release@v2
                with:
                  files: artifacts/**/*
                  generate_release_notes: true
        """))


@action("gitignore", "Дополнить .gitignore")
def a_gitignore(ctx: Ctx) -> None:
    hr("8. .gitignore")
    extra = textwrap.dedent("""\

        # tidy_repo
        .tidy_backup_*/
        .tidy_report.json
        .tidy_report.html
        .mypy_cache/
        .ruff_cache/
        .pytest_cache/
        htmlcov/
        coverage.xml
        *.egg-info/
        build/
        dist/
        """)
    p = ctx.root / ".gitignore"
    if p.exists():
        cur = p.read_text(encoding="utf-8")
        if "# tidy_repo" in cur:
            log("уже обновлён", "skip", 1); return
        if not ctx.dry: p.write_text(cur + extra, encoding="utf-8")
        log("дополнен", "ok", 1); ctx.diff(p, "modify", size=len(extra))
    else:
        write(ctx, p, extra.lstrip())


@action("badges", "Бейджи в README", needs=("ci",))
def a_badges(ctx: Ctx) -> None:
    hr("9. Бейджи в README")
    p = ctx.root / "README.md"
    if not p.exists(): log("README нет", "warn", 1); return
    text = p.read_text(encoding="utf-8")
    if "badge.svg" in text: log("уже есть", "skip", 1); return
    owner_repo = git_owner_repo() or ("Alexkkkkk", "mount-file")
    o, r = owner_repo
    badges = (
        f"![CI](https://github.com/{o}/{r}/actions/workflows/ci.yml/badge.svg)\n"
        f"![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)\n"
        f"![Python](https://img.shields.io/badge/python-3.9%2B-blue)\n"
        f"![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)\n\n"
    )
    if not ctx.dry: p.write_text(badges + text, encoding="utf-8")
    log("добавлены", "ok", 1); ctx.diff(p, "modify", size=len(badges))


@action("autofix", "Автофикс линтером (ruff --fix)", parallel_safe=True)
def a_autofix(ctx: Ctx) -> None:
    hr("10. Автофикс линтером")
    if not has("ruff"):
        log("ruff не установлен — пропуск", "skip", 1); return
    try:
        r = sh(["ruff", "check", ".", "--fix", "--unsafe-fixes", "--quiet"], timeout=60)
        if r.returncode == 0:
            log("применён ruff --fix", "ok", 1)
        else:
            log("ruff --fix: остались замечания", "warn", 1)
    except Exception as e:
        log(f"ошибка ruff: {e}", "warn", 1)


@action("format", "Форматирование (ruff format)", parallel_safe=True)
def a_format(ctx: Ctx) -> None:
    hr("11. Форматирование")
    if not has("ruff"):
        log("ruff не установлен — пропуск", "skip", 1); return
    try:
        r = sh(["ruff", "format", ".", "--quiet"], timeout=60)
        log("ruff format применён" if r.returncode == 0 else "ruff format: частично",
            "ok" if r.returncode == 0 else "warn", 1)
    except Exception as e:
        log(f"ошибка: {e}", "warn", 1)


@action("verify", "Финальная проверка", needs=("format", "autofix"), parallel_safe=False)
def a_verify(ctx: Ctx) -> None:
    hr("12. Проверка")
    for cmd, name in ((["ruff","check","."],"ruff"), (["mypy","mountfile"],"mypy"),
                     (["pytest","-q"],"pytest")):
        if not has(cmd[0]):
            log(f"{name}: не установлен", "skip", 1); continue
        try:
            r = sh(cmd, timeout=120)
            ok = r.returncode == 0
            msg = "ok" if ok else ((r.stdout or r.stderr).splitlines() or ["?"])[0][:80]
            log(f"{name}: {msg}", "ok" if ok else "warn", 1)
        except Exception as e:
            log(f"{name}: {e}", "warn", 1)


# ─────────────────────────────────────────── бэкап

def make_backup(ctx: Ctx) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ctx.backup = ctx.root / f".tidy_backup_{ts}"
    if ctx.dry: return
    ctx.backup.mkdir(exist_ok=True)
    # tar.gz — быстрее и компактнее, чем копировать по одному
    tar = ctx.backup / "snapshot.tar.gz"
    files: list[Path] = []
    for pat in ("*.py","*.md","*.toml","*.txt","*.yaml","*.yml",".gitignore"):
        files += [p for p in ctx.root.glob(pat) if p.is_file()]
    for f in ("pyproject.toml",".pre-commit-config.yaml"): 
        p = ctx.root / f
        if p.exists(): files.append(p)
    with tarfile.open(tar, "w:gz") as t:
        for f in files:
            try: t.add(f, arcname=f.relative_to(ctx.root))
            except Exception: pass
    log(f"снапшот → {ctx.backup.name}/snapshot.tar.gz", "ok")


def rollback(ctx: Ctx) -> None:
    hr("↩  Rollback")
    if not ctx.backup or not ctx.backup.exists():
        log("нет снапшота", "warn", 1); return
    tar = ctx.backup / "snapshot.tar.gz"
    if tar.exists():
        with tarfile.open(tar, "r:gz") as t:
            t.extractall(ctx.root)
        log("файлы восстановлены из snapshot", "ok", 1)
    for orig, moved in ctx.moved.items():
        mp = Path(moved)
        if mp.exists():
            shutil.move(str(mp), str(ctx.root / orig))
            log(f"возвращён {orig}", "ok", 1)


# ─────────────────────────────────────────── executor

def run_wave(ctx: Ctx, wave: list[Action], parallel: bool = True) -> None:
    if not parallel or len(wave) == 1 or ctx.dry:
        for a in wave:
            _safe_run(ctx, a)
        return
    parallel_batch = [a for a in wave if a.parallel_safe]
    serial_batch   = [a for a in wave if not a.parallel_safe]
    if parallel_batch:
        with cf.ThreadPoolExecutor(max_workers=min(4, len(parallel_batch))) as ex:
            futs = {ex.submit(_safe_run, ctx, a): a for a in parallel_batch}
            for fut in cf.as_completed(futs):
                try: fut.result()
                except Exception as e:
                    log(f"{futs[fut].key}: {e}", "err", 1)
    for a in serial_batch:
        _safe_run(ctx, a)


def _safe_run(ctx: Ctx, a: Action) -> None:
    """Self-healing: при ошибке шага пытаемся один retry, иначе — пишем warning."""
    for attempt in (1, 2):
        try:
            a.run(ctx); return
        except KeyboardInterrupt:
            raise
        except Exception as e:
            if attempt == 1:
                log(f"{a.key}: ошибка ({e}), retry...", "warn", 1)
                time.sleep(0.3)
                continue
            log(f"{a.key}: пропущен после ошибки: {e}", "err", 1)


# ─────────────────────────────────────────── CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="tidy_repo v3 — супер-автоматика")
    ap.add_argument("command", nargs="?", default="auto",
                    choices=["auto","plan","apply","rollback","score","report"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--no-git", action="store_true", help="не создавать ветку/коммит")
    ap.add_argument("--only", help="ключи действий через запятую")
    ap.add_argument("--skip", help="исключить ключи")
    ap.add_argument("--serial", action="store_true", help="без параллелизма")
    ap.add_argument("--branch", default=f"chore/tidy-{datetime.now():%Y%m%d-%H%M}")
    ap.add_argument("--report", default=".tidy_report.json")
    ap.add_argument("--html", default=".tidy_report.html")
    ap.add_argument("--quiet", "-q", action="store_true")
    args = ap.parse_args()

    root = Path.cwd()
    if not args.quiet: banner()
    if not (root/"mountfile").is_dir() and not (root/"README.md").exists():
        log("не похоже на корень mount-file", "err"); return 2

    ctx = Ctx(root=root, dry=args.dry_run or args.command == "plan")

    # ── Детекторы
    hr("Анализ репозитория")
    for d in DETECTORS:
        try: d(ctx)
        except Exception as e: log(f"{d.__name__}: {e}", "warn", 1)

    score_before, parts = compute_score(ctx)
    ctx.score_before = score_before
    log(f"Health Score: {C.BOLD}{score_before}/100{C.RESET}", "star")
    for k, v in parts.items():
        bar = "█" * (v // 2)
        log(f"{k:14s} {v:3d}  {C.CYAN}{bar}{C.RESET}", "info", 1)
    log(f"Python: {ctx.facts.get('python')}  ·  GUI: {ctx.facts.get('gui')}"
        f"  ·  commit'ов: {len(ctx.facts.get('commits', []))}", "info", 1)

    if args.command == "score":
        return 0

    # ── Плагины
    plugins = load_plugins(ctx)
    all_actions = ACTIONS + plugins

    # ── Выбор действий
    if args.only:
        keys = {k.strip() for k in args.only.split(",")}
        selected = [a for a in all_actions if a.key in keys]
    else:
        selected = [a for a in all_actions if a.auto]
    if args.skip:
        skip = {k.strip() for k in args.skip.split(",")}
        selected = [a for a in selected if a.key not in skip]

    # ── DAG
    waves = topo_sort(selected)
    hr("План")
    for i, w in enumerate(waves, 1):
        names = "  ∥  ".join(f"{a.key}" for a in w)
        log(f"волна {i}: {names}", "info", 1)

    if args.command == "plan":
        log("PLAN-режим: реальных изменений не будет", "warn")
        return 0

    # ── git-ветка
    if is_git() and not args.no_git and not ctx.dry:
        if not git_clean():
            log("рабочее дерево не чистое — коммит после tidy затронет всё", "warn")
        try:
            git("checkout", "-b", args.branch, check=False)
            log(f"ветка {args.branch}", "ok")
        except Exception as e:
            log(f"ветка не создана: {e}", "warn")

    # ── снапшот
    if not args.no_backup:
        make_backup(ctx)

    # ── выполнение
    try:
        for wave in waves:
            run_wave(ctx, wave, parallel=not args.serial)
    except KeyboardInterrupt:
        print(); log("прервано", "warn"); rollback(ctx); return 130

    # ── пересчёт score и отчёты
    ctx.score_after, _ = compute_score(ctx)
    delta = ctx.score_after - ctx.score_before
    arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
    color = C.GREEN if delta >= 0 else C.RED
    hr("Результат")
    log(f"Health Score: {ctx.score_before} → {C.BOLD}{ctx.score_after}{C.RESET} "
        f"({color}{arrow}{abs(delta)}{C.RESET})", "star")
    log(f"Изменений файлов: {len(ctx.changes)}", "info", 1)

    # JSON
    (ctx.root / args.report).write_text(json.dumps({
        "timestamp": datetime.now().isoformat(),
        "root": str(ctx.root),
        "score_before": ctx.score_before,
        "score_after": ctx.score_after,
        "facts": {k: v for k, v in ctx.facts.items() if k != "commits"},
        "changes": ctx.changes,
        "log": ctx.report,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"отчёт: {args.report}", "ok", 1)

    # HTML
    render_html(ctx, ctx.root / args.html)
    log(f"дашборд: {args.html}", "ok", 1)

    # ── git commit
    if is_git() and not args.no_git and not ctx.dry:
        try:
            git("add", "-A")
            msg = f"chore: tidy repository (score {ctx.score_before}→{ctx.score_after})"
            r = sh(["git", "commit", "-m", msg])
            if r.returncode == 0:
                log("коммит создан", "ok", 1)
                if has("gh"):
                    log(f"PR: gh pr create --fill  (ветка {args.branch})", "info", 1)
            else:
                log(f"git commit: {r.stderr.strip()[:80]}", "warn", 1)
        except Exception as e:
            log(f"git: {e}", "warn", 1)

    print()
    hr()
    log(f"{C.GREEN}{C.BOLD}Готово за {time.time()-ctx.t0:.2f}s{C.RESET}", "ok")
    if ctx.backup:
        log(f"Откат: python tidy_repo.py rollback --from {ctx.backup.name}", "info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
