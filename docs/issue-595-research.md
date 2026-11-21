# Issue #595 research snapshot

Source: https://github.com/Alexkkkkk/mount-file/issues/595

Issue title: Аудит состояния репозитория на 2026-11-21 (main @ 96bda60, v1.100.1).

The issue identifies these implementation priorities:

1. #389: add a macOS runner to the evidence job; the issue says no macOS runner exists in any workflow.
2. #390: require the repository status verification check on `main` and trigger the gate on `pull_request`.
3. #530 P1: add at least five dirty NTFS fixtures and chkdsk reference evidence.
4. #385: apply the checkpoint plan in the writer path and enable `allow_checksums=True` only after the safety gate is complete.
5. #387: obtain two clean nightly matrix runs before claiming closure.

The issue explicitly says none of the eight tracked issues is ready to close. External Windows/macOS evidence and scheduled-run history cannot be fabricated by local code; implementation should remain fail-closed and product-level ext3/ext4/NTFS writes must not be marked supported without independent evidence.

Current public repository main after fetch: 6c07459 (`docs(issue-510): publish 2026-11-21 tracker audit and NTFS oracle spec (#596)`).
