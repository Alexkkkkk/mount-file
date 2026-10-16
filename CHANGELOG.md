# Changelog

Release notes are generated from conventional commits by the
`create-release` workflow and attached to every GitHub Release, so this
file only tracks the releases themselves.

## Unreleased

- Добавлен самостоятельный одностраничный инструмент `mountfile.py`: монтирование образов (`GUI` на PyQt6 + `CLI`), палитра команд `Ctrl+K`, значок в лотке, перетаскивание файлов, темы system/light/dark; атомарная запись состояния, ротация журналов, отчёт о падении, единственный экземпляр приложения. Платформенные механизмы: `udisksctl`/`mount -o loop` (Linux), `hdiutil` (macOS), `Mount-DiskImage` (Windows). Тесты — `tests/test_mountfile_standalone.py`, цель `make test-standalone`.

- GUI «Разбить дамп по udev…»: перед записью файлов в журнал выводится
  таблица партиций и запрашивается подтверждение («Извлечь партиции?»,
  по умолчанию «Нет»); при отказе файлы не создаются, после извлечения —
  построчный отчёт по каждой партиции. Общая функция `format_parts_table()`
  в `mountfile/rt809.py`.

- Дампы RT809H: расшифровка дампов eMMC MStar/MTK по XOR-таблицам и разбиение дампа
  на партиции по файлу udev — команды `decode` и `split` в CLI, пункты меню
  «Инструменты» в GUI, модуль `mountfile/rt809.py` (таблицы XOR встроены в
  `mountfile/data/`), тесты `tests/test_rt809.py`.

- Сборки Windows в релизе снова собираются и публикуются: проба
  `import pytsk3` в задании установки зависимостей возвращала ненулевой код
  и валила задания `exe (win7)` и `exe (universal)`, из-за чего `publish`
  не прикреплял бинарники; `requirements-win7.txt` больше не тянет
  `pytsk3` на Python 3.8 (колёс для Windows на 3.8 нет).
- `release.yml` получил вход `source_ref`: досборка уже выпущенного
  релиза идёт с актуального кода `main`, а номер версии по-прежнему
  берётся из тега — так наполняются релизы, вышедшие без вложений.
- Версия сборки штампуется из тега релиза (`scripts/stamp_version.py`),
  поэтому `.exe` больше не сообщает старый номер; `__version__` приведён к
  выпускаемой версии, ссылки на сборки в README переведены на
  `releases/latest`.
- Пустыми вышли также **v1.1.2 и v1.3.0** (ни одного вложения) — та же
  причина; `v1.3.0` собрался без бинарников, потому что шаг установки
  зависимостей падал на `pytsk3` до запуска PyInstaller.

- Интерфейс приведён к виду классического Total Commander: меню
  Файлы/Выделение/Команды/Сеть/Вид/Конфигурация/Запуск/Справка, панель
  инструментов с иконками, вкладки каталогов, строка диска со свободным
  местом, строка пути вида `c:\путь\*.*`, колонки
  Имя / Тип / Размер / Дата / Атрибуты, строка состояния у каждой панели
  и строка функциональных клавиш (F3 Просмотр … Alt+F4 Выход).
  Добавлены тёмная и светлая темы, настройки шрифта и журнала
  (`mountfile/tc_style.py`), отметка файлов Space/Insert и копирование
  группами, вкладки (Ctrl+T/Ctrl+W), смена панелей (Ctrl+U).
- Массовые операции: F5/F6/F8 работают с отмеченными файлами, копирование
  папок целиком (локально и в образ).
- Новое: `tests/test_tc_ui.py` — тест интерфейса на синтетическом образе
  (offscreen) плюс рендер скриншотов обеих тем.
- Fixed the startup crash on PyQt5 builds ("Unhandled exception in
  script" / `ImportError: cannot import name 'QAbstractScrollArea'`):
  the PyQt5 branch of `mountfile/qt_compat.py` exported only a handful
  of widgets, so `mountfile/hexview.py` could not import its names.
- `main.py`: `sys.stdin` is `None` in a windowed build, so the trailing
  `isatty` AttributeError replaced the real traceback with a useless
  dialog; the pause is now guarded and `--diag`/`--version`/
  `--check-updates` were added.
- Auto-update from GitHub Releases on every start (`mountfile/updater.py`).

- Full automation: version-bump, create-release, release, autofix,
  auto-merge, nightly, chatops, docs, dependency-review.

## v1.0.0

- Windows build (PyInstaller, one-file) attached to the GitHub Release.

## v0.2.0

- Release pipeline, Windows EXE attached to the release, test suite.

<<<<<<< HEAD
- Исправлено отображение панелей: колонка «Имя» больше не сжимается до
  «s..» — она тянется по ширине панели, а ширины колонок Тип/Размер/Дата/
  Атрибуты считаются из заголовка и самого длинного значения в них и
  уступают первыми при узком окне; заголовок «Атрибуты» больше не срезается
  по правому краю (было `ResizeToContents` на всех колонках).
- Колонка «Тип» перестала быть пустой для папок: каталог показывает «Папка»
  (в размере по-прежнему `<Папка>`), строка `..` тоже.
- Длинные имена обрезаются по центру (`elide middle`) — хвост
  `…_backup.bin` остаётся видимым, полное имя по-прежнему во всплывающей
  подсказке.
- Длинная подсказка клавиш больше не всплывает поверх обеих панелей: она
  стала короткой, а полный текст перенесён в строку состояния пустой панели.
- В строке состояния пустой панели вместо «0 Кб из 0 Кб» печатается
  подсказка клавиш; панель без образа — пустая строка.
- Стрелка направления сортировки в заголовке колонки, по которой идёт
  сортировка; размер иконок панели инструментов зафиксирован (18 px).
- `qt_compat`: добавлены отсутствовавшие общие имена (`QSize`, `AlignLeft`,
  `ElideMiddle`, `DescendingOrder`, `SizePolicyIgnored`,
  `TB_TextBesideIcon`) и enum-алиасы, из-за которых PyQt5-сборка для
  Windows 7 могла падать на `QSizePolicy.Policy` в `tc_window.py`.
- Исправлены сообщения: «Создана папка …» и список подтверждения
  копирования в образ собираются через `join()` и не теряют разделитель.
=======
>>>>>>> origin/main