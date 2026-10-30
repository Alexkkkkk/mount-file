# Issue #182: follow-up notes

This note records the user-supplied recommendations for continuing the
read-only filesystem work tracked in [issue #182](https://github.com/Alexkkkkk/mount-file/issues/182).
The project names below are leads from that material, not an independently
verified compatibility or security review. No implementation code from those
projects is copied by this change.

## Changes in this follow-up

- The ext superblock profile exposes and validates the declared internal JBD2
  journal inode. The ext3 reader uses that inode rather than assuming inode 8.
- Successful read-only journal replay invalidates metadata caches so later
  reads observe the replay overlay consistently.
- JBD2 transaction targets that overlap physical data blocks of the journal
  inode are rejected in both clean scans and explicit read-only replay.
- The bounded NTFS reader validates allocated, data and initialized sizes,
  rejects overlapping attribute-name regions, and returns zeroes beyond the
  initialized-size boundary.
- Regression tests exercise the declared-journal path, cached-bitmap overlay,
  NTFS size invariants, zero-fill behavior and malformed attribute ranges.

## Suggested follow-up backlog

1. Build self-contained Python filesystem-image fixtures/builders where they
   reduce dependence on optional tools such as `mke2fs`; keep real-image tests
   as additional integration coverage when those tools are available.
2. Add property-based tests and a fuzzing corpus for bounded on-disk parsers.
   Seed cases should include valid structures and malformed boundary cases;
   fuzzing complements, but does not replace, deterministic regressions.
3. Expand ext4 support only as separately gated read-only profiles with explicit
   geometry, checksum, bounds and malformed-image tests. Extents, inline data,
   64-bit layouts, `flex_bg`, `meta_bg` and indexed directories are distinct
   formats/features, not a safe blanket extension of the current ext2 reader.
4. Treat NTFS writing as a separate future project: design a plan-then-commit
   transaction, dry-run reporting, independent post-write verification and
   image/raw-device boundaries before enabling any mutation. Current NTFS
   writes remain disabled.
5. Review license compatibility before reusing code or test corpora from
   third-party projects. The supplied notes specifically flag GPL-licensed
   material; this change adopts no third-party implementation code.
6. Consider broader NTFS timestamp normalization and partition-selection
   behavior as separate, testable work items rather than coupling them to the
   current metadata-hardening changes.

The supplied comparison mentions ext2py, dfir_ntfs, checkdisk, FSParser,
python-ext4, data-recovery, RPTester and fshammer as possible references for
architecture, test generation, fuzzing, transaction design and concurrency
testing. Their current implementations and licenses should be checked before
using them as technical or legal sources.
