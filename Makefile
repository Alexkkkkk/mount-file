# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Convenience targets for local development and for reproducing CI locally.

PY ?= python3

.PHONY: help install tools tools-check test test-ai test-standalone lint lint-correctness fmt exe mountfile-exe clean all-in-one sample smoke-images \
        sync sync-status sync-check sync-pr sync-watch test-fast

help:
	@echo "make install    - install runtime dependencies"
	@echo "make tools      - install the complete Linux checker and Python toolchain"
	@echo "make tools-check - verify all optional tools are available"
	@echo "make test       - run the test suite"
	@echo "make test-ai    - run deterministic AI safety and contract tests"
	@echo "make test-standalone - run the standalone mountfile.py tests"
	@echo "make lint       - ruff static check (E9,F63,F7,F82)"
	@echo "make lint-correctness - repository-wide correctness lint (B006,E722,S110,S112,F841)"
	@echo "make fmt        - apply ruff --fix + black + isort"
	@echo "make exe        - build MountFile.exe with PyInstaller"
	@echo "make mountfile-exe - build the standalone mountfile.py binary"
	@echo "make clean      - remove build artifacts"
	@echo "make all-in-one - run every feature: CLI + API + tests (no GUI)"
	@echo "make sample     - build a FAT32 dump.img and an RT809H-style sample"
	@echo "make smoke-images - end-to-end CLI+GUI checks on freshly built images"
	@echo "make sync       - pull --rebase + commit + push (tools/mfsync.py)"
	@echo "make sync-status - branch, ahead/behind, dirty files, version parity"
	@echo "make sync-check - fail on version mismatch between pyproject/__init__"
	@echo "make sync-pr    - sync through a pull request (protected main)"
	@echo "make sync-watch - auto-commit + push on every save"

install:
	$(PY) -m pip install -e ".[api,ai,dfir]"

tools:
	bash scripts/bootstrap-tools.sh

tools-check:
	$(PY) scripts/bootstrap-tools.py

test:
	$(PY) -m pytest tests/ -v

# Быстрый локальный гейт: параллельно по ядрам, без GUI/e2e,
# та же конфигурация, что в PR-прогоне CI.
test-fast:
	$(PY) -m pytest tests/ -q -n auto --dist=worksteal -m "not gui and not e2e"

test-ai:
	$(PY) -m pytest \
		tests/test_ai_agents.py tests/test_ai_backend.py \
		tests/test_ai_guard.py \
		tests/test_ai_legacy_agent.py tests/test_ai_redteam.py \
		tests/test_ai_schema.py \
		tests/test_ai_structured_backend.py tests/test_mcp_server.py -v

# Tests for the standalone single-file build (mountfile.py). Neither
# pytsk3 nor a display is required, so these run anywhere, including CI.
test-standalone:
	$(PY) -m pytest tests/test_mountfile_standalone.py -v

lint:
	$(PY) -m ruff check . --select E9,F63,F7,F82

lint-correctness:
	$(PY) -m ruff check . --select B006,E722,S110,S112,F841

fmt:
	$(PY) -m ruff check --fix mountfile tests main.py || true
	$(PY) -m isort --quiet mountfile tests main.py
	$(PY) -m black --quiet mountfile tests main.py

exe:
	$(PY) -m PyInstaller --noconfirm --clean --onefile --windowed \
		--specpath build/spec --hidden-import mountfile_dfir_tools \
		--icon mountfile/data/icon.ico --collect-data mountfile --name MountFile main.py

# Standalone single-file build: GUI + CLI in one binary, no package needed.
mountfile-exe:
	$(PY) -m PyInstaller --noconfirm --clean --onefile --windowed \
		--specpath build/spec --icon mountfile/data/icon.png --name mountfile mountfile.py
	@test -f dist/mountfile -o -f dist/mountfile.exe && echo "Done: dist/mountfile[.exe]"

clean:
	rm -rf build dist

# One-shot exercise of everything the project can do (mountfile_all.py):
# CLI commands, the Python API demo and the test suite. The GUI is skipped so
# the target also works over SSH and in CI (no display needed).
# Extra flags:  make all-in-one ARGS="--build"
all-in-one:
	$(PY) mountfile_all.py --no-gui $(ARGS)

# Build the sample images the launcher uses by default (dump.img / dump.bin /
# udev.txt) with the system dosfstools + mtools, then nothing is skipped in
# `make all-in-one`.
sample:
	bash scripts/make_sample_image.sh

# End-to-end check on real, freshly built images: mkfs builds them, mtools and
# fsck/e2fsck verify every write made by Mount File. Sections whose system tool
# is missing are skipped. Work dir:  make smoke-images WORK=/tmp/mf-smoke
smoke-images:
	bash scripts/smoke-real-images.sh $(WORK)

# --------------------------------------------------------------- sync targets
# Requires only git + Python 3.8 (stdlib). Token is read from GH_TOKEN at call
# time and never stored. See docs/sync.md.

sync:
	$(PY) tools/mfsync.py sync

sync-status:
	$(PY) tools/mfsync.py status

sync-check:
	$(PY) tools/mfsync.py check --strict

sync-pr:
	$(PY) tools/mfsync.py sync --pr

sync-watch:
	$(PY) tools/mfsync.py watch --push
