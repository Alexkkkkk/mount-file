#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Plan the next stable release from local git history.

The script intentionally makes no network requests.  It considers only stable
``vX.Y.Z`` tags reachable from ``HEAD`` and reports JSON suitable for a release
workflow::

    python scripts/release_plan.py --json
    python scripts/release_plan.py --patch --cwd /path/to/repository

``--patch`` explicitly requests a patch release and therefore overrides a
conventional-commit-derived bump.  Output is JSON in both normal and
``--json`` modes so workflow callers never have to parse human prose.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

STABLE_TAG_RE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PACKAGE_VERSION_RE = re.compile(
    r"^\s*__version__\s*=\s*(['\"])(?P<value>[^'\"\r\n]*)\1\s*(?:#.*)?$"
)
PROJECT_HEADER_RE = re.compile(r"^\s*\[\s*project\s*\]\s*(?:#.*)?$")
TABLE_HEADER_RE = re.compile(r"^\s*\[\[?[^\]\r\n]+\]\]?\s*(?:#.*)?$")
PROJECT_VERSION_RE = re.compile(
    r"^\s*version\s*=\s*(['\"])(?P<value>[^'\"\r\n]*)\1\s*(?:#.*)?$"
)
CONVENTIONAL_SUBJECT_RE = re.compile(
    r"^(?P<kind>[A-Za-z][A-Za-z0-9-]*)(?:\([^\r\n)]*\))?(?P<breaking>!)?:\s+"
)
BREAKING_FOOTER_RE = re.compile(r"(?:^|\n)\s*BREAKING(?:[ -])CHANGE\s*:", re.IGNORECASE)
IGNORED_SUBJECT_RE = re.compile(
    r"^(?:chore\((?:release|sync|autofix)\)(?:!)?:|"
    r"autofix(?:\([^\r\n)]*\))?(?:!)?:|chore:\s*autofix\b)",
    re.IGNORECASE,
)
RELEASE_COMMIT_RE = re.compile(r"^chore\(release\)(?:!)?:", re.IGNORECASE)

BUMP_PRIORITY = {"none": 0, "patch": 1, "minor": 2, "major": 3}
VersionTuple = Tuple[int, int, int]


class PlanError(Exception):
    """Raised for local repository or version-file problems."""


def _run_git(repository: pathlib.Path, arguments: Sequence[str]) -> str:
    command = ["git", "-C", str(repository)] + list(arguments)
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise PlanError("could not execute git: %s" % exc)
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip()
            or completed.stdout.strip()
            or "unknown git failure"
        )
        raise PlanError("git %s failed: %s" % (" ".join(arguments), detail))
    return completed.stdout


def _parse_stable_tag(tag: str) -> Optional[VersionTuple]:
    match = STABLE_TAG_RE.fullmatch(tag)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _previous_tag(
    repository: pathlib.Path,
) -> Tuple[Optional[str], Optional[VersionTuple]]:
    """Return the greatest stable SemVer tag that is an ancestor of HEAD."""
    tags = _run_git(repository, ["tag", "--merged", "HEAD"]).splitlines()
    candidates = []
    for tag in tags:
        version = _parse_stable_tag(tag.strip())
        if version is not None:
            candidates.append((version, tag.strip()))
    if not candidates:
        return None, None
    version, tag = max(candidates, key=lambda item: item[0])
    return tag, version


def _commits_since(
    repository: pathlib.Path, previous_tag: Optional[str]
) -> Iterable[Tuple[str, str]]:
    revision_range = "%s..HEAD" % previous_tag if previous_tag else "HEAD"
    # NUL/record-separator delimiters cannot occur in Git commit subject/body
    # text, unlike ordinary newlines used by BREAKING CHANGE footers.
    output = _run_git(repository, ["log", "--format=%s%x1f%b%x1e", revision_range])
    for record in output.split("\x1e"):
        # git appends a newline after each pretty-format record.  Once split
        # on the explicit record separator, that newline is not a commit.
        if not record.strip():
            continue
        record = record.lstrip("\n")
        try:
            subject, body = record.split("\x1f", 1)
        except ValueError:
            raise PlanError("could not parse local git log output")
        yield subject.rstrip("\n"), body.rstrip("\n")


def _commit_bump(subject: str, body: str) -> str:
    """Classify one conventional commit, returning ``none`` when ignored."""
    if IGNORED_SUBJECT_RE.match(subject):
        return "none"
    match = CONVENTIONAL_SUBJECT_RE.match(subject)
    if match is None:
        return "none"
    if match.group("breaking") or BREAKING_FOOTER_RE.search(body):
        return "major"
    kind = match.group("kind").lower()
    if kind == "feat":
        return "minor"
    if kind in ("fix", "perf"):
        return "patch"
    return "none"


def _detected_bump(repository: pathlib.Path, previous_tag: Optional[str]) -> str:
    bump = "none"
    for subject, body in _commits_since(repository, previous_tag):
        candidate = _commit_bump(subject, body)
        if BUMP_PRIORITY[candidate] > BUMP_PRIORITY[bump]:
            bump = candidate
    return bump


def _pending_release_bump(
    repository: pathlib.Path,
    previous_tag: Optional[str],
    previous_version: Optional[VersionTuple],
    package_version: str,
    project_version: str,
) -> str:
    """Recognise a merged, already-stamped release PR that still needs a tag.

    A manually requested ``--patch`` first creates a
    ``chore(release): synchronize ...`` PR.  Its successful merge contains no
    feat/fix of its own, so conventional-commit analysis alone must not make
    the planned patch disappear.  Do not infer a release merely from a newer
    file: it must have exact package/project parity, be exactly one valid
    semantic increment from the last reachable tag, and include the release
    commit in the range being planned.
    """
    if previous_tag is None or previous_version is None:
        return "none"
    if package_version != project_version:
        return "none"
    stamped_version = _parse_version(package_version)
    if stamped_version is None:
        return "none"

    pending_bump = "none"
    for candidate in ("patch", "minor", "major"):
        if _increment(previous_version, candidate) == stamped_version:
            pending_bump = candidate
            break
    if pending_bump == "none":
        return "none"

    for subject, _body in _commits_since(repository, previous_tag):
        if RELEASE_COMMIT_RE.match(subject):
            return pending_bump
    return "none"


def _version_from_package(path: pathlib.Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PlanError("could not read %s: %s" % (path, exc))
    matches = [PACKAGE_VERSION_RE.fullmatch(line) for line in lines]
    found = [match.group("value") for match in matches if match is not None]
    if len(found) != 1:
        raise PlanError(
            "expected exactly one __version__ assignment in %s, found %d"
            % (path, len(found))
        )
    if _parse_version(found[0]) is None:
        raise PlanError("package version is not stable SemVer: %s" % found[0])
    return found[0]


def _version_from_project(path: pathlib.Path) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PlanError("could not read %s: %s" % (path, exc))
    headers = [
        index for index, line in enumerate(lines) if PROJECT_HEADER_RE.fullmatch(line)
    ]
    if len(headers) != 1:
        raise PlanError(
            "expected exactly one [project] table in %s, found %d"
            % (path, len(headers))
        )

    start = headers[0] + 1
    end = len(lines)
    for index in range(start, len(lines)):
        if TABLE_HEADER_RE.fullmatch(lines[index]):
            end = index
            break
    matches = [PROJECT_VERSION_RE.fullmatch(line) for line in lines[start:end]]
    found = [match.group("value") for match in matches if match is not None]
    if len(found) != 1:
        raise PlanError(
            "expected exactly one version in [project] in %s, found %d"
            % (path, len(found))
        )
    if _parse_version(found[0]) is None:
        raise PlanError("project version is not stable SemVer: %s" % found[0])
    return found[0]


def _parse_version(value: str) -> Optional[VersionTuple]:
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _increment(version: VersionTuple, bump: str) -> VersionTuple:
    major, minor, patch = version
    if bump == "major":
        return major + 1, 0, 0
    if bump == "minor":
        return major, minor + 1, 0
    if bump == "patch":
        return major, minor, patch + 1
    raise PlanError("cannot increment an unknown bump: %s" % bump)


def make_plan(repository: pathlib.Path, force_patch: bool = False) -> Dict[str, object]:
    """Build a JSON-serialisable release plan for a local Git repository."""
    repository = repository.resolve()
    if not repository.is_dir():
        raise PlanError("repository path is not a directory: %s" % repository)

    package_version = _version_from_package(repository / "mountfile" / "__init__.py")
    project_version = _version_from_project(repository / "pyproject.toml")
    previous_tag, previous_version = _previous_tag(repository)
    detected = _detected_bump(repository, previous_tag)
    pending = _pending_release_bump(
        repository, previous_tag, previous_version, package_version, project_version
    )
    if force_patch:
        bump = "patch"
    elif detected != "none":
        bump = detected
    else:
        bump = pending
    has_release_change = bump != "none"

    version = None
    tag = None
    if has_release_change:
        base = previous_version if previous_version is not None else (0, 0, 0)
        next_version = _increment(base, bump)
        version = "%d.%d.%d" % next_version
        tag = "v" + version

    needs_version_pr = bool(
        has_release_change
        and (package_version != version or project_version != version)
    )
    return {
        "previous": previous_tag,
        "tag": tag,
        "version": version,
        "bump": bump,
        "needs_version_pr": needs_version_pr,
        "has_release_change": has_release_change,
        "versions": {"package": package_version, "project": project_version},
    }


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan the next stable release from local Git history (no network)."
    )
    parser.add_argument(
        "--patch",
        action="store_true",
        help="force a patch release, overriding the detected conventional-commit bump",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="explicitly request JSON (JSON is also the default output format)",
    )
    parser.add_argument(
        "--cwd",
        default=".",
        metavar="PATH",
        help="repository directory to inspect (default: current directory)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _argument_parser()
    args = parser.parse_args(None if argv is None else list(argv)[1:])
    try:
        plan = make_plan(pathlib.Path(args.cwd), force_patch=args.patch)
    except PlanError as exc:
        print("release_plan.py: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
