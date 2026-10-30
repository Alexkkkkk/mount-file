# Copilot instructions for MountFile

MountFile is a Python 3.10+ desktop and CLI tool for examining raw disk and flash images. The GUI uses PyQt6; the package and CLI live under `mountfile/`. Preserve the project's forensic-safety guarantees and keep changes narrowly scoped.

## Safety and architecture

- Treat image offsets and lengths as untrusted. Validate every extent against the selected volume, not merely the image file; use checked arithmetic and fail closed on malformed, dirty, unsupported, or ambiguous filesystems.
- Do not let a parser or writer access bytes outside the selected volume. In particular, `pytsk3.Img_Info` opens the whole image: do not use it for a partition-sized extent unless a bounded adapter enforces every read. Keep legacy fallback explicit and optional.
- Native filesystem writes are intentionally limited to validated profiles and operations. Do not advertise or perform unsupported writes; keep capability/status APIs and tests in sync with behavior.
- Preserve transaction, read-back verification, and rollback guarantees in writers. Do not add network access, subprocesses, or host mounting to native filesystem parsing/writing paths.
- Prefer the existing VFS and format-specific backends over duplicate parsers. Consult nearby tests before changing public behavior.

## Validation

Run the narrowest relevant tests first, then the full suite when practical:

```bash
python3 -m pytest tests/ -q
make lint
python3 -m black --check .
python3 -m isort --check-only .
python3 -m compileall -q mountfile main.py tests
python3 -m mkdocs build --strict --clean
```

The full suite may skip integrations when optional tools such as `pytsk3`, `dosfstools`/`mtools`, or YARA are unavailable. Report those skips accurately; a local Linux run does not prove Windows or macOS CI results.

## GitHub workflows and dependencies

- Keep Actions permissions minimal and third-party Actions pinned to full commit SHAs, matching the existing workflows.
- Update tests and user-facing API/docs when changing filesystem support, bounds, capabilities, or write semantics.
- Do not add credentials, tokens, image data, or generated build artifacts to commits.
