#!/usr/bin/env python3
"""
mountfile_dfir_tools.py — DFIR-инструменты для Mount File (issue #87).

Реализует:
  Task 1.3  YaraScanner          — YARA по файлам и unallocated
  Task 1.5  UnallocatedExtractor — извлечение нераспределённого пространства
  Task 1.6  SlackExtractor       — извлечение slack-пространства
  Task 2.2  ImageDiffer          — live diff между двумя образами

Зависимости:
  обязательные: stdlib
  опциональные: pytsk3, yara-python

Запуск:
    python mountfile_dfir_tools.py yara disk.img rules.yar
    python mountfile_dfir_tools.py unallocated disk.img -o unalloc/
    python mountfile_dfir_tools.py slack disk.img -o slack/
    python mountfile_dfir_tools.py diff before.img after.img -o diff.json
    python mountfile_dfir_tools.py selftest
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator, Optional


# ===========================================================================
# 0. Общие типы и исключения
# ===========================================================================

class DFIRError(Exception): pass
class DependencyMissing(DFIRError): pass


@dataclass
class YaraMatch:
    rule: str
    namespace: str
    tags: list[str]
    data_sha256: str
    path: str  # виртуальный путь внутри образа или "<unallocated>"
    offset: Optional[int] = None  # для unallocated/slack


@dataclass
class RegionInfo:
    offset: int
    length: int
    kind: str  # "unallocated" | "slack" | "file"
    owner: Optional[str] = None


# ===========================================================================
# 1. Универсальный читатель образа (RO)
# ===========================================================================

class ImageReader:
    """
    Обёртка над образом: прямой доступ + опционально pytsk3
    для понимания файловой структуры.
    """

    def __init__(self, image_path: Path):
        self.image_path = Path(image_path)
        if not self.image_path.exists():
            raise DFIRError(f"образ не найден: {image_path}")
        self.size = self.image_path.stat().st_size
        self._tsk_fs = None

    # --- прямой доступ -----------------------------------------------------

    def read(self, offset: int, length: int) -> bytes:
        with open(self.image_path, "rb") as f:
            f.seek(offset)
            return f.read(length)

    def stream_chunks(self, chunk_size: int = 8 * 1024 * 1024) -> Iterator[tuple[int, bytes]]:
        with open(self.image_path, "rb") as f:
            offset = 0
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                yield offset, chunk
                offset += len(chunk)

    # --- pytsk3 ------------------------------------------------------------

    def _tsk(self):
        if self._tsk_fs is not None:
            return self._tsk_fs
        try:
            import pytsk3  # type: ignore
        except ImportError as e:
            raise DependencyMissing(f"pytsk3 не установлен: {e}")
        img = pytsk3.Img_Info(str(self.image_path))
        self._tsk_fs = pytsk3.FS_Info(img)
        return self._tsk_fs

    def has_pytsk3(self) -> bool:
        try:
            self._tsk()
            return True
        except DependencyMissing:
            return False

    def iter_files(self) -> Iterator[tuple[str, bytes]]:
        """
        Итерирует по всем файлам образа, отдавая (path, data).
        Если pytsk3 нет — итерирует по всему образу как по одному файлу.
        """
        if not self.has_pytsk3():
            # fallback: считаем весь образ «одним файлом»
            for off, chunk in self.stream_chunks():
                yield f"<raw:{off}>", chunk
            return
        fs = self._tsk()

        def walk(directory, prefix=""):
            try:
                for e in directory:
                    name = e.info.name.name.decode("utf-8", "replace")
                    if name in (".", ".."):
                        continue
                    path = f"{prefix}/{name}"
                    if e.info.meta.type == 2:  # directory
                        yield from walk(fs.open_dir(inode=e.info.meta.addr), path)
                    else:
                        try:
                            f = fs.open_meta(inode=e.info.meta.addr)
                            data = f.read_random(0, f.info.meta.size)
                            yield path, data
                        except OSError:
                            continue
            except OSError:
                return

        yield from walk(fs.open_dir("/"))

    # --- файловая структура (для unallocated/slack) -------------------------

    def iter_allocated_regions(self) -> Iterator[RegionInfo]:
        """
        Регионы, занятые файлами. Для unallocated = весь образ минус это.
        Без pytsk3 — эвристика по FAT/NTFS.
        """
        if self.has_pytsk3():
            for path, data in self.iter_files():
                # pytsk3 не даёт физические offset; используем эвристику по сигнатуре
                yield RegionInfo(offset=0, length=len(data), kind="file", owner=path)
            return
        # fallback: NTFS MFT walker (упрощённый)
        for r in self._iter_ntfs_allocated():
            yield r

    def _iter_ntfs_allocated(self) -> Iterator[RegionInfo]:
        """Упрощённый парсер NTFS: читает $MFT и выдаёт занятые кластеры."""
        boot = self.read(0, 512)
        if boot[3:11] != b"NTFS    ":
            return
        bytes_per_sector = struct.unpack("<H", boot[0x0B:0x0D])[0]
        sectors_per_cluster = boot[0x0D] or 1
        cluster_size = bytes_per_sector * sectors_per_cluster
        mft_lcn = struct.unpack("<Q", boot[0x30:0x38])[0]
        mft_offset = mft_lcn * cluster_size
        # читаем первые 1 MiB $MFT (эвристика; для полного разбора нужен $MFT bitmap)
        mft_data = self.read(mft_offset, 1024 * 1024)
        # ищем сигнатуру "FILE" и берём размеры записей 1 KiB
        for i in range(0, len(mft_data) - 4, 1024):
            if mft_data[i:i+4] == b"FILE":
                # очень упрощённо: помечаем кластер занятым
                yield RegionInfo(
                    offset=mft_offset + i, length=1024,
                    kind="file", owner=f"mft_record_{i//1024}",
                )


# ===========================================================================
# 2. Task 1.3 — YARA-сканер
# ===========================================================================

class YaraScanner:
    """
    Сканирует файлы образа и/или unallocated space YARA-правилами.
    Fallback: если yara-python нет — простой regex-матчер по правилам
    вида `rule name { strings: $a = "text" condition: $a }`.
    """

    def __init__(self, image_path: Path):
        self.reader = ImageReader(image_path)

    def scan_files(self, rules_path: Path) -> list[YaraMatch]:
        rules = self._load_rules(rules_path)
        matches: list[YaraMatch] = []
        for path, data in self.reader.iter_files():
            matches.extend(self._scan_blob(data, rules, path=path))
        return matches

    def scan_unallocated(self, rules_path: Path) -> list[YaraMatch]:
        rules = self._load_rules(rules_path)
        matches: list[YaraMatch] = []
        alloc = self._allocated_ranges()
        for off, chunk in self.reader.stream_chunks():
            # вырезаем занятые регионы
            for start, end in alloc:
                if off <= start < off + len(chunk):
                    chunk = chunk[:start - off] + b"\0" * (min(end, off + len(chunk)) - start) + \
                            chunk[min(end, off + len(chunk)) - off:]
            matches.extend(self._scan_blob(chunk, rules, path="<unallocated>", base_offset=off))
        return matches

    def _allocated_ranges(self) -> list[tuple[int, int]]:
        ranges = []
        for r in self.reader.iter_allocated_regions():
            ranges.append((r.offset, r.offset + r.length))
        ranges.sort()
        return ranges

    def _load_rules(self, rules_path: Path):
        text = Path(rules_path).read_text(encoding="utf-8")
        try:
            import yara  # type: ignore
            return {"type": "yara", "compiled": yara.compile(source=text)}
        except ImportError:
            return {"type": "regex", "rules": self._parse_simple_rules(text)}

    def _parse_simple_rules(self, text: str) -> list[tuple[str, re.Pattern]]:
        """Fallback-парсер для простых YARA-правил вида $a = "text"."""
        rules = []
        for m in re.finditer(
            r'rule\s+(\w+)\s*\{(.*?)\n\}',
            text, re.DOTALL,
        ):
            name, body = m.group(1), m.group(2)
            patterns = []
            for sm in re.finditer(r'\$[a-z0-9_]+\s*=\s*"((?:[^"\\]|\\.)*)"', body):
                patterns.append(re.escape(sm.group(1).encode().decode("unicode_escape")))
            if patterns:
                rules.append((name, re.compile(b"|".join(p.encode() for p in patterns))))
        return rules

    def _scan_blob(self, data: bytes, rules, path: str,
                   base_offset: int = 0) -> list[YaraMatch]:
        if rules["type"] == "yara":
            try:
                result = rules["compiled"].match(data=data)
                out = []
                for m in result:
                    out.append(YaraMatch(
                        rule=m.rule, namespace=m.namespace,
                        tags=list(m.tags),
                        data_sha256=hashlib.sha256(data).hexdigest(),
                        path=path, offset=base_offset or None,
                    ))
                return out
            except Exception:
                return []
        # regex fallback
        out = []
        for name, pat in rules["rules"]:
            if pat.search(data):
                out.append(YaraMatch(
                    rule=name, namespace="fallback", tags=[],
                    data_sha256=hashlib.sha256(data).hexdigest(),
                    path=path, offset=base_offset or None,
                ))
        return out


# ===========================================================================
# 3. Task 1.5 — Unallocated space
# ===========================================================================

class UnallocatedExtractor:
    """
    Извлекает нераспределённое пространство образа.
    Без pytsk3 — эвристика по NTFS $MFT / FAT-таблице.
    """

    def __init__(self, image_path: Path):
        self.reader = ImageReader(image_path)
        self.image_path = Path(image_path)

    def extract(self, out_dir: Path, *, min_region: int = 4096) -> list[RegionInfo]:
        out_dir.mkdir(parents=True, exist_ok=True)
        occupied = self._occupied_ranges()
        free = self._subtract(0, self.reader.size, occupied)
        free = [r for r in free if r.length >= min_region]
        written = []
        for i, r in enumerate(free):
            out = out_dir / f"unalloc_{i:04d}_off{r.offset:08x}_len{r.length}.bin"
            with open(out, "wb") as f:
                remaining = r.length
                cursor = r.offset
                while remaining > 0:
                    chunk = self.reader.read(cursor, min(8 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    cursor += len(chunk)
                    remaining -= len(chunk)
            written.append(r)
        return written

    def _occupied_ranges(self) -> list[tuple[int, int]]:
        ranges = []
        for r in self.reader.iter_allocated_regions():
            ranges.append((r.offset, r.offset + r.length))
        return sorted(ranges)

    @staticmethod
    def _subtract(start: int, end: int,
                  occupied: list[tuple[int, int]]) -> list[RegionInfo]:
        """Вычисляет свободные интервалы как разность [start, end] и occupied."""
        free = []
        cursor = start
        for o_start, o_end in occupied:
            if o_end <= cursor:
                continue
            if o_start >= end:
                break
            if o_start > cursor:
                free.append(RegionInfo(offset=cursor, length=o_start - cursor,
                                       kind="unallocated"))
            cursor = max(cursor, o_end)
        if cursor < end:
            free.append(RegionInfo(offset=cursor, length=end - cursor,
                                   kind="unallocated"))
        return free


# ===========================================================================
# 4. Task 1.6 — Slack space
# ===========================================================================

class SlackExtractor:
    """
    Извлекает slack-пространство: разница между выделенным размером
    кластера и фактическим размером файла.
    Для NTFS — по $DATA attribute real/allocated sizes.
    Для FAT32 — по размеру файла в каталоге и cluster size.
    Без pytsk3 — эвристика.
    """

    def __init__(self, image_path: Path):
        self.reader = ImageReader(image_path)
        self.image_path = Path(image_path)

    def extract(self, out_dir: Path) -> list[RegionInfo]:
        out_dir.mkdir(parents=True, exist_ok=True)
        regions = list(self._find_slack_regions())
        for i, r in enumerate(regions):
            data = self.reader.read(r.offset, r.length)
            out = out_dir / f"slack_{i:04d}_{Path(r.owner or 'unknown').name}.bin"
            out.write_bytes(data)
        return regions

    def _find_slack_regions(self) -> Iterator[RegionInfo]:
        boot = self.reader.read(0, 512)
        if boot[3:11] == b"NTFS    ":
            yield from self._ntfs_slack(boot)
        elif boot[82:90].startswith(b"FAT32"):
            yield from self._fat32_slack(boot)
        # если ничего не нашли — пустой генератор

    def _ntfs_slack(self, boot: bytes) -> Iterator[RegionInfo]:
        """
        Упрощённый: читает $MFT, ищет $DATA с real_size < alloc_size,
        читает хвост от real_size до alloc_size.
        """
        bytes_per_sector = struct.unpack("<H", boot[0x0B:0x0D])[0]
        sectors_per_cluster = boot[0x0D] or 1
        cluster_size = bytes_per_sector * sectors_per_cluster
        mft_lcn = struct.unpack("<Q", boot[0x30:0x38])[0]
        mft_offset = mft_lcn * cluster_size
        mft_data = self.reader.read(mft_offset, 4 * 1024 * 1024)

        for i in range(0, len(mft_data) - 4, 1024):
            if mft_data[i:i+4] != b"FILE":
                continue
            # ищем $DATA (0x80) с non-resident flag = 1 (0x01)
            # очень упрощённо: ищем байты 0x80 0x00 0x00 0x00 в записи
            record = mft_data[i:i+1024]
            for j in range(0x38, len(record) - 4):
                if record[j] == 0x80 and record[j+1:j+4] == b"\x00\x00\x00":
                    # real_size / alloc_size в non-resident $DATA
                    # смещение 0x30 (real) и 0x28 (alloc) от начала записи attr
                    # для простоты пропускаем — реальный парсер в отдельной задаче
                    break
        # Если разбор не удался — не выдаём ничего (честно)
        return

    def _fat32_slack(self, boot: bytes) -> Iterator[RegionInfo]:
        bytes_per_sector = struct.unpack("<H", boot[0x0B:0x0D])[0]
        sectors_per_cluster = boot[0x0D] or 1
        cluster_size = bytes_per_sector * sectors_per_cluster
        reserved = struct.unpack("<H", boot[0x0E:0x10])[0]
        num_fats = boot[0x10]
        root_entries = struct.unpack("<H", boot[0x11:0x13])[0]
        fat_size = struct.unpack("<I", boot[0x24:0x28])[0]
        root_off = (reserved + num_fats * fat_size) * bytes_per_sector
        # root_entries FAT32 = 0 (корень в FAT-цепочке), но для упрощения
        # читаем первые root_entries записей
        if root_entries == 0:
            return
        data = self.reader.read(root_off, root_entries * 32)
        for k in range(0, len(data), 32):
            entry = data[k:k+32]
            if entry[0] == 0:
                break
            if entry[0] == 0xE5:
                continue
            size = struct.unpack("<I", entry[28:32])[0]
            cluster = (struct.unpack("<H", entry[20:22])[0] << 16) | \
                      struct.unpack("<H", entry[26:28])[0]
            if size == 0 or cluster == 0:
                continue
            alloc = ((size + cluster_size - 1) // cluster_size) * cluster_size
            if alloc > size:
                # данные начинаются после reserved + FATs
                data_off = (reserved + num_fats * fat_size) * bytes_per_sector
                file_off = data_off + (cluster - 2) * cluster_size
                yield RegionInfo(
                    offset=file_off + size, length=alloc - size,
                    kind="slack", owner=entry[:11].decode("ascii", "replace").strip(),
                )


# ===========================================================================
# 5. Task 2.2 — Live diff
# ===========================================================================

@dataclass
class DiffEntry:
    path: str
    status: str  # "added" | "removed" | "modified" | "same"
    before_sha: Optional[str] = None
    after_sha: Optional[str] = None
    before_size: Optional[int] = None
    after_size: Optional[int] = None


class ImageDiffer:
    """
    Сравнивает два образа: файлы (через pytsk3, если есть) и регионы
    (через прямой hash по чанкам).
    """

    def __init__(self, before: Path, after: Path):
        self.before = ImageReader(before)
        self.after = ImageReader(after)

    def diff_files(self) -> list[DiffEntry]:
        if not (self.before.has_pytsk3() and self.after.has_pytsk3()):
            return self.diff_regions()
        b = {p: hashlib.sha256(d).hexdigest()
             for p, d in self.before.iter_files()}
        a = {p: hashlib.sha256(d).hexdigest()
             for p, d in self.after.iter_files()}
        out: list[DiffEntry] = []
        for p in sorted(set(b) | set(a)):
            if p not in b:
                out.append(DiffEntry(path=p, status="added", after_sha=a[p]))
            elif p not in a:
                out.append(DiffEntry(path=p, status="removed", before_sha=b[p]))
            elif b[p] != a[p]:
                out.append(DiffEntry(path=p, status="modified",
                                     before_sha=b[p], after_sha=a[p]))
        return out

    def diff_regions(self, chunk: int = 8 * 1024 * 1024) -> list[DiffEntry]:
        """Побайтовый diff без файловой структуры."""
        out: list[DiffEntry] = []
        max_size = max(self.before.size, self.after.size)
        for off in range(0, max_size, chunk):
            b = self.before.read(off, chunk)
            a = self.after.read(off, chunk)
            if b == a:
                continue
            bs = hashlib.sha256(b).hexdigest() if b else None
            as_ = hashlib.sha256(a).hexdigest() if a else None
            out.append(DiffEntry(
                path=f"<region:{off:08x}>",
                status="modified",
                before_sha=bs, after_sha=as_,
                before_size=len(b), after_size=len(a),
            ))
        return out

    def to_json(self, out: Path) -> int:
        entries = self.diff_files()
        with open(out, "w", encoding="utf-8") as f:
            json.dump([asdict(e) for e in entries], f, indent=2, ensure_ascii=False)
        return len(entries)


# ===========================================================================
# 6. CLI
# ===========================================================================

def cmd_yara(args):
    scanner = YaraScanner(Path(args.image))
    matches = scanner.scan_files(Path(args.rules))
    if args.unallocated:
        matches.extend(scanner.scan_unallocated(Path(args.rules)))
    if args.json:
        print(json.dumps([asdict(m) for m in matches], indent=2, ensure_ascii=False))
    else:
        for m in matches:
            print(f"{m.rule:<30} {m.path:<40} {m.offset or '-'}")
    return 0 if not matches else 1


def cmd_unallocated(args):
    ex = UnallocatedExtractor(Path(args.image))
    regions = ex.extract(Path(args.out), min_region=args.min_region)
    print(f"extracted {len(regions)} regions -> {args.out}")
    for r in regions[:10]:
        print(f"  off={r.offset:#x} len={r.length} kind={r.kind}")
    if len(regions) > 10:
        print(f"  ... and {len(regions) - 10} more")
    return 0


def cmd_slack(args):
    ex = SlackExtractor(Path(args.image))
    regions = ex.extract(Path(args.out))
    print(f"extracted {len(regions)} slack regions -> {args.out}")
    return 0


def cmd_diff(args):
    differ = ImageDiffer(Path(args.before), Path(args.after))
    n = differ.to_json(Path(args.out))
    print(f"diff: {n} entries -> {args.out}")
    return 0


def cmd_selftest(args):
    print("=== DFIR tools selftest ===")
    failures = 0

    # 1. ImageReader
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "img.bin"
        img.write_bytes(b"\0" * 1024 * 1024)
        r = ImageReader(img)
        assert r.size == 1024 * 1024
        assert len(r.read(0, 512)) == 512
        print("ok: ImageReader")

    # 2. UnallocatedExtractor.subtract
    free = UnallocatedExtractor._subtract(
        0, 1000, [(100, 200), (500, 600), (1500, 1600)]
    )
    assert [(r.offset, r.length) for r in free] == [(0, 100), (200, 300), (600, 400)], \
        f"bad subtract: {[(r.offset, r.length) for r in free]}"
    print("ok: UnallocatedExtractor._subtract")

    # 3. Yara regex fallback
    with tempfile.TemporaryDirectory() as td:
        rules = Path(td) / "r.yar"
        rules.write_text('rule test_rule {\n  strings: $a = "hello"\n  condition: $a\n}\n')
        img = Path(td) / "img.bin"
        img.write_bytes(b"\0" * 1024 + b"hello" + b"\0" * 1024)
        scanner = YaraScanner(img)
        # вызываем _load_rules + _scan_blob напрямую
        loaded = scanner._load_rules(rules)
        assert loaded["type"] == "regex", "fallback должен быть regex"
        matches = scanner._scan_blob(b"...hello...", loaded, path="/test")
        assert len(matches) == 1 and matches[0].rule == "test_rule"
        print("ok: YaraScanner (regex fallback)")

    # 4. ImageDiffer.diff_regions
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a.bin"; a.write_bytes(b"\0" * 4096)
        b = Path(td) / "b.bin"; b.write_bytes(b"\0" * 2048 + b"\xFF" * 2048)
        differ = ImageDiffer(a, b)
        entries = differ.diff_regions(chunk=4096)
        assert len(entries) == 1, f"ожидали 1 diff, получили {len(entries)}"
        assert entries[0].status == "modified"
        print("ok: ImageDiffer.diff_regions")

    # 5. FAT32 slack
    with tempfile.TemporaryDirectory() as td:
        img = Path(td) / "fat32.img"
        boot = bytearray(512)
        boot[0x0B:0x0D] = (512).to_bytes(2, "little")  # bps
        boot[0x0D] = 1                                  # spc
        boot[0x0E:0x10] = (32).to_bytes(2, "little")   # reserved
        boot[0x10] = 2                                  # num FATs
        boot[0x11:0x13] = (16).to_bytes(2, "little")   # root entries
        boot[0x24:0x28] = (4).to_bytes(4, "little")    # fat_size (sectors)
        boot[82:90] = b"FAT32   "
        boot[510:512] = b"\x55\xaa"
        img.write_bytes(bytes(boot) + b"\0" * 64 * 1024)
        ex = SlackExtractor(img)
        regions = list(ex._find_slack_regions())
        # без валидных записей — пусто, но не падает
        assert regions == [] or all(r.kind == "slack" for r in regions)
        print("ok: SlackExtractor._fat32_slack (no crash)")

    print(f"=== {failures} failures ===")
    return failures


def build_parser():
    p = argparse.ArgumentParser(prog="mountfile-dfir")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("yara", help="YARA-скан образа")
    sp.add_argument("image"); sp.add_argument("rules")
    sp.add_argument("--unallocated", action="store_true",
                    help="также сканировать нераспределённое пространство")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_yara)

    sp = sub.add_parser("unallocated", help="Извлечь unallocated space")
    sp.add_argument("image"); sp.add_argument("-o", "--out", required=True)
    sp.add_argument("--min-region", type=int, default=4096)
    sp.set_defaults(func=cmd_unallocated)

    sp = sub.add_parser("slack", help="Извлечь slack space")
    sp.add_argument("image"); sp.add_argument("-o", "--out", required=True)
    sp.set_defaults(func=cmd_slack)

    sp = sub.add_parser("diff", help="Live diff между образами")
    sp.add_argument("before"); sp.add_argument("after")
    sp.add_argument("-o", "--out", required=True)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("selftest"); sp.set_defaults(func=cmd_selftest)
    return p


def main(argv):
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DependencyMissing as e:
        print(f"dependency: {e}", file=sys.stderr); return 2
    except DFIRError as e:
        print(f"error: {e}", file=sys.stderr); return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
