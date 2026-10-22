# Установка

## Поддерживаемые варианты

Текущая версия пакета требует Python **3.10 или новее**. `requirements.txt`
устанавливает полный набор возможностей для Python 3.10+:

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
| `pytsk3` | обязательная Python-зависимость; нужен для NTFS/EXT и прочих ФС | сборка/колёса зависят от Python и платформы |
| `debugfs` из e2fsprogs | необязателен; нужен для записи в ext2/3/4 через F4/F5 | исполняемый файл должен быть в `PATH`; не поставляется вместе с приложением |
| ImDisk Virtual Disk Driver | необязателен; нужен для записи в NTFS/exFAT/FAT12/16-образы на Windows | установить отдельно; запуск MountFile от имени администратора; [сайт автора](https://ltr-data.se/opencode.html/) |
| `mcp[cli]`, `pydantic`, `openai` | нужны `mountfile_ai.py` и его MCP/OpenAI-совместимому слою | AI-сервер не нужен обычному CLI |
| `dosfstools`, `mtools` | только разработка | создание синтетических образов через `make sample` |
| `fastapi`, `uvicorn` | только DFIR REST-команда | нужны лишь для `mountfile_dfir.py serve` |

`requirements.txt` — полный профиль и включает optional AI/API зависимости.
Команда `python -m pip install .` устанавливает обязательные PyQt6 и `pytsk3`;
дополнительные возможности устанавливаются через `.[api]`, `.[ai]` или `.[dfir]`.

В репозитории сохранены `requirements-win7.txt` и отдельные legacy build
сценарии, но текущие метаданные пакета требуют Python 3.10+, а Windows 7 не
входит в поддерживаемую матрицу этой версии.

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

Поддержка Windows 7/Python 3.8 не входит в текущую версию: исходный пакет
объявляет `requires-python >=3.10`, а legacy-файл `requirements-win7.txt` не
делает текущий код совместимым с Python 3.8. Скрипт автоустановки намеренно
отказывает в `--win7`, чтобы не сообщать об успешной установке неработающей
сборки. Для текущей версии используйте Windows 10/11 и Python 3.10+.

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
