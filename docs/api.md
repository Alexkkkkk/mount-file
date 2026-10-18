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
