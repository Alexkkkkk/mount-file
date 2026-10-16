<!--
SPDX-License-Identifier: MIT
Copyright (c) 2026 Alexkkkkk
Licensed under the MIT License - see the LICENSE file for details.
-->
# Mount File

Файловый менеджер для raw-образов дисков и дампов NAND/eMMC. Позволяет просматривать образы, извлекать файлы и изменять файлы в разделах FAT32 — без записи образа обратно на устройство.

A file manager for raw disk images and NAND/eMMC dumps. Browse images, extract files, and modify files in FAT32 partitions without flashing the image back to the device.

> ⚠️ Запись изменяет файл образа на диске. Перед импортом файлов сделайте резервную копию.

## Содержание

- [Скачать](#скачать)
- [Возможности](#возможности)
- [Быстрый старт](#быстрый-старт)
- [Использование](#использование)
- [Командная строка](#командная-строка)
- [Windows 7](#windows-7)
- [Автообновление](#автообновление)
- [Интерфейс и клавиши](#интерфейс-и-клавиши)
- [Разработка и тесты](#разработка-и-тесты)
- [Ограничения](#ограничения)
- [Лицензия](#лицензия)

## Скачать

Список версий и файлов: [GitHub Releases](https://github.com/Alexkkkkk/mount-file/releases).

Ссылки ниже ведут на сборки **последнего** релиза, поэтому их не нужно
править при выпуске новой версии. К каждому `.exe` приложен файл `.sha256`
с контрольной суммой.

| Платформа | Сборка |
|---|---|
| Windows 10/11 x64 | [MountFile-win10-x64.exe](https://github.com/Alexkkkkk/mount-file/releases/latest/download/MountFile-win10-x64.exe) |
| Windows 7 x64 | [MountFile-win7-x64.exe](https://github.com/Alexkkkkk/mount-file/releases/latest/download/MountFile-win7-x64.exe) |
| Windows, универсальная сборка | [MountFile.exe](https://github.com/Alexkkkkk/mount-file/releases/latest/download/MountFile.exe) |
| Linux x86-64 | [MountFile-linux-x86-64](https://github.com/Alexkkkkk/mount-file/releases/latest/download/MountFile-linux-x86-64) |

Универсальная сборка `MountFile.exe` собрана на Python 3.8 + PyQt5 и
работает на Windows 7 SP1 и новее (x64). Именно её выбирает автообновление.

## Возможности

- **Разметка диска:** автоматическое определение MBR и GPT, выбор раздела или всего образа.
- **Дампы RT809H:** расшифровка зашифрованных дампов eMMC плат MStar (MST) и MediaTek (MTK) по XOR-таблицам (таблицы `XOR_MST_00.bin` / `XOR_MTK_00.bin` встроены), разбиение дампа на партиции по файлу `udev` (секции `[PARTITIONS]`, переводы строк Windows/Unix/Mac).
- **Просмотр файлов:** через The Sleuth Kit (`pytsk3`) для FAT12/16/32, exFAT, NTFS, EXT2/3/4, HFS+ и ISO9660.
- **Извлечение:** копирование файлов из образа на диск.
- **Запись в FAT32:** создание и перезапись файлов, длинные имена (LFN) и имена 8.3, создание каталогов, выделение и освобождение кластеров.
- **HEX-просмотр:** первые 1 МБ выбранного файла.
- **Интерфейс в стиле Total Commander:** две панели, вкладки каталогов, светлая и тёмная темы, массовые операции с файлами.
  Колонки Имя / Тип / Размер / Дата / Атрибуты: «Имя» занимает свободное место, у папок тип — «Папка»,
  размер — `<Папка>`, атрибуты — `----c`; длинные имена обрезаются по центру, полное имя видно в подсказке.
- **Windows 7:** поддерживается через слой совместимости с PyQt5.

## Быстрый старт

```bash
git clone https://github.com/Alexkkkkk/mount-file.git
cd mount-file
pip install -r requirements.txt
python main.py
```

Чтобы сразу открыть образ:

```bash
python main.py dump.img
```

Для Windows 10/11 и Linux/macOS используется PyQt6 из `requirements.txt`. Инструкции для Windows 7 приведены ниже.

## Использование

1. Откройте образ через **File → Open image…**.
2. В списке **Volume** выберите раздел или **Whole image**.
3. Дважды щёлкните файл, чтобы открыть его в HEX-просмотрщике; для извлечения используйте **Extract…**.
4. Для тома FAT32 доступны **Import file…** и **New folder**.

Импорт изменяет исходный файл образа. Перед записью сохраните его резервную копию.

## Командная строка

Команды доступны и через `python main.py`, и через собранный бинарник;
для CLI установка Qt не требуется. Все пути ниже — примеры.

```bash
python -m mountfile --help
python main.py volumes dump.img
python main.py ls dump.img /
python main.py get dump.img /photos -o ./out
python main.py put dump.img ./hello.txt --dest / --yes
python main.py mkdir dump.img /new
python main.py mv dump.img /hello.txt renamed.txt
python main.py rm dump.img /new --recursive --yes
python main.py decode dump.bin                # расшифровка дампа RT809H (MST/MTK) -> dump.bin_decode.bin
python main.py split dump.bin udev.txt -o ./parts   # разбиение дампа на партиции по udev
```

Без `pytsk3` CLI и классическое дерево используют встроенный провайдер FAT32.
Запись, переименование и удаление поддерживаются только на FAT32. Команды
`put` и `rm` требуют `--yes`; перед изменением образа сделайте резервную копию.
`mv` переименовывает файл в том же каталоге, а не переносит между томами.

Сборка Linux: `./build_exe.sh`; результат — `dist/MountFile-linux-x86-64`.
Для запуска Qt на Linux могут потребоваться системные пакеты `libgl1`,
`libegl1`, `libxkbcommon-x11-0`, `libdbus-1-3` и `libxcb-cursor0`.

## Windows 7

Для Windows 7 используется та же кодовая база через `mountfile/qt_compat.py`: PyQt6 не поддерживает Windows 7, поэтому нужны PyQt5 и Python 3.8.10.

```bat
py -3.8 -m pip install -r requirements-win7.txt
py -3.8 main.py dump.img
build_exe.bat
```

Последняя команда собирает автономный `dist\MountFile.exe`.

На Python 3.8 `pytsk3` не устанавливается: PyPI публикует её колёса для
Windows только начиная с Python 3.10, поэтому `requirements-win7.txt`
пропускает эту зависимость на 3.8. Сборка для Windows 7 работает на
встроенном движке FAT32 — просмотр, извлечение и запись FAT32 доступны,
чтение NTFS/EXT требует `pytsk3` и на 3.8 недоступно.

## Автообновление

При запуске программа в фоновом потоке проверяет GitHub Releases. Если доступна более новая версия, приложение предлагает обновление. В собранном `.exe` обновление скачивается, проверяется по SHA-256, устанавливается и запускает программу заново. Если сеть недоступна или URL некорректен, программа продолжает работу.

Проверка из командной строки:

```bash
python main.py --check-updates
```

Код возврата: `10` — найдено обновление, `0` — обновлений нет.

| Переменная окружения | Действие |
|---|---|
| `MOUNTFILE_UPDATE_REPO=owner/repo` | Использовать другой репозиторий с релизами |
| `MOUNTFILE_NO_UPDATE=1` | Отключить проверку обновлений |
| `MOUNTFILE_AUTO_UPDATE=1` | Устанавливать обновление и перезапускаться без подтверждения |
| `MOUNTFILE_GIT_PULL=1` | Разрешить `git pull --ff-only` при запуске из исходников |
| `MOUNTFILE_UPDATE_URL=…` | Использовать прямой URL JSON последнего релиза |

Проверка из Python:

```python
from mountfile import __version__, updater
updater.check(__version__)
```

## Интерфейс и клавиши

| Клавиши | Действие |
|---|---|
| `Enter` / двойной щелчок | Открыть каталог или просмотреть файл в HEX |
| `Backspace` | Перейти на уровень вверх |
| `Space` / `Insert` | Отметить файл |
| `Ctrl+A` | Выделить всё |
| `Tab` / `Ctrl+U` | Переключить панель / поменять панели местами |
| `Ctrl+T` / `Ctrl+W` | Создать / закрыть вкладку |
| `F3` / `F4` | Просмотр / редактирование (редактирование файлов внутри образа недоступно) |
| `F5` / `F6` | Копировать / переместить между панелями |
| `F7` / `F8` | Создать каталог / удалить локальные файлы |
| `F9` / `Ctrl+L` | Показать или скрыть меню / журнал окна |
| `Ctrl+R` / `Ctrl+O` | Обновить панель / открыть образ |

Отмеченные файлы можно копировать, перемещать и удалять группами. Каталоги копируются целиком; запись в образ выполняется движком FAT32. Настройки темы, размера шрифта и журнала доступны в **Конфигурация → Настройки**.

## Разработка и тесты

Запуск полного набора тестов:

```bash
python -m pytest tests/ -v
```

Тесты проверяют FAT32, пользовательский интерфейс, запуск приложения и обновление; для проверки FAT32 без `pytest`:

```bash
python tests/test_fat32.py
```

Основные модули:

```text
mount-file/
├── main.py
├── mountfile/
│   ├── qt_compat.py
│   ├── partition.py
│   ├── fat32.py
│   ├── vfs.py
│   ├── hexview.py
│   ├── updater.py
│   ├── mainwindow.py
│   ├── tc_window.py
│   └── tc_style.py
└── tests/
    ├── smoke_startup.py
    ├── test_fat32.py
    ├── test_tc_ui.py
    └── test_updater.py
```

## Ограничения

- Запись поддерживается только для **FAT32**. FAT12/16, exFAT и другие перечисленные файловые системы доступны для чтения через The Sleuth Kit.
- Области OOB/ECC в дампах eMMC/NAND не обрабатываются. Используйте образ пользовательского раздела без OOB.

## Лицензия

MIT. См. файл [LICENSE](LICENSE).

## Отказ от ответственности

Программа предоставляется «как есть». Всегда храните резервную копию образа, в который записываете данные.
