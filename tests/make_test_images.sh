#!/usr/bin/env bash
# Build the test images used by tests/test_mfim.py.
# Everything happens in user space: no root, no losetup, no mounting.
#
#   mbr.img           MBR: FAT32 (partition 1, bootable) + ext4 (partition 2)
#   gpt.img           GPT: one FAT32 "EFI" partition
#   mbr_extended.img  MBR: FAT16 primary + extended container with 2 logical FAT16
#   raw_fat.img       no partition table at all, bare FAT16 at offset 0
#   raw_ext4.img      no partition table at all, bare ext4 at offset 0
#   squash.img        SquashFS filesystem
#
# The extended-partition image is laid out by hand (MBR + EBR chain written with
# struct.pack) because sfdisk's logical-partition auto-placement is version
# dependent and rejected our explicit layout. A hand-written EBR chain is still
# a spec-correct table, so it exercises the parser exactly like a real one.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="$HERE/images"
WORK="$OUT/.work"
mkdir -p "$OUT" "$WORK"
rm -f "$WORK"/*.part "$OUT"/*.img

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing tool: $1" >&2; exit 3; }; }
need dd; need sfdisk; need mkfs.vfat; need mkfs.ext4; need mcopy; need mmd; need debugfs

SECTOR=512

# --------------------------------------------------------------------------- #
# payload filesystems
# --------------------------------------------------------------------------- #
# FAT32, 64 MiB. It must be this big on purpose: FAT32 requires > 65525
# clusters, and a smaller volume makes both TSK and every other reader refuse
# the filesystem (a 32 MiB FAT32 volume is technically out of spec).
dd if=/dev/zero of="$WORK/fat32.part" bs=1M count=64 status=none
mkfs.vfat -F 32 -n MFIMFAT "$WORK/fat32.part" >/dev/null 2>&1

printf 'hello from FAT\n' > "$WORK/hello.txt"
printf 'second payload file for extraction test\n' > "$WORK/payload.bin"
head -c 300000 /dev/urandom > "$WORK/big.bin"

cat > "$WORK/mtools_fat32.rc" <<EOF
drive z: file="$WORK/fat32.part"
EOF
MTOOLSRC="$WORK/mtools_fat32.rc" mcopy -o "$WORK/hello.txt" z:/HELLO.TXT
MTOOLSRC="$WORK/mtools_fat32.rc" mmd z:/SUBDIR
MTOOLSRC="$WORK/mtools_fat32.rc" mcopy -o "$WORK/hello.txt" z:/SUBDIR/NESTED.TXT
MTOOLSRC="$WORK/mtools_fat32.rc" mcopy -o "$WORK/payload.bin" z:/PAYLOAD.BIN
MTOOLSRC="$WORK/mtools_fat32.rc" mcopy -o "$WORK/big.bin" z:/BIG.BIN

# FAT16, 16 MiB (used as the bare raw image)
dd if=/dev/zero of="$WORK/fat16.part" bs=1M count=16 status=none
mkfs.vfat -F 16 -n MFIMRAW16 "$WORK/fat16.part" >/dev/null 2>&1
cat > "$WORK/mtools_fat16.rc" <<EOF
drive y: file="$WORK/fat16.part"
EOF
MTOOLSRC="$WORK/mtools_fat16.rc" mcopy -o "$WORK/hello.txt" y:/RAWHELLO.TXT

# FAT16, 20 MiB (second logical volume)
dd if=/dev/zero of="$WORK/fat16b.part" bs=1M count=20 status=none
mkfs.vfat -F 16 -n MFIMLOG2 "$WORK/fat16b.part" >/dev/null 2>&1
cat > "$WORK/mtools_fat16b.rc" <<EOF
drive x: file="$WORK/fat16b.part"
EOF
MTOOLSRC="$WORK/mtools_fat16b.rc" mcopy -o "$WORK/hello.txt" x:/LOG2.TXT

# ext4, 40 MiB
dd if=/dev/zero of="$WORK/ext4.part" bs=1M count=40 status=none
mkfs.ext4 -q -F -L MFIMEXT4 "$WORK/ext4.part"
printf 'readme in ext4\n' > "$WORK/readme.txt"
printf 'root level file\n' > "$WORK/root.txt"
printf 'nested\n' > "$WORK/nested.txt"
debugfs -w -R "mkdir /docs" "$WORK/ext4.part" >/dev/null 2>&1
debugfs -w -R "mkdir /docs/deep" "$WORK/ext4.part" >/dev/null 2>&1
debugfs -w -R "mkdir /empty" "$WORK/ext4.part" >/dev/null 2>&1
debugfs -w -R "write $WORK/readme.txt /docs/readme.txt" "$WORK/ext4.part" >/dev/null 2>&1
debugfs -w -R "write $WORK/nested.txt /docs/deep/nested.txt" "$WORK/ext4.part" >/dev/null 2>&1
debugfs -w -R "write $WORK/root.txt /root.txt" "$WORK/ext4.part" >/dev/null 2>&1

# ext4, 32 MiB bare image
dd if=/dev/zero of="$WORK/ext4raw.part" bs=1M count=32 status=none
mkfs.ext4 -q -F -L MFIMEXTRAW "$WORK/ext4raw.part"
debugfs -w -R "write $WORK/readme.txt /readme.txt" "$WORK/ext4raw.part" >/dev/null 2>&1

# --------------------------------------------------------------------------- #
# image 1: MBR, FAT32 @2048 (131072 sectors = 64 MiB) + ext4 @133120 (81920 = 40 MiB)
# --------------------------------------------------------------------------- #
truncate -s 108M "$OUT/mbr.img"
sfdisk --quiet --label dos "$OUT/mbr.img" >/dev/null <<'EOF'
unit: sectors
start=2048, size=131072, type=0c, bootable
start=133120, size=81920, type=83
EOF
dd if="$WORK/fat32.part" of="$OUT/mbr.img" bs=$SECTOR seek=2048   conv=notrunc status=none
dd if="$WORK/ext4.part"  of="$OUT/mbr.img" bs=$SECTOR seek=133120 conv=notrunc status=none

# --------------------------------------------------------------------------- #
# image 2: GPT, FAT32 "EFI" @2048 (64 MiB, leaves room for the backup GPT)
# --------------------------------------------------------------------------- #
truncate -s 68M "$OUT/gpt.img"
sfdisk --quiet --label gpt "$OUT/gpt.img" >/dev/null <<'EOF'
unit: sectors
start=2048, size=131072, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name="EFI"
EOF
dd if="$WORK/fat32.part" of="$OUT/gpt.img" bs=$SECTOR seek=2048 conv=notrunc status=none

# --------------------------------------------------------------------------- #
# image 3: MBR with extended partition and two logical FAT16 volumes
#   primary   FAT16      2048 .. 34815   (32768 sectors)
#   extended             34816 .. 112639 (77824 sectors)
#   EBR1 @34816  logical1  rel 2048 -> abs 36864  (32768 sectors)
#   EBR2 @69632  logical2  rel 2048 -> abs 71680  (40960 sectors)
# --------------------------------------------------------------------------- #
truncate -s 56M "$OUT/mbr_extended.img"
python3 - "$OUT/mbr_extended.img" <<'PY'
import struct, sys

path = sys.argv[1]
CHS = b"\xfe\xff\xff"  # "use LBA", ignored by every modern parser


def entry(status, ptype, lba, count):
    return bytes([status, 0, 0, 0, ptype]) + CHS + struct.pack("<II", lba, count)


def table(entries):
    data = bytearray(512)
    for slot, rec in enumerate(entries):
        data[446 + slot * 16:446 + slot * 16 + 16] = rec
    data[510:512] = b"\x55\xaa"
    return bytes(data)


MBR = table([
    entry(0x00, 0x06, 2048, 32768),    # primary FAT16
    entry(0x00, 0x05, 34816, 77824),   # extended container
    b"\x00" * 16,
    b"\x00" * 16,
])
EBR1 = table([
    entry(0x00, 0x06, 2048, 32768),    # logical 1: abs 36864
    entry(0x00, 0x05, 34816, 43008),   # link to the next EBR: abs 69632
    b"\x00" * 16,
    b"\x00" * 16,
])
EBR2 = table([
    entry(0x00, 0x06, 2048, 40960),    # logical 2: abs 71680
    b"\x00" * 16,
    b"\x00" * 16,
    b"\x00" * 16,
])

with open(path, "r+b") as fp:
    for lba, data in ((0, MBR), (34816, EBR1), (69632, EBR2)):
        fp.seek(lba * 512)
        fp.write(data)
print("wrote MBR + EBR chain")
PY
dd if="$WORK/fat16.part"  of="$OUT/mbr_extended.img" bs=$SECTOR seek=36864 conv=notrunc status=none
dd if="$WORK/fat16b.part" of="$OUT/mbr_extended.img" bs=$SECTOR seek=71680 conv=notrunc status=none

# --------------------------------------------------------------------------- #
# image 4/5: bare filesystems, no partition table
# --------------------------------------------------------------------------- #
cp "$WORK/fat16.part"   "$OUT/raw_fat.img"
cp "$WORK/ext4raw.part" "$OUT/raw_ext4.img"

# --------------------------------------------------------------------------- #
# image 6: squashfs
# --------------------------------------------------------------------------- #
if command -v mksquashfs >/dev/null 2>&1; then
  rm -rf "$WORK/squashsrc"; mkdir -p "$WORK/squashsrc/docs"
  cp "$WORK/readme.txt" "$WORK/squashsrc/docs/readme.txt"
  cp "$WORK/root.txt"   "$WORK/squashsrc/root.txt"
  mksquashfs "$WORK/squashsrc" "$OUT/squash.img" -noappend -quiet >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------------------- #
# checksums so the test-suite can prove that parsing never modified an image
# --------------------------------------------------------------------------- #
( cd "$OUT" && sha256sum ./*.img > SHA256SUMS )
ls -l "$OUT"/*.img
echo "test images built in $OUT"
