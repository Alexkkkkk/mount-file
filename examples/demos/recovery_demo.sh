#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON:-python3}"
for tool in mkfs.vfat mcopy mdel; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "error: install dosfstools and mtools (missing $tool)" >&2
        exit 2
    }
done
"$PYTHON_BIN" -c 'import pytsk3, pydantic' >/dev/null 2>&1 || {
    echo 'error: install pytsk3 and pydantic in the active Python environment' >&2
    exit 2
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
IMAGE="$WORK/deleted-demo.img"
SOURCE="$WORK/source.txt"
OUTPUT="$WORK/recovered.txt"
mkdir -p "$WORK/workdir"
printf 'Mount File deleted-entry recovery demo\n' > "$SOURCE"

# Create a disposable MBR + FAT32 image, add a file, then mark its directory
# entry deleted. No user image is opened by this demo.
mkfs.vfat --invariant --mbr -C "$IMAGE" 65536 -F 32 -S 512 -n DEMO >/dev/null
mcopy -i "$IMAGE" "$SOURCE" ::/DELETED.TXT
mdel -i "$IMAGE" ::/DELETED.TXT

SCAN="$(MOUNTFILE_READONLY=1 \
    MOUNTFILE_WORKDIR="$WORK/workdir" \
    MOUNTFILE_AUDIT="$WORK/scan-audit.jsonl" \
    PYTHONPATH="$ROOT" "$PYTHON_BIN" examples/demos/recovery_scan.py "$IMAGE")"
read -r OFFSET INODE SIZE PATH_IN_IMAGE < <(
    printf '%s' "$SCAN" | "$PYTHON_BIN" -c \
      'import json,sys; x=json.load(sys.stdin); print(x["offset"],x["inode"],x["size"],x["path"])'
)
printf 'Read-only scan found deleted directory entry %s (offset=%s, inode=%s, size=%s bytes).\n' \
    "$PATH_IN_IMAGE" "$OFFSET" "$INODE" "$SIZE"

MOUNTFILE_WORKDIR="$WORK/workdir" \
MOUNTFILE_AUDIT="$WORK/recovery-audit.jsonl" \
PYTHONPATH="$ROOT" "$PYTHON_BIN" examples/demos/recovery_write.py \
    "$IMAGE" "$OFFSET" "$INODE" "$SIZE" "$OUTPUT"
cmp "$SOURCE" "$OUTPUT"
echo 'Verified: recovered bytes match the synthetic source; only the temporary image was used.'
