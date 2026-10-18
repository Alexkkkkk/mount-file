# Командная строка

CLI не импортирует Qt и доступен через `python -m mountfile`. `main.py` также
передаёт ему команды, поэтому оба варианта эквивалентны:

```bash
python -m mountfile --help
python main.py volumes dump.img
```

## Команды

### Read-only DFIR-команды

```bash
python -m mountfile grep dump.img "password=" --encoding utf-8
python -m mountfile hash dump.img
python -m mountfile timeline dump.img --csv timeline.csv
python -m mountfile timestomp dump.img --csv timeline.csv
```

`grep` возвращает ограниченные byte offsets и контекст, `hash` вычисляет
SHA-256, а `timeline` и `timestomp` работают с CSV, полученным внешним
коллектором. Эти команды не изменяют исходный образ. Полноценный
filesystem-aware collector, YARA pipeline, carving и slack discovery пока не
являются частью package CLI.

Отдельные `mountfile_dfir.py` и `mountfile_grep.py` сохраняются как
экспериментальные standalone-инструменты с расширенными сценариями. Их наличие
не означает полноценного forensic recovery или гарантии chain of custody.

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
