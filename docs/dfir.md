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
