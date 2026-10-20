# DFIR

DFIR-команды работают локально с копией образа. Перед анализом сохраните
исходный SHA-256:

```bash
python mountfile_dfir.py hash evidence.img > evidence.img.sha256
```

`mountfile_dfir.py` использует только стандартную библиотеку для `grep`,
SHA-256, timeline CSV → SQLite, простого timestomp-детектора и plugin registry.
REST-команда — отдельная опция и требует `fastapi` и `uvicorn`.

```bash
python mountfile_dfir.py grep evidence.img "powershell" --encoding utf-16le --json
python mountfile_dfir.py timeline timeline.csv --db timeline.db
python mountfile_dfir.py timeline timeline.csv --db timeline.db --query --limit 100
python mountfile_dfir.py timestomp timeline.csv --json
python mountfile_dfir.py serve --db timeline.db --host 127.0.0.1 --port 8000
```

`grep` ищет байтовую строку в mmap raw-файла, в том числе вне выделенных
файлов; это не файловый поиск и не антивирус. `timestomp` применяет простые
сравнения времён (`mtime`, `ctime`, `atime`, `birth`) и выдаёт эвристические
признаки.

## Explicit plugin discovery

Примеры `examples/plugins/yara.py` и `examples/plugins/timeline.py` экспортируют
`PLUGINS`. Импорт не регистрирует и не запускает плагины автоматически;
владелец процесса явно перечисляет доверенные файлы и открывает lifecycle:

```python
from mountfile.plugins import PluginRegistry

registry = PluginRegistry()
registry.discover(paths=[
    "examples/plugins/yara.py",
    "examples/plugins/timeline.py",
])
with registry.session("evidence.img", {"size": 4096}):
    registry.file({"path": "/note.txt", "data": b"example", "size": 7})
```

`discover(paths=...)` импортирует исполняемый Python-код, поэтому загружайте
только проверенные плагины. YARA-адаптер требует явно настроенных правил и
опциональной зависимости `yara-python`; плагины не получают изменяемый handle
на образ и не меняют поведение записи.

## Tutorial: PDF на FAT32

### Живой файл

Это безопасный read-only маршрут, если PDF присутствует в каталоге:

```bash
cp --reflink=auto evidence.img work.img
sha256sum work.img
python -m mountfile volumes work.img
python -m mountfile ls work.img / --auto
python -m mountfile get work.img /docs/report.pdf -o recovered --auto
sha256sum recovered/report.pdf
```

Параметр `--auto` можно заменить на известный `--offset 0x...`. `get` только
извлекает файл; если файл не найден, это не означает, что он был удалён.

### Удалённая запись через `pytsk3`

Встроенного file carving и отдельной DFIR-команды «восстановить PDF» нет. Для
записи удалённой мета-записи используется API `mountfile_ai.py`: он может
прочитать `UNALLOC`-записи, открыть inode и сохранить полученные байты во
внешнюю папку. Сначала найдите offset и inode в read-only режиме:

```python
# python recover_pdf.py; image — копия, не оригинал
import os
from pathlib import Path

os.environ["MOUNTFILE_READONLY"] = "1"
os.environ["MOUNTFILE_WORKDIR"] = "recovery-work"
import mountfile_ai as mf

image = Path("work.img").resolve()
mf.CONFIG.allow(image)
parts = mf.list_partitions(image)
fat = next(p for p in parts if "FAT" in (p.fs_type or "").upper())
hits = mf.search_files(image, fat.offset, r"\.pdf$", include_deleted=True)
for item in hits:
    print(item.inode, item.path, item.size, item.deleted)
```

Выберите подтверждённую удалённую запись, затем **в отдельном процессе**
снимите read-only только для записи результата во внешнюю папку:

```python
import os
from pathlib import Path

os.environ["MOUNTFILE_READONLY"] = "0"
os.environ["MOUNTFILE_WORKDIR"] = "recovery-work"
import mountfile_ai as mf

image = Path("work.img").resolve()
mf.CONFIG.allow(image)
result = mf.recover_deleted_file(
    image, offset=FAT_OFFSET, inode=PDF_INODE,
    output=Path("recovery-work/recovered.pdf"),
)
print(result.model_dump())
```

Замените `FAT_OFFSET` и `PDF_INODE` значениями из первого запуска. Требуются
`pydantic` и `pytsk3`; на Windows 7/Python 3.8 `pytsk3` обычно недоступен.
Метод читает inode, не карвит фрагменты и не гарантирует целостность PDF.
`complete=true` означает равенство переданному `original_size` (или размеру
мета-записи), а не успешное открытие PDF. Сохраните audit JSONL и отдельно
проверьте тип файла, размер и SHA-256.

## Tutorial: анализ Android boot image

В `mountfile.android` есть read-only parser API для Android sparse image и
boot-image headers/chunks. Эти библиотечные функции не распаковывают ramdisk,
не проверяют AVB и не прошивают устройство; они пока не подключены к основному
CLI/GUI. Для поиска сигнатур можно также выполнить:

```bash
python mountfile_dfir.py hash boot.img > boot.img.sha256
python mountfile_ai.py inspect --image boot.img
python -m mountfile info boot.img
```

`inspect` смотрит сигнатуры в первых 1 MiB и пытается прочитать таблицу томов
через `pytsk3`, если она есть. Отсутствие результата не доказывает отсутствие
Android-разделов: boot image может быть контейнером с заголовком и сжатым
ramdisk. Пример parser API для структурированных заголовков приведён в
[API reference](api.md).

Для отдельного извлечения используйте внешний инструмент, установленный и
проверенный по его документации, например `unpackbootimg` или `magiskboot`:

```bash
unpackbootimg -i boot.img -o boot-unpacked   # внешний инструмент, optional
# затем анализируйте извлечённые файлы штатными локальными средствами
sha256sum boot.img boot-unpacked/*
```

Эти программы не входят в зависимости Mount File. Mount File не подтверждает
формат, AVB-цепочку, подпись, адрес загрузки или пригодность образа для
прошивки; не записывайте результат обратно в устройство без отдельной
процедуры производителя.

## Tutorial: поиск malware в NTFS

Для имён и метаданных NTFS требуется `pytsk3`. Сначала осмотрите раздел и
подберите offset без записи:

```bash
python -m pip install pytsk3
python -m mountfile volumes evidence.img
python -m mountfile autopick evidence.img --json > candidates.json
python -m mountfile ls evidence.img / --offset 0xNTFS_OFFSET
```

Для рекурсивного поиска имён используйте read-only функции `mountfile_ai.py`:

```python
import os
from pathlib import Path

os.environ["MOUNTFILE_READONLY"] = "1"
import mountfile_ai as mf

image = Path("evidence.img").resolve()
mf.CONFIG.allow(image)
for part in mf.list_partitions(image):
    if "NTFS" not in (part.fs_type or "").upper():
        continue
    hits = mf.search_files(
        image, part.offset,
        r"\.(exe|dll|sys|ps1|vbs|js|scr|lnk)$",
        include_deleted=True,
    )
    for hit in hits:
        print(hit.model_dump())
```

Для содержимого, включая невыделенные области, добавьте независимый raw-поиск:

```bash
python mountfile_dfir.py grep evidence.img "powershell" --encoding utf-16le --json
python mountfile_dfir.py grep evidence.img "http://" --encoding latin-1 --max-hits 200
```

Это triage, а не вердикт о вредоносности: совпадение имени или строки не
доказывает запуск malware, а отсутствие совпадения не доказывает чистоту.
Проверяйте временную шкалу, подпись, хэш и контекст отдельными инструментами.
`include_deleted=True` видит удалённые записи, доступные через метаданные; он
не выполняет carving и может не обойти удалённые каталоги.

## Read-only библиотечные API

Дополнительно доступны composable Python API. Они пока не являются командами
`mountfile_dfir.py` и не подключены к GUI. Примеры ниже работают с явно
переданными метаданными, диапазонами или образом только для чтения; они не
определяют allocation map и не доказывают наличие malware или timestomping.

### Timeline и timestamp triage

Нормализатор принимает caller-supplied records и переводит даты в UTC. CSV
экспорт открывает только явно заданный выходной файл:

```python
from mountfile.dfir_timeline import normalize_timeline, export_timeline_csv
from mountfile.dfir_timestomp import TimestampRecord, analyze_timestomp

records = [
    TimestampRecord(
        path="/evidence/a.bin",
        created=1_700_000_000,
        modified=1_699_999_000,
        accessed=1_700_000_100,
        record_id="inode-17",
    )
]
events = normalize_timeline(records)
export_timeline_csv(records, "timeline-normalized.csv")
for finding in analyze_timestomp(records):
    print(finding.as_dict())
```

Порядок времён — эвристика: файловые системы и collectors могут сообщать
семантически разные `ctime`/creation times и иметь отличающуюся точность.
Boundary/range alerts по умолчанию отключены и требуют явных thresholds.

### YARA и сигнатуры в переданных unallocated ranges

YARA доступен через optional `yara-python`; он не нужен для импорта остальных
DFIR модулей. Правила поставляет пользователь. В package CLI доступны
`yara` и `unallocated-scan`; последний читает JSON-файл с явными
`[offset, length]` ranges:

```bash
python -m pip install yara-python  # optional, только для YARA
python -m mountfile yara evidence.img --rules user-rules.yar
printf '[[1048576, 2097152]]\n' > ranges.json
python -m mountfile unallocated-scan evidence.img --ranges ranges.json
```

Чтобы выгрузить нормализованный UTC timeline CSV из метаданных существующего
collector CSV:

```bash
python -m mountfile timeline-export \
  --csv collector.csv --output normalized-timeline.csv
```

Правила и ranges не скачиваются автоматически; сканеры не определяют
allocation map. Примеры Python API:

```python
from mountfile.dfir_yara import scan_yara
from mountfile.dfir_unallocated_scan import scan_unallocated

hits = scan_yara(
    "evidence.img",
    'rule marker { strings: $s = "powershell" wide condition: $s }',
)
for hit in hits:
    print(hit["rule"], hit["offsets"])

# Укажите предварительно известные half-open byte ranges (offset, length).
for hit in scan_unallocated("evidence.img", [(0x100000, 0x200000)]):
    print(hit.offset, hit.signature)
```

Сигнатурный поиск читает диапазоны bounded chunks и возвращает только magic
prefix + абсолютный offset. Это triage, не полноценная проверка формата или
carving; allocation ranges должны быть найдены вызывающей стороной.

### File slack при уже известном extent

Хелпер читает slack последнего кластера только если вызывающий код уже знает
его абсолютный offset; он не разрешает путь в inode/cluster chain:

```python
from mountfile.dfir_slack import read_file_slack

result = read_file_slack(
    "evidence.img",
    last_cluster_offset=0x400000,
    file_size=5000,
    cluster_size=4096,
)
print(result.offset, len(result.bytes))
```

Для пустого или выровненного по границе кластера файла результат пустой и
`offset` равен `None`. Проверяйте путь, cluster chain и offset независимо до
вызова; slack может содержать остатки прежних данных.

---

## Пакетные DFIR-команды (issue #87)

Помимо standalone-скриптов, те же возможности доступны через пакетный CLI
(`python -m mountfile <команда>`). Все команды работают **read-only** и никогда
не изменяют образ-источник.

### Timeline (Task 1.1)

```bash
python -m mountfile timeline-build evidence.img -o timeline.db --csv timeline.csv
python -m mountfile timeline-query --db timeline.db \
    --start 2024-03-12T14:00 --end 2024-03-12T15:00 --event mtime --json
```

Схема: `timestamp | source | path | event | sha256`. События формируются по
четырём меткам `pytsk3` (`created`, `mtime`, `atime`, `ctime`); `timeline-query`
отвечает на вопрос «что происходило в окне …» — это хук для AI-интеграции.
Разбор `$STANDARD_INFORMATION` против `$FILE_NAME` требует отдельного MFT-парсера
и в эту команду не входит.

### Content search (Task 1.2)

```bash
python -m mountfile search evidence.img "password=" --json
python -m mountfile search evidence.img "powershell" --encoding utf-16le
python -m mountfile search evidence.img "id=\d+" --regex
python -m mountfile search evidence.img "AKIA" --files   # по содержимому файлов
```

Без `--files` поиск идёт потоково по сырым байтам с перекрытием чанков (находит
строки в unallocated и slack), с `--files` — по содержимому каждого файла с
указанием пути.

### YARA (Task 1.3)

```bash
python -m mountfile yara-scan evidence.img --rules rules/ --json
python -m mountfile yara-scan evidence.img --rules rules/pii.yar --targets unallocated
```

`--rules` принимает как один файл, так и каталог. Встроенные правила:
`rules/pii.yar` (карты, IBAN, SSN, приватные ключи) и
`rules/anti_forensics.yar` (CCleaner, timestomp-утилиты, очистка журналов,
secure-delete). Требуется `pip install yara-python`.

### Timestomp (Task 1.4)

```bash
python -m mountfile timestomp-scan evidence.img --skew 2.0 --json
```

Четыре независимых правила с причиной в отчёте: timestamp в будущем,
`mtime == atime == ctime`, нулевые наносекунды и `$SI.mtime < $FN.mtime`
(последнее — только если вызывающий код передал пару значений).

### Slack (Task 1.6)

```bash
python -m mountfile slack-extract evidence.img -o slack/
```

Slack-область каждого файла выгружается в отдельный `.bin`. Offset считается
абсолютным: `slack_end = (last_run.addr + last_run.len) * block_size`, поэтому
данные читаются из правильного места образа.

### Live diff (Task 2.2)

```bash
python -m mountfile live-diff before.img after.img -o diff.md
```

Отчёт Markdown: `added` / `modified` / `removed` со размерами и SHA-256.
