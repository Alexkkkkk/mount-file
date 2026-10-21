"""Offline regression tests for the safety contracts of repository automation.

The actual GitHub Actions schema is validated separately with actionlint.
These assertions protect the security conditions from accidental removal.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")


def test_autosync_requires_successful_verification_and_manual_request():
    text = workflow("mfsync.yml").split("  autosync:", 1)[1]
    assert "needs.verify.result == 'success'" in text
    assert "github.event_name == 'workflow_dispatch'" in text
    assert "always()" not in text.split("runs-on:", 1)[0]
    assert "repository-maintenance-write" in text


def test_manual_sync_inputs_are_environment_data_not_shell_source():
    text = workflow("mfsync.yml")
    assert "MESSAGE: ${{ inputs.message }}" in text
    assert "MODE: ${{ inputs.mode }}" in text
    assert 'MESSAGE="${{ ' not in text
    assert 'MODE="${{ ' not in text
    assert "[skip ci]" not in text


def test_release_calculation_is_gated_on_trusted_main_ci():
    text = workflow("version-bump.yml")
    assert "workflow_run:" in text
    assert "workflow_run.conclusion == 'success'" in text
    assert "workflow_run.event == 'push'" in text
    assert "workflow_run.head_branch == 'main'" in text
    assert "workflow_run.head_repository.full_name == github.repository" in text
    assert "ref: main" in text
    assert "ref: ${{ github.event.workflow_run.head_sha }}" not in text
    assert "CI-Gate" in text
    assert '"$SHA" != "$TESTED_SHA"' in text
    assert 'scripts/stamp_version.py "$TAG" --check' in text
    assert "git push --force" not in text


def test_ci_gate_accounts_for_every_build_and_test_stage():
    text = workflow("ci.yml").split("  ci-gate:", 1)[1]
    assert "name: CI-Gate" in text
    assert "if: always()" in text
    for job in ("tests", "integration", "build", "gui-import", "gui"):
        assert "needs.%s.result" % job in text
    assert "exit 1" in text


def test_self_healing_never_checks_out_fork_pr_code_with_secrets():
    text = workflow("self-heal.yml")
    assert "vars.ENABLE_AI_AUTOMATION == 'true'" in text
    assert "workflow_run.event == 'push'" in text
    assert "workflow_run.head_branch == 'main'" in text
    assert "workflow_run.head_repository.full_name == github.repository" in text
    assert "ref: main" in text


def test_auto_merge_requires_trust_label_head_match_and_all_gates():
    text = workflow("auto-merge.yml")
    assert "head.repo.full_name == github.repository" in text
    assert "labels.*.name, 'auto-merge'" in text
    assert "author_association" in text
    assert '--match-head-commit "$EXPECTED_SHA"' in text
    assert "--admin" not in text
    assert "gh pr review --approve" not in text
    for name in (
        "CI-Gate",
        "quality-gate",
        "dependency-review",
        "Version parity + secret scan",
        "Verify (status, secrets, versions, tests)",
    ):
        assert name in text


def test_release_checks_both_platform_version_stamping_and_failed_upload():
    text = workflow("release.yml")
    assert "python scripts/stamp_version.py $env:TAG" in text
    assert 'python scripts/stamp_version.py "$TAG"' in text
    assert 'gh release view "$TAG" --repo "$GITHUB_REPOSITORY"' in text
    assert "UPLOADED=false" in text
    assert 'if [ "$UPLOADED" != true ]' in text
    assert '"$asset.sha256"' in text


def test_new_privileged_workflows_pin_action_revisions():
    for name in ("mfsync.yml", "version-bump.yml", "auto-merge.yml"):
        uses = re.findall(r"uses:\s+([\w.-]+/[\w.-]+)@([^\s]+)", workflow(name))
        assert uses
        assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for _, revision in uses)


def test_autofix_does_not_remove_reexports_or_publish_untested_changes():
    text = workflow("autofix.yml")
    assert "ruff check --fix" not in text
    assert "--profile black --line-length 100" in text
    assert "--target-version py38" in text
    assert "python -m pytest tests/ -q" in text
    assert text.index("python -m pytest tests/ -q") < text.index("gh pr create")
    assert "--label auto-merge" in text


def test_chatops_reuses_ci_gated_release_pipeline():
    text = workflow("chatops.yml")
    assert "comment.author_association" in text
    assert "BODY: ${{ github.event.comment.body }}" in text
    assert "gh workflow run version-bump.yml" in text
    assert "gh workflow run autofix.yml" in text
    assert "gh workflow run mfsync.yml" in text
    assert "git tag " not in text
    assert "x-access-token:" not in text


def test_merged_release_pr_dispatches_full_main_ci_before_tagging():
    text = workflow("version-bump.yml")
    assert "types: [closed]" in text
    assert "github.event.pull_request.merged == true" in text
    assert "head.ref, 'automation/release-'" in text
    assert "gh workflow run ci.yml" in text
    assert "workflow_run.event == 'workflow_dispatch'" in text
    assert "--label auto-merge" in text


def test_workflow_quality_validation_is_integrity_checked():
    text = workflow("quality.yml")
    assert "actionlint" in text
    assert "sha256sum --check" in text
    assert "collected no tests" not in text
