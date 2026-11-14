# Issue #510 acceptance status

This note maps the 2026-11-14 audit checklist to the code that actually exists
on `main` plus PR #514. It is deliberately conservative: a row is `implemented`
only when a test exercises the mechanism; planning-only code is marked `plan`.

## Block B — ext4 / JBD2

| Item | Status | Evidence |
| --- | --- | --- |
| Checksums end-to-end | partial | v2/v3 primitives in `mountfile/ext4_jbd2_csum.py`; decoder refuses a corrupted commit and a truncated ring in `tests/test_jbd2_checkpoint.py`. A full checksummed replay engine is not wired into the writer. |
| Checkpointing in the writer cycle | verified | Every ext4 write ends with `JournalState.CLEAN` and `s_sequence == 0`, checked by `e2fsck -fn` in `tests/test_writer_fault_coverage.py`. |
| Process-kill at every transaction boundary | verified | Real `SIGKILL` at every `FAULT_POINTS` boundary; the source image is byte-identical until `publish:done` (`tests/test_writer_fault_coverage.py`). |
| Interrupted commit / malformed journal refusal | partial | Structural decoder refusal is tested; `e2fsck -fy` recovery on a disposable copy is re-verified with `e2fsck -fn` and the journal gate. |
| `e2fsck -f -n` after every writer operation | verified | `verify_image_after_operation()` runs after every libext2fs write and after delegated `create_file`, before atomic publication. |

## Block C — NTFS / `$LogFile`

| Item | Status | Evidence |
| --- | --- | --- |
| `$LogFile` parser: RCRD, restart area, USA, redo/undo | implemented | `mountfile/ntfs_logfile.py`, `tests/test_ntfs_logfile_parser.py`. |
| Undo for OPEN transactions | partial | `_build_plans()` produces an undo plan from active transactions; `replay_analysis_to_overlay()` now applies it after committed redo to a private overlay with explicit targets. It is not an image writer. |
| Checkpointing and full redo | partial | Checkpoint LSN, transaction table and redo plan are parsed; the bounded executor applies redo/undo in recovery order, but `executable` stays `False` until MFT/runlist resolution and crash evidence exist. |
| Safe MFT allocation/reuse and `$MFTMirr` repair | not implemented | The gate only compares `$MFT`/`$MFTMirr` record 0 and refuses a mismatch. |
| Replay on copied Windows/NTFS crash fixtures | not implemented | Only the read-only fixture exists; dirty/crashed fixtures are not available. |
| Quarantine for unproven replay | implemented | `recover="copy"` writes a content-addressed copy plus JSON marker and raises `NtfsReplayQuarantined`; tests in `tests/test_native_journal_writers.py`. |
| `ntfsfix` is secondary, not authoritative | verified | `probe_ntfs_log()` reads `$Volume` and `$LogFile` structures; it never trusts a command exit code. |

## Block D — acceptance matrix (#387)

| Item | Status | Evidence |
| --- | --- | --- |
| Failure injection after each step | partial | Runtime fault seam covers copy/helper/mutation/publish; per-metadata-step injection is not complete for every profile. |
| Process-kill for each transaction phase | partial | ext4 has real process-kill coverage; other writers keep their existing tests. |
| Hash preservation on failure | verified | ext4 tests assert the source hash is unchanged; NTFS staged writes use the same source-identity guard. |
| Source identity / concurrent modification | verified | SHA-256 is captured before staging and re-checked before atomic replace in ext4 and NTFS writers. |
| Independent checker + read-back per operation | partial | ext4 uses `e2fsck -fn` plus journal gate after every operation; NTFS uses the `$LogFile` gate but has no replay checker. |
| CI artifact profile × operation × state × checker | implemented | `scripts/recovery_matrix.py` emits explicit operation/state/checker cells with typed `pass`, `unavailable`, and `not_proven` statuses; `docs/generated/recovery-matrix.json` is verified against the generator. |
| Unsupported cells as typed refusals | partial | Capability contracts and NTFS quarantine are typed; not every profile has an end-to-end assertion. |

## Block E — parity and status

| Item | Status | Evidence |
| --- | --- | --- |
| #389 cross-platform fixtures | open | No new Windows/macOS replay fixtures in this change. |
| #390 README/status CI gate | open | The trusted generated-status gate remains unchanged; README still reports ext3/ext4 and NTFS as read-only. |

## Conclusion

Replay must not be declared supported. The remaining blockers are the NTFS
image-level executor (MFT allocation, `$MFTMirr`, indexes), full JBD2
checksum/checkpoint integration in the writer, and real NTFS crash fixtures with
an independent checker. Until then the writers stay fail-closed, and dirty NTFS
volumes are quarantined rather than modified.
