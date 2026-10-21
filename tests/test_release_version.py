"""Regression tests for local version stamping and release planning tools."""

import json
import pathlib
import shutil
import subprocess
import sys


SOURCE_ROOT = pathlib.Path(__file__).resolve().parents[1]
STAMP_SCRIPT = SOURCE_ROOT / "scripts" / "stamp_version.py"
PLAN_SCRIPT = SOURCE_ROOT / "scripts" / "release_plan.py"


def _write_version_files(repository, version="1.0.0", package_version=None, project_version=None):
    package_version = package_version or version
    project_version = project_version or version
    package = repository / "mountfile"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text(
        '__version__ = "%s"\n' % package_version, encoding="utf-8"
    )
    (repository / "pyproject.toml").write_text(
        "[project]\nname = \"demo\"\nversion = \"%s\"\n" % project_version,
        encoding="utf-8",
    )


def _run_stamp(repository, *arguments):
    return subprocess.run(
        [sys.executable, str(repository / "scripts" / "stamp_version.py"), *arguments],
        cwd=str(repository),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def _stamp_repository(tmp_path, version="1.0.0"):
    repository = tmp_path / "stamp-repository"
    (repository / "scripts").mkdir(parents=True)
    shutil.copy2(str(STAMP_SCRIPT), str(repository / "scripts" / "stamp_version.py"))
    _write_version_files(repository, version)
    return repository


def test_stamp_is_idempotent_and_check_validates_both_targets(tmp_path):
    repository = _stamp_repository(tmp_path)

    first = _run_stamp(repository, "v1.2.3")
    assert first.returncode == 0, first.stderr
    package = repository / "mountfile" / "__init__.py"
    project = repository / "pyproject.toml"
    package_after_first = package.read_bytes()
    project_after_first = project.read_bytes()
    package_mtime = package.stat().st_mtime_ns
    project_mtime = project.stat().st_mtime_ns

    second = _run_stamp(repository, "1.2.3")
    assert second.returncode == 0, second.stderr
    assert "already stamped" in second.stdout
    assert package.read_bytes() == package_after_first
    assert project.read_bytes() == project_after_first
    assert package.stat().st_mtime_ns == package_mtime
    assert project.stat().st_mtime_ns == project_mtime

    checked = _run_stamp(repository, "--check", "v1.2.3")
    assert checked.returncode == 0, checked.stderr
    project.write_text(project.read_text(encoding="utf-8").replace("1.2.3", "1.2.2"), encoding="utf-8")
    mismatch = _run_stamp(repository, "--check", "v1.2.3")
    assert mismatch.returncode == 1
    assert "pyproject.toml" in mismatch.stderr


def test_stamp_rejects_malformed_version_without_touching_targets(tmp_path):
    repository = _stamp_repository(tmp_path)
    package = repository / "mountfile" / "__init__.py"
    project = repository / "pyproject.toml"
    before = (package.read_bytes(), project.read_bytes())

    result = _run_stamp(repository, '1.2.3"\nversion = "99.99.99')

    assert result.returncode == 2
    assert "malformed" in result.stderr
    assert (package.read_bytes(), project.read_bytes()) == before


def test_stamp_checks_every_target_before_first_write_when_field_is_missing(tmp_path):
    repository = _stamp_repository(tmp_path)
    package = repository / "mountfile" / "__init__.py"
    project = repository / "pyproject.toml"
    package.write_text("# __version__ intentionally absent\n", encoding="utf-8")
    before_project = project.read_bytes()

    result = _run_stamp(repository, "1.2.3")

    assert result.returncode == 1
    assert "__version__" in result.stderr
    assert project.read_bytes() == before_project


def test_stamp_does_not_write_package_when_project_version_is_missing(tmp_path):
    repository = _stamp_repository(tmp_path)
    package = repository / "mountfile" / "__init__.py"
    project = repository / "pyproject.toml"
    project.write_text("[project]\nname = \"demo\"\n", encoding="utf-8")
    before_package = package.read_bytes()

    result = _run_stamp(repository, "1.2.3")

    assert result.returncode == 1
    assert "version in [project]" in result.stderr
    assert package.read_bytes() == before_package


def test_stamp_changes_only_version_in_exact_project_table(tmp_path):
    repository = _stamp_repository(tmp_path)
    project = repository / "pyproject.toml"
    project.write_text(
        "[project]\n"
        "name = \"demo\"\n"
        "version = \"1.0.0\"\n"
        "[project.urls]\n"
        "version = \"https://example.invalid/version\"\n"
        "[tool.unrelated]\n"
        "version = \"keep-me\"\n",
        encoding="utf-8",
    )

    result = _run_stamp(repository, "v1.2.3")

    assert result.returncode == 0, result.stderr
    content = project.read_text(encoding="utf-8")
    assert 'version = "1.2.3"' in content
    assert 'version = "https://example.invalid/version"' in content
    assert 'version = "keep-me"' in content


def _git(repository, *arguments):
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )


def _release_repository(tmp_path, version="1.0.0"):
    repository = tmp_path / "release-repository"
    repository.mkdir(parents=True)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Release tests")
    _write_version_files(repository, version)
    (repository / "history.txt").write_text("base\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "chore: initial")
    _git(repository, "tag", "v" + version)
    return repository


def _commit(repository, subject, body=None):
    history = repository / "history.txt"
    history.write_text(history.read_text(encoding="utf-8") + subject + "\n", encoding="utf-8")
    _git(repository, "add", "history.txt")
    arguments = ["commit", "-qm", subject]
    if body is not None:
        arguments.extend(["-m", body])
    _git(repository, *arguments)


def _plan(repository, *arguments):
    result = subprocess.run(
        [sys.executable, str(PLAN_SCRIPT), "--json", "--cwd", str(repository), *arguments],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_release_plan_detects_major_from_breaking_subject(tmp_path):
    repository = _release_repository(tmp_path, "1.2.3")
    _commit(repository, "feat!: remove an incompatible API")

    plan = _plan(repository)

    assert plan["previous"] == "v1.2.3"
    assert plan["bump"] == "major"
    assert plan["version"] == "2.0.0"
    assert plan["tag"] == "v2.0.0"
    assert plan["has_release_change"] is True
    assert plan["needs_version_pr"] is True


def test_release_plan_detects_minor_even_with_later_release_chore(tmp_path):
    repository = _release_repository(tmp_path, "1.2.3")
    _commit(repository, "feat(cli): add local planning")
    _commit(repository, "chore(release): 1.3.0 [skip ci]")

    plan = _plan(repository)

    assert plan["bump"] == "minor"
    assert plan["version"] == "1.3.0"
    assert plan["tag"] == "v1.3.0"


def test_release_plan_detects_patch_and_can_force_patch(tmp_path):
    repository = _release_repository(tmp_path, "1.2.3")
    _commit(repository, "fix(parser): reject malformed tags")
    detected = _plan(repository)
    assert detected["bump"] == "patch"
    assert detected["version"] == "1.2.4"

    forced_repository = _release_repository(tmp_path / "forced", "1.2.3")
    _commit(forced_repository, "docs: clarify the release process")
    forced = _plan(forced_repository, "--patch")
    assert forced["bump"] == "patch"
    assert forced["version"] == "1.2.4"
    assert forced["has_release_change"] is True


def test_release_plan_tags_a_merged_manual_patch_pr_without_a_feat_or_fix(tmp_path):
    repository = _release_repository(tmp_path, "1.2.3")
    _write_version_files(repository, "1.2.4")
    _git(repository, "add", "mountfile/__init__.py", "pyproject.toml")
    _git(repository, "commit", "-qm", "chore(release): synchronize 1.2.4")

    plan = _plan(repository)

    assert plan["bump"] == "patch"
    assert plan["version"] == "1.2.4"
    assert plan["tag"] == "v1.2.4"
    assert plan["has_release_change"] is True
    assert plan["needs_version_pr"] is False


def test_pending_release_requires_version_parity_and_one_semantic_step(tmp_path):
    mismatched = _release_repository(tmp_path / "mismatched", "1.2.3")
    _write_version_files(
        mismatched, package_version="1.2.4", project_version="1.2.3"
    )
    _git(mismatched, "add", "mountfile/__init__.py", "pyproject.toml")
    _git(mismatched, "commit", "-qm", "chore(release): synchronize 1.2.4")
    assert _plan(mismatched)["has_release_change"] is False

    skipped = _release_repository(tmp_path / "skipped", "1.2.3")
    _write_version_files(skipped, "1.2.5")
    _git(skipped, "add", "mountfile/__init__.py", "pyproject.toml")
    _git(skipped, "commit", "-qm", "chore(release): synchronize 1.2.5")
    assert _plan(skipped)["has_release_change"] is False


def test_release_plan_has_no_release_for_irrelevant_or_ignored_commits(tmp_path):
    repository = _release_repository(tmp_path, "1.2.3")
    _commit(repository, "docs: improve examples")
    _commit(repository, "chore(sync): mirror metadata")
    _commit(repository, "chore: autofix formatting only", "BREAKING CHANGE: ignored automation")

    plan = _plan(repository)

    assert plan["bump"] == "none"
    assert plan["version"] is None
    assert plan["tag"] is None
    assert plan["has_release_change"] is False
    assert plan["needs_version_pr"] is False


def test_release_plan_ignores_non_ancestor_and_nonstable_tags(tmp_path):
    repository = _release_repository(tmp_path, "1.0.0")
    branch = _git(repository, "branch", "--show-current").stdout.strip()
    _git(repository, "checkout", "-qb", "side")
    _commit(repository, "feat: unreachable feature")
    _git(repository, "tag", "v9.0.0")
    _git(repository, "checkout", "-q", branch)
    _git(repository, "tag", "v8.0.0-rc.1")
    _commit(repository, "fix: reachable patch")

    plan = _plan(repository)

    assert plan["previous"] == "v1.0.0"
    assert plan["bump"] == "patch"
    assert plan["tag"] == "v1.0.1"


def test_release_plan_detects_breaking_change_footer(tmp_path):
    repository = _release_repository(tmp_path, "1.2.3")
    _commit(
        repository,
        "fix(api): change response shape",
        "BREAKING CHANGE: clients must use the replacement field.",
    )

    plan = _plan(repository)

    assert plan["bump"] == "major"
    assert plan["version"] == "2.0.0"
