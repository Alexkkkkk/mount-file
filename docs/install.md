# Установка

## Поддерживаемые варианты

Проект требует Python **3.8 или новее**. Для обычной установки из текущего
репозитория используйте `requirements.txt`:

```bash
git clone https://github.com/Alexkkkkk/mount-file.git
cd mount-file
python -m venv .venv
source .venv/bin/activate       # Linux/macOS
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Запуск и диагностика:

```bash
python main.py --diag
python main.py path/to/image.img
python -m mountfile --help
```

`main.py --diag` показывает версию Python, доступный Qt, `pytsk3` и основные
импорты. В headless-среде этот шаг не заменяет проверку GUI: Qt может потребовать
дисплей или Xvfb.

## Зависимости и возможности

| Компонент | Статус | Назначение и ограничение |
|---|---|---|
| PyQt6 (в `requirements.txt` `>=6.11`) | основной для Win 10/11, Linux, macOS | GUI `main.py` |
| `pytsk3` | необязателен для встроенного FAT32; нужен для NTFS/EXT и прочих ФС | сборка/колёса зависят от Python и платформы |
| `debugfs` из e2fsprogs | необязателен; нужен для записи в ext2/3/4 через F4/F5 | исполняемый файл должен быть в `PATH`; не поставляется вместе с приложением |
| `mcp[cli]`, `pydantic`, `openai` | нужны `mountfile_ai.py` и его MCP/OpenAI-совместимому слою | AI-сервер не нужен обычному CLI |
| `dosfstools`, `mtools` | только разработка | создание синтетических образов через `make sample` |
| `fastapi`, `uvicorn` | только DFIR REST-команда | нужны лишь для `mountfile_dfir.py serve` |

`requirements.txt` содержит AI-пакеты как часть текущего набора зависимостей,
но соответствующие функции остаются отдельными: для обычного CLI не требуется
настраивать модель или MCP. Базовая конфигурация проекта (`pyproject.toml`)
также описывает PyQt6 и `pytsk3`; фактический режим без `pytsk3` поддерживается
кодом и использует только FAT32.

## Docker

Контейнер запускает только CLI от non-root пользователя. Для read-only проверки
образов используйте отдельные каталоги `evidence` и `output`:

```bash
mkdir -p evidence output
docker compose run --rm mountfile --version
docker compose run --rm mountfile info /evidence/dump.img
```

`evidence` монтируется read-only, а запись в исходный образ не должна
выполняться. Для дополнительного forensic-инструментария собирайте профиль
`docker build --build-arg INSTALL_FORENSIC_TOOLS=1 -t mountfile:forensics .`.
Этот профиль добавляет системные утилиты, но не превращает экспериментальные
DFIR-модули проекта в полноценную систему carving или malware verdict.

CI собирает образ и выполняет CLI smoke test на PR и push. Публикация в GHCR —
отдельный ручной запуск **Actions → Docker → Run workflow** с `publish=true`;
workflow сначала повторяет build/smoke и только затем делает push в
`ghcr.io/alexkkkkk/mount-file`. Доступ к `docker pull` зависит от visibility
container package в GitHub Packages; успешный build не означает, что image уже
публичен.

На Linux для Qt могут понадобиться системные библиотеки `libgl1`, `libegl1`,
`libxkbcommon-x11-0`, `libdbus-1-3`, `libxcb-cursor0`. Для Codespaces скрипт
`scripts/setup-gui.sh` дополнительно настраивает Xvfb, x11vnc и noVNC.

## Windows 7

Для Python 3.8.10 используйте отдельный файл:

```bat
py -3.8 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -r requirements-win7.txt
python main.py path\to\image.img
```

На Windows 7 применяется PyQt5. Колёса `pytsk3` для Windows начинаются с
Python 3.10, поэтому в Python 3.8 зависимость пропускается: встроенный FAT32
работает, а чтение NTFS через `pytsk3` недоступно. Для EXT предусмотрен
debugfs fallback, но он работает только при наличии совместимого `debugfs` в
`PATH`.

Если `debugfs` недоступен, EXT на Windows 7 остаётся только для чтения.
Приложение не устанавливает системные инструменты автоматически.

## Проверка образа перед работой

1. Сделайте копию и запишите SHA-256 исходника.
2. Запустите `python main.py --diag`.
3. Для образа сначала используйте read-only операции `info`, `volumes`, `ls`.
4. Если таблица разделов сомнительна, попробуйте `autopick` и сохраните его
   JSON-вывод.

Файлы `.exe` из GitHub Releases сопровождаются `.sha256`; проверяйте хэш до
запуска. Проверка обновлений при старте GUI не блокирует работу без сети. Её
можно отключить переменной `MOUNTFILE_NO_UPDATE=1`. Структурированная
документация публикуется по адресу
<https://alexkkkkk.github.io/mount-file/> после успешного Pages workflow.
