# MF-WE — движок записи (ТЗ MF-WE-TZ-001)

`mountfile/write_engine.py` — транзакционный фасад над RW-бэкендами
`mountfile/universal_fs_rw.py` (native FAT12/16, ext2/3/4 через `debugfs`,
NTFS через mount-helper, loop mount, rebuild). Собственный нативный движок
NTFS/ext RW пока не включён в этот фасад; его поэтапная разработка отслеживается
в Issue #182. Новый NTFS boot-sector probe ниже только проверяет геометрию и не
переключает существующий backend, не предоставляет чтение файлов и не разрешает
запись.

## Что гарантирует MF-WE

| Требование ТЗ §8 | Реализация |
|---|---|
| RO по умолчанию | `WriteEngine(image)` — все мутации бросают `ReadOnlyError`; запись только при `read_only=False` |
| Backup перед записью | снапшот сессии + снапшот транзакции (ротация `keep_snapshots`, по умолчанию 3) |
| Транзакции с журналом | `EngineJournal`: JSONL с полями `prev`/`hash`; `verify_journal()` пересчитывает цепочку |
| Verify после commit | `verify()`: размер образа, структурные проверки BPB/суперблока, внешний чеккер (`fsck.fat`/`e2fsck`) — с фактическими значениями в `detail` |
| Откат | `rollback()` возвращает образ к состоянию на старте транзакции побайтово; `restore_from_backup()` — к снапшоту |
| Dry-run | `dry_run=True`: проверяются пути и предусловия, ни один байт образа не меняется |
| Запрет записи в повреждённые образы | `damage_report()` + `DamagedImageError`; обход только явным `allow_damaged=True` |

Проверки, которым нужен внешний инструмент, при его отсутствии возвращают
`ok=None` (неизвестно), а не «прошло» — `verify()` не выдаёт непроверенное
за проверенное.

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

* FAT12/16/32 — собственный движок (как и раньше).
* ext2/3/4 — `debugfs -w`; на Windows используется `debugfs.exe` из `PATH` или
  `debugfs` из WSL, если установлен Linux-дистрибутив с пакетом `e2fsprogs`
  (например, `sudo apt install e2fsprogs`). MountFile конвертирует host paths
  для временных файлов, а исходный image extent проходит через существующую
  проверяемую transactional commit/rollback процедуру. `chmod`/`chown`/`symlink`
  доступны только на ext.

* NTFS — через `ntfs-3g` на Linux или системный файловый драйвер Windows через
  ImDisk. На Windows требуется отдельно установленный ImDisk Virtual Disk
  Driver и запуск MountFile от имени администратора; если одно из условий не
  выполнено, GUI показывает точную причину и не предлагает сохранение в образ.

* exFAT и FAT12/16 — через ImDisk и системный Windows filesystem driver на
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

## Ограничения и риски

* Корректность записи в NTFS/ext равна корректности бэкенда, который её
  выполняет; MF-WE добавляет транзакционность и аудит, а не новую реализацию ФС.

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
