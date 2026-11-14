# $LogFile on-disk layout verified against Linux `fs/ntfs3/fslog.c`

> **Implementation boundary:** this document specifies the read-only, MIT
> implementation in `mountfile.ntfs_logfile`. It is not an image-level replay
> implementation. `NtfsReplayOverlay` applies a plan only to a private in-memory
> byte buffer when the caller has already resolved every target; it never opens
> an image for writing and does not guess MFT/attribute/runlist locations.
> The external `dfir_ntfs` project is GPL-3.0 and is therefore not vendored or
> imported into this MIT project; it is also a parser rather than a complete
> NTFS metadata mutation engine. Dirty-log replay remains `unavailable` and is
> rejected fail-closed until an independently licensed executor has complete
> acceptance evidence.

Source of truth: <https://raw.githubusercontent.com/torvalds/linux/master/fs/ntfs3/fslog.c>
(fetched 2026-11-14, line numbers below refer to that file). Every offset in this
note is either taken from a struct comment in that file or confirmed by one of
its `static_assert` calls; nothing here is inferred from memory.

This note backs the bounded `$LogFile` analyzer in
`mountfile/ntfs_logfile.py` (issue #510 block C, issue #386). The analyzer parses
and plans; it does **not** mutate images --
execution stays gated on the acceptance evidence of issue #387.

## Restart page (`RSTR`)

`struct RESTART_HDR` (fslog.c:29-37):

| Offset | Field | Size | Note |
|---:|---|---:|---|
| 0x00 | signature `RSTR` | 4 | `NTFS_RECORD_HEADER` prefix |
| 0x04 | update-sequence-array offset | 2 | validated by `mountfile.ntfs_reader._fixup_logfile_page` |
| 0x06 | update-sequence-array count | 2 | must be `page_size / 512 + 1` |
| 0x10 | `sys_page_size` | 4 | system page, 4096 for the bounded profile |
| 0x14 | `page_size` | 4 | log page size used by this log |
| 0x18 | `ra_off` | 2 | restart area offset inside the page |
| 0x1A | `minor_ver` | 2 | supported pairs: (1,1), (2,0) |
| 0x1C | `major_ver` | 2 | |

Two restart pages exist at the start of the stream; the newer valid one wins
(`log_replay`, fslog.c:3959-3998). `mountfile.ntfs_reader.validate_ntfs_logfile_restart_pages`
already implements that choice and the client-array validation.

## Restart area

`struct RESTART_AREA` (fslog.c:56-71), relative to `ra_off`:

| Offset | Field | Size |
|---:|---|---|
| 0x00 | `current_lsn` | 8 |
| 0x08 | `log_clients` | 2 |
| 0x0A | `client_idx[2]` (free / in use) | 2+2 |
| 0x0E | `flags` | 2 |
| 0x10 | `seq_num_bits` | 4 |
| 0x14 | `ra_len` | 2 |
| 0x16 | `client_off` | 2 |
| 0x18 | `l_size` | 8 |
| 0x20 | `last_lsn_data_len` | 4 |
| 0x24 | `rec_hdr_len` | 2 |
| 0x26 | `data_off` | 2 |
| 0x28 | `open_log_count` | 4 |
| 0x40 | `clients[]` | `client_off` may point elsewhere |

`RESTART_SINGLE_PAGE_IO` is bit 0 of `flags`; the clean-dismount bit is
`0x0002` (`mountfile.ntfs_reader._LOGFILE_CLEAN_DISMOUNT`).

## Client record

`struct CLIENT_REC` (fslog.c:42-53), `static_assert(sizeof == 0xa0)`:

| Offset | Field | Size |
|---:|---|---|
| 0x00 | `oldest_lsn` | 8 |
| 0x08 | `restart_lsn` | 8 |
| 0x10 | `prev_client` | 2 |
| 0x12 | `next_client` | 2 |
| 0x14 | `seq_num` | 2 |
| 0x1C | `name_bytes` | 4 |
| 0x20 | `name[]` (UTF-16LE, "NTFS") | 128 |

The client array is walked through `client_idx[1]` -> `next_client`
(`oldest_client_lsn`, fslog.c:414-427); the NTFS client is found by comparing
`name_bytes == 8` and the UTF-16LE name "NTFS" (fslog.c:4247-4254).

## Record page (`RCRD`)

`struct RECORD_PAGE_HDR` (fslog.c:269-277) plus `struct LFS_RECORD`
(fslog.c:261-267):

| Offset | Field | Size |
|---:|---|---|
| 0x00 | signature `RCRD` (or `BAAD`) | 4 |
| 0x04 | USA offset | 2 |
| 0x06 | USA count | 2 |
| 0x08 | `rhdr.lsn` (page LSN) | 8 |
| 0x10 | `rflags` (bit0 = `LOG_PAGE_LOG_RECORD_END`) | 4 |
| 0x14 | `page_count` | 2 |
| 0x16 | `page_pos` | 2 |
| 0x18 | `record_hdr.next_record_off` (free-space offset) | 2 |
| 0x20 | `record_hdr.last_end_lsn` | 8 |
| 0x28 | `fixups[]` | 20 |
| 0x3C | `file_off` (major >= 2) | 4 |

Records start at `data_off` (0x40 for a 4 KiB page, already validated by
`parse_ntfs_logfile_restart_area`) and end at `next_record_off`.
`static_assert(offsetof(struct RECORD_PAGE_HDR, file_off) == 0x3c)` pins the
layout.

## Log record header

`struct LFS_RECORD_HDR` (fslog.c:245-255), `static_assert(sizeof == 0x30)`:

| Offset | Field | Size |
|---:|---|---|
| 0x00 | `this_lsn` | 8 |
| 0x08 | `client_prev_lsn` | 8 |
| 0x10 | `client_undo_next_lsn` | 8 |
| 0x18 | `client_data_len` | 4 |
| 0x1C | `client.seq_num` | 2 |
| 0x1E | `client.client_idx` | 2 |
| 0x20 | `record_type` | 4 |
| 0x24 | `transact_id` | 4 |
| 0x28 | `flags` | 2 |
| 0x2A | align | 6 |

`record_type`: `LfsClientRecord = 1`, `LfsClientRestart = 2` (fslog.c:235-236);
`flags`: `LOG_RECORD_MULTI_PAGE = 1` (fslog.c:257).

## Log record body

`struct LOG_REC_HDR` (fslog.c:73-88), `static_assert(sizeof == 0x20)`:

| Offset | Field | Size |
|---:|---|---|
| 0x00 | `redo_op` (NTFS_LOG_OPERATION) | 2 |
| 0x02 | `undo_op` (NTFS_LOG_OPERATION) | 2 |
| 0x04 | `redo_off` | 2 |
| 0x06 | `redo_len` | 2 |
| 0x08 | `undo_off` | 2 |
| 0x0A | `undo_len` | 2 |
| 0x0C | `target_attr` | 2 |
| 0x0E | `lcns_follow` | 2 |
| 0x10 | `record_off` | 2 |
| 0x12 | `attr_off` | 2 |
| 0x14 | `cluster_off` | 2 |
| 0x16 | `reserved` | 2 |
| 0x18 | `target_vcn` | 8 |
| 0x20 | `page_lcns[]` | 8 each |

**Redo and undo are two independent slices**: `redo_off/redo_len` and
`undo_off/undo_len` point into the same record body at different offsets with
different lengths. Using one payload for both (the v1 draft of issue #510) makes
an undo write redo bytes -- that is corruption, not recovery. The v2 draft
(`docs/ci-drafts/ntfs_logfile_replay_v2.py`) and this module keep them separate.

## Operations

`enum NTFS_LOG_OPERATION` (fslog.c:298-340): `Noop` 0x00,
`CompensationLogRecord` 0x01, `InitializeFileRecordSegment` 0x02,
`DeallocateFileRecordSegment` 0x03, `WriteEndOfFileRecordSegment` 0x04,
`CreateAttribute` 0x05, `DeleteAttribute` 0x06, `UpdateResidentValue` 0x07,
`UpdateNonresidentValue` 0x08, `UpdateMappingPairs` 0x09,
`DeleteDirtyClusters` 0x0A, `SetNewAttributeSizes` 0x0B,
`AddIndexEntryRoot` 0x0C, `DeleteIndexEntryRoot` 0x0D,
`AddIndexEntryAllocation` 0x0E, `DeleteIndexEntryAllocation` 0x0F,
`WriteEndOfIndexBuffer` 0x10, `SetIndexEntryVcnRoot` 0x11,
`SetIndexEntryVcnAllocation` 0x12, `UpdateFileNameRoot` 0x13,
`UpdateFileNameAllocation` 0x14, `SetBitsInNonresidentBitMap` 0x15,
`ClearBitsInNonresidentBitMap` 0x16, `HotFix` 0x17, `EndTopLevelAction` 0x18,
`PrepareTransaction` 0x19, `CommitTransaction` 0x1A, `ForgetTransaction` 0x1B,
`OpenNonresidentAttribute` 0x1C, `OpenAttributeTableDump` 0x1D,
`AttributeNamesDump` 0x1E, `DirtyPageTableDump` 0x1F, `TransactionTableDump` 0x20,
`UpdateRecordDataRoot` 0x21, `UpdateRecordDataAllocation` 0x22,
`UpdateRelativeDataInIndex` 0x23, `UpdateRelativeDataInIndex2` 0x24,
`ZeroEndOfFileRecord` 0x25.

`can_skip_action()` (fslog.c:358-378) lists the operations that carry no byte
payload: Noop, DeleteDirtyClusters, HotFix, EndTopLevelAction, Prepare, Commit,
Forget, CLR, OpenNonresidentAttribute and the four table dumps.

## Restart tables

`struct RESTART_TABLE` (fslog.c:95-106, size 0x18) is the header of the four
tables dumped into the log: open attribute table, attribute names, dirty page
table (`struct DIR_PAGE_ENTRY`, 0x20) and transaction table
(`struct TRANSACTION_ENTRY`, 0x28: `next`, `transact_state`, `first_lsn`,
`prev_lsn`, `undo_next_lsn`, `undo_records`, `undo_len`).
`struct NTFS_RESTART` (0x40, fslog.c:201-215) carries `check_point_start` and
the LSN of every table. `transact_state` is `TransactionUninitialized` 0,
`TransactionActive` 1, `TransactionPrepared` 2, `TransactionCommitted` 3
(fslog.c:181-186).

## Analysis pass rules that a replay must reproduce

From `log_replay` (fslog.c:4560-4870) and the undo pass (fslog.c:5200-5310):

1. The analysis pass starts at `checkpt_lsn`, taken from the newest client
   restart record's `NTFS_RESTART.check_point_start`
   (fslog.c:4261-4271). A zero checkpoint means "freshly formatted, nothing to
   do" (fslog.c:4517-4521).
2. For every `LfsClientRecord`, the transaction table entry for `transact_id`
   is created as `TransactionActive` with `first_lsn = rec_lsn`, and
   `prev_lsn = undo_next_lsn = rec_lsn`. If `undo_op == CompensationLogRecord`,
   `undo_next_lsn` is instead the record's `client_undo_next_lsn`
   (fslog.c:4608-4627).
3. `PrepareTransaction` -> `TransactionPrepared`; `CommitTransaction` ->
   `TransactionCommitted`; **`ForgetTransaction` frees the table entry**
   (fslog.c:4834-4846). A forgotten transaction is neither redone nor undone --
   ignoring `Forget` (the v1 draft) rolls back work that must not be rolled
   back.
4. `redo_lsn` is the lowest LSN found in the dirty-page and transaction tables
   (fslog.c:4870-4890).
5. Undo walks **only `TransactionActive` entries with a non-zero
   `undo_next_lsn`**; prepared and committed entries are freed without rollback
   (fslog.c:5204-5216). Within a transaction the records are read forward from
   `undo_next_lsn` (`read_next_log_rec`), and `undo_op == Noop` or
   `can_skip_action(undo_op)` records write nothing (fslog.c:5236-5270).
6. The bytes applied for undo are `undo_off/undo_len`, for redo
   `redo_off/redo_len` (fslog.c:5188-5191, 5272-5297).
7. `do_action` maps a record to volume bytes through
   `cluster_off << SECTOR_SHIFT` + `target_vcn << cluster_bits` and the record's
   `page_lcns[]` (fslog.c:3144-3152), and refuses when the target is sparse or
   the mapping is missing.

## LSN arithmetic

fslog.c:1119-1146 with `file_data_bits = 64 - seq_num_bits`:

- `lsn_to_vbo(lsn) = (lsn & ((1 << file_data_bits) - 1)) << 3`
- `vbo_to_lsn(offset, seq) = (offset >> 3) + (seq << file_data_bits)`
- `lsn_to_page_off(lsn) = ((u32)lsn << 3) & page_mask`
- `next_page_off(off) = ((off & ~sys_page_mask) + page_size) % l_size -> first_page`
- `base_lsn` bumps the sequence when a record offset falls before the page's own
  offset, which is how the ring wrap is recognised (fslog.c:1158-1180).
