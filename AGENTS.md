# Agent instructions for MountFile

Repository-wide rules for AI coding agents (Copilot, Claude Code, Codex, Cursor)
and for the human contributors who review their output. `AGENTS.md` is the
single source of process truth; `.github/copilot-instructions.md` keeps the
*safety and architecture* rules and both must agree.

## 1. Fast loop before any PR

Run the narrowest thing that can fail, then widen:

```bash
make test-fast                 # parallel, no gui/e2e (pytest -n auto)
make test-changed              # only tests touched since origin/main
python -m pytest tests/test_x.py -q -x --lf   # while iterating on one module
make lint-fast                 # ruff: syntax + undefined names + correctness rules
make fmt-check                 # black + isort --check (CI parity)
make tracker-status            # regenerate the #510 slice block instead of editing it by hand
```

A change is not "verified" because it passes locally on Linux: the CI matrix
covers Windows and macOS, and integrations skip when `pytsk3`, `dosfstools`,
`mtools` or YARA are missing. Report skips as skips.

## 2. Quality gates that must stay green

`quality.yml` (lint → quality-gate) and `ci.yml` (test matrix → CI-Gate) are
protected. Do not weaken them to get a change through:

- Keep `ruff check --select E9,F63,F7,F82` and `--select B006,E722,S110,S112,F841`
  passing. **Never** add a new `--ignore` or `per-file-ignores` entry to silence a
  finding you introduced.
- `black --check` and `isort --check-only` are authoritative for formatting. If you
  reformat a file, reformat it in its own commit so the reviewer can skip it.
- `.pre-commit-config.yaml` intentionally runs a *narrow* ruff selection; widening it
  is a separate, reviewed change (see `docs/speedup-playbook.md`, item 3).

## 3. Protected check names

Branch protection expects these contexts to report on every PR. A workflow that
is skipped by `paths-ignore` never reports them and blocks the PR forever, so:

- do **not** add `paths-ignore` to `ci.yml` or `quality.yml`;
- do **not** rename `test (3.10)`, `test (3.11)`, `test (3.12)`,
  `Tests (ubuntu-latest, py3.12)`, `Errors`/`quality-gate`, or `CI-Gate`;
- if a leg must be dropped for cost, keep a single aggregating job that reports
  the same name and fails closed.

## 4. Dependencies and the lockfile

`uv.lock` is the resolved environment and CI installs with `uv sync --frozen`, so a
`pyproject.toml` dependency edit without a re-lock makes CI fail with a frozen-lock
error. Rules:

- never add, remove or bump a dependency by hand;
- relock with `uv lock` (and commit `uv.lock` in the same commit) or leave the
  dependency alone;
- `extra ci` / `extra dev` are the supported test stacks — do not invent a third one.

## 5. Evidence, not prose

Acceptance work for #385 / #386 / #387 / #510 is evidence-driven:

- `docs/replay-acceptance.json` is the manifest; an item marked `verified` or
  `partial` **must** cite a file that exists in the tree, and `not_started` must
  cite nothing. `python scripts/replay_acceptance_status.py --check` enforces this.
- generated reports under `docs/generated/` are produced by `scripts/*.py`; edit the
  generator, never the JSON.
- `docs/proposals/` is not runtime: nothing under `mountfile/` may import from `docs/`.
- A synthetic NTFS fixture, `ntfsfix -n`, or `probe_ntfs_log` alone is **not** an
  independent oracle. Do not describe them as one.
- Never write "supported", "production-ready", "parity" or "verified" in a doc,
  README or comment unless the tracker item that proves it is closed.

## 6. Tracker comments

The #510 thread is long and manual edits drifted from `main`. Generate the status
block instead of typing it:

```bash
make tracker-status                                  # Markdown block for the issue
python scripts/tracker_status.py --check             # exit 1 on manifest/tree drift
python scripts/tracker_status.py --markdown --similarity last-comment.md
```

`--similarity` exits 3 when the new block is more than 90% identical to the text
you pass, which is the "did I already post this?" gate the thread used to apply by
hand. Do not post a near-duplicate comment.

## 7. Parallel work

Use one worktree per task so switching branches never rebuilds the environment:

```bash
git worktree add ../mount-file-c6 -b feat/c6-oracle
```

## 8. Commits and PRs

- Conventional commits (`feat(#510): ...`, `fix: ...`, `test: ...`, `docs: ...`,
  `chore: ...`). `semantic-release` derives the version from them.
- One logical change per PR; the PR body states which tracker item it moves and
  which evidence file proves it.
- Do not commit tokens, image data, build artifacts (`build/`, `dist/`), or secrets.
  `tools/mfsync.py scan --all` runs in CI.
- Do not force-push `main`; it is protected — use a branch and a PR.
