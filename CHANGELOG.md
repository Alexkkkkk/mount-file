# Changelog

Release notes are generated from conventional commits by the
`create-release` workflow and attached to every GitHub Release, so this
file only tracks the releases themselves.

## Unreleased

- Сборки Windows в релизе снова собираются и публикуются: проба
  `import pytsk3` в задании установки зависимостей возвращала ненулевой код
  и валила задания `exe (win7)` и `exe (universal)`, из-за чего `publish`
  не прикреплял бинарники; `requirements-win7.txt` больше не тянет
  `pytsk3` на Python 3.8 (колёс для Windows на 3.8 нет).
- Версия сборки штампуется из тега релиза (`scripts/stamp_version.py`),
  поэтому `.exe` больше не сообщает старый номер; `__version__` приведён к
  выпускаемой версии, ссылки на сборки в README переведены на
  `releases/latest`.

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
