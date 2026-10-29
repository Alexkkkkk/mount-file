# Mount File

**Mount File** — локальный файловый менеджер для raw-образов дисков и дампов
NAND/eMMC. Он умеет читать таблицы MBR/GPT, находить тома и просматривать или
извлекать данные из поддерживаемых bounded-профилей FAT12/16/32, ext2/3/4,
exFAT и NTFS встроенными pure-Python readers. Ограниченная native-запись
доступна для FAT32, clean allowlisted ext2 и выбранного exFAT профиля.
Актуальный датированный статус и критерии этой работы ведутся в
[issue #182](https://github.com/Alexkkkkk/mount-file/issues/182).

Работа ведётся с файлом образа. Проект не прошивает исходное устройство и не
обещает восстановление повреждённых данных.

## Что где находится

| Задача | Точка входа | Что важно знать |
|---|---|---|
| Полный GUI и CLI | `main.py`, `python -m mountfile` | GUI требует Qt; CLI Qt не требует |
| Просмотр/извлечение | `mountfile/cli.py`, `mountfile/vfs.py` | native readers работают без `pytsk3` только для перечисленных bounded-профилей; legacy fallback — отдельный opt-in |
| Native-запись | `mountfile/universal_fs_rw.py` и профильные writers | ограниченные операции для FAT32, clean allowlisted ext2 и выбранного exFAT профиля; возможности зависят от backend/profile |
| Монтирование ОС | `mountfile.py` | отдельный инструмент, использует системный механизм mount |
| AI-слой | `mountfile/ai/`, `mountfile_ai.py` | бэкенд и сетевой вызов включаются явно |
| DFIR-утилиты | `mountfile_dfir.py` | grep по raw-файлу, timeline, timestomp, SHA-256 |

## Быстрый старт

```bash
python -m venv .venv
source .venv/bin/activate             # Linux/macOS
python -m pip install -r requirements.txt
python main.py --diag
python main.py path/to/image.img
```

Для Windows PowerShell активация выглядит так:

```powershell
.venv\Scripts\Activate.ps1
python main.py path\to\image.img
```

Подробности установки и ограничения платформ собраны в [Установке](install.md), команды —
в [CLI](cli.md), интерфейс — в [GUI](gui.md).
Экспериментальные библиотечные интерфейсы описаны в [API reference](api.md),
а сценарии запуска — в [Demos](demos.md).

## Безопасная модель работы

!!! warning "Образ — это данные доказательства"
    Перед `put`, `mkdir`, `mv` или `rm` создайте копию образа. Эти команды
    открывают его на запись; флаг `--yes` является обязательным подтверждением.
    `--backup` дополнительно создаёт `<image>.bak`, но не заменяет независимую
    резервную копию и проверку свободного места.

По умолчанию DFIR-слой и AI-модуль работают в режиме read-only. Снятие этой
защиты разрешает запись результатов извлечения во внешнюю рабочую папку, но
не делает восстановление гарантированным — хэшируйте исходный образ и
результаты отдельно.

## Ограничения

- Native-запись ограничена профилями FAT32, clean allowlisted ext2 и exFAT;
  FAT12/16, ext3/ext4 и NTFS остаются read-only в native-пути. Legacy/system
  writers — отдельные compatibility routes, а не native capability.
- Native readers не требуют `pytsk3` для bounded accepted-профилей FAT12/16/32,
  ext2/3/4, exFAT и NTFS. Это не полное покрытие каждого варианта формата:
  неизвестные, dirty, повреждённые или неподдержанные профили отклоняются.
  `pytsk3`/`libtsk` — optional opt-in fallback для нераспознанных форматов;
  распознанный unsupported профиль не передаётся ему автоматически.
- Сырые NAND-данные OOB/ECC не преобразуются; нужен образ пользовательской
  области без этих данных.
- AI не является антивирусом и не подтверждает выводы без результата
  проверяемого инструмента. Восстановление удалённых файлов не равно file
  carving и может вернуть неполный или повреждённый файл.

Документация опубликована на [GitHub Pages](https://alexkkkkk.github.io/mount-file/).
Workflow `docs.yml` собирает этот сайт через `mkdocs build --strict`; локальная
сборка выполняется командами из корня репозитория: `pip install -r
requirements-docs.txt` и `mkdocs build --strict`.
