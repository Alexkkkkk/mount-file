# Командная строка

CLI не импортирует Qt и доступен через `python -m mountfile`. `main.py` также
передаёт ему команды, поэтому оба варианта эквивалентны:

```bash
python -m mountfile --help
python main.py volumes dump.img
```

## Команды

### DFIR-команды

```bash
python -m mountfile grep dump.img "password=" --encoding utf-8
python -m mountfile hash dump.img
python -m mountfile evidence-manifest evidence.img --output evidence.img.manifest.json
python -m mountfile timeline dump.img --csv timeline.csv
python -m mountfile timestomp dump.img --csv timeline.csv
python -m mountfile search dump.img "credential" --files --max-results 100
python -m mountfile yara-scan dump.img --rules rules.yar --targets files unallocated
python -m mountfile timestomp-scan dump.img
python -m mountfile timeline-build dump.img --csv timeline.csv
python -m mountfile slack-extract dump.img --output ./slack
python -m mountfile unallocated-scan dump.img --ranges ranges.json
python -m mountfile slack dump.img --slack-only \
    --last-cluster-offset 0x400000 --file-size 5000 --cluster-size 4096 \
    -o slack.bin
```

`grep` возвращает ограниченные byte offsets и контекст, `hash` вычисляет
SHA-256, а `evidence-manifest` потоково считает SHA-256 одного файла и создаёт
новый JSON-файл только по явному `--output`; исходник открывается только для
чтения, а существующий output никогда не перезаписывается. Эта команда не
определяет формат контейнера и не считает логический media-stream hash. Команды
`timeline`/`timestomp` принимают CSV внешнего коллектора. Отдельные
`timeline-build`, `timeline-query`, `search`, `yara-scan`, `timestomp-scan` и
`slack-extract` — filesystem-aware команды package CLI; они требуют доступного
filesystem backend (обычно `pytsk3`/`libtsk`), а для YARA дополнительно нужен
`yara-python`.
`yara-scan` проверяет filesystem-файлы и/или нераспределённые области с учётом
`--targets`; это не заменяет полноценное восстановление файлов и не обещает
корректный allocation map для каждой ФС.

`unallocated-scan` читает только явно переданные ranges. `slack --slack-only`
принимает уже подтверждённые offset последнего кластера, логический
размер файла и размер кластера и не разрешает путь файла и cluster
chain: это должен сделать вызывающий код.

GUI **Инструменты → Командный центр** (`Ctrl+Shift+X`) использует реестр этих
подкоманд напрямую. Все 27 команд имеют в интерфейсе контекстную справку из
того же CLI parser; аргументы передаются по одному на строку, без shell.

| Команда | Назначение | Запись в образ |
|---|---|---:|
| `volumes` | найденные тома и смещения | нет |
| `info` | размер, сигнатура и сводка томов | нет |
| `ls` | список каталога | нет |
| `get` | извлечение файла или каталога | нет |
| `put` | записать локальные файлы | **да, FAT32** |
| `mkdir` | создать каталог | **да, FAT32** |
| `mv` | переименовать запись в том же каталоге | **да, FAT32** |
| `rm` | удалить файл/каталог | **да, FAT32** |
| `autopick` | выбрать рабочий том по проверяемым кандидатам | нет |
| `decode` | расшифровать RT809H MST/MTK-дамп встроенной таблицей | создаёт новый файл |
| `split` | разбить дамп по секции `[PARTITIONS]` файла `udev` | создаёт файлы в каталоге вывода |
| `grep` | поиск строки/regex в байтах сырого образа | нет |
| `hash` | SHA-256 образа | нет |
| `evidence-manifest` | потоковый SHA-256 одного файла и отдельный JSON manifest | создаёт новый manifest |
| `timeline` / `timestomp` | просмотр timeline CSV / проверка аномалий | нет |
| `timeline-export` | нормализация timestamp CSV в UTC | создаёт CSV |
| `yara` | YARA-сканирование по исходникам правил | нет |
| `search` | поиск в байтах образа или содержимом файлов | нет |
| `yara-scan` | YARA по файлам и/или unallocated ranges | нет |
| `unallocated-scan` | сигнатурный поиск только в заданных JSON ranges | нет |
| `timestomp-scan` | сбор и анализ временных меток из образа | нет |
| `timeline-build` / `timeline-query` | построить/запросить timeline | создаёт SQLite/CSV при передаче output path |
| `slack-extract` | извлечь slack хвосты файлов по найденным extent | создаёт файлы в output-каталоге |
| `live-diff` | сравнить два образа и сформировать Markdown-отчёт | создаёт отчёт при `-o` |
| `slack` | slack последнего кластера по явно заданному extent | нет; файл только через `--output`, без перезаписи |

Общие аргументы целевых команд (`volumes`, `info`, `ls`, `get`, `put`, `mkdir`,
`mv`, `rm`, `autopick`):

- `-V N`, `--volume N` — номер тома из `volumes`, начиная с 1;
- `-O 0x400000`, `--offset ...` — прямое байтовое смещение файловой системы;
- `--scan-limit BYTES` — сколько байт искать при обнаружении сигнатур;
- `--auto` — использовать автоматический выбор тома;
- `--backup` — перед записью скопировать образ в `<image>.bak`.

Для команд записи обязательны `--yes`. Без него программа ничего не меняет,
показывает предупреждение и возвращает код **2**. Нормальный результат — **0**,
ошибка — **1**; `Ctrl+C` возвращает **130**.

## Примеры чтения

```bash
python -m mountfile info dump.img
python -m mountfile volumes dump.img
python -m mountfile ls dump.img /
python -m mountfile ls dump.bin /system --auto
python -m mountfile get dump.img /photos -o ./evidence/photos
```

`get` извлекает во внешний каталог и не меняет образ. При отсутствии `pytsk3`
чтение ограничено встроенным FAT32-провайдером; NTFS, EXT и другие ФС требуют
`pytsk3` и поддержки установленной `libtsk`.

## Примеры FAT32-записи

```bash
python -m mountfile put dump.img ./hello.txt --dest /docs --backup --yes
python -m mountfile mkdir dump.img /docs/new --backup --yes
python -m mountfile mv dump.img /hello.txt renamed.txt --yes
python -m mountfile rm dump.img /docs/new --recursive --backup --yes
```

Сначала работайте с копией, затем проверьте результат повторным `ls`. `--backup`
создаёт соседний файл, но при нехватке места или повреждённом образе операция
всё равно может завершиться ошибкой; он не является транзакцией.

## RT809H и авто-подбор

```bash
python -m mountfile decode dump.bin
python -m mountfile split dump.bin udev.txt -o ./parts
python -m mountfile autopick dump.bin --json > candidates.json
python -m mountfile autopick dump.bin --hint system --extract ./rootfs
```

`autopick` сопоставляет MBR/GPT, `udev` и сигнатуры ФС, затем по умолчанию
подтверждает кандидата реальным открытием. `--no-verify` быстрее, но может
выбрать нерабочее смещение. OOB/ECC сырых NAND-дампов не обрабатываются.
