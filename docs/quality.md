# Quality and correctness triage

This document records the reproducible static-analysis baseline for issue #182. It
separates correctness-oriented findings from formatting and modernization debt.

## Correctness gate

The following rules are treated as a blocking gate for filesystem and recovery
code:

- `B006` — mutable default arguments;
- `E722` — bare `except`;
- `S110` — exception swallowed by `except ...: pass`;
- `S112` — exception swallowed before `continue`;
- `F841` — assigned value never used.

Run the gate locally with:

```bash
make lint-correctness
# or
python -m ruff check mountfile tests --select B006,E722,S110,S112,F841
```

The current `main` baseline contained **42 findings** in these rules: 31
`S110`, 4 `S112`, and 7 `F841`. They were triaged in the issue #182 follow-up:
optional cleanup and parser-rejection paths now emit debug diagnostics, real
unused assignments were removed or corrected, and intentional test rejection
paths are explicitly marked. The gate now reports **0 findings**.

`S110`/`S112` are also enabled in `pyproject.toml` and run as a required step in
the `Quality` GitHub Actions workflow. Debug-level logging is deliberate: normal
operation remains quiet, while forensic troubleshooting can enable the module
logger without silently discarding an exception.

## Full Ruff debt

The complete configured check is intentionally tracked separately. On the same
Python 3.12 environment, before this follow-up it reported **1,525 findings**.
The largest groups were `UP031` (754), `UP045` (331), `UP006` (115), `UP007`
(69), and `UP035` (51). These are modernization/import/simplification groups,
not equivalent to a filesystem write-safety failure. They should be reduced in
separate bounded PRs rather than hidden by a broad ignore or unsafe autofix.

Useful commands:

```bash
ruff check mountfile tests --statistics
ruff check mountfile tests --select B006,E722,S110,S112,F841
```

## Test and coverage reporting

The acceptance subset for the issue-182 filesystem work passes with the optional
checker skip documented by pytest:

```text
404 passed, 1 skipped (dosfstools unavailable)
```

Coverage must be reported with the optional runtime/tooling matrix present. A
local run without Qt, `pytsk3`, YARA, and filesystem checker binaries is not a
cross-platform proof; those skips remain visible rather than being counted as
successful feature coverage. The reproducible local run for this follow-up
used the available non-GUI test set and reported **1,677 passed, 36 skipped,
63% total coverage**. The skipped items are explicitly attributed by pytest to
missing optional tooling (Qt, `pytsk3`, YARA, PowerShell, or filesystem
checkers). The 80% project-wide goal therefore remains an open follow-up, not a
silently waived requirement.
