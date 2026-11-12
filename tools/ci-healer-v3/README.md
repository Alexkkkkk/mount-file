# CI Healer v3

This is an opt-in, fail-closed repair proposer for the Mount File repository. It listens to failed `CI` runs on `main` only when the repository variable `ENABLE_CI_HEALER_V3` is exactly `true`.

The agent only considers `missing_dependency` and `assertion_error` failures. Generated patches are restricted to `mountfile/`, `scripts/`, `tools/`, and `tests/`; `.github/`, Dockerfiles, packaging metadata, parent traversal, dangerous imports, and dangerous calls are rejected. A candidate is checked with `git apply`, tested in a disposable Docker workspace with `--network none` and a non-root user, and only a verified-green candidate may become a draft PR. No PR is approved or merged automatically.

## Enablement

1. Add repository variable `ENABLE_CI_HEALER_V3=true`.
2. Add `LLM_API_KEY` as an Actions secret. Optionally add `LLM_BASE_URL`, `LLM_MODEL`, and `OTEL_ENDPOINT`.
3. Ensure the runner can build and run Docker and that the repository token has the permissions required by the workflow.

The existing `self-heal.yml` workflow remains separate. Enable only one automated healer at a time during rollout to avoid duplicate proposals.
