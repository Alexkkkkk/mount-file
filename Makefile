# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Convenience targets for local development and for reproducing CI locally.

PY ?= python3

.PHONY: help install test test-standalone lint fmt exe mountfile-exe clean all-in-one sample

help:
	@echo "make install    - install runtime dependencies"
	@echo "make test       - run the test suite"
	@echo "make test-standalone - run the standalone mountfile.py tests"
	@echo "make lint       - ruff static check (E9,F63,F7,F82)"
	@echo "make fmt        - apply ruff --fix + black + isort"
	@echo "make exe        - build MountFile.exe with PyInstaller"
	@echo "make mountfile-exe - build the standalone mountfile.py binary"
	@echo "make clean      - remove build artifacts"
	@echo "make all-in-one - run every feature: CLI + API + tests (no GUI)"
	@echo "make sample     - build a FAT32 dump.img and an RT809H-style sample"

install:
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest tests/ -v

# Tests for the standalone single-file build (mountfile.py). Neither
# pytsk3 nor a display is required, so these run anywhere, including CI.
test-standalone:
	$(PY) -m pytest tests/test_mountfile_standalone.py -v

lint:
	$(PY) -m ruff check . --select E9,F63,F7,F82

fmt:
	$(PY) -m ruff check --fix mountfile tests main.py || true
	$(PY) -m black --quiet mountfile tests main.py
	$(PY) -m isort --quiet mountfile tests main.py

exe:
	$(PY) -m PyInstaller --noconfirm --clean --onefile --windowed \
		--icon mountfile/data/icon.ico --name MountFile main.py

# Standalone single-file build: GUI + CLI in one binary, no package needed.
mountfile-exe:
	$(PY) -m PyInstaller --noconfirm --clean --onefile --windowed \
		--onefile --icon mountfile/data/icon.png --name mountfile mountfile.py
	@test -f dist/mountfile && echo "Done: dist/mountfile"

clean:
	rm -rf build dist *.spec

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
