# Issue #510 acceptance status

This note maps the 2026-11-14 audit checklist to the code on
`feat/issue-510-replay-completion`, branched from `main` at `3bbf208`. It is
deliberately conservative: a row is `implemented` only when a test exercises
the mechanism; a bounded executor is not a product-level "replay supported"
claim.

The audit was written against `main` at `b8cecb6` (v1.91.0). Its Block C rows
that say the `$LogFile` parser, the private overlay and quarantine do not exist
are stale: those landed before this branch. The new work adds the
filesystem-aware image executor, the checksummed JBD2 replay path and the
acceptance evidence below.

## Block B — ext4 / JBD2

| Item | Status | Evidence |
| --- | --- | --- |
| Checksums end-to-end | verified | `mountfile/ext4_jbd2_csum.py` implements the v2/v3 superblock, descriptor tag and revoke checksums with the kernel's big-endian CRC32C seed. `tests/test_jbd2_csum_replay.py` round-trips the encoder/decoder, selects the checksummed profile and replays a committed v3 transaction; a corrupted commit checksum and a corrupted journal superblock are refused before any write (`test_bad_commit_checksum_is_refused_before_any_write`, `test_bad_journal_superblock_checksum_is_an_error`). |
| Journal head is read correctly | verified | `journal_superblock_t` puts `s_sequence` at 0x18 and `s_start` at 0x1C. `probe_journal()` had them swapped (`7b48efe`), which applied the ring-geometry guard to the transaction id, so any filesystem with more transactions than journal blocks (the default journal is 1024 blocks) was reported `ERROR` and every native write was refused on a healthy image. `tests/test_native_journal_writers.py` pins both offsets against the `jbd2_writer` parser, the checkpoint planner that zeroes 8/24/28 and the `test_ext3_reader` fixtures. |
| Checkpointing in the writer cycle | verified | Every ext4 write ends with `JournalState.CLEAN` and `s_start == 0` (the durable empty marker the checkpoint writes), checked by `e2fsck -fn` in `tests/test_writer_fault_coverage.py`. |
| Process-kill at every transaction boundary | verified | Real `SIGKILL` at every `FAULT_POINTS` boundary; the source image is byte-identical until `publish:done` (`tests/test_writer_fault_coverage.py`). |
| Interrupted commit / malformed journal refusal | verified | A zero-filled commit block (interrupted) and a commit whose sequence disagrees are both refused by the scanner, the refusal surfaces as `Ext4ProfileUnsupported` and the source image is byte-identical afterwards with no staging file left behind (`test_ext4_native_writer_refuses_malformed_journal_and_keeps_original`). `e2fsck -fy` recovery on a disposable copy remains re-verified with `e2fsck -fn` and the journal gate. |
| Native replay applied to the filesystem | verified (classic and checksummed v2/v3) | `recover="native"` + `native_classic_jbd2_recover` reads the ring through the journal inode's resolved extents, applies a committed transaction and checkpoints the head; an independent `debugfs cat` shows the journaled payload rather than the on-disk one, the gate reports `CLEAN` and `e2fsck -fn` exits 0 (`test_ext4_native_replay_applies_the_committed_transaction`, `test_ext4_native_writer_replays_then_writes_on_the_native_path`). The checksummed path is covered by `test_native_v3_replay_restores_committed_block`. `RECOVER` is cleared on the copy only after the coordinator succeeded. |
| `e2fsck -f -n` after every writer operation | verified | `verify_image_after_operation()` runs after every libext2fs write and after delegated `create_file`, before atomic publication. |

## Block C — NTFS / `$LogFile`

| Item | Status | Evidence |
| --- | --- | --- |
| `$LogFile` parser: RCRD, restart area, USA, redo/undo | implemented | `mountfile/ntfs_logfile.py`, `tests/test_ntfs_logfile_parser.py`. The parser also handles restart-table dump records (`OpenAttributeTableDump`, `DirtyPageTableDump`, `TransactionTableDump`) and accepts open-attribute slot 0 as a valid target. |
| Undo for OPEN transactions | implemented (bounded image executor) | `native_ntfs_recover()` applies committed redo first and then walks each active transaction's `undo_next_lsn` chain, applying the undo payloads. `test_active_transaction_is_undone_after_redo` starts from an active transaction whose update is in the dirty-page table, confirms redo ran, confirms undo ran, and reads the original bytes back with `ntfscat`. |
| Checkpointing and full redo | implemented (bounded profile) | `parse_restart_tables()` reads the open-attribute, dirty-page and transaction tables. `plan_ntfs_replay()` derives the redo start from `min(oldest_lsn, first_lsn)` instead of the checkpoint LSN, matches records through `find_dp`-style `target_attr`/VCN checks, and resolves targets through the open-attribute table and the `$MFT` runlist. The executor writes both clean `RSTR` pages, clears `$Volume`, refreshes `$MFTMirr` and re-runs the independent gate (`test_committed_resident_update_is_replayed_and_read_back`, `test_committed_nonresident_update_is_replayed`, `test_second_recovery_is_a_clean_noop`). |
| Safe resident target resolution | implemented | `mountfile/ntfs_replay.py` validates the USA-restored FILE record, the open-attribute entry's record/sequence, the attribute type at `record_off`, the resident value bounds and refuses `$ATTRIBUTE_LIST`/resizing. `test_committed_resident_update_is_replayed_and_read_back`. |
| Bounded nonresident run resolution | implemented | `_run_segments()` maps the VCN/`cluster_off` byte range through the attribute's runlist, refuses sparse runs and holes, and writes the redo payload at `record_off`; `test_committed_nonresident_update_is_replayed` reads the changed bytes back with `ntfscat`. |
| Safe MFT allocation/reuse and `$MFTMirr` repair | partial | The bounded executor supports `InitializeFileRecordSegment`, `DeallocateFileRecordSegment`, `WriteEndOfFileRecordSegment`, `CreateAttribute`, `DeleteAttribute`, `UpdateMappingPairs`, `SetNewAttributeSizes`, `ZeroEndOfFileRecord` and the nonresident bitmap operations inside the existing `$MFT` DATA stream. `test_create_and_delete_attribute_handlers_round_trip` and `test_initialize_and_deallocate_record_handlers` exercise the record/attribute handlers, and `test_mft_mirror_is_repaired_on_the_volume` starts from a corrupt `$MFTMirr`. `$MFTMirr` records 0-3 are refreshed from `$MFT` after replay. Extending `$MFT` with new runs, allocating records beyond the current stream, index-entry surgery and cross-record `$ATTRIBUTE_LIST` targets remain typed refusals (`test_unsupported_operation_is_refused_before_writing`). |
| Replay on copied Windows/NTFS crash fixtures | partial | `tests/ntfs_crash_fixture.py` builds an fslog.c-shaped crash image from a real `mkntfs` volume and a real `ntfscp` file: two valid `RSTR` pages, one `RCRD` page, a client restart record, all three checkpoint tables, a committed/active update and a dirty `$Volume`. There is still no real Windows crash capture; the fixture is synthetic but byte-layout compatible. |
| Quarantine for unproven replay | implemented | `recover="copy"` writes a content-addressed copy plus JSON marker and raises `NtfsReplayQuarantined`; `recover="native"` replays on the disposable copy and re-gates before `ntfscp`. Tests in `tests/test_native_journal_writers.py` and `tests/test_ntfs_replay.py`. |
| `ntfsfix` is secondary, not authoritative | verified | `probe_ntfs_log()` reads `$Volume` and `$LogFile` structures; it never trusts a command exit code. |

## Block D — acceptance matrix (#387)

| Item | Status | Evidence |
| --- | --- | --- |
| Failure injection after each step | partial | Runtime fault seam covers copy/helper/mutation/publish; per-metadata-step injection is not complete for every profile. The NTFS atomic-replace owner emitted each publication boundary twice through two overlapping seams, so an injected stop recorded a phase the executor never ran; both paths now share one seam and `test_ntfs_replay_publish_fault_boundary_is_emitted_once` asserts the trace contains exactly the expected boundary once (`3492988`). |
| Process-kill for each transaction phase | partial | ext4 has real process-kill coverage; NTFS keeps the staged publish seam and source-identity checks but has no per-transaction `SIGKILL` fixture. |
| Hash preservation on failure | verified | ext4 tests assert the source hash is unchanged; the recovery path now does too, because a malformed journal is refused before any block is applied (`test_ext4_native_writer_refuses_malformed_journal_and_keeps_original`). NTFS replay refuses unsupported plans before opening the image for writing, and `test_unsupported_operation_is_refused_before_writing` asserts the source digest is unchanged. |
| Source identity / concurrent modification | verified | SHA-256 is captured before staging and re-checked before atomic replace in ext4 and NTFS writers. |
| Independent checker + read-back per operation | partial | ext4 uses `e2fsck -fn` plus the journal gate after every operation. NTFS replay uses the independent `$LogFile` gate plus `ntfscat` read-back on the synthetic crash fixtures; a real Windows checker and capture are still unavailable. |
| CI artifact profile × operation × state × checker | implemented | `scripts/recovery_matrix.py` emits explicit operation/state/checker cells with typed `pass`, `unavailable`, and `not_proven` statuses into `docs/generated/recovery-acceptance.json`, including the experimental ext4/NTFS replay profiles. The NTFS `journal_replay` evidence now records `tested_native_synthetic_crash_fixture`; `docs/generated/recovery-matrix.json` stays byte-identical to the trusted generator. |
| Unsupported cells as typed refusals | partial | Capability contracts, NTFS quarantine and the bounded replay operation whitelist are typed; not every profile has an end-to-end assertion. |

## Tooling contracts

| Item | Status | Evidence |
| --- | --- | --- |
| Hang guard is really installed | verified | `pyproject.toml` declares `timeout`/`timeout_method`, but pytest only warns on an unknown ini option, so the jobs that wrote their own pip line were running with no per-test timeout. Every workflow that invokes `python -m pytest` now provisions `pytest-timeout`, and `test_pytest_hang_guard_is_declared_and_actually_installed` walks the workflows and fails on the drift (`0942b63`). |

## Block E — parity and status

| Item | Status | Evidence |
| --- | --- | --- |
| #389 cross-platform fixtures | open | No new Windows/macOS replay fixtures in this change. |
| #390 README/status CI gate | open | The trusted generated-status gate remains unchanged; README still reports ext3/ext4 and NTFS as read-only. The bounded executors are experimental and are not a product-level support claim. |

## Conclusion

Block B is closed for the classic and checksummed JBD2 profiles: the journal
head is read correctly, an uncheckpointed head is refused instead of published,
a committed transaction is replayed to the filesystem image, checksums are
validated end to end and the result is confirmed by `e2fsck -fn` and a reader.

Block C now has a filesystem-aware bounded executor: restart tables, dirty-page
replay start, open-attribute/MFT resolution, resident and nonresident updates,
active-transaction undo, clean restart pages, `$Volume` clearing and `$MFTMirr`
refresh are implemented and tested on fslog.c-shaped crash fixtures. It is not
a product-level claim: real Windows crash captures are unavailable, `$MFT`
extension/allocation beyond the existing stream and index surgery are typed
refusals, and #389/#390 remain open. Until then README keeps ext3/ext4 and NTFS
read-only and the writers stay fail-closed outside `recover="native"`.
