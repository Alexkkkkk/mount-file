# MF-WE — движок записи (ТЗ MF-WE-TZ-001)

`mountfile/write_engine.py` — транзакционный фасад над RW-бэкендами
`mountfile/universal_fs_rw.py` (native FAT12/16/32 и ограниченный clean ext2
profile; legacy ext2/3/4 через `debugfs`; NTFS через системный/mount-helper
backend). Native ext2 — первая фаза собственной записи; ext3/ext4 и NTFS остаются
внешними backend-ами до отдельной journal/recovery-aware реализации (issue #182).

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
* Чистый ext2 с allowlisted features (1/2/4 KiB blocks, без журнала, direct и
  один single-indirect pointer для данных файла) использует собственный Python
  writer без `debugfs`; предел файла — `12 + block_size/4` data blocks, без
  sparse/double/triple indirect. Рост каталогов за пределы direct blocks не
  поддерживается. Unsupported/dirty/out-of-bounds volumes не
  получают native RW. Для ext2 вне этого профиля и ext3/ext4 пока сохраняется
  legacy `debugfs -w` fallback: на Windows нужен `debugfs.exe` из `PATH` либо
  WSL/e2fsprogs. `chmod`/`chown`/`symlink` поддерживаются только этим fallback.

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

* Native ext2 имеет консервативный feature allowlist и проверяет staged image
  до commit, но не является power-loss-atomic; NTFS, ext3/ext4 и unsupported ext2
  профили всё ещё зависят от внешних backend-ов.

  GUI и `WriteEngine(read_only=False)` используют тот же `UniversalFSRW`,
  поэтому это не только read/extract режим.
* Полный набор fuzz/crash-тестов из §9.1 ТЗ (Atheris/hypothesis, ≥10 000
  итераций, `kill -9` на каждом шаге) **не выполнен** — реализованы
  детерминированные crash/rollback-сценарии в `tests/test_write_engine.py`.
* Покрытие строк/ветвей из §9.3 (≥85% / ≥75%, транзакционный слой ≥95%) на
  текущем прогоне не подтверждено для всего пакета.
