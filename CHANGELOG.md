# Changelog

Release notes are generated from conventional commits by the
`create-release` workflow and attached to every GitHub Release, so this
file only tracks the releases themselves.

## Unreleased

- Fixed the startup crash on PyQt5 builds ("Unhandled exception in
  script" / `ImportError: cannot import name 'QAbstractScrollArea'`):
  the PyQt5 branch of `mountfile/qt_compat.py` exported only a handful
  of widgets, so `mountfile/hexview.py` could not import its names.
- `main.py`: `sys.stdin` is `None` in a windowed build, so the trailing
  `isatty` AttributeError replaced the real traceback with a useless
  dialog; the pause is now guarded and `--diag`/`--version`/
  `--check-updates` were added.
- Auto-update from GitHub Releases on every start (`mountfile/updater.py`).

- Full automation: version-bump, create-release, release, autofix,
  auto-merge, nightly, chatops, docs, dependency-review.

## v1.0.0

- Windows build (PyInstaller, one-file) attached to the GitHub Release.

## v0.2.0

- Release pipeline, Windows EXE attached to the release, test suite.
