# Changelog

Release notes are generated from conventional commits by the
`create-release` workflow and attached to every GitHub Release, so this
file only tracks the releases themselves.

## Unreleased

- **Автообновление**: исправлен выбор Win7-сборки, когда generic `.exe` шёл
  первым в GitHub Release; добавлена поддержка GitHub asset `digest`, лимит
  размера скачивания 1 GiB и атомарная загрузка через `.part` с удалением
  повреждённых остатков после ошибки.

- **Сохранение из Блокнота**: текстовые файлы, открытые из образа через
  `Enter`/`F3`, можно импортировать обратно после сохранения и закрытия редактора
  с явным подтверждением в Mount File. Перед записью создаётся backup,
  используется текущий writer, кодировка и переводы строк исходного файла
  сохраняются.

- Добавлена read-only команда `python -m mountfile slack --slack-only`: она
  принимает подтверждённые вызывающим offset/размер последнего кластера,
  извлекает только slack и создаёт выходной файл без перезаписи существующего.
  Если slack нет, файл не создаётся, а JSON-сводка содержит флаг `created`;
  `docs/cli.md` и подсказка лаунчера приведены в соответствие.

- **RW capability matrix**: display filesystem names are normalized for loop
  mounts (`HFS+`/FAT variants), writable formats use the existing OS backend,
  and ISO9660/SquashFS/JFFS2/UBIFS now fail closed as explicit read-only
  formats instead of attempting a misleading RW mount.

- **Рефакторинг RW-слоя**: `mountfile_universal_rw.py`, `mountfile_rw_complete.py`
  и `mountfile_rw_v2.py` переехали в пакет `mountfile/rw/` (`universal.py`,
  `complete.py`, `v2.py`). В корне остались тонкие шимы с реэкспортом, поэтому
  `[project.scripts]`, внешние скрипты и тесты работают без изменений.
  Импортировать новый код следует из `mountfile.rw.*`.
- **CI**: удалён `dfir-quality.yml` — он дублировал `ci.yml`; macos-latest и
  Python 3.11 перенесены в матрицу `ci.yml`.
- **Документация**: в `docs/ai.md` зафиксировано разделение
  `ai_format_advisor` (только метаданные, используется GUI) и
  `ai_format_advisor_v2` (мульти-гипотезы + LLM, пока только тесты);
  в `docs/structure.md` добавлен раздел про устаревший обязательный чек
  `Tests (ubuntu-latest, py3.8)`.

- Total Commander mode: F4 now edits bounded text files and saves changes into
  writable FAT32 or ext2/3/4 volumes. ext writes use a temporary filesystem
  extent, read-back/SHA-256 verification, a rollback snapshot, and a persistent
  image backup; ext browse/write fallback requires `debugfs` when pytsk3 is
  absent. Existing image backups are not overwritten, and declining a backup
  cancels the write.

- **Поиск файлов в образе**: меню «Инструменты → Поиск файла в образе…» и
  `Ctrl+Shift+F` рекурсивно ищут имена/маски файлов во всех найденных томах,
  показывают путь, ФС, размер и offset, а двойной клик открывает каталог файла.
  Поиск ограничен 30 000 каталогами и 2 000 результатами, чтобы большие
  forensic-образы не зависали в интерфейсе.

- **Обзор образа целиком**: в меню «Инструменты» добавлена таблица всех
  найденных томов сразу с файловой системой, размером, offset/LBA и источником
  обнаружения. Двойной клик открывает выбранный том целиком в активной панели;
  одинаковые сигнатуры одного физического тома больше не дублируются в списке.

- **Non-FAT image writes in the GUI**: the Total Commander window now keeps
  native FAT32 writes and falls back to `UniversalFSRW` for ext2/3/4 via
  `debugfs`, NTFS via `ntfs-3g`, and supported loop-mounted filesystems.
  Import, mkdir, overwrite, rename, and delete use the same explicit backup
  confirmation. EXT4 `debugfs` listing and file rename are covered by new
  regression tests; unsupported system helpers remain read-only with a clear
  log message.

- **macOS: `$MOUNTFILE_EDITOR` / `$VISUAL` / `$EDITOR` снова учитываются**:
  в `text_editor.editor_command` ветка `open -t` проверялась раньше
  переменных окружения, поэтому на macOS явно заданный редактор
  игнорировался. Теперь переменные окружения проверяются первыми на всех
  POSIX-хостах, а `open -t` остаётся запасным вариантом. Это же
  исправляет три падавших джоба `Unit tests (…, macos-latest)`.

- **Обновление больше не «висит»**: скрипт подмены файла на Windows ждал
  завершения процесса бесконечным ``goto wait`` без счётчика — если процесс
  не завершался или ``tasklist`` его не находил, ожидание не заканчивалось
  никогда, а висящий консольный скрипт с проверкой PID было видно на экране
  (окно вида ``findstr /i "<pid>"``). Теперь ожидание ограничено
  ``WINDOWS_WAIT_TRIES`` проверок, затем файл всё равно подменяется, а
  ``move`` повторяется до ``WINDOWS_MOVE_TRIES`` раз; скрипт запускается с
  ``CREATE_NO_WINDOW`` вместо ``DETACHED_PROCESS`` (тот показывал окно
  консоли) и больше не содержит ``pause`` — при ошибке ничего не ждёт
  нажатия клавиши, результат пишется в ``%TEMP%\mountfile-update.log``.

- **Закрытие окна и индикатор обновления не блокируют GUI**: из
  ``closeEvent`` убран ``updater.wait(1500)`` (при скачивании обновления окно
  замирало на всё время загрузки) — поток больше не ожидается в потоке
  интерфейса, а ссылка на него переносится в модульный список, чтобы Qt не
  уничтожил работающий ``QThread``; из ``_on_update_installed`` убран
  ``wait_installer(3000)`` — окно закрывается сразу, чтобы освободить exe для
  ``move``. Добавлен сторож ``UPDATE_WATCHDOG_MS``: если этап обновления не
  завершился за 15 минут, индикатор «занят» сбрасывается сам и в журнал
  пишется путь к журналу установки.

- **Текстовые файлы открываются в Блокноте**: `Enter` / двойной щелчок и `F3`
  / `F4` больше не показывают `.txt` в HEX-просмотре — файл уходит в системный
  редактор (Windows: `notepad.exe`; macOS: `open -t`; Linux: `$MOUNTFILE_EDITOR`
  / `$VISUAL` / `$EDITOR`, иначе gedit/kate/xed/pluma/mousepad/leafpad/featherpad,
  иначе `xdg-open`). Новый модуль `mountfile/text_editor.py`; для файла внутри
  образа сначала пишется временная копия, поэтому образ не изменяется, а
  не-UTF-8 байты (CP1251 / CP866) перекодируются в UTF-8 с BOM, чтобы кириллица
  не превращалась в кракозябры. В контекстном меню появился пункт «Открыть в
  Блокноте» и отдельный «HEX-просмотр».

- **Package linking for EPIC #87**: `JournalV2` is now implemented in
  `mountfile.universal_fs_rw` with in-place legacy SQLite migration and
  byte-exact rollback; `mountfile_rw_v2.JournalV2` remains a compatibility
  alias. `UnallocatedExtractor` is exported from `mountfile.dfir` through a
  lazy legacy adapter, while bounded range scanning remains the canonical
  read-only package primitive. Added regression coverage for both public
  import paths and evidence immutability.

- **DFIR-модули эпика #87 закрыты отдельными пакетными модулями** (Tasks 1.1,
  1.2, 1.3, 1.4, 1.6, 2.2). Раньше функциональность жила только в
  standalone-скриптах и была недоступна как `python -m mountfile <команда>`:

  | Задача | Модуль | CLI |
  |---|---|---|
  | 1.1 Timeline | `mountfile/timeline.py` | `timeline-build`, `timeline-query` |
  | 1.2 Content search | `mountfile/search_content.py` | `search` |
  | 1.3 YARA | `mountfile/yara_scan.py` + `rules/` | `yara-scan` |
  | 1.4 Timestomp | `mountfile/timestomp.py` | `timestomp-scan` |
  | 1.6 Slack | `mountfile/slack.py` | `slack-extract` |
  | 2.2 Live diff | `mountfile/live_diff.py` | `live-diff` |

  Ключевое отличие от прежних черновиков: `slack.py` вычисляет **абсолютный
  offset** slack-области (хвост последнего выделенного run), а не только её
  длину, поэтому данные действительно можно прочитать; `yara_scan.py` умеет
  сканировать unallocated space как дополнение к карте занятости; `timeline.py`
  пишет SQLite + CSV со схемой `timestamp | source | path | event | sha256`.
  Все команды read-only, добавлены тесты `tests/test_dfir_tools_87.py` (45
  тестов) и встроенные правила `rules/pii.yar`, `rules/anti_forensics.yar`.

- **RW v2 (PR #122)**: `MountFileBridge` получил рабочий RO-путь — раньше он
  импортировал несуществующий `mountfile.main` и вызывал несуществующие
  ключи `mountfile --list/--extract`, поэтому на чистой установке всегда
  падал в `StrategyUnavailable`. Теперь чтение идёт через нативный движок
  FAT32 (`find_dir`/`list_dir`/`entry`/`read_file`), затем pytsk3. В
  `pyproject.toml` добавлен `py-modules` — без него консольные скрипты
  `mountfile-rw` / `mountfile-rw-v2` вообще не устанавливались. Ветка
  синхронизирована с исправлениями #121. Добавлены тесты
  `tests/test_rw_v2.py` (журнал v2, rollback, RO-мост).

- **RW-слой (PR #121) доведён до рабочего состояния**: `Fat32Adapter`
  переведён на реальный контракт `mountfile/fat32.py` (файловый объект,
  пары «каталог + имя», `entry()` возвращает `(parent, DirEntry)`);
  `DryRunMixin` действительно блокирует запись (исправлен порядок базовых
  классов — раньше `--dry-run` молча писал в образ); `create_rw_full`
  учитывает `dry_run`; `put` не пытается пересоздать существующий каталог;
  `get`/MCP-чтение больше не импортируют несуществующий `mountfile.main`.
  Добавлены сквозные тесты `tests/test_rw_layer.py`

- **Исправлены два дефекта записи FAT32**, из-за которых образ, который
  `WriteEngine.verify()` объявлял проверенным, `fsck.fat` отвергал как
  повреждённый (из-за этого падал `test_transaction_commit_writes_and_verifies`
  и весь CI-матрикс был красным):
  * `mountfile/universal_fs_rw.py` (`Fat32RW`) не обновлял сектор FSINFO —
    кэш числа свободных кластеров оставался на значении до записи
    («Free cluster summary wrong»). Добавлены `free_clusters()` и
    `sync_fsinfo()`, которые **пересчитывают** счётчики по FAT (а не
    инкрементируют) и вызываются из `write_file`/`mkdir`/`delete`; сектор
    с неверными сигнатурами не трогается вообще.
  * `mkdir` записывал в слот `..` каталога, созданного в корне, номер
    корневого кластера (2) вместо 0, как того требует спецификация Microsoft
    FAT («Invalid '..' entry in the second slot»).
  * Те же исправления внесены в `mountfile/fat32.py` (независимый движок
    FAT32), а синтетический фикстур `tests/test_fat32.py` дополнен до
    структурно полного тома: media-байт 0xF8, FSINFO, backup boot sector,
    extended boot signature и метка тома в BPB + корневом каталоге.
  * Регрессионный тест `test_written_image_leaves_fat_metadata_consistent`
    сверяет кэш FSINFO с пересчётом по FAT, проверяет `..` и (когда
    установлен `dosfstools`) требует `fsck.fat -n` с кодом 0.
- **MF-WE — транзакционный движок записи** (`mountfile/write_engine.py`,
  ТЗ MF-WE-TZ-001). Тонкий фасад над существующими RW-бэкендами
  (`mountfile/universal_fs_rw.py`), который добавляет обязательный по ТЗ §8
  слой: RO по умолчанию, авто-бэкап сессии и снапшот перед транзакцией,
  hash-chain журнал операций (`verify_journal()`), байт-в-байт откат транзакции
  (в том числе после имитированного краха), `verify()` после коммита с
  построчным отчётом «что реально проверено», `dry_run` без единой записи и
  отказ от RW-доступа к образу с признаками повреждения без явного
  `allow_damaged=True`. Собственный нативный движок NTFS/ext не пишется
  (ТЗ §15) — операции делегируются уже существующим бэкендам.
- Исправлены два дефекта в `mountfile/universal_fs_rw.py`, найденные при
  интеграции: `RWBackend.exists()` всегда возвращал `True`, потому что `stat()`
  отдаёт `None`, а не бросает исключение; `UniversalFSRW` не распознавал
  заголовок ФС по смещению 0, когда `detect_volumes()` не находил сигнатуру в
  окне сканирования (синтетические и маленькие образы), и сравнивал имена ФС
  от детектора (`FAT32`, `ext2/3/4`) с lowercase-таблицей без нормализации.
- `mountfile/write_engine.py` покрыт тестами `tests/test_write_engine.py`
  (31 тест: RO-гейт по каждой мутирующей операции, коммит/откат, dry-run,
  повреждённый образ, подделка журнала, запрет `..`/относительных путей,
  реальный ext4-образ через `mkfs.ext4` + `debugfs`).

- Epic #87 roadmap batch: experimental library modules for unallocated-range
  indexing, crypto/Android/macOS/NAND/Registry inspection, AI orchestration and
  metadata-only copilot summaries, MCP/RAG, plugins and the REST API. These
  modules do not turn the main GUI/CLI into a complete DFIR suite; see
  `docs/dfir.md` and `docs/api.md` for explicit boundaries.
- Documentation now has a strict MkDocs Material + mkdocstrings build deployed
  to GitHub Pages. The Docker workflow keeps build/smoke checks on CI and makes
  GHCR publication an explicit manual action. Added reproducible asciinema
  recordings for timeline, recovery triage and the metadata-only copilot.
- Added the package MCP stdio entry point `python -m mountfile.ai.cli mcp` and
  included byte offsets/size metadata in default partition-list results so
  clients can pass a discovered volume to subsequent MCP tools.
- The GUI Format Advisor now uses the deterministic metadata-only engine: it
  never reads or sends file bytes, does not call a model/network, and stores a
  selected recipe locally only after explicit user confirmation.
- The first-party YARA and timeline plugin examples now export `PLUGINS` and
  are covered by an explicit-discovery test; loading remains opt-in because
  discovering Python plugins imports executable code.
- Добавлен read-only package DFIR слой `mountfile.dfir`: bounded raw search
  (`grep`), SHA-256 identity (`hash`), импорт timeline CSV и консервативная
  проверка timestomp. Команды доступны через `python -m mountfile`; операции
  не изменяют исходный образ и ограничивают количество результатов.
- GUI получил контекстное меню правой кнопки, сводку образа/томов и предложение
  создать `<image>.bak` перед записью в FAT32.

- Кнопка «AI» на панели инструментов: диалог добавляет файл для анализа —
  выбранный в активной панели (в том числе **внутри образа**) или с диска —
  и показывает ответ модели; результат дублируется строкой в журнал окна.
  Новый слой `mountfile/ai/` (без Qt и без сети на импорте): `OllamaBackend`,
  `OpenAiCompatBackend`, `NullBackend` с понятным сообщением, чтение файла
  чанками через `collect()` с признаком усечения. В `qt_compat` добавлены
  `QTextEdit`, `DlgClose`, `DlgActionRole`, `EchoPassword`; настройки
  (`ai_backend`, `ai_base_url`, `ai_model`, `ai_api_key`, `ai_max_bytes`) —
  в диалоге «Настройки», ключ вводится как пароль и в журнал не пишется.
  Лимит чтения F3 вынесен в `PREVIEW_LIMIT`. Тесты — `tests/test_ai_backend.py`,
  `tests/test_ai_ui.py`, дополнен `tests/test_tc_ui.py` (кнопка AI в тулбаре
  и в меню «Инструменты»).

- Обновление программы: индикатор хода в строке состояния окна — этапы (скачивание → контрольная сумма → установка) текстом и полоса прогресса скачивания (проценты по Content-Length, режим «занят», если размер неизвестен; скрывается по завершении/ошибке). Исправлено падение `AttributeError` при подключении сигнала прогресса установщика: сигналы `progress`/`stage`/`failed` подняты на `StartupUpdater` (`mountfile/updater.py`), постоянные виджеты статуса — `MainWindow._build_update_status()` (`mountfile/tc_window.py`), `QProgressBar` добавлен в `mountfile/qt_compat.py`. Тесты — `tests/test_updater.py`.

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
