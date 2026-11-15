# Issue #510 acceptance status

This note maps the 2026-11-14 audit checklist to the code that actually exists
on `main` plus PR #514. It is deliberately conservative: a row is `implemented`
only when a test exercises the mechanism; planning-only code is marked `plan`.

## Block B — ext4 / JBD2

| Item | Status | Evidence |
| --- | --- | --- |
| Checksums end-to-end | partial | v2/v3 primitives in `mountfile/ext4_jbd2_csum.py`; the classic decoder and `coordinate_classic_jbd2_replay()` reject any nonzero JBD2 feature tuple, while corrupted commits and truncated rings fail closed. A checksummed replay engine is still not wired into the ext4 writer. |
| Journal head is read correctly | verified | `journal_superblock_t` puts `s_sequence` at 0x18 and `s_start` at 0x1C. `probe_journal()` had them swapped (`7b48efe`), which applied the ring-geometry guard to the transaction id, so any filesystem with more transactions than journal blocks (the default journal is 1024 blocks) was reported `ERROR` and every native write was refused on a healthy image. `tests/test_native_journal_writers.py` pins both offsets against the `jbd2_writer` parser, the checkpoint planner that zeroes 8/24/28 and the `test_ext3_reader` fixtures. |
| Checkpointing in the writer cycle | verified | Every ext4 write ends with `JournalState.CLEAN` and `s_start == 0` (the durable empty marker the checkpoint writes), checked by `e2fsck -fn` in `tests/test_writer_fault_coverage.py`. |
| Process-kill at every transaction boundary | verified | Real `SIGKILL` at every `FAULT_POINTS` boundary; the source image is byte-identical until `publish:done` (`tests/test_writer_fault_coverage.py`). |
| Interrupted commit / malformed journal refusal | verified | A zero-filled commit block (interrupted) and a commit whose sequence disagrees are both refused by the scanner, the refusal surfaces as `Ext4ProfileUnsupported` and the source image is byte-identical afterwards with no staging file left behind (`test_ext4_native_writer_refuses_malformed_journal_and_keeps_original`). `e2fsck -fy` recovery on a disposable copy remains re-verified with `e2fsck -fn` and the journal gate. |
| Native replay applied to the filesystem | verified (classic profile) | `recover="native"` + `native_classic_jbd2_recover` reads the ring through the journal inode's resolved extents, applies a committed transaction and checkpoints the head; an independent `debugfs cat` shows the journaled payload rather than the on-disk one, the gate reports `CLEAN` and `e2fsck -fn` exits 0 (`test_ext4_native_replay_applies_the_committed_transaction`, `test_ext4_native_writer_replays_then_writes_on_the_native_path`). `RECOVER` is cleared on the copy only after the coordinator succeeded. The checksummed v2/v3 profile still refuses. |
| `e2fsck -f -n` after every writer operation | verified | `verify_image_after_operation()` runs after every libext2fs write and after delegated `create_file`, before atomic publication. |

## Block C — NTFS / `$LogFile`

| Item | Status | Evidence |
| --- | --- | --- |
| `$LogFile` parser: RCRD, restart area, USA, redo/undo | implemented | `mountfile/ntfs_logfile.py`, `tests/test_ntfs_logfile_parser.py`. |
| Undo for OPEN transactions | partial | `_build_plans()` produces an undo plan from active transactions; `replay_analysis_on_resident_record()` derives the target from a validated FILE record and applies committed redo before undo in a private overlay. The profile is limited to `UpdateResidentValue`; MFT/index/bitmap/mirror operations are typed refusals. It is not an image writer. |
| Checkpointing and full redo | partial | Checkpoint LSN, transaction table and redo plan are parsed; `NtfsReplayOverlay.apply_to_stream()` now validates the complete explicit-target plan before writing, flushes/syncs and performs full read-back on a disposable stream. `executable` stays `False` until dirty-page-table replay start, MFT/runlist resolution and crash evidence exist. |
| Safe resident target resolution | partial | `resolve_resident_attribute_target()` validates a USA-restored FILE record, unique resident attribute id, value bounds and rejects `$ATTRIBUTE_LIST`/nonresident targets. |
| Bounded nonresident run resolution | partial | `resolve_nonresident_run_target()` validates contiguous VCN coverage, `highest_vcn`, allocated/data/initialized sizes and one fully mapped non-sparse run; incomplete, sparse, unsupported and cross-run ranges refuse. `replay_analysis_on_nonresident_run()` is private-overlay only and not wired to image publication. |
| Safe MFT allocation/reuse and `$MFTMirr` repair | not implemented | The gate only compares `$MFT`/`$MFTMirr` record 0 and refuses a mismatch; allocation, reuse, index and mirror operations remain typed refusals. |
| Replay on copied Windows/NTFS crash fixtures | not implemented | Only the read-only fixture exists; dirty/crashed fixtures are not available. |
| Quarantine for unproven replay | implemented | `recover="copy"` writes a content-addressed copy plus JSON marker and raises `NtfsReplayQuarantined`; tests in `tests/test_native_journal_writers.py`. |
| `ntfsfix` is secondary, not authoritative | verified | `probe_ntfs_log()` reads `$Volume` and `$LogFile` structures; it never trusts a command exit code. |

## Block D — acceptance matrix (#387)

| Item | Status | Evidence |
| --- | --- | --- |
| Failure injection after each step | partial | Runtime fault seam covers copy/helper/mutation/publish; per-metadata-step injection is not complete for every profile. The NTFS atomic-replace owner emitted each publication boundary twice through two overlapping seams, so an injected stop recorded a phase the executor never ran; both paths now share one seam and `test_ntfs_replay_publish_fault_boundary_is_emitted_once` asserts the trace contains exactly the expected boundary once (`3492988`). |
| Process-kill for each transaction phase | partial | ext4 has real process-kill coverage; other writers keep their existing tests. |
| Hash preservation on failure | verified | ext4 tests assert the source hash is unchanged; the recovery path now does too, because a malformed journal is refused before any block is applied (`test_ext4_native_writer_refuses_malformed_journal_and_keeps_original`). NTFS staged writes use the same source-identity guard. |
| Source identity / concurrent modification | verified | SHA-256 is captured before staging and re-checked before atomic replace in ext4 and NTFS writers. |
| Independent checker + read-back per operation | partial | ext4 uses `e2fsck -fn` plus journal gate after every operation; the bounded NTFS executor adds stream flush/read-back, but NTFS still has no filesystem-aware replay checker. |
| CI artifact profile × operation × state × checker | implemented | `scripts/recovery_matrix.py` emits explicit operation/state/checker cells with typed `pass`, `unavailable`, and `not_proven` statuses into `docs/generated/recovery-acceptance.json`, including the experimental ext4/NTFS replay profiles. A `journal_replay` evidence key now records `tested_native_classic_end_to_end` for ext4 and an explicit `not_proven` for NTFS, so the gap is machine-visible rather than absent; the key is outside `_REQUIRED`, so no cell verdict or blocking set changes (`e058d96`). The canonical `docs/generated/recovery-matrix.json` stays byte-identical to the trusted generator, because the trusted status gate only accepts a one-step capability schema bump for it; a unit test pins both artifacts to their generators. |
| Unsupported cells as typed refusals | partial | Capability contracts and NTFS quarantine are typed; not every profile has an end-to-end assertion. |

## Tooling contracts

| Item | Status | Evidence |
| --- | --- | --- |
| Hang guard is really installed | verified | `pyproject.toml` declares `timeout`/`timeout_method`, but pytest only warns on an unknown ini option, so the jobs that wrote their own pip line were running with no per-test timeout. Every workflow that invokes `python -m pytest` now provisions `pytest-timeout`, and `test_pytest_hang_guard_is_declared_and_actually_installed` walks the workflows and fails on the drift (`0942b63`). |

## Block E — parity and status

| Item | Status | Evidence |
| --- | --- | --- |
| #389 cross-platform fixtures | open | No new Windows/macOS replay fixtures in this change. |
| #390 README/status CI gate | open | The trusted generated-status gate remains unchanged; README still reports ext3/ext4 and NTFS as read-only. |

## Conclusion

The ext4 blocker in the checklist is closed for the classic JBD2 profile: the
journal gate reads the head correctly, an uncheckpointed head is refused instead
of published, and a committed transaction is replayed to the filesystem image and
confirmed by an independent checker and reader. It is still not a product-level
"Replay supported" claim: the checksummed v2/v3 profile refuses, the capability
matrix is unchanged, and README keeps ext3/ext4 and NTFS read-only until the
trusted generated-status gate can close row by row.

The remaining blockers are the NTFS filesystem-aware image executor (MFT
allocation, `$MFTMirr`, indexes) with dirty-page-table replay start and real NTFS
crash fixtures, JBD2 checksum integration into the writer, and the Windows/macOS
replay fixtures of #389. Until then the writers stay fail-closed, and dirty NTFS
volumes are quarantined rather than modified.
