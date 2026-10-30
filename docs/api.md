# API reference

Эти разделы описывают библиотечные интерфейсы. Они не добавляют команды в GUI
или основной CLI автоматически. Перед работой с доказательствами используйте
копию образа, фиксируйте исходный SHA-256 и проверяйте результаты независимыми
средствами.

## Unallocated-space index

`UnallocatedIndexer` индексирует только диапазоны, переданные вызывающим кодом.
Он не определяет allocation map файловой системы самостоятельно и не выполняет
file carving. SQLite хранит SHA-256, дедуплицированные chunks, occurrences и
компактное bitmap-покрытие блоков.

::: mountfile.unallocated.UnallocatedIndexer

## Forensic copilot summary

Сводка принимает уже вычисленные метаданные и коллекции событий. Builder не
открывает файлы, не читает образ и не вызывает сеть или модель.

::: mountfile.ai.copilot.CopilotSummaryBuilder

::: mountfile.ai.copilot.build_summary

## Android image headers

Парсеры проверяют Android sparse image и boot image headers/chunks. Это не
распаковка ramdisk, не AVB verification и не операция прошивки.

::: mountfile.android.parse_sparse_header

::: mountfile.android.parse_boot_image_header

## Read-only image VFS

`open_image_provider` открывает выбранный bounded filesystem profile. Providers
поддерживают `list_dir`, `stat`, `read_file_range` и `read_file`; extraction
использует тот же bounded range API. `stat` возвращает metadata выбранной записи
и фактический регистр имени из образа. FAT поддерживает предусмотренный reader-ом
case-insensitive lookup. NTFS использует проверенную таблицу `$UpCase` для
Unicode-сравнения, если её bounded stream доступен; иначе Unicode-имена требуют
точного регистра. Для ext namespace lookup остаётся case-sensitive.
Каждый native provider также публикует `backend_status`: сериализуемый словарь
с backend name, read/write capabilities, режимом и явной причиной отказа от
записи. Пустой `write_capabilities` означает именно read-only native profile,
а не скрытый переход на `debugfs`, `ntfs-3g` или системный mount.
Распознанный malformed/unsupported volume завершается ошибкой и не подменяется
другим parser-ом. Обычная маршрутизация не выполняет replay/recovery. Для
проверенного classic JBD2 существует только явный `open_native_ext3(...,
replay_journal=True)`: он строит bounded in-memory overlay из committed
descriptor/data/commit records, не изменяет исходный образ и отклоняет revoke,
checksum, неизвестные feature flags, повреждённые или неполные транзакции.

### Семантика метаданных bounded providers

Общие поля `stat` — `path`, `name`, `is_dir`, `size` и `date`; отдельные
providers могут добавлять поля вроде `kind` или `inode`. Это parser view, а не
полная POSIX metadata model. Наличие `kind`/`inode` не означает поддержки
восстановления владельца, режима доступа или link graph.

`date` остаётся строкой, предоставленной конкретным reader-ом, и может быть
пустой. ext2 выводит mtime обычного файла; FAT выводит доступную ему дату;
exFAT и NTFS в текущем VFS listing возвращают пустое значение. Формат,
точность, timezone и наличие времён создания/доступа не нормализуются между
форматами; поле нельзя использовать как нормализованный UTC timeline timestamp.

Общий VFS contract не предоставляет owner/group, POSIX permissions/ACL,
xattrs, число или семантику hard links и все timestamps. NTFS reader открывает
только unnamed `$DATA`; alternate data streams не перечисляются и не извлекаются
через этот API. ext2 может обозначить тип записи как `symlink`, но это не
обещание следования по ссылке: callers не должны трактовать имя/`kind` как
разрешённый host path. Неподдержанное чтение или изменение такого объекта должно
завершаться явным отказом.

Текущий операционный контракт bounded native profiles можно получить как
detached JSON-compatible snapshot:

```python
from mountfile.capabilities import get_native_capability_matrix

matrix = get_native_capability_matrix()
schema_version = matrix["version"]
profiles = matrix["profiles"]
```

Ту же versioned matrix можно получить из package CLI без открытия образа:

```bash
python -m mountfile capabilities
```

Матрица задаёт vocab интерфейсов VFS и `UniversalFSRW` и сверяется тестами с
provider status. Она не классифицирует произвольное состояние image и не
заменяет profile validators: malformed, dirty или unsupported volume всё равно
отвергается при open.

`pytsk3` остаётся optional compatibility fallback только для форматов, которые
native routing не распознал, и теперь отключён по умолчанию. Для его явного
включения используйте `MOUNTFILE_ALLOW_LEGACY_TSK=1` или передайте
`allow_legacy_fallback=True` в `open_image_provider`. Установка базового
MountFile не требует `pytsk3`; native routing не запускает сеть, subprocess или
системный mount. Так как `pytsk3.Img_Info` открывает весь файл и текущий адаптер
не может ограничить чтение размером раздела, legacy fallback разрешён только
для файловой системы, занимающей весь файл-образ. Для раздела внутри disk image
fallback fail-closed откажет; используйте native provider либо отдельный
адаптер, который умеет ограничивать каждый read выбранным extent.

::: mountfile.vfs.open_image_provider

::: mountfile.vfs.ReadOnlyVFSStatMixin
