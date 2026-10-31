# Техническое задание: полная реализация возможностей файловых систем MountFile

## 1. Цель

Реализовать в MountFile полноценные native readers и writers для всех заявленных файловых систем и их поддерживаемых вариантов без обязательной зависимости от `pytsk3`, `debugfs`, `ntfs-3g`, ImDisk, FUSE или других внешних filesystem CLI.

Под «полной реализацией» понимается не только распознавание и чтение отдельных образов, а корректная поддержка всех заявленных on-disk структур, feature flags, metadata layouts, журналов, recovery-состояний, каталогов, потоков, ссылок, атрибутов, sparse-файлов, больших файлов и операций изменения, которые допускает соответствующий стандарт и выбранный профиль.

Физические устройства, зашифрованные тома без ключа, proprietary-расширения и намеренно неподдерживаемые форматы должны быть явно перечислены как исключения, а не молча интерпретироваться.

## 2. Обязательные принципы

1. **Fail-closed.** Неизвестная feature, повреждённая metadata, невалидный checksum, dirty/recovery-required состояние или выход за границы образа должны останавливать операцию до первой записи.
2. **Точные границы.** Каждый offset, sector, block, cluster, extent, run и размер проверяется относительно выбранного filesystem extent и физического размера образа.
3. **Единый API.** Для всех native providers реализуются `list`, `stat`, `read`, `read-range`, `extract`, `put`, `mkdir`, `rename` и `delete` с едиными capability/status/refusal contracts.
4. **Отсутствие скрытого fallback.** Распознанный, но неподдержанный профиль не передаётся другому parser или writer. Compatibility backend включается только явным флагом и показывается пользователю.
5. **Транзакционность.** До commit создаётся проверяемое состояние восстановления; исходная identity образа перепроверяется; выполняются read-back, structural validation и независимая проверка.
6. **On-volume recovery.** Host snapshot не считается заменой filesystem journal. Для journaled filesystem запись разрешается только после реализации корректного ordering, checkpointing, replay, revoke и crash recovery.
7. **Безопасность по умолчанию.** Read-only режим включён по умолчанию. Запись требует явного выбора режима и проверенного capability profile.
8. **Никаких ложных гарантий.** Power-loss atomicity заявляется только после fault-injection тестов с остановкой процесса и проверкой восстановленного образа.

## 3. Полный API-контракт

### 3.1 Read API

Каждый native provider обязан поддерживать:

- открытие whole-image и filesystem extent с ненулевым partition offset;
- `list_dir(path)` с корректной сортировкой, типами объектов и metadata;
- `stat(path)` с размером, типом, timestamps, attributes и ссылками;
- `read_file(path)`;
- `read_file_range(path, offset, length)` с bounded arithmetic;
- `extract(path, destination)` с защитой от traversal, symlink escape и overwrite policy;
- корректные `close()` и `reopen()`;
- typed refusal codes для `unsupported`, `dirty`, `corrupt`, `out-of-bounds`, `unsafe`, `concurrent-change`;
- capability matrix с точными ограничениями профиля.

### 3.2 Write API

Для каждого filesystem profile по отдельности реализовать и описать:

- создание и замену regular file;
- создание каталога;
- переименование внутри одного каталога;
- переименование между каталогами;
- удаление файла;
- удаление пустого каталога;
- рекурсивное удаление только при явно включённой политике;
- длинные имена, Unicode и case-folding;
- timestamps, permissions, ownership, DOS/POSIX attributes;
- hard links, symbolic links и special files — либо корректно поддержать, либо явно отказать;
- sparse, compressed, encrypted и alternate data streams — только при полной проверке соответствующей структуры;
- размер файла 0, 1 byte, sector boundary, cluster boundary, block boundary и максимально допустимый размер;
- rollback каждой операции и всей транзакции.

Не поддерживаемые операции должны отказывать до изменения первого байта и возвращать машинно различимую причину.

## 4. FAT12/16/32

### Read

- корректный BPB и geometry validation;
- FAT12 packed entries, FAT16 и FAT32;
- mirrored FAT и active FAT semantics;
- backup boot sector;
- root directory special case FAT12/16;
- LFN checksum, Unicode conversion и short-name aliases;
- cluster chains, bad/free/reserved clusters и cycle detection;
- volume labels, timestamps, attributes и deleted entries policy;
- maximum file/cluster counts и overflow protection.

### Write

- поддержка FAT12, FAT16 и FAT32 отдельными профилями;
- atomic metadata update всех нужных FAT copies;
- directory slot allocation, LFN creation/removal и collision handling;
- free-cluster allocation и rollback при short write;
- FSInfo validation/update для FAT32;
- backup boot synchronization;
- failure injection после каждого FAT, directory, data и commit шага;
- независимая проверка копии через `fsck.fat`/`dosfsck` в read-only режиме.

## 5. exFAT

- main и backup boot regions с checksum и допустимыми различиями;
- `VolumeFlags`, `NumberOfFats`, active FAT и sector geometry;
- FAT, allocation bitmap и cluster heap consistency;
- `$UpCase` table, checksum и case-insensitive name comparison;
- полный directory entry-set parser: primary, stream extension, name entries, bitmap, upcase, label, vendor entries;
- entry-set checksum и строгий порядок entries;
- no-FAT-chain и обычные FAT chains;
- cycle, cross-link, free-cluster, overlong-chain и out-of-volume checks;
- ValidDataLength, DataLength, allocation length и sparse semantics;
- file/directory creation, replacement, move, delete и directory growth;
- Unicode names, case collisions и canonical lookup;
- staging, read-back, consistency validation и recovery marker;
- multi-FAT profiles только после отдельной реализации active-FAT semantics;
- независимая проверка через `fsck.exfat` или equivalent read-only oracle.

## 6. ext2

- rev0/rev1 superblock, group descriptors и all supported block sizes;
- direct, single, double и triple indirect blocks;
- directory growth across all indirect levels;
- directory indexing/hash trees where enabled;
- inode flags, file types, link counts and timestamps;
- regular files, directories, symlinks and hard links;
- sparse files and large files;
- block/inode bitmap allocation and counter updates;
- group descriptor backups and metadata consistency;
- transactional staging with independent `e2fsck -fn` verification;
- durable recovery protocol for process termination and interrupted commit;
- explicit quarantine state when recovery cannot be proven.

## 7. ext3/ext4 and JBD2

### Read and replay

- journal superblock and UUID validation;
- ring geometry, head/tail, sequence and wraparound;
- descriptor, data, commit and revoke records;
- transaction checksums and checksum feature variants;
- incomplete transactions and clean/dirty state;
- external journal rejection or complete support;
- fast commit records if the profile accepts them;
- 64-bit block numbers and descriptor sizes;
- metadata checksums for superblock, group descriptors, inode tables, bitmaps, directories, extents and journal records;
- extents with index/leaf bounds, depth, cycles, overlap, unwritten and preallocated extents;
- flex_bg, meta_bg, bigalloc, inline data, casefold and encryption gates;
- large files and 64-bit sizes;
- directory indexing and directory-tail checksums.

### Native writing

Native ext3/ext4 writing is allowed only after implementing:

- correct journal transaction construction;
- descriptor/data/commit ordering;
- revoke handling;
- checksum generation;
- checkpointing and superblock state updates;
- atomic allocation and inode metadata changes;
- crash recovery after every commit boundary;
- recovery from a power-loss simulation without an external host snapshot;
- independent verification with `e2fsck -fn` on a copy.

Until all items pass, ext3/ext4 must remain read-only.

## 8. NTFS

### Read

- boot sector and backup boot sector;
- MFT and `$MFTMirr` consistency;
- FILE record USA fixups and sequence/reference validation;
- resident and non-resident attributes;
- `$STANDARD_INFORMATION`, `$FILE_NAME`, `$DATA`, `$INDEX_ROOT`, `$INDEX_ALLOCATION`, `$BITMAP`;
- `ATTRIBUTE_LIST` extension records and cross-record reconstruction;
- runlists with signed deltas, VCN coverage, sparse/compressed/encrypted flags;
- directory B-tree traversal and `INDX` USA fixups;
- case-insensitive Unicode collation;
- alternate data streams;
- reparse points, hard links and junction policy;
- `$Volume` flags and clean/dirty state;
- `$LogFile` restart pages and transaction records;
- bounded reads for resident and non-resident data.

### Native writing

Implement a complete NTFS transaction layer:

- MFT record allocation and reuse;
- `$MFTMirr` updates;
- bitmap updates;
- directory index insertion/removal and B-tree split/merge;
- runlist allocation/deallocation;
- resident/non-resident conversion;
- `$ATTRIBUTE_LIST` updates;
- timestamps, links and parent references;
- alternate data streams and reparse points where supported;
- `$LogFile` redo/undo records;
- restart area update and checkpointing;
- transaction ordering and replay after interruption;
- rollback and quarantine when replay cannot be proven;
- validation against a Windows/NTFS oracle on copied fixtures.

Until `$LogFile` replay and recovery are complete, native NTFS writes must remain disabled.

## 9. Cross-platform and external adapters

- Linux, Windows and macOS CI for every supported Python version;
- real and synthetic fixture matrix for every filesystem/profile/operation;
- optional FUSE, WinFsp and macFUSE adapters separated from in-process VFS;
- adapters must expose mount read/write status and never upgrade a read-only native profile silently;
- driver and privilege failures must return typed refusals;
- no platform adapter may bypass native bounds or transaction checks.

## 10. Testing and acceptance

### Mandatory tests

- unit tests for every parser and writer primitive;
- property tests for ranges, runlists, extents, cluster chains, directory records and checksums;
- malformed/corrupt/truncated fixture matrix;
- no-fallback routing tests;
- image hash unchanged after every refused operation;
- failure injection at every write and commit boundary;
- process termination during each transaction phase;
- concurrent modification detection;
- rollback and quarantine tests;
- independent filesystem checker on a copied image;
- differential read tests against trusted OS/tool oracles;
- cross-platform CI tests;
- benchmark tests for list, stat, range-read, extraction and representative writes.

### Acceptance gate

Этап считается готовым только если одновременно выполнены все условия:

1. Все заявленные операции отражены в capability matrix.
2. Каждая поддержанная feature имеет positive и negative fixtures.
3. Dirty, corrupt, unsupported и recovery-required profiles fail closed.
4. Ни одна отказанная операция не изменяет image hash.
5. Все writers проходят read-back и независимый checker.
6. Process kill/power-loss simulation приводит к валидному восстановленному состоянию или безопасной quarantine-блокировке.
7. В CI нет скрытых `xfail`, необъяснимых `skip` или fallback, маскирующего отказ native backend.
8. Документация, GUI, CLI, VFS API и release artifacts показывают одинаковый статус.
9. Benchmark report воспроизводим на Linux, Windows и macOS.
10. Только после выполнения всех пунктов соответствующий filesystem profile может быть объявлен `production-ready`.

## 11. Результаты

Для каждого filesystem profile подготовить отдельный PR или независимый commit stack с:

- кодом reader/writer;
- capability/refusal integration;
- fixtures и tests;
- failure-injection suite;
- независимым validation report;
- benchmark report;
- обновлённой документацией;
- changelog и migration notes;
- ссылкой на CI runs и проверенные commit SHA.

Запрещено закрывать issue общим заявлением «полная поддержка» без перечисления реально реализованных feature flags, операций, ограничений и доказательств recovery.
