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
the main and backup boot regions, FAT geometry, cluster chains, allocation
bitmap/FAT consistency and checksummed file-entry sets, but never writes the
image. `put`, `mkdir`, rename and delete remain
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
  один single-indirect pointer для данных файла и каталогов) использует
  собственный Python writer без `debugfs`; предел файла и каталога —
  `12 + block_size/4` data blocks, без sparse/double/triple indirect. При
  расширении каталога pointer block, bitmap/counters и directory map проходят
  read-back до commit. Unsupported/dirty/out-of-bounds volumes не получают native RW.
  Для ext2 вне этого профиля и ext3/ext4 автоматического fallback нет: legacy
  `debugfs -w` доступен только прямому library caller с
  `allow_legacy_debugfs=True`, с явным `volume_size` для каждого nonzero offset.
  Он staging-копирует и commit-ит ровно filesystem extent; `chmod`/`chown`/
  `symlink` остаются за этим compatibility API.

* NTFS — через `ntfs-3g` на Linux или системный файловый драйвер Windows через
  ImDisk. На Windows требуется отдельно установленный ImDisk Virtual Disk
  Driver и запуск MountFile от имени администратора; если одно из условий не
  выполнено, GUI показывает точную причину и не предлагает сохранение в образ.

* exFAT — через ImDisk и системный Windows filesystem driver на
  Windows (те же требования установки и прав); на Linux — через `LoopMountRW`,
  если доступны `mount`/`umount` и соответствующий драйвер. HFS+ и другие
  неподдерживаемые Windows filesystem drivers остаются read-only.
  Имена детектора (`FAT16`, `FAT32`, `HFS+`) нормализуются для mount-helper.
* ISO9660, SquashFS, JFFS2 и UBIFS объявлены `read-only-format`: чтение и
  извлечение доступны, но мутации отклоняются до монтирования. Эти форматы
  нельзя честно сделать обычными RW-ФС без пересборки образа по их спецификации.
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

* Native ext2 имеет консервативный feature allowlist и проверяет staged image
  до commit, но не является power-loss-atomic; ext3/ext4 и unsupported ext2
  профили по умолчанию остаются read-only. Native NTFS metadata browse также
  fail-closed до отдельной реализации валидации restart area `$LogFile`; boot/MFT
  probe остаётся доступным только для диагностики.

  GUI и `WriteEngine(read_only=False)` используют тот же `UniversalFSRW`,
  поэтому это не только read/extract режим.
* Полный набор fuzz/crash-тестов из §9.1 ТЗ (Atheris/hypothesis, ≥10 000
  итераций, `kill -9` на каждом шаге) **не выполнен** — реализованы
  детерминированные crash/rollback-сценарии в `tests/test_write_engine.py`.
* Покрытие строк/ветвей из §9.3 (≥85% / ≥75%, транзакционный слой ≥95%) на
  текущем прогоне не подтверждено для всего пакета.

## NTFS boot-sector probe (read-only groundwork)

`mountfile.vfs.probe_ntfs_profile(image_path, offset, volume_size)` вызывает
pure-Python probe первичного 512-байтового NTFS boot sector. Он требует явную
внешнюю границу образа/раздела и проверяет OEM/signature, bytes-per-sector,
sectors-per-cluster, объявленный размер тома, расположение `$MFT`/`$MFTMirr` и
кодирование размеров MFT/index records. Автопоиск томов использует тот же parser,
чтобы отвергать NTFS-подобные случайные boot sectors и геометрию вне образа.
Разметка полей и кодирование размеров сверены с upstream Linux NTFS3 [структурами
on-disk](https://raw.githubusercontent.com/torvalds/linux/master/fs/ntfs3/ntfs.h)
и [проверками геометрии](https://raw.githubusercontent.com/torvalds/linux/master/fs/ntfs3/super.c).

Успешный результат означает только валидную геометрию boot sector. Probe не
проверяет сам backup boot sector, MFT FILE records и USA fixups, атрибуты и
runlists, каталоговые индексы, `$Volume` dirty flag либо `$LogFile` recovery.
Поэтому он не является готовым NTFS reader, clean-volume verdict или разрешением
на запись. Native VFS routing остаётся выключенным до завершения последующих
read-only metadata-validation этапов.

## NTFS FILE-record header and USA validation

`mountfile.vfs.parse_ntfs_file_record_header(record)` валидирует заголовок одного
in-use `FILE` record и Update Sequence Array: требуемый count, границы массива,
совпадение update sequence number в конце каждого 512-байтового блока и поля
размера/смещения заголовка. Исправленные trailer words возвращаются в отдельной
копии только после успешной проверки всех блоков. При несовпадении parser
отклоняет запись целиком; атрибуты остаются непрочитанными.

Реализация принимает старое и новое расположение USA (`0x2A` и `0x30`) и
ограничивает запись 1 MiB. Формат заголовка и алгоритм fixup сверены с
[документацией Microsoft](https://learn.microsoft.com/en-us/windows/win32/devnotes/file-record-segment-header)
и [реализацией NTFS3](https://raw.githubusercontent.com/torvalds/linux/master/fs/ntfs3/fsntfs.c).
Это низкоуровневый parser, а не чтение `$MFT` с диска: он не проверяет атрибуты,
родительские ссылки, каталоги, `$Volume`/`$LogFile` и не включает NTFS read/write
routing.

`mountfile.vfs.probe_ntfs_metadata(image_path, offset, volume_size)` — следующий
read-only этап. Он требует точную границу тома, повторно проверяет backup boot
sector, читает `$MFT` и `$MFTMirr` record 0 с bounded reads и проверяет полный
поток атрибутов: длины, выравнивание, resident/nonresident layout, VCN bounds и
структуру runlist. Результат является metadata profile и не включает обход
каталогов, восстановление `$LogFile`, symlink traversal или любые операции
записи. При несогласованности primary/backup boot, MFT или USA весь профиль
отклоняется.
