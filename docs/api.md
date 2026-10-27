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
и фактический регистр имени из образа. FAT и NTFS включают предусмотренный их
reader-ом case-insensitive lookup; для ext namespace он остаётся case-sensitive.
Каждый native provider также публикует `backend_status`: сериализуемый словарь
с backend name, read/write capabilities, режимом и явной причиной отказа от
записи. Пустой `write_capabilities` означает именно read-only native profile,
а не скрытый переход на `debugfs`, `ntfs-3g` или системный mount.
Распознанный malformed/unsupported volume завершается ошибкой и не подменяется
другим parser-ом. Чтение не разрешает запись или replay/recovery.

`pytsk3` остаётся optional compatibility fallback только для форматов, которые
native routing не распознал. Установка базового MountFile не требует `pytsk3`.

::: mountfile.vfs.open_image_provider

::: mountfile.vfs.ReadOnlyVFSStatMixin
