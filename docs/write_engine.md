# MF-WE — движок записи (ТЗ MF-WE-TZ-001)

`mountfile/write_engine.py` — транзакционный фасад над RW-бэкендами
`mountfile/universal_fs_rw.py` (native FAT12/16/32, ext2/3/4 через `debugfs`,
NTFS через mount-helper, loop mount, rebuild). Собственный нативный движок
NTFS/ext не реализуется: по ТЗ §15 стоимость и риск такого решения
несопоставимы с выгодой.

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

* Корректность записи в NTFS/ext равна корректности бэкенда, который её
  выполняет; MF-WE добавляет транзакционность и аудит, а не новую реализацию ФС.

  GUI и `WriteEngine(read_only=False)` используют тот же `UniversalFSRW`,
  поэтому это не только read/extract режим.
* Полный набор fuzz/crash-тестов из §9.1 ТЗ (Atheris/hypothesis, ≥10 000
  итераций, `kill -9` на каждом шаге) **не выполнен** — реализованы
  детерминированные crash/rollback-сценарии в `tests/test_write_engine.py`.
* Покрытие строк/ветвей из §9.3 (≥85% / ≥75%, транзакционный слой ≥95%) на
  текущем прогоне не подтверждено для всего пакета.
