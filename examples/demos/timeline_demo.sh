#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

printf '%s\n' 'Synthetic timeline CSV -> SQLite (no source image is read or changed).'
python3 mountfile_dfir.py timeline examples/demos/timeline.csv --db "$WORK/timeline.db"
python3 mountfile_dfir.py timeline examples/demos/timeline.csv --db "$WORK/timeline.db" --query --limit 10
