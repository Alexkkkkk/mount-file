# Issue #182 — external-checker matrix

`scripts/checker_matrix.py` emits a JSON capability inventory for the external
checker tools used by the native filesystem acceptance tests. A per-platform,
per-Python inventory is uploaded by the regular test matrix. Linux integration
also publishes an executed checker-fixture acceptance artifact. The inventory
is deliberately not a parity verdict.

## Matrix

| Native profile | Independent tool | Package/runtime | Checker authority | Fixture evidence |
|---|---|---|---|---|
| `ext2-native-rw` | `e2fsck -f -n` | `e2fsprogs` | authoritative for the checked ext2 fixture | native mutation, read-back, disposable-copy checker |
| `fat12-16-native-rw` | `fsck.fat -n` | `dosfstools` | authoritative for the checked FAT fixture | real-image mutation and rollback fixtures |
| `fat32-native-rw` | `fsck.fat -n` | `dosfstools` | authoritative for the checked FAT32 fixture | native mutation, read-back, disposable-copy checker |
| `exfat-native-rw` | `fsck.exfat -n` | `exfatprogs` | authoritative for the checked one-FAT fixture | real-image mutation, read-back, checker fixture |
| `ntfs-native-ro` | `ntfsfix -n` | `ntfs-3g` | **not authoritative**; result is always inconclusive | explicit test that a zero exit is not called clean |
| native readers | `pytsk3` | Python optional runtime | differential/reference only | benchmark or format-specific differential fixture where available |

The report includes tool availability, the first bounded tool-version line,
runner platform, and the fixture test names. It parses the test suite without
importing it and publishes `fixture_status`: `fixture_declared` means every
named test exists but was not executed by this inventory command;
`fixture_missing` means at least one declaration is absent. Accordingly,
`evidence` is `fixture_required` or `fixture_missing`, never a pass verdict.
`parity: not_claimed` remains mandatory for every inventory entry: an installed
binary is not evidence that a fixture passed. If a tool is absent, the entry is
`unavailable` and never `pass`; its version is `null`. A checker receiving a
disposable image copy must not mutate the source image; the wrapper tests
enforce source and copy identity rules. They record SHA-256 values for the
source image and disposable copy before and after each checker invocation as
JUnit properties. `scripts/checker_acceptance.py` validates and carries those
hashes into its executed JSON report per fixture; FAT12 and FAT16 remain
separate content-addressed cases. Hashes identify the evidence inputs and do
not establish broader format parity.

## Reproduction

```bash
python scripts/checker_matrix.py --json-out checker-matrix.json
python -m pytest -q tests/test_checker_matrix.py tests/test_independent_checkers.py
python scripts/native_read_evidence.py --junitxml test-results.xml --json-out native-read-evidence.json
```

## Cross-platform native read evidence

Each Ubuntu, Windows, and macOS test leg runs
`scripts/native_read_evidence.py` and uploads a uniquely named
`native-read-evidence-<runner>-py<version>` JSON artifact. It covers all eight
native VFS read profiles and the `list`, `stat`, `read-file`, `read-range`, and
`extract` operations. Legacy fallback is disabled. Every fixture image has a
SHA-256 before and after the read-only checks; a changed image blocks the
report. The artifact records runner/Python provenance, fixture kind/source,
observed native backend, operation results, and the separate checker inventory
with bounded tool-version strings.

The report also consumes that CI leg's `test-results.xml`. Required mocked
macOS adapter probes (signature/offset detection, no silent fallback,
read-only APFS status, and lifecycle behavior) and Windows installer contract
tests must be present and pass; a missing or skipped required probe blocks the
artifact. The Windows runner additionally emits a PowerShell JSON sidecar with
script hashes and mock assertion results, then records both installer dry-run
results. That sidecar is required and validated on Windows; Windows-specific
installer execution is marked not applicable on other operating systems. This
does not claim that HFS+/APFS images were read on macOS or that optional
`apfs-fuse`/pytsk3 dependencies are available.

FAT12/16/32, exFAT, ext2/3/4 use the existing portable synthetic builders.
NTFS uses the real-format compressed image
`tests/fixtures/ntfs-readonly-32m.img.gz`; its manifest pins compressed and
uncompressed image hashes and the expected file content. A fixture execution
may report `pass` for the native read contract, but its independent-oracle
field remains `not_claimed`. The external checker inventory does not imply
that a checker ran on the same fixture. Linux checker and writer recovery
acceptance artifacts remain the separate evidence for mutations; this read
report does not enable or claim additional writers.

Real-image tests remain conditional on their required formatter/checker tools.
Linux CI installs and requires the e2fsprogs, dosfstools, exfatprogs and ntfs-3g
binaries for the integration job. Windows and macOS CI execute the native and
synthetic fixtures that do not require Linux-only binaries; their absence is
reported as unavailable rather than being interpreted as cross-platform parity.

The `cross-platform-evidence` CI job downloads all three platform artifacts and
runs `scripts/cross_platform_evidence.py`. This aggregate gate is fail-closed:
missing, duplicate, blocked, fallback-enabled, or parity-claiming platform
reports fail the job. A complete aggregate requires Linux, Windows, and macOS,
while retaining `parity=not_claimed`; an unavailable checker remains
`unavailable`/`not_claimed` and can never become a pass merely because a native
fixture ran.

This matrix does not enable an NTFS writer, does not treat `ntfsfix` as a full
NTFS checker, and does not claim complete filesystem-format parity.
