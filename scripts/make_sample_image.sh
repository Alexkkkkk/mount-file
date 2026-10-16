#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
#
# Build the sample images mountfile_all.py uses by default, so that a plain
# `python mountfile_all.py` exercises the CLI and API sections instead of
# skipping every one of them:
#
#   dump.img   FAT32 volume image (built by mkfs.vfat + mcopy)
#   dump.bin   RT809H-style MST dump with the "MBOT" marker and the "JC"
#              encode flag, long enough to cover the decoder's head region
#   udev.txt   [PARTITIONS] table for that dump
#
# Requirements: dosfstools + mtools  (sudo apt-get install dosfstools mtools)
#
# Usage:
#   bash scripts/make_sample_image.sh            # writes into the repo root
#   OUT_DIR=/tmp/sample bash scripts/make_sample_image.sh
set -euo pipefail

cd "$(dirname "$0")/.."
OUT_DIR="${OUT_DIR:-.}"
mkdir -p "$OUT_DIR"

IMG="$OUT_DIR/dump.img"
DUMP="$OUT_DIR/dump.bin"
UDEV="$OUT_DIR/udev.txt"

# ------------------------------------------------------------ FAT32 image
# 32 MiB volume, 512-byte sectors (mkfs.vfat -C creates the file itself).
for tool in mkfs.vfat mcopy; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "error: $tool not found - install dosfstools + mtools" >&2
        exit 1
    fi
done

rm -f "$IMG"
mkfs.vfat -C "$IMG" 65536 -F 32 -S 512 -n MOUNTFILE >/dev/null

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

printf 'Hello from Mount File!\n' > "$TMP/HELLO.TXT"
printf 'readme for the sample image\n' > "$TMP/README.TXT"
printf 'nested payload\n' > "$TMP/NESTED.TXT"

# a directory with a file inside, so `get /photos` has something to extract
mcopy -i "$IMG" "$TMP/HELLO.TXT" "$TMP/README.TXT" "::/"
mmd -i "$IMG" "::/photos" >/dev/null
mcopy -i "$IMG" "$TMP/NESTED.TXT" "::/photos/"

echo "FAT32 sample written: $IMG"
mtype -i "$IMG" "::/" | sed 's/^/  /'

# ------------------------------------------------- RT809H-style MST dump
# detect() reads 0x200090..0x200098: "JC" + 2 pad bytes + "MBOT".
# The file must be at least MST_REGION_END (3 * 0x200000) bytes long.
SIZE=$((8 * 1024 * 1024))
rm -f "$DUMP"
head -c "$SIZE" /dev/zero > "$DUMP"
# a recognisable payload so the decoded output is not all zeroes
printf 'MOUNTFILE RT809H SAMPLE DUMP\n' | dd of="$DUMP" bs=1 seek=0 conv=notrunc status=none
printf 'SYSTEM PARTITION'.           | dd of="$DUMP" bs=1 seek=$((0x200000)) conv=notrunc status=none
printf 'JC\x00\x00MBOT'             | dd of="$DUMP" bs=1 seek=$((0x200090)) conv=notrunc status=none

echo "RT809H sample written: $DUMP ($SIZE bytes)"

# ------------------------------------------------------------- udev table
cat > "$UDEV" <<'EOF'
[PARTITIONS]
0x00000000,0x00200000,BOOT
0x00200000,0x00600000,SYSTEM
EOF
echo "udev table written: $UDEV"
