# Issue #510 acceptance status

Status snapshot: 2026-11-17. This follow-up starts from `main@9769bab`. The full local suite on code head `57b3c77` completed with **2763 passed, 99 skipped** in 225.59s; the focused writer/checker/failure suite reported **97 passed, 3 skipped**. Skips were for optional/platform tools unavailable in the sandbox. Earlier per-subsystem counts below are historical unless explicitly tied to this run. This document distinguishes code/tests from external acceptance evidence. Nothing here declares ext4 or NTFS product writes supported.

## ext4 / JBD2

The repository contains bounded ext4 writing and JBD2 replay/checkpoint paths. The focused suite at the base revision reported 112 passing tests across checksums, replay, interrupted commits, checkpointing, writer fault boundaries, and process-kill cases. Relevant code and tests include `mountfile/ext4_jbd2_csum.py`, `mountfile/native_write.py`, `tests/test_jbd2_csum_replay.py`, `tests/test_jbd2_checkpoint_interruption.py`, and `tests/test_ext4_writer_fault_coverage.py`.

This is implementation and test evidence, not production readiness. Independent ext4 checking is exercised in selected tests, but the full acceptance requirement for every operation and failure phase is not yet established. Product status remains read-only for ext3/ext4. Power-loss durability is not claimed.

## NTFS / `$LogFile`

The repository contains a bounded `$LogFile` parser and replay path. `recover="native"` invokes replay on a disposable copy and remains fail-closed for unsupported operations. The gated experimental `NtfsNativeWriter` now compares staged file bytes from `ntfscat` with the requested payload before publication. This is not the VFS/application route: `NtfsVFS` uses `NtfsTransactionalWriter`, whose existing log gate remains fail-closed because `ntfsfix` cannot establish Windows parity. The focused parser/replay suite at the base revision reported 87 passing tests. The crash-oracle cases are synthetic NTFS images, not Windows-generated crash captures.

`ntfsfix -n` is an auxiliary check only. It is not a substitute for Windows `chkdsk`, and it does not establish Windows parity. No Windows crash-capture reference set or byte-level `$MFT`/`$BITMAP` comparison against a `chkdsk`-recovered image is available. The staged read-back hardening applies only to the gated `NtfsNativeWriter` helper and does not make product-level/VFS NTFS writes supported; keep them unsupported/read-only and do not describe bounded replay as production-ready.

## Failure-matrix evidence

The artifact generator now reads pytest JUnit XML and maps exact test node IDs to profile/state cells. A passing test is recorded separately from checker evidence. The artifact's `acceptance` is always `not_claimed`; a missing or skipped node is typed as `not_proven` or `unavailable`, not green. The status `passed` means only that the mapped pytest case passed.

The scheduled failure workflow includes FAT12/16, FAT32, ext2, exFAT, ext4/JBD2, and synthetic NTFS cases. As checked on 2026-11-17, GitHub reports no runs for this workflow with event `schedule`; the available run is a manual `workflow_dispatch`. Therefore the requirement for two consecutive scheduled runs with valid artifacts remains unproven.

The main-push CI platform gate expects Linux, Windows, and macOS. The PR/merge-queue fast tier expects Linux and Windows only. These artifacts cover the read-only VFS contracts and do not establish independent filesystem parity.

## Remaining external acceptance criteria

- Capture crash fixtures from Windows-generated NTFS images and retain the source/tooling provenance.
- Recover those images with Windows `chkdsk`, then compare byte-level `$MFT` and `$BITMAP` state with the native replay result.
- Complete two consecutive scheduled nightly failure-matrix runs; do not substitute a manual workflow dispatch.
- Establish full per-operation/per-phase independent checker and read-back coverage before considering any product write support.

Until those items are met, ext4 and NTFS write support must remain unclaimed in product-facing status and documentation.
