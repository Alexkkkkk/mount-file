# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Convenience targets for local development and for reproducing CI locally.

PY ?= python3

.PHONY: help install test lint fmt exe clean

help:
	@echo "make install  - install runtime dependencies"
	@echo "make test     - run the test suite"
	@echo "make lint     - ruff static check (E9,F63,F7,F82)"
	@echo "make fmt      - apply ruff --fix + black + isort"
	@echo "make exe      - build MountFile.exe with PyInstaller"
	@echo "make clean    - remove build artifacts"

install:
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest tests/ -v

lint:
	$(PY) -m ruff check . --select E9,F63,F7,F82

fmt:
	$(PY) -m ruff check --fix mountfile tests main.py || true
	$(PY) -m black --quiet mountfile tests main.py
	$(PY) -m isort --quiet mountfile tests main.py

exe:
	$(PY) -m PyInstaller --noconfirm --clean --onefile --windowed --name MountFile main.py

clean:
	rm -rf build dist *.spec
