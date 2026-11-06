# Issue #182 — external-checker matrix

`scripts/checker_matrix.py` emits a JSON capability inventory for the external
checker tools used by the native filesystem acceptance tests. The report is
uploaded by the Linux integration job as the `external-checker-matrix-linux`
artifact. It is deliberately an inventory, not a parity verdict.

## Matrix

| Native profile | Independent tool | Package/runtime | Checker authority | Fixture evidence |
|---|---|---|---|---|
| `ext2-native-rw` | `e2fsck -f -n` | `e2fsprogs` | authoritative for the checked ext2 fixture | native mutation, read-back, disposable-copy checker |
| `fat12-16-native-rw` | `fsck.fat -n` | `dosfstools` | authoritative for the checked FAT fixture | real-image mutation and rollback fixtures |
| `fat32-native-rw` | `fsck.fat -n` | `dosfstools` | authoritative for the checked FAT32 fixture | native mutation, read-back, disposable-copy checker |
| `exfat-native-rw` | `fsck.exfat -n` | `exfatprogs` | authoritative for the checked one-FAT fixture | real-image mutation, read-back, checker fixture |
| `ntfs-native-ro` | `ntfsfix -n` | `ntfs-3g` | **not authoritative**; result is always inconclusive | explicit test that a zero exit is not called clean |
| native readers | `pytsk3` | Python optional runtime | differential/reference only | benchmark or format-specific differential fixture where available |

The report includes tool availability, runner platform, the fixture test names,
and `parity: not_claimed`. If a tool is absent, the entry is `unavailable` and
never `pass`. A checker receiving a disposable image copy must not mutate the
source image; the wrapper tests enforce source and copy identity rules.

## Reproduction

```bash
python scripts/checker_matrix.py --json-out checker-matrix.json
python -m pytest -q tests/test_checker_matrix.py tests/test_independent_checkers.py
```

Real-image tests remain conditional on their required formatter/checker tools.
Linux CI installs and requires the e2fsprogs, dosfstools, exfatprogs and ntfs-3g
binaries for the integration job. Windows and macOS CI execute the native and
synthetic fixtures that do not require Linux-only binaries; their absence is
reported as unavailable rather than being interpreted as cross-platform parity.

This matrix does not enable an NTFS writer, does not treat `ntfsfix` as a full
NTFS checker, and does not claim complete filesystem-format parity.
