# План: conservative read-only ext3/ext4 profiles

Статус: только план, инварианты и рекомендации. Общая рабочая копия не изменялась, commit/push не выполнялись.

## 1. Цель и границы

Добавить в существующие `mountfile/ext2_reader.py` и `mountfile/vfs.py` два явно ограниченных профиля: `native-ext3-ro` для classic inode pointers + linear directories + JBD2 validation и `native-ext4-ro` для того же чтения с отдельно включаемыми extents и 64-bit/group layouts.

Оба профиля открывают image только `rb`, требуют явные `offset` и `volume_size`, не выполняют journal replay, не исправляют metadata и не имеют writer API. Любой неизвестный feature bit, dirty/recovery state, checksum mismatch, malformed layout, overflow или выход за выбранный extent означает fail-closed ошибку. Если обнаружен ext superblock, отказ native-профиля не должен незаметно переключаться на `pytsk3`; fallback допустим только для нераспознанных ext-образов или отдельного opt-in legacy режима.

## 2. Feature gating

Ввести централизованный immutable `ExtFeatureProfile` с наборами `compat`, `incompat`, `ro_compat`, layout capabilities и checksum capabilities. Все numeric masks должны находиться в одном модуле.

Базовый allowlist:

- `compat`: `EXT_ATTR` только при явной xattr policy; `RESIZE_INODE` только после проверки layout; `HAS_JOURNAL` только для ext3 и только после JBD2 validation. `DIR_INDEX`, `SPARSE_SUPER2`, `FAST_COMMIT` — reject.
- `incompat`: `FILETYPE` разрешать при строгом dirent parser. `RECOVER` — немедленный reject. `EXTENTS`, `64BIT`, `FLEX_BG`, `META_BG`, `EA_INODE`, `INLINE_DATA`, `ENCRYPT`, `CASEFOLD`, `LARGEDIR`, `MMP`, `CSUM_SEED` — отдельные capability profiles; до реализации — reject.
- `ro_compat`: `SPARSE_SUPER` и `LARGE_FILE` — базовые. `BTREE_DIR`, `HUGE_FILE`, `METADATA_CSUM`, `READONLY`, `PROJECT`, `VERITY`, `ORPHAN_FILE` — только при полной реализации; иначе reject. Для `METADATA_CSUM` default должен быть require-and-verify, не ignore.

Проверять `feature_* & ~allowlist == 0`, допустимость комбинаций, revision/inode size/block size и per-inode flags. Нельзя трактовать extent, inline, encrypted, verity, EA inode или compression flag как обычные block pointers.

Порядок включения: (1) ext3 classic clean journal; (2) ext4 classic с checksum verification; (3) ext4 extents; (4) ext4 64-bit; (5) flex_bg/meta_bg/large_dir/inline/encryption/fast commit отдельными профилями.

## 3. Superblock и geometry invariants

Читать superblock строго с `offset + 1024`, проверять exact read и magic. Все `add/mul/ceil_div` выполнять через checked arithmetic: отрицательные значения, wraparound и `end < start` запрещены.

Проверять block size `1024 << s_log_block_size`, `s_first_data_block`, low/high block count при 64-bit, `blocks_per_group`, `inodes_per_group`, `inode_size`, `desc_size`, ненулевое ограниченное число groups, filesystem byte size и все metadata ranges внутри `volume_size` и image size. Inode size должен быть степенью двойки, `>=128`, `<= block_size`; inode table entry обязан целиком помещаться в table. Проверять journal inode/external journal identifiers и UUID, backup superblock/GDT ranges и отсутствие пересечений с bitmap/inode/data.

Не смешивать filesystem-relative block numbers с image `offset`. Public read-range проверять на `0 <= offset <= file_size`, ограничивать maximum request. Short underlying read — ошибка, не EOF.

## 4. Block-group и inode layouts

Сделать отдельный `GroupLayout`, который возвращает проверенные 64-bit physical numbers для block bitmap, inode bitmap и inode table. Для 32-byte descriptors использовать только low words; для 64-bit использовать `s_desc_size` и high words. Никогда не читать high fields из короткого descriptor.

На каждую группу проверять group start/end, последний укороченный group, bitmap ranges, inode table extent, отсутствие пересечений с backup metadata и правильную bitmap allocation. Free/metadata blocks нельзя читать как file data. `flex_bg`/`meta_bg` не принимать по одному флагу: нужен отдельный placement algorithm и coverage tests.

Inode number проверять в `1..inodes_count`; inode читать целиком. Размер regular file собирать из low/high полей только с `LARGE_FILE` и корректным type. Symlink не follow-ить автоматически; inline/encrypted/verity/EA inode reject.

## 5. JBD2 state и recovery detection

Добавить отдельный bounded JBD2 reader; on-disk JBD2 поля — big-endian. Проверять magic `0xC03B3998`, block type 3/4, journal block size, `s_first`, `s_maxlen`, `s_start`, sequence, ring bounds, UUID и journal feature bits. Revoke, 64-bit, async commit, checksum v2/v3 и fast commit разрешать только после реализации, иначе reject.

`EXT*_INCOMPAT_RECOVER`, dirty `s_state`, error state и fast commit означают отказ: replay отсутствует. `jbd2.s_start == 0` сам по себе не доказывает clean journal. Требуется доказать пустой корректный ring по geometry/sequence/head/tail либо полностью просканировать ring и доказать отсутствие валидной незаписанной транзакции.

Любая descriptor/data/revoke sequence с commit record, которая не доказанно checkpointed, означает `RecoveryRequired`. Неполный transaction, повреждённый header, неизвестный block type или непроверяемый checksum также являются отказом. Не писать journal и не менять superblock. Native writes запрещены до реализации ordering, checkpointing, revoke handling, checksums и crash-injection tests.

## 6. Checksum invariants

Создать единую `checksum.py` с CRC32C Castagnoli и endian/seed helpers. Не заменять CRC32C обычным CRC32. Checksum field обнулять ровно по спецификации, длину брать из validated layout.

При `metadata_csum` проверять superblock; group descriptor (`UUID + group number + descriptor`); inode (`UUID + inode number + inode generation + inode bytes`); block/inode bitmap; extent blocks; directory checksum tail и htree checksum при поддержке htree. При legacy `gdt_csum` применять корректный CRC16 либо reject. `csum_seed` разрешать только после полной проверки layout. Journal checksum проверять по JBD2 endian/version rules; неизвестный type reject.

Любой mismatch — hard failure при open или первом чтении объекта. Нельзя возвращать partial read после checksum error и нельзя downgrade на pytsk3.

## 7. Extent mapping

Для `EXT4_EXTENTS_FL` не интерпретировать `i_block` как 15 pointers. Проверять extent header magic, `entries <= max`, bounded depth, sorted/non-overlapping index coverage, child ranges and cycles.

В leaf проверять logical start/length against file size, physical start low/high against block count, bitmap allocation and metadata exclusions. Unwritten extent возвращает zeroes без чтения physical blocks; holes — zeroes. Overlap, unsorted/cyclic/excessive-depth trees и bad checksum — reject. Extent checksum проверять до использования entries при metadata_csum.

## 8. Directory и file read contract

Сохранить bounded API: `list_dir`, `stat/file_size`, `read_file_range`, `read_file`, `close`, `reopen`; backend names `native-ext3-ro`/`native-ext4-ro`.

Для linear directories проверять `rec_len >= 8`, 4-byte alignment, record end inside block, `name_len <= rec_len - 8`, inode range/allocation, file type consistency, zero-progress loops, max entries/bytes, `.` и `..`, malformed tail и checksum tail. Htree/indexed directories reject до реализации hash/index parser и checksums.

Для file reads валидировать каждый mapped block; reject directory-as-file и unsupported special types. Enforce range/allocation limits, no follow symlink, no read outside `[offset, offset + volume_size)`. Return short data only at logical EOF.

## 9. VFS routing recommendation

Routing должен сначала классифицировать image, затем выбрать один closed profile. Если ext magic валиден: supported profile -> native provider; unsupported feature/layout/checksum/recovery/dirty -> `TskError` с причиной; невалидный ext profile не должен уходить в `pytsk3-fallback`. Non-ext images могут сохранить legacy routing. `probe_ext_profile` остаётся informational; provider повторяет все проверки.

## 10. Test plan

### Synthetic fixtures

Добавить fixtures для clean ext3 с empty internal JBD2; dirty state/`RECOVER`; descriptor+data+commit; incomplete transaction; invalid JBD2 magic/type/sequence/ring; UUID mismatch; unsupported journal features/checksum type; valid/invalid superblock, group descriptor, inode, bitmap, extent и directory-tail checksums; ext4 large file; extent holes/unwritten/overlap/bad child/depth/cycle; 64-bit numbers и descriptor sizes 32/64/invalid; flex_bg/meta_bg/bigalloc/inline/encryption/fast-commit reject; malformed group layout; truncation and partition sentinels; malformed dirents; range overflow, metadata overlap and short reads.

Каждый negative fixture должен проверять конкретную fail-closed category и доказывать, что fallback provider не был открыт.

### Real-image matrix

Если доступны `mke2fs`, `tune2fs`, `debugfs`, `e2fsck`, создавать temporary images для ext2 rev0/1 и 1/2/4 KiB, clean/recovery ext3, ext4 classic/extent/64bit/metadata_csum и unsupported combinations. Сравнивать native reads с trusted read-only oracle на listing, nested directories, sparse files, boundary ranges and large files. Repair tools использовать только на копиях и в non-repair mode.

### Fuzz/property tests

Мутировать superblock, descriptors, journal blocks, inode pointers, extents and dirents. Гарантии: нет uncaught `struct.error`, `OverflowError`, `MemoryError` или infinite loop; нет reads outside image/volume; нет write syscall/subprocess; malformed input даёт bounded result или designated fail-closed error. One-bit checksum mutations всегда reject.

### VFS regressions

`pytsk3=None` не влияет на accepted native reads; rejected ext profiles не downgrade; non-ext behavior сохраняется; partition sentinels/hash неизменны; public API и maximum-read limits стабильны.

## 11. Benchmark и compatibility criteria

Добавить воспроизводимый benchmark для `list`, `stat`, bounded `read-range`, `extract` и будущего `write` против текущего `pytsk3` baseline на одинаковых images/offsets. Фиксировать image size, block size, cache state, entry count и file sizes. Цель — не более 20% regression на описанном workload; превышение требует документировать bottleneck.

## 12. Cross-cutting safety gates будущих writers

Перед commit проверять исходный image hash, size, mtime/identity и concurrent modification. Использовать bounded staging, rollback и read-back validation. Fault injection обязана покрывать short write, I/O exception, cancellation, concurrent modification и crash points. При неудачном rollback image оставить закрытым для записи/quarantined и сообщить о backup recovery. Native writer не запускает subprocess и не зависит от filesystem driver.

FUSE/WinFsp/macFUSE держать отдельным optional adapter, не смешивать с in-app VFS. Перед любым repair использовать временную копию и non-repair checker. Для текущего read-only этапа добавить проверки отсутствия write syscalls/subprocess, сохранности исходного image hash и невозможности открыть rejected profile через compatibility fallback.

## 13. Exit criteria до writer work

Read-only phase complete only when every accepted feature maps to a tested capability, all enabled checksums verify, JBD2 classification is deterministic, extents/64-bit have dedicated bounds tests, accepted real images pass differential tests, benchmark is recorded, and negative matrix proves no fallback on dirty/unknown/checksum/recovery input. До этого ext3/ext4 writer methods не добавлять и не expose-ить.

## References

[1]: https://docs.kernel.org/filesystems/ext4/journal.html "Linux kernel ext4 JBD2 journal documentation"
[2]: https://docs.kernel.org/filesystems/ext4/checksums.html "Linux kernel ext4 checksum documentation"
[3]: https://man7.org/linux/man-pages/man5/ext4.5.html "ext4 filesystem features manual"
[4]: https://github.com/Alexkkkkk/mount-file/pull/186 "MountFile native filesystem VFS integration PR"
[5]: https://github.com/Alexkkkkk/mount-file/issues/182 "MountFile issue 182"
