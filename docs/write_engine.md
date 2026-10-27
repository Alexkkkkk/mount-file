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
file sizes, range reads and extraction through the common read API. It validates
the main and backup boot regions, FAT geometry, cluster chains and file-entry
sets, but never writes the image. `put`, `mkdir`, rename and delete remain
unsupported until allocation bitmap, upcase table and transactional metadata
updates have independent validation and rollback coverage.

### Native exFAT phase-one writes

`UniversalFSRW` exposes `native-exfat-phase1` only for an explicitly bounded
volume. It can replace an existing regular file without growing its allocated
cluster chain and rename an existing entry within the same directory. Each
operation copies the complete image to a staging file, validates the staged
image through the native reader, checks the source extent hash under an
exclusive image lock, and commits with an atomic replacement. A changed source,
short write or failed read-back leaves the original image untouched.

`mkdir`, `delete`, file growth and allocation are rejected deliberately. The
remaining exFAT writer work requires allocation-bitmap updates, complete
directory-set transactions and independent crash/rollback tests.

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
  `mountfile/fat32.py`; FAT12/16 через этот VFS намеренно read-only.
* Чистый ext2 с allowlisted features (1/2/4 KiB blocks, без журнала, direct и
  один single-indirect pointer для данных файла) использует собственный Python
  writer без `debugfs`; предел файла — `12 + block_size/4` data blocks, без
  sparse/double/triple indirect. Рост каталогов за пределы direct blocks не
  поддерживается. Unsupported/dirty/out-of-bounds volumes не получают native RW.
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
профили остаются на pytsk3/существующих fallback путях. Это не разрешение на
запись и не FUSE export. `phase1_ext2_profile_candidate` по-прежнему означает
только предварительное соответствие write profile. Writer обязан самостоятельно
проверять затрагиваемые группы, bitmaps, inode metadata и partition bounds.

## Ограничения и риски

* Native ext2 writes remain limited to the clean allowlisted profile and are not
  power-loss-atomic; ext3/ext4 and unsupported ext2 profiles remain read-only.
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

The reader validates resident and nonresident metadata records, parent/sequence
references and bounded `$I30` index roots/allocation blocks. It rejects malformed
indexes, cycles, unsupported `ATTRIBUTE_LIST` streams, sparse/compressed/encrypted
data, and path components outside its declared profile. Native NTFS writes remain
disabled; the system-driver path is a separate explicit compatibility choice, not
a substitute for `$LogFile`-aware native transactions.
