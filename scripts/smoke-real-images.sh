#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Alexkkkkk
# Licensed under the MIT License - see the LICENSE file for details.
#
# End-to-end smoke test of the Mount File CLI on freshly built, real images.
# Every check calls the real entry point (`python -m mountfile ...`) and, where
# an external checker exists, verifies the result with mkfs/fsck/mtools instead
# of trusting the writer that produced it.
#
# Usage:
#     scripts/smoke-real-images.sh [WORK_DIR]
# Environment overrides:
#     PYTHON_BIN   interpreter used for every MountFile call
#                  (default: <repo>/.venv/bin/python, then python3)
#
# Requires (sections are skipped when missing):
#     dosfstools + mtools   FAT32 read/write + fsck.vfat
#     exfatprogs            exFAT image + fsck.exfat
#     e2fsprogs             ext2/ext4 images, debugfs writer, e2fsck
#     ntfs-3g               mkfs.ntfs for the read-only NTFS profile
#     pytsk3                filesystem-aware DFIR commands
#     PyQt6                 offscreen GUI check
#
# Exit code 0 means every executed check passed; 1 lists the failed checks.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
PY="${PYTHON_BIN:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi
WORK="${1:-$(mktemp -d "${TMPDIR:-/tmp}/mountfile-smoke.XXXXXX")}"
mkdir -p "$WORK"
echo "interpreter: $PY"
echo "work dir:    $WORK"

PASS=0
FAIL=0
SKIP=0
FAILED_NAMES=()
SKIPPED_NAMES=()

ok() { PASS=$((PASS + 1)); printf '  PASS  %s\n' "$1"; }
bad() {
  FAIL=$((FAIL + 1))
  FAILED_NAMES+=("$1")
  printf '  FAIL  %s\n     %s\n' "$1" "${2:-}"
}
skip() { SKIP=$((SKIP + 1)); SKIPPED_NAMES+=("$1"); printf '  SKIP  %s (%s)\n' "$1" "${2:-}"; }

have() { command -v "$1" >/dev/null 2>&1; }

# run NAME -- command...                      expect exit status 0
run() {
  local name="$1"
  shift
  [ "${1:-}" = "--" ] && shift
  local out rc
  out=$("$@" 2>&1)
  rc=$?
  if [ $rc -eq 0 ]; then ok "$name"; else bad "$name" "rc=$rc :: $(printf '%s' "$out" | tail -4 | tr '\n' ' ')"; fi
  printf '%s' "$out" >"$WORK/last.out"
}

# run_contains NAME NEEDLE -- command...      exit 0 and output contains NEEDLE
run_contains() {
  local name="$1" needle="$2"
  shift 2
  [ "${1:-}" = "--" ] && shift
  local out rc
  out=$("$@" 2>&1)
  rc=$?
  if [ $rc -eq 0 ] && printf '%s' "$out" | grep -qF -- "$needle"; then
    ok "$name"
  else
    bad "$name" "rc=$rc needle='$needle' :: $(printf '%s' "$out" | tail -4 | tr '\n' ' ')"
  fi
  printf '%s' "$out" >"$WORK/last.out"
}

# run_output NAME NEEDLE -- command...        any exit code, output contains NEEDLE
run_output() {
  local name="$1" needle="$2"
  shift 2
  [ "${1:-}" = "--" ] && shift
  local out rc
  out=$("$@" 2>&1)
  rc=$?
  if printf '%s' "$out" | grep -qF -- "$needle"; then
    ok "$name"
  else
    bad "$name" "rc=$rc needle='$needle' :: $(printf '%s' "$out" | tail -4 | tr '\n' ' ')"
  fi
  printf '%s' "$out" >"$WORK/last.out"
}

# run_status NAME EXPECTED_WITHOUT_YES -- command... (write commands need --yes)
run_refused() {
  local name="$1"
  shift
  [ "${1:-}" = "--" ] && shift
  local out rc
  out=$("$@" 2>&1)
  rc=$?
  if [ $rc -eq 2 ]; then ok "$name"; else bad "$name" "expected exit 2, got $rc :: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"; fi
}

exists() { if [ -e "$2" ]; then ok "$1"; else bad "$1" "missing $2"; fi; }

echo "== 1. entry points =="
run_contains "cli --version" "Mount File" -- "$PY" -m mountfile --version
run_contains "main.py --version" "Mount File" -- "$PY" main.py --version
run "main.py --diag" -- "$PY" main.py --diag
run_contains "capabilities" "capabilities" -- "$PY" -m mountfile capabilities

if ! have mkfs.vfat || ! have mcopy; then
  skip "FAT32 profile (mkfs.vfat + mcopy)" "dosfstools/mtools not installed"
else
  echo "== 2. FAT32 read + bounded write (verified by mtools and fsck.vfat) =="
  # 64 MiB keeps the cluster count inside the unambiguous FAT32 range; a forced
  # -F 32 image of a tiny volume is refused by design.
  IMG="$WORK/fat32.img"
  dd if=/dev/zero of="$IMG" bs=1M count=64 status=none
  if ! mkfs.vfat -F 32 -n SMOKE "$IMG" >/dev/null 2>&1; then
    bad "mkfs.vfat" "could not format the test image"
  fi
  printf 'hello from fat\n' >"$WORK/hello.txt"
  printf 'second file\n' >"$WORK/second.txt"
  mcopy -i "$IMG" "$WORK/hello.txt" ::HELLO.TXT >/dev/null 2>&1
  mmd -i "$IMG" ::DOCS >/dev/null 2>&1
  mcopy -i "$IMG" "$WORK/second.txt" ::DOCS/SECOND.TXT >/dev/null 2>&1

  run_contains "volumes" "FAT32" -- "$PY" -m mountfile volumes "$IMG"
  run_contains "info" "FAT32" -- "$PY" -m mountfile info "$IMG"
  run_contains "ls /" "HELLO.TXT" -- "$PY" -m mountfile ls "$IMG" /
  run_contains "ls /DOCS" "SECOND.TXT" -- "$PY" -m mountfile ls "$IMG" /DOCS
  run_contains "stat" "HELLO.TXT" -- "$PY" -m mountfile stat "$IMG" /HELLO.TXT
  run_contains "read-range" "hello from fat" -- "$PY" -m mountfile read-range "$IMG" /HELLO.TXT --length 32
  run "get file" -- "$PY" -m mountfile get "$IMG" /HELLO.TXT -o "$WORK/out"
  exists "get wrote the file" "$WORK/out/HELLO.TXT"
  run "get directory" -- "$PY" -m mountfile get "$IMG" /DOCS -o "$WORK/out"
  exists "get wrote the directory tree" "$WORK/out/DOCS/SECOND.TXT"
  run_contains "preflight" "FAT32" -- "$PY" -m mountfile preflight "$IMG"
  run_contains "status" "write" -- "$PY" -m mountfile status "$IMG"
  run_contains "autopick" "native-fat32" -- "$PY" -m mountfile autopick "$IMG"

  printf 'written by mountfile\n' >"$WORK/put.txt"
  run "put --yes" -- "$PY" -m mountfile put "$IMG" "$WORK/put.txt" --dest / --yes
  if mtype -i "$IMG" ::PUT.TXT >/dev/null 2>&1; then ok "put verified by mtools"; else bad "put verified by mtools"; fi
  run "mkdir --yes" -- "$PY" -m mountfile mkdir "$IMG" /NEWDIR --yes
  if mdir -i "$IMG" ::NEWDIR >/dev/null 2>&1; then ok "mkdir verified by mtools"; else bad "mkdir verified by mtools"; fi
  run "mv --yes" -- "$PY" -m mountfile mv "$IMG" /PUT.TXT RENAMED.TXT --yes
  if mtype -i "$IMG" ::RENAMED.TXT >/dev/null 2>&1; then ok "mv verified by mtools"; else bad "mv verified by mtools"; fi
  run "rm --yes" -- "$PY" -m mountfile rm "$IMG" /RENAMED.TXT --yes
  if mtype -i "$IMG" ::RENAMED.TXT >/dev/null 2>&1; then bad "rm removed the file" "RENAMED.TXT is still readable"; else ok "rm removed the file"; fi
  run_refused "put without --yes is refused" -- "$PY" -m mountfile put "$IMG" "$WORK/put.txt" --dest /
  if have fsck.vfat; then
    if fsck.vfat -n "$IMG" >/dev/null 2>&1; then ok "fsck.vfat clean after four writes"; else bad "fsck.vfat clean after four writes"; fi
  else
    skip "fsck.vfat clean after four writes" "fsck.vfat not installed"
  fi
fi

IMG="${IMG:-$WORK/fat32.img}"

if ! have mkfs.exfat; then
  skip "exFAT profile" "exfatprogs not installed"
else
  echo "== 3. exFAT =="
  EX="$WORK/exfat.img"
  dd if=/dev/zero of="$EX" bs=1M count=32 status=none
  mkfs.exfat -n SMOKEEX "$EX" >/dev/null 2>&1 || bad "mkfs.exfat" "could not format the test image"
  run_contains "exfat info" "exFAT" -- "$PY" -m mountfile info "$EX"
  run_contains "exfat ls" "exFAT" -- "$PY" -m mountfile ls "$EX" /
  run "exfat preflight" -- "$PY" -m mountfile preflight "$EX"
  if have fsck.exfat; then
    if fsck.exfat -n "$EX" >/dev/null 2>&1; then ok "fsck.exfat clean"; else bad "fsck.exfat clean"; fi
  else
    skip "fsck.exfat clean" "fsck.exfat not installed"
  fi
fi

if ! have mkfs.ext2 || ! have debugfs; then
  skip "ext2 read/write profile" "e2fsprogs not installed"
else
  echo "== 4. ext2 (documented clean, non-indexed profile) =="
  E2="$WORK/ext2.img"
  dd if=/dev/zero of="$E2" bs=1M count=24 status=none
  mkfs.ext2 -q -F -O ^dir_index -L SMOKEE2 "$E2" >/dev/null 2>&1 || bad "mkfs.ext2" "could not format the test image"
  printf 'ext2 payload\n' >"$WORK/ext.txt"
  debugfs -w -R "write $WORK/ext.txt ext.txt" "$E2" >/dev/null 2>&1
  run_contains "ext2 info" "ext2" -- "$PY" -m mountfile info "$E2"
  run_contains "ext2 ls" "ext.txt" -- "$PY" -m mountfile ls "$E2" /
  run_contains "ext2 stat" "ext.txt" -- "$PY" -m mountfile stat "$E2" /ext.txt
  run "ext2 get" -- "$PY" -m mountfile get "$E2" /ext.txt -o "$WORK/out_e2"
  exists "ext2 get wrote the file" "$WORK/out_e2/ext.txt"
  run_contains "ext2 preflight" "ext2" -- "$PY" -m mountfile preflight "$E2"
  run "ext2 mkdir --yes" -- "$PY" -m mountfile mkdir "$E2" /newdir --yes
  run "ext2 put --yes" -- "$PY" -m mountfile put "$E2" "$WORK/ext.txt" --dest / --yes
  if debugfs -R "ls -l /" "$E2" 2>/dev/null | grep -q "ext.txt"; then ok "ext2 still lists the file after writes"; else bad "ext2 still lists the file after writes"; fi
  if have e2fsck; then
    if e2fsck -fn "$E2" >/dev/null 2>&1; then ok "e2fsck clean after ext2 writes"; else bad "e2fsck clean after ext2 writes"; fi
  else
    skip "e2fsck clean after ext2 writes" "e2fsck not installed"
  fi
fi

if ! have mkfs.ext4; then
  skip "ext4 read-only profile" "mkfs.ext4 not installed"
else
  echo "== 5. ext4 read-only =="
  E4="$WORK/ext4.img"
  dd if=/dev/zero of="$E4" bs=1M count=24 status=none
  mkfs.ext4 -q -F -L SMOKEE4 "$E4" >/dev/null 2>&1 || bad "mkfs.ext4" "could not format the test image"
  printf 'ext4 payload\n' >"$WORK/ext4.txt"
  if have debugfs; then
    debugfs -w -R "write $WORK/ext4.txt e4.txt" "$E4" >/dev/null 2>&1
  fi
  run_contains "ext4 info" "ext4" -- "$PY" -m mountfile info "$E4"
  run_contains "ext4 ls" "e4.txt" -- "$PY" -m mountfile ls "$E4" /
  run_contains "ext4 read-range" "ext4 payload" -- "$PY" -m mountfile read-range "$E4" /e4.txt --length 32
  run_contains "ext4 preflight" "ext4" -- "$PY" -m mountfile preflight "$E4"
  out=$("$PY" -m mountfile put "$E4" "$WORK/ext.txt" --dest / --yes 2>&1)
  rc=$?
  if [ $rc -ne 0 ]; then ok "ext4 write is refused (read-only profile)"; else bad "ext4 write is refused (read-only profile)" "rc=$rc"; fi
fi

if ! have mkfs.ntfs; then
  skip "NTFS read-only profile" "ntfs-3g (mkfs.ntfs) not installed"
else
  echo "== 6. NTFS read-only =="
  NT="$WORK/ntfs.img"
  dd if=/dev/zero of="$NT" bs=1M count=40 status=none
  mkfs.ntfs -F -Q -L SMOKENT "$NT" >/dev/null 2>&1 || bad "mkfs.ntfs" "could not format the test image"
  run_contains "ntfs info" "NTFS" -- "$PY" -m mountfile info "$NT"
  run_contains "ntfs ls" "/" -- "$PY" -m mountfile ls "$NT" /
  run "ntfs preflight" -- "$PY" -m mountfile preflight "$NT"
fi

echo "== 7. DFIR commands =="
if [ -f "$IMG" ]; then
  cat >"$WORK/detect.yar" <<'YAR'
rule Greeting_marker {
    strings:
        $a = "hello from fat" ascii
    condition:
        $a
}
YAR
  run_contains "hash" "SHA-256:" -- "$PY" -m mountfile hash "$IMG"
  run_contains "grep" "hello from fat" -- "$PY" -m mountfile grep "$IMG" "hello from fat"
  run "search" -- "$PY" -m mountfile search "$IMG" "hello from fat"
  run "timeline-build" -- "$PY" -m mountfile timeline-build "$IMG" -o "$WORK/timeline.db" --csv "$WORK/timeline.csv"
  exists "timeline csv written" "$WORK/timeline.csv"
  run "timeline-query" -- "$PY" -m mountfile timeline-query --db "$WORK/timeline.db"
  run "timestomp-scan" -- "$PY" -m mountfile timestomp-scan "$IMG"
  run "yara-scan (files + unallocated)" -- "$PY" -m mountfile yara-scan "$IMG" --rules "$WORK/detect.yar"
  run "yara-scan (files only)" -- "$PY" -m mountfile yara-scan "$IMG" --rules "$WORK/detect.yar" --targets files
  run_contains "yara single file" "Greeting_marker" -- "$PY" -m mountfile yara "$WORK/hello.txt" --rules "$WORK/detect.yar"
  printf '[[0, 131072]]\n' >"$WORK/ranges.json"
  run_output "unallocated-scan" "Matches:" -- "$PY" -m mountfile unallocated-scan "$IMG" --ranges "$WORK/ranges.json"
  run "slack-extract" -- "$PY" -m mountfile slack-extract "$IMG" -o "$WORK/slack"
  run "evidence-manifest" -- "$PY" -m mountfile evidence-manifest "$IMG" --output "$WORK/manifest.json"
  exists "evidence manifest written" "$WORK/manifest.json"
  cp "$IMG" "$WORK/fat32_after.img"
  printf 'appended\n' >>"$WORK/fat32_after.img"
  run "live-diff" -- "$PY" -m mountfile live-diff "$IMG" "$WORK/fat32_after.img" -o "$WORK/diff.md"
  exists "live-diff markdown written" "$WORK/diff.md"
else
  skip "DFIR commands" "no FAT32 fixture was built"
fi

echo "== 8. RT809H dump decode / split =="
DUMP="$WORK/dump.bin"
dd if=/dev/zero of="$DUMP" bs=1M count=4 status=none
# The MST decoder looks for the plain-text "MBOT" marker at 0x200094.
printf 'MBOT' | dd of="$DUMP" bs=1 seek=$((0x200094)) conv=notrunc status=none
run "decode MST dump" -- "$PY" -m mountfile decode "$DUMP"
run "split needs an existing udev file" -- bash -c "! '$PY' -m mountfile split '$DUMP' '$WORK/missing_udev.txt' -o '$WORK/parts'"
cat >"$WORK/udev.txt" <<'UDEV'
show = 0

[PARTITIONS]
0x0,0x100000,preloader
0x100000,0x200000,system
UDEV
run "split by udev [PARTITIONS]" -- "$PY" -m mountfile split "$DUMP" "$WORK/udev.txt" -o "$WORK/parts2"
if ls "$WORK"/parts2/*preloader.bin >/dev/null 2>&1; then ok "split wrote the preloader partition"; else bad "split wrote the preloader partition"; fi

echo "== 9. GUI (offscreen) =="
cat >"$WORK/gui_smoke.py" <<'PYEOF'
import sys

from mountfile.qt_compat import QApplication, QMessageBox

QMessageBox.warning = staticmethod(lambda *a, **k: print("WARN:", a[1], flush=True))
QMessageBox.question = staticmethod(lambda *a, **k: print("ASK:", a[1], flush=True))
QMessageBox.information = staticmethod(lambda *a, **k: print("INFO:", a[1], flush=True))

from mountfile import tc_window

app = QApplication([])
window = tc_window.MainWindow()
assert window.load_image(sys.argv[1]), "load_image failed"
panel = window.panels["L"]
panel.refresh()
assert panel.writer is not None, "no writer: %s" % panel.write_reason
names = {entry["name"] for entry in panel.entries}
assert "HELLO.TXT" in names, names
print("gui ok:", len(panel.entries), "entries, writable =", panel.writer is not None, flush=True)
window.close()
PYEOF
if [ -f "$IMG" ]; then
  run "gui import + open FAT32 + writable volume" -- env QT_QPA_PLATFORM=offscreen MOUNTFILE_NO_UPDATE=1 "$PY" "$WORK/gui_smoke.py" "$IMG"
else
  skip "gui import + open FAT32 + writable volume" "no FAT32 fixture was built"
fi

echo
echo "== summary: $PASS passed, $FAIL failed, $SKIP skipped =="
if [ $SKIP -ne 0 ]; then
  printf 'skipped checks:\n'
  for name in "${SKIPPED_NAMES[@]}"; do printf ' - %s\n' "$name"; done
fi
if [ $FAIL -ne 0 ]; then
  printf 'failed checks:\n'
  for name in "${FAILED_NAMES[@]}"; do printf ' - %s\n' "$name"; done
  exit 1
fi
