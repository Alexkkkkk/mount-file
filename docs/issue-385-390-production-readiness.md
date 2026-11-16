# Production readiness: issues #385–#390

Status snapshot: 2026-11-16, based on `main` at `0559287`. This note supersedes older design snapshots that predate the current bounded ext4/JBD2 and NTFS replay paths. It records implementation status, not product-support approval.

## Implemented and tested mechanisms

`TransactionalWriter` rechecks source-image identity around staging and before publication. A concurrent or external change prevents publishing the staged image. The ext4/JBD2 code includes checksummed journal parsing, bounded replay/checkpointing, staged native writes, semantic read-back, and fail-closed handling for unsupported or malformed states. The focused ext4/JBD2 suite reported 112 passing tests on the base revision.

The NTFS code includes `$LogFile` parsing and bounded replay for a limited set of operations under `recover="native"`. Replay is performed on a disposable copy and gated before publication. The focused parser/replay suite reported 87 passing tests on the base revision. The updated failure-matrix artifact is based on executed JUnit node IDs; test results, checker results, and acceptance claims are separate fields.

## Not established

Ext4/JBD2 and NTFS native writes are not production-supported. Product-facing capability status remains read-only for ext3/ext4 and NTFS. Process-kill coverage at selected staged-writer boundaries does not establish power-loss durability or complete per-metadata-operation coverage.

Synthetic NTFS fixtures are not Windows crash captures. `ntfsfix -n` is auxiliary and does not establish Windows/`chkdsk` parity. No byte-level `$MFT`/`$BITMAP` comparison against Windows `chkdsk`-recovered images is available. A manual failure-matrix workflow run does not satisfy the two-consecutive-scheduled-nightly criterion.

The main-push read-only VFS evidence includes Linux, Windows, and macOS; PR/merge-queue fast runs use Linux and Windows. Neither matrix establishes independent filesystem parity. The required `Verify (status, secrets, versions, tests)` context remains in branch protection; this change does not add the separate `status-sync.yml` check or alter branch-protection settings.

Until independent acceptance evidence closes the remaining criteria, do not describe the experimental/bounded writers as production-ready or change the product-level read-only status.