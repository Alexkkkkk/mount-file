# Mount File

**Файловый менеджер поверх образов** — просмотр и извлечение файлов из raw-дампов
дисков и чипов (`.img`, `.bin`, `.dd`), без монтирования образа в систему.

> Это не «обычный файловый менеджер»: файлы читаются напрямую из образа через
> собственный парсер разметки и слой виртуальной файловой системы, а не через
> драйверы ОС.

```bash
python -m mfim.info          tests/images/mbr.img      # что внутри образа
python -m mfim.cli tree      tests/images/mbr.img --part 2 --depth 3
python -m mfim.cli extract   tests/images/mbr.img /docs/readme.txt out.txt --part 2 --verify
python -m mfim.cli hex       tests/images/mbr.img /HELLO.TXT --part 1 --length 64
python -m mfim               tests/images/mbr.img     # GUI (PyQt6), если есть дисплей
```

---

## Возможности MVP

| Возможность | Статус | Как реализовано |
|---|---|---|
| Открытие raw-образа, определение размера | ✅ | `mfim/rawio.py` |
| Разбор MBR (4 первичных) | ✅ | `mfim/partition.py`, свой парсер |
| Разбор EBR-цепочки extended-разделов | ✅ | цикл с защитой от зацикливания |
| Разбор GPT (заголовок, массив записей, CRC32, UTF-16 имена, GUID) | ✅ | свой парсер, CRC проверяется |
| Определение ФС по магическим сигнатурам | ✅ | FAT12/16/32, exFAT, NTFS, ext2/3/4, SquashFS, UBI/UBIFS, JFFS2, ISO9660, LUKS |
| Дерево файлов (ленивая загрузка), таблица каталога | ✅ | `mfim/tree_model.py` + `mfim/gui.py` |
| Чтение и извлечение файлов из FAT/NTFS/ext | ✅ | `pytsk3` (The Sleuth Kit) |
| HEX-просмотр файла и «сырого» образа | ✅ | `mfim/hexview.py` |
| Запись в FAT-раздел (копирование внутрь, mkdir, rm, ren) | ⚠️ опционально | `mtools` (`mfim/fatwrite.py`), только FAT |
| Транзакционная запись по секторам (patch) в образ | ⚠️ opt-in | `mfim/rawio.py`: план → бэкап → запись → верификация |
| Запись в ext4/NTFS/SquashFS/UBIFS | ❌ не реализовано | см. «Ограничения» |

---

## Архитектура

```
┌──────────────────────────────────────────────┐
│  GUI (PyQt6): дерево, таблица, HEX-редактор   │  mfim/gui.py
├──────────────────────────────────────────────┤
│  CLI: info / parts / sniff / ls / tree /      │  mfim/cli.py
│       extract / hex / fatwrite / patch        │
├──────────────────────────────────────────────┤
│  Слой дерева (без Qt, тестируется headless)   │  mfim/tree_model.py
├──────────────────────────────────────────────┤
│  VFS: list / stat / read / extract            │  mfim/vfs.py   (pytsk3)
├──────────────────────────────────────────────┤
│  Запись: FAT через mtools | секторный patch   │  mfim/fatwrite.py, mfim/rawio.py
├──────────────────────────────────────────────┤
│  Разметка: MBR / EBR / GPT + sniff ФС         │  mfim/partition.py (свой код)
├──────────────────────────────────────────────┤
│  I/O: файл образа (чтение / r+b запись)       │  mfim/rawio.py
└──────────────────────────────────────────────┘
```

Ключевое разделение: **весь слой работы с ФС не знает про GUI**. GUI — тонкая
обёртка (`QAbstractItemModel` вокруг `ImageTreeModelData`), поэтому все функции
доступны и проверяются из CLI без дисплея.

### Почему `QFileSystemModel` не подходит

`QFileSystemModel` обходит **хост-файловую систему** и не умеет смотреть внутрь
образа. Поэтому в `gui.py` реализована собственная модель
`ImageTreeQtModel(QAbstractItemModel)` с ленивой загрузкой: каталог читается
только при раскрытии узла (`hasChildren` + `rowCount`), что позволяет работать с
образами, где сотни тысяч элементов.

---

## Установка

```bash
python -m pip install -r requirements.txt        # pytsk3 (чтение)
python -m pip install PyQt6                      # только для GUI
```

Дополнительно (по желанию):

* **mtools** + **dosfstools** — запись в FAT-разделы (`apt install mtools dosfstools`);
* **squashfs-tools** — извлечение SquashFS через внешний `unsquashfs` (пока вручную);
* **libguestfs / guestfish** (Linux) — если нужен универсальный read/write-бэкенд.

Проверка окружения: `python -m mfim.cli --version`, `python -c "import pytsk3"`.

---

## Использование

### CLI (работает без GUI и без дисплея)

```bash
python -m mfim.cli info    IMAGE                    # размер, разметка, разделы
python -m mfim.cli parts   IMAGE --json             # таблица разделов (JSON)
python -m mfim.cli sniff   IMAGE                    # определение ФС по магии
python -m mfim.cli ls      IMAGE /        --part 1  # листинг каталога
python -m mfim.cli ls      IMAGE /        --virtual  # + служебные $MBR/$FAT1/...
python -m mfim.cli tree    IMAGE --part 2 --depth 4 # дерево
python -m mfim.cli extract IMAGE /dir/file.txt out.bin --part 2 --verify
python -m mfim.cli extract IMAGE --all --out-dir dump --part 2   # весь раздел
python -m mfim.cli hex     IMAGE /HELLO.TXT --part 1 --offset 0 --length 256
python -m mfim.cli hex     IMAGE --raw --offset 0x1b8 --length 16
```

Выбор раздела: `--part N` (нумерация с 1) либо `--offset 0x100000`. Если у образа
нет таблицы разделов (типичный дамп SPI-флеш), берётся смещение 0.

### Запись (осторожно, включается явно)

```bash
# FAT: копирование файла внутрь образа через mtools (offset-режим, без монтирования)
python -m mfim.cli fatwrite put IMAGE --local ./new.bin --dest /new.bin --allow-write --yes --part 1

# Секторный patch: сначала план, потом запись с автоматическим .bak и верификацией
python -m mfim.cli patch IMAGE 0x400 deadbeef            # только план, ничего не пишет
python -m mfim.cli patch IMAGE 0x400 deadbeef --allow-write
```

Перед записью в `rawio.ImagePatcher.commit()` создаётся полная копия образа
(`*.bak`), затем каждая область читается обратно и сравнивается побайтно — отчёт
показывает, что именно записано и проверено.

### GUI

```bash
python -m mfim IMAGE            # или python -m mfim.gui IMAGE
python -m mfim --cli IMAGE ...  # принудительно CLI (нет дисплея / нет Qt)
```

Слева — дерево образа, справа сверху — содержимое каталога, снизу — вкладки
«HEX» (просмотр файла по смещению) и «Разделы» (полный разбор разметки).

---

## Тесты

```bash
bash tests/make_test_images.sh     # собрать образы (ниже)
python -m pytest tests/test_mfim.py -v
```

Скрипт `tests/make_test_images.sh` создаёт в `tests/images/` образы без root и без
`losetup` (всё в user space: `dd` + `sfdisk` + `mkfs.*` + `mtools` + `debugfs`):

| Образ | Что проверяет |
|---|---|
| `mbr.img` | MBR: FAT32 (загрузочный) + ext4; чтение обоих, вложенные каталоги |
| `gpt.img` | GPT: CRC заголовка и массива записей, имя раздела, тип `EFI System` |
| `mbr_extended.img` | EBR-цепочка из двух logical FAT16 внутри extended-контейнера |
| `raw_fat.img` | образ без таблицы разделов, FAT16 со смещения 0 |
| `raw_ext4.img` | то же для ext4 |
| `squash.img` | детект SquashFS |

Проверки: `SHA256SUMS` фиксирует контрольные суммы — тест `test_tools_do_not_change_images`
доказывает, что разбор образа ничего в нём не изменил.

---

## Что было исправлено относительно типового подхода (и почему это важно)

1. **`pytsk3.TskFile.read()` не существует.** В ТСК чтение random-access:
   `read_random(offset, size)`. В `vfs.iter_file()` реализован цикл до
   `info.meta.size` с обработкой **разрежённых областей**: ТСК возвращает `None`
   для «дыры», и это не конец файла — читатель отдаёт нули, а не обрывается.
   Покрыто тестами `test_iter_file_handles_sparse_hole` / `..._stops_on_empty_read`.
2. **`entry.info.meta` может быть `None`** (специальные и удалённые записи) —
   адаптер `entry_from_tsk()` это допускает; тест `test_entry_from_tsk_tolerates_meta_none`.
3. **Имена на диске — байты, а не UTF-8**: Windows-1251, обрезанные
   многобайтовые последовательности, мусор у удалённых записей. Декодирование с
   `errors="replace"`, листинг никогда не падает из-за одного имени.
4. **Смещение суперблока ext2/3/4 — `0x400` (1024), а не `0x438`.** Ошибка чтения
   магической сигнатуры на 0x438 «сдвигала» все поля на 1 КиБ и определяла живой
   ext4 как `unknown`/`ext2` с нулевыми inode. Нашёл именно интеграционный тест.
5. **TSK подмешивает свои псевдо-файлы** (`$MBR`, `$FAT1`, `$FAT2`, `$OrphanFiles`,
   плюс синтетическая запись «Volume Label Entry») в любой листинг FAT/NTFS. В
   пользовательском режиме они отфильтрованы (meta.type 10/11 и суффикс подписи),
   но доступны через `include_virtual=True` / `ls --virtual`.
6. **FAT32 меньше 65525 кластеров не читается вообще** (в т.ч. ТСК) — тестовый
   образ пересобран на 64 МиБ, иначе проверка «чтение FAT32» была бы фиктивной.
7. **Запись в образ — только явно и транзакционно**: `ImagePatcher` не пишет без
   `readonly=False`, делает `.bak` и верифицирует read-back.

---

## Ограничения (честно)

* **Запись реализована только для FAT** (через `mtools`) и для «сырых» секторов
  (`patch`). Запись в ext4 требует обновления inode, bitmap'ов групп и журнала;
  UBIFS — LEB/PEB и wear-leveling. Это отдельный, большой этап (см. «Дальше»).
* **SquashFS, UBIFS, JFFS2 — только детект** по сигнатуре. Извлечение файлов из них
  пока выполняется внешними утилитами (`unsquashfs`, `ubi_reader`, `jefferson`) и в
  MVP не обёрнуто.
* **exFAT** открывается ТСК частично; запись не поддержана.
* **Шифрованные разделы** (LUKS) только детектируются.
* **eMMC/NAND-дампы с OOB-областями и ECC** требуют отдельного парсера формата дампа
  — в MVP считается, что образ уже «чистый» (только данные, без служебных областей).
* **GUI не проверен в этой среде** (в песочнице нет дисплея и libGL): тестируется
  только Qt-модель в offscreen-режиме (`test_gui_model_offscreen`), полный запуск
  окна нужно проверить на своей машине.
* `mtools` не монтирует раздел: в режиме `offset=` он работает прямо по образу; при
  сборке из исходников без поддержки `offset` бэкенд переключается на
  extract-modify-merge по временной копии раздела.

---

## Структура репозитория

```
mfim/
  partition.py   MBR/EBR/GPT, sniff ФС, guid_str/guid_bytes (чистый Python)
  rawio.py       чтение образа, транзакционный патчер, бэкап и верификация
  vfs.py         VFS поверх pytsk3: list/stat/read/extract/hexdump
  tree_model.py  ленивое дерево без Qt (единый источник данных для GUI и CLI)
  gui.py         PyQt6: дерево + таблица + HEX + вкладка разделов + FAT-запись
  cli.py         headless-интерфейс (info/parts/sniff/ls/tree/extract/hex/fatwrite/patch)
  hexview.py     xxd-подобный дамп
  util.py        размеры, время, нормализация путей, sha256, декодирование имён
tests/
  make_test_images.sh  сборка тестовых образов (user space, без root)
  test_mfim.py         49 тестов: unit + интеграционные + GUI offscreen
tools/
  push_to_github.py    публикация каталога в репозиторий через Git Data API
```

---

## Дальше (по приоритету)

1. **Замена файла в FAT32 «на месте»** — аллокация кластеров, обновление FAT и
   каталогов своим кодом (сейчас запись делегирована `mtools`).
2. **SquashFS reader на Python** (метаданные → inode → блоки, с распаковкой
   gzip/xz/zstd/lz4): только чтение, самый частый формат в прошивках роутеров.
3. **UBIFS через `ubi_reader`** и JFFS2 через `jefferson` — как внешние backend'ы с
   единым интерфейсом VFS.
4. **Парсер форматов программаторов** (OOB-области, ECC, перестановка байт) для
   дампов NAND/eMMC.
5. **Монтирование образа как диска** (Dokany на Windows / FUSE на Linux), чтобы
   работать в Проводнике.
6. **Редактор разделов с записью** MBR/GPT (используя `guid_bytes` + `ImagePatcher`).

---

## Лицензия

MIT — см. [LICENSE](LICENSE).
