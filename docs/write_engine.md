# MF-WE — движок записи (ТЗ MF-WE-TZ-001)

`mountfile/write_engine.py` — транзакционный фасад над RW-бэкендами
`mountfile/universal_fs_rw.py` (native FAT12/16/32 и ограниченный clean ext2
profile; явный library-level legacy ext2/3/4 compatibility backend через
`debugfs`; NTFS через системный/mount-helper backend). Native ext2 — первая фаза
собственной записи; GUI и `WriteEngine` не переключают ext3/ext4 автоматически
на внешний writer до отдельной journal/recovery-aware реализации (issue #182).
Новый NTFS boot-sector probe и FILE-record parser ниже только проверяют
метаданные и не переключают существующий backend, не предоставляют полное
чтение файлов и не разрешают запись.

## Что гарантирует MF-WE

| Требование ТЗ §8 | Реализация |
|---|---|
| RO по умолчанию | `WriteEngine(image)` — все мутации бросают `ReadOnlyError`; запись только при `read_only=False` |
| Backup перед записью | снапшот сессии + снапшот транзакции (ротация `keep_snapshots`, по умолчанию 3) |
| Транзакции с журналом | `EngineJournal`: JSONL с полями `prev`/`hash`; `verify_journal()` пересчитывает цепочку |
| Verify после commit | `verify()`: размер образа, структурные проверки BPB/суперблока; ext2 writer проверяет staged metadata после каждой мутации, независимый `e2fsck` для него не запускается |
| Откат | `rollback()` возвращает образ к состоянию на старте транзакции побайтово; `restore_from_backup()` — к снапшоту |
| Dry-run | `dry_run=True`: проверяются пути и предусловия, ни один байт образа не меняется |
| Запрет записи в повреждённые образы | `damage_report()` + `DamagedImageError`; обход только явным `allow_damaged=True` |

Проверки, которым нужен внешний инструмент, при его отсутствии возвращают
`ok=None` (неизвестно), а не «прошло» — `verify()` не выдаёт непроверенное
за проверенное.

## Native exFAT read-only profile

VFS can browse a validated exFAT image without `pytsk3` when an explicit
partition extent is supplied. The bounded provider supports directory listing,
file sizes, bounded range reads and extraction through the common read API. It
currently accepts only single-FAT volumes; multi-FAT/active-FAT profiles are
rejected rather than interpreted using the first FAT. A range read fetches only
the clusters and byte spans intersecting the requested range. The provider
validates the main and backup boot regions, cluster chains and checksummed file
entry sets. Opening the provider validates the allocation bitmap and the
checksum-protected, full-range `$UpCase` table (compressed or uncompressed).
The same volume-specific up-case mapping is used for `NameHash` verification,
path lookup and writer-created or renamed entries. The allocation bitmap is
authoritative: clusters traversed in FAT chains and `NoFatChain` contiguous
streams must be marked allocated, while FAT entries for `NoFatChain` streams
are ignored as required by exFAT. The reader never writes the image. Mutations
are provided separately through the explicitly bounded `NativeExfatRW` writer
described below.

### Native exFAT phase-one writes

`UniversalFSRW` exposes `native-exfat-phase1` only for an explicitly bounded
volume. It supports file creation and replacement, directory creation and
growth, same- and cross-directory rename, and file or recursive directory
deletion. Before freeing clusters it scans allocation ownership and rejects
cross-linked or inconsistent streams. Each operation copies the complete image
to a staging file, validates the staged image through the native reader, checks
the full source-image size and SHA-256 under the native exFAT image lock, and
commits with an atomic replacement only if that identity is unchanged. This
detects changes both inside and outside the selected exFAT extent during
staging, preserving concurrent external changes instead of replacing them with
an older staged copy. The lock is advisory; an unrelated writer that ignores it
can still race in the final identity-check/replacement window. This is not a
power-loss recovery protocol. A rejected commit, short write or failed read-back
discards the stage; unsupported or ambiguous metadata still fails closed.

## Использование

```python
from mountfile.write_engine import WriteEngine

engine = WriteEngine("dump.img", read_only=False)
with engine.transaction("добавить заметку"):
    engine.mkdir("/EVIDENCE")
    engine.write_file("/EVIDENCE/NOTES.TXT", b"collected")
    engine.rename("/EVIDENCE/NOTES.TXT", "/EVIDENCE/FINAL.TXT")
report = engine.verify()
assert report["ok"], report["problems"]
engine.close()
```

Любое исключение внутри `with engine.transaction(...)` откатывает транзакцию
целиком. Одиночный вызов без явной транзакции оборачивается в неё автоматически.

## Границы

* FAT12/16/32 — bounded pure-Python readers `native-fat12-ro`,
  `native-fat16-ro` и `native-fat32-ro` выбираются через unified VFS без
  `pytsk3`. Они проверяют BPB-геометрию, FAT12 packed entries, цепочки
  кластеров и LFN checksum. Запись FAT32 остаётся отдельным writer-ом
  `mountfile/fat32.py`; FAT12/16 через этот VFS намеренно read-only. Native
  reader также отклоняет рассинхронные mirrored FAT copies и повреждённую
  backup BPB FAT32 до чтения файлов.
  Статус read parity (issue #182): подтверждена differential тест-матрицей
  в `tests/test_fat_reader.py` — для всех трёх вариантов FAT прогоняются
  valid- и invalid-fixture (синтетические образы со short-name, LFN, пустым,
  многокластерным файлом и вложенным каталогом), а результаты
  list/stat/read-range/extract native reader сравниваются с независимой
  референс-реализацией FAT внутри тестов (`pytsk3` и `mkfs.vfat` в тестовом
  окружении недоступны, поэтому oracle — собственная минимальная
  reference-читалка, не разделяющая код с `mountfile.fat_reader`). Повреждённые
  профили (битая boot signature, усечённый образ, цикл или свободная ссылка
  в цепочке кластеров) завершаются явным отказом; routing-тесты дополнительно
  фиксируют, что bounded FAT-профили всегда обрабатываются native reader-ом
  и legacy `pytsk3` fallback не вызывается.
  Статус write parity: native WRITE parity для FAT12/16 НЕ заявляется —
  у этих вариантов нет write path вовсе (read-only); это честное ограничение,
  а не скрытый fallback. FAT32 writer (`mountfile/fat32.py`) остаётся отдельным
  bounded-движком и также не претендует на паритет с системными средствами
  записи.
  Writer принимает только mirrored FAT32 mode с идентичными FAT copies.
  Non-mirrored mode и рассинхронные копии отклоняются до mutation, потому что
  текущий writer обновляет все FAT copies одновременно и не поддерживает
  active-FAT semantics.
  Если BPB объявляет backup boot sector, он должен быть байт-в-байт идентичен
  primary boot sector; повреждённая резервная BPB отклоняется до записи.
* Чистый ext2 с allowlisted features (1/2/4 KiB blocks, без журнала, direct и
  один single-indirect pointer для данных файла и каталогов) использует
  собственный Python writer без `debugfs`; предел файла и каталога —
  `12 + block_size/4` data blocks, без sparse/double/triple indirect. При
  расширении каталога pointer block, bitmap/counters и directory map проходят
  read-back до commit. Unsupported/dirty/out-of-bounds volumes не получают native RW.
  Перед изменением image writer сохраняет и fsync-ит проверенный before-image
  выбранного ext2 extent рядом с образом, затем атомарно публикует
  `<image>.mountfile-ext2-recovery.json` marker и fsync-ит родительский каталог.
  После внезапного завершения процесса следующий native writer сверяет identity
  image и SHA-256 snapshot, восстанавливает только выбранный extent, делает
  read-back и удаляет marker до snapshot. Process-crash tests покрывают
  прерывание commit, повторного recovery и завершённый commit до удаления marker.
  При обычном отказе rollback marker
  дополнительно привязывается к точному post-failure extent и mtime; любое
  последующее изменение блокирует автоматическое восстановление. Не редактируйте
  image и не удаляйте sidecar-файлы, пока recovery не завершён. Это не заменяет
  backup и не является универсальной power-loss гарантией для всех файловых
  систем, устройств и ОС;
  directory fsync применяется там, где ОС его поддерживает.
  Для ext2 вне этого профиля и ext3/ext4 автоматического fallback нет: legacy
  `debugfs -w` доступен только прямому library caller с
  `allow_legacy_debugfs=True`, с явным `volume_size` для каждого nonzero offset.
  Он staging-копирует и commit-ит ровно filesystem extent; `chmod`/`chown`/
  `symlink` остаются за этим compatibility API.

* NTFS — native bounded reader is available through the VFS, but native NTFS
  writes are not implemented. The `UniversalFSRW` facade refuses NTFS writes by
  default; the old `ntfs-3g`/ImDisk compatibility backend is reachable only with
  the explicit `allow_legacy_mount=True` option. This compatibility path is not
  the issue #182 native writer and retains its driver/privilege requirements.

* exFAT — через ImDisk и системный Windows filesystem driver на
  Windows (те же требования установки и прав); на Linux — через `LoopMountRW`,
  если доступны `mount`/`umount` и соответствующий драйвер. HFS+ и другие
  неподдерживаемые Windows filesystem drivers остаются read-only.
  Имена детектора (`FAT16`, `FAT32`, `HFS+`) нормализуются для mount-helper.
* ISO9660, SquashFS, JFFS2 и UBIFS объявлены `read-only-format`: чтение и
  извлечение доступны, но мутации отклоняются до монтирования. Эти форматы
  нельзя честно сделать обычными RW-ФС без пересборки образа по их спецификации.
* `Fat32RW` receives and enforces the selected volume extent. It rejects a BPB
  whose declared size exceeds that extent and checks every relative read/write
  against the declared filesystem boundary. Partition-table extents are used
  when available; signature-scan hits (including FAT backup boot sectors) are
  never treated as partition ends. At nonzero offsets without an authoritative
  partition-table size, a caller must supply `UniversalFSRW(..., volume_size=...)`.
* `UniversalFSRW` is default-deny for unsupported profiles. System/mount-based
  fallback is disabled unless a caller explicitly opts into
  `allow_legacy_mount=True`; a recognized filesystem declined by its native
  provider is never silently redirected to a different writer.
  `UniversalFSRW.capability_status()` and `FSInfo.write_capabilities` report the
  exact bounded mutations of the selected backend. Native ext3/ext4 and NTFS
  writes fail with an explicit unsupported-profile reason; they do not invoke a
  subprocess or fall back to a legacy writer. A read-only backend reports an
  empty write capability list and a human-readable rejection reason.
* Если backend, драйвер или права отсутствуют, операция завершается явной
  ошибкой; фасад не сообщает об успешной записи после молчаливого RO-mount.
* Вне области действия: физические устройства, таблицы разделов, шифрованные
  тома, спарс-файлы/сжатие/EFS.

## Ext-профиль и native ext2 read-only VFS

`mountfile.vfs.probe_ext_profile(image_path, offset=0, volume_size=None)` —
ограниченный pure-Python probe первичного 1024-байтового superblock ext2/3/4.
Он возвращает feature-derived классификацию, геометрию и флаги состояния,
наличия журнала и необходимости recovery. Для деталей feature flags и
superblock state см. [Linux ext4 superblock specification](https://docs.kernel.org/filesystems/ext4/super.html)
и [JBD2 journal documentation](https://docs.kernel.org/filesystems/ext4/journal.html).

Сам probe не является файловым reader: он не обходит каталоги и не читает
содержимое файлов. Отдельный `mountfile.vfs.open_native_ext2(image_path,
offset=0, volume_size=...)` открывает pure-Python `NativeExt2Image` для явного
чтения ограниченного ext2 subset. Provider проверяет границы раздела, group
descriptors, inode/block allocation bitmaps, linear directory records и
direct/single/double/triple-indirect block maps. Он требует явный `volume_size`
и открывает образ только для чтения. Один вызов чтения ограничен 16 MiB;
экспорт файлов использует 1 MiB ranged chunks, чтобы не собирать большие файлы
целиком в памяти.

Native reader принимает только clean ext2 revision 0/1 с 1/2/4 KiB blocks и
allowlisted features. Dirty/recovery-required volumes, JBD2/ext3, ext4 layout
features, unknown feature flags, indexed directories, compressed/encrypted/
extent/inline inodes и symlink traversal отклоняются. `open_image_provider`
автоматически выбирает этот read-only backend в GUI, CLI и MCP только при наличии
точного размера раздела (или whole-image ext2 без таблицы разделов); остальные
профили не становятся автоматически читаемыми через этот provider. Распознанный,
но неподдержанный ext profile отвергается; compatibility fallback на `pytsk3`
возможен только для нераспознанного формата и при явном opt-in. Это не разрешение
на запись и не FUSE export. `phase1_ext2_profile_candidate` по-прежнему означает
только предварительное соответствие write profile. Writer обязан самостоятельно
проверять затрагиваемые группы, bitmaps, inode metadata и partition bounds.

Ограниченный `NativeExt3Image` проверяет объявленный в superblock номер internal
JBD2 journal inode и читает именно этот inode; external journal devices остаются
запрещены. Активный журнал по умолчанию отклоняется. Только явный
`replay_journal=True` включает read-only overlay для поддерживаемого классического
descriptor/data/commit формата; после публикации overlay metadata-кэши очищаются,
чтобы дальнейшие reads видели согласованное представление. Это ограниченная
проверка/просмотр, не полноценное JBD2 recovery и не разрешение на запись.

## Ограничения и риски

* Native FAT mutations enforce the selected extent and use a same-directory
  full-image staging copy, sidecar writer lock, source-hash guard, independent
  read-back, atomic replace, and rollback copy. A failed rollback quarantines
  the image and retains the rollback file for manual recovery. Staging requires
  free space for two additional image copies. The commit is atomic at file
  replacement, not a guarantee against storage hardware failure; use a
  disposable working copy, never an original evidence image. Native ext2 writes
  remain limited to the clean allowlisted profile and are not power-loss-atomic;
  ext3/ext4 and unsupported ext2 profiles remain read-only.
  Native NTFS metadata browsing requires its bounded boot/MFT/USA, volume-state,
  restart-area and directory-index validations. It remains read-only: log replay,
  attribute-list streams, unsupported runlists and NTFS mutations are not enabled.

  GUI и `WriteEngine(read_only=False)` используют тот же `UniversalFSRW`,
  поэтому это не только read/extract режим.
* Полный набор fuzz/crash-тестов из §9.1 ТЗ (Atheris/hypothesis, ≥10 000
  итераций, `kill -9` на каждом шаге) **не выполнен** — реализованы
  детерминированные crash/rollback-сценарии в `tests/test_write_engine.py`.
* Покрытие строк/ветвей из §9.3 (≥85% / ≥75%, транзакционный слой ≥95%) на
  текущем прогоне не подтверждено для всего пакета.

## NTFS bounded native read profile

`mountfile.vfs.probe_ntfs_profile(image_path, offset, volume_size)` вызывает
pure-Python probe первичного 512-байтового NTFS boot sector. Он требует явную
внешнюю границу образа/раздела и проверяет OEM/signature, bytes-per-sector,
sectors-per-cluster, объявленный размер тома, расположение `$MFT`/`$MFTMirr` и
кодирование размеров MFT/index records. Автопоиск томов использует тот же parser,
чтобы отвергать NTFS-подобные случайные boot sectors и геометрию вне образа.
Разметка полей и кодирование размеров сверены с upstream Linux NTFS3 [структурами
on-disk](https://raw.githubusercontent.com/torvalds/linux/master/fs/ntfs3/ntfs.h)
и [проверками геометрии](https://raw.githubusercontent.com/torvalds/linux/master/fs/ntfs3/super.c).

`mountfile.vfs.probe_ntfs_profile` remains a geometry-only probe. The native
read-only provider `open_native_ntfs_metadata(image_path, offset, volume_size)`
requires the exact selected volume extent and validates the primary and backup
boot sectors, `$MFT`/`$MFTMirr`, FILE-record USA fixups, bounded attribute streams
and runlists, `$Volume` dirty flags, and clean `$LogFile` restart pages before
exposing a tree. It supports list/stat/read-range/extract through the existing
VFS; it does not replay the journal or authorize writes.

For nonresident attributes the reader retains and checks the on-disk allocated,
data and initialized sizes. Bounded reads expose zero bytes after `initialized_size`
up to `data_size`, without returning stale physical bytes from the allocated run.

The reader validates resident and nonresident metadata records, parent/sequence
references and bounded `$I30` index roots/allocation blocks. It supports valid
Unicode names for listing. If MFT record 10 exposes the exact-size, bounded
unnamed `$UpCase` DATA stream, the reader validates and uses its 65,536-entry
UTF-16 mapping, including fixed-point and ASCII invariants, for case-insensitive
path lookup; otherwise Unicode lookup is exact-case only. Resident
`$ATTRIBUTE_LIST` values are now decoded with bounded entry lengths, 8-byte
alignment, UTF-16 names and MFT references. Type order, ASCII name/VCN order and
duplicate keys are checked; Unicode collation uses a validated `$UpCase` table
when available and is not guessed when absent. The reader still refuses extension-
record resolution and nonresident attribute-list streams, so it cannot mistake a
partial attribute for a complete stream. The entry layout follows [Microsoft's
ATTRIBUTE_LIST_ENTRY definition](https://learn.microsoft.com/en-us/windows/win32/devnotes/attribute-list-entry)
and the [Linux NTFS on-disk reference](https://flatcap.github.io/linux-ntfs/ntfs/attributes/attribute_list.html).
The reader also rejects malformed indexes, cycles, sparse/compressed/encrypted
data and invalid path components. Native NTFS writes remain disabled; the
system-driver path is a separate explicit compatibility choice, not a substitute
for `$LogFile`-aware native transactions.
